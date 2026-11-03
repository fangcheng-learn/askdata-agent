"""SQL output shapes, clarification adapters and schema transport; no paid calls."""
from copy import deepcopy
import json
import unittest

from app.errors import ModelOutputError
from app.models import Clarification, ClarificationOption
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.sql_module import SqlQueryModule
from app.querying.sql_output_protocol import (SqlBatchOutput, normalize_clarification,
                                               sql_batch_output_schema)
from app.skills import SkillRegistry
from app.structured_output import current_output_schema, schema_response_format


def query(qid='Q1'):
    return {'query_id': qid, 'status': 'query',
            'sql': 'SELECT SUM(gmv) AS gmv FROM daily_store_metrics',
            'metric_declarations': [{'name': 'gmv', 'output_columns':['gmv'],
                                     'source_fields': ['daily_store_metrics.gmv'],
                                     'aggregation': 'sum'}]}


class CaptureModel:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def chat_json(self, system, user):
        self.calls.append({'system': system, 'payload': json.loads(user),
                           'selected_schema': deepcopy(current_output_schema.get()),
                           'response_format': schema_response_format()})
        if not self.values:
            raise AssertionError('SQL module made an unreserved extra request')
        response = self.values.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class SqlOutputProtocolV4Test(unittest.TestCase):
    def module(self, responses):
        model = CaptureModel(responses)
        return SqlQueryModule(model, lambda scope: None, SkillRegistry().get('database_query'),
                              DuckDbEngine()), model

    def generate(self, response, *, ids=('Q1',), context=None):
        module, model = self.module([response])
        result = module.generate_batch([{'query_id': qid, 'goal': '取得GMV'} for qid in ids],
            {'execution_protocol_version': 4, **(context or {})})
        return result, model

    def test_query_can_declare_a_typed_calculation_without_choosing_other_result(self):
        from tests.test_state_duration import request
        plan = {**request(), 'result_id': 'query_result'}
        result, model = self.generate({'queries': [{**query(), 'postprocess': plan}]})
        self.assertEqual(result['candidates']['Q1']['postprocess']['operation'], 'state_duration')
        self.assertEqual(len(model.calls), 1)
        rejected, _ = self.generate({'queries': [{**query(), 'postprocess': {**plan, 'result_id': 'OTHER:R1'}}]})
        self.assertFalse(rejected['candidates'])
        self.assertEqual(rejected['errors']['Q1']['kind'], 'output_format')

    def test_generated_schema_reuses_real_models_and_binds_server_ids(self):
        schema = sql_batch_output_schema(['Q1', 'Q2'])
        definitions = schema['$defs']
        success = definitions['SqlQueryOutput']
        self.assertEqual(success['properties']['query_id']['enum'], ['Q1', 'Q2'])
        self.assertEqual(success['additionalProperties'], False)
        self.assertNotIn('query_contract', success['properties'])
        self.assertNotIn('time_range', success['properties'])
        self.assertEqual(definitions['ClarificationOption'], ClarificationOption.model_json_schema())
        self.assertEqual(set(definitions['BusinessClarification']['properties']),
                         {'parameter', 'question', 'reason', 'options', 'category', 'required', 'allow_skip'})
        self.assertEqual(set(definitions['SqlMetricDeclaration']['properties']),
                         {'name', 'definition', 'source_fields', 'aggregation', 'output_columns'})
        self.assertEqual(set(definitions['BusinessClarification']['properties']['category']['enum']),
                         {'business_parameter', 'metric_definition', 'entity_identity'})
        SqlBatchOutput.model_validate({'queries': [query()]})
        for ids in ([], ['Q1', 'Q1'], ['']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                sql_batch_output_schema(ids)

    def test_v4_passes_strict_schema_without_new_model_kwargs_or_retries(self):
        result, model = self.generate({'queries': [query()]})
        self.assertEqual(len(model.calls), 1)
        selected = model.calls[0]['selected_schema']
        self.assertEqual(selected['name'], 'sql_batch')
        self.assertEqual(model.calls[0]['response_format']['type'], 'json_schema')
        self.assertTrue(model.calls[0]['response_format']['json_schema']['strict'])
        self.assertEqual(selected['schema'], model.calls[0]['payload']['query_protocol']['output_schema'])
        self.assertNotIn('query_contract_schema', model.calls[0]['payload']['query_protocol'])
        self.assertEqual(set(result['candidates']), {'Q1'})
        self.assertEqual(set(result['candidates']['Q1']['query_contract']), {'metrics'})
        self.assertEqual(result['candidates']['Q1']['query_contract']['metrics'][0]['name'], 'gmv')
        self.assertIsNone(current_output_schema.get())

    def test_model_callback_observes_schema_context_too(self):
        module, model = self.module([])
        captures = []

        def callback(node, system, payload):
            captures.append((node, current_output_schema.get(), payload))
            return {'queries': [query()]}

        result = module.generate_batch([{'query_id': 'Q1'}], {'execution_protocol_version': 4},
                                       model_call=callback)
        self.assertEqual(captures[0][0], 'query_coder')
        self.assertEqual(captures[0][1]['name'], 'sql_batch')
        self.assertEqual(model.calls, [])
        self.assertTrue(result['candidates'])
        self.assertIsNone(current_output_schema.get())

    def test_valid_sibling_survives_bad_business_declaration(self):
        invalid = query('Q2')
        invalid['metric_declarations'][0]['source_fields'] = []
        result, model = self.generate({'queries': [query(), invalid]}, ids=('Q1', 'Q2'))
        self.assertEqual(set(result['candidates']), {'Q1'})
        self.assertEqual(result['errors']['Q2']['kind'], 'output_format')
        self.assertIn('source_fields', result['errors']['Q2']['message'])
        self.assertEqual(len(model.calls), 1)

    def test_two_model_authored_business_contracts_cannot_conflict(self):
        value = query()
        value['query_contract'] = {'metrics': [{'name': 'different'}]}
        result, _ = self.generate({'queries': [value]})
        self.assertFalse(result['candidates'])
        self.assertIn('不能同时', result['errors']['Q1']['message'])

    def test_legacy_option_strings_and_partial_objects_normalize_stably(self):
        detail = {'category': 'metric_definition', 'question': '按哪类会话统计？',
                  'options': ['落地会话', {'label': '浏览会话', 'recommended': True}]}
        first = normalize_clarification(detail)
        self.assertEqual(first, normalize_clarification(detail))
        self.assertEqual(set(first), {'parameter', 'question', 'reason', 'options', 'required', 'allow_skip'})
        self.assertEqual([value['label'] for value in first['options']], ['落地会话', '浏览会话'])
        self.assertEqual(len(set(value['id'] for value in first['options'])), 2)
        self.assertTrue(first['options'][1]['recommended'])
        Clarification.model_validate(first)
        self.assertEqual(detail['options'][0], '落地会话')

    def test_business_clarification_normalized_before_controller(self):
        result, _ = self.generate({'queries': [{'query_id': 'Q1', 'status': 'clarify',
            'clarification': {'category': 'entity_identity', 'question': '你指哪家店？',
                              'options': ['HomeNova Berlin', {'label': 'HomeNova Hamburg'}]}}]})
        error = result['errors']['Q1']
        self.assertEqual(error['kind'], 'clarification')
        self.assertEqual(error['clarification_category'], 'entity_identity')
        parsed = Clarification.model_validate(error['clarification'])
        self.assertEqual(len(parsed.options), 2)

    def test_v4_requires_business_category_and_bad_options_stay_local_errors(self):
        for detail in ({'question': '哪家店？'},
                       {'category': 'entity_identity', 'question': '哪家店？', 'options': [42]},
                       {'category': 'entity_identity', 'question': '哪家店？', 'options': [{}]}):
            with self.subTest(detail=detail):
                result, _ = self.generate({'queries': [{'query_id': 'Q1', 'status': 'clarify',
                                                        'clarification': detail}]})
                self.assertFalse(result['candidates'])
                self.assertEqual(result['errors']['Q1']['kind'], 'output_format')

    def test_internal_mapping_is_not_user_clarification_and_unsupported_is_capability(self):
        result, _ = self.generate({'queries': [
            {'query_id': 'Q1', 'status': 'clarify', 'clarification': {
                'category': 'technical_mapping', 'question': '应该用哪个时间字段？',
                'options': ['orders.ordered_at', 'sessions.started_at']}},
            {'query_id': 'Q2', 'status': 'unsupported', 'reason': '公开数据不含广告消耗'},
            {'query_id': 'Q3', 'status': 'missing_context', 'reason': '需要店铺键映射',
             'missing_requests': [{'kind': 'entity_mapping', 'request': '店铺名称对应的store_id'}]},
        ]}, ids=('Q1', 'Q2', 'Q3'))
        self.assertEqual(result['errors']['Q1']['kind'], 'missing_context')
        self.assertNotIn('clarification', result['errors']['Q1'])
        self.assertEqual(result['errors']['Q2']['kind'], 'capability')
        self.assertEqual(result['errors']['Q3']['kind'], 'missing_context')

    def test_new_local_measurement_requires_scope_without_weakening_legacy(self):
        value = query()
        value['local_measurements'] = [{'id': 'average_item_value', 'definition': '商品实付金额/商品件数'}]
        failed, _ = self.generate({'queries': [value]})
        self.assertEqual(failed['errors']['Q1']['kind'], 'output_format')
        value['local_measurements'][0]['scope'] = '本店2026年6、7月成功支付商品'
        result, _ = self.generate({'queries': [value]})
        local = result['candidates']['Q1']['local_measurements'][0]
        self.assertEqual(local['kind'], 'task_local')
        self.assertEqual(local['validation_status'], 'requires_evidence_and_review')
        self.assertEqual(local['scope'], value['local_measurements'][0]['scope'])
        legacy = query()
        legacy['query_contract'] = {'metrics': legacy.pop('metric_declarations')}
        legacy['local_measurements'] = [{'id': 'local', 'definition': '已有旧任务定义'}]
        old, old_model = self.generate({'queries': [legacy]}, context={'execution_protocol_version': 3})
        self.assertTrue(old['candidates'])
        self.assertIsNone(old_model.calls[0]['selected_schema'])

    def test_legacy_ordinary_actions_keep_their_protocol_and_normalize_options(self):
        for response in ({'action': 'clarify', 'clarification': {'question': '查哪家店？', 'options': ['甲店', '乙店']}},
                         {'action': 'call_tool', 'tool': 'query_ecommerce_ops',
                          'arguments': {'sql': 'SELECT 1'}, 'query_contract': {'metrics': []}},
                         {'action': 'step_decision', 'decision': 'stop'}):
            with self.subTest(response=response):
                result, model = self.generate(response, context={'allow_preparation_actions': True})
                self.assertIsNone(model.calls[0]['selected_schema'])
                self.assertEqual(result['decision']['action'], response['action'])
                if response['action'] == 'clarify':
                    Clarification.model_validate(result['decision']['clarification'])
                elif response['action'] == 'call_tool':
                    self.assertEqual(result['candidates']['Q1']['sql'], 'SELECT 1')

    def test_model_output_error_spends_one_call_and_resets_schema(self):
        result, model = self.generate(ModelOutputError('malformed provider output'))
        self.assertEqual(result['errors']['Q1']['kind'], 'output_format')
        self.assertEqual(len(model.calls), 1)
        self.assertIsNone(current_output_schema.get())

    def test_bad_legacy_clarification_keeps_exact_error_for_bounded_adapter(self):
        result, model = self.generate({'action': 'clarify', 'clarification': {
            'question': '查哪家店？', 'options': [{}]}}, context={'allow_preparation_actions': True})
        self.assertNotIn('decision', result)
        self.assertEqual(result['errors']['Q1']['kind'], 'output_format')
        self.assertIn('options[0].label', result['errors']['Q1']['message'])
        self.assertEqual(len(model.calls), 1)


if __name__ == '__main__':
    unittest.main()
