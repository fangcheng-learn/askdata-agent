"""Protocol integration: schemas, real readonly execution and scope counterexamples."""
from copy import deepcopy
import unittest

from app.structured_output import using_output_schema, current_output_schema, schema_response_format
from app.workflows.investigation_output import output_schema
from app.querying.request_constraints import validate_request_constraints
from app.workflows.react_harness import ReactRecord
from app.runtime_budget import RunBudget
from tests import test_sql_batch_controller as batch
from tests.test_react_harness import action, review
from app.querying.data_qa_agent import DataQaAgent


class ProtocolIntegrationV4Test(unittest.TestCase):
    def test_provider_must_distinguish_registered_values_from_exploration(self):
        query=output_schema('react')['$defs']['BusinessQuery']
        self.assertIn('execution_mode',query['required'])
        self.assertEqual(set(query['properties']['execution_mode']['enum']),{'registered_metrics','exploratory'})

    def test_provider_conditions_have_closed_keys_and_typed_field_values(self):
        from jsonschema import Draft202012Validator
        schema=output_schema('react')
        query_schema={'$defs':schema['$defs'],**schema['$defs']['BusinessQuery']}
        validator=Draft202012Validator(query_schema)
        base={'goal':'获取登记指标','execution_mode':'registered_metrics'}
        for conditions in [{},{'months':['2026-06'],'filters':[
                {'field':'category_code','operator':'in','values':['POWER']}]},
                {'time_range':{'start':'2026-06-01','end':'2026-08-01','end_inclusive':False}}]:
            self.assertEqual(list(validator.iter_errors({**base,'conditions':conditions})),[])
        for conditions in [{"months': ['2026-06']":None},{'months':None},
                {'category_code':'POWER'}, {'time_range':None}, {'months':[]},
                {'filters':[{'field':'category_code','values':[None]}]},
                {'filters':[{'field':'category_code','values':['POWER'],'sql':'ignored'}]}]:
            with self.subTest(conditions=conditions):
                self.assertTrue(list(validator.iter_errors({**base,'conditions':conditions})))

    def test_nested_schema_resets_on_failure_and_transport_does_not_mutate_schema(self):
        with using_output_schema('outer', {'type': 'object'}):
            original = deepcopy(current_output_schema.get())
            schema_response_format()['json_schema']['schema']['type'] = 'array'
            self.assertEqual(current_output_schema.get(), original)
            with self.assertRaises(RuntimeError):
                with using_output_schema('inner', {'type': 'string'}):
                    self.assertEqual(current_output_schema.get()['name'], 'inner')
                    raise RuntimeError('local')
            self.assertEqual(current_output_schema.get(), original)
        self.assertIsNone(current_output_schema.get())

    def test_loaded_skill_unavailable_in_next_decision_schema(self):
        schema = output_schema('react', skill_names=[])
        self.assertNotIn({'$ref': '#/$defs/LoadSkill'}, schema['properties']['action']['anyOf'])
        self.assertEqual(output_schema('react', skill_names=['goal_analysis'])['$defs']['LoadSkill']
                         ['properties']['names']['items']['enum'], ['goal_analysis'])

    def test_new_budget_eight_old_saved_budget_five(self):
        self.assertEqual(RunBudget('new').react_round_limit, 8)
        self.assertEqual(RunBudget.from_state('old', {}).react_round_limit, 5)

    def test_resolved_scope_and_or_counterexamples(self):
        graph = {'tables': [{'id': 't', 'name': 'items'}], 'fields': [
            {'table_id': 't', 'name': 'category'}, {'table_id': 't', 'name': 'amount'}]}
        resolution = [{'query_id': 'A1', 'kind': 'entity', 'status': 'resolved',
                       'canonical_field': 'items.category', 'canonical_value': 'POWER'}]
        cases = [("SELECT SUM(amount) FROM items WHERE category='POWER'", True, True),
                 ("WITH a AS (SELECT * FROM items WHERE category='POWER') SELECT SUM(amount) FROM a", True, True),
                 ("SELECT SUM(amount) FROM items WHERE category='LIGHT'", False, True),
                 ("SELECT SUM(amount) FROM items", False, True),
                 ("SELECT SUM(amount) FROM items WHERE category='POWER' OR amount>0", True, False),
                 ("SELECT SUM(amount) FROM items WHERE category IN ('POWER','LIGHT')", False, True)]
        for sql, passed, complete in cases:
            with self.subTest(sql=sql):
                checked = validate_request_constraints(sql=sql, query_contract={}, schema_graph=graph,
                    entity_resolutions=resolution, query_id='A1')
                self.assertEqual(checked['passed'], passed)
                self.assertEqual(checked['complete'], complete)

    def test_full_business_sql_boundary_uses_v4_and_reuses_skill(self):
        fixture = batch.SqlBatchControllerTest(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        h, model, state = fixture.new_run([
            action('load_skill', names=['business_diagnosis']),
            action('load_skill', names=['business_diagnosis']),
            batch.business(('核实GMV', 'gmv')),
            {'queries': [{'query_id': 'A3', 'status': 'query',
                'sql': 'SELECT SUM(gmv) AS value FROM daily_store_metrics',
                'metric_declarations': [{'name': 'gmv', 'source_fields': ['daily_store_metrics.gmv'],
                                         'aggregation': 'sum'}]}]},
            action('deliver'), {'title': '经营调查报告', 'markdown': '已取得GMV [T1:R1]'},
            review(question_assessments=[{'question_id': 'Q1', 'status': 'answered',
                'evidence_ids': ['T1:R1'], 'answer_level': 'fact', 'reason': '适用完整事实'}],
                explanation_coverage_ack=True),
        ])
        state['investigation_protocol_version'] = 4
        selected = []
        original = model.chat_json
        def capture(system, user):
            selected.append(deepcopy(current_output_schema.get()))
            return original(system, user)
        model.chat_json = capture
        result, budget = fixture.run_bounded(h, state)
        self.assertTrue(result.evidence, result.stop_reason)
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(budget.tools, 2)
        self.assertEqual([item['name'] for item in selected], ['investigation_react'] * 3 +
                         ['sql_batch', 'investigation_react', 'investigation_draft', 'investigation_delivery_review'])
        sql_context = model.calls[3][1]
        self.assertEqual(sql_context['execution_protocol_version'], 4)
        self.assertNotIn('query_contract_schema', sql_context['query_protocol'])
        self.assertTrue(h.records['T1'].actions[1]['reused'])


if __name__ == '__main__':
    unittest.main()
