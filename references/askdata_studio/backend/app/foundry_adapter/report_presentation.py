"""Render Agent-selected report references. Never compose business analysis."""
import base64
import json
import math
import re
from urllib.parse import quote

APPENDIX = '<!-- homenova-report-appendix:v1 -->'


def present_report(report: dict) -> str:
    text = str(report.get('markdown') or '')
    # Internal rendering blocks are created here, never accepted from model text.
    text = re.sub(r'<!-- homenova-(?:chart|source|report-appendix):.*?-->', '', text, flags=re.S)
    sources = {s['result_id']: s for s in report.get('data_sources') or [] if s.get('result_id')}
    used = []

    def remember(rid):
        if rid not in used:
            used.append(rid)
        return quote(rid, safe='')

    def citation(match):
        refs = [r.strip() for r in match.group(1).split(',')]
        if not refs or any(r.split('@')[0] not in sources for r in refs):
            return match.group(0)
        return ' '.join(f'[查看明细](#report-source-{remember(r.split("@")[0])})' for r in refs)

    text = re.sub(r'(?<!!)\[([^\[\]\n]+)\](?!\()', citation, text)
    charts = {}
    for spec in report.get('visualizations') or []:
        source = sources.get(spec.get('source_result_id'))
        if not source or not source.get('complete') or spec.get('source_version') != source.get('version'):
            continue
        field = spec.get('value_field')
        points = [{'label': str(row[spec['category_field']]) + (
                      ' · ' + str(row.get(spec['series_field'], '')) if spec.get('series_field') else ''), 'value': row[field]}
                  for row in source.get('rows', [])
                  if row.get(spec.get('category_field')) is not None
                  and isinstance(row.get(field), (int, float)) and not isinstance(row.get(field), bool)
                  and math.isfinite(row[field])]
        if not points:
            continue
        if spec.get('order') == 'value_desc':
            points.sort(key=lambda p: p['value'], reverse=True)
        elif spec.get('order') == 'category_asc':
            points.sort(key=lambda p: p['label'])
        payload = {k: spec.get(k) for k in ('type', 'title', 'unit', 'currency', 'precision')}
        payload.update(points=points[:spec.get('max_items', 12)], source=quote(source['result_id'], safe=''))
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).decode()
        charts[f'{source["result_id"]}:{field}'] = (source['result_id'], '<!-- homenova-chart:' + encoded + ' -->')

    def chart(match):
        item = charts.get(match.group(1).strip())
        if not item:
            return ''  # A rejected chart cannot survive through its placeholder.
        remember(item[0])
        return item[1]

    text = re.sub(r'<!--\s*chart:(.*?)\s*-->', chart, text)
    if used:
        sections = []
        for rid in used:
            source = sources[rid]
            columns = source.get('columns') or []
            rows = source.get('rows') or []
            def cell(value):
                return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')
            units = ', '.join(f'{key}: {value}' for key, value in (source.get('units') or {}).items())
            lines = [f'<!-- homenova-source:{quote(rid, safe="")} -->',
                     f'### {cell(source.get("title") or rid)}',
                     f'来源：{cell(rid)}。展示 {min(len(rows), 12)} 行，结果共 {source.get("total_rows") or len(rows)} 行。']
            if units:
                lines.append('原始数据单位：' + units)
            if columns:
                lines += ['| ' + ' | '.join(map(cell, columns)) + ' |', '| ' + ' | '.join('---' for _ in columns) + ' |']
                lines += ['| ' + ' | '.join(cell(row.get(c)) for c in columns) + ' |' for row in rows[:12]]
            sections.append('\n\n'.join(lines[:4]) + '\n' + '\n'.join(lines[4:]))
        text += '\n\n' + APPENDIX + '\n\n' + '\n\n'.join(sections)
    return text
