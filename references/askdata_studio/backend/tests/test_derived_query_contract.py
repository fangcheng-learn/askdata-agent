"""SQL lineage regressions use compilation only, with no provider calls."""
from copy import deepcopy
import unittest

from app.database import SCHEMA, physical_table_name
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.querying.query_protocol import complete_contract, model_contract_schema
from app.security import AccessController


SOURCE = 'ecommerce_ops'
SQL = """WITH a AS (
    SELECT strftime(stat_date, '%Y-%m') AS period, SUM(gmv) AS value
    FROM daily_store_metrics
    WHERE stat_date >= '2026-06-01' AND stat_date < '2026-08-01' GROUP BY 1
), b AS (
    SELECT strftime(ordered_at, '%Y-%m') AS period, SUM(paid_amount) AS paid
    FROM orders
    WHERE ordered_at >= '2026-06-01' AND ordered_at < '2026-08-01' GROUP BY 1
)
SELECT COALESCE(a.period,b.period) AS period, a.value, b.paid
FROM a FULL OUTER JOIN b ON a.period=b.period"""


def metric(field='daily_store_metrics.gmv'):
    return {'name': field.split('.')[-1], 'source_fields': [field], 'aggregation': 'sum'}


def graph():
    tables = [item for item in SCHEMA if item.get('database') == SOURCE
              and physical_table_name(item) in {'orders', 'daily_store_metrics'}]
    return {'database': SOURCE,
            'tables': [{'id': table['id'], 'name': physical_table_name(table), 'database': SOURCE} for table in tables],
            'fields': [{**field, 'sql_name': physical_table_name(table)+'.'+field['name']}
                       for table in tables for field in table['fields']], 'joins': []}


class DerivedQueryContractTest(unittest.TestCase):
    def test_correlated_lineage_obeys_alias_visibility_and_shadowing(self):
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import traverse_scope
        from app.querying.query_protocol import _SqlLineage
        cases = [
            ('SELECT (SELECT d.gmv FROM daily_store_metrics d WHERE d.store_id=o.store_id) FROM orders o', 'orders.store_id'),
            ('SELECT (SELECT o.store_id FROM daily_store_metrics o) FROM orders o', 'daily_store_metrics.store_id'),
            ('WITH a AS (SELECT o.store_id FROM orders o), b AS (SELECT o.store_id FROM daily_store_metrics d) SELECT * FROM b', None)]
        for sql, expected in cases:
            scopes = traverse_scope(sqlglot.parse_one(sql, read='duckdb'))
            lineage = _SqlLineage(scopes, {'orders.store_id':{}, 'daily_store_metrics.store_id':{}, 'daily_store_metrics.gmv':{}})
            local = next(s for s in scopes if s.expression.args.get('from_')
                         and s.expression.args['from_'].this.name == 'daily_store_metrics')
            column = next(c for c in local.expression.find_all(exp.Column) if c.table=='o')
            found = lineage.resolve(column, local)
            self.assertEqual(found['fields'], {expected} if expected else set())
            self.assertEqual(bool(found['unresolved']), expected is None)

    @classmethod
    def setUpClass(cls):
        cls.engine = DuckDbEngine()
        cls.validator = QueryContractValidator(cls.engine)
        cls.graph = graph()
        cls.scope = AccessController().scoped('demo_current_sales', SOURCE).public()

    def validate(self, sql=SQL, extra=None, question='比较经营指标'):
        return self.validator.validate({'metrics': [metric()], **(extra or {})}, sql,
                                       question, SOURCE, self.graph, self.scope)

    def test_explicit_coalesce_preserves_multiple_time_origins(self):
        checked = self.validate(extra={'dimensions': ['period'], 'grain': 'month'})
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date', 'orders.ordered_at'])
        derived = checked['derived_dimensions'][0]
        self.assertEqual(derived['source_fields'], checked['dimensions'])
        self.assertEqual(derived['output_name'], 'period')
        self.assertIn('COALESCE', derived['expression'])
        self.assertEqual(derived['role'], 'time')
        self.assertTrue(checked['validation']['sql_compiled'])
        self.assertFalse(checked['validation']['business_definition_verified'])

    def test_omitted_dimensions_infer_final_output_not_all_inner_groups(self):
        checked = self.validate()
        self.assertEqual(set(checked['dimensions']), {'daily_store_metrics.stat_date', 'orders.ordered_at'})
        outer_overall = SQL[:SQL.rfind('SELECT COALESCE')] + 'SELECT SUM(a.value) AS value FROM a'
        # b is an unreferenced CTE; the actual returned grain is still overall.
        checked = self.validate(outer_overall)
        self.assertEqual(checked['dimensions'], [])
        self.assertEqual(checked['derived_dimensions'], [])
        self.assertEqual(checked['grain'], 'overall')

    def test_different_event_time_ranges_are_retained(self):
        checked = self.validate(question='比较2026年6月和7月经营指标')
        ranges = [checked['time_range'], *checked['time_ranges']]
        self.assertEqual({item['field'] for item in ranges}, {'daily_store_metrics.stat_date', 'orders.ordered_at'})
        self.assertTrue(all(item['end'] == '2026-08-01' and item['end_inclusive'] is False for item in ranges))

    def test_roundtrip_metadata_is_idempotent_and_not_model_authored(self):
        raw = {'metrics': [metric()], 'dimensions': ['period'], 'grain': 'month'}
        first, _ = complete_contract(raw, SQL, '比较经营指标', SOURCE, self.graph)
        second, _ = complete_contract(first, SQL, '比较经营指标', SOURCE, self.graph)
        self.assertEqual(first, second)
        self.assertNotIn('derived_dimensions', model_contract_schema()['properties'])
        forged = deepcopy(first)
        forged['derived_dimensions'][0]['source_fields'] = ['orders.region']
        with self.assertRaisesRegex(ValueError, 'derived_dimensions.*不一致'):
            self.validate(extra=forged)

    def test_ambiguous_duplicate_output_stays_rejected(self):
        sql = SQL.replace('COALESCE(a.period,b.period) AS period', 'a.period,b.period')
        with self.assertRaises(ValueError):
            self.validate(sql, {'dimensions': ['period'], 'grain': 'month'})

    def test_bare_join_column_is_not_treated_as_multi_source_expression(self):
        sql = SQL.replace('COALESCE(a.period,b.period) AS period', 'period')
        with self.assertRaises(ValueError):
            self.validate(sql, {'dimensions': ['period'], 'grain': 'month'})

    def test_unknown_column_inside_composite_is_not_hidden_by_known_origin(self):
        for invalid in ('b.unknown_period', 'z.period'):
            sql = SQL.replace('COALESCE(a.period,b.period)', f'COALESCE(a.period,{invalid})')
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.validate(sql, {'dimensions': ['period'], 'grain': 'month'})

    def test_calculated_numeric_group_preserves_expression_and_pending_semantics(self):
        sql = ("SELECT CASE WHEN gmv >= 1000 THEN 'large' ELSE 'small' END AS band, SUM(gmv) AS value "
               'FROM daily_store_metrics GROUP BY band')
        checked = self.validate(sql, {'dimensions': ['band'], 'grain': 'dimension'})
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.gmv'])
        self.assertIn('CASE', checked['derived_dimensions'][0]['expression'])
        self.assertIn('derived_dimension_semantics_not_proven',
                      {check['code'] for check in checked['validation']['pending_checks']})

    def test_raw_metric_is_not_arbitrarily_reclassified_as_dimension(self):
        sql = 'SELECT SUM(gmv) AS value FROM daily_store_metrics'
        with self.assertRaisesRegex(ValueError, '不能作为分析维度'):
            self.validate(sql, {'dimensions': ['daily_store_metrics.gmv'], 'grain': 'dimension'})

    def test_union_positional_lineage_preserves_both_branch_sources(self):
        sql = ("SELECT strftime(stat_date,'%Y-%m') AS period,SUM(gmv) AS value FROM daily_store_metrics GROUP BY 1 "
               "UNION ALL SELECT strftime(ordered_at,'%Y-%m') AS different_name,SUM(paid_amount) AS other_value FROM orders GROUP BY 1")
        checked = self.validate(sql, {'dimensions': ['period'], 'grain': 'month'})
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date', 'orders.ordered_at'])
        self.assertEqual(checked['derived_dimensions'][0]['kind'], 'union')
        self.assertEqual(len(checked['derived_dimensions'][0]['source_expressions']), 4)

    def test_union_literal_labels_have_no_fabricated_physical_source(self):
        sql = ("SELECT 'early' AS period,SUM(gmv) AS value FROM daily_store_metrics WHERE stat_date<'2026-07-01' "
               "UNION ALL SELECT 'late',SUM(gmv) FROM daily_store_metrics WHERE stat_date>='2026-07-01'")
        checked = self.validate(sql, {'dimensions': ['period'], 'grain': 'dimension'})
        self.assertEqual(checked['dimensions'], ['period'])
        self.assertEqual(checked['derived_dimensions'][0]['source_fields'], [])
        self.assertIn('literal_dimension_not_proven', {item['code'] for item in checked['validation']['pending_checks']})

    def test_union_lineage_survives_cte_and_nested_three_branches(self):
        union = ("SELECT strftime(stat_date,'%Y-%m') AS period,SUM(gmv) AS value FROM daily_store_metrics GROUP BY 1 "
                 "UNION ALL SELECT 'manual',SUM(gmv) FROM daily_store_metrics "
                 "UNION ALL SELECT strftime(ordered_at,'%Y-%m'),SUM(paid_amount) FROM orders GROUP BY 1")
        checked = self.validate('WITH periods AS (' + union + ') SELECT period,value FROM periods')
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date', 'orders.ordered_at'])
        self.assertEqual(checked['derived_dimensions'][0]['kind'], 'union')

    def test_legacy_explicit_time_and_filter_mismatches_still_reject(self):
        cases = [
            {'time_range': {'field': 'orders.ordered_at', 'start': '2026-05-01',
                            'end': '2026-08-01', 'end_inclusive': False}},
            {'filters': [{'field': 'orders.ordered_at', 'operator': 'between',
                          'value': ['2026-06-01', '2026-08-01']}]},
            {'filters': [{'field': 'orders.region', 'operator': '=', 'value': 'unfiltered'}]},
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.validate(extra=extra)


if __name__ == '__main__':
    unittest.main()
