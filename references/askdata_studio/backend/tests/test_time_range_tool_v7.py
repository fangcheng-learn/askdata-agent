"""Date-tool endpoint contracts through real local MCP; no query/model calls."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import unittest
from unittest.mock import patch

from app import homenova
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.mcp_runtime.schemas import DateRangeResult
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController


class TimeRangeToolV7Test(unittest.TestCase):
    def client(self, database='ecommerce_ops'):
        user = 'demo_admin' if homenova.is_homenova(database) else 'demo_current_sales'
        scope = AccessController().scoped(user, database)
        return LocalMcpClient(create_local_mcp_server(DuckDbEngine(), scope))

    def resolved_month(self, year, month, day=15, expression='本月'):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(year, month, day, 12, 0, tzinfo=tz)

        with patch('app.mcp_runtime.tools.time_tools.datetime', FrozenDatetime):
            return self.client().call_tool('resolve_date_range', {
                'expression': expression, 'timezone_name': 'Asia/Shanghai'})

    def assert_exclusive_endpoint(self, result):
        self.assertEqual(result['end_exclusive'],
                         (date.fromisoformat(result['end_date']) + timedelta(days=1)).isoformat())
        self.assertLess(result['start_date'], result['end_exclusive'])

    def test_computed_endpoint_is_in_all_default_serialization_modes(self):
        result = DateRangeResult(expression='八月', start_date='2026-08-01',
                                 end_date='2026-08-31', timezone='Asia/Shanghai')
        for serialized in [result.model_dump(), result.model_dump(mode='json'), json.loads(result.model_dump_json())]:
            with self.subTest(serialized=serialized):
                self.assertEqual(serialized['end_date'], '2026-08-31', 'legacy inclusive endpoint must remain')
                self.assertEqual(serialized['end_exclusive'], '2026-09-01')
        with self.assertRaises((AttributeError, ValueError)):
            result.end_exclusive = '2026-08-31'

    def test_local_mcp_output_schema_exposes_readonly_endpoint_and_boundary_meanings(self):
        tool = next(tool for tool in self.client().list_tools() if tool['name'] == 'resolve_date_range')
        fields = tool['output_schema']['properties']
        self.assertIn('包含起始', fields['start_date']['description'])
        self.assertIn('包含当天', fields['end_date']['description'])
        self.assertIn('排他终点', fields['end_exclusive']['description'])
        self.assertTrue(fields['end_exclusive']['readOnly'])

    def test_standard_mcp_month_preserves_legacy_end_and_supplies_next_day(self):
        result = self.resolved_month(2026, 8)
        self.assertEqual(result['start_date'], '2026-08-01')
        self.assertEqual(result['end_date'], '2026-08-31')
        self.assertEqual(result['end_exclusive'], '2026-09-01')
        self.assert_exclusive_endpoint(result)

    def test_standard_mcp_handles_leap_and_nonleap_february(self):
        for year, last_day in [(2024, '2024-02-29'), (2026, '2026-02-28')]:
            with self.subTest(year=year):
                result = self.resolved_month(year, 2)
                self.assertEqual(result['end_date'], last_day)
                self.assertEqual(result['end_exclusive'], f'{year}-03-01')
                self.assert_exclusive_endpoint(result)

    def test_standard_mcp_year_end_and_previous_month_cross_year(self):
        result = self.resolved_month(2026, 12, expression='今年')
        self.assertEqual(result['start_date'], '2026-01-01')
        self.assertEqual(result['end_date'], '2026-12-31')
        self.assertEqual(result['end_exclusive'], '2027-01-01')
        self.assert_exclusive_endpoint(result)
        prior = self.resolved_month(2027, 1, expression='上月')
        self.assertEqual(prior['start_date'], '2026-12-01')
        self.assertEqual(prior['end_date'], '2026-12-31')
        self.assertEqual(prior['end_exclusive'], '2027-01-01')

    def test_homenova_mcp_uses_same_endpoint_contract_without_reading_machine_clock(self):
        class NoMachineDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError('HomeNova must use snapshot scenario date')

        for database in ['homenova_20260831', 'homenova_20261015']:
            with self.subTest(database=database), patch('app.mcp_runtime.server.datetime', NoMachineDatetime):
                client = self.client(database)
                scenario = homenova.scenario(database)
                expected = datetime.fromisoformat(scenario['analysis_datetime']).date()
                today = client.call_tool('resolve_date_range', {'expression': '今天', 'timezone_name': 'Europe/Berlin'})
                self.assertEqual(today['start_date'], expected.isoformat())
                self.assertEqual(today['end_date'], expected.isoformat())
                self.assert_exclusive_endpoint(today)
                month = client.call_tool('resolve_date_range', {'expression': '本月', 'timezone_name': 'Europe/Berlin'})
                self.assertEqual(month['start_date'], expected.replace(day=1).isoformat())
                self.assert_exclusive_endpoint(month)
                fields = next(tool for tool in client.list_tools() if tool['name'] == 'resolve_date_range')['output_schema']['properties']
                self.assertTrue(fields['end_exclusive']['readOnly'])


if __name__ == '__main__':
    unittest.main()
