from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from app.querying.task_measurements import (measurement_memory, covered_result_ids,
    canonical_name, inherit_definitions)
from tests import test_react_calculate_contract as calculation_fixture


class TaskMeasurementMemoryTest(unittest.TestCase):
    def test_child_clarification_returns_to_business_agent(self):
        from app.workflows.sql_batch import QueryPreparationClarification
        detail={'parameter':'other','question':'请选择内部维度映射','reason':'内部方法','options':[]}
        for stage in ('preparation','coder'):
            self.t.protocol_version=4
            if stage=='preparation':
                self.h._prepare_sql_context=Mock(side_effect=QueryPreparationClarification(detail))
            else:
                self.h._prepare_sql_context=Mock(return_value={'database':'fixture'})
                self.h._sql_module=Mock(return_value=SimpleNamespace(generate_batch=lambda queries,*args,**kwargs:
                    {'candidates':{},'errors':{q['query_id']:{'kind':'clarification','clarification_category':'scope',
                     'clarification':detail,'message':detail['question']} for q in queries}}))
            query={**self.q,'execution_mode':'exploratory','goal':'核实不同集合 '+stage}
            self.h._business_query_batch(self.t,{'queries':[query]},'内部方法')
            self.assertEqual(self.t.observation['type'],'query_method_decision')
            self.assertNotEqual(self.t.final_status,'waiting_clarification')
            self.assertIsNone(self.t.clarification)

    def test_exploratory_goal_is_not_satisfied_by_same_named_numeric_output(self):
        exploratory={**self.q,'execution_mode':'exploratory','goal':'核实该指标变化是否来自新原始事件'}
        self.assertEqual(covered_result_ids(self.t,self.a,exploratory,self.canonical),[])

    def test_exploration_reaches_coder_without_compilation_or_numeric_reuse(self):
        from unittest.mock import patch
        from app.runtime_budget import BudgetStop
        module=SimpleNamespace(generate_batch=Mock(side_effect=RuntimeError('test transport stops here')))
        self.h._sql_module=Mock(return_value=module)
        self.h._prepare_sql_context=Mock(return_value={'database':'fixture'})
        exploratory={**self.q,'execution_mode':'exploratory','goal':'检查该指标背后的原始事件'}
        with patch('app.querying.compiled_metrics.compile_paid_goods',side_effect=AssertionError('must not compile')):
            self.h._business_query_batch(self.t,{'queries':[exploratory]},'new evidence')
        self.assertEqual(self.t.observation['queries'][0]['status'], 'interrupted')
        module.generate_batch.assert_called_once()
        self.assertEqual(len(self.t.evidence),1)

    def test_extra_declared_metric_reuses_actual_output_not_original_request_list(self):
        self.a['business_request']['metric_requirements'] = ['amount']
        self.assertEqual(covered_result_ids(self.t,self.a,self.q,self.canonical), ['T:R1'])
        self.assertEqual(covered_result_ids(self.t,self.a,{**self.q,'input_result_ids':['T:R1']},self.canonical), ['T:R1'])

    def test_explicit_and_inherited_periods_match_but_new_period_does_not(self):
        self.t.state['extraction'] = {'time_expressions':['2026-06','2026-07']}
        self.a['business_request']['conditions']['months'] = ['2026-07','2026-06']
        self.assertEqual(covered_result_ids(self.t,self.a,self.q,self.canonical), ['T:R1'])
        changed = {**self.q, 'conditions':{'store':'A','months':['2026-08']}}
        self.assertEqual(covered_result_ids(self.t,self.a,changed,self.canonical), [])

    def test_changed_registered_version_cannot_reuse_old_numeric_evidence(self):
        self.t.metric_pool['units']['version']='changed-definition'
        self.t.evidence[0]['column_metadata']['units']['kind']='registered'
        self.assertEqual(covered_result_ids(self.t,self.a,self.q,self.canonical), [])

    def setUp(self):
        self.fixture = calculation_fixture.ReactCalculateContractTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.harness
        self.t = self.fixture.task
        self.t.protocol_version = 4
        self.t.metric_pool = {'amount': {'id': 'amount', 'label':'销售金额'},
                              'units': {'id':'units', 'label':'销售件数'}}
        self.t.field_pool = {'store': {'sql_name':'stores.id', 'name':'store', 'label':'店铺'}}
        self.request = {'goal':'两个指标', 'metric_requirements':['amount','units'],
                        'dimensions':['month'], 'conditions':{'store':'A'}, 'input_result_ids':[]}
        self.a = {'id':'S1','action_type':'query','status':'completed','business_request':deepcopy(self.request),
                  'result_ids':['T:R1']}
        self.t.actions.append(self.a)
        self.fixture.evidence(['amount','units','extra'], [{'amount':100,'units':4,'extra':9}])
        self.t.evidence[0].update(step_id='S1', column_metadata={
            'amount': {'kind':'registered','measurement_id':'amount','measurement_version':'v1'},
            'units': {'kind':'task_local','measurement_id':'units','measurement_version':'local-v1',
                      'definition':'成功成交的商品件数','scope':'对象A期间内成功事件',
                      'source_fields':['items.quantity'],'event_time':['payments.at']}})
        self.q = {**self.request, 'goal':'只看件数', 'metric_requirements':['销售件数']}
        self.canonical = lambda v: canonical_name(self.t,v)

    def test_complete_superset_reuses_without_sql_or_query_round(self):
        self.h._prepare_sql_context = Mock(side_effect=AssertionError('must not retrieve or generate'))
        self.h._business_query_batch(self.t, {'queries':[self.q]}, '已有指标子集')
        self.assertEqual(self.t.observation['queries'][0]['result_ids'], ['T:R1'])
        self.assertEqual(self.t.observation['queries'][0]['status'], 'reused')
        self.assertEqual(self.fixture.budget.react_rounds, 0)
        self.h._prepare_sql_context.assert_not_called()

    def test_raw_query_with_pending_calculation_is_not_claimed_as_finished_measure(self):
        from app.workflows.sql_batch import _key
        # First request was a raw source for a calculation that still failed.
        self.a['completion_role']='calculation_source'
        self.a['request_identity']={'source':'fixture','snapshot':None,'conditions':{},'understanding':{},
            'terms':{'metric_requirements':['amount','units'],'dimensions':['month']},'inputs':[],
            'query_conditions':[('stores.id','A')],'comparison_purpose':''}
        self.a['request_key']=_key(self.a['request_identity'])
        self.h._prepare_sql_context=Mock(side_effect=AssertionError('successful raw SQL must not rerun'))
        self.h._business_query_batch(self.t,{'queries':[self.request]},'retry source')
        self.assertEqual(self.t.observation['queries'][0]['status'],'needs_calculation_repair')
        self.assertEqual(self.t.observation['queries'][0]['source_result_ids'],['T:R1'])
        self.assertEqual(self.fixture.budget.react_rounds,0)

    def test_scope_grain_definition_inputs_and_required_output_are_not_ignored(self):
        changes = [{'conditions':{'store':'B'}}, {'dimensions':['sku']},
                   {'comparison_purpose':'使用不同统计集合验证'}, {'input_result_ids':['T:R0']},
                   {'required_output_fields':['unknown']}, {'metric_requirements':['extra']}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(covered_result_ids(self.t,self.a,{**self.q,**change},self.canonical),[])
        self.t.evidence[0]['complete']=False
        self.assertEqual(covered_result_ids(self.t,self.a,self.q,self.canonical),[])

    def test_renamed_unambiguous_condition_field_has_same_identity(self):
        self.assertEqual(covered_result_ids(self.t,self.a,{**self.q,'conditions':{'店铺':'A'}},self.canonical),['T:R1'])
        self.t.field_pool['different']={'sql_name':'other.id','label':'店铺'}
        self.assertEqual(covered_result_ids(self.t,self.a,{**self.q,'conditions':{'店铺':'A'}},self.canonical),[])

    def test_same_name_does_not_certify_unresolved_output(self):
        self.t.evidence[0]['column_metadata']['units']['kind']='unresolved'
        self.assertEqual(covered_result_ids(self.t,self.a,self.q,self.canonical),[])
        memory=measurement_memory(self.t)
        extra=next(x for x in memory['results'][0]['outputs'] if x['column']=='extra')
        self.assertEqual(extra['semantic_status'],'definition_not_declared')
        self.assertEqual(extra['kind'],'observed_output')

    def test_directory_deduplicates_definitions_preserves_conflicting_versions(self):
        original=deepcopy(self.t.evidence[0])
        self.t.evidence.append({**deepcopy(original),'id':'T:R2'})
        self.assertEqual(len(measurement_memory(self.t)['definitions']),2)
        self.t.evidence[-1]['column_metadata']['units']['measurement_version']='different'
        memory=measurement_memory(self.t)
        self.assertEqual(memory['conflicts'][0]['measurement_id'],'units')
        self.assertNotIn('rows',memory['results'][0])
        self.assertEqual(self.t.evidence[0],original)

    def test_invalid_foreign_task_user_source_snapshot_not_in_memory(self):
        for change in [{'check_status':'superseded'},{'check_status':'invalid'}, {'task_id':'other'},
                       {'user_id':'other'},{'datasource_id':'other'},{'snapshot':{'cutoff':'other'}}]:
            old=deepcopy(self.t.evidence[0]);self.t.evidence[0].update(change)
            self.assertEqual(measurement_memory(self.t)['results'],[],change)
            self.t.evidence[0]=old

    def test_inherit_local_definition_without_promoting_it_to_registered(self):
        gap={'kind':'unresolved_measure','request':'units'}
        entry={'measurements':[],'unresolved_requirements':[gap]}
        resolution={'queries':[entry],'unresolved_requirements':[gap]}
        inherit_definitions(resolution,measurement_memory(self.t,['units']))
        self.assertEqual(entry['measurements'][0]['kind'],'task_local')
        self.assertEqual(entry['measurements'][0]['event_time'],['payments.at'])
        self.assertEqual(resolution['unresolved_requirements'],[])

    def test_sql_inherits_local_definition_without_explicit_result_reference(self):
        self.t.metric_pool={}
        self.t.field_pool={'quantity':{'name':'quantity','sql_name':'items.quantity'}}
        self.h._retrieve=Mock(side_effect=AssertionError('known task definition must not be retrieved again'))
        context=self.h._prepare_sql_context(self.t,[{'query_id':'S2','goal':'复用商品件数定义',
                    'metric_requirements':['units'],'dimensions':[],'input_result_ids':[]}])
        self.assertIn('T:R1',context['dependency_results'])
        definition=context['measurement_requests']['queries'][0]['measurements'][0]
        self.assertEqual(definition['kind'],'task_local')
        self.assertEqual(definition['event_time'],['payments.at'])
        self.assertEqual(context['dependency_results']['T:R1']['rows'][0]['units'],4)
        self.h._retrieve.assert_not_called()

    def test_automatic_sql_dependency_still_uses_scoped_reader(self):
        self.h._read_evidence=Mock(side_effect=PermissionError('wrong owner'))
        with self.assertRaises(PermissionError):
            self.h._business_query_batch(self.t,{'queries':[self.q]},'reuse')
        self.assertEqual(self.fixture.budget.react_rounds,0)

    def test_equivalent_duplicate_condition_aliases_collapse_but_conflicts_remain(self):
        from app.querying.task_measurements import canonical_conditions
        self.assertEqual(canonical_conditions({'store':'A','店铺':'A'},self.canonical),
                         canonical_conditions({'store':'A'},self.canonical))
        self.assertNotEqual(canonical_conditions({'store':'A','店铺':'B'},self.canonical),
                            canonical_conditions({'store':'A'},self.canonical))

    def test_context_references_shared_registered_definition_without_losing_local_versions(self):
        from app.querying.task_measurements import compact_measurement_memory
        self.t.evidence[0]['column_metadata']['amount']['definition']='registered meaning'
        full=measurement_memory(self.t)
        compact=compact_measurement_memory(full,{'amount'})
        amount=next(x for x in compact['definitions'] if x['measurement_id']=='amount')
        units=next(x for x in compact['definitions'] if x['measurement_id']=='units')
        self.assertNotIn('definition',amount)
        self.assertEqual(amount['measurement_version'],'v1')
        self.assertEqual(amount['evidence_ids'],['T:R1'])
        self.assertEqual(units['definition'],'成功成交的商品件数')
        self.assertEqual(next(x for x in full['definitions'] if x['measurement_id']=='amount')['definition'],'registered meaning')
