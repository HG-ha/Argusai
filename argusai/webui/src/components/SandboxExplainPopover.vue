<template>
  <Teleport to="body">
  <div
    v-if="state.open"
    ref="popoverEl"
    class="sb-explain-popover"
    :class="{ dragging: isDragging }"
    :style="popoverStyle"
    role="dialog"
    aria-modal="false"
    aria-labelledby="sb-explain-title"
    tabindex="-1"
    @mousedown.stop
    @keydown.esc.stop="closeExplain"
  >
    <header
      class="sb-explain-head"
      :title="t('explorer.dragTitle')"
      @pointerdown="startDrag"
    >
      <div class="sb-explain-heading">
        <span id="sb-explain-title" class="sb-explain-title">{{ t('explorer.askTitle') }}</span>
        <span v-if="selectionLabel" class="sb-explain-context" :title="selectionLabel">{{ selectionLabel }}</span>
      </div>
      <div class="sb-explain-head-actions" data-no-drag>
        <button
          v-if="state.text"
          type="button"
          class="ghost tiny"
          :title="t('explorer.copyAnswer')"
          @click="copyText"
        >
          {{ t('common.copy') }}
        </button>
        <button
          v-if="canSendToAssistant"
          type="button"
          class="ghost tiny"
          :title="t('explorer.sendToChatTitle')"
          @click="emit('send-to-assistant')"
        >
          {{ t('explorer.sendToChat') }}
        </button>
        <button
          v-if="state.loading"
          type="button"
          class="ghost tiny"
          :title="t('explorer.stopTitle')"
          @click="cancelExplain"
        >
          {{ t('common.stop') }}
        </button>
        <button
          type="button"
          class="ghost tiny icon"
          :title="t('explorer.closeAsk')"
          :aria-label="t('explorer.closeAskAria')"
          @click="closeExplain"
        >
          ×
        </button>
      </div>
    </header>
    <div class="sb-explain-body" data-no-drag>
      <div class="sb-ask-compose">
        <div class="sb-ask-presets" role="group" :aria-label="t('explorer.presetsAria')">
          <button
            v-for="preset in askPresets"
            :key="preset.id"
            type="button"
            class="sb-ask-preset"
            :class="{ active: state.selectedPresetId === preset.id }"
            :disabled="state.loading"
            @click="onPreset(preset)"
          >
            {{ preset.label }}
          </button>
        </div>
        <textarea
          ref="questionInputRef"
          v-model="state.question"
          class="sb-ask-input"
          rows="3"
          :placeholder="t('explorer.questionPlaceholder')"
          :aria-label="t('explorer.questionAria')"
          :disabled="state.loading"
          @keydown="onQuestionKeydown"
        />
        <button
          type="button"
          class="primary tiny sb-ask-submit"
          :disabled="!canSubmit"
          @click="submitQuestion"
        >
          {{ state.loading ? t('explorer.generating') : t('explorer.ask') }}
        </button>
      </div>
      <div
        v-if="state.loading || state.error || state.text"
        class="sb-ask-answer"
        aria-live="polite"
        :aria-busy="state.loading"
      >
        <div v-if="state.loading && !state.text" class="sb-explain-muted" role="status">{{ t('explorer.generatingAnswer') }}</div>
        <div v-else-if="state.error" class="sb-explain-error" role="alert">{{ state.error }}</div>
        <MarkdownText
          v-else-if="state.text"
          class="sb-explain-md"
          :text="state.text"
          :session-id="sessionId"
        />
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { t } from '../i18n.js'
import MarkdownText from './assistant/MarkdownText.vue'
import { getAskPresets, useSandboxExplain } from '../composables/useSandboxExplain.js'

defineProps({
  sessionId: { type: String, default: '' },
  canSendToAssistant: { type: Boolean, default: true },
})

const emit = defineEmits(['send-to-assistant'])

const { state, submitAsk, applyPreset, cancelExplain, closeExplain } = useSandboxExplain()
const askPresets = computed(() => getAskPresets())

const popoverEl = ref(null)
const questionInputRef = ref(null)
const isDragging = ref(false)

const popoverStyle = computed(() => ({
  left: `${state.anchor.left}px`,
  top: `${state.anchor.top}px`,
}))

const canSubmit = computed(() => {
  return !!state.pending && String(state.question || '').trim() && !state.loading
})

const selectionLabel = computed(() => {
  const refValue = state.pending?.selectionReference || {}
  const path = String(refValue.file_name || refValue.file_path || '').trim()
  if (!path) return ''
  const start = Number(refValue.start_line || 0)
  const end = Number(refValue.end_line || 0)
  if (!start) return path
  if (end && end !== start) return t('explorer.lineRangeSpan', { path, start, end })
  return t('explorer.lineRange', { path, start })
})

function onPreset(preset) {
  applyPreset(preset)
}

function submitQuestion() {
  if (!canSubmit.value) return
  submitAsk(state.question)
}

function onQuestionKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    submitQuestion()
  }
}

function clampAnchor(left, top) {
  const pop = popoverEl.value
  if (!pop) {
    state.anchor = { left, top }
    return
  }
  const pad = 8
  const maxLeft = Math.max(pad, window.innerWidth - pop.offsetWidth - pad)
  const maxTop = Math.max(pad, window.innerHeight - pop.offsetHeight - pad)
  state.anchor = {
    left: Math.max(pad, Math.min(left, maxLeft)),
    top: Math.max(pad, Math.min(top, maxTop)),
  }
}

function startDrag(event) {
  if (event.button !== 0) return
  if (event.target?.closest?.('button, a, input, textarea, [data-no-drag]')) return
  event.preventDefault()
  const startX = event.clientX
  const startY = event.clientY
  const startLeft = state.anchor.left
  const startTop = state.anchor.top
  isDragging.value = true
  const onMove = (ev) => {
    clampAnchor(startLeft + ev.clientX - startX, startTop + ev.clientY - startY)
  }
  const stop = () => {
    isDragging.value = false
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', stop)
  window.addEventListener('pointercancel', stop)
}

async function copyText() {
  const text = String(state.text || '').trim()
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch (_) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    } catch (err) {
      console.warn('copy ask answer failed', err)
    }
  }
}

watch(() => state.open, async (open) => {
  if (!open) return
  await nextTick()
  questionInputRef.value?.focus()
  questionInputRef.value?.select()
})
</script>

<style scoped>
.sb-explain-popover {
  position: fixed;
  z-index: 9100;
  width: min(440px, calc(100vw - 24px));
  max-height: min(520px, 78vh);
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}
.sb-explain-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 85%, var(--bg-sunken));
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
}
.sb-explain-popover.dragging .sb-explain-head {
  cursor: grabbing;
}
.sb-explain-title {
  font-size: 12px;
  font-weight: 650;
  color: var(--fg-strong);
}
.sb-explain-heading {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.sb-explain-context {
  max-width: 210px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted-2, var(--muted));
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 9px;
}
.sb-explain-head-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  cursor: default;
}
.sb-explain-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sb-ask-compose {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sb-ask-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.sb-ask-preset {
  font-size: 11px;
  line-height: 1.2;
  padding: 4px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-sunken);
  color: var(--fg);
  cursor: pointer;
}
.sb-ask-preset:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--fg-strong);
}
.sb-ask-preset.active {
  border-color: color-mix(in srgb, var(--accent) 70%, var(--border));
  background: color-mix(in srgb, var(--accent) 12%, var(--panel));
  color: var(--fg-strong);
}
.sb-ask-preset:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.sb-ask-input {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  min-height: 64px;
  max-height: 140px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg-sunken);
  color: var(--fg);
  font-size: 12px;
  line-height: 1.5;
  font-family: inherit;
}
.sb-ask-input:focus {
  outline: none;
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
}
.sb-ask-input:disabled {
  opacity: 0.7;
}
.sb-ask-submit {
  align-self: flex-end;
}
.sb-ask-answer {
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.sb-explain-md {
  font-size: 12px;
  line-height: 1.6;
}
.sb-explain-muted {
  font-size: 12px;
  color: var(--muted);
}
.sb-explain-error {
  font-size: 12px;
  color: var(--err);
}
@media (max-width: 620px) {
  .sb-explain-popover {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-height: min(560px, calc(100vh - 16px));
  }
  .sb-explain-head {
    align-items: flex-start;
  }
  .sb-explain-head-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
}
</style>
