from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import secrets
from urllib.parse import unquote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..api.routes import (service, auth_service, workbench_runs, workbench_options,
                          start_workbench_run, WorkbenchRequest)
from ..config import BASE_DIR
from ..models import QueryResult
from ..security import public_demo
from .store import UIStore
from .projection import (now, artifacts, conversation, event, encode_event, delivery_events, trace_dag)

router = APIRouter(prefix='/api/foundry')
store = UIStore(BASE_DIR / 'data' / 'foundry_ui.sqlite3')
ACTIVE = {'running','stopping'}
CAPABILITIES = {key: False for key in ('artifact.promote','chat.fileUpload','chat.imageInput',
    'conversation.memory','datasource.fieldMasking','datasource.extendedTypes',
    'datasource.introspectionPolicy','datasource.queryPolicy','datasource.samplePolicy',
    'datasource.server','kb.chunking','kb.citationPolicy','kb.scope','llm.advancedSampling',
    'mcp.stdio','mcp.toolPolicy','skill.resourceBinding','llm.samplingParams','knowledge','files')}
CAPABILITIES.update({'artifact.export':True,'artifact.list':True,'conversation.title':True,
    'interaction.resume':True,'conversation.memory':True,'mcp':True,'skills':True})


def success(value, **kwargs):
    return JSONResponse({'success':True,'data':value},**kwargs)


def error_response(exc):
    if isinstance(exc, HTTPException):
        status=exc.status_code; message=str(exc.detail)
    elif isinstance(exc, PermissionError):
        status=403;message='无权访问此资源'
    elif isinstance(exc, KeyError):
        status=404;message='资源不存在或不可访问'
    elif isinstance(exc, ValueError):
        status=422;message=str(exc)
    else:
        raise exc
    code={401:'UNAUTHORIZED',403:'FORBIDDEN',404:'RESOURCE_NOT_FOUND',409:'CONFLICT',501:'NOT_ENABLED'}.get(status,'BAD_REQUEST')
    return JSONResponse({'success':False,'error':{'code':code,'message':message}},status_code=status)


def identity(user):
    return {'user':{'id':user.user_id,'displayName':user.display_name,'email':user.username},
            'workspace':{'id':'insightpilot-'+user.user_id,'name':'InsightPilot · 经营分析'}}


def authenticate(request, *, mutation=False):
    authorization=request.headers.get('authorization')
    if not authorization:
        token=request.cookies.get('ip_session')
        authorization='Bearer '+token if token else None
        if mutation:
            csrf=request.cookies.get('ip_csrf',''); supplied=request.headers.get('x-csrf-token','')
            if not csrf or not secrets.compare_digest(csrf,supplied):
                raise HTTPException(403,'会话校验失败，请刷新后重试')
    user=auth_service.authenticate(authorization)
    if not user:raise HTTPException(401,'请登录经营分析账号')
    if mutation:
        origin=request.headers.get('origin')
        if origin and origin not in public_demo.allowed_origins():
            raise HTTPException(403,'请求来源不受信任')
    return user


def sources(user):
    return service.list_datasources(user.user_id)


def check_source(user, source):
    if source not in {s['id'] for s in sources(user)}:raise HTTPException(403,'无权访问此数据源')
    service.visible_schema(user.user_id,source)
    return source


def ui_session_id(source, session):
    return 'history-'+hashlib.sha256((source+'\0'+session).encode()).hexdigest()[:32]


def import_history(user):
    loaded={}
    for source in sources(user):
        loaded[source['id']]=service.list_history_summaries(user.user_id,source['id'],100)
        for record in loaded[source['id']]:
            # Runs created through this UI already have a binding; preserve their native thread id.
            known=store.get(user.user_id,'binding',source['id']+'\0'+record['session_id'])
            sid=known['id'] if known else ui_session_id(source['id'],record['session_id'])
            previous=store.get(user.user_id,'session',sid)
            if not previous:
                stamp=record.get('updated_at') or now()
                store.put(user.user_id,'session',sid,{'id':sid,'threadId':sid,
                    'source':source['id'],'backendSession':record['session_id'],
                    'title':record['query'][:50],'createdAt':stamp,'updatedAt':stamp})
    return loaded


def get_session(user, sid):
    result=store.get(user.user_id,'session',sid)
    if not result:
        import_history(user);result=store.get(user.user_id,'session',sid)
    if not result or result.get('hidden'):raise HTTPException(404,'会话不存在或不可访问')
    check_source(user,result['source'])
    return result


def records_for(user, session):
    source=session['source']; check_source(user,source)
    inherited=set(session.get('inheritedTasks') or [])
    all_records=service.list_history(user.user_id,source,100,session_ids={session['backendSession']},task_ids=inherited)
    records={r['task_id']:r for r in all_records if r['session_id']==session['backendSession'] or r['task_id'] in inherited}
    # Include completed asynchronous runs even if the original task archive has not flushed yet.
    run_links={r['jobId']:r['id'] for r in store.list(user.user_id,'run')}
    for job in workbench_runs.list(user.user_id,source,session['backendSession']):
        if job['session_id']!=session['backendSession']:continue
        if not job.get('result'):
            run_id=run_links.get(job['id'])
            if run_id and job['status'] not in ACTIVE:
                records[run_id]={'task_id':run_id,'run_id':run_id,'query':job['query'],'workspace':job['workspace'],
                    'updated_at':job['created_at'],'job_status':job['status'],'execution_events':job.get('events',[]),
                    'result':{'status':'failed','message':job.get('error') or '本次分析已停止，没有生成结论。'}}
            continue
        result=job['result'];task=result['task_id']
        run_id=run_links.get(job['id'])
        if run_id or task not in records:
            # Recheck original task authorization before exposing its result.
            try:service.get_task(task,user.user_id)
            except (PermissionError,KeyError):continue
            if run_id:records.pop(task,None)
            records[run_id or task]={'task_id':task,'run_id':run_id or task,'session_id':job['session_id'],'query':job['query'],
                'workspace':job['workspace'],'result':result,'job_status':job['status'],'updated_at':job['created_at'], 'execution_events':job.get('events',[]),'finished_at':job.get('finished_at')}
    # Existing task access has been checked above; project its persisted business notes.
    from ..business_progress import snapshot_updates
    harness=getattr(service.workflow,'harness',None)
    for record in records.values():
        task=getattr(harness,'records',{}).get(record['task_id'])
        if task and hasattr(task,'progress'):
            record['business_updates']=snapshot_updates({key:getattr(task,key) for key in ('state','actions','progress','evidence','stop_reason','clarification','logs','business_note_rounds')})
    return sorted(records.values(),key=lambda r:str(r.get('updated_at') or ''))


def active_run(user, session):
    for link in store.list(user.user_id,'run'):
        if link['sessionId']!=session['id']:continue
        try:job=workbench_runs.read(link['jobId'],user.user_id,include_result=False)
        except KeyError:continue
        if job['status']=='running':
            return {'sessionId':session['id'],'activeRunId':link['id'],'status':'running',
                'startedAt':job['created_at'],'userInputPreview':job['query']}
    return None


def workspace_config(user):
    ds=sources(user)
    options=workbench_options(ds[0]['id'],user) if ds else {'models':[],'skills':[],'tools':[]}
    # Tools may differ by datasource. Keep stable identifiers and validate against the selected scope at run time.
    tools={t['name']:t for d in ds for t in service.list_mcp_tools(user.user_id,d['id'])}
    return {'datasources':[{'id':s['id'],'name':s.get('label',s['id']),
            'description':s.get('description','经营分析已配置的数据源'), 'type':'duckdb',
            'defaultEnabled':i==0,'builtin':True,'revision':1,'config':{}, 'connectionStatus':(store.get(user.user_id,'health',s['id']) or {}).get('status','unknown')} for i,s in enumerate(ds)],
        'knowledgeBases':[],
        'mcpServers':[{'id':name,'name':name,'description':t.get('description',''),
            'transport':'builtin','toolManifest':[t],'defaultEnabled':True,'builtin':True,'revision':1} for name,t in tools.items()],
        'modelProfiles':[{'id':m,'name':m,'modelName':m,'provider':'openai-compatible',
            'description':'经营分析服务已配置的模型','defaultEnabled':True,'builtin':True,'hasSecret':bool(service.config.api_key),'revision':1} for i,m in enumerate(options['models'])],
        'skills':[{'id':s['name'],'name':s['name'],'description':s.get('description',''),
            'defaultEnabled':s.get('enabled',True),'builtin':True,'revision':1,
            'manifest':{'skillMd':s.get('instructions','')}} for s in options['skills']]}


def defaults(user):
    config=workspace_config(user)
    return {'enabledDatasourceIds':[d['id'] for d in config['datasources'][:1]],'enabledKnowledgeIds':[],
        'enabledMcpServerIds':[d['id'] for d in config['mcpServers']],
        'enabledSkillIds':[d['id'] for d in config['skills'] if d['defaultEnabled']],
        'activeDatasourceId':config['datasources'][0]['id'] if config['datasources'] else None,
        'activeLlmProfileId':service.config.llm_model,'activeSkillId':'database_query'}


def schema(user, source):
    check_source(user,source)
    return {'datasourceId':source,'tables':[{'name':t.get('name',t['id']), 'table':t.get('name',t['id']),
        'description':t.get('description',''),'sampleAvailable':True,
        'columns':[{'name':c['name'],'type':c.get('type',c.get('field_type','')),
                    'description':c.get('description',c.get('label',''))} for c in t.get('fields',t.get('columns',[]))]}
        for t in service.visible_schema(user.user_id,source)]}


def find_artifact(user, artifact_id):
    for session in store.list(user.user_id,'session'):
        if session.get('hidden'):continue
        try:records=records_for(user,session)
        except (PermissionError,HTTPException):continue
        for record in records:
            for artifact in artifacts(record):
                if artifact['id']==artifact_id:return artifact
    raise HTTPException(404,'产物不存在或不可访问')


def dispatch(request, path, body, user):
    owner=user.user_id;method=request.method;parts=[unquote(p) for p in path.split('/')];query=request.query_params
    if method=='GET':
        if path=='me':return identity(user)
        if path=='capabilities':return CAPABILITIES
        if path=='workspace-config':return workspace_config(user)
        if path=='run-defaults':return defaults(user)
        if path=='datasource-types':return [{'name':'duckdb','label':'DuckDB','enabled':True,'parameters':[]}]
        if path=='sessions':
            loaded=import_history(user);allowed=set(loaded)
            latest={}
            for source,records in loaded.items():
                for record in records:
                    key=(source,record['session_id'])
                    if key not in latest:latest[key]=record
            items=[]
            for session in store.list(owner,'session'):
                if session.get('hidden') or session['source'] not in allowed:continue
                last=latest.get((session['source'],session['backendSession']))
                items.append({k:v for k,v in {**session,'lastMessageAt':last.get('updated_at') if last else session['createdAt'],
                    'activeRun':active_run(user,session), 'config':session.get('config') or {'disabled':{'db':[d for d in allowed if d!=session['source']],'kb':[],'mcp':[],'skill':[]}}}.items() if k in {'id','threadId','title','createdAt','updatedAt','lastMessageAt','activeRun','config'}})
            items.sort(key=lambda s:s.get('lastMessageAt') or '',reverse=True)
            offset=max(0,int(query.get('cursor','0')));limit=min(100,max(1,int(query.get('limit','50'))))
            return {'sessions':items[offset:offset+limit],'nextCursor':str(offset+limit) if len(items)>offset+limit else None}
        if parts[0]=='sessions' and len(parts)>=3:
            if parts[2]=='conversation' and not store.get(owner,'session',parts[1]) and not parts[1].startswith('history-'):
                return conversation({'id':parts[1]},[])
            if parts[2]=='active-run':
                if not store.get(owner,'session',parts[1]):return None
                return active_run(user,get_session(user,parts[1]))
            session=get_session(user,parts[1]);records=records_for(user,session)
            if parts[2]=='conversation':
                result=conversation(session,records);result['activeRun']=active_run(user,session)
                if session.get('branch'):result['branch']=session['branch']
                result['branches']=[s['branch'] | {'id':s['id'],'session':{'id':s['id'],'threadId':s['id'],'title':s['title']}}
                    for s in store.list(owner,'session') if s.get('branch',{}).get('parentSessionId')==session['id'] and not s.get('hidden')]
                return result
            if parts[2]=='trace-dag':
                for link in store.list(owner,'run'):
                    if link['sessionId']!=session['id']:continue
                    job=workbench_runs.read_progress(link['jobId'],owner)
                    if job['status'] not in ACTIVE:continue
                    records.append({'task_id':link['id'],'run_id':link['id'],'query':job['query'],
                        'updated_at':job['created_at'],'result':{'status':'running'},'job_status':job['status'],
                        'execution_events':job['events'],'stage':job['stage']})
                return trace_dag(session,records)
        if parts[0]=='datasources' and len(parts)>=3:
            source=parts[1];check_source(user,source)
            if parts[2]=='schema':return schema(user,source)
            if parts[2]=='queries':return {'queries':[]}
            if parts[2]=='tables' and len(parts)>=5 and parts[4]=='preview':
                table=next((t for t in service.visible_schema(owner,source) if parts[3] in {t['id'],t.get('name')}),None)
                if not table:raise HTTPException(404,'数据表不可访问')
                limit=min(200,max(1,int(query.get('limit','50'))));offset=max(0,int(query.get('offset','0')))
                name=table.get('name') or table['id'].split('.')[-1]
                sql='SELECT * FROM "'+name.replace('"','""')+'" LIMIT '+str(limit+1)+' OFFSET '+str(offset)
                scope=service.access_controller.scoped(owner,source)
                result=service.workflow.database_engine.execute(source,sql,scope)
                if not result.success:raise HTTPException(422,result.error or '读取失败')
                return {'columns':[{'name':c} for c in result.columns],'rows':result.rows[:limit],
                    'hasMore':len(result.rows)>limit}
        if parts[0]=='mcp-servers' and len(parts)==3 and parts[2]=='tools':
            match=next((t for t in workspace_config(user)['mcpServers'] if t['id']==parts[1]),None)
            if not match:raise HTTPException(404,'工具不存在')
            return {'tools':match['toolManifest']}
        if path=='datalink/servers':return {'servers':[]}
        if path=='files':return {'files':[]}
        if path=='artifacts':
            sid=query.get('sessionId','')
            if not store.get(owner,'session',sid):return {'artifacts':[]}
            session=get_session(user,sid)
            return {'artifacts':[a for r in records_for(user,session) for a in artifacts(r)]}
        if parts[0]=='artifacts' and len(parts)>=2:
            a=find_artifact(user,parts[1])
            if len(parts)==2:return a
            if parts[2]=='preview':return a['preview_json']
            if parts[2]=='versions':return {'versions':[]}
            if parts[2]=='download':
                preview=a['preview_json'];fmt=query.get('format')
                if a['type']=='table':
                    if fmt=='xlsx':
                        from .xlsx_export import workbook_bytes
                        return Response(workbook_bytes(preview['columns'],preview['rows']),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            headers={'Content-Disposition':f'attachment; filename="analysis-{parts[1]}.xlsx"'})
                    if fmt not in {None,'csv'}:raise HTTPException(422,'表格支持 CSV/XLSX 导出')
                    buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=preview['columns'],extrasaction='ignore');w.writeheader();w.writerows(preview['rows'])
                    content='\ufeff'+buf.getvalue();mime='text/csv';extension='csv'
                elif a['type']=='chart':
                    content=json.dumps(preview,ensure_ascii=False);mime='application/json';extension='json'
                else:
                    if fmt not in {None,'markdown','md'}:raise HTTPException(422,'报告支持 Markdown 导出')
                    content=preview['content'];mime='text/markdown';extension='md'
                return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="analysis-{parts[1]}.{extension}"'})
    if method=='PATCH' and parts[0]=='sessions' and len(parts)==2:
        session=get_session(user,parts[1]);title=str(body.get('title','')).strip()
        if not title or len(title)>120:raise HTTPException(422,'标题需要 1–120 个字符')
        session.update(title=title,updatedAt=now());store.put(owner,'session',session['id'],session)
        return {'id':session['id'],'title':title,'updatedAt':session['updatedAt']}
    if method=='DELETE' and parts[0]=='sessions' and len(parts)==2:
        session=get_session(user,parts[1])
        if active_run(user,session):raise HTTPException(409,'请先停止正在运行的分析')
        service.hide_history(owner,session['source'],session['backendSession'])
        workbench_runs.hide_session(owner,session['source'],session['backendSession'])
        session['hidden']=True;store.put(owner,'session',session['id'],session)
        return {'sessionId':session['id'],'deleted':True,'deletedSessionIds':[session['id']]}
    if method=='POST' and parts[0]=='sessions' and len(parts)==3 and parts[2]=='branches':
        parent=get_session(user,parts[1]);records=records_for(user,parent)
        run=body.get('runId') or str(body.get('checkpointId','')).removeprefix('checkpoint-')
        if run:
            cutoff=next((i for i,r in enumerate(records) if (r.get('run_id',r['task_id'])==run or r['task_id']==run)),None)
            if cutoff is None:raise HTTPException(404,'检查点不存在')
            records=records[:cutoff+1]
        if not records:raise HTTPException(409,'本会话还没有可分支的分析')
        sid=str(uuid4());stamp=now();branch={'sessionId':sid,'threadId':sid,'parentSessionId':parent['id'],
            'rootSessionId':parent.get('branch',{}).get('rootSessionId',parent['id']),
            'forkRunId':records[-1]['task_id'],'forkMessageEndPosition':len(records)*2-1,'createdAt':stamp}
        session={'id':sid,'threadId':sid,'source':parent['source'],'backendSession':sid,
            'title':body.get('title') or parent['title']+' · 分支','createdAt':stamp,'updatedAt':stamp,
            'inheritedTasks':[r['task_id'] for r in records],'branch':branch,'config':parent.get('config') or {'disabled':{'db':[d['id'] for d in sources(user) if d['id']!=parent['source']],'kb':[],'mcp':[],'skill':[]}}}
        store.put(owner,'session',sid,session);store.put(owner,'binding',parent['source']+'\0'+sid,{'id':sid})
        return {'session':session,'branch':branch}
    if method=='POST' and parts[0]=='runs' and len(parts)==3 and parts[2]=='cancel':
        link=store.get(owner,'run',parts[1])
        if not link:raise HTTPException(404,'运行不存在')
        get_session(user,link['sessionId']);job=workbench_runs.cancel(link['jobId'],owner)
        return {'canceled':job['status'] in {'stopping','stopped','cancelled','canceled'},'runId':parts[1],
            'sessionId':link['sessionId'],'reason':'当前调用返回后停止后续分析'}
    if method=='POST' and len(parts)==3 and parts[2] in {'test','validate'}:
        if parts[0]=='datasources':
            source=check_source(user,parts[1])
            with service.workflow.database_engine.connect(source) as db:db.execute('SELECT 1').fetchone()
            store.put(owner,'health',source,{'status':'connected','testedAt':now()})
            return {'success':True,'status':'connected','message':'本地数据连接正常'}
        if parts[0]=='mcp-servers':
            if parts[1] not in {t['id'] for t in workspace_config(user)['mcpServers']}:raise HTTPException(404,'工具不存在')
            return {'success':True,'status':'ready','message':'工具已在经营分析服务注册'}
        if parts[0]=='skills':
            if parts[1] not in {s['name'] for s in service.list_skills()}:raise HTTPException(404,'Skill 不存在')
            return {'valid':True,'success':True,'status':'valid','message':'Skill 已加载'}
    raise HTTPException(501,'此资源由经营分析服务配置管理；当前工作台支持选择与查看，暂不支持此操作')


@router.api_route('/v1/{path:path}',methods=['GET','POST','PATCH','PUT','DELETE'])
async def rest(request:Request,path:str):
    try:
        raw=await request.body()
        body=json.loads(raw) if raw and request.headers.get('content-type','').startswith('application/json') else {}
        if path=='auth/status':return success({'registrationEnabled':False,
            'publicBaseUrl':public_demo.public_origin(),'publicDemo':public_demo.enabled()})
        if path=='auth/guest' and request.method=='POST':
            if not public_demo.enabled():raise HTTPException(404,'访客试用未启用')
            if request.headers.get('origin') not in public_demo.allowed_origins():
                raise HTTPException(403,'请求来源不受信任')
            token,user=auth_service.guest_login(request.cookies.get('ip_session'))
            response=success(identity(user))
            response.set_cookie('ip_session',token,httponly=True,secure=True,
                                samesite='lax',path='/',max_age=30*86400)
            response.set_cookie('ip_csrf',secrets.token_urlsafe(24),secure=True,
                                samesite='lax',path='/',max_age=30*86400)
            return response
        if path=='auth/login' and request.method=='POST':
            origin=request.headers.get('origin')
            if origin and origin not in public_demo.allowed_origins():raise HTTPException(403,'请求来源不受信任')
            login=auth_service.login(str(body.get('email') or body.get('username') or ''),str(body.get('password') or ''))
            if not login:raise HTTPException(401,'账号或密码不正确')
            token,user=login;response=success(identity(user))
            response.set_cookie('ip_session',token,httponly=True,samesite='lax',path='/')
            response.set_cookie('ip_csrf',secrets.token_urlsafe(24),samesite='lax',path='/')
            return response
        user=authenticate(request,mutation=request.method!='GET')
        if path=='auth/logout' and request.method=='POST':
            auth_service.logout(request.headers.get('authorization') or 'Bearer '+request.cookies.get('ip_session',''))
            response=success({'loggedOut':True});response.delete_cookie('ip_session');response.delete_cookie('ip_csrf');return response
        result=await run_in_threadpool(dispatch,request,path,body,user)
        return result if isinstance(result,Response) else success(result)
    except (HTTPException,PermissionError,KeyError,ValueError) as exc:return error_response(exc)


def prepare_run(user, body):
    with store.lock:
        return _prepare_run(user,body)

def _prepare_run(user, body):
    sid=str(body.get('threadId') or '')
    run_id=str(body.get('runId') or '')
    if not re.fullmatch(r'[\w:.-]{1,160}',sid) or not re.fullmatch(r'[\w:.-]{1,160}',run_id):
        raise HTTPException(422,'无效会话或运行编号')
    fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    existing=store.get(user.user_id,'run',run_id)
    if existing:
        if existing.get('requestHash') and existing['requestHash']!=fingerprint:raise HTTPException(409,'运行编号已用于另一请求，请重新发送')
        if existing['sessionId']!=sid:raise HTTPException(409,'运行编号与会话不匹配')
        get_session(user,sid);return existing
    props=body.get('forwardedProps') or {};config=props.get('run_config') or (body.get('state') or {}).get('run_config') or {}
    selected=config.get('enabledDatasourceIds') or []
    source=config.get('activeDatasourceId') or props.get('datasourceId') or (selected[0] if selected else None)
    if not source:raise HTTPException(422,'请在输入框下方选择数据源')
    check_source(user,source)
    if len(selected)>1:raise HTTPException(422,'当前经营分析一次使用一个数据源，请保留一个数据源后重试')
    messages=[m for m in body.get('messages',[]) if m.get('role')=='user']
    query=messages[-1].get('content','') if messages else ''
    if not isinstance(query,str):query=''.join(p.get('text','') for p in query if isinstance(p,dict))
    query=query.strip()
    if not query or len(query)>500:raise HTTPException(422,'请输入 1–500 字的经营分析问题')
    session=store.get(user.user_id,'session',sid)
    if session:
        session=get_session(user,sid)
        if session['source']!=source:raise HTTPException(409,'会话已绑定数据源，请新建会话后切换')
    else:
        session={'id':sid,'threadId':sid,'source':source,'backendSession':sid,'title':query[:50],'createdAt':now(),'updatedAt':now()}
    available=workbench_options(source,user)
    tool_ids=config.get('enabledMcpServerIds')
    if tool_ids is not None:
        known={t['id'] for t in workspace_config(user)['mcpServers']}
        if not set(tool_ids).issubset(known):raise HTTPException(422,'包含不可用工具')
        tool_ids=[t for t in tool_ids if t in {t['name'] for t in available['tools']}]
    workspace={'datasource_id':source,'analysis_table_ids':session.get('inheritedTasks',[])}
    for ref in config.get('evidenceRefs') or []:
        artifact_id=(ref.get('source') or {}).get('artifactId')
        if not artifact_id:raise HTTPException(422,'当前支持引用分析结果或报告，请选择对应输出')
        artifact=find_artifact(user,artifact_id)
        task_id=artifact['metadata']['task_id']
        result=service.get_task(task_id,user.user_id)
        if result.datasource_id and result.datasource_id!=source:raise HTTPException(422,'不能引用其他数据源的分析结果')
        workspace['analysis_table_ids'].append(task_id)
    workspace['analysis_table_ids']=list(dict.fromkeys(workspace['analysis_table_ids']))
    options={'model':config.get('activeLlmProfileId') or service.config.llm_model,
        'skills':config.get('enabledSkillIds'),'tools':tool_ids}
    if options['model'] not in available['models']:raise HTTPException(422,'请选择已配置的模型')
    if options['skills'] is not None and not set(options['skills']).issubset({s['name'] for s in available['skills']}):raise HTTPException(422,'包含不可用 Skill')
    previous=records_for(user,session) if store.get(user.user_id,'session',sid) else []
    last=previous[-1]['result'] if previous else None
    resume=None
    if last and last.get('status')=='partial' and query in {'继续','继续分析','继续检查点','继续未完成的分析'}:
        resume=last['task_id']
    # A clarification answer resumes the original task instead of starting another SQL/model plan.
    if last and last.get('status')=='waiting_clarification' and not service._is_report_followup(query):
        choices=(last.get('clarification') or {}).get('options') or []
        choice=next((c for c in choices if query in {c['id'],c['label']}),None)
        skipping = query == '跳过，继续分析' and (last.get('clarification') or {}).get('allow_skip') is True and (last.get('clarification') or {}).get('required') is False
        if not choice and not skipping and not service._free_clarification(QueryResult.model_validate(last)):
            raise HTTPException(422,'请回复上轮列出的一个选项名称，以继续确认口径')
        response = choice['id'] if choice else query
        job=workbench_runs.start(user.user_id,query,session['backendSession'],workspace,options,
            lambda:service.clarify(last['task_id'],response,user_message=query,user_id=user.user_id), count_analysis=False)
    else:
        payload=WorkbenchRequest(query=query,session_id=session['backendSession'],workspace=workspace,options=options,resume_task_id=resume)
        job=start_workbench_run(payload,user)
    session['config']={'disabled':{'db':[d['id'] for d in sources(user) if d['id']!=source], 'kb':[],
        'mcp':[t['id'] for t in workspace_config(user)['mcpServers'] if tool_ids is not None and t['id'] not in tool_ids],
        'skill':[s['name'] for s in available['skills'] if options['skills'] is not None and s['name'] not in options['skills']]}}
    session['updatedAt']=now();store.put(user.user_id,'session',sid,session)
    store.put(user.user_id,'binding',source+'\0'+session['backendSession'],{'id':sid})
    link={'id':run_id,'sessionId':sid,'jobId':job['id'],'requestHash':fingerprint}
    store.put(user.user_id,'run',run_id,link)
    return link


async def stream_run(user, link):
    sid=link['sessionId'];rid=link['id'];cursor=0
    yield encode_event(event('RUN_STARTED',threadId=sid,runId=rid))
    yield encode_event(event('TEXT_MESSAGE_START',messageId=rid+'-answer',role='assistant'))
    session=await run_in_threadpool(get_session,user,sid)
    yield encode_event(event('CUSTOM',name='session.title',value={'sessionId':sid,'title':session['title']}))
    while True:
        job=workbench_runs.read_progress(link['jobId'],user.user_id,cursor)
        for item in job['events']:
            yield encode_event(item)
        cursor=job['cursor']
        if job['status'] not in ACTIVE:break
        # Cancellation is acknowledged immediately; the worker checks the flag before its next external call.
        if job['status']=='stopping':
            yield encode_event(event('TEXT_MESSAGE_END',messageId=rid+'-answer'))
            yield encode_event(event('STATE_SNAPSHOT',snapshot={'runId':rid,'runStatus':'canceled'}))
            yield encode_event(event('RUN_FINISHED',threadId=sid,runId=rid))
            return
        yield ': keep-alive\n\n'
        await asyncio.sleep(.1)
    job=workbench_runs.read(link['jobId'],user.user_id)
    if job.get('result'):
        result=job['result'];record={'task_id':result['task_id'],'run_id':rid,'query':job['query'],'result':result,'workspace':job['workspace'],'updated_at':job['created_at'], 'execution_events':job.get('events',[]),'finished_at':job.get('finished_at')}
        for item in delivery_events(record,sid,rid,answer_started=True):yield encode_event(item)
    else:
        yield encode_event(event('TEXT_MESSAGE_END',messageId=rid+'-answer'))
    if job['status']=='failed':
        yield encode_event(event('RUN_ERROR',message=job.get('error') or '分析未完成',code='ANALYSIS_FAILED'))
    elif job['status'] in {'stopped','canceled','cancelled','interrupted'}:
        yield encode_event(event('STATE_SNAPSHOT',snapshot={'runId':rid,'runStatus':'canceled'}))
        yield encode_event(event('RUN_FINISHED',threadId=sid,runId=rid))
    else:
        yield encode_event(event('RUN_FINISHED',threadId=sid,runId=rid))


@router.post('/copilotkit')
async def copilotkit(request:Request):
    try:
        payload=await request.json();method=payload.get('method')
        user=authenticate(request,mutation=method not in {'info','agent/connect'})
        if method=='info':return {'version':'1.60.1','agents':{'dataFoundry':{'description':'InsightPilot 经营分析 Agent','capabilities':{}}},'audioFileTranscriptionEnabled':False,'telemetryDisabled':True}
        if method not in {'agent/run','agent/connect'}:raise HTTPException(422,'不支持的运行方法')
        if (payload.get('params') or {}).get('agentId') not in {None,'dataFoundry'}:raise HTTPException(404,'Agent 不存在')
        body=payload.get('body') or {}
        if method=='agent/run':link=await run_in_threadpool(prepare_run,user,body)
        else:
            sid=str(body.get('threadId') or '')
            if not store.get(user.user_id,'session',sid):
                return StreamingResponse(iter([]),media_type='text/event-stream')
            session=await run_in_threadpool(get_session,user,sid)
            active=await run_in_threadpool(active_run,user,session)
            if not active:return StreamingResponse(iter([]),media_type='text/event-stream')
            link=store.get(user.user_id,'run',active['activeRunId'])
        return StreamingResponse(stream_run(user,link),media_type='text/event-stream',
            headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})
    except (HTTPException,PermissionError,KeyError,ValueError) as exc:return error_response(exc)
