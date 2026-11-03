from copy import deepcopy
from unittest.mock import patch
import unittest

import duckdb

from app.querying.compiled_metrics import RecipeNotCovered
from app.querying.compiled_sessions import SessionPopulationMismatch, compile_sessions


def context():
    return {'database': 'homenova_20260831', 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'},
            'requested_time_expressions': ['2026-06', '2026-07']}


def request(**changes):
    return {'execution_mode': 'registered_metrics', 'metric_requirements': ['sessions_count', 'checkout_session_cvr'],
            'dimensions': ['month'], **changes}


class CompiledSessionsTest(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect(':memory:')
        self.db.execute('CREATE TABLE sessions(session_id VARCHAR PRIMARY KEY, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, '
                        'source_channel VARCHAR, is_internal_traffic BOOLEAN)')
        self.db.execute('CREATE TABLE orders(order_id VARCHAR PRIMARY KEY, checkout_session_id VARCHAR, expires_at TIMESTAMPTZ, status_as_of VARCHAR)')
        self.db.execute('CREATE TABLE payments(order_id VARCHAR, captured_at TIMESTAMPTZ, status VARCHAR)')
        self.db.execute('CREATE TABLE visit_events(session_id VARCHAR, sku_id VARCHAR, event_type VARCHAR)')
        self.db.execute('CREATE TABLE order_items(order_id VARCHAR, sku_id VARCHAR)')
        self.db.execute('CREATE TABLE sku_catalog(sku_id VARCHAR PRIMARY KEY, category_code VARCHAR)')
        self.db.executemany('INSERT INTO sku_catalog VALUES (?,?)', [('X', 'A'), ('Y', 'A'), ('Z', 'B')])
        self.db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?)', [
            ('s1', '2026-06-04T10:00:00Z', '2026-06-04T10:20:00Z', 'direct', False),
            ('s2', '2026-06-04T11:00:00Z', '2026-06-04T11:20:00Z', 'direct', False),
            ('s3', '2026-06-04T12:00:00Z', '2026-06-04T12:20:00Z', 'paid', False),
            ('s4', '2026-06-04T13:00:00Z', '2026-06-04T13:20:00Z', 'direct', True),
            ('s5', '2026-06-30T21:59:00Z', '2026-06-30T22:20:00Z', 'direct', False),
            ('s6', '2026-07-04T10:00:00Z', '2026-07-04T10:20:00Z', 'organic', False),
            ('s7', '2026-07-04T11:00:00Z', '2026-07-04T11:20:00Z', 'organic', False)])
        self.db.executemany('INSERT INTO visit_events VALUES (?,?,?)', [
            ('s1', 'X', 'product_view'), ('s1', 'X', 'product_view'), ('s1', 'Y', 'product_view'),
            ('s2', 'X', 'product_view'), ('s3', 'Y', 'product_view'), ('s4', 'X', 'product_view'),
            ('s5', 'X', 'product_view'), ('s6', 'Z', 'product_view'), ('s7', 'Z', 'product_view'),
            ('s7', 'X', 'ad_click')])
        self.db.executemany('INSERT INTO orders VALUES (?,?,?,?)', [
            ('o1', 's1', '2026-06-04T10:35:00Z', 'refunded'),
            ('o3', 's3', '2026-06-04T12:35:00Z', 'delivered'),
            ('o4', 's4', '2026-06-04T13:35:00Z', 'paid'),
            ('o5', 's5', '2026-06-30T22:25:00Z', 'shipped'),
            ('o6', 's6', '2026-07-04T10:35:00Z', 'delivered')])
        self.db.executemany('INSERT INTO order_items VALUES (?,?)', [('o1', 'Y'), ('o1', 'Y'), ('o3', 'Z'),
                                                                   ('o4', 'X'), ('o5', 'X'), ('o6', 'X')])
        self.db.executemany('INSERT INTO payments VALUES (?,?,?)', [
            ('o1', '2026-06-04T10:10:00Z', 'captured'), ('o1', '2026-06-04T10:10:00Z', 'captured'),
            ('o1', None, 'failed'), ('o3', '2026-06-04T12:10:00Z', 'captured'),
            ('o4', '2026-06-04T13:10:00Z', 'captured'), ('o5', '2026-06-30T22:10:00Z', 'captured'),
            ('o6', '2026-07-04T10:10:00Z', 'captured')])

    def tearDown(self):
        self.db.close()

    def run_recipe(self, query):
        candidate = compile_sessions(query, context())
        cur = self.db.execute(candidate['sql'])
        return candidate, [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

    def test_all_and_channel_cohorts_keep_refunded_and_cross_month_capture(self):
        candidate, rows = self.run_recipe(request())
        june, july = rows
        self.assertEqual((june['sessions_count'], june['checkout_paid_sessions'], june['checkout_session_cvr']), (4, 3, .75))
        self.assertEqual((july['sessions_count'], july['checkout_paid_sessions']), (2, 1))
        self.assertNotIn('status_as_of', candidate['sql'])
        _, filtered = self.run_recipe(request(dimensions=['month', 'sessions.source_channel'],
            conditions={'sessions.source_channel': 'direct'}))
        self.assertEqual((filtered[0]['sessions_count'], filtered[0]['checkout_paid_sessions']), (3, 2))

    def test_session_count_alone_does_not_introduce_purchase_dependencies(self):
        candidate, rows = self.run_recipe(request(metric_requirements=['sessions_count']))
        self.assertNotIn('payments', candidate['sql'])
        self.assertNotIn('orders', candidate['sql'])
        self.assertEqual(set(rows[0]), {'month', 'sessions_count'})
        self.assertEqual(candidate['registered_metric_ids'], ['sessions_count'])

    def test_sku_denominator_has_nonbuyers_and_purchase_must_match_browsed_sku(self):
        candidate, rows = self.run_recipe(request(metric_requirements=['sku_view_session_cvr'], dimensions=['month', 'visit_events.sku_id']))
        values = {(r['month'], r['sku_id']): r for r in rows}
        x = values['2026-06', 'X']; y = values['2026-06', 'Y']; z = values['2026-07', 'Z']
        self.assertEqual((x['sku_view_sessions'], x['sku_view_paid_sessions']), (3, 1))
        self.assertEqual((y['sku_view_sessions'], y['sku_view_paid_sessions']), (2, 1))
        self.assertEqual((z['sku_view_sessions'], z['sku_view_paid_sessions'], z['sku_view_session_cvr']), (2, 0, 0))
        self.assertFalse(candidate['output_metadata']['sku_view_sessions']['additive_across_entities'])
        self.assertNotIn(('2026-07', 'X'), values)  # ad_click is not a product view

    def test_category_denominator_deduplicates_skus_and_allows_same_category_purchase(self):
        candidate, rows = self.run_recipe(request(metric_requirements=['category_view_session_cvr'],
            dimensions=['month', 'sku_catalog.category_code']))
        values = {(r['month'], r['category_code']): r for r in rows}
        a = values['2026-06', 'A']; b = values['2026-07', 'B']
        self.assertEqual((a['category_view_sessions'], a['category_view_paid_sessions'], a['category_view_session_cvr']), (4, 2, .5))
        self.assertEqual((b['category_view_sessions'], b['category_view_paid_sessions']), (2, 0))
        self.assertEqual(candidate['recipe']['cohort'], 'category_view_sessions')

    def test_single_entity_filter_can_keep_month_only_without_changing_grouping(self):
        candidate, rows = self.run_recipe(request(metric_requirements=['category_view_session_cvr'],
            conditions={'sku_catalog.category_code': 'A'}))
        self.assertEqual(candidate['recipe']['dimensions'], ('month',))
        self.assertNotIn('category_code', rows[0])
        self.assertEqual((rows[0]['category_view_sessions'], rows[0]['category_view_paid_sessions']), (4, 2))
        with self.assertRaises(RecipeNotCovered):
            compile_sessions(request(metric_requirements=['category_view_session_cvr'],
                conditions={'sku_catalog.category_code': ['A', 'B']}), context())

    def test_immature_sessions_preserve_denominator_and_make_rate_unknown(self):
        self.db.execute("UPDATE sessions SET ended_at=NULL WHERE session_id='s2'")
        _, rows = self.run_recipe(request())
        self.assertEqual(rows[0]['sessions_count'], 4)
        self.assertEqual(rows[0]['unmatured_sessions'], 1)
        self.assertIsNone(rows[0]['checkout_session_cvr'])
        self.db.execute("UPDATE sessions SET ended_at='2026-06-04T11:20:00Z' WHERE session_id='s2'")
        self.db.execute("UPDATE orders SET expires_at='2026-09-01T00:00:00Z' WHERE order_id='o1'")
        _, rows = self.run_recipe(request())
        self.assertEqual(rows[0]['unmatured_sessions'], 1)
        self.assertIsNone(rows[0]['checkout_session_cvr'])

    def test_typed_filter_and_injection_are_data_not_sql(self):
        _, rows = self.run_recipe(request(conditions={'filters': [{'field': 'sessions.source_channel', 'values': ["direct') OR 1=1 --"]}]}))
        self.assertEqual(rows, [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 7)

    def test_unsupported_populations_and_exploration_are_not_relabelled(self):
        for changes in [{'execution_mode': 'exploratory'}, {'conditions': {'sku_catalog.category_code': 'A'}},
                        {'metric_requirements': ['sessions_count', 'sku_view_session_cvr']},
                        {'metric_requirements': ['sku_view_session_cvr'], 'dimensions': ['month', 'sku_catalog.category_code']},
                        {'metric_requirements': ['category_view_session_cvr'], 'dimensions': ['month']},
                        {'required_output_fields': ['paid_order_count']}, {'conditions': {'months': ['2026-09']}}]:
            with self.subTest(changes=changes), self.assertRaises(RecipeNotCovered):
                compile_sessions(request(**changes), context())

    def test_registered_store_cohort_entity_scope_requires_explicit_method(self):
        for metrics in (['sessions_count'], ['checkout_session_cvr'], ['sessions_count', 'checkout_session_cvr']):
            for changes, alternatives in [
                ({'conditions': {'sku_catalog.category_code': 'POWER'}}, ['category_view_session_cvr']),
                ({'conditions': {'filters': [{'field': 'sku_catalog.category_code', 'values': ['POWER']}]}},
                 ['category_view_session_cvr']),
                ({'dimensions': ['month', 'sku_catalog.category_code']}, ['category_view_session_cvr']),
                ({'conditions': {'order_items.sku_id': 'X'}}, ['sku_view_session_cvr']),
                ({'dimensions': ['month', 'visit_events.sku_id']}, ['sku_view_session_cvr']),
                ({'conditions': {'sessions.landing_sku_id': 'X'}}, ['sku_view_session_cvr'])]:
                query = request(metric_requirements=metrics, **changes)
                original = deepcopy(query)
                with self.subTest(metrics=metrics, changes=changes), self.assertRaises(SessionPopulationMismatch) as error:
                    compile_sessions(query, context())
                self.assertEqual(error.exception.alternative_metric_ids, alternatives)
                self.assertEqual(query, original)  # No automatic population/metric substitution.

    def test_uncovered_capability_and_exploration_are_not_population_conflicts(self):
        for changes in [
            {'execution_mode': 'exploratory', 'conditions': {'sku_catalog.category_code': 'POWER'}},
            {'conditions': {'sessions.device_type': 'mobile'}},
            {'conditions': {'months': ['2026-09']}}]:
            with self.subTest(changes=changes), self.assertRaises(RecipeNotCovered) as error:
                compile_sessions(request(**changes), context())
            self.assertIs(type(error.exception), RecipeNotCovered)

    def test_changed_definition_refuses_and_app_definition_leaves_public_sku_unchanged(self):
        from app.homenova import registered_metric
        from app.querying.session_metric_definitions import extend_session_metrics
        original = [deepcopy(registered_metric('homenova_20260831', 'sku_view_session_cvr'))]
        extended = extend_session_metrics(original, 'homenova_20260831')
        self.assertEqual(extended[0], original[0])
        self.assertEqual(len(extended), 2)
        self.assertEqual(extend_session_metrics(original, 'homenova_20261015'), original)
        def changed(database, name):
            metric = deepcopy(registered_metric(database, name))
            if metric and metric['id'] == 'checkout_session_cvr':
                metric['formula'] = 'different cohort'
            return metric
        with patch('app.homenova.registered_metric', side_effect=changed):
            with self.assertRaisesRegex(RecipeNotCovered, 'requires revalidation'):
                compile_sessions(request(), context())
