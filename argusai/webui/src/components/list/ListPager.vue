<script setup>
import { computed } from 'vue'
import { t } from '../../i18n.js'
import ListSelect from './ListSelect.vue'

const props = defineProps({
  page: { type: Number, default: 1 },
  pageCount: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  total: { type: Number, default: 0 },
  pageSizes: { type: Array, default: () => [10, 20, 50, 100] },
})
const emit = defineEmits(['update:page', 'update:pageSize'])

const canPrev = computed(() => props.page > 1)
const canNext = computed(() => props.page < props.pageCount)

const sizeOptions = computed(() =>
  props.pageSizes.map((n) => ({
    label: t('common.pageSizeOption', { n }),
    value: n,
  })),
)

/** Compact page list: 1 … 4 5 6 … 20 */
const pageButtons = computed(() => {
  const total = Math.max(1, props.pageCount)
  const cur = Math.min(Math.max(1, props.page), total)
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const set = new Set([1, total, cur, cur - 1, cur + 1, cur - 2, cur + 2])
  const nums = [...set].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b)
  const out = []
  let prev = 0
  for (const n of nums) {
    if (prev && n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
})

function go(delta) {
  const next = Math.min(props.pageCount, Math.max(1, props.page + delta))
  if (next !== props.page) emit('update:page', next)
}

function goTo(n) {
  if (typeof n !== 'number') return
  if (n !== props.page) emit('update:page', n)
}

function onSize(n) {
  const size = Number(n) || 20
  if (size !== props.pageSize) emit('update:pageSize', size)
}
</script>

<template>
  <div v-if="total > 0" class="list-pager">
    <span class="list-pager-meta">
      {{ t('common.pagerMeta', { total, page, pageCount }) }}
    </span>
    <ListSelect
      class="list-pager-size"
      :model-value="pageSize"
      :options="sizeOptions"
      min-width="104px"
      prefer-up
      @update:model-value="onSize"
    />
    <div class="list-pager-pages">
      <button
        type="button"
        class="ghost list-pager-btn"
        :disabled="!canPrev"
        @click="go(-1)"
      >{{ t('common.prevPage') }}</button>
      <button
        v-for="(p, idx) in pageButtons"
        :key="`${p}-${idx}`"
        type="button"
        class="ghost list-pager-btn list-pager-num"
        :class="{ on: p === page, ellipsis: p === '…' }"
        :disabled="p === '…'"
        @click="goTo(p)"
      >{{ p }}</button>
      <button
        type="button"
        class="ghost list-pager-btn"
        :disabled="!canNext"
        @click="go(1)"
      >{{ t('common.nextPage') }}</button>
    </div>
  </div>
</template>
