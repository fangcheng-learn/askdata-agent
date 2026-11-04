"""Assemble Agent-authored report sections, without inventing prose or data.

Legacy markdown drafts remain readable. New drafts expose their presentation
choice before prose so evidence, a table/chart and interpretation stay together.
"""
from copy import deepcopy
import re


def compose_report(response: dict) -> dict:
    raw = response.get('report') if isinstance(response.get('report'), dict) else response
    if 'analysis_points' not in raw:
        return response
    points = raw.get('analysis_points')
    if not isinstance(points, list):
        raise ValueError('analysis_points必须为观点数组')
    parts = ['## 分析综述', str(raw.get('overview') or '').strip()]
    charts = []

    def citations(ids):
        if not isinstance(ids, list) or not all(isinstance(rid, str) for rid in ids):
            raise ValueError('观点证据必须为真实结果ID数组')
        return ' '.join('[' + rid + ']' for rid in dict.fromkeys(ids))

    for index, point in enumerate(points, 1):
        if not isinstance(point, dict):
            raise ValueError('每个观点必须是对象')
        # Add numbering once here, instead of asking the model to keep headings
        # and chart anchors synchronized. No business conclusion is generated.
        title = re.sub(r'^(?:[一二三四五六七八九十]+[、．.]|\d+[、．.])\s*', '', str(point.get('title') or '').strip())
        heading = f'{index}. {title}'
        parts.append('### ' + heading)
        presentation = point.get('presentation', 'text')
        table = str(point.get('table_markdown') or '').strip()
        chart = point.get('visualization')
        both = presentation == 'both' and bool(str(point.get('dual_presentation_reason') or '').strip())
        if presentation == 'table' or presentation == 'both' and (both or table):
            if table:
                parts.append(table)
        if isinstance(chart, dict) and (presentation == 'chart' or both or presentation == 'both' and not table):
            charts.append({**deepcopy(chart), 'section_heading': heading})
        explanation = str(point.get('analysis') or '').strip()
        if explanation:
            parts.append(explanation)
        refs = citations(point.get('evidence_ids') or [])
        if refs:
            parts.append(refs)
    actions = raw.get('action_items') or []
    if not isinstance(actions, list):
        raise ValueError('action_items必须为行动数组')
    if actions:
        priorities = {'高': ('🔴', '高优先级'), 'high': ('🔴', '高优先级'),
                      '中': ('🟠', '中优先级'), 'medium': ('🟠', '中优先级'),
                      '低': ('🔵', '低优先级'), 'low': ('🔵', '低优先级')}
        rows = []
        for item in actions:
            if not isinstance(item, dict):
                raise ValueError('行动必须为对象')
            priority = str(item.get('priority') or '').strip()
            icon, label = priorities.get(priority.lower().removesuffix('优先级'), ('⚪', priority))
            heading = '：'.join(v for v in (label, str(item.get('action') or '').strip()) if v)
            details = []
            for key in ('basis', 'expected_check'):
                detail = str(item.get(key) or '').strip()
                if detail and detail not in details:
                    # Keep intended validation distinct from observed facts;
                    # a target outcome is not evidence it already happened.
                    details.append(('验证目标：' if key == 'expected_check' else '') + detail)
            body = ' '.join(v if v.endswith(('。', '！', '？', '.', '!', '?')) else v + '。' for v in details)
            refs = citations(item.get('evidence_ids') or [])
            rows.append(f'- {icon} **{heading}**' + (f'\n\n  {body}' if body else '') + (f' {refs}' if refs else ''))
        parts.extend(['## 行动建议', '\n\n'.join(rows)])
    return {key: value for key, value in {**raw, 'markdown': '\n\n'.join(p for p in parts if p),
            'visualizations': charts}.items() if key not in {'overview', 'analysis_points', 'action_items'}}
