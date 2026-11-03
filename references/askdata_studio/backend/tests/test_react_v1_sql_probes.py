"""Real local SQL/contract regressions; no model or remote endpoint is used."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from app.homenova import schema_payload
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController, AccessScope


SCRIPT = Path(__file__).resolve().parents[4] / "scripts/homenova_harness_v1/probe_react_v1_sql.py"
SPEC = importlib.util.spec_from_file_location("react_v1_sql_probe_cases", SCRIPT)
PROBES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBES)


class ReactV1SqlProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = "homenova_20260831"
        cls.engine = DuckDbEngine()
        cls.validator = QueryContractValidator(cls.engine)
        cls.graph = schema_payload(cls.database)
        cls.scope = AccessController().scoped("demo_admin", cls.database)

    def validate(self, probe):
        scope = self.scope
        if probe["limited_scope"]:
            scope = AccessScope(scope.user_id, scope.roles, scope.allowed_databases,
                                frozenset(x for x in scope.allowed_tables if x != "hn08_order_items"))
        return self.validator.validate(probe["contract"], probe["sql"],
                                       probe["contract"]["original_question"], self.database,
                                       self.graph, scope.public())

    def test_fixed_positive_sql_variants_pass_contract_and_compile(self):
        positives = [case for case in PROBES.probes() if case["expected"] == "accepted"]
        self.assertEqual(len(positives), 20)
        for probe in positives:
            with self.subTest(name=probe["name"]):
                checked = self.validate(probe)
                self.assertTrue(checked["validation"]["schema_checked"])
                self.assertTrue(checked["validation"]["sql_compiled"])
                self.assertFalse(checked["validation"]["business_definition_verified"])

    def test_fixed_negative_cases_reject_without_data_execution(self):
        negatives = [case for case in PROBES.probes() if case["expected"] == "rejected"]
        self.assertEqual(len(negatives), 10)
        before = set(self.engine.results)
        for probe in negatives:
            with self.subTest(name=probe["name"]), self.assertRaises((ValueError, PermissionError)):
                self.validate(probe)
        self.assertEqual(set(self.engine.results), before)

    def test_unproven_equivalent_expressions_are_pending_instead_of_rejected(self):
        names = {"aug_gmv_closed_business_date", "two_months_union", "aug_paid_orders_upstream_distinct"}
        for probe in PROBES.probes():
            if probe["name"] in names:
                with self.subTest(name=probe["name"]):
                    checked = self.validate(probe)
                    self.assertTrue(checked["validation"]["pending_checks"])

    def test_real_mcp_preview_is_not_the_complete_result(self):
        probe = next(case for case in PROBES.probes() if case["name"] == "aug_detail_preview_and_reference")
        checked = self.validate(probe)
        budget = RunBudget("react-sql-detail-test")
        token = current_budget.set(budget)
        budget.start()
        try:
            client = LocalMcpClient(create_local_mcp_server(self.engine, self.scope))
            result = client.call_tool("query_" + self.database, {
                "sql": checked["validation"]["sql_summary"]["normalized_sql"]})
            self.assertTrue(result["success"], result.get("error"))
            self.assertEqual(len(result["rows"]), 200)
            self.assertTrue(result["preview_truncated"])
            self.assertTrue(result["complete"])
            rows, offset = [], 0
            while True:
                page = self.engine.read_result(result["result_ref"], self.scope.user_id,
                                              budget.task_id, self.database, offset, 1000)
                rows.extend(page["rows"])
                if not page["has_more"]:
                    break
                offset += len(page["rows"])
            independent = PROBES.independent_csv()["detail"]
            self.assertGreater(len(rows), 1000)
            self.assertEqual(len(rows), result["total_rows"])
            self.assertTrue(PROBES.equal_values(rows, result["columns"], independent))
            with self.assertRaises(PermissionError):
                self.engine.read_result(result["result_ref"], "other-user", budget.task_id,
                                        self.database, 0, 1)
        finally:
            budget.pause()
            current_budget.reset(token)


if __name__ == "__main__":
    unittest.main()
