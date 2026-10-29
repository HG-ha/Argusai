<template>
  <div class="sb-preview" :aria-label="t('explorer.previewAria')" :aria-busy="preview.loading || preview.loadingMore">
    <header v-if="preview.path || preview.loading" class="sb-preview-head">
      <div class="sb-preview-title">
        <strong>{{ preview.name || preview.path }}</strong>
        <span v-if="preview.path" class="sb-preview-path" :title="preview.path">{{ preview.path }}</span>
        <span v-if="metaLine" class="sb-preview-meta">{{ metaLine }}</span>
      </div>
      <div class="sb-preview-actions">
        <div
          v-if="isMarkdownFile && preview.path"
          class="sb-md-view-toggle"
          role="tablist"
          :aria-label="t('explorer.mdViewAria')"
        >
          <button
            type="button"
            role="tab"
            :class="{ active: mdViewMode === 'preview' }"
            :aria-selected="mdViewMode === 'preview'"
            :title="t('explorer.mdPreviewTitle')"
            @click="setMdViewMode('preview')"
          >
            {{ t('explorer.mdPreview') }}
          </button>
          <button
            type="button"
            role="tab"
            :class="{ active: mdViewMode === 'source' }"
            :aria-selected="mdViewMode === 'source'"
            :title="t('explorer.mdSourceTitle')"
            @click="setMdViewMode('source')"
          >
            {{ t('explorer.mdSource') }}
          </button>
        </div>
        <button
          v-if="preview.kind === 'image' && preview.content"
          class="ghost tiny"
          :title="t('explorer.fullscreenTitle')"
          @click="openImageFullscreen"
        >
          {{ t('explorer.fullscreen') }}
        </button>
        <button
          v-if="preview.path && sessionId"
          type="button"
          class="ghost tiny sb-preview-ask"
          :title="t('explorer.askFileTitle')"
          @click="askWholeFile"
        >
          {{ t('explorer.ask') }}
        </button>
        <button
          v-if="preview.path"
          type="button"
          class="ghost tiny"
          :title="t('explorer.referenceFileTitle')"
          @click="referenceWholeFile"
        >
          {{ t('explorer.referenceFile') }}
        </button>
        <button
          v-if="preview.path && canOpenInOs"
          class="ghost tiny"
          :title="t('explorer.revealFileTitle')"
          @click="openInOs"
        >
          {{ t('explorer.openInOs') }}
        </button>
      </div>
    </header>

    <div class="sb-preview-body">
      <div v-if="!preview.path && !preview.loading" class="sb-preview-empty">
        <p>{{ t('explorer.pickFile') }}</p>
        <p class="muted">{{ t('explorer.pickFileHint') }}</p>
      </div>
      <div v-else-if="preview.loading" class="sb-preview-empty" role="status">{{ t('explorer.readingFile') }}</div>
      <div v-else-if="preview.error" class="sb-preview-empty error" role="alert">{{ preview.error }}</div>
      <div
        v-else-if="preview.kind === 'image'"
        class="sb-preview-image-wrap"
        :title="t('explorer.clickFullscreen')"
        role="button"
        tabindex="0"
        :aria-label="t('explorer.fullscreenAria', { name: preview.name || preview.path })"
        @click="openImageFullscreen"
        @keydown.enter="openImageFullscreen"
        @keydown.space.prevent="openImageFullscreen"
      >
        <img :src="preview.content" :alt="preview.name" draggable="false" />
      </div>
      <div
        v-else-if="preview.kind === 'text'"
        ref="textWrapRef"
        class="sb-preview-textwrap"
        @pointerdown="onPreviewPointerDown"
        @pointermove="onPreviewPointerMove"
        @pointerup="onPreviewPointerUp"
        @pointercancel="onPreviewPointerUp"
        @click="onPreviewClick"
      >
        <div
          v-if="showMdAsPreview"
          ref="markdownPreviewWrapRef"
          class="sb-preview-markdown-wrap"
          @keyup="onTextKeyup"
          @contextmenu="handleContextMenu"
          @scroll="hideSelectionUi"
        >
          <MarkdownText
            class="sb-preview-markdown"
            :text="formattedTextContent"
            :session-id="sessionId"
          />
        </div>
        <pre
          v-else
          ref="textContentRef"
          class="sb-preview-text"
          :class="'language-' + textLanguage"
          @keyup="onTextKeyup"
          @contextmenu="handleContextMenu"
          @scroll="hideSelectionUi"
        ><code v-if="skipSyntaxHighlight">{{ formattedTextContent }}</code><code v-else v-html="highlightedTextContent"></code></pre>
        <div
          v-if="preview.hasMore && preview.kind === 'text'"
          class="sb-preview-loadmore"
        >
          <button
            type="button"
            class="ghost tiny"
            :disabled="preview.loadingMore"
            @click="loadMorePreview"
          >
            {{ preview.loadingMore ? t('common.loading') : loadMoreLabel }}
          </button>
          <span class="sb-preview-loadmore-hint muted">{{ t('explorer.loadMoreHint') }}</span>
        </div>
        <div
          v-if="selectionPill.visible"
          class="selection-pill-row"
          :style="selectionPillStyle"
          @pointerdown.stop
          @pointerup.stop
          @click.stop
        >
          <button
            type="button"
            class="text-selection-pill"
            :disabled="!sessionId"
            :aria-label="t('explorer.askSelectionAria')"
            @click.prevent.stop="onPillAsk"
          >
            {{ t('explorer.ask') }}
          </button>
          <button
            type="button"
            class="text-selection-pill"
            :aria-label="t('explorer.referenceSelectionAria')"
            @click.prevent.stop="onPillReference"
          >
            {{ t('explorer.reference') }}
          </button>
        </div>
        <div
          v-if="selectionMenu.visible"
          class="sb-selection-menu"
          :style="selectionMenuStyle"
          role="menu"
          :aria-label="t('explorer.selectionActionsAria')"
          @pointerdown.prevent.stop
          @pointerup.prevent.stop
          @click.stop
        >
          <button type="button" role="menuitem" @click.prevent.stop="copyTextSelection">{{ t('common.copy') }}</button>
          <button type="button" role="menuitem" @click.prevent.stop="askTextSelection">{{ t('explorer.ask') }}</button>
          <button type="button" role="menuitem" @click.prevent.stop="referenceTextSelection">{{ t('explorer.referenceSnippet') }}</button>
        </div>
      </div>
      <div v-else class="sb-preview-empty">{{ t('explorer.unsupportedPreview') }}</div>
    </div>

  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { normalizeSelectionReference } from '../assistantProtocol.js'
import { useMediaPreview } from '../composables/useMediaPreview.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'
import { useSandboxExplain } from '../composables/useSandboxExplain.js'
import { localAnchorToViewport } from '../composables/usePreviewSelectionAnchor.js'
import MarkdownText from './assistant/MarkdownText.vue'
import { formatPreviewContent, highlightCode, lineAndColumnFromIndex } from './assistant/cards/cardUtils.js'

const props = defineProps({
  sessionId: { type: String, default: '' },
  page: { type: String, default: '' },
  context: { type: Object, default: null },
})

const emit = defineEmits(['reference-file'])

const { state: browserState, loadMorePreview } = useSandboxBrowser()
const { openAsk, closeExplain } = useSandboxExplain()
const mediaPreview = useMediaPreview()
const preview = computed(() => browserState.preview || {})

const textContentRef = ref(null)
const textWrapRef = ref(null)
const markdownPreviewWrapRef = ref(null)
const mdViewMode = ref('preview')
const previewSelection = ref(null)
const wholeFilePillMode = ref(false)
const selectionPill = ref({ visible: false, left: 0, top: 0 })
const selectionMenu = ref({ visible: false, left: 0, top: 0 })
const lastPointerClient = ref({ x: 0, y: 0, valid: false })
let pointerSelecting = false

const PILL_W = 168
const PILL_W_WHOLE = 220
const PILL_H = 30
const MENU_W = 132
const MENU_H = 72

const selectionPillStyle = computed(() => ({
  left: `${selectionPill.value.left}px`,
  top: `${selectionPill.value.top}px`,
}))
const selectionMenuStyle = computed(() => ({
  left: `${selectionMenu.value.left}px`,
  top: `${selectionMenu.value.top}px`,
}))

const metaLine = computed(() => {
  const p = preview.value
  const parts = []
  if (p.kind === 'text') parts.push(t('explorer.kindText'))
  else if (p.kind === 'image') parts.push(t('explorer.kindImage'))
  else if (p.kind) parts.push(p.kind)
  if (p.mime) parts.push(p.mime)
  if (p.size) parts.push(formatBytes(p.size))
  if (p.hasMore) parts.push(t('explorer.partialPreview'))
  else if (p.truncated) parts.push(t('explorer.truncated'))
  return parts.join(' · ')
})

const loadMoreLabel = computed(() => {
  const p = preview.value
  const loaded = Number(p.endOffset || p.content?.length || 0)
  const total = Number(p.size || p.totalChars || 0)
  if (total > 0) {
    return t('explorer.loadMoreBytes', { loaded: formatBytes(loaded), total: formatBytes(total) })
  }
  return t('explorer.loadMoreBytesOnly', { loaded: formatBytes(loaded) })
})

const skipSyntaxHighlight = computed(() => {
  const p = preview.value
  return (p.size || 0) > 200_000 || String(p.content || '').length > 48_000
})

const canOpenInOs = computed(() => Boolean(preview.value.path && props.sessionId))

function formatBytes(n) {
  const value = Number(n || 0)
  if (!value) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function textExt() {
  const name = String(preview.value.name || preview.value.path || '').toLowerCase()
  const m = name.match(/\.([a-z0-9]+)(?:$|\?)/)
  return m ? m[1] : ''
}

const textLanguage = computed(() => {
  const ext = textExt()
  if (ext === 'py') return 'python'
  if (['js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx'].includes(ext)) return 'javascript'
  if (['json', 'jsonl'].includes(ext)) return 'json'
  if (['md', 'markdown'].includes(ext)) return 'markdown'
  if (['html', 'htm', 'xml', 'vue'].includes(ext)) return 'html'
  if (ext === 'css' || ext === 'scss') return 'css'
  if (['yaml', 'yml'].includes(ext)) return 'yaml'
  if (['ps1', 'bat', 'cmd', 'sh', 'bash'].includes(ext)) return 'shell'
  return 'text'
})

const textMode = computed(() => {
  const ext = textExt()
  if (['json', 'jsonl'].includes(ext)) return 'json'
  return 'code'
})

const formattedTextContent = computed(() => {
  const raw = preview.value.content || ''
  if (raw.length > 48_000) return raw
  return formatPreviewContent(raw, textMode.value)
})
const highlightedTextContent = computed(() => {
  if (skipSyntaxHighlight.value) return ''
  return highlightCode(formattedTextContent.value, textLanguage.value)
})

const isMarkdownFile = computed(() => ['md', 'markdown'].includes(textExt()))
const showMdAsPreview = computed(() =>
  isMarkdownFile.value && mdViewMode.value === 'preview',
)

function setMdViewMode(mode) {
  if (mode !== 'preview' && mode !== 'source') return
  if (mdViewMode.value === mode) return
  mdViewMode.value = mode
  clearTextSelection()
}

function getSelectionContainer() {
  if (showMdAsPreview.value) return markdownPreviewWrapRef.value
  return textContentRef.value
}

/** Best-effort map rendered preview selection back to source line/column. */
function estimateSourceRange(selectedText, contentText) {
  const raw = String(selectedText || '')
  const trimmed = raw.trim()
  if (!trimmed) return null
  const content = String(contentText || '')
  let startIndex = content.indexOf(raw)
  let span = raw.length
  if (startIndex < 0) {
    startIndex = content.indexOf(trimmed)
    span = trimmed.length
  }
  if (startIndex < 0) return null
  const endIndex = startIndex + span
  return {
    start: lineAndColumnFromIndex(content, startIndex),
    end: lineAndColumnFromIndex(content, endIndex),
  }
}

function hideSelectionUi() {
  selectionPill.value = { visible: false, left: 0, top: 0 }
  selectionMenu.value = { visible: false, left: 0, top: 0 }
}

function clearTextSelection() {
  previewSelection.value = null
  wholeFilePillMode.value = false
  hideSelectionUi()
  lastPointerClient.value = { x: 0, y: 0, valid: false }
  pointerSelecting = false
}

function pillLayout() {
  return wholeFilePillMode.value
    ? { width: PILL_W_WHOLE, height: PILL_H }
    : { width: PILL_W, height: PILL_H }
}

function showFilePillAtPointer(event) {
  if (!preview.value.path) return
  wholeFilePillMode.value = true
  previewSelection.value = null
  const pos = uiPositionFromEvent(event, pillLayout().width, pillLayout().height)
  selectionMenu.value = { visible: false, left: 0, top: 0 }
  selectionPill.value = { visible: true, left: pos.left, top: pos.top }
}

function recordPointerClient(event) {
  if (!event || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return
  lastPointerClient.value = { x: event.clientX, y: event.clientY, valid: true }
}

function isPointerLikeEvent(event) {
  const t = String(event?.type || '')
  return t.startsWith('pointer') || t.startsWith('mouse') || t === 'contextmenu'
}

function caretClientPoint() {
  const sel = window.getSelection?.()
  if (!sel?.rangeCount) return null
  const range = sel.getRangeAt(0).cloneRange()
  range.collapse(false)
  const rects = range.getClientRects()
  const r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
  if (!r || (!r.width && !r.height && r.top === 0 && r.left === 0)) return null
  return { x: r.left + 4, y: r.bottom }
}

/** Place floating UI at the last pointer position; flip when near edges. */
function uiPositionFromEvent(event, width, height) {
  const wrap = textWrapRef.value
  if (!wrap) return { left: 8, top: 8 }
  const rect = wrap.getBoundingClientRect()
  let clientX
  let clientY
  if (lastPointerClient.value.valid) {
    clientX = lastPointerClient.value.x
    clientY = lastPointerClient.value.y
  } else if (isPointerLikeEvent(event) && Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) {
    clientX = event.clientX
    clientY = event.clientY
  } else {
    const caret = caretClientPoint()
    if (caret) {
      clientX = caret.x
      clientY = caret.y
    } else {
      clientX = rect.left + 24
      clientY = rect.top + 24
    }
  }
  const pad = 8
  const maxW = wrap.clientWidth || 0
  const maxH = wrap.clientHeight || 0
  const offsetX = 8
  let left = clientX - rect.left + offsetX
  let top = clientY - rect.top - height - 8
  if (left + width > maxW - pad) {
    left = clientX - rect.left - width - offsetX
  }
  if (top < pad) {
    top = clientY - rect.top + 12
  }
  if (top + height > maxH - pad) {
    top = clientY - rect.top - height - 8
  }
  return {
    left: Math.max(pad, Math.min(left, Math.max(pad, maxW - width - pad))),
    top: Math.max(pad, Math.min(top, Math.max(pad, maxH - height - pad))),
  }
}

function onPreviewPointerDown(event) {
  if (event.button !== 0) return
  recordPointerClient(event)
  pointerSelecting = true
}

function onPreviewPointerMove(event) {
  if (pointerSelecting || (event.buttons & 1)) {
    recordPointerClient(event)
  }
}

function onPreviewPointerUp(event) {
  if (event.target?.closest?.('.text-selection-pill, .sb-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  recordPointerClient(event)
  pointerSelecting = false
  requestAnimationFrame(() => handleTextSelection(event))
}

function captureTextSelection() {
  const container = getSelectionContainer()
  if (!container || preview.value.kind !== 'text') {
    return false
  }
  const sel = window.getSelection?.()
  if (!sel || !sel.rangeCount || sel.isCollapsed) {
    return false
  }
  const range = sel.getRangeAt(0)
  if (!container.contains(range.commonAncestorContainer)) {
    return false
  }
  const selectedRaw = String(sel.toString() || '')
  if (!selectedRaw.trim()) {
    return false
  }
  const contentText = formattedTextContent.value || ''
  const base = {
    file_path: preview.value.path || '',
    file_name: preview.value.name || '',
    text: selectedRaw,
  }
  if (showMdAsPreview.value) {
    const est = estimateSourceRange(selectedRaw, contentText)
    previewSelection.value = est
      ? {
        ...base,
        start_line: est.start.line,
        start_column: est.start.column,
        end_line: est.end.line,
        end_column: est.end.column,
      }
      : base
    return true
  }
  const startProbe = document.createRange()
  startProbe.selectNodeContents(container)
  startProbe.setEnd(range.startContainer, range.startOffset)
  const startIndex = startProbe.toString().length
  const endProbe = document.createRange()
  endProbe.selectNodeContents(container)
  endProbe.setEnd(range.endContainer, range.endOffset)
  const endIndex = endProbe.toString().length
  const startPos = lineAndColumnFromIndex(contentText, startIndex)
  const endPos = lineAndColumnFromIndex(contentText, endIndex)
  previewSelection.value = {
    ...base,
    start_line: startPos.line,
    start_column: startPos.column,
    end_line: endPos.line,
    end_column: endPos.column,
  }
  return true
}

function handleTextSelection(event) {
  // Loom22 §17 "predictable affordances": the floating pill must only
  // appear when the user has *actually* selected text. Older builds
  // fell back to a "whole-file" pill on empty selection, which made a
  // plain click on the preview pane spawn 提问 / 引用 chips — directly
  // contradicting the documented "select first, then act" behaviour
  // and confusing users into thinking they had a selection when they
  // did not. Whole-file referencing now lives only on header buttons
  // (or an explicit `referenceWholeFile` shortcut from the host).
  if (captureTextSelection(event)) {
    wholeFilePillMode.value = false
    const layout = pillLayout()
    const pos = uiPositionFromEvent(event, layout.width, layout.height)
    selectionMenu.value = { visible: false, left: 0, top: 0 }
    selectionPill.value = { visible: true, left: pos.left, top: pos.top }
    return
  }
  clearTextSelection()
}

function onPreviewClick(event) {
  if (event.target?.closest?.('.text-selection-pill, .sb-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  recordPointerClient(event)
  // Mirror of handleTextSelection above: a bare click MUST NOT show
  // the pill. The pointerup handler will already process the (much
  // more frequent) drag-select case via `handleTextSelection`.
  if (captureTextSelection(event)) return
  // No selection captured; make sure no stale pill hangs around.
  clearTextSelection()
}

function onTextKeyup(event) {
  if (event.key === 'Shift' || event.key === 'Control' || event.key === 'Alt' || event.key === 'Meta') {
    return
  }
  handleTextSelection(event)
}

function handleContextMenu(event) {
  // Same guarantee as handleTextSelection: only show UI when the user
  // really has a selection. Right-clicking empty space inside the
  // preview pane is left to the browser default (which we suppress
  // with preventDefault only when we actually have something to show).
  if (!captureTextSelection(event)) {
    clearTextSelection()
    return
  }
  event.preventDefault()
  recordPointerClient(event)
  wholeFilePillMode.value = false
  const pos = uiPositionFromEvent(event, MENU_W, MENU_H)
  selectionPill.value = { visible: false, left: 0, top: 0 }
  selectionMenu.value = { visible: true, left: pos.left, top: pos.top }
}

async function copyTextSelection() {
  const text = String(previewSelection.value?.text || window.getSelection?.()?.toString() || '').trim()
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
      console.warn('copy selection failed', err)
    }
  }
  selectionMenu.value = { visible: false, left: 0, top: 0 }
}

function onDocumentPointerDown(event) {
  const wrap = textWrapRef.value
  if (!wrap) return
  if (event.target?.closest?.('.text-selection-pill, .sb-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  if (!wrap.contains(event.target) && !event.target?.closest?.('.sb-explain-popover')) {
    clearTextSelection()
    closeExplain()
    try { window.getSelection?.()?.removeAllRanges() } catch (_) {}
  }
}

function selectionAnchorForExplain() {
  let local = { left: 8, top: 8 }
  if (selectionMenu.value.visible) {
    local = { left: selectionMenu.value.left, top: selectionMenu.value.top + MENU_H + 8 }
  } else if (selectionPill.value.visible) {
    local = { left: selectionPill.value.left, top: selectionPill.value.top + PILL_H + 8 }
  }
  return localAnchorToViewport(textWrapRef.value, local)
}

function buildSelectionReference() {
  const sel = previewSelection.value
  if (!sel?.file_path) return null
  return normalizeSelectionReference({
    kind: 'file_selection',
    file_path: sel.file_path,
    file_name: sel.file_name,
    start_line: sel.start_line,
    start_column: sel.start_column,
    end_line: sel.end_line,
    end_column: sel.end_column,
    text: sel.text,
  })
}

async function askTextSelection() {
  const ref = buildSelectionReference()
  if (!ref || !props.sessionId) return
  const anchor = selectionAnchorForExplain()
  hideSelectionUi()
  selectionMenu.value = { visible: false, left: 0, top: 0 }
  await openAsk({
    sessionId: props.sessionId,
    selectionReference: ref,
    page: props.page,
    context: props.context || { source: 'sandbox_preview' },
    anchor,
  })
}

function referenceTextSelection() {
  const sel = previewSelection.value
  if (!sel?.file_path) return
  emit('reference-file', {
    file_path: sel.file_path,
    file_name: sel.file_name,
    start_line: sel.start_line,
    start_column: sel.start_column,
    end_line: sel.end_line,
    end_column: sel.end_column,
    text: sel.text,
  })
  clearTextSelection()
  try { window.getSelection?.()?.removeAllRanges() } catch (_) {}
}

onMounted(() => {
  document.addEventListener('mousedown', onDocumentPointerDown)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocumentPointerDown)
})

function openImageFullscreen() {
  const p = preview.value
  if (p.kind !== 'image' || !p.content) return
  mediaPreview.openPayload({
    kind: 'image',
    content: p.content,
    name: p.name || p.path,
    path: p.path || '',
    sessionId: props.sessionId || '',
    mime: p.mime || '',
    size: p.size || 0,
    context: props.context || { source: 'sandbox_preview' },
  })
}

function referenceWholeFile() {
  const rel = preview.value.path
  if (!rel) return
  emit('reference-file', {
    file_path: rel,
    file_name: preview.value.name || rel.split('/').pop() || rel,
  })
  clearTextSelection()
}

async function askWholeFile() {
  const rel = preview.value.path
  if (!rel || !props.sessionId) return
  const name = preview.value.name || rel.split('/').pop() || rel
  const ref = normalizeSelectionReference({
    kind: 'file_selection',
    file_path: rel,
    file_name: name,
  })
  const anchor = selectionAnchorForExplain()
  hideSelectionUi()
  await openAsk({
    sessionId: props.sessionId,
    selectionReference: ref,
    page: props.page,
    context: props.context || { source: 'sandbox_preview' },
    anchor,
  })
}

function onPillAsk() {
  if (wholeFilePillMode.value) askWholeFile()
  else askTextSelection()
}

function onPillReference() {
  if (wholeFilePillMode.value) referenceWholeFile()
  else referenceTextSelection()
}

async function openInOs() {
  if (!props.sessionId || !preview.value.path) return
  const rel = String(preview.value.path || '').replace(/^\/+|\/+$/g, '')
  try {
    // Reveal in Explorer (select file) — same path as the sandbox toolbar
    // button. Default-app open is available via the OS after reveal.
    const r = await api.openAssistantSandbox(props.sessionId, rel)
    if (!r?.ok) throw new Error(r?.error || t('explorer.openFailed'))
  } catch (e) {
    showToast(t('explorer.openFailedWith', { msg: e?.message || e }), 'error')
  }
}

watch(() => preview.value.path, () => {
  mdViewMode.value = 'preview'
  clearTextSelection()
  closeExplain()
})
</script>

<style scoped>
.sb-preview {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-sunken, var(--panel-2));
}
.sb-preview-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 70%, transparent);
}
.sb-preview-title {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.sb-preview-title strong {
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-preview-title span {
  font-size: 10px;
  color: var(--text-2, var(--muted));
}
.sb-preview-title .sb-preview-path {
  max-width: min(48vw, 520px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg);
  font-family: "Cascadia Code", Consolas, monospace;
}
.sb-preview-title .sb-preview-meta {
  color: var(--muted-2, var(--muted));
}
.sb-preview-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px;
  flex-shrink: 0;
}
.sb-preview-ask {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  color: var(--fg-strong);
}
.sb-md-view-toggle {
  display: inline-flex;
  padding: 2px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg-sunken, var(--panel-2));
  gap: 2px;
}
.sb-md-view-toggle button {
  padding: 3px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-2, var(--muted));
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  line-height: 1.3;
}
.sb-md-view-toggle button:hover {
  color: var(--fg);
}
.sb-md-view-toggle button.active {
  background: color-mix(in srgb, var(--accent) 22%, transparent);
  color: var(--fg);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 35%, var(--border));
}
.sb-preview-markdown-wrap {
  width: 100%;
  height: 100%;
  overflow: auto;
  padding: 14px 18px;
}
.sb-preview-markdown-wrap :deep(.markdown-text) {
  font-size: 13px;
  line-height: 1.65;
  color: var(--fg);
}
.sb-preview-markdown-wrap :deep(.markdown-text pre) {
  max-height: none;
}
.sb-preview-markdown-wrap ::selection,
.sb-preview-markdown-wrap :deep(*)::selection {
  background: color-mix(in srgb, var(--accent) 38%, transparent);
}
.sb-preview-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  position: relative;
}
.sb-preview-empty {
  padding: 32px 20px;
  text-align: center;
  font-size: 12px;
  color: var(--text-2, var(--muted));
}
.sb-preview-empty.error { color: #f48771; }
.sb-preview-empty .muted {
  margin-top: 6px;
  font-size: 11px;
  opacity: 0.75;
}
.sb-preview-image-wrap {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: auto;
  padding: 12px;
  cursor: zoom-in;
}
.sb-preview-image-wrap img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  pointer-events: none;
}
.sb-preview-image-wrap:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 68%, transparent);
  outline-offset: -3px;
}
.sb-preview-textwrap {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
.sb-preview-loadmore {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 88%, transparent);
}
.sb-preview-loadmore-hint {
  font-size: 10px;
}
.sb-preview-textwrap > .sb-preview-text,
.sb-preview-textwrap > .sb-preview-markdown-wrap {
  flex: 1 1 auto;
  min-height: 0;
}
.sb-preview-text {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 14px 16px;
  overflow: auto;
  color: var(--fg);
  background: transparent;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 4;
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.sb-preview-text code {
  display: block;
  font: inherit;
  color: inherit;
  user-select: text;
  -webkit-user-select: text;
}
.sb-preview-text :deep(*) {
  user-select: text;
  -webkit-user-select: text;
}
.sb-preview-text :deep(.syntax-comment) {
  color: var(--syntax-comment, color-mix(in srgb, var(--muted) 82%, transparent));
  font-style: italic;
}
.sb-preview-text :deep(.syntax-string) { color: var(--syntax-string, #7dd3fc); }
.sb-preview-text :deep(.syntax-keyword) {
  color: var(--syntax-keyword, #c084fc);
  font-weight: 650;
}
.sb-preview-text :deep(.syntax-number) { color: var(--syntax-number, #fbbf24); }
.sb-preview-text :deep(.syntax-function) { color: var(--syntax-function, var(--accent)); }
.sb-preview-text :deep(.syntax-class) {
  color: var(--syntax-class, var(--ok));
  font-weight: 650;
}
.sb-preview-text :deep(.syntax-builtin) { color: var(--syntax-builtin, var(--accent)); }
.sb-preview-text ::selection,
.sb-preview-text :deep(*)::selection {
  background: color-mix(in srgb, var(--accent) 38%, transparent);
}
.selection-pill-row {
  position: absolute;
  z-index: 5;
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 0;
  border-radius: 999px;
  background: transparent;
  border: 0;
  box-shadow: none;
}
.text-selection-pill {
  position: relative;
  z-index: 5;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  background: color-mix(in srgb, var(--bg-sunken) 72%, var(--accent) 28%);
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  white-space: nowrap;
  user-select: none;
}
.text-selection-pill:hover:not(:disabled) {
  background: color-mix(in srgb, var(--bg-sunken) 58%, var(--accent) 42%);
  border-color: color-mix(in srgb, var(--accent) 62%, var(--border));
}
.sb-selection-menu {
  position: absolute;
  z-index: 6;
  min-width: 128px;
  padding: 4px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--card-bg, var(--panel)) 96%, transparent);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  gap: 2px;
  user-select: none;
}
.sb-selection-menu button {
  display: block;
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.sb-selection-menu button:hover,
.sb-selection-menu button:focus-visible {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  outline: none;
}
.sb-preview-foot {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 94%, #000 6%);
}
.preview-foot-pill {
  padding: 5px 14px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  background: color-mix(in srgb, var(--accent-2, var(--accent)) 92%, #000 8%);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.24);
  white-space: nowrap;
}
.preview-foot-pill:hover:not(:disabled) {
  background: var(--accent);
  border-color: var(--accent);
}
.preview-foot-pill:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@container sandbox-panel (max-width: 680px) {
  .sb-preview-head {
    align-items: flex-start;
    padding: 7px 10px;
  }
  .sb-preview-title .sb-preview-path {
    max-width: 58vw;
  }
  .sb-preview-actions {
    max-width: 52%;
  }
  .sb-preview-text {
    padding: 12px;
  }
}
</style>
