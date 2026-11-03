<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import Icon from './WorkbenchIcon.vue'

interface Choice { id: string; label: string; description?: string; required?: boolean }
const props = defineProps<{
  id: string
  label: string
  icon: string
  options: Choice[]
  selected: string[] | null
  count?: number
  multiple?: boolean
  locked?: boolean
  description: string
}>()
const emit = defineEmits<{ select: [id: string]; manage: [] }>()
const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const checked = (option: Choice) => option.required || props.selected === null || props.selected.includes(option.id)
function close(focus = false) {
  open.value = false
  if (focus) trigger.value?.focus()
}
function select(option: Choice) {
  if (props.locked || option.required) return
  emit('select', option.id)
  if (!props.multiple) close(true)
}
function outside(event: Event) {
  if (event.target instanceof Node && !root.value?.contains(event.target)) close()
}
onMounted(() => {
  document.addEventListener('pointerdown', outside)
  document.addEventListener('focusin', outside)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside)
  document.removeEventListener('focusin', outside)
})
</script>

<template>
  <div ref="root" class="resource-picker" @keydown.esc.stop.prevent="close(true)">
    <button ref="trigger" type="button" class="resource-picker-trigger" :class="{active:open}"
      :aria-expanded="open" :aria-controls="id" aria-haspopup="dialog" @click="open=!open">
      <Icon :name="icon" :size="14" />{{ label }}<small v-if="count !== undefined">{{ count }}</small>
      <Icon name="chevron" :size="11" class="picker-caret" />
    </button>
    <section v-if="open" :id="id" class="resource-picker-panel" role="dialog" :aria-label="label+'选择'">
      <header><strong>{{ label }}</strong><span v-if="locked">已锁定</span><button type="button" :aria-label="'关闭'+label+'选择'" @click="close(true)"><Icon name="close" :size="14" /></button></header>
      <p class="picker-description">{{ description }}</p>
      <div class="picker-options">
        <label v-for="option in options" :key="option.id" :class="{chosen:checked(option)}">
          <span><strong>{{ option.label }}</strong><small :title="option.description">{{ option.required ? '基础能力 · 始终启用' : option.description }}</small></span>
          <input :type="multiple ? 'checkbox' : 'radio'" :name="id" :checked="checked(option)" :disabled="locked || option.required" :aria-label="option.label" @change="select(option)">
        </label>
        <p v-if="!options.length" class="picker-empty">暂无可用选项</p>
      </div>
      <footer><button type="button" @click="close(); emit('manage')">管理{{ label }}<Icon name="chevron" :size="13" /></button></footer>
    </section>
  </div>
</template>
