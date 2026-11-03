"""Entity binding uses acquired public metadata and real values, never guesses."""
from copy import deepcopy
import unittest

from app.querying.entity_resolution import (is_time_grain_term, prepare_entity_context,
                                           resolve_dimension)
from app.querying.measurement_context import resolve_query_requirements
from app.security import AccessScope


def graph():
    return {
        'database': 'fixture_aug',
        'tables': [
            {'id': 'catalog', 'name': 'sku_catalog', 'database': 'fixture_aug', 'primary_key': ['sku_id']},
            {'id': 'sessions', 'name': 'sessions', 'database': 'fixture_aug', 'primary_key': ['session_id']},
            {'id': 'items', 'name': 'order_items', 'database': 'fixture_aug'},
            {'id': 'future', 'name': 'future_sessions', 'database': 'fixture_sep'},
        ],
        'fields': [
            {'id': 'catalog.sku_id', 'table_id': 'catalog', 'name': 'sku_id',
             'sql_name': 'sku_catalog.sku_id', 'role': 'identifier', 'samples': ['SKU-01']},
            {'id': 'catalog.category_code', 'table_id': 'catalog', 'name': 'category_code',
             'sql_name': 'sku_catalog.category_code', 'label': '商品类目代码',
             'aliases': ['category code'], 'role': 'dimension', 'enum_values': ['POWER', 'HOME']},
            {'id': 'catalog.store_id', 'table_id': 'catalog', 'name': 'store_id',
             'sql_name': 'sku_catalog.store_id', 'label': '经营店铺标识',
             'role': 'dimension', 'enum_values': ['HN-DE-01']},
            {'id': 'sessions.source_channel', 'table_id': 'sessions', 'name': 'source_channel',
             'sql_name': 'sessions.source_channel', 'label': '会话来源渠道',
             'aliases': ['source channel', 'traffic origin'], 'role': 'dimension',
             'samples': ['direct', 'organic_search']},
            {'id': 'sessions.started_at', 'table_id': 'sessions', 'name': 'started_at',
             'sql_name': 'sessions.started_at', 'role': 'time'},
            {'id': 'items.sku_id', 'table_id': 'items', 'name': 'sku_id',
             'sql_name': 'order_items.sku_id', 'role': 'dimension'},
            {'id': 'future.source_channel', 'table_id': 'future', 'name': 'source_channel',
             'sql_name': 'future_sessions.source_channel', 'enum_values': ['future_secret']},
        ],
        'joins': [{'left_table': 'items', 'left_field': 'sku_id',
                   'right_table': 'catalog', 'right_field': 'sku_id', 'relation_type': 'many_to_one'}],
    }


def scope(tables=('catalog', 'sessions', 'items'), databases=('fixture_aug',)):
    return AccessScope(user_id='analyst', roles=('analyst',),
                       allowed_databases=frozenset(databases), allowed_tables=frozenset(tables))


def prepare(requests, metadata=None, **kwargs):
    return prepare_entity_context(requests, metadata or graph(), access_scope=kwargs.pop('access_scope', scope()),
                                  datasource_id=kwargs.pop('datasource_id', 'fixture_aug'), **kwargs)


class DimensionResolutionTest(unittest.TestCase):
    def test_business_labels_ids_and_aliases_select_one_existing_field(self):
        for term in ('channel', '渠道', '会话来源渠道', 'traffic origin',
                     'sessions.source_channel', 'sessions.source_channel'):
            with self.subTest(term=term):
                resolved = resolve_dimension(term, graph(), access_scope=scope(), datasource_id='fixture_aug')
                self.assertEqual(resolved['status'], 'resolved')
                self.assertEqual(resolved['field'], 'sessions.source_channel')
                self.assertEqual(resolved['field_info']['field_name'], 'source_channel')

    def test_wrong_technical_spelling_does_not_erase_real_candidate(self):
        for term, expected in [('sessions.channel', 'sessions.source_channel'),
                               ('order_items.category', 'sku_catalog.category_code')]:
            resolved = resolve_dimension(term, graph(), access_scope=scope(), datasource_id='fixture_aug')
            self.assertEqual(resolved['field'], expected)

    def test_multiple_fields_remain_technical_candidates(self):
        metadata = graph()
        metadata['fields'].append({'id': 'items.channel', 'table_id': 'items', 'name': 'channel',
                                    'label': '渠道', 'sql_name': 'order_items.channel'})
        # Exact metadata name is more specific than a concept; generic Chinese
        # label deliberately exists on both fields to exercise true ambiguity.
        metadata['fields'][3]['aliases'].append('渠道')
        resolved = resolve_dimension('渠道', metadata, access_scope=scope(), datasource_id='fixture_aug')
        self.assertEqual(resolved['status'], 'technical_candidates')
        self.assertEqual({f['sql_name'] for f in resolved['candidates']},
                         {'sessions.source_channel', 'order_items.channel'})
        prepared = prepare([{'dimensions': ['渠道']}], metadata)
        self.assertIsNone(prepared['clarification'])
        self.assertEqual(prepared['normalized_requests'][0]['dimensions'], ['渠道'])
        self.assertEqual(prepared['missing_requests'][0]['kind'], 'technical_dimension_mapping')

    def test_unique_authoritative_sku_key_and_explicit_foreign_key(self):
        generic = resolve_dimension('sku_id', graph(), access_scope=scope(), datasource_id='fixture_aug')
        specific = resolve_dimension('order_items.sku_id', graph(), access_scope=scope(), datasource_id='fixture_aug')
        self.assertEqual(generic['field'], 'sku_catalog.sku_id')
        self.assertEqual(specific['field'], 'order_items.sku_id')

    def test_two_authoritative_identifiers_are_not_chosen_arbitrarily(self):
        metadata = graph()
        metadata['fields'][5]['role'] = 'identifier'
        resolved = resolve_dimension('sku_id', metadata, access_scope=scope(), datasource_id='fixture_aug')
        self.assertEqual(resolved['status'], 'technical_candidates')

    def test_time_output_labels_do_not_require_fake_physical_columns(self):
        for term in ('month_str', 'order_month_label', 'display_quarter_key', 'period', '月份'):
            with self.subTest(term=term):
                self.assertTrue(is_time_grain_term(term))
                self.assertEqual(resolve_dimension(term, graph())['status'], 'time_grain')
        result = prepare([{'dimensions': ['month_str', 'period', 'channel']}])
        self.assertEqual(result['normalized_requests'][0]['dimensions'],
                         ['month_str', 'period', 'sessions.source_channel'])
        self.assertEqual(result['missing_requests'], [])
        self.assertNotIn('sessions.started_at', result['requested_fields'])
        self.assertFalse(is_time_grain_term("month'; DROP TABLE sessions"))

    def test_exact_time_named_field_takes_precedence_over_grouping_hint(self):
        metadata = graph()
        metadata['fields'].append({'id': 'sessions.period', 'table_id': 'sessions', 'name': 'period'})
        self.assertEqual(resolve_dimension('period', metadata)['field'], 'sessions.period')

    def test_unrecognized_dimension_stays_explicit(self):
        result = prepare([{'dimensions': ['not_a_registered_dimension']}])
        self.assertEqual(result['normalized_requests'][0]['dimensions'], ['not_a_registered_dimension'])
        self.assertEqual(result['resolutions'][0]['status'], 'unresolved')
        self.assertIsNone(result['clarification'])

    def test_other_source_and_denied_table_never_become_candidates(self):
        resolved = resolve_dimension('channel', graph(), access_scope=scope(), datasource_id='fixture_aug')
        self.assertNotIn('future_secret', str(resolved))
        denied = resolve_dimension('channel', graph(), access_scope=scope(('catalog',)), datasource_id='fixture_aug')
        self.assertEqual(denied['status'], 'unresolved')
        denied_source = resolve_dimension('channel', graph(), access_scope=scope(), datasource_id='fixture_sep')
        self.assertEqual(denied_source['status'], 'unresolved')

    def test_physical_identity_is_derived_from_authorized_table_membership(self):
        metadata = graph()
        metadata['fields'][3]['sql_name'] = 'future_sessions.secret'
        resolved = resolve_dimension('channel', metadata, access_scope=scope(), datasource_id='fixture_aug')
        self.assertEqual(resolved['field'], 'sessions.source_channel')
        self.assertNotIn('future_sessions', str(resolved))
        metadata['fields'][3]['datasource_id'] = 'fixture_sep'
        self.assertEqual(resolve_dimension('channel', metadata, access_scope=scope(),
                                          datasource_id='fixture_aug')['status'], 'unresolved')

    def test_measurement_context_uses_same_mapping_and_keeps_candidate_metadata(self):
        result = resolve_query_requirements([{'dimensions': ['sessions.channel', 'order_items.category', 'month_str'],
            'conditions': {'time_range': {'start': '2026-07-01'}, 'store_id': 'HN-DE-01'}}], graph(), [])
        self.assertEqual(result['unresolved_requirements'], [])
        self.assertEqual(set(result['requested_fields']),
                         {'sessions.source_channel', 'sku_catalog.category_code', 'sku_catalog.store_id'})


class EntityValueResolutionTest(unittest.TestCase):
    def test_unknown_store_name_is_not_mapped_to_only_real_store(self):
        requests = [{'query_id': 'Q1', 'goal': '调查A店', 'conditions': {'shop_name': 'A店'}}]
        original = deepcopy(requests)
        result = prepare(requests)
        self.assertEqual(result['normalized_requests'], original)
        self.assertEqual(requests, original)
        self.assertEqual(result['clarification']['category'], 'entity_identity')
        self.assertEqual(result['clarification']['query_id'], 'Q1')
        self.assertEqual(result['clarification']['options'][0]['id'], 'HN-DE-01')
        self.assertFalse(result['clarification']['options'][0]['recommended'])

    def test_actual_value_is_bound_and_original_and_provenance_are_kept(self):
        result = prepare([{'query_id': 'Q1', 'conditions': {'品类': 'power'}}])
        self.assertEqual(result['normalized_requests'][0]['conditions'], {'sku_catalog.category_code': 'POWER'})
        record = result['resolutions'][0]
        self.assertEqual(record['original_condition'], '品类')
        self.assertEqual(record['original_value'], 'power')
        self.assertEqual(record['canonical_value'], 'POWER')
        self.assertEqual(record['basis'], 'exact_available_entity_value')
        self.assertEqual(result['requested_fields'], ['sku_catalog.category_code'])
        self.assertIsNone(result['clarification'])

    def test_equality_and_membership_shapes_are_preserved(self):
        requests = [{'conditions': {'category': {'operator': 'in', 'values': ['power', 'HOME']}}},
                    {'conditions': {'category': {'operator': '=', 'value': 'POWER'}}}]
        result = prepare(requests)
        self.assertEqual(result['normalized_requests'][0]['conditions']['sku_catalog.category_code'],
                         {'operator': 'in', 'values': ['POWER', 'HOME']})
        self.assertEqual(result['normalized_requests'][1]['conditions']['sku_catalog.category_code'],
                         {'operator': '=', 'value': 'POWER'})

    def test_non_equality_range_is_not_mistaken_for_an_entity_name(self):
        requests = [{'conditions': {'category': {'operator': '!=', 'value': 'POWER'}}}]
        self.assertEqual(prepare(requests)['normalized_requests'], requests)

    def test_lookup_uses_only_authorized_field_and_requested_values_as_data(self):
        calls = []
        def lookup(field_info, *, limit):
            calls.append((field_info, limit))
            return {'values': ['HN-DE-01'], 'complete': True}
        metadata = graph()
        metadata['fields'][2].pop('enum_values')
        attack = "A店'; DROP TABLE orders; --"
        result = prepare([{'conditions': {'shop_name': attack}}], metadata, lookup_values=lookup)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 100)
        info = calls[0][0]
        self.assertEqual(info['requested_values'], [attack])
        self.assertEqual(info['sql_name'], 'sku_catalog.store_id')
        self.assertEqual(info['datasource_id'], 'fixture_aug')
        self.assertNotIn('sql', info)
        self.assertEqual(result['normalized_requests'][0]['conditions'], {'shop_name': attack})
        self.assertEqual(result['clarification']['category'], 'entity_identity')

    def test_no_lookup_for_denied_field_or_other_datasource(self):
        calls = []
        result = prepare([{'conditions': {'shop_name': 'A店'}}], access_scope=scope(('sessions',)),
                         lookup_values=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])
        self.assertIsNone(result['clarification'])
        self.assertEqual(result['missing_requests'][0]['kind'], 'technical_entity_mapping')

    def test_samples_are_not_a_complete_catalog(self):
        metadata = graph()
        metadata['fields'][2]['samples'] = metadata['fields'][2].pop('enum_values')
        result = prepare([{'conditions': {'store_id': 'A店'}}], metadata)
        self.assertIsNone(result['clarification'])
        self.assertEqual(result['resolutions'][0]['status'], 'lookup_required')
        self.assertEqual(result['missing_requests'][0]['kind'], 'entity_values_required')

    def test_truncated_or_bare_catalog_does_not_claim_missing_entity(self):
        for response in ({'values': ['HN-DE-01'], 'complete': False}, ['HN-DE-01']):
            with self.subTest(response=response):
                result = prepare([{'conditions': {'shop_name': 'A店'}}],
                                 lookup_values=lambda *args, **kwargs: response)
                self.assertIsNone(result['clarification'])
                self.assertEqual(result['missing_requests'][0]['kind'], 'entity_values_required')

    def test_real_lookup_value_is_not_replaced_with_metadata_sample(self):
        result = prepare([{'conditions': {'store_id': 'ACTUAL-02'}}],
            lookup_values=lambda *args, **kwargs: {'values': ['ACTUAL-02'], 'complete': False})
        self.assertEqual(result['normalized_requests'][0]['conditions'], {'sku_catalog.store_id': 'ACTUAL-02'})

    def test_ambiguous_casefold_values_are_not_silently_merged(self):
        metadata = graph()
        metadata['fields'][1]['enum_values'] = ['POWER', 'power']
        result = prepare([{'conditions': {'category': 'Power'}}], metadata)
        self.assertEqual(result['resolutions'][0]['status'], 'needs_clarification')
        self.assertIsNotNone(result['clarification'])

    def test_lookup_duplicates_deduplicate_without_false_ambiguity(self):
        result = prepare([{'conditions': {'store_id': 'ACTUAL-02'}}],
            lookup_values=lambda *args, **kwargs: {'values': ['ACTUAL-02', 'ACTUAL-02'], 'complete': True})
        self.assertEqual(result['resolutions'][0]['status'], 'resolved')

    def test_lookup_cache_stays_local_and_does_not_cross_requests_or_users(self):
        calls = []
        def lookup(info, *, limit):
            calls.append(info)
            return {'values': ['A店'], 'complete': True}
        request = {'conditions': {'shop_name': 'A店'}}
        prepare([deepcopy(request), deepcopy(request)], lookup_values=lookup)
        self.assertEqual(len(calls), 1)
        prepare([deepcopy(request)], lookup_values=lookup)
        self.assertEqual(len(calls), 2)

    def test_invalid_lookup_response_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, '真实values数组'):
            prepare([{'conditions': {'shop_name': 'A店'}}], lookup_values=lambda *args, **kwargs: {'sql': 'SELECT 1'})

    def test_entity_numeric_zero_is_a_valid_exact_identifier(self):
        metadata = graph()
        metadata['fields'][2]['enum_values'] = [0, 1]
        result = prepare([{'conditions': {'store_id': 0}}], metadata)
        self.assertEqual(result['normalized_requests'][0]['conditions'], {'sku_catalog.store_id': 0})

    def test_conflicting_conditions_are_not_silently_overwritten(self):
        metadata = graph()
        metadata['fields'][2]['enum_values'].append('OTHER-02')
        requests = [{'conditions': {'store_id': 'HN-DE-01', 'sku_catalog.store_id': 'OTHER-02'}}]
        result = prepare(requests, metadata)
        self.assertEqual(result['clarification']['category'], 'entity_identity')
        self.assertEqual(result['normalized_requests'], requests)
        self.assertEqual(result['missing_requests'][0]['kind'], 'conflicting_entity_conditions')

    def test_channel_scope_uses_actual_channel_values(self):
        result = prepare([{'conditions': {'source_channel': 'direct'}}])
        self.assertEqual(result['normalized_requests'][0]['conditions'], {'sessions.source_channel': 'direct'})
        self.assertEqual(result['resolutions'][0]['canonical_field'], 'sessions.source_channel')


if __name__ == '__main__':
    unittest.main()
