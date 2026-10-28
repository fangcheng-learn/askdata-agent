"""Sanitized failure regression fixtures and public local CSV time boundaries."""
from __future__ import annotations

import copy
import csv
from decimal import Decimal
from pathlib import Path
import unittest

from app.database import SCHEMA, physical_table_name
from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


BACKEND = Path(__file__).resolve().parents[1]


class TimeContractV7Test(unittest.TestCase):
    def setUp(self):
        self.database = 'ecommerce_ops'
        self.engine = DuckDbEngine()
        self.validator = QueryContractValidator(self.engine)
        self.scope = AccessController().scoped('demo_current_sales', self.database)
        self.graph = {'tables': [dict(id=table['id'], name=physical_table_name(table), database=self.database)
                                for table in SCHEMA if table.get('database') == self.database]}

    def contract(self, *, start='2026-08-01', end='2026-09-01', inclusive=False):
        period = {'field': 'daily_store_metrics.stat_date', 'start': start, 'end': end}
        if inclusive is not None:
            period['end_inclusive'] = inclusive
        return {
            'original_question': '核对指定期间店铺GMV', 'data_source': self.database,
            'tables': ['daily_store_metrics'],
            'metrics': [{'name': 'gmv', 'source_fields': ['daily_store_metrics.gmv'], 'aggregation': 'sum'}],
            'dimensions': [], 'filters': [], 'time_range': period,
            'grain': 'overall', 'assumptions': [], 'open_questions': [],
        }

    @staticmethod
    def sql(*, start='2026-08-01', upper='2026-09-01', operator='<'):
        return ("SELECT SUM(gmv) AS gmv FROM daily_store_metrics WHERE stat_date >= '"
                + start + "' AND stat_date " + operator + " '" + upper + "'")

    def validate(self, contract, sql):
        return self.validator.validate(copy.deepcopy(contract), sql, contract['original_question'],
                                       self.database, self.graph, self.scope.public())

    @staticmethod
    def has_time_pending(checked):
        return any(item['code'].startswith('time_') and item['code'] != 'time_coverage_not_proven'
                   for item in checked['validation']['pending_checks'])

    def august_csv_total(self):
        with (BACKEND / 'data/databases/ecommerce_ops/daily_store_metrics.csv').open(
                encoding='utf-8-sig', newline='') as stream:
            return sum((Decimal(row['gmv']) for row in csv.DictReader(stream)
                        if '2026-08-01' <= row['stat_date'] < '2026-09-01'), Decimal(0))

    def test_saved_real_candidates_validate_and_execute_as_full_august(self):
        # Minimal public-schema SQL/contract fixtures from failure task
        # 5d857a88b6f6. Full private task replay is preserved separately; this
        # regression deliberately does not read task history or private reports.
        attempts = []
        for number, (operator, upper) in enumerate([
                ('<', '2026-09-01'), ('<=', '2026-08-31'), ('<=', '2026-08-31')], 1):
            contract = self.contract(end='2026-08-31', inclusive=None)
            contract['original_question'] = '2026年8月GMV'
            contract['filters'] = [
                {'field': 'daily_store_metrics.stat_date', 'operator': '>=', 'value': '2026-08-01'},
                {'field': 'daily_store_metrics.stat_date', 'operator': operator, 'value': upper},
            ]
            attempts.append({'attempt': number, 'query_contract': contract,
                             'sql': self.sql(operator=operator, upper=upper)})
        self.assertEqual(len(attempts), 3)
        expected = self.august_csv_total()
        for attempt in attempts:
            with self.subTest(attempt=attempt['attempt']):
                checked = self.validate(attempt['query_contract'], attempt['sql'])
                self.assertEqual(checked['time_range']['end'], '2026-09-01')
                self.assertIs(checked['time_range']['end_inclusive'], False)
                self.assertTrue(self.has_time_pending(checked), 'Legacy boundary interpretation must remain visible')
                actual = self.engine.execute(self.database, attempt['sql'], self.scope)
                self.assertTrue(actual.success, actual.error)
                self.assertTrue(actual.complete)
                self.assertEqual(actual.total_rows, 1)
                self.assertAlmostEqual(actual.rows[0]['gmv'], float(expected), places=5)

    def test_correct_calendar_range_still_marks_missing_observation_coverage(self):
        checked = self.validate(self.contract(start='2026-08-01', end='2026-09-01', inclusive=False),
                                self.sql())
        coverage = next(item for item in checked['validation']['pending_checks']
                        if item['code'] == 'time_coverage_not_proven')
        self.assertEqual(coverage['field'], 'daily_store_metrics.stat_date')
        self.assertEqual(coverage['last_observed_date'], '2026-08-22')
        self.assertEqual(coverage['requested_end'], '2026-09-01')

    def test_explicit_exclusive_date_end_accepts_the_two_equivalent_sql_spellings(self):
        contract = self.contract()
        for operator, upper in [('<', '2026-09-01'), ('<=', '2026-08-31')]:
            with self.subTest(operator=operator):
                checked = self.validate(contract, self.sql(operator=operator, upper=upper))
                self.assertEqual(checked['time_range']['end'], '2026-09-01')
                self.assertIs(checked['time_range']['end_inclusive'], False)

    def test_explicit_exclusive_boundary_does_not_silently_gain_an_extra_day(self):
        contract = self.contract(end='2026-08-31')
        for operator, upper in [('<', '2026-09-01'), ('<=', '2026-08-31')]:
            with self.subTest(operator=operator), self.assertRaises(ValueError):
                self.validate(contract, self.sql(operator=operator, upper=upper))

    def test_explicit_inclusive_date_end_is_normalized_to_exclusive_next_day(self):
        contract = self.contract(end='2026-08-31', inclusive=True)
        for operator, upper in [('<', '2026-09-01'), ('<=', '2026-08-31')]:
            with self.subTest(operator=operator):
                checked = self.validate(contract, self.sql(operator=operator, upper=upper))
                self.assertEqual(checked['time_range']['end'], '2026-09-01')
                self.assertIs(checked['time_range']['end_inclusive'], False)
                self.assertTrue(checked['validation']['normalizations'])

    def test_legacy_end_matching_sql_still_does_not_certify_the_requested_calendar_month(self):
        checked = self.validate(self.contract(end='2026-08-31', inclusive=None), self.sql(upper='2026-08-31'))
        self.assertTrue(self.has_time_pending(checked), 'Unspecified legacy endpoint semantics must not be silently certified')
        self.assertFalse(checked['validation']['business_definition_verified'])

    def test_legacy_compatibility_does_not_allow_an_arbitrary_later_boundary(self):
        with self.assertRaises(ValueError):
            self.validate(self.contract(end='2026-08-31', inclusive=None), self.sql(upper='2026-09-02'))

    def test_inclusive_calendar_rollover_handles_leap_month_and_year_end(self):
        for start, end, upper in [('2024-02-01', '2024-02-29', '2024-03-01'),
                                  ('2026-12-01', '2026-12-31', '2027-01-01')]:
            with self.subTest(end=end):
                checked = self.validate(self.contract(start=start, end=end, inclusive=True),
                                        self.sql(start=start, upper=upper))
                self.assertEqual(checked['time_range']['end'], upper)
                self.assertIs(checked['time_range']['end_inclusive'], False)

    def assert_single_day_matches_csv(self, *, inclusive):
        # Use a populated day for the arithmetic check. The demo CSV does not
        # cover August 31; SQL SUM over that empty day is NULL, not zero.
        day, next_day = '2026-08-01', '2026-08-02'
        with (BACKEND / 'data/databases/ecommerce_ops/daily_store_metrics.csv').open(
                encoding='utf-8-sig', newline='') as stream:
            expected = sum((Decimal(row['gmv']) for row in csv.DictReader(stream)
                            if row['stat_date'] == day), Decimal(0))
        contract = self.contract(start=day, end=day, inclusive=inclusive)
        for operator, upper in [('<', next_day), ('<=', day)]:
            with self.subTest(inclusive=inclusive, operator=operator):
                sql = self.sql(start=day, upper=upper, operator=operator)
                checked = self.validate(contract, sql)
                self.assertEqual(checked['time_range']['start'], day)
                self.assertEqual(checked['time_range']['end'], next_day)
                self.assertIs(checked['time_range']['end_inclusive'], False)
                if inclusive is None:
                    self.assertTrue(self.has_time_pending(checked), 'A legacy same-day boundary still requires interpretation review')
                actual = self.engine.execute(self.database, sql, self.scope)
                self.assertTrue(actual.success, actual.error)
                self.assertTrue(actual.complete)
                self.assertEqual(actual.total_rows, 1)
                self.assertAlmostEqual(actual.rows[0]['gmv'], float(expected), places=5)

    def test_explicit_inclusive_equal_start_and_end_is_a_real_single_day(self):
        self.assert_single_day_matches_csv(inclusive=True)

    def test_legacy_equal_start_and_end_can_be_a_single_day_with_pending(self):
        self.assert_single_day_matches_csv(inclusive=None)

    def test_explicit_exclusive_equal_start_and_end_remains_invalid(self):
        day = '2026-08-31'
        with self.assertRaises(ValueError):
            self.validate(self.contract(start=day, end=day, inclusive=False),
                          self.sql(start=day, upper='2026-09-01'))

    def test_timestamp_midnight_inclusion_cannot_certify_an_entire_last_calendar_day(self):
        database = 'homenova_20260831'
        contract = {
            'original_question': '核对指定期间成功支付GMV', 'data_source': database,
            'tables': ['payments', 'order_items'],
            'metrics': [{'name': 'paid_goods_gmv_gross', 'source_fields': ['order_items.line_paid_gross_cents'], 'aggregation': 'sum'}],
            'dimensions': [], 'filters': [{'field': 'payments.status', 'operator': '=', 'value': 'captured'}],
            'time_range': {'field': 'payments.captured_at', 'start': '2026-08-01', 'end': '2026-08-31', 'end_inclusive': True},
            'grain': 'overall', 'assumptions': [], 'open_questions': [],
        }
        sql = ("SELECT SUM(oi.line_paid_gross_cents) AS gmv FROM payments p "
               "JOIN order_items oi ON p.order_id=oi.order_id WHERE p.status='captured' "
               "AND p.captured_at>='2026-08-01' AND p.captured_at<='2026-08-31'")
        # Wrong/incomplete temporal coverage is never actually executed here.
        try:
            checked = self.validator.validate(contract, sql, contract['original_question'], database,
                schema_payload(database), AccessController().scoped('demo_admin', database).public())
        except ValueError:
            return
        self.assertTrue(self.has_time_pending(checked), 'TIMESTAMPTZ <= midnight must not be treated as an entire final day')


if __name__ == '__main__':
    unittest.main()
