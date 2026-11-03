"""Fixed, offline ReAct V1 SQL probes against the authorized public snapshot.

No model/API request, private answer, or data mutation is used. Accepted SQL
passes the contract validator and EXPLAIN, then the real in-process MCP tool.
Independent arithmetic reads public CSV with Python's standard library.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "data/homenova_harness_v1/private/react_v1_20261029/sql_probes"
DATABASE = "homenova_20260831"
PUBLIC = ROOT / "data/homenova_phase_d/public/asof_2026_08_31"
SEMANTICS = ROOT / "data/homenova_phase_e/public_semantics/asof_2026_08_31"
sys.path.insert(0, str(ROOT / "references/askdata_studio/backend"))

from app.homenova import schema_payload
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController, AccessScope


START = "2026-08-01T00:00:00+02:00"
END = "2026-09-01T00:00:00+02:00"
JULY = "2026-07-01T00:00:00+02:00"
JOIN = "FROM payments p JOIN order_items oi ON p.order_id=oi.order_id"
WHERE = f"p.status='captured' AND p.captured_at>='{START}' AND p.captured_at<'{END}'"


def metric(name="paid_goods_gmv_gross", fields=None, aggregation="sum"):
    return {"name": name, "source_fields": fields or ["order_items.line_paid_gross_cents"],
            "aggregation": aggregation}


def contract(*, metrics=None, dimensions=None, tables=None, grain="overall", start=START,
             end=END, end_inclusive=False, filters=None):
    return {"original_question": "核对2026年8月成功支付商品GMV及相关经营指标",
            "data_source": DATABASE, "tables": tables or ["payments", "order_items"],
            "metrics": metrics if metrics is not None else [metric()],
            "dimensions": dimensions or [], "grain": grain,
            "filters": filters if filters is not None else [
                {"field": "payments.status", "operator": "=", "value": "captured"}],
            "time_range": {"field": "payments.captured_at", "start": start,
                           "end": end, "end_inclusive": end_inclusive},
            "assumptions": [], "open_questions": []}


def probes():
    """The expected scope is fixed before execution, including negative cases."""
    cases = []

    def add(name, sql, group=None, spec=None, negative=False, limited_scope=False, reason=""):
        cases.append({"name": name, "sql": sql, "contract": spec or contract(),
                      "expected": "rejected" if negative else "accepted",
                      "independent_group": group, "limited_scope": limited_scope, "reason": reason})

    direct = f"SELECT SUM(oi.line_paid_gross_cents) AS gmv_cents {JOIN} WHERE {WHERE}"
    add("aug_gmv_direct", direct, "aug_gmv")
    add("aug_gmv_chinese_alias", direct.replace("AS gmv_cents", 'AS "八月商品GMV"'), "aug_gmv",
        contract(metrics=[metric("商品GMV")]), reason="显示别名与登记同义名称不改变来源")
    add("aug_gmv_cte_column_alias", f"WITH paid_amount(v) AS (SELECT oi.line_paid_gross_cents {JOIN} WHERE {WHERE}) SELECT SUM(v) AS gmv_cents FROM paid_amount", "aug_gmv")
    add("aug_gmv_nested_cte", f"WITH paid AS (SELECT oi.line_paid_gross_cents AS amount {JOIN} WHERE {WHERE}), carried AS (SELECT * FROM paid) SELECT SUM(amount) AS total FROM carried", "aug_gmv")
    add("aug_gmv_single_value_in", direct.replace("p.status='captured'", "p.status IN ('captured')"), "aug_gmv")
    add("aug_gmv_reversed_boundary", f"SELECT SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE p.status='captured' AND '{START}'<=p.captured_at AND '{END}'>p.captured_at", "aug_gmv")
    add("aug_gmv_utc_boundary", direct.replace(START, "2026-07-31T22:00:00Z").replace(END, "2026-08-31T22:00:00Z"), "aug_gmv")
    add("aug_gmv_closed_business_date", f"SELECT SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE p.status='captured' AND CAST(p.captured_at AS DATE) BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'", "aug_gmv",
        contract(start="2026-08-01", end="2026-08-31", end_inclusive=True))

    monthly_spec = contract(start=JULY, dimensions=["payments.captured_at"], grain="month")
    two_months = WHERE.replace(START, JULY)
    add("two_months_group", f"SELECT strftime(p.captured_at,'%Y-%m') AS month, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {two_months} GROUP BY 1 ORDER BY 1", "monthly", monthly_spec)
    add("two_months_union", f"SELECT '2026-07' AS month, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {two_months} AND p.captured_at<'{START}' UNION ALL SELECT '2026-08' AS month, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {two_months} AND p.captured_at>='{START}' ORDER BY 1", "monthly", monthly_spec)
    add("two_months_sum_case", f"SELECT SUM(CASE WHEN p.captured_at<'{START}' THEN oi.line_paid_gross_cents ELSE 0 END) AS july, SUM(CASE WHEN p.captured_at>='{START}' THEN oi.line_paid_gross_cents ELSE 0 END) AS august {JOIN} WHERE {two_months}", "pivot", contract(start=JULY))
    add("two_months_sum_filter", f"SELECT SUM(oi.line_paid_gross_cents) FILTER (WHERE p.captured_at<'{START}') AS july, SUM(oi.line_paid_gross_cents) FILTER (WHERE p.captured_at>='{START}') AS august {JOIN} WHERE {two_months}", "pivot", contract(start=JULY))

    orders = contract(tables=["payments"], metrics=[metric("paid_order_count", ["payments.order_id"], "count_distinct")])
    paid_where = WHERE.replace("p.", "")
    add("aug_paid_orders_distinct", f"SELECT COUNT(DISTINCT order_id) AS paid_orders FROM payments WHERE {paid_where}", "orders", orders)
    add("aug_paid_orders_upstream_distinct", f"WITH paid(order_key) AS (SELECT DISTINCT order_id FROM payments WHERE {paid_where}) SELECT COUNT(*) AS paid_orders FROM paid", "orders", orders)

    measures = [metric(), metric("quantity", ["order_items.quantity"]),
                metric("paid_order_count", ["payments.order_id"], "count_distinct")]
    projection = "SUM(oi.line_paid_gross_cents) AS gmv_cents, SUM(oi.quantity) AS units, COUNT(DISTINCT p.order_id) AS paid_orders"
    store_join = JOIN + " JOIN sku_catalog sk ON sk.sku_id=oi.sku_id"
    store_sql = f"SELECT sk.store_id, {projection} {store_join} WHERE {WHERE} GROUP BY 1 ORDER BY 1"
    store_contract = contract(metrics=measures, dimensions=["sku_catalog.store_id"],
                              tables=["payments", "order_items", "sku_catalog"], grain="dimension")
    add("aug_store_multi_metrics", store_sql, "store", store_contract,
        reason="公开快照只有HN-DE-01一家店，不把单店结果包装成多店贡献分析")
    sku_sql = f"SELECT oi.sku_id, {projection} {JOIN} WHERE {WHERE} GROUP BY 1 ORDER BY 1"
    sku_contract = contract(metrics=measures, dimensions=["order_items.sku_id"], grain="dimension")
    add("aug_sku_multi_metrics", sku_sql, "sku", sku_contract)
    add("aug_sku_renamed_cte", f"WITH totals(k, amount, qty, paid) AS ({sku_sql}) SELECT k AS product, amount AS sales, qty AS quantity, paid AS orders FROM totals ORDER BY 1", "sku", sku_contract)

    aov_spec = contract(metrics=[metric(), metric("paid_order_count", ["payments.order_id"], "count_distinct"),
                                 metric("goods_aov_gross", ["order_items.line_paid_gross_cents", "payments.order_id"], "derived")])
    add("aug_gmv_orders_aov", f"SELECT SUM(oi.line_paid_gross_cents) AS gmv_cents, COUNT(DISTINCT p.order_id) AS paid_orders, SUM(oi.line_paid_gross_cents)::DOUBLE/NULLIF(COUNT(DISTINCT p.order_id),0) AS aov_cents {JOIN} WHERE {WHERE}", "aov", aov_spec,
        reason="同一支付订单集合的GMV/订单数，不使用购买人数代替订单数")

    session_spec = contract(tables=["sessions"], metrics=[metric("sessions_count", ["sessions.session_id"], "count_distinct")],
                            filters=[{"field": "sessions.is_internal_traffic", "operator": "=", "value": False}])
    session_spec["time_range"]["field"] = "sessions.started_at"
    add("aug_valid_sessions", f"SELECT COUNT(DISTINCT session_id) AS sessions FROM sessions WHERE is_internal_traffic=false AND started_at>='{START}' AND started_at<'{END}'", "sessions", session_spec)

    detail_spec = contract(metrics=[metric("line_paid_gross_cents", ["order_items.line_paid_gross_cents"], "none"),
                                    metric("quantity", ["order_items.quantity"], "none")],
                           dimensions=["order_items.order_item_id", "order_items.sku_id"], grain="detail")
    add("aug_detail_preview_and_reference", f"SELECT oi.order_item_id,oi.sku_id,oi.line_paid_gross_cents,oi.quantity {JOIN} WHERE {WHERE} ORDER BY oi.order_item_id", "detail", detail_spec,
        reason="预览200行与授权完整结果引用分别检查，不把预览当全集")

    add("negative_wrong_registered_source", direct.replace("oi.line_paid_gross_cents", "oi.quantity"),
        spec=contract(metrics=[metric(fields=["order_items.quantity"])]), negative=True)
    add("negative_wrong_aggregation", direct.replace("SUM(", "AVG("), negative=True)
    add("negative_missing_capture_filter", direct.replace("p.status='captured' AND ", ""), negative=True)
    add("negative_missing_start", direct.replace(f"p.captured_at>='{START}' AND ", ""), negative=True)
    add("negative_wrong_start_hour", direct.replace(START, "2026-08-01T01:00:00+02:00"), negative=True)
    add("negative_inclusive_timestamp_end", direct.replace(f"p.captured_at<'{END}'", f"p.captured_at<='{END}'"), negative=True)
    add("negative_cross_snapshot", direct.replace("FROM payments", "FROM homenova_20261015.payments"), negative=True)
    add("negative_write", "DELETE FROM payments", negative=True)
    add("negative_multiple_statements", direct + "; SELECT 1 AS injected", negative=True)
    add("negative_unauthorized_table", direct, negative=True, limited_scope=True)
    return cases


def independent_csv():
    """Compute every positive expectation without running or parsing SQL."""
    def read(name):
        with (PUBLIC / f"{name}.csv").open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    catalog = {row["sku_id"]: row for row in read("sku_catalog")}
    items = defaultdict(list)
    for row in read("order_items"):
        items[row["order_id"]].append(row)
    aggregates = defaultdict(lambda: {"amount": 0, "units": 0, "orders": set()})
    detail = []
    paid_seen = set()
    for payment in read("payments"):
        if payment["status"] != "captured":
            continue
        if payment["order_id"] in paid_seen:
            raise ValueError("独立核数发现重复成功capture，必须先确定去重口径")
        paid_seen.add(payment["order_id"])
        month = datetime.fromisoformat(payment["captured_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")
        if month not in {"2026-07", "2026-08"}:
            continue
        for item in items[payment["order_id"]]:
            amount, quantity = int(item["line_paid_gross_cents"]), int(item["quantity"])
            dimensions = [("month", month)]
            if month == "2026-08":
                dimensions += [("sku", item["sku_id"]), ("store", catalog[item["sku_id"]]["store_id"])]
                detail.append([item["order_item_id"], item["sku_id"], amount, quantity])
            for key in dimensions:
                target = aggregates[key]
                target["amount"] += amount
                target["units"] += quantity
                target["orders"].add(payment["order_id"])
    july, august = aggregates[("month", "2026-07")], aggregates[("month", "2026-08")]
    orders = len(august["orders"])
    expectations = {
        "aug_gmv": [[august["amount"]]], "orders": [[orders]],
        "monthly": [["2026-07", july["amount"]], ["2026-08", august["amount"]]],
        "pivot": [[july["amount"], august["amount"]]],
        "aov": [[august["amount"], orders, august["amount"] / orders if orders else None]],
        "detail": sorted(detail),
    }
    for dimension in ("sku", "store"):
        expectations[dimension] = [[key, value["amount"], value["units"], len(value["orders"])]
                                   for (kind, key), value in sorted(aggregates.items()) if kind == dimension]
    session_ids = set()
    for session in read("sessions"):
        if str(session["is_internal_traffic"]).lower() not in {"false", "0"}:
            continue
        day = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        if "2026-08-01" <= day < "2026-09-01":
            session_ids.add(session["session_id"])
    expectations["sessions"] = [[len(session_ids)]]
    return expectations


def equal_values(rows, columns, expected):
    actual = sorted([[row[column] for column in columns] for row in rows], key=lambda x: json.dumps(x, ensure_ascii=False))
    expected = sorted(expected, key=lambda x: json.dumps(x, ensure_ascii=False))
    if len(actual) != len(expected):
        return False
    for a, b in zip(actual, expected):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if not math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-7):
                    return False
            elif x != y:
                return False
    return True


def fingerprint(paths):
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def save_new(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def network_guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "subprocess.Popen"}:
        raise RuntimeError("离线SQL探针禁止网络与外部进程")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("baseline.json"))
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else DIRECTORY / args.output
    output = output.resolve()
    if DIRECTORY.resolve() not in output.parents:
        parser.error("输出只能保存到新的react_v1_20261029/sql_probes目录")
    cases = probes()
    plan_json = json.dumps(cases, ensure_ascii=False, sort_keys=True)
    plan_hash = hashlib.sha256(plan_json.encode()).hexdigest()
    plan = {"datasource_id": DATABASE, "cases_sha256": plan_hash, "probe_count": len(cases),
            "fixed_before_execution": True, "cases": cases,
            "criteria": ["所有正例校验、预编译、真实MCP执行及独立CSV核数通过",
                         "所有负例拒绝且不执行；不能通过关闭权限或绕过契约凑成功",
                         "200行预览与完整结果引用分开，原始数据和历史记录不修改"]}
    plan_path = DIRECTORY / "plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text())["cases_sha256"] != plan_hash:
            parser.error("既定探针范围发生变化；禁止根据失败静默缩小范围")
    else:
        save_new(plan_path, plan)
    if args.plan_only:
        print(json.dumps({"plan": str(plan_path), "probe_count": len(cases), "cases_sha256": plan_hash}, ensure_ascii=False))
        return
    if output.exists():
        parser.error("输出文件已存在，请使用新文件名，禁止覆盖历史")
    sys.addaudithook(network_guard)
    logging.getLogger().setLevel(logging.WARNING)
    sources = [PUBLIC / f"{name}.csv" for name in ("payments", "order_items", "sku_catalog", "sessions")]
    source_before = fingerprint(sources)
    guard_paths = [ROOT / "references/askdata_studio/backend/app/querying" / name
                   for name in ("query_contract.py", "duckdb_engine.py")]
    guards_before = fingerprint(guard_paths)
    expected = independent_csv()
    engine = DuckDbEngine()
    validator = QueryContractValidator(engine)
    scope = AccessController().scoped("demo_admin", DATABASE)
    limited = AccessScope(scope.user_id, scope.roles, scope.allowed_databases,
                          frozenset(value for value in scope.allowed_tables if value != "hn08_order_items"))
    graph = schema_payload(DATABASE)
    records = []
    for probe in cases:
        started = time.monotonic()
        record = copy.deepcopy(probe)
        actual_scope = limited if probe["limited_scope"] else scope
        budget = RunBudget("sql-probe-" + probe["name"])
        token = current_budget.set(budget)
        budget.start()
        try:
            try:
                checked = validator.validate(probe["contract"], probe["sql"], probe["contract"]["original_question"],
                                             DATABASE, graph, actual_scope.public())
            except (ValueError, KeyError, TypeError, PermissionError) as exc:
                record.update(validation_passed=False, validation_error=str(exc), executed=False,
                              preflight_error=engine.preflight(DATABASE, probe["sql"], actual_scope),
                              status="rejected_as_expected" if probe["expected"] == "rejected" else "unexpected_rejection")
            else:
                sql = checked["validation"]["sql_summary"]["normalized_sql"]
                record.update(validation_passed=True, validated_contract=checked,
                              pending_checks=checked["validation"]["pending_checks"],
                              preflight_error=engine.preflight(DATABASE, sql, actual_scope))
                if probe["expected"] == "rejected":
                    record.update(status="unexpected_acceptance", executed=False)
                elif record["preflight_error"]:
                    record.update(status="preflight_failed", executed=False)
                else:
                    client = LocalMcpClient(create_local_mcp_server(engine, actual_scope))
                    executed = client.call_tool(f"query_{DATABASE}", {"sql": sql})
                    record.update(executed=True, execution=executed, status="execution_failed")
                    if executed.get("success"):
                        rows = executed["rows"]
                        if executed.get("preview_truncated"):
                            rows, offset, pages = [], 0, 0
                            while True:
                                page = engine.read_result(executed["result_ref"], actual_scope.user_id,
                                                          budget.task_id, DATABASE, offset, 1000)
                                rows.extend(page["rows"])
                                pages += 1
                                if not page["has_more"]:
                                    break
                                offset += len(page["rows"])
                            record.update(full_result_rows=rows, authorized_read_pages=pages)
                        group = probe["independent_group"]
                        equal = equal_values(rows, executed["columns"], expected[group])
                        record.update(independent_equal=equal, actual_row_count=len(rows),
                                      expected_row_count=len(expected[group]),
                                      status="accepted_and_verified" if equal else "independent_mismatch")
        except Exception as exc:
            record.update(status="probe_error", error_type=type(exc).__name__, error=str(exc))
        finally:
            budget.pause()
            current_budget.reset(token)
            record["budget"] = budget.public()
        record["elapsed_seconds"] = round(time.monotonic() - started, 4)
        records.append(record)
    source_after = fingerprint(sources)
    guards_after = fingerprint(guard_paths)
    summary = {status: sum(case["status"] == status for case in records)
               for status in sorted({case["status"] for case in records})}
    payload = {"mode": "offline SQL/contract/real in-process MCP, not live Agent acceptance",
               "created_at_utc": datetime.now(timezone.utc).isoformat(), "datasource_id": DATABASE,
               "plan_sha256": plan_hash, "probe_count": len(records), "summary": summary,
               "model_calls": 0, "embedding_calls": 0, "rerank_calls": 0, "http_calls": 0,
               "network_guard_installed": True, "source_before": source_before, "source_after": source_after,
               "public_data_unchanged": source_before == source_after,
               "guard_before": guards_before, "guard_after": guards_after,
               "guard_changed_during_run": guards_before != guards_after,
               "independent_source": "Python csv/datetime/integer arithmetic, authorized public CSV only",
               "independent_values": expected, "cases": records}
    save_new(output, payload)
    print(json.dumps({"output": str(output), "probe_count": len(records), "summary": summary,
                      "model_calls": 0, "public_data_unchanged": payload["public_data_unchanged"]}, ensure_ascii=False))
    if any(case["status"] not in {"accepted_and_verified", "rejected_as_expected"} for case in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
