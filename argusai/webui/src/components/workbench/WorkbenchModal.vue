<script setup>
import { computed, nextTick, onBeforeUnmount, ref, useSlots, watch } from 'vue'
import { t } from '../../i18n.js'
import WorkbenchOverlay from './WorkbenchOverlay.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  size: {
    type: String,
    default: 'md',
    validator: value => ['sm', 'md', 'lg', 'xl'].includes(value),
  },
  closeLabel: { type: String, default: '' },
  closeOnBackdrop: { type: Boolean, default: true },
  closeOnEsc: { type: Boolean, default: true },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])
const slots = useSlots()
const shell = ref(null)
const titleId = `workbench-modal-title-${Math.random().toString(36).slice(2, 9)}`
const descriptionId = `workbench-modal-description-${Math.random().toString(36).slice(2, 9)}`
let previouslyFocused = null

const hasFooter = computed(() => Boolean(slots.footer))
const resolvedCloseLabel = computed(() => props.closeLabel || t('common.close'))

function requestClose(reason) {
  if (props.busy) return
  emit('close', reason)
}

function focusableElements() {
  return [...(shell.value?.querySelectorAll?.(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) || [])].filter(el => !el.hidden && el.getAttribute('aria-hidden') !== 'true')
}

function onKeydown(event) {
  if (!props.open) return
  if (event.key === 'Escape' && props.closeOnEsc) {
    event.preventDefault()
    requestClose('escape')
    return
  }
  if (event.key !== 'Tab') return
  const items = focusableElements()
  if (!items.length) {
    event.preventDefault()
    shell.value?.focus?.()
    return
  }
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(
  () => props.open,
  async open => {
    if (open) {
      previouslyFocused = document.activeElement
      document.addEventListener('keydown', onKeydown, true)
      await nextTick()
      const preferred = shell.value?.querySelector?.('[autofocus]')
      const first = focusableElements()[0]
      ;(preferred || first || shell.value)?.focus?.()
    } else {
      document.removeEventListener('keydown', onKeydown, true)
      previouslyFocused?.focus?.()
      previouslyFocused = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown, true)
  previouslyFocused?.focus?.()
})
</script>

<template>
  <WorkbenchOverlay
    :open="open"
    level="modal"
    :close-on-backdrop="closeOnBackdrop"
    @close="requestClose"
  >
    <section
      ref="shell"
      class="workbench-modal"
      :class="`size-${size}`"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="title ? titleId : undefined"
      :aria-describedby="description ? descriptionId : undefined"
      tabindex="-1"
      @pointerdown.stop
    >
      <header class="workbench-modal-head">
        <div class="workbench-modal-heading">
          <slot name="heading">
            <h2 v-if="title" :id="titleId">{{ title }}</h2>
            <p v-if="description" :id="descriptionId">{{ description }}</p>
          </slot>
        </div>
        <button
          type="button"
          class="workbench-modal-close"
          :aria-label="resolvedCloseLabel"
          :title="resolvedCloseLabel"
          :disabled="busy"
          @click="requestClose('button')"
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="M3.5 3.5l9 9m0-9l-9 9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </header>
      <div class="workbench-modal-body">
        <slot />
      </div>
      <footer v-if="hasFooter" class="workbench-modal-foot">
        <slot name="footer" />
      </footer>
    </section>
  </WorkbenchOverlay>
</template>

<style scoped>
.workbench-modal {
  display: flex;
  width: min(680px, 100%);
  max-height: min(820px, calc(100dvh - var(--titlebar-h, 0px) - 48px));
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 16px;
  background: var(--card-bg, var(--panel));
  color: var(--fg);
  box-shadow: var(--shadow-md);
}
.workbench-modal.size-sm { width: min(440px, 100%); }
.workbench-modal.size-md { width: min(600px, 100%); }
.workbench-modal.size-lg { width: min(760px, 100%); }
.workbench-modal.size-xl { width: min(980px, 100%); }
.workbench-modal-head {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 15px 18px 13px;
  border-bottom: 1px solid var(--border);
}
.workbench-modal-heading {
  min-width: 0;
}
.workbench-modal-heading h2 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 16px;
  line-height: 1.35;
}
.workbench-modal-heading p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.workbench-modal-close {
  display: inline-flex;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
  cursor: pointer;
}
.workbench-modal-close:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--panel-hover);
  color: var(--fg-strong);
}
.workbench-modal-close:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.workbench-modal-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 16px 18px;
}
.workbench-modal-foot {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--border);
  background: var(--panel-2);
}
.workbench-modal-foot :deep(.modal-actions) {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--control-cluster-gap, 8px);
}

@media (max-width: 640px) {
  .workbench-modal {
    width: 100%;
    max-height: 100%;
    border-radius: 14px;
  }
  .workbench-modal-head,
  .workbench-modal-body,
  .workbench-modal-foot {
    padding-right: 14px;
    padding-left: 14px;
  }
}
</style>
