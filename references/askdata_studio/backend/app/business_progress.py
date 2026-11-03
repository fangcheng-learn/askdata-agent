"""User-facing, evidence-linked progress. Never expose private model deliberation."""
from __future__ import annotations
import json
from hashlib import sha256


def public_issue(message):
    message=str(message)
    if 'text_edits' in message and '数字' in message:
        limit=message.split('；')[0]+'。' if '上限' in message.split('；')[0] else ''
        return limit+'最终审核未通过：修改建议涉及数字或单位，必须重新核对证据。当前发现仍是阶段性结果，尚未形成通过审核的完整报告。'
    if 'timed out' in message or 'TimeoutError' in message:
        return '模型或数据请求超时，本轮分析未能继续完成。'
    return message


def query_table(result: dict, key: str, *, title='查询结果', status='待核对', task_id=None):
    if result.get('success') is False or result.get('error') or not isinstance(result.get('rows'), list) or not result.get('columns'):
        return None
    rows=result['rows'][:20]
    total=result.get('total_rows',result.get('row_count'))
    truncated=bool(result.get('preview_truncated') or result.get('display_truncated') or len(result['rows'])>20 or (isinstance(total,int) and total>len(rows)))
    boundary='结果未完整返回' if result.get('complete') is False else ('展示前20行' if truncated else '完整预览')
    return {'id':'query-'+str(result.get('result_ref') or key),'type':'table','title':title,'name':title,
        'summary':f'{status} · {"共 "+str(total)+" 行" if total is not None else "已返回 "+str(len(rows))+" 行"} · {boundary}',
        'preview_available':False,'preview_json':{'columns':result['columns'],'rows':rows,'row_count':total,'truncated':truncated},
        'metadata':{'task_id':task_id,'validation':status,'intermediate':True}}


def round_index(task):
    goals={};notes={};last=1
    for log in task.get('logs',[]):
        if log.get('stage')!='react_decision':continue
        number=int(log.get('revision',0))+1;last=max(last,number)
        action=log.get('action') or {}
        for goal in [log.get('action_reason'),action.get('goal'),*[q.get('goal') for q in action.get('queries',[]) if isinstance(q,dict)]]:
            if goal:goals.setdefault(goal,number)
        progress=log.get('progress_update') or {}
        if not isinstance(progress,dict):continue
        for category in ('facts','hypotheses','explanations','core_questions'):
            for note in progress.get(category) or []:
                if isinstance(note,dict):
                    notes.setdefault((category,str(note.get('id') or note.get('text')),note.get('text') or note.get('question'),note.get('status')),number)
                    notes.setdefault((category,note.get('text') or note.get('question')),number)
    actions={a['id']:a.get('business_round') or goals.get(a.get('goal')) or a.get('evidence_round') or 1 for a in task.get('actions',[])}
    return actions,notes,last


def snapshot_updates(task: dict):
    """Project saved goals, validated facts and issues; no LLM calls or raw thoughts."""
    tid=task['state']['task_id']; updates=[]
    action_rounds,note_rounds,last_round=round_index(task)
    def card(key,kind,title,body,status='completed',refs=None,round_number=1):
        if not body:return
        updates.append({'type':'CUSTOM','name':'insightpilot.analysis','value':{
            'id':tid+'-'+key+'-round-'+str(round_number),'round':round_number,'kind':kind,'title':title,'body':str(body),'status':status,'evidenceIds':refs or []}})
    for a in task.get('actions',[]):
        kind=a.get('action_type'); status=a.get('status','running')
        title={'query':'查证数据','calculate':'计算与比较','read_result':'查看已有结果','load_skill':'确定分析方法',
               'resolve_time':'确认比较时间','retrieve':'核对指标口径'}.get(kind,'推进分析')
        card('action-'+a['id'],'action',title,a.get('goal'),status,a.get('result_ids'),action_rounds[a['id']])
        issue = a.get('error') or (a.get('last_error') or (a.get('last_generation_error') or {}).get('message') if status == 'completed' else None)
        if issue:
            card('issue-'+a['id'],'issue','遇到的问题',public_issue(issue),'resolved' if status=='completed' else 'open',round_number=action_rounds[a['id']])
    for e in task.get('evidence',[]):
        status={'basic_valid':'已通过基础校验','reviewed':'已审核','superseded':'已被新结果替代'}.get(e.get('check_status'),'待核对')
        artifact=query_table(e,e['id'],title=e.get('goal') or '查询结果',status=status,task_id=tid)
        if artifact:updates.append({'type':'CUSTOM','name':'artifact','value':artifact})
        card('result-'+e['id'],'result','拿到的数据',f'{e.get("goal") or "查询结果"}：返回 {e.get("total_rows") if e.get("total_rows") is not None else len(e.get("rows",[]))} 行，{status}。可在右侧查看结果表。',e.get('check_status','pending'),[e['id']],action_rounds.get(e.get('step_id'),1))
    progress=task.get('progress') or {}
    for kind,title,items in [('finding','已核实的发现',progress.get('facts',[])),('hypothesis','待验证的解释',progress.get('hypotheses',[])),('explanation','当前分析判断',progress.get('explanations',[]))]:
        for i,item in enumerate(items):
            if not isinstance(item,dict):continue
            refs=item.get('evidence_ids') or item.get('supporting_evidence_ids') or []
            # Facts are emitted only after the existing progress validator commits them.
            if kind=='finding' and not refs:continue
            note_title=title
            note_status=item.get('status','supported' if kind=='finding' else 'open')
            if kind=='finding':
                valid={e['id'] for e in task.get('evidence',[]) if e.get('check_status') in {'basic_valid','reviewed'}}
                if item.get('status') in {'invalidated','superseded','rejected','refuted','retracted','removed'}:note_title='已撤回的发现'
                elif not set(refs)<=valid:note_title='待重新核对的发现';note_status='open'
            category={'finding':'facts','hypothesis':'hypotheses','explanation':'explanations'}[kind]
            number=task.get('business_note_rounds',{}).get(category+':'+str(item.get('id') or item.get('text'))) or note_rounds.get((category,str(item.get('id') or item.get('text')),item.get('text'),item.get('status'))) or note_rounds.get((category,item.get('text')),1)
            card(kind+'-'+str(item.get('id',i)),kind,note_title,item.get('text'),note_status,refs,number)
    for q in progress.get('core_questions',[]):
        issue_round = (task.get('business_issue_rounds') or {}).get(q['id'])
        if q.get('status') == 'blocked' or issue_round is not None:
            resolved = q.get('status') in {'answered', 'not_applicable'}
            card('question-'+q['id'],'issue','已解决' if resolved else '尚待解决',
                 q['question']+'；'+str(q.get('reason') or ''), 'resolved' if resolved else 'open',
                 round_number=issue_round or last_round)
    # Surface actual failed review/budget/clarification outcomes, not a fake completion.
    if task.get('stop_reason'):card('stop','issue','本轮分析的限制',public_issue(task['stop_reason']),'open',round_number=last_round)
    clarification=task.get('clarification') or {}
    if clarification.get('question'):card('clarification','issue','需要确认',clarification['question'],'open',round_number=last_round)
    return updates


def emit_snapshot(task: dict):
    from .workbench_runtime import current_execution, emit_execution_event
    control=current_execution.get()
    if not control:return
    for update in snapshot_updates(task):
        value=update['value'];key=update['name']+':'+value['id']
        signature=sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
        if control.business_signatures.get(key)==signature:continue
        control.business_signatures[key]=signature
        emit_execution_event(update)


def emit_plan(task_id: str, action: dict, round_number: int):
    from .workbench_runtime import emit_execution_event
    targets=[q.get('goal') or q.get('question') for q in action.get('queries',[]) if isinstance(q,dict)]
    text=action.get('goal') or '；'.join(t for t in targets if t)
    if action.get('type')=='deliver':text='整理已验证的发现，并检查结论是否回答了原问题。'
    if text:emit_execution_event({'type':'CUSTOM','name':'insightpilot.analysis','value':{
        'id':task_id+'-plan-'+str(round_number),'round':round_number,'kind':'plan','title':'接下来','body':text,'status':'planned','evidenceIds':[]}})
