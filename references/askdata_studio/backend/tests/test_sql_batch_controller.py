"""Fixed business requests, controlled models, and actual readonly local MCP."""
import json
import unittest

from app.querying.data_qa_agent import DataQaAgent
from app.runtime_budget import RunBudget
from app.workflows.react_harness import ReactRecord
from tests import test_react_harness as legacy
from tests.test_react_harness import action, query, review


def business(*goals):
    return action('query_batch', queries=[{'goal': goal, 'metric_requirements': [metric],
        'dimensions': [], 'input_result_ids': []} for goal, metric in goals])


def generated(*entries):
    return {'queries': [{'query_id': entry[0], 'sql': entry[1] or query()['sql'],
                         'query_contract': query(entry[2] if len(entry) > 2 else 'gmv')['query_contract']}
                        for entry in entries]}


class SqlBatchControllerTest(unittest.TestCase):
    """New execution path; legacy cases remain in their original suite."""

    setUp = legacy.ReactHarnessTest.setUp
    harness = legacy.ReactHarnessTest.harness
    run_bounded = legacy.ReactHarnessTest.run_bounded

    @staticmethod
    def state():
        state = legacy.ReactHarnessTest.state()
        state['investigation_protocol_version'] = 1
        return state

    def new_run(self, outputs):
        harness, model = self.harness(outputs)
        harness.workflow.data_qa_agent = DataQaAgent(model, harness.workflow.mcp_client,
                                                    harness.workflow.skills.get('data_qa'))
        state = self.state()
        state['investigation_protocol_version'] = 2
        return harness, model, state

    def test_business_decides_sql_module_generates_then_same_dataqa_drafts(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('核实GMV', 'gmv')), generated(('A2', None)),
            action('deliver'), {'title': '经营调查报告', 'markdown': '已取得GMV [T1:R1]'}, review(),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(result.report.markdown, result.analysis)
        self.assertEqual(result.report.data_sources[0].result_id, 'T1:R1')
        self.assertEqual(h.records['T1'].protocol_version, 2)
        react_system, react_payload = model.calls[1]
        self.assertIn('不写SQL', react_system)
        self.assertNotIn('query_contract_schema', react_payload)
        self.assertNotIn('schema_graph', react_payload['semantics'])
        sql_system, sql_payload = model.calls[2]
        self.assertIn('DuckDB', sql_system)
        self.assertEqual(sql_payload['queries'][0]['query_id'], 'A2')
        self.assertNotIn('internal_schema_graph', sql_payload)
        self.assertEqual(model.calls[4][1]['mode'], 'investigation_report')
        self.assertEqual(len(result.reviews), 1)
        self.assertEqual(len(model.calls), 6)

    def test_batch_once_and_only_failed_subset_repaired(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('核实GMV', 'gmv'), ('核实订单', 'paid_orders')),
            generated(('A2', None), ('A3', 'SELECT SUM(nonexistent) AS value FROM daily_store_metrics', 'paid_orders')),
            generated(('A3', 'SELECT SUM(paid_orders) AS value FROM daily_store_metrics', 'paid_orders')),
            action('deliver'), {'draft': '有限证据'}, review(ids=('T1:R1', 'T1:R2')),
        ])
        result, budget = self.run_bounded(h, state)
        self.assertEqual(result.status, 'completed', result.stop_reason)
        queries = [a for a in result.plan if a.get('action_type') == 'query']
        self.assertEqual([a['sql_generations'] for a in queries], [1, 2])
        self.assertEqual([q['query_id'] for q in model.calls[2][1]['queries']], ['A2', 'A3'])
        self.assertEqual([q['query_id'] for q in model.calls[3][1]['queries']], ['A3'])
        self.assertIn('nonexistent', json.dumps(model.calls[3][1]['failed_queries']))
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual(len(result.evidence), 2)

    def test_two_bad_formats_stop_without_third_generation(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']),
            business(('核实GMV', 'gmv')), {'wrong': []}, {'queries': []},
        ])
        result, _ = self.run_bounded(h, state)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.plan[-1]['sql_generations'], 2)
        self.assertEqual(len(model.calls), 4)

    def test_progress_keeps_earlier_facts_when_update_omits_them(self):
        h, _, state = self.new_run([])
        task = ReactRecord(state, protocol_version=2)
        task.evidence = [{'id': 'R1'}, {'id': 'R2'}]
        h._update_progress(task, {'facts': [{'id': 'F1', 'text': '早期事实', 'evidence_ids': ['R1']}]})
        h._update_progress(task, {'facts': [{'id': 'F2', 'text': '新事实', 'evidence_ids': ['R2']}]})
        self.assertEqual([v['id'] for v in task.progress['facts']], ['F1', 'F2'])

    def test_sql_clarification_keeps_generation_and_resumes_same_query(self):
        h, model, state = self.new_run([
            action('load_skill', names=['business_diagnosis']), business(('核实GMV', 'gmv')),
            {'queries': [{'query_id': 'A2', 'status': 'clarify',
                          'clarification': {'question': '需要确认哪个范围？', 'parameter': 'scope'}}]},
            business(('核实GMV', 'gmv')), generated(('A2', None)),
            action('deliver'), {'draft': '已取得结果'}, review(),
        ])
        first, budget = self.run_bounded(h, state)
        self.assertEqual(first.status, 'waiting_clarification', first.stop_reason)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(first.plan[-1]['sql_generations'], 1)
        second, _ = self.run_bounded(h, state, response='当前授权全盘范围', budget=budget)
        self.assertEqual(second.status, 'completed', second.stop_reason)
        queries = [a for a in second.plan if a.get('action_type') == 'query']
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]['sql_generations'], 2)
        self.assertEqual(budget.react_rounds, 2)

    def test_fifth_batch_closes_without_sixth_business_decision(self):
        outputs = [action('load_skill', names=['business_diagnosis'])]
        for index, column in enumerate(['gmv', 'paid_orders', 'visitors', 'buyers', 'refund_amount'], 2):
            outputs.extend([business(('核实' + column, column)), generated((
                'A' + str(index), f'SELECT SUM({column}) AS value FROM daily_store_metrics', column))])
        outputs.extend([{'draft': '五轮实际证据'}, review(ids=tuple(f'T1:R{i}' for i in range(1, 6)))])
        h, model, state = self.new_run(outputs)
        result, budget = self.run_bounded(h, state, budget=RunBudget('T1', react_round_limit=5))
        self.assertEqual(budget.react_rounds, 5)
        self.assertEqual(len(result.evidence), 5, result.stop_reason)
        self.assertEqual(len(model.calls), 13)  # skill + 5*(business+SQL) + draft/review
        self.assertEqual(len(result.reviews), 1)
        self.assertEqual(model.calls[-2][1]['mode'], 'investigation_report')


if __name__ == '__main__':
    unittest.main()
