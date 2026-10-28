<script setup lang="ts">
import type { QueryResult } from '../types'
defineProps<{ result: QueryResult }>()
const labels: Record<string, string> = { pending: '待执行', completed: '已完成', failed: '失败', blocked: '依赖阻塞', not_applicable: '不适用', basic_valid: '基础检查通过', reviewed: '审核通过', needs_review: '待核验', superseded: '已失效' }
const actions: Record<string, string> = { execute_step: '执行本步', skip_step: '建议跳过相关步骤', request_delivery: '建议进入交付', request_replan: '请求补充计划', request_clarification: '请求补充条件' }
const premises: Record<string, string> = { confirmed: '前提成立', refuted: '前提不成立', unknown: '尚不能判断' }
</script>

<template>
  <section class="harness-panel" aria-label="调查计划与证据">
    <div class="harness-summary">
      <strong>调查计划与证据</strong>
      <span>{{ result.final_reviewed ? '交付审核通过' : result.status === 'waiting_clarification' ? '补充后继续当前任务' : '尚未完成交付审核' }}</span>
    </div>
    <small>任务 {{ result.task_id }}</small>
    <p v-if="result.budget" class="budget-line">工具 {{ result.budget.tools }}/{{ result.budget.tool_limit }} · 模型 {{ Object.values(result.budget.model_calls).reduce((a, b) => a + b, 0) }} 次 · 主动处理 {{ result.budget.active_seconds.toFixed(1) }} 秒 · 重规划 {{ result.budget.replans }} 次</p>
    <p v-if="result.stop_reason" class="stop-reason">停止原因：{{ result.stop_reason }}</p>
    <small>独立过程审核 {{ result.reviews?.filter(item => !item.final).length || 0 }} 次 · 最终交付审核 {{ result.reviews?.filter(item => item.final).length || 0 }} 次</small>
    <ol class="harness-steps">
      <li v-for="step in result.plan" :key="step.id">
        <div><strong>{{ step.id }} · {{ labels[step.status] || step.status }}</strong><span>{{ step.goal }}</span></div>
        <small v-if="step.depends_on.length">依赖 {{ step.depends_on.join('、') }} · 实际输入 {{ step.input_result_ids.join('、') || '尚未取得' }}</small>
        <small v-if="step.result_ids.length">结果 {{ step.result_ids.join('、') }}</small>
        <small v-if="step.error" class="stop-reason">{{ step.error }}</small>
        <div v-for="(decision, index) in result.step_decisions?.filter(item => item.step_id === step.id) || []" :key="index" class="step-decision">
          <small>执行器建议：{{ actions[decision.action] || decision.action }} · {{ premises[decision.premise_status] || decision.premise_status }}</small>
          <small>{{ decision.reason }}</small>
          <small v-if="decision.evidence_ids.length">依据 {{ decision.evidence_ids.join('、') }}</small>
          <small v-if="decision.affected_step_ids.length">影响步骤 {{ decision.affected_step_ids.join('、') }} · 要求 {{ decision.affected_requirement_ids.join('、') }}</small>
        </div>
      </li>
    </ol>
    <details v-if="result.requirements?.length">
      <summary>任务要求与剩余工作</summary>
      <small v-for="item in result.requirements" :key="item.id">{{ item.id }} · {{ item.status === 'answered' ? '已回答' : item.status === 'not_applicable' ? '有依据不适用' : '尚未完成' }}：{{ item.question }}</small>
    </details>
    <details v-if="result.budget?.events?.length" class="evidence-item">
      <summary>过程记录与 Skill 注入</summary>
      <small v-for="event in result.budget.events" :key="event.sequence">
        {{ event.active_seconds.toFixed(1) }}秒 · {{ event.step_id }} ·
        {{ event.stage === 'skill_injection' ? `节点 ${event.node} 注入 ${event.skill}` : event.reason || event.name || event.stage }}
      </small>
      <details><summary>结构化原始记录</summary><pre>{{ JSON.stringify(result.budget.events, null, 2) }}</pre></details>
    </details>
    <details v-for="item in result.evidence" :key="item.id" class="evidence-item">
      <summary>{{ item.id }} · {{ labels[item.check_status] || item.check_status }} · {{ item.total_rows ?? '未知' }} 行</summary>
      <p>{{ item.goal }}</p>
      <small>{{ item.complete ? '服务端结果完整' : '结果不完整' }}{{ item.display_truncated ? '，下方仅展示预览' : '' }}</small>
      <small v-for="(check, index) in item.query_contract?.validation?.pending_checks || []" :key="index" class="stop-reason">待核对：{{ check.message }}</small>
      <details v-if="item.query_contract?.validation?.normalizations?.length">
        <summary>程序补齐与规范化记录</summary><pre>{{ JSON.stringify(item.query_contract.validation.normalizations, null, 2) }}</pre>
      </details>
      <pre v-if="item.sql"><code>{{ item.sql }}</code></pre>
      <pre>{{ JSON.stringify(item.rows.slice(0, 20), null, 2) }}</pre>
    </details>
  </section>
</template>

<style scoped>
.harness-panel { margin: 20px 0; padding: 18px 20px; border: 1px solid #dedfd8; border-radius: 12px; background: #fafbf7; }
.harness-summary { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.harness-summary span, .budget-line, small { color: #697465; font-size: 12px; }
.budget-line { margin: 10px 0; }
.harness-steps { list-style: none; padding: 0; margin: 16px 0; }
.harness-steps li { padding: 12px 0; border-top: 1px solid #e3e6dd; }
.harness-steps strong, .harness-steps span, small { display: block; margin-bottom: 5px; }
.harness-steps strong { font-size: 12px; color: #47744b; }
.harness-steps span, .evidence-item p { font-size: 13px; line-height: 1.6; }
.evidence-item { margin-top: 12px; font-size: 12px; }
.evidence-item pre { max-height: 280px; overflow: auto; padding: 12px; background: #f1f3ed; white-space: pre-wrap; }
.stop-reason { color: #9a5923; font-size: 12px; line-height: 1.6; }
.step-decision { border-left: 2px solid #d5ddd0; padding-left: 10px; margin-top: 7px; }
</style>
