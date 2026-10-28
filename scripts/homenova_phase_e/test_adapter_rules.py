"""Small real-Schema/real-DuckDB tests, distinct from live Agent acceptance."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "references/askdata_studio/backend"))
from app import homenova
from app.database import SCHEMA
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


class AdapterRules(unittest.TestCase):
    def setUp(self):
        self.database = "homenova_20260831"
        self.validator = QueryContractValidator(DuckDbEngine())
        self.scope = AccessController().scoped("demo_admin", self.database)
        self.graph = {"tables": [table for table in SCHEMA if table.get("database") == self.database]}
        metric = homenova.registered_metric(self.database, "成功支付订单量")
        self.question = "按明确边界统计支付订单"
        self.contract = {
            "original_question": self.question, "data_source": self.database,
            "tables": ["orders", "payments"],
            "metrics": [{"name": metric["label"], "definition": metric["definition"],
                         "source_fields": ["payments.order_id"], "aggregation": "count_distinct"}],
            "dimensions": [], "filters": [{"field": "payments.status", "operator": "=", "value": "captured"}],
            "time_range": {"field": "payments.captured_at", "start": "2026-08-01", "end": "2026-09-01"},
            "grain": "overall", "assumptions": [], "open_questions": [],
        }
        self.sql = ("SELECT COUNT(DISTINCT p.order_id) AS paid_orders FROM orders o JOIN payments p "
                    "ON p.order_id=o.order_id AND p.status='captured' "
                    "WHERE p.captured_at>=DATE '2026-08-01' AND p.captured_at<DATE '2026-09-01'")

    def validate(self, contract=None, sql=None):
        return self.validator.validate(contract or self.contract, sql or self.sql, self.question,
                                       self.database, self.graph, self.scope.public())

    def test_registered_count_accepts_filter_in_join_on(self):
        result = self.validate()
        self.assertTrue(result["validation"]["sql_compiled"])
        self.assertFalse(result["validation"]["business_definition_verified"])

    def test_wrong_join_filter_value_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(sql=self.sql.replace("='captured'", "='failed'"))

    def test_result_alias_cannot_be_declared_as_physical_dimension(self):
        contract = copy.deepcopy(self.contract)
        contract.update(dimensions=["month_start"], grain="month")
        with self.assertRaises(ValueError):
            self.validate(contract=contract)

    def test_primary_key_date_keeps_time_role(self):
        table = next(table for table in SCHEMA if table.get("database") == self.database
                     and table.get("name") == "ad_daily")
        self.assertIn("stat_local_date", table["primary_key"])
        field = next(field for field in table["fields"] if field["name"] == "stat_local_date")
        self.assertEqual(field["role"], "time")


if __name__ == "__main__":
    unittest.main(verbosity=2)
