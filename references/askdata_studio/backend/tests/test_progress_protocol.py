from copy import deepcopy
import unittest

from app.workflows.investigation_progress import merge_progress_records
from app.workflows.progress_protocol import normalize_progress, output_schema, resolve_references


EVIDENCE = [
    {'id': 'T:R1', 'check_status': 'basic_valid'},
    {'id': 'T:R2', 'check_status': 'reviewed'},
    {'id': 'T:R3', 'check_status': 'superseded'},
    {'id': 'T:R4', 'check_status': 'failed'},
    {'id': 'T:R5', 'check_status': 'needs_review'},
]


class ProgressProtocolTest(unittest.TestCase):
    def normalize(self, patch, existing=None):
        return normalize_progress(patch, existing_progress=existing or {}, evidence=EVIDENCE)

    def test_real_shape_facts_are_expanded_in_all_dependent_records(self):
        patch = {
            'facts': [
                {'id': 'F1', 'text': 'GMV下降', 'evidence_ids': ['T:R1']},
                {'id': 'F2', 'text': '销量也下降', 'evidence_ids': ['T:R1', 'T:R2']},
                {'id': 'F3', 'text': '客单价上升', 'evidence_ids': ['T:R1']},
            ],
            'hypotheses': [{'id': 'H1', 'text': '优先检查转化', 'status': 'open',
                            'evidence_ids': ['F1', 'F2', 'F3']}],
            'core_questions': [{'id': 'Q1', 'question': '为什么下降？', 'importance': 'core',
                                'status': 'open', 'evidence_ids': ['F1', 'F2', 'F3']}],
            'explanations': [{'id': 'E1', 'text': '销量影响', 'status': 'supported',
                              'supporting_evidence_ids': ['F1', 'F2'],
                              'refuting_evidence_ids': ['F3'], 'boundary': '只说明数量关联'}],
        }
        before = deepcopy(patch)
        result = self.normalize(patch)
        self.assertFalse(result['rejected_fields'])
        self.assertEqual(result['progress']['hypotheses'][0]['evidence_ids'], ['T:R1', 'T:R2'])
        self.assertEqual(result['progress']['hypotheses'][0]['fact_ids'], ['F1', 'F2', 'F3'])
        self.assertEqual(result['progress']['explanations'][0]['supporting_fact_ids'], ['F1', 'F2'])
        self.assertEqual(result['progress']['explanations'][0]['refuting_evidence_ids'], ['T:R1'])
        self.assertEqual(patch, before)

    def test_candidate_order_and_recursive_existing_fact_lineage(self):
        result = self.normalize({'facts': [
            {'id': 'F3', 'text': '第三层', 'evidence_ids': ['F2']},
            {'id': 'F2', 'text': '第二层', 'evidence_ids': ['F1']},
        ]}, {'facts': [{'id': 'F1', 'text': '第一层', 'evidence_ids': ['T:R1']}]})
        self.assertFalse(result['rejected_fields'])
        self.assertEqual(result['progress']['facts'][0]['evidence_ids'], ['T:R1'])
        self.assertEqual(result['progress']['facts'][0]['fact_ids'], ['F2', 'F1'])
        resolved = resolve_references(['F3'], result['fact_lookup'], EVIDENCE)
        self.assertTrue(resolved['safe'])
        self.assertEqual(resolved['evidence_ids'], ['T:R1'])

    def test_short_result_id_only_if_unique(self):
        result = resolve_references(['R1'], {}, EVIDENCE)
        self.assertTrue(result['safe'])
        self.assertEqual(result['evidence_ids'], ['T:R1'])
        ambiguous = resolve_references(['R1'], {}, [*EVIDENCE, {'id': 'U:R1', 'check_status': 'basic_valid'}])
        self.assertFalse(ambiguous['safe'])
        self.assertEqual(ambiguous['errors'][0]['code'], 'ambiguous_result')

    def test_cannot_promote_hypothesis_or_guess_fact_from_result_number(self):
        for ref, lookup in [('H1', {'H1': {'evidence_ids': ['T:R1']}}), ('F1', {})]:
            with self.subTest(ref=ref):
                resolved = resolve_references([ref], lookup, EVIDENCE)
                self.assertFalse(resolved['safe'])
                self.assertFalse(resolved['evidence_ids'])

    def test_result_validation_is_required(self):
        for ref in ['T:R3', 'T:R4', 'T:R5', 'T:R6']:
            self.assertFalse(resolve_references([ref], {}, EVIDENCE)['safe'])
        self.assertFalse(resolve_references(['T:R1'], {}, [{'id': 'T:R1'}])['safe'])
        self.assertFalse(resolve_references(['T:R1'], {}, ['T:R1'])['safe'])
        self.assertTrue(resolve_references(['T:R1'], {}, {'T:R1'})['safe'])

    def test_duplicate_result_ids_are_not_authenticated(self):
        for status in ('superseded', 'basic_valid'):
            with self.subTest(status=status):
                evidence = [*EVIDENCE, {'id': 'T:R1', 'check_status': status}]
                for ref in ('T:R1', 'R1'):
                    resolved = resolve_references([ref], {}, evidence)
                    self.assertFalse(resolved['safe'])
                    self.assertFalse(resolved['evidence_ids'])

    def test_fact_id_cannot_shadow_result_or_short_result_id(self):
        for fid in ['R1', 'T:R1']:
            result = self.normalize({'facts': [{'id': fid, 'text': '冲突', 'evidence_ids': ['T:R2']}]})
            self.assertEqual(result['progress']['facts'], [])
            self.assertIn(fid, result['unsafe_fact_ids'])
            resolved = resolve_references([fid], {fid: {'evidence_ids': ['T:R2']}}, EVIDENCE)
            self.assertFalse(resolved['safe'])
            self.assertEqual(resolved['errors'][0]['code'], 'ambiguous_reference')

    def test_optional_null_means_no_change_and_extra_goals_survive(self):
        result = self.normalize({'explanations': None, 'hypotheses': None, 'goal': '解释指定店铺',
                                 'facts': [{'text': '销量下降', 'evidence_ids': 'R1', 'goal': '核实销量'}]})
        self.assertNotIn('explanations', result['progress'])
        self.assertNotIn('hypotheses', result['progress'])
        self.assertEqual(result['progress']['goal'], '解释指定店铺')
        self.assertEqual(result['progress']['facts'][0]['goal'], '核实销量')
        self.assertEqual(result['progress']['facts'][0]['evidence_ids'], ['T:R1'])

    def test_bad_optional_item_does_not_drop_valid_notes_or_action_inputs(self):
        result = self.normalize({
            'facts': [{'id': 'F1', 'text': '有效事实', 'evidence_ids': ['T:R1']}],
            'explanations': [None, {'id': 'E1', 'text': '有效待验证解释', 'status': 'open'},
                             {'id': 'E2', 'text': '无证据的因果', 'status': 'supported'}],
        })
        self.assertEqual(len(result['progress']['facts']), 1)
        self.assertEqual([item['id'] for item in result['progress']['explanations']], ['E1'])
        self.assertEqual(len(result['rejected_fields']), 2)
        self.assertTrue(result['action_references_safe'])
        self.assertTrue(resolve_references(['F1'], result['fact_lookup'], EVIDENCE)['safe'])

    def test_invalid_current_fact_update_shadows_previous_version(self):
        old = {'facts': [{'id': 'F1', 'text': '旧事实', 'evidence_ids': ['T:R1']}]}
        result = self.normalize({'facts': [{'id': 'F1', 'evidence_ids': ['T:R3']}]}, old)
        self.assertEqual(result['progress']['facts'], [])
        self.assertIn('F1', result['unsafe_fact_ids'])
        self.assertFalse(resolve_references(['F1'], result['fact_lookup'], EVIDENCE)['safe'])
        self.assertEqual(old['facts'][0]['text'], '旧事实')

    def test_fact_cycles_and_rejection_propagation_preserve_independent_facts(self):
        result = self.normalize({'facts': [
            {'id': 'F1', 'text': '循环一', 'evidence_ids': ['F2']},
            {'id': 'F2', 'text': '循环二', 'evidence_ids': ['F1']},
            {'id': 'F3', 'text': '独立', 'evidence_ids': ['T:R1']},
            {'id': 'F4', 'text': '没有证据', 'evidence_ids': []},
        ]})
        self.assertEqual([item['id'] for item in result['progress']['facts']], ['F3'])
        self.assertEqual(set(result['unsafe_fact_ids']), {'F1', 'F2', 'F4'})

    def test_duplicate_conflicting_fact_ids_are_not_arbitrarily_chosen(self):
        result = self.normalize({'facts': [
            {'id': 'F1', 'text': '一个版本', 'evidence_ids': ['T:R1']},
            {'id': 'F1', 'text': '另一个版本', 'evidence_ids': ['T:R2']},
        ]})
        self.assertEqual(result['progress']['facts'], [])
        self.assertFalse(resolve_references(['F1'], result['fact_lookup'], EVIDENCE)['safe'])

    def test_core_question_updates_inherit_fields_and_cannot_downgrade(self):
        existing = {'core_questions': [{'id': 'Q1', 'question': '为什么下滑', 'importance': 'core',
                                       'status': 'open', 'evidence_ids': []}]}
        result = self.normalize({'core_questions': [
            {'id': 'Q1', 'status': 'answered', 'evidence_ids': ['R1'], 'reason': '已取得充分证据'},
            {'id': 'Q1', 'importance': 'optional'},
        ]}, existing)
        self.assertEqual(len(result['progress']['core_questions']), 1)
        question = result['progress']['core_questions'][0]
        self.assertEqual(question['question'], '为什么下滑')
        self.assertEqual(question['importance'], 'core')
        self.assertEqual(question['status'], 'answered')
        self.assertEqual(len(result['rejected_fields']), 1)
        merge_progress_records(existing, result['progress'], {'T:R1', 'T:R2'})

    def test_new_core_question_bad_closure_or_reference_keeps_open_gap(self):
        for changes in [{'evidence_ids': ['unknown']}, {'evidence_ids': []}, {'reason': None}]:
            with self.subTest(changes=changes):
                question = {'id': 'Q2', 'question': '是否还有其他核心原因？', 'importance': 'core',
                            'status': 'answered', 'evidence_ids': ['T:R1'], 'reason': '核对'}
                question.update(changes)
                result = self.normalize({'core_questions': [question]})
                self.assertTrue(result['rejected_fields'])
                kept = result['progress']['core_questions'][0]
                self.assertEqual(kept['question'], question['question'])
                self.assertEqual(kept['status'], 'open')
                self.assertEqual(kept['importance'], 'core')
                self.assertEqual(kept['evidence_ids'], [])

    def test_original_question_and_explanation_evidence_gates_remain(self):
        existing = {'core_questions': [{'id': 'Q1', 'question': '原问题', 'importance': 'core',
                                       'original': True, 'status': 'open', 'evidence_ids': []}]}
        result = self.normalize({'core_questions': [{'id': 'Q1', 'question': '缩小范围'}],
                                 'explanations': [{'id': 'E1', 'text': '错误关闭', 'status': 'refuted'}]}, existing)
        self.assertEqual(result['progress']['core_questions'], [])
        self.assertEqual(result['progress']['explanations'], [])
        self.assertEqual(len(result['rejected_fields']), 2)

    def test_bad_types_are_local_errors_instead_of_exceptions(self):
        result = self.normalize({'facts': [{'id': ['F1'], 'text': '错误编号', 'evidence_ids': ['R1']},
                                           {'id': 'F2', 'text': '有效', 'evidence_ids': ['R1']}],
                                 'hypotheses': [{'id': 'H1', 'text': '错误引用', 'evidence_ids': [12]}],
                                 'unresolved_questions': ['有效问题', 123]})
        self.assertEqual([item['id'] for item in result['progress']['facts']], ['F2'])
        self.assertEqual(result['progress']['hypotheses'], [])
        self.assertEqual(result['progress']['unresolved_questions'], ['有效问题'])

    def test_replacement_evidence_does_not_inherit_superseded_fact_lineage(self):
        existing = {'facts': [{'id': 'F1', 'text': '旧事实', 'status': 'invalidated', 'evidence_ids': ['T:R1']},
                              {'id': 'F2', 'text': '旧下游', 'evidence_ids': ['T:R1'], 'fact_ids': ['F1']}]}
        result = self.normalize({'facts': [{'id': 'F2', 'text': '使用新证据', 'evidence_ids': ['T:R2']}]}, existing)
        self.assertFalse(result['rejected_fields'])
        self.assertNotIn('fact_ids', result['progress']['facts'][0])
        self.assertTrue(resolve_references(['F2'], result['fact_lookup'], EVIDENCE)['safe'])

    def test_schema_is_typed_nullable_patch_schema(self):
        schema = output_schema()
        self.assertIn('CoreQuestionUpdate', schema['$defs'])
        self.assertNotIn('question', schema['$defs']['CoreQuestionUpdate'].get('required', []))
        self.assertEqual(schema['$defs']['CoreQuestionUpdate']['required'], ['id'])
        self.assertIn({'type': 'null'}, schema['properties']['explanations']['anyOf'])
        self.assertEqual(schema['$defs']['FactUpdate']['properties']['evidence_ids']['anyOf'][0]['items'], {'type': 'string'})


if __name__ == '__main__':
    unittest.main()
