<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 18 })
const paths: Record<string, string> = {
  plus: 'M12 5v14M5 12h14', chat: 'M4 4h16v12H9l-5 4V4Z',
  data: 'M4 5c0-4 16-4 16 0s-16 4-16 0Zm0 0v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0',
  skill: 'm12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3Z',
  model: 'M8 3h8l5 9-5 9H8l-5-9 5-9ZM8 3l8 18M16 3 8 21M3 12h18',
  tool: 'm8 5-6 7 6 7m8-14 6 7-6 7m-3-16-2 18',
  book: 'M12 5C9 3 5 3 2 4v15c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1Zm0 0v15',
  file: 'M5 3h9l5 5v13H5V3Zm9 0v5h5M8 12h8M8 16h6',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6',
  settings: 'M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-6 0v6',
  arrow: 'M12 20V4m-6 6 6-6 6 6', close: 'm6 6 12 12M6 18 18 6',
  expand: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
  branch: 'M6 3v12a4 4 0 0 0 8 0V9m0 0-3 3m3-3 3 3M6 3h.01',
  chevron: 'm9 5 7 7-7 7', check: 'm5 12 4 4L19 6',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2',
  stop: 'M6 6h12v12H6Z', download: 'M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4',
  panel: 'M3 4h18v16H3V4Zm12 0v16', chart: 'M4 3v18h17M8 16v-5m5 5V7m5 9V4',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.file" /></svg></template>
