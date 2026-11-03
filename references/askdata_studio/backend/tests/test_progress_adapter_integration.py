"""Offline protocol/controller checks; not real-model business acceptance."""
from copy import deepcopy
import unittest

from app.workflows.progress_protocol import output_schema
from app.workflows.react_harness import ReactRecord
from tests.test_react_harness import action
from tests import test_diagnosis_reliability_v3 as reliability
from tests.test_sql_batch_controller import business, generated


class ProgressAdapterIntegrationTest(unittest.TestCase):
    setUp = reliability.DiagnosisReliabilityV3Test.setUp
    harness = reliability.DiagnosisReliabilityV3Test.harness
    run_bounded = reliability.DiagnosisReliabilityV3Test.run_bounded
    state = staticmethod(reliability.DiagnosisReliabilityV3Test.state)
    new_run = reliability.DiagnosisReliabilityV3Test.new_run

    def task(self):
        harness, _, state = self.new_run([])
        task = ReactRecord(state, protocol_version=3)
        task.progress['core_questions'] = [{'id': 'Q1', 'question': state['query'],
            'importance': 'core', 'status': 'open', 'original': True, 'evidence_ids': []}]
        task.evidence = [{'id': 'T1:R1', 'check_status': 'basic_valid', 'complete': True,
            'task_id': state['task_id'], 'user_id': state['access_scope']['user_id'],
            'datasource_id': state['datasource_id'], 'snapshot': state.get('scenario') or {}}]
        return harness, task

    def test_same_frame_fact_reference_becomes_actual_result_without_repair(self):
        h, task = self.task()
        patch = {'facts': [{'id': 'F1', 'text': '有真实来源的观察', 'evidence_ids': ['T1:R1']}],
            'hypotheses': [{'id': 'H1', 'text': '尚待进一步验证的解释', 'evidence_ids': ['F1']}],
            'core_questions': [{'id': 'Q1', 'evidence_ids': ['F1']}], 'explanations': None}
        diagnostics = h._update_progress(task, patch)
        self.assertFalse(diagnostics['rejected_fields'])
        self.assertEqual(task.progress['hypotheses'][0]['evidence_ids'], ['T1:R1'])
        self.assertEqual(task.progress['hypotheses'][0]['fact_ids'], ['F1'])
        self.assertEqual(task.progress['core_questions'][0]['evidence_ids'], ['T1:R1'])
        request = {'type': 'query_batch', 'queries': [{'input_result_ids': ['F1']}]}
        normalized = h._normalize_action_references(task, request, diagnostics)
        self.assertEqual(normalized['queries'][0]['input_result_ids'], ['T1:R1'])
        self.assertEqual(request['queries'][0]['input_result_ids'], ['F1'])

    def test_bad_optional_note_does_not_block_independent_real_local_query(self):
        next_query = business(('独立核实订单', 'paid_orders'))
        next_query['progress'] = {'hypotheses': [{'id': 'H1', 'text': '坏的可选笔记',
                                                 'evidence_ids': ['R999']}]}
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            generated(('A2', None)), next_query,
            generated(('A3', 'SELECT SUM(paid_orders) AS value FROM daily_store_metrics', 'paid_orders')),
            action('deliver'), {'draft': '仅验证协议，不认证业务归因'}, reliability.final_review(ids=('T1:R1', 'T1:R2')),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(len(model.calls), 8)
        self.assertEqual(budget.react_rounds, 2)
        self.assertFalse(any('react_protocol' in str(k) for k in budget.repairs))
        self.assertTrue(any(e['stage'] == 'progress_adaptation' for e in h.records['T1'].logs))

    def test_unknown_action_dependency_is_still_blocked(self):
        h, task = self.task()
        with self.assertRaisesRegex(ValueError, 'action.queries'):
            h._normalize_action_references(task, {'type': 'query_batch',
                'queries': [{'input_result_ids': ['F999']}]})
        self.assertEqual(task.actions, [])

    def test_invalid_current_fact_update_cannot_use_old_fact_for_action(self):
        h, task = self.task()
        task.progress['facts'] = [{'id': 'F1', 'text': '旧观察', 'evidence_ids': ['T1:R1']}]
        diagnostics = h._update_progress(task, {'facts': [{'id': 'F1', 'text': '坏更新',
                                                         'evidence_ids': ['T1:R999']}]})
        self.assertEqual(task.progress['facts'][0]['text'], '旧观察')
        with self.assertRaisesRegex(ValueError, 'F1'):
            h._normalize_action_references(task, {'type': 'read_result', 'result_id': 'F1'}, diagnostics)

    def test_cross_scope_evidence_is_never_an_adapter_authority(self):
        for field, value in [('user_id', 'someone_else'), ('task_id', 'other_task'),
                             ('datasource_id', 'other_source'), ('snapshot', {'snapshot_id': 'future'})]:
            with self.subTest(field=field):
                h, task = self.task()
                task.evidence[0][field] = value
                diagnostics = h._update_progress(task, {'facts': [{'id': 'F1', 'text': '越范围观察',
                                                                 'evidence_ids': ['T1:R1']}]})
                self.assertFalse(task.progress['facts'])
                with self.assertRaises(ValueError):
                    h._normalize_action_references(task, {'type': 'read_result', 'result_id': 'T1:R1'}, diagnostics)

    def test_bad_new_core_reference_keeps_the_question_open(self):
        h, task = self.task()
        original = deepcopy(task.progress['core_questions'][0])
        h._update_progress(task, {'core_questions': [
            {'id': 'Q1', 'question': '偷偷缩小问题', 'importance': 'optional'},
            {'id': 'Q2', 'question': '尚需解释的新核心问题', 'importance': 'core',
             'status': 'answered', 'reason': '没有有效依据', 'evidence_ids': ['F999']}]})
        self.assertEqual(task.progress['core_questions'][0], original)
        question = next(q for q in task.progress['core_questions'] if q['id'] == 'Q2')
        self.assertEqual(question['status'], 'open')
        self.assertEqual(question['evidence_ids'], [])

    def test_prompt_uses_the_actual_schema_and_closing_keeps_diagnostics(self):
        h, task = self.task()
        task.evidence = []
        h._update_progress(task, {'hypotheses': ['malformed optional note']})
        context = h._context(task)
        self.assertEqual(context['progress_protocol'], output_schema())
        self.assertTrue(context['progress_validation_feedback']['rejected_fields'])
        closing = h._context(task, closing=True, role='review')
        self.assertNotIn('progress_protocol', closing)
        self.assertIn('progress_validation_feedback', closing)

    def test_failed_evidence_request_stays_visible_in_report_and_review(self):
        h, task = self.task()
        task.evidence = []
        task.actions = [{'id':'A2','status':'failed','goal':'核实事件分布','error':'timeout',
                         'question_ids':['Q1'],'result_ids':[]}]
        for role in ('', 'review'):
            context = h._context(task, closing=True, role=role)
            self.assertEqual(context['unsuccessful_attempts'][0]['goal'],'核实事件分布')
            self.assertEqual(context['unsuccessful_attempts'][0]['result_ids'],[])


if __name__ == '__main__':
    unittest.main()
