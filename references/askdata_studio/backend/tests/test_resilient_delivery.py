import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from app.querying.query_scope import complete_registered_request
from app.querying.compiled_metrics import compile_paid_goods, RecipeNotCovered
from app.querying.compiled_inventory import compile_inventory_duration
from app.querying.compiled_sessions import compile_sessions, SessionPopulationMismatch
from app.runtime_budget import RunBudget, current_budget
from app.workflows.react_harness import ReactRecord
from tests import test_react_harness as legacy


SOURCE = 'homenova_20260831'
CONTEXT = {'database': SOURCE, 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'}}


class RegisteredRequestTest(unittest.TestCase):
    def test_known_aggregate_implementation_is_chosen_by_query_module(self):
        query = self.request('category_view_session_cvr', execution_mode='exploratory',
                             dimensions=['month'], conditions={'months': ['2026-06', '2026-07'],
                             'sku_catalog.category_code': 'POWER'})
        resolved = complete_registered_request(query, SOURCE)
        self.assertEqual(resolved['execution_mode'], 'registered_metrics')
        compile_sessions(resolved, CONTEXT)
        query['required_output_fields'] = ['session_id', 'occurred_at', 'failure_reason']
        self.assertEqual(complete_registered_request(query, SOURCE)['execution_mode'], 'exploratory')

    def test_explicit_task_months_survive_empty_entry_extraction_without_year_guess(self):
        from app.querying.query_scope import effective_conditions
        state = {'extraction': {'time_expressions': []},
                 'task_understanding': {'period': '2026年6月和7月'}}
        query = {'conditions': {'category_code': 'POWER'}}
        self.assertEqual(effective_conditions(query, state)['months'], ['2026-06', '2026-07'])
        state['task_understanding']['period'] = '6月和7月'
        self.assertNotIn('months', effective_conditions(query, state))
        state['task_understanding']['period'] = '2026年6月和7月'
        state['confirmed_parameters'] = {'time_range': {'start': '2026-08-01'}}
        self.assertNotIn('months', effective_conditions(query, state))

    def request(self, metric, **extra):
        return {'goal': '比较指定两期', 'metric_requirements': [metric], 'dimensions': [],
                'comparison_purpose': '比较两个月', 'conditions': {'months': ['2026-06', '2026-07']}, **extra}

    def test_explicit_comparison_fills_technical_month_and_routes_without_model_choice(self):
        original = self.request('paid_goods_gmv_gross', required_output_fields=['month'])
        before = deepcopy(original)
        resolved = complete_registered_request(original, SOURCE)
        self.assertEqual(original, before)
        self.assertEqual(resolved['execution_mode'], 'registered_metrics')
        candidate = compile_paid_goods(resolved, CONTEXT)
        self.assertIn('GROUP BY', candidate['sql'])
        self.assertEqual(resolved['dimensions'], ['month'])

    def test_month_filter_does_not_change_requested_overall_aggregation(self):
        resolved = complete_registered_request(self.request('paid_goods_gmv_gross', comparison_purpose=''), SOURCE)
        self.assertEqual(resolved['dimensions'], [])
        with self.assertRaises(RecipeNotCovered):
            compile_paid_goods(resolved, CONTEXT)

    def test_inventory_partition_is_bound_internally_not_replaced_by_category_average(self):
        resolved = complete_registered_request(self.request('stockout_duration',
            dimensions=['category'], conditions={'months': ['2026-06', '2026-07'],
                                                'sku_catalog.category_code': 'POWER'}), SOURCE)
        candidate = compile_inventory_duration(resolved, CONTEXT)
        self.assertIn('inventory_movements.sku_id', resolved['dimensions'])
        self.assertTrue(candidate.get('postprocess'))

    def test_session_population_conflict_is_not_silently_rewritten(self):
        resolved = complete_registered_request(self.request('sessions_count',
            conditions={'months': ['2026-06', '2026-07'], 'sku_catalog.category_code': 'POWER'}), SOURCE)
        with self.assertRaises(SessionPopulationMismatch):
            compile_sessions(resolved, CONTEXT)
        self.assertEqual(resolved['metric_requirements'], ['sessions_count'])

    def test_scoped_mixed_request_keeps_payment_and_conflicting_sessions_independent(self):
        from app.querying.query_scope import partition_registered_request
        query = self.request('paid_goods_gmv_gross', metric_requirements=['paid_goods_gmv_gross', 'sessions_count'],
            dimensions=['month'], conditions={'months': ['2026-06', '2026-07'], 'sku_catalog.category_code': 'POWER'})
        parts = partition_registered_request(query, SOURCE)
        self.assertEqual(len(parts), 2)
        compile_paid_goods(parts[0], CONTEXT)
        with self.assertRaises(SessionPopulationMismatch):
            compile_sessions(complete_registered_request(parts[1], SOURCE), CONTEXT)
        self.assertTrue(all(p['conditions'] == query['conditions'] for p in parts))

    def test_category_metric_cannot_fall_back_to_sql_for_a_different_sku_population(self):
        query = self.request('category_view_session_cvr', dimensions=['month', 'sku_catalog.sku_id'],
            conditions={'months': ['2026-06', '2026-07'], 'sku_catalog.category_code': 'POWER'})
        with self.assertRaises(SessionPopulationMismatch) as caught:
            compile_sessions(complete_registered_request(query, SOURCE), CONTEXT)
        self.assertEqual(caught.exception.alternative_metric_ids, ['sku_view_session_cvr'])


class ResilientDeliveryTest(unittest.TestCase):
    setUp = legacy.ReactHarnessTest.setUp
    harness = legacy.ReactHarnessTest.harness
    run_bounded = legacy.ReactHarnessTest.run_bounded
    state = staticmethod(legacy.ReactHarnessTest.state)

    def test_v4_user_question_is_visible_and_keeps_the_saved_budget(self):
        from app.foundry_adapter.projection import result_text
        h, _ = self.harness([])
        state = self.state()
        state['investigation_protocol_version'] = 4
        budget = RunBudget('T1', react_rounds=2, tools=3)
        with patch.object(h, '_call', return_value=legacy.action('clarify', parameter='scope',
                question='这次比较全店，还是只看线上渠道？', reason='范围会改变比较结果')):
            result, _ = self.run_bounded(h, state, budget=budget)
        self.assertEqual(result.status, 'waiting_clarification')
        self.assertIn('这次比较全店', result_text(result.model_dump()))
        self.assertEqual((budget.react_rounds, budget.tools), (2, 3))
        restored, _ = self.harness([])
        restored.restore('T1', state['access_scope']['user_id'])
        self.assertEqual(restored.snapshot('T1').clarification, result.clarification)
        self.assertEqual(restored.workflow.budgets['T1'].react_rounds, 2)

    def saved_task(self):
        h, model = self.harness([])
        state = self.state()
        task = ReactRecord(state)
        h.records['T1'] = task
        h._inherit_semantics(task, state)
        token = current_budget.set(RunBudget('T1'))
        try:
            outcome = h._query(task, legacy.query(), '核实已有数据')
        finally:
            current_budget.reset(token)
        self.assertEqual(outcome['status'], 'completed', outcome)
        task.protocol_version = 4
        return h, model, task

    def test_eight_rounds_and_failed_model_preserve_results_without_fake_report(self):
        h, model, task = self.saved_task()
        task.progress['core_questions'] = [{'id': 'Q1', 'question': '下降原因是什么', 'status': 'open'}]
        budget = RunBudget('T1', react_rounds=8)
        model.outputs = [{'action_reason': '取证次数用尽，整理已有结果', 'action': {'type': 'deliver'}}]
        with patch.object(h, '_deliver', side_effect=RuntimeError('provider unavailable')):
            result, _ = self.run_bounded(h, task.state, budget=budget)
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.final_reviewed)
        self.assertIsNone(result.report)
        self.assertTrue(result.evidence)
        self.assertIn('进展已保存', result.message)
        self.assertEqual(len(model.calls), 1, '允许一次收尾决策，但不新增取证')
        self.assertEqual(budget.react_rounds, 8)
        restored, _ = self.harness([])
        restored.restore('T1', task.state['access_scope']['user_id'])
        self.assertEqual(restored.snapshot('T1').report, result.report)

    def test_exhausted_budget_does_not_fake_agent_delivery_or_reset_budget(self):
        h, model, task = self.saved_task()
        result, _ = self.run_bounded(h, task.state, budget=RunBudget('T1', spent=480))
        self.assertEqual(result.status, 'partial')
        self.assertIsNone(result.report)
        self.assertTrue(result.evidence)
        self.assertEqual(model.calls, [])

    def test_rejected_report_is_revised_by_agent_and_not_a_template(self):
        h, _, task = self.saved_task()
        task.reviews = [{'completion_check': {'metric_scope_matches': False}, 'reason': '单位错误'}]
        before = deepcopy(task.evidence)
        def deliver(record, *, stopping):
            self.assertTrue(stopping)
            self.assertIn('单位错误', record.feedback)
            self.assertEqual(record.observation['type'], 'delivery_repair')
            record.approved_text = '渠道变化集中在自然流量。'
            record.approved_report = {'title': '渠道经营分析', 'markdown': record.approved_text}
            record.final_reviewed = True
            record.delivery_kind = 'reviewed_report'
            record.final_status = 'partial'
        with patch.object(h, '_deliver', side_effect=deliver) as draft:
            h._deliver_saved_results(task)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(task.approved_text, '渠道变化集中在自然流量。')
        self.assertEqual(task.evidence, before)

    def test_report_only_review_repair_closes_investigation_without_invalidating_evidence(self):
        h, _, task = self.saved_task()
        from app.querying.data_qa_agent import DataQaAgent
        from app.skills import SkillRegistry
        h.workflow.data_qa_agent = DataQaAgent(None, lambda *_: None, SkillRegistry().get('data_qa'))
        before = deepcopy(task.evidence)
        decision = legacy.review(action='partial', approved_text='', text_edits=[],
            approve_draft_unchanged=False, can_repair=True, repair_scope='report',
            repair_requests=[{'target':'report.markdown','instruction':'核对正文金额单位换算'}],
            question_assessments=[], explanation_coverage_ack=True)
        with patch.object(h, '_call', side_effect=[{'draft':'已有发现[T1:R1]'}, decision]) as model:
            h._deliver(task)
        self.assertEqual([call.args[1] for call in model.call_args_list], ['draft','delivery_review'])
        self.assertEqual(task.final_status, 'partial', '终止主调查循环，进入统一报告修订')
        self.assertEqual(task.reviews[-1]['controller_action'], 'report_revision')
        self.assertEqual(task.evidence, before)
        self.assertEqual(task.observation['type'], 'delivery_repair')

    def test_failed_revision_never_publishes_unreviewed_draft(self):
        h, _, task = self.saved_task()
        task.draft = '错误的因果结论'
        with patch.object(h, '_deliver', side_effect=RuntimeError('provider unavailable')) as draft:
            h._deliver_saved_results(task)
        self.assertEqual(draft.call_count, 1)
        self.assertIsNone(task.approved_report)
        self.assertEqual(task.approved_text, '')
        self.assertTrue(task.evidence)

    def test_closing_reserve_keeps_existing_budget(self):
        h, model, task = self.saved_task()
        budget = RunBudget('T1', spent=361, react_rounds=3, tools=4)
        def deliver(record, *, stopping=False):
            self.assertTrue(stopping)
            record.final_status = 'partial'
            record.final_reviewed = True
            record.approved_text = '当前结论来自已经保存的查询结果。'
        with patch.object(h, '_deliver', side_effect=deliver) as draft:
            result, _ = self.run_bounded(h, task.state, budget=budget)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual((budget.react_rounds, budget.tools), (3, 4))
        self.assertGreaterEqual(budget.elapsed(), 361)
        self.assertEqual(model.calls, [])

    def test_model_sql_outage_is_a_local_outcome_not_task_stop(self):
        h, _, task = self.saved_task()
        module = Mock()
        module.generate_batch.side_effect = RuntimeError('provider read timeout')
        request = {'queries': [{'goal': '探索其他因素', 'metric_requirements': ['other_metric'],
                               'dimensions': [], 'conditions': {}}]}
        budget = RunBudget('T1')
        token = current_budget.set(budget)
        try:
            with patch.object(h, '_prepare_sql_context', return_value={}), patch.object(h, '_sql_module', return_value=module):
                h._business_query_batch(task, request, '继续调查')
        finally:
            current_budget.reset(token)
        self.assertEqual(task.observation['queries'][0]['status'], 'interrupted')
        self.assertEqual(len(task.evidence), 1)
        self.assertEqual(task.final_status, '')
        self.assertEqual(module.generate_batch.call_count, 1)
        self.assertEqual(budget.react_rounds, 1)

    def test_exploratory_flag_does_not_redefine_a_registered_session_population(self):
        h, _, task = self.saved_task()
        task.state['datasource_id'] = SOURCE
        request = {'queries': [{'goal': '核实品类流量', 'metric_requirements': ['sessions_count'],
            'execution_mode': 'exploratory', 'dimensions': ['month'],
            'conditions': {'months': ['2026-06', '2026-07'], 'category_code': 'POWER'}}]}
        token = current_budget.set(RunBudget('T1'))
        try:
            with patch.object(h, '_prepare_sql_context', return_value=CONTEXT), \
                    patch.object(h, '_persist'), patch.object(h, '_sql_module') as module:
                h._business_query_batch(task, request, '继续调查')
                module.assert_not_called()
        finally:
            current_budget.reset(token)
        self.assertEqual(task.observation['queries'][0]['status'], 'needs_method_decision')
        self.assertEqual(len(task.evidence), 1)

    def test_preparation_failure_keeps_other_queries(self):
        h, _, task = self.saved_task()
        bound = [({'id': 'A2'}, {'goal': 'bad'}), ({'id': 'A3'}, {'goal': 'good'})]
        def prepare(_task, requests):
            if any(q['goal'] == 'bad' for q in requests):
                raise ValueError('missing scoped metadata')
            return {'valid': True}
        with patch.object(h, '_prepare_sql_context', side_effect=prepare):
            context, survivors, failures = h._prepare_independent_queries(task, bound)
        self.assertTrue(context['valid'])
        self.assertEqual([a['id'] for a, _ in survivors], ['A3'])
        self.assertEqual(list(failures), ['A2'])


if __name__ == '__main__':
    unittest.main()
