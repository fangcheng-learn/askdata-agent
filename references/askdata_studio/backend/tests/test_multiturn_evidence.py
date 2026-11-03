"""Multi-turn reports use real scoped stored fixtures, no model service or SQL."""
from copy import deepcopy
import json
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.config import Settings
from app.models import QueryResult
from app.preprocessing import PreparedRequest
from app.querying.analysis_evidence import load_analysis_evidence, model_analysis_context
from app.querying.data_qa_agent import DataQaAgent
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessScope
from app.services.investigation_store import InvestigationStore
from app.services.session_context import SessionContext
from app.skills import SkillRegistry
from app.workflows.query_graph import QueryWorkflow


SOURCE = 'homenova_20260831'
SNAPSHOT = {'snapshot_id': 'fixture_aug', 'observation_cutoff_at': '2026-08-31'}
TABLE = {'id': 'fixture_table', 'name': 'fixture_metrics', 'database': SOURCE, 'fields': []}


class ScriptModel:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def chat_json(self, system, payload):
        self.calls.append((system, json.loads(payload)))
        return self.outputs.pop(0)


class MultiturnEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = InvestigationStore(Path(self.folder.name) / 'results.sqlite3')
        self.engine = DuckDbEngine()
        self.engine.result_store = self.store
        self.engine.execute = self.no_query
        self.scope = AccessScope('user-1', ('analyst',), frozenset({SOURCE}), frozenset({'fixture_table'})).public()
        self.schema_patch = patch('app.querying.analysis_evidence.SCHEMA', [TABLE])
        self.schema_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.entries = [self.save('R1', [{'month': f'row-{i}', 'gmv': i} for i in range(300)]),
                        self.save('R2', [{'category': 'A', 'change': -30}, {'category': 'B', 'change': 10}])]

    @staticmethod
    def no_query(*args, **kwargs):
        raise AssertionError('Multi-turn analysis must not query SQL, MCP, or the model service')

    def save(self, suffix, rows, *, complete=True):
        rid, ref = f'old-task:{suffix}', 'storage-' + suffix
        columns = list(rows[0]) if rows else ['value']
        sql = 'SELECT ' + ','.join(columns) + ' FROM fixture_metrics'
        full = {'user_id': 'user-1', 'task_id': 'old-task', 'datasource_id': SOURCE,
                'sql': sql, 'columns': columns, 'rows': rows, 'complete': complete,
                'total_rows': len(rows) if complete else None}
        self.store.save_result(ref, full)
        return {'id': rid, 'user_id': 'user-1', 'task_id': 'old-task', 'datasource_id': SOURCE,
                'snapshot': deepcopy(SNAPSHOT), 'result_ref': ref, 'sql': sql,
                'query_contract': {'tables': ['fixture_metrics']}, 'columns': columns, 'rows': rows[:20],
                'stored_rows': len(rows), 'total_rows': full['total_rows'], 'complete': complete,
                'display_truncated': len(rows) > 20, 'check_status': 'reviewed',
                'fingerprint': 'version-' + suffix, 'goal': suffix}

    def state(self, entries=None, **overrides):
        parent = {'task_id': 'old-task', 'user_id': 'user-1', 'status': 'completed',
                  'workflow_mode': 'investigation_react_v1', 'final_reviewed': True,
                  'datasource_id': SOURCE, 'scenario': SNAPSHOT, 'analysis': '原报告[old-task:R1]',
                  'evidence': deepcopy(self.entries if entries is None else entries)}
        return {'task_id': 'follow-task', 'query': '在原报告里补上核心指标对比', 'datasource_id': SOURCE,
                'scenario': deepcopy(SNAPSHOT), 'session_id': 'same-session',
                'access_scope': deepcopy(self.scope), 'intent': {'reason': '已有结果追问'},
                'recent_result_context': json.dumps(parent), **overrides}

    def bundle(self, **kwargs):
        return load_analysis_evidence(self.state(), self.engine, **kwargs)

    def workflow(self, outputs):
        model = ScriptModel(outputs)
        agent = DataQaAgent(model, self.no_query, SkillRegistry().get('data_qa'))
        workflow = QueryWorkflow.__new__(QueryWorkflow)
        workflow.database_engine = self.engine
        workflow.model_client = model
        workflow.data_qa_agent = agent
        workflow._business_methods = lambda state: {}
        return workflow, model

    @staticmethod
    def report(**updates):
        return {'action': 'report', 'title': '补充报告', 'markdown': '核心指标[old-task:R1]；贡献[old-task:R2]',
                'visualizations': [{'type': 'bar', 'title': '贡献', 'source_result_id': 'old-task:R2',
                                    'category_field': 'category', 'value_field': 'change'}], **updates}

    @staticmethod
    def approved(text='核心指标[old-task:R1]；贡献[old-task:R2]'):
        from app.querying.analysis_output import QA_REVIEW_CRITERIA
        refs = re.findall(r'\[([^\]@]+)(?:@[^\]]+)?\]', text)
        return {'valid': True, 'approved_text': '', 'approve_draft_unchanged': True,
                'reason': '引用原证据', 'text_edits': [],
                'audit_findings': [{'finding_id': f'F{index}', 'criterion': criterion,
                    'verdict': 'supported', 'evidence_ids': refs, 'context_refs': ['current_question'],
                    'finding': '仅解释已保存的金额和贡献列，未新增因果判断',
                    'resolved_in_final': False, 'text_edit_indices': []}
                    for index, criterion in enumerate(QA_REVIEW_CRITERIA)]}

    def test_each_r_is_loaded_from_its_original_task_and_300_rows_are_not_preview(self):
        bundle = self.bundle()
        self.assertEqual([item['id'] for item in bundle['evidence']], ['old-task:R1', 'old-task:R2'])
        self.assertEqual(len(bundle['evidence'][0]['rows']), 300)
        self.assertTrue(bundle['evidence'][0]['complete'])
        self.assertFalse(bundle['evidence'][0]['display_truncated'])
        self.assertEqual(bundle['evidence'][0]['version'], 'version-R1')
        self.assertEqual(bundle['evidence_gaps'], [])
        self.assertEqual(self.engine.results['storage-R1']['task_id'], 'old-task')

    def test_process_restart_reads_durable_result_without_new_query_or_clone(self):
        first = self.bundle()
        self.engine = DuckDbEngine()
        self.engine.result_store = InvestigationStore(Path(self.folder.name) / 'results.sqlite3')
        self.engine.execute = self.no_query
        restored = self.bundle()
        self.assertEqual(restored['evidence'], first['evidence'])
        self.assertEqual(set(self.engine.results), {'storage-R1', 'storage-R2'})

    def test_bounded_context_remains_preview_and_inherited_gap(self):
        bundle = self.bundle(max_rows=30)
        self.assertEqual(len(bundle['evidence'][0]['rows']), 30)
        self.assertFalse(bundle['evidence'][0]['complete'])
        self.assertTrue(bundle['evidence'][0]['display_truncated'])
        self.assertEqual(bundle['evidence'][0]['total_rows'], 300)
        self.assertIn('禁止据此计算全集', bundle['evidence_gaps'][0])

    def test_original_incomplete_result_cannot_become_complete_when_all_stored_rows_fit(self):
        incomplete = self.save('R3', [{'value': 2}], complete=False)
        result = load_analysis_evidence(self.state([incomplete]), self.engine)
        self.assertFalse(result['evidence'][0]['complete'])
        self.assertTrue(result['evidence_gaps'])

    def test_current_question_reads_only_selected_r_and_rejects_foreign_reference(self):
        bundle = load_analysis_evidence(self.state(analysis_result_ids=['old-task:R2']), self.engine)
        self.assertEqual([e['id'] for e in bundle['evidence']], ['old-task:R2'])
        self.assertEqual(len(bundle['evidence'][0]['rows']), 2)
        with self.assertRaises(PermissionError):
            load_analysis_evidence(self.state(analysis_result_ids=['unknown-task:R1']), self.engine)

    def report_state(self, *, entries=None, markdown='原报告[old-task:R1]；已核实对比[old-task:R2]', **overrides):
        state = self.state(entries, analysis_mode='report', analysis_result_ids=['old-task:R1'])
        parent = json.loads(state['recent_result_context'])
        parent['report'] = {'markdown': markdown, 'visualizations': []}
        state['analysis_context'] = json.dumps([parent])
        return {**state, **overrides}

    def test_report_selected_subset_closes_real_parent_citations_but_not_all_task_results(self):
        untouched = self.save('R3', [{'ledger_row': 'unneeded'}])
        state = self.report_state(entries=[*self.entries, untouched])
        original_context = state['analysis_context']
        workflow, model = self.workflow([self.report(), self.approved()])
        with patch.object(self.engine, 'read_result', wraps=self.engine.read_result) as read:
            result = workflow._answer_qa(state)['result']
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['final_reviewed'])
        self.assertEqual(len(model.calls), 2, '不增加路由、SQL、模型补写或修复调用')
        self.assertEqual([call.args[0] for call in read.call_args_list], ['storage-R1', 'storage-R2'])
        self.assertEqual({call.args[2] for call in read.call_args_list}, {'old-task'})
        self.assertEqual([source['result_id'] for source in result['report']['data_sources']], ['old-task:R1', 'old-task:R2'])
        reuse = next(log for log in result['execution_log'] if log['stage'] == 'existing_evidence_reuse')
        self.assertEqual(reuse['report_reference_closure']['added_result_ids'], ['old-task:R2'])
        self.assertEqual(reuse['new_sql_calls'], 0)
        references = model.calls[0][1]['context']['prior_reports'][0]['references']
        self.assertTrue(all(ref['status'] == 'available' for ref in references))
        self.assertEqual(references[1]['version'], 'version-R2')
        self.assertEqual(state['analysis_context'], original_context)

    def test_fact_mode_does_not_close_parent_report_or_expand_selected_result(self):
        state = self.report_state(analysis_mode='fact', query='刚才指标差多少？')
        answer = '已保存对比[old-task:R1]'
        workflow, model = self.workflow([{'action': 'answer', 'answer': answer}, self.approved(answer)])
        with patch.object(self.engine, 'read_result', wraps=self.engine.read_result) as read:
            result = workflow._answer_qa(state)['result']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([call.args[0] for call in read.call_args_list], ['storage-R1'])
        self.assertEqual([item['id'] for item in result['evidence']], ['old-task:R1'])
        self.assertNotIn('prior_reports', model.calls[0][1]['context'])
        self.assertNotIn('report_reference_closure', model.calls[0][1]['context'])

    def test_report_closure_does_not_discover_unselected_task_from_citation_or_reusable_cache(self):
        foreign = {**self.entries[1], 'id': 'unselected:R1', 'task_id': 'unselected'}
        state = self.report_state(markdown='原报告[old-task:R1]及外部引用[unselected:R1]',
                                  reusable_evidence=[foreign])
        with patch.object(self.engine, 'read_result', wraps=self.engine.read_result) as read:
            bundle = load_analysis_evidence(state, self.engine)
        self.assertEqual([call.args[0] for call in read.call_args_list], ['storage-R1'])
        self.assertEqual([item['id'] for item in bundle['evidence']], ['old-task:R1'])
        ref = bundle['report_reference_closure']['unavailable_references'][0]
        self.assertEqual(ref['result_id'], 'unselected:R1')
        self.assertIn('未搜索其他任务', ref['unavailable_reason'])
        self.assertIn('不可沿用', str(bundle['data_gaps']))

    def test_report_missing_or_superseded_reference_is_explicit_gap_not_authorized_source(self):
        for change in [{'check_status': 'superseded'}, {'result_ref': 'deleted-result'}]:
            with self.subTest(change=change):
                entries = [self.entries[0], {**self.entries[1], **change}]
                bundle = load_analysis_evidence(self.report_state(entries=entries), self.engine)
                self.assertEqual([item['id'] for item in bundle['evidence']], ['old-task:R1'])
                self.assertNotIn('old-task:R2', bundle['report_scope']['authorized_analysis_sources'])
                self.assertEqual(bundle['report_reference_closure']['added_result_ids'], [])
                ref = bundle['report_reference_closure']['unavailable_references'][0]
                self.assertEqual(ref['result_id'], 'old-task:R2')
                self.assertEqual(ref['status'], 'unavailable')
                self.assertTrue(bundle['data_gaps'])

    def test_report_stale_citation_or_source_version_cannot_silently_bind_current_result(self):
        for origin in ['citation', 'source_catalog', 'chart']:
            with self.subTest(origin=origin):
                state = self.report_state(markdown='原报告[old-task:R1]')
                parent = json.loads(state['analysis_context'])[0]
                report = parent['report']
                if origin == 'citation':
                    report['markdown'] += '[old-task:R2@obsolete]'
                elif origin == 'source_catalog':
                    report['markdown'] += '[old-task:R2]'
                    report['data_sources'] = [{'result_id': 'old-task:R2', 'version': 'obsolete'}]
                else:
                    report['visualizations'] = [{'source_result_id': 'old-task:R2', 'source_version': 'obsolete'}]
                state['analysis_context'] = json.dumps([parent])
                with patch.object(self.engine, 'read_result', wraps=self.engine.read_result) as read:
                    bundle = load_analysis_evidence(state, self.engine)
                self.assertEqual([call.args[0] for call in read.call_args_list], ['storage-R1'])
                self.assertEqual([item['id'] for item in bundle['evidence']], ['old-task:R1'])
                self.assertIn('版本', bundle['report_reference_closure']['unavailable_references'][0]['unavailable_reason'])

    def test_report_chart_reference_uses_saved_version_without_loading_unused_catalog(self):
        state = self.report_state(markdown='原报告[old-task:R1]')
        parent = json.loads(state['analysis_context'])[0]
        parent['report']['visualizations'] = [{'source_result_id': 'old-task:R2', 'source_version': 'version-R2'}]
        parent['report']['data_sources'] = [{'result_id': 'not-used:R8', 'version': 'unused'}]
        state['analysis_context'] = json.dumps([parent])
        bundle = load_analysis_evidence(state, self.engine)
        self.assertEqual(bundle['report_reference_closure']['added_result_ids'], ['old-task:R2'])
        self.assertEqual(bundle['report_reference_closure']['unavailable_references'], [])

    def test_auto_added_report_results_keep_current_owner_source_snapshot_and_table_checks(self):
        for key, value in [('user_id', 'other-user'), ('datasource_id', 'other-source'),
                           ('task_id', 'other-original-task'), ('snapshot', {'snapshot_id': 'changed'}),
                           ('query_contract', {'tables': ['forbidden_table']})]:
            entries = [self.entries[0], {**self.entries[1], key: value}]
            with self.subTest(key=key), self.assertRaises(PermissionError):
                load_analysis_evidence(self.report_state(entries=entries), self.engine)

    def test_report_parent_owner_source_and_snapshot_cannot_be_rebound_by_valid_child_evidence(self):
        for key, value in [('user_id', 'other-user'), ('datasource_id', 'other-source'),
                           ('scenario', {'snapshot_id': 'changed'})]:
            state = self.report_state()
            parent = json.loads(state['analysis_context'])[0]
            parent[key] = value
            state['analysis_context'] = json.dumps([parent])
            with self.subTest(key=key), self.assertRaises(PermissionError):
                load_analysis_evidence(state, self.engine)

    def test_prior_followup_report_retains_original_result_task_binding(self):
        state = self.report_state()
        parent = json.loads(state['analysis_context'])[0]
        parent['task_id'] = 'prior-followup'
        state['analysis_context'] = json.dumps([parent])
        with patch.object(self.engine, 'read_result', wraps=self.engine.read_result) as read:
            bundle = load_analysis_evidence(state, self.engine)
        self.assertEqual({call.args[2] for call in read.call_args_list}, {'old-task'})
        self.assertEqual(bundle['report_reference_closure']['added_result_ids'], ['old-task:R2'])
        self.assertEqual(bundle['evidence'][1]['version'], 'version-R2')

    def test_report_closure_keeps_existing_row_bounds_and_exposes_incomplete_result(self):
        bundle = load_analysis_evidence(self.report_state(), self.engine, max_rows=1, max_total_rows=2)
        self.assertEqual([len(item['rows']) for item in bundle['evidence']], [1, 1])
        self.assertFalse(bundle['evidence'][1]['complete'])
        self.assertFalse(bundle['prior_reports'][0]['references'][1]['complete'])
        self.assertTrue(bundle['data_gaps'])

    def test_model_view_deduplicates_validation_without_altering_stored_evidence(self):
        entry = deepcopy(self.entries[0])
        entry['column_metadata'] = {'gmv': {'unit': 'cents', 'definition': '保持原口径'}}
        entry['query_contract']['validation'] = {'column_metadata': deepcopy(entry['column_metadata']),
            'normalizations': [{'path': 'tables', 'before': None, 'after': ['fixture_metrics']}],
            'pending_checks': [{'code': 'needs_verification'}],
            'sql_summary': {'normalized_sql': entry['sql'], 'source_fields': ['fixture_metrics.gmv']}}
        view = model_analysis_context(entry)
        self.assertEqual(view['column_metadata'], entry['column_metadata'])
        self.assertNotIn('column_metadata', view['query_contract']['validation'])
        self.assertEqual(view['query_contract']['validation']['pending_checks'], [{'code': 'needs_verification'}])
        self.assertIn('normalizations', entry['query_contract']['validation'])
        self.assertEqual(view['sql'], entry['sql'])

    def test_selected_task_precedes_unrelated_recent_result(self):
        state = self.state()
        selected = json.loads(state['recent_result_context'])
        selected['evidence'] = [self.entries[1]]
        state['analysis_context'] = json.dumps([selected])
        bundle = load_analysis_evidence(state, self.engine)
        self.assertEqual([item['id'] for item in bundle['evidence']], ['old-task:R2'])

    def test_other_user_source_snapshot_or_task_binding_is_rejected(self):
        for key, value in [('user_id', 'other-user'), ('datasource_id', 'future-source'),
                           ('task_id', 'other-task'), ('snapshot', {'snapshot_id': 'future'})]:
            changed = deepcopy(self.entries)
            changed[0][key] = value
            with self.subTest(key=key), self.assertRaises(PermissionError):
                load_analysis_evidence(self.state(changed), self.engine)

    def test_current_table_permissions_are_rechecked_before_reading(self):
        state = self.state()
        state['access_scope']['allowed_tables'] = []
        with self.assertRaisesRegex(PermissionError, '表已不在当前权限'):
            load_analysis_evidence(state, self.engine)
        self.assertEqual(self.engine.results, {})

    def test_actual_persisted_sql_cannot_hide_denied_table_behind_metadata(self):
        self.store.save_result('evil-storage', {'user_id': 'user-1', 'task_id': 'old-task', 'datasource_id': SOURCE,
            'sql': 'SELECT secret FROM forbidden_table', 'columns': ['month', 'gmv'],
            'rows': [{'month': 'x', 'gmv': 1}], 'complete': True, 'total_rows': 1})
        entry = deepcopy(self.entries[0]); entry['result_ref'] = 'evil-storage'
        with self.assertRaisesRegex(PermissionError, '真实SQL来源'):
            load_analysis_evidence(self.state([entry]), self.engine)

    def test_missing_result_is_gap_not_zero_and_superseded_is_not_used(self):
        entries = deepcopy(self.entries)
        entries[0]['result_ref'] = 'not-stored'
        entries[1]['check_status'] = 'superseded'
        result = load_analysis_evidence(self.state(entries), self.engine)
        self.assertEqual(result['evidence'], [])
        self.assertTrue(result['evidence_gaps'])
        self.assertNotIn('gmv', str(result['evidence']))

    def test_conflicting_result_identity_is_not_overwritten(self):
        duplicate = {**self.entries[0], 'fingerprint': 'different-version'}
        with self.assertRaisesRegex(ValueError, '冲突来源或版本'):
            load_analysis_evidence(self.state([self.entries[0], duplicate]), self.engine)

    def test_unselected_reusable_evidence_is_not_mixed_in(self):
        other = {**self.entries[0], 'id': 'other:R1', 'task_id': 'unrelated-task'}
        result = load_analysis_evidence(self.state(reusable_evidence=[other]), self.engine)
        self.assertEqual(len(result['evidence']), 2)

    def test_report_supplement_preserves_multi_result_versions_and_does_not_call_mcp(self):
        workflow, model = self.workflow([self.report(), self.approved()])
        raw = workflow._answer_qa(self.state())['result']
        self.assertEqual(raw['status'], 'completed')
        self.assertTrue(raw['final_reviewed'])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual([item['result_id'] for item in raw['report']['data_sources']], ['old-task:R1', 'old-task:R2'])
        chart = raw['report']['visualizations'][0]
        self.assertEqual(chart['source_result_id'], 'old-task:R2')
        self.assertEqual(chart['source_task_id'], 'old-task')
        self.assertEqual(chart['source_version'], 'version-R2')
        self.assertEqual(len(model.calls[0][1]['context']['evidence'][0]['rows']), 300)
        self.assertNotIn('storage-R1', json.dumps(model.calls, ensure_ascii=False))
        self.assertNotIn('user_id', model.calls[0][1]['context']['evidence'][0])
        self.assertEqual(raw['sql'], None)

    def test_plain_followup_keeps_answer_mode_with_the_same_result_catalog(self):
        workflow, model = self.workflow([{'action': 'answer', 'answer': '两份结果都可解释[old-task:R1][old-task:R2]', 'tool_calls': []},
                                        self.approved('两份结果都可解释[old-task:R1][old-task:R2]')])
        state = self.state(query='刚才的贡献里有哪些抵消项？')
        result = workflow._answer_qa(state)['result']
        self.assertIsNone(result['report'])
        self.assertEqual(result['route'], 'data_qa')
        self.assertEqual(len(result['evidence']), 2)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual({item['result_id'] for item in model.calls[0][1]['available_data_sources']},
                         {'old-task:R1', 'old-task:R2'})

    def test_original_partial_does_not_become_completed_by_reformatting(self):
        state = self.state()
        parent = json.loads(state['recent_result_context'])
        parent.update(status='partial', stop_reason='支付障碍的必要证据尚未取得',
                      requirements=[{'id': 'Q2', 'question': '支付为何减少', 'status': 'open'}])
        state['recent_result_context'] = json.dumps(parent)
        workflow, _ = self.workflow([self.report(), self.approved()])
        result = workflow._answer_qa(state)['result']
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['final_reviewed'])
        self.assertIn('支付障碍', result['stop_reason'])
        self.assertTrue(result['report']['warnings'])

    def test_reviewer_cannot_invent_result_reference_or_version(self):
        for text in ('结论[other:R1]', '结论[old-task:R1@obsolete]'):
            review = self.approved()
            review['approve_draft_unchanged'] = False
            review['text_edits'] = [{'original': self.report()['markdown'], 'replacement': text,
                                     'criterion': 'claim_strength'}]
            review['audit_findings'][2].update(verdict='correction_required', resolved_in_final=True, text_edit_indices=[0])
            workflow, _ = self.workflow([self.report(), review])
            with self.subTest(text=text):
                result = workflow._answer_qa(self.state())['result']
                self.assertEqual(result['status'], 'partial')
                self.assertFalse(result['final_reviewed'])
                self.assertIsNone(result['report'])

    def test_report_scope_permits_only_explicitly_materialized_original_results(self):
        bundle = self.bundle()
        altered = deepcopy(bundle['evidence'])
        altered[0]['task_id'] = 'different-original-task'
        with self.assertRaises(PermissionError):
            DataQaAgent._investigation_sources(altered, bundle['report_scope'])
        altered = deepcopy(bundle['evidence']); altered[0]['version'] = 'changed'
        with self.assertRaises(PermissionError):
            DataQaAgent._investigation_sources(altered, bundle['report_scope'])

    def test_catalog_distinguishes_nested_results_and_ambiguous_task_only_chart(self):
        contexts = {'recent_result': self.state()['recent_result_context']}
        catalog = DataQaAgent._source_catalog(contexts)
        self.assertEqual(set(catalog), {'old-task:R1', 'old-task:R2'})
        with self.assertRaisesRegex(ValueError, '多个结果'):
            DataQaAgent._validate_chart_source({'source_task_id': 'old-task'}, catalog)

    def test_latest_partial_investigation_blocks_fallback_even_without_rows(self):
        context = SessionContext(SimpleNamespace(), Settings(api_key='', session_archive_enabled=False,
                                                            short_term_summary_enabled=False))
        old = QueryResult(task_id='older', route='database_query', status='completed', message='ok',
                          rows=[{'value': 1}], columns=['value'])
        partial = QueryResult(task_id='latest', route='database_query', status='partial', message='partial',
                              workflow_mode='investigation_react_v1', datasource_id=SOURCE,
                              scenario=SNAPSHOT, stop_reason='查询失败')
        context.remember('session', old, '旧问题', {}, 'user-1')
        context.remember('session', partial, '新问题', {}, 'user-1')
        self.assertEqual(context.latest_table_result('session').task_id, 'latest')
        parent = json.loads(context.recent_result_context('session'))
        self.assertEqual(parent['status'], 'partial')
        result = load_analysis_evidence(self.state(recent_result_context=json.dumps(parent)), self.engine)
        self.assertTrue(result['managed'])
        self.assertEqual(result['evidence'], [])
        self.assertTrue(result['evidence_gaps'])

    def test_selected_partial_investigation_includes_all_r_metadata(self):
        context = SessionContext(SimpleNamespace(), Settings(api_key='', session_archive_enabled=False,
                                                            short_term_summary_enabled=False))
        result = QueryResult(task_id='old-task', route='database_query', status='partial', message='partial',
                             workflow_mode='investigation_react_v1', datasource_id=SOURCE,
                             scenario=SNAPSHOT, evidence=self.entries)
        context.remember('same-session', result, '原调查', {}, 'user-1')
        raw, _ = context.analysis_context('same-session', {'analysis_table_ids': ['old-task']})
        selected = json.loads(raw)[0]
        self.assertEqual(selected['status'], 'partial')
        self.assertEqual([item['id'] for item in selected['evidence']], ['old-task:R1', 'old-task:R2'])

    def test_router_selects_referenced_older_source_and_never_reads_unknown_task(self):
        workflow, _ = self.workflow([])
        workflow.config = Settings(session_archive_enabled=False)
        decision = PreparedRequest(action='data_qa', confidence=1, reason='引用前一份报告',
                                   analysis_task_ids=['old-task'])
        workflow.preprocessor = SimpleNamespace(prepare=lambda *args: decision)
        parent = json.loads(self.state()['recent_result_context'])
        state = self.state(recent_result_context=json.dumps({'task_id': 'unrelated-latest'}),
                           recent_result_candidates=json.dumps([parent]))
        update = workflow._preprocess(state)
        self.assertEqual([x['task_id'] for x in json.loads(update['analysis_context'])], ['old-task'])
        loaded = load_analysis_evidence({**state, **update}, self.engine)
        self.assertEqual(len(loaded['evidence']), 2)
        self.assertEqual(loaded['source_tasks'][0]['task_id'], 'old-task')
        decision.analysis_task_ids = ['another-users-task']
        update = workflow._preprocess(state)
        self.assertEqual(update['intent']['action'], 'direct_response')
        self.assertEqual(update['intent']['response_type'], 'clarification')
        self.assertNotIn('analysis_context', update)

    def test_explicit_reference_selection_cannot_be_replaced_by_router(self):
        workflow, _ = self.workflow([])
        workflow.config = Settings(session_archive_enabled=False)
        workflow.preprocessor = SimpleNamespace(prepare=lambda *args: PreparedRequest(
            action='data_qa', confidence=1, reason='使用历史', analysis_task_ids=['unrelated']))
        state = self.state(analysis_context=json.dumps([json.loads(self.state()['recent_result_context'])]))
        update = workflow._preprocess(state)
        self.assertNotIn('analysis_context', update)
        self.assertEqual(update['intent']['action'], 'data_qa')

    def test_router_sees_investigation_results_even_when_outer_table_is_empty(self):
        config = Settings(session_archive_enabled=False, short_term_summary_enabled=False)
        context = SessionContext(ScriptModel([]), config)
        result = QueryResult(task_id='old-task', route='database_query', status='partial', message='未审核',
                             workflow_mode='investigation_react_v1', datasource_id=SOURCE,
                             scenario=SNAPSHOT, evidence=self.entries, rows=[], columns=[])
        context.remember('same-session', result, '调查变化', {}, 'user-1')
        route = json.loads(context.route_context('same-session', {'analysis_table_ids': ['old-task']}))
        for header in (route['recent_turns'][0], route['selected_table_headers'][0]):
            self.assertEqual(header['row_count'], 0)
            self.assertEqual([e['result_id'] for e in header['available_evidence']], ['old-task:R1', 'old-task:R2'])
            self.assertEqual(header['available_evidence'][0]['total_rows'], 300)
            self.assertNotIn('rows', header['available_evidence'][0])
            self.assertNotIn('result_ref', header['available_evidence'][0])

    def test_legacy_source_is_resolved_from_saved_binding_not_default(self):
        state = self.state(datasource_id='')
        result = load_analysis_evidence(state, self.engine)
        self.assertEqual(result['report_scope']['datasource_id'], SOURCE)
        self.assertEqual(len(result['evidence']), 2)
        state['access_scope']['allowed_databases'] = []
        with self.assertRaises(PermissionError):
            load_analysis_evidence(state, self.engine)

    def test_unbound_or_mixed_legacy_evidence_is_rejected(self):
        for source in (None, 'other-source'):
            entries = deepcopy(self.entries)
            entries[1]['datasource_id'] = source
            with self.subTest(source=source), self.assertRaises(PermissionError):
                load_analysis_evidence(self.state(entries, datasource_id=''), self.engine)

    def test_answer_can_complete_without_upgrading_incomplete_original_task(self):
        state = self.state(query='刚才这张表的gmv列是什么意思？')
        parent = json.loads(state['recent_result_context'])
        parent.update(status='partial', stop_reason='另一项原因尚未查清')
        state['recent_result_context'] = json.dumps(parent)
        workflow, _ = self.workflow([
            {'action': 'answer', 'answer': '这是已取得的金额列[old-task:R1]', 'evidence_gaps': []},
            self.approved('这是已取得的金额列[old-task:R1]')])
        result = workflow._answer_qa(state)['result']
        self.assertEqual(result['status'], 'completed')
        trace = next(x for x in result['execution_log'] if x['stage'] == 'existing_evidence_reuse')
        self.assertEqual(trace['source_tasks'][0]['status'], 'partial')
        self.assertTrue(trace['warnings'])
        self.assertEqual(trace['new_sql_calls'], 0)
        self.assertEqual(json.loads(state['recent_result_context']), parent)

    def test_answer_missing_requested_evidence_stays_partial(self):
        workflow, _ = self.workflow([
            {'action': 'answer', 'answer': '已有金额[old-task:R1]，缺少新期间数据',
             'evidence_gaps': ['需要新期间数据']},
            self.approved('已有金额[old-task:R1]，缺少新期间数据')])
        result = workflow._answer_qa(self.state())['result']
        self.assertEqual(result['status'], 'partial')
        self.assertIn('需要新期间数据', result['stop_reason'])

    def test_analysis_and_review_each_receive_typed_provider_schema(self):
        from app.structured_output import current_output_schema
        schemas = []
        workflow, model = self.workflow([
            {'action': 'answer', 'answer': '金额[old-task:R1]'}, self.approved('金额[old-task:R1]')])
        original = model.chat_json
        def capture(system, payload):
            schemas.append(deepcopy(current_output_schema.get()))
            return original(system, payload)
        model.chat_json = capture
        workflow._answer_qa(self.state())
        self.assertEqual([s['name'] for s in schemas], ['existing_evidence_analysis', 'existing_evidence_review'])
        self.assertEqual(schemas[0]['schema']['properties']['action']['enum'], ['answer', 'report'])
        self.assertFalse(schemas[0]['schema']['additionalProperties'])
        self.assertEqual(schemas[1]['schema']['properties']['approved_text']['enum'], [''])
        self.assertEqual(schemas[1]['schema']['$defs']['AnalysisReviewFinding']['properties']['evidence_ids']['items']['enum'],
                         ['old-task:R1', 'old-task:R2'])
        self.assertIsNone(current_output_schema.get())

    def test_report_generation_and_review_share_current_evidence_not_historical_approval(self):
        self.entries[0]['interpretation_limits'] = ['金额不是因果效果']
        self.entries[0]['units'] = {'gmv': 'EUR cents'}
        self.entries[1]['statistical_population'] = 'paid order items'
        parent_text = '历史供给中断直接导致下降[old-task:R1]'
        state = self.state(analysis_mode='report', short_term_context=json.dumps({
            'history_summary': parent_text,
            'pending_turns': [{'turn_id': 't-old', 'user_message': '比较这两个期间', 'assistant_message': parent_text}]}))
        parent = json.loads(state['recent_result_context'])
        parent['analysis'] = parent_text
        state['recent_result_context'] = json.dumps(parent)
        before = deepcopy(state)
        review = self.approved()
        workflow, model = self.workflow([self.report(), review])
        result = workflow._answer_qa(state)['result']
        generation, audit = model.calls[0][1]['context'], model.calls[1][1]
        self.assertEqual(json.dumps(generation, ensure_ascii=False).count(parent_text), 1)
        self.assertNotIn('short_term', generation)
        self.assertEqual(generation['prior_user_messages'][0]['user_message'], '比较这两个期间')
        self.assertEqual(generation['prior_reports'][0]['material_role'], 'historical_claims_to_recheck')
        self.assertEqual(generation['trust_boundary'], audit['trust_boundary'])
        self.assertEqual(generation['source_tasks'], audit['source_tasks'])
        self.assertNotIn('final_reviewed', generation['source_tasks'][0])
        self.assertTrue(generation['source_tasks'][0]['historical_delivery_status']['final_reviewed'])
        self.assertEqual(generation['evidence'][0]['interpretation_limits'], ['金额不是因果效果'])
        self.assertEqual(audit['evidence'][0]['units'], {'gmv': 'EUR cents'})
        self.assertEqual(audit['evidence'][1]['statistical_population'], 'paid order items')
        self.assertNotIn('markdown', audit['report_metadata'])
        self.assertEqual(json.dumps(audit, ensure_ascii=False).count(self.report()['markdown']), 1)
        self.assertTrue(result['final_reviewed'])
        saved = result['reviews'][0]
        self.assertEqual(saved['review'], review)
        self.assertEqual(saved['context']['draft'], self.report()['markdown'])
        self.assertEqual(saved['evidence_metadata'][0]['version'], 'version-R1')
        self.assertNotIn('rows', saved['evidence_metadata'][0])
        self.assertEqual(len(saved['input_sha256']), 64)
        self.assertTrue(saved['accepted'])
        self.assertEqual(state, before)
        self.assertEqual(len(model.calls), 2)

    def test_parent_approval_does_not_approve_new_unsubstantiated_review(self):
        raw_review = {'valid': True, 'approve_draft_unchanged': True, 'approved_text': '', 'reason': '旧稿审核过'}
        workflow, _ = self.workflow([self.report(), raw_review])
        result = workflow._answer_qa(self.state(analysis_mode='report'))['result']
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['final_reviewed'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['reviews'][0]['review'], raw_review)
        self.assertIn('历史审核状态不能替代', result['reviews'][0]['validation_errors'][0])
        self.assertFalse(result['reviews'][0]['accepted'])

    def test_qa_precise_edits_are_applied_and_reviewed_without_extra_calls(self):
        draft = '金额为123欧分。供给直接导致下降[old-task:R1]'
        review = self.approved(draft)
        review.update(approve_draft_unchanged=False, text_edits=[{
            'original': '供给直接导致下降', 'replacement': '供给与下降同时出现，尚未核实因果', 'criterion': 'claim_strength'}])
        review['audit_findings'][2].update(verdict='correction_required', resolved_in_final=True, text_edit_indices=[0])
        workflow, model = self.workflow([self.report(markdown=draft, visualizations=[]), review])
        result = workflow._answer_qa(self.state(analysis_mode='report'))['result']
        self.assertTrue(result['final_reviewed'])
        self.assertEqual(result['report']['markdown'], draft.replace('供给直接导致下降', '供给与下降同时出现，尚未核实因果'))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['reviews'][0]['review'], review)

    def test_review_missing_evidence_remains_current_gap_not_source_incapability(self):
        review = self.approved()
        review['audit_findings'][2].update(verdict='missing_evidence', finding='本轮未取得客户属性，不能解释客户财富')
        workflow, model = self.workflow([self.report(), review])
        result = workflow._answer_qa(self.state(analysis_mode='report'))['result']
        self.assertFalse(result['final_reviewed'])
        self.assertIn('本轮未取得客户属性', result['stop_reason'])
        self.assertNotIn('源数据不存在', result['stop_reason'])
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(result['reviews'][0]['review']['valid'])
        self.assertFalse(result['reviews'][0]['accepted'])

    def test_bounded_supplement_can_pass_current_review_without_resolving_parent_cause(self):
        state = self.state(query='在刚才报告上补一段正常指标和不确定的地方', analysis_mode='report')
        parent = json.loads(state['recent_result_context'])
        parent.update(status='partial', stop_reason='因果尚未查明，需要独立证据',
                      requirements=[{'id': 'Q-cause', 'question': '为何下滑', 'status': 'open'}])
        state['recent_result_context'] = json.dumps(parent)
        before = deepcopy(state)
        draft = ('正常项：品类B的贡献为10，存在正向抵消项[old-task:R2]。'
                 '不确定性：原调查的因果尚未查明，本轮未取得对应证据，不主张因果，不能据此断言源数据不存在。')
        review = self.approved(draft)
        findings = [
            '只陈述已保存的品类贡献，未扩大到整体经营或供应链状态',
            'R2中品类B的change为10，直接引用保存值，未重新计算',
            '正向贡献有证据；当前正文明确未取得因果证据，没有作因果断言',
            '只说明本轮未取证，没有据此宣称源数据不存在',
            '已补正向指标和不确定性，当前问题不要求补齐原调查的全部原因',
        ]
        for item, finding in zip(review['audit_findings'], findings):
            item.update(finding=finding, context_refs=['current_question', 'evidence_gaps', 'draft'])
        workflow, model = self.workflow([self.report(markdown=draft, visualizations=[]), review])
        result = workflow._answer_qa(state)['result']
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['final_reviewed'])
        self.assertEqual(result['report']['markdown'], draft)
        self.assertIn('因果尚未查明', result['stop_reason'])
        self.assertTrue(result['report']['warnings'])
        self.assertTrue(result['reviews'][0]['accepted'])
        self.assertEqual(result['reviews'][0]['validation_errors'], [])
        self.assertIn('准确承认未取证本身可以通过', model.calls[1][0])
        self.assertEqual(state, before)
        self.assertEqual(len(model.calls), 2)

        # The same inherited gap cannot excuse an unsupported claim newly
        # retained in the output. This must still fail the actual QA resolver.
        overclaim = '这些结果证明供给造成下滑[old-task:R2]。'
        rejected_review = deepcopy(review)
        rejected_review['audit_findings'][2].update(verdict='missing_evidence',
            finding='当前正文断言供给造成下滑，但本轮没有支持该因果的证据')
        workflow, model = self.workflow([self.report(markdown=overclaim, visualizations=[]), rejected_review])
        rejected = workflow._answer_qa(state)['result']
        self.assertEqual(rejected['status'], 'partial')
        self.assertFalse(rejected['final_reviewed'])
        self.assertIsNone(rejected['report'])
        self.assertFalse(rejected['reviews'][0]['accepted'])
        self.assertIn('当前正文断言供给造成下滑', rejected['stop_reason'])
        self.assertEqual(len(model.calls), 2)


if __name__ == '__main__':
    unittest.main()


class FocusedQaContextTest(__import__('unittest').TestCase):
    def test_numeric_question_excludes_prior_narrative_without_losing_evidence_or_gaps(self):
        from app.querying.analysis_evidence import focus_qa_context
        original = {'evidence': [{'id': 'R1'}], 'prior_reports': ['unverified cause'],
                    'short_term': 'old cause', 'business_methods': 'full investigation',
                    'source_gaps': ['unfinished original task'], 'data_gaps': [],
                    'scenario': {'snapshot': 'S'}}
        focused = focus_qa_context(original, 'fact')
        self.assertEqual(focused['evidence'], original['evidence'])
        self.assertEqual(focused['source_gaps'], original['source_gaps'])
        self.assertNotIn('prior_reports', focused)
        self.assertNotIn('short_term', focused)
        self.assertNotIn('business_methods', focused)
        self.assertIn('prior_reports', original)
        report = focus_qa_context(original, 'report')
        self.assertEqual(report['prior_reports'][0]['markdown'], original['prior_reports'][0])
        self.assertEqual(report['prior_reports'][0]['material_role'], 'historical_claims_to_recheck')


class AnalysisReviewTrustTest(unittest.TestCase):
    def setUp(self):
        from app.querying.analysis_output import resolve_analysis_review
        self.resolve = lambda review, draft='甲断言。乙断言。': resolve_analysis_review(review, draft, {'old-task:R1'})

    @staticmethod
    def approved():
        return MultiturnEvidenceTest.approved('结果[old-task:R1]')

    @staticmethod
    def correction(review, indices, *, finding_id='other-claim', criterion='claim_strength', resolved=True):
        item = deepcopy(review['audit_findings'][2])
        item.update(finding_id=finding_id, criterion=criterion, verdict='correction_required',
                    resolved_in_final=resolved, text_edit_indices=indices)
        return item

    def test_historical_review_parses_but_is_not_current_audit(self):
        from app.querying.analysis_output import AnalysisReview, resolve_review_text
        old = {'valid': True, 'approved_text': '旧正文', 'reason': '旧版审核'}
        self.assertEqual(AnalysisReview.model_validate(old).approved_text, '旧正文')
        self.assertEqual(resolve_review_text(old, '旧草稿'), '旧正文')
        with self.assertRaisesRegex(ValueError, '不能自由重写'):
            self.resolve(old)

    def test_live_schema_binds_evidence_and_does_not_change_main_findings(self):
        from jsonschema import Draft202012Validator
        from app.querying.analysis_output import analysis_schema
        from app.workflows.investigation_output import output_schema
        schema = analysis_schema(review=True, evidence_ids=['old-task:R1'])
        validator = Draft202012Validator(schema)
        validator.validate(self.approved())
        old = {'valid': True, 'approved_text': '', 'reason': '空审核'}
        self.assertTrue(list(validator.iter_errors(old)))
        changed = self.approved()
        changed['audit_findings'][0]['evidence_ids'] = ['other:R1']
        self.assertTrue(list(validator.iter_errors(changed)))
        finding = output_schema('delivery_review')['$defs']['ReviewFinding']
        self.assertNotIn('finding_id', finding['properties'])
        self.assertNotIn('text_edit_indices', finding['properties'])

    def test_one_patch_cannot_resolve_multiple_problems_with_same_criterion(self):
        review = self.approved()
        review.update(approve_draft_unchanged=False, text_edits=[{
            'original': '甲断言', 'replacement': '甲尚未核实', 'criterion': 'claim_strength'}])
        review['audit_findings'][2] = self.correction(review, [0], finding_id='claim-a')
        review['audit_findings'].append(self.correction(review, [0], finding_id='claim-b'))
        with self.assertRaisesRegex(ValueError, '多个问题共用'):
            self.resolve(review)
        review['audit_findings'][-1].update(text_edit_indices=[], resolved_in_final=False)
        with self.assertRaisesRegex(ValueError, 'claim-b/claim_strength'):
            self.resolve(review)

    def test_separate_problems_need_separate_applied_patches(self):
        review = self.approved()
        review.update(approve_draft_unchanged=False, text_edits=[
            {'original': '甲断言', 'replacement': '甲尚未核实', 'criterion': 'claim_strength'},
            {'original': '乙断言', 'replacement': '乙尚未核实', 'criterion': 'claim_strength'}])
        review['audit_findings'][2] = self.correction(review, [0], finding_id='claim-a')
        review['audit_findings'].append(self.correction(review, [1], finding_id='claim-b'))
        self.assertEqual(self.resolve(review), '甲尚未核实。乙尚未核实。')
        review['text_edits'][1]['original'] = '没有出现的断言'
        with self.assertRaises(ValueError):
            self.resolve(review)

    def test_method_error_or_missing_evidence_cannot_be_resolved_by_wording(self):
        review = self.approved()
        review.update(approve_draft_unchanged=False, text_edits=[{
            'original': '甲断言', 'replacement': '甲已修正', 'criterion': 'calculation_method'}])
        review['audit_findings'][1] = self.correction(review, [0], criterion='calculation_method')
        with self.assertRaisesRegex(ValueError, '未解决的事实、方法或证据'):
            self.resolve(review)
        review = self.approved()
        review['audit_findings'][2].update(verdict='missing_evidence', resolved_in_final=True)
        with self.assertRaisesRegex(ValueError, '补丁补齐证据'):
            self.resolve(review)

    def test_missing_criterion_unknown_refs_unbound_or_false_boolean_approval_are_rejected(self):
        missing = self.approved()
        missing['audit_findings'].pop()
        foreign = self.approved()
        foreign['audit_findings'][0]['evidence_ids'] = ['other:R1']
        wrong_context = self.approved()
        wrong_context['audit_findings'][0]['context_refs'] = ['parent_approved']
        no_basis = self.approved()
        no_basis['audit_findings'][0].update(evidence_ids=[], context_refs=[])
        orphan = self.approved()
        orphan.update(approve_draft_unchanged=False, text_edits=[{
            'original': '甲断言', 'replacement': '甲尚未核实', 'criterion': 'claim_strength'}])
        false_boolean = self.approved()
        false_boolean['valid'] = 'false'
        for case in [missing, foreign, wrong_context, no_basis, orphan, false_boolean]:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.resolve(case)

    def test_numeric_and_unit_guards_apply_to_qa_patches_too(self):
        for replacement in ['金额700000欧分', '金额7000欧元']:
            review = self.approved()
            review.update(approve_draft_unchanged=False, text_edits=[{
                'original': '金额7000欧分', 'replacement': replacement, 'criterion': 'claim_strength'}])
            review['audit_findings'][2] = self.correction(review, [0])
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, 'repair'):
                self.resolve(review, '金额7000欧分。')
