from pathlib import Path
from threading import Event
import tempfile
import time
import unittest
from app.workbench_runtime import WorkbenchRuns, ExecutionControl, current_execution, check_execution
from app.runtime_budget import RunBudget, BudgetStop
from app.skills.registry import SkillRegistry


def wait_terminal(store, run_id):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        item=store.read(run_id,'owner')
        if item['status'] not in {'running','stopping'}:
            return item
        time.sleep(.01)
    raise AssertionError('background run did not finish')


class WorkbenchRuntimeTests(unittest.TestCase):
    def test_owner_isolation_single_active_run_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);store=WorkbenchRuns(directory);gate=Event()
            run=store.start('owner','question','session',{'datasource_id':'source'},
                {'model':'test','skills':None,'tools':None},lambda:(gate.wait(2),{'message':'done'})[1])
            with self.assertRaises(KeyError): store.read(run['id'],'another')
            with self.assertRaises(KeyError): store.cancel(run['id'],'another')
            self.assertEqual(store.list('another','source'),[])
            with self.assertRaises(ValueError): store.start('owner','q','s',{}, {},lambda:None)
            gate.set();result=wait_terminal(store,run['id'])
            self.assertEqual(result['result'],{'message':'done'})
            self.assertEqual(WorkbenchRuns(directory).read(run['id'],'owner')['result'],result['result'])
            self.assertNotIn('owner',result)

    def test_cancel_prevents_next_external_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkbenchRuns(Path(tmp));entered=Event();release=Event();calls=[]
            def work():
                entered.set();release.wait(2);check_execution('next call');calls.append('external')
            run=store.start('owner','q','s',{}, {},work)
            self.assertTrue(entered.wait(1))
            self.assertEqual(store.cancel(run['id'],'owner')['status'],'stopping')
            release.set()
            self.assertEqual(wait_terminal(store,run['id'])['status'],'stopped')
            self.assertEqual(calls,[])

    def test_skill_selection_and_budget_cancellation_are_scoped(self):
        registry=SkillRegistry();original={s['name'] for s in registry.list()}
        control=ExecutionControl(skills=[]);token=current_execution.set(control)
        try:
            self.assertEqual({s['name'] for s in registry.list()},{'database_query','data_qa'})
            with self.assertRaises(ValueError): registry.get('business_diagnosis')
            control.cancelled.set()
            with self.assertRaises(BudgetStop): RunBudget('test').check()
        finally: current_execution.reset(token)
        self.assertEqual({s['name'] for s in registry.list()},original)

    def test_restart_interrupts_without_replay(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/'abc.json').write_text(json.dumps({'id':'abc','owner':'owner','status':'running','workspace':{},'created_at':'2026-11-02'}))
            self.assertEqual(WorkbenchRuns(directory).read('abc','owner')['status'],'interrupted')

    def test_model_selection_does_not_mutate_shared_config(self):
        from app.config import Settings
        from app.model_client import ModelClient
        client=ModelClient(Settings(llm_model='default'));payloads=[]
        client._post=lambda url,payload:(payloads.append(payload),{'choices':[{'message':{'content':'ok'}}]})[1]
        token=current_execution.set(ExecutionControl(model='selected'))
        try: client.chat('system','question')
        finally: current_execution.reset(token)
        client.chat('system','question')
        self.assertEqual([p['model'] for p in payloads],['selected','default'])
        self.assertEqual(client.config.llm_model,'default')

    def test_disabled_mcp_tool_is_blocked_before_execution(self):
        from app.mcp_runtime.client import LocalMcpClient
        client=LocalMcpClient(None);token=current_execution.set(ExecutionControl(tools=[]))
        try:
            with self.assertRaises(PermissionError): client.call_tool('query_private',{})
        finally: current_execution.reset(token)

    def test_hiding_session_stays_hidden_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkbenchRuns(Path(tmp))
            run=store.start('owner','q','session',{'datasource_id':'source'}, {},lambda:{'message':'done'})
            wait_terminal(store,run['id'])
            store.hide_session('owner','source','session')
            self.assertEqual(WorkbenchRuns(Path(tmp)).list('owner','source'),[])
            self.assertEqual(store.read(run['id'],'owner')['result'],{'message':'done'})

    def test_selected_analysis_model_keeps_router_model(self):
        from app.config import Settings
        from app.model_client import ModelClient
        from app.runtime_budget import current_budget
        client=ModelClient(Settings(llm_model='router'));observed=[]
        client._post=lambda url,payload:(observed.append(payload['model']),{'choices':[{'message':{'content':'ok'}}]})[1]
        token=current_execution.set(ExecutionControl(model='analysis-model'))
        budget_token=current_budget.set(RunBudget('test',model_node='preprocess'))
        try: client.chat('system','question')
        finally: current_budget.reset(budget_token);current_execution.reset(token)
        self.assertEqual(observed,['router'])
