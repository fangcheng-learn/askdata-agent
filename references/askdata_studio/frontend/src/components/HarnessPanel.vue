<script setup lang="ts">
import { computed } from 'vue'
import type { QueryResult } from '../types'
const props = defineProps<{ result: QueryResult; busy?: boolean }>()
defineEmits<{ continue: [] }>()
const isReact = computed(() => props.result.workflow_mode === 'investigation_react_v1')
const tokenObservationOnly = computed(() => props.result.budget?.model_budget?.mode === 'observe_only' || props.result.budget?.model_budget?.enforced === false)
const canContinue = computed(() => props.result.status !== 'completed' && (props.result.resumable || props.result.clarification?.parameter === 'resume'))
const labels: Record<string, string> = { pending: '待执行', running: '执行中', generating: 'SQL生成中', waiting_clarification: '等待补充', awaiting_calculation: '数据已查到，计算待完成', rewriting: 'SQL重写中', not_executed: '未执行', interrupted: '中断／结果未知', completed: '已完成', failed: '失败', blocked: '依赖阻塞', not_applicable: '不适用', basic_valid: '基础检查通过', reviewed: '审核通过', needs_review: '待核验', superseded: '已失效' }
const nodes: Record<string, string> = { react: 'ReAct决策', sql_repair: 'SQL重写', query_coder: 'SQL生成', preprocess: '入口理解', schema_retrieval: '语义检索', draft: '报告草稿', delivery_review: '最终审核' }
const nodeLabel = (node?: string) => nodes[node ?? ''] || node || '未知节点'
const actions: Record<string, string> = { query: '查询', load_skill: '读取 Skill', retrieve: '补充语义', read_result: '读取结果', calculate: '确定性计算', clarify: '请求补充', deliver: '请求交付', execute_step: '执行本步', skip_step: '建议跳过相关步骤', request_delivery: '建议进入交付', request_replan: '请求补充计划', request_clarification: '请求补充条件' }
const premises: Record<string, string> = { confirmed: '前提成立', refuted: '前提不成立', unknown: '尚不能判断' }
const usage = computed(() => props.result.budget?.model_usage ?? [])
const chatTotals = computed(() => usage.value.filter(item => item.kind === 'chat').reduce((total, item) => {
  total.input += item.usage?.prompt_tokens ?? item.usage?.input_tokens ?? 0
  total.output += item.usage?.completion_tokens ?? item.usage?.output_tokens ?? 0
  total.cached += item.usage?.prompt_tokens_details?.cached_tokens ?? 0
  total.unknown += item.usage ? 0 : 1
  return total
}, { input: 0, output: 0, cached: 0, unknown: 0 }))
const count = (value?: number | null) => (value ?? 0).toLocaleString('zh-CN')
const seconds = (value?: number) => (value ?? 0).toFixed(1)
function eventText(event: NonNullable<NonNullable<QueryResult['budget']>['events']>[number]) {
  if (event.stage === 'react_round') return `ReAct决策 ${event.index}/${event.limit} 轮`
  if (event.stage === 'query_round') return `业务取证 ${event.index}/${event.limit} 轮`
  if (event.stage === 'react_decision') return `ReAct动作决策第 ${event.index} 次（含内部准备）`
  if (event.stage === 'direct_sql_generation') return `SQL已生成 ${event.used}/${event.limit} 次`
  if (event.stage === 'skill_loaded') return `读取 ${event.skill ?? ''}（${event.characters ?? 0} 字符）`
  if (event.stage === 'skill_injection') return `${nodeLabel(event.node)} 注入 ${event.skill ?? ''}（${event.characters ?? 0} 字符）`
  if (event.stage === 'context_block') {
    const estimate = String(event.estimated_tokens ?? '未知')
    const basis = String(event.estimator ?? '').includes('utf8') ? `UTF-8 保守上界 ${estimate}（非实际 token）` : `估算 ${estimate} token`
    return `${nodeLabel(event.node)} · ${String(event.block ?? '')}：${event.characters ?? 0} 字符，${basis}`
  }
  return event.reason || event.name || event.stage
}
</script>

<template>
  <section class="harness-panel" :aria-label="isReact ? '调查行动与证据' : '调查计划与证据'">
    <div class="harness-summary">
      <strong>{{ isReact ? '调查行动与证据' : '调查计划与证据' }}</strong>
      <span>{{ result.final_reviewed ? '交付审核通过' : result.status === 'waiting_clarification' ? '补充后继续当前任务' : '尚未完成交付审核' }}</span>
    </div>
    <small>任务 {{ result.task_id }}</small>
    <small v-if="(result.execution_protocol_version ?? 0) >= 2">经营 ReAct 决定取证方向 · 独立 SQL 模块取数 · DataQa 整理报告</small>
    <p v-if="result.budget?.model_calls" class="budget-line">工具 {{ result.budget.tools }}/{{ result.budget.tool_limit }} · Chat {{ result.budget.model_calls.chat || 0 }} 次 · Embedding {{ result.budget.model_calls.embedding || 0 }} 次 · Rerank {{ result.budget.model_calls.rerank || 0 }} 次 · 主动处理 {{ seconds(result.budget.active_seconds) }} 秒<span v-if="!isReact"> · 重规划 {{ result.budget.replans }} 次</span></p>
    <p v-if="isReact && result.budget?.react_rounds !== undefined" class="budget-line">{{ result.budget.react_round_policy === 'query_batches' ? '业务取证' : '历史决策计数' }} {{ result.budget.react_rounds }}/{{ result.budget.react_round_limit }} 轮<span v-if="result.budget.react_decisions !== undefined"> · ReAct动作决策 {{ result.budget.react_decisions }} 次</span> · 每条 SQL 最多生成 {{ result.budget.sql_generation_limit }} 次（首次＋最多 {{ Math.max(0, (result.budget.sql_generation_limit ?? 2) - 1) }} 次修复）<br>Skill、字段检索、日期解析、读取和计算已有结果为准备动作，不占业务取证轮次，仍记录模型／工具用量；每个查询批次计一轮。</p>
    <p v-if="usage.length" class="budget-line">Chat 实际输入 {{ count(chatTotals.input) }} · 输出 {{ count(chatTotals.output) }} token · 缓存命中 {{ count(chatTotals.cached) }}（包含在输入内）<span v-if="chatTotals.unknown"> · {{ chatTotals.unknown }} 次用量未返回</span></p>
    <template v-if="result.budget?.model_budget">
      <p v-if="tokenObservationOnly" class="budget-line">Token 仅估算和统计，不设用量硬限制 · Chat 已返回实际用量 {{ count(result.budget.model_budget.used.chat) }} · 检索已返回实际用量 {{ count(result.budget.model_budget.used.retrieval) }}<span v-if="result.budget.model_budget.estimated_unknown?.chat || result.budget.model_budget.estimated_unknown?.retrieval"> · 未返回用量的估计值：Chat {{ count(result.budget.model_budget.estimated_unknown?.chat) }}／检索 {{ count(result.budget.model_budget.estimated_unknown?.retrieval) }}</span></p>
      <p v-else class="budget-line">历史 Token 预算：Chat 已计 {{ count(result.budget.model_budget.used.chat) }}/{{ count(result.budget.model_budget.limit) }} · 收尾预留 {{ count(result.budget.model_budget.closing_reserve) }} · 待结算 {{ count(result.budget.model_budget.reserved) }}</p>
    </template>
    <p v-if="result.stop_reason" class="stop-reason">停止原因：{{ result.stop_reason }}</p>
    <small>独立过程审核 {{ result.reviews?.filter(item => !item.final).length || 0 }} 次 · 最终交付审核 {{ result.reviews?.filter(item => item.final).length || 0 }} 次</small>
    <small v-if="result.selected_skills?.length">已读取业务方法：{{ result.selected_skills.join('、') }}</small>
    <button v-if="canContinue" class="continue-task" :disabled="busy" @click="$emit('continue')">从检查点继续</button>
    <ol class="harness-steps">
      <li v-for="step in result.plan" :key="step.id">
        <div><strong>{{ step.id }} · {{ actions[step.action_type || step.kind] || step.kind }} · {{ labels[step.status] || step.status }}{{ step.reused ? ' · 复用已有结果' : '' }}</strong><span>{{ step.goal }}</span></div>
        <small v-if="step.input_result_ids?.length || step.depends_on?.length">实际输入 {{ step.input_result_ids?.join('、') || '无' }}<span v-if="step.depends_on?.length"> · 依赖 {{ step.depends_on.join('、') }}</span></small>
        <small v-if="step.result_ids?.length">结果 {{ step.result_ids.join('、') }}</small>
        <small v-if="step.sql_generations !== undefined">SQL 已生成 {{ step.sql_generations }}/{{ step.sql_generation_limit ?? result.budget?.sql_generation_limit ?? 2 }} 次</small>
        <small v-if="step.generation_closed">本查询已关闭；其他可行调查共享剩余预算继续</small>
        <small v-else-if="step.attempts && step.attempts > 1">{{ step.action_type === 'query' ? '历史尝试' : '动作尝试' }} {{ step.attempts }} 次<span v-if="step.action_type === 'query'">（历史记录未分离生成与执行次数）</span></small>
        <small v-if="step.error" class="stop-reason">{{ step.error }}</small>
        <div v-for="(decision, index) in result.step_decisions?.filter(item => item.step_id === step.id) || []" :key="index" class="step-decision">
          <small>执行器建议：{{ actions[decision.action] || decision.action }} · {{ premises[decision.premise_status] || decision.premise_status }}</small>
          <small>{{ decision.reason }}</small>
          <small v-if="decision.evidence_ids.length">依据 {{ decision.evidence_ids.join('、') }}</small>
          <small v-if="decision.affected_step_ids.length">影响步骤 {{ decision.affected_step_ids.join('、') }} · 要求 {{ decision.affected_requirement_ids.join('、') }}</small>
        </div>
      </li>
    </ol>
    <details v-if="result.requirements?.length || result.required_questions?.length">
      <summary>任务目标与回答状态</summary>
      <template v-if="result.requirements?.length"><small v-for="item in result.requirements" :key="item.id">{{ item.id }} · {{ item.status === 'answered' ? '已回答' : item.status === 'not_applicable' ? '有依据不适用' : '尚未完成' }}：{{ item.question }}</small></template>
      <template v-else><small v-for="question in result.required_questions" :key="question">{{ question }}</small></template>
    </details>
    <details v-if="usage.length" class="evidence-item">
      <summary>逐次模型用量</summary>
      <small v-for="(item, index) in usage" :key="index">{{ index + 1 }} · {{ nodeLabel(item.node || item.kind) }} / {{ item.step_id }} · {{ item.model }} · {{ item.status }} · 输入 {{ item.usage?.prompt_tokens ?? item.usage?.input_tokens ?? '未知' }} / 输出 {{ item.usage?.completion_tokens ?? item.usage?.output_tokens ?? '未知' }} / 总量 {{ item.usage?.total_tokens ?? '未知' }}</small>
    </details>
    <details v-if="result.budget?.events?.length" class="evidence-item">
      <summary>过程记录、Skill 读取与注入</summary>
      <small v-for="event in result.budget.events" :key="event.sequence">{{ seconds(event.active_seconds) }}秒 · {{ event.step_id }} · {{ eventText(event) }}</small>
      <details><summary>结构化原始记录</summary><pre>{{ JSON.stringify(result.budget.events, null, 2) }}</pre></details>
    </details>
    <details v-for="item in result.evidence" :key="item.id" class="evidence-item">
      <summary>{{ item.id }} · {{ labels[item.check_status] || item.check_status }} · {{ item.total_rows ?? '未知' }} 行</summary>
      <p>{{ item.goal }}</p>
      <small>{{ item.complete ? '服务端结果完整' : '结果不完整' }}{{ item.display_truncated ? '，下方仅展示预览' : '' }}</small>
      <small v-for="(check, index) in item.query_contract?.validation?.pending_checks || []" :key="index" class="stop-reason">待核对：{{ check.message }}</small>
      <details v-if="item.query_contract?.validation?.normalizations?.length"><summary>程序补齐与规范化记录</summary><pre>{{ JSON.stringify(item.query_contract.validation.normalizations, null, 2) }}</pre></details>
      <pre v-if="item.sql"><code>{{ item.sql }}</code></pre>
      <pre>{{ JSON.stringify(item.rows.slice(0, 20), null, 2) }}</pre>
    </details>
  </section>
</template>

<style scoped>
.harness-panel { margin: 20px 0; padding: 18px 20px; border: 1px solid #dedfd8; border-radius: 12px; background: #fafbf7; }
.harness-summary { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.harness-summary span, .budget-line, small { color: #697465; font-size: 12px; }
.budget-line { margin: 10px 0; line-height: 1.7; }
.harness-steps { list-style: none; padding: 0; margin: 16px 0; }
.harness-steps li { padding: 12px 0; border-top: 1px solid #e3e6dd; }
.harness-steps strong, .harness-steps span, small { display: block; margin-bottom: 5px; }
.harness-steps strong { font-size: 12px; color: #47744b; }
.harness-steps span, .evidence-item p { font-size: 13px; line-height: 1.6; }
.evidence-item { margin-top: 12px; font-size: 12px; }
.evidence-item pre { max-height: 280px; overflow: auto; padding: 12px; background: #f1f3ed; white-space: pre-wrap; }
.stop-reason { color: #9a5923; font-size: 12px; line-height: 1.6; }
.step-decision { border-left: 2px solid #d5ddd0; padding-left: 10px; margin-top: 7px; }
.continue-task { margin-top: 12px; padding: 8px 12px; border: 1px solid #bccab9; border-radius: 7px; color: #355c3b; background: #f4f7ee; cursor: pointer; }
.continue-task:disabled { opacity: .5; cursor: wait; }
</style>
