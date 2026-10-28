#!/usr/bin/env python3
"""Offline comparison of real API evidence with private D reference result JSON.

No network or model calls. This process is separate from run_agent_tests.py.
Numerical agreement is not treated as proof of business semantics or causality.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
KEYS = {
    "q01": ("month",), "q02": ("sku_id",), "q03": ("month", "sku_id", "source_channel"),
    "q04": ("label", "channel_code"), "q05": ("label", "sku_id"), "q06": ("sku_id",),
    "q07": ("carrier_code", "postal_band"), "q08": ("original_capture_month", "refund_branch"),
    "q09": ("cohort_month",), "q10": ("sku_id",),
}
REQUIRED = {
    "q01": {"paid_orders", "gmv_gross_cents", "goods_aov_gross_cents", "net_sales_ex_vat_cents", "goods_profit_ex_vat_cents"},
    "q02": {"paid_units", "gmv_gross_cents", "goods_profit_ex_vat_cents", "goods_margin"},
    "q03": {"product_view_sessions", "paid_product_view_sessions", "product_view_session_cvr",
            "fresh_comparable_quote_sessions", "average_displayed_unit_gross_cents", "average_known_competitor_unit_gross_cents"},
    "q04": {"summed_budget_caps_ex_vat_cents", "spend_ex_vat_cents", "paid_landing_sessions",
            "attributed_goods_ex_vat_cents", "goods_roas_ex_vat"},
    "q05": {"paid_units", "realized_unit_gross_cents", "gmv_gross_cents", "cohort_goods_profit_ex_vat_cents"},
    "q06": {"oos_intervals", "oos_hours", "longest_continuous_oos_hours", "blocked_attempts",
            "billed_clicks_while_unavailable", "spend_while_unavailable_ex_vat_cents"},
    "q07": {"shipped_orders", "overdue_orders", "orders_with_inquiry_or_complaint", "inquiry_or_complaint_roots",
            "mean_order_level_rating", "completed_cash_refund_gross_cents"},
    "q08": {"cash_refund_gross_cents", "refunded_vat_cents", "goods_revenue_reversal_ex_vat_cents",
            "these_refunds_cost_recovered_as_of_ex_vat_cents"},
    "q09": {"fixed_paid_order_count", "not_shipped_orders", "shipped_not_delivered_orders",
            "completed_cash_refund_gross_cents", "recovered_cost_ex_vat_cents",
            "cohort_net_sales_ex_vat_cents", "cohort_goods_profit_ex_vat_cents", "customer_contact_roots", "review_events"},
    "q10": set(),
}
ALIASES = {
    "month": ("月份", "年月", "支付月", "stat_month"),
    "cohort_month": ("支付月份", "原支付月", "原成交月", "capture_month", "month"),
    "original_capture_month": ("原成交月份", "原支付月份", "capture_month", "cohort_month"),
    "sku_id": ("sku", "商品编号", "SKU编号", "商品SKU"),
    "source_channel": ("来源渠道", "访问来源", "渠道", "channel"),
    "channel_code": ("channel", "渠道", "付费渠道", "source_channel"),
    "label": ("period", "时期", "期间", "阶段", "comparison_period"),
    "carrier_code": ("承运商", "carrier"), "postal_band": ("邮区", "邮编组", "postal_group"),
    "refund_branch": ("退款类型", "退款分支", "refund_type"),
    "paid_orders": ("已支付订单数", "支付订单数", "paid_order_count"),
    "gmv_gross_cents": ("商品GMV", "GMV", "商品成交额", "商品成交额欧分"),
    "goods_aov_gross_cents": ("客单价", "商品客单价", "goods_aov"),
    "net_sales_ex_vat_cents": ("商品净销售额", "净销售额", "net_sales"),
    "goods_profit_ex_vat_cents": ("商品毛利", "商品毛利润", "goods_profit", "gross_profit_ex_vat_cents"),
    "paid_units": ("销量", "已支付件数", "销售件数", "units_sold"),
    "goods_margin": ("商品毛利率", "毛利率", "gross_margin"),
    "realized_unit_gross_cents": ("成交单价", "实际成交单价", "average_unit_price"),
    "cohort_goods_profit_ex_vat_cents": ("队列商品毛利", "商品毛利", "商品毛利润", "goods_profit_ex_vat_cents"),
    "cohort_net_sales_ex_vat_cents": ("队列净销售额", "净销售额", "net_sales_ex_vat_cents"),
    "cohort_goods_margin": ("队列毛利率", "商品毛利率", "毛利率"),
    "fixed_paid_order_count": ("固定队列订单数", "队列订单数", "cohort_order_count", "paid_order_count", "paid_orders"),
    "not_shipped_orders": ("未发货订单数",), "shipped_not_delivered_orders": ("在途订单数", "已发货未送达订单数"),
    "completed_cash_refund_gross_cents": ("已完成现金退款", "完成退款金额", "已完成退款", "cash_refund_gross_cents"),
    "recovered_cost_ex_vat_cents": ("可再售成本恢复", "成本恢复", "退货恢复成本"),
    "customer_contact_roots": ("投诉根", "投诉根数量", "咨询投诉根数量"),
    "review_events": ("评价数", "评价事件数"),
    "spend_ex_vat_cents": ("广告花费", "实际花费", "广告实耗"),
    "goods_roas_ex_vat": ("ROAS", "商品ROAS", "成交ROAS"),
    "summed_budget_caps_ex_vat_cents": ("预算", "预算上限合计", "总预算"),
    "paid_landing_sessions": ("付费落地会话数", "广告访问数", "落地会话数"),
    "attributed_goods_ex_vat_cents": ("归因商品成交额", "归因商品收入", "不含税归因商品金额"),
    "oos_intervals": ("缺货区间数", "不可售区间数"),
    "oos_hours": ("缺货小时", "累计缺货小时", "不可售小时数"),
    "longest_continuous_oos_hours": ("最长连续缺货小时",),
    "blocked_attempts": ("购买受阻次数", "库存阻断次数"),
    "billed_clicks_while_unavailable": ("缺货期间付费点击数",),
    "spend_while_unavailable_ex_vat_cents": ("缺货期间广告费",),
}


def normalized(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value).casefold())


def find_column(canonical, columns):
    desired = {normalized(canonical), *(normalized(name) for name in ALIASES.get(canonical, ()))}
    exact = [name for name in columns if normalized(name) == normalized(canonical)]
    if exact:
        return exact[0]
    matches = [name for name in columns if normalized(name) in desired]
    return matches[0] if len(matches) == 1 else None


def dimension(value, field, question):
    text = str(value).strip()
    if "month" in field:
        match = re.search(r"(2026)[年/-](\d{1,2})", text)
        return match.group(1) + "-" + match.group(2).zfill(2) if match else text
    if field == "label":
        small = text.casefold()
        if question == "q04":
            if any(term in small for term in ("before", "前", "4月13", "04-13")):
                return "before"
            if any(term in small for term in ("after", "后", "5月4", "05-04")):
                return "after"
        if question == "q05":
            if any(term in small for term in ("reference", "baseline", "基准", "对照期", "促销前", "5月25", "05-25")):
                return "reference"
            if any(term in small for term in ("comparison", "promotion", "促销期", "活动期", "6月8", "06-08")):
                return "comparison"
    return text


def number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    percent = text.endswith("%")
    try:
        result = Decimal(text.rstrip("%"))
        return result / 100 if percent else result
    except InvalidOperation:
        return None


def tolerance(field):
    if field.endswith("_cents") and not any(word in field for word in ("average", "aov", "realized_unit", "cpc")):
        return Decimal(0)
    return Decimal("0.000001")


def numeric_review(case, reference):
    ident = case["question_id"]
    expected = reference["rows"]
    if ident == "q09":
        expected = [row for row in expected if row["snapshot"] == case["snapshot"]]
    actual = (case.get("response") or {}).get("rows") or []
    if ident == "q10" and not actual:
        return {"status": "not_required_for_an_evidence_limited_refusal", "note": "Evidence boundary requires manual reading; no fabricated counterfactual number is expected."}
    if not actual:
        return {"status": "no_result_rows", "expected_rows": len(expected)}
    columns = list(actual[0])
    dimensions = {field: find_column(field, columns) for field in KEYS[ident]}
    if any(value is None for value in dimensions.values()):
        return {"status": "needs_manual_mapping", "missing_dimensions": [key for key, value in dimensions.items() if value is None],
                "actual_columns": columns, "expected_columns": reference["columns"]}
    expected_index = {tuple(dimension(row[field], field, ident) for field in KEYS[ident]): row for row in expected}
    actual_index = {}
    duplicate_keys = []
    for row in actual:
        key = tuple(dimension(row[column], field, ident) for field, column in dimensions.items())
        if key in actual_index:
            duplicate_keys.append(key)
        actual_index[key] = row
    required_keys = set(expected_index)
    if ident == "q06":
        required_keys = {key for key, row in expected_index.items() if row["oos_intervals"] > 0}
    missing_keys, extra_keys = required_keys - actual_index.keys(), actual_index.keys() - expected_index.keys()
    comparable = set(actual_index) & set(expected_index)
    fields = [field for field in reference["columns"] if field not in KEYS[ident] and field != "snapshot"
              and any(isinstance(row.get(field), (int, float)) or number(row.get(field)) is not None for row in expected)]
    mapped = {field: find_column(field, columns) for field in fields}
    mismatches = []
    matched = []
    canonical_actual = {}
    for key in comparable:
        canonical_actual[str(key)] = {field: actual_index[key].get(column) for field, column in mapped.items() if column}
    for field, column in mapped.items():
        if column is None:
            continue
        field_ok = True
        for key in comparable:
            wanted, obtained = expected_index[key].get(field), actual_index[key].get(column)
            same = wanted is None and obtained is None
            left, right = number(wanted), number(obtained)
            if left is not None and right is not None:
                same = abs(left - right) <= tolerance(field)
            if not same:
                field_ok = False
                mismatches.append({"key": key, "metric": field, "column": column, "expected": wanted, "actual": obtained})
        if field_ok and comparable:
            matched.append(field)
    missing_fields = sorted(REQUIRED[ident] - set(matched))
    status = ("reported_numeric_mismatch" if mismatches else
              "required_numeric_metrics_match" if not missing_fields and not missing_keys and not extra_keys and not duplicate_keys else
              "partial_or_needs_manual_review")
    return {
        "status": status, "expected_rows": len(expected), "actual_rows": len(actual), "compared_rows": len(comparable),
        "dimension_mapping": dimensions, "metric_mapping": {key: value for key, value in mapped.items() if value},
        "matched_metrics": matched, "missing_required_metrics": missing_fields,
        "missing_keys": sorted(missing_keys), "extra_keys": sorted(extra_keys), "duplicate_keys": duplicate_keys,
        "mismatch_count": len(mismatches), "mismatch_examples": mismatches[:40], "canonical_actual": canonical_actual,
        "limitation": "Only recognized output columns are automatically compared. Alternative layouts/aliases need manual review; value agreement alone does not prove metric semantics.",
    }


def assess(case, reference):
    response = case.get("response") or {}
    numeric = numeric_review(case, reference)
    trace = response.get("execution_log") or []
    successful_sql = [entry for entry in trace if entry.get("stage") == "execute_duckdb" and entry.get("success")]
    calls = response.get("tool_calls") or []
    sql_evidence = bool(successful_sql or any(call.get("success") and call.get("sql") for call in calls))
    analysis = str(response.get("analysis") or response.get("message") or "")
    if not case.get("selected_snapshot_verified"):
        overall = "snapshot_context_failed"
    elif case.get("status") != "executed_requires_independent_review":
        overall = case.get("status", "missing_execution")
    elif numeric["status"] == "reported_numeric_mismatch":
        overall = "numeric_failed"
    elif numeric["status"] == "required_numeric_metrics_match" and sql_evidence:
        overall = "numeric_pass_semantics_need_review"
    else:
        overall = "needs_manual_review"
    return {
        "case_id": case["case_id"], "question_id": case["question_id"], "snapshot": case["snapshot"],
        "verdict": overall, "agent_status": response.get("status"), "route": response.get("route"),
        "http_status": case.get("http_status"), "selected_snapshot_verified": case.get("selected_snapshot_verified"),
        "successful_sql_evidence": sql_evidence, "numeric": numeric,
        "business_semantics": {"status": "manual_review_required",
            "checks": ["correct event-time and snapshot", "grain and join duplication", "VAT/shipping/refund treatment",
                       "cohort maturity and operating opportunity", "facts versus causal/counterfactual claims"]},
        "evidence_boundary": {"automatic_keyword_signals_only": [term for term in ("无法", "不能证明", "不足", "反事实", "假设", "混杂", "估计")
                                                              if term in analysis],
                              "status": "manual_review_required", "answer": analysis},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, default=ROOT / "data/homenova_phase_d/private/evaluation_baseline")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    index = json.loads((args.baseline_dir / "index.json").read_text())
    specs = {question["id"]: question for question in index["questions"]}
    cases = []
    for path in sorted(args.run_dir.glob("q??_asof_*.json")):
        case = json.loads(path.read_text())
        if "question_id" in case:
            cases.append(case)
    evaluated = []
    for case in cases:
        reference = json.loads((args.baseline_dir / "results" / (case["question_id"] + ".json")).read_text())
        review = assess(case, reference)
        review["passing_conditions"] = specs[case["question_id"]]["passing_conditions"]
        review["uncertainty_boundary"] = specs[case["question_id"]]["uncertainty_boundary"]
        evaluated.append(review)
    q09 = [case for case in cases if case["question_id"] == "q09"]
    pair = {
        "cases": [case["case_id"] for case in q09],
        "both_snapshots_present": len(q09) == 2 and len({case["snapshot"] for case in q09}) == 2,
        "independent_fresh_sessions": len(q09) == 2 and len({case["session_id"] for case in q09}) == 2,
        "identical_question_bytes": len(q09) == 2 and len({case["prompt_sha256"] for case in q09}) == 1,
        "comparison_location": "offline evaluator; neither Agent session received the other snapshot or private reference",
        "snapshot_numeric_checks": {row["snapshot"]: row["numeric"] for row in evaluated if row["question_id"] == "q09"},
    }
    result = {
        "network_or_model_calls": False, "private_reference_loaded_only_offline": True,
        "case_count": len(evaluated), "question_count": len({case["question_id"] for case in cases}),
        "counts": dict(Counter(row["verdict"] for row in evaluated)), "cases": evaluated, "q09_external_comparison": pair,
        "full_business_passes_automatically_claimed": 0,
        "policy": "Transport/execution, numbers, and business evidence are separate. No keyword-only causality/refusal pass; aliases not comparable are explicitly deferred.",
    }
    output = args.output or args.run_dir / "offline_evaluation.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"cases": len(evaluated), "verdict_counts": result["counts"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
