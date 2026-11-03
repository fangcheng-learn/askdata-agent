"""Constant timezone semantics and refusal to evaluate dynamic SQL."""
import unittest
import sqlglot

from app.querying.sql_literals import constant_value


class SqlLiteralTest(unittest.TestCase):
    def value(self, text):
        return constant_value(sqlglot.parse_one('SELECT ' + text, read='duckdb').expressions[0])

    def test_timestamp_timezone_constants_preserve_real_utc_offset(self):
        for date, expected in [('2026-01-01', '+01:00'), ('2026-06-01', '+02:00')]:
            for expression in [f"TIMESTAMP '{date}' AT TIME ZONE 'Europe/Berlin'",
                               f"CAST('{date}' AS TIMESTAMP) AT TIME ZONE 'Europe/Berlin'"]:
                with self.subTest(expression=expression):
                    self.assertEqual(self.value(expression), date + 'T00:00:00' + expected)

    def test_aware_input_localizes_without_zone_as_duckdb_at_timezone_does(self):
        self.assertEqual(self.value("TIMESTAMPTZ '2026-06-01T00:00:00Z' AT TIME ZONE 'Europe/Berlin'"),
                         '2026-06-01T02:00:00')

    def test_dynamic_expressions_are_not_executed_or_guessed(self):
        for text in ['CURRENT_DATE', "CURRENT_DATE - INTERVAL '1 day'", 'random()',
                     "TIMESTAMP '2026-01-01' AT TIME ZONE 'Unknown/Zone'", 'captured_at']:
            with self.subTest(text=text):
                self.assertIsNone(self.value(text))

    def test_numeric_boolean_and_literal_cast_compatibility(self):
        self.assertEqual(self.value('-12'), -12)
        self.assertIs(self.value('FALSE'), False)
        self.assertEqual(self.value("CAST('2026-06-01' AS DATE)"), '2026-06-01')


if __name__ == '__main__':
    unittest.main()
