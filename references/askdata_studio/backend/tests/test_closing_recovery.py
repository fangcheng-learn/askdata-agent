"""Deterministic regressions for exhausted dependencies and delivery loops."""
import json
import unittest

from app.errors import PipelineStageError
from app.runtime_budget import RunBudget, current_budget
from app.workflows.harness import TaskRecord
import test_harness as fixture


class ClosingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.helper = fixture.HarnessTest()

    def record(self):
        task = TaskRecord(self.helper.state(), [fixture.QUESTION])
        task.steps = [dict(**fixture.step(), status='completed', result_ids=['T1:R1']),
                      dict(**fixture.step('S2', ['S1']), status='failed', result_ids=[]),
                      dict(**fixture.step('S3', ['S2']), status='blocked', result_ids=[])]
        task.evidence = [dict(id='T1:R1', step_id='S1', check_status='basic_valid', complete=True,
                             columns=['v'], rows=[{'v': 3}], task_id='T1', user_id='admin',
                             datasource_id='homenova_20260831')]
        task.draft = '只有总体事实，未完成下钻，不能交付完整原因结论。'
        return task

    def test_exhausted_upstream_blocks_repairs_to_descendant_and_accepts_bounded_partial(self):
        for action in ['complete', 'partial']:
            final = fixture.reviewed(action, ids=['T1:R1'], final=True)
            final.update(affected_steps=['S2', 'S3'], issue_type='execution', reason='总体可答，下钻仍失败')
            model = fixture.ScriptModel([final])
            harness, _ = self.helper.harness(model)
            task = self.record()
            budget = RunBudget('T1', repairs={'S2': 2}); budget.start()
            token = current_budget.set(budget)
            try:
                harness._delivery_review({'record': task})
            finally:
                current_budget.reset(token)
            with self.subTest(action=action):
                self.assertEqual(task.next_node, 'end')
                self.assertEqual(task.final_status, 'partial')
                self.assertTrue(task.final_reviewed)
                self.assertEqual(task.approved_text, final['approved_text'])
                self.assertEqual(budget.repairs, {'S2': 2})
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(len(task.reviews), 1)

    def test_repair_returns_to_recoverable_root_and_preserves_independent_evidence(self):
        harness, _ = self.helper.harness(fixture.ScriptModel([]))
        task = self.record()
        task.steps.append(dict(**fixture.step('S4'), status='completed', result_ids=['T1:R2']))
        independent = {**task.evidence[0], 'id': 'T1:R2', 'step_id': 'S4'}
        task.evidence.append(independent)
        final = fixture.reviewed('repair', ids=['T1:R1'])
        final.update(affected_steps=['S3'], unanswered=[fixture.QUESTION], issue_type='execution')
        from app.workflows.harness import Review
        budget = RunBudget('T1'); budget.start(); token = current_budget.set(budget)
        try:
            self.assertTrue(harness._review_work(task, Review.model_validate(final)))
            self.assertEqual(task.pending_review['affected_steps'], ['S2'])
            harness._controller({'record': task})
        finally:
            current_budget.reset(token)
        self.assertEqual([s['status'] for s in task.steps], ['completed', 'pending', 'pending', 'completed'])
        self.assertEqual(budget.repairs, {'S2': 1})
        self.assertEqual(independent['check_status'], 'basic_valid')
        self.assertEqual(task.steps[3]['result_ids'], ['T1:R2'])

    def test_inner_no_progress_is_terminal_for_same_inputs_not_an_outer_retry(self):
        error = PipelineStageError('query_contract_validation', 'same invalid output', {'no_progress': True})
        model = fixture.ScriptModel([dict(conditions={}, steps=[fixture.step()])])
        harness, calls = self.helper.harness(model, [error])
        budget = RunBudget('T1')
        result = self.helper.bounded(harness, self.helper.state(), budget=budget)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.repairs, {})
        task = harness.records['T1']
        self.assertEqual(harness._repair_roots(task, ['S1']), [])
        harness.invalidate(task, 'S1')  # changed upstream/plan can explicitly invalidate this local event
        self.assertEqual(harness._repair_roots(task, ['S1']), ['S1'])

    def test_same_review_inputs_are_not_reviewed_twice_but_changed_claim_is(self):
        from app.runtime_budget import BudgetStop
        harness, _ = self.helper.harness(fixture.ScriptModel([]))
        task = self.record(); calls = []
        def review(record, final):
            from app.workflows.harness import Review
            output = fixture.reviewed('partial', ids=['T1:R1'], final=True)
            record.reviews.append({'final': True, **output}); calls.append(record.draft)
            return Review.model_validate(output)
        harness._review_call = review
        harness._review_work = lambda *args: False
        task.draft = '结果73765c84d353:R1显示3。'
        harness._delivery_review({'record': task})
        task.draft = '结果73765c84d353:R2 显示 3。\n'
        with self.assertRaises(BudgetStop):
            harness._delivery_review({'record': task})
        task.draft = '结果73765c84d353:R2显示4。'
        harness._delivery_review({'record': task})
        self.assertEqual(len(calls), 2)

    def test_analysis_and_query_inputs_do_not_expose_storage_reference(self):
        task = self.record()
        task.steps[1].update(status='pending', kind='analysis', input_result_ids=[])
        task.steps = task.steps[:2]
        task.evidence[0]['result_ref'] = 'internal-reference'
        proposal = dict(action='execute_step', evidence_ids=['internal-reference'], reason='只分析当前输入')
        model = fixture.ScriptModel([dict(step_decision=proposal, evidence_ids=['internal-reference'],
                                         columns=['v'], rows=[{'v': 3}], findings='总体事实')])
        harness, _ = self.helper.harness(model)
        budget = RunBudget('T1'); budget.start(); token = current_budget.set(budget)
        try:
            harness._execute({'record': task})
        finally:
            current_budget.reset(token)
        self.assertEqual(task.steps[1]['status'], 'completed')
        self.assertEqual(task.evidence[-1]['evidence_ids'], ['T1:R1'])
        self.assertNotIn('result_ref', model.calls[0][1])
        self.assertIn('T1:R1', model.calls[0][1])
        self.assertEqual(task.evidence[0]['result_ref'], 'internal-reference')

    def test_cross_task_reuse_gets_current_identity_without_weakening_reference_reads(self):
        from app.result_references import resolve_result_ids
        task = TaskRecord(self.helper.state(), [fixture.QUESTION])
        task.state['query'] = '复用已确认总体'
        task.steps = [dict(**fixture.step(), status='pending', result_ids=[], input_result_ids=[], reuse_result_id='OLD:R1')]
        old = dict(id='OLD:R1', task_id='OLD', user_id='admin', datasource_id='homenova_20260831',
                   goal=task.steps[0]['goal'], conditions={}, check_status='reviewed', complete=True,
                   columns=['v'], rows=[{'v': 3}], total_rows=1, sql='SELECT 3 AS v', result_ref='old-ref', snapshot={})
        task.state['reusable_evidence'] = [old]
        harness, calls = self.helper.harness(fixture.ScriptModel([]))
        harness.workflow.database_engine.results['old-ref'] = dict(user_id='admin', task_id='OLD', datasource_id='homenova_20260831',
                                                                  rows=old['rows'], columns=['v'], complete=True, total_rows=1)
        budget = RunBudget('T1'); budget.start(); token = current_budget.set(budget)
        try:
            harness._execute({'record': task})
        finally:
            current_budget.reset(token)
        self.assertEqual(calls, [])
        current = task.evidence[0]
        self.assertEqual(current['id'], 'T1:R1')
        self.assertEqual(current['task_id'], 'T1')
        self.assertEqual(current['source_result_id'], 'OLD:R1')
        self.assertNotEqual(current['result_ref'], old['result_ref'])
        context = harness._reference_context(task, task.evidence)
        self.assertEqual(resolve_result_ids([current['result_ref']], context), ['T1:R1'])
        engine = harness.workflow.database_engine
        self.assertEqual(engine.read_result(current['result_ref'], 'admin', 'T1', 'homenova_20260831')['rows'], [{'v': 3}])
        with self.assertRaises(PermissionError):
            engine.read_result('old-ref', 'admin', 'T1', 'homenova_20260831')
        with self.assertRaises(PermissionError):
            engine.clone_result_reference('old-ref', 'other', 'OLD', 'T1', 'homenova_20260831')
        self.assertEqual(old['task_id'], 'OLD')

    def test_model_projection_does_not_remove_business_columns_named_result_ref(self):
        from app.result_references import model_result_context
        record = dict(id='T1:R1', check_status='basic_valid', result_ref='internal-id', columns=['result_ref'],
                      rows=[{'result_ref': 'actual-cell-value'}])
        projected = model_result_context({'dependency_results': [record]})['dependency_results'][0]
        self.assertNotIn('result_ref', projected)
        self.assertEqual(projected['rows'], [{'result_ref': 'actual-cell-value'}])
        projected['rows'][0]['result_ref'] = 'different'
        self.assertEqual(record['rows'][0]['result_ref'], 'actual-cell-value')
