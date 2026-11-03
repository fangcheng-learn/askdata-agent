"""Discovery IDs share the application registry without inventing new metrics."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import homenova
from app.database import SCHEMA
from app.querying.compiled_sessions import compile_sessions
from app.security import AccessScope
from app.workflows.react_harness import ReactInvestigationHarness


SOURCE = 'homenova_20260831'


def task(source=SOURCE, table_ids=None):
    if table_ids is None:
        table_ids = [table['id'] for table in SCHEMA if table.get('database') == source]
    scope = AccessScope('catalog-test', ('analyst',), frozenset({source}), frozenset(table_ids))
    return SimpleNamespace(state={'datasource_id': source, 'access_scope': scope.public()})


class CapabilityMetricCatalogTest(unittest.TestCase):
    def test_supported_ids_and_directory_follow_the_same_application_registry_for_each_snapshot(self):
        for source in homenova.SOURCES:
            with self.subTest(source=source):
                catalog = ReactInvestigationHarness._capability_directory(task(source))
                expected = [metric['id'] for metric in homenova.metrics(source)]
                self.assertEqual([metric['id'] for metric in catalog['metric_directory']], expected)
                self.assertEqual(catalog['supported_metric_ids'], expected)
                self.assertEqual(len(catalog['supported_metric_ids']), len(set(expected)))

    def test_discovery_projection_does_not_modify_cached_public_capabilities(self):
        original = deepcopy(homenova.public_capabilities(SOURCE))
        for key in ['metric_directory', 'available_fact_domains', 'catalog_boundary']:
            original.pop(key, None)
        before = deepcopy(original)
        with patch('app.workflows.react_harness.homenova.public_capabilities', return_value=original):
            ReactInvestigationHarness._capability_directory(task())
        self.assertEqual(original, before)

    def test_other_scope_projection_does_not_rewrite_a_previous_callers_directory(self):
        original = deepcopy(homenova.public_capabilities(SOURCE))
        source_tables = [table['id'] for table in SCHEMA if table.get('database') == SOURCE]
        self.assertGreater(len(source_tables), 1)
        with patch('app.workflows.react_harness.homenova.public_capabilities', return_value=original):
            first = ReactInvestigationHarness._capability_directory(task())
            first_snapshot = deepcopy(first)
            restricted = ReactInvestigationHarness._capability_directory(task(table_ids=source_tables[:1]))
        self.assertEqual(first, first_snapshot)
        self.assertEqual([entry['id'] for entry in restricted['available_fact_domains']], source_tables[:1])

    def test_catalog_remains_compact_and_only_lists_authorized_fact_domains(self):
        tables = [table['id'] for table in SCHEMA if table.get('database') == SOURCE]
        request_task = task(table_ids=tables[:1])
        catalog = ReactInvestigationHarness._capability_directory(request_task)
        self.assertEqual([entry['id'] for entry in catalog['available_fact_domains']], tables[:1])
        self.assertTrue(all(set(entry) <= {'id', 'label'} for entry in catalog['available_fact_domains']))
        self.assertTrue(all(set(entry) <= {'id', 'label'} for entry in catalog['metric_directory']))

    def test_category_ratio_already_supplies_local_count_components_without_new_global_metrics(self):
        identity = 'category_view_session_cvr'
        denominator, numerator = 'category_view_sessions', 'category_view_paid_sessions'
        self.assertIsNotNone(homenova.registered_metric(SOURCE, identity))
        self.assertIsNone(homenova.registered_metric(SOURCE, denominator))
        self.assertIsNone(homenova.registered_metric(SOURCE, numerator))
        candidate = compile_sessions({
            'execution_mode': 'registered_metrics', 'metric_requirements': [identity],
            'dimensions': ['month', 'sku_catalog.category_code'],
            'conditions': {'months': ['2026-06', '2026-07']},
            'required_output_fields': [denominator, numerator, identity],
        }, {'database': SOURCE, 'scenario': {'observation_cutoff_at': '2026-08-31T22:00:00Z'}})
        self.assertEqual(candidate['registered_metric_ids'], [identity])
        local = {entry['id']: entry for entry in candidate['local_measurements']}
        self.assertTrue({denominator, numerator} <= local.keys())
        for name in [denominator, numerator]:
            self.assertEqual(local[name]['unit'], 'sessions')
            self.assertEqual(local[name]['event_time'], ['sessions.started_at'])
            self.assertTrue(local[name]['definition'])
        self.assertEqual(candidate['output_metadata'][identity]['denominator_column'], denominator)
        self.assertEqual(candidate['output_metadata'][identity]['numerator_column'], numerator)
        catalog = ReactInvestigationHarness._capability_directory(task())
        self.assertNotIn(denominator, {entry['id'] for entry in catalog['metric_directory']})
        self.assertNotIn(numerator, {entry['id'] for entry in catalog['metric_directory']})


if __name__ == '__main__':
    unittest.main()
