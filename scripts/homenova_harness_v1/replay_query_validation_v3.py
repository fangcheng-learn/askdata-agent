"""Replay saved query candidates locally, without a model, router, or network.

Run from the repository root with:
references/askdata_studio/backend/.venv/bin/python \
    scripts/homenova_harness_v1/replay_query_validation_v3.py

Only the two saved candidate envelopes, public schema/semantics, selected public
snapshot, and the local validator/engine are used. Independent CSV arithmetic is
development evaluation evidence and is never injected into runtime inputs.
"""
from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "references/askdata_studio/backend"
EVALUATION = ROOT / "data/homenova_harness_v1/private/query_validation_v3"
SAVED = ROOT / "data/homenova_harness_v1/private/implementation_v2"
SOURCE = "homenova_20260831"
NETWORK_ATTEMPTS: list[str] = []
sys.dont_write_bytecode = True
sys.path.insert(0, str(BACKEND))


def deny_external_operations(event: str, arguments: tuple) -> None:
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "subprocess.Popen"}:
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError("Offline evaluation forbids network and subprocess operations")


sys.addaudithook(deny_external_operations)

from app.homenova import schema_payload, semantic_folder, snapshot_folder  # noqa: E402
from app.querying.duckdb_engine import DuckDbEngine  # noqa: E402
from app.querying.query_contract import QueryContractValidator  # noqa: E402
from app.security import AccessController  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def file_fingerprints(paths: list[Path]) -> dict[str, str]:
    return {relative(path): sha256(path) for path in paths}


def candidates(envelope: dict) -> list[tuple[str, dict]]:
    """Read candidate audit entries, never old analysis, reports, or result rows."""
    result = []
    for index, event in enumerate(envelope["response"]["execution_log"]):
        attempts = event.get("validation_attempts")
        location = f"response.execution_log.{index}.validation_attempts"
        if attempts is None:
            attempts = event.get("context", {}).get("validation_attempts", [])
            location = f"response.execution_log.{index}.context.validation_attempts"
        for position, attempt in enumerate(attempts):
            if "sql" in attempt and "query_contract" in attempt:
                result.append((f"{location}.{position}", attempt))
    if not result:
        raise ValueError("Saved case contains no query candidates")
    return result


def independent_csv_check(folder: Path) -> dict:
    """Calculate paid merchandise metrics independently with standard library CSV."""
    berlin = ZoneInfo("Europe/Berlin")
    items_by_order: dict[str, dict] = {}
    item_ids: set[str] = set()
    item_rows = 0
    with (folder / "order_items.csv").open(encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            item_rows += 1
            item_id = item["order_item_id"]
            if item_id in item_ids:
                raise ValueError(f"Independent check found duplicate item key: {item_id}")
            item_ids.add(item_id)
            aggregate = items_by_order.setdefault(item["order_id"], {"gmv_cents": 0, "paid_units": 0})
            aggregate["gmv_cents"] += int(item["line_paid_gross_cents"])
            aggregate["paid_units"] += int(item["quantity"])

    monthly: dict[str, dict] = {}
    seen_orders: set[str] = set()
    payment_ids: set[str] = set()
    payment_rows = 0
    captured_rows = 0
    capture_times = []
    with (folder / "payments.csv").open(encoding="utf-8", newline="") as handle:
        for payment in csv.DictReader(handle):
            payment_rows += 1
            if payment["payment_id"] in payment_ids:
                raise ValueError("Independent check found duplicate payment key")
            payment_ids.add(payment["payment_id"])
            if payment["status"] != "captured":
                continue
            captured_rows += 1
            order_id = payment["order_id"]
            if order_id in seen_orders:
                raise ValueError("Multiple successful captures for an order; no arbitrary deduplication allowed")
            seen_orders.add(order_id)
            if order_id not in items_by_order:
                raise ValueError("Successful capture has no matching merchandise rows")
            instant = datetime.fromisoformat(payment["captured_at"].replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("Capture time lacks timezone")
            local = instant.astimezone(berlin)
            capture_times.append(local)
            period = local.strftime("%Y-%m")
            aggregate = monthly.setdefault(period, {"gmv_cents": 0, "paid_orders": 0, "paid_units": 0})
            aggregate["gmv_cents"] += items_by_order[order_id]["gmv_cents"]
            aggregate["paid_units"] += items_by_order[order_id]["paid_units"]
            aggregate["paid_orders"] += 1

    rows = []
    for period in ("2026-06", "2026-07"):
        aggregate = monthly.get(period)
        if not aggregate or not aggregate["paid_orders"]:
            raise ValueError(f"No successful paid orders for requested comparison month {period}")
        aov = Decimal(aggregate["gmv_cents"]) / Decimal(aggregate["paid_orders"])
        rows.append({"month": period, **aggregate,
                     "gmv_eur": str(Decimal(aggregate["gmv_cents"]) / 100),
                     "aov_cents": float(aov), "aov_cents_decimal": str(aov),
                     "aov_eur_rounded": str((aov / 100).quantize(Decimal("0.01")))})
    first, second = rows
    changes = {key: {"absolute": second[key] - first[key],
                     "percent": (second[key] / first[key] - 1) * 100}
               for key in ("gmv_cents", "paid_orders", "paid_units", "aov_cents")}
    return {
        "method": "Python standard-library csv + integer cents; successful captures joined to merchandise by order_id",
        "source_files": [relative(folder / "payments.csv"), relative(folder / "order_items.csv")],
        "timezone": "Europe/Berlin", "refund_policy": "Historical paid GMV is not reduced by later refunds",
        "uniqueness": {"payment_rows": payment_rows, "order_item_rows": item_rows,
                       "successful_capture_rows": captured_rows,
                       "distinct_successfully_captured_orders": len(seen_orders),
                       "one_successful_capture_per_order": captured_rows == len(seen_orders),
                       "unique_payment_ids": True, "unique_order_item_ids": True,
                       "all_captured_orders_have_merchandise": True},
        "observed_successful_capture_range_local": {
            "min": min(capture_times).isoformat(), "max": max(capture_times).isoformat()},
        "rows": rows, "july_vs_june": changes,
        "2025_july": {"observed_successful_orders": monthly.get("2025-07", {}).get("paid_orders", 0),
                      "coverage_confirmed": False, "usable_yoy_evidence": False,
                      "reason": "No verified complete coverage for July 2025; a returned zero is not evidence of zero business activity"},
        "runtime_injection": False,
    }


def compare_rows(execution: dict, independent: dict) -> dict:
    if not execution["success"] or len(execution["rows"]) != 1:
        return {"matched": False, "error": "Expected a successful single-row GMV execution"}
    actual = execution["rows"][0]
    checks = []
    prefixes = {"gmv_cents": "gmv", "paid_orders": "order_count", "paid_units": "items", "aov_cents": "aov"}
    for row in independent["rows"]:
        suffix = row["month"].replace("-", "")
        for metric, prefix in prefixes.items():
            column = prefix + "_" + suffix
            expected = row[metric]
            observed = actual.get(column)
            equal = (isinstance(observed, (int, float)) and
                     (math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-8)
                      if metric == "aov_cents" else observed == expected))
            checks.append({"month": row["month"], "metric": metric, "sql_column": column,
                           "expected": expected, "actual": observed, "matched": equal})
    return {"matched": all(check["matched"] for check in checks), "checks": checks,
            "scope": "June and July 2026 only; no July 2025 year-over-year certification"}


def fresh_output(requested: Path | None) -> Path:
    if requested is not None:
        output = requested.resolve()
        if output.parent != EVALUATION.resolve():
            raise ValueError("Output must be a new file directly inside the development evaluation directory")
        if output.exists():
            raise FileExistsError("Refusing to overwrite an existing evaluation artifact")
        return output
    output = EVALUATION / "replay.json"
    index = 2
    while output.exists():
        output = EVALUATION / f"replay_{index}.json"
        index += 1
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = fresh_output(args.output)
    folder = snapshot_folder(SOURCE)
    public_semantics = semantic_folder(SOURCE)
    protected = [SAVED / "case_1.json", SAVED / "case_2.json", SAVED / "paid_runs.json",
                 *sorted(folder.glob("*")), *sorted(public_semantics.glob("*"))]
    protected = [path for path in protected if path.is_file()]
    code = [Path(__file__).resolve(), BACKEND / "app/querying/query_contract.py",
            BACKEND / "app/querying/duckdb_engine.py", BACKEND / "app/homenova.py",
            BACKEND / "app/security/access_control.py", BACKEND / "app/database.py"]
    protected_before = file_fingerprints(protected)
    code_before = file_fingerprints(code)
    graph = schema_payload(SOURCE)
    scope = AccessController().scoped("demo_admin", SOURCE)
    engine = DuckDbEngine()
    validator = QueryContractValidator(engine)
    executions: dict[str, dict] = {}
    cases = []
    execute_calls = 0
    for name in ("case_1", "case_2"):
        path = SAVED / f"{name}.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        question = envelope["request"]["query"]
        results = []
        for location, candidate in candidates(envelope):
            sql = candidate["sql"]
            database = candidate["database"]
            if database != SOURCE:
                raise ValueError("Saved candidate points outside the authorized snapshot")
            sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            item = {"attempt": candidate["attempt"], "saved_candidate_path": location,
                    "sql": sql, "sql_sha256": sql_hash,
                    "input_query_contract": copy.deepcopy(candidate["query_contract"]),
                    "validation_passed": False, "normalizations": None,
                    "pending_checks": None, "sql_summary": None}
            try:
                normalized = validator.validate(copy.deepcopy(candidate["query_contract"]), sql,
                                                question, database, graph, scope.public())
            except Exception as exc:
                item.update(exception={"type": type(exc).__name__, "message": str(exc)},
                            validation_metadata_note="Validator raised before returning its metadata; unavailable values remain null",
                            execution_skipped="Candidate failed validation; SQL and contract were not repaired")
            else:
                metadata = normalized["validation"]
                item.update(validation_passed=True, validated_query_contract=normalized,
                            normalizations=metadata["normalizations"], pending_checks=metadata["pending_checks"],
                            sql_summary=metadata["sql_summary"], execution_key=sql_hash,
                            execution_reused=sql_hash in executions)
                if sql_hash not in executions:
                    execute_calls += 1
                    executions[sql_hash] = asdict(engine.execute(database, sql, scope))
            results.append(item)
        cases.append({"id": name, "input_path": relative(path), "original_question": question,
                      "original_question_binding": "Saved request.query, not model-declared rewritten question",
                      "candidate_count": len(results), "validation_passed": sum(item["validation_passed"] for item in results),
                      "unique_candidate_sql_count": len({item["sql_sha256"] for item in results}),
                      "candidates": results})

    gmv_keys = {item["execution_key"] for item in cases[0]["candidates"] if item.get("validation_passed")}
    independent = None
    if gmv_keys:
        independent = independent_csv_check(folder)
        independent["sql_comparisons"] = {key: compare_rows(executions[key], independent) for key in sorted(gmv_keys)}
    protected_after = file_fingerprints(protected)
    code_after = file_fingerprints(code)
    if protected_before != protected_after:
        raise RuntimeError("Protected saved artifacts or public inputs changed during replay")
    if code_before != code_after:
        raise RuntimeError("Validator, engine, or replay source changed during replay; rerun against stable code")
    prohibited_modules = [name for name in sys.modules if name.startswith(("app.agents", "app.llm", "app.retrieval", "app.services"))]
    payload = {
        "format": "query_validation_v3_offline_replay_v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "Development-only offline validation and read-only local DuckDB replay",
        "datasource_id": SOURCE, "schema_snapshot_id": graph["snapshot_id"],
        "access_scope": scope.public(), "source_code_sha256": code_before,
        "restrictions": {"network_guard_installed": True, "blocked_network_or_subprocess_attempts": NETWORK_ATTEMPTS,
                         "new_paid_model_or_agent_runs": 0, "embedding_calls": 0, "rerank_calls": 0,
                         "runtime_route_or_agent_used": False, "prohibited_runtime_modules_imported": prohibited_modules,
                         "answer_reference_sql_or_generator_loaded": False,
                         "input_candidate_sql_modified": False, "existing_artifacts_overwritten": False,
                         "local_execute_calls": execute_calls, "same_sql_execution_reused": True},
        "protected_input_sha256": protected_before, "protected_inputs_unchanged": True,
        "source_code_stable_during_replay": True, "cases": cases, "executions": executions,
        "independent_public_csv_check": independent,
        "limitations": ["Structure and compilation passing do not certify business correctness",
                        "Pending checks remain in the returned contract; independent arithmetic is development evidence only",
                        "July 2025 coverage is unconfirmed; a SQL zero cannot support year-over-year analysis",
                        "No new live model, Agent, HTTP, embedding, or rerank verification was performed"],
    }
    if NETWORK_ATTEMPTS or prohibited_modules:
        raise RuntimeError("Offline isolation check failed")
    EVALUATION.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": relative(output), "validation": [
        {"case": case["id"], "passed": case["validation_passed"], "candidates": case["candidate_count"]} for case in cases],
        "local_execute_calls": execute_calls, "independent_months_match": all(
            result["matched"] for result in independent["sql_comparisons"].values()) if independent else None,
        "protected_inputs_unchanged": True, "new_paid_model_or_agent_runs": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
