"""Task-scoped prompt/Skill overrides, separate from credentials and data."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
import os
from pathlib import Path

from .config import BASE_DIR

PROMPT_IDS = frozenset({'preprocess', 'react', 'sql_query', 'data_qa',
                        'investigation_report', 'review', 'memory_summary', 'result_draft', 'simple_review'})
SKILL_IDS = frozenset({'database_query', 'data_qa', 'business_diagnosis',
                       'goal_analysis', 'inventory_risk_check'})
current_configuration: ContextVar[dict | None] = ContextVar('agent_configuration', default=None)


@lru_cache(maxsize=8)
def _store(path: str):
    from .services.agent_configuration import AgentConfigurationStore
    return AgentConfigurationStore(Path(path), prompt_ids=PROMPT_IDS, skill_ids=SKILL_IDS)


def configuration_store():
    path = os.getenv('AGENT_CONFIGURATION_PATH') or str(BASE_DIR / 'data/agent_configuration/overrides.json')
    return _store(str(Path(path).resolve()))


def default_snapshot() -> dict:
    return {'revision': '0', 'updated_at': None, 'updated_by': None, 'prompts': {}, 'skills': {}}


def configuration_snapshot() -> dict:
    current = current_configuration.get()
    return deepcopy(current if current is not None else configuration_store().snapshot())


def effective_prompt(prompt_id: str, default_text: str) -> str:
    return configuration_snapshot().get('prompts', {}).get(prompt_id, default_text)


def effective_skill(name: str, default_text: str) -> str:
    return configuration_snapshot().get('skills', {}).get(name, default_text)
