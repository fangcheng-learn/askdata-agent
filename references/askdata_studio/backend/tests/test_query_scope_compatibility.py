"""Actual query validation across independent CTE scopes; no provider calls."""
from copy import deepcopy
import unittest

from app.errors import PipelineStageError
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.querying.query_protocol import complete_contract
from app.runtime_budget import RunBudget
from app.security import AccessController
from tests import test_query_protocol as protocol
from tests import test_direct_sql_generation_policy as direct


SQL = """WITH a AS (
    SELECT date_trunc('month', stat_date) AS period, SUM(gmv) AS value
    FROM daily_store_metrics GROUP BY 1
), b AS (
    SELECT date_trunc('month', ordered_at) AS period, COUNT(*) AS n
    FROM orders GROUP BY 1
)
SELECT a.period, a.value, b.n FROM a LEFT JOIN b ON a.period=b.period"""


class QueryScopeCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = DuckDbEngine()
        cls.validator = QueryContractValidator(cls.engine)
        cls.graph = protocol.graph()
        cls.scope = AccessController().scoped('demo_current_sales', protocol.SOURCE).public()

    def validate(self, sql=SQL, *, dimensions=None, metrics=None):
        return self.validator.validate({'metrics': metrics or [protocol.metric()],
            'dimensions': dimensions or ['period'], 'grain': 'month'}, sql,
            '比较经营指标', protocol.SOURCE, self.graph, self.scope)

    def test_selected_output_resolves_before_same_named_join_inputs(self):
        checked = self.validate()
        self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date'])
        changes = checked['validation']['normalizations']
        self.assertTrue(any(c.get('path') == 'dimensions' and c.get('before') == ['period'] for c in changes))

    def test_explicit_select_alias_and_qualified_cte_column_match(self):
        for sql, name in [(SQL.replace('SELECT a.period, a.value', 'SELECT a.period AS displayed_period, a.value'), 'displayed_period'),
                          (SQL, 'a.period')]:
            with self.subTest(name=name):
                checked = self.validate(sql, dimensions=[name])
                self.assertEqual(checked['dimensions'], ['daily_store_metrics.stat_date'])

    def test_explicit_multi_source_expression_retains_all_origins(self):
        sql = SQL.replace('SELECT a.period,', 'SELECT COALESCE(a.period, b.period) AS period,')
        checked = self.validate(sql)
        self.assertEqual(set(checked['dimensions']), {'daily_store_metrics.stat_date', 'orders.ordered_at'})
        self.assertTrue(checked['derived_dimensions'])
        self.assertFalse(checked['validation']['business_definition_verified'])

    def test_duplicate_output_still_rejected(self):
        sql = SQL.replace('SELECT a.period,', 'SELECT a.period, b.period,')
        with self.assertRaises(ValueError):
            self.validate(sql)

    def test_count_star_uses_its_local_physical_table_not_global_table_count(self):
        orders_pk = protocol.table('orders')['primary_key'][0]
        checked = self.validate(metrics=[protocol.metric(), protocol.metric('row_count', 'orders.' + orders_pk, 'count')])
        self.assertTrue(checked['validation']['sql_compiled'])
        self.assertEqual(len(checked['tables']), 2)

    def test_count_star_does_not_prove_nullable_or_distinct_entity_count(self):
        pk = protocol.table('orders')['primary_key'][0]
        cases = [(SQL, 'orders.paid_amount'),
            (SQL.replace('FROM orders GROUP BY 1',
                'FROM orders FULL JOIN (SELECT 1 AS x) other ON FALSE GROUP BY 1'), 'orders.' + pk)]
        for sql, reference in cases:
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    self.validate(sql, metrics=[protocol.metric(), protocol.metric('row_count', reference, 'count')])

        with self.assertRaises(ValueError):
            self.validate(metrics=[protocol.metric(), protocol.metric('unique_orders', 'orders.' + pk, 'count_distinct')])

    def test_joined_count_star_equals_nonnull_pk_count_but_not_unique_entities(self):
        pk = protocol.table('orders')['primary_key'][0]
        joined = SQL.replace('FROM orders GROUP BY 1',
            'FROM orders CROSS JOIN (SELECT 1 AS x UNION ALL SELECT 2) fanout GROUP BY 1')
        checked = self.validate(joined, metrics=[protocol.metric(), protocol.metric('row_count', 'orders.'+pk, 'count')])
        self.assertIn('joined_row_count_not_entity_count', [x['code'] for x in checked['validation']['pending_checks']])
        import duckdb
        with duckdb.connect(':memory:') as db:
            counts = db.execute('SELECT COUNT(*), COUNT(t.id), COUNT(DISTINCT t.id) '
                'FROM (VALUES (1),(2)) t(id) CROSS JOIN (VALUES (1),(2)) fanout(x)').fetchone()
        self.assertEqual(counts, (4,4,2))

    def test_direct_permission_denial_is_not_sent_for_sql_rewrite(self):
        fixture = direct.DirectGenerationTest()
        agent, model, tools, validations = fixture.agent([direct.sql('first'), direct.sql('forbidden')],
            validation=[PermissionError('无权访问测试表')])
        budget = RunBudget('permission')
        with self.assertRaises(PipelineStageError) as caught:
            fixture.run_agent(agent, budget)
        self.assertEqual(caught.exception.stage, 'query_permission')
        self.assertFalse(caught.exception.context['repair_available'])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(validations), 1)
        self.assertFalse(tools)
        self.assertFalse(budget.repairs)


if __name__ == '__main__':
    unittest.main()
