from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import time
from fastapi.testclient import TestClient
from app.main import app
from app.api import routes
from app.models import QueryResult
from app.workbench_runtime import WorkbenchRuns


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=WorkbenchRuns(Path(temp.name))
        self.enterContext(patch.object(routes,'workbench_runs',self.store))
        self.client=self.enterContext(TestClient(app))
        self.admin=self.login('admin','admin123')
        self.sales=self.login('sales','sales123')
        self.execute=self.enterContext(patch.object(routes.service,'submit',return_value=QueryResult(
            task_id='offline',status='completed',route='direct_response',message='offline test')))

    def login(self,name,password):
        response=self.client.post('/api/auth/login',json={'username':name,'password':password})
        return {'Authorization':'Bearer '+response.json()['access_token']}

    def test_auth_and_invalid_model_never_call_analysis(self):
        self.assertEqual(self.client.get('/api/workbench/options?datasource_id=askdata_mock').status_code,401)
        response=self.client.post('/api/workbench/runs',headers=self.admin,json={
            'query':'offline','workspace':{'datasource_id':'askdata_mock'},'options':{'model':'not-registered'}})
        self.assertEqual(response.status_code,422)
        self.execute.assert_not_called()

    def test_real_run_endpoint_preserves_origin_and_options_and_owner(self):
        response=self.client.post('/api/workbench/runs',headers=self.admin,json={
            'query':'offline','session_id':'origin','workspace':{'datasource_id':'askdata_mock'},'options':{'skills':[],'tools':[]}})
        self.assertEqual(response.status_code,200,response.text)
        item=response.json();run_id=item['id']
        self.assertEqual(item['session_id'],'origin')
        self.assertEqual(item['options']['skills'],[])
        self.assertEqual(self.client.get('/api/workbench/runs/'+run_id,headers=self.sales).status_code,404)
        self.assertEqual(self.client.post('/api/workbench/runs/'+run_id+'/stop',headers=self.sales).status_code,404)
        for _ in range(50):
            item=self.client.get('/api/workbench/runs/'+run_id,headers=self.admin).json()
            if item['status']=='completed':break
            time.sleep(.01)
        self.assertEqual(item['result']['message'],'offline test')
        self.assertEqual(self.execute.call_args.args[1],'origin')
        self.assertNotIn('owner',item)

    def test_unavailable_tool_rejected(self):
        response=self.client.post('/api/workbench/runs',headers=self.admin,json={
            'query':'offline','workspace':{'datasource_id':'askdata_mock'},'options':{'tools':['unavailable']}})
        self.assertEqual(response.status_code,422)
        self.execute.assert_not_called()
