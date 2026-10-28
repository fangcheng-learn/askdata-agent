"""Save offline protocol replay evidence, never live Agent acceptance.

The test model returns fixed protocol messages. LangGraph pause/resume, code
validation, in-process MCP and local CSV SQL execute normally. No HTTP/model
service, embedding or reranking requests are allowed. Existing records are kept.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'references/askdata_studio/backend'
OUTPUT = ROOT / 'data/homenova_harness_v1/private/contract_validation_v6/offline_flow.json'
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'tests'))


def main():
    if OUTPUT.exists():
        raise RuntimeError('Preserve offline replay history; output already exists: ' + str(OUTPUT))

    denied_operations = []

    def deny_network(event, _args):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen'}:
            denied_operations.append(event)
            raise RuntimeError('Offline replay forbids network and external processes')

    sys.addaudithook(deny_network)
    import test_metric_source_clarification_v6 as replay

    protected = [BACKEND / 'data/databases/ecommerce_ops' / name for name in (
        'daily_store_metrics.csv', 'daily_product_metrics.csv', '_schema.json', '_database_manifest.json')]
    protected += [ROOT / 'data/homenova_harness_v1/private' / name / 'paid_runs.json'
                  for name in ('implementation_v2', 'validation_v3_real')]
    protected = [path for path in protected if path.exists()]

    def hashes():
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in protected}

    before = hashes()
    records = []
    real_workflow_class = replay.QueryWorkflow
    for legacy in (False, True):
        instances = []

        def capture_workflow(*args, **kwargs):
            # Capture the genuine workflow; no production node or MCP is replaced.
            workflow = real_workflow_class(*args, **kwargs)
            instances.append(workflow)
            return workflow

        started = time.monotonic()
        with patch.object(replay, 'QueryWorkflow', side_effect=capture_workflow):
            replay.RealGraphSourceResumeTest().run_resume(legacy=legacy)
        duration = time.monotonic() - started
        assert len(instances) == 1
        workflow = instances[0]
        task_id = 'offline-legacy-source' if legacy else 'offline-structured-source'
        state = workflow.graph.get_state(workflow.run_config(task_id)).values
        result = state['result']
        budget = workflow.budgets[task_id].public()
        events = budget['events']
        coder_inputs = [{key: item.get(key) for key in (
            'query', 'original_question', 'confirmed_parameters', 'confirmed_metric_sources',
            'clarification_history', 'confirmed_fields')}
            for item in workflow.model_client.coder_payloads]
        assert result['status'] == 'completed' and result['final_reviewed']
        assert len(result['tool_calls']) == 1 and result['tool_calls'][0]['success']
        assert len(coder_inputs) == 2
        records.append({
            'case': 'legacy_source_option' if legacy else 'structured_metric_source_option',
            'task_id': task_id, 'task_question': replay.QUESTION,
            'user_reply': '店铺维度GMV', 'resume_option_id': 'store',
            'datasource_id': replay.DATABASE, 'duration_seconds': round(duration, 3),
            'assertions_passed': [
                'same task and same budget resumed', 'preprocessing executed once',
                'two coder protocol returns: clarification then SQL; no repair retry',
                'original question unchanged', 'waiting paused active-time budget',
                'real local SQL total matched independent Decimal CSV summation',
            ],
            'result': result,
            'confirmed_metric_sources': state['workspace'].get('confirmed_metric_sources'),
            'clarification_history': state['workspace'].get('clarification_history') or [],
            'confirmed_schema_tables': state['workspace'].get('confirmed_schema_tables') or [],
            'confirmed_schema_fields': state['workspace'].get('schema_fields') or [],
            'coder_inputs': coder_inputs,
            'controlled_coder_returns': len(coder_inputs),
            'local_sql_executions': len(result['tool_calls']),
            'tool_events': [item for item in events if item['stage'] == 'tool_dispatch'],
            'skill_events': [item for item in events if item['stage'] == 'skill_injection'],
            'budget': budget,
            'review_note': 'final_reviewed comes from a fixed offline reviewer response; not live-model validation',
        })
    after = hashes()
    assert before == after, 'Protected data or paid-run ledgers changed'
    assert not denied_operations, 'An offline replay attempted a prohibited operation'
    payload = {
        'recorded_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'mode': 'controlled protocol outputs with real LangGraph, local in-process MCP and CSV SQL',
        'live_agent_acceptance': False,
        'runtime_knowledge': False,
        'live_model_calls': 0, 'paid_calls': 0, 'http_calls': 0,
        'embedding_calls': 0, 'rerank_calls': 0,
        'network_and_external_processes_blocked': True,
        'protected_files_unchanged': True, 'protected_file_sha256': after,
        'limitations': [
            'Model outputs are fixed test messages, not real model inference.',
            'Preprocessing and retrieval are deterministic test nodes; no live retrieval quality claim.',
            'SQL total is checked against public demo CSV; this is not a HomeNova business acceptance run.',
            'Budget model_calls does not meter the fixed test model; controlled coder returns are listed separately.',
        ],
        'flows': records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write('\n')
    print(json.dumps({
        'output': str(OUTPUT), 'cases': len(records), 'live_model_calls': 0,
        'local_sql_executions': sum(record['local_sql_executions'] for record in records),
        'results': [{'case': record['case'], 'status': record['result']['status'],
                     'duration_seconds': record['duration_seconds'], 'rows': record['result']['rows'],
                     'skill_events': len(record['skill_events'])} for record in records],
        'protected_files_unchanged': True,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
