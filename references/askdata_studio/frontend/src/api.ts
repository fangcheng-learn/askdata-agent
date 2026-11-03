import type { AuthUser, DatasourceOption, LoginResponse, QueryResult, SavedMemory, SchemaField, SchemaTable, WorkspaceConfig, RuntimeOptions, WorkbenchOptions, WorkbenchRun } from "./types"

export interface AgentConfiguration {
  revision: string
  updated_at: string | null
  updated_by: string | null
  agents: { id: string; label: string; description: string; model: string; node_names: string[]; prompt_ids: string[]; skill_ids: string[]; context_inputs: string[]; loading_rule: string }[]
  prompts: { id: string; agent_id: string; label: string; description: string; default_text: string; effective_text: string; source: string; overridden: boolean; editable: boolean; required_placeholders?: string[]; assembly_notes?: string[]; assembled_text?: string }[]
  skills: { id: string; label: string; description: string; default_text: string; effective_text: string; source: string; overridden: boolean; editable: boolean; allowed_tools: string[]; used_by: string[]; loading_rule: string }[]
  policies: { id: string; label: string; description: string }[]
}

export interface AgentConfigurationUpdate {
  expected_revision: string
  prompts?: Record<string, string | null>
  skills?: Record<string, string | null>
}

const API_BASE = import.meta.env.VITE_API_BASE ?? ""
const TOKEN_KEY = "askdata_access_token"

function token() {
  return sessionStorage.getItem(TOKEN_KEY)
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 540_000)
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
        ...options?.headers,
      },
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请检查模型接口配置后重试")
    }
    throw new Error("无法连接后端服务，请先启动 FastAPI（127.0.0.1:8001）")
  } finally {
    window.clearTimeout(timeout)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? "服务暂时不可用")
  }
  return response.json() as Promise<T>
}

export const api = {
  workbenchOptions: (source: string) => request<WorkbenchOptions>(`/api/workbench/options?datasource_id=${encodeURIComponent(source)}`),
  runs: (source: string) => request<WorkbenchRun[]>(`/api/workbench/runs?datasource_id=${encodeURIComponent(source)}`),
  run: (id: string) => request<WorkbenchRun>(`/api/workbench/runs/${encodeURIComponent(id)}`),
  stopRun: (id: string) => request<WorkbenchRun>(`/api/workbench/runs/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  startRun: (query: string, workspace: WorkspaceConfig, sessionId: string, options: RuntimeOptions, resumeTaskId?: string) =>
    request<WorkbenchRun>('/api/workbench/runs', { method: 'POST', body: JSON.stringify({query, workspace, session_id: sessionId, options, resume_task_id: resumeTaskId}) }),
  agentConfiguration: () => request<AgentConfiguration>("/api/agent-config"),
  saveAgentConfiguration: (payload: AgentConfigurationUpdate) => request<AgentConfiguration>("/api/agent-config", {
    method: "PUT", body: JSON.stringify(payload),
  }),
  hasSession: () => Boolean(token()),
  login: async (username: string, password: string) => {
    const result = await request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    })
    sessionStorage.setItem(TOKEN_KEY, result.access_token)
    return result.user
  },
  me: () => request<AuthUser>("/api/auth/me"),
  logout: async () => {
    try {
      if (token()) await request<{ logged_out: boolean }>("/api/auth/logout", { method: "POST" })
    } finally {
      sessionStorage.removeItem(TOKEN_KEY)
    }
  },
  clearSession: () => sessionStorage.removeItem(TOKEN_KEY),
  datasources: () => request<DatasourceOption[]>("/api/datasources"),
  schema: (datasourceId?: string) => request<SchemaTable[]>("/api/schema" + (datasourceId ? "?datasource_id=" + encodeURIComponent(datasourceId) : "")),
  task: (taskId: string) => request<QueryResult>(`/api/tasks/${encodeURIComponent(taskId)}`),
  history: (datasourceId: string) => request<{ task_id: string; session_id: string; query: string;
    workspace: WorkspaceConfig; updated_at: string | null; result: QueryResult }[]>(
      `/api/history?datasource_id=${encodeURIComponent(datasourceId)}`),
  hideHistory: (sessionId: string, datasourceId: string) => request<{ hidden: boolean }>(
    `/api/history/${encodeURIComponent(sessionId)}?datasource_id=${encodeURIComponent(datasourceId)}`, { method: 'DELETE' }),
  query: (query: string, workspace: WorkspaceConfig, sessionId: string) =>
    request<QueryResult>("/api/query", {
      method: "POST",
      body: JSON.stringify({ query, session_id: sessionId, workspace }),
    }),
  clarify: (taskId: string, optionId: string) =>
    request<QueryResult>(`/api/tasks/${encodeURIComponent(taskId)}/clarify`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId }),
    }),
  save: (taskId: string) =>
    request<{ saved: boolean }>("/api/memories", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    }),
  memories: (datasourceId?: string) => request<SavedMemory[]>("/api/memories" + (datasourceId ? "?datasource_id=" + encodeURIComponent(datasourceId) : "")),
  saveField: (tableId: string, field: SchemaField) =>
    request<{ saved: boolean }>("/api/memories/fields", {
      method: "POST",
      body: JSON.stringify({
        table_id: tableId,
        name: field.name,
        label: field.label,
        field_type: field.type,
      }),
    }),
  deleteMemory: (memoryId: string, datasourceId?: string) =>
    request<{ deleted: boolean }>(`/api/memories/${encodeURIComponent(memoryId)}` + (datasourceId ? "?datasource_id=" + encodeURIComponent(datasourceId) : ""), {
      method: "DELETE",
    }),
}
