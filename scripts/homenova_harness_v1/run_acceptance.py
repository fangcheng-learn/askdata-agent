"""Real model/retrieval/MCP acceptance. Natural requests only, no expected answers."""
import sys,json,time,uuid,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/homenova_phase_e'))
from run_agent_tests import credential,request,save
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'data/homenova_harness_v1/private/acceptance_round1');args=parser.parse_args()
folder=args.output
if folder.exists():raise RuntimeError('Use a fresh output directory')
folder.mkdir(parents=True)
base='http://127.0.0.1:8001';password=credential('admin');_,auth=request(base,'/api/auth/login',payload={'username':'admin','password':password});token=auth['access_token']

def ask(name,query,session):
 payload={'query':query,'session_id':session,'workspace':{'datasource_id':'homenova_20260831'}};start=time.monotonic()
 code,result=request(base,'/api/query',token=token,payload=payload,timeout=360)
 save(folder/(name+'.json'),{'request':payload,'http_status':code,'response':result,'elapsed_seconds':round(time.monotonic()-start,3),'answer_assets_loaded':False},(password,token))
 print(name+': '+str(result.get('status'))+' '+str(result.get('budget')),flush=True)
 return result

try:
 session='harness-simple-'+uuid.uuid4().hex
 ask('simple','查询本月成功支付订单量和商品GMV，金额用欧分。',session)
 ask('followup','解释刚才的GMV和订单量，时间和口径是什么？',session)
 session='harness-target-'+uuid.uuid4().hex
 first=ask('missing_target','本月成功支付商品GMV距离全店目标还差多少？金额用欧分。',session)
 if first.get('status')=='waiting_clarification':
  ask('resume_target','本月全店成功支付商品GMV目标是5000000欧分，按完整8月目标计算。',session)
 ask('false_premise','2026年8月成功支付商品GMV为什么比7月下降？请先核实是否下降；如果没有下降就纠正前提并给出两期数字。金额欧分。','harness-premise-'+uuid.uuid4().hex)
 ask('unsupported','请精确计算2026年7月缺货真正造成的失单数与GMV损失；如果数据不能支持，请给可核实部分并明确未知。','harness-unsupported-'+uuid.uuid4().hex)
finally:request(base,'/api/auth/logout',token=token,payload={})
