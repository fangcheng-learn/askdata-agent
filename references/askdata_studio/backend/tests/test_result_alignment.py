import unittest
from copy import deepcopy

from app.querying.calculations import execute_calculation, CalculationError
from tests import test_react_calculate_contract as fixture


def request():
    return {'type': 'calculate', 'operation': 'align_results', 'result_id': 'T:R1', 'right_result_id': 'T:R2',
            'join_keys': {'month': 'month', 'sku': 'sku'},
            'left_fields': {'gmv': 'amount'}, 'right_fields': {'sessions': 'visits'}}


def data(rows, value):
    return {'complete': True, 'columns': ['month', 'sku', value], 'rows': rows, 'total_rows': len(rows),
            'column_metadata': {value: {'unit': 'EUR cents' if value == 'amount' else 'sessions'}}}


class ResultAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.left = data([{'month': '2026-06', 'sku': 'A', 'amount': 100}, {'month': '2026-07', 'sku': 'A', 'amount': 80}], 'amount')
        self.right = data([{'month': '2026-07', 'sku': 'A', 'visits': 20}, {'month': '2026-06', 'sku': 'A', 'visits': 20}], 'visits')

    def test_aligns_by_explicit_keys_not_row_order_and_retains_units(self):
        before = deepcopy((self.left, self.right))
        result = execute_calculation(request(), self.left, right_data=self.right)
        self.assertEqual([(r['gmv'], r['sessions']) for r in result['rows']], [(100, 20), (80, 20)])
        self.assertEqual(result['column_metadata']['gmv']['unit'], 'EUR cents')
        self.assertEqual(result['column_metadata']['sessions']['source_result_id'], 'T:R2')
        comparison = execute_calculation({'operation': 'period_change', 'result_id': 'T:R3',
            'period_field': 'month', 'base_period': '2026-06', 'current_period': '2026-07',
            'group_by': ['sku'], 'value_fields': ['gmv']}, result)
        self.assertEqual(comparison['rows'][0]['change_rate'], -0.2)
        self.assertEqual((self.left, self.right), before)

    def test_keeps_unmatched_and_null_values_without_zero_fill(self):
        self.right['rows'][0]['sku'] = 'B'
        result = execute_calculation(request(), self.left, right_data=self.right)
        self.assertEqual(len(result['rows']), 3)
        self.assertIsNone(result['rows'][1]['sessions'])
        self.assertIsNone(result['rows'][2]['gmv'])
        self.assertEqual(result['diagnostics'][0]['count'], 2)

    def test_rejects_partial_inputs_missing_keys_duplicates_and_alias_collisions(self):
        bad = deepcopy(self.right); bad['complete'] = False
        variants = [(request(), bad)]
        bad = deepcopy(self.right); bad['rows'].append(dict(bad['rows'][0])); variants.append((request(), bad))
        bad = deepcopy(self.right); bad['rows'][0]['month'] = None; variants.append((request(), bad))
        variants.append(({**request(), 'right_fields': {'gmv': 'visits'}}, self.right))
        variants.append(({**request(), 'join_keys': {'sku': 'sku'}}, self.right))
        for req, right in variants:
            with self.subTest(req=req), self.assertRaises(CalculationError):
                execute_calculation(req, self.left, right_data=right)

    def test_harness_authorizes_both_sources_and_persists_lineage_without_query(self):
        f = fixture.ReactCalculateContractTest(); f.setUp(); self.addCleanup(f.doCleanups)
        f.task.protocol_version = 4
        for rid, source in [('T:R1', self.left), ('T:R2', self.right)]:
            f.evidence(source['columns'], source['rows'], identifier=rid)
            f.task.evidence[-1]['column_metadata'] = source['column_metadata']
        f.dispatch(request())
        saved = f.task.evidence[-1]
        self.assertEqual(saved['input_result_ids'], ['T:R1', 'T:R2'])
        self.assertEqual([s['result_id'] for s in saved['source_results']], ['T:R1', 'T:R2'])
        self.assertEqual(saved['rows'][1]['gmv'], 80)
        self.assertEqual(f.budget.react_rounds, 0)
        f.dispatch(request()); self.assertEqual(f.task.observation['status'], 'reused')
        self.assertEqual(f.budget.tools, 1)
        f.dispatch({**request(), 'right_result_id': 'OTHER:R1'})
        self.assertEqual(f.task.actions[-1]['status'], 'failed')
        self.assertEqual(len(f.task.evidence), 3)


if __name__ == '__main__':
    unittest.main()
