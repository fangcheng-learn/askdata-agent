from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..errors import PipelineStageError
from ..models import (
    ClarificationRequest,
    LoginRequest,
    QueryRequest,
    QueryResult,
    SaveFieldMemoryRequest,
    SaveMemoryRequest,
    SchemaTable,
)
from ..security import AuthService, AuthUser
from ..services.askdata_service import AskDataService


router = APIRouter(prefix="/api")
service = AskDataService()
auth_service = AuthService()


def require_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AuthUser:
    user = auth_service.authenticate(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以执行该操作")
    return user


class AgentConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: str = Field(min_length=1)
    prompts: dict[str, str | None] = Field(default_factory=dict)
    skills: dict[str, str | None] = Field(default_factory=dict)


@router.get('/agent-config')
def agent_configuration(_: AuthUser = Depends(require_admin)) -> dict:
    from ..services.agent_configuration_catalog import configuration_catalog
    from ..services.agent_configuration import ConfigurationStorageError
    try:
        return configuration_catalog(service.config)
    except ConfigurationStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put('/agent-config')
def update_agent_configuration(payload: AgentConfigurationUpdate,
                               user: AuthUser = Depends(require_admin)) -> dict:
    from ..agent_configuration_runtime import configuration_store
    from ..services.agent_configuration_catalog import configuration_catalog, validate_prompt_markers
    from ..services.agent_configuration import (ConfigurationConflict, ConfigurationValidationError,
                                                ConfigurationStorageError)
    try:
        validate_prompt_markers(payload.prompts)
        saved = configuration_store().update(expected_revision=payload.expected_revision,
            prompts=payload.prompts, skills=payload.skills, actor=user.user_id)
        return configuration_catalog(service.config, snapshot=saved)
    except ConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigurationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfigurationStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/health")
def health() -> dict:
    return {"status": "ok", **service.status()}


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    result = auth_service.login(payload.username, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token, user = result
    return {"access_token": token, "token_type": "bearer", "user": user.public()}


@router.get("/auth/me")
def me(user: AuthUser = Depends(require_user)) -> dict[str, str]:
    return user.public()


@router.post("/auth/logout")
def logout(
    authorization: str | None = Header(default=None, alias="Authorization"),
    _: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    auth_service.logout(authorization)
    return {"logged_out": True}


@router.get("/schema", response_model=list[SchemaTable])
def schema(
    user: AuthUser = Depends(require_user),
    datasource_id: str | None = None,
) -> list[dict]:
    try:
        return service.visible_schema(user.user_id, datasource_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get("/datasources")
def datasources(user: AuthUser = Depends(require_user)) -> list[dict]:
    return service.list_datasources(user.user_id)


@router.get("/schema/search")
def search_schema(
    q: str,
    threshold: float | None = None,
    user: AuthUser = Depends(require_user),
    datasource_id: str | None = None,
) -> dict:
    if not q.strip():
        raise HTTPException(status_code=400, detail="检索问题不能为空")
    if threshold is not None and not 0 <= threshold <= 1:
        raise HTTPException(status_code=400, detail="阈值必须在0到1之间")
    try:
        return service.search_schema(q.strip(), threshold, user.user_id, datasource_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.post("/schema/index/rebuild")
def rebuild_schema_index(_: AuthUser = Depends(require_admin)) -> dict:
    try:
        return service.rebuild_schema_index()
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.get("/config")
def config(_: AuthUser = Depends(require_user)) -> dict:
    return service.status()


@router.get("/mcp/tools")
def mcp_tools(
    user: AuthUser = Depends(require_user),
    datasource_id: str | None = None,
) -> list[dict]:
    """查看模型可用的MCP工具及其输入输出Schema。"""
    try:
        return service.list_mcp_tools(user.user_id, datasource_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get("/skills")
def skills(_: AuthUser = Depends(require_user)) -> list[dict]:
    """查看当前启用的应用级Skill。"""
    return service.list_skills()


@router.post("/query", response_model=QueryResult)
def query(
    payload: QueryRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    workspace = payload.workspace.model_dump(exclude_none=True) if payload.workspace else None
    try:
        return service.submit(payload.query, payload.session_id, workspace, user.user_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get('/history')
def history(datasource_id: str, limit: int = Query(default=50, ge=1, le=100),
            user: AuthUser = Depends(require_user)) -> list[dict]:
    try:
        return service.list_history(user.user_id, datasource_id, limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete('/history/{session_id}')
def hide_history(session_id: str, datasource_id: str, user: AuthUser = Depends(require_user)) -> dict:
    try:
        service.hide_history(user.user_id, datasource_id, session_id)
        workbench_runs.hide_session(user.user_id, datasource_id, session_id)
        return {'hidden': True}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/clarify", response_model=QueryResult)
def clarify(
    task_id: str,
    payload: ClarificationRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        return service.clarify(task_id, payload.option_id, user_id=user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="无效的澄清选项") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get("/tasks/{task_id}", response_model=QueryResult)
def task(
    task_id: str,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        return service.get_task(task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.post("/memories")
def save_memory(
    payload: SaveMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        service.save_memory(payload.task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="没有可保存的查询结果") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"saved": True}


@router.get("/memories")
def memories(user: AuthUser = Depends(require_user), datasource_id: str | None = None) -> list[dict]:
    try:
        return service.list_memories(user.user_id, datasource_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.post("/memories/fields")
def save_field_memory(
    payload: SaveFieldMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        service.save_field_memory(
            payload.table_id,
            payload.name,
            payload.label,
            payload.field_type,
            user.user_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"saved": True}


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: str,
    user: AuthUser = Depends(require_user),
    datasource_id: str | None = None,
) -> dict[str, bool]:
    try:
        service.delete_memory(memory_id, user.user_id, datasource_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"deleted": True}


@router.get("/tasks/{task_id}/results/{result_ref}")
def read_result(task_id: str, result_ref: str, datasource_id: str, offset: int = 0, limit: int = 200,
                user: AuthUser = Depends(require_user)) -> dict:
    try:
        service.get_task(task_id, user.user_id)
        service.access_controller.scoped(user.user_id, datasource_id)
        return service.workflow.database_engine.read_result(result_ref, user.user_id, task_id, datasource_id, offset, limit)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="任务或结果引用不存在") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# Workbench controls are isolated from the existing synchronous API.
from ..workbench_runtime import WorkbenchRuns
from ..config import BASE_DIR
workbench_runs = WorkbenchRuns(BASE_DIR / 'data' / 'workbench_runs')


class WorkbenchOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = ''
    skills: list[str] | None = None
    tools: list[str] | None = None


class WorkbenchRequest(QueryRequest):
    options: WorkbenchOptions = Field(default_factory=WorkbenchOptions)
    resume_task_id: str | None = None


@router.get('/workbench/options')
def workbench_options(datasource_id: str, user: AuthUser = Depends(require_user)) -> dict:
    try:
        tools = service.list_mcp_tools(user.user_id, datasource_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {'models': list(dict.fromkeys([service.config.llm_model, service.config.route_model or service.config.llm_model])),
            'default_model': service.config.llm_model, 'skills': service.list_skills(), 'tools': tools}


@router.post('/workbench/runs')
def start_workbench_run(payload: WorkbenchRequest, user: AuthUser = Depends(require_user)) -> dict:
    workspace = payload.workspace.model_dump(exclude_none=True) if payload.workspace else {}
    source = workspace.get('datasource_id', '')
    available = workbench_options(source, user)
    options = payload.options.model_dump()
    options['model'] = options['model'] or available['default_model']
    if options['model'] not in available['models']:
        raise HTTPException(422, '请选择已配置的模型')
    for key in ('skills', 'tools'):
        names = {v['name'] for v in available[key]}
        if options[key] is not None and not set(options[key]).issubset(names):
            raise HTTPException(422, '配置包含不可用的 ' + key)
    if payload.resume_task_id:
        try:
            service.get_task(payload.resume_task_id, user.user_id)
        except (KeyError, PermissionError) as exc:
            raise HTTPException(404, '找不到可访问的任务') from exc
    try:
        return workbench_runs.start(user.user_id, payload.query, payload.session_id, workspace, options,
            lambda: service.clarify(payload.resume_task_id, '__continue__', user_id=user.user_id)
            if payload.resume_task_id else service.submit(payload.query, payload.session_id, workspace, user.user_id),
            count_analysis=not bool(payload.resume_task_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/workbench/runs')
def list_workbench_runs(datasource_id: str, user: AuthUser = Depends(require_user)) -> list[dict]:
    # Recheck current datasource access before exposing saved results.
    try:
        service.visible_schema(user.user_id, datasource_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return workbench_runs.list(user.user_id, datasource_id)


@router.get('/workbench/runs/{run_id}')
def get_workbench_run(run_id: str, user: AuthUser = Depends(require_user)) -> dict:
    try:
        item = workbench_runs.read(run_id, user.user_id)
        service.visible_schema(user.user_id, item['workspace'].get('datasource_id'))
        return item
    except (KeyError, PermissionError) as exc:
        raise HTTPException(404, '任务不存在或不可访问') from exc


@router.post('/workbench/runs/{run_id}/stop')
def stop_workbench_run(run_id: str, user: AuthUser = Depends(require_user)) -> dict:
    get_workbench_run(run_id, user)
    return workbench_runs.cancel(run_id, user.user_id)
