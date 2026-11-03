"""Controlled model outputs exercise the real local MCP/readonly SQL path.

These checks establish controller behavior, not real-model business accuracy.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from app.database import SCHEMA, physical_table_name
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.querying.duckdb_engine import DuckDbEngine
from app.runtime_budget import RunBudget, current_budget
from app.runtime_budget import BudgetStop
from app.security import AccessController, AccessScope
from app.services.investigation_store import InvestigationStore
from app.skills import SkillRegistry
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_harness import _json, _fingerprint
from app.workflows.react_prompts import REACT_SYSTEM, DRAFT_SYSTEM, react_system_for_skills
from app import homenova
from app.retrieval.graph import SchemaGraphBuilder


SOURCE = 'ecommerce_ops'
QUESTION = '比较店铺GMV和订单，解释变化'


class ScriptModel:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def chat_json(self, system, user):
        self.calls.append((system, json.loads(user)))
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def graph():
    table = next(t for t in SCHEMA if t.get('database') == SOURCE and physical_table_name(t) == 'daily_store_metrics')
    return {'database': SOURCE,
            'tables': [{'id': table['id'], 'name': 'daily_store_metrics', 'database': SOURCE}],
            'fields': [{'id': table['id']+'.'+f['name'], 'table_id': table['id'],
                        'sql_name': 'daily_store_metrics.'+f['name'], **f}
                       for f in table['fields']], 'joins': []}


def query(column='gmv', *, goal='核对总体GMV', sql=None):
    return {'goal': goal, 'sql': sql or f'SELECT SUM({column}) AS value FROM daily_store_metrics',
            'input_result_ids': [],
            'query_contract': {'original_question': QUESTION, 'data_source': SOURCE,
                               'tables': ['daily_store_metrics'],
                               'metrics': [{'name': column, 'definition': '', 'source_fields': ['daily_store_metrics.'+column],
                                            'aggregation': 'sum'}], 'dimensions': [], 'filters': [],
                               'time_range': None, 'grain': 'overall', 'assumptions': [], 'open_questions': []}}


def action(kind, **payload):
    return {'action_reason': '核实当前目标', 'action': {'type': kind, **payload}}


def review(ids=('T1:R1',), **extra):
    return {'action': 'complete', 'reason': '依据完整结果', 'approved_text': '最终审核定稿',
            'answered': [{'question': QUESTION, 'evidence_ids': list(ids), 'strength': 'contribution'}],
            'unanswered': [], 'can_repair': False, 'repair_requests': [],
            'completion_check': {'goal_status': 'answered', 'metric_scope_matches': True,
                                 'capability_claims_supported': True, 'remaining_core_gaps': [],
                                 'completion_basis': '依据适用结果完成受控测试中的目标'}, **extra}


class ReactHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = InvestigationStore(Path(self.tmp.name)/'state.sqlite3')

    def harness(self, outputs):
        model = ScriptModel(outputs)
        engine = DuckDbEngine()
        workflow = SimpleNamespace(model_client=model, database_engine=engine, skills=SkillRegistry(), budgets={})
        workflow.mcp_client = lambda scope: LocalMcpClient(create_local_mcp_server(engine, AccessScope.from_dict(scope)))
        workflow._retrieve_schema = lambda state, charge_budget=True: {
            'schema_graph': graph(), 'selected_semantics': [], 'retrieval': {'selected_count': 3}}
        return ReactInvestigationHarness(workflow, self.store), model

    @staticmethod
    def state():
        return {'investigation_protocol_version': 1, 'task_id': 'T1', 'session_id': 'test-session', 'query': QUESTION,
                'standalone_query': QUESTION, 'required_questions': [QUESTION],
                'datasource_id': SOURCE, 'access_scope': AccessController().resolve('demo_current_sales').public(),
                'schema_graph': graph(), 'workspace': {}, 'scenario': {},
                'time_context': {'reference_now': '2026-10-29T10:00:00+08:00', 'rule': 'real_current_time'}}

    def run_bounded(self, harness, state=None, response=None, budget=None):
        budget = budget or RunBudget('T1')
        harness.workflow.budgets['T1'] = budget
        budget.start()
        token = current_budget.set(budget)
        try:
            result = harness.run(state or self.state(), response)
            return result, budget
        finally:
            budget.pause()
            current_budget.reset(token)

    def test_single_loop_real_mcp_and_approved_text_exact(self):
        h, model = self.harness([
            action('load_skill', names=['business_diagnosis']),
            action('query', queries=[query()]), action('deliver'),
            {'draft': '待审核草稿'}, review(),
        ])
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'completed')
        self.assertTrue(result.final_reviewed)
        self.assertEqual(result.analysis, '最终审核定稿')
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(budget.tools, 2)  # Skill read + one real MCP query.
        self.assertEqual(len(result.reviews), 1)
        next_context = model.calls[2][1]
        self.assertEqual(next_context['evidence'][0]['id'], 'T1:R1')
        self.assertGreater(next_context['evidence'][0]['rows'][0]['value'], 0)
        self.assertNotIn('schema_text', next_context['semantics'])
        self.assertNotIn('events', next_context['budget'])
        self.assertNotIn('model_usage', next_context['budget'])
        self.assertNotIn('result_ref', json.dumps(next_context))
        self.assertNotIn('result', next_context['last_observation']['queries'][0])

    def test_independent_batch_partial_failure_preserves_success(self):
        bad = query('does_not_exist', goal='核对另一个错误字段')
        h, model = self.harness([
            action('load_skill', names=['business_diagnosis']),
            action('query', queries=[query(), bad]),
            {'query_id': 'A3', 'sql': bad['sql'], 'query_contract': bad['query_contract']},
            {'draft': '成功部分及失败限制'}, review(action='partial', approved_text='审核认可的部分答案'),
        ])
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'partial')
        self.assertTrue(result.final_reviewed)
        self.assertEqual([a['status'] for a in result.plan], ['completed', 'completed', 'failed'])
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(budget.tools, 2)
        self.assertEqual(len(result.reviews), 1)

    def test_identical_failed_candidate_stops_without_querying(self):
        bad = query('does_not_exist')
        h, model = self.harness([action('load_skill', names=['business_diagnosis']),
                                 action('query', queries=[bad]),
                                 {'query_id': 'A2', 'sql': bad['sql'], 'query_contract': bad['query_contract']}])
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.final_reviewed)
        self.assertIn('无进展', result.stop_reason)
        self.assertEqual(budget.tools, 1)
        self.assertEqual(budget.repairs.get('A2'), 1)
        self.assertEqual(len(model.calls), 3)

    def test_batch_cannot_guess_result_from_same_batch(self):
        second = query('paid_orders', goal='不应在同批使用尚未取得结果')
        second['input_result_ids'] = ['T1:R1']
        h, model = self.harness([
            action('load_skill', names=['business_diagnosis']),
            action('query', queries=[query(), second]), action('deliver'), {'draft': '只用成功结果'}, review(),
        ])
        result, budget = self.run_bounded(h)
        self.assertEqual(len(result.evidence), 1)
        observation = model.calls[2][1]['last_observation']
        self.assertEqual(observation['queries'][1]['status'], 'blocked')
        self.assertEqual(budget.tools, 2)

    def test_final_review_requires_executable_gap_to_return_to_loop(self):
        gap = review(action='repair', approved_text='', can_repair=True,
                     repair_requests=[{'question': '补充支付订单数核对'}])
        h, model = self.harness([
            action('load_skill', names=['business_diagnosis']), action('query', queries=[query()]),
            action('deliver'), {'draft': '只有GMV'}, gap,
            action('query', queries=[query('paid_orders', goal='补充支付订单数')]),
            action('deliver'), {'draft': '新增订单证据'}, review(ids=('T1:R1', 'T1:R2')),
        ])
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.reviews), 2)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(budget.tools, 3)
        self.assertEqual(model.calls[5][1]['last_observation']['type'], 'delivery_repair')

    def test_clarification_and_restart_restore_same_task_and_budget(self):
        h, _ = self.harness([action('clarify', parameter='time_range', question='最近多久？', reason='期间影响结果')])
        first, budget = self.run_bounded(h)
        self.assertEqual(first.status, 'waiting_clarification')
        budget.tools = 4
        token = current_budget.set(budget)
        try:
            h._persist(h.records['T1'])
        finally:
            current_budget.reset(token)
        h2, model = self.harness([action('load_skill', names=['business_diagnosis']),
                                  action('query', queries=[query()]), action('deliver'), {'draft': '草稿'}, review()])
        restored = h2.restore('T1', 'demo_current_sales')
        self.assertIsNotNone(restored)
        self.assertEqual(h2.snapshot('T1').status, 'waiting_clarification')
        prior = h2.workflow.budgets['T1']
        result, _ = self.run_bounded(h2, restored.state, '2026年6月对5月', prior)
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(prior.tools, 6)
        self.assertIn('2026年6月对5月', json.dumps(model.calls[0][1], ensure_ascii=False))

    def test_restore_interrupted_does_not_call_model_or_reset_budget(self):
        h, model = self.harness([])
        record = ReactRecord(self.state(), actions=[{'id': 'A1', 'action_type': 'query', 'kind': 'query',
                                                    'status': 'running', 'goal': '中断查询', 'result_ids': []}])
        budget = RunBudget('T1', tools=3, spent=12)
        token = current_budget.set(budget)
        try:
            h._persist(record)
        finally:
            current_budget.reset(token)
        restored = h.restore('T1', 'demo_current_sales')
        self.assertEqual(restored.actions[0]['status'], 'interrupted')
        snapshot = h.snapshot('T1')
        self.assertEqual(snapshot.status, 'waiting_clarification')
        self.assertEqual(snapshot.clarification.parameter, 'resume')
        self.assertEqual(h.workflow.budgets['T1'].tools, 3)
        self.assertEqual(len(model.calls), 0)

    def test_semantic_retrieval_upserts_without_duplicate_or_loss(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        first = graph()
        first['fields'] = first['fields'][:2]
        h._inherit_semantics(task, {'schema_graph': first, 'selected_semantics': [{'id': 'gmv', 'definition': '金额'}]})
        second = graph()
        second['fields'] = second['fields'][1:4]
        h._inherit_semantics(task, {'schema_graph': second, 'selected_semantics': [{'id': 'gmv', 'definition': '金额'}, {'id': 'orders', 'definition': '订单'}]})
        self.assertEqual(len(task.field_pool), 4)
        self.assertEqual(len(task.metric_pool), 2)
        self.assertEqual(task.semantic_conflicts, [])
        second['fields'][0] = {**second['fields'][0], 'unit': 'conflicting_unit'}
        h._inherit_semantics(task, {'schema_graph': second})
        self.assertEqual(len(task.field_pool), 4)

    def test_unverified_draft_not_exposed_and_unknown_review_id_rejected(self):
        h, _ = self.harness([action('load_skill', names=['business_diagnosis']),
                              action('query', queries=[query()]), action('deliver'),
                              {'draft': '不得未经审核展示'}, review(ids=('OTHER:R9',))])
        result, _ = self.run_bounded(h)
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.final_reviewed)
        self.assertNotIn('不得未经审核展示', result.analysis)

    def test_calculation_reads_complete_rows_not_context_preview(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        ref = 'full-result'
        full = {'user_id': 'demo_current_sales', 'task_id': 'T1', 'datasource_id': SOURCE,
                'columns': ['v'], 'rows': [{'v': i} for i in range(100)], 'complete': True, 'total_rows': 100, 'sql': ''}
        self.store.save_result(ref, full)
        task.evidence = [{'id': 'T1:R1', 'result_ref': ref, 'check_status': 'basic_valid', 'complete': True,
                          'rows': full['rows'][:20], 'stored_rows': 100, 'columns': ['v']}]
        action_record = h._new_action(task, 'calculate', '全结果求和')
        h._calculate(task, action_record, {'result_id': 'T1:R1', 'operation': 'sum', 'column': 'v'})
        self.assertEqual(task.evidence[-1]['rows'][0]['sum'], sum(range(100)))
        with self.assertRaises(ValueError):
            h._calculate(task, action_record, {'result_id': 'T1:R1', 'operation': 'eval', 'code': '1+1'})

    def test_corrected_completed_upstream_invalidates_only_dependents(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        h._inherit_semantics(task, self.state())
        skill = h.workflow.skills.get('business_diagnosis')
        task.skills['business_diagnosis'] = {'instructions': skill.instructions, 'version': 'test'}
        budget = RunBudget('T1')
        budget.start()
        token = current_budget.set(budget)
        try:
            first = h._query(task, query(), '核对GMV')
            second_query = query('paid_orders', goal='依赖总体的订单核对')
            second_query['input_result_ids'] = ['T1:R1']
            h._query(task, second_query, '核对订单')
            h._query(task, query('visitors', goal='独立访客核对'), '核对访客')
            task.progress['facts'] = [{'text': f'发现{i}', 'evidence_ids': [f'T1:R{i}']} for i in (1, 2, 3)]
            task.progress['hypotheses'] = [{'text': '旧解释', 'status': 'supported', 'evidence_ids': ['T1:R2']}]
            correction = query(sql='SELECT SUM(gmv) AS value FROM daily_store_metrics WHERE gmv>0')
            correction['query_contract']['filters'] = [{'field': 'daily_store_metrics.gmv', 'operator': '>', 'value': 0}]
            correction['repair_action_id'] = first['action_id']
            outcome = h._query(task, correction, '更正已完成总体')
        finally:
            budget.pause()
            current_budget.reset(token)
        self.assertEqual(outcome['status'], 'completed', outcome)
        self.assertEqual([e['check_status'] for e in task.evidence], ['superseded', 'superseded', 'basic_valid', 'basic_valid'])
        self.assertEqual(task.progress['facts'], [{'text': '发现3', 'evidence_ids': ['T1:R3']}])
        self.assertEqual(task.progress['hypotheses'][0]['status'], 'open')
        self.assertEqual(task.progress['hypotheses'][0]['evidence_ids'], [])
        self.assertEqual(task.actions[1]['status'], 'blocked')
        self.assertEqual(task.actions[2]['status'], 'completed')
        self.assertEqual(budget.repairs['A1'], 1)

    def test_changed_feedback_does_not_re_review_same_draft_and_evidence(self):
        h, model = self.harness([{'draft': '相同草稿'}, review(ids=()), {'draft': ' 相 同 草 稿 '}])
        task = ReactRecord(self.state())
        budget = RunBudget('T1')
        budget.start()
        token = current_budget.set(budget)
        try:
            h._deliver(task)
            task.final_status = ''
            task.feedback = '新的审核反馈文字，但实际证据与草稿均未改变'
            with self.assertRaises(BudgetStop):
                h._deliver(task)
        finally:
            budget.pause()
            current_budget.reset(token)
        self.assertEqual(len(model.calls), 3)  # second draft allowed, second review denied.
        self.assertEqual(len(task.reviews), 1)

    def test_storage_failure_stops_before_any_model_or_tool(self):
        h, model = self.harness([])
        def broken(*args, **kwargs):
            raise OSError('disk full')
        h.store.save_task = broken
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'failed')
        self.assertIn('持久化失败', result.stop_reason)
        self.assertEqual(len(model.calls), 0)
        self.assertEqual(budget.tools, 0)

    def test_real_mcp_storage_error_is_fatal_not_sql_repair(self):
        h, model = self.harness([action('load_skill', names=['business_diagnosis']),
                                  action('query', queries=[query()])])
        def broken(*args, **kwargs):
            raise RuntimeError('disk full')
        h.workflow.database_engine.result_store = SimpleNamespace(save_result=broken)
        result, budget = self.run_bounded(h)
        self.assertEqual(result.status, 'failed')
        self.assertIn('持久化失败', result.stop_reason)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.plan[-1].get('error_kind'), 'storage')
        self.assertEqual(result.plan[-1]['status'], 'failed')
        self.assertEqual(budget.repairs, {})

    def test_missing_field_is_explicitly_bound_to_retrieval_workspace(self):
        h, _ = self.harness([])
        captured = []
        def retrieve(state, charge_budget=True):
            captured.append(state)
            return {'schema_graph': graph(), 'selected_semantics': [], 'retrieval': {}}
        h.workflow._retrieve_schema = retrieve
        task = ReactRecord(self.state())
        h._retrieve(task, {'goal': '订单和GMV', 'fields': ['daily_store_metrics.paid_orders'], 'metric_ids': []})
        self.assertEqual(captured[0]['workspace']['schema_fields'],
                         [{'tableId': 'ecommerce_ops.daily_store_metrics', 'name': 'paid_orders'}])
        self.assertEqual(task.observation['missing_requests']['fields'], [])
        h._retrieve(task, {'goal': '订单和GMV', 'fields': ['daily_store_metrics.paid_orders'], 'metric_ids': []})
        self.assertEqual(len(captured), 1)

    def test_same_unfilled_semantic_gap_does_not_repeat_retrieval(self):
        h, _ = self.harness([])
        count = []
        h.workflow._retrieve_schema = lambda state, charge_budget=True: (
            count.append(state) or {'schema_graph': graph(), 'selected_semantics': [], 'retrieval': {}})
        task = ReactRecord(self.state())
        request = {'goal': '补充尚未取得字段', 'fields': ['daily_store_metrics.missing'], 'metric_ids': []}
        h._retrieve(task, request)
        with self.assertRaises(BudgetStop):
            h._retrieve(task, request)
        self.assertEqual(len(count), 1)

    def test_resolve_time_only_dispatches_two_allowlisted_tools(self):
        h, _ = self.harness([])
        calls = []
        class Clock:
            def call_tool(self, name, arguments):
                calls.append((name, arguments))
                if name == 'current_datetime':
                    return {'date': '2026-10-29', 'datetime': '2026-10-29T10:00:00+00:00', 'timezone': 'UTC'}
                return {'expression': arguments['expression'], 'start_date': '2026-10-01',
                        'end_date': '2026-10-31', 'end_exclusive': '2026-11-01', 'timezone': 'UTC'}
        h.workflow.mcp_client = lambda scope: Clock()
        task = ReactRecord(self.state())
        h._resolve_time(task, {'type': 'resolve_time', 'timezone_name': 'UTC'})
        h._resolve_time(task, {'type': 'resolve_time', 'expression': '本月', 'timezone_name': 'UTC'})
        with self.assertRaises(ValueError):
            h._resolve_time(task, {'type': 'resolve_time', 'tool_name': 'query_ecommerce_ops', 'expression': '本月'})
        self.assertEqual([c[0] for c in calls], ['current_datetime', 'resolve_date_range'])
        self.assertEqual(task.observation['value']['start_date'], '2026-10-01')

    def test_resolve_time_caches_same_task_and_preserves_primary_period(self):
        h, _ = self.harness([])
        calls = []
        class Clock:
            def call_tool(self, name, arguments):
                calls.append((name, arguments))
                month = '10' if arguments['expression'] == '本月' else '09'
                return {'start_date': f'2026-{month}-01', 'end_date': f'2026-{month}-30', 'timezone': 'UTC'}
        h.workflow.mcp_client = lambda scope: Clock()
        task = ReactRecord(self.state())
        request = {'type': 'resolve_time', 'expression': '本月', 'timezone_name': 'UTC'}
        h._resolve_time(task, request)
        first = dict(task.state['confirmed_parameters']['time_range'])
        task.state['time_context']['reference_now'] = '2026-11-01T00:00:00+00:00'
        h._resolve_time(task, request)
        self.assertTrue(task.observation['cached'])
        self.assertEqual(len(calls), 1)
        h._resolve_time(task, {**request, 'expression': '上月'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(task.state['confirmed_parameters']['time_range'], first)

    def test_compact_public_semantics_preserve_definitions_and_input_headroom(self):
        h, _ = self.harness([])
        source = 'homenova_20260831'
        scope = AccessController().scoped('demo_admin', source)
        retrieval = homenova.selected_retrieval(source, 'GMV 销量', {'hits': []},
                                                {'retrieval_terms': ['GMV', '销量']})
        public_graph = SchemaGraphBuilder().build(retrieval['hits'], scope)
        state = {**self.state(), 'datasource_id': source, 'access_scope': scope.public(),
                 'scenario': homenova.scenario(source)}
        task = ReactRecord(state)
        h._inherit_semantics(task, {'schema_graph': public_graph, 'selected_semantics': retrieval['selected_semantics']})
        skill = h.workflow.skills.get('business_diagnosis')
        version = _fingerprint(skill.instructions)
        task.skills['business_diagnosis'] = {'instructions': skill.instructions, 'version': version}
        payload = h._context(task)
        metric = payload['semantics']['registered_metrics'][0]
        original = retrieval['selected_semantics'][0]
        for key in ('formula', 'definition', 'required_predicates', 'component_policies', 'maturity',
                    'unit', 'as_of_policy', 'duplicate_risks', 'time_fields'):
            self.assertEqual(metric[key], original[key], key)
        self.assertTrue(all(isinstance(ref, str) for ref in metric['required_fields']))
        fields_by_sql = {f['sql_name']: f for f in payload['semantics']['schema_graph']['fields']}
        self.assertIn('payments.captured_at', fields_by_sql)
        self.assertIn('order_items.line_paid_gross_cents', fields_by_sql)
        self.assertEqual(len(payload['semantics']['schema_graph']['joins']), len(public_graph['joins']))
        self.assertEqual(payload['semantic_directory']['fields'], [])
        self.assertEqual(payload['semantic_directory']['metrics'], [])
        system = REACT_SYSTEM + '\n\n' + f'【已加载 Skill：business_diagnosis，版本 {version}】\n' + skill.instructions
        upper_bound = RunBudget.estimate_tokens({'messages': [{'role': 'system', 'content': system},
                                                            {'role': 'user', 'content': _json(payload)}]})
        self.assertLess(upper_bound, 42000)

    def test_closing_supplies_old_used_definition_and_omits_unused_prefetch(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        h._inherit_semantics(task, self.state())
        task.metric_pool = {
            'gmv_registered': {'id': 'gmv_registered', 'label': 'GMV', 'definition': '已支付金额',
                               'required_fields': ['daily_store_metrics.gmv'], 'depends_on': ['base_amount']},
            'base_amount': {'id': 'base_amount', 'definition': '金额基础集合',
                            'required_fields': ['daily_store_metrics.stat_date']},
            'unused': {'id': 'unused', 'definition': '尚未使用的探索指标',
                       'required_fields': ['daily_store_metrics.visitors']},
        }
        task.active_metrics = ['unused']
        task.active_fields = [k for k, v in task.field_pool.items() if v['sql_name'] == 'daily_store_metrics.visitors']
        task.evidence = [{'id': 'T1:R1', 'check_status': 'basic_valid', 'complete': True,
                          'rows': [{'value': 7}], 'columns': ['value'], 'stored_rows': 1,
                          'query_contract': query()['query_contract']}]
        task.evidence[0]['query_contract']['metrics'][0]['name'] = 'GMV'
        payload = h._context(task, closing=True)
        selected_ids = {metric['id'] for metric in payload['semantics']['registered_metrics']}
        self.assertEqual(selected_ids, {'gmv_registered', 'base_amount'})
        field_names = {f['sql_name'] for f in payload['semantics']['schema_graph']['fields']}
        self.assertIn('daily_store_metrics.gmv', field_names)
        self.assertIn('daily_store_metrics.stat_date', field_names)
        self.assertNotIn('daily_store_metrics.visitors', field_names)
        self.assertNotIn('definition', payload['evidence'][0]['query_contract']['metrics'][0])
        self.assertNotIn('semantic_directory', payload)
        self.assertNotIn('evidence_directory', payload)

    def test_broad_retrieval_expands_metric_closure_and_caches_unrelated_hits(self):
        h, _ = self.harness([])
        source = 'homenova_20260831'
        scope = AccessController().scoped('demo_admin', source)
        retrieval = homenova.selected_retrieval(source, 'paid_goods_gmv_gross paid_order_count completed_cash_refunds_gross',
                                                {'hits': []}, {'retrieval_terms': ['GMV', '订单数', '退款']})
        required_ids = {item['doc_id'] for item in retrieval['hits']}
        extra_hits = []
        for table in SCHEMA:
            if table.get('database') != source:
                continue
            for spec in table['fields']:
                doc_id = f'{table["id"]}.{spec["name"]}'
                if doc_id in required_ids:
                    continue
                extra_hits.append({'doc_id': doc_id, 'table_id': table['id'], 'field_name': spec['name'],
                                   'field_label': spec.get('label', spec['name']), 'field_type': spec.get('type', '文本'),
                                   'field_description': spec.get('description', ''), 'field_role': spec.get('role', ''),
                                   'database_id': source})
                if len(extra_hits) == 20:
                    break
            if len(extra_hits) == 20:
                break
        wide_graph = SchemaGraphBuilder().build([*retrieval['hits'], *extra_hits], scope)
        task = ReactRecord({**self.state(), 'datasource_id': source, 'access_scope': scope.public()})
        h._inherit_semantics(task, {'schema_graph': wide_graph, 'selected_semantics': retrieval['selected_semantics']})
        payload = h._context(task)
        expanded = {f['sql_name'] for f in payload['semantics']['schema_graph']['fields']}
        required = set()
        for metric in retrieval['selected_semantics']:
            required.update(h._metric_field_references(metric))
        self.assertTrue(required <= expanded)
        self.assertLess(len(expanded), len(wide_graph['fields']))
        self.assertTrue(payload['semantic_directory']['fields'])
        omitted = next(v['sql_name'] for v in task.field_pool.values() if v['sql_name'] not in expanded)
        task.active_requested_fields = [omitted]
        requested = h._context(task)
        self.assertIn(omitted, {f['sql_name'] for f in requested['semantics']['schema_graph']['fields']})
        self.assertNotIn(omitted, requested['semantic_directory']['fields'])

    def test_cached_field_selection_keeps_registered_metric_context(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        h._inherit_semantics(task, self.state())
        task.metric_pool = {'gmv': {'id': 'gmv', 'definition': '已登记金额口径',
                                    'required_fields': ['daily_store_metrics.gmv']}}
        task.active_metrics = ['gmv']
        h._retrieve(task, {'fields': ['daily_store_metrics.paid_orders'], 'metric_ids': []})
        self.assertEqual(task.active_metrics, ['gmv'])
        payload = h._context(task)
        self.assertEqual(payload['semantics']['registered_metrics'][0]['definition'], '已登记金额口径')
        self.assertIn('daily_store_metrics.paid_orders', {f['sql_name'] for f in payload['semantics']['schema_graph']['fields']})

    def test_loaded_method_dedup_keeps_time_and_protocol_and_full_skill(self):
        self.assertEqual(react_system_for_skills(set()), REACT_SYSTEM)
        reduced = react_system_for_skills({'business_diagnosis'})
        for first, last in (('【时间和完整性】', '【语义与 Skill】'), ('【动作协议】', '【停止与费用】')):
            section = REACT_SYSTEM[REACT_SYSTEM.index(first):REACT_SYSTEM.index(last)]
            self.assertIn(section, reduced)
        h, model = self.harness([{'draft': '结构化草稿'}])
        task = ReactRecord(self.state())
        task.skills = {'business_diagnosis': {'version': 'v1', 'instructions': 'COMPLETE_BUSINESS_METHOD'},
                       'database_query': {'version': 'v1', 'instructions': 'SQL_TOOL_PROTOCOL_SENTINEL'}}
        h._call(task, 'draft', DRAFT_SYSTEM, h._context(task, closing=True))
        self.assertIn('COMPLETE_BUSINESS_METHOD', model.calls[0][0])
        self.assertNotIn('SQL_TOOL_PROTOCOL_SENTINEL', model.calls[0][0])

    def test_context_dedup_keeps_local_meaning_and_actual_pending_checks(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        metadata = {'gmv': {'kind': 'registered', 'metric_id': 'registered_gmv', 'definition': '登记定义',
                           'source_fields': ['long.dependencies'] * 50, 'source_unit': 'cents',
                           'binding_basis': 'alias', 'requires_review': True},
                    'new_metric': {'kind': 'task_local', 'definition': '同集合的新增度量',
                                   'source_fields': ['actual.field'], 'requires_review': True}}
        entry = {'id': 'T1:R1', 'rows': [{'gmv': 10, 'new_metric': 2}], 'stored_rows': 1,
                 'column_metadata': metadata, 'query_contract': {'validation': {
                     'column_metadata': metadata, 'pending_checks': [{'code': 'verify_cohort'}]}}}
        output = h._evidence_context(task, entry, 20, expanded_metric_names={'registered_gmv'})
        self.assertNotIn('column_metadata', output['query_contract']['validation'])
        self.assertNotIn('definition', output['column_metadata']['gmv'])
        self.assertEqual(output['column_metadata']['gmv']['source_unit'], 'cents')
        self.assertEqual(output['column_metadata']['new_metric'], metadata['new_metric'])
        self.assertEqual(output['query_contract']['validation']['pending_checks'], [{'code': 'verify_cohort'}])
        self.assertEqual(entry['column_metadata']['gmv']['definition'], '登记定义')

    def test_capability_directory_contains_only_authorized_source_tables(self):
        h, _ = self.harness([])
        task = ReactRecord(self.state())
        payload = h._capability_directory(task)
        allowed = AccessScope.from_dict(task.state['access_scope'])
        self.assertTrue(all(allowed.allows_table(SOURCE, t['id']) for t in payload['available_fact_domains']))
        self.assertFalse(any('fields' in t for t in payload['available_fact_domains']))

    def test_post_query_uses_actual_metrics_and_keeps_pending_checks(self):
        h, _ = self.harness([])
        source = 'homenova_20260831'
        scope = AccessController().scoped('demo_admin', source)
        retrieval = homenova.selected_retrieval(source, 'paid_goods_gmv_gross paid_order_count completed_cash_refunds_gross',
                                                {'hits': []}, {'retrieval_terms': ['GMV', '订单数', '退款']})
        public_graph = SchemaGraphBuilder().build(retrieval['hits'], scope)
        task = ReactRecord({**self.state(), 'datasource_id': source, 'access_scope': scope.public()})
        h._inherit_semantics(task, {'schema_graph': public_graph, 'selected_semantics': retrieval['selected_semantics']})
        skill = h.workflow.skills.get('business_diagnosis')
        task.skills = {'business_diagnosis': {'version': _fingerprint(skill.instructions), 'instructions': skill.instructions}}
        contract = {'data_source': source, 'original_question': '重复任务问题',
                    'tables': ['order_items', 'payments'], 'metrics': [
                        {'name': 'paid_goods_gmv_gross', 'definition': '重复的正式定义',
                         'source_fields': ['order_items.line_paid_gross_cents'], 'aggregation': 'sum'}],
                    'dimensions': [], 'filters': [{'field': 'payments.status', 'operator': '=', 'value': 'captured'}],
                    'time_range': {'field': 'payments.captured_at', 'start': '2026-08-01', 'end': '2026-09-01', 'end_inclusive': False},
                    'grain': 'overall', 'assumptions': [], 'open_questions': [],
                    'validation': {'normalizations': [{'path': 'metrics.0.definition', 'before': '原始重复说明'*100,
                                                      'after': '正式重复说明'*100}],
                                   'pending_checks': [{'code': 'expression_pending', 'message': '仍需核对等价表达'}],
                                   'sql_summary': {'normalized_sql': '重复SQL'*200, 'source_fields': ['payments.status'],
                                                   'predicates': [{'sql': "payments.status='captured'"}],
                                                   'aggregations': [{'sql': '重复聚合'*200}]}}}
        task.evidence = [{'id': 'T1:R1', 'check_status': 'basic_valid', 'complete': True,
                          'sql': 'SELECT SUM(line_paid_gross_cents) AS amount FROM order_items',
                          'rows': [{'amount': 100}], 'columns': ['amount'], 'stored_rows': 1,
                          'query_contract': contract}]
        task.observation = {'type': 'query_batch', 'queries': [{'status': 'completed', 'result_id': 'T1:R1'}]}
        payload = h._context(task)
        self.assertNotIn('completed_cash_refunds_gross', {m['id'] for m in payload['semantics']['registered_metrics']})
        selected_fields = {f['sql_name'] for f in payload['semantics']['schema_graph']['fields']}
        self.assertIn('payments.captured_at', selected_fields)
        self.assertIn('payments.status', selected_fields)
        self.assertTrue(all(not name.startswith('refunds.') for name in selected_fields))
        provided = payload['evidence'][0]['query_contract']
        self.assertEqual(provided['filters'], contract['filters'])
        self.assertEqual(provided['time_range'], contract['time_range'])
        self.assertEqual(provided['validation']['pending_checks'], contract['validation']['pending_checks'])
        self.assertEqual(provided['validation']['normalization_count'], 1)
        self.assertNotIn('normalizations', provided['validation'])
        self.assertNotIn('normalized_sql', provided['validation']['sql_summary'])
        self.assertEqual(payload['evidence'][0]['sql'], task.evidence[0]['sql'])
        system = react_system_for_skills(set(task.skills)) + '\n\n' + skill.instructions
        from app.workflows.react_harness import _prompt_json
        upper = RunBudget.estimate_tokens({'messages': [{'role': 'system', 'content': system},
                                                      {'role': 'user', 'content': _prompt_json(payload)}]})
        self.assertLess(upper, 48000)


if __name__ == '__main__':
    unittest.main()
