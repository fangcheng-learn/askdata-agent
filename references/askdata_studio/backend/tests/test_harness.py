import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.runtime_budget import RunBudget, BudgetStop, current_budget
from app.workflows.harness import InvestigationHarness, TaskRecord
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController
from app.skills import SkillRegistry
from app.preprocessing import RequestPreprocessor

QUESTION = '比较两期GMV并检验解释'

def step(ident='S1', deps=None, goal='比较两期GMV', kind='query', review=False):
    return dict(id=ident, goal=goal, kind=kind, depends_on=deps or [], input_fields={},
        expected_output='完整聚合', metric_ids=[], required_fields=[], review_after=review,
        review_reason='核实前提' if review else '')

def reviewed(action='complete', ids=None, final=False):
    return dict(action=action, answered=[dict(question=QUESTION, evidence_ids=ids or [], strength='fact', explanation='已核实')],
        unanswered=[], issue_type='none', affected_steps=[], reason='口径已核验', approved_text='审核定稿' if final else '')

class ScriptModel:
    def __init__(self, outputs):self.outputs=list(outputs);self.calls=[]
    def chat_json(self, system, user):
        self.calls.append((system,user));output=self.outputs.pop(0)
        if isinstance(output,Exception):raise output
        return output

class HarnessTest(unittest.TestCase):
    def harness(self, model, executions=None):
        executions=list(executions or []);calls=[]
        def prepare(*args, **kwargs):
            calls.append((args,kwargs));output=executions.pop(0)
            if isinstance(output,Exception):raise output
            return output
        workflow=SimpleNamespace(model_client=model,skills=SkillRegistry(),single_database_agent=SimpleNamespace(prepare=prepare),
            database_engine=DuckDbEngine(),data_qa_agent=SimpleNamespace(run=lambda *a:SimpleNamespace(report=None,answer='草稿')))
        harness=InvestigationHarness(workflow)
        def retrieve(task,query,fields=None):
            task.state.update(schema_graph={'tables':[],'fields':[]},schema_context='',retrieval={},database_names=['homenova_20260831'])
            task.retrievals.append({'query':query})
        harness._retrieve=retrieve
        return harness,calls
    @staticmethod
    def state():
        return dict(task_id='T1',query=QUESTION,standalone_query=QUESTION,required_questions=[QUESTION],intent={'action':'database_query'},
            datasource_id='homenova_20260831',scenario={},access_scope={'user_id':'admin'},workspace={},business_skills=['business_diagnosis'])
    @staticmethod
    def execution(value=3):
        return dict(action='executed',execution=dict(success=True,sql='SELECT 3 AS v',columns=['v'],rows=[{'v':value}],complete=True,total_rows=1),tool_trace=[],validation_attempts=[])
    def bounded(self,h,state,response=None,budget=None):
        budget=budget or RunBudget('T1');budget.start();token=current_budget.set(budget)
        try:return h.run(state,response)
        finally:budget.pause();current_budget.reset(token)
    def test_structured_dependencies_and_exact_approved_delivery(self):
        model=ScriptModel([{'conditions':{},'steps':[step(),step('S2',['S1'],'验证解释')]},reviewed(ids=['T1:R1','T1:R2'],final=True)])
        h,calls=self.harness(model,[self.execution(3),self.execution(8)]);r=self.bounded(h,self.state())
        self.assertEqual(r.status,'completed');self.assertTrue(r.final_reviewed);self.assertEqual(r.analysis,'审核定稿');self.assertEqual(len(model.calls),2)
        dependency=calls[1][1]['task_context']['dependency_results'][0]
        self.assertEqual(dependency['rows'],[{'v':3}]);self.assertEqual(dependency['id'],'T1:R1');self.assertEqual(r.plan[1]['input_result_ids'],['T1:R1'])
    def test_failed_upstream_blocks_only_dependencies_and_keeps_independent(self):
        model=ScriptModel([{'conditions':{},'steps':[step(),step('S2',['S1']),step('S3',goal='独立调查')]},reviewed('continue'),reviewed('partial',ids=['T1:R1'],final=True)])
        h,calls=self.harness(model,[RuntimeError('broken'),self.execution()]);r=self.bounded(h,self.state(),budget=RunBudget('T1',repair_limit=0))
        self.assertEqual([s['status'] for s in r.plan],['failed','blocked','completed']);self.assertEqual(r.status,'partial');self.assertEqual(len(calls),2)
    def test_clarification_restores_task_and_remaining_budget_without_entry(self):
        plan={'conditions':{},'steps':[step()],'clarification':{'parameter':'target','question':'目标是多少？','reason':'配置无目标','options':[]}}
        model=ScriptModel([plan,reviewed(ids=['T1:R1'],final=True)]);h,calls=self.harness(model,[self.execution()]);b=RunBudget('T1')
        first=self.bounded(h,self.state(),budget=b);self.assertEqual(first.status,'waiting_clarification');spent=b.spent
        second=self.bounded(h,self.state(),response='1000欧分',budget=b)
        self.assertEqual(second.task_id,first.task_id);self.assertGreaterEqual(b.spent,spent);self.assertEqual(len(model.calls),2);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1]['task_context']['conditions']['target'],'1000欧分')
    def test_false_premise_makes_diagnostics_not_applicable(self):
        condition=dict(id='C1',description='比较前提未成立',affected_requirement_ids=['Q1'],affected_step_ids=['S2'])
        second={**step('S2',['S1']),'entry_conditions':[condition]}
        proposal=dict(action='request_delivery',premise_status='refuted',condition_id='C1',evidence_ids=['T1:R1'],
                      affected_requirement_ids=['Q1'],affected_step_ids=['S2'],reason='受控前提不成立')
        model=ScriptModel([{'conditions':{},'steps':[step(review=True),second]},reviewed(ids=['T1:R1'],final=True)])
        h,calls=self.harness(model,[self.execution(),dict(action='step_decision',step_decision=proposal)]);r=self.bounded(h,self.state())
        self.assertEqual(r.status,'completed');self.assertEqual(len(calls),2);self.assertEqual(r.plan[1]['status'],'not_applicable')
        self.assertTrue(all(item['final'] for item in r.reviews))
    def test_invalidation_preserves_independent_results_and_old_versions(self):
        t=TaskRecord(self.state(),[QUESTION]);t.steps=[{**step(),'status':'completed','result_ids':['R1']},{**step('S2',['S1']),'status':'completed','result_ids':['R2']},{**step('S3'),'status':'completed','result_ids':['R3']}]
        t.evidence=[{'id':f'R{i}','check_status':'reviewed'} for i in range(1,4)];InvestigationHarness.invalidate(t,'S1')
        self.assertEqual([r['check_status'] for r in t.evidence],['superseded','superseded','reviewed']);self.assertEqual(t.steps[2]['result_ids'],['R3'])
    def test_unreviewed_budget_stop_never_exposes_draft(self):
        model=ScriptModel([{'conditions':{},'steps':[step()]},BudgetStop('总时间耗尽'),BudgetStop('总时间耗尽')])
        h,_=self.harness(model,[self.execution()]);r=self.bounded(h,self.state())
        self.assertEqual(r.status,'partial');self.assertFalse(r.final_reviewed);self.assertNotEqual(r.analysis,'草稿');self.assertIn('尚未完成',r.message)
    def test_final_review_cannot_invent_result_references(self):
        model=ScriptModel([{'conditions':{},'steps':[step()]},reviewed(ids=['OTHER:R9'],final=True),RuntimeError('unavailable')])
        h,_=self.harness(model,[self.execution()]);r=self.bounded(h,self.state());self.assertFalse(r.final_reviewed);self.assertEqual(r.status,'partial')

class BudgetTest(unittest.TestCase):
    def test_shared_tools_repairs_and_replanning(self):
        b=RunBudget('T');b.start()
        for _ in range(20):b.tool('query')
        with self.assertRaises(BudgetStop):b.tool('21st')
        b.repair('format');b.repair('network')
        with self.assertRaises(BudgetStop):b.repair('SQL')
        b.replan()
        with self.assertRaises(BudgetStop):b.replan()
    def test_wait_is_paused_and_closing_reserves_one_minute(self):
        with patch('app.runtime_budget.time.monotonic',return_value=10):b=RunBudget('T');b.start()
        with patch('app.runtime_budget.time.monotonic',return_value=250):
            self.assertEqual(b.remaining(),0)
            with self.assertRaises(BudgetStop):b.check()
            b.phase='closing';self.assertEqual(b.remaining(),60);b.pause()
        with patch('app.runtime_budget.time.monotonic',return_value=10000):self.assertEqual(b.elapsed(),240);b.start();self.assertEqual(b.remaining(),60)
        with patch('app.runtime_budget.time.monotonic',return_value=10060):
            with self.assertRaises(BudgetStop):b.check()
    def test_preprocessing_selects_skills_in_same_call(self):
        model=ScriptModel([{'action':'database_query','standalone_query':QUESTION,'needs_planner':True,'business_skills':['business_diagnosis','untrusted_path'],'required_questions':[QUESTION]}])
        r=RequestPreprocessor(model).prepare(QUESTION,'');self.assertTrue(r.needs_planner);self.assertEqual(r.business_skills,['business_diagnosis']);self.assertEqual(len(model.calls),1)

class FullResultTest(unittest.TestCase):
    def test_preview_and_scoped_full_result(self):
        engine=DuckDbEngine();scope=AccessController().scoped('demo_admin','homenova_20260831');b=RunBudget('TASK');b.start();token=current_budget.set(b)
        try:r=engine.execute('homenova_20260831','SELECT order_id FROM orders',scope)
        finally:current_budget.reset(token)
        self.assertTrue(r.success);self.assertEqual(len(r.rows),200);self.assertTrue(r.preview_truncated);self.assertTrue(r.complete);self.assertGreater(r.total_rows,200)
        page=engine.read_result(r.result_ref,scope.user_id,'TASK','homenova_20260831',200,200);self.assertEqual(len(page['rows']),200)
        for user,task,source in [('other','TASK','homenova_20260831'),(scope.user_id,'OTHER','homenova_20260831'),(scope.user_id,'TASK','homenova_20261015')]:
            with self.assertRaises(PermissionError):engine.read_result(r.result_ref,user,task,source)



class ContractLineageTest(unittest.TestCase):
    def test_cte_aggregate_resolves_to_physical_column(self):
        from app.querying.query_contract import QueryContractValidator
        contract = dict(original_question='查询销售额', data_source='askdata_mock', tables=['orders_current'],
            metrics=[dict(name='销售额', definition='客户实际支付金额，可用于计算实际成交金额',
                          source_fields=['orders_current.paid_amount'], aggregation='sum')],
            dimensions=[], filters=[], time_range=None, grain='overall', assumptions=[], open_questions=[])
        graph = {'tables':[{'name':'orders_current','id':'orders_current','database':'askdata_mock'}]}
        scope=AccessController().resolve('demo_analyst').public()
        actual=QueryContractValidator(DuckDbEngine()).validate(contract,
            'WITH x AS (SELECT paid_amount AS v FROM orders_current) SELECT SUM(v) AS total FROM x',
            '查询销售额','askdata_mock',graph,scope)
        self.assertTrue(actual['validation']['sql_compiled'])

    def test_wrong_table_filter_is_rejected_despite_same_column_name(self):
        from app.querying.query_contract import QueryContractValidator
        from app.database import SCHEMA
        tables=[t for t in SCHEMA if t.get('database')=='askdata_mock' and t.get('name') in {'orders_current','orders_history'}]
        graph={'tables':tables}
        contract=dict(original_question='查询地区',data_source='askdata_mock',tables=['orders_current','orders_history'],
            metrics=[],dimensions=['orders_current.region'],filters=[{'field':'orders_current.region','operator':'=','value':'华东'}],
            time_range=None,grain='detail',assumptions=[],open_questions=[])
        sql="SELECT a.region FROM orders_current a JOIN orders_history b ON a.region=b.region WHERE b.region='华东'"
        with self.assertRaises(ValueError):
            QueryContractValidator(DuckDbEngine()).validate(contract,sql,'查询地区','askdata_mock',graph,AccessController().resolve('demo_analyst').public())


class MetricEquivalenceTest(unittest.TestCase):
    def test_registered_count_accepts_only_equal_inner_join_key(self):
        from app.querying.query_contract import QueryContractValidator
        from app import homenova
        source='homenova_20260831'
        metric=homenova.registered_metric(source,'paid_order_count')
        tables=homenova.schema_payload(source)['tables']
        graph={'tables':[t for t in tables if t['name'] in {'payments','orders'}]}
        contract=dict(original_question='统计成功支付订单量',data_source=source,tables=['payments','orders'],
            metrics=[dict(name=metric['id'],definition=metric['definition'],source_fields=['orders.order_id'],aggregation='count_distinct')],
            dimensions=[],filters=[dict(field='payments.status',operator='=',value='captured')],time_range=None,grain='overall',assumptions=[],open_questions=[])
        sql="SELECT COUNT(DISTINCT o.order_id) AS n FROM payments p JOIN orders o ON p.order_id=o.order_id WHERE p.status='captured'"
        validator=QueryContractValidator(DuckDbEngine());scope=AccessController().scoped('demo_admin',source).public()
        actual=validator.validate(contract,sql,'统计成功支付订单量',source,graph,scope)
        self.assertTrue(actual['validation']['sql_compiled'])
        contract['metrics'][0]['source_fields']=['payments.order_id']
        validator.validate(contract,sql,'统计成功支付订单量',source,graph,scope)
        outer=validator.validate(contract,sql.replace(' JOIN orders',' LEFT JOIN orders'),'统计成功支付订单量',source,graph,scope)
        self.assertIn('distinct_count_not_proven', [item['code'] for item in outer['validation']['pending_checks']])
        self.assertFalse(outer['validation']['business_definition_verified'])


class DerivedGuardContractTest(unittest.TestCase):
    def test_projection_zero_denominator_guard_is_not_a_population_filter(self):
        from app.querying.query_contract import QueryContractValidator
        from app import homenova
        source='homenova_20260831';metric=homenova.registered_metric(source,'paid_order_count')
        contract=dict(original_question='统计支付订单量',data_source=source,tables=['payments'],
            metrics=[dict(name=metric['id'],definition=metric['definition'],source_fields=['payments.order_id'],aggregation='count_distinct')],
            dimensions=[],filters=[dict(field='payments.status',operator='=',value='captured')],time_range=None,grain='overall',assumptions=[],open_questions=[])
        graph={'tables':[t for t in homenova.schema_payload(source)['tables'] if t['name']=='payments']}
        sql="WITH x AS (SELECT COUNT(DISTINCT order_id) AS n FROM payments WHERE status='captured') SELECT n, CASE WHEN n>0 THEN 100.0/n ELSE NULL END AS v FROM x"
        result=QueryContractValidator(DuckDbEngine()).validate(contract,sql,'统计支付订单量',source,graph,AccessController().scoped('demo_admin',source).public())
        self.assertTrue(result['validation']['sql_compiled'])


class FreeTextClarificationTest(unittest.TestCase):
    def test_business_parameter_allows_text_but_database_and_schema_choices_do_not(self):
        from app.services.askdata_service import AskDataService
        from app.models import QueryResult
        from app.database import SCHEMA
        def make(parameter, ident):
            return QueryResult(task_id='t',status='waiting_clarification',route='database_query',message='补充',
                clarification={'parameter':parameter,'question':'补充条件','reason':'缺参数','options':[{'id':ident,'label':'选择','description':'选择'}]})
        self.assertTrue(AskDataService._free_clarification(make('target','provide_value')))
        self.assertFalse(AskDataService._free_clarification(make('database','homenova_20260831')))
        self.assertFalse(AskDataService._free_clarification(make('table',SCHEMA[0]['id'])))


class DisplayAliasContractTest(unittest.TestCase):
    def test_physical_definition_and_lineage_allow_display_alias(self):
        from app.querying.query_contract import QueryContractValidator
        contract=dict(original_question='查询销售额', data_source='askdata_mock',tables=['orders_current'],
            metrics=[dict(name='total_sales',definition='客户实际支付金额，可用于计算实际成交金额',source_fields=['orders_current.paid_amount'],aggregation='sum')],
            dimensions=[],filters=[],time_range=None,grain='overall',assumptions=[],open_questions=[])
        graph={'tables':[{'name':'orders_current','id':'orders_current','database':'askdata_mock'}]}
        validator=QueryContractValidator(DuckDbEngine());scope=AccessController().resolve('demo_analyst').public()
        validator.validate(contract,'SELECT SUM(paid_amount) AS total_sales FROM orders_current','查询销售额','askdata_mock',graph,scope)
        other=validator.validate(contract,'SELECT SUM(paid_amount) AS another_name FROM orders_current','查询销售额','askdata_mock',graph,scope)
        self.assertEqual(other['validation']['metric_output_bindings'][0]['output_aliases'],['another_name'])
        with self.assertRaises(ValueError):
            validator.validate(contract,'SELECT COUNT(*) AS another_name FROM orders_current','查询销售额','askdata_mock',graph,scope)


class RepairControllerTest(unittest.TestCase):
    harness = HarnessTest.harness
    state = staticmethod(HarnessTest.state)
    execution = staticmethod(HarnessTest.execution)
    bounded = HarnessTest.bounded
    def test_review_repair_recomputes_only_affected_branch(self):
        repair=reviewed('repair',ids=['T1:R1']);repair['affected_steps']=['S2','S3'];repair['issue_type']='semantics'
        model=ScriptModel([{'conditions':{},'steps':[step(),step('S2',goal='待修正步骤',review=True),step('S3',['S2']),step('S4',goal='独立结果')]},
            repair,reviewed(ids=['T1:R1','T1:R4','T1:R5','T1:R6'],final=True)])
        h,calls=self.harness(model,[self.execution(10),self.execution(3),self.execution(9),self.execution(20),self.execution(8),self.execution(11)])
        b=RunBudget('T1');result=self.bounded(h,self.state(),budget=b)
        self.assertEqual(result.status,'completed');self.assertEqual(len(calls),6)
        self.assertEqual(result.evidence[0]['check_status'],'reviewed');self.assertEqual(result.evidence[1]['check_status'],'superseded')
        self.assertEqual(calls[5][1]['task_context']['dependency_results'][0]['rows'],[{'v':8}]);self.assertEqual(b.repairs,{'S2':1})


class DeadlineEnforcementTest(unittest.TestCase):
    def test_exhausted_tool_budget_prevents_mcp_execution(self):
        from app.mcp_runtime.client import LocalMcpClient
        budget=RunBudget('T',tool_limit=0);budget.start();token=current_budget.set(budget)
        try:
            client=LocalMcpClient(None)
            with patch.object(client,'_call_tool') as call:
                with self.assertRaises(BudgetStop):client.call_tool('query_homenova_20260831',{'sql':'SELECT 1 AS n'})
                call.assert_not_called()
        finally:current_budget.reset(token)
    def test_model_transport_timeout_obeys_remaining_active_time(self):
        from app.model_client import ModelClient
        from app.config import Settings
        from unittest.mock import MagicMock
        budget=RunBudget('T',seconds=10,investigation_seconds=8)
        with patch('app.runtime_budget.time.monotonic',return_value=10):budget.start()
        token=current_budget.set(budget)
        try:
            response=MagicMock();response.__enter__.return_value.read.return_value=b'{"ok":true}'
            with patch('app.runtime_budget.time.monotonic',return_value=12),patch('app.model_client.urllib.request.urlopen',return_value=response) as call:
                result=ModelClient(Settings(api_key='test-only-non-secret'))._post('https://example.invalid/test',{})
                self.assertTrue(result['ok']);self.assertLessEqual(call.call_args.kwargs['timeout'],6)
        finally:current_budget.reset(token)

class ContinueGapTest(unittest.TestCase):
    harness = HarnessTest.harness
    state = staticmethod(HarnessTest.state)
    def test_continue_with_no_queued_work_repairs_named_completed_step(self):
        h,_=self.harness(ScriptModel([]))
        task=TaskRecord(self.state(),[QUESTION]);task.steps=[{**step(), 'status':'completed','result_ids':['R1']}]
        task.evidence=[{'id':'R1','check_status':'basic_valid'}]
        review=reviewed('continue');review.update(unanswered=[QUESTION],issue_type='evidence_gap',affected_steps=['S1'])
        task.pending_review=review;b=RunBudget('T1');b.start();token=current_budget.set(b)
        try:h._controller({'record':task})
        finally:current_budget.reset(token)
        self.assertEqual(task.next_node,'execute');self.assertEqual(task.steps[0]['status'],'pending');self.assertEqual(b.repairs,{'S1':1})

class NumericAggregationTest(unittest.TestCase):
    def test_unregistered_default_does_not_forbid_typed_quantity_sum(self):
        from app.querying.query_contract import QueryContractValidator
        from app import homenova
        source='homenova_20260831';table=next(t for t in homenova.schema_payload(source)['tables'] if t['name']=='order_items')
        field=next(f for f in table['fields'] if f['name']=='quantity')
        contract=dict(original_question='统计商品行总件数',data_source=source,tables=['order_items'],
            metrics=[dict(name='total_quantity',definition=field['description'],source_fields=['order_items.quantity'],aggregation='sum')],
            dimensions=[],filters=[],time_range=None,grain='overall',assumptions=[],open_questions=[])
        validator=QueryContractValidator(DuckDbEngine());scope=AccessController().scoped('demo_admin',source).public()
        validator.validate(contract,'SELECT SUM(quantity) AS units FROM order_items','统计商品行总件数',source,{'tables':[table]},scope)
        contract['metrics'][0]['aggregation']='none'
        with self.assertRaises(ValueError):validator.validate(contract,'SELECT SUM(quantity) AS units FROM order_items','统计商品行总件数',source,{'tables':[table]},scope)

class JsonModeTest(unittest.TestCase):
    def test_json_mode_keeps_existing_model_and_thinking_configuration(self):
        from app.model_client import ModelClient
        from app.config import Settings
        from unittest.mock import patch
        client=ModelClient(Settings(api_key='test-only',llm_model='existing-model',llm_enable_thinking=False))
        with patch.object(client, '_post', return_value={'choices':[{'message':{'content':'{"ok":true}'}}]}) as call:
            self.assertEqual(client.chat_json('Return JSON','test'),{'ok':True})
            payload=call.call_args.args[1]
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            self.assertEqual(payload['model'],'existing-model');self.assertFalse(payload['enable_thinking'])

class TimestampEquivalenceTest(unittest.TestCase):
    def test_date_only_and_explicit_berlin_midnight_are_equal_not_just_prefixes(self):
        from app.querying.query_contract import QueryContractValidator
        from app import homenova
        source='homenova_20260831';metric=homenova.registered_metric(source,'paid_order_count')
        table=next(t for t in homenova.schema_payload(source)['tables'] if t['name']=='payments')
        contract=dict(original_question='查询2026年8月成功支付订单数',data_source=source,tables=['payments'],
            metrics=[dict(name=metric['id'],definition=metric['definition'],source_fields=['payments.order_id'],aggregation='count_distinct')],
            dimensions=[],filters=[dict(field='payments.status',operator='=',value='captured')],
            time_range=dict(field='payments.captured_at',start='2026-08-01',end='2026-09-01'),grain='overall',assumptions=[],open_questions=[])
        sql="SELECT COUNT(DISTINCT order_id) AS n FROM payments WHERE status='captured' AND captured_at>='2026-07-31T22:00:00Z' AND captured_at<'2026-09-01T00:00:00+02:00'"
        validator=QueryContractValidator(DuckDbEngine());scope=AccessController().scoped('demo_admin',source).public()
        validator.validate(contract,sql,contract['original_question'],source,{'tables':[table]},scope)
        with self.assertRaises(ValueError):validator.validate(contract,sql.replace('2026-07-31T22:00:00Z','2026-07-31T23:00:00Z'),contract['original_question'],source,{'tables':[table]},scope)

class FiniteResultTest(unittest.TestCase):
    def test_nonfinite_result_becomes_repairable_query_failure(self):
        engine=DuckDbEngine();scope=AccessController().scoped('demo_admin','homenova_20260831')
        result=engine.execute('homenova_20260831','SELECT 1.0/0 AS invalid_ratio',scope)
        self.assertFalse(result.success);self.assertIn('NULLIF',result.error)
        repaired=engine.execute('homenova_20260831','SELECT 1.0/NULLIF(0,0) AS valid_unknown',scope)
        self.assertTrue(repaired.success);self.assertIsNone(repaired.rows[0]['valid_unknown'])

class SkillInjectionTest(unittest.TestCase):
    def test_selected_method_reaches_planner_and_reviewer_system_messages(self):
        model=ScriptModel([{'conditions':{},'steps':[step()]},reviewed(ids=['T1:R1'],final=True)])
        fixture=HarnessTest();h,_=fixture.harness(model,[fixture.execution()]);result=fixture.bounded(h,fixture.state())
        self.assertTrue(result.final_reviewed)
        chosen=SkillRegistry().get('business_diagnosis').instructions
        unrelated=SkillRegistry().get('goal_analysis').instructions
        for system,_ in model.calls:
            self.assertIn(chosen,system);self.assertNotIn(unrelated,system)

class TimeoutRoutingTest(unittest.TestCase):
    def test_mcp_timeout_becomes_budget_stop_and_counts_failed_call(self):
        from app.mcp_runtime.client import LocalMcpClient
        from unittest.mock import AsyncMock
        b=RunBudget('T');b.start();token=current_budget.set(b)
        try:
            client=LocalMcpClient(None)
            with patch.object(client,'_call_tool',new=AsyncMock(side_effect=TimeoutError)):
                with self.assertRaises(BudgetStop):client.call_tool('query_homenova_20260831',{'sql':'SELECT 1 AS n'})
            self.assertEqual(b.tools,1)
        finally:current_budget.reset(token)

class DeliveryClaimTest(unittest.TestCase):
    def test_aov_cannot_be_used_to_exclude_sku_price_or_mix_changes(self):
        check=InvestigationHarness.delivery_text_issues
        self.assertTrue(check('客单价上升排除了低价促销或低价商品结构作为下降主因。'))
        self.assertTrue(check('客单价上升。这表明下降并非由低值商品结构主导。'))
        self.assertFalse(check('客单价上升仅支持算术分解，不能排除SKU单价下降或商品组合变化。'))
        self.assertFalse(check('客单价上升部分抵消订单量减少；价格和商品结构尚未验证。'))

class DeliveryGateFlowTest(unittest.TestCase):
    harness = HarnessTest.harness
    state = staticmethod(HarnessTest.state)
    execution = staticmethod(HarnessTest.execution)
    bounded = HarnessTest.bounded

    def test_invalid_approved_draft_is_returned_to_reviewer_before_display(self):
        bad=reviewed(ids=['T1:R1'],final=True)
        bad['approved_text']='客单价上升排除了商品结构或降价作为主因。'
        good=reviewed(ids=['T1:R1'],final=True)
        good['approved_text']='订单量与客单价支持算术分解；SKU价格和商品结构尚未验证。'
        model=ScriptModel([{'conditions':{},'steps':[step()]},bad,good])
        h,_=self.harness(model,[self.execution()]);b=RunBudget('T1')
        drafts=iter(['需要修正的初稿','删除无依据推断后的新稿'])
        h.workflow.data_qa_agent.run=lambda *a:SimpleNamespace(report=None,answer=next(drafts))
        result=self.bounded(h,self.state(),budget=b)
        self.assertTrue(result.final_reviewed);self.assertEqual(result.analysis,good['approved_text'])
        self.assertEqual(b.repairs,{'closing':1});self.assertEqual(len(model.calls),3)
        self.assertFalse(result.reviews[0]['programmatic_text_check']['passed'])
        self.assertIn('交付未通过',model.calls[-1][1])

    def test_unchanged_draft_stops_before_another_review(self):
        bad=reviewed(ids=['T1:R1'],final=True)
        bad['approved_text']='客单价上升排除了商品结构或降价作为主因。'
        model=ScriptModel([{'conditions':{},'steps':[step()]},bad,bad,bad])
        h,_=self.harness(model,[self.execution()]);b=RunBudget('T1')
        result=self.bounded(h,self.state(),budget=b)
        self.assertEqual(result.status,'partial');self.assertFalse(result.final_reviewed)
        self.assertNotIn(bad['approved_text'],result.analysis)
        self.assertEqual(b.repairs,{'closing':1});self.assertEqual(len(model.calls),2)
        self.assertIn('未变化',result.stop_reason)

    def test_modified_existing_global_condition_invalidates_prior_evidence(self):
        model=ScriptModel([reviewed(ids=['T1:R2','T1:R3'],final=True)])
        h,calls=self.harness(model,[self.execution(20),self.execution(30)])
        task=TaskRecord(self.state(),[QUESTION],conditions={'period':'June'})
        task.retrievals=[{}];task.steps=[{**step(),'status':'completed','result_ids':['T1:R1'],'input_result_ids':[]},
            {**step('S2',['S1']),'status':'pending','result_ids':[],'input_result_ids':[]}]
        task.evidence=[{'id':'T1:R1','step_id':'S1','check_status':'reviewed','columns':['v'],'rows':[{'v':10}],'complete':True}]
        task.clarification={'parameter':'period','question':'比较期？','reason':'业务条件','options':[]}
        task.state.update(schema_graph={'tables':[],'fields':[]},schema_context='',retrieval={},database_names=['homenova_20260831'])
        h.records['T1']=task
        result=self.bounded(h,self.state(),response='July')
        self.assertEqual(result.evidence[0]['check_status'],'superseded');self.assertEqual(len(calls),2)
        self.assertEqual(calls[1][1]['task_context']['dependency_results'][0]['rows'],[{'v':20}])

class ModelUsageAccountingTest(unittest.TestCase):
    def test_records_provider_usage_per_node_without_prompt_or_credentials(self):
        from app.model_client import ModelClient
        from app.config import Settings
        from unittest.mock import MagicMock
        b=RunBudget('T');b.start();b.model_node='query_coder';b.step='S2'
        token=current_budget.set(b)
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({
            'ok':True,'usage':{'prompt_tokens':120,'completion_tokens':30,'total_tokens':150,
                'prompt_tokens_details':{'cached_tokens':80},'sensitive_string':'must-not-be-recorded'}}).encode()
        try:
            with patch('app.model_client.urllib.request.urlopen',return_value=response):
                ModelClient(Settings(api_key='unit-test-secret'))._post('https://example.invalid/test',{'model':'test-model','messages':[{'role':'user','content':'private prompt'}]})
        finally:current_budget.reset(token)
        event=b.public()['model_usage'][0]
        self.assertEqual(event['node'],'query_coder');self.assertEqual(event['step_id'],'S2')
        self.assertEqual(event['usage']['total_tokens'],150)
        self.assertEqual(event['usage']['prompt_tokens_details']['cached_tokens'],80)
        self.assertGreater(event['request_characters'],0)
        serialized=json.dumps(b.public())
        for secret in ['unit-test-secret','private prompt','must-not-be-recorded']:
            self.assertNotIn(secret,serialized)

if __name__=='__main__':unittest.main()
