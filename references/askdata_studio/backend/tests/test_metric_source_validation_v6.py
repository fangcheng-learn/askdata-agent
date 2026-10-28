"""Server-owned metric confirmations, independent of model SQL declarations."""
from __future__ import annotations

import copy
import unittest

from app.database import SCHEMA, physical_table_name
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


class MetricSourceValidationTest(unittest.TestCase):
    def setUp(self):
        self.database = 'ecommerce_ops'
        self.validator = QueryContractValidator(DuckDbEngine())
        self.scope = AccessController().scoped('demo_current_sales', self.database).public()
        self.graph = {'tables': [dict(id=table['id'], name=physical_table_name(table), database=self.database)
                                for table in SCHEMA if table.get('database') == self.database]}

    def context(self):
        return {'task_id': 'current', 'confirmed_metric_sources': {
            'database': self.database, 'task_id': 'current',
            'sources': {'gmv': 'daily_store_metrics.gmv'}},
            'clarification_history': [{'label': '店铺维度GMV', 'option_id': 'store'}]}

    def validate(self, table='daily_store_metrics', *, context=None, metric_name='GMV'):
        contract = dict(original_question='2026年六月GMV', data_source=self.database,
            tables=[table], metrics=[dict(name=metric_name, source_fields=[table + '.gmv'], aggregation='sum')],
            dimensions=[], filters=[], time_range=dict(field=table + '.stat_date', start='2026-06-01', end='2026-07-01'),
            grain='overall', assumptions=[], open_questions=[])
        sql = f"SELECT SUM(gmv) AS gmv FROM {table} WHERE stat_date>='2026-06-01' AND stat_date<'2026-07-01'"
        return self.validator.validate(contract, sql, contract['original_question'], self.database,
                                       self.graph, self.scope, validation_context=context)

    def test_confirmed_store_source_does_not_require_original_question_to_name_table(self):
        checked = self.validate(context=self.context())
        self.assertEqual(checked['original_question'], '2026年六月GMV')
        self.assertEqual(checked['validation']['confirmed_metric_sources'], {'gmv': 'daily_store_metrics.gmv'})
        self.assertNotIn('metric_source_not_proven', {c['code'] for c in checked['validation']['pending_checks']})
        self.assertFalse(checked['validation']['business_definition_verified'])

    def test_model_cannot_replace_confirmed_store_gmv_with_product_gmv(self):
        with self.assertRaisesRegex(ValueError, '用户已确认来源冲突'):
            self.validate('daily_product_metrics', context=self.context())

    def test_changing_display_name_cannot_hide_confirmed_source_conflict(self):
        with self.assertRaisesRegex(ValueError, '用户已确认来源冲突'):
            self.validate('daily_product_metrics', context=self.context(), metric_name='本次合计')

    def test_descriptions_and_model_source_fields_do_not_prove_user_confirmation(self):
        checked = self.validate()
        self.assertEqual(checked['validation']['confirmed_metric_sources'], {})
        self.assertIn('metric_source_not_proven', {c['code'] for c in checked['validation']['pending_checks']})

    def test_legacy_answer_is_preserved_as_review_material_without_guessing_binding(self):
        context = self.context()
        context.pop('confirmed_metric_sources')
        checked = self.validate(context=context)
        pending = next(c for c in checked['validation']['pending_checks'] if c['code'] == 'metric_source_not_proven')
        self.assertEqual(pending['clarification_history'][0]['label'], '店铺维度GMV')
        self.assertEqual(checked['validation']['confirmed_metric_sources'], {})

    def test_confirmation_from_another_task_or_database_does_not_become_authority(self):
        for key, value in [('task_id', 'old-task'), ('database', 'homenova_20260831')]:
            context = self.context()
            context['confirmed_metric_sources'][key] = value
            with self.subTest(key=key):
                checked = self.validate(context=context)
                self.assertEqual(checked['validation']['confirmed_metric_sources'], {})
                self.assertIn('metric_confirmation_scope_mismatch', {c['code'] for c in checked['validation']['pending_checks']})

    def test_confirmation_cannot_authorize_an_inaccessible_source(self):
        self.scope = copy.deepcopy(self.scope)
        self.scope['allowed_tables'] = ['ecommerce_ops.daily_product_metrics']
        with self.assertRaisesRegex(ValueError, '授权Schema图'):
            self.validate('daily_product_metrics', context=self.context())


if __name__ == '__main__':
    unittest.main()
