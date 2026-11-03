"""Inherited time fields must reach the authorized schema check as full names."""
from copy import deepcopy
import unittest

from app.querying.query_context import assemble_query_context
from app.querying.task_measurements import measurement_field_refs


class MeasurementFieldRefsTest(unittest.TestCase):
    def setUp(self):
        self.graph = {'database': 'test', 'tables': [{'id': 'payments', 'name': 'payments', 'database': 'test'}],
            'fields': [{'id': 'payments.' + name, 'table_id': 'payments', 'table_name': 'payments',
                        'name': name, 'sql_name': 'payments.' + name} for name in ('order_id', 'captured_at')],
            'joins': []}
        self.scope = {'user_id': 'reader', 'allowed_databases': ['test'], 'allowed_tables': ['payments']}

    def context(self, definition):
        return assemble_query_context(self.graph, access_scope=self.scope,
                                      requested_fields=measurement_field_refs(definition))

    def test_scalar_and_list_time_fields_reach_same_schema_context(self):
        scalar = {'source_fields': ['payments.order_id'], 'event_time': 'payments.captured_at'}
        original = deepcopy(scalar)
        listed = {**scalar, 'event_time': ['payments.captured_at']}
        actual = self.context(scalar)
        self.assertEqual(actual, self.context(listed))
        self.assertEqual(actual['missing_requests'], [])
        self.assertEqual({f['sql_name'] for f in actual['schema_graph']['fields']},
                         {'payments.order_id', 'payments.captured_at'})
        self.assertEqual(scalar, original, 'Do not rewrite immutable evidence definitions')

    def test_unknown_time_field_stays_one_explicit_missing_reference(self):
        for event_time in ('payments.unknown_at', ['payments.unknown_at']):
            with self.subTest(event_time=event_time):
                actual = self.context({'source_fields': ['payments.order_id'], 'event_time': event_time})
                self.assertEqual([(m['kind'], m['id']) for m in actual['missing_requests']],
                                 [('field', 'payments.unknown_at')])
                self.assertEqual({f['sql_name'] for f in actual['schema_graph']['fields']}, {'payments.order_id'})

    def test_malformed_time_reference_is_rejected_not_silently_dropped(self):
        for event_time in ({'field': 'payments.captured_at'}, 42, ['payments.captured_at', None]):
            with self.subTest(event_time=event_time), self.assertRaisesRegex(ValueError, 'event_time'):
                self.context({'event_time': event_time})


if __name__ == '__main__':
    unittest.main()
