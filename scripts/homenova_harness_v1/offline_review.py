"""Development-only independent review of saved real runs; no network/model calls.

Private reference assets are loaded here, in a separate process. Nothing from
this evaluator is imported by the application, Skills, retriever or API runners.
"""
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data/homenova_harness_v1/private'
RUN = OUT / 'baseline_signoff'
REFERENCE = ROOT / 'data/homenova_phase_d/private/evaluation_baseline/results'


def load(path):
    return json.loads(path.read_text())


def successful_queries(response):
    evidence = [item for item in response.get('evidence', []) if item.get('sql') and item.get('check_status') != 'superseded']
    calls = [item for item in response.get('tool_calls', []) if item.get('sql') and item.get('success')]
    return evidence or calls


# Human adjudication after reading saved SQL, scope, rows and approved text.
# This is assessment data, not task-specific instructions for the Agent.
notes = {
    'q01': ('failed', 'query_contract', '多事件日期筛选仍未完整声明，真实数据库工具未执行；不是业务通过。'),
    'q02': ('failed', 'metric_semantics', '把发货数量当支付销量；PLG-MINI返回2069件，独立支付口径为2146件。部分净销售/毛利率吻合不能抵消销量与GMV缺口。'),
    'q03': ('partial', 'query_contract', '取得竞品平均观测价格；商品浏览转化与本店价格修复耗尽，缺SKU/渠道及报价时效队列。部分内容明确保留因果未知。'),
    'q04': ('failed', 'query_contract', 'orders.ordered_at、sessions.started_at等筛选未完整声明；无真实业务查询结果。'),
    'q05': ('failed', 'metric_semantics', '派生利润与来源字段契约失败；未取得两期销量/单价/利润的可核验结果。'),
    'q06': ('failed', 'query_contract', '库存区间语义/字段与查询契约修复耗尽；不能以无查询结果解释为无缺货。'),
    'q07': ('failed', 'query_contract', '履约、反馈与退款多事实查询未通过；未覆盖所需粒度。'),
    'q08': ('failed', 'query_contract', '退款发生期与原成交队列查询未通过；未交付所需金额拆分。'),
    'q09': ('failed', 'cohort_semantics', '早截面修复耗尽；晚截面把状态再过滤进固定支付队列，分母1501少于独立1542，且退款/成本恢复/投诉缺失。'),
    'q10': ('partial', 'verified_capability_boundary', '未伪造精确反事实数字，公开能力边界说明合理；未查询可观察量化证据且未做独立交付审核，保留部分完成。'),
}
rows = []
for path in sorted(RUN.glob('q??_asof_*.json')):
    case = load(path)
    response = case.get('response') or {}
    verdict, category, reason = notes[case['question_id']]
    rows.append({'case_id': case['case_id'], 'question_id': case['question_id'],
        'snapshot': case['snapshot'], 'agent_status': response.get('status'), 'business_verdict': verdict,
        'category': category, 'reason': reason, 'actual_query_count': len(successful_queries(response)),
        'final_reviewed': response.get('final_reviewed'), 'budget': response.get('budget'),
        'elapsed_seconds': case['elapsed_seconds'], 'selected_snapshot_verified': case.get('selected_snapshot_verified')})
ref_q02 = {row['sku_id']: row for row in load(REFERENCE / 'q02.json')['rows']}
actual_q02 = load(RUN / 'q02_asof_2026_08_31.json')['response']['rows']
q02_comparisons = [{'sku_id': row['sku_id'], 'actual_quantity': row['total_quantity'],
    'expected_paid_units': ref_q02[row['sku_id']]['paid_units'],
    'actual_net_sales': row['net_sales_ex_vat_cents'],
    'expected_net_sales': ref_q02[row['sku_id']]['net_sales_ex_vat_cents'],
    'actual_margin': row['goods_margin_ratio'], 'expected_margin': ref_q02[row['sku_id']]['goods_margin']}
    for row in actual_q02]
refs = load(REFERENCE / 'q09.json')['rows']
q09_expected_counts = [{'snapshot': row['snapshot'], 'month': row['cohort_month'],
                        'fixed_paid_order_count': row['fixed_paid_order_count']} for row in refs]
q10 = next(row for row in rows if row['question_id'] == 'q10')
result = {'method': 'Independent offline numerical comparison plus explicit review of SQL and approved text',
    'network_or_model_calls': False, 'reference_assets_used_only_in_this_evaluator': True,
    'case_count': len(rows), 'question_count': len({row['question_id'] for row in rows}),
    'business_verdict_counts': dict(Counter(row['business_verdict'] for row in rows)),
    'question_verdict_counts': dict(Counter(notes[key][0] for key in sorted(notes))),
    'cases': rows, 'q02_independent_comparisons': q02_comparisons, 'q09_expected_fixed_counts': q09_expected_counts,
    'full_business_passes': 0, 'latest_delivery_gate_real_validation': 'blocked_by_model_Arrearage',
    'caution': 'The last complete baseline predates the delivery text gate; later source changes are not represented as a new full real run.'}
target = OUT / 'independent_business_review.json'
if target.exists():
    raise RuntimeError('Preserve the earlier independent review; use a new target')
target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: result[key] for key in ['case_count', 'question_count', 'business_verdict_counts', 'full_business_passes']}, ensure_ascii=False))
