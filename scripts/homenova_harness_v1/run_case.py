"""Real API only. No answers/SQL assets are imported into this process."""
import argparse
import sys
import json
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/homenova_phase_e'))
from run_agent_tests import credential, request, save

p=argparse.ArgumentParser();p.add_argument('--query',required=True);p.add_argument('--output',type=Path,required=True)
p.add_argument('--session');p.add_argument('--datasource',default='homenova_20260831');args=p.parse_args()
if args.output.exists(): p.error('use a new output filename')
args.output.parent.mkdir(parents=True,exist_ok=True)
base='http://127.0.0.1:8001';password=credential('admin')
_,login=request(base,'/api/auth/login',payload={'username':'admin','password':password});token=login['access_token']
payload={'query':args.query,'session_id':args.session or 'harness-v1-'+uuid.uuid4().hex,'workspace':{'datasource_id':args.datasource}}
started=time.monotonic()
try:
 status,result=request(base,'/api/query',token=token,payload=payload,timeout=360)
 save(args.output,{'request':payload,'http_status':status,'response':result,'elapsed_seconds':round(time.monotonic()-started,3),'answer_assets_loaded':False},(password,token))
 print(json.dumps({'status':result.get('status'),'task_id':result.get('task_id'),'mode':result.get('workflow_mode'),'budget':result.get('budget'),'message':result.get('message'),'stop_reason':result.get('stop_reason'),'output':str(args.output)},ensure_ascii=False))
finally:
 request(base,'/api/auth/logout',token=token,payload={})
