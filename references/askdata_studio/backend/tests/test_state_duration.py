import copy
import random
import unittest
from datetime import datetime, timedelta, timezone

from app.querying.calculations import CalculationError, execute_calculation


def request():
    return {'operation': 'state_duration', 'result_id': 'T:R1', 'partition_by': ['item', 'warehouse'],
            'time_field': 'at', 'order_field': 'seq', 'delta_terms': [{'column': 'delta', 'coefficient': 1}],
            'opening_field': 'kind', 'opening_value': 'opening', 'timezone_name': 'UTC',
            'periods': [{'label': 'before', 'start': '2026-06-01', 'end': '2026-07-01'},
                        {'label': 'after', 'start': '2026-07-01', 'end': '2026-08-01'}]}


def event(at, seq, delta, kind='change', item='A', warehouse='W1'):
    return dict(at=at, seq=seq, delta=delta, kind=kind, item=item, warehouse=warehouse)


def source(rows):
    return {'sql': 'SELECT item, warehouse, at, seq, delta, kind FROM ledger',
            'columns': ['item', 'warehouse', 'at', 'seq', 'delta', 'kind'], 'rows': rows,
            'complete': True, 'total_rows': len(rows), 'observation_cutoff_at': '2026-09-01T00:00:00Z'}


class StateDurationTest(unittest.TestCase):
    def calculate(self, rows, **changes):
        params = request(); params.update(changes)
        return execute_calculation(params, source(rows))

    def assert_code(self, code, fn):
        with self.assertRaises(CalculationError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_cross_month_interval_and_separate_opening_ending(self):
        result = self.calculate([event('2026-05-01Z'.replace('Z', 'T00:00:00Z'), 1, 5, 'opening'),
            event('2026-06-30T12:00:00Z', 2, -5), event('2026-07-02T12:00:00Z', 3, 2)])
        before, after = result['rows']
        self.assertEqual([before['duration_hours'], after['duration_hours']], [12, 36])
        self.assertEqual([before['opening_state'], before['ending_state'], after['opening_state'], after['ending_state']], [5, 0, 0, 2])
        self.assertEqual([before['episode_count'], after['episode_count']], [1, 1])
        self.assertFalse(result['column_metadata']['duration_hours']['additive'])

    def test_positive_replenishment_ends_interval_and_zero_events_do_not_count_as_episodes(self):
        result = self.calculate([event('2026-05-01', 1, 3, 'opening'), event('2026-07-02', 2, -3),
            event('2026-07-03', 3, 0), event('2026-07-04', 4, 2), event('2026-07-06', 5, -2), event('2026-07-07', 6, 1)])
        self.assertEqual(result['rows'][1]['duration_hours'], 72)
        self.assertEqual(result['rows'][1]['episode_count'], 2)

    def test_same_time_stable_sequence_and_input_order_do_not_change_result(self):
        rows = [event('2026-05-01', 1, 1, 'opening'), event('2026-07-02', 3, 1), event('2026-07-02', 2, -1)]
        a = self.calculate(rows); b = self.calculate(list(reversed(rows)))
        self.assertEqual(a['rows'], b['rows'])
        self.assertEqual(a['rows'][1]['duration_hours'], 0)
        self.assertEqual(a['rows'][1]['episode_count'], 0)

    def test_partitions_are_not_combined(self):
        result = self.calculate([event('2026-05-01', 1, 0, 'opening'),
                                event('2026-05-01', 1, 10, 'opening', warehouse='W2')])
        self.assertEqual([r['duration_hours'] for r in result['rows']], [720, 744, 0, 0])

    def test_reserved_deltas_are_subtracted_and_negative_balances_count(self):
        rows = [event('2026-05-01', 1, 3, 'opening'), event('2026-07-01', 2, 0)]
        for row, reserved in zip(rows, [0, 4]): row['reserved'] = reserved
        data = source(rows); data['columns'].append('reserved'); data['sql'] = 'SELECT * FROM ledger'
        params = request(); params['delta_terms'].append({'column': 'reserved', 'coefficient': -1})
        result = execute_calculation(params, data)
        self.assertEqual(result['rows'][1]['opening_state'], -1)
        self.assertEqual(result['rows'][1]['duration_hours'], 744)

    def test_dst_elapsed_time_uses_utc_not_wall_clock(self):
        result = self.calculate([event('2026-01-01', 1, 0, 'opening')], timezone_name='Europe/Berlin', periods=[
            {'label': 'spring', 'start': '2026-03-29', 'end': '2026-03-30'}])
        self.assertEqual(result['rows'][0]['duration_hours'], 23)

    def test_naive_ambiguous_timestamp_is_not_guessed(self):
        data = source([event('2026-01-01', 1, 1, 'opening'), event('2026-10-25T02:30:00', 2, -1)])
        data['observation_cutoff_at'] = '2026-11-01T00:00:00Z'
        params = request(); params['timezone_name'] = 'Europe/Berlin'
        self.assert_code('invalid_ledger', lambda: execute_calculation(params, data))

    def test_missing_opening_null_delta_and_duplicate_order_have_distinct_errors(self):
        for rows, code in [([event('2026-05-01', 1, 1)], 'missing_opening_state'),
            ([event('2026-07-01', 1, 1, 'opening')], 'missing_opening_state'),
            ([event('2026-05-01', 1, None, 'opening')], 'invalid_ledger'),
            ([event('2026-05-01', 1, 1, 'opening'), event('2026-05-01', 1, -1)], 'ambiguous_event_order')]:
            with self.subTest(code=code): self.assert_code(code, lambda: self.calculate(rows))

    def test_cutoff_cannot_be_extended_by_model_parameters(self):
        data = source([event('2026-05-01', 1, 0, 'opening')]); data.pop('observation_cutoff_at')
        self.assert_code('missing_observation_boundary', lambda: execute_calculation(request(), data))
        self.assert_code('invalid_period', lambda: self.calculate([event('2026-05-01', 1, 0, 'opening')],
            periods=[{'label':'future','start':'2026-09-01','end':'2026-10-01'}]))

    def test_filtered_time_or_state_history_is_not_mistaken_for_full_population(self):
        data = source([event('2026-05-01', 1, 0, 'opening')])
        for where in ["at >= '2026-06-01'", "at < '2026-07-15'", "delta <= 0", "random() > 0.5"]:
            with self.subTest(where=where):
                data['sql'] = 'SELECT * FROM ledger WHERE ' + where
                self.assert_code('ledger_history_filtered', lambda: execute_calculation(request(), data))
        data['sql'] = "SELECT * FROM ledger WHERE item IN ('A') AND warehouse = 'W1'"
        self.assertEqual(execute_calculation(request(), data)['rows'][0]['duration_hours'], 720)

    def test_entity_catalog_semijoin_selects_whole_partitions_but_cannot_filter_events(self):
        data = source([event('2026-05-01', 1, 0, 'opening')])
        data['sql'] = "SELECT l.* FROM ledger l WHERE l.item IN (SELECT item FROM catalog WHERE category = 'C')"
        self.assertEqual(execute_calculation(request(), data)['rows'][0]['duration_hours'], 720)
        for condition in ["l.at >= '2026-06-01'", "delta < 0", "random() > 0.5"]:
            data['sql'] = 'SELECT l.* FROM ledger l WHERE l.item IN (SELECT item FROM catalog WHERE ' + condition + ')'
            self.assert_code('ledger_history_filtered', lambda: execute_calculation(request(), data))

    def test_aggregated_joined_sampled_or_limited_source_is_not_raw(self):
        data = source([event('2026-05-01', 1, 0, 'opening')])
        for sql in ['SELECT * FROM ledger LIMIT 1', 'SELECT DISTINCT * FROM ledger',
                    'SELECT * FROM ledger JOIN other USING(item)', 'SELECT * FROM ledger TABLESAMPLE 10%',
                    'SELECT item, SUM(delta) AS delta FROM ledger GROUP BY item']:
            with self.subTest(sql=sql):
                data['sql'] = sql
                self.assert_code('ledger_source_not_raw', lambda: execute_calculation(request(), data))

    def test_transparent_cte_is_equivalent_to_raw_projection(self):
        data = source([event('2026-05-01', 1, 0, 'opening')])
        data['sql'] = 'WITH h AS (SELECT item,warehouse,at,seq,delta,kind FROM ledger) SELECT item,warehouse,at,seq,delta,kind FROM h'
        self.assertEqual(execute_calculation(request(), data)['rows'][0]['duration_hours'], 720)
        data['sql'] = data['sql'].replace('FROM ledger)', "FROM ledger WHERE delta<0)")
        self.assert_code('ledger_history_filtered', lambda: execute_calculation(request(), data))

    def test_upper_bound_after_report_preserves_opening_and_intervals(self):
        data=source([event('2026-05-01',1,0,'opening')])
        expected=execute_calculation(request(),data)['rows']
        data['sql'] += " WHERE at < CAST('2026-08-01T00:00:00Z' AS TIMESTAMPTZ)"
        self.assertEqual(execute_calculation(request(),data)['rows'],expected)

    def test_nested_qualified_lookup_ctes_with_safe_upper_bound(self):
        data=self.lookup_source()
        data['schema_graph']['fields'] += [{'table_id':'catalog','name':'item'}, {'table_id':'catalog','name':'category'}]
        data['sql'] = """WITH selected AS (SELECT item FROM catalog WHERE category='C'),
        history AS (SELECT l.item,l.warehouse,l.at,l.seq,l.delta,l.kind FROM ledger l
        JOIN selected c ON l.item=c.item WHERE l.at<'2026-08-01')
        SELECT item,warehouse,at,seq,delta,kind FROM history ORDER BY item,warehouse,at,seq"""
        self.assertEqual(execute_calculation(request(),data)['rows'][0]['duration_hours'],720)

    def lookup_source(self):
        data=source([event('2026-05-01',1,0,'opening')])
        data['schema_graph']={'tables':[{'id':'ledger','name':'ledger','primary_key':['seq']},
            {'id':'catalog','name':'catalog','primary_key':['item']}],
            'fields':[{'table_id':'ledger','name':f} for f in data['columns']]}
        data['sql']="SELECT l.* FROM ledger l JOIN catalog c ON l.item=c.item WHERE c.category='C'"
        return data

    def test_unique_partition_dimension_lookup_preserves_all_events(self):
        data=self.lookup_source()
        self.assertEqual(execute_calculation(request(),data)['rows'][0]['duration_hours'],720)
        data['sql']=data['sql'].replace(" WHERE c.category='C'"," AND c.category='C'")
        self.assertEqual(execute_calculation(request(),data)['rows'][0]['duration_hours'],720)
        data['sql']=data['sql'].replace('JOIN','LEFT JOIN')
        self.assertEqual(execute_calculation(request(),data)['rows'][0]['duration_hours'],720)

    def test_dimension_join_requires_registered_complete_unique_key(self):
        data=self.lookup_source();data['schema_graph']['tables'][1]['primary_key']=['item','version']
        self.assert_code('ledger_source_not_raw',lambda:execute_calculation(request(),data))
        data=self.lookup_source();data['schema_graph']={}
        self.assert_code('ledger_source_not_raw',lambda:execute_calculation(request(),data))

    def labelled_lookup_source(self):
        data = self.lookup_source()
        data['schema_graph']['fields'] += [{'table_id': 'catalog', 'name': 'item'},
                                          {'table_id': 'catalog', 'name': 'category'}]
        data['sql'] = 'SELECT l.*, c.category AS category FROM ledger l JOIN catalog c ON l.item=c.item'
        data['columns'].append('category')
        for row in data['rows']:
            row['category'] = 'C'
        params = request(); params['partition_by'].append('category')
        return params, data

    def test_fixed_unique_dimension_label_is_retained_without_changing_duration(self):
        params, data = self.labelled_lookup_source()
        labelled = execute_calculation(params, data)
        base = execute_calculation(request(), self.lookup_source())
        self.assertEqual([{k: v for k, v in row.items() if k != 'category'} for row in labelled['rows']], base['rows'])
        self.assertEqual({row['category'] for row in labelled['rows']}, {'C'})

    def test_dimension_label_rejects_nonunique_nonpartition_unknown_and_mutable_sources(self):
        params, data = self.labelled_lookup_source()
        data['schema_graph']['tables'][1]['primary_key'] = ['item', 'version']
        self.assert_code('ledger_source_not_raw', lambda: execute_calculation(params, data))
        params, data = self.labelled_lookup_source()
        data['sql'] = data['sql'].replace('l.item=c.item', 'l.seq=c.item')
        self.assert_code('ledger_history_filtered', lambda: execute_calculation(params, data))
        params, data = self.labelled_lookup_source()
        data['schema_graph']['fields'] = [f for f in data['schema_graph']['fields'] if f.get('name') != 'category']
        self.assert_code('ledger_source_not_raw', lambda: execute_calculation(params, data))
        params, data = self.labelled_lookup_source()
        data['rows'].append({**event('2026-06-30T12:00:00Z', 2, 1), 'category': 'CHANGED'})
        data['total_rows'] = len(data['rows'])
        self.assert_code('ledger_partition_label_changed', lambda: execute_calculation(params, data))
        params, data = self.labelled_lookup_source()
        data['rows'].append(copy.deepcopy(data['rows'][0]))
        data['total_rows'] = len(data['rows'])
        self.assert_code('missing_opening_state', lambda: execute_calculation(params, data))

    def test_stable_dimension_attribute_cannot_replace_raw_state_or_time_input(self):
        _, data = self.labelled_lookup_source()
        for change in ({'delta_terms': [{'column': 'category', 'coefficient': 1}]}, {'time_field': 'category'}):
            params = request(); params.update(change)
            self.assert_code('ledger_source_not_raw', lambda: execute_calculation(params, data))

    def test_lookup_cannot_drop_history_by_event_time_or_delta(self):
        for suffix in [" AND l.at>='2026-06-01'",' AND l.delta<0']:
            data=self.lookup_source();data['sql']+=suffix
            self.assert_code('ledger_history_filtered',lambda:execute_calculation(request(),data))
        data=self.lookup_source();data['sql']=data['sql'].replace('l.item=c.item','l.seq=c.item')
        self.assert_code('ledger_history_filtered',lambda:execute_calculation(request(),data))

    def test_unused_window_column_allowed_but_not_as_a_raw_input(self):
        data=source([event('2026-05-01',1,0,'opening')]);data['columns'].append('rn');data['rows'][0]['rn']=1
        data['sql']='SELECT item,warehouse,at,seq,delta,kind,ROW_NUMBER() OVER(ORDER BY seq) AS rn FROM ledger'
        self.assertEqual(execute_calculation(request(),data)['rows'][0]['duration_hours'],720)
        params=request();params['opening_field']='rn';params['opening_value']='1'
        self.assert_code('ledger_source_not_raw',lambda:execute_calculation(params,data))

    def test_preview_or_empty_source_is_not_zero_duration_fact(self):
        data = source([event('2026-05-01', 1, 0, 'opening')]); data['total_rows'] = 2
        self.assert_code('incomplete_result', lambda: execute_calculation(request(), data))
        empty = self.calculate([])
        self.assertFalse(empty['calculation_complete']); self.assertEqual(empty['rows'], [])

    def test_random_ledgers_match_independent_minute_grid(self):
        rng = random.Random(42)
        for trial in range(20):
            start = datetime(2026, 6, 1, tzinfo=timezone.utc)
            values = [rng.randint(-2, 5) for _ in range(61)]
            rows = [event(start.isoformat(), 0, values[0], 'opening')]
            for minute in range(1, 61):
                rows.append(event((start + timedelta(minutes=minute)).isoformat(), minute, values[minute] - values[minute-1]))
            rng.shuffle(rows)
            result = self.calculate(rows, periods=[{'label': 'hour', 'start': start.isoformat(),
                'end': (start + timedelta(hours=1)).isoformat()}])['rows'][0]
            self.assertEqual(result['duration_seconds'], sum(v <= 0 for v in values[:60]) * 60)
            self.assertEqual(result['episode_count'], sum(v <= 0 and (i == 0 or values[i-1] > 0) for i,v in enumerate(values[:60])))


if __name__ == '__main__': unittest.main()
