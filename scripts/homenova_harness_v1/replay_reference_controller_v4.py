"""Replay saved outputs and local data; no new model/Agent/HTTP requests.

This is development evidence, never runtime knowledge. The model adapter returns
only recorded output; its calls are controlled replay, not live inference. The
tool adapter runs the saved S2 SQL once against local public CSV through DuckDB.
"""
from __future__ import annotations

import copy
import csv
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / 'data/homenova_harness_v1/private/reference_controller_v4'
SOURCE = ROOT / 'data/homenova_harness_v1/private/validation_v3_real/case_1.json'
sys.path.insert(0, str(ROOT / 'references/askdata_studio/backend'))


def deny_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen'}:
        raise RuntimeError('Offline replay forbids network and external processes')


sys.addaudithook(deny_network)

from app.homenova import schema_payload, registered_metric
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.single_database_agent import SingleDatabaseAgent
from app.runtime_budget import RunBudget, current_budget
from app.security import AccessController
from app.skills import SkillRegistry
from app.task_decisions import validate_step_decision
from app.workflows.harness import InvestigationHarness, TaskRecord
from types import SimpleNamespace


class RecordedModel:
    def __init__(self, outputs):
        self.outputs = copy.deepcopy(outputs)
        self.calls = []

    def chat_json(self, system, user):
        assert '"result_ref"' not in user, 'Internal reference was exposed to the model'
        self.calls.append({'controlled_output': True, 'request_characters': len(system) + len(user)})
        if not self.outputs:
            raise RuntimeError('Unexpected extra model call in recorded replay')
        return self.outputs.pop(0)


def main():
    output = DIRECTORY / 'replay.json'
    if output.exists():
        raise RuntimeError('Preserve replay history; output already exists')
    protected = json.loads((DIRECTORY / 'before_sha256.json').read_text())
    original_files = {name: sha for name, sha in protected.items() if '/private/' in name}
    saved = json.loads(SOURCE.read_text())
    response = saved['response']
    evidence = copy.deepcopy(response['evidence'][0])
    database = response['datasource_id']
    scope = AccessController().scoped('demo_admin', database)
    # The old response predates user_id in evidence. Bind a controlled owner for
    # replay and state this explicitly; do not invent historical owner evidence.
    evidence['user_id'] = scope.user_id
    step = response['plan'][1]
    context = dict(step_protocol=True, step_id=step['id'], task_id=response['task_id'], user_id=scope.user_id,
                   datasource_id=database, snapshot=response['scenario'], task_question=saved['request']['query'],
                   conditions=evidence['conditions'], expected_output=step['expected_output'], required_output_fields=[],
                   dependency_results=[evidence], requirements={'Q1': saved['request']['query']},
                   step_conditions=step['entry_conditions'], plan_steps=response['plan'])
    attempts = [attempt for event in response['execution_log'] if event.get('step_id') == 'S2'
                for attempt in (event.get('validation_attempts') or event.get('context', {}).get('validation_attempts') or [])]
    references = []
    for attempt in attempts:
        changes = []
        decision = validate_step_decision(attempt['step_decision'], context, normalizations=changes)
        assert decision.evidence_ids == [evidence['id']]
        references.append(dict(original=attempt['step_decision'], normalized=decision.model_dump(), changes=changes))

    first = attempts[0]
    model_output = dict(action=first['action'], tool_name=first['tool_name'], arguments={'sql': first['sql']},
                        query_contract=first['query_contract'], step_decision=first['step_decision'])
    model = RecordedModel([model_output])
    engine = DuckDbEngine()
    executions = []

    def local_tool(name, arguments):
        assert name == 'query_' + database and not executions
        result = asdict(engine.execute(database, arguments['sql'], scope))
        executions.append(result)
        return result

    client = SimpleNamespace(list_tools=lambda: [dict(name='query_' + database, input_schema={})], call_tool=local_tool)
    skills = SkillRegistry()
    executor = SingleDatabaseAgent(model, lambda _: client, skills.get('database_query'), engine)
    metrics = [registered_metric(database, name) for name in step['metric_ids']]
    budget = RunBudget(response['task_id']); budget.start(); token = current_budget.set(budget)
    try:
        result = executor.prepare(step['goal'], database, schema_payload(database), '', {},
                                  dict(selected_semantics=metrics, scenario=response['scenario']), scope.public(),
                                  original_query=saved['request']['query'], task_context=context)
    finally:
        budget.pause(); current_budget.reset(token)
    assert result['action'] == 'executed' and result['execution']['success']
    assert len(model.calls) == len(executions) == 1
    assert result['step_decision']['evidence_ids'] == [evidence['id']]
    assert all(item['success'] for item in result['validation_attempts'])

    # Independent standard-library SKU arithmetic, never sent to the executor.
    public = ROOT / 'data/homenova_phase_d/public/asof_2026_08_31'
    items = defaultdict(list)
    with (public / 'order_items.csv').open(newline='') as handle:
        for row in csv.DictReader(handle):
            items[row['order_id']].append(row)
    monthly = defaultdict(lambda: {'gmv': 0, 'qty': 0, 'orders': set()})
    with (public / 'payments.csv').open(newline='') as handle:
        for row in csv.DictReader(handle):
            if row['status'] != 'captured':
                continue
            month = datetime.fromisoformat(row['captured_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Berlin')).strftime('%Y-%m')
            for item in items[row['order_id']]:
                target = monthly[(month, item['sku_id'])]
                target['gmv'] += int(item['line_paid_gross_cents'])
                target['qty'] += int(item['quantity'])
                target['orders'].add(row['order_id'])
    checks = []
    for row in executions[0]['rows']:
        june, july = monthly[('2026-06', row['sku_id'])], monthly[('2026-07', row['sku_id'])]
        expected = dict(gmv_diff=july['gmv'] - june['gmv'], jul_gmv=july['gmv'], jun_gmv=june['gmv'],
                        jul_qty=july['qty'], jun_qty=june['qty'], jul_orders=len(july['orders']), jun_orders=len(june['orders']))
        checks.append(dict(sku_id=row['sku_id'], expected=expected, matched=all(row[key] == value for key, value in expected.items())))
    assert checks and all(item['matched'] for item in checks)

    # Reproduce the point before the old second review's invalid repair loop.
    closing_model = RecordedModel([response['reviews'][1]])
    workflow = SimpleNamespace(model_client=closing_model, skills=skills)
    harness = InvestigationHarness(workflow)
    state = dict(task_id=response['task_id'], query=saved['request']['query'], intent={'action': 'database_query'},
                 datasource_id=database, scenario=response['scenario'], access_scope=scope.public())
    task = TaskRecord(state, response['required_questions'], steps=copy.deepcopy(response['plan']), evidence=[evidence],
                      skills=response['selected_skills'], conditions=evidence['conditions'])
    task.draft = '受控重放使用已保存的修正后审核文稿作为输入；只证明代码收尾，不代表新模型生成。'
    budget = RunBudget(response['task_id'], repairs={'S2': 2, 'closing': 1})
    budget.start(); token = current_budget.set(budget)
    try:
        harness._delivery_review({'record': task})
    finally:
        budget.pause(); current_budget.reset(token)
    assert task.next_node == 'end' and task.final_status == 'partial' and task.final_reviewed
    assert len(closing_model.calls) == 1 and budget.repairs == {'S2': 2, 'closing': 1}
    unchanged = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha for name, sha in original_files.items())
    assert unchanged
    record = dict(source=str(SOURCE.relative_to(ROOT)), mode='offline recorded-output replay, not live Agent acceptance',
                  live_model_calls=0, paid_calls=0, http_calls=0, embedding_calls=0, rerank_calls=0,
                  ownership_metadata_note='Historical evidence lacked user_id; controlled replay owner explicitly bound; no historical user claim',
                  original_outputs_and_ledgers_unchanged=unchanged, reference_replays=references,
                  saved_s2_single_step=dict(controlled_model_returns=len(model.calls), local_sql_executions=len(executions),
                                            result=result, independent_sku_checks=checks),
                  closing_replay=dict(controlled_model_returns=len(closing_model.calls), final_status=task.final_status,
                                      final_reviewed=task.final_reviewed, repairs=budget.repairs, next_node=task.next_node),
                  limits=['S3 has no saved execution and is not fabricated', 'No claim of full live business acceptance',
                          'Saved S2 is a Top 5 subset; not a complete SKU contribution total or causal proof'])
    with output.open('x') as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, default=str); handle.write('\n')
    print(json.dumps(dict(output=str(output), reference_outputs_normalized=len(references),
                          s2_sql_executions=1, independent_sku_rows_matched=len(checks),
                          closing_status=task.final_status, closing_reviewer_returns=1, new_paid_calls=0), ensure_ascii=False))


if __name__ == '__main__':
    main()
