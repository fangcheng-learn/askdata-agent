"""Optional business methods with controlled decisions and real local MCP.

The model is a script, so these are controller/SQL integration checks, not
real-model business acceptance. They do not write business data or call a
model provider. A business method is never a database execution permission.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app.agent_configuration_runtime import current_configuration
from app.runtime_budget import RunBudget, current_budget
from app.workflows.react_harness import ReactRecord
from tests import test_diagnosis_reliability_v3 as reliability
from tests import test_sql_batch_controller as base
from tests.test_react_harness import action, query
from tests.test_sql_batch_controller import business, generated


class OptionalBusinessSkillTest(unittest.TestCase):
    harness = base.SqlBatchControllerTest.harness
    run_bounded = base.SqlBatchControllerTest.run_bounded
    state = staticmethod(base.SqlBatchControllerTest.state)

    def setUp(self):
        base.SqlBatchControllerTest.setUp(self)
        environment = patch.dict(os.environ, {
            'AGENT_CONFIGURATION_PATH': str(Path(self.tmp.name) / 'configuration.json')})
        environment.start()
        self.addCleanup(environment.stop)
        token = current_configuration.set(None)
        self.addCleanup(current_configuration.reset, token)
        network = patch('socket.socket.connect', side_effect=AssertionError('Offline integration: no network'))
        network.start()
        self.addCleanup(network.stop)

    def new_run(self, outputs):
        harness, model, state = base.SqlBatchControllerTest.new_run(self, outputs)
        state['investigation_protocol_version'] = 3
        return harness, model, state

    @contextmanager
    def bounded(self, harness):
        budget = RunBudget('T1')
        harness.workflow.budgets['T1'] = budget
        budget.start()
        token = current_budget.set(budget)
        try:
            yield budget
        finally:
            budget.pause()
            current_budget.reset(token)

    @staticmethod
    def query_actions(result):
        return [entry for entry in result.plan if entry.get('action_type') == 'query']

    def test_first_action_can_query_without_business_skill(self):
        h, model, state = self.new_run([
            business(('先核实GMV', 'gmv')), generated(('A1', None)),
            action('deliver'), {'draft': '受控接口测试，不声明经营原因'}, reliability.final_review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(h.records['T1'].skills, {})
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(self.query_actions(result)[0]['sql_generations'], 1)
        self.assertEqual(budget.repairs, {})
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(budget.tools, 1)  # Actual MCP query; no business Skill read.
        self.assertEqual(len(model.calls), 5)
        self.assertFalse(h.records['T1'].failed_signatures)
        self.assertIn('# 只读数据查询（database_query）', model.calls[1][0])
        self.assertTrue(any(event.get('stage') == 'skill_injection'
                            and event.get('skill') == 'database_query'
                            and event.get('node') == 'query_coder' for event in budget.events))
        self.assertEqual(model.calls[1][1]['queries'][0]['query_id'], 'A1')

    def test_first_action_can_load_method_then_query(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('按方法核实GMV', 'gmv')), generated(('A2', None)),
            action('deliver'), {'draft': '受控接口测试'}, reliability.final_review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertIn('business_diagnosis', h.records['T1'].skills)
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(budget.tools, 2)  # One Skill read, one actual MCP query.
        self.assertEqual(self.query_actions(result)[0]['sql_generations'], 1)
        self.assertEqual(budget.repairs, {})
        self.assertEqual(len(model.calls), 6)

    def test_query_then_load_method_then_new_query_preserves_first_result(self):
        h, model, state = self.new_run([
            business(('先核实GMV', 'gmv')), generated(('A1', None)),
            action('load_skill', names=['business_diagnosis']),
            business(('补查订单', 'paid_orders')),
            generated(('A3', 'SELECT SUM(paid_orders) AS value FROM daily_store_metrics', 'paid_orders')),
            action('deliver'), {'draft': '受控接口测试，两份结果均保留'},
            reliability.final_review(ids=('T1:R1', 'T1:R2')),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        queries = self.query_actions(result)
        self.assertEqual([entry['id'] for entry in queries], ['A1', 'A3'])
        self.assertEqual([entry['sql_generations'] for entry in queries], [1, 1])
        self.assertEqual([entry['result_ids'] for entry in queries], [['T1:R1'], ['T1:R2']])
        self.assertEqual([entry['id'] for entry in result.evidence], ['T1:R1', 'T1:R2'])
        self.assertEqual(budget.react_rounds, 2)
        self.assertEqual(budget.tools, 3)
        self.assertEqual(budget.repairs, {})
        self.assertEqual(len(model.calls), 8)

    def test_without_method_real_sql_error_can_succeed_on_third_candidate(self):
        h, model, state = self.new_run([
            business(('核实GMV', 'gmv')),
            generated(('A1', 'SELECT SUM(missing_one) AS value FROM daily_store_metrics')),
            generated(('A1', 'SELECT SUM(missing_two) AS value FROM daily_store_metrics')),
            generated(('A1', None)), action('deliver'), {'draft': '受控接口测试'}, reliability.final_review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(self.query_actions(result)[0]['sql_generations'], 3)
        self.assertEqual(budget.repairs, {'A1': 2})
        self.assertEqual(budget.tools, 1)
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(len(model.calls), 7)
        self.assertIn('missing_one', str(model.calls[2][1]['failed_queries']))
        self.assertIn('missing_two', str(model.calls[3][1]['failed_queries']))
        self.assertEqual(h.records['T1'].skills, {})

    def test_three_failed_sql_candidates_do_not_allow_fourth_generation(self):
        h, model, state = self.new_run([
            business(('核实GMV', 'gmv')),
            generated(('A1', 'SELECT SUM(missing_one) AS value FROM daily_store_metrics')),
            generated(('A1', 'SELECT SUM(missing_two) AS value FROM daily_store_metrics')),
            generated(('A1', 'SELECT SUM(missing_three) AS value FROM daily_store_metrics')),
            action('clarify', parameter='other', question='受控测试在此暂停', reason='避免额外模型调用'),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        query_action = self.query_actions(result)[0]
        self.assertEqual(query_action['sql_generations'], 3)
        self.assertTrue(query_action['generation_closed'])
        self.assertEqual(len([call for call in model.calls if 'queries' in call[1]]), 3)
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(budget.repairs, {'A1': 2})
        self.assertEqual(budget.tools, 0)
        self.assertEqual(budget.react_rounds, 1)

    def test_invalid_skill_is_preparation_feedback_and_does_not_poison_sql(self):
        h, model, state = self.new_run([
            action('load_skill', names=['not_registered']),
            business(('独立核实GMV', 'gmv')), generated(('A2', None)),
            action('deliver'), {'draft': '受控接口测试'}, reliability.final_review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        failure = result.plan[0]
        self.assertEqual(failure['action_type'], 'load_skill')
        self.assertEqual(failure['status'], 'failed')
        self.assertEqual(failure['error_kind'], 'preparation')
        feedback = model.calls[1][1]['last_observation']
        self.assertEqual(feedback['type'], 'preparation_error')
        self.assertEqual(feedback['category'], 'skill_loading')
        self.assertEqual(self.query_actions(result)[0]['sql_generations'], 1)
        self.assertFalse(h.records['T1'].failed_signatures)
        self.assertEqual(budget.repairs, {'react_protocol': 1})  # Bad preparation is bounded, not SQL repair.
        self.assertEqual(budget.tools, 1)
        self.assertEqual(budget.react_rounds, 1)

    def test_missing_skill_file_is_preparation_error_not_sql_rewrite(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('仍可独立核实GMV', 'gmv')), generated(('A2', None)),
            action('deliver'), {'draft': '受控接口测试'}, reliability.final_review(),
        ])
        get_skill = h.workflow.skills.get

        def unavailable_method(name):
            if name == 'business_diagnosis':
                raise FileNotFoundError('受控测试：业务方法正文不可读')
            return get_skill(name)

        with patch.object(h.workflow.skills, 'get', side_effect=unavailable_method):
            result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(result.plan[0]['error_kind'], 'preparation')
        self.assertEqual(model.calls[1][1]['last_observation']['category'], 'skill_loading')
        self.assertFalse(h.records['T1'].failed_signatures)
        self.assertEqual(self.query_actions(result)[0]['sql_generations'], 1)
        self.assertEqual(budget.repairs, {'react_protocol': 1})
        self.assertEqual(budget.react_rounds, 1)

    def test_missing_dependency_still_blocks_before_sql_generation(self):
        requested = business(('依赖不明结果的查询', 'gmv'))
        requested['action']['queries'][0]['input_result_ids'] = ['T1:R404']
        h, model, state = self.new_run([
            requested,
            action('clarify', parameter='other', question='受控测试暂停', reason='依赖必须明确'),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        self.assertEqual(self.query_actions(result), [])
        self.assertFalse(result.evidence)
        self.assertEqual(len(model.calls), 2)
        self.assertIn('T1:R404', model.calls[1][1]['last_observation']['error'])
        self.assertEqual(budget.tools, 0)
        self.assertEqual(budget.react_rounds, 0)

    def test_readonly_guard_still_rejects_write_sql_without_method(self):
        h, _, state = self.new_run([])
        task = ReactRecord(state, protocol_version=3)
        h._inherit_semantics(task, state)
        request = query(sql='DELETE FROM daily_store_metrics')
        with self.bounded(h) as budget:
            outcome = h._query(task, request, '禁止写入的受控反例')
        self.assertEqual(outcome['status'], 'failed')
        self.assertFalse(task.evidence)
        self.assertRegex(outcome['error'], '只读|SELECT|DELETE|select')
        self.assertEqual(budget.tools, 0)

    def test_table_permission_guard_still_rejects_query_without_method(self):
        h, _, state = self.new_run([])
        state['access_scope']['allowed_tables'] = []
        task = ReactRecord(state, protocol_version=3)
        h._inherit_semantics(task, state)
        with self.bounded(h) as budget:
            outcome = h._query(task, query(), '无表权限的受控反例')
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(outcome['error_kind'], 'permission')
        self.assertFalse(task.evidence)
        self.assertEqual(budget.tools, 0)
        self.assertFalse(task.failed_signatures)

    def test_permission_denial_in_real_batch_is_not_sent_to_sql_repair(self):
        h, model, state = self.new_run([
            business(('受控模型请求无权访问表', 'gmv')), generated(('A1', None)),
            action('clarify', parameter='other', question='受控测试暂停', reason='权限拒绝不能靠SQL改写解决'),
        ])
        state['access_scope']['allowed_tables'] = []
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        queries = self.query_actions(result)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]['sql_generations'], 1)
        self.assertEqual(queries[0]['error_kind'], 'permission')
        self.assertIn('无权访问的表', queries[0]['error'])
        self.assertEqual(len(model.calls), 3)  # Business decision, first SQL, controlled pause.
        self.assertFalse(any('failed_queries' in payload for _, payload in model.calls))
        self.assertEqual(budget.repairs, {})
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(budget.tools, 0)
        self.assertFalse(result.evidence)
        self.assertFalse(h.records['T1'].failed_signatures)


if __name__ == '__main__':
    unittest.main()
