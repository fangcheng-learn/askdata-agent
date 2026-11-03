"""Provider response shapes for existing-evidence answers and final review."""
from collections import Counter
import re
from typing import Literal
import unicodedata
from pydantic import BaseModel, ConfigDict, Field
from ..models import VisualizationSpec


class TextEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    original: str = Field(min_length=1, description='原稿中唯一出现的完整原文片段，逐字复制，不使用省略号')
    replacement: str = Field(description='只修正措辞或删除整个数值项；数字和单位不得新增或改写，数字或单位纠错必须返回repair')
    criterion: Literal['metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage']


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion: Literal['metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage']
    verdict: Literal['supported', 'correction_required', 'missing_evidence']
    evidence_ids: list[str]
    context_refs: list[str] = Field(default_factory=list, description='References to supplied review context, not numerical query evidence')
    finding: str = Field(description='简短可核查事实：实际SQL/数据/公开目录支持什么、缺什么，不复述主Agent自评')
    resolved_in_final: bool = Field(description='仅有文案问题且本次已用text_edits修好才可为true；缺数据或数字错误须repair')


class AnalysisReviewFinding(ReviewFinding):
    finding_id: str = Field(min_length=1, description='本轮唯一问题编号；不同具体问题分别填写，即使criterion相同')
    text_edit_indices: list[int] = Field(default_factory=list,
        description='本问题对应text_edits的零基索引；不同问题不能共用补丁。supported或missing_evidence用空数组')


QA_REVIEW_CRITERIA = ('metric_scope', 'calculation_method', 'claim_strength', 'capability', 'answer_coverage')
QA_REVIEW_CONTEXT_REFS = ('current_question', 'evidence_metadata', 'evidence_gaps', 'draft', 'trust_boundary')


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['answer', 'report']
    answer: str = ''
    title: str = ''
    markdown: str = ''
    visualizations: list[VisualizationSpec] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)

class AnalysisReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    valid: bool
    approve_draft_unchanged: bool = False
    approved_text: str
    reason: str
    audit_findings: list[AnalysisReviewFinding] = Field(default_factory=list,
        description='逐项核对五类依据；同类多个具体错误分别给唯一finding_id，不把历史审核当本轮证据')
    text_edits: list[TextEdit] = Field(default_factory=list)


def analysis_schema(review=False, *, evidence_ids: list[str] | None = None):
    schema = (AnalysisReview if review else AnalysisOutput).model_json_schema()
    for definition in schema.get('$defs', {}).values():
        if definition.get('type') == 'object' and definition.get('properties'):
            definition['additionalProperties'] = False
    if review:
        # Close only the live protocol. Old stored reviews remain readable.
        schema['properties']['approved_text']['enum'] = ['']
        schema.setdefault('required', []).extend(['audit_findings', 'text_edits'])
        finding = schema['$defs']['AnalysisReviewFinding']
        finding.setdefault('required', []).append('text_edit_indices')
        finding['properties']['context_refs']['items']['enum'] = list(QA_REVIEW_CONTEXT_REFS)
        if evidence_ids is not None:
            if evidence_ids:
                finding['properties']['evidence_ids']['items']['enum'] = sorted(set(evidence_ids))
            else:
                finding['properties']['evidence_ids']['maxItems'] = 0
    return schema


def resolve_analysis_review(review: dict, draft: str, evidence_ids: set[str]) -> str:
    """Resolve this QA review, never inherit a parent report's approval.

    A correction belongs to one declared finding, not every issue sharing its
    criterion. These checks bind the model's judgments; they do not prove that
    it discovered every unsupported claim in the document.
    """
    parsed = AnalysisReview.model_validate(review)
    if type(review.get('valid')) is not bool or type(review.get('approve_draft_unchanged', False)) is not bool:
        raise ValueError('审核批准状态必须为布尔值')
    if parsed.approved_text:
        raise ValueError('本轮审核不能自由重写正文，只能批准原稿或提交精确text_edits')
    findings = parsed.audit_findings
    if {item.criterion for item in findings} != set(QA_REVIEW_CRITERIA):
        raise ValueError('本轮审核缺少五类可核查依据，历史审核状态不能替代')
    ids = [item.finding_id.strip() for item in findings]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError('不同具体审核问题必须使用非空唯一finding_id')
    used_edits: set[int] = set()
    unresolved = []
    for item, raw in zip(findings, review['audit_findings']):
        if (not item.finding.strip() or not (item.evidence_ids or item.context_refs)
                or not set(item.evidence_ids) <= evidence_ids
                or not set(item.context_refs) <= set(QA_REVIEW_CONTEXT_REFS)
                or type(raw.get('resolved_in_final')) is not bool):
            raise ValueError('审核依据、证据或上下文引用无效')
        indices = item.text_edit_indices
        if (not isinstance(raw.get('text_edit_indices'), list)
                or any(type(index) is not int for index in raw['text_edit_indices'])
                or len(set(indices)) != len(indices)):
            raise ValueError('每个问题必须给出不重复的整数补丁索引')
        for index in indices:
            if index < 0 or index >= len(parsed.text_edits) or index in used_edits:
                raise ValueError('补丁索引无效或被多个问题共用，不能批量宣告同类问题已解决')
            if parsed.text_edits[index].criterion != item.criterion:
                raise ValueError('补丁与具体审核问题的criterion不一致')
            used_edits.add(index)
        if item.verdict != 'correction_required' and (indices or item.resolved_in_final):
            raise ValueError('supported或missing_evidence不能绑定文字修订或声称补丁补齐证据')
        if item.verdict == 'missing_evidence' or (
            item.verdict == 'correction_required' and (item.criterion == 'calculation_method'
                or not item.resolved_in_final or not indices or parsed.approve_draft_unchanged)
        ):
            unresolved.append(f'{item.finding_id}/{item.criterion}：{item.finding}')
    if used_edits != set(range(len(parsed.text_edits))):
        raise ValueError('每个文字补丁必须绑定一个具体审核问题')
    if unresolved:
        raise ValueError('本轮仍有未解决的事实、方法或证据问题：' + '；'.join(unresolved))
    if not parsed.valid:
        raise ValueError(parsed.reason or '本轮审核未通过')
    return resolve_review_text(parsed.model_dump(), draft)


def resolve_review_text(review: dict, draft: str) -> str:
    """Resolve legacy approval or apply simultaneous exact edits to the draft.

    Every new edit is anchored in the original draft, never a previous edit's
    replacement. This keeps order immaterial and prevents cascading rewrites.
    Numeric corrections require new evidence/drafting, not this text channel.
    """
    unchanged = review.get('approve_draft_unchanged', False)
    if type(unchanged) is not bool:
        raise ValueError('approve_draft_unchanged必须为布尔值')
    text = review.get('approved_text') or ''
    if not isinstance(text, str):
        raise ValueError('approved_text必须为文字')
    edits = review.get('text_edits', [])
    if not isinstance(edits, list):
        raise ValueError('text_edits必须为数组，无修改时使用空数组')
    if edits and unchanged:
        raise ValueError('批准原稿不变时不能同时提交text_edits')
    if unchanged:
        if not draft.strip() or (text and text != draft):
            raise ValueError('原稿批准标志与提交的正文不一致')
        return draft
    if edits:
        if text:
            raise ValueError('text_edits不能与自由重写的approved_text同时提交')
        if not isinstance(draft, str) or not draft.strip():
            raise ValueError('精确文字修正需要非空原稿')
        spans = []
        for index, raw in enumerate(edits):
            try:
                edit = TextEdit.model_validate(raw)
            except ValueError as exc:
                raise ValueError(f'text_edits[{index}]需完整original/replacement/criterion') from exc
            original, replacement = edit.original, edit.replacement
            start = draft.find(original)
            if not original.strip() or start < 0 or draft.find(original, start + 1) >= 0:
                raise ValueError(f'text_edits[{index}].original必须在原稿中非空且唯一匹配')
            if ''.join(original.split()) == ''.join(replacement.split()):
                raise ValueError(f'text_edits[{index}]必须实质改变措辞，不能仅改变空白')
            if _numbers(replacement) - _numbers(original):
                raise ValueError(f'text_edits[{index}]不得新增或更改数字及紧邻单位；数字或单位纠错必须返回repair重新核对证据')
            # Removing a decimal point or a separator can create a new number
            # even though replacement itself contains no digit. Check the
            # touched original paragraph(s), including unchanged boundaries.
            end = start + len(original)
            left, right = 0, len(draft)
            for boundary in re.finditer(r'\r?\n[ \t]*\r?\n', draft):
                if boundary.end() <= start:
                    left = boundary.end()
                elif boundary.start() >= end:
                    right = boundary.start()
                    break
            region = draft[left:right]
            changed = draft[left:start] + replacement + draft[end:right]
            if _numbers(changed) - _numbers(region):
                raise ValueError(f'text_edits[{index}]会改变所在原段落的数字或紧邻单位；请返回repair核对数值和单位')
            spans.append((start, start + len(original), replacement))
        spans.sort(key=lambda span: span[0])
        if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
            raise ValueError('text_edits原文片段不得重叠；所有修正按同一份原稿定位')
        pieces, cursor = [], 0
        for start, end, replacement in spans:
            pieces.extend((draft[cursor:start], replacement))
            cursor = end
        pieces.append(draft[cursor:])
        result = ''.join(pieces)
        if not result.strip():
            raise ValueError('文字修正不能删除整个交付正文；需返回repair或明确未完成')
        if _numbers(result) - _numbers(draft):
            raise ValueError('文字修正组合后产生新数字或紧邻单位；请返回repair重新核对证据')
        return result
    # Old records may carry an empty default edits list together with their
    # approved_text. Preserve that reader; only the live delivery schema closes
    # the free-rewrite channel. New no-edit output keeps the submitted draft.
    if 'text_edits' in review and not text:
        if review.get('approve_draft_unchanged') is False:
            return ''  # An explicit rejection is never implicit no-edit approval.
        if not isinstance(draft, str) or not draft.strip():
            raise ValueError('无修改批准需要非空原稿')
        return draft
    return text.strip()


_NUMERIC_LITERAL = re.compile(
    r'[+\-−]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:[eE][+\-]?\d+)?(?:[%‰]|万|亿)?'
    r'|[负零〇一二两三四五六七八九十百千万亿点]{2,}'
    r'|[零〇一二两三四五六七八九十](?=倍|欧元|欧分|元|件|单|次|小时|天|月|年)')

# A closed surface safeguard, not a unit inference/conversion system. Shared
# table headers, footnotes and units in another sentence still need review.
_ADJACENT_UNITS = (
    'EUR cents', 'USD cents', '欧分', '欧元', '美分', '美元', '人民币', '港元', '港币',
    '日元', '英镑', '元', '分', 'EUR', 'USD', 'CNY', 'RMB', 'GBP', 'JPY', 'HKD',
    'cents', 'cent', 'euros', 'euro', 'dollars', 'dollar', '€', '$', '¥', '£',
    '%', '‰', '倍', 'times', 'x', '×')
_UNIT_ALTERNATIVES = '|'.join(re.escape(unit) for unit in sorted(_ADJACENT_UNITS, key=len, reverse=True))
_UNIT_BEFORE = re.compile(r'(?<![A-Za-z])(' + _UNIT_ALTERNATIVES + r')\s*$')
_UNIT_AFTER = re.compile(r'^\s*(' + _UNIT_ALTERNATIVES + r')(?![A-Za-z])')


def _numbers(text: str) -> Counter:
    """Literal numbers with their directly adjacent known unit tokens.

    No equivalent-unit conversion or semantic inference. Other units and claim
    meaning still require review; unchanged numerals do not prove a claim.
    """
    text = unicodedata.normalize('NFKC', text)
    mentions = []
    for number in _NUMERIC_LITERAL.finditer(text):
        before = _UNIT_BEFORE.search(text[:number.start()])
        after = _UNIT_AFTER.match(text[number.end():])
        mentions.append((number.group(), before.group(1) if before else '', after.group(1) if after else ''))
    return Counter(mentions)
