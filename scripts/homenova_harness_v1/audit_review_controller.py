"""Offline diagnosis of archived reviews and current Controller branches.

Developer evaluation only. No model, retrieval, MCP, SQL or service calls.
Controlled review objects test routing, never business-answer correctness.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'data/homenova_harness_v1/private'
sys.path.insert(0, str(ROOT / 'references/askdata_studio/backend'))

from app.runtime_budget import RunBudget, current_budget
from app.workflows.harness import InvestigationHarness, Review, TaskRecord


def outcome(task, budget):
    return {
        'next_node': task.next_node,
        'final_status': task.final_status,
        'final_reviewed': task.final_reviewed,
        'stop_reason': task.stop_reason,
        'steps': [{'id': s['id'], 'status': s['status']} for s in task.steps],
        'repairs': dict(budget.repairs),
    }


def task_from(response):
    task = TaskRecord({'task_id': response['task_id']}, list(response['required_questions']))
    task.steps = copy.deepcopy(response['plan'])
    task.evidence = copy.deepcopy(response['evidence'])
    return task


def budget_from(response):
    b = response['budget']
    return RunBudget(response['task_id'], spent=b['active_seconds'], tools=b['tools'],
                     repairs=dict(b['repairs']), replans=b['replans'])


def control_review(task, action, answered=None, unanswered=None, affected=None):
    return Review.model_validate({
        'action': action, 'answered': answered or [], 'unanswered': unanswered or [],
        'issue_type': 'evidence_gap' if unanswered else 'none',
        'affected_steps': affected or [], 'reason': '受控分支检查，不代表业务审核结论',
        'approved_text': '仅用于本地状态分支验证，不构成经营结论。' if action in {'complete', 'partial'} else '',
    })


def inject_review(harness, task, review):
    def call(record, final):
        record.reviews.append({'final': final, **review.model_dump()})
        return review
    harness._review_call = call


def under_budget(budget, callback):
    token = current_budget.set(budget)
    try:
        callback()
    finally:
        current_budget.reset(token)


def main():
    output = PRIVATE / 'review_controller_audit_20261028'
    if output.exists():
        raise RuntimeError('Choose a fresh evaluation directory; do not overwrite an audit')
    paths = [PRIVATE / f'ui_gmv_round{n}.json' for n in range(1, 7)]
    original_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    responses = [json.loads(p.read_text())['response'] for p in paths]
    inventory = []
    for n, response in enumerate(responses, 1):
        reviews = response.get('reviews') or []
        events = response.get('execution_log') or []
        inventory.append({
            'round': n, 'task_id': response['task_id'], 'status': response['status'],
            'stop_reason': response.get('stop_reason'),
            'process_reviews': sum(not r.get('final') for r in reviews),
            'delivery_reviews': sum(bool(r.get('final')) for r in reviews),
            'review_seconds': round(sum(e.get('elapsed_seconds', 0) for e in events
                                        if e.get('stage') in {'result_review', 'delivery_review'}), 3),
            'same_last_two_answered_objects': len(reviews) > 1 and reviews[-1].get('answered') == reviews[-2].get('answered'),
            'investigation_seconds_remaining': round(max(0, response['budget']['investigation_seconds'] - response['budget']['active_seconds']), 3),
            'replans': response['budget']['replans'],
        })

    harness = object.__new__(InvestigationHarness)
    findings = []
    # Network access is forbidden even if a future branch accidentally tries it.
    with patch.object(socket, 'socket', side_effect=AssertionError('Offline audit forbids network access')):
        response = responses[2]
        task = task_from(response)
        task.pending_review = copy.deepcopy(response['reviews'][1])
        budget = budget_from(response)
        under_budget(budget, lambda: harness._controller({'record': task}))
        findings.append({'case': 'historical_round3_gap_current_fallback',
                         'expectation': 'Current source repairs S3; this fallback was already added after the archived failure.',
                         'observed': outcome(task, budget),
                         'check': task.next_node == 'execute' and task.steps[2]['status'] == 'pending'})

        task = task_from(response)
        budget = budget_from(response)
        answered = [{'question': q, 'evidence_ids': [task.evidence[0]['id']],
                     'strength': 'fact', 'explanation': '仅检查控制流，不评价业务证据是否充分'} for q in task.required]
        task.pending_review = control_review(task, 'continue', answered=answered).model_dump()
        under_budget(budget, lambda: harness._controller({'record': task}))
        before = outcome(task, budget)
        inject_review(harness, task, control_review(task, 'complete', answered=answered))
        under_budget(budget, lambda: harness._delivery_review({'record': task}))
        findings.append({'case': 'continue_without_open_gap_creates_sticky_stop', 'before_delivery': before,
                         'observed': outcome(task, budget),
                         'check': bool(before['stop_reason']) and task.final_reviewed and task.final_status == 'partial'})

        for action in ['complete', 'partial']:
            task = task_from(response)
            task.required = ['已回答项', '仍需查询项']
            task.steps[2].update(status='pending', result_ids=[])
            answered = [{'question': '已回答项', 'evidence_ids': [task.evidence[0]['id']],
                         'strength': 'fact', 'explanation': '仅为分支输入'}]
            review = control_review(task, action, answered=answered, unanswered=['仍需查询项'])
            inject_review(harness, task, review)
            budget = budget_from(response)
            under_budget(budget, lambda: harness._delivery_review({'record': task}))
            findings.append({'case': f'delivery_{action}_ends_with_runnable_required_step',
                             'observed': outcome(task, budget),
                             'check': task.next_node == 'end' and task.final_status == 'partial' and task.steps[2]['status'] == 'pending'})

        task = task_from(response)
        task.required = ['已回答项', '尚未补查项']
        task.steps[2].update(status='completed')
        inject_review(harness, task, control_review(task, 'partial', unanswered=['尚未补查项']))
        budget = budget_from(response)
        under_budget(budget, lambda: harness._delivery_review({'record': task}))
        findings.append({'case': 'partial_accepted_without_exhaustion_or_verified_capability_limit',
                         'observed': outcome(task, budget),
                         'check': task.next_node == 'end' and budget.remaining() > 0 and budget.replans == 0})

        task = task_from(response)
        task.stop_reason = '必要步骤修复额度耗尽，无法交付完整结论'
        task.steps[0]['status'] = 'failed'
        task.steps[1]['depends_on'] = []  # independent branch remains repairable
        inject_review(harness, task, control_review(task, 'repair', affected=['S2']))
        budget = budget_from(response)
        budget.repairs = {'S1': budget.repair_limit}
        under_budget(budget, lambda: harness._delivery_review({'record': task}))
        findings.append({'case': 'global_stop_blocks_repair_of_independent_unexhausted_step',
                         'observed': outcome(task, budget),
                         'check': task.next_node == 'end' and budget.repairs.get('S2', 0) == 0})

    unchanged = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == value for p, value in original_hashes.items())
    result = {'date': '2026-10-28', 'scope': 'Archived real records plus controlled current-code routing checks; not business acceptance',
              'network_or_model_calls': False, 'business_data_queries': 0,
              'source_records_unchanged': unchanged, 'source_sha256': original_hashes,
              'harness_source_sha256': hashlib.sha256((ROOT / 'references/askdata_studio/backend/app/workflows/harness.py').read_bytes()).hexdigest(),
              'archived_runs': inventory, 'current_code_probes': findings,
              'all_expected_diagnostic_behaviors_reproduced': all(f['check'] for f in findings)}
    assert unchanged and result['all_expected_diagnostic_behaviors_reproduced'], result
    output.mkdir()
    (output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(output / 'audit.json'), 'probes': len(findings),
                      'all_expected_diagnostic_behaviors_reproduced': True,
                      'network_or_model_calls': False, 'source_records_unchanged': unchanged}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
