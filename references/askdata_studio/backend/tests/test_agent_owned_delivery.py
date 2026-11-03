"""Offline regressions: model-owned metadata answers and report-only follow-ups."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from langgraph.checkpoint.memory import InMemorySaver
from app.preprocessing import RequestPreprocessor
from app.querying.data_qa_agent import DataQaAgent
from app.skills import SkillRegistry
from app.workflows.query_graph import QueryWorkflow
from app.services.askdata_service import AskDataService
from app.runtime_budget import RunBudget
from app.foundry_adapter.projection import result_text, artifacts
from app.models import QueryResult
from app.security import AccessScope
from tests import test_resilient_delivery as fixtures
from tests import test_multiturn_evidence as multi

class MetadataAgentTest(TestCase):
    def test_count_list_and_summary_all_reach_agent_and_authorized_catalog(self):
        tables = [{'id':'visible','name':'orders','database':'db','fields':[{'name':'amount','type':'int'}]},
                  {'id':'hidden','name':'private','database':'db','fields':[{'name':'secret'}]}]
        scope={'user_id':'u','allowed_databases':['db'],'allowed_tables':['visible']}
        for query, answer in [('当前一共几张表','当前授权范围共有1张表。'),
                              ('展示数据源中的表有哪些','当前可见orders表。'),
                              ('展示数据源中的表，并总结每张表的主要字段','orders表包含amount金额字段。')]:
            with self.subTest(query=query):
                model=Mock();model.chat_json.side_effect=[{'action':'metadata_query','confidence':1,'reason':'读取目录'},
                    {'action':'answer','answer':answer,'tool_calls':[]}]
                workflow=object.__new__(QueryWorkflow)
                workflow.preprocessor=RequestPreprocessor(model)
                workflow.config=SimpleNamespace(harness_react_enabled=True)
                workflow.checkpointer=InMemorySaver()
                workflow.data_qa_agent=DataQaAgent(model,Mock(side_effect=AssertionError('No SQL or display tools')),SkillRegistry().get('data_qa'))
                with patch('app.workflows.query_graph.SCHEMA',tables):
                    output=workflow._compile().invoke({'task_id':'T','datasource_id':'db','query':query,'access_scope':scope},
                        config={'configurable':{'thread_id':'T'}})['result']
                self.assertEqual(model.chat_json.call_count,2)
                self.assertEqual(output['analysis'],answer)
                catalog=json.loads(model.chat_json.call_args_list[1].args[1])['context']['authorized_metadata']
                self.assertEqual(catalog['table_count'],1)
                self.assertEqual(catalog['tables'][0]['name'],'orders')
                self.assertNotIn('secret',json.dumps(catalog))
                self.assertEqual(output['rows'],[], 'Do not dump raw fields as the user-facing result')

    def test_route_is_model_decision_even_for_previous_hardcoded_phrase(self):
        model=Mock();model.chat_json.return_value={'action':'direct_response','response':'模型决定的说明','confidence':1}
        out=RequestPreprocessor(model).prepare('展示数据源中的表和字段','')
        self.assertEqual(out.action,'direct_response');model.chat_json.assert_called_once()

class DirectReportTest(TestCase):
    setUp=fixtures.ResilientDeliveryTest.setUp
    harness=fixtures.ResilientDeliveryTest.harness
    state=staticmethod(fixtures.ResilientDeliveryTest.state)
    saved_task=fixtures.ResilientDeliveryTest.saved_task
    run_bounded=fixtures.ResilientDeliveryTest.run_bounded

    def test_report_delivered_once_without_review_or_false_certification(self):
        h,_,task=self.saved_task()
        h.workflow.config=SimpleNamespace(report_content_review_enabled=False)
        h.workflow.data_qa_agent=DataQaAgent(None,lambda *_:None,SkillRegistry().get('data_qa'))
        before=deepcopy(task.evidence)
        task.report_only=True
        with patch.object(h,'_call',return_value={'title':'经营报告','markdown':'已有结果的分析[T1:R1]'}) as call:
            result,_=self.run_bounded(h,task.state,budget=RunBudget('T1',react_rounds=8))
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[1],'draft')
        self.assertIsNotNone(result.report)
        self.assertFalse(result.final_reviewed)
        self.assertEqual(result.content_review_status,'skipped')
        self.assertEqual(task.evidence,before)
        self.assertIn('未经过独立内容审核',result_text(result.model_dump(mode='json')))
        self.assertTrue(artifacts({'task_id':'T1','result':result.model_dump(mode='json')}))
        self.assertEqual(h.workflow.budgets['T1'].react_rounds,8)

class ExistingReportWithoutReviewTest(TestCase):
    setUp=multi.MultiturnEvidenceTest.setUp
    save=multi.MultiturnEvidenceTest.save
    no_query=staticmethod(multi.MultiturnEvidenceTest.no_query)
    state=multi.MultiturnEvidenceTest.state
    workflow=multi.MultiturnEvidenceTest.workflow
    report=staticmethod(multi.MultiturnEvidenceTest.report)

    def test_existing_evidence_report_is_delivered_without_second_model_review(self):
        workflow,model=self.workflow([self.report()])
        workflow.config=SimpleNamespace(report_content_review_enabled=False)
        old_budget=RunBudget('old-task',spent=480,react_rounds=8)
        workflow.budgets={'old-task':old_budget}
        before=old_budget.public()
        result=workflow._answer_qa(self.state(analysis_mode='report'))['result']
        self.assertIsNotNone(result['report'])
        self.assertEqual(result['content_review_status'],'skipped')
        self.assertFalse(result['final_reviewed'])
        self.assertEqual(len(model.calls),1)
        self.assertEqual(before,old_budget.public())
        self.assertEqual({s['task_id'] for s in result['report']['data_sources']},{'old-task'})

    def test_full_graph_uses_new_report_budget_and_never_resumes_old_investigation(self):
        from app.config import Settings
        route={'action':'data_qa','confidence':1,'reason':'复用原调查结果整理报告',
               'analysis_mode':'report','analysis_task_ids':['old-task'],
               'analysis_result_ids':['old-task:R1','old-task:R2'],'needs_investigation':False}
        workflow,model=self.workflow([route,self.report()])
        workflow.config=Settings(report_content_review_enabled=False)
        def chat(system, payload):
            model.calls.append((system, json.loads(payload) if payload.startswith('{') else payload))
            return model.outputs.pop(0)
        model.chat_json=chat
        workflow.preprocessor=RequestPreprocessor(model)
        workflow.checkpointer=InMemorySaver()
        workflow.harness=SimpleNamespace(records={},run=Mock(side_effect=AssertionError('No investigation')))
        workflow.graph=workflow._compile()
        workflow.task_locks={};workflow.configuration_snapshots={}
        old_budget=RunBudget('old-task',spent=480,react_rounds=8,tools=20)
        workflow.budgets={'old-task':old_budget}
        before=old_budget.public()
        state=self.state(query='生成报告')
        parent=json.loads(state['recent_result_context'])
        parent['query']='七月份GMV为什么下降'
        state['recent_result_candidates']=json.dumps([parent])
        result=workflow.invoke(state,'follow-task')
        self.assertIsNotNone(result.report)
        self.assertEqual(result.content_review_status,'skipped')
        self.assertEqual(len(model.calls),2)
        self.assertEqual(before,old_budget.public())
        self.assertEqual(workflow.budgets['follow-task'].react_rounds,0)
        self.assertEqual(workflow.budgets['follow-task'].tools,0)
        workflow.harness.run.assert_not_called()

class FollowupBudgetTest(TestCase):
    def test_submit_does_not_resume_expired_investigation(self):
        service=object.__new__(AskDataService)
        scope=AccessScope('u',('analyst',),frozenset({'db'}),frozenset({'table'}))
        old=QueryResult(task_id='old',route='database_query',status='partial',message='旧调查结束',
            workflow_mode='investigation_react_v1',evidence=[{'id':'old:R1'}])
        old_budget=RunBudget('old',spent=480,react_rounds=8)
        service.access_controller=Mock();service.access_controller.scoped.return_value=scope
        service.context=Mock();service.context.normalize_workspace.return_value={'datasource_id':'db'}
        service.context.latest_pending.return_value={'user_id':'u','result':QueryResult(
            task_id='wrong',route='database_query',status='waiting_clarification',message='请选择期间',
            clarification={'parameter':'period','question':'哪一年','reason':'期间未定','options':[]})}
        service._restore_session=Mock()
        service.tasks={'old':{'user_id':'u','session_id':'u:db:s','workspace':{'datasource_id':'db'},'result':old}}
        service.session_tasks={'u:db:s':['old']}
        service.clarify=Mock(side_effect=AssertionError('Must not resume expired investigation'))
        service._payload=lambda tid,q,s,w,a:{'task_id':tid,'query':q,'session_id':s,'access_scope':a}
        service.workflow=Mock();service.workflow.budgets={'old':old_budget}
        service.workflow.invoke.side_effect=lambda payload,tid: QueryResult(task_id=tid,route='data_qa',status='completed',message='报告整理')
        before=old_budget.public()
        result=service.submit('生成报告','s',{'datasource_id':'db'},'u')
        self.assertNotEqual(result.task_id,'old')
        service.workflow.invoke.assert_called_once()
        self.assertEqual(before,old_budget.public())
        self.assertIs(service.tasks['old']['result'],old)
