"""Paid recipe outputs must be bound to its selected dimensions, not all possible ones."""
from copy import deepcopy
import unittest

from sqlglot import parse_one

from app.querying.compiled_metrics import RecipeNotCovered, compile_paid_goods


def request(*, dimensions=None, outputs=None, conditions=None):
    return {'execution_mode': 'registered_metrics', 'metric_requirements': ['paid_goods_gmv_gross'],
            'dimensions': ['month'] if dimensions is None else dimensions,
            'required_output_fields': [] if outputs is None else outputs,
            'conditions': {'months': ['2026-06', '2026-07']} if conditions is None else conditions}


def compile_request(query):
    return compile_paid_goods(query, {'database': 'homenova_20260831',
        'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'}})


class CompiledMetricOutputBindingsTest(unittest.TestCase):
    def test_selected_category_alias_matches_qualified_output_without_changing_query(self):
        query = request(dimensions=['month', 'sku_catalog.category_code'],
                        outputs=['month', 'category_code', 'paid_goods_gmv_gross'])
        before = deepcopy(query)
        actual = compile_request(query)
        qualified = compile_request({**query, 'required_output_fields': [
            'month', 'sku_catalog.category_code', 'paid_goods_gmv_gross']})
        self.assertEqual(actual, qualified)
        self.assertEqual(query, before)
        self.assertIn('category_code', parse_one(actual['sql'], read='duckdb').named_selects)

    def test_selected_sku_alias_matches_each_supported_qualified_dimension(self):
        for dimension in ['order_items.sku_id', 'sku_catalog.sku_id']:
            with self.subTest(dimension=dimension):
                query = request(dimensions=['month', dimension], outputs=['month', 'sku_id', 'paid_goods_gmv_gross'])
                actual = compile_request(query)
                qualified = compile_request({**query, 'required_output_fields': ['month', dimension, 'paid_goods_gmv_gross']})
                self.assertEqual(actual, qualified)
                self.assertIn('sku_id', parse_one(actual['sql'], read='duckdb').named_selects)

    def test_unselected_alias_is_not_a_request_to_add_a_dimension(self):
        for output in ['category_code', 'sku_id']:
            with self.subTest(output=output), self.assertRaises(RecipeNotCovered):
                compile_request(request(outputs=[output]))

    def test_unselected_qualified_output_is_rejected_instead_of_silently_omitted(self):
        for output in ['sku_catalog.category_code', 'sku_catalog.sku_id', 'order_items.sku_id']:
            with self.subTest(output=output), self.assertRaises(RecipeNotCovered):
                compile_request(request(outputs=[output]))

    def test_filter_does_not_implicitly_select_the_corresponding_output_dimension(self):
        conditions = {'months': ['2026-06', '2026-07'], 'sku_catalog.category_code': 'POWER'}
        for output in ['category_code', 'sku_catalog.category_code']:
            with self.subTest(output=output), self.assertRaises(RecipeNotCovered):
                compile_request(request(outputs=[output], conditions=conditions))

    def test_another_physical_sku_field_is_not_implicitly_added_as_an_output(self):
        with self.assertRaises(RecipeNotCovered):
            compile_request(request(dimensions=['month', 'order_items.sku_id'], outputs=['sku_catalog.sku_id']))

    def test_alias_does_not_relax_unknown_conditions_or_missing_month_grouping(self):
        invalid = [request(dimensions=['month', 'sku_catalog.category_code'], outputs=['unknown_output']),
                   request(dimensions=['sku_catalog.category_code'], outputs=['category_code']),
                   request(dimensions=['month', 'sku_catalog.category_code'], outputs=['category_code'],
                           conditions={'months': ['2026-06'], 'unknown_filter': 'KEEP'})]
        for query in invalid:
            before = deepcopy(query)
            with self.subTest(query=query), self.assertRaises(RecipeNotCovered):
                compile_request(query)
            self.assertEqual(query, before)
