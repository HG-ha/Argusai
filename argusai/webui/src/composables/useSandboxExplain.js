import { reactive } from 'vue'
import { api } from '../api.js'
import { i18n, t } from '../i18n.js'
import { normalizeSelectionReference } from '../assistantProtocol.js'

const POLL_MS = 280

/** @type {{ id: string, labelKey: string, promptKey: string }[]} */
const ASK_PRESET_DEFS = [
  { id: 'explain', labelKey: 'explorer.askPresetExplain', promptKey: 'explorer.askPromptExplain' },
  { id: 'risk', labelKey: 'explorer.askPresetRisk', promptKey: 'explorer.askPromptRisk' },
  { id: 'verify', labelKey: 'explorer.askPresetVerify', promptKey: 'explorer.askPromptVerify' },
  { id: 'dataflow', labelKey: 'explorer.askPresetDataflow', promptKey: 'explorer.askPromptDataflow' },
]

export function getAskPresets() {
  void i18n.locale
  return ASK_PRESET_DEFS.map((item) => ({
    id: item.id,
    label: t(item.labelKey),
    prompt: t(item.promptKey),
  }))
}


const state = reactive({
  open: false,
  loading: false,
  text: '',
  error: '',
  runId: '',
  anchor: { left: 8, top: 8 },
  question: getAskPresets()[0].prompt,
  selectedPresetId: getAskPresets()[0].id,
  pending: null,
})

let pollTimer = null
let runSeq = 0

function stopPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function resetContent() {
  state.text = ''
  state.error = ''
  state.runId = ''
}

export function useSandboxExplain() {
  async function cancelExplain() {
    const rid = state.runId
    stopPoll()
    state.loading = false
    if (rid) {
      try {
        await api.cancelAssistantStream?.(rid)
      } catch (_) {}
    }
    state.runId = ''
  }

  function closeExplain() {
    cancelExplain()
    state.open = false
    state.pending = null
    resetContent()
    state.question = getAskPresets()[0].prompt
    state.selectedPresetId = getAskPresets()[0].id
  }

  function applyPreset(preset) {
    if (!preset) return
    state.selectedPresetId = preset.id
    state.question = preset.prompt
  }

  async function pollExplainRun(runId, seq) {
    let offset = 0
    let finished = false
    while (!finished && seq === runSeq && state.runId === runId) {
      const chunk = await api.getAssistantStreamEvents(runId, offset)
      if (seq !== runSeq || state.runId !== runId) return
      if (!chunk?.ok) {
        state.error = chunk?.error || t('explorer.askFailed')
        state.loading = false
        return
      }
      offset = chunk.next || offset
      for (const ev of chunk.events || []) {
        const kind = ev.kind || ev.type
        const data = ev.data || null
        if (kind === 'text_chunk' || kind === 'delta') {
          state.text += (data && data.text) || ev.text || ''
        } else if (kind === 'run_done' || kind === 'done') {
          const r = ev.result || (data && data.result) || {}
          if (!r.ok && r.error) state.error = r.error
        } else if (kind === 'run_error' || kind === 'error') {
          state.error = (data && data.message) || ev.error || t('explorer.askFailed')
        }
      }
      finished = !!chunk.done
      if (!finished) {
        await new Promise((r) => { pollTimer = setTimeout(r, POLL_MS) })
      }
    }
    if (seq === runSeq) state.loading = false
  }

  async function openAsk({ sessionId, selectionReference, page = '', context = null, anchor = null }) {
    const ref = normalizeSelectionReference(selectionReference)
    if (!ref || !sessionId) return
    await cancelExplain()
    runSeq += 1
    resetContent()
    state.open = true
    state.loading = false
    state.question = getAskPresets()[0].prompt
    state.selectedPresetId = getAskPresets()[0].id
    state.pending = {
      sessionId,
      selectionReference: ref,
      page,
      context: context || { source: 'sandbox_preview' },
    }
    if (anchor && Number.isFinite(anchor.left) && Number.isFinite(anchor.top)) {
      state.anchor = { left: anchor.left, top: anchor.top }
    }
  }

  async function submitAsk(question) {
    const pending = state.pending
    const q = String(question ?? state.question ?? '').trim()
    if (!pending || !q) return
    await cancelExplain()
    runSeq += 1
    const seq = runSeq
    resetContent()
    state.open = true
    state.loading = true
    state.question = q
    try {
      const started = await api.startAssistantExplain({
        session_id: pending.sessionId,
        page: pending.page,
        context: pending.context,
        selection_reference: pending.selectionReference,
        question: q,
      })
      if (!started?.ok) throw new Error(started?.error || t('explorer.askSubmitFailed'))
      state.runId = started.run_id
      await pollExplainRun(started.run_id, seq)
    } catch (e) {
      if (seq === runSeq) {
        state.error = e?.message || String(e)
        state.loading = false
      }
    }
  }

  return {
    state,
    ASK_PRESETS: getAskPresets,
    openAsk,
    submitAsk,
    applyPreset,
    cancelExplain,
    closeExplain,
  }
}
