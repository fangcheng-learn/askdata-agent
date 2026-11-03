"""Offline regressions for bounded delivery and local report revision."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch
from app.preprocessing import RequestPreprocessor
from app.querying.data_qa_agent import DataQaAgent, ReportRevisionError
from app.skills import SkillRegistry
from app.workflows.investigation_output import output_schema
from tests import test_react_harness as legacy
from tests import test_resilient_delivery as fixtures

class ReportPatchTest(TestCase):
    def test_patch_preserves_correct_paragraphs_and_corrects_numbers(self):
        prior = {'title': '库存导致下降', 'markdown': '正常检查：流量稳定。[R1]\n\n金额为766欧元。[R2]\n\n库存是主因。', 'visualizations': []}
        before = deepcopy(prior)
        result = DataQaAgent._apply_report_revision(prior, {'action': 'revise_report', 'title': '经营变化与待核实线索',
            'markdown_edits': [{'before': '金额为766欧元。', 'after': '金额为7661.78欧元。'},
                               {'before': '库存是主因。', 'after': '库存与下降同时出现，原因仍待核实。'}]})
        self.assertEqual(prior, before)
        self.assertTrue(result['markdown'].startswith('正常检查：流量稳定。[R1]\n\n'))
        self.assertIn('7661.78欧元。[R2]', result['markdown'])
        self.assertNotIn('主因', result['markdown'])

    def test_bad_edits_do_not_silently_corrupt_report(self):
        for edits in [[], [{'before': 'ABC', 'after': 'G'}], [{'before': 'missing', 'after': 'G'}],
                      [{'before': 'ABC DEF', 'after': 'G'}, {'before': 'DEF ABC', 'after': 'H'}],
                      [{'before': 'ABC DEF ABC', 'after': ''}]]:
            with self.subTest(edits=edits), self.assertRaises(ReportRevisionError):
                DataQaAgent._apply_report_revision({'markdown': 'ABC DEF ABC'}, {'markdown_edits': edits})

    def test_wire_schema_separates_author_from_reviewer(self):
        revision = output_schema('report_revision', result_columns={'T:R1': ['month', 'gmv']})
        self.assertIn('markdown_edits', revision['properties']); self.assertNotIn('markdown', revision['properties'])
        review = output_schema('delivery_review')
        self.assertEqual(review['properties']['text_edits']['maxItems'], 0)
        self.assertEqual(review['properties']['approved_text']['enum'], [''])

class ReviewDeliveryTest(TestCase):
    setUp = fixtures.ResilientDeliveryTest.setUp
    harness = fixtures.ResilientDeliveryTest.harness
    state = staticmethod(fixtures.ResilientDeliveryTest.state)
    saved_task = fixtures.ResilientDeliveryTest.saved_task

    def ready(self):
        h, _, task = self.saved_task()
        h.workflow.data_qa_agent = DataQaAgent(None, lambda *_: None, SkillRegistry().get('data_qa'))
        return h, task

    def decision(self, **extra):
        return legacy.review(approved_text='', text_edits=[], approve_draft_unchanged=True,
                             question_assessments=[], explanation_coverage_ack=True, **extra)

    def test_approved_partial_not_blocked_by_further_work(self):
        for repair_scope in ['report', 'evidence', 'mixed']:
            with self.subTest(repair_scope=repair_scope):
                h, task = self.ready()
                decision = self.decision(action='partial', can_repair=True, repair_scope=repair_scope,
                    repair_requests=['可再研究其他原因'], unanswered=['原因未查清'])
                with patch.object(h, '_call', side_effect=[{'draft': '已确认事实[T1:R1]，原因待核实。'}, decision]):
                    h._deliver(task)
                self.assertTrue(task.final_reviewed); self.assertEqual(task.final_status, 'partial')
                self.assertIn('已确认', task.approved_report['markdown'])

    def test_closing_does_not_downgrade_complete_answer(self):
        h, task = self.ready()
        with patch.object(h, '_call', side_effect=[{'draft': '已回答[T1:R1]'}, self.decision()]):
            h._deliver(task, stopping=True)
        self.assertEqual(task.final_status, 'completed'); self.assertTrue(task.final_reviewed)

    def test_error_is_patched_then_reviewed_without_requery(self):
        h, task = self.ready(); before = deepcopy(task.evidence)
        findings = [{'criterion': c, 'verdict': 'correction_required' if c == 'calculation_method' else 'supported',
            'finding': '正文抄错，原始查询正确' if c == 'calculation_method' else '实际证据核对通过',
            'resolved_in_final': False, 'evidence_ids': ['T1:R1']} for c in
            ['metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage']]
        rejection = self.decision(action='repair', can_repair=True, repair_scope='report', audit_findings=findings,
                                  repair_requests=[{'target': 'markdown', 'instruction': '修正抄错金额'}])
        rejection['approve_draft_unchanged'] = False
        outputs = [{'title': '经营情况', 'markdown': '正常检查保持不变。[T1:R1]\n\n金额766欧元。[T1:R1]'}, rejection,
                   {'action': 'revise_report', 'markdown_edits': [{'before': '金额766欧元。', 'after': '金额7661.78欧元。'}]},
                   self.decision(action='partial', unanswered=['原因待核实'])]
        with patch.object(h, '_call', side_effect=outputs) as calls, patch.object(h, '_query', side_effect=AssertionError('No new query')):
            h._deliver(task)
            self.assertFalse(task.final_reviewed)
            self.assertEqual(task.reviews[-1]['controller_repair_requests'], rejection['repair_requests'])
            h._deliver_saved_results(task)
        self.assertTrue(task.final_reviewed)
        self.assertEqual(calls.call_args_list[2].args[3]['mode'], 'report_revision')
        self.assertEqual(task.approved_text, '正常检查保持不变。[T1:R1]\n\n金额7661.78欧元。[T1:R1]')
        self.assertEqual(task.evidence[0]['rows'], before[0]['rows'])

    def test_local_revision_is_bounded_and_never_publishes_a_rejected_draft(self):
        h, task = self.ready()
        task.draft = '初稿'
        def reject(record, *, stopping):
            self.assertTrue(stopping)
            record.draft += '修订'
            record.reviews.append({'controller_action': 'report_revision', 'reason': '还有实质错误'})
            record.final_reviewed = False
        with patch.object(h, '_deliver', side_effect=reject) as calls:
            h._deliver_saved_results(task)
        self.assertEqual(calls.call_count, 2)
        self.assertIsNone(task.approved_report)
        self.assertFalse(task.final_reviewed)

    def test_material_error_still_blocks_publication(self):
        h, task = self.ready()
        decision = self.decision(action='repair', repair_scope='report', can_repair=True,
            completion_check={'goal_status': 'partial', 'metric_scope_matches': False,
                'capability_claims_supported': True, 'remaining_core_gaps': [], 'completion_basis': '对象混淆'})
        with patch.object(h, '_call', side_effect=[{'draft': '错误口径[T1:R1]'}, decision]):
            h._deliver(task)
        self.assertFalse(task.final_reviewed); self.assertIsNone(task.approved_report)
