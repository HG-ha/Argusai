<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  options: { type: Array, default: () => [] },
  placeholder: { type: String, default: '' },
  minWidth: { type: String, default: '112px' },
  /** Prefer opening upward (for bottom bars like pager). */
  preferUp: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const root = ref(null)
const menuEl = ref(null)
const menuStyle = ref({})

const current = computed(() =>
  props.options.find((o) => o.value === props.modelValue) || null,
)
const label = computed(() => current.value?.label ?? props.placeholder ?? '—')

function pick(value) {
  emit('update:modelValue', value)
  open.value = false
}

function placeMenu() {
  const trigger = root.value?.querySelector?.('.list-dd-trigger')
  if (!trigger) return
  const r = trigger.getBoundingClientRect()
  const menuH = Math.min(240, (props.options?.length || 1) * 34 + 12)
  const spaceBelow = window.innerHeight - r.bottom - 8
  const spaceAbove = r.top - 8
  const up = props.preferUp
    ? spaceAbove >= Math.min(menuH, 120) || spaceAbove > spaceBelow
    : spaceBelow < menuH && spaceAbove > spaceBelow
  const width = Math.max(r.width, 120)
  const left = Math.min(Math.max(8, r.left), window.innerWidth - width - 8)
  menuStyle.value = {
    position: 'fixed',
    left: `${left}px`,
    width: `${width}px`,
    zIndex: 10050,
    maxHeight: '240px',
    overflowY: 'auto',
    ...(up
      ? { bottom: `${window.innerHeight - r.top + 6}px`, top: 'auto' }
      : { top: `${r.bottom + 6}px`, bottom: 'auto' }),
  }
}

async function toggle() {
  open.value = !open.value
  if (open.value) {
    await nextTick()
    placeMenu()
  }
}

function onDoc(e) {
  if (!open.value) return
  const t = e.target
  if (root.value?.contains(t) || menuEl.value?.contains(t)) return
  open.value = false
}

function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

function onReposition() {
  if (open.value) placeMenu()
}

watch(open, (v) => {
  if (v) nextTick(placeMenu)
})

onMounted(() => {
  document.addEventListener('pointerdown', onDoc, true)
  document.addEventListener('keydown', onKey, true)
  window.addEventListener('resize', onReposition)
  window.addEventListener('scroll', onReposition, true)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', onDoc, true)
  document.removeEventListener('keydown', onKey, true)
  window.removeEventListener('resize', onReposition)
  window.removeEventListener('scroll', onReposition, true)
})
</script>

<template>
  <div ref="root" class="list-dd" :class="{ open }" :style="{ minWidth }">
    <button type="button" class="list-dd-trigger" @click="toggle">
      <span class="list-dd-label">{{ label }}</span>
      <span class="list-dd-caret" aria-hidden="true">▾</span>
    </button>
    <Teleport to="body">
      <div
        v-if="open"
        ref="menuEl"
        class="list-dd-menu list-dd-menu--portal"
        role="listbox"
        :style="menuStyle"
      >
        <button
          v-for="opt in options"
          :key="String(opt.value)"
          type="button"
          class="list-dd-opt"
          :class="{ on: opt.value === modelValue }"
          role="option"
          @click="pick(opt.value)"
        >{{ opt.label }}</button>
      </div>
    </Teleport>
  </div>
</template>
