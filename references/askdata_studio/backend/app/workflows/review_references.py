"""Separate reviewable context from actual query evidence without inventing IDs."""
from copy import deepcopy

CONTEXT_KEYS = frozenset({'task', 'data_capabilities', 'semantics', 'report', 'draft',
    'draft_evidence_gaps', 'core_questions_to_assess', 'explanations_to_assess', 'delivery_constraints'})


def context_directory(payload):
    return {f'context:{key}': {'kind': 'context', 'section': key, 'numeric_evidence': False}
            for key in sorted(CONTEXT_KEYS) if key in payload}


def normalize_finding_references(review, directory, result_ids):
    """Move only exact references to supplied context sections, never fuzzy IDs.

    Unknown references stay invalid for the controller. Context is usable for
    reviewing scope/capability/draft, but never becomes numerical query evidence.
    """
    review = deepcopy(review)
    changes = []
    for finding in review.get('audit_findings') or []:
        if not isinstance(finding, dict) or not isinstance(finding.get('evidence_ids'), list):
            continue
        context_refs = finding.get('context_refs', [])
        if not isinstance(context_refs, list):
            continue
        actual = []
        for ref in finding['evidence_ids']:
            if isinstance(ref, str) and ref not in result_ids:
                canonical = ref if ref.startswith('context:') else 'context:' + ref
                if canonical in directory:
                    context_refs = [*context_refs, canonical]
                    changes.append({'criterion': finding.get('criterion'), 'from': ref, 'to': canonical})
                    continue
            actual.append(ref)
        finding['evidence_ids'] = actual
        finding['context_refs'] = list(dict.fromkeys(context_refs)) if all(isinstance(v, str) for v in context_refs) else context_refs
    return review, changes
