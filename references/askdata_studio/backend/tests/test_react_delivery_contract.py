"""Offline checks for explicit contradictions in the final review contract.

No model, HTTP, SQL, MCP or business data is used by these tests.
"""
from __future__ import annotations

from copy import deepcopy
import unittest

from app.runtime_budget import RunBudget, current_budget
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_prompts import DRAFT_SYSTEM, REVIEW_SYSTEM


def assessment(**changes):
    return {'goal_status': 'answered', 'metric_scope_matches': True,
            'capability_claims_supported': True, 'remaining_core_gaps': [],
            'completion_basis': '原问题已由适用证据回答', **changes}


def review(**changes):
    return {'action': 'complete', 'reason': '有证据的定稿', 'approved_text': '审核认可的正文',
            'answered': [{'question': '为什么变化', 'evidence_ids': ['T:R1'], 'strength': 'association'}],
            'unanswered': [], 'can_repair': False, 'repair_requests': [],
            'affected_evidence_ids': [], 'completion_check': assessment(), **changes}


class ReactDeliveryContractTest(unittest.TestCase):
    def test_negative_coverage_is_a_repairable_gap_not_invalid_boolean(self):
        from app.workflows.investigation_progress import reconcile_review
        decision={'question_assessments':[{'question_id':'Q1','status':'unresolved','evidence_ids':[],
                   'answer_level':'contribution','reason':'仍需核实解释'}],'explanation_coverage_ack':False}
        outcome=reconcile_review(decision,[{'id':'Q1','question':'为何变化'}],[{'id':'E1'}],set())
        self.assertEqual(outcome['errors'],[])
        self.assertIn('主要解释的支持与反证尚未核对充分',outcome['gaps'])
        decision.pop('explanation_coverage_ack')
        self.assertTrue(reconcile_review(decision,[{'id':'Q1','question':'为何变化'}],[{'id':'E1'}],set())['errors'])

    def deliver(self, response, *, stopping=False, budget=None, evidence=True):
        harness = ReactInvestigationHarness.__new__(ReactInvestigationHarness)
        task = ReactRecord({'task_id': 'T'})
        task.evidence = [{'id': 'T:R1', 'fingerprint': 'F1', 'complete': True,
                          'check_status': 'basic_valid', 'query_contract': {}}] if evidence else []
        if stopping:
            task.stop_reason = '额外修复额度耗尽'
        calls = []
        outputs = iter([{'draft': '待审核正文'}, deepcopy(response)])
        harness._call = lambda task, node, system, payload: (calls.append((node, payload)) or next(outputs))
        harness._context = lambda task, closing=False: {'task': {'required_questions': ['为什么变化']}}
        harness._persist = lambda task: None
        harness._invalidate = lambda task, affected: None
        token = current_budget.set(budget)
        try:
            harness._deliver(task, stopping=stopping)
        finally:
            current_budget.reset(token)
        self.assertEqual([node for node, _ in calls], ['draft', 'delivery_review'])
        return task, calls

    @staticmethod
    def findings(**overrides):
        return [{'criterion': name, 'verdict': 'supported', 'evidence_ids': ['T:R1'],
                 'finding': '实际依据', 'resolved_in_final': False, **overrides.get(name, {})}
                for name in ['metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage']]

    def test_audit_evidence_gap_cannot_be_overruled_by_complete_label(self):
        task, _ = self.deliver(review(audit_findings=self.findings(answer_coverage={
            'verdict': 'missing_evidence', 'finding': '原因尚缺适用的机制证据'})))
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)
        self.assertIn('原因尚缺适用的机制证据', task.stop_reason)
        self.assertEqual(task.reviews[0]['controller_action'], 'partial_delivery')

    def test_method_gap_cannot_be_resolved_by_self_certified_rewrite(self):
        task, _ = self.deliver(review(audit_findings=self.findings(calculation_method={
            'verdict': 'correction_required', 'finding': '结果只有月度共同变化，缺少时段机制证据',
            'resolved_in_final': True})))
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.final_status, '')
        self.assertEqual(task.observation['type'], 'delivery_repair')
        self.assertIn('复用', task.observation['requests'][0]['instruction'])
        self.assertEqual(task.reviews[0]['action'], 'complete')

    def test_stopping_does_not_publish_unfixed_method_error(self):
        task, _ = self.deliver(review(audit_findings=self.findings(calculation_method={
            'verdict': 'correction_required', 'finding': '实际计算不支持原稿公式',
            'resolved_in_final': True})), stopping=True)
        self.assertEqual(task.final_status, 'partial')
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')
        self.assertTrue(task.reviews[-1]['controller_repair_requests'])

    def test_unavailable_evidence_can_finish_as_bounded_partial(self):
        task, _ = self.deliver(review(action='partial',
            audit_findings=self.findings(answer_coverage={
                'verdict': 'missing_evidence', 'finding': '数据源没有网站性能日志，无法继续核实性能影响'}),
            completion_check=assessment(goal_status='partial', remaining_core_gaps=['网站性能影响未知'])))
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)
        self.assertNotIn('controller_repair_requests', task.reviews[-1])

    def test_partial_with_method_repair_records_controller_action(self):
        task, _ = self.deliver(review(action='partial', audit_findings=self.findings(calculation_method={
            'verdict': 'correction_required', 'finding': '实际方法仍需核对', 'resolved_in_final': True})))
        self.assertEqual(task.final_status, '')
        self.assertEqual(task.reviews[-1]['action'], 'partial')
        self.assertEqual(task.reviews[-1]['controller_action'], 'repair')
        self.assertTrue(task.reviews[-1]['controller_repair_requests'])

    def test_resolved_prose_claim_requires_matching_applied_edit(self):
        task, _ = self.deliver(review(approved_text='', text_edits=[],
            audit_findings=self.findings(claim_strength={
                'verdict': 'correction_required', 'finding': '需删除无依据的因果断言',
                'resolved_in_final': True})))
        self.assertFalse(task.final_reviewed)

    def test_audit_uncorrected_claim_never_publishes_original_draft(self):
        task, _ = self.deliver(review(approve_draft_unchanged=True, approved_text='',
            audit_findings=self.findings(claim_strength={'verdict': 'correction_required',
                'finding': '仅有同时变化，原稿写成直接因果', 'resolved_in_final': True})))
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')

    def test_audit_findings_with_repair_path_return_to_investigation(self):
        task, _ = self.deliver(review(audit_findings=self.findings(calculation_method={
            'verdict': 'correction_required', 'finding': '累计状态缺少期初'}),
            can_repair=True, repair_requests=['补齐期初后重新计算']))
        self.assertEqual(task.final_status, '')
        self.assertEqual(task.observation['type'], 'delivery_repair')
        self.assertFalse(task.final_reviewed)

    def test_approved_unchanged_draft_is_displayed_exactly_without_third_call(self):
        task, calls = self.deliver(review(approved_text='', approve_draft_unchanged=True))
        self.assertEqual(task.approved_text, '待审核正文')
        self.assertTrue(task.final_reviewed)
        self.assertEqual(task.final_status, 'completed')
        self.assertEqual(len(calls), 2)

    def test_unchanged_flag_cannot_replace_text_or_override_semantic_failure(self):
        task, _ = self.deliver(review(approve_draft_unchanged=True, approved_text='different'))
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')
        task, _ = self.deliver(review(approve_draft_unchanged=True, approved_text='',
                                      completion_check=assessment(metric_scope_matches=False)))
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')

    def test_complete_label_with_gap_delivers_approved_partial_without_reinvestigation(self):
        task, _ = self.deliver(review(
            unanswered=['原问题的必要解释尚未核实'], can_repair=True,
            repair_requests=[{'question': '补取可区分的证据'}]))
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)
        self.assertIn('原问题的必要解释尚未核实', task.stop_reason)
        self.assertEqual(task.reviews[0]['action'], 'complete')  # Preserve model output.
        self.assertEqual(task.reviews[0]['controller_action'], 'partial_delivery')

    def test_stopping_with_necessary_gap_delivers_partial_not_complete(self):
        task, calls = self.deliver(review(
            can_repair=True, repair_requests=[{'question': '尚需补查'}],
            completion_check=assessment(goal_status='partial', remaining_core_gaps=['原因证据未取得'])),
            stopping=True)
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)
        self.assertEqual(task.approved_text, '审核认可的正文')
        self.assertIn('原因证据未取得', task.stop_reason)
        self.assertTrue(calls[-1][1]['delivery_constraints']['investigation_stopped'])

    def test_unanswered_without_feasible_path_cannot_be_complete(self):
        task, _ = self.deliver(review(unanswered=['源覆盖之外的必要时期']))
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)

    def test_partial_coverage_cannot_be_complete_even_if_unanswered_array_empty(self):
        task, _ = self.deliver(review(completion_check=assessment(goal_status='partial')))
        self.assertEqual(task.final_status, 'partial')

    def test_remaining_metric_object_error_never_publishes_the_incorrect_text(self):
        task, _ = self.deliver(review(completion_check=assessment(metric_scope_matches=False)))
        self.assertEqual(task.final_status, 'partial')
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')
        self.assertIn('未展示该稿', task.stop_reason)

    def test_remaining_false_capability_claim_is_not_approved(self):
        task, _ = self.deliver(review(completion_check=assessment(capability_claims_supported=False)))
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')

    def test_technical_stop_with_sufficient_answer_can_still_complete(self):
        task, _ = self.deliver(review(), stopping=True)
        self.assertEqual(task.final_status, 'completed')
        self.assertTrue(task.final_reviewed)

    def test_reliably_refuted_premise_can_complete_after_technical_stop(self):
        task, _ = self.deliver(review(
            answered=[{'question': '为什么变化', 'evidence_ids': ['T:R1'], 'strength': 'not_applicable'}],
            completion_check=assessment(goal_status='not_applicable', completion_basis='可比证据推翻下降前提')),
            stopping=True)
        self.assertEqual(task.final_status, 'completed')

    def test_no_investigation_budget_left_preserves_reviewed_partial(self):
        budget = RunBudget('T', tool_limit=1)
        budget.tools = 1
        task, _ = self.deliver(review(action='repair', can_repair=True,
                                     repair_requests=[{'question': '必要补查'}],
                                     unanswered=['尚缺解释']), budget=budget)
        self.assertEqual(task.final_status, 'partial')
        self.assertTrue(task.final_reviewed)

    def test_new_review_without_completion_check_is_unreviewed_partial(self):
        payload = review()
        payload.pop('completion_check')
        task, _ = self.deliver(payload)
        self.assertEqual(task.final_status, 'partial')
        self.assertFalse(task.final_reviewed)
        self.assertEqual(task.approved_text, '')
        self.assertEqual(task.reviews[-1]['controller_action'], 'reject')

    def test_new_review_missing_check_without_evidence_fails(self):
        payload = review()
        payload.pop('completion_check')
        task, _ = self.deliver(payload, evidence=False)
        self.assertEqual(task.final_status, 'failed')
        self.assertFalse(task.final_reviewed)

    def test_new_review_requires_object_and_every_completion_field(self):
        candidates = [None, [], '', False]
        for key in assessment():
            incomplete = assessment()
            incomplete.pop(key)
            candidates.append(incomplete)
        for value in candidates:
            with self.subTest(value=value):
                task, _ = self.deliver(review(completion_check=value))
                self.assertEqual(task.final_status, 'partial')
                self.assertFalse(task.final_reviewed)
                self.assertEqual(task.approved_text, '')

    def test_completion_field_types_are_not_coerced(self):
        invalid = [('goal_status', 'complete'), ('goal_status', []), ('goal_status', True),
                   ('metric_scope_matches', 'true'), ('metric_scope_matches', 1),
                   ('capability_claims_supported', 'false'), ('capability_claims_supported', 0),
                   ('remaining_core_gaps', '缺口'), ('remaining_core_gaps', {}),
                   ('remaining_core_gaps', [None]), ('remaining_core_gaps', ['']),
                   ('completion_basis', None), ('completion_basis', ''), ('completion_basis', 1)]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                task, _ = self.deliver(review(completion_check=assessment(**{key: value})))
                self.assertEqual(task.final_status, 'partial')
                self.assertFalse(task.final_reviewed)

    def test_historical_saved_snapshot_does_not_require_new_model_contract(self):
        harness = ReactInvestigationHarness.__new__(ReactInvestigationHarness)
        legacy_review = review()
        legacy_review.pop('completion_check')
        task = ReactRecord({'task_id': 'old-task', 'datasource_id': 'source', 'query': '旧问题'})
        task.final_status = 'completed'
        task.final_reviewed = True
        task.approved_text = '历史定稿'
        task.reviews = [legacy_review]
        harness.records = {'old-task': task}
        harness._call = lambda *args: self.fail('读取历史快照不得新增模型调用')
        result = harness.snapshot('old-task')
        self.assertEqual(result.status, 'completed')
        self.assertTrue(result.final_reviewed)
        self.assertEqual(result.analysis, '历史定稿')

    def test_prompt_names_counting_objects_and_unqueried_capabilities(self):
        for prompt in (DRAFT_SYSTEM, REVIEW_SYSTEM):
            self.assertIn('会话', prompt)
            self.assertIn('访客', prompt)
            self.assertIn('尚未', prompt)
        self.assertIn('completion_check', REVIEW_SYSTEM)
        self.assertIn('remaining_core_gaps', REVIEW_SYSTEM)


if __name__ == '__main__':
    unittest.main()
