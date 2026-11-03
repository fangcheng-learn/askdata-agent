<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'

type Section = 'agents' | 'skills' | 'policies'
interface AgentEntry {
  id: string
  label: string
  description: string
  model: string
  node_names: string[]
  prompt_ids: string[]
  skill_ids: string[]
  context_inputs: string[]
  loading_rule: string
}
interface PromptEntry {
  id: string
  agent_id: string
  label: string
  description: string
  default_text: string
  effective_text: string
  source: string
  overridden: boolean
  editable: boolean
  required_placeholders?: string[]
  assembly_notes?: string[]
  assembled_text?: string
}
interface SkillEntry {
  id: string
  label: string
  description: string
  default_text: string
  effective_text: string
  source: string
  overridden: boolean
  editable: boolean
  allowed_tools: string[]
  used_by: string[]
  loading_rule: string
}
interface AgentConfiguration {
  revision: string
  updated_at: string | null
  updated_by: string | null
  agents: AgentEntry[]
  prompts: PromptEntry[]
  skills: SkillEntry[]
  policies: { id: string; label: string; description: string }[]
}
type DocumentEntry = (PromptEntry | SkillEntry) & {
  kind: 'prompts' | 'skills'
  required_placeholders?: string[]
  assembly_notes?: string[]
  assembled_text?: string
}

const emit = defineEmits<{ close: [] }>()
const configuration = ref<AgentConfiguration | null>(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const search = ref('')
const section = ref<Section>('agents')
const selectedAgentId = ref('')
const selectedPromptId = ref('')
const selectedSkillId = ref('')
const draft = ref('')
const editing = ref(false)
const restoreDefaultPending = ref(false)
const pendingAction = ref<(() => void) | null>(null)
const pendingLabel = ref('')
const editor = ref<HTMLTextAreaElement | null>(null)
const unsavedPanel = ref<HTMLElement | null>(null)

const selectedAgent = computed(() => configuration.value?.agents.find(item => item.id === selectedAgentId.value))
const agentPrompts = computed(() => configuration.value?.prompts.filter(item =>
  selectedAgent.value?.prompt_ids.includes(item.id)) ?? [])
const selectedSkill = computed(() => configuration.value?.skills.find(item => item.id === selectedSkillId.value))
const selectedDocument = computed<DocumentEntry | null>(() => {
  if (section.value === 'agents') {
    const prompt = agentPrompts.value.find(item => item.id === selectedPromptId.value)
    return prompt ? { ...prompt, kind: 'prompts' } : null
  }
  if (section.value === 'skills' && selectedSkill.value) return { ...selectedSkill.value, kind: 'skills' }
  return null
})
const dirty = computed(() => Boolean(selectedDocument.value &&
  (draft.value !== selectedDocument.value.effective_text || restoreDefaultPending.value)))
const searchTerm = computed(() => search.value.trim().toLocaleLowerCase())
const matches = (entry: { id: string; label: string; description: string }) =>
  !searchTerm.value || `${entry.label} ${entry.id} ${entry.description}`.toLocaleLowerCase().includes(searchTerm.value)
const filteredAgents = computed(() => configuration.value?.agents.filter(matches) ?? [])
const filteredSkills = computed(() => configuration.value?.skills.filter(matches) ?? [])
const filteredPolicies = computed(() => configuration.value?.policies.filter(matches) ?? [])
const overrideCount = computed(() => [...(configuration.value?.prompts ?? []), ...(configuration.value?.skills ?? [])]
  .filter(item => item.overridden).length)
const updatedLabel = computed(() => {
  if (!configuration.value?.updated_at) return '当前使用默认配置'
  const date = new Date(configuration.value.updated_at)
  const formatted = Number.isNaN(date.getTime()) ? configuration.value.updated_at : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai', hour12: false,
  }).format(date)
  return `更新于 ${formatted}${configuration.value.updated_by ? ` · ${configuration.value.updated_by}` : ''}`
})
const countLines = computed(() => draft.value ? draft.value.split('\n').length : 0)

function resetEditor() {
  draft.value = selectedDocument.value?.effective_text ?? ''
  editing.value = false
  restoreDefaultPending.value = false
  error.value = ''
  pendingAction.value = null
}
function guarded(action: () => void, label: string) {
  if (saving.value) return
  if (dirty.value) {
    pendingAction.value = action
    pendingLabel.value = label
    void nextTick(() => unsavedPanel.value?.scrollIntoView({ block: 'nearest' }))
    return
  }
  action()
}
function selectSection(next: Section) {
  if (section.value === next) return
  guarded(() => {
    section.value = next
    search.value = ''
    resetEditor()
    notice.value = ''
  }, '切换配置分类')
}
function selectAgent(agent: AgentEntry) {
  if (selectedAgentId.value === agent.id && section.value === 'agents') return
  guarded(() => {
    section.value = 'agents'
    selectedAgentId.value = agent.id
    selectedPromptId.value = agent.prompt_ids[0] ?? ''
    resetEditor()
    notice.value = ''
  }, `查看${agent.label}`)
}
function selectPrompt(id: string) {
  if (selectedPromptId.value === id) return
  guarded(() => {
    selectedPromptId.value = id
    resetEditor()
    notice.value = ''
  }, '切换提示词')
}
function selectSkill(skill: SkillEntry) {
  if (selectedSkillId.value === skill.id && section.value === 'skills') return
  guarded(() => {
    if (section.value !== 'skills') search.value = ''
    section.value = 'skills'
    selectedSkillId.value = skill.id
    resetEditor()
    notice.value = ''
  }, `查看${skill.label}`)
}
function openSkill(id: string) {
  const skill = configuration.value?.skills.find(item => item.id === id)
  if (skill) selectSkill(skill)
}
function agentLabel(id: string) {
  return configuration.value?.agents.find(item => item.id === id)?.label ?? id
}
function skillLabel(id: string) {
  return configuration.value?.skills.find(item => item.id === id)?.label ?? id
}
async function loadConfiguration() {
  loading.value = true
  error.value = ''
  try {
    configuration.value = await api.agentConfiguration() as AgentConfiguration
    if (!configuration.value.agents.some(item => item.id === selectedAgentId.value)) {
      selectedAgentId.value = configuration.value.agents[0]?.id ?? ''
    }
    if (!agentPrompts.value.some(item => item.id === selectedPromptId.value)) {
      selectedPromptId.value = agentPrompts.value[0]?.id ?? ''
    }
    if (!configuration.value.skills.some(item => item.id === selectedSkillId.value)) {
      selectedSkillId.value = configuration.value.skills[0]?.id ?? ''
    }
    resetEditor()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '配置读取失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}
async function saveChanges(): Promise<boolean> {
  const document = selectedDocument.value
  if (!document || !configuration.value || !dirty.value || saving.value || !document.editable) return false
  if (!restoreDefaultPending.value && !draft.value.trim()) {
    error.value = '正文不能为空。需要撤销自定义内容时，请使用恢复默认。'
    return false
  }
  saving.value = true
  error.value = ''
  const wasRestore = restoreDefaultPending.value
  try {
    const changes = { [document.id]: wasRestore ? null : draft.value }
    configuration.value = await api.saveAgentConfiguration({
      expected_revision: configuration.value.revision,
      prompts: document.kind === 'prompts' ? changes : {},
      skills: document.kind === 'skills' ? changes : {},
    }) as AgentConfiguration
    draft.value = selectedDocument.value?.effective_text ?? ''
    restoreDefaultPending.value = false
    editing.value = false
    notice.value = `${wasRestore ? '已恢复默认并保存' : '已保存'}。后续新任务使用此配置，正在运行的任务保留原配置。`
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败，请重试。'
    return false
  } finally {
    saving.value = false
  }
}
function restoreDefault() {
  const document = selectedDocument.value
  if (!document?.editable || saving.value) return
  draft.value = document.default_text
  restoreDefaultPending.value = true
  editing.value = true
  error.value = ''
  notice.value = '已载入默认内容，点击保存后恢复生效。'
}
function discardChanges() {
  resetEditor()
  notice.value = '已放弃未保存的修改。'
}
async function saveAndProceed() {
  const action = pendingAction.value
  if (await saveChanges()) {
    pendingAction.value = null
    action?.()
  }
}
function discardAndProceed() {
  const action = pendingAction.value
  resetEditor()
  action?.()
}
async function copyText() {
  try {
    await navigator.clipboard.writeText(draft.value)
    notice.value = '已复制全文。'
  } catch {
    editor.value?.focus()
    editor.value?.select()
    notice.value = '已选中全文，请使用键盘复制。'
  }
}
function guardUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => {
  void loadConfiguration()
  window.addEventListener('beforeunload', guardUnload)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', guardUnload))
</script>

<template>
  <main class="agent-configuration" aria-labelledby="agent-config-title">
    <header class="configuration-heading">
      <div class="configuration-title">
        <button class="back-link" type="button" @click="guarded(() => emit('close'), '返回对话')"><span aria-hidden="true">←</span> 返回对话</button>
        <div class="title-line"><h1 id="agent-config-title">Agent 配置</h1><span class="configuration-access">管理员</span></div>
        <p>查看每个环节的实际提示词，以及业务方法如何被读取和使用。</p>
      </div>
      <div class="configuration-heading-actions">
        <small>{{ updatedLabel }}</small>
        <button class="quiet-button" type="button" :disabled="loading || saving" @click="guarded(() => { void loadConfiguration() }, '重新读取配置')">重新读取</button>
      </div>
    </header>

    <div v-if="error" class="configuration-message error-message" role="alert">
      <div><strong>操作未完成</strong><p>{{ error }}</p><small v-if="configuration">未保存的正文仍保留。版本冲突时，可先复制正文，再重新读取最新配置。</small></div>
      <button v-if="!configuration && !loading" class="quiet-button" type="button" @click="loadConfiguration">重试</button>
    </div>
    <div v-if="notice" class="configuration-message notice-message" role="status">{{ notice }}</div>

    <section v-if="loading && !configuration" class="configuration-loading" aria-label="正在读取配置" aria-busy="true">
      <div class="skeleton-heading"></div><div class="skeleton-subheading"></div>
      <div class="skeleton-layout"><div></div><div></div></div>
      <span>读取当前应用配置…</span>
    </section>

    <template v-else-if="configuration">
      <section class="configuration-flow" aria-label="经营调查的职责分工">
        <div class="flow-intro"><strong>一次经营调查</strong><small>各环节共用任务与证据</small></div>
        <ol>
          <li><b>01</b><span>Flash<small>理解与路由</small></span></li>
          <li><b>02</b><span>经营 ReAct<small>选择 Skill · 决定取证</small></span></li>
          <li><b>03</b><span>SQL 模块<small>生成与执行查询</small></span></li>
          <li><b>04</b><span>DataQa<small>整理报告草稿</small></span></li>
          <li><b>05</b><span>Reviewer<small>最终交付审核</small></span></li>
        </ol>
      </section>

      <section v-if="pendingAction" ref="unsavedPanel" class="unsaved-guard" role="alert" aria-label="有尚未保存的修改">
        <div><strong>当前正文还没有保存</strong><p>保存或放弃修改后，再{{ pendingLabel }}。</p></div>
        <div class="guard-actions">
          <button class="primary-button" type="button" :disabled="saving" @click="saveAndProceed">{{ saving ? '保存中…' : '保存并继续' }}</button>
          <button class="quiet-button" type="button" :disabled="saving" @click="discardAndProceed">放弃并继续</button>
          <button class="text-button" type="button" :disabled="saving" @click="pendingAction = null">留在本条</button>
        </div>
      </section>

      <div class="configuration-workspace" :aria-busy="loading || saving">
        <aside class="configuration-navigation">
          <nav class="configuration-tabs" aria-label="配置分类">
            <button type="button" :class="{ active: section === 'agents' }" :aria-current="section === 'agents' ? 'page' : undefined" @click="selectSection('agents')">Agent <span>{{ configuration.agents.length }}</span></button>
            <button type="button" :class="{ active: section === 'skills' }" :aria-current="section === 'skills' ? 'page' : undefined" @click="selectSection('skills')">Skill <span>{{ configuration.skills.length }}</span></button>
            <button type="button" :class="{ active: section === 'policies' }" :aria-current="section === 'policies' ? 'page' : undefined" @click="selectSection('policies')">执行规则</button>
          </nav>
          <label class="configuration-search"><span class="sr-only">搜索名称或说明</span><span aria-hidden="true">⌕</span><input v-model="search" type="search" placeholder="搜索名称或说明" /></label>
          <div class="configuration-list">
            <template v-if="section === 'agents'">
              <button v-for="agent in filteredAgents" :key="agent.id" type="button" class="configuration-list-item" :class="{ selected: selectedAgentId === agent.id }" :aria-current="selectedAgentId === agent.id ? 'page' : undefined" @click="selectAgent(agent)">
                <span class="item-title"><strong>{{ agent.label }}</strong><span aria-hidden="true">↗</span></span>
                <span class="item-description">{{ agent.description }}</span>
                <code>{{ agent.model || '代码组件' }}</code>
              </button>
              <p v-if="!filteredAgents.length" class="list-empty">没有匹配的 Agent。</p>
            </template>
            <template v-else-if="section === 'skills'">
              <button v-for="skill in filteredSkills" :key="skill.id" type="button" class="configuration-list-item" :class="{ selected: selectedSkillId === skill.id }" :aria-current="selectedSkillId === skill.id ? 'page' : undefined" @click="selectSkill(skill)">
                <span class="item-title"><strong>{{ skill.label }}</strong><span v-if="skill.overridden" class="custom-mark">自定义</span></span>
                <span class="item-description">{{ skill.description }}</span><code>{{ skill.id }}</code>
              </button>
              <p v-if="!filteredSkills.length" class="list-empty">没有匹配的 Skill。</p>
            </template>
            <template v-else>
              <div class="policy-navigation-note"><strong>由程序执行</strong><p>这里展示预算、权限和运行约束，修改提示词不会改变这些代码规则。</p></div>
            </template>
          </div>
          <footer class="configuration-nav-footer"><span>{{ overrideCount }} 项自定义配置</span><code :title="configuration.revision">版本 {{ configuration.revision.slice(0, 12) }}</code></footer>
        </aside>

        <article class="configuration-detail">
          <template v-if="section === 'policies'">
            <header class="detail-heading"><div><span class="eyebrow">CONTROLLER</span><h2>执行规则</h2><p>这些约束由程序管理，供你核对当前运行边界。</p></div><span class="read-only-label">只读</span></header>
            <dl class="policy-list"><div v-for="policy in filteredPolicies" :key="policy.id"><dt>{{ policy.label }}</dt><dd>{{ policy.description }}</dd></div></dl>
            <p v-if="!filteredPolicies.length" class="detail-empty">没有匹配的执行规则。</p>
          </template>
          <template v-else>
            <header class="detail-heading">
              <div><span class="eyebrow">{{ section === 'agents' ? 'AGENT' : 'SKILL' }}</span><h2>{{ section === 'agents' ? selectedAgent?.label : selectedSkill?.label }}</h2><p>{{ section === 'agents' ? selectedAgent?.description : selectedSkill?.description }}</p></div>
              <span v-if="section === 'agents' && selectedAgent?.model" class="model-label"><i></i>{{ selectedAgent.model }}</span>
            </header>

            <section v-if="section === 'agents' && selectedAgent" class="agent-metadata" aria-label="Agent 使用的资料">
              <div><h3>职责与加载规则</h3><p>{{ selectedAgent.loading_rule }}</p></div>
              <div v-if="selectedAgent.context_inputs.length"><h3>每次调用还会组装</h3><ul class="context-tags"><li v-for="input in selectedAgent.context_inputs" :key="input">{{ input }}</li></ul></div>
              <div v-if="selectedAgent.skill_ids.length"><h3>关联 Skill</h3><div class="linked-skills"><button v-for="id in selectedAgent.skill_ids" :key="id" type="button" @click="openSkill(id)">{{ skillLabel(id) }} <span aria-hidden="true">↗</span></button></div></div>
              <details v-if="selectedAgent.node_names.length" class="node-details"><summary>调用节点</summary><code>{{ selectedAgent.node_names.join(' · ') }}</code></details>
            </section>
            <section v-if="section === 'skills' && selectedSkill" class="agent-metadata" aria-label="Skill 加载方式">
              <div><h3>何时加载</h3><p>{{ selectedSkill.loading_rule }}</p></div>
              <div v-if="selectedSkill.used_by.length"><h3>使用环节</h3><p>{{ selectedSkill.used_by.map(agentLabel).join('、') }}</p></div>
              <div v-if="selectedSkill.allowed_tools.length"><h3>登记工具</h3><ul class="context-tags"><li v-for="tool in selectedSkill.allowed_tools" :key="tool"><code>{{ tool }}</code></li></ul></div>
            </section>

            <nav v-if="section === 'agents' && agentPrompts.length > 1" class="prompt-modes" aria-label="提示词模式">
              <button v-for="prompt in agentPrompts" :key="prompt.id" type="button" :class="{ active: selectedPromptId === prompt.id }" :aria-current="selectedPromptId === prompt.id ? 'page' : undefined" @click="selectPrompt(prompt.id)">{{ prompt.label }}<span v-if="prompt.overridden" class="mode-custom-dot" aria-label="已自定义"></span></button>
            </nav>

            <section v-if="selectedDocument" class="document-editor" aria-label="提示词与 Skill 正文">
              <header class="document-toolbar"><div><h3>{{ selectedDocument.label }}{{ section === 'agents' ? ' · 提示词' : ' · 正文' }}</h3><span class="document-state" :class="{ custom: selectedDocument.overridden, modified: dirty }">{{ dirty ? '有未保存修改' : selectedDocument.overridden ? '自定义配置' : '默认配置' }}</span></div><div class="document-tools"><button type="button" class="text-button" @click="copyText">复制全文</button><button v-if="selectedDocument.editable && !editing" type="button" class="quiet-button" :disabled="saving" @click="editing = true">编辑正文</button><span v-else-if="!selectedDocument.editable" class="read-only-label">只读</span></div></header>
              <p class="document-explanation">{{ selectedDocument.description }}</p>
              <div v-if="selectedDocument.required_placeholders?.length" class="template-markers"><strong>组装标记</strong><code v-for="marker in selectedDocument.required_placeholders" :key="marker">{{ marker }}</code><small>保存时需保留，用于注入实际 Skill 或工具说明。</small></div>
              <div class="editor-surface" :class="{ editing }"><label class="sr-only" for="agent-config-document">{{ selectedDocument.label }}正文</label><textarea id="agent-config-document" ref="editor" v-model="draft" :readonly="!editing || !selectedDocument.editable || saving" :spellcheck="false" wrap="soft" @input="restoreDefaultPending = false"></textarea></div>
              <div class="document-footnote"><span>{{ countLines }} 行 · {{ draft.length.toLocaleString('zh-CN') }} 字符</span><span>{{ editing ? '编辑中' : '当前生效正文' }}</span></div>
              <details v-if="selectedDocument.assembled_text || selectedDocument.assembly_notes?.length" class="source-details assembly-preview"><summary>查看组装预览与输入说明</summary><p>以下预览使用已保存配置和当前 Skill，未保存的修改尚未进入预览。任务、检索资料和查询证据会在实际调用时另行组装。</p><ul v-if="selectedDocument.assembly_notes?.length"><li v-for="note in selectedDocument.assembly_notes" :key="note">{{ note }}</li></ul><pre v-if="selectedDocument.assembled_text">{{ selectedDocument.assembled_text }}</pre></details>
              <details class="source-details"><summary>配置来源与默认正文</summary><p><strong>默认来源</strong><code>{{ selectedDocument.source }}</code></p><p>自定义内容另行保存，恢复默认会重新使用应用内置正文。</p><pre>{{ selectedDocument.default_text }}</pre></details>
              <footer v-if="selectedDocument.editable" class="document-save-bar"><div><strong>{{ restoreDefaultPending ? '默认内容待保存' : dirty ? '修改尚未生效' : '保存后用于后续新任务' }}</strong><small>已有任务保留原配置；业务数据、权限和预算仍由程序管理。</small></div><div class="save-actions"><button type="button" class="text-button" :disabled="saving || (!selectedDocument.overridden && !dirty)" @click="restoreDefault">恢复默认</button><button v-if="dirty" type="button" class="quiet-button" :disabled="saving" @click="discardChanges">放弃修改</button><button type="button" class="primary-button" :disabled="saving || !dirty || !draft.trim()" @click="saveChanges">{{ saving ? '保存中…' : '保存配置' }}</button></div></footer>
            </section>
            <p v-else class="detail-empty">此环节没有单独可编辑的提示词，请查看上方职责和输入说明。</p>
          </template>
        </article>
      </div>
      <footer class="configuration-page-note">此页展示固定提示词和运行时注册的 Skill。实际调用还会加入当前任务、授权指标与 Schema、已取得证据等上下文，按当前需要组装。</footer>
    </template>
  </main>
</template>

<style scoped>
.agent-configuration { --config-line: #e0e6df; --config-muted: #738077; --config-green: #22644e; width: 100%; height: 100dvh; overflow-y: auto; padding: 30px clamp(20px, 3.8vw, 64px) 20px; color: #24382e; background: #f7f8f4; font-size: 13px; }
.agent-configuration button { border: 0; transition: background .16s, color .16s, border-color .16s; }
.agent-configuration button:focus-visible, .agent-configuration input:focus-visible, .agent-configuration textarea:focus-visible, .agent-configuration summary:focus-visible { outline: 2px solid #629b81; outline-offset: 3px; }
.agent-configuration button:disabled { opacity: .48; cursor: default; }
.configuration-heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; max-width: 1440px; margin: 0 auto 26px; }
.back-link { display: inline-flex; align-items: center; gap: 8px; padding: 0 0 17px; color: #66766b; background: transparent; font-size: 12px; }
.back-link:hover { color: var(--config-green); }
.back-link span { font-size: 20px; }
.title-line { display: flex; align-items: center; gap: 14px; }
h1 { margin: 0; font-size: 28px; font-weight: 650; letter-spacing: -.7px; }
.configuration-access { border-left: 1px solid #d3ddd2; padding-left: 13px; color: #788374; font-size: 11px; }
.configuration-title > p { margin: 10px 0 0; color: var(--config-muted); line-height: 1.7; }
.configuration-heading-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 12px; }
.configuration-heading-actions small { color: #7d887c; font-size: 11px; }
.quiet-button, .primary-button { padding: 8px 13px; border-radius: 6px; font-size: 12px; line-height: 1.4; white-space: nowrap; }
.quiet-button { color: #425d4e; background: #edf1e9; }
.quiet-button:hover:not(:disabled) { background: #e1e9dc; }
.primary-button { color: white; background: var(--config-green); }
.primary-button:hover:not(:disabled) { background: #174b3a; }
.text-button { padding: 5px 7px; color: #687b6e; background: transparent; font-size: 12px; white-space: nowrap; }
.text-button:hover:not(:disabled) { color: #234e3d; background: #eff3ed; border-radius: 4px; }
.configuration-message { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; max-width: 1440px; margin: 0 auto 18px; padding: 13px 17px; border-left: 3px solid; border-radius: 0 5px 5px 0; line-height: 1.6; }
.configuration-message p { margin: 3px 0; }
.configuration-message small { display: block; font-size: 11px; }
.error-message { color: #914330; background: #fbefea; border-color: #c68068; }
.notice-message { color: #356b4b; background: #eaf1e6; border-color: #79a478; }
.configuration-flow { display: grid; grid-template-columns: 170px minmax(0, 1fr); align-items: center; gap: 28px; max-width: 1440px; margin: 0 auto 23px; padding: 19px 22px; border-top: 1px solid #d9e1d5; border-bottom: 1px solid #d9e1d5; }
.flow-intro strong, .flow-intro small { display: block; }
.flow-intro strong { font-size: 13px; font-weight: 600; }
.flow-intro small { margin-top: 6px; color: #8a9586; font-size: 10px; }
.configuration-flow ol { display: flex; align-items: center; justify-content: space-between; gap: 13px; margin: 0; padding: 0; list-style: none; }
.configuration-flow li { display: flex; align-items: flex-start; gap: 10px; }
.configuration-flow b { padding-top: 2px; color: #9aa794; font-size: 10px; font-weight: 500; font-variant-numeric: tabular-nums; }
.configuration-flow li > span { display: block; font-size: 12px; font-weight: 600; }
.configuration-flow li small { display: block; margin-top: 5px; color: #82907d; font-size: 10px; font-weight: 400; white-space: nowrap; }
.unsaved-guard { display: flex; align-items: center; justify-content: space-between; gap: 18px; max-width: 1440px; margin: 0 auto 18px; padding: 15px 19px; border: 1px solid #d8c997; border-radius: 7px; color: #756239; background: #faf6e9; }
.unsaved-guard strong { font-size: 12px; }
.unsaved-guard p { margin: 4px 0 0; font-size: 11px; }
.guard-actions, .save-actions { display: flex; align-items: center; gap: 8px; }
.configuration-workspace { display: grid; grid-template-columns: 268px minmax(0, 1fr); min-height: 650px; max-width: 1440px; margin: 0 auto; border: 1px solid var(--config-line); border-radius: 9px; background: #fff; overflow: hidden; }
.configuration-navigation { display: flex; flex-direction: column; min-width: 0; background: #f3f5f0; border-right: 1px solid var(--config-line); }
.configuration-tabs { display: flex; align-items: center; gap: 4px; margin: 0 14px; padding: 16px 0 13px; border-bottom: 1px solid #e1e6dc; }
.configuration-tabs button { display: inline-flex; align-items: center; gap: 5px; padding: 7px 9px; border-radius: 5px; color: #83907c; background: transparent; font-size: 11px; white-space: nowrap; }
.configuration-tabs button span { color: #a2ac9e; font-size: 10px; }
.configuration-tabs button.active { color: #2a5941; background: #e4ebde; font-weight: 600; }
.configuration-search { display: flex; align-items: center; gap: 8px; margin: 15px 18px 12px; padding: 7px 9px; border: 1px solid #e0e5db; border-radius: 5px; background: #fcfdfb; }
.configuration-search > span[aria-hidden] { color: #8b9883; font-size: 20px; line-height: .8; }
.configuration-search input { min-width: 0; width: 100%; border: 0; outline: none; color: #4a604f; background: transparent; font-size: 11px; }
.configuration-search input::placeholder { color: #9aa493; }
.configuration-list { flex: 1; min-height: 0; padding: 0 9px 15px; }
.configuration-list-item { display: block; width: 100%; margin: 4px 0; padding: 13px 13px 12px; border-radius: 6px; color: #64795e; background: transparent; text-align: left; }
.configuration-list-item:hover { background: #eaf0e4; }
.configuration-list-item.selected { color: #315e45; background: #e3ebdc; }
.item-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.item-title strong { font-size: 12px; font-weight: 600; }
.item-title > span[aria-hidden] { color: #a0b294; font-size: 14px; }
.item-description { display: block; margin-top: 7px; color: #66775e; font-size: 11px; line-height: 1.65; }
.configuration-list-item code { display: block; margin-top: 7px; color: #76866c; font-size: 10px; word-break: break-word; }
.custom-mark { padding: 2px 5px; border-radius: 3px; color: #678251; background: #d7e2ca; font-size: 9px; }
.list-empty { padding: 25px 15px; color: #8a9680; font-size: 11px; }
.configuration-nav-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 15px 18px; border-top: 1px solid #e1e6dc; color: #8a9680; font-size: 10px; }
.configuration-nav-footer code { max-width: 130px; overflow: hidden; text-overflow: ellipsis; color: #98a38e; font-size: 9px; }
.configuration-detail { min-width: 0; padding: 29px 32px 0; }
.detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.eyebrow { display: block; margin-bottom: 7px; color: #92a18b; font-size: 9px; letter-spacing: 1.4px; }
h2 { margin: 0; color: #284534; font-size: 21px; font-weight: 600; letter-spacing: -.5px; }
.detail-heading p { max-width: 65ch; margin: 10px 0 0; color: #68785e; font-size: 12px; line-height: 1.8; }
.model-label { display: inline-flex; align-items: center; gap: 7px; margin-top: 20px; padding: 6px 9px; border: 1px solid #e4eadf; border-radius: 5px; color: #668358; font: 10px ui-monospace, 'SFMono-Regular', Consolas, monospace; white-space: nowrap; }
.model-label i { width: 5px; height: 5px; border-radius: 50%; background: #7fa26b; }
.agent-metadata { display: grid; gap: 14px; margin: 22px 0 23px; padding: 16px 18px; background: #f6f8f3; border-radius: 5px; }
.agent-metadata h3 { margin: 0 0 5px; color: #78906a; font-size: 10px; font-weight: 600; }
.agent-metadata p { margin: 0; color: #586f4d; font-size: 12px; line-height: 1.8; }
.context-tags { display: flex; flex-wrap: wrap; gap: 5px 7px; margin: 0; padding: 0; list-style: none; }
.context-tags li { padding: 2px 6px; border: 1px solid #e4eade; border-radius: 3px; color: #7d8f72; background: #fcfdfb; font-size: 10px; line-height: 1.6; }
.context-tags code { font-size: 10px; }
.linked-skills { display: flex; flex-wrap: wrap; gap: 7px; }
.linked-skills button { padding: 4px 7px; border-radius: 4px; color: #4f7856; background: #e8efe2; font-size: 10px; }
.linked-skills button:hover { background: #dce8d3; }
.linked-skills span { margin-left: 4px; color: #90a380; }
.node-details { color: #98a28e; font-size: 10px; }
.node-details summary { cursor: pointer; }
.node-details code { display: block; margin-top: 6px; color: #8d9b83; font-size: 10px; overflow-wrap: anywhere; }
.prompt-modes { display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 16px; padding-bottom: 12px; border-bottom: 1px solid #e7ebdf; }
.prompt-modes button { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 4px; color: #8c9b7f; background: transparent; font-size: 11px; }
.prompt-modes button.active { color: #3e6c46; background: #edf3e6; font-weight: 600; }
.mode-custom-dot { width: 4px; height: 4px; border-radius: 50%; background: #9db17d; }
.document-editor { border-top: 1px solid #e6ecdf; }
.document-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 17px 0 8px; }
.document-toolbar > div { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }
.document-toolbar h3 { margin: 0; color: #48663f; font-size: 12px; font-weight: 600; }
.document-state { color: #94a38a; font-size: 10px; }
.document-state.custom { color: #73905b; }
.document-state.modified { color: #a57d3f; }
.document-explanation { margin: 0 0 13px; color: #738467; font-size: 12px; line-height: 1.7; }
.template-markers { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin: 0 0 13px; padding: 10px 12px; border-left: 2px solid #a5b89a; color: #728366; background: #f6f8f1; font-size: 10px; }
.template-markers strong { font-weight: 500; }
.template-markers code { padding: 2px 5px; border-radius: 3px; color: #56734a; background: #e9efdf; font-size: 10px; overflow-wrap: anywhere; }
.template-markers small { flex-basis: 100%; color: #8a987e; font-size: 10px; line-height: 1.7; }
.editor-surface { padding: 17px 19px; border: 1px solid #e4e9de; border-radius: 5px; background: #fafbf8; }
.editor-surface.editing { border-color: #9eb896; background: #fff; box-shadow: 0 0 0 2px #eff4e9; }
.editor-surface textarea { display: block; width: 100%; min-height: 400px; resize: vertical; border: 0; outline: none; padding: 0; color: #465b3e; background: transparent; font: 12px/1.95 ui-monospace, 'SFMono-Regular', 'PingFang SC', 'Microsoft YaHei', Consolas, monospace; tab-size: 2; overflow-wrap: anywhere; }
.editor-surface textarea:read-only { cursor: text; }
.document-footnote { display: flex; justify-content: space-between; gap: 16px; margin-top: 8px; color: #a2ad98; font-size: 9px; font-variant-numeric: tabular-nums; }
.source-details { margin: 16px 0 0; color: #8c9d7d; font-size: 10px; }
.source-details summary { padding: 5px 0; cursor: pointer; }
.source-details p { margin: 10px 0; color: #8d9b81; line-height: 1.7; }
.source-details p strong, .source-details p code { display: block; }
.source-details p strong { margin-bottom: 3px; color: #708761; font-weight: 500; }
.source-details p code { color: #8f9e82; overflow-wrap: anywhere; }
.source-details pre { max-height: 350px; overflow: auto; padding: 15px; border-radius: 4px; color: #778c65; background: #f5f8f0; white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.9 ui-monospace, 'SFMono-Regular', 'PingFang SC', Consolas, monospace; }
.assembly-preview ul { margin: 10px 0; padding-left: 17px; color: #788e67; font-size: 11px; line-height: 1.9; }
.document-save-bar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 24px -32px 0; padding: 19px 32px; border-top: 1px solid #e5eadf; background: #fbfcf9; }
.document-save-bar > div:first-child { min-width: 0; }
.document-save-bar strong { display: block; color: #839474; font-size: 11px; font-weight: 500; }
.document-save-bar small { display: block; margin-top: 5px; color: #a1ad95; font-size: 10px; line-height: 1.6; }
.read-only-label { padding: 5px 8px; border: 1px solid #e3e9dc; border-radius: 4px; color: #94a487; font-size: 10px; white-space: nowrap; }
.policy-navigation-note { padding: 17px 12px; color: #809471; font-size: 11px; }
.policy-navigation-note strong { font-weight: 600; }
.policy-navigation-note p { margin: 8px 0; color: #95a489; line-height: 1.8; }
.policy-list { margin: 25px 0 30px; }
.policy-list > div { padding: 20px 0; border-top: 1px solid #e8eddf; }
.policy-list dt { color: #516f43; font-size: 13px; font-weight: 600; }
.policy-list dd { margin: 8px 0 0; max-width: 76ch; color: #697d5b; font-size: 12px; line-height: 1.9; white-space: pre-line; }
.detail-empty { padding: 30px 0; color: #91a283; font-size: 12px; line-height: 1.8; }
.configuration-page-note { max-width: 1440px; margin: 16px auto 0; color: #99a68f; font-size: 10px; line-height: 1.8; }
.configuration-loading { position: relative; max-width: 1440px; margin: 35px auto; }
.skeleton-heading, .skeleton-subheading, .skeleton-layout > div { background: #e8ede2; border-radius: 6px; animation: config-skeleton 1.8s ease-in-out infinite alternate; }
.skeleton-heading { width: 180px; height: 22px; margin-bottom: 14px; }
.skeleton-subheading { width: min(400px, 80%); height: 12px; margin-bottom: 24px; }
.skeleton-layout { display: grid; grid-template-columns: 268px 1fr; gap: 25px; height: 500px; }
.configuration-loading > span { position: absolute; top: 43%; left: 50%; transform: translateX(-50%); color: #8c9b7e; font-size: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@keyframes config-skeleton { to { opacity: .45; } }
@media (max-width: 1180px) { .configuration-flow { grid-template-columns: 1fr; gap: 17px; } .flow-intro { display: flex; align-items: baseline; gap: 12px; } .flow-intro small { margin-top: 0; } .configuration-workspace { grid-template-columns: 236px minmax(0, 1fr); } .configuration-detail { padding: 25px 24px 0; } .document-save-bar { margin-left: -24px; margin-right: -24px; padding-left: 24px; padding-right: 24px; } .document-save-bar { flex-wrap: wrap; } }
@media (max-width: 780px) { .agent-configuration { padding: 22px 16px; } .configuration-heading { align-items: flex-start; margin-bottom: 20px; } h1 { font-size: 24px; } .configuration-heading-actions { padding-top: 4px; max-width: 135px; } .configuration-heading-actions small { text-align: right; line-height: 1.7; } .configuration-title > p { max-width: 34ch; font-size: 11px; } .configuration-flow { padding: 15px 0; } .configuration-flow ol { flex-wrap: wrap; gap: 18px 25px; justify-content: flex-start; } .configuration-flow li { min-width: 110px; } .configuration-workspace { grid-template-columns: 1fr; } .configuration-navigation { border-right: 0; border-bottom: 1px solid var(--config-line); } .configuration-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; max-height: 280px; overflow: auto; padding: 0 12px 12px; } .configuration-list-item { margin: 0; } .configuration-tabs { padding: 13px 0; } .configuration-nav-footer { display: none; } .configuration-detail { padding: 22px 18px 0; } .detail-heading { flex-wrap: wrap; gap: 8px; } .model-label { margin-top: 0; } .document-toolbar { align-items: flex-start; flex-wrap: wrap; } .document-save-bar { margin-left: -18px; margin-right: -18px; padding: 17px 18px; } .document-save-bar, .unsaved-guard { align-items: flex-start; flex-direction: column; } .save-actions { flex-wrap: wrap; } .guard-actions { flex-wrap: wrap; } .editor-surface { padding: 12px; } .editor-surface textarea { min-height: 370px; font-size: 11px; } .skeleton-layout { grid-template-columns: 1fr; height: 400px; } .skeleton-layout > div:first-child { display: none; } }
@media (prefers-reduced-motion: reduce) { .agent-configuration button { transition: none; } .skeleton-heading, .skeleton-subheading, .skeleton-layout > div { animation: none; } }
</style>
