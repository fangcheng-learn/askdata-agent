"""Contract proof boundaries using public local CSVs; no model or HTTP calls."""
from __future__ import annotations

import copy
import math
import unittest

from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


class ContractBoundariesV6Test(unittest.TestCase):
    def setUp(self):
        self.database = "homenova_20260831"
        self.engine = DuckDbEngine()
        self.validator = QueryContractValidator(self.engine)
        self.graph = schema_payload(self.database)
        self.scope = AccessController().scoped("demo_admin", self.database)

    def quantity_contract(self):
        return {
            "original_question": "核对商品购买件数",
            "data_source": self.database,
            "tables": ["order_items"],
            "metrics": [{"name": "quantity", "source_fields": ["order_items.quantity"], "aggregation": "sum"}],
            "dimensions": [], "filters": [], "time_range": None,
            "grain": "overall", "assumptions": [], "open_questions": [],
        }

    def gmv_contract(self):
        return {
            "original_question": "核对2026年6月成功支付商品GMV",
            "data_source": self.database,
            "tables": ["payments", "order_items"],
            "metrics": [{"name": "paid_goods_gmv_gross", "source_fields": ["order_items.line_paid_gross_cents"], "aggregation": "sum"}],
            "dimensions": [],
            "filters": [{"field": "payments.status", "operator": "=", "value": "captured"}],
            "time_range": {"field": "payments.captured_at", "start": "2026-06-01", "end": "2026-07-01", "end_inclusive": False},
            "grain": "overall", "assumptions": [], "open_questions": [],
        }

    def validate(self, contract, sql):
        return self.validator.validate(copy.deepcopy(contract), sql, contract["original_question"],
                                       self.database, self.graph, self.scope.public())

    def assert_equivalent_pending(self, contract, variant, baseline):
        checked = self.validate(contract, variant)
        self.assertTrue(checked["validation"]["pending_checks"], "Unproven equivalence must stay visible to delivery review")
        actual = self.engine.execute(self.database, variant, self.scope)
        expected = self.engine.execute(self.database, baseline, self.scope)
        self.assertTrue(actual.success, actual.error)
        self.assertTrue(expected.success, expected.error)
        self.assertTrue(actual.complete)
        self.assertTrue(expected.complete)
        self.assertEqual(actual.columns, expected.columns)
        self.assertEqual(len(actual.rows), len(expected.rows))
        for actual_row, expected_row in zip(actual.rows, expected.rows):
            self.assertEqual(set(actual_row), set(expected_row))
            for field in actual_row:
                a, b = actual_row[field], expected_row[field]
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    self.assertTrue(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8), (field, a, b))
                else:
                    self.assertEqual(a, b)

    def assert_rejected_or_pending(self, contract, sql):
        # Intentionally wrong/unproven SQL is validated only, never executed.
        try:
            checked = self.validate(contract, sql)
        except ValueError:
            return
        self.assertTrue(checked["validation"]["pending_checks"], "A scoped predicate cannot silently certify the whole result population")

    def test_joined_independent_components_do_not_create_global_predicate_conflict(self):
        contract = self.quantity_contract()
        contract['metrics'][0]['aggregation'] = 'derived'
        contract['filters'] = [{'field': 'order_items.quantity', 'operator': '=', 'value': value} for value in (1, 2)]
        sql = ("WITH a AS (SELECT SUM(quantity) AS units FROM order_items WHERE quantity=1), "
               "b AS (SELECT SUM(quantity) AS units FROM order_items WHERE quantity=2) "
               "SELECT a.units+b.units AS units FROM a CROSS JOIN b")
        checked = self.validate(contract, sql)
        self.assertIn('filter_relation_scope_not_proven',
                      [item['code'] for item in checked['validation']['pending_checks']])
        actual = self.engine.execute(self.database, sql, self.scope)
        expected = self.engine.execute(self.database,
            'SELECT SUM(quantity) AS units FROM order_items WHERE quantity IN (1,2)', self.scope)
        self.assertTrue(actual.success, actual.error)
        self.assertEqual(actual.rows, expected.rows)

    def test_contradiction_in_one_component_is_still_not_accepted_as_proven(self):
        contract = self.quantity_contract()
        contract['filters'] = [{'field': 'order_items.quantity', 'operator': '=', 'value': 1}]
        self.assert_rejected_or_pending(contract,
            "WITH a AS (SELECT quantity FROM order_items WHERE quantity=1 AND quantity=2) "
            "SELECT SUM(quantity) AS units FROM a")

    def test_between_split_into_and_is_executable_with_reviewable_uncertainty(self):
        contract = self.quantity_contract()
        contract["filters"] = [{"field": "order_items.quantity", "operator": "between", "value": [1, 2]}]
        self.assert_equivalent_pending(contract,
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity>=1 AND quantity<=2",
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity BETWEEN 1 AND 2")

    def test_in_split_into_or_is_executable_with_reviewable_uncertainty(self):
        contract = self.quantity_contract()
        contract["filters"] = [{"field": "order_items.quantity", "operator": "in", "value": [1, 2]}]
        self.assert_equivalent_pending(contract,
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity=1 OR quantity=2",
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity IN (1,2)")

    def test_equality_split_into_two_bounds_is_executable_with_reviewable_uncertainty(self):
        contract = self.quantity_contract()
        contract["filters"] = [{"field": "order_items.quantity", "operator": "=", "value": 1}]
        self.assert_equivalent_pending(contract,
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity>=1 AND quantity<=1",
            "SELECT SUM(quantity) AS units FROM order_items WHERE quantity=1")

    def test_date_arithmetic_endpoint_is_executable_with_reviewable_uncertainty(self):
        contract = self.gmv_contract()
        prefix = ("SELECT SUM(oi.line_paid_gross_cents) AS gmv FROM payments p "
                  "JOIN order_items oi ON p.order_id=oi.order_id WHERE p.status='captured' "
                  "AND p.captured_at>=DATE '2026-06-01' AND p.captured_at<")
        self.assert_equivalent_pending(contract,
            prefix + "DATE '2026-06-01'+INTERVAL '1 month'",
            prefix + "DATE '2026-07-01'")

    def test_single_source_derived_metric_does_not_require_literal_name_in_user_text(self):
        contract = self.quantity_contract()
        contract["original_question"] = "平均每条明细购买多少件"
        contract["metrics"] = [{"name": "average_quantity", "source_fields": ["order_items.quantity"], "aggregation": "derived"}]
        self.assert_equivalent_pending(contract,
            "SELECT SUM(quantity)/NULLIF(COUNT(quantity),0) AS average_quantity FROM order_items",
            "SELECT AVG(quantity) AS average_quantity FROM order_items")

    def test_equivalent_sum_expression_is_executable_with_reviewable_uncertainty(self):
        self.assert_equivalent_pending(self.quantity_contract(),
            "SELECT AVG(quantity)*COUNT(quantity) AS units FROM order_items",
            "SELECT SUM(quantity) AS units FROM order_items")

    def test_case_predicate_cannot_silently_override_conflicting_global_filter(self):
        contract = self.gmv_contract()
        contract["original_question"] = "核对成功支付商品GMV"
        contract["time_range"] = None
        self.assert_rejected_or_pending(contract,
            "SELECT SUM(CASE WHEN p.status='captured' THEN oi.line_paid_gross_cents ELSE 0 END) AS gmv "
            "FROM payments p JOIN order_items oi ON p.order_id=oi.order_id WHERE p.status='failed'")

    def test_left_join_on_predicate_cannot_silently_filter_preserved_side(self):
        contract = self.gmv_contract()
        contract["original_question"] = "payments成功支付订单数"
        contract["time_range"] = None
        contract["metrics"] = [{"name": "order_id", "source_fields": ["payments.order_id"], "aggregation": "count_distinct"}]
        self.assert_rejected_or_pending(contract,
            "SELECT COUNT(DISTINCT p.order_id) AS orders FROM payments p LEFT JOIN order_items oi "
            "ON p.order_id=oi.order_id AND p.status='captured'")

    def test_explicitly_different_simple_filter_value_is_still_rejected(self):
        contract = self.quantity_contract()
        contract["filters"] = [{"field": "order_items.quantity", "operator": "=", "value": 1}]
        with self.assertRaises(ValueError):
            self.validate(contract, "SELECT SUM(quantity) AS units FROM order_items WHERE quantity=2")

    def test_repeating_or_adding_conditions_does_not_hide_a_known_value_conflict(self):
        contract = self.quantity_contract()
        contract['filters'] = [{'field': 'order_items.quantity', 'operator': '=', 'value': 1}]
        for condition in ['quantity=2 AND quantity=2', 'quantity=2 AND quantity>0', 'quantity=2 OR quantity=3']:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                self.validate(contract, 'SELECT SUM(quantity) AS units FROM order_items WHERE ' + condition)

    def test_boolean_identity_and_numeric_literal_spelling_do_not_false_block(self):
        contract = self.quantity_contract()
        contract['filters'] = [{'field': 'order_items.quantity', 'operator': '=', 'value': 1}]
        baseline = self.engine.execute(self.database, 'SELECT SUM(quantity) AS units FROM order_items WHERE quantity=1', self.scope)
        for condition in ['quantity=1 OR FALSE', 'quantity=1.0', 'quantity=1 OR (quantity=2 AND 1=0)']:
            with self.subTest(condition=condition):
                sql = 'SELECT SUM(quantity) AS units FROM order_items WHERE ' + condition
                self.validate(contract, sql)
                actual = self.engine.execute(self.database, sql, self.scope)
                self.assertTrue(actual.success, actual.error)
                self.assertEqual(actual.rows, baseline.rows)

    def test_explicitly_different_simple_time_boundaries_are_still_rejected(self):
        contract = self.gmv_contract()
        prefix = ("SELECT SUM(oi.line_paid_gross_cents) AS gmv FROM payments p "
                  "JOIN order_items oi ON p.order_id=oi.order_id WHERE p.status='captured' AND ")
        for condition in ["p.captured_at>='2026-06-01' AND p.captured_at<'2026-07-02'",
                          "p.captured_at>='2026-06-01' AND p.captured_at<='2026-07-01'",
                          "p.captured_at>'2026-06-01' AND p.captured_at<'2026-07-01'"]:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                self.validate(contract, prefix + condition)

    def test_gmv_cannot_use_quantity_as_money(self):
        contract = self.gmv_contract()
        contract["metrics"][0]["source_fields"] = ["order_items.quantity"]
        sql = ("SELECT SUM(oi.quantity) AS gmv FROM payments p JOIN order_items oi ON p.order_id=oi.order_id "
               "WHERE p.status='captured' AND p.captured_at>='2026-06-01' AND p.captured_at<'2026-07-01'")
        with self.assertRaises(ValueError):
            self.validate(contract, sql)


if __name__ == "__main__":
    unittest.main()
