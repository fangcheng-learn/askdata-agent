from copy import deepcopy
import unittest
from unittest.mock import Mock
from app.runtime_budget import BudgetStop
from app.workflows.investigation_output import output_schema
from app.querying.calculations import execute_calculation, CalculationError
from tests import test_react_calculate_contract as fixture
from tests.test_state_duration import request,source,event


class ReadCalculationHandoffTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ReactCalculateContractTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.h,self.t,self.b=self.f.harness,self.f.task,self.f.budget
        self.t.protocol_version=4

    def test_read_schema_only_exposes_existing_result_ids_and_real_columns(self):
        schema=output_schema('react',result_columns={'T:R1':['x','y'],'T:R2':['z']})
        p=schema['$defs']['ReadResult']['properties']
        self.assertEqual(p['result_id']['enum'],['T:R1','T:R2'])
        self.assertEqual(p['columns']['items']['enum'],['x','y','z'])
        empty=output_schema('react',result_columns={})
        self.assertNotIn({'$ref':'#/$defs/ReadResult'},empty['properties']['action']['anyOf'])

    def test_unknown_projection_returns_only_actual_fields_and_repeated_page_is_cached(self):
        self.f.evidence(['x'],[{'x':7}]);req={'type':'read_result','result_id':'T:R1','columns':['invented']}
        self.h._dispatch(self.t,req,'read')
        self.assertEqual(self.t.observation['columns'],['x']);self.assertEqual(self.t.observation['rows'],[{'x':7}])
        self.assertEqual(self.t.observation['missing_requested_columns'],['invented'])
        used=self.b.tools
        self.h._dispatch(self.t,{**req,'columns':[]},'same actual page')
        self.assertTrue(self.t.observation['cache_hit']);self.assertEqual(self.b.tools,used)
        self.h._dispatch(self.t,req,'same page again')
        with self.assertRaises(BudgetStop):self.h._dispatch(self.t,req,'endless repeat')

    def test_wrong_opening_literal_reports_actual_values_without_guessing(self):
        with self.assertRaises(CalculationError) as caught:
            execute_calculation({**request(),'opening_value':'invented'},source([event('2026-05-01',1,2,'opening')]))
        self.assertEqual(caught.exception.details['requested_opening_value'],'invented')
        self.assertEqual(caught.exception.details['observed_values'],['opening'])

    def test_sql_module_repairs_only_calculation_parameters_using_original_saved_result(self):
        candidate=self.f.raw_ledger_source()
        candidate['postprocess']['opening_value']='invented'
        self.h.workflow.model_client=object()
        self.h._sql_module=Mock(return_value=Mock(technical_system=Mock(return_value='technical rules')))
        plan={**request(),'result_id':'query_result'}
        self.h._call=Mock(return_value=plan)
        action={'id':'Q','goal':'state durations','question_ids':['Q1'],'status':'completed'}
        outcome=self.h._postprocess_query(self.t,action,candidate,{'status':'completed','result':{'id':'T:R1'}})
        self.assertTrue(outcome['calculation_complete'])
        self.assertEqual(outcome['source_result_id'],'T:R1')
        self.assertEqual(action['calculation_repair_calls'],1)
        self.assertEqual(self.t.evidence[0]['result_kind'],'calculation_source')
        self.assertEqual(self.t.evidence[0]['pending_calculation']['status'],'completed')
        self.assertEqual(self.t.evidence[-1]['result_kind'],'calculated_result')
        self.assertEqual(self.b.react_rounds,0)
        args=self.h._call.call_args.args
        self.assertEqual(args[1],'sql_calculation_repair')
        self.assertEqual(args[3]['error']['details']['observed_values'],['opening'])
        self.assertEqual(sum(self.b.repairs.values()),1)

    def test_parameter_repair_has_shared_bound_and_does_not_return_raw_as_final_metric(self):
        candidate=self.f.raw_ledger_source()
        candidate['postprocess']['opening_value']='invented'
        self.h.workflow.model_client=object();self.h._sql_module=Mock(return_value=Mock(technical_system=Mock(return_value='rules')))
        self.h._call=Mock(return_value={'wrong':'format'})
        action={'id':'Q','goal':'state','question_ids':[],'status':'completed'}
        outcome=self.h._postprocess_query(self.t,action,candidate,{'status':'completed','result':{'id':'T:R1'}})
        self.assertEqual(outcome['status'],'needs_calculation_repair')
        self.assertEqual(action['status'],'awaiting_calculation')
        self.assertEqual(self.h._call.call_count,2)
        self.assertEqual(sum(self.b.repairs.values()),2)
        self.assertEqual(outcome['result']['result_kind'],'calculation_source')
        self.assertEqual(len(self.t.evidence),1)

    def test_filtered_source_returns_sql_repair_without_wasting_parameter_rewrites(self):
        candidate=self.f.raw_ledger_source()
        ref=self.t.evidence[0]['result_ref']
        data=self.f.store.load_result(ref,'U','T','fixture')
        data['sql'] += " WHERE occurred_at >= '2026-06-01'"
        self.f.store.save_result(ref+'-filtered',data)
        self.t.evidence[0]['result_ref']=ref+'-filtered'
        self.h.workflow.model_client=object();self.h._call=Mock()
        action={'id':'Q','goal':'state','status':'completed'}
        result=self.h._postprocess_query(self.t,action,candidate,{'status':'completed','result':{'id':'T:R1'}})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(action['error_kind'],'calculation_source')
        self.assertEqual(action['last_generation_error']['kind'],'calculation_source')
        self.h._call.assert_not_called()

    def test_closing_context_cannot_present_raw_input_as_computed_metric(self):
        self.f.raw_ledger_source()
        e=self.t.evidence[0];e['result_kind']='calculation_source';e['pending_calculation']={'status':'failed'}
        self.t.state['query']='investigate';self.t.state['session_context']={}
        for role in ['', 'review']:
            entry=self.h._context(self.t,closing=True,role=role)['evidence'][0]
            self.assertFalse(entry['usable_for_conclusions'])
            self.assertEqual(entry['rows'],[])
            self.assertEqual(entry['pending_calculation']['status'],'failed')
