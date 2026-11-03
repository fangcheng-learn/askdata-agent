"""Offline projections and orchestration; no model, SQL or network calls."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from app.preprocessing import RequestPreprocessor
from app.workflows.business_context import evidence_brief, current_progress, investigation_checks
from app.workflows.query_graph import QueryWorkflow
from app.workflows.react_harness import ReactRecord, ReactInvestigationHarness
from app.runtime_budget import RunBudget, BudgetStop, current_budget
from app.services.askdata_service import AskDataService
from app.models import QueryResult

class ContextDeliveryTest(TestCase):
    def test_brief_keeps_normal_check_and_scope_without_sql_or_full_contract(self):
        original = {'id':'T:R1', 'goal':'核实退款，未发现异常', 'columns':['n'], 'rows':[{'n':5}],
            'sql':'SELECT secret', 'check_status':'basic_valid', 'complete':True,
            'query_contract':{'filters':[{'field':'kind','value':'paid'}], 'validation':{'pending_checks':['口径待核对']}}}
        before=deepcopy(original)
        brief=evidence_brief(original)
        self.assertNotIn('sql',brief); self.assertNotIn('rows',brief)
        self.assertEqual(brief['scope']['filters'],original['query_contract']['filters'])
        self.assertEqual(brief['pending_checks'],['口径待核对'])
        self.assertEqual(original,before)
        task=SimpleNamespace(evidence=[original], actions=[{'id':'A1','action_type':'query','goal':original['goal'],'status':'completed','result_ids':['T:R1']}])
        self.assertEqual(investigation_checks(task)[0]['result_ids'], ['T:R1'])
        notes={'facts':[{'id':'F1','status':'retracted'},{'id':'F2','status':'supported'}]}
        self.assertEqual(current_progress(notes)['facts'],[{'id':'F2','status':'supported'}])
        self.assertEqual(len(notes['facts']),2)

    def test_closing_decision_does_not_reset_or_extend_evidence_rounds(self):
        budget=RunBudget('T',react_round_limit=8);budget.react_rounds=8
        with self.assertRaises(BudgetStop): budget.react_decision()
        budget.react_decision(allow_closing=True)
        self.assertEqual(budget.react_rounds,8)
        with self.assertRaises(BudgetStop): budget.query_round()

    def test_required_question_blocks_before_main_or_sql_and_cannot_skip(self):
        harness=object.__new__(ReactInvestigationHarness)
        state={'task_id':'T','query':'范围不明','datasource_id':'db','access_scope':{'user_id':'u'},
               'intent':{'response_type':'clarification'},'direct_response':'请指定期间'}
        task=ReactRecord(state,protocol_version=4)
        harness.records={'T':task};harness._persist=Mock();harness._result=lambda t:t
        harness._call=Mock(side_effect=AssertionError('No main model before required answer'))
        self.assertEqual(harness.run(state).final_status,'waiting_clarification')
        self.assertTrue(task.clarification['required'])
        with self.assertRaisesRegex(ValueError,'必要条件'): harness.run(state,'__skip__')
        harness._call.assert_not_called()

    def test_resolved_query_issue_updates_same_card_and_failed_delivery_is_not_report(self):
        from app.business_progress import snapshot_updates
        from app.foundry_adapter.projection import artifacts,result_text
        task={'state':{'task_id':'T'},'actions':[{'id':'A1','action_type':'query','goal':'核实反馈','status':'completed',
              'last_generation_error':{'message':'缺少字段'}}]}
        cards=snapshot_updates(task)
        issue=next(c['value'] for c in cards if c['name']=='insightpilot.analysis' and c['value']['kind']=='issue')
        self.assertEqual(issue['status'],'resolved')
        result={'task_id':'T','status':'partial','workflow_mode':'investigation_react_v1',
                'analysis':'已有进展已保存，报告暂未生成。','stop_reason':'InternalSQLException'}
        self.assertEqual(artifacts({'task_id':'T','result':result}),[])
        self.assertNotIn('InternalSQLException', result_text(result))

    def test_skipped_background_stays_unknown_and_report_resume_bypasses_main(self):
        for response in ['__skip__','__report__']:
            with self.subTest(response=response):
                harness=object.__new__(ReactInvestigationHarness)
                state={'task_id':'T','query':'解释变化','datasource_id':'db','access_scope':{'user_id':'u'}}
                task=ReactRecord(state,protocol_version=4)
                task.evidence=[{'id':'T:R1','check_status':'basic_valid'}]
                task.clarification={'parameter':'other','question':'当月有活动吗','reason':'解释变化',
                                    'required':False,'allow_skip':True,'options':[]}
                task.final_status='waiting_clarification'
                harness.records={'T':task};harness._persist=Mock();harness._result=lambda t:t
                harness._context=lambda *args,**kwargs:{}
                harness._call=Mock(return_value={'progress':{},'action':{'type':'deliver'}})
                def deliver(t,**kwargs):
                    t.final_status='partial';t.final_reviewed=True
                harness._deliver=deliver
                harness.run(state,response)
                self.assertNotIn('confirmed_parameters',task.state)
                if response=='__skip__':
                    self.assertEqual(task.clarification_history[-1]['status'],'skipped')
                    self.assertIsNone(task.clarification_history[-1]['answer'])
                else:
                    harness._call.assert_not_called()
                    self.assertFalse(task.report_only)

    def test_withdrawn_fact_cannot_be_reused_and_issue_keeps_one_round(self):
        from app.workflows.progress_protocol import resolve_references
        from app.business_progress import snapshot_updates
        for status in ['retracted','removed']:
            resolved=resolve_references(['F1'],{'F1':{'status':status,'evidence_ids':['T:R1']}},
                                        [{'id':'T:R1','check_status':'basic_valid'}])
            self.assertFalse(resolved['safe'])
        task={'state':{'task_id':'T'},'business_issue_rounds':{'Q1':2},'progress':{'core_questions':[
              {'id':'Q1','question':'为什么下降','status':'blocked','reason':'缺少证据'}]}}
        first=next(x['value'] for x in snapshot_updates(task) if x['name']=='insightpilot.analysis')
        task['progress']['core_questions'][0].update(status='answered',reason='依据结果已回答')
        second=next(x['value'] for x in snapshot_updates(task) if x['name']=='insightpilot.analysis')
        self.assertEqual(first['id'],second['id']);self.assertEqual(second['status'],'resolved')

    def test_previous_draft_does_not_duplicate_full_source_rows(self):
        harness=object.__new__(ReactInvestigationHarness);harness.workflow=SimpleNamespace()
        task=ReactRecord({'task_id':'T','query':'解释变化','datasource_id':'fixture','workspace':{},
                          'access_scope':{'user_id':'u'}},protocol_version=4)
        task.observation={'type':'delivery_repair'}
        task.draft_report={'title':'已知结论','markdown':'上一稿正文','data_sources':[{'rows':[{'x':999}]}]}
        context=harness._context(task,closing=True)
        self.assertEqual(context['previous_report'],{'title':'已知结论','markdown':'上一稿正文'})
        self.assertIn('data_sources',task.draft_report)
