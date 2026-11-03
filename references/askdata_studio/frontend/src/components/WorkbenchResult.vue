<script setup lang="ts">
import { computed, ref } from 'vue'
import type { QueryResult, QueryTimeRange, ReportDataSource } from '../types'
import ReportCard from './ReportCard.vue'
import ResultTableCard from './ResultTableCard.vue'
import HarnessPanel from './HarnessPanel.vue'
import Icon from './WorkbenchIcon.vue'
const props = defineProps<{ result: QueryResult | null; sources: ReportDataSource[]; tab: string; busy: boolean; sourceLabel: string; coverage: string }>()
defineEmits<{ save: [result: QueryResult]; reference: [result: QueryResult, quote?: string]; continue: [result: QueryResult]; fork: [result: QueryResult] }>()
const resultRoot = ref<HTMLElement | null>(null)
const quote = ref('')
function selectQuote() {
  const selection = window.getSelection()
  quote.value = selection && resultRoot.value?.contains(selection.anchorNode) ? selection.toString().trim().slice(0, 200) : ''
}
const status = computed(() => ({completed:'已完成',partial:'部分完成',failed:'分析失败',waiting_clarification:'等待补充'}[props.result?.status || 'completed']))
const warnings = computed(() => [...new Set([
  ...(props.result?.report?.warnings || []), ...(props.result?.interpretation?.assumptions || []),
  ...(props.result?.stop_reason ? [props.result.stop_reason] : []),
  ...(props.result?.evidence || []).flatMap(e => (e.query_contract?.validation?.pending_checks || []).map(c => c.message)),
])])
const comparison = computed(() => {
  if (props.result?.interpretation?.time_range) return props.result.interpretation.time_range
  const ranges = (props.result?.evidence || []).filter(e => e.check_status !== 'superseded')
    .flatMap(e => [...(e.query_contract?.time_range ? [e.query_contract.time_range] : []), ...(e.query_contract?.time_ranges || [])])
  const date = (value: string) => value.replace(/T00:00:00(?:[+-]\d{2}:\d{2}|Z)?$/, '')
  const label = (range: QueryTimeRange) => range.start && range.end
    ? `${date(range.start)} 至 ${date(range.end)}${range.end_inclusive === false ? '（不含结束日）' : ''}` : ''
  return [...new Set(ranges.map(label).filter(Boolean))].join('；') || '以报告及证据中的时间范围为准'
})
const metricDefinition = computed(() => props.result?.interpretation?.metric ||
  (props.result?.selected_semantics || []).map(metric => metric.label || metric.name || metric.id).filter(Boolean).join('、') || '查看报告与查询依据')
const snapshotLabel = computed(() => {
  const snapshot = props.result?.scenario
  const date = snapshot?.analysis_date || snapshot?.as_of_local_date
  return date ? `本次分析快照 ${date}${snapshot?.timezone ? ' · ' + snapshot.timezone : ''}` : props.coverage || '数据源未提供更新时间'
})
</script>
<template>
  <div v-if="!result" class="result-empty"><div class="empty-symbol"><Icon name="chart" :size="28" /></div><h3>让每个结论都有据可查</h3><p>分析完成后，在这里查看报告、数据与依据。你可以保留结果，继续追问。</p><div class="empty-result-line"><Icon name="file" />分析报告<span>等待分析</span></div><div class="empty-result-line"><Icon name="data" />关键数据<span>等待分析</span></div><div class="empty-result-line"><Icon name="book" />口径与依据<span>等待分析</span></div></div>
  <div v-else ref="resultRoot" class="result-content" @mouseup="selectQuote">
    <template v-if="tab === 'results'">
      <div class="result-status"><span :class="result.status">{{ status }}</span><small>{{ result.result_title || result.report?.title || '经营分析' }}</small></div>
      <dl class="business-facts"><div><dt>数据来源</dt><dd>{{ sourceLabel }}</dd></div><div><dt>比较区间</dt><dd>{{ comparison }}</dd></div><div><dt>指标口径</dt><dd>{{ metricDefinition }}</dd></div><div><dt>数据快照</dt><dd>{{ snapshotLabel }}</dd></div></dl>
      <div v-if="warnings.length" class="business-warning"><strong>分析局限与待核对项</strong><p v-for="warning in warnings" :key="warning">{{ warning }}</p></div>
      <div class="result-toolbar"><button @click="$emit('reference', result)"><Icon name="chat" :size="14" />引用追问</button><button @click="$emit('fork',result)"><Icon name="branch" :size="14" />从此问题分支</button><button v-if="result.rows.length && result.status === 'completed'" :disabled="result.saved" @click="$emit('save',result)"><Icon name="file" :size="14" />{{ result.saved ? '已保存' : '保存结果' }}</button></div>
      <button v-if="quote" class="quote-action" @click="$emit('reference',result,quote); quote=''">引用所选文字继续分析</button>
      <ReportCard v-if="result.report" :report="result.report" :sources="sources" />
      <p v-else-if="result.analysis" class="analysis-text">{{ result.analysis }}</p>
      <ResultTableCard v-if="!result.report && result.rows.length" :title="result.result_title || '查询结果'" :columns="result.columns" :rows="result.rows" />
      <p v-if="result.preview_truncated" class="muted-copy">当前为预览，共 {{ result.total_rows ?? '未知' }} 行；完整数据范围见依据。</p>
      <div v-if="result.evidence?.length" class="evidence-links"><h4>关键证据</h4><details v-for="evidence in result.evidence.filter(e => e.check_status !== 'superseded')" :key="evidence.id"><summary>{{ evidence.goal || evidence.id }}<small>{{ evidence.total_rows ?? evidence.rows.length }} 行</small></summary><p>{{ evidence.complete ? '服务端结果完整' : '结果尚不完整' }}{{ evidence.display_truncated ? '，当前仅展示预览' : '' }}</p><div class="evidence-table"><table><thead><tr><th v-for="column in evidence.columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row,index) in evidence.rows.slice(0,20)" :key="index"><td v-for="column in evidence.columns" :key="column">{{ row[column] }}</td></tr></tbody></table></div></details></div>
    </template>
    <template v-else>
      <h3 class="evidence-title">分析依据与运行详情</h3><p class="muted-copy">保留真实执行记录，便于核查口径和结论。</p>
      <HarnessPanel v-if="result.plan?.length || result.budget" :result="result" :busy="busy" @continue="$emit('continue',result)" />
      <details v-if="result.selected_semantics?.length" class="technical-detail"><summary>指标定义与统计口径（{{ result.selected_semantics.length }}）</summary><div v-for="metric in result.selected_semantics" :key="metric.id || metric.name || metric.label"><h4>{{ metric.label || metric.name || metric.id }}</h4><p>{{ metric.definition || metric.formula || '未提供详细定义，请核对查询依据' }}</p></div></details>
      <details v-if="result.sql" class="technical-detail"><summary>SQL 查询</summary><pre>{{ result.sql }}</pre></details>
      <details v-if="result.execution_log?.length" class="technical-detail"><summary>完整执行记录</summary><pre>{{ JSON.stringify(result.execution_log,null,2) }}</pre></details>
      <details v-if="result.retrieval" class="technical-detail"><summary>口径检索记录</summary><pre>{{ JSON.stringify(result.retrieval,null,2) }}</pre></details>
      <p v-if="!result.plan?.length && !result.execution_log?.length && !result.sql" class="muted-copy">本轮没有数据库执行记录。</p>
    </template>
  </div>
</template>
