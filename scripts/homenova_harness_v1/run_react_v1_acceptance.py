"""Three distinct real tasks, reserved before HTTP; never repeat a spent slot."""
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
DIRECTORY = ROOT / 'data/homenova_harness_v1/private/react_v1_20261029'
LEDGER = DIRECTORY / 'paid_runs.json'
QUESTIONS = [
    {'case': 1, 'scenario': '全盘变化调查', 'query': '2026年7月GMV为什么比6月少了？感觉销量还可以啊。'},
    {'case': 2, 'scenario': '指定对象调查', 'query': 'PLG-MINI在2026年7月的GMV怎么比6月降这么多？'},
    {'case': 3, 'scenario': '用户口述看板发现后追问', 'query': '看板上2026年7月比6月的访客少了，流量为什么会掉？'},
]
sys.path.insert(0, str(ROOT / 'scripts/homenova_phase_e'))
from run_agent_tests import credential, request, save


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def prepare():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if LEDGER.exists():
        current = json.loads(LEDGER.read_text())
        if current.get('limit') != 3 or current.get('questions') != QUESTIONS:
            raise RuntimeError('已有新台账配置不同，禁止重置')
        return
    with LEDGER.open('x') as handle:
        json.dump({'limit':3,'questions':QUESTIONS,'created_at':stamp(),'run_id':uuid.uuid4().hex[:12],
            'authorization':'2026-10-29用户明确授权三个不同场景；失败也计次，旧台账保持',
            'runs':[]}, handle, ensure_ascii=False, indent=2)


def mutate(callback):
    with LEDGER.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ledger = json.load(handle)
        result = callback(ledger)
        handle.seek(0)
        json.dump(ledger, handle, ensure_ascii=False, indent=2)
        handle.write('\n'); handle.truncate(); handle.flush()
        return result


def reserve(case):
    def change(ledger):
        if ledger.get('limit') != 3 or len(ledger['runs']) >= 3:
            raise RuntimeError('三个真实任务额度已用完')
        if ledger.get('provider_blocked'):
            raise RuntimeError('已有模型服务错误，保留未用名额，不探测同一故障')
        if any(x['case'] == case for x in ledger['runs']):
            raise RuntimeError('该场景已预留或运行，禁止重跑')
        item = {**QUESTIONS[case-1], 'status':'reserved','reserved_at':stamp(),
                'output':str(DIRECTORY/f'case_{case}.json')}
        if Path(item['output']).exists():
            raise RuntimeError('存在历史输出，禁止覆盖或重新运行')
        ledger['runs'].append(item)
        return item
    return mutate(change)


def update(case, **values):
    def change(ledger):
        next(x for x in ledger['runs'] if x['case']==case).update(values)
    mutate(change)


def main():
    global DIRECTORY, LEDGER
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--case',type=int,choices=[1,2,3])
    parser.add_argument('--run-dir',type=Path,help='New explicitly authorized batch; old ledgers are never reset')
    args=parser.parse_args()
    if args.run_dir:
        selected=args.run_dir.resolve()
        private=(ROOT/'data/homenova_harness_v1/private').resolve()
        if selected == private or not selected.is_relative_to(private):
            parser.error('--run-dir必须是本项目独立private评测子目录')
        DIRECTORY,LEDGER=selected,selected/'paid_runs.json'
    prepare()
    if args.prepare:
        print(json.dumps({'ledger':str(LEDGER),'limit':3,'questions':QUESTIONS},ensure_ascii=False));return
    if args.case is None:
        parser.error('必须选择 --prepare 或 --case，不能自动循环重跑')
    item=reserve(args.case)
    password=credential('admin'); token=None; started=time.monotonic()
    try:
        _,login=request('http://127.0.0.1:8001','/api/auth/login',payload={'username':'admin','password':password})
        token=login['access_token']
        batch=json.loads(LEDGER.read_text()).get('run_id',DIRECTORY.name)
        payload={'query':item['query'],'session_id':f'react-v1-{batch}-case-{args.case}',
                 'workspace':{'datasource_id':'homenova_20260831'}}
        update(args.case,status='started',started_at=stamp())
        status,response=request('http://127.0.0.1:8001','/api/query',token=token,payload=payload,timeout=360)
        record={'request':payload,'http_status':status,'response':response,
                'elapsed_seconds':round(time.monotonic()-started,3),
                'source_kind':'user_described_dashboard' if args.case==3 else 'natural_question',
                'answer_assets_loaded':False}
        save(Path(item['output']),record,(password,token))
        update(args.case,status='recorded',task_id=response.get('task_id'),agent_status=response.get('status'),
               completed_at=stamp(),elapsed_seconds=record['elapsed_seconds'])
        usage=(response.get('budget') or {}).get('model_usage') or []
        if any(x.get('status') in {'http_error','transport_error'} for x in usage):
            mutate(lambda ledger: ledger.update(provider_blocked=True))
        print(json.dumps({'case':args.case,'task_id':response.get('task_id'),'status':response.get('status'),
            'final_reviewed':response.get('final_reviewed'),'stop_reason':response.get('stop_reason'),
            'seconds':record['elapsed_seconds'],'output':item['output']},ensure_ascii=False))
    except Exception as exc:
        update(args.case,status='error',error_type=type(exc).__name__,completed_at=stamp())
        raise RuntimeError(f'场景{args.case}异常，额度已占用且不会重跑：{type(exc).__name__}') from None
    finally:
        if token:
            request('http://127.0.0.1:8001','/api/auth/logout',token=token,payload={})


if __name__=='__main__':
    main()
