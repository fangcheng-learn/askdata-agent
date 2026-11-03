<script setup lang="ts">
import { computed } from "vue"
import type { ReportDataSource, VisualizationSpec } from "../types"

const props = defineProps<{
  spec: VisualizationSpec
  sources: ReportDataSource[]
}>()

const colors = ["#277456", "#62a07f", "#98bca8", "#d2aa60", "#6983a8", "#a77b8c", "#7e9b5c", "#c27c5a"]
const source = computed(() => props.spec.source_result_id
  ? props.sources.find((item) => item.resultId === props.spec.source_result_id && item.version === props.spec.source_version)
  : props.sources.find((item) => item.taskId === props.spec.source_task_id))
const allPoints = computed(() => (source.value?.rows ?? [])
  .map((row) => ({
    label: String(row[props.spec.category_field] ?? ""),
    value: row[props.spec.value_field] == null ? NaN : Number(row[props.spec.value_field]),
  }))
  .filter((item) => item.label && Number.isFinite(item.value)))
const points = computed(() => {
  const values = [...allPoints.value]
  if (props.spec.order === "category_asc") values.sort((left, right) => left.label.localeCompare(right.label, "zh-CN", { numeric: true }))
  else if (props.spec.order !== "source") values.sort((left, right) => right.value - left.value)
  return values.slice(0, props.spec.max_items)
})
const invalidPie = computed(() => props.spec.type === "pie" &&
  (allPoints.value.some((item) => item.value < 0) || allPoints.value.length > props.spec.max_items))
const maximum = computed(() => Math.max(...points.value.map((item) => Math.abs(item.value)), 1))
const total = computed(() => points.value.reduce((sum, item) => sum + Math.max(item.value, 0), 0))
const pieBackground = computed(() => {
  if (!total.value) return "#e8ece9"
  let start = 0
  const stops = points.value.map((item, index) => {
    const end = start + Math.max(item.value, 0) / total.value * 100
    const stop = `${colors[index % colors.length]} ${start}% ${end}%`
    start = end
    return stop
  })
  return `conic-gradient(${stops.join(", ")})`
})

function formatNumber(value: number) {
  const precision = props.spec.precision ?? 2
  const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: precision }).format(value)
  return `${number}${props.spec.currency ? ` ${props.spec.currency}` : props.spec.unit ? ` ${props.spec.unit}` : ""}`
}
</script>

<template>
  <section class="analysis-chart">
    <header><div><small>{{ spec.type === "bar" ? "柱状图" : "饼图" }}</small><strong>{{ spec.title }}</strong></div><span>{{ spec.category_field }} × {{ spec.value_field }}</span></header>
    <div v-if="!source || !points.length" class="chart-empty">找不到该图表引用的数据或数值字段。</div>
    <div v-else-if="invalidPie" class="chart-empty">该结果含负值或被截取的分类，不适合以饼图表达总体占比。</div>
    <div v-else-if="spec.type === 'bar'" class="bar-chart">
      <div v-for="point in points" :key="point.label" class="bar-row">
        <span :title="point.label">{{ point.label }}</span>
        <div><i :style="{ width: `${Math.abs(point.value) / maximum * 100}%`, background: point.value < 0 ? '#b8664e' : undefined }"></i></div>
        <strong>{{ formatNumber(point.value) }}</strong>
      </div>
    </div>
    <div v-else class="pie-chart">
      <div class="pie-disc" :style="{ background: pieBackground }"><span>{{ points.length }}<small>项</small></span></div>
      <div class="pie-legend">
        <div v-for="(point, index) in points" :key="point.label">
          <i :style="{ background: colors[index % colors.length] }"></i>
          <span>{{ point.label }}</span>
          <strong>{{ total ? (Math.max(point.value, 0) / total * 100).toFixed(1) : "0.0" }}%</strong>
        </div>
      </div>
    </div>
  </section>
</template>
