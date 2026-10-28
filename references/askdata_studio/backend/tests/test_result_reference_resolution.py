"""One authorized result identity may have a logical ID and storage reference.

These tests never invoke a model, tool transport, or database. They exercise
normalization and scope boundaries at the existing step-decision interface.
"""
from __future__ import annotations

import copy
import json
import unittest

import sqlglot
from sqlglot import exp

from app.task_decisions import validate_step_decision
from app.result_references import model_result_context
from app.workflows.harness import TaskRecord
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController
import test_harness as harness_fixture


class ResultReferenceResolutionTest(unittest.TestCase):
    def context(self):
        return dict(
            step_id='S2', task_id='T1', user_id='analyst-1', datasource_id='homenova_20260831',
            requirements={'Q1': '核实变化', 'Q2': '下降解释'},
            step_conditions=[dict(id='C1', description='可比结果未下降', comparison_required=True,
                                  affected_requirement_ids=['Q2'], affected_step_ids=['S2'])],
            plan_steps=[dict(id='S1', requirement_ids=['Q1']), dict(id='S2', requirement_ids=['Q2'])],
            dependency_results=[dict(id='T1:R1', result_ref='stored-result-1', task_id='T1',
                                     user_id='analyst-1', datasource_id='homenova_20260831',
                                     complete=True, check_status='basic_valid', columns=['period', 'v'],
                                     rows=[dict(period='baseline', v=10), dict(period='current', v=12)])],
        )

    def execute(self, reference='stored-result-1'):
        return dict(action='execute_step', premise_status='confirmed', condition_id='C1',
                    evidence_ids=[reference], reason='读取真实依赖后继续本步取证')

    def skip(self, evidence='stored-result-1', left='T1:R1', right='stored-result-1'):
        return dict(action='skip_step', premise_status='refuted', condition_id='C1',
                    evidence_ids=[evidence], affected_requirement_ids=['Q2'], affected_step_ids=['S2'],
                    remaining_requirement_ids=['Q1'], reason='同一可比范围本期不低于基期',
                    comparisons=[dict(left=dict(result_id=left, field='v', row_match={'period': 'current'}),
                                      operator='ge', right=dict(result_id=right, field='v', row_match={'period': 'baseline'}),
                                      holds=True)])

    def test_storage_reference_and_logical_id_normalize_to_same_evidence(self):
        for reference in ['T1:R1', 'stored-result-1']:
            with self.subTest(reference=reference):
                actual = validate_step_decision(self.execute(reference), self.context())
                self.assertEqual(actual.evidence_ids, ['T1:R1'])

    def test_mixed_aliases_in_numeric_comparison_are_canonicalized(self):
        for action in ['skip_step', 'request_delivery']:
            for refs in [('stored-result-1', 'T1:R1', 'stored-result-1'),
                         ('T1:R1', 'stored-result-1', 'T1:R1')]:
                proposal = self.skip(*refs); proposal['action'] = action
                with self.subTest(action=action, refs=refs):
                    actual = validate_step_decision(proposal, self.context())
                    self.assertEqual(actual.evidence_ids, ['T1:R1'])
                    self.assertEqual(actual.comparisons[0].left.result_id, 'T1:R1')
                    self.assertEqual(actual.comparisons[0].right.result_id, 'T1:R1')

    def test_normalization_does_not_mutate_saved_model_output_or_dependencies(self):
        proposal, context = self.skip(), self.context()
        before = copy.deepcopy((proposal, context))
        validate_step_decision(proposal, context)
        self.assertEqual((proposal, context), before)

    def test_model_view_exposes_logical_identity_without_storage_reference(self):
        context = self.context(); before = copy.deepcopy(context)
        visible = model_result_context(context)
        self.assertEqual(visible['dependency_results'][0]['id'], 'T1:R1')
        self.assertNotIn('result_ref', visible['dependency_results'][0])
        self.assertEqual(visible['dependency_results'][0]['rows'], context['dependency_results'][0]['rows'])
        self.assertEqual(context, before)

    def test_normalized_reference_audit_does_not_claim_new_evidence(self):
        changes = []
        actual = validate_step_decision(self.skip(), self.context(), normalizations=changes)
        self.assertEqual(actual.evidence_ids, ['T1:R1'])
        self.assertTrue(changes)
        self.assertTrue(all(change['from'] == 'stored-result-1' and change['to'] == 'T1:R1' for change in changes))

    def test_alias_and_logical_id_duplicates_represent_one_used_result(self):
        proposal = self.execute()
        proposal['evidence_ids'] = ['stored-result-1', 'T1:R1', 'stored-result-1']
        self.assertEqual(validate_step_decision(proposal, self.context()).evidence_ids, ['T1:R1'])

    def test_unknown_or_non_dependency_reference_is_not_resolved_globally(self):
        context = self.context()
        context['available_results'] = [dict(id='T1:R2', result_ref='not-a-dependency')]
        for reference in ['missing', 'T1:R2', 'not-a-dependency']:
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                validate_step_decision(self.execute(reference), context)

    def test_result_ref_shared_by_two_dependency_results_is_ambiguous(self):
        context = self.context()
        second = copy.deepcopy(context['dependency_results'][0]); second['id'] = 'T1:R2'
        context['dependency_results'].append(second)
        with self.assertRaises(ValueError):
            validate_step_decision(self.execute(), context)

    def test_logical_id_colliding_with_another_results_storage_reference_is_ambiguous(self):
        context = self.context()
        second = copy.deepcopy(context['dependency_results'][0])
        second.update(id='T1:R2', result_ref='T1:R1')
        context['dependency_results'].append(second)
        with self.assertRaises(ValueError):
            validate_step_decision(self.execute('T1:R1'), context)

    def test_duplicate_logical_identity_cannot_silently_select_the_last_version(self):
        context = self.context()
        second = copy.deepcopy(context['dependency_results'][0]); second['result_ref'] = 'different-storage'
        second['rows'][1]['v'] = 5
        context['dependency_results'].append(second)
        with self.assertRaises(ValueError):
            validate_step_decision(self.execute('T1:R1'), context)

    def test_cross_user_task_snapshot_and_invalid_version_are_rejected_for_every_action(self):
        for action in ['execute_step', 'skip_step', 'request_delivery']:
            for field, bad in [('user_id', 'someone-else'), ('task_id', 'T2'),
                               ('datasource_id', 'homenova_20261015'), ('check_status', 'superseded'),
                               ('check_status', 'needs_review'), ('complete', False)]:
                context = self.context(); context['dependency_results'][0][field] = bad
                proposal = self.execute('T1:R1') if action == 'execute_step' else self.skip('T1:R1', 'T1:R1', 'T1:R1')
                proposal['action'] = action
                with self.subTest(action=action, field=field, bad=bad), self.assertRaises(ValueError):
                    validate_step_decision(proposal, context)

    def test_explicit_scope_cannot_be_satisfied_by_missing_metadata(self):
        for field in ['user_id', 'task_id', 'datasource_id']:
            context = self.context(); del context['dependency_results'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_step_decision(self.execute('T1:R1'), context)

    def test_observation_cutoff_scope_is_enforced_without_requiring_display_metadata_equality(self):
        context = self.context()
        context['snapshot'] = dict(observation_cutoff_at='2026-08-31T22:00:00Z', analysis_date='2026-08-31')
        context['dependency_results'][0]['snapshot'] = dict(observation_cutoff_at='2026-08-31T22:00:00Z')
        actual = validate_step_decision(self.execute('T1:R1'), context)
        self.assertEqual(actual.evidence_ids, ['T1:R1'])
        context['dependency_results'][0]['snapshot']['observation_cutoff_at'] = '2026-10-15T22:00:00Z'
        with self.assertRaises(ValueError):
            validate_step_decision(self.execute('T1:R1'), context)

    def test_missing_snapshot_identity_is_not_sufficient_for_explicit_cutoff_scope(self):
        context = self.context(); context['snapshot'] = dict(observation_cutoff_at='2026-08-31T22:00:00Z')
        with self.assertRaises(ValueError):
            validate_step_decision(self.execute('T1:R1'), context)

    def test_legacy_fixture_without_task_or_user_scope_keeps_logical_id_support(self):
        context = self.context()
        for field in ['task_id', 'user_id']:
            del context[field]; del context['dependency_results'][0][field]
        self.assertEqual(validate_step_decision(self.execute('T1:R1'), context).evidence_ids, ['T1:R1'])

    def test_normalized_comparison_still_requires_declared_supporting_evidence(self):
        context = self.context()
        second = copy.deepcopy(context['dependency_results'][0]); second.update(id='T1:R2', result_ref='stored-result-2')
        context['dependency_results'].append(second)
        proposal = self.skip(); proposal['comparisons'][0]['right']['result_id'] = 'stored-result-2'
        with self.assertRaises(ValueError):
            validate_step_decision(proposal, context)

    def test_normalized_comparison_does_not_bypass_numeric_truth_or_row_uniqueness(self):
        for change in ['false', 'missing_row', 'duplicate_row', 'unknown_field']:
            context = self.context(); proposal = self.skip('T1:R1', 'T1:R1', 'T1:R1')
            if change == 'false': context['dependency_results'][0]['rows'][1]['v'] = 8
            if change == 'missing_row': context['dependency_results'][0]['rows'] = context['dependency_results'][0]['rows'][:1]
            if change == 'duplicate_row': context['dependency_results'][0]['rows'].append(dict(period='current', v=12))
            if change == 'unknown_field': proposal['comparisons'][0]['left']['field'] = 'invented'
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_step_decision(proposal, context)

    def test_pending_check_blocks_branch_skip_but_allows_more_evidence_collection(self):
        context = self.context()
        context['dependency_results'][0]['query_contract'] = dict(validation=dict(pending_checks=[dict(code='unresolved_scope')]))
        self.assertEqual(validate_step_decision(self.execute(), context).evidence_ids, ['T1:R1'])
        for action in ['skip_step', 'request_delivery']:
            proposal = self.skip(); proposal['action'] = action
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_step_decision(proposal, context)

    def test_final_reviewer_uses_same_reference_resolution_and_single_identity_view(self):
        helper = harness_fixture.HarnessTest()
        reply = harness_fixture.reviewed(ids=['stored-result-1'], final=True)
        model = harness_fixture.ScriptModel([reply])
        harness, _ = helper.harness(model)
        state = helper.state(); state['access_scope']['user_id'] = 'analyst-1'
        task = TaskRecord(state, [harness_fixture.QUESTION])
        task.steps = [{**harness_fixture.step(), 'status': 'completed', 'result_ids': ['T1:R1']}]
        task.evidence = self.context()['dependency_results']
        result = harness._review_call(task, True)
        self.assertEqual(result.answered[0].evidence_ids, ['T1:R1'])
        model_payload = json.loads(model.calls[0][1])
        self.assertEqual(model_payload['evidence'][0]['id'], 'T1:R1')
        self.assertNotIn('result_ref', model_payload['evidence'][0])
        self.assertEqual(reply['answered'][0]['evidence_ids'], ['stored-result-1'])

    def test_final_reviewer_does_not_gain_access_to_unknown_or_expired_results(self):
        for reference, status in [('unknown', 'basic_valid'), ('stored-result-1', 'superseded')]:
            helper = harness_fixture.HarnessTest()
            model = harness_fixture.ScriptModel([harness_fixture.reviewed(ids=[reference], final=True)])
            harness, _ = helper.harness(model)
            state = helper.state(); state['access_scope']['user_id'] = 'analyst-1'
            task = TaskRecord(state, [harness_fixture.QUESTION]); task.steps = [harness_fixture.step()]
            task.evidence = self.context()['dependency_results']; task.evidence[0]['check_status'] = status
            with self.subTest(reference=reference, status=status), self.assertRaises(ValueError):
                harness._review_call(task, True)


class ExplicitDerivedStarCompatibilityTest(unittest.TestCase):
    """Only parse/normalize SQL; never preflight or read database rows."""

    def setUp(self):
        self.engine = DuckDbEngine()
        self.scope = AccessController().resolve('demo_analyst')

    def validate(self, sql):
        return self.engine._validate_sql('askdata_mock', sql, self.scope)

    def assert_explicit(self, sql, expected_columns):
        normalized = self.validate(sql)
        statement = sqlglot.parse_one(normalized, read='duckdb')
        for select in statement.find_all(exp.Select):
            self.assertFalse(any(isinstance(item, exp.Star) or isinstance(item, exp.Column) and item.is_star
                                 for item in select.expressions))
        self.assertEqual([item.alias_or_name for item in statement.selects], expected_columns)
        return normalized

    def test_single_explicit_cte_star_is_expanded_in_place(self):
        sql = ('WITH monthly AS (SELECT region AS area, SUM(paid_amount) AS amount '
               'FROM orders_current GROUP BY region) SELECT * FROM monthly')
        normalized = self.assert_explicit(sql, ['area', 'amount'])
        self.assertIn('SUM(paid_amount)', normalized)
        self.assertIn('GROUP BY region', normalized)

    def test_nested_derived_stars_and_cte_column_aliases_keep_names_and_order(self):
        self.assert_explicit(
            'WITH source(region_name, total) AS (SELECT region, paid_amount FROM orders_current), '
            'projected AS (SELECT s.* FROM source s) SELECT p.* FROM projected p',
            ['region_name', 'total'])

    def test_physical_stars_and_star_modifiers_are_still_rejected(self):
        cases = [
            'SELECT * FROM orders_current',
            'SELECT o.* FROM orders_current o',
            'WITH x AS (SELECT * FROM orders_current) SELECT * FROM x',
            'WITH x AS (SELECT region, paid_amount FROM orders_current) SELECT * EXCLUDE(region) FROM x',
            'WITH x AS (SELECT region, paid_amount FROM orders_current) SELECT * REPLACE(paid_amount * 2 AS paid_amount) FROM x',
        ]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.validate(sql)

    def test_using_or_natural_join_star_is_not_expanded_to_duplicate_merged_columns(self):
        ctes = ('WITH x AS (SELECT region, SUM(paid_amount) AS first_total FROM orders_current GROUP BY region), '
                'y AS (SELECT region, SUM(paid_amount) AS second_total FROM orders_current GROUP BY region) ')
        for suffix in ['SELECT * FROM x JOIN y USING (region)', 'SELECT * FROM x NATURAL FULL JOIN y']:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.validate(ctes + suffix)

    def test_duplicate_case_insensitive_derived_names_are_not_guessed(self):
        with self.assertRaises(ValueError):
            self.validate('WITH x AS (SELECT region AS amount, paid_amount AS AMOUNT FROM orders_current) SELECT * FROM x')

    def test_derived_star_does_not_hide_denied_physical_source(self):
        denied = AccessController().resolve('demo_analyst').public()
        denied['allowed_tables'] = ['orders_current']
        from app.security import AccessScope
        for sql in [
            'WITH x AS (SELECT paid_amount FROM orders_history) SELECT * FROM x',
            'WITH orders_history AS (SELECT paid_amount FROM orders_history) SELECT * FROM orders_history',
        ]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.engine._validate_sql('askdata_mock', sql, AccessScope.from_dict(denied))


if __name__ == '__main__':
    unittest.main()
