"""Metadata and dependency regressions for shared, deterministic SQL inputs."""
from copy import deepcopy
import unittest

from app.database import SCHEMA, physical_table_name
from app.querying.query_context import assemble_query_context, select_evidence_ids, select_query_graph
from app.querying.query_protocol import sql_facing_graph
from app.retrieval.graph import SchemaGraphBuilder


def field(table, name, **extra):
    return {'id': f'{table}.{name}', 'table_id': table, 'table_name': table,
            'name': name, 'sql_name': f'{table}.{name}', 'type': 'INTEGER', **extra}


def bridge_graph():
    return {'database': 'test', 'snapshot_id': 'aug31', 'graph_version': 'v1',
            'tables': [{'id': t, 'name': t, 'database': 'test'} for t in ('a', 'bridge', 'c', 'unrelated')],
            'fields': [field('a', 'amount', unit='EUR'), field('a', 'key'), field('bridge', 'a_key'),
                       field('bridge', 'c_key'), field('c', 'key'), field('c', 'name'),
                       field('unrelated', 'value')],
            'joins': [{'left_table': 'a', 'left_field': 'key', 'right_table': 'bridge', 'right_field': 'a_key'},
                      {'left_table': 'bridge', 'left_field': 'c_key', 'right_table': 'c', 'right_field': 'key'}]}


class SharedQueryContextTest(unittest.TestCase):
    def test_selected_model_graph_preserves_declared_grain_keys_and_join_risks(self):
        source = bridge_graph()
        source['tables'][0].update(primary_key=['key'], grain='one payment attempt')
        source['tables'][1].update(primary_key=['a_key', 'c_key'], grain='one item in an order')
        source['joins'][0].update(cardinality='1:N', cardinality_basis='declared_logical_contract',
                                  duplicate_risk='Parent facts can repeat after this join.')
        scope = {'user_id': 'u', 'allowed_databases': ['test'], 'allowed_tables': ['a', 'bridge', 'c']}
        bundle = assemble_query_context(source, access_scope=scope, requested_fields=['a.amount', 'c.name'])
        graph = bundle['schema_graph']
        tables = {table['name']: table for table in graph['tables']}
        self.assertEqual(set(tables), {'a', 'bridge', 'c'})
        self.assertEqual(tables['a']['primary_key'], ['key'])
        self.assertEqual(tables['a']['grain'], 'one payment attempt')
        self.assertEqual(tables['bridge']['primary_key'], ['a_key', 'c_key'])
        declared, unknown = graph['joins']
        self.assertEqual((declared['left'], declared['right']), ('a.key', 'bridge.a_key'))
        for key in ('cardinality', 'cardinality_basis', 'duplicate_risk'):
            self.assertEqual(declared[key], source['joins'][0][key])
            self.assertNotIn(key, unknown, 'Unknown relationship facts must not be invented')
        self.assertNotIn('primary_key', tables['c'])
        self.assertNotIn('unrelated.value', str(graph))

    def test_bridge_table_keys_and_full_path_survive_relevance_selection(self):
        source = bridge_graph()
        selected = select_query_graph(source, requested_fields=['a.amount', 'c.name'])
        self.assertEqual({t['id'] for t in selected['tables']}, {'a', 'bridge', 'c'})
        self.assertEqual(len(selected['joins']), 2)
        self.assertEqual({f['sql_name'] for f in selected['fields']}, {
            'a.amount', 'a.key', 'bridge.a_key', 'bridge.c_key', 'c.key', 'c.name'})
        self.assertEqual(len(source['tables']), 4, 'Selection does not mutate the shared pool')

    def test_first_and_later_partial_retrieval_deduplicate_and_preserve_values(self):
        graph = bridge_graph()
        graph['fields'] += [field('a', 'amount', samples=[12], profile='合法金额', nullable=False)]
        retrieval = {'hits': [{'doc_id': 'a.amount', 'database_id': 'test', 'table_id': 'a',
                              'field_name': 'amount', 'samples': [12, 20], 'profile': '合法金额'},
                             {'doc_id': 'c.name', 'database_id': 'test', 'table_id': 'c',
                              'field_name': 'name', 'samples': ['Store A'], 'profile': '空值率0%'}]}
        bundle = assemble_query_context(graph, retrieval=retrieval)
        amounts = [f for f in bundle['schema_graph']['fields'] if f['sql_name'] == 'a.amount']
        self.assertEqual(len(amounts), 1)
        self.assertEqual(amounts[0]['samples'], [12, 20])
        self.assertEqual(amounts[0]['profile'], '合法金额')
        self.assertEqual(amounts[0]['unit'], 'EUR')
        self.assertIn('unrelated.value', bundle['manifest']['field_ids'])
        self.assertEqual(bundle['conflicts'], [])
        self.assertEqual(bundle, assemble_query_context(graph, retrieval=retrieval), 'Assembly is deterministic')

    def test_conflicting_versions_or_units_are_not_silently_merged(self):
        graph = bridge_graph()
        graph['fields'][0].update(version='v1', samples=[10])
        graph['fields'].append(field('a', 'amount', unit='cents', version='v2', samples=[1000]))
        result = assemble_query_context(graph)
        amount = next(f for f in result['schema_graph']['fields'] if f['sql_name'] == 'a.amount')
        self.assertEqual(amount['version'], 'v1')
        self.assertEqual(amount['samples'], [10])
        self.assertEqual(result['conflicts'][0]['attributes'], ['unit', 'version'])

    def test_metric_dependency_closure_and_missing_fields_are_explicit(self):
        metrics = [{'id': 'gmv', 'required_fields': [{'table': 'a', 'field': 'amount'}]},
                   {'id': 'rate', 'depends_on': ['gmv'], 'formula': 'a.amount / c.key'}]
        result = assemble_query_context(bridge_graph(), metrics=metrics, metric_ids=['rate'], requested_fields=[])
        self.assertEqual(result['manifest']['metric_ids'], ['gmv', 'rate'])
        self.assertEqual(result['missing_requests'], [])
        self.assertEqual(result['manifest']['join_count'], 2)
        result = assemble_query_context(bridge_graph(), requested_fields=['a.not_retrieved'])
        self.assertEqual(result['missing_requests'][0]['kind'], 'field')
        self.assertEqual(result['internal_schema_graph']['fields'], [])

    def test_scope_prevents_foreign_metadata_and_forbidden_bridge(self):
        graph = bridge_graph()
        graph['tables'].append({'id': 'future', 'name': 'future', 'database': 'different_snapshot'})
        graph['fields'].append(field('future', 'secret'))
        scope = {'user_id': 'u', 'allowed_databases': ['test'], 'allowed_tables': ['a', 'c']}
        selected = assemble_query_context(graph, access_scope=scope,
                                          requested_fields=['a.amount', 'c.name'])
        self.assertEqual(selected['manifest']['table_ids'], ['a', 'c'])
        self.assertEqual(selected['manifest']['join_count'], 0)
        self.assertNotIn('future.secret', str(selected))
        self.assertNotIn('bridge.a_key', str(selected))

    def test_independent_query_table_does_not_break_another_connected_pair(self):
        graph = bridge_graph()
        # The disconnected table sorts before the connected A / C pair.
        graph['tables'].append({'id': '0_independent', 'name': '0_independent', 'database': 'test'})
        graph['fields'].append(field('0_independent', 'count'))
        result = assemble_query_context(graph, requested_fields=['0_independent.count', 'a.amount', 'c.name'])
        self.assertEqual(result['manifest']['join_count'], 2)
        self.assertIn('bridge', result['manifest']['table_ids'])

    def test_explicit_other_datasource_metrics_are_not_injected(self):
        metrics = [{'id': 'local', 'database_id': 'test', 'required_fields': ['a.amount']},
                   {'id': 'future', 'datasource_id': 'different_snapshot', 'definition': 'future facts'}]
        result = assemble_query_context(bridge_graph(), metrics=metrics)
        self.assertEqual(result['manifest']['metric_ids'], ['local'])

    def test_early_evidence_facts_and_transitive_dependencies_are_kept(self):
        evidence = [{'id': f'T:R{i}', 'check_status': 'basic_valid'} for i in range(1, 7)]
        evidence[3]['input_result_ids'] = ['T:R2']
        evidence[1]['input_result_ids'] = ['T:R1']
        evidence[2]['check_status'] = 'superseded'
        selected = select_evidence_ids(evidence,
            progress={'facts': [{'text': '早期金额', 'evidence_ids': ['T:R1']}],
                      'hypotheses': [{'evidence_ids': ['T:R3'], 'status': 'invalid'}]},
            requests=[{'input_result_ids': ['T:R4']}])
        self.assertEqual(selected, ['T:R1', 'T:R2', 'T:R4', 'T:R5', 'T:R6'])
        self.assertEqual(select_evidence_ids(evidence, recent=0), [])
        self.assertEqual(select_evidence_ids(evidence, recent=0,
                         observation={'queries': [{'result': {'id': 'T:R1'}}]}), ['T:R1'])

    def test_schema_builder_carries_samples_and_profile_into_single_graph(self):
        table = next(t for t in SCHEMA if t.get('database') == 'ecommerce_ops'
                     and physical_table_name(t) == 'orders')
        definition = next(f for f in table['fields'] if f['name'] == 'order_status')
        hit = {'doc_id': table['id']+'.order_status', 'database_id': 'ecommerce_ops',
               'table_id': table['id'], 'field_name': 'order_status',
               'field_label': definition.get('label'), 'field_type': definition['type'],
               'samples': ['paid', 'cancelled'], 'profile': '不同值2个', 'schema_version': 'public-v1'}
        graph = SchemaGraphBuilder().build([hit])
        model_field = sql_facing_graph(graph)['fields'][0]
        self.assertEqual(model_field['samples'], ['paid', 'cancelled'])
        self.assertEqual(model_field['profile'], '不同值2个')
        self.assertEqual(model_field['schema_version'], 'public-v1')
        if definition.get('data_profile'):
            self.assertEqual(model_field['data_profile'], definition['data_profile'])


if __name__ == '__main__':
    unittest.main()
