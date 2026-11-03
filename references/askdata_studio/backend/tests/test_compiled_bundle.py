from copy import deepcopy
import unittest
from unittest.mock import patch

import duckdb

from app.querying.compiled_bundle import compile_bundle
from app.querying.compiled_metrics import METRICS, RecipeNotCovered, compile_paid_goods
from app.querying.compiled_sessions import SessionPopulationMismatch, compile_sessions


def context():
    return {'database': 'homenova_20260831', 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'}}


def request(**changes):
    return {'execution_mode': 'registered_metrics', 'metric_requirements': [
        'paid_goods_gmv_gross', 'paid_order_count', 'sessions_count', 'checkout_session_cvr'],
        'dimensions': ['month'], 'conditions': {'months': ['2026-05', '2026-06', '2026-07', '2026-08']}, **changes}


class CompiledBundleTest(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect(':memory:')
        self.db.execute('CREATE TABLE sessions(session_id VARCHAR, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, '
                        'source_channel VARCHAR, is_internal_traffic BOOLEAN)')
        self.db.execute('CREATE TABLE orders(order_id VARCHAR, checkout_session_id VARCHAR, expires_at TIMESTAMPTZ)')
        self.db.execute('CREATE TABLE payments(order_id VARCHAR, captured_at TIMESTAMPTZ, status VARCHAR)')
        self.db.execute('CREATE TABLE order_items(order_id VARCHAR, quantity INTEGER, line_paid_gross_cents BIGINT)')
        self.db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?)', [
            ('s0', '2026-04-30T21:59:00Z', '2026-04-30T22:20:00Z', 'direct', False),
            ('s1', '2026-06-05T10:00:00Z', '2026-06-05T10:20:00Z', 'direct', False),
            ('s2', '2026-06-30T21:59:00Z', '2026-06-30T22:20:00Z', 'direct', False),
            ('s3', '2026-07-05T10:00:00Z', '2026-07-05T10:20:00Z', 'direct', False),
            ('internal', '2026-07-05T11:00:00Z', '2026-07-05T11:20:00Z', 'direct', True),
            ('unfinished', '2026-08-31T21:59:00Z', None, 'direct', False)])
        self.db.executemany('INSERT INTO orders VALUES (?,?,?)', [
            ('o0', 's0', '2026-04-30T22:25:00Z'), ('o1', 's1', '2026-06-05T10:35:00Z'),
            ('o2', 's2', '2026-06-30T22:25:00Z'), ('o3', 'internal', '2026-07-05T11:35:00Z')])
        self.db.executemany('INSERT INTO payments VALUES (?,?,?)', [
            ('o0', '2026-04-30T22:05:00Z', 'captured'), ('o1', '2026-06-05T10:10:00Z', 'captured'),
            ('o1', '2026-06-05T10:10:00Z', 'captured'), ('o1', None, 'failed'),
            ('o2', '2026-06-30T22:05:00Z', 'captured'), ('o3', '2026-07-05T11:10:00Z', 'captured')])
        self.db.executemany('INSERT INTO order_items VALUES (?,?,?)', [
            ('o0', 1, 100), ('o1', 2, 1000), ('o1', 1, 800), ('o2', 1, 500), ('o3', 1, 700)])

    def tearDown(self):
        self.db.close()

    def execute(self, query):
        candidate = compile_bundle(query, context())
        result = self.db.execute(candidate['sql'])
        return candidate, [dict(zip([c[0] for c in result.description], row)) for row in result.fetchall()]

    def test_full_join_retains_one_sided_months_and_distinct_time_cohorts(self):
        _, rows = self.execute(request())
        may, june, july, august = rows
        self.assertEqual([r['month'] for r in rows], ['2026-05', '2026-06', '2026-07', '2026-08'])
        self.assertEqual(may['paid_goods_gmv_gross'], 100)
        self.assertIsNone(may['sessions_count'])  # No fabricated zero cohort.
        self.assertEqual((june['paid_goods_gmv_gross'], june['paid_order_count'], june['paid_goods_quantity']), (1800, 1, 3))
        self.assertEqual((june['sessions_count'], june['checkout_paid_sessions'], june['checkout_session_cvr']), (2, 2, 1))
        self.assertEqual((july['paid_goods_gmv_gross'], july['paid_order_count']), (1200, 2))
        self.assertEqual((july['sessions_count'], july['checkout_paid_sessions'], july['checkout_session_cvr']), (1, 0, 0))
        self.assertIsNone(august['paid_goods_gmv_gross'])
        self.assertEqual((august['sessions_count'], august['unmatured_sessions']), (1, 1))
        self.assertIsNone(august['checkout_session_cvr'])

    def test_actual_outputs_definitions_units_and_component_versions_survive(self):
        query = request()
        candidate, rows = self.execute(query)
        paid = compile_paid_goods({**query, 'metric_requirements': ['paid_goods_gmv_gross', 'paid_order_count']}, context())
        sessions = compile_sessions({**query, 'metric_requirements': ['sessions_count', 'checkout_session_cvr']}, context())
        self.assertEqual(set(rows[0]), {'month', *paid['output_metadata'], *sessions['output_metadata']})
        self.assertEqual(candidate['output_metadata'], {**paid['output_metadata'], **sessions['output_metadata']})
        self.assertEqual(candidate['local_measurements'], sessions['local_measurements'])
        self.assertEqual(set(candidate['registered_metric_ids']), {*METRICS, 'sessions_count', 'checkout_session_cvr'})
        self.assertEqual([t['field'] for t in candidate['query_contract']['time_ranges']],
                         ['payments.captured_at', 'sessions.started_at'])
        for saved, original in zip(candidate['recipe']['components'], (paid, sessions)):
            self.assertEqual(saved, {'recipe': original['recipe'], 'spec_fingerprint': original['spec_fingerprint']})

    def test_count_only_session_component_and_disjoint_months_are_preserved(self):
        candidate, rows = self.execute(request(metric_requirements=['paid_order_count', 'sessions_count'],
            conditions={'months': ['2026-08', '2026-05', '2026-08']}))
        self.assertEqual([r['month'] for r in rows], ['2026-05', '2026-08'])
        self.assertNotIn('checkout_session_cvr', candidate['registered_metric_ids'])
        self.assertNotIn('checkout_paid_sessions', rows[0])
        self.assertEqual(candidate['local_measurements'], [])

    def test_unknown_outputs_or_uncovered_conditions_cannot_be_dropped(self):
        for changes in [
            {'required_output_fields': ['month', 'unknown_output']},
            {'metric_requirements': ['paid_order_count', 'sessions_count'], 'required_output_fields': ['checkout_session_cvr']},
            {'conditions': {'months': ['2026-06'], 'sessions.source_channel': 'direct'}},
            {'conditions': {'months': ['2026-06'], 'uncovered': None}},
            {'dimensions': ['month', 'sessions.source_channel']}, {'conditions': {}},
            {'conditions': {'months': ['2026-09']}}, {'metric_requirements': ['paid_order_count']},
            {'metric_requirements': ['paid_order_count', 'unknown_metric']}, {'execution_mode': 'exploratory'}]:
            query = request(**changes)
            original = deepcopy(query)
            with self.subTest(changes=changes), self.assertRaises(RecipeNotCovered):
                compile_bundle(query, context())
            self.assertEqual(query, original)
        scoped = context(); scoped['task'] = {'task_understanding': {'scope': {'kind': 'category'}}}
        with self.assertRaises(RecipeNotCovered):
            compile_bundle(request(), scoped)

    def test_mixed_scope_conflicts_take_precedence_over_unknown_family(self):
        for compiler in (compile_sessions, compile_bundle):
            for metrics in (['paid_goods_gmv_gross', 'sessions_count', 'checkout_session_cvr'],
                            ['unregistered_metric', 'sessions_count']):
                query = request(metric_requirements=metrics, dimensions=['month', 'sku_catalog.category_code'])
                with self.subTest(compiler=compiler.__name__, metrics=metrics), self.assertRaises(SessionPopulationMismatch):
                    compiler(query, context())
                with self.assertRaises(RecipeNotCovered) as error:
                    compiler({**query, 'execution_mode': 'exploratory'}, context())
                self.assertIs(type(error.exception), RecipeNotCovered)

    def test_alias_order_does_not_change_recipe_and_changed_definition_refuses(self):
        a = compile_bundle(request(), context())
        b = compile_bundle(request(metric_requirements=['checkout_session_cvr', 'sessions_count', 'paid_order_count', 'GMV']), context())
        self.assertEqual(a['spec_fingerprint'], b['spec_fingerprint'])
        from app.homenova import registered_metric
        def changed(database, name):
            metric = deepcopy(registered_metric(database, name))
            if metric and metric['id'] == 'checkout_session_cvr':
                metric['maturity'] = 'changed observation window'
            return metric
        with patch('app.homenova.registered_metric', side_effect=changed), self.assertRaisesRegex(RecipeNotCovered, 'requires revalidation'):
            compile_bundle(request(), context())


if __name__ == '__main__':
    unittest.main()
