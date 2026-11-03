"""Typed progress patches and evidence-reference adaptation, without model calls.

Progress is a patch, not an instruction to replace the investigation.  Rejected
notes are reported individually; an action must independently validate its own
references with ``resolve_references`` before execution.  This module never
authenticates a result by loading it: its authority is the supplied evidence.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.workflows.investigation_progress import merge_progress_records


class _PatchRecord(BaseModel):
    # Preserve scope, goal, lineage and other notes already used by the caller.
    # Known fields remain typed; extra metadata is never interpreted as proof.
    model_config = ConfigDict(extra='allow', strict=True)


class FactUpdate(_PatchRecord):
    id: str | None = None
    text: str | None = None
    evidence_ids: list[str] | None = None
    fact_ids: list[str] | None = None
    status: str | None = None
    scope: str | dict[str, Any] | None = None


class HypothesisUpdate(FactUpdate):
    pass


class CoreQuestionUpdate(_PatchRecord):
    id: str = Field(min_length=1)
    question: str | None = None
    importance: Literal['core', 'optional'] | None = None
    status: Literal['open', 'answered', 'blocked', 'not_applicable'] | None = None
    evidence_ids: list[str] | None = None
    fact_ids: list[str] | None = None
    reason: str | None = None
    original: bool | None = None


class ExplanationUpdate(_PatchRecord):
    id: str = Field(min_length=1)
    text: str | None = None
    status: Literal['open', 'supported', 'refuted', 'low_explanatory_power',
                    'blocked', 'weak', 'unresolved', 'not_applicable', 'unsupported', 'retracted', 'superseded'] | None = None
    supporting_evidence_ids: list[str] | None = None
    refuting_evidence_ids: list[str] | None = None
    supporting_fact_ids: list[str] | None = None
    refuting_fact_ids: list[str] | None = None
    fact_ids: list[str] | None = None
    needed_evidence: list[str] | None = None
    boundary: str | None = None


class ProgressUpdate(_PatchRecord):
    """All arrays are patches; omission/null means no change, [] adds nothing.

    Reference fields should contain result IDs from the current evidence
    catalog. Known fact IDs are accepted and expanded by the deterministic
    adapter, with their fact lineage preserved separately.
    """
    facts: list[FactUpdate] | None = None
    hypotheses: list[HypothesisUpdate] | None = None
    core_questions: list[CoreQuestionUpdate] | None = None
    explanations: list[ExplanationUpdate] | None = None
    unresolved_questions: list[str] | None = None


def output_schema() -> dict[str, Any]:
    """The prompt schema comes from the same models used by the adapter."""
    return ProgressUpdate.model_json_schema()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _merge_patch(previous: dict, patch: dict) -> dict:
    merged = {**previous, **{k: deepcopy(v) for k, v in patch.items() if v is not None}}
    for field in ('evidence_ids', 'supporting_evidence_ids', 'refuting_evidence_ids'):
        lineage = field.replace('evidence_ids', 'fact_ids')
        if patch.get(field) is not None and patch.get(lineage) is None:
            merged.pop(lineage, None)
    return merged


def _catalog(evidence: Any) -> tuple[set[str], set[str]]:
    # A set is an explicit caller-authenticated allowlist, useful to pure
    # validation callers. Lists must carry actual result validation metadata.
    if isinstance(evidence, (set, frozenset)):
        ids = {value for value in evidence if isinstance(value, str)}
        return ids, ids
    if isinstance(evidence, Mapping):
        records = evidence.values()
    else:
        records = evidence if isinstance(evidence, (list, tuple)) else []
    all_ids, active, duplicates = set(), set(), set()
    for entry in records:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
            continue
        rid = entry['id']
        if rid in all_ids:
            duplicates.add(rid)
        all_ids.add(rid)
        if entry.get('check_status') in {'basic_valid', 'reviewed'}:
            active.add(rid)
    # A duplicate result ID cannot identify a unique current version, even
    # when both entries happen to claim the same validation status.
    return active - duplicates, all_ids


def resolve_references(refs: Any, fact_lookup: Mapping[str, dict], evidence: Any,
                       *, path: str = '') -> dict[str, Any]:
    """Resolve only explicit fact lineage to current authenticated results.

    ``fact_lookup`` contains facts only, never hypotheses. A rejected current
    update must shadow its old fact with ``_rejected=True`` so callers cannot
    silently use the prior version. Returned ``safe`` is false on any error;
    consumers must not use a partially resolved dependency list in that case.
    """
    active, known = _catalog(evidence)
    result: dict[str, Any] = {'evidence_ids': [], 'fact_ids': [],
                              'normalizations': [], 'errors': [], 'safe': True}

    def error(code: str, reference: Any, message: str) -> None:
        result['errors'].append({'path': path, 'code': code, 'reference': reference,
                                 'message': message})

    def as_list(value: Any, field_path: str) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            result['normalizations'].append({'path': field_path, 'kind': 'reference_array',
                                               'from': value, 'to': [value]})
            return [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            error('invalid_reference_shape', value, '引用必须是非空字符串组成的数组')
            return []
        return value

    def visit(ref: str, chain: tuple[str, ...]) -> None:
        if ref in active:
            if ref in fact_lookup:
                error('ambiguous_reference', ref, '同一编号同时被用作事实和结果，不能确定引用对象')
                return
            result['evidence_ids'].append(ref)
            return
        if ref in known:
            error('inactive_result', ref, '结果未通过基础校验或已失效，不能作为当前证据')
            return
        # Short result IDs may be adapted only when the current catalog has a
        # single match. Never map F1 to R1 by its numeric suffix.
        if re.fullmatch(r'R\d+', ref):
            if ref in fact_lookup:
                error('ambiguous_reference', ref, '事实编号与结果简写冲突，不能确定引用对象')
                return
            matches = [rid for rid in active if rid.rsplit(':', 1)[-1] == ref]
            if len(matches) == 1:
                result['evidence_ids'].append(matches[0])
                result['normalizations'].append({'path': path, 'kind': 'scoped_result_id',
                                                   'from': ref, 'to': matches[0]})
            else:
                error('ambiguous_result' if matches else 'unknown_result', ref,
                      '简写结果编号无法唯一对应当前有效证据')
            return
        if re.fullmatch(r'H\d+', ref, re.IGNORECASE):
            error('hypothesis_is_not_evidence', ref, '假设编号不能当作证据或已确认事实')
            return
        fact = fact_lookup.get(ref)
        if not isinstance(fact, dict):
            error('unknown_reference', ref, '引用不在当前有效结果或已登记事实中')
            return
        if fact.get('_rejected') or fact.get('status') in {
                'invalidated', 'superseded', 'rejected', 'refuted', 'failed', 'blocked', 'retracted', 'removed'}:
            error('inactive_fact', ref, '引用的事实已失效或本轮更新未通过校验')
            return
        if ref in chain:
            error('fact_reference_cycle', ref, '事实引用形成循环：' + ' -> '.join((*chain, ref)))
            return
        result['fact_ids'].append(ref)
        children = [*as_list(fact.get('evidence_ids'), f'facts.{ref}.evidence_ids'),
                    *as_list(fact.get('fact_ids'), f'facts.{ref}.fact_ids')]
        if not children:
            error('ungrounded_fact', ref, '事实没有通向真实结果的证据引用')
            return
        for child in _unique(children):
            visit(child, (*chain, ref))

    for ref in as_list(refs, path):
        visit(ref, ())
    result['evidence_ids'] = _unique(result['evidence_ids'])
    result['fact_ids'] = _unique(result['fact_ids'])
    result['safe'] = not result['errors']
    return result


def normalize_progress(progress: Any, *, existing_progress: dict | None,
                       evidence: Any) -> dict[str, Any]:
    """Validate independent patch records and adapt their evidence references.

    Rejection does not drop any previously committed progress or unrelated
    valid update. ``unsafe_fact_ids`` and tombstones in ``fact_lookup`` ensure
    a dependent action cannot fall back to a rejected fact's old version.
    Core question and causal support rules are checked through the existing
    merge contract, one record at a time, so they are never weakened here.
    """
    existing = deepcopy(existing_progress or {})
    result: dict[str, Any] = {'progress': {}, 'normalizations': [], 'warnings': [],
                             'rejected_fields': [], 'action_references_safe': True,
                             'fact_lookup': {}, 'unsafe_fact_ids': []}

    def reject(path: str, code: str, message: str, **details: Any) -> None:
        issue = {'path': path, 'code': code, 'message': message, **details}
        result['rejected_fields'].append(issue)
        result['warnings'].append(issue.copy())

    if progress is None:
        progress = {}
    if not isinstance(progress, dict):
        reject('progress', 'invalid_progress_shape', 'progress 必须为对象；本轮忽略记录更新')
        progress = {}
    # Unknown notes are preserved; they do not become control fields or proof.
    result['progress'] = {key: deepcopy(value) for key, value in progress.items()
                          if key not in ProgressUpdate.model_fields and value is not None}
    previous_maps = {category: {entry['id']: entry for entry in existing.get(category) or []
                               if isinstance(entry, dict) and isinstance(entry.get('id'), str)}
                     for category in ('facts', 'hypotheses', 'core_questions', 'explanations')}
    lookup = deepcopy(previous_maps['facts'])
    candidates: list[tuple[int, dict]] = []
    seen: dict[str, dict] = {}
    blocked = set()
    values = progress.get('facts')
    if values is not None and not isinstance(values, list):
        reject('facts', 'invalid_collection_shape', 'facts 必须为数组；保留已有事实')
    elif isinstance(values, list):
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                reject(f'facts[{index}]', 'invalid_record_shape', '事实条目必须为对象')
                continue
            fid = item.get('id')
            if isinstance(fid, str) and fid:
                if fid in seen and seen[fid] != item:
                    blocked.add(fid)
                    reject(f'facts[{index}]', 'duplicate_fact_id', '同一批次的同名事实更新互相冲突', fact_id=fid)
                seen[fid] = item
                merged = _merge_patch(lookup.get(fid, {}), item)
                lookup[fid] = merged
            else:
                merged = deepcopy(item)
            candidates.append((index, merged))

    def tombstone(fid: Any) -> None:
        if isinstance(fid, str) and fid:
            blocked.add(fid)
            lookup[fid] = {'id': fid, '_rejected': True}

    for fid in blocked:
        lookup[fid] = {'id': fid, '_rejected': True}

    def normalize_record(item: dict, model: type[BaseModel], category: str, index: int) -> dict | None:
        path = f'{category}[{index}]'
        normalized = {k: deepcopy(v) for k, v in item.items() if v is not None}
        fields = ('supporting_evidence_ids', 'refuting_evidence_ids') if category == 'explanations' else ('evidence_ids',)
        combined_fact_ids: list[str] = []
        for field in fields:
            corresponding = field.replace('evidence_ids', 'fact_ids')
            # Plain fact_ids belongs to evidence_ids; explanations preserve
            # supporting/refuting provenance in their respective fields.
            raw_refs = normalized.get(field)
            lineage = normalized.get(corresponding)
            parts = []
            for raw in (raw_refs, lineage):
                if raw is None:
                    continue
                if isinstance(raw, str):
                    parts.append(raw)
                    result['normalizations'].append({'path': path + '.' + field,
                        'kind': 'reference_array', 'from': raw, 'to': [raw]})
                elif isinstance(raw, list):
                    parts.extend(raw)
                else:
                    reject(path + '.' + field, 'invalid_reference_shape', '证据引用必须为字符串数组')
                    return None
            resolved = resolve_references(parts, lookup, evidence, path=path + '.' + field)
            result['normalizations'].extend(resolved['normalizations'])
            if not resolved['safe']:
                for issue in resolved['errors']:
                    reject(issue['path'], issue['code'], issue['message'], reference=issue.get('reference'))
                return None
            if raw_refs is not None or lineage is not None:
                normalized[field] = resolved['evidence_ids']
            if resolved['fact_ids']:
                normalized[corresponding] = resolved['fact_ids']
                combined_fact_ids.extend(resolved['fact_ids'])
                result['normalizations'].append({'path': path + '.' + field, 'kind': 'fact_evidence_expansion',
                    'fact_ids': resolved['fact_ids'], 'evidence_ids': resolved['evidence_ids']})
        if category == 'explanations':
            # General fact lineage is contextual metadata, not a substitute
            # for explicitly supporting or refuting evidence.
            contextual = resolve_references(normalized.get('fact_ids'), lookup, evidence,
                                            path=path + '.fact_ids')
            if not contextual['safe']:
                for issue in contextual['errors']:
                    reject(issue['path'], issue['code'], issue['message'], reference=issue.get('reference'))
                return None
            result['normalizations'].extend(contextual['normalizations'])
            if combined_fact_ids or normalized.get('fact_ids') is not None:
                normalized['fact_ids'] = _unique([*contextual['fact_ids'], *combined_fact_ids])
        try:
            parsed = model.model_validate(normalized).model_dump(exclude_none=True)
        except ValidationError as exc:
            reject(path, 'invalid_record_fields', '记录字段格式无效', errors=exc.errors(include_url=False))
            return None
        if category in {'facts', 'hypotheses'}:
            if not isinstance(parsed.get('text'), str) or not parsed['text'].strip():
                reject(path, 'missing_text', '调查记录缺少 text')
                return None
            if category == 'facts' and not parsed.get('evidence_ids'):
                reject(path, 'ungrounded_fact', '事实必须引用当前真实证据，用户描述应保留为线索')
                return None
            if category == 'facts' and re.fullmatch(r'H\d+', str(parsed.get('id') or ''), re.IGNORECASE):
                reject(path, 'hypothesis_is_not_fact', '不能把假设编号登记成已确认事实')
                return None
        return parsed

    # Structural failures are known before any dependent record is evaluated.
    for index, item in candidates:
        fid = item.get('id')
        if isinstance(fid, str) and fid in blocked:
            continue
        try:
            FactUpdate.model_validate({k: v for k, v in item.items() if v is not None and k not in {'evidence_ids', 'fact_ids'}})
            if not isinstance(item.get('text'), str) or not item['text'].strip():
                raise ValueError('调查记录缺少 text')
            if isinstance(fid, str) and (fid in _catalog(evidence)[1] or re.fullmatch(r'R\d+', fid)):
                raise ValueError('事实编号不能占用结果编号或其简写')
        except (ValidationError, ValueError) as exc:
            reject(f'facts[{index}]', 'invalid_fact_fields', str(exc))
            tombstone(fid)
    normalized_facts = []
    for index, item in candidates:
        if isinstance(item.get('id'), str) and item['id'] in blocked:
            continue
        normalized = normalize_record(item, FactUpdate, 'facts', index)
        if normalized is None:
            tombstone(item.get('id'))
        else:
            normalized_facts.append(normalized)
    # An earlier fact may depend on a later rejected fact. Revalidate until
    # rejection reaches every affected dependency, independent of model order.
    while True:
        survivors = []
        changed = False
        for item in normalized_facts:
            check = resolve_references([*(item.get('evidence_ids') or []), *(item.get('fact_ids') or [])], lookup, evidence)
            if check['safe']:
                survivors.append(item)
            else:
                reject('facts.' + str(item.get('id') or item.get('text')), 'invalid_fact_dependency',
                       '事实依赖本轮被拒绝或已失效的记录', errors=check['errors'])
                tombstone(item.get('id'))
                changed = True
        normalized_facts = survivors
        if not changed:
            break
    for item in normalized_facts:
        if item.get('id'):
            lookup[item['id']] = item
    if isinstance(values, list):
        result['progress']['facts'] = normalized_facts

    current = deepcopy(existing)
    active_ids, _ = _catalog(evidence)
    models = {'hypotheses': HypothesisUpdate, 'core_questions': CoreQuestionUpdate,
              'explanations': ExplanationUpdate}
    for category, model in models.items():
        entries = progress.get(category)
        if entries is None:
            continue
        if not isinstance(entries, list):
            reject(category, 'invalid_collection_shape', category + ' 必须为数组；保留已有记录')
            continue
        accepted = []

        def keep_new_core_question_open(item: dict, previous: dict) -> None:
            if (category != 'core_questions' or previous or item.get('importance') != 'core'
                    or not isinstance(item.get('id'), str) or not item['id'].strip()
                    or not isinstance(item.get('question'), str) or not item['question'].strip()):
                return
            safe = {'id': item['id'], 'question': item['question'], 'importance': 'core',
                    'status': 'open', 'evidence_ids': [],
                    'validation_issue': '本轮关闭或引用更新无效，保留核心问题等待取证'}
            accepted.append(safe)
            previous_maps[category][safe['id']] = safe
            current.setdefault(category, []).append(deepcopy(safe))
            result['warnings'].append({'path': category + '.' + safe['id'],
                'code': 'core_question_kept_open', 'message': safe['validation_issue']})

        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                reject(f'{category}[{index}]', 'invalid_record_shape', '记录条目必须为对象')
                continue
            previous = previous_maps[category].get(item.get('id'), {}) if isinstance(item.get('id'), str) else {}
            merged = _merge_patch(previous, item)
            normalized = normalize_record(merged, model, category, index)
            if normalized is None:
                keep_new_core_question_open(item, previous)
                continue
            if category in {'core_questions', 'explanations'}:
                try:
                    current = merge_progress_records(current, {category: [normalized]}, active_ids)
                    normalized = next(entry for entry in current[category] if entry['id'] == normalized['id'])
                except ValueError as exc:
                    reject(f'{category}[{index}]', 'progress_rule_violation', str(exc))
                    keep_new_core_question_open(item, previous)
                    continue
            accepted.append(normalized)
            if normalized.get('id'):
                previous_maps[category][normalized['id']] = normalized
        result['progress'][category] = accepted
    questions = progress.get('unresolved_questions')
    if questions is not None:
        if isinstance(questions, list):
            valid = [value for value in questions if isinstance(value, str) and value.strip()]
            for index, value in enumerate(questions):
                if not isinstance(value, str) or not value.strip():
                    reject(f'unresolved_questions[{index}]', 'invalid_question', '未解决问题必须为非空字符串')
            result['progress']['unresolved_questions'] = valid
        else:
            reject('unresolved_questions', 'invalid_collection_shape', '未解决问题必须为字符串数组')
    result['fact_lookup'] = lookup
    result['unsafe_fact_ids'] = sorted(blocked)
    return result
