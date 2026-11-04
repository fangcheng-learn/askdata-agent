"""Render Agent-selected report references. Never compose business analysis."""
import base64
import json
import math
import re
from urllib.parse import quote

APPENDIX = '<!-- homenova-report-appendix:v1 -->'


def chart_payload(spec: dict, source: dict) -> dict | None:
    """Project frozen rows once for both the report and output-pane artifacts."""
    if not source.get('complete') or spec.get('source_version') != source.get('version'):
        return None
    kind = spec.get('type')
    if kind not in {'bar', 'grouped_bar', 'line', 'area', 'pie', 'donut', 'scatter'}:
        return None
    category, field, series, x_field = (spec.get(k) for k in ('category_field', 'value_field', 'series_field', 'x_field'))
    points = []
    try:
        for row in source.get('rows', []):
            if row.get(category) is None or isinstance(row.get(field), bool):
                return None
            value = float(row[field])
            if not math.isfinite(value):
                return None
            point = {'label': str(row[category]), 'value': value}
            if series:
                if row.get(series) is None:
                    return None
                point['series'] = str(row[series])
            if kind == 'scatter':
                if isinstance(row.get(x_field), bool):
                    return None
                point['x'] = float(row[x_field])
                if not math.isfinite(point['x']):
                    return None
            points.append(point)
    except (ValueError, TypeError, KeyError):
        return None
    if not points:
        return None
    if kind == 'grouped_bar' and not series:
        return None
    if kind in {'grouped_bar', 'line', 'area'} or (kind == 'bar' and series):
        pairs = [(p['label'], p.get('series', '')) for p in points]
        if len(set(pairs)) != len(pairs):
            return None
    order = spec.get('order', 'source')
    if kind in {'line', 'area'} and order == 'value_desc':
        order = 'category_asc'
    if order == 'value_desc':
        points.sort(key=lambda p: p['value'], reverse=True)
    elif order == 'category_asc':
        points.sort(key=lambda p: [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', p['label'])])
    count = max(3, min(int(spec.get('max_items', 12)), 30))
    total = len(points)
    if kind in {'pie', 'donut'} and (total > count or any(p['value'] < 0 for p in points)):
        return None
    if series and kind in {'bar', 'grouped_bar', 'line', 'area'}:
        # Limit categories, keeping all observed series within each category.
        labels = list(dict.fromkeys(p['label'] for p in points))[:count]
        if len({p['series'] for p in points}) > 12:
            return None
        points = [p for p in points if p['label'] in labels]
    else:
        points = points[:count]
    units, currencies, precisions = (source.get(k) or {} for k in ('units', 'currencies', 'precisions'))
    return {'type': kind, 'title': spec.get('title', ''), 'points': points,
            'source': quote(source['result_id'], safe=''), 'total_points': total,
            'unit': units.get(field), 'currency': currencies.get(field), 'precision': precisions.get(field, 2),
            'x_unit': units.get(x_field), 'x_precision': precisions.get(x_field, 2),
            'x_title': x_field, 'value_title': field}


def encode_chart(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).decode()


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
        links = []
        for rid in dict.fromkeys(r.split('@')[0] for r in refs):
            anchor = remember(rid)
            links.append(f'[证据{used.index(rid)+1}](#report-source-{anchor})')
        return ' '.join(links)

    text = re.sub(r'(?<!!)\[([^\[\]\n]+)\](?!\()', citation, text)
    charts = []
    sections_by_chart = {}
    for spec in report.get('visualizations') or []:
        source = sources.get(spec.get('source_result_id'))
        payload = chart_payload(spec, source) if source else None
        if payload:
            sections_by_chart[len(charts)] = str(spec.get('section_heading') or '').strip()
            charts.append((f'{source["result_id"]}:{spec["value_field"]}', source['result_id'],
                           '<!-- homenova-chart:' + encode_chart(payload) + ' -->'))
    placed = set()

    def chart(match):
        key = match.group(1).strip()
        for index, (chart_key, rid, markup) in enumerate(charts):
            if chart_key == key and index not in placed:
                placed.add(index)
                remember(rid)
                return markup
        return ''

    # Preserve older explicit placements, but a missing model marker never hides a chart.
    text = re.sub(r'<!--\s*chart:(.*?)\s*-->', chart, text)
    remaining = []
    section_blocks = {}
    section_pattern = re.compile(r'^#{1,6}[ \t]+(.+?)[ \t]*$', re.M)
    heading_matches = list(section_pattern.finditer(text))
    available_headings = {m.group(1) for m in heading_matches}
    for index, (_, rid, markup) in enumerate(charts):
        if index not in placed:
            remember(rid)
            heading = sections_by_chart.get(index)
            if heading in available_headings:
                section_blocks.setdefault(heading, []).append(markup)
            else:
                # Legacy drafts may omit an anchor. Attach to the section that
                # cites this source, rather than inventing a separate chart dump.
                citation_pos = text.find('#report-source-' + quote(rid, safe=''))
                preceding = [m for m in heading_matches if m.start() < citation_pos]
                if preceding:
                    section_blocks.setdefault(preceding[-1].group(1), []).append(markup)
                else:
                    remaining.append(markup)
    def place_section(match):
        blocks = section_blocks.pop(match.group(1), [])
        return match.group(0) + ('\n\n' + '\n\n'.join(blocks) + '\n' if blocks else '')
    text = section_pattern.sub(place_section, text)
    if remaining:
        # Retain unanchored historical charts without a new generic section.
        text = text.rstrip() + '\n\n' + '\n\n'.join(remaining) + '\n'
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
