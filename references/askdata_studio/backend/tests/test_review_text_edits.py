"""Exact final-review edits cannot freely rewrite measured report values."""
from copy import deepcopy
import unittest

from jsonschema import Draft202012Validator

from app.querying.analysis_output import resolve_review_text
from app.workflows.investigation_output import DeliveryReview, output_schema


def edit(original, replacement, criterion='claim_strength'):
    return {'original': original, 'replacement': replacement, 'criterion': criterion}


def review(edits, **changes):
    return {'approved_text': '', 'text_edits': edits, 'approve_draft_unchanged': False, **changes}


class ReviewTextEditsTest(unittest.TestCase):
    def test_live_wording_edit_can_preserve_numbers_without_forcing_empty_placeholders(self):
        definition = output_schema('delivery_review')['$defs']['TextEdit']
        validator = Draft202012Validator(definition)
        original = '金额123元直接证明涨价。'
        correction = edit(original, '金额123元，仅为成交均值，不能证明调价。')
        validator.validate(correction)
        self.assertEqual(resolve_review_text(review([correction]), original), correction['replacement'])
        with self.assertRaisesRegex(ValueError, 'repair'):
            resolve_review_text(review([edit(original, '金额19.99元。')]), original)

    def test_exact_wording_patch_preserves_unedited_text_numbers_and_whitespace(self):
        draft = '\n金额为123.45元。\n\n缺货直接导致下滑。\n'
        original = deepcopy(draft)
        result = resolve_review_text(review([edit('缺货直接导致下滑。', '缺货与下滑同时出现，尚不能确认因果。')]), draft)
        self.assertEqual(result, '\n金额为123.45元。\n\n缺货与下滑同时出现，尚不能确认因果。\n')
        self.assertEqual(draft, original)

    def test_edits_use_original_offsets_and_are_order_independent(self):
        patches = [edit('甲段', '乙段'), edit('乙段', '丙段')]
        self.assertEqual(resolve_review_text(review(patches), '甲段 / 乙段'), '乙段 / 丙段')
        self.assertEqual(resolve_review_text(review(list(reversed(patches))), '甲段 / 乙段'), '乙段 / 丙段')

    def test_original_must_be_nonempty_unique_and_not_overlap_another_edit(self):
        cases = [('重复 重复', [edit('重复', '仅一次')]),
                 ('aaa', [edit('aa', 'b')]),
                 ('正文', [edit('', '内容')]),
                 ('正文 空格', [edit(' ', '：')]),
                 ('正文', [edit('不存在', '内容')]),
                 ('完整段落', [edit('完整段落', '已改'), edit('段落', '内容')])]
        for draft, patches in cases:
            with self.subTest(draft=draft, patches=patches), self.assertRaises(ValueError):
                resolve_review_text(review(patches), draft)

    def test_noop_and_whitespace_only_changes_are_rejected(self):
        for replacement in ['原始正文', ' 原始正文 ', '原始\n正文']:
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, '实质改变'):
                resolve_review_text(review([edit('原始正文', replacement)]), '原始正文')

    def test_reviewer_cannot_introduce_hundredfold_amount_or_new_numerical_claim(self):
        cases = [('金额123.45元。', '金额12345元。'),
                 ('金额123.45元。', '金额12,345元。'),
                 ('幅度1.5%。', '幅度150%。'),
                 ('结果-10。', '结果10。'),
                 ('仅有相关性。', '造成100元损失。'),
                 ('数量为一百件。', '数量为一万件。'),
                 ('金额1.5万元。', '金额1.5元。')]
        for original, replacement in cases:
            with self.subTest(original=original, replacement=replacement), self.assertRaisesRegex(ValueError, 'repair'):
                resolve_review_text(review([edit(original, replacement, 'calculation_method')]), original)

    def test_numbers_cannot_be_borrowed_from_other_paragraphs_or_duplicated(self):
        with self.assertRaisesRegex(ValueError, '数字'):
            resolve_review_text(review([edit('没有结论。', '减少了25件。')]), '已有25件。\n\n没有结论。')
        with self.assertRaisesRegex(ValueError, '数字'):
            resolve_review_text(review([edit('金额25元。', '金额25元，损失25元。')]), '金额25元。')

    def test_decimal_point_deletion_cannot_bypass_number_guard(self):
        with self.assertRaisesRegex(ValueError, 'repair'):
            resolve_review_text(review([edit('.', '')]), '金额123.45元。')
        with self.assertRaisesRegex(ValueError, 'repair'):
            resolve_review_text(review([edit('.', ''), edit('其他12345元。', '')]),
                                '金额123.45元。\n\n其他12345元。\n\n保留结论。')

    def test_adjacent_chinese_currency_cannot_change_without_changing_digits(self):
        for original, replacement in [('金额7000欧分。', '金额7000欧元。'),
                                      ('欧分', '欧元')]:
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, '单位.*repair|repair.*单位'):
                resolve_review_text(review([edit(original, replacement)]), '金额7000欧分。')

    def test_adjacent_english_currency_and_symbol_cannot_be_rebound(self):
        for original, replacement in [('Amount EUR 70.', 'Amount USD 70.'),
                                      ('Amount 70 cents.', 'Amount 70 euro.'),
                                      ('Amount €70.', 'Amount $70.')]:
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, '单位'):
                resolve_review_text(review([edit(original, replacement)]), original)

    def test_unit_deletion_or_insertion_while_number_remains_requires_repair(self):
        for original, replacement in [('金额7000欧分。', '金额7000。'),
                                      ('金额7000。', '金额7000欧元。'),
                                      ('Amount EUR 70.', 'Amount 70.')]:
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, 'repair'):
                resolve_review_text(review([edit(original, replacement)]), original)
        self.assertEqual(resolve_review_text(review([edit('金额7000欧分。', '')]),
                                            '金额7000欧分。仍需核实原因。'), '仍需核实原因。')

    def test_ratio_and_multiplier_units_cannot_change_with_same_number(self):
        for original, replacement in [('变化5倍。', '变化5%。'),
                                      ('变化5%。', '变化5。'),
                                      ('Ratio 5 times.', 'Ratio 5 x.'),
                                      ('变化5 倍。', '变化5。')]:
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, 'repair'):
                resolve_review_text(review([edit(original, replacement)]), original)

    def test_numerical_deletion_and_preservation_are_allowed(self):
        draft = '金额123.45元，原因未知。其余内容保留。'
        self.assertEqual(resolve_review_text(review([edit('金额123.45元，原因未知。', '金额仍需核实。')]), draft),
                         '金额仍需核实。其余内容保留。')
        self.assertEqual(resolve_review_text(review([edit('原因未知。', '')]), draft), '金额123.45元，其余内容保留。')
        self.assertEqual(resolve_review_text(review([edit('金额123.45元，原因未知。', '金额123.45元，原因尚未核实。')]), draft),
                         '金额123.45元，原因尚未核实。其余内容保留。')
        with self.assertRaisesRegex(ValueError, '整个交付'):
            resolve_review_text(review([edit(draft, '')]), draft)

    def test_legacy_approval_is_compatible_but_ambiguous_protocol_is_rejected(self):
        draft = '\n原稿\n'
        self.assertEqual(resolve_review_text({'approved_text': '  旧定稿  '}, draft), '旧定稿')
        self.assertEqual(resolve_review_text({'approved_text': '', 'approve_draft_unchanged': True}, draft), draft)
        self.assertEqual(resolve_review_text(review([]), draft), '', '明确不批准原稿时，空补丁不等于批准')
        self.assertEqual(resolve_review_text({'approved_text': '', 'text_edits': []}, draft), draft)
        self.assertEqual(resolve_review_text(review([], approved_text='旧定稿'), draft), '旧定稿')
        self.assertEqual(resolve_review_text(review([], approve_draft_unchanged=True), draft), draft)
        for changes in [{'approve_draft_unchanged': True}, {'approved_text': '另写一份'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve_review_text(review([edit('原稿', '修正文稿')], **changes), draft)

    def test_malformed_edit_protocol_is_rejected_locally(self):
        for patches in [None, '替换正文', [{'original': '原文', 'replacement': '修正'}],
                        [edit('原文', '修正', 'invented')], [{**edit('原文', '修正'), 'extra': True}]]:
            with self.subTest(patches=patches), self.assertRaises(ValueError):
                resolve_review_text(review(patches), '原文')

    def test_live_reviewer_cannot_rewrite_or_patch_but_legacy_model_remains_readable(self):
        live = output_schema('delivery_review')
        self.assertEqual(live['properties']['approved_text']['enum'], [''])
        self.assertIn('text_edits', live['required'])
        self.assertNotIn('text_edits', DeliveryReview.model_json_schema()['required'])
        self.assertNotIn('enum', DeliveryReview.model_json_schema()['properties']['approved_text'])
        schema = {'$defs': live['$defs'], 'type': 'object',
                  'properties': {key: live['properties'][key] for key in ('approved_text', 'text_edits')},
                  'required': ['approved_text', 'text_edits']}
        validate = Draft202012Validator(schema)
        self.assertFalse(list(validate.iter_errors({'approved_text': '', 'text_edits': []})))
        self.assertTrue(list(validate.iter_errors({'approved_text': '自由重写', 'text_edits': []})))
        self.assertTrue(list(validate.iter_errors({'approved_text': ''})))
        self.assertTrue(list(validate.iter_errors({'approved_text': '', 'text_edits': [edit('原句', '修正句')]})))


if __name__ == '__main__':
    unittest.main()
