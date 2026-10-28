import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.runtime_budget import RunBudget, current_budget
from app.task_decisions import validate_step_decision
from app.workflows.harness import InvestigationHarness, TaskRecord
import test_harness as legacy
from test_harness import ScriptModel, QUESTION, reviewed, step


def condition():
    return dict(id='C1', description='完整可比结果未显示下降', affected_requirement_ids=['Q2'],
                affected_step_ids=['S2'], comparison_required=True)


def stop_proposal(result='T1:R1'):
    reference = lambda period: dict(result_id=result, field='v', row_match={'period': period})
    return dict(action='request_delivery', premise_status='refuted', condition_id='C1', evidence_ids=[result],
                affected_requirement_ids=['Q2'], affected_step_ids=['S2'], remaining_requirement_ids=[],
                reason='本期不低于基期，仅下降归因不适用',
                comparisons=[dict(left=reference('current'), operator='ge', right=reference('baseline'), holds=True)])


class ApplicabilityContractTest(unittest.TestCase):
    def context(self):
        return dict(step_id='S2', requirements={'Q1': '事实', 'Q2': '下降解释', 'Q3': '独立目标'},
                    datasource_id='homenova_20260831', step_conditions=[condition()],
                    plan_steps=[dict(id='S1', requirement_ids=['Q1']), dict(id='S2', requirement_ids=['Q2']),
                                dict(id='S3', requirement_ids=['Q3'])],
                    dependency_results=[dict(id='T1:R1', datasource_id='homenova_20260831', complete=True,
                                             check_status='basic_valid', columns=['period', 'v'],
                                             rows=[dict(period='baseline', v=10), dict(period='current', v=12)])])

    def test_refutation_requires_complete_evidence_and_true_comparison(self):
        context = self.context()
        self.assertEqual(validate_step_decision(stop_proposal(), context).premise_status, 'refuted')
        for change in ['incomplete', 'wrong_snapshot', 'false_comparison', 'missing_period']:
            modified = copy.deepcopy(context)
            evidence = modified['dependency_results'][0]
            if change == 'incomplete': evidence['complete'] = False
            if change == 'wrong_snapshot': evidence['datasource_id'] = 'homenova_20261015'
            if change == 'false_comparison': evidence['rows'][1]['v'] = 8
            if change == 'missing_period': evidence['rows'] = evidence['rows'][:1]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_step_decision(stop_proposal(), modified)

    def test_cannot_invent_conditions_evidence_or_skip_independent_scope(self):
        for field, value in [('condition_id', 'invented'), ('evidence_ids', ['OTHER:R1']),
                             ('affected_requirement_ids', ['Q2', 'Q3']), ('affected_step_ids', ['S2', 'S3'])]:
            proposal = stop_proposal(); proposal[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_step_decision(proposal, self.context())


class SingleCallExecutorTest(unittest.TestCase):
    def agent(self, outputs, database='askdata_mock'):
        from app.querying.single_database_agent import SingleDatabaseAgent
        from app.querying.duckdb_engine import DuckDbEngine
        from app.skills import SkillRegistry
        model = ScriptModel(outputs)
        calls = []
        client = SimpleNamespace(list_tools=lambda: [dict(name='query_' + database, input_schema={})],
                                 call_tool=lambda name, arguments: calls.append((name, arguments)) or dict(success=True, columns=['total'], rows=[{'total': 3}], complete=True))
        agent = SingleDatabaseAgent(model, lambda scope: client, SkillRegistry().get('database_query'), DuckDbEngine())
        return agent, model, calls

    def test_query_and_applicability_share_one_model_call_and_keep_legacy_query(self):
        from app.security import AccessController
        query = '查询销售额'
        raw = dict(action='call_tool', tool_name='query_askdata_mock', arguments={'sql': 'SELECT SUM(paid_amount) AS total FROM orders_current'},
                   query_contract=dict(original_question=query, data_source='askdata_mock', tables=['orders_current'],
                                       metrics=[dict(name='销售额', definition='客户实际支付金额，可用于计算实际成交金额',
                                                     source_fields=['orders_current.paid_amount'], aggregation='sum')],
                                       dimensions=[], filters=[], time_range=None, grain='overall', assumptions=[], open_questions=[]),
                   reason='查询当前销售额')
        graph = {'tables': [dict(name='orders_current', id='orders_current', database='askdata_mock')]}
        scope = AccessController().resolve('demo_analyst').public()
        for protocol in [False, True]:
            output = copy.deepcopy(raw)
            context = None
            if protocol:
                output['step_decision'] = dict(action='execute_step', reason='执行本步')
                context = dict(step_protocol=True, step_id='S1', requirements={'Q1': query}, plan_steps=[dict(id='S1')], dependency_results=[])
            agent, model, calls = self.agent([output])
            budget = RunBudget('T1')
            budget.start()
            token = current_budget.set(budget)
            try:
                result = agent.prepare(query, 'askdata_mock', graph, '', {}, {}, scope, task_context=context)
            finally:
                current_budget.reset(token)
            self.assertEqual(result['action'], 'executed')
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(len(calls), 1)
            injected = [e for e in budget.public()['events'] if e['stage'] == 'skill_injection']
            self.assertEqual([(e['node'], e['skill']) for e in injected], [('query_coder', 'database_query')])
            self.assertNotIn('instructions', injected[0])
            if protocol: self.assertEqual(result['step_decision']['action'], 'execute_step')

    def test_skip_is_structured_in_existing_call_and_dispatches_no_query(self):
        context = ApplicabilityContractTest().context(); context['step_protocol'] = True
        agent, model, calls = self.agent([dict(action='step_decision', step_decision=stop_proposal())], 'homenova_20260831')
        result = agent.prepare('拆解下降', 'homenova_20260831', {'tables': [], 'fields': []}, '', {}, {}, {}, task_context=context)
        self.assertEqual(result['action'], 'step_decision')
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(calls, [])

    def test_invalid_skip_never_reaches_mcp(self):
        from app.errors import PipelineStageError
        context = ApplicabilityContractTest().context(); context['step_protocol'] = True
        bad = stop_proposal(); bad['affected_step_ids'].append('S3')
        agent, _, calls = self.agent([dict(action='step_decision', step_decision=bad)] * 3, 'homenova_20260831')
        with self.assertRaises(PipelineStageError) as caught:
            agent.prepare('拆解下降', 'homenova_20260831', {'tables': [], 'fields': []}, '', {}, {}, {}, task_context=context)
        self.assertEqual(caught.exception.stage, 'query_contract_validation')
        self.assertEqual(calls, [])


class LatestHarnessTest(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.HarnessTest()

    def test_skill_trace_records_actual_selected_injection_without_full_prompt(self):
        harness, _ = self.fixture.harness(ScriptModel([{}]))
        task = TaskRecord(self.fixture.state(), [QUESTION], skills=['business_diagnosis'])
        budget = RunBudget('T1'); budget.start()
        token = current_budget.set(budget)
        try:
            harness._call('planner', '受控规划', {}, task)
        finally:
            current_budget.reset(token)
        events = budget.public()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['skill'], 'business_diagnosis')
        self.assertEqual(events[0]['node'], 'planner')
        self.assertEqual(len(events[0]['content_sha256']), 64)
        self.assertNotIn('instructions', events[0])
        self.assertEqual(budget.tools, 0)

    def test_legacy_review_flags_do_not_call_process_reviewer(self):
        model = ScriptModel([dict(conditions={}, steps=[step(review=True), step('S2', ['S1'], review=True)]),
                             reviewed(ids=['T1:R1', 'T1:R2'], final=True)])
        harness, calls = self.fixture.harness(model, [self.fixture.execution(), self.fixture.execution(4)])
        result = self.fixture.bounded(harness, self.fixture.state())
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result.reviews), 1)
        self.assertTrue(all(item['final'] for item in result.reviews))
        self.assertNotIn('result_review', [item['stage'] for item in result.execution_log])

    def test_refutation_preserves_independent_work_and_delivers_normally(self):
        first = {**step(), 'requirement_ids': ['Q1']}
        second = {**step('S2', ['S1']), 'requirement_ids': ['Q2'], 'entry_conditions': [condition()]}
        independent = {**step('S3', goal='独立目标'), 'requirement_ids': ['Q3']}
        state = self.fixture.state(); state['required_questions'] = ['比较事实', '下降解释', '独立目标']
        final = reviewed(final=True)
        final['answered'] = [dict(question=q, evidence_ids=[rid], strength='fact', explanation='受控结果')
                             for q, rid in [('比较事实', 'T1:R1'), ('独立目标', 'T1:R2')]]
        model = ScriptModel([dict(conditions={}, steps=[first, second, independent]), final])
        execution = self.fixture.execution()
        execution['execution'].update(columns=['period', 'v'], rows=[dict(period='baseline', v=10), dict(period='current', v=12)])
        harness, calls = self.fixture.harness(model, [execution, dict(action='step_decision', step_decision=stop_proposal()), self.fixture.execution(5)])
        result = self.fixture.bounded(harness, state)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([s['status'] for s in result.plan], ['completed', 'not_applicable', 'completed'])
        self.assertEqual(result.requirements[1]['status'], 'not_applicable')
        self.assertEqual(calls[1][1]['task_context']['dependency_results'][0]['rows'][1]['v'], 12)
        self.assertEqual(calls[2][0][0], '独立目标')
        self.assertEqual(len(result.reviews), 1)

    def test_final_review_returns_runnable_missing_work_to_executor(self):
        harness, _ = self.fixture.harness(ScriptModel([]))
        task = TaskRecord(self.fixture.state(), [QUESTION])
        task.steps = [{**step(), 'status': 'completed', 'result_ids': ['R1']},
                      {**step('S2', ['S1']), 'status': 'pending', 'result_ids': []}]
        task.evidence = [dict(id='R1', check_status='basic_valid', complete=True, rows=[{'v': 3}])]
        task.draft = '草稿'; task.stop_reason = '历史已解决事件'
        output = reviewed('partial', ids=['R1'], final=True)
        def review(record, final):
            from app.workflows.harness import Review
            record.reviews.append({'final': True, **output}); return Review.model_validate(output)
        harness._review_call = review
        token = current_budget.set(RunBudget('T1'))
        try: harness._delivery_review({'record': task})
        finally: current_budget.reset(token)
        self.assertEqual(task.next_node, 'execute')
        self.assertFalse(task.final_reviewed)

    def test_unchanged_draft_is_not_reviewed_again_after_invalid_approval(self):
        bad = reviewed(ids=['T1:R1'], final=True)
        bad['approved_text'] = '客单价上升排除了商品结构或降价作为主因。'
        model = ScriptModel([dict(conditions={}, steps=[step()]), bad])
        harness, _ = self.fixture.harness(model, [self.fixture.execution()])
        result = self.fixture.bounded(harness, self.fixture.state())
        self.assertEqual(len(model.calls), 2)
        self.assertFalse(result.final_reviewed)
        self.assertNotIn(bad['approved_text'], result.analysis)
        self.assertIn('未变化', result.stop_reason)

    def test_substantially_corrected_draft_can_have_one_more_final_review(self):
        bad = reviewed(ids=['T1:R1'], final=True)
        bad['approved_text'] = '客单价上升排除了商品结构或降价作为主因。'
        good = reviewed(ids=['T1:R1'], final=True)
        model = ScriptModel([dict(conditions={}, steps=[step()]), bad, good])
        harness, _ = self.fixture.harness(model, [self.fixture.execution()])
        drafts = iter(['存在待修正文案', '已删除无依据推断，只保留可核对事实'])
        harness.workflow.data_qa_agent.run = lambda *a: SimpleNamespace(report=None, answer=next(drafts))
        result = self.fixture.bounded(harness, self.fixture.state())
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.reviews), 2)
        self.assertTrue(all(item['final'] for item in result.reviews))

    def test_resolved_historical_stop_does_not_prevent_completion(self):
        harness, _ = self.fixture.harness(ScriptModel([]))
        task = TaskRecord(self.fixture.state(), [QUESTION])
        task.steps = [{**step(), 'status': 'completed', 'result_ids': ['R1']}]
        task.evidence = [dict(id='R1', check_status='basic_valid', complete=True, rows=[{'v': 3}])]
        task.stop_reason = '旧缺口已解决'; task.draft = '有效草稿'
        output = reviewed(ids=['R1'], final=True)
        def review(record, final):
            from app.workflows.harness import Review
            record.reviews.append({'final': True, **output}); return Review.model_validate(output)
        harness._review_call = review
        token = current_budget.set(RunBudget('T1'))
        try: harness._delivery_review({'record': task})
        finally: current_budget.reset(token)
        self.assertEqual(task.final_status, 'completed')
        self.assertEqual(task.stop_reason, '')

    def test_refuted_branch_propagates_to_related_descendants_without_partial(self):
        first = {**step(), 'requirement_ids': ['Q1']}
        second = {**step('S2', ['S1']), 'requirement_ids': ['Q2'], 'entry_conditions': [condition()]}
        third = {**step('S3', ['S2']), 'requirement_ids': ['Q2']}
        state = self.fixture.state(); state['required_questions'] = ['比较事实', '下降解释']
        final = reviewed(final=True)
        final['answered'] = [dict(question='比较事实', evidence_ids=['T1:R1'], strength='fact', explanation='前提已核实')]
        model = ScriptModel([dict(conditions={}, steps=[first, second, third]), final])
        execution = self.fixture.execution()
        execution['execution'].update(columns=['period', 'v'], rows=[dict(period='baseline', v=10), dict(period='current', v=12)])
        harness, calls = self.fixture.harness(model, [execution, dict(action='step_decision', step_decision=stop_proposal())])
        result = self.fixture.bounded(harness, state)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([s['status'] for s in result.plan], ['completed', 'not_applicable', 'not_applicable'])
        self.assertEqual(len(calls), 2)

    def test_dependent_independent_requirement_needs_new_path(self):
        harness, _ = self.fixture.harness(ScriptModel([]))
        task = TaskRecord(self.fixture.state(), ['比较事实', '下降解释', '独立目标'])
        task.steps = [{**step(), 'requirement_ids': ['Q1'], 'status': 'completed', 'result_ids': ['R1']},
                      {**step('S2', ['S1']), 'requirement_ids': ['Q2'], 'status': 'pending'},
                      {**step('S3', ['S2']), 'requirement_ids': ['Q3'], 'status': 'pending'}]
        task.pending_decision = {**stop_proposal(), 'step_id': 'S2'}
        token = current_budget.set(RunBudget('T1'))
        try:
            harness._controller({'record': task})
            harness._execute({'record': task})
            self.assertEqual(current_budget.get().replans, 1)
        finally:
            current_budget.reset(token)
        self.assertEqual(task.next_node, 'planner')
        self.assertEqual(task.steps[2]['status'], 'blocked')
        self.assertEqual(task.not_applicable_requirements, {'Q2'})
        self.assertEqual(task.steps[0]['result_ids'], ['R1'])

    def test_same_requirement_independent_branch_survives_related_descendant_skip(self):
        harness, _ = self.fixture.harness(ScriptModel([]))
        task = TaskRecord(self.fixture.state(), ['比较事实', '下降解释'])
        task.steps = [{**step(), 'requirement_ids': ['Q1'], 'status': 'completed'},
                      {**step('S2', ['S1']), 'requirement_ids': ['Q2'], 'status': 'pending'},
                      {**step('S3', ['S2']), 'requirement_ids': ['Q2'], 'status': 'pending'},
                      {**step('S4'), 'requirement_ids': ['Q2'], 'status': 'pending'}]
        task.pending_decision = {**stop_proposal(), 'step_id': 'S2'}
        harness._controller({'record': task})
        self.assertEqual([s['status'] for s in task.steps], ['completed', 'not_applicable', 'not_applicable', 'pending'])
        self.assertNotIn('Q2', task.not_applicable_requirements)


class PhysicalMeasureBindingTest(unittest.TestCase):
    def test_period_output_names_bind_only_exact_physical_measure_and_aggregation(self):
        from app import homenova
        from app.querying.duckdb_engine import DuckDbEngine
        from app.querying.query_contract import QueryContractValidator
        from app.security import AccessController
        database = 'homenova_20260831'
        schema = homenova.schema_payload(database)['tables']
        definition = next(f['description'] for t in schema if t['name'] == 'order_items' for f in t['fields'] if f['name'] == 'quantity')
        query = '比较两期购买件数'
        contract = dict(original_question=query, data_source=database, tables=['payments', 'order_items'],
                        metrics=[dict(name='purchased_units', definition=definition, source_fields=['order_items.quantity'], aggregation='sum')],
                        dimensions=[], filters=[dict(field='payments.status', operator='=', value='captured')],
                        time_range=dict(field='payments.captured_at', start='2026-06-01', end='2026-08-01'),
                        grain='overall', assumptions=[], open_questions=[])
        sql = """SELECT SUM(CASE WHEN p.captured_at < '2026-07-01' THEN oi.quantity ELSE 0 END) AS previous_units,
                 SUM(CASE WHEN p.captured_at >= '2026-07-01' THEN oi.quantity ELSE 0 END) AS current_units
                 FROM payments p JOIN order_items oi ON p.order_id=oi.order_id
                 WHERE p.status='captured' AND p.captured_at>='2026-06-01' AND p.captured_at<'2026-08-01'"""
        graph = {'tables': [t for t in schema if t['name'] in contract['tables']]}
        scope = AccessController().scoped('demo_admin', database).public()
        validator = QueryContractValidator(DuckDbEngine())
        validated = validator.validate(contract, sql, query, database, graph, scope)
        self.assertEqual(validated['validation']['metric_output_bindings'][0]['output_aliases'], ['previous_units', 'current_units'])
        for changed in [sql.replace('oi.quantity', 'oi.line_paid_gross_cents'), sql.replace('SUM(', 'AVG('), sql.replace('oi.quantity ELSE', 'oi.quantity * 2 ELSE')]:
            with self.assertRaises(ValueError): validator.validate(contract, changed, query, database, graph, scope)


if __name__ == '__main__':
    unittest.main()
