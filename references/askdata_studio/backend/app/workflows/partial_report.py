"""Deterministic delivery of authorized results when investigation cannot finish.

No model text, draft claims, or failed-query values are promoted to findings.
The caller reads each source through the normal authorization/version boundary.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html

from ..models import AnalysisReport


def cell(value):
    return html.escape(str(value if value is not None else '未提供')).replace('|', '&#124;').replace('\n', ' ')


def build_partial_report(task, evidence, *, budget=None, unavailable=()):
    from ..querying.data_qa_agent import DataQaAgent
    sources = DataQaAgent._investigation_sources(evidence, task.state['access_scope'])
    title = '阶段性经营分析报告' if sources else '调查进度说明'
    lines = [f'# {title}', '', '> 分析尚未完成。以下保留已取得的数据，未确认的原因不作为结论。', '',
             '## 本次问题', '', cell(task.state.get('query', '经营分析')), '', '## 当前进展', '']
    if sources:
        lines.append(f'已整理 **{len(sources)} 份可读取的查询结果**。这些数据可用于继续调查，但不足以代表所有原因均已验证。')
    else:
        lines.append('本次尚未取得可用于业务判断的有效数据，因此暂不提供经营结论。')
    if budget and budget.react_rounds >= budget.react_round_limit:
        lines.append(f'本次已达到 **{budget.react_round_limit} 轮取证上限**，已停止新增查询，先交付已有结果。')
    elif task.stop_reason:
        lines.append('调查未能继续完成；已保存进度与执行记录。后续继续时仍使用原任务的剩余预算。')
    lines += ['', '## 已取得的数据', '']
    by_id = {e['id']: e for e in evidence}
    for source in sources:
        item = by_id[source.result_id]
        metadata = item.get('column_metadata') or {}
        contract = item.get('query_contract') or {}
        definition = item.get('calculation_definition') or {}
        titles = {'homenova.paid_goods.monthly.v1': '支付商品表现',
            'homenova.inventory.duration.monthly.v1': '库存状态时长',
            'homenova.paid_and_sessions.monthly.v1': '支付商品与访问会话（各自统计口径）'}
        source_title = titles.get(definition.get('recipe_id'), source.title)
        if definition.get('recipe_id') == 'homenova.sessions.monthly.v1':
            source_title = {'valid_sessions': '访问会话与成功购买', 'sku_view_sessions': 'SKU 浏览与同 SKU 购买',
                     'category_view_sessions': '品类浏览与同品类购买'}.get(definition.get('cohort'), source_title)
        lines += [f'### {cell(source_title)}', '', f'证据：`{source.result_id}`', '']
        if definition.get('filters'):
            labels = {'category_code': '品类', 'sku_id': 'SKU', 'source_channel': '渠道'}
            scope = [labels.get(field.rsplit('.', 1)[-1], field) + '：' +
                     '、'.join(str(v) for v in (values if isinstance(values, (list, tuple)) else [values]))
                     for field, values in definition['filters']]
            lines += ['统计范围：' + cell('；'.join(scope)), '']
        grain = contract.get('grain')
        if grain and grain not in {'dimension', 'overall', 'aggregate'}:
            lines += ['统计粒度：' + cell(grain), '']
        if not source.rows:
            lines += ['该查询返回空结果；不能将空结果解释为业务指标为零。', '']
            continue
        # Display slices are explicitly labelled. Never sum or extrapolate them.
        columns = source.columns[:10]
        if definition.get('recipe_id') == 'homenova.inventory.duration.monthly.v1':
            columns = [c for c in ('period', 'sku_id', 'warehouse_id', 'duration_hours',
                                   'time_fraction', 'episode_count') if c in source.columns]
            lines += ['按 SKU、仓库分别统计，不能直接相加为整个品类的缺货时长。', '']
        labels, divisors, rounded = [], {}, set()
        for column in columns:
            definition = metadata.get(column) or {}
            metric = task.metric_pool.get(definition.get('metric_id') or column) or {}
            label = metric.get('label') or {'month': '月份', 'period': '期间', 'category_code': '品类',
                'sku_id': 'SKU', 'warehouse_id': '仓库', 'source_channel': '渠道',
                'duration_hours': '状态持续时间（小时）', 'time_fraction': '时间占比',
                'episode_count': '持续区间数', 'unmatured_sessions': '观察期未结束的会话数',
                'category_view_sessions': '品类浏览会话数', 'category_view_paid_sessions': '浏览且购买同品类的会话数',
                'sku_view_sessions': 'SKU 浏览会话数', 'sku_view_paid_sessions': '浏览且购买同 SKU 的会话数',
                'checkout_paid_sessions': '产生成功支付订单的会话数'}.get(column, column)
            unit = source.units.get(column) or ''
            if not unit and metadata.get(column):
                unit = '原始值，展示单位未确认'
            # Convert only an explicitly bound unit; never guess from magnitude.
            money_units = {'EUR cents': '欧元', 'EUR cents/order': '欧元/单', 'EUR cents/item': '欧元/件'}
            if unit in money_units:
                divisors[column] = Decimal(100)
                unit = money_units[unit]
                rounded.add(column)
            elif unit == 'ratio':
                divisors[column] = Decimal('0.01')
                unit = '%'
                rounded.add(column)
            elif unit == 'items/order':
                unit = '件/单'
                rounded.add(column)
            elif unit == 'hours':
                rounded.add(column)
            unit = {'orders': '单', 'items': '件', 'sessions': '次会话',
                    'hours': '小时', 'episodes': '段'}.get(unit, unit)
            labels.append(cell(label + (f'（{unit}）' if unit else '')))
        lines += ['| ' + ' | '.join(labels) + ' |', '| ' + ' | '.join(['---'] * len(columns)) + ' |']
        for row in source.rows[:12]:
            values = []
            for column in columns:
                value = row.get(column)
                if value is not None and column in rounded:
                    try:
                        value = format((Decimal(str(value)) / divisors.get(column, Decimal(1)))
                            .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), ',.2f')
                    except InvalidOperation:
                        pass
                values.append(cell(value))
            lines.append('| ' + ' | '.join(values) + ' |')
        if not source.complete or len(source.rows) > 12 or len(source.columns) > 10:
            lines += ['', f'此处展示前 {min(12, len(source.rows))} 行、{len(columns)} 列；为展示节选，不代表完整统计集合。完整结果请查看对应证据。']
        for limit in item.get('interpretation_limits') or []:
            lines += ['', '解释边界：' + cell(limit)]
        lines.append('')
    gaps = []
    for review in task.reviews[-1:]:
        gaps.extend(str(g) for g in (review.get('completion_check') or {}).get('remaining_core_gaps') or [])
        gaps.extend(str(g) for g in review.get('unanswered') or [])
    for q in task.progress.get('core_questions') or []:
        if q.get('status') not in {'answered', 'not_applicable'}:
            gaps.append(str(q.get('question') or '原问题尚未回答完整'))
    for action in task.actions:
        if action.get('action_type') == 'query' and action.get('status') != 'completed':
            reason = {'metric_scope': '统计集合需要明确', 'request_shape': '计算参数尚未补齐',
                'recipe_dependencies': '必要数据字段尚未准备完整', 'model_service': '查询生成服务中断',
                'model_not_sent': '查询生成请求未能发出', 'budget': '已到执行预算边界',
                'missing_context': '必要资料尚未取得'}.get(action.get('error_kind'), '查询尚未取得有效结果')
            gaps.append(f'{action.get("goal", "待完成查询")}：{reason}')
    gaps.extend(str(g) for g in unavailable)
    gaps.append('经营变化的完整原因及各因素贡献尚未完成核实；以上数据不单独证明因果关系。')
    lines += ['## 尚未完成与结论边界', '']
    lines += ['- ' + cell(g) for g in dict.fromkeys(gaps)]
    lines += ['', '## 建议下一步', '', '优先补齐上述未完成事项，复用已有结果核对原因；在原因确认前，不据此直接调整投放、促销或库存策略。']
    return AnalysisReport(title=title, markdown='\n'.join(lines), data_sources=sources,
                          warnings=['阶段性报告：数据摘录由保存结果生成，尚未完成全部业务分析。'])
