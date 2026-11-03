"""Offline controller regression with controlled model outputs and real local MCP."""
from copy import deepcopy
import unittest

from app.runtime_budget import RunBudget, current_budget
from app.workflows.investigation_progress import merge_progress_records, reconcile_review
from app.workflows.react_harness import ReactRecord
from tests import test_sql_batch_controller as base
from tests.test_react_harness import action, query, review
from tests.test_sql_batch_controller import business, generated


def final_review(*, ids=('T1:R1',), unresolved=False, questions=('Q1',)):
    value = review(ids=ids)
    value.update(question_assessments=[{'question_id': q, 'status': 'unresolved' if unresolved else 'answered',
        'evidence_ids': list(ids), 'answer_level': 'association', 'reason': '受控接口测试依据实际结果或明确缺口'}
        for q in questions], explanation_coverage_ack=True)
    if unresolved:
        value.update(action='partial', can_repair=False, unanswered=['原问题尚有缺口'])
        value['completion_check'].update(goal_status='partial', remaining_core_gaps=['原问题尚有缺口'])
    return value


class DiagnosisReliabilityV3Test(unittest.TestCase):
    setUp = base.SqlBatchControllerTest.setUp
    harness = base.SqlBatchControllerTest.harness
    run_bounded = base.SqlBatchControllerTest.run_bounded
    state = staticmethod(base.SqlBatchControllerTest.state)

    def new_run(self, outputs):
        harness, model, state = base.SqlBatchControllerTest.new_run(self, outputs)
        state['investigation_protocol_version'] = 3
        return harness, model, state

    def test_third_generation_can_succeed_no_fourth_and_one_evidence_round(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            generated(('A2', 'SELECT SUM(missing_one) AS value FROM daily_store_metrics')),
            generated(('A2', 'SELECT SUM(missing_two) AS value FROM daily_store_metrics')),
            generated(('A2', None)), action('deliver'), {'draft': '仅用于控制流检查'}, final_review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(result.plan[1]['sql_generations'], 3)
        self.assertEqual(len(model.calls), 8)
        self.assertEqual(budget.react_rounds, 1)
        self.assertNotIn('manifest', model.calls[2][1])
        self.assertIn('measurement_requests', model.calls[2][1])
        self.assertIn('repair_diagnostics', model.calls[3][1]['failed_queries']['A2'])
        self.assertNotIn('draft', model.calls[-1][1])  # Markdown exists once in report.
        self.assertNotIn('schema_graph', model.calls[-2][1]['context']['semantics'])
        review_graph = model.calls[-1][1]['semantics']['schema_graph']
        self.assertEqual([t['name'] for t in review_graph['tables']], ['daily_store_metrics'])

    def test_closed_query_does_not_stop_independent_query(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            generated(('A2', 'SELECT SUM(missing_one) AS value FROM daily_store_metrics')),
            generated(('A2', 'SELECT SUM(missing_two) AS value FROM daily_store_metrics')),
            generated(('A2', 'SELECT SUM(missing_three) AS value FROM daily_store_metrics')),
            business(('独立核实支付订单', 'paid_orders')),
            generated(('A3', 'SELECT SUM(paid_orders) AS value FROM daily_store_metrics', 'paid_orders')),
            action('deliver'), {'draft': '已取得订单数据，GMV仍失败'}, final_review(unresolved=True),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'partial', result.stop_reason)
        self.assertTrue(result.plan[1]['generation_closed'])
        self.assertEqual(result.plan[2]['status'], 'completed')
        self.assertEqual(budget.react_rounds, 2)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.plan[1]['sql_generations'], 3)

    def test_renamed_output_cannot_reopen_three_exhausted_generations(self):
        renamed = business(('换措辞查GMV', 'gmv'))
        renamed['action']['queries'][0]['required_output_fields'] = ['another_alias']
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            {'queries': []}, {'queries': []}, {'queries': []}, renamed,
            action('clarify', parameter='scope', question='受控测试等待用户', reason='接口测试'),
        ])
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        queries = [a for a in result.plan if a.get('action_type') == 'query']
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]['sql_generations'], 3)
        self.assertEqual(len([c for c in model.calls if 'failed_queries' in c[1]]), 2)

    def test_core_question_omitted_by_reviewer_cannot_complete(self):
        update = action('deliver')
        update['progress'] = {'core_questions': [{'id': 'D1', 'question': '关键业务解释仍待验证',
                                                  'importance': 'core', 'status': 'open'}]}
        h, _, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            generated(('A2', None)), update, {'draft': '阶段性指标变化'}, final_review(),
        ])
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.final_reviewed)
        self.assertIsNone(result.report)
        self.assertIn('D1', result.stop_reason)

    def test_splitting_exhausted_request_cannot_open_new_generations(self):
        combined = business(('核实GMV与订单', 'gmv'))
        combined['action']['queries'][0]['metric_requirements'].append('paid_orders')
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), combined,
            {'queries': []}, {'queries': []}, {'queries': []}, business(('拆项再查GMV', 'gmv')),
            action('clarify', parameter='scope', question='受控等待', reason='接口测试'),
        ])
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        self.assertEqual(len([a for a in result.plan if a.get('action_type') == 'query']), 1)
        self.assertEqual(result.plan[1]['sql_generations'], 3)
        self.assertEqual(len(model.calls), 7)

    def test_calculator_rejects_original_self_comparison_then_uses_long_table(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('取得对比数据', 'gmv')),
            generated(('A2', "SELECT '2026-06' AS month, SUM(gmv) AS value FROM daily_store_metrics UNION ALL SELECT '2026-07' AS month, SUM(gmv)*0.8 AS value FROM daily_store_metrics")),
            action('calculate', operation='change', result_id='T1:R1', base_field='value',
                   current_field='value', group_by='month'),
            action('calculate', operation='period_change', result_id='T1:R1', period_field='month',
                   base_period='2026-06', current_period='2026-07', value_fields=['value']),
            action('deliver'), {'draft': '跨期比较'}, final_review(ids=('T1:R1', 'T1:R2')),
        ])
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(result.plan[2]['attempts'], 1)
        self.assertEqual(result.plan[2]['status'], 'failed')
        self.assertIn('error_code', model.calls[4][1]['last_observation'])
        self.assertAlmostEqual(result.evidence[-1]['rows'][0]['change_rate'], -0.2)
        self.assertEqual(len(result.evidence), 2)  # Invalid self-comparison saved no evidence.

    def test_alias_resolved_after_first_retrieval_keeps_original_ledger(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'GMV')),
            {'queries': []}, {'queries': []}, {'queries': []}, business(('再查GMV', 'gmv_registered')),
            action('clarify', parameter='scope', question='等待用户', reason='接口测试'),
        ])
        state['schema_graph'] = {}
        original = h.workflow._retrieve_schema
        def retrieval(s, charge_budget=True):
            update = original(s, charge_budget=charge_budget)
            update['selected_semantics'] = [{'id': 'gmv_registered', 'aliases': ['GMV'],
                'definition': '测试公开定义', 'required_fields': ['daily_store_metrics.gmv']}]
            return update
        h.workflow._retrieve_schema = retrieval
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'waiting_clarification', result.stop_reason)
        self.assertEqual(len([a for a in result.plan if a.get('action_type') == 'query']), 1)
        self.assertEqual(result.plan[1]['sql_generations'], 3)

    def test_month_grain_does_not_trigger_retrieval_when_measurement_is_cached(self):
        h, _, state = self.new_run([])
        task = ReactRecord(state, protocol_version=3)
        h._inherit_semantics(task, state)
        def forbidden(*args, **kwargs):
            self.fail('A known time grain is not a missing physical column')
        h.workflow._retrieve_schema = forbidden
        context = h._prepare_sql_context(task, [{'query_id': 'A1', 'goal': '按月比较GMV',
            'metric_requirements': ['gmv'], 'dimensions': ['month'], 'input_result_ids': []}])
        self.assertIn('daily_store_metrics.stat_date', [f.get('sql_name') for f in context['schema_graph']['fields']])
        self.assertFalse(context['measurement_requests']['unresolved_requirements'])


class InvestigationProgressContractTest(unittest.TestCase):
    def test_normal_and_blocked_explanations_match_the_model_protocol(self):
        for status in ('low_explanatory_power', 'blocked', 'open', 'not_applicable', 'unsupported'):
            updated = merge_progress_records({}, {'explanations': [{'id': 'H1', 'text': '待比较解释',
                'status': status, 'supporting_evidence_ids': [], 'refuting_evidence_ids': []}]}, set())
            self.assertEqual(updated['explanations'][0]['status'], status)

    def test_core_question_cannot_be_downgraded_or_disappear(self):
        progress = {'core_questions': [{'id': 'D1', 'question': '核心缺口', 'importance': 'core',
                                       'status': 'open', 'evidence_ids': []}]}
        with self.assertRaisesRegex(ValueError, '不能降为'):
            merge_progress_records(progress, {'core_questions': [{'id': 'D1', 'importance': 'optional'}]}, set())
        self.assertEqual(merge_progress_records(progress, {}, set()), progress)

    def test_unresolved_core_gap_is_not_overruled_by_complete_label(self):
        result = reconcile_review(final_review(unresolved=True), [{'id': 'Q1', 'question': '原因',
                                  'importance': 'core', 'status': 'blocked'}], [], {'T1:R1'})
        self.assertFalse(result['errors'])
        self.assertEqual(result['gaps'], ['原因'])

    def test_legacy_checkpoint_does_not_inherit_third_slot(self):
        self.assertEqual(RunBudget.from_state('old', {}).sql_generation_limit, 2)
        self.assertEqual(RunBudget('new').sql_generation_limit, 3)


if __name__ == '__main__':
    unittest.main()
