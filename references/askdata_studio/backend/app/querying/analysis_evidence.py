"""Materialize already selected analysis results without SQL or model calls.

The server supplies the same-session candidates; this adapter rechecks current
owner, source, snapshot and table permissions before reading every result using
its original task binding. Storage references never become model-facing IDs.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from ..database import SCHEMA, physical_table_name
from ..security import AccessScope


def _objects(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []
    items = raw if isinstance(raw, list) else [raw]
    return [item for item in items if isinstance(item, dict)]


def _sql_tables(sql: Any) -> set[str]:
    if not sql:
        return set()
    try:
        statements = [item for item in sqlglot.parse(str(sql), read='duckdb') if item]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError('历史结果SQL来源无法解析')
        return {source.name.casefold() for query_scope in traverse_scope(statements[0])
                for source in query_scope.sources.values() if isinstance(source, exp.Table)}
    except (sqlglot.errors.ParseError, ValueError) as exc:
        raise PermissionError('历史结果无法复核当前表权限') from exc


def _report_references(parent: dict) -> list[dict]:
    """Use the existing report citation grammar, never discover other tasks."""
    report = parent.get('report') if isinstance(parent.get('report'), dict) else {}
    markdown = str(report.get('markdown') or parent.get('analysis') or '')
    versions = {str(item.get('result_id')): str(item.get('version') or '')
                for item in report.get('data_sources') or [] if isinstance(item, dict)}
    references = [(rid, version or versions.get(rid, '')) for rid, version in
                  re.findall(r'\[([A-Za-z0-9_.:-]*R\d+)(?:@([^\]\s]+))?\]', markdown)]
    references.extend((str(chart['source_result_id']),
                       str(chart.get('source_version') or versions.get(str(chart['source_result_id']), '')))
                      for chart in report.get('visualizations') or []
                      if isinstance(chart, dict) and chart.get('source_result_id'))
    return [{'result_id': rid, 'version': version} for rid, version in dict.fromkeys(references)]


def load_analysis_evidence(state: dict[str, Any], engine: Any, *,
                           max_rows: int = 1000, max_total_rows: int = 3000) -> dict:
    """Read bounded real rows, retaining exact result identities and versions.

    This does not discover other tasks. Report work also materializes citations
    from its already selected parent reports; fact follow-ups keep the exact
    selected result set. Reusable evidence only fills metadata for those same
    source tasks. Failed/invalid report references remain explicit gaps.
    """
    if max_rows < 1 or max_total_rows < 1:
        raise ValueError('分析证据行数上限必须为正数')
    current = AccessScope.from_dict(state.get('access_scope'))
    source = str(state.get('datasource_id') or '')
    snapshot = state.get('scenario') or {}
    report_mode = state.get('analysis_mode') == 'report'
    selected = _objects(state.get('analysis_context'))
    parents = selected or _objects(state.get('recent_result_context'))
    task_ids = {str(item.get('task_id')) for item in parents if item.get('task_id')}
    entries, gaps, origins, prior_reports = [], [], [], []
    managed = False
    for parent in parents:
        task_id = str(parent.get('task_id') or '')
        if not task_id:
            continue
        if report_mode:
            if parent.get('user_id') and parent['user_id'] != current.user_id:
                raise PermissionError('所选父报告与当前用户不匹配')
            if parent.get('datasource_id') and source and parent['datasource_id'] != source:
                raise PermissionError('所选父报告与当前数据源不匹配')
            if (parent.get('snapshot') or parent.get('scenario')) and (
                parent.get('snapshot') or parent.get('scenario')) != snapshot:
                raise PermissionError('所选父报告与当前快照不匹配')
        if parent.get('evidence') or parent.get('result_ref') or parent.get('workflow_mode') == 'investigation_react_v1':
            managed = True
        entries.extend(_objects(parent.get('evidence')))
        if not parent.get('evidence') and parent.get('result_ref'):
            entries.append({**parent, 'id': parent.get('id') or f'{task_id}:R1',
                            'check_status': parent.get('check_status') or 'basic_valid'})
        origins.append({'task_id': task_id, 'status': parent.get('status') or 'completed',
                        'final_reviewed': bool(parent.get('final_reviewed')),
                        'stop_reason': parent.get('stop_reason'),
                        'requirements': deepcopy(parent.get('requirements') or [])})
        if parent.get('analysis') or parent.get('report'):
            prior_reports.append({'task_id': task_id, 'status': parent.get('status'),
                                  'markdown': ((parent.get('report') or {}).get('markdown')
                                      if isinstance(parent.get('report'), dict) else None) or parent.get('analysis'),
                                  **({'references': _report_references(parent)} if report_mode else {})})
        if parent.get('status') in {'partial', 'failed', 'waiting_clarification'}:
            gaps.append(f'原任务{task_id}仍为{parent["status"]}：{parent.get("stop_reason") or "必要调查或交付尚未完成"}')
        for requirement in parent.get('requirements') or []:
            if isinstance(requirement, dict) and requirement.get('status') not in {'answered', 'completed', 'verified'}:
                gaps.append(f'原任务{task_id}未完成问题：{requirement.get("question") or requirement.get("id") or "未说明"}')
    source_gaps = list(dict.fromkeys(gaps))
    existing = {str(item.get('id') or item.get('result_id') or '') for item in entries}
    for item in state.get('reusable_evidence') or []:
        if not isinstance(item, dict) or str(item.get('task_id') or '') not in task_ids:
            continue
        rid = str(item.get('id') or item.get('result_id') or '')
        if rid and rid not in existing:
            entries.append(item)
            existing.add(rid)
    requested_results = state.get('analysis_result_ids') or []
    available_ids = {str(item.get('id') or item.get('result_id') or '') for item in entries}
    if requested_results:
        if (not isinstance(requested_results, list) or not all(isinstance(item, str) for item in requested_results)
                or not set(requested_results) <= available_ids):
            raise PermissionError('本轮分析结果选择不属于当前已授权的历史任务')
    report_references = [ref for report in prior_reports for ref in report.get('references', [])]
    referenced_ids = {ref['result_id'] for ref in report_references}
    closure_ids = set()
    for ref in report_references:
        candidates = [item for item in entries if str(item.get('id') or item.get('result_id') or '') == ref['result_id']]
        valid = [item for item in candidates if item.get('check_status') in {'basic_valid', 'reviewed'}]
        if not candidates:
            ref['unavailable_reason'] = '不在已选父任务的证据目录，未搜索其他任务'
        elif not valid:
            ref['unavailable_reason'] = '原结果已失效或未通过有效性检查'
        elif ref['version'] and all(
            str(item.get('version') or item.get('result_version') or item.get('fingerprint') or '')
            not in {'', ref['version']} for item in valid
        ):
            ref['unavailable_reason'] = '原报告引用版本与已保存证据版本不一致'
        else:
            closure_ids.add(ref['result_id'])
    if requested_results:
        chosen = set(requested_results) | closure_ids
        entries = [item for item in entries if str(item.get('id') or item.get('result_id') or '') in chosen]
    if not source and entries:
        # Legacy callers may omit a selected source. Resolve only a single
        # source already bound to server-selected evidence, never guess a
        # default or search results across databases. The checks below still
        # enforce current permissions, owner, original task and snapshot.
        bound_sources = {str(item.get('datasource_id') or '') for item in entries}
        if len(bound_sources) != 1 or '' in bound_sources:
            raise PermissionError('历史证据没有唯一且明确的数据源绑定')
        source = next(iter(bound_sources))
    allowed_tables = {physical_table_name(table).casefold() for table in SCHEMA
                      if str(table.get('database') or 'askdata_mock') == source
                      and current.allows_table(source, str(table['id']))}
    evidence, seen, remaining = [], {}, max_total_rows
    for entry in entries:
        rid = str(entry.get('id') or entry.get('result_id') or '')
        if not rid:
            gaps.append('所选调查有缺少结果编号的证据，未用于分析')
            continue
        if entry.get('check_status') not in {'basic_valid', 'reviewed'}:
            if rid in referenced_ids:
                gaps.append(f'原报告引用{rid}已失效或未通过有效性检查，不能沿用为有效证据')
            continue
        identity = (entry.get('user_id'), entry.get('task_id'), entry.get('datasource_id'))
        if not all(identity) or identity[0] != current.user_id or identity[2] != source or not current.allows_database(source):
            raise PermissionError(f'分析证据的用户、原任务或数据源范围不匹配：{rid}')
        if (entry.get('snapshot') or entry.get('scenario') or {}) != snapshot:
            raise PermissionError(f'分析证据快照范围不匹配：{rid}')
        declared_tables = {str(name).casefold() for name in (entry.get('query_contract') or {}).get('tables') or []} | _sql_tables(entry.get('sql'))
        if not declared_tables <= allowed_tables:
            raise PermissionError(f'分析证据引用的表已不在当前权限中：{rid}')
        ref = entry.get('result_ref')
        if rid in seen:
            if seen[rid] != (ref, identity, entry.get('version') or entry.get('fingerprint')):
                raise ValueError(f'同一结果编号出现冲突来源或版本：{rid}')
            continue
        seen[rid] = (ref, identity, entry.get('version') or entry.get('fingerprint'))
        if not ref:
            gaps.append(f'{rid}缺少受控结果引用，仅有历史预览，未当作完整证据')
            continue
        rows, page, limit = [], None, min(max_rows, remaining)
        if limit <= 0:
            gaps.append(f'{rid}超出本轮提供行数，未加载，不能把已显示结果当全集')
            continue
        try:
            while len(rows) < limit:
                page = engine.read_result(ref, current.user_id, str(identity[1]), source,
                                          offset=len(rows), limit=min(1000, limit - len(rows)))
                if page.get('task_id') != identity[1] or page.get('datasource_id') != source:
                    raise PermissionError(f'读取结果的原任务或数据源不匹配：{rid}')
                chunk = page.get('rows')
                if not isinstance(chunk, list):
                    raise ValueError(f'结果读取未返回真实行：{rid}')
                rows.extend(chunk)
                if not page.get('has_more'):
                    break
                if not chunk:
                    raise ValueError(f'结果分页未取得后续数据：{rid}')
        except PermissionError:
            raise
        except (KeyError, OSError, ValueError) as exc:
            gaps.append(f'{rid}的已保存结果不可读取：{type(exc).__name__}；没有重新查询或把缺失当零')
            continue
        if page is None:
            continue
        if not _sql_tables(page.get('sql')) <= allowed_tables:
            raise PermissionError(f'已保存结果的真实SQL来源已不在当前表权限中：{rid}')
        columns = list(page.get('columns') or [])
        if entry.get('columns') and columns != list(entry['columns']):
            gaps.append(f'{rid}的真实结果列与历史证据不一致，未用于分析')
            continue
        if any(not isinstance(row, dict) or not set(row) <= set(columns) for row in rows):
            gaps.append(f'{rid}的真实结果列与数据不一致，未用于分析')
            continue
        complete = bool(page.get('complete')) and not page.get('has_more')
        total = page.get('total_rows')
        if total is not None and len(rows) < int(total):
            complete = False
        version = str(entry.get('version') or entry.get('result_version') or entry.get('fingerprint') or '')
        if not version:
            version = hashlib.sha256(json.dumps({'id': rid, 'task_id': identity[1], 'datasource_id': source,
                'sql': page.get('sql'), 'columns': columns, 'rows': rows,
                'complete': complete}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        output = deepcopy(entry)
        output.update(id=rid, task_id=identity[1], user_id=current.user_id, datasource_id=source,
                      snapshot=deepcopy(snapshot), rows=rows, columns=columns,
                      sql=page.get('sql') or entry.get('sql') or '',
                      complete=complete, total_rows=total, stored_rows=int(total) if total is not None else len(rows),
                      display_truncated=not complete, provided_rows=len(rows), version=version,
                      source_task_id=entry.get('source_task_id') or identity[1],
                      source_result_id=entry.get('source_result_id') or rid)
        evidence.append(output)
        remaining -= len(rows)
        if not complete:
            gaps.append(f'{rid}仅提供{len(rows)}行，结果仍有截断或原查询不完整；禁止据此计算全集或总体占比')
    loaded = {item['id']: item for item in evidence}
    unavailable_references = []
    for report in prior_reports:
        for ref in report.get('references', []):
            item = loaded.get(ref['result_id'])
            reason = ref.get('unavailable_reason')
            if not reason and item is None:
                reason = '本轮未能读取有效保存结果，不能从原报告正文补造证据'
            if not reason and ref['version'] and ref['version'] != item['version']:
                reason = '原报告引用版本与本轮保存结果版本不一致'
            if reason:
                ref.update(status='unavailable', unavailable_reason=reason)
                unavailable_references.append({'report_task_id': report['task_id'], **ref})
                gaps.append(f'原报告{report["task_id"]}引用{ref["result_id"]}不可沿用：{reason}')
            else:
                ref.update(status='available', version=item['version'], complete=item['complete'])
    if requested_results and report_mode:
        available_refs = {ref['result_id'] for ref in report_references if ref.get('status') == 'available'}
        # A version only computable after reading must also match before the
        # automatically added result becomes an authorized report source.
        evidence = [item for item in evidence if item['id'] in set(requested_results) | available_refs]
        loaded = {item['id']: item for item in evidence}
    if managed and not evidence:
        gaps.append('最近所选调查没有可读取的有效证据；未回退到更早任务，也未重新查数')
    authorized_sources = {item['id']: {key: deepcopy(item[key]) for key in
                          ('user_id', 'task_id', 'datasource_id', 'snapshot', 'version')} for item in evidence}
    return {'managed': managed, 'evidence': evidence, 'evidence_gaps': list(dict.fromkeys(gaps)),
            'source_gaps': source_gaps, 'data_gaps': [gap for gap in dict.fromkeys(gaps) if gap not in source_gaps],
            'source_tasks': origins, 'prior_reports': prior_reports,
            'report_reference_closure': ({'requested_result_ids': list(requested_results),
                'added_result_ids': sorted((closure_ids - set(requested_results)) & loaded.keys()) if requested_results else [],
                'unavailable_references': unavailable_references} if report_mode else {}),
            'report_scope': {**current.public(), 'task_id': state.get('task_id'),
                             'datasource_id': source, 'snapshot': snapshot,
                             'authorized_analysis_sources': authorized_sources}}


def model_analysis_context(value: Any) -> Any:
    """Keep storage handles and user IDs internal; R identities stay stable."""
    if isinstance(value, dict):
        output = {key: model_analysis_context(item) for key, item in value.items()
                  if key not in {'result_ref', 'user_id', 'report_scope'}}
        contract = output.get('query_contract')
        if isinstance(contract, dict) and isinstance(contract.get('validation'), dict):
            validation = contract['validation']
            if output.get('column_metadata') == validation.get('column_metadata'):
                validation.pop('column_metadata', None)
            normalizations = validation.pop('normalizations', None)
            if isinstance(normalizations, list):
                validation['normalized_paths'] = list(dict.fromkeys(str(item.get('path'))
                    for item in normalizations if isinstance(item, dict)))
            summary = validation.get('sql_summary')
            if isinstance(summary, dict):
                validation['sql_summary'] = {key: item for key, item in summary.items()
                    if key in {'source_fields', 'predicates'}}
        return output
    if isinstance(value, list):
        return [model_analysis_context(item) for item in value]
    return deepcopy(value)


def focus_qa_context(context: dict, mode: str) -> dict:
    """Task purpose controls which kinds of memory enter the answering prompt.

    Numeric follow-ups keep their real evidence, scope and gaps, but do not
    inherit an earlier narrative or the original investigation's to-do list.
    Source records remain unchanged and available for explicit report work.
    """
    focused = dict(context)
    focused['analysis_mode'] = mode
    if not isinstance(context.get('evidence'), list):
        return focused
    focused['trust_boundary'] = {
        'observed_results': 'evidence中的真实SQL/计算结果及行、完整性、单位、统计集合和解释限制是核对依据；不是全部业务主张自动成立',
        'historical_narrative': 'prior_reports是待重新核查的历史主张，不是本轮事实来源；引用可读取仅证明来源绑定，不证明句子含义',
        'historical_delivery_status': '旧status/final_reviewed/问题完成状态仅为历史交付记录，不认证本轮任何结论',
        'interpretation_limits': '遵守证据已记录的interpretation/interpretation_limits及列定义；旧记录缺失限制字段表示未记录，不等于无限解释权限',
        'missing_evidence': '本轮未提供或未取得的证据不得猜造，也不能据此宣称源数据不存在；不自动新查SQL',
        'editing_scope': '重排报告不等于核实事实；新输出中保留的旧主张和新增主张都须当前证据支持。发现事实或方法错误须明确未解决，不能仅润色后通过',
    }
    focused['source_tasks'] = [{'task_id': item.get('task_id'),
        'historical_delivery_status': {key: deepcopy(value) for key, value in item.items() if key != 'task_id'}}
        for item in context.get('source_tasks') or [] if isinstance(item, dict)]
    if mode == 'fact':
        for key in ('prior_reports', 'short_term', 'business_methods'):
            focused.pop(key, None)
    elif mode == 'report':
        # Keep one selected report as material to check, not a second copy of
        # the same assistant narrative in short-term memory or its summary.
        focused['prior_reports'] = []
        for item in context.get('prior_reports') or []:
            previous = deepcopy(item) if isinstance(item, dict) else {'markdown': str(item)}
            if 'status' in previous:
                previous['historical_delivery_status'] = previous.pop('status')
            previous['material_role'] = 'historical_claims_to_recheck'
            focused['prior_reports'].append(previous)
        history = focused.pop('short_term', '')
        if isinstance(history, str):
            try:
                history = json.loads(history) if history else {}
            except ValueError:
                history = {}
        if isinstance(history, dict):
            user_messages = [{'turn_id': item.get('turn_id'), 'user_message': item['user_message']}
                for item in history.get('pending_turns') or []
                if isinstance(item, dict) and isinstance(item.get('user_message'), str) and item['user_message']]
            if user_messages:
                focused['prior_user_messages'] = user_messages
    return focused
