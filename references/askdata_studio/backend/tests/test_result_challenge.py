"""Controller tests only: no model, SQL or business-data success is simulated as acceptance."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
from app.workflows.investigation_output import output_schema
from tests import test_task_measurement_memory as fixture

class ResultChallengeTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TaskMeasurementMemoryTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.h,self.t,self.a,self.b=self.f.h,self.f.t,self.f.a,self.f.fixture.budget
        self.a.update(input_result_ids=[],goal=self.a['business_request']['goal'],sql_generations=1,attempts=1,sql='OLD SQL')
        self.f.fixture.evidence(['value'],[{'value':2}],identifier='T:R2')
        self.t.evidence[-1].update(input_result_ids=['T:R1'])
        self.f.fixture.evidence(['value'],[{'value':3}],identifier='T:R3')
        self.t.progress['facts']=[{'id':'F1','text':'dependent','evidence_ids':['T:R2']},
                                  {'id':'F2','text':'independent','evidence_ids':['T:R3']}]
        self.module=Mock()
        self.module.generate_batch.return_value={'candidates':{'S1':{'sql':'FIXED SQL','query_contract':{}}},'errors':{}}
        self.h._sql_module=Mock(return_value=self.module)
        self.h._prepare_sql_context=Mock(return_value={})
        def execute(task,request,reason,**kw):
            self.a.update(status='completed',result_ids=['T:R3'])
            return {'status':'completed','result':{'id':'T:R3'}}
        self.h._query=Mock(side_effect=execute)

    def test_result_repair_keeps_original_request_and_budget_even_after_eight_rounds(self):
        self.b.react_rounds=8
        original=deepcopy(self.a['business_request'])
        self.h._dispatch(self.t,{'type':'repair_query','query_id':'S1','issue':'订单列全为空，但已有整体订单不为零'},'核查关联')
        self.assertEqual(self.a['sql_generations'],2)
        self.assertEqual(self.b.react_rounds,8)
        self.assertEqual(sum(self.b.repairs.values()),1)
        self.assertEqual(self.a['business_request'],original)
        self.assertEqual(self.t.evidence[0]['check_status'],'superseded')
        self.assertEqual(self.t.evidence[1]['check_status'],'superseded')
        self.assertEqual(self.t.evidence[2]['check_status'],'basic_valid')
        self.assertEqual([f['id'] for f in self.t.progress['facts']],['F2'])
        repair=self.module.generate_batch.call_args.kwargs['repair']['S1']
        self.assertEqual(repair['error_kind'],'result_semantics')
        self.assertEqual(repair['diagnostics']['observed_results'][0]['id'],'T:R1')

    def test_exhausted_query_invalidates_bad_evidence_without_fourth_generation(self):
        self.a['sql_generations']=3
        self.h._dispatch(self.t,{'type':'repair_query','query_id':'S1','issue':'口径错误'},'核查')
        self.module.generate_batch.assert_not_called()
        self.assertEqual(self.a['sql_generations'],3)
        self.assertEqual(self.t.evidence[0]['check_status'],'superseded')
        self.assertEqual(self.t.observation['type'],'query_exhausted')

    def test_foreign_query_cannot_invalidate_or_generate(self):
        self.h._dispatch(self.t,{'type':'repair_query','query_id':'OTHER_TASK:A1','issue':'wrong'},'repair')
        self.module.generate_batch.assert_not_called()
        self.assertEqual(self.t.evidence[0]['check_status'],'basic_valid')

    def test_dynamic_schema_exposes_only_real_query_ids(self):
        schema=output_schema('react',query_ids=['A2'])
        self.assertEqual(schema['$defs']['RepairQuery']['properties']['query_id']['enum'],['A2'])
        self.assertNotIn({'$ref':'#/$defs/RepairQuery'},output_schema('react',query_ids=[])['properties']['action']['anyOf'])

    def test_changing_page_limit_does_not_charge_for_same_actual_rows(self):
        req={'type':'read_result','result_id':'T:R3','limit':100}
        self.h._dispatch(self.t,req,'read');used=self.b.tools
        self.h._dispatch(self.t,{**req,'limit':200},'read')
        self.assertTrue(self.t.observation['cache_hit']);self.assertEqual(self.b.tools,used)
