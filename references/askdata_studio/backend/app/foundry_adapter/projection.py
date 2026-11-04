"""Project real analysis records onto DataFoundry DTOs and AG-UI events.

This layer never generates business findings, infers missing metrics, executes SQL,
or calls a model. Historical restoration uses the same projection as live delivery.
"""
import json
import math
from datetime import datetime, timezone
from .report_presentation import present_report, chart_payload, encode_chart

def now():
    return datetime.now(timezone.utc).isoformat()

def result_text(result):
    report = result.get('report') or {}
    text = present_report(report) if report.get('markdown') else result.get('analysis') or result.get('message') or ''
    if report.get('markdown') and result.get('content_review_status') == 'skipped':
        text = '报告已生成，未经过独立内容审核。\n\n' + text
    clarification = result.get('clarification') or {}
    if clarification.get('question') and clarification['question'] not in text:
        text += '\n\n' + clarification['question']
    for item in clarification.get('options') or []:
        text += '\n- ' + item.get('label','')
    # The report Agent owns the narrative and its closing limitations. Keep
    # diagnostic fields in the record instead of appending a second report footer.
    if report.get('markdown') or str(result.get('workflow_mode', '')).startswith('investigation_'):
        return text.strip()
    warnings = [*report.get('warnings', []), *((result.get('interpretation') or {}).get('assumptions') or [])]
    if result.get('stop_reason') and not report:
        warnings.append(result['stop_reason'])
    if result.get('status') == 'partial':
        warnings.insert(0,'本次分析仅部分完成，请结合已取得证据使用。')
    if warnings:
        text += '\n\n### 分析局限与待核对项\n' + '\n'.join('- '+str(w) for w in dict.fromkeys(warnings))
    return text.strip() or '本轮没有可展示的分析结论。'

def artifacts(record):
    r = record['result']; task = record.get('run_id',record['task_id']); output = []
    def add(suffix, title, kind, preview):
        output.append({'id':task+'~'+suffix, 'type':kind, 'name':title, 'title':title,
            'runId':task, 'preview_json':preview, 'preview_available':True,
            'createdAt':record.get('updated_at'), 'metadata':{'task_id':record['task_id'],'status':r.get('status'),
                'saved':r.get('saved',False)}})
    if r.get('rows'):
        add('table',r.get('result_title') or '查询结果','table',{'columns':r.get('columns',[]),'rows':r['rows'],
            'row_count':r.get('total_rows') or len(r['rows']), 'truncated':r.get('preview_truncated',False)})
    if r.get('report') or (r.get('analysis') and not str(r.get('workflow_mode', '')).startswith('investigation_')):
        add('report',(r.get('report') or {}).get('title') or '经营分析报告','markdown',{'content':result_text(r)})
    report=r.get('report') or {}
    for i,spec in enumerate(report.get('visualizations') or []):
        source=next((d for d in report.get('data_sources',[]) if d.get('result_id')==spec.get('source_result_id')),None)
        if not spec.get('source_result_id'):
            # Legacy AskData reports bind task results, not investigation result IDs.
            source=next((d for d in report.get('data_sources',[]) if d.get('task_id')==spec.get('source_task_id')),None)
            if not source and spec.get('source_task_id')==record['task_id']:
                source={'rows':r.get('rows',[])}
            if source:
                points=[]
                for row in source.get('rows',[]):
                    number,label=row.get(spec['value_field']),row.get(spec['category_field'])
                    if isinstance(number,(int,float)) and not isinstance(number,bool) and math.isfinite(number) and label is not None:
                        points.append({'label':str(label),'value':number})
                if spec.get('order')=='value_desc':points.sort(key=lambda p:p['value'],reverse=True)
                elif spec.get('order')=='category_asc':points.sort(key=lambda p:p['label'])
                if points:add('chart-'+str(i),spec['title'],'chart',{'chartType':spec['type'],'points':points[:spec.get('max_items',12)],'unit':spec.get('unit')})
            continue
        payload=chart_payload(spec,source) if source else None
        if payload:
            # The same renderer keeps axes, grouping and unit conversion consistent.
            add('chart-'+str(i),spec['title'],'chart',{'chartType':spec['type'],
                'points':payload['points'],'reportChart':encode_chart(payload),'unit':payload['unit']})
    # Query previews are session outputs, never automatically promoted to workspace files.
    for e in [*record.get('execution_events',[]),*record.get('business_updates',[])]:
        if e.get('type')=='CUSTOM' and e.get('name')=='artifact':
            a={**e['value'], 'runId':task}
            a['metadata']={**a.get('metadata',{}),'task_id':record['task_id']}
            output=[old for old in output if old['id']!=a['id']]
            output.append(a)
    return output

def custom_events(record, session_id):
    r=record['result']; run_id=record.get('run_id',record['task_id'])
    values=[('run.config.resolved', {'activeDatasourceId':record.get('workspace',{}).get('datasource_id'),
        'enabledDatasourceIds':[record.get('workspace',{}).get('datasource_id')]})]
    values.append(('insightpilot.delivery', {'runId':run_id,'status':r.get('status'),
                                           'hasReport':bool(r.get('report')), 'taskId':r.get('task_id'), 'clarification':r.get('clarification')}))
    values.extend(('insightpilot.activity', e) for e in record.get('execution_events',[]) if e.get('type')=='ACTIVITY_SNAPSHOT')
    business={}
    for e in [*record.get('execution_events',[]),*record.get('business_updates',[])]:
        if e.get('type')=='CUSTOM' and e.get('name')=='insightpilot.analysis':business[e['value']['id']]=e['value']
    # Replace legacy ungrouped snapshots with their round-aware projection.
    grouped={key.rsplit('-round-',1)[0] for key,value in business.items() if value.get('round')}
    values.extend(('insightpilot.analysis',v) for key,v in business.items() if v.get('round') or key not in grouped)
    values.extend(('artifact', a) for a in artifacts(record))
    budget=r.get('budget') or {}
    for usage in budget.get('model_usage') or []:
        counts=usage.get('usage') or {}
        if counts:
            values.append(('token_usage', {'model':usage.get('model'), 'usage':counts,
                'input_tokens':counts.get('input_tokens',counts.get('prompt_tokens',0)),
                'output_tokens':counts.get('output_tokens',counts.get('completion_tokens',0)),
                'total_tokens':counts.get('total_tokens',0)}))
    return [{'runId':run_id,'seq':i+1,'name':name,'value':value} for i,(name,value) in enumerate(values)]

def tool_calls(record):
    r=record['result']; task=record.get('run_id',record['task_id']); calls=[]
    actual={}
    for seq,e in enumerate(record.get('execution_events',[])):
        cid=e.get('toolCallId')
        if e['type']=='TOOL_CALL_START':
            actual[cid]={'runId':task,'toolCallId':cid,'name':e['toolCallName'],'status':'running','args':{},'result':None,'callEventSeq':seq,'startedAtMs':e.get('timestamp'),'_args':''}
        elif cid in actual and e['type']=='TOOL_CALL_ARGS':
            actual[cid]['_args']+=e['delta']
        elif cid in actual and e['type']=='TOOL_CALL_RESULT':
            try: result=json.loads(e['content'])
            except (ValueError,TypeError):result=e['content']
            actual[cid].update(result=result,status='failed' if isinstance(result,dict) and (result.get('error') or result.get('success') is False) else 'completed',resultEventSeq=seq,finishedAtMs=e.get('timestamp'))
    if actual:
        for call in actual.values():
            try:call['args']=json.loads(call.pop('_args'))
            except (ValueError,TypeError):call['args']={}
        return list(actual.values())
    evidence=r.get('evidence') or ([{'sql':r['sql'],'columns':r.get('columns',[]),'rows':r.get('rows',[]),'complete':r.get('complete'),'total_rows':r.get('total_rows')}] if r.get('sql') else [])
    for i,e in enumerate(evidence):
        if e.get('check_status')=='superseded':continue
        calls.append({'runId':task,'toolCallId':task+'-evidence-'+str(i), 'name':'run_sql_readonly',
            'status':'failed' if e.get('error') else 'completed',
            'args':{'sql':e.get('sql',''), 'purpose':e.get('goal','')},
            'result':{'columns':e.get('columns',[]),'rows':e.get('rows',[]),
                'row_count':e.get('total_rows') if e.get('total_rows') is not None else len(e.get('rows',[])), 'complete':e.get('complete'),
                'query_contract':e.get('query_contract')},'callEventSeq':i*3+1,'resultEventSeq':i*3+3})
    return calls

def conversation(session, records):
    messages=[]; checkpoints=[]; events=[]; calls=[]; refs=[]
    for record in records:
        task=record.get('run_id',record['task_id']); r=record['result']; pos=len(messages); stamp=record.get('updated_at') or now()
        messages.extend([
            {'id':task+'-user','messageId':task+'-user','runId':task,'role':'user','source':'client',
             'contentText':record['query'],'position':pos,'createdAt':stamp},
            {'id':task+'-answer','messageId':task+'-answer','runId':task,'role':'assistant','source':'agent',
             'contentText':result_text(r),'position':pos+1,'createdAt':stamp}])
        last_event=max((e.get('timestamp',0) for e in record.get('execution_events',[])),default=0)
        finished=record.get('finished_at') or (datetime.fromtimestamp(last_event/1000,timezone.utc).isoformat() if last_event else stamp)
        status='canceled' if record.get('job_status') in {'stopped','interrupted'} else ('failed' if r.get('status')=='failed' else 'completed')
        checkpoints.append({'runId':task,'status':status,'messageStartPosition':pos,'messageEndPosition':pos+1,
            'startedAt':stamp,'finishedAt':finished,'terminalEvent':'RUN_ERROR' if status=='failed' else 'RUN_FINISHED',
            'errorMessage':r.get('message') if status=='failed' else None,
            'artifactIds':[a['id'] for a in artifacts(record)]})
        events.extend(custom_events(record,session['id']));calls.extend(tool_calls(record))
        refs.append({'runId':task,'eventCount':len(tool_calls(record))*3+2})
    return {'sessionId':session['id'],'title':session.get('title'), 'messages':messages, 'checkpoints':checkpoints,
        'toolCalls':calls,'runEventRefs':refs,'restorableCustomEvents':events, 'branches':[],
        'activeRun':None}

def event(event_type, **value):
    return {'type':event_type,**value}

def encode_event(value):
    return 'data: '+json.dumps(value,ensure_ascii=False,default=str)+'\n\n'

def delivery_events(record, session_id, run_id, *, answer_started=False):
    # Real MCP events already streamed before delivery. Legacy results still project SQL evidence.
    live_tools=any(e['type']=='TOOL_CALL_START' for e in record.get('execution_events',[]))
    for call in ([] if live_tools else tool_calls(record)):
        call_id=call['toolCallId']
        yield event('TOOL_CALL_START',toolCallId=call_id,toolCallName=call['name'])
        yield event('TOOL_CALL_ARGS',toolCallId=call_id,delta=json.dumps(call['args'],ensure_ascii=False))
        yield event('TOOL_CALL_END',toolCallId=call_id)
        yield event('TOOL_CALL_RESULT',toolCallId=call_id,messageId=call_id+'-result',content=json.dumps(call['result'],ensure_ascii=False,default=str))
    for custom in custom_events(record,session_id):
        yield event('CUSTOM',name=custom['name'],value=custom['value'])
    mid=record.get('run_id',record['task_id'])+'-answer'
    if not answer_started:
        yield event('TEXT_MESSAGE_START',messageId=mid,role='assistant')
    yield event('TEXT_MESSAGE_CONTENT',messageId=mid,delta=result_text(record['result']))
    yield event('TEXT_MESSAGE_END',messageId=mid)


def tool_label(name):
    if name.startswith('query_') or name=='run_sql_readonly':return '查询经营数据'
    return {'current_datetime':'获取当前时间','resolve_date_range':'确认比较日期',
        'build_bar_chart':'生成柱状图','build_pie_chart':'生成占比图','inspect_schema':'检查数据结构'}.get(name,name)


def trace_dag(session,records):
    nodes=[];edges=[];sections=[];previous=None
    for position,record in enumerate(records):
        run=record.get('run_id',record['task_id']);stamp=record.get('updated_at');uid=run+'-user';start=run+'-start'
        common={'runId':run,'sessionId':session['id'],'createdAt':stamp}
        nodes.extend([{**common,'id':uid,'kind':'user-turn','label':record['query'],'messagePosition':position*2},
            {**common,'id':start,'kind':'run-start','label':'开始分析'}])
        section_ids=[uid,start]
        if previous:edges.append({'id':previous+'-'+uid,'source':previous,'target':uid,'kind':'continues_to'})
        edges.append({'id':uid+'-'+start,'source':uid,'target':start,'kind':'starts_run'})
        last=start;steps=[];phases={}
        for seq,e in enumerate(record.get('execution_events',[])):
            if e['type']=='ACTIVITY_SNAPSHOT' and e.get('content',{}).get('phase'):
                c=e['content'];first=phases.get(c['step_id'],{}).get('eventSeq',seq)
                phases[c['step_id']]={**common,'id':c['step_id'],'kind':'context','label':c['title'],
                    'status':c['status'],'eventSeq':first,'detail':{'type':'context','assistantOutput':c['title']}}
        steps.extend(phases.values())
        for call in tool_calls(record):
            steps.append({**common,'id':call['toolCallId'],'kind':'tool','label':tool_label(call['name']),
                'status':call['status'],'eventSeq':call.get('callEventSeq',0),'toolCallId':call['toolCallId'],
                'detail':{'type':'tool','toolName':call['name'],'arguments':call['args'],'result':call['result']}})
        for node in sorted(steps,key=lambda n:n['eventSeq']):
            nodes.append(node);section_ids.append(node['id'])
            edges.append({'id':last+'-'+node['id'],'source':last,'target':node['id'],'kind':'emits'});last=node['id']
        active=record.get('job_status') in {'running','stopping'}
        canceled=record.get('job_status') in {'stopped','stopping','interrupted'}
        status='canceled' if canceled else record['result'].get('status')
        if not active:
            terminal=run+'-terminal';nodes.append({**common,'id':terminal,'kind':'run-terminal',
                'label':{'canceled':'已停止分析','partial':'部分完成','waiting_clarification':'等待确认','failed':'分析失败'}.get(status,'分析完成'),
                'status':status,'checkpointId':'checkpoint-'+run,'checkpointKind':'run-terminal',
                'checkpointStatus':'terminal','rollbackable':True,'messagePosition':position*2+1,
                'detail':{'type':'terminal','message':record['result'].get('message')}})
            edges.append({'id':last+'-'+terminal,'source':last,'target':terminal,'kind':'emits'});last=terminal;section_ids.append(terminal)
        for artifact in artifacts(record):
            nodes.append({**common,'id':artifact['id'],'kind':'artifact','label':artifact['name'],'artifactId':artifact['id'],
                'detail':{'type':'artifact','artifactType':artifact['type'],'name':artifact['name'],'preview':artifact['preview_json']}})
            edges.append({'id':last+'-'+artifact['id'],'source':last,'target':artifact['id'],'kind':'produces_artifact'});section_ids.append(artifact['id'])
        sections.append({'id':run+'-section','runId':run,'phaseKey':'analysis','status':'in-progress' if active else 'completed',
            'title':record['query'],'summary':record.get('stage') if active else ('已停止，已完成的依据保留' if canceled else '查看本轮分析步骤与依据'),
            'startEventSeq':0,'endEventSeq':len(record.get('execution_events',[])), 'nodeIds':section_ids})
        previous=last
    return {'sessionId':session['id'],'nodes':nodes,'edges':edges,'sections':sections}
