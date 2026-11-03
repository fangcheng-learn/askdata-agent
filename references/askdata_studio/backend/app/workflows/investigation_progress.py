"""Evidence-linked questions and review reconciliation; no model or SQL calls."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

EXPLANATION_STATUSES = frozenset({'open', 'supported', 'refuted', 'low_explanatory_power', 'blocked',
                                  'weak', 'unresolved', 'not_applicable', 'unsupported', 'retracted', 'superseded'})


def merge_progress_records(progress: dict, changes: dict, valid_ids: set[str]) -> dict:
    result = deepcopy(progress)
    for category in ('core_questions', 'explanations'):
        if category not in changes:
            continue
        if not isinstance(changes[category], list):
            raise ValueError(category + '必须是数组')
        records = list(result.get(category) or [])
        by_id = {item['id']: i for i, item in enumerate(records)}
        for update in changes[category]:
            if not isinstance(update, dict) or not isinstance(update.get('id'), str) or not update['id'].strip():
                raise ValueError(category + '条目必须有稳定id')
            previous = records[by_id[update['id']]] if update['id'] in by_id else {}
            entry = {**previous, **update}
            if category == 'core_questions':
                if not isinstance(entry.get('question'), str) or not entry['question'].strip():
                    raise ValueError('调查问题缺少question')
                if previous.get('original') and entry['question'] != previous['question']:
                    raise ValueError('原始问题不能被改写或缩小')
                if previous.get('original'):
                    entry.update(original=True, importance='core')
                if previous.get('importance') == 'core' and entry.get('importance') != 'core':
                    raise ValueError('已有核心问题不能降为可选项；须有证据回答或明确不适用')
                if entry.get('importance') not in {'core', 'optional'} or entry.get('status') not in {
                    'open', 'answered', 'blocked', 'not_applicable'}:
                    raise ValueError('调查问题importance/status无效')
                refs = entry.get('evidence_ids') or []
                if entry['status'] in {'answered', 'not_applicable'} and (not refs or not entry.get('reason')):
                    raise ValueError('关闭调查问题需要真实证据和理由')
            else:
                if not isinstance(entry.get('text'), str) or not entry['text'].strip():
                    raise ValueError('候选解释缺少text')
                if entry.get('status') not in EXPLANATION_STATUSES:
                    raise ValueError('候选解释status无效')
                for key in ('supporting_evidence_ids', 'refuting_evidence_ids'):
                    if key in entry and (not isinstance(entry[key], list) or
                                         not all(isinstance(v, str) for v in entry[key])):
                        raise ValueError('解释证据引用必须为字符串数组')
                refs = [*(entry.get('supporting_evidence_ids') or []), *(entry.get('refuting_evidence_ids') or [])]
                if entry['status'] == 'supported' and not entry.get('supporting_evidence_ids'):
                    raise ValueError('支持解释需要真实支持证据')
                if entry['status'] == 'refuted' and not entry.get('refuting_evidence_ids'):
                    raise ValueError('反驳解释需要真实反证')
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs) or not set(refs) <= valid_ids:
                raise ValueError('问题或解释引用不存在／失效的证据')
            if entry['id'] in by_id:
                records[by_id[entry['id']]] = entry
            else:
                by_id[entry['id']] = len(records)
                records.append(entry)
        result[category] = records
    return result


def review_questions(progress: dict, draft_gaps: list[str]) -> list[dict]:
    questions = [deepcopy(q) for q in progress.get('core_questions') or [] if q.get('importance') == 'core']
    for index, gap in enumerate(draft_gaps, 1):
        if isinstance(gap, str) and gap.strip():
            questions.append({'id': f'DRAFT_GAP_{index}', 'question': gap, 'importance': 'core',
                              'status': 'open', 'source': 'draft'})
    return questions


def reconcile_review(review: dict, questions: list[dict], explanations: list[dict], valid_ids: set[str]) -> dict:
    """Check explicit IDs and contradictions, not arbitrary natural-language causality."""
    assessments = review.get('question_assessments')
    errors, gaps = [], []
    if not isinstance(assessments, list):
        return {'errors': ['最终审核缺少question_assessments逐项回答'], 'gaps': []}
    expected = {q['id']: q for q in questions}
    seen = set()
    for entry in assessments:
        if not isinstance(entry, dict) or entry.get('question_id') not in expected or entry.get('question_id') in seen:
            errors.append('最终审核问题编号未知或重复')
            continue
        qid = entry['question_id']
        seen.add(qid)
        status = entry.get('status')
        if status not in {'answered', 'not_applicable', 'unresolved'}:
            errors.append(qid + '的回答状态无效')
        refs = entry.get('evidence_ids') or []
        if not isinstance(refs, list) or not all(isinstance(v, str) for v in refs) or not set(refs) <= valid_ids:
            errors.append(qid + '引用失效证据')
        if entry.get('answer_level') not in {'fact', 'decomposition', 'contribution', 'association', 'mechanism'}:
            errors.append(qid + '缺少有效解释层级')
        if not isinstance(entry.get('reason'), str) or not entry['reason'].strip():
            errors.append(qid + '缺少处理依据')
        if status in {'answered', 'not_applicable'} and not refs:
            errors.append(qid + '没有关闭问题的证据')
        if status == 'unresolved':
            gaps.append(expected[qid]['question'])
    errors.extend('最终审核遗漏核心问题：' + qid for qid in expected.keys() - seen)
    if explanations:
        coverage = review.get('explanation_coverage_ack')
        if type(coverage) is not bool:
            errors.append('最终审核缺少布尔值explanation_coverage_ack')
        elif not coverage:
            gaps.append('主要解释的支持与反证尚未核对充分')
    return {'errors': errors, 'gaps': gaps}
