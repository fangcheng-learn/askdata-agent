"""Owner-scoped background runs and cooperative controls for the analysis UI.

No provider call is made by discovery, polling, cancellation or restoration.
Cancellation takes effect at the next model/tool boundary; an in-flight request
is allowed to return so its actual usage and evidence can still be recorded.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Callable, Any
from uuid import uuid4


@dataclass
class ExecutionControl:
    model: str = ''
    skills: list[str] | None = None
    tools: list[str] | None = None
    cancelled: Event = field(default_factory=Event)
    on_progress: Callable[[str], None] = lambda stage: None
    on_event: Callable[[dict], None] = lambda event: None
    business_signatures: dict[str,str] = field(default_factory=dict)

    def check(self) -> None:
        if self.cancelled.is_set():
            from .runtime_budget import BudgetStop
            raise BudgetStop('用户已停止分析；已完成的证据保留')


current_execution: ContextVar[ExecutionControl | None] = ContextVar('workbench_execution', default=None)


def check_execution(stage: str = '') -> None:
    control = current_execution.get()
    if control:
        control.check()
        if stage:
            control.on_progress(stage)


def emit_execution_event(event: dict) -> None:
    control = current_execution.get()
    if control:
        control.on_event(event)


class WorkbenchRuns:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = RLock()
        self.records: dict[str, dict] = {}
        self.controls: dict[str, ExecutionControl] = {}
        for path in directory.glob('*.json') if directory.exists() else []:
            try:
                item = json.loads(path.read_text())
                if item['status'] in {'running', 'stopping'}:
                    item.update(status='interrupted', stage='服务重启，执行已中断', error='请查看已保存任务后重新发起分析')
                self.records[item['id']] = item
            except (ValueError, KeyError, OSError):
                continue

    def _save(self, item: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (item['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(path)

    def read(self, run_id: str, owner: str, *, include_result: bool = True) -> dict:
        with self.lock:
            item = self.records.get(run_id)
            if not item or item['owner'] != owner:
                raise KeyError(run_id)
            return deepcopy({k: v for k, v in item.items() if k != 'owner' and (include_result or k not in {'result','events'})})

    def read_progress(self, run_id: str, owner: str, after: int = 0) -> dict:
        with self.lock:
            item = self.read(run_id, owner, include_result=False)
            events = self.records[run_id].get('events', [])
            return {**item, 'events':deepcopy(events[after:]), 'cursor':len(events)}

    def list(self, owner: str, datasource: str, session_id: str | None = None) -> list[dict]:
        with self.lock:
            items = [v for v in self.records.values() if v['owner'] == owner and v['workspace'].get('datasource_id') == datasource and not v.get('hidden') and (session_id is None or v['session_id'] == session_id)]
            return [self.read(v['id'], owner) for v in sorted(items, key=lambda v: v['created_at'], reverse=True)[:100]]

    def start(self, owner: str, query: str, session_id: str, workspace: dict,
              options: dict, execute: Callable[[], Any], *, count_analysis: bool = True) -> dict:
        from .security import public_demo, trial_quota
        with self.lock:
            if any(v['owner'] == owner and v['status'] == 'stopping' for v in self.records.values()):
                raise ValueError('上一次分析已停止调度，正在等待已发送的调用结束，请稍后再提交')
            if any(v['owner'] == owner and v['status'] == 'running' for v in self.records.values()):
                raise ValueError('已有分析运行中，请等待完成或停止后再提交')
            run_id = uuid4().hex
            item = dict(id=run_id, owner=owner, query=query, session_id=session_id, workspace=deepcopy(workspace),
                        options=deepcopy(options), status='running', stage='正在理解问题', result=None, error='', events=[],
                        created_at=datetime.now(timezone.utc).isoformat())
            self.records[run_id] = item
            try:
                self._save(item)
            except OSError:
                self.records.pop(run_id, None)
                raise
            phase = None
            def emit(event: dict):
                with self.lock:
                    item['events'].append(deepcopy({'timestamp':int(time.time()*1000), **event}))
            def finish_phase(status='completed'):
                if phase:
                    emit({'type':'ACTIVITY_SNAPSHOT', 'messageId':phase['step_id'], 'activityType':'STEP',
                          'content':{**phase, 'status':status}})
            def progress(stage: str):
                nonlocal phase
                with self.lock:
                    if item['status'] == 'running' and (phase is None or phase['title'] != stage):
                        finish_phase()
                        item['stage'] = stage
                        phase = {'step_id':run_id+'-phase-'+str(len(item['events'])), 'title':stage,
                                 'phase':True}
                        emit({'type':'ACTIVITY_SNAPSHOT', 'messageId':phase['step_id'], 'activityType':'STEP',
                              'content':{**phase, 'status':'running'}})
                        self._save(item)
            progress(item['stage'])
            control = ExecutionControl(**options, on_progress=progress, on_event=emit)
            self.controls[run_id] = control

        def work():
            token = current_execution.set(control)
            trial_token = trial_quota.active_run.set(run_id) if public_demo.enabled() else None
            try:
                control.check()
                result = execute()
                with self.lock:
                    item['result'] = result.model_dump(mode='json') if hasattr(result, 'model_dump') else result
                    item['status'] = 'stopped' if control.cancelled.is_set() else 'completed'
                    item['stage'] = '已停止' if control.cancelled.is_set() else '分析已结束'
            except Exception as exc:
                with self.lock:
                    item.update(status='stopped' if control.cancelled.is_set() else 'failed',
                                stage='已停止' if control.cancelled.is_set() else '分析失败', error=str(exc))
            finally:
                if trial_token is not None:
                    trial_quota.active_run.reset(trial_token)
                    try:
                        trial_quota.finish_run(run_id)
                    except trial_quota.TrialLimit:
                        pass  # The persisted lease expires; never clear quotas locally.
                current_execution.reset(token)
                with self.lock:
                    item['finished_at'] = datetime.now(timezone.utc).isoformat()
                    finish_phase('completed' if item['status'] == 'completed' else 'failed')
                    self.controls.pop(run_id, None)
                    self._save(item)
        try:
            if public_demo.enabled():
                trial_quota.begin_run(run_id, owner, count_analysis=count_analysis)
            Thread(target=work, daemon=True, name='analysis-' + run_id[:8]).start()
        except Exception:
            with self.lock:
                self.controls.pop(run_id, None)
                item.update(status='failed', stage='未开始分析', error='本次分析未启动',
                            finished_at=datetime.now(timezone.utc).isoformat())
                self._save(item)
            raise
        return self.read(run_id, owner)

    def hide_session(self, owner: str, datasource: str, session_id: str) -> None:
        with self.lock:
            for item in self.records.values():
                if item['owner'] == owner and item['workspace'].get('datasource_id') == datasource and item['session_id'] == session_id:
                    item['hidden'] = True
                    self._save(item)

    def cancel(self, run_id: str, owner: str) -> dict:
        with self.lock:
            self.read(run_id, owner)
            item = self.records[run_id]
            control = self.controls.get(run_id)
            if control and item['status'] in {'running', 'stopping'}:
                control.cancelled.set()
                item.update(status='stopping', stage='正在停止，等待当前调用结束')
                self._save(item)
            return self.read(run_id, owner)
