from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib,json,unittest
from fastapi.testclient import TestClient
from app.main import app
from app.api import routes
from app.models import QueryResult
from app.workbench_runtime import WorkbenchRuns,current_execution
from app.foundry_adapter.store import UIStore
from app.foundry_adapter.projection import conversation,delivery_events
bridge=importlib.import_module('app.foundry_adapter.router')

class FoundryAdapterTests(unittest.TestCase):
    def test_free_text_clarification_resumes_original_task_once(self):
        waiting=QueryResult(task_id='offline-task',session_id='native-session',status='waiting_clarification',route='database_query',
            workflow_mode='investigation_react_v1',datasource_id='askdata_mock',message='需要补充信息',
            clarification={'parameter':'time_range','question':'请说明月份','reason':'需要期间','options':[]})
        self.execute.side_effect=None;self.execute.return_value=waiting
        self.assertEqual(self.post_run().status_code,200)
        payload=self.run_body();payload['body']['runId']='clarification-run'
        payload['body']['messages']=[{'id':'reply','role':'user','content':'8月份'}]
        with patch.object(bridge.service,'get_task',return_value=waiting), patch.object(bridge.service,'clarify',return_value=
                waiting.model_copy(update={'status':'completed','message':'已接收期间','clarification':None})) as clarify:
            response=self.post_run(payload)
            self.assertEqual(response.status_code,200,response.text)
            clarify.assert_called_once_with('offline-task','8月份',user_message='8月份',user_id='demo_admin')
            self.post_run(payload)
            self.assertEqual(clarify.call_count,1)
        self.assertEqual(self.execute.call_count,1)
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        starts=[e for e in events if e['type']=='TEXT_MESSAGE_START']
        self.assertEqual(len(starts),1)
        self.assertEqual(events[1]['type'],'TEXT_MESSAGE_START')

    def test_unmatched_closed_clarification_still_rejects(self):
        waiting=QueryResult(task_id='offline-task',session_id='native-session',status='waiting_clarification',route='database_query',
            datasource_id='askdata_mock',message='选择口径',clarification={'parameter':'metric','question':'选哪个',
            'reason':'确认口径','options':[{'id':'a','label':'口径A','description':''},{'id':'b','label':'口径B','description':''}]})
        self.execute.side_effect=None;self.execute.return_value=waiting
        self.post_run();payload=self.run_body();payload['body']['runId']='invalid-choice'
        payload['body']['messages']=[{'id':'reply','role':'user','content':'8月份'}]
        with patch.object(bridge.service,'get_task',return_value=waiting),patch.object(bridge.service,'clarify') as clarify:
            self.assertEqual(self.post_run(payload).status_code,422)
            clarify.assert_not_called()

    def setUp(self):
        temp=self.enterContext(TemporaryDirectory());self.state=UIStore(Path(temp)/'ui.sqlite3');self.runs=WorkbenchRuns(Path(temp)/'runs')
        self.enterContext(patch.object(bridge,'store',self.state));self.enterContext(patch.object(bridge,'workbench_runs',self.runs));self.enterContext(patch.object(routes,'workbench_runs',self.runs))
        self.enterContext(patch.object(bridge.service,'list_history',return_value=[]))
        self.client=self.enterContext(TestClient(app));self.admin=self.login('admin','admin123');self.sales=self.login('sales','sales123')
        def execute(query,sid,workspace,owner):
            self.control=current_execution.get()
            return QueryResult(task_id='offline-task',session_id=sid,status='completed',route='direct_response',message='离线接口验证',datasource_id=workspace['datasource_id'])
        self.execute=self.enterContext(patch.object(bridge.service,'submit',side_effect=execute))
        self.enterContext(patch.object(bridge.service,'get_task',return_value=QueryResult(task_id='offline-task',status='completed',route='direct_response',message='离线接口验证')))
    def login(self,name,password):
        r=self.client.post('/api/auth/login',json={'username':name,'password':password});return {'Authorization':'Bearer '+r.json()['access_token']}
    def get(self,path,headers=None):return self.client.get('/api/foundry/v1/'+path,headers=headers or self.admin)
    def run_body(self,**config):
        return {'method':'agent/run','params':{'agentId':'dataFoundry'},'body':{'threadId':'native-session','runId':'native-run','messages':[{'id':'user','role':'user','content':'离线接口验证'}],'forwardedProps':{'run_config':{'activeDatasourceId':'askdata_mock','enabledDatasourceIds':['askdata_mock'],'enabledSkillIds':[],'enabledMcpServerIds':[],'activeLlmProfileId':bridge.service.config.llm_model,**config}}}}
    def post_run(self,payload=None,headers=None):return self.client.post('/api/foundry/copilotkit',headers=headers or self.admin,json=payload or self.run_body())
    def test_cookie_auth_csrf_and_isolated_cookie_names(self):
        r=self.client.post('/api/foundry/v1/auth/login',json={'email':'admin','password':'admin123'});self.assertEqual(r.status_code,200)
        self.assertIn('ip_session',self.client.cookies);self.assertNotIn('df_session',self.client.cookies)
        self.assertEqual(self.client.get('/api/foundry/v1/me').status_code,200)
        self.assertEqual(self.client.post('/api/foundry/v1/auth/logout').status_code,403)
        r=self.client.post('/api/foundry/v1/auth/logout',headers={'X-CSRF-Token':self.client.cookies['ip_csrf'],'Content-Type':'application/json'})
        self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/foundry/v1/me').status_code,401)
    def test_config_contract_and_no_inference_on_reads(self):
        self.assertTrue(self.get('capabilities').json()['data']['conversation.memory'])
        types=self.get('datasource-types').json()['data'];self.assertIsInstance(types,list);self.assertIn('name',types[0])
        workspace=self.get('workspace-config').json()['data'];defaults=self.get('run-defaults').json()['data']
        self.assertEqual(len(defaults['enabledDatasourceIds']),1);self.assertTrue(workspace['datasources']);self.assertTrue(workspace['modelProfiles']);self.execute.assert_not_called()
    def test_initial_connect_is_not_a_new_analysis(self):
        p=self.run_body();p['method']='agent/connect';self.assertEqual(self.post_run(p).status_code,200);self.execute.assert_not_called()
        self.assertEqual(self.get('sessions/native-session/conversation').json()['data']['messages'],[])
    def test_stream_result_restoration_selection_and_idempotency(self):
        r=self.post_run();self.assertEqual(r.status_code,200,r.text)
        events=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(events[0]['type'],'RUN_STARTED');self.assertEqual(events[-1]['type'],'RUN_FINISHED');self.assertIn('离线接口验证',[e.get('delta') for e in events])
        self.assertEqual(self.control.skills,[]);self.assertEqual(self.control.tools,[]);self.assertEqual(self.execute.call_args.args[1],'native-session')
        self.assertEqual(self.execute.call_args.args[2]['datasource_id'],'askdata_mock')
        restored=self.get('sessions/native-session/conversation').json()['data'];self.assertEqual(restored['messages'][-1]['contentText'],'离线接口验证')
        self.post_run();self.assertEqual(self.execute.call_count,1)
    def test_cross_owner_and_datasource_permissions(self):
        self.post_run();self.assertEqual(self.get('sessions/native-session/conversation',self.sales).json()['data']['messages'],[])
        r=self.client.post('/api/foundry/v1/runs/native-run/cancel',headers=self.sales);self.assertEqual(r.status_code,404)
        p=self.run_body(activeDatasourceId='not-authorized');p['body']['runId']='other-run';self.assertEqual(self.post_run(p).status_code,403);self.assertEqual(self.execute.call_count,1)
    def test_invalid_model_and_multiple_databases_never_execute(self):
        self.assertEqual(self.post_run(self.run_body(activeLlmProfileId='not-configured')).status_code,422)
        self.assertEqual(self.post_run(self.run_body(enabledDatasourceIds=['askdata_mock','ecommerce_ops'])).status_code,422);self.execute.assert_not_called()
    def test_foreign_origin_rejected(self):
        self.client.post('/api/foundry/v1/auth/login',json={'email':'admin','password':'admin123'})
        r=self.client.post('/api/foundry/copilotkit',headers={'X-CSRF-Token':self.client.cookies['ip_csrf'],'Origin':'https://other.example'},json=self.run_body());self.assertEqual(r.status_code,403);self.execute.assert_not_called()
    def test_metadata_persistence_and_owner_isolation(self):
        self.state.put('one','session','same',{'id':'same','title':'original'});other=UIStore(self.state.path)
        self.assertEqual(other.get('one','session','same')['title'],'original');self.assertIsNone(other.get('two','session','same'))
    def test_partial_findings_keep_limits_and_real_evidence(self):
        record={'task_id':'task','query':'why','workspace':{'datasource_id':'askdata_mock'},'result':{'status':'partial','analysis':'已核对的结论','stop_reason':'预算已用完','evidence':[{'sql':'SELECT 1','goal':'核对数据','complete':True,'columns':['n'],'rows':[{'n':1}]}]}}
        view=conversation({'id':'session'},[record]);answer=view['messages'][-1]['contentText'];self.assertIn('部分完成',answer);self.assertIn('预算已用完',answer)
        self.assertTrue(any(e['type']=='TOOL_CALL_RESULT' for e in delivery_events(record,'session','run')));self.assertEqual(view['toolCalls'][0]['args']['sql'],'SELECT 1')
    def test_cancel_marks_stream_as_canceled(self):
        from threading import Event
        import asyncio,time
        from app.foundry_adapter.router import stream_run
        started=Event();release=Event();owner='demo_admin'
        def execution():
            started.set();release.wait(2)
            return QueryResult(task_id='cancel-task',status='completed',route='direct_response',message='已停止后返回')
        job=self.runs.start(owner,'cancel check','cancel-session',{'datasource_id':'askdata_mock'},
            {'model':'','skills':None,'tools':None},execution)
        self.assertTrue(started.wait(1))
        self.state.put(owner,'session','cancel-session',{'id':'cancel-session','threadId':'cancel-session','title':'cancel check','source':'askdata_mock','backendSession':'cancel-session'})
        link={'id':'cancel-run','sessionId':'cancel-session','jobId':job['id']};self.state.put(owner,'run','cancel-run',link)
        r=self.client.post('/api/foundry/v1/runs/cancel-run/cancel',headers=self.admin)
        self.assertTrue(r.json()['data']['canceled']);release.set()
        for _ in range(100):
            if self.runs.read(job['id'],owner)['status']=='stopped':break
            time.sleep(.01)
        user=routes.auth_service.authenticate(self.admin['Authorization'])
        async def collect():return [item async for item in stream_run(user,link)]
        text=''.join(asyncio.run(collect()));self.assertIn('"runStatus": "canceled"',text)
        restored=self.get('sessions/cancel-session/conversation').json()['data']
        self.assertEqual(restored['checkpoints'][-1]['status'],'canceled')
    def test_branch_preserves_selected_evidence_and_native_graph(self):
        self.post_run();owner='demo_admin';user=routes.auth_service.authenticate(self.admin['Authorization'])
        original=bridge.records_for(user,bridge.get_session(user,'native-session'))
        with patch.object(bridge.service,'list_history',return_value=original):
            r=self.client.post('/api/foundry/v1/sessions/native-session/branches',headers=self.admin,json={'runId':'native-run'})
            self.assertEqual(r.status_code,200,r.text);sid=r.json()['data']['session']['id']
            restored=self.get('sessions/'+sid+'/conversation').json()['data'];self.assertEqual(len(restored['messages']),2)
            self.assertEqual(self.state.get(owner,'session',sid)['inheritedTasks'],['offline-task'])
            dag=self.get('sessions/native-session/trace-dag').json()['data']
            self.assertTrue(any(n['kind']=='run-terminal' for n in dag['nodes']))
            self.assertTrue(dag['edges'])
    def test_native_csv_and_xlsx_download_preserve_values(self):
        from io import BytesIO
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET
        self.execute.side_effect=None;self.execute.return_value=QueryResult(task_id='table-task',status='completed',route='database_query',message='结果',columns=['sku','gmv'],rows=[{'sku':'=literal','gmv':12.5}])
        self.post_run()
        response=self.get('artifacts/native-run~table/download?format=xlsx');self.assertEqual(response.status_code,200)
        with ZipFile(BytesIO(response.content)) as z:
            sheet=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            self.assertFalse(sheet.findall('.//{*}f'));self.assertIn('=literal',ET.tostring(sheet).decode())
        response=self.get('artifacts/native-run~table/download?format=csv');self.assertIn('12.5',response.text)

    def test_sidebar_and_run_status_do_not_restore_reports(self):
        with patch.object(bridge.service,'list_history_summaries',return_value=[]) as summaries, patch.object(bridge.service,'list_history',side_effect=AssertionError('must not restore reports')):
            self.assertEqual(self.get('sessions').status_code,200)
            self.assertEqual(self.get('sessions/empty/active-run').json()['data'],None)
            self.assertTrue(summaries.called)

    def test_live_events_arrive_before_completion_and_cancel_ack_is_immediate(self):
        import asyncio,time
        from threading import Event
        from unittest.mock import AsyncMock
        from app.workbench_runtime import check_execution
        from app.mcp_runtime.client import LocalMcpClient
        entered=Event();release=Event();owner='demo_admin'
        def execute():
            check_execution('正在准备数据查询')
            client=LocalMcpClient(None)
            client._call_tool=AsyncMock(return_value={'columns':['gmv'],'rows':[{'gmv':10}]})
            client.call_tool('query_askdata_mock',{'sql':'SELECT 10 AS gmv'})
            entered.set();release.wait(5)
            check_execution('不应再执行的后续调用')
        job=self.runs.start(owner,'实时检查','live-session',{'datasource_id':'askdata_mock'}, {},execute)
        self.assertTrue(entered.wait(2))
        self.state.put(owner,'session','live-session',{'id':'live-session','title':'实时检查','source':'askdata_mock','backendSession':'live-session'})
        link={'id':'live-run','sessionId':'live-session','jobId':job['id']};self.state.put(owner,'run','live-run',link)
        dag=self.get('sessions/live-session/trace-dag').json()['data']
        self.assertTrue(dag['sections'])
        self.assertEqual(dag['sections'][0]['status'],'in-progress')
        self.assertTrue(any(n['kind']=='tool' and n['label']=='查询经营数据' for n in dag['nodes']))
        self.assertFalse(any(n['kind']=='run-terminal' for n in dag['nodes']))
        user=routes.auth_service.authenticate(self.admin['Authorization'])
        async def check_stream():
            stream=bridge.stream_run(user,link);seen=[]
            async for line in stream:
                if line.startswith('data: '):seen.append(json.loads(line[6:]))
                if any(e.get('name')=='artifact' for e in seen):break
            self.assertTrue(any(e.get('name')=='artifact' and e['value']['preview_json']['rows']==[{'gmv':10}] for e in seen))
            self.assertFalse(release.is_set())
            self.assertTrue(any(e['type']=='ACTIVITY_SNAPSHOT' for e in seen))
            self.assertEqual(self.runs.read(job['id'],owner)['status'],'running')
            t=time.monotonic();self.runs.cancel(job['id'],owner)
            rest=[json.loads(line[6:]) async for line in stream if line.startswith('data: ')]
            self.assertLess(time.monotonic()-t,.5)
            self.assertTrue(any(e.get('snapshot',{}).get('runStatus')=='canceled' for e in rest))
        try:asyncio.run(check_stream())
        finally:release.set()
        for _ in range(100):
            if self.runs.read(job['id'],owner)['status']=='stopped':break
            time.sleep(.01)
        self.assertEqual(self.runs.read(job['id'],owner)['status'],'stopped')
        self.assertFalse(any(e.get('type')=='ACTIVITY_SNAPSHOT' and e['content'].get('title')=='不应再执行的后续调用' for e in self.runs.read(job['id'],owner)['events']))
