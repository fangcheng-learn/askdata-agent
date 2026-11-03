"""Direct-query generation limits; all model, validation and MCP calls are stubs."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from app.errors import PipelineStageError
from app.querying.single_database_agent import SingleDatabaseAgent
from app.runtime_budget import BudgetStop, RunBudget, current_budget


SOURCE = 'generation_fixture'


def sql(label='one', database=SOURCE):
    return {'action': 'call_tool', 'tool_name': 'query_' + database,
            'arguments': {'sql': 'SELECT 1 AS ' + label}, 'query_contract': {'tag': label}}


def clock():
    return {'action': 'call_tool', 'tool_name': 'current_datetime', 'arguments': {}}


class Model:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def chat_json(self, system, user):
        budget = current_budget.get()
        self.calls.append({'request': json.loads(user), 'node': budget.model_node if budget else None,
                           'saved_counts': dict(budget.direct_sql_generations) if budget else {}})
        if not self.outputs:
            raise AssertionError('A forbidden extra SQL generation was requested')
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class DirectGenerationTest(unittest.TestCase):
    def agent(self, outputs, *, validation=(), execution=(), database=SOURCE):
        model = Model(outputs)
        calls, validations = [], []
        validation = iter(validation)
        execution = iter(execution)

        def validate(*args, **kwargs):
            validations.append((args, kwargs))
            outcome = next(validation, None)
            if isinstance(outcome, Exception):
                raise outcome
            return {'validation': {'normalizations': [], 'pending_checks': []}}

        def tool(name, arguments):
            calls.append((name, arguments))
            if name == 'current_datetime':
                return {'datetime': '2026-10-29T12:00:00+08:00'}
            return next(execution, {'success': True, 'rows': [{'v': 1}], 'columns': ['v'], 'complete': True})

        client = SimpleNamespace(list_tools=lambda: [{'name': 'query_' + database}, {'name': 'current_datetime'}],
                                 call_tool=tool)
        skill = SimpleNamespace(name='database_query', instructions='Return JSON.', allowed_tools=('query_*', 'current_datetime'),
                                max_tool_calls=5, output_actions=('call_tool', 'clarify'))
        agent = SingleDatabaseAgent(model, lambda scope: client, skill, SimpleNamespace(), max_tool_calls=5)
        agent.contract_validator = SimpleNamespace(validate=validate)
        return agent, model, calls, validations

    def run_agent(self, agent, budget=None, *, step=None, database=SOURCE):
        token = current_budget.set(budget)
        try:
            return agent.prepare('核对指标', database, {}, '', {}, {}, {},
                                 task_context={'step_id': step} if step else None)
        finally:
            current_budget.reset(token)

    def test_inner_validation_and_outer_execution_share_three_generations(self):
        agent, model, calls, _ = self.agent([sql('first'), sql('second'), sql('third'), sql('forbidden')],
            validation=[ValueError('invalid contract')], execution=[{'success': False, 'error': 'execution failed'},
                                                                  {'success': False, 'error': 'still failed'}])
        budget = RunBudget('direct')
        with self.assertRaises(BudgetStop) as stopped:
            self.run_agent(agent, budget)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(budget.direct_sql_generations, {SOURCE + ':direct': 3})
        self.assertEqual(budget.repairs, {'entry': 2})
        self.assertEqual(stopped.exception.context['sql_generations'], 3)

    def test_execution_failure_then_invalid_rewrite_cannot_reset_inner_loop(self):
        agent, model, calls, _ = self.agent([sql('first'), sql('second'), sql('third'), sql('forbidden')],
            validation=[None, ValueError('invalid replacement'), ValueError('replacement still invalid')],
            execution=[{'success': False, 'error': 'bad SQL'}])
        with self.assertRaises(PipelineStageError):
            self.run_agent(agent, RunBudget('direct'))
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(len(calls), 1)

    def test_non_sql_date_actions_do_not_consume_generation_or_repair(self):
        agent, model, calls, _ = self.agent([sql('first'), clock(), sql('second')],
            execution=[{'success': False, 'error': 'bad SQL'}, {'success': True}])
        budget = RunBudget('direct')
        result = self.run_agent(agent, budget)
        self.assertEqual(result['sql_generations'], 2)
        self.assertEqual(budget.repairs, {'entry': 1})
        self.assertEqual(len(model.calls), 3)
        self.assertEqual([name for name, _ in calls], ['query_' + SOURCE, 'current_datetime', 'query_' + SOURCE])

    def test_success_returns_without_another_coder_call(self):
        agent, model, calls, _ = self.agent([clock(), clock(), sql('one')])
        result = self.run_agent(agent, RunBudget('direct'))
        self.assertEqual(result['action'], 'executed')
        self.assertEqual(result['sql_generations'], 1)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(sum(name.startswith('query_') for name, _ in calls), 1)

    def test_usage_unknown_call_stays_counted_before_restore(self):
        agent, model, calls, _ = self.agent([RuntimeError('unknown model response')])
        budget = RunBudget('direct')
        with self.assertRaises(PipelineStageError):
            self.run_agent(agent, budget)
        self.assertEqual(model.calls[0]['saved_counts'], {SOURCE + ':direct': 1})
        restored = RunBudget.from_state('direct', json.loads(json.dumps(budget.export_state())))
        second, second_model, _, _ = self.agent([sql('second')])
        self.run_agent(second, restored)
        self.assertEqual(second_model.calls[0]['saved_counts'], {SOURCE + ':direct': 2})
        third, third_model, _, _ = self.agent([sql('third')])
        self.run_agent(third, restored)
        self.assertEqual(third_model.calls[0]['saved_counts'], {SOURCE + ':direct': 3})
        forbidden, forbidden_model, _, _ = self.agent([sql('fourth')])
        with self.assertRaises(BudgetStop):
            self.run_agent(forbidden, restored)
        self.assertEqual(forbidden_model.calls, [])
        self.assertEqual(calls, [])

    def test_clarification_from_sql_generation_keeps_dispatched_slot(self):
        clarification = {'action': 'clarify', 'clarification': {'parameter': 'scope', 'question': '范围？',
            'reason': '影响查询', 'options': [{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}]}}
        agent, model, calls, _ = self.agent([clarification])
        budget = RunBudget('direct')
        self.assertEqual(self.run_agent(agent, budget)['action'], 'clarify')
        self.assertEqual(budget.direct_sql_generations, {SOURCE + ':direct': 1})
        self.assertEqual(calls, [])

    def test_separate_steps_have_separate_limits(self):
        agent, model, _, _ = self.agent([sql('s1'), sql('s2')])
        budget = RunBudget('direct')
        self.run_agent(agent, budget, step='S1')
        self.run_agent(agent, budget, step='S2')
        self.assertEqual(budget.direct_sql_generations, {SOURCE + ':S1': 1, SOURCE + ':S2': 1})
        self.assertTrue(all(item['node'] == 'query_coder' for item in model.calls))

    def test_limit_can_be_lowered_to_one(self):
        agent, model, calls, _ = self.agent([sql('first'), sql('forbidden')],
            execution=[{'success': False, 'error': 'bad SQL'}])
        with self.assertRaises(BudgetStop):
            self.run_agent(agent, RunBudget('direct', sql_generation_limit=1))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(calls), 1)

    def test_without_run_budget_still_uses_three_generations(self):
        agent, model, calls, _ = self.agent([sql('first'), sql('second'), sql('third'), sql('forbidden')],
            execution=[{'success': False}, {'success': False}, {'success': False}])
        with self.assertRaises(BudgetStop):
            self.run_agent(agent)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(len(calls), 3)

    def test_old_checkpoint_limit_two_is_not_upgraded_or_refunded(self):
        saved = RunBudget('old', sql_generation_limit=2).export_state()
        restored = RunBudget.from_state('old', saved)
        agent, model, calls, _ = self.agent([sql('first'), sql('second'), sql('forbidden')],
            execution=[{'success': False}, {'success': False}])
        with self.assertRaises(BudgetStop):
            self.run_agent(agent, restored)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(calls), 2)

    def test_sql_disguised_as_clock_action_is_not_refunded_or_executed(self):
        disguised = clock()
        disguised['arguments'] = {'sql': 'SELECT 99 AS hidden'}
        agent, model, calls, _ = self.agent([disguised, sql('fixed')])
        budget = RunBudget('direct')
        result = self.run_agent(agent, budget)
        self.assertEqual(result['sql_generations'], 2)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual([name for name, _ in calls], ['query_' + SOURCE])


if __name__ == '__main__':
    unittest.main()
