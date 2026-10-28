#!/usr/bin/env python3
"""Call the real AskData JSON API with natural questions, never private answers.

This process does not load D reference queries/results. Offline comparison is a
separate evaluate.py process. Credentials are read from the existing local auth
definition into memory; passwords and tokens are never written to run evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
AUTH_FILE = ROOT / "references/askdata_studio/backend/app/security/auth.py"
SOURCES = {
    "asof_2026_08_31": ("homenova_20260831", "2026-08-31T22:00:00Z"),
    "asof_2026_10_15": ("homenova_20261015", "2026-10-15T22:00:00Z"),
}
TABLES = {"sku_catalog", "price_intervals", "sessions", "orders", "order_items", "payments",
          "procurements", "inventory_movements", "shipments", "refunds", "campaigns",
          "ad_daily", "visit_events", "competitor_prices", "customer_feedback"}
# Natural questions only. These literals contain no expected amounts or SQL.
QUESTIONS = {
    "q01": "截至8月底，3—8月每月成交、净销售和商品毛利分别怎样？",
    "q02": "按类目和SKU比较销量、销售额、毛利率，主销商品是否也最赚钱？",
    "q03": "比较3月和4月，商品浏览转化与本店、竞品价格怎样变化？能证明竞品降价造成变化吗？",
    "q04": "比较4月13日—5月3日和5月4日—24日，各付费渠道预算、花费、访问与成交回报有什么差别？",
    "q05": "比较5月25日—6月7日与6月8日—21日，价格、件数和商品利润各变了多少？",
    "q06": "7月1日至8月14日哪些SKU不可售、多久？是否一段连续缺货？期间仍有广告费或购买受阻吗？",
    "q07": "调查7月20—27日发货：按承运商和邮区看延迟、咨询投诉、评价、退款及对应商品金额。",
    "q08": "8月完成的退款对应哪些原成交月份？现金退回、商品收入冲减与成本恢复如何区分？",
    "q09": "锁定2026年8月31日当地结束前已成功支付且支付月在7—8月的固定订单队列，按原支付月分别统计本数据截面可见的未发货、在途、发货商品收入与成本、已完成退款、待退款或待验收、可再售成本恢复、净销售、商品毛利、投诉根和评价，并说明未成熟结果。只使用当前截面可见事实。",
    "q10": "请精确算出缺货真正流失的订单、被竞品抢走的客户，以及促销创造的增量利润。数据能证明吗？",
}
FORMAT = "请给出可核对的表格或证据；金额用整数欧分、比例用0到1表示，区分观察事实与推测。"


def make_plan(only=None):
    result = []
    for ident, question in QUESTIONS.items():
        if only and ident not in only:
            continue
        snapshots = list(SOURCES) if ident == "q09" else ["asof_2026_08_31"]
        for snapshot in snapshots:
            datasource, cutoff = SOURCES[snapshot]
            prompt = question + FORMAT
            if len(prompt) > 500:
                raise ValueError("Natural question exceeds the API's 500-character contract")
            result.append({
                "case_id": ident + "_" + snapshot, "question_id": ident, "snapshot": snapshot,
                "datasource_id": datasource, "expected_cutoff": cutoff, "question": question,
                "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "session_id": "homenova-e-" + uuid.uuid4().hex,
            })
    return result


def credential(username):
    spec = importlib.util.spec_from_file_location("homenova_e_local_auth_definition", AUTH_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    account = module.AuthService.ACCOUNTS.get(username)
    if not account:
        raise ValueError("Selected local authentication account does not exist")
    return str(account["password"])


def clean(value, secrets=()):
    if isinstance(value, dict):
        return {key: clean(child, secrets) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.~-]{12,}", "Bearer [REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", value)
    return value


def save(path, value, secrets=()):
    path.write_text(json.dumps(clean(value, secrets), ensure_ascii=False, indent=2) + "\n")


def request(base, path, *, token=None, payload=None, timeout=15):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw, status = response.read().decode(), response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(errors="replace"), exc.code
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"non_json_response": raw[:16000]}
    return status, body


def classify(http_status, response):
    if http_status != 200:
        return "http_failure"
    if response.get("status") == "waiting_clarification":
        return "waiting_clarification"
    if response.get("status") == "failed":
        stages = [entry.get("stage", "") for entry in response.get("execution_log", []) if not entry.get("success", True)]
        return "agent_failed:" + ",".join(dict.fromkeys(stages)) if stages else "agent_failed"
    if response.get("status") == "completed":
        return "executed_requires_independent_review"
    return "unexpected_response"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--account", default="admin")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only", help="Optional comma-separated question IDs for a smoke run")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="Write the natural-question plan only; no auth/network/model call")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if urllib.parse.urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("This local acceptance runner requires a loopback AskData endpoint")
    selected = set(args.only.split(",")) if args.only else None
    if selected and not selected <= QUESTIONS.keys():
        parser.error("Unknown question ID")
    plan = make_plan(selected)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = args.output or ROOT / "data/homenova_phase_e/private/agent_runs" / stamp
    if output.exists() and any(output.iterdir()):
        parser.error("Use a new empty output directory to preserve earlier run evidence")
    output.mkdir(parents=True, exist_ok=True)
    save(output / "plan.json", {"transport": "AskData synchronous JSON API", "cases": plan,
         "answer_assets_loaded_by_runner": False, "dry_run": args.dry_run})
    if args.dry_run:
        print(json.dumps({"dry_run": True, "cases": len(plan), "output": str(output)}))
        return 0
    password = credential(args.account)
    status, login = request(base, "/api/auth/login", payload={"username": args.account, "password": password})
    if status != 200 or not login.get("access_token"):
        save(output / "preflight_failure.json", {"stage": "authentication", "http_status": status, "response": login}, (password,))
        raise RuntimeError("AskData authentication failed; sanitized evidence was saved")
    token = login["access_token"]
    secrets = (password, token)
    preflight = {"user": login.get("user"), "base_url": base}
    try:
        health_status, health = request(base, "/api/health", token=token)
        sources_status, sources = request(base, "/api/datasources", token=token)
        preflight.update(health={"status": health_status, "body": health},
                         datasources={"status": sources_status, "body": sources}, schemas={})
        if sources_status != 200 or not isinstance(sources, list):
            raise RuntimeError("Selected-datasource API is not ready")
        visible = {entry.get("id"): entry for entry in sources}
        for snapshot in dict.fromkeys(case["snapshot"] for case in plan):
            datasource, cutoff = SOURCES[snapshot]
            if datasource not in visible or visible[datasource].get("scenario", {}).get("observation_cutoff_at") != cutoff:
                raise RuntimeError("Datasource scenario/cutoff mismatch: " + datasource)
            schema_status, schema = request(base, "/api/schema?datasource_id=" + urllib.parse.quote(datasource), token=token)
            preflight["schemas"][datasource] = {"status": schema_status, "body": schema}
            names = {entry.get("name") or str(entry.get("id", "")).rsplit(".", 1)[-1] for entry in schema} if isinstance(schema, list) else set()
            if schema_status != 200 or names != TABLES or any(entry.get("database") != datasource for entry in schema):
                raise RuntimeError("Selected datasource must expose exactly its fifteen tables: " + datasource)
        save(output / "preflight.json", preflight, secrets)
        summary = {"cases_planned": len(plan), "results": [], "output": str(output)}
        for case in plan:
            print(case["case_id"] + " started", flush=True)
            payload = {"query": case["prompt"], "session_id": case["session_id"],
                       "workspace": {"datasource_id": case["datasource_id"]}}
            record = {**case, "request": payload}
            start = time.perf_counter()
            try:
                http_status, response = request(base, "/api/query", token=token, payload=payload, timeout=args.timeout)
                record.update(http_status=http_status, response=response, status=classify(http_status, response))
                scenario = response.get("scenario") or {}
                selected_ok = (response.get("datasource_id") == case["datasource_id"]
                               and scenario.get("observation_cutoff_at") == case["expected_cutoff"])
                query_databases = sorted({call["database"] for call in response.get("tool_calls", [])
                                          if call.get("database")})
                record["query_databases"] = query_databases
                selected_ok = selected_ok and all(database == case["datasource_id"] for database in query_databases)
                record["selected_snapshot_verified"] = selected_ok
                if not selected_ok:
                    record["status"] = "snapshot_context_mismatch"
            except Exception as exc:
                record.update(status="transport_error", error=type(exc).__name__ + ": " + str(exc))
            record["elapsed_seconds"] = round(time.perf_counter() - start, 3)
            save(output / (case["case_id"] + ".json"), record, secrets)
            summary["results"].append({key: record.get(key) for key in
                ("case_id", "question_id", "snapshot", "session_id", "status", "http_status", "elapsed_seconds", "selected_snapshot_verified")})
            save(output / "run_summary.json", summary, secrets)
            print(case["case_id"] + ": " + record["status"], flush=True)
        return 0
    except Exception as exc:
        save(output / "preflight_failure.json", {"error": type(exc).__name__ + ": " + str(exc), "preflight": preflight}, secrets)
        raise
    finally:
        try:
            request(base, "/api/auth/logout", token=token, payload={})
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
