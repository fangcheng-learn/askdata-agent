"""Calculator contracts and arithmetic; no model, query, network or business data."""
from decimal import Decimal
import unittest

from app.querying.calculations import (
    CalculationError, execute_calculation, model_tool_schema, parse_calculation_request,
)


def data(columns, rows, **extra):
    return {'columns': columns, 'rows': rows, 'complete': True, 'total_rows': len(rows), **extra}


def request(operation, **parameters):
    return {'operation': operation, 'result_id': 'T:R1', **parameters}


class CalculationProtocolTest(unittest.TestCase):
    def assert_error(self, code, callback):
        with self.assertRaises(CalculationError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_original_bad_request_cannot_create_zero_change_evidence(self):
        error = self.assert_error('invalid_parameters', lambda: execute_calculation(
            request('change', base_field='value', current_field='value', group_by='month'),
            data(['month', 'value'], [{'month': 'before', 'value': 10}, {'month': 'after', 'value': 8}])))
        self.assertIn('errors', error.details)

    def test_unknown_parameter_is_not_silently_ignored(self):
        error = self.assert_error('invalid_parameters', lambda: parse_calculation_request(
            request('sum', column='value', accidental_filter='channel=a')))
        self.assertEqual(error.details['errors'][0]['type'], 'extra_forbidden')

    def test_only_explicit_controller_envelope_keys_are_ignored(self):
        parsed = parse_calculation_request({'type': 'calculate', 'goal': 'compare', 'repair_action_id': 'A1',
                                            **request('change', base_field='before', current_field='after')})
        self.assertEqual(parsed.operation, 'change')
        self.assertNotIn('goal', parsed.model_dump())
        self.assert_error('invalid_parameters', lambda: parse_calculation_request(
            {'type': 'query', **request('sum', column='value')}))

    def test_actual_schema_exposes_all_operations_and_forbids_extra(self):
        schema = model_tool_schema()
        self.assertEqual(set(schema['discriminator']['mapping']),
                         {'sum', 'column_stats', 'change', 'ratio', 'period_change', 'product_decomposition', 'state_duration'})
        self.assertFalse(schema['$defs']['PeriodChangeRequest']['additionalProperties'])

    def test_wide_change_preserves_original_columns_and_decimal_precision(self):
        result = execute_calculation(request('change', base_field='before', current_field='after'),
            data(['channel', 'before', 'after'], [{'channel': 'a', 'before': Decimal('0.3'), 'after': Decimal('0.2') }]))
        self.assertEqual(result['rows'][0]['change'], -0.1)
        self.assertEqual(result['rows'][0]['channel'], 'a')
        self.assertAlmostEqual(result['rows'][0]['change_rate'], -1 / 3)

    def test_zero_base_is_distinct_from_null_and_never_infinite(self):
        result = execute_calculation(request('change', base_field='before', current_field='after'),
            data(['before', 'after'], [{'before': 0, 'after': 2}, {'before': None, 'after': 2}]))
        self.assertEqual(result['rows'][0]['change'], 2)
        self.assertIsNone(result['rows'][0]['change_rate'])
        self.assertIsNone(result['rows'][1]['change'])
        self.assertEqual([item['code'] for item in result['diagnostics']], ['zero_base', 'null_value'])

    def test_ratio_zero_denominator_and_different_dimension_unit(self):
        result = execute_calculation(request('ratio', numerator='amount', denominator='orders'),
            data(['amount', 'orders'], [{'amount': 10, 'orders': 2}, {'amount': 10, 'orders': 0}],
                 column_metadata={'amount': {'unit': 'EUR'}, 'orders': {'unit': 'orders'}}))
        self.assertEqual(result['rows'][0]['ratio'], 5)
        self.assertIsNone(result['rows'][1]['ratio'])
        self.assertNotIn('unit', result['column_metadata']['ratio'], 'EUR/orders is not automatically a dimensionless ratio')
        self.assertEqual(result['diagnostics'][0]['code'], 'zero_denominator')

    def test_known_incompatible_units_are_not_subtracted(self):
        self.assert_error('incompatible_units', lambda: execute_calculation(
            request('change', base_field='a', current_field='b'),
            data(['a', 'b'], [{'a': 100, 'b': 1}], units={'a': 'EUR_cents', 'b': 'EUR'})))

    def test_sum_and_stats_do_not_replace_null_with_zero(self):
        result = execute_calculation(request('column_stats', column='v'),
            data(['v'], [{'v': 2}, {'v': -1}, {'v': None}]))
        self.assertEqual(result['rows'], [{'field': 'v', 'sum': 1.0, 'non_null_count': 2, 'row_count': 3,
                                          'positive_sum': 2.0, 'negative_sum': -1.0,
                                          'positive_count': 1, 'negative_count': 1}])
        empty = execute_calculation(request('sum', column='v'), data(['v'], []))
        self.assertIsNone(empty['rows'][0]['sum'])

    def test_explicit_non_additive_measure_is_not_summed(self):
        self.assert_error('non_additive', lambda: execute_calculation(request('sum', column='people'),
            data(['people'], [{'people': 2}], column_metadata={'people': {'additive': False}})))

    def test_non_numeric_boolean_and_infinite_fail_with_specific_diagnosis(self):
        for value, code in [(True, 'non_numeric'), ('not a number', 'non_numeric'), (float('inf'), 'non_finite')]:
            with self.subTest(value=value):
                self.assert_error(code, lambda: execute_calculation(request('sum', column='v'), data(['v'], [{'v': value}])))

    def test_missing_column_empty_result_and_incomplete_source_fail(self):
        self.assert_error('unknown_columns', lambda: execute_calculation(request('sum', column='missing'), data(['v'], [])))
        source = data(['v'], [{'v': 1}], complete=False)
        self.assert_error('incomplete_result', lambda: execute_calculation(request('sum', column='v'), source))
        self.assert_error('incomplete_result', lambda: execute_calculation(request('sum', column='v'),
                                                                 data(['v'], [{'v': 1}], total_rows=2)))

    def test_long_period_comparison_selects_explicit_periods_and_all_metrics(self):
        source = data(['month', 'amount', 'orders'], [
            {'month': 'earlier', 'amount': 100, 'orders': 4},
            {'month': 'later', 'amount': 75, 'orders': 3},
            {'month': 'outside', 'amount': 999, 'orders': 999}])
        result = execute_calculation(request('period_change', period_field='month', base_period='earlier',
            current_period='later', value_fields=['amount', 'orders']), source)
        self.assertEqual([(r['metric'], r['change'], r['change_rate']) for r in result['rows']],
                         [('amount', -25.0, -0.25), ('orders', -1.0, -0.25)])
        self.assertTrue(result['calculation_complete'])

    def test_grouped_period_comparison_is_not_cross_group_matched(self):
        source = data(['month', 'channel', 'v'], [
            {'month': 1, 'channel': 'a', 'v': 100}, {'month': 2, 'channel': 'a', 'v': 80},
            {'month': 1, 'channel': 'b', 'v': 40}, {'month': 2, 'channel': 'b', 'v': 50}])
        result = execute_calculation(request('period_change', period_field='month', base_period=1,
            current_period=2, value_fields=['v'], group_by=['channel']), source)
        self.assertEqual([(r['channel'], r['change']) for r in result['rows']], [('a', -20.0), ('b', 10.0)])

    def test_group_names_cannot_be_overwritten_by_calculation_output_names(self):
        result = execute_calculation(request('period_change', period_field='p', base_period=1,
            current_period=2, value_fields=['value'], group_by=['metric', 'status']),
            data(['p', 'metric', 'status', 'value'], [
                {'p': 1, 'metric': 'revenue', 'status': 'paid', 'value': 10},
                {'p': 2, 'metric': 'revenue', 'status': 'paid', 'value': 8}]))
        self.assertEqual(result['group_field_map'], {'metric': 'group_metric', 'status': 'group_status'})
        row = result['rows'][0]
        self.assertEqual(row['group_metric'], 'revenue')
        self.assertEqual(row['group_status'], 'paid')
        self.assertEqual(row['metric'], 'value')
        self.assertEqual(row['status'], 'ok')

    def test_empty_grouped_comparison_does_not_invent_a_group(self):
        result = execute_calculation(request('period_change', period_field='p', base_period=1,
            current_period=2, value_fields=['v'], group_by=['channel']), data(['p', 'channel', 'v'], []))
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['columns'][0], 'channel')
        self.assertFalse(result['calculation_complete'])

    def test_duplicate_period_is_neither_summed_nor_first_row_selected(self):
        self.assert_error('duplicate_period', lambda: execute_calculation(request('period_change',
            period_field='month', base_period=1, current_period=2, value_fields=['v']),
            data(['month', 'v'], [{'month': 1, 'v': 2}, {'month': 1, 'v': 3}, {'month': 2, 'v': 9}])))

    def test_missing_period_is_exposed_not_filled_with_zero(self):
        result = execute_calculation(request('period_change', period_field='month', base_period=1,
            current_period=2, value_fields=['v']), data(['month', 'v'], [{'month': 1, 'v': 10}]))
        self.assertEqual(result['rows'][0]['status'], 'missing_current_period')
        self.assertIsNone(result['rows'][0]['current'])
        self.assertIsNone(result['rows'][0]['change'])
        self.assertFalse(result['calculation_complete'])
        self.assertTrue(result['complete'], 'Stored rows are complete even when the comparison has a missing period')

    def test_null_and_zero_period_values_have_distinct_statuses(self):
        result = execute_calculation(request('period_change', period_field='month', base_period=1,
            current_period=2, value_fields=['null_v', 'zero_v']), data(['month', 'null_v', 'zero_v'], [
                {'month': 1, 'null_v': None, 'zero_v': 0}, {'month': 2, 'null_v': 2, 'zero_v': 2}]))
        self.assertEqual([r['status'] for r in result['rows']], ['null_value', 'zero_base'])
        self.assertEqual(result['rows'][1]['change'], 2)

    def test_period_values_and_dimensions_must_be_unambiguous(self):
        for changes in [{'base_period': True}, {'current_period': float('nan')}, {'group_by': ['month']},
                        {'value_fields': ['month']}, {'value_fields': ['v', 'v']}]:
            params = dict(period_field='month', base_period=1, current_period=2, value_fields=['v'])
            params.update(changes)
            with self.subTest(changes=changes):
                self.assert_error('invalid_parameters', lambda: parse_calculation_request(request('period_change', **params)))

    def test_product_decomposition_reconciles_and_preserves_positive_offset(self):
        result = execute_calculation(request('product_decomposition', period_field='period', base_period='base',
            current_period='current', factor_fields=['count', 'average'], product_field='total'),
            data(['period', 'count', 'average', 'total'], [
                {'period': 'base', 'count': 10, 'average': 5, 'total': 50},
                {'period': 'current', 'count': 6, 'average': 7, 'total': 42}]))
        row = result['rows'][0]
        self.assertEqual(row['factor_a_contribution'], -24)
        self.assertEqual(row['factor_b_contribution'], 16)
        self.assertEqual(row['change'], -8)
        self.assertEqual(row['reconciliation_error'], 0)
        self.assertEqual(result['interpretation'], 'arithmetic_decomposition_not_causal_effect')

    def test_product_cannot_claim_identity_inconsistent_with_real_total(self):
        self.assert_error('product_identity_mismatch', lambda: execute_calculation(request('product_decomposition',
            period_field='p', base_period=1, current_period=2, factor_fields=['a', 'b'], product_field='g'),
            data(['p', 'a', 'b', 'g'], [{'p': 1, 'a': 2, 'b': 3, 'g': 99}, {'p': 2, 'a': 2, 'b': 4, 'g': 8}])))

    def test_source_and_request_are_not_modified(self):
        source = data(['a', 'b'], [{'a': 2, 'b': 3}])
        args = request('change', base_field='a', current_field='b')
        execute_calculation(args, source)
        self.assertEqual(source['rows'], [{'a': 2, 'b': 3}])
        self.assertNotIn('change', source['columns'])
        self.assertNotIn('type', args)


if __name__ == '__main__':
    unittest.main()
