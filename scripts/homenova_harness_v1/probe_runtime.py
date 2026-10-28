#!/usr/bin/env python3
"""Exercise real API authorization and actual DuckDB filesystem boundaries."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "references/askdata_studio/backend"
PRIVATE = ROOT / "data/homenova_harness_v1/private"
EARLY, LATE = "homenova_20260831", "homenova_20261015"
sys.path.insert(0, str(BACKEND))


def request(base, path, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if payload is None else json.dumps(payload).encode()
    try:
        with urlopen(Request(base + path, data=body, headers=headers), timeout=30) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def login(base, username):
    from app.security.auth import AuthService
    account = AuthService.ACCOUNTS[username]
    status, payload = request(base, "/api/auth/login",
                              payload={"username": username, "password": account["password"]})
    if status != 200:
        raise RuntimeError("Existing authentication endpoint did not accept the existing account")
    return payload["access_token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    from app.database import SCHEMA, physical_table_name
    from app.querying.duckdb_engine import DuckDbEngine
    from app.security import AccessScope
    checks = []

    def check(name, actual, expected=True, evidence=None):
        checks.append({"name": name, "passed": actual == expected, "actual": actual,
                       "expected": expected, "evidence": evidence})

    base = args.base_url.rstrip("/")
    status, _ = request(base, "/api/datasources")
    check("datasource_metadata_requires_existing_login", status, 401)
    token = login(base, "admin")
    status, sources = request(base, "/api/datasources", token)
    check("datasources_registered", status == 200 and {EARLY, LATE, "askdata_mock", "ecommerce_ops"}
          <= {item["id"] for item in sources})
    for database in (EARLY, LATE):
        status, schema = request(base, "/api/schema?datasource_id=" + database, token)
        check(database + "_only_fifteen_current_tables", status == 200 and len(schema) == 15
              and {table["database"] for table in schema} == {database})
        status, tools = request(base, "/api/mcp/tools?datasource_id=" + database, token)
        tools = tools.get("tools", []) if isinstance(tools, dict) else tools
        query_tools = [tool["name"] for tool in tools if tool["name"].startswith("query_")]
        check(database + "_tool_single_database", status == 200 and query_tools == ["query_" + database],
              evidence=query_tools)
        status, memories = request(base, "/api/memories?datasource_id=" + database, token)
        check(database + "_separate_memory_endpoint", status == 200 and isinstance(memories, list))
    mock_token = login(base, "mock")
    status, _ = request(base, "/api/schema?datasource_id=" + EARLY, mock_token)
    check("old_mock_role_cannot_select_homenova", status, 403)
    status, _ = request(base, "/api/schema?datasource_id=not_registered", token)
    check("unknown_datasource_rejected", status in {400, 403, 404})

    tables = [table for table in SCHEMA if table.get("database") == EARLY]
    scope = AccessScope("demo_admin", ("admin",), frozenset({EARLY}),
                        frozenset(table["id"] for table in tables))
    check("schema_registration_matches_fifteen_table_whitelist", len(tables), 15)
    engine = DuckDbEngine()
    with (ROOT / "data/homenova_phase_d/public/asof_2026_08_31/orders.csv").open(newline="") as stream:
        expected_orders = sum(1 for _ in csv.DictReader(stream))
    result = engine.execute(EARLY, "SELECT COUNT(*) AS orders FROM orders", scope)
    check("actual_sql_can_query_selected_snapshot", result.success and result.rows == [{"orders": expected_orders}])
    result = engine.execute(LATE, "SELECT COUNT(*) AS orders FROM orders", scope)
    check("query_engine_blocks_other_snapshot", not result.success)
    sentinel = PRIVATE / "runtime_private_sentinel.txt"
    marker = "PRIVATE_E_BOUNDARY_" + uuid.uuid4().hex
    sentinel.write_text(marker)
    path = str(sentinel).replace("'", "''")
    for name, sql in [
        ("file_table_function", "SELECT content FROM read_text('" + path + "')"),
        ("file_scan_function", "SELECT column0 FROM read_csv('" + path + "')"),
        ("qualified_other_snapshot", "SELECT order_id FROM " + LATE + ".orders LIMIT 1"),
        ("machine_current_date", "SELECT CURRENT_DATE AS today"),
        ("machine_now", "SELECT now() AS clock"),
        ("multiple_statements", "SELECT 1 AS value; SELECT 2 AS value"),
    ]:
        result = engine.execute(EARLY, sql, scope)
        check("sql_rejects_" + name, not result.success, evidence=result.error)
        check("no_private_content_returned_" + name, marker not in json.dumps(result.rows))
    with engine.connect(EARLY) as connection:
        physical = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        check("physical_connection_only_current_fifteen_tables",
              physical == {physical_table_name(table) for table in tables})
        try:
            connection.execute("SELECT content FROM read_text('" + path + "')").fetchall()
            blocked = False
        except Exception:
            blocked = True
        check("duckdb_itself_disables_external_access_even_without_sql_guard", blocked)
        typed = connection.execute(
            "SELECT typeof(item_paid_gross_cents), typeof(ordered_at), typeof(destination_postal_prefix) FROM orders LIMIT 1"
        ).fetchone()
        check("integer_cents_and_utc_timestamp_and_postal_text",
              typed == ("BIGINT", "TIMESTAMP WITH TIME ZONE", "VARCHAR"), evidence=list(typed))
    sentinel.unlink()
    for folder in (ROOT / "data/homenova_phase_e/public_semantics").iterdir():
        if not folder.is_dir():
            continue
        text = "\n".join(path.read_text() for path in folder.glob("*.json"))
        forbidden = ["evaluation_baseline", "reference_answers", "true_lost_orders", "random_seed",
                     "recompute_checks", "q01.sql", "homenova_storyline_manual"]
        check(folder.name + "_no_private_answer_or_generator_markers",
              not any(marker in text for marker in forbidden))
    result = {"passed": all(item["passed"] for item in checks),
              "checks_passed": sum(item["passed"] for item in checks), "checks_total": len(checks),
              "checks": checks, "method": "Authenticated real HTTP routes and actual local DuckDB; no model stubs."}
    (PRIVATE / "runtime_boundary_checks.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: result[key] for key in ("passed", "checks_passed", "checks_total")}))
    for item in checks:
        if not item["passed"]:
            print(item["name"])
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
