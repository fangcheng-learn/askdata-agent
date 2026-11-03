"""Dependency acquisition and execution gates; no remote models or business SQL."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.database import SCHEMA, physical_table_name
from app.homenova import registered_metric
from app.querying.compiled_metrics import METRICS, compile_paid_goods
from app.runtime_budget import BudgetStop, RunBudget, current_budget
from app.security import AccessScope
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord


SOURCE = 'homenova_20260831'


def query():
    return {'goal': '核实支付订单数', 'execution_mode': 'registered_metrics',
            'metric_requirements': ['paid_order_count'], 'dimensions': ['month'],
            'conditions': {'months': ['2026-06', '2026-07']}}


class RegisteredRecipeDependencyTest(unittest.TestCase):
    def setUp(self):
        self.tables = {t['id']: t for t in SCHEMA if t.get('database') == SOURCE}
        self.scope = {'user_id': 'dependency-fixture', 'allowed_databases': [SOURCE],
                      'allowed_tables': list(self.tables)}
        self.context = {'database': SOURCE, 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'}}
        self.task = ReactRecord({'task_id': 'T', 'query': '比较6月和7月', 'datasource_id': SOURCE,
            'session_id': 'dependency-fixture-session', 'workspace': {}, 'access_scope': deepcopy(self.scope),
            'scenario': self.context['scenario'], 'extraction': {'time_expressions': ['2026-06', '2026-07']}},
            protocol_version=4)
        # The actual candidate has a full output pack although its request is
        # only paid_order_count. Do not manufacture candidate metric IDs here.
        self.candidate = compile_paid_goods(query(), self.context)
        self.task.metric_pool = {name: deepcopy(registered_metric(SOURCE, name)) for name in METRICS}
        self.harness = object.__new__(ReactInvestigationHarness)
        self.retrieval_calls = []

        def retrieve_schema(state, charge_budget):
            # Run the real _retrieve method; replace only the external retriever
            # with acquired authorized metadata selected by explicit field IDs.
            scope = AccessScope.from_dict(state['access_scope'])
            self.retrieval_calls.append(deepcopy(state))
            if charge_budget and current_budget.get():
                current_budget.get().tool('metadata_fixture')
            graph = self.graph(state['workspace'].get('schema_fields') or [], scope)
            return {'schema_graph': graph, 'selected_semantics': [deepcopy(m) for m in self.task.metric_pool.values()],
                    'retrieval': {'selected_count': len(graph['fields'])}}

        self.harness.workflow = SimpleNamespace(_retrieve_schema=retrieve_schema)
        initial = [{'tableId': t['id'], 'name': name} for t in self.tables.values()
                   if physical_table_name(t) == 'payments' for name in ('order_id', 'captured_at', 'status')]
        self.harness._inherit_semantics(self.task, {'schema_graph': self.graph(initial, AccessScope.from_dict(self.scope))})

    def graph(self, selections, scope):
        tables, fields = {}, []
        for selected in selections:
            table = self.tables.get(selected['tableId'])
            if not table or not scope.allows_table(SOURCE, table['id']):
                continue
            definition = next((f for f in table['fields'] if f['name'] == selected['name']), None)
            if not definition:
                continue
            name = physical_table_name(table)
            tables[table['id']] = {**{k: deepcopy(v) for k, v in table.items() if k != 'fields'}, 'name': name}
            fields.append({**deepcopy(definition), 'id': table['id'] + '.' + definition['name'],
                'table_id': table['id'], 'table_name': name, 'sql_name': name + '.' + definition['name']})
        return {'database': SOURCE, 'tables': list(tables.values()), 'fields': fields, 'joins': []}

    def prepare_route(self):
        self.harness._persist = Mock()
        self.harness._prepare_sql_context = Mock(return_value=deepcopy(self.context))
        coder = SimpleNamespace(generate_batch=Mock(side_effect=AssertionError('No model SQL fallback permitted')))
        self.harness._sql_module = lambda: coder
        def execute(task, candidate, reason, **kwargs):
            action = next(a for a in task.actions if a['id'] == candidate['repair_action_id'])
            action['status'] = 'completed'
            return {'action_id': action['id'], 'status': 'completed'}
        self.harness._query = Mock(side_effect=execute)
        self.harness._postprocess_query = lambda task, action, candidate, outcome: outcome
        return coder

    def test_actual_paid_candidate_acquires_full_pack_from_minimal_known_fields(self):
        self.assertEqual(set(self.candidate['registered_metric_ids']), set(METRICS))
        self.task.actions = [{'id': 'OLD', 'sql_generations': 2, 'attempts': 2, 'status': 'failed'}]
        before = deepcopy(self.task.actions)
        budget = RunBudget('T'); budget.react_rounds = 3; budget.tools = 5
        token = current_budget.set(budget)
        try:
            self.harness._prepare_recipe_dependencies(self.task, self.candidate)
        finally:
            current_budget.reset(token)
        self.assertEqual(len(self.retrieval_calls), 1)
        fields = {f['sql_name'] for f in self.task.field_pool.values()}
        self.assertTrue({'order_items.quantity', 'order_items.line_paid_gross_cents'} <= fields)
        self.assertEqual(self.retrieval_calls[0]['extraction']['metric_ids'], self.candidate['registered_metric_ids'])
        self.assertEqual(self.task.actions, before, 'Dependency retrieval cannot renew SQL attempts')
        self.assertEqual((budget.react_rounds, budget.tools), (3, 6))
        self.assertEqual(budget.model_calls, {})
        self.assertEqual(self.task.logs[-1]['missing_fields'], [])

    def test_fully_cached_pack_makes_no_retrieval_or_budget_increment(self):
        self.harness._prepare_recipe_dependencies(self.task, self.candidate)
        previous_calls = len(self.retrieval_calls)
        budget = RunBudget('T'); budget.react_rounds = 4; budget.tools = 8
        token = current_budget.set(budget)
        try:
            self.harness._prepare_recipe_dependencies(self.task, self.candidate)
        finally:
            current_budget.reset(token)
        self.assertEqual(len(self.retrieval_calls), previous_calls)
        self.assertEqual((budget.react_rounds, budget.tools, budget.model_calls), (4, 8, {}))

    def test_missing_permission_is_not_bypassed_by_explicit_field_retrieval(self):
        self.task.state['access_scope']['allowed_tables'] = [identity for identity, t in self.tables.items()
                                                          if physical_table_name(t) != 'order_items']
        with self.assertRaisesRegex(ValueError, 'order_items.quantity'):
            self.harness._prepare_recipe_dependencies(self.task, self.candidate)
        selected = self.retrieval_calls[0]['workspace']['schema_fields']
        forbidden = next(identity for identity, t in self.tables.items() if physical_table_name(t) == 'order_items')
        self.assertFalse(any(s['tableId'] == forbidden for s in selected))
        self.assertFalse(any(f['sql_name'].startswith('order_items.') for f in self.task.field_pool.values()))

    def test_unknown_dependency_is_explicitly_blocked_not_synthesized(self):
        self.task.metric_pool['paid_goods_gmv_gross']['required_fields'].append('order_items.not_registered')
        with self.assertRaisesRegex(ValueError, 'order_items.not_registered'):
            self.harness._prepare_recipe_dependencies(self.task, self.candidate)
        self.assertIn('order_items.not_registered', self.task.logs[-1]['missing_fields'])
        self.assertNotIn('order_items.not_registered', {f['sql_name'] for f in self.task.field_pool.values()})

    def test_route_blocks_unavailable_pack_before_execution_without_model_fallback(self):
        self.task.state['access_scope']['allowed_tables'] = [identity for identity, t in self.tables.items()
                                                          if physical_table_name(t) != 'order_items']
        coder = self.prepare_route()
        budget = RunBudget('T'); budget.react_rounds = 2; budget.tools = 4
        token = current_budget.set(budget)
        try:
            self.harness._business_query_batch(self.task, {'queries': [query()]}, '核对')
        finally:
            current_budget.reset(token)
        self.harness._query.assert_not_called()
        coder.generate_batch.assert_not_called()
        action = self.task.actions[0]
        self.assertEqual((action['status'], action['error_kind'], action['sql_generations']), ('blocked', 'recipe_dependencies', 1))
        self.assertEqual(action['model_generations'], 0)
        self.assertEqual((budget.react_rounds, budget.tools), (3, 5))

    def test_route_reserves_original_single_generation_then_executes_acquired_paid_pack(self):
        coder = self.prepare_route()
        self.harness._business_query_batch(self.task, {'queries': [query()]}, '核对')
        self.harness._query.assert_called_once()
        coder.generate_batch.assert_not_called()
        action = self.task.actions[0]
        self.assertEqual((action['sql_generations'], action['attempts'], action['model_generations']), (1, 1, 0))
        self.assertTrue(self.harness._query.call_args.kwargs['generation_reserved'])
        self.assertTrue(any(log.get('stage') == 'registered_recipe_executed' for log in self.task.logs))

    def test_repeating_dependency_block_cannot_switch_fixed_recipe_to_model_sql(self):
        self.task.state['access_scope']['allowed_tables'] = [identity for identity, t in self.tables.items()
                                                          if physical_table_name(t) != 'order_items']
        coder = self.prepare_route()
        self.harness._business_query_batch(self.task, {'queries': [query()]}, '核对')
        first_generations = self.task.actions[0]['sql_generations']
        try:
            self.harness._business_query_batch(self.task, {'queries': [query()]}, '再次核对')
        except BudgetStop:
            pass  # Explicitly stopping the identical missing-input retry is valid.
        coder.generate_batch.assert_not_called()
        self.harness._query.assert_not_called()
        self.assertEqual(len(self.task.actions), 1)
        self.assertEqual(self.task.actions[0]['sql_generations'], first_generations)

    def test_later_acquired_dependencies_resume_same_fixed_action_with_second_generation(self):
        self.task.state['access_scope']['allowed_tables'] = [identity for identity, t in self.tables.items()
                                                          if physical_table_name(t) != 'order_items']
        coder = self.prepare_route()
        self.harness._business_query_batch(self.task, {'queries': [query()]}, '核对')
        original_action = self.task.actions[0]
        self.assertEqual((original_action['status'], original_action['sql_generations']), ('blocked', 1))
        # Simulate a later authorized metadata acquisition; no identity or
        # query-budget reset, and no invented field is inserted into the pool.
        self.task.state['access_scope'] = deepcopy(self.scope)
        required = {ref for metric in self.task.metric_pool.values()
                    for ref in self.harness._metric_field_references(metric)}
        selections = [{'tableId': t['id'], 'name': f['name']} for t in self.tables.values() for f in t['fields']
                      if physical_table_name(t) + '.' + f['name'] in required]
        acquired = self.graph(selections, AccessScope.from_dict(self.scope))
        self.harness._inherit_semantics(self.task, {'schema_graph': acquired})
        previous_retrievals = len(self.retrieval_calls)
        self.harness._business_query_batch(self.task, {'queries': [query()]}, '补齐后核对')
        self.assertEqual(len(self.task.actions), 1)
        self.assertIs(self.task.actions[0], original_action)
        self.assertEqual((original_action['status'], original_action['sql_generations'], original_action['attempts']),
                         ('completed', 2, 2))
        self.assertEqual(original_action['compiled_generations'], 2)
        self.assertEqual(original_action['model_generations'], 0)
        self.assertEqual([c['generation'] for c in original_action['candidates']], [1, 2])
        self.assertEqual(len(self.retrieval_calls), previous_retrievals)
        self.harness._query.assert_called_once()
        self.assertTrue(self.harness._query.call_args.kwargs['generation_reserved'])
        coder.generate_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
