"""Run the unchanged 10 natural questions through the real API in isolated sessions.

No reference answer module is imported. Numerical evaluation is a separate process.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/homenova_phase_e'))
from run_agent_tests import make_plan, credential, request, save

p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--workers',type=int,default=1);p.add_argument('--only',help='Comma-separated question IDs for targeted repair; omit only for an intentional full baseline');args=p.parse_args()
if not 1 <= args.workers <= 3:p.error('workers must be between 1 and 3')
selected=set(args.only.split(',')) if args.only else None
if selected and not selected <= {'q'+str(i).zfill(2) for i in range(1,11)}:p.error('unknown question ID')
if args.output.exists() and any(args.output.iterdir()):p.error('use a new empty evidence directory')
args.output.mkdir(parents=True,exist_ok=True);base='http://127.0.0.1:8001'
plan=make_plan(selected);save(args.output/'plan.json',{'cases':plan,'answer_assets_loaded_by_runner':False,'workers':args.workers})
password=credential('admin');_,login=request(base,'/api/auth/login',payload={'username':'admin','password':password});token=login['access_token']
_,health=request(base,'/api/health',token=token);save(args.output/'preflight.json',{'health':health})

def run(case):
 start=time.monotonic();payload={'query':case['prompt'],'session_id':case['session_id'],'workspace':{'datasource_id':case['datasource_id']}}
 record={**case,'request':payload}
 try:
  code,result=request(base,'/api/query',token=token,payload=payload,timeout=360)
  record.update(http_status=code,response=result,status=result.get('status'))
  record['selected_snapshot_verified']=result.get('datasource_id')==case['datasource_id'] and (result.get('scenario') or {}).get('observation_cutoff_at')==case['expected_cutoff']
 except Exception as exc:record.update(status='transport_error',error=type(exc).__name__+': '+str(exc))
 record['elapsed_seconds']=round(time.monotonic()-start,3)
 save(args.output/(case['case_id']+'.json'),record,(password,token))
 print(case['case_id']+': '+str(record['status']),flush=True)
 return {k:record.get(k) for k in ['case_id','question_id','snapshot','status','http_status','elapsed_seconds','selected_snapshot_verified']}

try:
 results=[]
 with ThreadPoolExecutor(max_workers=args.workers) as pool:
  for future in as_completed([pool.submit(run,case) for case in plan]):
   results.append(future.result());save(args.output/'run_summary.json',{'cases_planned':len(plan),'results':results})
finally:request(base,'/api/auth/logout',token=token,payload={})
