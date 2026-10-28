# -*- coding: utf-8 -*-
"""Build offline per-analysis reports from saved real API records only.

No model/network imports or calls. No source record is modified. The output is
self-contained HTML plus Markdown; provider billing data is not part of it.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'data/homenova_harness_v1/private'
DEFAULT_OUTPUT = PRIVATE / 'process_reports'
STATUS = {'completed': '系统完成', 'partial': '部分完成', 'failed': '系统失败',
          'waiting_clarification': '等待补充', 'pending': '待执行', 'blocked': '依赖阻塞',
          'not_applicable': '不适用', 'basic_valid': '基础检查通过', 'reviewed': '模型审核通过',
          'needs_review': '待核验', 'superseded': '已失效'}
STAGE = {'planner':'规划', 'step_execution':'步骤执行', 'step_error':'步骤错误',
         'query_contract_validation':'查询契约检查', 'mcp_tool_call':'真实MCP调用',
         'execute_duckdb':'数据库执行', 'result_review':'结果审核', 'delivery_review':'交付审核',
         'step_analysis':'结构化结果分析', 'reuse':'结果复用', 'route_fallback':'模型入口故障',
         'budget_stop':'预算停止', 'step_repair_exhausted':'步骤修复耗尽',
         'closing_incomplete':'收尾未完成', 'harness_error':'调查错误',
         'planner_field_check':'计划字段检查', 'delivery_text_check':'交付文案检查'}
GROUPS = {'baseline_signoff':'最后完整经营题基线', 'baseline_final':'经营题基线 · final轮',
          'baseline_release':'经营题基线 · release轮', 'baseline_round3':'经营题基线 · 第3轮',
          'baseline_round2':'经营题基线 · 第2轮', 'baseline_round1':'经营题基线 · 第1轮',
          'acceptance_signoff':'专项验收 · signoff轮', 'acceptance_final':'专项验收 · final轮',
          'acceptance_round3':'专项验收 · 第3轮', 'acceptance_round2':'专项验收 · 第2轮',
          'acceptance_round1':'专项验收 · 第1轮', 'individual':'独立查询与定向复测', 'ui':'界面GMV调查'}
UI_INDEPENDENT = {'ui_gmv_round3':'未通过：缺少比较期渠道数据，已审核文案仍夸大关联。',
                  'ui_gmv_round5':'未通过：数字核对一致，客单价被误用来排除SKU价格或商品结构变化。',
                  'ui_gmv_round6':'未通过：数字核对一致，客单价被误用来排除SKU价格或商品结构变化。'}
TASK_NAMES = {
    'q01':'3—8月每月成交、净销售与商品毛利',
    'q02':'类目与SKU的销量、销售额及毛利率比较',
    'q03':'3月与4月浏览转化、本店及竞品价格比较',
    'q04':'两个投放周期的渠道预算、花费与成交回报比较',
    'q05':'两个经营周期的价格、件数与商品利润比较',
    'q06':'7月1日至8月14日SKU不可售区间与购买受阻调查',
    'q07':'7月20—27日发货的配送及售后调查',
    'q08':'8月退款的原成交月份、收入冲减与成本恢复',
    'q09':'7—8月固定支付订单队列的经营状态核对',
    'q10':'缺货失单、竞品抢客与促销增量利润的可证明性',
    'simple':'本月成功支付订单量与商品GMV查询',
    'followup':'解释上一轮GMV、订单量及时间口径',
    'missing_target':'本月GMV距离全店目标的缺口（含澄清恢复）',
    'false_premise':'核实8月GMV是否比7月下降并纠正前提',
    'unsupported':'7月缺货真实失单及GMV损失的证据边界',
    'gmv_initial':'2026年7月GMV下降调查（对比6月）',
    'gmv_round2':'2026年7月GMV下降调查（对比6月）',
    'gmv_price_hypothesis':'7月GMV下降的SKU贡献、成交单价与件数解释检验',
    'gmv_price_hypothesis_final':'7月GMV下降的SKU贡献、成交单价与件数解释检验',
    'simple_initial':'本月成功支付订单量与商品GMV查询',
    'json_mode_smoke':'本月成功支付订单量与商品GMV查询',
    'quantity_aggregation_real':'6月与7月成功支付商品GMV及购买总件数查询',
    'legacy_real_regression':'8月1日至22日订单明细的地区销售额查询',
    'legacy_grouped_regression':'8月1日至22日按地区汇总销售额',
    'premise_controller_fix':'核实8月GMV是否比7月下降并纠正前提',
    'model_availability_recheck':'7月GMV下降调查（模型服务恢复复查）',
}


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def esc(value):
    return html.escape(str(value if value is not None else '未记录'), quote=True)


def code(value, lang='json'):
    return '<pre class="code"><code>' + esc(value if isinstance(value, str) else dump(value)) + '</code></pre>'


def details(title, value, opened=False):
    return '<details'+(' open' if opened else '')+'><summary>'+esc(title)+'</summary>'+value+'</details>'


def badge(status):
    return '<span class="badge '+esc(status)+'">'+esc(STATUS.get(status, status))+'</span>'


def pairs(items):
    return '<dl>' + ''.join('<div><dt>'+esc(key)+'</dt><dd>'+esc(value)+'</dd></div>' for key,value in items) + '</dl>'


def table(columns, rows):
    if not rows:
        return '<p class="muted">未记录结果行。空结果不自动代表零。</p>'
    columns = columns or list(rows[0])
    return '<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+esc(column)+'</th>' for column in columns)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(dump(row.get(column)) if isinstance(row.get(column),(dict,list)) else row.get(column))+'</td>' for column in columns)+'</tr>' for row in rows)+'</tbody></table></div>'


def question(record):
    r = record['payload']['response']
    for origin, value in [('用户请求原文',record['payload'].get('request',{}).get('query')),
                          ('评测题目',record['payload'].get('prompt') or record['payload'].get('question')),
                          ('必须回答清单首项',next(iter(r.get('required_questions') or []),None)),
                          ('独立问题改写；原文未单独保存',r.get('standalone_query'))]:
        if value:
            return str(value), origin
    return '问题原文未保存在此记录中', '未记录'


def task_name(record):
    """Readable report labels, derived from saved requests; never runtime knowledge."""
    stem = record['source'].stem
    if stem.startswith('ui_gmv_round'):
        return '2026年7月GMV下降调查（对比6月）· 界面第'+stem.removeprefix('ui_gmv_round')+'次'
    key = stem.split('_asof_')[0]
    name = TASK_NAMES.get(key)
    if name and key == 'q09' and '_asof_' in stem:
        name += '（'+stem.split('_asof_')[1].replace('_','-')+'快照）'
    return name or question(record)[0]


def collect(output):
    tasks = defaultdict(list)
    for path in sorted(PRIVATE.rglob('*.json')):
        if output == path or output in path.parents:
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(payload,dict) or not isinstance(payload.get('response'),dict):
            continue
        response = payload['response']
        task_id = str(response.get('task_id') or '')
        if not re.fullmatch('[a-f0-9]{12}',task_id):
            continue
        tasks[task_id].append({'source':path,'payload':payload})
    return tasks


def score(record):
    r=record['payload']['response'];b=r.get('budget') or {}
    return (sum((b.get('model_calls') or {}).values()), b.get('active_seconds',0), len(r.get('execution_log') or []))


def group(record):
    path=record['source'].relative_to(PRIVATE)
    return path.parts[0] if len(path.parts)>1 else 'ui' if path.stem.startswith('ui_gmv_') else 'individual'


def independent(record, reviews):
    p=record['source'];r=record['payload']['response']
    if p.parent.name=='baseline_signoff' and record['payload'].get('case_id') in reviews:
        review=reviews[record['payload']['case_id']]
        verdict={'failed':'未通过','partial':'部分回答','passed':'通过'}.get(review['business_verdict'],'未独立核验')
        return verdict,review['reason']
    if p.stem in UI_INDEPENDENT:
        return '未通过',UI_INDEPENDENT[p.stem]
    explicit=record['payload'].get('independent_business_verdict')
    if explicit:
        return '未通过' if str(explicit).startswith('failed') else '已有独立说明',str(explicit)
    return '未独立核验','本报告保留真实执行和审核记录；系统状态及模型审核不代表业务正确。'


def attempts(event):
    return event.get('validation_attempts') or (event.get('context') or {}).get('validation_attempts') or []


def actual_calls(response):
    calls=[]
    for event in response.get('execution_log') or []:
        if event.get('stage')=='mcp_tool_call':
            calls.append(event)
        for trace in event.get('tool_trace') or (event.get('context') or {}).get('tool_trace') or []:
            calls.append({**trace,'step_id':event.get('step_id')})
    for item in response.get('tool_calls') or []:
        if item.get('sql') and not any((call.get('arguments') or {}).get('sql')==item['sql'] for call in calls):
            calls.append({**item,'tool':item.get('tool') or (item.get('arguments') or {}).get('tool_name') or 'query_'+str(item.get('database','')),
                          'arguments':{'sql':item['sql']},'result':{'success':item.get('success'),'row_count':item.get('row_count'),'error':item.get('error')}})
    return calls


def render_attempt(a):
    return '<article class="attempt">'+pairs([('候选序号',a.get('attempt')),('前置检查','通过' if a.get('success') else '未通过'),('工具',a.get('tool_name')),('错误',a.get('error') or '无记录')])+ (code(a['sql'],'sql') if a.get('sql') else '')+details('候选查询契约',code(a.get('query_contract') or {}))+'</article>'


def render_evidence(e):
    rows=e.get('rows') or []
    return '<article class="evidence"><h4>'+esc(e.get('id'))+' '+badge(e.get('check_status','unknown'))+'</h4>'+pairs([
        ('来源步骤',e.get('step_id')),('实际输入结果',', '.join(e.get('input_result_ids') or []) or '无'),
        ('数据源',e.get('datasource_id')),('服务端完整性',e.get('complete')),('完整结果行数',e.get('total_rows')),
        ('当前记录行数',len(rows)),('此记录是否仅预览',e.get('display_truncated',e.get('preview_truncated',False))),('完整结果引用',e.get('result_ref') or '未记录')])+ (code(e['sql'],'sql') if e.get('sql') else '')+details('查看已保存的结果行',table(e.get('columns'),rows))+details('口径、范围与契约',code({'conditions':e.get('conditions'),'snapshot':e.get('snapshot'),'query_contract':e.get('query_contract')}))+'</article>'


def render_body(task):
    chosen=task['chosen'];r=chosen['payload']['response'];b=r.get('budget') or {};q,origin=question(task['snapshots'][0])
    body='<header><a class="back" href="../index.html">← 全部分析</a><p class="eyebrow">'+esc(task['group_label'])+' · '+esc(task['id'])+'</p><h1>本次任务：'+esc(task['task_name'])+'</h1><p class="muted">任务名称按保存的请求整理；原始表述及来源如下。</p><h2>原始请求／已保存的任务描述</h2><p class="answer">'+esc(q)+'</p><p class="muted">描述来源：'+esc(origin)+'</p><div class="badges">'+badge(r.get('status','unknown'))+'<span class="badge independent">独立核查：'+esc(task['business'])+'</span></div><p class="notice">'+esc(task['business_note'])+'</p></header>'
    body+='<section><h2>1. 任务与消耗</h2>'+pairs([('原问题来源',origin),('数据源',r.get('datasource_id') or chosen['payload'].get('datasource_id')),('模式',r.get('workflow_mode')),('初始路由',r.get('route')),('路由理由',r.get('route_reason') or '未保存'),('选中业务Skill',', '.join(r.get('selected_skills') or []) or '无记录'),('累计主动秒数',b.get('active_seconds')),('本次API墙钟秒数',chosen['payload'].get('elapsed_seconds')),('工具次数',str(b.get('tools','未知'))+' / '+str(b.get('tool_limit','未知'))),('Chat / Embedding / Rerank', ' / '.join(str((b.get('model_calls') or {}).get(key,0)) for key in ['chat','embedding','rerank'])),('额外修复',dump(b.get('repairs') or {})),('重规划',b.get('replans')),('模型交付审核',r.get('final_reviewed')),('停止原因',r.get('stop_reason') or '无记录')])+'<p class="muted">历史文件没有服务端usage时，精确token与费用未知；调用尝试包含失败，不能当成计费token。</p>'+details('场景时钟与必须回答项',code({'scenario':r.get('scenario'),'required_questions':r.get('required_questions'),'standalone_query':r.get('standalone_query')}))+'</section>'
    body+='<section><h2>2. 请求与澄清恢复</h2><p>同一任务共保存 '+str(len(task['snapshots']))+' 份响应快照，累计次数按最后记录取值。</p>'
    for index,snap in enumerate(task['snapshots'],1):
        sr=snap['payload']['response'];sb=sr.get('budget') or {};sq,so=question(snap)
        body+='<article class="phase"><h3>响应快照 '+str(index)+' · '+esc(STATUS.get(sr.get('status'),sr.get('status')) )+'</h3><p>'+esc(sq)+'</p>'+pairs([('记录文件',str(snap['source'].relative_to(ROOT))),('累计主动秒数',sb.get('active_seconds')),('累计Chat尝试',(sb.get('model_calls') or {}).get('chat')),('澄清问题',(sr.get('clarification') or {}).get('question') or '无')])+details('请求及澄清选项',code({'request':snap['payload'].get('request'),'clarification':sr.get('clarification')}))+'<a href="../sources/'+task['id']+'_'+str(index)+'.json">打开此响应原始JSON</a></article>'
    body+='</section><section><h2>3. 计划、实际依赖与逐步执行</h2>'
    plan=r.get('plan') or []
    if not plan:body+='<p>此任务未保存多步计划；'+('走原有单步查询／结果问答路径。' if r.get('workflow_mode')!='investigation_harness_v1' else '可能在形成有效计划前停止，见错误日志。')+'</p>'
    for step in plan:
        body+='<article class="step"><h3>'+esc(step['id'])+' · '+esc(step['goal'])+'</h3>'+badge(step.get('status','unknown'))+pairs([('动作',step.get('kind')),('计划依赖',', '.join(step.get('depends_on') or []) or '无'),('实际输入结果',', '.join(step.get('input_result_ids') or []) or '无'),('本步结果',', '.join(step.get('result_ids') or []) or '无'),('预期产物',step.get('expected_output')),('审核触发',step.get('review_after')),('审核理由',step.get('review_reason') or '无'),('错误',step.get('error') or '无记录')])+details('计划输入字段与语义需求',code({k:step.get(k) for k in ['input_fields','metric_ids','required_fields','reuse_result_id']}))
        events=[e for e in r.get('execution_log') or [] if e.get('step_id')==step['id']]
        for no,event in enumerate(events,1):
            body+='<h4>执行记录 '+str(no)+' · '+esc(STAGE.get(event.get('stage'),event.get('stage')) )+'</h4>'+pairs([('耗时秒数',event.get('elapsed_seconds')),('错误',event.get('error') or '无')])
            for attempt in attempts(event):body+=render_attempt(attempt)
        for result in r.get('evidence') or []:
            if result.get('step_id')==step['id']:body+=render_evidence(result)
        body+='</article>'
    body+='</section><section><h2>4. 实际工具与SQL</h2><p class="muted">前置候选SQL与实际执行分开列出；生成了候选不代表执行过。</p>'
    calls=actual_calls(r)
    if not calls:body+='<p>此响应未记录实际工具调用。</p>'
    for index,call in enumerate(calls,1):
        result=call.get('result') or {}
        body+=details('工具 '+str(index)+' · '+str(call.get('tool') or '未记录')+' · '+('失败' if result.get('error') or result.get('success') is False else '返回结构化结果'),pairs([('步骤',call.get('step_id')),('原因',call.get('reason') or '无记录'),('结果行数',result.get('total_rows',result.get('row_count'))),('错误',result.get('error') or '无')])+code(call.get('arguments') or {})+details('工具结构化返回',code(result)))
    if r.get('sql'):body+=details('响应中展示的SQL',code(r['sql'],'sql'))
    if not plan:
        for event in r.get('execution_log') or []:
            for attempt in attempts(event):body+=render_attempt(attempt)
        if r.get('rows'):body+=details('已保存查询表格',table(r.get('columns'),r['rows']))
    body+='</section><section><h2>5. 检索与结果审核</h2>'
    retrievals=r.get('retrieval_history') or ([r['retrieval']] if r.get('retrieval') else [])
    for index,ret in enumerate(retrievals,1):
        body+=details('检索 '+str(index)+' · '+str(ret.get('query') or '本轮相关语义'),pairs([(k,ret.get(k)) for k in ['embedding_source','rerank_source','bm25_count','dense_count','rrf_count','selected_count']])+details('召回与补齐的公开指标',code(ret.get('selected_semantics') or [])))
    for index,review in enumerate(r.get('reviews') or [],1):
        body+='<article class="review"><h3>审核 '+str(index)+' · '+('交付审核' if review.get('final') else '过程审核')+'</h3>'+pairs([('动作',review.get('action')),('前提状态',review.get('premise_status','旧版未记录')),('问题类型',review.get('issue_type')),('受影响步骤',', '.join(review.get('affected_steps') or []) or '无'),('审核理由',review.get('reason'))])+details('已回答项、证据引用与未回答项',code({'answered':review.get('answered'),'unanswered':review.get('unanswered'),'programmatic_text_check':review.get('programmatic_text_check')}))+details('本次审核给出的文案', '<div class="answer">'+esc(review.get('approved_text') or '此次未提供交付文案')+'</div>')+'</article>'
    if not r.get('reviews'):body+='<p class="muted">此版本未保存独立审核输出。final_reviewed字段只能作为系统标记，不能重建缺失的审核内容。</p>'
    body+='</section><section><h2>6. 实际交付内容</h2><p class="notice">系统消息：'+esc(r.get('message'))+'</p><div class="answer">'+esc(r.get('analysis') or '无保存的文字交付')+'</div></section>'
    timeline=[{'event_index':i+1,**{k:event.get(k) for k in ['stage','step_id','success','elapsed_seconds','error'] if k in event}} for i,event in enumerate(r.get('execution_log') or [])]
    body+='<section><h2>7. 完整追踪与来源</h2>'+details('按保存顺序查看阶段时间线',code(timeline),True)+details('完整执行日志（含所有候选与错误）',code(r.get('execution_log') or []))+details('节点用量记录',code(b.get('model_usage') or '历史版本未记录服务端usage'))+'<p class="muted">报告由已保存记录离线生成，无新增模型、检索或业务查询。未记录的起止时间、草稿正文、模型输入输出token均标为未知，不从摘要补造。</p></section>'
    return body


CSS='''
:root{color-scheme:light;--ink:#233329;--muted:#677368;--line:#dce2d8;--paper:#f6f7f2;--accent:#246246}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}a{color:var(--accent)}header,section{max-width:1100px;margin:0 auto;padding:28px 30px}header{padding-top:38px}section{border-top:1px solid var(--line)}h1{font-size:27px;line-height:1.5;margin:8px 0 16px;max-width:1000px}h2{font-size:20px;margin:0 0 18px}h3{font-size:16px;line-height:1.7;margin:0 0 10px}h4{font-size:14px}.eyebrow{font-size:12px;letter-spacing:.06em;color:var(--muted)}.muted{color:var(--muted);font-size:13px}.badge{font-size:12px;display:inline-block;border:1px solid var(--line);padding:3px 9px;border-radius:5px;background:#edf0e8;margin:0 6px 5px 0}.completed,.reviewed{color:#226243;background:#e8f1e8}.failed,.superseded{color:#a24939;background:#f8eae5}.partial,.waiting_clarification,.needs_review{color:#95601b;background:#f8efd9}.independent{background:#fff}.notice{padding:12px 16px;background:#fff;border-left:3px solid #b49343;font-size:13px}.step,.review,.phase,.evidence{border-top:1px solid var(--line);padding:20px 0;margin-top:15px}dl{margin:14px 0}dl>div{display:grid;grid-template-columns:165px 1fr;gap:16px;padding:5px 0;border-bottom:1px solid #e8ece3}dt{color:var(--muted);font-size:12px}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}details{margin:14px 0;border:1px solid var(--line);border-radius:6px;padding:10px 14px;background:#fff}summary{font-size:13px;cursor:pointer;font-weight:600;overflow-wrap:anywhere}.code{background:#eef1e9;padding:14px;font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:580px;overflow:auto}.answer{white-space:pre-wrap;font-size:14px;line-height:1.9}.table-scroll{overflow:auto;max-height:520px}table{border-collapse:collapse;width:100%;font-size:12px}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}th{position:sticky;top:0;background:#edf1e7}.back{font-size:12px}.attempt{border-left:2px solid #d8dfd0;padding-left:14px;margin:18px 0}.shell{height:100vh;display:grid;grid-template-columns:380px 1fr}.sidebar{height:100vh;overflow:auto;background:#f1f3eb;border-right:1px solid var(--line);padding:24px}.sidebar h1{font-size:25px}.sidebar input,.sidebar select{font:13px inherit;padding:10px;border:1px solid #cbd4c5;border-radius:5px;background:white;width:100%;margin:5px 0}.filters{display:grid;grid-template-columns:1fr 1fr;gap:6px}.task-link{display:block;text-decoration:none;color:inherit;padding:13px 9px;border-bottom:1px solid var(--line)}.task-link:hover,.task-link.active{background:#e1e9dc}.task-link strong{display:block;font-size:13px;line-height:1.5}.task-link small{display:block;font-size:11px;color:var(--muted)}iframe{border:0;width:100%;height:100vh;background:var(--paper)}.count{font-size:12px;margin:14px 0}.kpis{display:flex;gap:12px}.kpis b{font-size:23px;display:block}.kpis span{font-size:11px;color:var(--muted)}@media(max-width:800px){.shell{display:block;height:auto}.sidebar{height:auto;max-height:42vh;border-bottom:1px solid var(--line)}iframe{height:75vh}header,section{padding:22px 18px}h1{font-size:23px}dl>div{grid-template-columns:105px 1fr}}@media print{details{break-inside:avoid}.code{max-height:none}.back{display:none}body{background:white}}
'''


def md_report(task):
    chosen=task['chosen'];r=chosen['payload']['response'];b=r.get('budget') or {};q,origin=question(task['snapshots'][0])
    out=[f'# 本次任务：{task["task_name"]}',f'任务ID：{task["id"]}；任务分组：{task["group_label"]}',
         '## 原始请求／已保存的任务描述',q,f'描述来源：{origin}。上面的任务名称为报告阅读标签，按已有记录整理。',
         f'系统状态：{STATUS.get(r.get("status"),r.get("status"))}；独立业务核查：{task["business"]}',task['business_note'],
         '## 任务与预算','```json',dump({'datasource_id':r.get('datasource_id'),'scenario':r.get('scenario'),'route':r.get('route'),
            'workflow_mode':r.get('workflow_mode'),'selected_skills':r.get('selected_skills'),'required_questions':r.get('required_questions'),'budget':b,'stop_reason':r.get('stop_reason')}),'```',
         '## 请求与澄清快照']
    for index,snap in enumerate(task['snapshots'],1):
        sr=snap['payload']['response'];sq,so=question(snap);out.extend([f'### 响应快照 {index}',f'本次输入／保存的问题（{so}）：{sq}',f'来源：[原始JSON](../sources/{task["id"]}_{index}.json)',
            '```json',dump({'request':snap['payload'].get('request'),'status':sr.get('status'),'clarification':sr.get('clarification'),'budget':sr.get('budget')}),'```'])
    out+=['## 步骤计划与实际依赖','```json',dump(r.get('plan') or '没有保存多步计划'),'```','## 实际执行时间线']
    for index,event in enumerate(r.get('execution_log') or [],1):
        out += [f'### 事件 {index}：{STAGE.get(event.get("stage"),event.get("stage"))}', '```json',dump(event),'```']
    out+=['## 实际结果及SQL']
    for result in r.get('evidence') or []:
        out += [f'### {result.get("id")}', '```sql',result.get('sql') or '-- 此步骤为结构化结果分析，无SQL','```','```json',dump(result),'```']
    if not r.get('evidence'):
        out += ['```sql',r.get('sql') or '-- 此响应没有实际SQL记录','```','```json',dump({'tool_calls':r.get('tool_calls'),'columns':r.get('columns'),'rows':r.get('rows')}),'```']
    out+=['## 过程审核与交付审核','```json',dump(r.get('reviews') or '该版本没有保存独立审核正文'),'```','## 实际交付',r.get('analysis') or '无文字交付',
          '## 记录边界','本文件为已保存记录的离线整理，无新增模型调用。模型内部思考、未保存的草稿和精确token不从摘要补造。']
    return '\n\n'.join(out)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT);parser.add_argument('--refresh-generated',action='store_true');args=parser.parse_args();output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()):
        previous = json.loads((output/'manifest.json').read_text()) if (output/'manifest.json').exists() else {}
        if not args.refresh_generated or previous.get('source') != str(PRIVATE) or previous.get('network_or_model_calls') is not False:
            raise RuntimeError('Output must be empty or an explicitly refreshed generated report directory')
    tasks=collect(output);reviews_path=PRIVATE/'independent_business_review.json'
    reviews={item['case_id']:item for item in json.loads(reviews_path.read_text()).get('cases',[])} if reviews_path.exists() else {}
    for sub in ['tasks','markdown','sources']:(output/sub).mkdir(exist_ok=True)
    (output/'report.css').write_text(CSS)
    normalized=[]
    order=['ui','baseline_signoff','acceptance_signoff','individual','baseline_final','baseline_release','baseline_round3','baseline_round2','baseline_round1','acceptance_final','acceptance_round3','acceptance_round2','acceptance_round1']
    for task_id,snapshots in tasks.items():
        snapshots.sort(key=score);chosen=snapshots[-1];r=chosen['payload']['response'];g=group(chosen);business,note=independent(chosen,reviews)
        normalized.append({'id':task_id,'snapshots':snapshots,'chosen':chosen,'group':g,'group_label':GROUPS.get(g,g),
                           'business':business,'business_note':note,'task_name':task_name(snapshots[0]),'question':question(snapshots[0])[0],'status':r.get('status'),'mode':r.get('workflow_mode') or '未记录'})
    normalized.sort(key=lambda task:(order.index(task['group']) if task['group'] in order else 99,task['chosen']['source'].name))
    for task in normalized:
        for index,snapshot in enumerate(task['snapshots'],1):
            (output/'sources'/f'{task["id"]}_{index}.json').write_text(dump(snapshot['payload'])+'\n')
        content='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(task['task_name'])+' · '+task['id']+'</title><link rel="stylesheet" href="../report.css"></head><body>'+render_body(task)+'</body></html>'
        (output/'tasks'/(task['id']+'.html')).write_text(content)
        (output/'markdown'/(task['id']+'.md')).write_text(md_report(task))
    catalog=[{key:task[key] for key in ['id','task_name','group','group_label','business','business_note','question','status','mode']} for task in normalized]
    summaries=Counter(task['status'] for task in normalized);model=Counter()
    for task in normalized:model.update(task['chosen']['payload']['response'].get('budget',{}).get('model_calls',{}))
    default=next((task['id'] for task in normalized if task['chosen']['source'].stem=='ui_gmv_round6'),normalized[0]['id'])
    index='''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HomeNova · 每次分析过程报告</title>
<link rel="stylesheet" href="report.css">
</head>
<body>
<div class="shell">
<aside class="sidebar">
<p class="eyebrow">HOMENOVA / RUN REVIEW</p>
<h1>每次分析的过程</h1>
<p class="muted">2026-10-27 · 从已有真实运行离线整理<br>系统完成 ≠ 业务核查通过</p>
<div class="kpis">
<div>
<b>'''+str(len(normalized))+'''</b>
<span>独立任务</span>
</div>
<div>
<b>'''+str(sum(len(t['snapshots']) for t in normalized))+'''</b>
<span>响应快照</span>
</div>
<div>
<b>'''+str(model['chat'])+'''</b>
<span>Chat调用尝试</span>
</div>
</div>
<input id="search" aria-label="搜索任务名称、问题或任务ID" placeholder="搜索任务名称、问题或ID…">
<div class="filters">
<select id="group" aria-label="筛选运行批次">
<option value="">全部批次</option>
</select>
<select id="status" aria-label="筛选系统状态">
<option value="">全部状态</option>
<option value="completed">系统完成</option>
<option value="partial">部分完成</option>
<option value="failed">系统失败</option>
<option value="waiting_clarification">等待补充</option>
</select>
</div>
<div id="count" class="count">
</div>
<nav id="list" aria-label="分析记录">
</nav>
<p class="muted">四组澄清恢复合并为原任务，两个响应快照均保留。历史模型usage未记录，不能据次数计算精确账单。</p>
</aside>
<main>
<iframe name="detail" id="detail" title="分析任务过程明细" src="tasks/'''+default+'''.html">
</iframe>
</main>
</div>
<script>
const records='''+json.dumps(catalog,ensure_ascii=False).replace('<',chr(92)+'u003c')+''';
const names={completed:'系统完成',partial:'部分完成',failed:'系统失败',waiting_clarification:'等待补充'};
const list=document.querySelector('#list'), detail=document.querySelector('#detail');let selected='''+json.dumps(default)+''';
for(const [value,label] of new Map(records.map(r=>[r.group,r.group_label]))){const option=document.createElement('option');option.value=value;option.textContent=label;document.querySelector('#group').append(option)}
function render(){const query=document.querySelector('#search').value.toLowerCase(),group=document.querySelector('#group').value,status=document.querySelector('#status').value;const filtered=records.filter(r=>(!query||[r.task_name,r.question,r.id,r.mode].join(' ').toLowerCase().includes(query))&&(!group||r.group===group)&&(!status||r.status===status));list.replaceChildren();document.querySelector('#count').textContent=`显示 ${filtered.length} / ${records.length} 个任务`;for(const r of filtered){const a=document.createElement('a');a.href='tasks/'+r.id+'.html';a.target='detail';a.className='task-link'+(r.id===selected?' active':'');a.dataset.taskId=r.id;const title=document.createElement('strong');title.textContent=r.task_name;const request=document.createElement('small');request.textContent='任务描述：'+r.question;const meta=document.createElement('small');meta.textContent=r.group_label+' · '+r.id;const state=document.createElement('small');state.textContent=(names[r.status]||r.status)+' · 独立核查：'+r.business;a.append(title,request,meta,state);a.addEventListener('click',()=>{selected=r.id;for(const element of list.children)element.classList.toggle('active',element.dataset.taskId===selected)});list.append(a)}}
for(const id of ['search','group','status'])document.getElementById(id).addEventListener(id==='search'?'input':'change',render);render();
</script>
</body>
</html>'''
    (output/'index.html').write_text(index)
    manifest={'task_count':len(normalized),'response_snapshots':sum(len(t['snapshots']) for t in normalized),'model_attempts':dict(model),'system_status_counts':dict(summaries),
              'source':str(PRIVATE),'network_or_model_calls':False,'default_task':default,'records':catalog,
              'source_sha256':{str(snap['source'].relative_to(ROOT)):hashlib.sha256(snap['source'].read_bytes()).hexdigest() for task in normalized for snap in task['snapshots']}}
    (output/'manifest.json').write_text(dump(manifest)+'\n')
    md=['# HomeNova 每次分析过程报告','生成日期：2026-10-27。仅离线整理已有真实记录，没有新增付费模型调用。',
        f'共 **{len(normalized)} 个任务、{manifest["response_snapshots"]} 份响应快照**。同任务澄清与恢复合并展示，两个响应均保留。',
        f'[打开可搜索的HTML报告]({output/"index.html"})','每份报告先写明本次任务及原始请求，再列口径、计划及真实依赖、SQL候选与实际工具、结果编号与数据、修复与审核、最终交付和消耗。任务名称按已有请求整理，澄清补充单独展示。',
        '系统completed与模型审核通过不等于业务正确。最后完整经营题基线及已审阅GMV单独标注独立核查；其他运行明确标未独立核验。',
        '记录限制：部分历史版本未保存模型草稿、完整审核正文及服务端usage，报告如实标明缺失。下列排序按批次和文件名组织，不冒充精确任务开始时间。',
        '## 优先查看',
        '- [先看GMV六次调查的过程对照](/Users/fangchengdemac/Documents/ChatGPT/dataagent/docs/homenova_harness_v1_gmv_process_comparison.md)',
        f'- [最后一次完整界面GMV调查：数值正确但解释未通过]({output/"markdown"/(default+".md")})',
        '## 全部逐次报告']
    for g in dict.fromkeys(t['group'] for t in normalized):
        md += ['### '+GROUPS.get(g,g),'| 本次任务（点击看过程） | 原始请求／已保存的任务描述 | 任务ID | 系统状态 | 独立核查 |','|---|---|---|---|---|']
        for task in normalized:
            if task['group']==g:
                label=task['question'].replace('|','／').replace('\n',' ')
                name=task['task_name'].replace('|','／').replace('\n',' ')
                md.append(f'| [{name}]({output/"markdown"/(task["id"]+".md")}) | {label} | {task["id"]} | {STATUS.get(task["status"],task["status"])} | {task["business"]} |')
    body = ''
    previous = ''
    for line in md:
        body += ('\n' if previous.startswith('|') and line.startswith('|') else '\n\n') + line
        previous = line
    (ROOT/'docs/homenova_harness_v1_process_report.md').write_text(body.lstrip()+'\n')
    print(dump({key:manifest[key] for key in ['task_count','response_snapshots','model_attempts','system_status_counts','network_or_model_calls']}))


if __name__=='__main__':main()
