"""Offline calculation protocol checks; no model, HTTP, SQL or business data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from app.runtime_budget import BudgetStop, RunBudget, current_budget
from app.services.investigation_store import InvestigationStore
from app.skills import SkillRegistry
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord
from app.workflows.react_prompts import REACT_SYSTEM


class ReactCalculateContractTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InvestigationStore(Path(temporary.name) / 'state.sqlite3')
        engine = SimpleNamespace(results={})
        self.harness = ReactInvestigationHarness(SimpleNamespace(model_client=None, database_engine=engine,
                                                                 skills=SkillRegistry()), self.store)
        self.task = ReactRecord({'task_id': 'T', 'session_id': 'test', 'datasource_id': 'fixture',
                                 'query': '测试计算协议', 'access_scope': {'user_id': 'U'}})
        self.budget = RunBudget('T')
        self.budget.start()
        token = current_budget.set(self.budget)
        self.addCleanup(current_budget.reset, token)
        self.addCleanup(self.budget.pause)

    def evidence(self, columns, rows, *, complete=True, identifier='T:R1'):
        ref = 'fixture-' + identifier
        full = {'user_id': 'U', 'task_id': 'T', 'datasource_id': 'fixture',
                'columns': columns, 'rows': rows, 'complete': complete, 'total_rows': len(rows)}
        self.store.save_result(ref, full)
        self.task.evidence.append({'id': identifier, 'step_id': 'SOURCE', 'result_ref': ref,
                                   'columns': columns, 'rows': rows[:1], 'stored_rows': len(rows),
                                   'complete': complete, 'check_status': 'basic_valid'})

    @staticmethod
    def change(base='base', current='current', **metadata):
        return {'type': 'calculate', 'operation': 'change', 'result_id': 'T:R1',
                'base_field': base, 'current_field': current, **metadata}

    def dispatch(self, request, reason='比较真实结果'):
        self.harness._dispatch(self.task, request, reason)

    def test_state_intervals_use_saved_full_result_and_server_cutoff_without_query_round(self):
        from tests.test_state_duration import event, source, request
        self.task.protocol_version = 4
        self.task.state['scenario'] = {'observation_cutoff_at': '2026-09-01T00:00:00Z'}
        raw = source([event('2026-05-01', 1, 2, 'opening'),
                      event('2026-06-30T12:00:00Z', 2, -2), event('2026-07-02T12:00:00Z', 3, 3)])
        self.store.save_result('fixture-T:R1', {**raw, 'user_id': 'U', 'task_id': 'T', 'datasource_id': 'fixture'})
        self.task.evidence.append({'id': 'T:R1', 'step_id': 'SOURCE', 'result_ref': 'fixture-T:R1',
            'columns': raw['columns'], 'rows': raw['rows'][:1], 'stored_rows': len(raw['rows']),
            'complete': True, 'check_status': 'basic_valid'})
        action = {'type': 'calculate', **request()}
        self.dispatch(action)
        self.assertEqual(self.task.actions[-1]['status'], 'completed')
        output = self.task.evidence[-1]
        self.assertEqual([r['duration_hours'] for r in output['rows']], [12, 36])
        self.assertEqual(output['input_result_ids'], ['T:R1'])
        self.assertEqual(output['source_result_id'], 'T:R1')
        self.assertEqual(self.budget.react_rounds, 0)
        self.assertEqual(self.budget.tools, 1)
        self.dispatch(action)
        self.assertEqual(self.task.observation['status'], 'reused')
        self.assertEqual(self.budget.tools, 1)

    def raw_ledger_source(self, opening='opening'):
        from tests.test_state_duration import event, source, request
        self.task.protocol_version = 4
        self.task.state['scenario'] = {'observation_cutoff_at': '2026-09-01T00:00:00Z'}
        raw = source([event('2026-05-01', 1, 0, opening)])
        self.store.save_result('ledger-ref', {**raw, 'user_id': 'U', 'task_id': 'T', 'datasource_id': 'fixture'})
        self.task.evidence.append({'id':'T:R1', 'step_id':'SOURCE', 'result_ref':'ledger-ref',
            'columns':raw['columns'], 'rows':raw['rows'], 'complete':True, 'check_status':'basic_valid'})
        return {'postprocess': {**request(), 'result_id':'query_result'}}

    def test_sql_postprocess_returns_calculated_result_and_reuses_success(self):
        candidate = self.raw_ledger_source()
        action = {'id': 'SOURCE', 'goal': '计算状态时长', 'question_ids': ['Q1'], 'sql_generations': 1}
        outcome = {'status':'completed', 'result':{'id':'T:R1'}}
        completed = self.harness._postprocess_query(self.task, action, candidate, outcome)
        self.assertEqual(completed['source_result_id'], 'T:R1')
        self.assertEqual(completed['result']['id'], 'T:R2')
        self.assertEqual(completed['result']['rows'][0]['duration_hours'], 720)
        self.assertEqual(self.task.evidence[-1]['question_ids'], ['Q1'])
        self.assertEqual(self.budget.tools, 1)
        again = self.harness._postprocess_query(self.task, action, candidate, outcome)
        self.assertEqual(again['result']['id'], 'T:R2')
        self.assertEqual(self.budget.tools, 1)
        self.assertEqual(action['sql_generations'], 1)

    def test_failed_postprocess_retains_raw_evidence_without_retrying_sql(self):
        candidate = self.raw_ledger_source(opening='change')
        action = {'id': 'SOURCE', 'goal': '计算状态时长', 'question_ids': ['Q1'], 'sql_generations': 1}
        result = self.harness._postprocess_query(self.task, action, candidate,
                                                {'status':'completed','result':{'id':'T:R1'}})
        self.assertEqual(result['status'], 'needs_calculation_repair')
        self.assertEqual(result['calculation']['error_code'], 'missing_opening_state')
        self.assertEqual(len(self.task.evidence), 1)
        self.assertEqual(self.task.evidence[0]['check_status'], 'basic_valid')
        self.assertEqual(action['sql_generations'], 1)
        self.assertEqual(self.budget.react_rounds, 0)

    def test_long_table_row_values_get_structured_repair_information(self):
        self.evidence(['month', 'channel', 'sessions'], [{'month': '六月', 'channel': 'a', 'sessions': 100}])
        self.dispatch(self.change('六月', '七月', column='sessions'))
        observation = self.task.observation
        self.assertEqual(observation['type'], 'calculation_error')
        self.assertEqual(observation['available_columns'], ['month', 'channel', 'sessions'])
        self.assertEqual(observation['expected_parameters'], ['base_field', 'current_field'])
        self.assertIn('长表', observation['repair_instruction'])
        self.assertEqual(self.task.actions[0]['input_result_ids'], ['T:R1'])
        self.assertEqual(self.task.actions[0]['depends_on'], ['SOURCE'])
        self.assertFalse(self.budget.repairs)  # first failure does not consume an extra attempt.

    def test_identical_failure_does_not_execute_or_create_new_action(self):
        self.evidence(['base', 'current'], [{'base': 100, 'current': 80}])
        self.dispatch(self.change('六月', '七月'))
        with self.assertRaises(BudgetStop):
            self.dispatch(self.change('六月', '七月', repair_action_id='NEW-ID', goal='新名字'), reason='改说明')
        self.assertEqual(self.budget.tools, 1)
        self.assertEqual(len(self.task.actions), 1)
        self.assertEqual(self.task.observation['status'], 'no_progress')

    def test_irrelevant_parameter_changes_do_not_bypass_repeat_guard(self):
        self.evidence(['base', 'current'], [{'base': 100, 'current': 80}])
        self.dispatch(self.change('六月', '七月', column='sessions'))
        with self.assertRaises(BudgetStop):
            self.dispatch(self.change('六月', '七月', column='another'))
        self.assertEqual(self.budget.tools, 1)

    def test_corrected_wide_columns_repair_original_action(self):
        self.evidence(['channel', 'base', 'current'], [{'channel': 'a', 'base': 100, 'current': 80}])
        self.dispatch(self.change('六月', '七月'))
        self.dispatch(self.change(repair_action_id='A1'))
        self.assertEqual(len(self.task.actions), 1)
        self.assertEqual(self.task.actions[0]['attempts'], 2)
        self.assertEqual(self.task.actions[0]['status'], 'completed')
        self.assertEqual(self.budget.repairs, {'A1': 1})
        self.assertEqual(self.task.evidence[-1]['rows'][0]['change'], -20)
        self.assertEqual(self.task.evidence[-1]['rows'][0]['change_rate'], -0.2)
        self.assertEqual(self.task.evidence[-1]['input_result_ids'], ['T:R1'])

    def test_only_two_extra_repairs_even_with_different_ids_and_goals(self):
        self.evidence(['base', 'current'], [{'base': 100, 'current': 80}])
        for index in range(3):
            self.dispatch(self.change(f'bad{index}', 'current', repair_action_id=f'A{index+1}'), reason=f'goal{index}')
        with self.assertRaises(BudgetStop):
            self.dispatch(self.change('base', 'current', repair_action_id='A9'), reason='different')
        self.assertEqual(len(self.task.actions), 1)
        self.assertEqual(self.task.actions[0]['attempts'], 3)
        self.assertEqual(self.budget.repairs, {'A1': 2})
        self.assertEqual(self.budget.tools, 3)

    def test_complete_success_is_reused_without_recalculating(self):
        self.evidence(['base', 'current'], [{'base': 100, 'current': 80}])
        self.dispatch(self.change())
        self.dispatch(self.change(goal='新名字', repair_action_id='NEW'))
        self.assertEqual(self.budget.tools, 1)
        self.assertEqual(len(self.task.actions), 1)
        self.assertEqual(len(self.task.evidence), 2)
        self.assertEqual(self.task.observation['status'], 'reused')

    def test_empty_result_still_rejects_nonexistent_column(self):
        self.evidence(['base', 'current'], [])
        self.dispatch(self.change('六月', '七月'))
        self.assertEqual(self.task.actions[0]['status'], 'failed')
        self.assertEqual(len(self.task.evidence), 1)

    def test_incomplete_result_cannot_be_calculated_as_complete(self):
        self.evidence(['base', 'current'], [{'base': 100, 'current': 80}], complete=False)
        self.dispatch(self.change())
        self.assertEqual(self.task.actions[0]['status'], 'failed')
        self.assertIn('完整', self.task.observation['error'])
        self.assertEqual(self.task.actions[0]['input_result_ids'], ['T:R1'])

    def test_zero_denominator_is_null_and_not_infinite(self):
        self.evidence(['base', 'current'], [{'base': 0, 'current': 2}])
        self.dispatch(self.change())
        self.assertIsNone(self.task.evidence[-1]['rows'][0]['change_rate'])
        self.assertEqual(self.task.evidence[-1]['rows'][0]['change'], 2)

    def test_prompt_distinguishes_row_values_from_column_names(self):
        self.assertIn('日期、渠道等行值不是列名', REACT_SYSTEM)
        self.assertIn('先用query条件聚合', REACT_SYSTEM)
        self.assertIn('base_sessions', REACT_SYSTEM)


if __name__ == '__main__':
    unittest.main()
