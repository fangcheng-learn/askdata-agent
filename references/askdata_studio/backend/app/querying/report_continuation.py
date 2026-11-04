"""Carry user-authored feedback with the selected report, never as SQL facts.

The lineage is saved on the new report. Older reports and investigation budgets
remain immutable; follow-ups don't rely on a lossy conversation summary.
"""
from copy import deepcopy


def continuation_context(query: str, contexts: dict, task_id: str) -> dict:
    questions, messages = [], []
    for report in contexts.get('prior_reports') or []:
        previous = report.get('continuation_context') or {}
        questions.extend(previous.get('original_questions') or [])
        messages.extend(previous.get('user_messages') or [])
    if not questions:
        for task in contexts.get('source_tasks') or []:
            original = task.get('historical_delivery_status') or task
            if original.get('query'):
                questions.append(original['query'])
    # User messages only; don't re-import assistant summaries or old judgments
    # as facts. Newer feedback stays last so explicit corrections can supersede
    # earlier statements in the Agent's interpretation.
    messages.extend(contexts.get('prior_user_messages') or [])
    current_identity = (task_id, query.strip())
    seen, unique = set(), []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get('user_message'), str):
            continue
        text = message['user_message'].strip()
        identity = (str(message.get('turn_id') or ''), text)
        if not text or identity in seen or identity == current_identity:
            continue
        seen.add(identity)
        unique.append({'turn_id': identity[0], 'user_message': text, 'source': 'user'})
    unique.append({'turn_id': task_id, 'user_message': query.strip(), 'source': 'user'})
    return {'original_questions': list(dict.fromkeys(q for q in questions if isinstance(q, str) and q)),
            'user_messages': deepcopy(unique)}
