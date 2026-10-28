"""Two distinct, persisted, real Agent trials; no availability probes or retries."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
from pathlib import Path
import sys
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / 'data/homenova_harness_v1/private/implementation_v2'
LEDGER = DIRECTORY / 'paid_runs.json'
sys.path.insert(0, str(ROOT / 'scripts/homenova_phase_e'))
from run_agent_tests import credential, request, save


def timestamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def reserve(case: int) -> dict:
    with LEDGER.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ledger = json.load(handle)
        if ledger.get('limit') != 2 or len(ledger['runs']) >= 2:
            raise RuntimeError('本次实施的两次真实验收额度已用完，禁止新增调用')
        query = ledger['questions'][case - 1]
        output = DIRECTORY / f'case_{case}.json'
        if output.exists():
            raise RuntimeError('该题已有真实输出，禁止重置台账后重复运行')
        for previous in ledger['runs']:
            previous_output = Path(previous['output'])
            if previous_output.exists():
                text = previous_output.read_text().lower()
                if any(marker in text for marker in ['arrearage', 'insufficient_balance', 'insufficient balance']):
                    raise RuntimeError('已有模型欠费记录，保留剩余名额，不再次探测同一服务故障')
        if any(item['query'] == query or item['case'] == case for item in ledger['runs']):
            raise RuntimeError('该问题已预留或运行，禁止重复验收')
        item = {'case': case, 'query': query, 'status': 'reserved', 'reserved_at': timestamp(),
                'output': str(output)}
        ledger['runs'].append(item)
        handle.seek(0); json.dump(ledger, handle, ensure_ascii=False, indent=2); handle.write('\n'); handle.truncate(); handle.flush()
        return item


def update(case: int, **values):
    with LEDGER.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ledger = json.load(handle)
        next(item for item in ledger['runs'] if item['case'] == case).update(values)
        handle.seek(0); json.dump(ledger, handle, ensure_ascii=False, indent=2); handle.write('\n'); handle.truncate(); handle.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=int, choices=[1, 2], required=True)
    args = parser.parse_args()
    item = reserve(args.case)  # reserve before any HTTP; crashes cannot silently reset the cap
    token = None
    password = credential('admin')
    base = 'http://127.0.0.1:8001'
    started = time.monotonic()
    try:
        _, login = request(base, '/api/auth/login', payload={'username': 'admin', 'password': password})
        token = login['access_token']
        payload = {'query': item['query'], 'session_id': 'harness-latest-' + uuid.uuid4().hex,
                   'workspace': {'datasource_id': 'homenova_20260831'}}
        update(args.case, status='started', started_at=timestamp())
        status, response = request(base, '/api/query', token=token, payload=payload, timeout=360)
        record = {'request': payload, 'http_status': status, 'response': response,
                  'elapsed_seconds': round(time.monotonic() - started, 3), 'answer_assets_loaded': False}
        save(Path(item['output']), record, (password, token))
        update(args.case, status='recorded', task_id=response.get('task_id'),
               agent_status=response.get('status'), completed_at=timestamp())
        print(json.dumps({'case': args.case, 'task_id': response.get('task_id'), 'status': response.get('status'),
                          'stop_reason': response.get('stop_reason'), 'output': item['output']}, ensure_ascii=False))
    except Exception as exc:
        update(args.case, status='error', error_type=type(exc).__name__, completed_at=timestamp())
        raise RuntimeError(f'第{args.case}次试运行失败，已计入上限；错误类型：{type(exc).__name__}') from None
    finally:
        if token:
            request(base, '/api/auth/logout', token=token, payload={})


if __name__ == '__main__':
    main()
