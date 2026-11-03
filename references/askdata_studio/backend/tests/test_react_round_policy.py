"""Offline round/generation limits, using controlled calls and no business SQL.

The SQL strings below are inert fixtures. Validator, engine and MCP execution
are explicit test doubles; these tests establish control behavior only.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from app.runtime_budget import RunBudget, current_budget
from app.services.investigation_store import InvestigationStore
from app.workflows.react_harness import ReactInvestigationHarness, ReactRecord


def state():
    return {'investigation_protocol_version': 1, 'task_id': 'round-test', 'session_id': 'round-session',
            'query': '解释给定期间变化', 'standalone_query': '解释给定期间变化',
            'required_questions': ['解释给定期间变化'], 'datasource_id': 'round_fixture',
            'access_scope': {'user_id': 'round-user'}, 'schema_graph': {}, 'workspace': {}}


def query(sql='GOOD', *, metric='value', goal='核实变化', refs=None):
    return {'goal': goal, 'sql': sql, 'input_result_ids': list(refs or []),
            'query_contract': {'tables': ['fixture'], 'dimensions': [], 'filters': [],
                               'grain': 'overall', 'time_range': None,
                               'metrics': [{'name': metric, 'source_fields': ['fixture.' + metric],
                                            'aggregation': 'sum'}]}}


class ControlledHarness:
    """Use the real controller while replacing every external business operation."""

    def __init__(self, normal=(), rewrites=(), *, fail_sql=('BAD',)):
        self.harness = object.__new__(ReactInvestigationHarness)
        self.task = ReactRecord(state())
        self.task.skills = {'business_diagnosis': {'instructions': 'fixture', 'version': 'fixture'}}
        self.harness.records = {self.task.state['task_id']: self.task}
        self.calls = []
        self.call_snapshots = []
        self.validated = []
        self.compiled = []
        self.executed = []
        self.persisted = []
        self.normal, self.rewrites = iter(normal), iter(rewrites)
        self.fail_sql = set(fail_sql)
        engine = SimpleNamespace(results={}, preflight=self.preflight)
        self.harness.workflow = SimpleNamespace(database_engine=engine, budgets={},
                                                mcp_client=lambda scope: SimpleNamespace(call_tool=self.execute))
        self.harness.validator = SimpleNamespace(validate=self.validate)
        self.harness._persist = self.persist
        self.harness._context = lambda task, **options: {'task': {'question': task.state['query']},
                                                        'semantics': {}}
        self.harness._call = self.call
        self.harness._full_execution = lambda task, result: result
        self.harness._save_evidence = self.save_evidence
        self.harness._deliver = self.deliver

    def persist(self, task, results=None):
        self.persisted.append(deepcopy(task.actions))

    def validate(self, contract, sql, *args, **options):
        self.validated.append(sql)
        if sql in self.fail_sql:
            raise ValueError('fixture contract rejected before SQL')
        return contract

    def preflight(self, source, sql, scope):
        self.compiled.append(sql)
        return None

    def execute(self, name, payload):
        self.executed.append(payload['sql'])
        budget = current_budget.get()
        if budget:
            budget.tool(name)
        return {'success': True, 'sql': payload['sql'], 'columns': ['value'], 'rows': [{'value': 7}],
                'complete': True, 'total_rows': 1, 'result_ref': 'fixture-ref'}

    def save_evidence(self, task, action, result, full):
        evidence = {'id': f'{task.state["task_id"]}:R{len(task.evidence)+1}',
                    'step_id': action['id'], 'check_status': 'basic_valid',
                    'sql': result['sql'], 'query_contract': result['query_contract'],
                    'columns': ['value'], 'rows': [{'value': 7}],
                    'complete': True, 'total_rows': 1, 'stored_rows': 1,
                    'input_result_ids': list(action.get('input_result_ids') or [])}
        task.evidence.append(evidence)
        return evidence

    def call(self, task, node, system, payload):
        self.calls.append((node, deepcopy(payload)))
        self.call_snapshots.append((node, deepcopy(task.actions), deepcopy(self.persisted)))
        budget = current_budget.get()
        if budget:
            budget.model_usage.append({'kind': 'chat', 'node': node, 'usage': {'total_tokens': 0}})
        if node == 'react':
            value = next(self.normal)
        elif node == 'sql_repair':
            value = next(self.rewrites)
        else:
            return {'fixture': node}
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def deliver(self, task, stopping=False):
        self.call(task, 'draft', '', {})
        self.call(task, 'delivery_review', '', {})
        task.final_status = 'partial' if stopping else 'completed'
        task.final_reviewed = True
        task.approved_text = '已取得的有限证据' if stopping else 'fixture approved'

    @contextmanager
    def bounded(self, budget=None):
        budget = budget or RunBudget('round-test', react_round_limit=5)
        self.harness.workflow.budgets[budget.task_id] = budget
        budget.start()
        token = current_budget.set(budget)
        try:
            yield budget
        finally:
            budget.pause()
            current_budget.reset(token)


class ReactRoundPolicyTest(unittest.TestCase):
    def test_preparation_does_not_spend_five_query_rounds(self):
        preparation = ['load_skill', 'retrieve', 'read_result', 'calculate', 'resolve_time']
        outputs = ([{'action': {'type': name}} for name in preparation]
                   + [{'action': {'type': 'query', 'queries': [query('Q'+str(i), metric='m'+str(i))]}}
                      for i in range(5)] + [{'action': {'type': 'deliver'}}])
        fixture = ControlledHarness(outputs)
        dispatched = []
        real_dispatch = fixture.harness._dispatch
        fixture.harness._dispatch = lambda task, action, reason: (
            real_dispatch(task, action, reason) if action['type'] == 'query' else dispatched.append(action['type']))
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual(budget.react_rounds, 5)
        self.assertEqual(dispatched, preparation)
        self.assertEqual(budget.react_decisions, 10)
        self.assertEqual([node for node, _ in fixture.calls], ['react'] * 10 + ['draft', 'delivery_review'])
        self.assertEqual(result.status, 'partial')
        self.assertIn('业务取证', result.stop_reason)
        self.assertEqual(fixture.executed, ['Q0', 'Q1', 'Q2', 'Q3', 'Q4'])

    def test_delivery_and_closing_do_not_count_as_query_rounds(self):
        fixture = ControlledHarness([{'action': {'type': 'query', 'queries': [query('Q'+str(i), metric='m'+str(i))]}}
                                     for i in range(4)] + [{'action': {'type': 'deliver'}}])
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual(budget.react_rounds, 4)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([node for node, _ in fixture.calls].count('react'), 5)
        self.assertEqual([node for node, _ in fixture.calls][-2:], ['draft', 'delivery_review'])

    def test_unknown_model_failure_is_recorded_without_replaying_or_inventing_query_round(self):
        fixture = ControlledHarness([RuntimeError('fixture model unavailable')])
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual(budget.react_rounds, 0)
        self.assertEqual(budget.react_decisions, 1)
        self.assertEqual([node for node, _ in fixture.calls], ['react'])
        self.assertEqual(result.status, 'failed')

    def test_contract_failure_rewrites_once_without_extra_react_round(self):
        request = query('BAD')
        fixture = ControlledHarness(rewrites=[{'query_id': 'A1', 'sql': 'GOOD',
                                               'query_contract': request['query_contract']}])
        with fixture.bounded() as budget:
            budget.react_decision()
            fixture.harness._dispatch(fixture.task, {'type': 'query', 'queries': [request]}, '验证')
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual([node for node, _ in fixture.calls], ['sql_repair'])
        self.assertEqual(fixture.validated, ['BAD', 'GOOD'])
        self.assertEqual(fixture.executed, ['GOOD'])
        self.assertEqual(fixture.task.actions[0]['sql_generations'], 2)
        self.assertEqual(len(fixture.task.evidence), 1)
        # The generation has already been persisted when the repair call starts.
        _, actions_at_call, checkpoints_at_call = fixture.call_snapshots[0]
        self.assertEqual(actions_at_call[0]['sql_generations'], 2)
        self.assertEqual(checkpoints_at_call[-1][0]['sql_generations'], 2)

    def test_changed_contract_with_same_sql_is_a_valid_second_candidate(self):
        request = query('GOOD')
        request['query_contract']['metrics'][0]['name'] = 'wrong_name'
        corrected = deepcopy(request['query_contract'])
        corrected['metrics'][0]['name'] = 'value'
        fixture = ControlledHarness(rewrites=[{'query_id': 'A1', 'sql': 'GOOD',
                                               'query_contract': corrected}])
        def validate(contract, sql, *args, **options):
            fixture.validated.append(sql)
            if contract['metrics'][0]['name'] == 'wrong_name':
                raise ValueError('fixture metric declaration conflict')
            return contract
        fixture.harness.validator.validate = validate
        with fixture.bounded() as budget:
            fixture.harness._dispatch(fixture.task, {'type': 'query', 'queries': [request]}, '原目标')
        self.assertEqual(fixture.validated, ['GOOD', 'GOOD'])
        self.assertEqual(fixture.executed, ['GOOD'])
        self.assertEqual(fixture.task.actions[0]['sql_generations'], 2)
        self.assertEqual(fixture.task.actions[0]['status'], 'completed')
        self.assertEqual(budget.react_rounds, 1)  # This dispatched query batch is one evidence round.

    def test_fifth_decision_can_use_its_sql_rewrite_before_controlled_closing(self):
        request = query('BAD')
        fixture = ControlledHarness(
            normal=[{'action': {'type': 'query', 'queries': [query('Q'+str(i), metric='m'+str(i))]}}
                    for i in range(4)] + [{'action': {'type': 'query', 'queries': [request]}}],
            rewrites=[{'query_id': 'A5', 'sql': 'GOOD', 'query_contract': request['query_contract']}])
        with fixture.bounded() as budget:
            result = fixture.harness.run(state())
        self.assertEqual(budget.react_rounds, 5)
        self.assertEqual([node for node, _ in fixture.calls],
                         ['react'] * 5 + ['sql_repair', 'draft', 'delivery_review'])
        self.assertEqual(fixture.executed, ['Q0', 'Q1', 'Q2', 'Q3', 'GOOD'])
        self.assertEqual(result.status, 'partial')

    def test_invalid_repair_output_still_uses_second_generation_and_cannot_repeat(self):
        request = query('BAD')
        fixture = ControlledHarness(rewrites=[{'action': {'type': 'query'}}])
        with fixture.bounded():
            outcome = fixture.harness._query(fixture.task, request, 'first')
            outcome = fixture.harness._repair_query(fixture.task, request, outcome)
            again = fixture.harness._repair_query(fixture.task, request, outcome)
        self.assertEqual(fixture.task.actions[0]['sql_generations'], 2)
        self.assertEqual(len(fixture.calls), 1)
        self.assertFalse(again['repair_available'])
        self.assertEqual(fixture.executed, [])

    def test_repair_cannot_change_query_id_or_include_new_goal_or_dependencies(self):
        request = query('BAD')
        invalid = [
            {'query_id': 'A99', 'sql': 'GOOD', 'query_contract': request['query_contract']},
            {'query_id': 'A1', 'sql': 'GOOD', 'query_contract': request['query_contract'], 'goal': '新目标'},
            {'query_id': 'A1', 'sql': 'GOOD', 'query_contract': request['query_contract'], 'input_result_ids': []},
        ]
        for response in invalid:
            with self.subTest(response=response):
                fixture = ControlledHarness(rewrites=[response])
                with fixture.bounded():
                    outcome = fixture.harness._query(fixture.task, request, 'first')
                    final = fixture.harness._repair_query(fixture.task, request, outcome)
                self.assertEqual(final['status'], 'failed')
                self.assertEqual(fixture.executed, [])
                self.assertEqual(fixture.task.actions[0]['sql_generations'], 2)

    def test_failed_repair_call_does_not_refund_or_replay_second_generation(self):
        request = query('BAD')
        fixture = ControlledHarness(rewrites=[RuntimeError('fixture transport failed')])
        with fixture.bounded():
            first = fixture.harness._query(fixture.task, request, 'first')
            second = fixture.harness._repair_query(fixture.task, request, first)
            repeated = fixture.harness._repair_query(fixture.task, request, second)
        self.assertEqual(fixture.task.actions[0]['sql_generations'], 2)
        self.assertEqual([node for node, _ in fixture.calls], ['sql_repair'])
        self.assertFalse(repeated['repair_available'])
        self.assertEqual(fixture.executed, [])

    def test_changed_client_id_or_goal_cannot_reset_identical_sql_allowance(self):
        request = query('BAD')
        fixture = ControlledHarness(rewrites=[{'query_id': 'A1', 'sql': 'BAD2',
                                               'query_contract': request['query_contract']}], fail_sql=('BAD', 'BAD2'))
        with fixture.bounded():
            first = fixture.harness._query(fixture.task, request, 'first')
            fixture.harness._repair_query(fixture.task, request, first)
            for original_or_rewritten in ('BAD', 'BAD2'):
                repeated = query(original_or_rewritten, goal='改写说明')
                repeated['repair_action_id'] = 'fake-new-id'
                blocked = fixture.harness._query(fixture.task, repeated, 'new wording')
                self.assertEqual(blocked['status'], 'generation_exhausted')
        self.assertEqual(len(fixture.task.actions), 1)
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.executed, [])

    def test_blocked_dependencies_are_counted_before_any_sql_execution(self):
        for refs in (['missing-result'], {'invalid': 'shape'}):
            with self.subTest(refs=refs):
                request = query('GOOD')
                request['input_result_ids'] = refs
                fixture = ControlledHarness()
                with fixture.bounded():
                    first = fixture.harness._query(fixture.task, request, 'first')
                    second = fixture.harness._query(fixture.task, request, 'retry')
                    third = fixture.harness._query(fixture.task, request, 'third')
                self.assertEqual(first['status'], 'blocked')
                self.assertEqual(second['status'], 'blocked')
                self.assertEqual(first['sql_generations'], 1)
                self.assertEqual(second['sql_generations'], 2)
                self.assertEqual(third['status'], 'generation_exhausted')
                self.assertEqual(len(fixture.task.actions), 1)
                self.assertEqual(fixture.validated, [])
                self.assertEqual(fixture.executed, [])

    def test_incomplete_dependency_does_not_create_free_generation_attempts(self):
        fixture = ControlledHarness()
        fixture.task.evidence = [{'id': 'round-test:R0', 'complete': False,
                                  'check_status': 'basic_valid'}]
        request = query('GOOD', refs=['round-test:R0'])
        with fixture.bounded():
            first = fixture.harness._query(fixture.task, request, 'first')
            second = fixture.harness._query(fixture.task, request, 'second')
            third = fixture.harness._query(fixture.task, request, 'third')
        self.assertEqual((first['sql_generations'], second['sql_generations']), (1, 2))
        self.assertEqual(third['status'], 'generation_exhausted')
        self.assertEqual(fixture.executed, [])

    def test_malformed_contract_still_uses_a_generation_before_rewrite(self):
        request = query('GOOD')
        valid_contract = request['query_contract']
        request['query_contract'] = ['invalid-shape']
        fixture = ControlledHarness(rewrites=[{'query_id': 'A1', 'sql': 'GOOD',
                                               'query_contract': valid_contract}])
        with fixture.bounded():
            first = fixture.harness._query(fixture.task, request, 'first')
            second = fixture.harness._repair_query(fixture.task, request, first)
        self.assertEqual(first['sql_generations'], 1)
        self.assertEqual(second['sql_generations'], 2)
        self.assertEqual(fixture.executed, ['GOOD'])

    def test_batch_keeps_independent_success_and_only_repairs_failed_query(self):
        good, bad = query('GOOD', metric='a'), query('BAD', metric='b')
        fixture = ControlledHarness(rewrites=[{'query_id': 'A2', 'sql': 'FIXED',
                                               'query_contract': bad['query_contract']}])
        with fixture.bounded() as budget:
            budget.react_decision()
            fixture.harness._dispatch(fixture.task, {'type': 'query', 'queries': [good, bad]}, '独立查询')
        self.assertEqual(fixture.executed, ['GOOD', 'FIXED'])
        self.assertEqual([action['sql_generations'] for action in fixture.task.actions], [1, 2])
        self.assertEqual([entry['sql'] for entry in fixture.task.evidence], ['GOOD', 'FIXED'])
        self.assertEqual(budget.react_rounds, 1)
        self.assertEqual([node for node, _ in fixture.calls], ['sql_repair'])

    def test_explicit_repair_action_cannot_replace_original_goal_or_real_inputs(self):
        for altered in ('goal', 'input_result_ids'):
            with self.subTest(altered=altered):
                fixture = ControlledHarness()
                fixture.task.evidence = [
                    {'id': 'round-test:R0', 'check_status': 'basic_valid', 'complete': True},
                    {'id': 'round-test:R1', 'check_status': 'basic_valid', 'complete': True},
                ]
                original = query('BAD', refs=['round-test:R0'], goal='原核查目标')
                changed = query('GOOD', refs=['round-test:R0'], goal='原核查目标')
                changed['repair_action_id'] = 'A1'
                changed[altered] = '另一个业务目标' if altered == 'goal' else ['round-test:R1']
                with fixture.bounded():
                    first = fixture.harness._query(fixture.task, original, 'first')
                    blocked = fixture.harness._query(fixture.task, changed, 'changed')
                self.assertEqual(first['status'], 'failed')
                self.assertEqual(blocked['status'], 'blocked')
                self.assertEqual(blocked['sql_generations'], 2)
                self.assertIn('不能更换', blocked['error'])
                self.assertEqual(fixture.task.actions[0]['goal'], '原核查目标')
                self.assertEqual(fixture.task.actions[0]['input_result_ids'], ['round-test:R0'])
                self.assertEqual(fixture.executed, [])
                self.assertEqual(fixture.validated, ['BAD'])

    def test_new_query_with_different_real_inputs_has_its_own_generation_record(self):
        fixture = ControlledHarness()
        fixture.task.evidence = [
            {'id': 'round-test:R0', 'check_status': 'basic_valid', 'complete': True},
            {'id': 'round-test:R1', 'check_status': 'basic_valid', 'complete': True},
        ]
        with fixture.bounded():
            first = fixture.harness._query(fixture.task, query('BAD', refs=['round-test:R0']), 'first')
            second = fixture.harness._query(fixture.task, query('GOOD', refs=['round-test:R1']), 'new input')
        self.assertEqual((first['action_id'], second['action_id']), ('A1', 'A2'))
        self.assertEqual([action['sql_generations'] for action in fixture.task.actions], [1, 1])
        self.assertEqual(fixture.task.actions[0]['input_result_ids'], ['round-test:R0'])
        self.assertEqual(fixture.task.actions[1]['input_result_ids'], ['round-test:R1'])
        self.assertEqual(fixture.executed, ['GOOD'])

    def test_clarification_and_resume_share_remaining_rounds(self):
        fixture = ControlledHarness([{'action': {'type': 'clarify'}}, {'action': {'type': 'deliver'}}])
        def dispatch(task, action, reason):
            task.clarification = {'parameter': 'scope', 'question': '补充对象', 'reason': 'fixture', 'options': []}
            task.final_status = 'waiting_clarification'
        fixture.harness._dispatch = dispatch
        with fixture.bounded() as budget:
            first = fixture.harness.run(state())
            self.assertEqual(first.status, 'waiting_clarification')
            self.assertEqual(budget.react_rounds, 0)
            final = fixture.harness.run(state(), '指定对象')
        self.assertEqual(final.status, 'completed')
        self.assertEqual(budget.react_rounds, 0)
        self.assertEqual(budget.react_decisions, 2)

    def test_checkpoint_restore_does_not_reset_rounds(self):
        fixture = ControlledHarness()
        fixture.task.final_status = 'waiting_clarification'
        fixture.task.clarification = {'parameter': 'scope', 'question': '补充对象', 'reason': 'fixture', 'options': []}
        budget = RunBudget('round-test', react_rounds=4, react_round_limit=5)
        with TemporaryDirectory() as directory:
            store = InvestigationStore(Path(directory) / 'state.sqlite3')
            store.save_task('round-test', 'round-user', 'round-session', 'round_fixture',
                            asdict(fixture.task), budget.export_state())
            restored = ControlledHarness([{'action': {'type': 'query', 'queries': [query('LAST')]}},
                                           {'action': {'type': 'deliver'}}])
            restored.harness.store = store
            restored.harness.records.clear()
            task = restored.harness.restore('round-test', 'round-user')
            restored_budget = restored.harness.workflow.budgets['round-test']
            with restored.bounded(restored_budget):
                result = restored.harness.run(task.state, '指定对象')
        self.assertEqual(restored_budget.react_rounds, 5)
        self.assertEqual([node for node, _ in restored.calls], ['react', 'draft', 'delivery_review'])
        self.assertEqual(result.status, 'partial')

    def test_old_checkpoint_uses_recorded_react_calls_without_counting_sql_repair(self):
        saved = {'model_usage': [{'kind': 'chat', 'node': name, 'usage': {'total_tokens': 1}}
                                 for name in ('preprocess', 'react', 'sql_repair', 'react', 'draft', 'delivery_review')]}
        budget = RunBudget.from_state('old-round', saved)
        self.assertEqual(budget.react_rounds, 2)
        self.assertEqual(budget.public()['react_round_limit'], 5)

    def test_restored_inflight_rewrite_is_interrupted_and_does_not_replay(self):
        fixture = ControlledHarness()
        fixture.task.actions = [{'id': 'A1', 'action_type': 'query', 'kind': 'query',
                                 'status': 'rewriting', 'attempts': 2, 'sql_generations': 2,
                                 'sql_generation_limit': 2, 'input_result_ids': [], 'result_ids': []}]
        with TemporaryDirectory() as directory:
            store = InvestigationStore(Path(directory) / 'state.sqlite3')
            store.save_task('round-test', 'round-user', 'round-session', 'round_fixture',
                            asdict(fixture.task), RunBudget('round-test', react_rounds=1).export_state())
            fixture.harness.records.clear()
            fixture.harness.store = store
            task = fixture.harness.restore('round-test', 'round-user')
        self.assertEqual(task.final_status, 'waiting_clarification')
        self.assertEqual(task.actions[0]['status'], 'interrupted')
        self.assertEqual(task.actions[0]['sql_generations'], 2)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.executed, [])

    def test_runtime_configuration_cannot_increase_user_maximums(self):
        for options in ({'react_round_limit': 9}, {'sql_generation_limit': 4},
                        {'react_round_limit': 0}, {'sql_generation_limit': True}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    RunBudget('invalid-limit', **options)
        self.assertEqual(RunBudget('lower-limit', react_round_limit=1,
                                   sql_generation_limit=1).react_round_limit, 1)

    def test_lower_sql_generation_setting_disables_automatic_rewrite(self):
        fixture = ControlledHarness()
        request = query('BAD')
        with fixture.bounded(RunBudget('round-test', sql_generation_limit=1)):
            first = fixture.harness._query(fixture.task, request, 'first')
            final = fixture.harness._repair_query(fixture.task, request, first)
        self.assertFalse(final['repair_available'])
        self.assertEqual(fixture.task.actions[0]['sql_generations'], 1)
        self.assertEqual(fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
