from copy import deepcopy
from unittest.mock import patch
import unittest

import duckdb

from app.querying.compiled_inventory import compile_inventory_duration, inventory_duration_requested
from app.querying.compiled_metrics import RecipeNotCovered
from app.querying.calculations import execute_calculation


def context():
    return {'database': 'homenova_20260831', 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'},
            'requested_time_expressions': ['2026-06', '2026-07']}


def request(**changes):
    return {'metric_requirements': ['stockout_duration', 'available_inventory'],
            'dimensions': ['month', 'sku_catalog.sku_id'], **changes}


class CompiledInventoryTest(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect(':memory:')
        self.db.execute('CREATE TABLE inventory_movements(sku_id VARCHAR, warehouse_id VARCHAR, occurred_at TIMESTAMPTZ, '
                        'event_seq INT, movement_type VARCHAR, on_hand_delta INT, reserved_delta INT)')
        self.db.execute('CREATE TABLE sku_catalog(sku_id VARCHAR PRIMARY KEY, category_code VARCHAR)')
        # Cross-month stockout, reservation then release at a stable timestamp,
        # plus an independent warehouse which never becomes unavailable.
        self.db.executemany('INSERT INTO inventory_movements VALUES (?,?,?,?,?,?,?)', [
            ('X', 'A', '2026-04-30T22:00:00Z', 1, 'opening', 3, 0),
            ('X', 'A', '2026-06-30T10:00:00Z', 2, 'reserve', 0, 3),
            ('X', 'A', '2026-07-02T10:00:00Z', 3, 'release', 0, -3),
            ('X', 'A', '2026-07-02T10:00:00Z', 4, 'ship', -1, 0),
            ('X', 'B', '2026-04-30T22:00:00Z', 1, 'opening', 4, 0),
            ('Y', 'A', '2026-04-30T22:00:00Z', 1, 'opening', 2, 0)])
        self.db.executemany('INSERT INTO sku_catalog VALUES (?,?)', [('X', 'GROUP'), ('Y', 'OTHER')])
        self.graph = {'tables': [{'id': 'im', 'name': 'inventory_movements'},
                                 {'id': 's', 'name': 'sku_catalog', 'primary_key': ['sku_id']}],
                      'fields': []}
        for table, fields in [('inventory_movements', [('sku_id', 'VARCHAR'), ('warehouse_id', 'VARCHAR'),
                                  ('occurred_at', 'TIMESTAMPTZ'), ('event_seq', 'INT'), ('movement_type', 'VARCHAR'),
                                  ('on_hand_delta', 'INT'), ('reserved_delta', 'INT')]),
                              ('sku_catalog', [('sku_id', 'VARCHAR'), ('category_code', 'VARCHAR')])]:
            for name, sql_type in fields:
                self.graph['fields'].append({'table_name': table, 'name': name, 'sql_type': sql_type})

    def tearDown(self):
        self.db.close()

    def calculate(self, query):
        compiled = compile_inventory_duration(query, context())
        cur = self.db.execute(compiled['sql'])
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        source = {'columns': columns, 'rows': rows, 'sql': compiled['sql'], 'complete': True,
                  'total_rows': len(rows), 'schema_graph': self.graph,
                  'observation_cutoff_at': context()['scenario']['observation_cutoff_at']}
        return compiled, execute_calculation(compiled['postprocess'], source)

    def test_cross_month_intersection_uses_complete_history_and_preserves_warehouses(self):
        candidate, result = self.calculate(request(conditions={'sku_catalog.category_code': 'GROUP'}))
        rows = {(r['warehouse_id'], r['period']): r for r in result['rows']}
        self.assertEqual(set(rows), {('A', '2026-06'), ('A', '2026-07'), ('B', '2026-06'), ('B', '2026-07')})
        self.assertEqual(rows['A', '2026-06']['duration_hours'], 12)
        self.assertEqual(rows['A', '2026-07']['duration_hours'], 36)
        self.assertEqual((rows['A', '2026-06']['opening_state'], rows['A', '2026-06']['ending_state']), (3, 0))
        self.assertEqual((rows['A', '2026-07']['opening_state'], rows['A', '2026-07']['ending_state']), (0, 2))
        self.assertEqual(rows['B', '2026-07']['duration_hours'], 0)
        self.assertNotIn('2026-06', candidate['sql'])
        self.assertFalse(candidate['output_metadata']['duration_hours']['additive_across_entities'])

    def test_noncontinuous_months_and_partition_filter_do_not_drop_opening_rows(self):
        _, result = self.calculate(request(conditions={'months': ['2026-07', '2026-05', '2026-07'],
            'inventory_movements.sku_id': ['X'], 'inventory_movements.warehouse_id': 'A'}))
        self.assertEqual([r['period'] for r in result['rows']], ['2026-05', '2026-07'])
        self.assertEqual(result['rows'][1]['duration_hours'], 36)

    def test_category_label_preserves_every_sku_warehouse_month_and_opening(self):
        self.db.execute("UPDATE sku_catalog SET category_code='GROUP' WHERE sku_id='Y'")
        conditions = {'months': ['2026-05', '2026-06', '2026-07']}
        old, baseline = self.calculate(request(conditions=conditions))
        for sku in ('sku_catalog.sku_id', 'inventory_movements.sku_id', 'order_items.sku_id', 'sku_id'):
            candidate, labelled = self.calculate(request(dimensions=['month', sku, 'category_code'],
                conditions=conditions, required_output_fields=['category_code', 'sku_id', 'duration_hours']))
            key = lambda r: (r['sku_id'], r['warehouse_id'], r['period'])
            self.assertEqual({key(r): {k: v for k, v in r.items() if k != 'category_code'} for r in labelled['rows']},
                             {key(r): r for r in baseline['rows']})
            self.assertEqual(len(labelled['rows']), 9, 'Two SKUs sharing a category remain separate')
            self.assertEqual({r['category_code'] for r in labelled['rows']}, {'GROUP'})
            self.assertEqual(candidate['postprocess']['partition_by'], ['category_code', 'sku_id', 'warehouse_id'])
            self.assertEqual(candidate['recipe']['attribute_bindings'], (('category_code', 'sku_catalog.category_code'),))
            self.assertEqual(candidate['output_metadata']['category_code']['kind'], 'dimension')
            self.assertNotEqual(candidate['spec_fingerprint'], old['spec_fingerprint'])
            self.assertNotIn('2026-06', candidate['sql'], 'Full opening history must remain available')

    def test_unknown_dimension_feedback_is_precise_and_category_cannot_replace_sku(self):
        with self.assertRaisesRegex(RecipeNotCovered, r'Unsupported inventory dimensions: sessions.source_channel; allowed:'):
            compile_inventory_duration(request(dimensions=['month', 'sku_id', 'sessions.source_channel']), context())
        with self.assertRaisesRegex(RecipeNotCovered, 'category is only an optional SKU label'):
            compile_inventory_duration(request(dimensions=['month', 'category_code']), context())
        with self.assertRaisesRegex(RecipeNotCovered, 'Extra requested output'):
            compile_inventory_duration(request(required_output_fields=['category_code']), context())

    def test_empty_filtered_ledger_is_unknown_not_zero_duration(self):
        _, result = self.calculate(request(conditions={'sku_catalog.category_code': "GROUP') OR 1=1 --"}))
        self.assertFalse(result['calculation_complete'])
        self.assertEqual(result['rows'], [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM inventory_movements').fetchone()[0], 6)

    def test_metric_alias_selects_capability_and_mixed_request_is_not_silently_reduced(self):
        self.assertTrue(inventory_duration_requested(request(metric_requirements=['缺货时长']), 'homenova_20260831'))
        self.assertFalse(inventory_duration_requested(request(metric_requirements=['available_inventory']), 'homenova_20260831'))
        with self.assertRaisesRegex(RecipeNotCovered, 'Split inventory'):
            compile_inventory_duration(request(metric_requirements=['stockout_duration', 'sku_view_session_cvr']), context())

    def test_unsupported_scope_periods_grain_and_extra_output_remain_explicit(self):
        for changes in [{'dimensions': ['month']}, {'dimensions': ['month', 'sku_catalog.category_code']},
                        {'conditions': {'channel': 'organic'}}, {'conditions': {'months': ['2026-09']}},
                        {'conditions': {'months': ['recent']}}, {'required_output_fields': ['conversion']},
                        {'metric_requirements': ['available_inventory']}]:
            with self.subTest(changes=changes), self.assertRaises(RecipeNotCovered):
                compile_inventory_duration(request(**changes), context())
        scoped = context(); scoped['task'] = {'task_understanding': {'scope': {'kind': 'category'}}}
        with self.assertRaisesRegex(RecipeNotCovered, 'scoped task'):
            compile_inventory_duration(request(), scoped)
        wrong = context(); wrong['database'] = 'homenova_20261015'
        with self.assertRaises(RecipeNotCovered):
            compile_inventory_duration(request(), wrong)

    def test_registered_definition_changes_invalidate_the_recipe(self):
        from app.homenova import registered_metric
        def changed(database, name):
            metric = deepcopy(registered_metric(database, name))
            if metric and metric['id'] == 'stockout_duration':
                metric['formula'] = 'different threshold'
            return metric
        with patch('app.homenova.registered_metric', side_effect=changed):
            with self.assertRaisesRegex(RecipeNotCovered, 'requires revalidation'):
                compile_inventory_duration(request(), context())
