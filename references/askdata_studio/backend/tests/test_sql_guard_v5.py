"""Hand-written SQL boundary probes: local public CSV only, no model or HTTP."""
from __future__ import annotations

import copy
import unittest

from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


class SqlGuardV5Test(unittest.TestCase):
    def setUp(self):
        self.database = 'homenova_20260831'
        self.engine = DuckDbEngine()
        self.validator = QueryContractValidator(self.engine)
        self.graph = schema_payload(self.database)
        self.scope = AccessController().scoped('demo_admin', self.database)

    def contract(self):
        return dict(original_question='核对成功支付商品GMV', data_source=self.database,
                    tables=['payments', 'order_items'],
                    metrics=[dict(name='paid_goods_gmv_gross', source_fields=['order_items.line_paid_gross_cents'], aggregation='sum')],
                    dimensions=[], filters=[dict(field='payments.status', operator='=', value='captured')],
                    time_range=None, grain='overall', assumptions=[], open_questions=[])

    def sql(self, condition="p.status='captured'", projection='SUM(oi.line_paid_gross_cents) AS gmv'):
        return (f'SELECT {projection} FROM payments p JOIN order_items oi ON p.order_id=oi.order_id'
                + (' WHERE ' + condition if condition else ''))

    def validate(self, sql, contract=None, scope=None):
        contract = contract or self.contract()
        return self.validator.validate(contract, sql, contract['original_question'], self.database,
                                       self.graph, (scope or self.scope).public())

    def checked_execute(self, sql, contract=None):
        self.validate(sql, contract)
        result = self.engine.execute(self.database, sql, self.scope)
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.complete)
        return result

    def test_parenthesized_capture_and_exact_utc_dates_match_local_boundaries(self):
        contract = self.contract()
        contract['time_range'] = dict(field='payments.captured_at', start='2026-06-01', end='2026-07-01')
        a = self.sql("(p.status='captured') AND (p.captured_at>='2026-05-31T22:00:00Z') AND (p.captured_at<'2026-06-30T22:00:00Z')")
        b = self.sql("p.status='captured' AND p.captured_at>='2026-06-01' AND p.captured_at<'2026-07-01'")
        self.assertEqual(self.checked_execute(a, contract).rows, self.checked_execute(b, contract).rows)

    def test_or_not_and_different_string_case_cannot_impersonate_captured_filter(self):
        # These are deliberately wrong queries; validate/preflight only, never execute.
        for condition in ["p.status='captured' OR p.status='failed'", "NOT(p.status='captured')", "p.status='CAPTURED'"]:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                self.validate(self.sql(condition))

    def test_half_open_time_range_does_not_accept_changed_endpoint_inclusion(self):
        contract = self.contract()
        contract['time_range'] = dict(field='payments.captured_at', start='2026-06-01', end='2026-08-01')
        for boundary in ["p.captured_at>='2026-06-01' AND p.captured_at<='2026-08-01'",
                         "p.captured_at>'2026-06-01' AND p.captured_at<'2026-08-01'",
                         "p.captured_at BETWEEN '2026-06-01' AND '2026-08-01'"]:
            with self.subTest(boundary=boundary), self.assertRaises(ValueError):
                self.validate(self.sql("p.status='captured' AND " + boundary), contract)

    def test_disjunction_preserving_capture_in_every_branch_is_valid(self):
        variant = self.sql("(p.status='captured' AND p.method='card') OR (p.status='captured' AND p.method!='card')")
        self.assertEqual(self.checked_execute(variant).rows, self.checked_execute(self.sql()).rows)

    def test_metric_local_case_filter_and_negated_inequality_are_valid(self):
        baseline = self.checked_execute(self.sql()).rows
        variants = [
            self.sql('', "SUM(CASE WHEN p.status='captured' THEN oi.line_paid_gross_cents ELSE 0 END) AS gmv"),
            self.sql('', "SUM(oi.line_paid_gross_cents) FILTER (WHERE p.status='captured') AS gmv"),
            self.sql("NOT(p.status!='captured')"),
        ]
        for sql in variants:
            with self.subTest(sql=sql):
                self.assertEqual(self.checked_execute(sql).rows, baseline)

    def test_nested_explicit_cte_star_transmits_real_columns(self):
        sql = ("WITH raw AS (SELECT order_id,status,captured_at FROM payments), "
               "paid AS (SELECT * FROM raw WHERE status='captured') "
               'SELECT SUM(oi.line_paid_gross_cents) AS gmv FROM paid p '
               'JOIN order_items oi ON p.order_id=oi.order_id')
        self.assertEqual(self.checked_execute(sql).rows, self.checked_execute(self.sql()).rows)

    def test_cte_column_renaming_preserves_count_and_expands_in_declared_order(self):
        contract = self.contract()
        contract['tables'] = ['payments']
        contract['metrics'] = [dict(name='paid_order_count', source_fields=['payments.order_id'], aggregation='count_distinct')]
        sql = ("WITH renamed(pid,pstate) AS (SELECT order_id,status FROM payments), "
               "paid AS (SELECT * FROM renamed WHERE pstate='captured') "
               'SELECT COUNT(DISTINCT pid) AS orders FROM paid')
        direct = "SELECT COUNT(DISTINCT order_id) AS orders FROM payments WHERE status='captured'"
        self.assertEqual(self.checked_execute(sql, contract).rows, self.checked_execute(direct, contract).rows)

    def test_qualified_cte_star_keeps_outer_join_values_without_using_merge(self):
        sql = ("WITH a AS (SELECT order_id,quantity FROM order_items), "
               "b AS (SELECT order_id FROM payments WHERE status='captured') "
               'SELECT a.* FROM a LEFT JOIN b ON a.order_id=b.order_id')
        explicit = sql.replace('a.*', 'a.order_id,a.quantity')
        # Engine-level projection test: no invented analytical metric contract.
        expanded = self.engine._validate_sql(self.database, sql, self.scope)
        self.assertNotIn('a.*', expanded)
        actual = self.engine.execute(self.database, expanded, self.scope)
        expected = self.engine.execute(self.database, explicit, self.scope)
        self.assertTrue(actual.success, actual.error)
        self.assertEqual(actual.rows, expected.rows)
        self.assertEqual(actual.total_rows, expected.total_rows)

    def test_scope_resolution_cannot_hide_denied_payments_behind_a_same_name_cte(self):
        denied = copy.deepcopy(self.scope.public())
        denied['allowed_tables'] = [t['id'] for t in self.graph['tables'] if t['name'] == 'order_items']
        from app.security import AccessScope
        resolved = AccessScope.from_dict(denied)
        sql = ('WITH payments AS (SELECT order_id FROM payments) '
               'SELECT SUM(oi.quantity) FROM order_items oi CROSS JOIN payments p')
        with self.assertRaises(ValueError):
            self.engine._validate_sql(self.database, sql, resolved)

    def test_external_tables_cross_snapshot_and_physical_stars_remain_rejected(self):
        for sql in ["SELECT order_id FROM homenova_20261015.orders",
                    "SELECT order_id FROM read_csv_auto('/tmp/not-a-project-file.csv')",
                    'SELECT * FROM payments',
                    'WITH q AS (SELECT * FROM payments) SELECT order_id FROM q']:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.engine._validate_sql(self.database, sql, self.scope)

    def test_between_endpoints_are_ordered_not_an_unordered_literal_set(self):
        contract = self.contract()
        contract['filters'].append(dict(field='order_items.quantity', operator='between', value=[1, 2]))
        with self.assertRaises(ValueError):
            self.validate(self.sql("p.status='captured' AND oi.quantity BETWEEN 2 AND 1"), contract)
        valid = self.checked_execute(self.sql("p.status='captured' AND oi.quantity BETWEEN 1 AND 2"), contract)
        self.assertTrue(valid.rows)

    def test_transformed_field_cannot_cleanly_prove_untransformed_filter(self):
        # UPPER(lowercase status)='captured' does not mean status='captured'.
        try:
            result = self.validate(self.sql("UPPER(p.status)='captured'"))
        except ValueError:
            return
        self.assertTrue(result['validation']['pending_checks'], 'A transformed predicate must not be silently certified as the declared raw filter')

    def test_uncorrelated_subquery_filter_does_not_cleanly_certify_outer_population(self):
        sql = self.sql("p.status='failed' AND EXISTS(SELECT pc.order_id FROM payments pc WHERE pc.status='captured')")
        try:
            result = self.validate(sql)
        except ValueError:
            return
        self.assertTrue(result['validation']['pending_checks'], 'An unrelated subquery filter does not constrain the outer failed-payment population')

    def test_reversed_equalities_and_singleton_in_are_valid_capture_filters(self):
        baseline = self.checked_execute(self.sql()).rows
        for condition in ["'captured'=p.status", "p.status IN ('captured')"]:
            with self.subTest(condition=condition):
                self.assertEqual(self.checked_execute(self.sql(condition)).rows, baseline)


if __name__ == '__main__':
    unittest.main()
