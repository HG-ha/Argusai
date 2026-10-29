<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="sb-ctx-backdrop"
      role="presentation"
      @pointerdown.self="emit('close')"
      @contextmenu.prevent="emit('close')"
      @keydown.esc.stop="emit('close')"
    >
      <div
        ref="menuRef"
        class="sb-ctx-menu"
        role="menu"
        :aria-label="t('explorer.fileActionsAria')"
        tabindex="-1"
        :style="menuStyle"
        @pointerdown.stop
        @click.stop
        @contextmenu.prevent.stop
        @keydown="onMenuKeydown"
      >
        <template
          v-for="action in actions"
          :key="action.id"
        >
          <div v-if="action.separatorBefore" class="sb-ctx-separator" role="separator"></div>
          <button
            type="button"
            role="menuitem"
            class="sb-ctx-item"
            :class="{ danger: action.danger, disabled: action.disabled }"
            :disabled="action.disabled"
            :aria-disabled="action.disabled || undefined"
            @click="onAction(action)"
          >
            {{ action.label }}
          </button>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { t } from '../i18n.js'

const props = defineProps({
  open: { type: Boolean, default: false },
  x: { type: Number, default: 0 },
  y: { type: Number, default: 0 },
  actions: { type: Array, default: () => [] },
})

const emit = defineEmits(['close', 'action'])
const menuRef = ref(null)
let previousFocus = null

const menuStyle = computed(() => {
  const pad = 8
  const estW = 168
  const estH = Math.max(120, (props.actions?.length || 0) * 32 + 12)
  let left = props.x
  let top = props.y
  if (left + estW > window.innerWidth - pad) left = Math.max(pad, window.innerWidth - estW - pad)
  if (top + estH > window.innerHeight - pad) top = Math.max(pad, window.innerHeight - estH - pad)
  return { left: `${left}px`, top: `${top}px` }
})

function onAction(action) {
  if (!action || action.disabled) return
  emit('action', action.id)
}

function enabledItems() {
  return [...(menuRef.value?.querySelectorAll?.('.sb-ctx-item:not(:disabled)') || [])]
}

function onMenuKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key === 'Tab') {
    emit('close')
    return
  }
  const items = enabledItems()
  if (!items.length) return
  const current = items.indexOf(document.activeElement)
  let next = null
  if (event.key === 'ArrowDown') next = items[(current + 1 + items.length) % items.length]
  else if (event.key === 'ArrowUp') next = items[(current - 1 + items.length) % items.length]
  else if (event.key === 'Home') next = items[0]
  else if (event.key === 'End') next = items[items.length - 1]
  if (!next) return
  event.preventDefault()
  next.focus()
}

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement
    await nextTick()
    enabledItems()[0]?.focus()
    return
  }
  if (previousFocus?.isConnected) previousFocus.focus()
  previousFocus = null
})
</script>

<style scoped>
.sb-ctx-backdrop {
  position: fixed;
  inset: 0;
  /* Above .assistant-panel (50) / drop-overlay (60); below toasts (~10500). */
  z-index: 80;
}
.sb-ctx-menu {
  position: fixed;
  min-width: 168px;
  padding: 6px;
  border-radius: 10px;
  border: 1px solid var(--border-strong, rgba(255, 255, 255, 0.2));
  /* Opaque solid — Teleport + glass panel used to paint this under a frosted
     layer so the menu looked like a transparent bubble. */
  background: var(--bg-sunken, #181818);
  color: var(--fg);
  box-shadow: var(--shadow-md), 0 0 0 1px rgba(0, 0, 0, 0.35);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sb-ctx-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 7px 10px;
  border: none;
  border-radius: 7px;
  background: transparent;
  box-shadow: none;
  color: var(--fg);
  font-size: 12px;
  cursor: pointer;
  transform: none;
}
.sb-ctx-item:hover:not(:disabled) {
  background: var(--panel-hover);
  color: var(--fg-strong);
  border-color: transparent;
  transform: none;
}
.sb-ctx-item:focus-visible {
  outline: none;
  background: color-mix(in srgb, var(--accent) 18%, var(--panel-hover));
  color: var(--fg-strong);
}
.sb-ctx-item.danger {
  color: var(--err, #f48771);
}
.sb-ctx-separator {
  height: 1px;
  margin: 4px 6px;
  background: var(--border);
}
.sb-ctx-item.disabled,
.sb-ctx-item:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
