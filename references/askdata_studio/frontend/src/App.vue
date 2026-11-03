<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from "vue"
import { api } from "./api"
import WorkbenchResult from "./components/WorkbenchResult.vue"
import ResourcePicker from "./components/ResourcePicker.vue"
import Icon from "./components/WorkbenchIcon.vue"
import AgentConfiguration from "./components/AgentConfiguration.vue"
import type { AuthUser, DatasourceOption, QueryResult, ReportDataSource, SavedMemory, SchemaField, SchemaTable, WorkspaceConfig, RuntimeOptions, WorkbenchOptions, WorkbenchRun } from "./types"

interface ConversationTurn {
  query: string
  result: QueryResult
}

interface ConversationRecord {
  id: string
  title: string
  updatedAt: number
  turns: ConversationTurn[]
  workspace: WorkspaceConfig
  options?: RuntimeOptions
  branchOf?: string
  draft?: string
}

interface HistoricalResultTable {
  taskId: string
  title: string
  query: string
  rowCount: number
  columns: string[]
  rows: Record<string, string | number | null>[]
}

const SAVED_TABLE_LIMIT = 8
const FIELD_LIBRARY_LIMIT = 12
const prompts = [
  { title: 'GMV 下降归因', description: '核实变化，追查原因与关键证据', icon: 'chart', question: '七月份 GMV 为什么相比六月份下降那么多？' },
  { title: '商品表现分析', description: '对比销售额与销量，定位主要变化', icon: 'data', question: '七月份相比六月份，哪些商品的销售额和销量下降最明显？' },
  { title: '投放效果分析', description: '对比广告花费与成交，检查投放效率', icon: 'search', question: '七月份相比六月份，哪些广告渠道花费增加了，但带来的成交没有同步增长？' },
]
const input = ref("")
const loading = ref(false)
const workspaceLoading = ref(false)
const error = ref("")
const restoreTaskId = ref(new URLSearchParams(window.location.search).get("task_id") ?? "")
const restoreOpened = ref(false)
const schema = ref<SchemaTable[]>([])
const savedMemories = ref<SavedMemory[]>([])
const workspace = ref<WorkspaceConfig>({})
const conversations = ref<ConversationRecord[]>([])
const activeConversationId = ref("")
const expandedSql = ref<string | null>(null)
const leftOpen = ref(false)
const rightOpen = ref(true)
const configurationOpen = ref(new URLSearchParams(window.location.search).get("view") === "agent-config")

function showConfiguration(open: boolean) {
  configurationOpen.value = open
  const url = new URL(window.location.href)
  if (open) url.searchParams.set("view", "agent-config")
  else url.searchParams.delete("view")
  window.history.replaceState({}, "", url)
}
const conversationScroll = ref<HTMLElement | null>(null)
const authUser = ref<AuthUser | null>(null)
const authReady = ref(false)
const datasources = ref<DatasourceOption[]>([])
const datasourceId = ref("")
const currentDatasource = computed(() => datasources.value.find((item) => item.id === datasourceId.value))
const scenarioLabel = computed(() => {
  const scenario = currentDatasource.value?.scenario
  if (!scenario) return ""
  const zone = scenario.timezone || "Europe/Berlin"
  const date = scenario.analysis_date || scenario.as_of_local_date || ""
  const end = scenario.visit_end_at
  const lastTradingDay = end
    ? new Intl.DateTimeFormat("sv-SE", { timeZone: zone }).format(new Date(new Date(end).getTime() - 1))
    : ""
  return "快照记录日 " + date + " · " + zone + (lastTradingDay ? " · 新订单覆盖至 " + lastTradingDay : "") + " · 相对时间按机器今天"
})
const loginLoading = ref(false)
const loginUsername = ref("admin")
const loginPassword = ref("admin123")

const activeConversation = computed(() =>
  conversations.value.find((item) => item.id === activeConversationId.value),
)
const currentTurns = computed(() => activeConversation.value?.turns ?? [])
const confirmedFields = computed(() => workspace.value.schema_fields ?? [])
const analysisTableIds = computed(() => workspace.value.analysis_table_ids ?? [])
const historicalResultTables = computed<HistoricalResultTable[]>(() => {
  return savedMemories.value
    .filter((item) => item.kind === "result_table" && item.task_id && item.rows?.length)
    .map((item) => ({
      taskId: item.task_id!,
      title: item.title || "查询结果",
      query: item.query || "",
      rowCount: item.rows?.length || 0,
      columns: item.columns || [],
      rows: item.rows || [],
    }))
    .slice(0, SAVED_TABLE_LIMIT)
})
const reportDataSources = computed<ReportDataSource[]>(() => {
  const sources = new Map<string, ReportDataSource>()
  for (const conversation of conversations.value) {
    for (const turn of conversation.turns) {
      if (!turn.result.rows.length) continue
      sources.set(turn.result.task_id, {
        taskId: turn.result.task_id,
        title: turn.result.result_title || "查询结果",
        columns: turn.result.columns,
        rows: turn.result.rows,
      })
    }
  }
  for (const table of historicalResultTables.value) {
    sources.set(table.taskId, {
      taskId: table.taskId,
      title: table.title,
      columns: table.columns,
      rows: table.rows,
    })
  }
  return [...sources.values()]
})
const selectedAnalysisTables = computed(() =>
  analysisTableIds.value
    .map((id) => {
      const saved = historicalResultTables.value.find((table) => table.taskId === id)
      if (saved) return saved
      for (const conversation of conversations.value) {
        const turn = conversation.turns.find(item => item.result.task_id === id)
        if (turn?.result.rows.length) return {
          taskId: id, title: turn.result.result_title || '已恢复结果', query: turn.query,
          rowCount: turn.result.total_rows ?? turn.result.rows.length,
          columns: turn.result.columns, rows: turn.result.rows,
        }
      }
      return undefined
    })
    .filter((table): table is HistoricalResultTable => Boolean(table)),
)
const recentFields = computed(() => {
  const items: { field: SchemaField; tableId: string }[] = []
  const keys = new Set<string>()
  const add = (fieldName: string, tableId: string) => {
    const table = schema.value.find((item) => item.id === tableId)
    const field = table?.fields.find((item) => item.name === fieldName)
    const key = `${tableId}:${fieldName}`
    if (field && !keys.has(key)) {
      keys.add(key)
      items.push({ field, tableId })
    }
  }
  for (const conversation of conversations.value) {
    for (const turn of [...conversation.turns].reverse()) {
      for (const field of turn.result.schema_graph?.fields ?? []) {
        if (field.source !== "relation_key") add(field.name, field.table_id)
      }
    }
    for (const field of conversation.workspace.schema_fields ?? conversation.workspace.fields ?? []) {
      const tableId = field.tableId
      if (tableId) add(field.name, tableId)
    }
  }
  return items.slice(0, FIELD_LIBRARY_LIMIT)
})
const savedFieldKeys = computed(() => new Set(
  savedMemories.value
    .filter((item) => item.kind === "schema_field")
    .map((item) => `${item.table_id}:${item.name}`),
))
const fieldLibrary = computed(() => {
  const items: { field: SchemaField; tableId: string; saved: boolean }[] = []
  const keys = new Set<string>()
  const add = (field: SchemaField, tableId: string, saved: boolean) => {
    const key = `${tableId}:${field.name}`
    if (keys.has(key)) return
    keys.add(key)
    items.push({ field, tableId, saved })
  }
  for (const memory of savedMemories.value) {
    if (memory.kind !== "schema_field" || !memory.table_id || !memory.name) continue
    const field = findField(memory.name, memory.table_id) ?? {
      name: memory.name,
      label: memory.label || memory.name,
      type: memory.field_type || "文本",
    }
    add(field, memory.table_id, true)
  }
  for (const item of recentFields.value) {
    add(item.field, item.tableId, savedFieldKeys.value.has(`${item.tableId}:${item.field.name}`))
  }
  return items.slice(0, FIELD_LIBRARY_LIMIT)
})
const historyGroups = computed(() => {
  const today = new Date().toDateString()
  const recent: ConversationRecord[] = []
  const earlier: ConversationRecord[] = []
  for (const item of conversations.value) {
    if (new Date(item.updatedAt).toDateString() === today) recent.push(item)
    else earlier.push(item)
  }
  return [
    { label: "今天", items: recent },
    { label: "更早", items: earlier },
  ].filter((group) => group.items.length)
})
const userRoleLabel = computed(() => {
  if (authUser.value?.role === "admin") return "全部数据权限"
  if (authUser.value?.role === "analyst") return "仅 Mock 数据"
  return "电商运营数据"
})

const availableOptions = ref<WorkbenchOptions | null>(null)
const runtimeOptions = ref<RuntimeOptions>({ model: '', skills: null, tools: null })
const runs = ref<WorkbenchRun[]>([])
const selectedTaskId = ref('')
const rightTab = ref<'results' | 'evidence'>('results')
const resourceSection = ref<'data' | 'model' | 'skills' | 'tools' | 'schema' | 'assets'>('data')
const resourcePageOpen = ref(false)
const resourcePages = {
  data: { title: '数据源', icon: 'data', description: '浏览已授权的数据源，选择分析范围，查看表结构与指标口径。' },
  schema: { title: '指标与数据口径', icon: 'book', description: '浏览字段和统计口径，为经营分析补充必要的上下文。' },
  skills: { title: 'Skills', icon: 'skill', description: '查看分析方法，调整当前会话可使用的业务能力。' },
  tools: { title: 'MCP 工具', icon: 'tool', description: '浏览已授权的工具，调整当前会话的工具选择。' },
  model: { title: '模型', icon: 'model', description: '查看已配置的模型，选择本次分析使用的模型。' },
  assets: { title: '已保存结果', icon: 'file', description: '查找主动保存的分析结果，打开后继续分析。' },
}
const resourcePage = computed(() => resourcePages[resourceSection.value])
const sourceChoices = computed(() => datasources.value.map(source => ({id:source.id, label:source.label, description:source.id})))
const skillChoices = computed(() => (availableOptions.value?.skills || []).map(skill => ({id:skill.name, label:skillLabels[skill.name] || skill.name, description:skill.description, required:['database_query','data_qa'].includes(skill.name)})))
const toolChoices = computed(() => (availableOptions.value?.tools || []).map(tool => ({id:tool.name, label:tool.title || tool.name, description:tool.description})))
async function selectComposerSource(source: string) {
  if (source === datasourceId.value || loading.value || workspaceLoading.value || currentTurns.value.length) return
  const draft = input.value
  datasourceId.value = source
  await changeDatasource()
  createConversation()
  input.value = draft
}
const searchHistory = ref('')
const referenceText = ref('')
const referenceTaskTitle = computed(() => analysisTableIds.value.filter(id => !selectedAnalysisTables.value.some(t => t.taskId===id)).map(id => ({id,title:currentTurns.value.find(t => t.result.task_id===id)?.result.report?.title || '分析依据 '+id})))
const fullResult = ref(false)
const composerInput = ref<HTMLTextAreaElement | null>(null)
const showRestore = ref(false)
let pollTimer: ReturnType<typeof setInterval> | undefined
let polling = false
const activeRun = computed(() => runs.value.find(run => run.session_id === activeConversationId.value && isRunning(run)))
const focusedTurn = computed(() => currentTurns.value.find(turn => turn.result.task_id === selectedTaskId.value) || currentTurns.value.at(-1))
const focusedResult = computed(() => focusedTurn.value?.result || null)
const filteredGroups = computed(() => historyGroups.value.map(group => ({...group, items: group.items.filter(item => item.title.toLowerCase().includes(searchHistory.value.toLowerCase()))})).filter(group => group.items.length))
const skillLabels: Record<string,string> = { database_query:'数据库查询', data_qa:'业务知识问答', business_diagnosis:'经营诊断', goal_analysis:'目标拆解', inventory_risk_check:'库存风险检查' }
const skillCount = computed(() => availableOptions.value?.skills.filter(skill => ['database_query','data_qa'].includes(skill.name) || !runtimeOptions.value.skills || runtimeOptions.value.skills.includes(skill.name)).length || 0)
const toolCount = computed(() => runtimeOptions.value.tools?.length ?? availableOptions.value?.tools.length ?? 0)
const optionSummary = computed(() => `${runtimeOptions.value.model || '默认模型'} · ${skillCount.value} Skills · ${toolCount.value} MCP`)
const isRunning = (run: WorkbenchRun) => ['running','stopping'].includes(run.status)
function defaultOptions(): RuntimeOptions { return { model: availableOptions.value?.default_model || '', skills: null, tools: null } }
function normalizeRuntimeOptions(value?: RuntimeOptions): RuntimeOptions {
  if (!value) return defaultOptions()
  return {model:availableOptions.value?.models.includes(value.model) ? value.model : availableOptions.value?.default_model || '',
    skills:value.skills?.filter(name => availableOptions.value?.skills.some(item => item.name===name)) ?? null,
    tools:value.tools?.filter(name => availableOptions.value?.tools.some(item => item.name===name)) ?? null}
}
function structuredCloneRuntime(): RuntimeOptions { return JSON.parse(JSON.stringify(runtimeOptions.value)) }
function uiKey() { return `insightpilot.workbench.v1:${authUser.value?.user_id}:${datasourceId.value}` }
function persistUi() {
  try { localStorage.setItem(uiKey(), JSON.stringify(conversations.value.map(c => ({id:c.id,title:c.title,updatedAt:c.updatedAt,workspace:c.workspace,options:c.options,branchOf:c.branchOf,draft:c.draft})))) } catch { /* Server history remains available. */ }
}
function restoreUi() {
  try {
    const saved = JSON.parse(localStorage.getItem(uiKey()) || '[]') as ConversationRecord[]
    for (const item of saved) {
      const existing = conversations.value.find(c => c.id === item.id)
      if (existing) Object.assign(existing, {options:item.options, branchOf:item.branchOf,title:item.title,draft:item.draft,workspace:item.workspace?.datasource_id === datasourceId.value ? normalizeWorkspace(item.workspace) : existing.workspace})
      else if (typeof item.id === 'string' && item.workspace?.datasource_id === datasourceId.value) conversations.value.push({...item, turns:[]})
    }
    tidyConversations()
  } catch { /* Invalid local UI preferences do not block server history. */ }
}
watch(input, () => { if(activeConversation.value) { activeConversation.value.draft=input.value;persistUi() } })
watch(runtimeOptions, () => {
  if (activeConversation.value) { activeConversation.value.options = structuredCloneRuntime(); persistUi() }
}, {deep:true})
function openResource(section: typeof resourceSection.value) { resourceSection.value=section; resourcePageOpen.value=true; fullResult.value=false }
function usePrompt(question: string) { input.value=question; nextTick(() => composerInput.value?.focus()) }
function onComposerKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); void submit()}
}
function selectResult(result: QueryResult, tab: 'results' | 'evidence' = 'results') { selectedTaskId.value=result.task_id; rightTab.value=tab; rightOpen.value=true }
function referenceResult(result: QueryResult, quote?: string) {
  addAnalysisTable(result.task_id)
  referenceText.value = quote ? `${result.task_id}：${quote}` : ''
  if (!input.value) input.value='基于这份分析，'
  nextTick(() => composerInput.value?.focus())
}
function forkResult(result: QueryResult) {
  const origin=currentTurns.value.find(turn => turn.result.task_id===result.task_id)
  if (!origin) return
  const branch: ConversationRecord={id:crypto.randomUUID(),title:origin.query.slice(0,16)+' · 分支',updatedAt:Date.now(),turns:[],
    workspace:{datasource_id:datasourceId.value},options:structuredCloneRuntime(),branchOf:result.task_id}
  conversations.value.unshift(branch); selectConversation(branch.id); input.value=origin.query; persistUi()
}
function toggleOption(kind: 'skills' | 'tools', name: string) {
  if (loading.value) return
  const options=runtimeOptions.value[kind] ?? availableOptions.value?.[kind].map(item => item.name) ?? []
  runtimeOptions.value[kind]=options.includes(name) ? options.filter(item => item!==name) : [...options,name]
}
function applyRun(run: WorkbenchRun) {
  let conversation=conversations.value.find(c => c.id===run.session_id)
  if (!conversation) { conversation={id:run.session_id,title:run.query.slice(0,24),updatedAt:Date.parse(run.created_at),turns:[],workspace:run.workspace,options:run.options};conversations.value.push(conversation) }
  if (run.result) {
    const turn=conversation.turns.find(t => t.result.task_id===run.result!.task_id)
    if (turn) turn.result=run.result
    else conversation.turns.push({query:run.query,result:run.result})
    if (activeConversationId.value===conversation.id) {selectedTaskId.value=run.result.task_id;setTaskUrl(run.result.task_id)}
  }
  loading.value=runs.value.some(isRunning)
  persistUi()
}
async function pollRuns() {
  if (polling) return
  polling=true
  try {
    for (const run of runs.value.filter(isRunning)) {
      const updated=await api.run(run.id)
      runs.value=runs.value.map(item => item.id===updated.id ? updated : item)
      applyRun(updated)
    }
    if (!runs.value.some(isRunning) && pollTimer) {clearInterval(pollTimer);pollTimer=undefined}
  } catch (caught) {error.value=caught instanceof Error ? caught.message : '无法获取任务状态，正在重试'}
  finally {polling=false}
}
function beginPolling() { if (runs.value.some(isRunning) && !pollTimer) pollTimer=setInterval(() => void pollRuns(),1500) }
async function stopAnalysis() {
  const run=activeRun.value
  if (!run || run.status==='stopping') return
  try { const updated=await api.stopRun(run.id); runs.value=runs.value.map(item => item.id===updated.id ? updated : item) }
  catch(caught) {error.value=caught instanceof Error ? caught.message : '停止请求失败'}
}
function runForConversation(id: string) { return runs.value.find(run => run.session_id===id && isRunning(run)) }
function phaseLabel(result: QueryResult) { return {completed:'分析完成',partial:'部分完成',failed:'分析遇到异常',waiting_clarification:'等待补充信息'}[result.status] }
function stageLabel(step: NonNullable<QueryResult['plan']>[number]) { return {completed:'已完成',failed:'失败',blocked:'受阻',not_applicable:'不适用'}[step.status] || '待核对' }
function answerPreview(result: QueryResult) {
  const text=result.analysis || result.message || result.report?.markdown || ''
  return text.replace(/^#{1,6}\s+/gm,'').replace(/[*`]/g,'').slice(0,360)
}
onBeforeUnmount(() => {if(pollTimer) clearInterval(pollTimer)})

onMounted(async () => {
  if (api.hasSession()) {
    try {
      authUser.value = await api.me()
    } catch {
      api.clearSession()
    }
  }
  authReady.value = true
  if (authUser.value) await initializeUserWorkspace()
})

async function initializeUserWorkspace() {
  try {
    datasources.value = await api.datasources()
    datasourceId.value = datasources.value.find((item) => item.id === "homenova_20260831")?.id
      || datasources.value[0]?.id || ""
    await changeDatasource()
    if (restoreTaskId.value && !restoreOpened.value) {
      restoreOpened.value = true
      await restoreTask(restoreTaskId.value)
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "数据源加载失败"
  }
}

async function changeDatasource() {
  if (loading.value) return
  const keepResourcePage = resourcePageOpen.value
  workspaceLoading.value = true
  conversations.value = []
  activeConversationId.value = ""
  workspace.value = { datasource_id: datasourceId.value }
  schema.value = []
  savedMemories.value = []
  input.value = ""
  error.value = ""
  try {
    const [schemaResult, memories, options] = await Promise.all([
      api.schema(datasourceId.value), api.memories(datasourceId.value), api.workbenchOptions(datasourceId.value),
    ])
    availableOptions.value = options
    runtimeOptions.value = defaultOptions()
    schema.value = schemaResult
    savedMemories.value = memories
    await loadHistory(datasourceId.value)
    restoreUi()
    runs.value = await api.runs(datasourceId.value)
    for (const run of [...runs.value].reverse()) applyRun(run)
    beginPolling()
    if (conversations.value[0]) selectConversation(conversations.value[0].id)
    else { workspaceLoading.value=false; createConversation() }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "工作区加载失败"
  } finally { workspaceLoading.value = false; resourcePageOpen.value = keepResourcePage }
}

async function loadHistory(source: string) {
  const records = await api.history(source)
  const sessions = new Map<string, ConversationRecord>()
  // The server returns newest first. Build turns in actual chronological order.
  for (const record of [...records].reverse()) {
    const timestamp = record.updated_at
      ? Date.parse(record.updated_at.endsWith('Z') || /[+-]\d\d:\d\d$/.test(record.updated_at)
        ? record.updated_at : record.updated_at.replace(' ', 'T') + 'Z') : 0
    let conversation = sessions.get(record.session_id)
    if (!conversation) {
      conversation = { id: record.session_id, title: record.query.slice(0, 18) || '已保存对话',
        updatedAt: Number.isFinite(timestamp) ? timestamp : 0, turns: [],
        workspace: { ...record.workspace, datasource_id: source } }
      sessions.set(record.session_id, conversation)
    }
    conversation.turns.push({ query: record.query, result: record.result })
    conversation.workspace = { ...record.workspace, datasource_id: source }
    conversation.updatedAt = Math.max(conversation.updatedAt, Number.isFinite(timestamp) ? timestamp : 0)
  }
  conversations.value = [...sessions.values()]
  tidyConversations()
}

function useMockAccount(username: string, password: string) {
  loginUsername.value = username
  loginPassword.value = password
  error.value = ""
}

async function loginUser() {
  if (!loginUsername.value.trim() || !loginPassword.value || loginLoading.value) return
  loginLoading.value = true
  error.value = ""
  try {
    authUser.value = await api.login(loginUsername.value.trim(), loginPassword.value)
    await initializeUserWorkspace()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "登录失败"
  } finally {
    loginLoading.value = false
  }
}

async function logoutUser() {
  if (loading.value) return
  try {
    await api.logout()
  } finally {
    authUser.value = null
    conversations.value = []
    activeConversationId.value = ""
    workspace.value = {}
    schema.value = []
    datasources.value = []
    datasourceId.value = ""
    savedMemories.value = []
    input.value = ""
    error.value = ""
  }
}

function normalizeWorkspace(value?: WorkspaceConfig): WorkspaceConfig {
  const fields = value?.schema_fields ?? value?.fields ?? []
  return {
    datasource_id: datasourceId.value,
    schema_fields: Array.isArray(fields)
      ? fields.map((field) => ({ name: field.name, tableId: field.tableId, aggregation: "auto" as const }))
      : [],
    analysis_table_ids: Array.isArray(value?.analysis_table_ids)
      ? [...value.analysis_table_ids]
      : [],
  }
}

function tidyConversations() {
  conversations.value.sort((a, b) => b.updatedAt - a.updatedAt)
  conversations.value = conversations.value.slice(0, 30)
}

function createConversation() {
  resourcePageOpen.value = false
  if (workspaceLoading.value) return
  const existingEmpty = conversations.value.find((item) => !item.turns.length && !item.branchOf && !item.draft?.trim() && !runs.value.some(run => run.session_id === item.id))
  if (existingEmpty) {
    selectConversation(existingEmpty.id)
    return
  }
  const item: ConversationRecord = {
    id: crypto.randomUUID(),
    title: "新对话",
    updatedAt: Date.now(),
    turns: [],
    workspace: { datasource_id: datasourceId.value },
  }
  conversations.value.unshift(item)
  activeConversationId.value = item.id
  workspace.value = { datasource_id: datasourceId.value }
  runtimeOptions.value = defaultOptions()
  selectedTaskId.value = ''
  referenceText.value = ''
  rightTab.value = 'results'
  fullResult.value = false
  input.value = ""
  error.value = ""
  leftOpen.value = false
  setTaskUrl()
  nextTick(() => conversationScroll.value?.scrollTo({ top: 0 }))
  tidyConversations()
}

function selectConversation(id: string) {
  resourcePageOpen.value = false
  const item = conversations.value.find((conversation) => conversation.id === id)
  if (!item) return
  activeConversationId.value = id
  workspace.value = normalizeWorkspace(item.workspace)
  runtimeOptions.value = normalizeRuntimeOptions(item.options)
  selectedTaskId.value = ''
  referenceText.value = ''
  input.value = item.draft || ""
  error.value = ""
  leftOpen.value = false
  setTaskUrl(item.turns[item.turns.length - 1]?.result.task_id)
  nextTick(() => conversationScroll.value?.scrollTo({ top: item.turns.length ? conversationScroll.value.scrollHeight : 0 }))
}

async function removeConversation(id: string) {
  if (loading.value) return
  try {
    await api.hideHistory(id, datasourceId.value)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '历史记录隐藏失败'
    return
  }
  conversations.value = conversations.value.filter((item) => item.id !== id)
  runs.value = runs.value.filter(run => run.session_id !== id)
  if (activeConversationId.value === id) {
    if (conversations.value[0]) selectConversation(conversations.value[0].id)
    else createConversation()
  }
  tidyConversations()
  persistUi()
}

function saveActiveConversation() {
  const conversation = activeConversation.value
  if (!conversation) return
  conversation.workspace = normalizeWorkspace(workspace.value)
  conversation.updatedAt = Date.now()
  if (conversation.title === "新对话" && conversation.turns[0]) {
    conversation.title = conversation.turns[0].query.slice(0, 18)
  }
  tidyConversations()
  persistUi()
}

async function submit(text?: string, resumeTaskId?: string) {
  const question = (text ?? input.value).trim()
  const query = referenceText.value ? `${question}\n引用依据：${referenceText.value}` : question
  if (!question || loading.value || workspaceLoading.value) return
  if (query.length > 500) { error.value = '问题与引用内容合计不能超过 500 字，请缩短后发送'; return }
  if (!activeConversation.value) createConversation()
  const origin = activeConversation.value!
  origin.updatedAt = Date.now()
  origin.workspace = normalizeWorkspace(workspace.value)
  origin.options = structuredCloneRuntime()
  loading.value = true
  error.value = ''
  input.value = ''
  referenceText.value = ''
  try {
    const requestWorkspace: WorkspaceConfig = {
      ...origin.workspace,
      analysis_tables: selectedAnalysisTables.value.map(table => ({task_id: table.taskId, title: table.title,
        query: table.query, columns: table.columns, rows: table.rows.slice(0,50)})),
    }
    const run = await api.startRun(query, requestWorkspace, origin.id, origin.options, resumeTaskId)
    runs.value = [run, ...runs.value.filter(item => item.id !== run.id)]
    if (origin.title === '新对话') origin.title = question.slice(0,24)
    persistUi()
    applyRun(run)
    beginPolling()
  } catch (caught) {
    loading.value = runs.value.some(isRunning)
    error.value = caught instanceof Error ? caught.message : '分析提交失败'
    if (activeConversationId.value === origin.id) input.value = query
  }
}

function setTaskUrl(taskId?: string) {
  const url = new URL(window.location.href)
  if (taskId) url.searchParams.set("task_id", taskId)
  else url.searchParams.delete("task_id")
  window.history.replaceState(null, "", url)
}

async function restoreTask(taskId = restoreTaskId.value) {
  const id = taskId.trim()
  if (!id || loading.value) return
  loading.value = true
  error.value = ""
  try {
    const result = await api.task(id)
    const source = result.datasource_id || datasourceId.value
    if (!datasources.value.some(item => item.id === source)) throw new Error("当前账号无法打开任务绑定的数据源")
    if (source !== datasourceId.value) {
      datasourceId.value = source
      conversations.value = []
      const [schemaResult, memories] = await Promise.all([api.schema(source), api.memories(source)])
      schema.value = schemaResult
      savedMemories.value = memories
      await loadHistory(source)
    }
    const sessionId = result.session_id || `restored-${result.task_id}`
    const existing = conversations.value.find(item => item.id === sessionId)
    const query = result.required_questions?.[0] || result.standalone_query || "恢复已保存的任务记录"
    const previousTurns = existing?.turns ?? []
    const turns = previousTurns.some(item => item.result.task_id === result.task_id)
      ? previousTurns.map(item => item.result.task_id === result.task_id ? { query, result } : item)
      : [...previousTurns, { query, result }]
    // Publish the complete restored conversation together with its turns,
    // including failed tasks which have no result rows or SQL.
    const conversation: ConversationRecord = {
      id: sessionId, title: existing?.title || result.result_title || query.slice(0, 18) || "已保存调查",
      updatedAt: Date.now(), turns,
      workspace: { ...(existing?.workspace ?? {}), datasource_id: source, analysis_table_ids: [result.task_id] },
    }
    conversations.value = [conversation, ...conversations.value.filter(item => item.id !== sessionId)].slice(0, 30)
    activeConversationId.value = sessionId
    workspace.value = normalizeWorkspace(conversation.workspace)
    input.value = ""
    restoreTaskId.value = result.task_id
    setTaskUrl(result.task_id)
    await nextTick()
    conversationScroll.value?.scrollTo({ top: 0 })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "任务恢复失败"
  } finally {
    loading.value = false
  }
}

async function continueTask(current: QueryResult) {
  await submit('从已保存检查点继续调查', current.task_id)
}

async function saveResult(result: QueryResult) {
  try {
    await api.save(result.task_id)
    result.saved = true
    savedMemories.value = await api.memories(datasourceId.value)
    saveActiveConversation()
  } catch (caught) { error.value = caught instanceof Error ? caught.message : '结果保存失败' }
}

async function toggleSavedField(field: SchemaField, tableId: string) {
  const memoryId = `field:${tableId}.${field.name}`
  if (savedFieldKeys.value.has(`${tableId}:${field.name}`)) {
    await api.deleteMemory(memoryId, datasourceId.value)
  } else {
    await api.saveField(tableId, field)
  }
  savedMemories.value = await api.memories(datasourceId.value)
}

function isSavedField(fieldName: string, tableId?: string) {
  return Boolean(tableId && savedFieldKeys.value.has(`${tableId}:${fieldName}`))
}

function toggleConfirmedFieldMemory(fieldName: string, tableId?: string) {
  const field = findField(fieldName, tableId)
  if (field && tableId) toggleSavedField(field, tableId)
}

function resetWorkspace() {
  workspace.value = { datasource_id: datasourceId.value }
  saveActiveConversation()
}

function dragAnalysisTable(event: DragEvent, table: HistoricalResultTable) {
  event.dataTransfer?.setData("analysis-result-table", table.taskId)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy"
}

function addAnalysisTable(taskId: string) {
  if (analysisTableIds.value.includes(taskId)) return
  workspace.value.analysis_table_ids = [...analysisTableIds.value, taskId]
  saveActiveConversation()
}

function dropAnalysisTable(event: DragEvent) {
  addAnalysisTable(event.dataTransfer?.getData("analysis-result-table") ?? "")
}

function removeAnalysisTable(taskId: string) {
  workspace.value.analysis_table_ids = analysisTableIds.value.filter((id) => id !== taskId)
  saveActiveConversation()
}

function dragField(event: DragEvent, field: SchemaField, tableId: string) {
  event.dataTransfer?.setData("field", field.name)
  event.dataTransfer?.setData("field-table", tableId)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy"
}

function findField(fieldName: string, tableId?: string) {
  const selectedTable = tableId ? schema.value.find((table) => table.id === tableId) : undefined
  return selectedTable?.fields.find((field) => field.name === fieldName)
    ?? schema.value.flatMap((table) => table.fields).find((field) => field.name === fieldName)
}

function isNumericField(fieldName: string, tableId?: string) {
  const field = findField(fieldName, tableId)
  return field?.type === "数值" || field?.type === "整数"
}

function addConfirmedField(fieldName: string, sourceTableId?: string) {
  const tableId = sourceTableId
  if (!tableId || !findField(fieldName, tableId)) return
  if (confirmedFields.value.some((field) => field.name === fieldName && field.tableId === tableId)) return
  workspace.value.schema_fields = [
    ...confirmedFields.value,
    {
      name: fieldName,
      aggregation: "auto",
      tableId,
    },
  ]
  saveActiveConversation()
}

function dropConfirmedField(event: DragEvent) {
  addConfirmedField(
    event.dataTransfer?.getData("field") ?? "",
    event.dataTransfer?.getData("field-table") ?? undefined,
  )
}

function removeConfirmedField(fieldName: string, tableId?: string) {
  workspace.value.schema_fields = confirmedFields.value.filter(
    (field) => !(field.name === fieldName && field.tableId === tableId),
  )
  saveActiveConversation()
}

function fieldLabel(fieldName: string, tableId?: string) {
  return findField(fieldName, tableId)?.label ?? fieldName
}

function tableLabel(tableId?: string) {
  return schema.value.find((table) => table.id === tableId)?.label ?? "未知数据表"
}

function clarificationHint(result: QueryResult) {
  return result.clarification?.options.map((option) => option.label).join("、") ?? ""
}

</script>

<template>
  <div v-if="!authReady" class="auth-loading"><span></span><p>正在连接 InsightPilot…</p></div>

  <main v-else-if="!authUser" class="login-page">
    <section class="login-intro">
      <div class="login-brand"><span>A</span><strong>InsightPilot</strong></div>
      <div>
        <p class="kicker">AI DATA ASSISTANT</p>
        <h1>用自然语言，<br>读懂你的数据。</h1>
        <p>追踪经营变化、核实指标口径，让每个结论都有据可查。</p>
      </div>
      <small>FastAPI · LangGraph · MCP</small>
    </section>

    <section class="login-side">
      <form class="login-card" @submit.prevent="loginUser">
        <header><span class="login-mark">A</span><div><h2>欢迎回来</h2><p>登录 InsightPilot</p></div></header>
        <label>
          <span>账号</span>
          <input v-model="loginUsername" autocomplete="username" placeholder="请输入账号">
        </label>
        <label>
          <span>密码</span>
          <input v-model="loginPassword" type="password" autocomplete="current-password" placeholder="请输入密码">
        </label>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button class="login-submit" :disabled="loginLoading" type="submit">
          {{ loginLoading ? "正在登录…" : "登录" }}
        </button>

        <div class="mock-accounts">
          <p>演示账号</p>
          <button type="button" @click="useMockAccount('admin', 'admin123')">
            <span class="account-icon admin">管</span>
            <span><strong>admin</strong><small>管理员 · 全部数据</small></span>
            <code>admin123</code>
          </button>
          <button type="button" @click="useMockAccount('sales', 'sales123')">
            <span class="account-icon user">销</span>
            <span><strong>sales</strong><small>电商运营 · ecommerce_ops</small></span>
            <code>sales123</code>
          </button>
          <button type="button" @click="useMockAccount('mock', 'mock123')">
            <span class="account-icon mock">示</span>
            <span><strong>mock</strong><small>基础演示 · askdata_mock</small></span>
            <code>mock123</code>
          </button>
        </div>
        <small class="login-note">Mock 登录仅用于本地学习，不适合生产环境。</small>
      </form>
    </section>
  </main>

  <AgentConfiguration v-else-if="configurationOpen && authUser.role === 'admin'" @close="showConfiguration(false)" />

  <div v-else class="foundry-workbench" :class="{'resource-page-open':resourcePageOpen}">
    <aside class="workbench-sidebar" aria-label="工作区与历史会话">
      <div class="workbench-brand"><span class="workbench-logo"><Icon name="chart" :size="23" /></span><div><strong>InsightPilot</strong><small>经营分析工作台</small></div></div>
      <div class="sidebar-section-label">工作区资源</div>
      <nav class="resource-nav">
        <button :class="{selected:resourcePageOpen && resourceSection==='data'}" @click="openResource('data')"><Icon name="data" /><span>数据源</span><small>{{ datasources.length }}</small></button>
        <button :class="{selected:resourcePageOpen && resourceSection==='schema'}" @click="openResource('schema')"><Icon name="book" /><span>指标与数据口径</span><small>{{ schema.length }}</small></button>
        <button :class="{selected:resourcePageOpen && resourceSection==='skills'}" @click="openResource('skills')"><Icon name="skill" /><span>Skills</span><small>{{ skillCount }}</small></button>
        <button :class="{selected:resourcePageOpen && resourceSection==='tools'}" @click="openResource('tools')"><Icon name="tool" /><span>MCP 工具</span><small>{{ toolCount }}</small></button>
        <button :class="{selected:resourcePageOpen && resourceSection==='model'}" @click="openResource('model')"><Icon name="model" /><span>模型</span><small>{{ availableOptions?.models.length || 0 }}</small></button>
        <button :class="{selected:resourcePageOpen && resourceSection==='assets'}" @click="openResource('assets')"><Icon name="file" /><span>已保存结果</span><small>{{ historicalResultTables.length }}</small></button>
      </nav>
      <div class="sidebar-divider"></div>
      <button class="workbench-new" :disabled="workspaceLoading" @click="createConversation"><Icon name="plus" />新建分析<span>＋</span></button>

      <div class="history-heading"><span>分析会话</span><button title="打开已保存任务" aria-label="打开已保存任务" @click="showRestore=!showRestore"><Icon name="clock" :size="15" /></button></div>
      <form v-if="showRestore" class="restore-inline" @submit.prevent="restoreTask()"><input v-model="restoreTaskId" placeholder="输入任务编号" aria-label="任务编号"><button :disabled="loading || !restoreTaskId.trim()">打开</button></form>
      <label class="history-search"><Icon name="search" :size="14" /><input v-model="searchHistory" placeholder="搜索会话" aria-label="搜索会话"></label>
      <nav class="workbench-history" aria-label="分析会话">
        <section v-for="group in filteredGroups" :key="group.label"><p>{{ group.label }}</p><div v-for="item in group.items" :key="item.id" class="workbench-history-row" :class="{active:item.id===activeConversationId}"><button @click="selectConversation(item.id)"><Icon :name="item.branchOf ? 'branch' : 'chat'" :size="15" /><span>{{ item.title }}</span><i v-if="runForConversation(item.id)" class="running-dot"></i></button><button class="history-hide" :disabled="loading" aria-label="隐藏会话" @click="removeConversation(item.id)">×</button></div></section>
        <p v-if="!filteredGroups.length" class="muted-copy">没有匹配的会话</p>
      </nav>
      <div class="workbench-account"><span>{{ authUser.display_name.slice(0,1) }}</span><div><strong>{{ authUser.display_name }}</strong><small>{{ userRoleLabel }}</small></div><button v-if="authUser.role==='admin'" aria-label="管理 Agent 配置" title="管理 Agent 配置" @click="showConfiguration(true)"><Icon name="settings" :size="17" /></button><button :disabled="loading" @click="logoutUser">退出</button></div>
    </aside>

    <main v-show="!resourcePageOpen" class="workbench-chat">
      <header class="workbench-chat-header"><div><h1>{{ activeConversation?.title || '新建分析' }}</h1><span><i :class="{'running-dot':activeRun}"></i>{{ activeRun ? activeRun.stage : '经营分析' }}</span></div><button class="icon-button" aria-label="查看分析结果" @click="rightTab='results'"><Icon name="panel" /></button></header>
      <div class="workbench-scope"><Icon name="data" :size="14" /><button @click="openResource('data')">{{ currentDatasource?.label || '选择数据源' }}<span>⌄</span></button><span class="scope-ready">只读分析</span></div>
      <section ref="conversationScroll" class="workbench-conversation" aria-label="经营分析对话">
        <div v-if="activeConversation?.branchOf" class="branch-notice"><Icon name="branch" :size="15" />从任务 {{ activeConversation.branchOf }} 创建的独立分析分支。修改问题后重新分析。</div>
        <div v-if="!currentTurns.length && !activeRun" class="workbench-welcome"><span class="welcome-emblem"><Icon name="chart" :size="27" /></span><h2>从一个经营问题开始</h2><p>看清指标变化，追查原因，让结论回到数据。</p><div class="welcome-tags"><span>经营诊断</span><span>指标口径</span><span>证据追溯</span></div><div class="business-prompts"><button v-for="prompt in prompts" :key="prompt.title" @click="usePrompt(prompt.question)"><div class="prompt-heading"><Icon :name="prompt.icon" :size="17" /><strong>{{ prompt.title }}</strong><span>使用此提示 ↗</span></div><small>{{ prompt.description }}</small><p>{{ prompt.question }}</p></button></div><small class="welcome-footnote">默认已配置当前分析资源，也可以在下方调整。</small></div>
        <article v-for="turn in currentTurns" :key="turn.result.task_id" class="workbench-turn">
          <div class="question-bubble">{{ turn.query }}</div>
          <div class="assistant-label"><span class="assistant-mark"><Icon name="chart" :size="14" /></span><strong>InsightPilot</strong><small>{{ phaseLabel(turn.result) }}</small></div>
          <div v-if="turn.result.status==='waiting_clarification'" class="business-clarification"><strong>{{ turn.result.clarification?.question }}</strong><p>{{ turn.result.clarification?.reason }}</p><button v-for="option in turn.result.clarification?.options || []" :key="option.id" :disabled="loading" @click="usePrompt(option.label)">{{ option.label }}</button></div>
          <details v-if="turn.result.plan?.length" class="business-stages" open><summary><Icon name="check" :size="15" />分析阶段<span>{{ turn.result.plan.length }} 项</span></summary><ol><li v-for="step in turn.result.plan" :key="step.id"><span>{{ stageLabel(step) }}</span><p>{{ step.goal }}</p><small v-if="step.error">{{ step.error }}</small></li></ol></details>
          <p class="answer-preview">{{ answerPreview(turn.result) }}</p>
          <div v-if="turn.result.stop_reason" class="turn-warning">{{ turn.result.stop_reason }}</div>
          <button v-if="turn.result.report || turn.result.rows.length || turn.result.evidence?.length" class="result-open-card" @click="selectResult(turn.result)"><span class="result-card-icon"><Icon :name="turn.result.report ? 'file' : 'chart'" :size="22" /></span><div><strong>{{ turn.result.report?.title || turn.result.result_title || '分析结果与关键证据' }}</strong><small>{{ turn.result.report ? '报告 · 图表 · 关键证据' : '数据结果 · 指标口径' }}</small></div><Icon name="chevron" :size="16" /></button>
          <div class="turn-actions"><button @click="selectResult(turn.result,'evidence')"><Icon name="book" :size="14" />查看依据</button><button @click="referenceResult(turn.result)"><Icon name="chat" :size="14" />引用追问</button><button @click="forkResult(turn.result)"><Icon name="branch" :size="14" />创建分支</button><button v-if="turn.result.resumable || turn.result.clarification?.parameter==='resume'" :disabled="loading" @click="continueTask(turn.result)">从检查点继续</button></div>
        </article>
        <div v-if="activeRun" class="workbench-turn"><div class="question-bubble">{{ activeRun.query }}</div><div class="live-analysis" role="status"><i class="running-dot"></i><div><strong>{{ activeRun.stage }}</strong><p>{{ activeRun.status==='stopping' ? '当前调用结束后停止，已完成的证据会保留。' : '可以切换会话，分析会继续运行。' }}</p></div></div></div>
        <div v-for="run in runs.filter(r => r.session_id===activeConversationId && !isRunning(r) && !r.result)" :key="run.id" class="run-error"><strong>{{ run.query }}</strong><p>{{ run.stage }}{{ run.error ? '：'+run.error : '' }}</p><button @click="usePrompt(run.query)">修改后重新分析</button></div>
      </section>
      <footer class="workbench-composer">
        <div v-if="error" class="composer-error" role="alert">{{ error }}<button aria-label="关闭错误提示" @click="error=''">×</button></div>
        <div v-if="loading && !activeRun" class="other-run-notice">其他会话正在分析。可以先编辑问题，完成或停止后再发送。</div>
        <div class="workbench-input-box"><div v-if="selectedAnalysisTables.length || referenceTaskTitle.length || referenceText" class="reference-chips"><span v-for="table in selectedAnalysisTables" :key="table.taskId"><Icon name="file" :size="12" />{{ table.title }}<button aria-label="移除引用" @click="removeAnalysisTable(table.taskId)">×</button></span><span v-for="reference in referenceTaskTitle" :key="reference.id"><Icon name="file" :size="12" />{{ reference.title }}<button aria-label="移除分析引用" @click="removeAnalysisTable(reference.id)">×</button></span><span v-if="referenceText">{{ referenceText.slice(0,48) }}<button aria-label="移除文本引用" @click="referenceText=''">×</button></span></div><textarea ref="composerInput" v-model="input" rows="2" maxlength="500" placeholder="描述你想了解的经营问题…" aria-label="经营分析问题" @keydown="onComposerKey"></textarea><div class="composer-config"><div class="composer-resources">
          <ResourcePicker id="composer-data" label="数据源" icon="data" :options="sourceChoices" :selected="[datasourceId]" :locked="loading || workspaceLoading || currentTurns.length>0" :description="currentTurns.length ? '当前会话的数据源已锁定。新建分析可重新选择。' : '为新分析选择一个已授权的数据源。'" @select="selectComposerSource" @manage="openResource('data')" />
          <ResourcePicker id="composer-skills" label="Skills" icon="skill" :options="skillChoices" :selected="runtimeOptions.skills" :count="skillCount" :locked="loading || workspaceLoading" multiple description="按需启用分析方法，基础查询与知识问答始终保留。" @select="toggleOption('skills',$event)" @manage="openResource('skills')" />
          <ResourcePicker id="composer-mcp" label="MCP" icon="tool" :options="toolChoices" :selected="runtimeOptions.tools" :count="toolCount" :locked="loading || workspaceLoading" multiple description="选择本次分析可调用的工具。" @select="toggleOption('tools',$event)" @manage="openResource('tools')" />
        </div><button v-if="activeRun" class="send-analysis stop-analysis" :disabled="activeRun.status==='stopping'" aria-label="停止分析" @click="stopAnalysis"><Icon name="stop" :size="16" /></button><button v-else class="send-analysis" :disabled="!input.trim() || loading || workspaceLoading || !availableOptions" aria-label="发送分析问题" @click="submit()"><Icon name="arrow" :size="18" /></button></div><div class="composer-model"><Icon name="model" :size="13" /><select v-model="runtimeOptions.model" :disabled="loading || !availableOptions" aria-label="本次分析模型"><option v-for="model in availableOptions?.models || []" :key="model" :value="model">{{ model }}</option></select><span>Enter 发送 · Shift + Enter 换行</span></div></div><p class="composer-footnote">结论以数据与指标口径为依据，缺少的信息会向你确认。</p>
      </footer>
    </main>

    <aside v-show="!resourcePageOpen" class="workbench-results" :class="{'result-expanded':fullResult}" aria-label="分析结果与资源"><header class="results-header"><div><Icon name="panel" :size="17" /><strong>分析工作区</strong></div><button class="icon-button" :aria-label="fullResult ? '收起结果' : '展开结果'" @click="fullResult=!fullResult"><Icon :name="fullResult ? 'close' : 'expand'" :size="17" /></button></header><nav class="results-tabs" aria-label="分析工作区标签"><button :class="{active:rightTab==='results'}" @click="rightTab='results'">分析结果</button><button :class="{active:rightTab==='evidence'}" @click="rightTab='evidence'">查看依据</button></nav><div class="results-scroll">
      <select v-if="currentTurns.length>1" v-model="selectedTaskId" class="result-picker" aria-label="选择分析结果"><option value="">最近一次分析</option><option v-for="turn in currentTurns" :key="turn.result.task_id" :value="turn.result.task_id">{{ turn.query.slice(0,40) }}</option></select><WorkbenchResult :result="focusedResult" :sources="reportDataSources" :tab="rightTab" :busy="loading" :source-label="currentDatasource?.label || ''" :coverage="scenarioLabel" @save="saveResult" @reference="referenceResult" @continue="continueTask" @fork="forkResult" />

    </div></aside>

    <main v-if="resourcePageOpen" class="workbench-resource-page" aria-label="工作区资源管理">
      <header class="resource-page-header">
        <button class="icon-button resource-back" aria-label="返回分析" @click="resourcePageOpen=false"><Icon name="chevron" /></button>
        <span class="resource-page-icon"><Icon :name="resourcePage.icon" :size="24" /></span>
        <div><h1>{{ resourcePage.title }}</h1><p>{{ resourceSection==='assets' ? '工作区资产' : '工作区配置' }}</p></div>
      </header>
      <div class="resource-page-scroll">
        <p class="resource-page-description">{{ resourcePage.description }}</p>
        <p v-if="error" class="composer-error" role="alert">{{ error }}</p>
      <section class="resource-content">
        <template v-if="resourceSection==='data'"><h3>当前数据源</h3><p class="resource-description">默认使用经营分析数据，可以切换到其他已授权的数据源。</p><div class="resource-option-grid"><label v-for="source in datasources" :key="source.id" class="resource-option"><input type="radio" :value="source.id" v-model="datasourceId" :disabled="loading || workspaceLoading" @change="changeDatasource"><span><strong>{{ source.label }}</strong><small>{{ source.id }}</small></span></label></div><div class="resource-note">{{ scenarioLabel || '此数据源未提供覆盖时间，请在分析中核实。' }}</div><p class="muted-copy">切换后展示对应数据源的会话和结果。</p></template>
        <template v-else-if="resourceSection==='model'"><h3>本次分析模型</h3><p class="resource-description">选择已配置的模型。本次选择用于新的模型请求，历史分析保持原样。</p><div class="resource-option-grid"><label v-for="model in availableOptions?.models || []" :key="model" class="resource-option"><input type="radio" v-model="runtimeOptions.model" :value="model" :disabled="loading"><span><strong>{{ model }}</strong><small>{{ model===availableOptions?.default_model ? '默认模型' : '可选模型' }}</small></span></label></div></template>
        <template v-else-if="resourceSection==='skills'"><h3>分析 Skills</h3><p class="resource-description">默认启用全部业务方法，Agent 根据问题按需使用。查询与知识问答作为基础能力保留。</p><div class="resource-option-grid"><label v-for="skill in availableOptions?.skills || []" :key="skill.name" class="resource-option"><input type="checkbox" :checked="!runtimeOptions.skills || runtimeOptions.skills.includes(skill.name) || ['database_query','data_qa'].includes(skill.name)" :disabled="loading || ['database_query','data_qa'].includes(skill.name)" @change="toggleOption('skills',skill.name)"><span><strong>{{ skillLabels[skill.name] || skill.name }}</strong><small>{{ skill.description }}</small></span></label></div><button v-if="authUser.role==='admin'" class="resource-manage" @click="showConfiguration(true)">管理 Skills 与提示词 <Icon name="chevron" :size="14" /></button></template>
        <template v-else-if="resourceSection==='tools'"><h3>MCP 工具</h3><p class="resource-description">默认启用当前数据源已授权的工具。关闭后，本次分析不能调用该工具。</p><div class="resource-option-grid"><label v-for="tool in availableOptions?.tools || []" :key="tool.name" class="resource-option"><input type="checkbox" :checked="!runtimeOptions.tools || runtimeOptions.tools.includes(tool.name)" :disabled="loading" @change="toggleOption('tools',tool.name)"><span><strong>{{ tool.title || tool.name }}</strong><small>{{ tool.description }}</small></span></label></div><p v-if="!availableOptions?.tools.length" class="muted-copy">当前数据源没有可用的 MCP 工具。</p></template>
        <template v-else-if="resourceSection==='assets'"><h3>已保存结果</h3><p class="resource-description">只收录主动保存的结果，中间执行记录在“查看依据”中。</p><p v-if="!historicalResultTables.length" class="muted-copy">还没有保存的结果。</p><button v-for="table in historicalResultTables" :key="table.taskId" class="saved-result-item" @click="restoreTask(table.taskId);rightTab='results';resourcePageOpen=false"><Icon name="file" /><span><strong>{{ table.title }}</strong><small>{{ table.rowCount }} 行 · {{ table.query }}</small></span><Icon name="chevron" :size="14" /></button></template>
        <template v-else><h3>指标与数据口径</h3><p class="resource-description">分析会自动查找相关口径。需要检查表结构或限定字段时，可在这里展开。</p><details v-for="table in schema" :key="table.id" class="schema-resource"><summary>{{ table.label }}<small>{{ table.fields.length }} 字段</small></summary><p>{{ table.description }}</p><div v-for="field in table.fields" :key="field.name" class="schema-resource-field"><span><strong>{{ field.label || field.name }}</strong><small>{{ field.description || field.name }}</small></span><button :disabled="loading" @click="addConfirmedField(field.name,table.id)">引用字段</button></div></details><details class="advanced-context"><summary>高级上下文与收藏字段</summary>      <section class="context-block query-context-block">
        <div class="context-heading">
          <div><strong>查询字段</strong><small>用于生成 SQL</small></div>
          <em>{{ confirmedFields.length }} 个</em>
        </div>
        <div
          class="confirmed-fields-zone"
          :class="{ filled: confirmedFields.length }"
          @dragover.prevent
          @drop.prevent="dropConfirmedField"
        >
          <div v-if="!confirmedFields.length" class="empty-confirmation">
            <span>＋</span>
            <strong>拖入字段</strong>
          </div>
          <div v-for="field in confirmedFields" :key="`${field.tableId}:${field.name}`" class="confirmed-field">
            <span class="field-badge">{{ isNumericField(field.name, field.tableId) ? "#" : "T" }}</span>
            <span class="confirmed-field-name"><strong>{{ fieldLabel(field.name, field.tableId) }}</strong><small>{{ tableLabel(field.tableId) }}</small></span>
            <button class="save-field" :class="{ saved: isSavedField(field.name, field.tableId) }" :aria-label="`保存${fieldLabel(field.name, field.tableId)}`" @click="toggleConfirmedFieldMemory(field.name, field.tableId)">{{ isSavedField(field.name, field.tableId) ? "★" : "☆" }}</button>
            <button class="remove-field" :aria-label="`移除${fieldLabel(field.name, field.tableId)}`" @click="removeConfirmedField(field.name, field.tableId)">×</button>
          </div>
        </div>
      </section>

      <section class="context-block analysis-context-block">
        <div class="context-heading">
          <div><strong>参考结果</strong><small>用于综合分析</small></div>
          <em>{{ selectedAnalysisTables.length }} 张</em>
        </div>
        <div
          class="analysis-tables-zone"
          :class="{ filled: selectedAnalysisTables.length }"
          @dragover.prevent
          @drop.prevent="dropAnalysisTable"
        >
          <div v-if="!selectedAnalysisTables.length" class="empty-confirmation">
            <span>＋</span>
            <strong>拖入已保存结果</strong>
          </div>
          <div v-for="table in selectedAnalysisTables" :key="table.taskId" class="analysis-table-chip">
            <span class="table-mark">▦</span>
            <span><strong>{{ table.title }}</strong><small>{{ table.rowCount }} 行 · {{ table.columns.slice(0, 3).join(" / ") }}</small></span>
            <button :aria-label="`移除${table.title}`" @click="removeAnalysisTable(table.taskId)">×</button>
          </div>
        </div>
      </section>

      <section class="recent-library">
        <div class="library-heading">
          <div><strong>字段与结果</strong><small>拖拽或双击添加</small></div>
        </div>
        <div class="recent-columns">
          <div class="recent-column">
            <p>字段 · 最多 {{ FIELD_LIBRARY_LIMIT }} 个</p>
            <div v-if="!fieldLibrary.length" class="library-empty">查询后可收藏关键字段</div>
            <div
              v-for="item in fieldLibrary"
              :key="`${item.tableId}:${item.field.name}`"
              class="field-library-row"
              draggable="true"
              @dragstart="dragField($event, item.field, item.tableId)"
            >
              <button class="recent-card field-card" @dblclick="addConfirmedField(item.field.name, item.tableId)">
                <span class="field-badge">{{ item.field.type === "数值" || item.field.type === "整数" ? "#" : "T" }}</span>
                <span><strong>{{ item.field.label }}</strong><small>{{ tableLabel(item.tableId) }}</small></span>
              </button>
              <button class="save-field" :class="{ saved: item.saved }" :aria-label="`${item.saved ? '取消收藏' : '收藏'}${item.field.label}`" @click="toggleSavedField(item.field, item.tableId)">{{ item.saved ? "★" : "☆" }}</button>
            </div>
          </div>
          <div class="recent-column">
            <p>结果表 · 最多 {{ SAVED_TABLE_LIMIT }} 张</p>
            <div v-if="!historicalResultTables.length" class="library-empty">保存查询结果后显示</div>
            <button
              v-for="table in historicalResultTables"
              :key="table.taskId"
              class="recent-card table-card"
              draggable="true"
              @dragstart="dragAnalysisTable($event, table)"
              @dblclick="addAnalysisTable(table.taskId)"
            >
              <span class="table-mark">▦</span>
              <span><strong>{{ table.title }}</strong><small>{{ table.rowCount }} 行 · {{ table.query }}</small></span>
              <em>⋮⋮</em>
            </button>
          </div>
        </div>
      </section>
</details></template>
        <div class="runtime-config-summary"><Icon name="settings" :size="14" /><span>{{ optionSummary }}</span><button :disabled="loading" @click="runtimeOptions=defaultOptions()">恢复默认</button></div>
      </section>
      </div>
    </main>
  </div>
</template>
