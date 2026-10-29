<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

defineProps({
  label: { type: String, required: true },
  /** ``{ key, label, disabled? }`` or ``{ divider: true }``. */
  items: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])

const open = ref(false)
const trigger = ref(null)
const menuEl = ref(null)
const menuStyle = ref({})

function place() {
  const r = trigger.value?.getBoundingClientRect()
  if (!r) return
  const width = Math.max(r.width, 188)
  menuStyle.value = {
    position: 'fixed',
    top: `${r.bottom + 6}px`,
    left: `${Math.min(Math.max(8, r.right - width), window.innerWidth - width - 8)}px`,
    width: `${width}px`,
    zIndex: 10050,
  }
}

async function toggle() {
  open.value = !open.value
  if (open.value) {
    await nextTick()
    place()
    menuEl.value?.querySelector('button:not(:disabled)')?.focus()
  }
}

function pick(item) {
  if (item.disabled) return
  open.value = false
  emit('select', item.key)
}

function onDoc(e) {
  if (!open.value) return
  if (trigger.value?.contains(e.target) || menuEl.value?.contains(e.target)) return
  open.value = false
}

function onKey(e) {
  if (open.value && e.key === 'Escape') {
    open.value = false
    trigger.value?.focus()
  }
}

function onReposition() {
  if (open.value) place()
}

onMounted(() => {
  document.addEventListener('pointerdown', onDoc, true)
  document.addEventListener('keydown', onKey, true)
  window.addEventListener('resize', onReposition)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', onDoc, true)
  document.removeEventListener('keydown', onKey, true)
  window.removeEventListener('resize', onReposition)
})
</script>

<template>
  <button
    ref="trigger"
    type="button"
    class="proxy-menu-trigger"
    :class="{ open }"
    :disabled="disabled"
    aria-haspopup="menu"
    :aria-expanded="open ? 'true' : 'false'"
    @click="toggle"
  >
    <span>{{ label }}</span>
    <span class="proxy-menu-caret" aria-hidden="true">▾</span>
  </button>
  <Teleport to="body">
    <div v-if="open" ref="menuEl" class="list-dd-menu--portal proxy-menu" role="menu" :style="menuStyle">
      <template v-for="(item, idx) in items" :key="item.key || `divider-${idx}`">
        <div v-if="item.divider" class="proxy-menu-divider" role="separator"></div>
        <button
          v-else
          type="button"
          class="list-dd-opt"
          role="menuitem"
          :disabled="item.disabled"
          @click="pick(item)"
        >{{ item.label }}</button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.proxy-menu-trigger {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.proxy-menu-trigger.open {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.proxy-menu-caret {
  color: var(--muted);
  font-size: 11px;
  line-height: 1;
}
.proxy-menu-divider {
  height: 1px;
  margin: 4px 6px;
  background: var(--border);
}
.proxy-menu .list-dd-opt:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
