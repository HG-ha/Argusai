<script setup>
import { computed, onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { normalizeSelectionReference } from '../assistantProtocol.js'
import { useMediaPreview } from '../composables/useMediaPreview.js'
import { useSandboxExplain } from '../composables/useSandboxExplain.js'
import ImageCanvasEditor from './ImageCanvasEditor.vue'
import { usePreviewSelectionAnchor, localAnchorToViewport } from '../composables/usePreviewSelectionAnchor.js'
import { formatPreviewContent, highlightCode, lineAndColumnFromIndex } from './assistant/cards/cardUtils.js'

const { state, closePreview, openFile, loadMoreMediaText } = useMediaPreview()
const { openAsk, closeExplain } = useSandboxExplain()
const scale = ref(1)
const pan = ref({ x: 0, y: 0 })
const dragging = ref(false)
const sendingAnnotated = ref(false)
const savingMedia = ref(false)
const copyingImage = ref(false)
const editingImage = ref(false)
const editorSource = ref('')
const mediaRenderKey = ref(0)
const retryingPreview = ref(false)
const mediaLoadFailed = ref(false)
const previewShellRef = ref(null)
const previewCloseRef = ref(null)
let previewReturnFocus = null
let dragStart = null

const item = computed(() => state.item || {})
const title = computed(() => item.value.name || item.value.path || item.value.url || t('mediaPreview.title'))
const canOpenExternal = computed(() => /^https?:\/\//i.test(item.value.url || item.value.content || ''))
const canOpenPath = computed(() => Boolean(item.value.path))
const isAttachmentEditor = computed(() => item.value.applyTarget === 'attachment')
const previewKindLabel = computed(() => ({
  image: t('mediaPreview.kindImage'),
  pdf: t('mediaPreview.kindPdf'),
  video: t('mediaPreview.kindVideo'),
  audio: t('mediaPreview.kindAudio'),
  text: t('mediaPreview.kindText'),
  loading: t('mediaPreview.kindMedia'),
})[item.value.kind] || t('mediaPreview.kindMedia'))
const previewMetadata = computed(() => [
  previewKindLabel.value,
  item.value.mime || '',
  formatBytes(item.value.size),
  textHasMore.value ? t('mediaPreview.partialPreview') : '',
  !textHasMore.value && item.value.truncated ? t('mediaPreview.truncated') : '',
].filter(Boolean))
const previewSource = computed(() => item.value.path || item.value.url || '')
const previewHasContent = computed(() => Boolean(String(item.value.content || '')))
const previewIsEmpty = computed(() =>
  ['image', 'pdf', 'video', 'audio', 'text'].includes(item.value.kind) && !previewHasContent.value
)
const previewCanRetry = computed(() =>
  mediaLoadFailed.value
  || item.value.kind === 'loading'
  || Boolean(item.value.kind === 'text' && item.value.path && item.value.hasMore)
)
const previewErrorTitle = computed(() =>
  previewCanRetry.value
    ? t('mediaPreview.loadFailed', { kind: previewKindLabel.value })
    : t('mediaPreview.actionFailed')
)
const previewHint = computed(() => {
  if (item.value.kind === 'image') {
    return t('mediaPreview.zoomHint', { percent: Math.round(scale.value * 100) })
  }
  if (item.value.kind === 'text') return t('mediaPreview.textHint')
  if (item.value.kind === 'pdf') return t('mediaPreview.pdfHint')
  if (item.value.kind === 'video' || item.value.kind === 'audio') return t('mediaPreview.playbackHint')
  return ''
})

// ---- Text preview: language inference + syntax highlight + selection ------
//
// For the `text` kind we want the same affordances the AssistantCard's old
// in-card preview pane had:
//   1. naive language-aware syntax highlight (via shared `highlightCode`)
//   2. rendering quirks per format (JSON pretty-print, markdown rendered raw)
//   3. selecting a span pops a floating "引用到对话" chip — clicking it
//      dispatches `argusai:reference-file` with line / column / text so the
//      AssistantPanel turns it into a `selection_reference` chip above the
//      input area.

const textContentRef = ref(null)
const textWrapRef = ref(null)
const previewSelection = ref(null)
const wholeFilePillMode = ref(false)
const PILL_W = 168
const PILL_W_WHOLE = 300
const PILL_H = 32
const MENU_W = 132
const MENU_H = 96

// Destructure top-level so Vue unwraps the composable's computed style refs.
// Nested ``selectionUi.pillStyle`` in templates stays a ComputedRef and
// :style never receives { left, top } — pills stick at (0, 0).
const {
  anchor: selectionAnchor,
  menu: selectionMenu,
  pillStyle: selectionPillStyle,
  menuStyle: selectionMenuStyle,
  hideAnchor: hideSelectionAnchor,
  resetPointer: resetSelectionPointer,
  showPill: showSelectionPill,
  showMenu: showSelectionMenu,
  recordPointerClient: recordSelectionPointer,
  onPointerDown: onSelectionPointerDown,
  onPointerMove: onSelectionPointerMove,
} = usePreviewSelectionAnchor(textWrapRef, {
  ignoreSelector: '.text-selection-pill, .preview-selection-menu, .selection-pill-row, .sb-explain-popover',
  pillWidth: PILL_W,
  pillHeight: PILL_H,
  menuWidth: MENU_W,
  menuHeight: MENU_H,
})

const sessionId = computed(() => String(item.value.sessionId || ''))

function textExt() {
  const name = String(item.value.name || item.value.path || '').toLowerCase()
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

const skipSyntaxHighlight = computed(() => {
  const it = item.value
  return (it.size || 0) > 200_000 || String(it.content || '').length > 48_000
})

const formattedTextContent = computed(() => {
  const raw = item.value.content || ''
  if (raw.length > 48_000) return raw
  return formatPreviewContent(raw, textMode.value)
})
const highlightedTextContent = computed(() => {
  if (skipSyntaxHighlight.value) return ''
  return highlightCode(formattedTextContent.value, textLanguage.value)
})

const textHasMore = computed(() => Boolean(item.value.hasMore))
const loadMoreLabel = computed(() => {
  const loaded = String(item.value.content || '').length
  const total = Number(item.value.size || item.value.totalChars || 0)
  if (total > 0) {
    return t('mediaPreview.loadMoreBytes', { loaded: formatBytes(loaded), total: formatBytes(total) })
  }
  return t('mediaPreview.loadMoreBytesOnly', { loaded: formatBytes(loaded) })
})

/** Map rendered selection back to source line/column without DOM walks (large files). */
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

function clearTextSelection() {
  previewSelection.value = null
  wholeFilePillMode.value = false
  hideSelectionAnchor()
  resetSelectionPointer()
}

function pillLayout() {
  return wholeFilePillMode.value
    ? { width: PILL_W_WHOLE, height: PILL_H }
    : { width: PILL_W, height: PILL_H }
}

function showFilePillAtPointer(event) {
  wholeFilePillMode.value = true
  previewSelection.value = null
  showSelectionPill(event, pillLayout())
}

function captureTextSelection() {
  const container = textContentRef.value
  if (!container || item.value.kind !== 'text') {
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
    file_path: item.value.path || '',
    file_name: item.value.name || '',
    text: selectedRaw,
  }
  // Syntax-highlighted megabyte bundles make selectNodeContents()+toString()
  // hang the WebView; use string search for large / plain-text previews.
  if (skipSyntaxHighlight.value) {
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
    start_index: startIndex,
    end_index: endIndex,
    start_line: startPos.line,
    start_column: startPos.column,
    end_line: endPos.line,
    end_column: endPos.column,
  }
  return true
}

function showSelectionUi(event) {
  if (!previewSelection.value?.text) {
    clearTextSelection()
    return
  }
  wholeFilePillMode.value = false
  if (event) showSelectionPill(event, pillLayout())
}

function handleTextSelection(event) {
  // Pill only appears for an *actual* selection (Loom22 §17
  // predictable affordances). The previous "no selection? show
  // whole-file pill anyway" branch made a plain click on the preview
  // body spawn 提问 / 引用 chips, which contradicted the documented
  // "select first" behaviour and confused users.
  if (captureTextSelection()) {
    showSelectionUi(event)
    return
  }
  clearTextSelection()
}

/** pywebview often clears getSelection() before rAF; capture synchronously on release. */
function onTextPointerUp(event) {
  if (event.target?.closest?.('.text-selection-pill, .preview-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  recordSelectionPointer(event)
  const hadSelection = captureTextSelection()
  requestAnimationFrame(() => {
    if (hadSelection || captureTextSelection()) {
      showSelectionUi(event)
    } else {
      clearTextSelection()
    }
  })
}

function onTextClick(event) {
  if (event.target?.closest?.('.text-selection-pill, .preview-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  recordSelectionPointer(event)
  // Same guarantee as handleTextSelection: a bare click MUST NOT show
  // the pill. The pointerup pathway already covers drag-select.
  if (captureTextSelection()) return
  clearTextSelection()
}

function onTextKeyup(event) {
  if (event.key === 'Shift' || event.key === 'Control' || event.key === 'Alt' || event.key === 'Meta') {
    return
  }
  handleTextSelection(event)
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

function selectionAnchorForExplain() {
  let local = { left: 8, top: 8 }
  if (selectionMenu.visible) {
    local = { left: selectionMenu.left, top: selectionMenu.top + MENU_H + 8 }
  } else if (selectionAnchor.visible) {
    local = { left: selectionAnchor.left, top: selectionAnchor.top + PILL_H + 8 }
  }
  return localAnchorToViewport(textWrapRef.value, local)
}

function referenceCurrentTextSelection() {
  const sel = previewSelection.value
  if (!sel) return
  try {
    window.dispatchEvent(new CustomEvent('argusai:reference-file', {
      detail: {
        file_path: sel.file_path,
        file_name: sel.file_name,
        start_line: sel.start_line,
        start_column: sel.start_column,
        end_line: sel.end_line,
        end_column: sel.end_column,
        text: sel.text,
      },
    }))
  } catch (_) {}
  clearTextSelection()
  try { window.getSelection?.()?.removeAllRanges() } catch (_) {}
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
  hideSelectionAnchor()
}

function handleContextMenu(event) {
  if (!captureTextSelection()) {
    clearTextSelection()
    return
  }
  event.preventDefault()
  recordSelectionPointer(event)
  wholeFilePillMode.value = false
  showSelectionMenu(event)
}

async function askTextSelection() {
  const ref = buildSelectionReference()
  if (!ref || !sessionId.value) return
  const anchor = selectionAnchorForExplain()
  hideSelectionAnchor()
  await openAsk({
    sessionId: sessionId.value,
    selectionReference: ref,
    page: 'media_preview',
    context: {
      source: 'media_preview',
      file_path: item.value.path || '',
      file_name: item.value.name || '',
    },
    anchor,
  })
}

// When the previewed file changes (different file opened), reset any
// dangling selection from the previous one.
watch(() => item.value.path, () => { clearTextSelection() })

const imageStyle = computed(() => ({
  transform: `translate(${pan.value.x}px, ${pan.value.y}px) scale(${scale.value})`,
}))

function formatBytes(n) {
  const value = Number(n || 0)
  if (!value) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function resetImageTransform() {
  scale.value = 1
  pan.value = { x: 0, y: 0 }
}

function onWheel(e) {
  if (item.value.kind !== 'image' || editingImage.value) return
  e.preventDefault()
  const next = scale.value + (e.deltaY < 0 ? 0.12 : -0.12)
  scale.value = Math.max(0.2, Math.min(5, next))
}

function onPointerDown(e) {
  if (item.value.kind !== 'image' || editingImage.value) return
  dragging.value = true
  dragStart = { x: e.clientX, y: e.clientY, pan: { ...pan.value } }
  e.currentTarget?.setPointerCapture?.(e.pointerId)
}

function onPointerMove(e) {
  if (!dragging.value || !dragStart) return
  pan.value = {
    x: dragStart.pan.x + e.clientX - dragStart.x,
    y: dragStart.pan.y + e.clientY - dragStart.y,
  }
}

function stopDrag() {
  dragging.value = false
  dragStart = null
}

function trapPreviewFocus(e) {
  if (e.key !== 'Tab' || !previewShellRef.value) return
  const focusable = Array.from(previewShellRef.value.querySelectorAll(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), iframe, [tabindex]:not([tabindex="-1"])',
  )).filter(el => el.getClientRects().length > 0)
  if (!focusable.length) {
    e.preventDefault()
    previewShellRef.value.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

function onKeydown(e) {
  if (!state.open) return
  trapPreviewFocus(e)
  if (e.key === 'Escape') {
    if (editingImage.value) cancelEditing()
    else closePreview()
  }
  if (e.key === '0' && item.value.kind === 'image') resetImageTransform()
}

async function imageSourceForExport() {
  const src = String(item.value.content || '')
  if (!/^https?:\/\//i.test(src)) return src
  const result = await api.readMediaUrl?.(src)
  if (!result?.ok) throw new Error(result?.error || t('mediaPreview.remoteImageFailed'))
  state.item = { ...item.value, ...result, url: item.value.url || src }
  return result.content
}

async function composedImageDataUrl() {
  if (item.value.kind !== 'image') throw new Error(t('mediaPreview.notImage'))
  if (String(item.value.content || '').startsWith('data:image/')) return String(item.value.content || '')
  return imageSourceForExport()
}

function annotatedFilename(suffix = 'annotated') {
  const nameBase = String(title.value || 'image').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 80)
  return `${nameBase || 'image'}-${suffix}.png`
}

async function editImage() {
  if (item.value.kind !== 'image' || editingImage.value) return
  try {
    editorSource.value = await imageSourceForExport()
    editingImage.value = true
    resetImageTransform()
  } catch (e) {
    state.error = String(e?.message || e)
  }
}

function updatePreviewWithEditedImage(result) {
  if (!result?.dataUrl) return
  state.item = {
    ...item.value,
    content: result.dataUrl,
    mime: result.mime || 'image/png',
    name: result.name || annotatedFilename('edited'),
    size: result.size || item.value.size || 0,
    encoding: 'base64',
    edit: false,
  }
}

function notifyPreviewApply(result, mode) {
  const onApply = item.value.onApply
  if (typeof onApply === 'function') {
    onApply({ mode, result, context: item.value.context || null })
  }
}

function applyEditedImage(result) {
  if (!result?.dataUrl) return
  updatePreviewWithEditedImage(result)
  notifyPreviewApply(result, 'replace')
  if (isAttachmentEditor.value) {
    closePreview()
    return
  }
  editingImage.value = false
  editorSource.value = ''
}

function duplicateEditedImage(result) {
  if (!result?.dataUrl) return
  notifyPreviewApply(result, 'duplicate')
}

function cancelEditing() {
  if (isAttachmentEditor.value) {
    closePreview()
    return
  }
  editingImage.value = false
  editorSource.value = ''
}

async function sendAnnotatedToAi() {
  if (item.value.kind !== 'image' || sendingAnnotated.value) return
  sendingAnnotated.value = true
  try {
    const dataUrl = await composedImageDataUrl()
    const b64 = dataUrl.split(',', 2)[1] || ''
    window.dispatchEvent(new CustomEvent('argusai:add-assistant-attachment', {
      detail: {
        attachment: {
          type: 'image',
          mime: 'image/png',
          data_base64: b64,
          name: annotatedFilename('annotated'),
          size: Math.round((b64.length * 3) / 4),
        },
        openAssistant: true,
      },
    }))
  } catch (e) {
    state.error = String(e?.message || e)
  } finally {
    sendingAnnotated.value = false
  }
}

function dataUrlToBlob(dataUrl) {
  const [meta, b64] = String(dataUrl || '').split(',', 2)
  const mime = (meta.match(/^data:([^;]+)/) || [])[1] || 'image/png'
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

async function copyImageToClipboard() {
  if (item.value.kind !== 'image' || copyingImage.value) return
  copyingImage.value = true
  try {
    const dataUrl = await composedImageDataUrl()
    const blob = dataUrlToBlob(dataUrl)
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
      throw new Error(t('mediaPreview.clipboardUnsupported'))
    }
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })])
  } catch (e) {
    state.error = String(e?.message || e)
  } finally {
    copyingImage.value = false
  }
}

async function saveImage(mode) {
  if (item.value.kind !== 'image' || savingMedia.value) return
  savingMedia.value = true
  try {
    const dataUrl = await composedImageDataUrl()
    const result = await api.saveMediaData?.(dataUrl, annotatedFilename(mode === 'sandbox' ? 'sandbox' : 'saved'), item.value.sessionId || '', mode)
    if (!result?.ok) {
      if (result?.cancelled) return
      throw new Error(result?.error || t('mediaPreview.saveFailed'))
    }
  } catch (e) {
    state.error = String(e?.message || e)
  } finally {
    savingMedia.value = false
  }
}

async function copyReference() {
  const text = item.value.path || item.value.url || item.value.content || ''
  if (text) await navigator.clipboard?.writeText(text)
}

// Tell the assistant input "the user is pointing at THIS image". Unlike
// `sendAnnotatedToAi`, this does NOT re-upload the bytes — it only writes a
// `selectionReference` chip carrying the image's name / mime so the next
// prompt arrives with a textual hint. The multimodal model is expected to
// already have the image in context from a previous turn.
function referenceCurrentImage() {
  if (item.value.kind !== 'image') return
  const name = item.value.name || (item.value.path || item.value.url || 'image').split(/[\\/]/).pop() || 'image'
  window.dispatchEvent(new CustomEvent('argusai:reference-image', {
    detail: {
      name,
      mime: item.value.mime || '',
      sessionId: item.value.sessionId || '',
      source: item.value.path || item.value.url || '',
    },
  }))
  closePreview()
}

const canReferenceWholeFile = computed(() =>
  Boolean(item.value.path) && ['text', 'pdf', 'video', 'audio'].includes(item.value.kind)
)

function referenceWholeFile() {
  const path = item.value.path
  if (!path) return
  const name = item.value.name || path.split(/[/\\]/).pop() || path
  try {
    window.dispatchEvent(new CustomEvent('argusai:reference-file', {
      detail: { file_path: path, file_name: name },
    }))
  } catch (_) {}
  clearTextSelection()
}

async function askWholeFile() {
  const path = item.value.path
  if (!path || !sessionId.value) return
  const name = item.value.name || path.split(/[/\\]/).pop() || path
  const ref = normalizeSelectionReference({
    kind: 'file_selection',
    file_path: path,
    file_name: name,
  })
  const anchor = selectionAnchorForExplain()
  hideSelectionAnchor()
  await openAsk({
    sessionId: sessionId.value,
    selectionReference: ref,
    page: 'media_preview',
    context: {
      source: 'media_preview',
      file_path: path,
      file_name: name,
    },
    anchor,
  })
}

function onPillAsk() {
  if (wholeFilePillMode.value) askWholeFile()
  else askTextSelection()
}

function onPillReference() {
  if (wholeFilePillMode.value) referenceWholeFile()
  else referenceCurrentTextSelection()
}

async function onPillCopyReference() {
  await copyReference()
  clearTextSelection()
}

function openExternal() {
  const url = item.value.url || item.value.content || ''
  if (url && /^https?:\/\//i.test(url)) api.openExternalUrl?.(url)
}

function openPath() {
  if (item.value.path) api.openUserPath?.(item.value.path, item.value.sessionId || null)
}

function markMediaLoaded() {
  mediaLoadFailed.value = false
  state.loading = false
  state.error = ''
}

function markMediaFailed(kind = previewKindLabel.value) {
  mediaLoadFailed.value = true
  state.loading = false
  state.error = t('mediaPreview.loadFailedRetry', { kind })
}

async function retryPreview() {
  if (retryingPreview.value) return
  const snapshot = { ...item.value }
  retryingPreview.value = true
  mediaLoadFailed.value = false
  state.error = ''
  try {
    if (snapshot.kind === 'text' && snapshot.path && snapshot.content && snapshot.hasMore) {
      await loadMoreMediaText()
      return
    }
    if (snapshot.path) {
      const preserved = {
        edit: snapshot.edit,
        applyTarget: snapshot.applyTarget,
        context: snapshot.context,
        onApply: snapshot.onApply,
      }
      await openFile(snapshot.path, {
        name: snapshot.name,
        sessionId: snapshot.sessionId || '',
      })
      if (state.item) state.item = { ...state.item, ...preserved }
      return
    }
    if (snapshot.content || snapshot.url) {
      state.loading = true
      mediaRenderKey.value += 1
      await nextTick()
      state.loading = false
      return
    }
    throw new Error(t('mediaPreview.noReloadSource'))
  } catch (e) {
    state.loading = false
    state.error = String(e?.message || e)
  } finally {
    retryingPreview.value = false
  }
}

function dismissPreviewError() {
  state.error = ''
}

function onDocumentPointerDown(event) {
  const wrap = textWrapRef.value
  if (!wrap || item.value.kind !== 'text') return
  if (event.target?.closest?.('.text-selection-pill, .preview-selection-menu, .selection-pill-row, .sb-explain-popover')) return
  if (!wrap.contains(event.target) && !event.target?.closest?.('.sb-explain-popover')) {
    clearTextSelection()
    closeExplain()
    try { window.getSelection?.()?.removeAllRanges() } catch (_) {}
  }
}

watch(() => state.open, (open) => {
  if (open) {
    previewReturnFocus = document.activeElement
    resetImageTransform()
    nextTick(() => previewCloseRef.value?.focus())
  } else {
    editingImage.value = false
    editorSource.value = ''
    closeExplain()
    clearTextSelection()
    const returnTarget = previewReturnFocus
    previewReturnFocus = null
    nextTick(() => {
      if (returnTarget?.isConnected && typeof returnTarget.focus === 'function') returnTarget.focus()
    })
  }
})

watch(() => state.item, (current) => {
  if (!state.open || !current) return
  mediaLoadFailed.value = false
  mediaRenderKey.value += 1
  resetImageTransform()
  editingImage.value = false
  editorSource.value = ''
  if (current.edit && current.kind === 'image') editImage()
})

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  document.addEventListener('pointerdown', onDocumentPointerDown, true)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  document.removeEventListener('pointerdown', onDocumentPointerDown, true)
  closeExplain()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="state.open" class="media-preview-overlay" @click.self="closePreview">
      <section
        ref="previewShellRef"
        class="media-preview-shell"
        role="dialog"
        aria-modal="true"
        aria-labelledby="media-preview-title"
        aria-describedby="media-preview-meta"
        tabindex="-1"
      >
        <header class="media-preview-head">
          <div class="media-preview-title">
            <span class="media-preview-kicker">{{ t('mediaPreview.kicker') }}</span>
            <h2 id="media-preview-title" :title="title">{{ title }}</h2>
            <div id="media-preview-meta" class="media-preview-meta" :aria-label="t('mediaPreview.metaAria')">
              <span v-for="meta in previewMetadata" :key="meta">{{ meta }}</span>
            </div>
            <div v-if="previewSource" class="media-preview-source" :title="previewSource">{{ previewSource }}</div>
          </div>
          <div class="media-preview-actions">
            <template v-if="!editingImage">
              <template v-if="item.kind === 'image' && !state.loading">
                <button type="button" class="ghost tiny" :disabled="editingImage" @click="editImage">{{ t('mediaPreview.editImage') }}</button>
                <button type="button" class="ghost tiny" :title="t('mediaPreview.referenceAssistantTitle')" @click="referenceCurrentImage">{{ t('mediaPreview.referenceAssistant') }}</button>
                <button type="button" class="primary tiny" :disabled="sendingAnnotated" @click="sendAnnotatedToAi">
                  {{ sendingAnnotated ? t('mediaPreview.sending') : t('mediaPreview.sendToAi') }}
                </button>
              </template>
              <template v-if="item.kind === 'image'">
                <button v-if="item.sessionId" type="button" class="ghost tiny" :disabled="savingMedia" @click="saveImage('sandbox')">{{ t('mediaPreview.saveToSandbox') }}</button>
                <button type="button" class="ghost tiny" :disabled="savingMedia" @click="saveImage('dialog')">{{ t('mediaPreview.saveAs') }}</button>
                <button type="button" class="ghost tiny" :disabled="copyingImage" @click="copyImageToClipboard">
                  {{ copyingImage ? t('mediaPreview.copying') : t('mediaPreview.copyImage') }}
                </button>
              </template>
              <button v-if="item.kind !== 'text'" type="button" class="ghost tiny" @click="copyReference">{{ t('mediaPreview.copyReference') }}</button>
              <button v-if="canOpenExternal" type="button" class="ghost tiny" @click="openExternal">{{ t('mediaPreview.openInBrowser') }}</button>
              <button v-if="canOpenPath" type="button" class="ghost tiny" @click="openPath">{{ t('mediaPreview.openInSystem') }}</button>
            </template>
            <button
              ref="previewCloseRef"
              type="button"
              class="media-preview-close"
              :aria-label="t('mediaPreview.closeAria')"
              :title="t('mediaPreview.closeTitle')"
              @click="closePreview"
            >
              {{ t('mediaPreview.close') }}
            </button>
          </div>
        </header>

        <main
          class="media-preview-body"
          :class="{ editing: editingImage }"
          :aria-busy="state.loading ? 'true' : 'false'"
          @wheel="onWheel"
        >
          <div v-if="state.loading" class="media-preview-state" role="status" aria-live="polite">
            <span class="media-preview-spinner" aria-hidden="true"></span>
            <strong>{{ t('mediaPreview.loadingKind', { kind: previewKindLabel }) }}</strong>
            <p>{{ t('mediaPreview.loadingHint') }}</p>
          </div>
          <div v-else-if="state.error" class="media-preview-state error" role="alert">
            <strong>{{ previewErrorTitle }}</strong>
            <p>{{ state.error }}</p>
            <div class="media-preview-state-actions">
              <button v-if="previewCanRetry" type="button" class="primary tiny" :disabled="retryingPreview" @click="retryPreview">
                {{ retryingPreview ? t('mediaPreview.retrying') : t('mediaPreview.retryLoad') }}
              </button>
              <button v-if="previewHasContent && !previewCanRetry" type="button" class="ghost tiny" @click="dismissPreviewError">{{ t('mediaPreview.backToPreview') }}</button>
              <button type="button" class="ghost tiny" @click="closePreview">{{ t('mediaPreview.closePreview') }}</button>
            </div>
          </div>
          <div v-else-if="previewIsEmpty" class="media-preview-state empty">
            <strong>{{ t('mediaPreview.emptyTitle', { kind: previewKindLabel }) }}</strong>
            <p>{{ t('mediaPreview.emptyHint') }}</p>
            <button v-if="item.path || item.url" type="button" class="ghost tiny" @click="retryPreview">{{ t('mediaPreview.reread') }}</button>
          </div>
          <ImageCanvasEditor
            v-else-if="item.kind === 'image' && editingImage"
            :source="editorSource"
            :name="title"
            :apply-label="isAttachmentEditor ? t('mediaPreview.applyAttachment') : t('mediaPreview.applyPreview')"
            :allow-duplicate="isAttachmentEditor"
            :duplicate-label="t('mediaPreview.duplicate')"
            :cancel-label="t('mediaPreview.exitEdit')"
            @apply="applyEditedImage"
            @duplicate="duplicateEditedImage"
            @cancel="cancelEditing"
            @error="state.error = String($event)"
          />
          <div
            v-else-if="item.kind === 'image'"
            class="media-preview-image-stage"
            :class="{ dragging }"
            @pointerdown="onPointerDown"
            @pointermove="onPointerMove"
            @pointerup="stopDrag"
            @pointercancel="stopDrag"
            @dblclick="resetImageTransform"
          >
            <img
              :key="mediaRenderKey"
              :src="item.content"
              :alt="title"
              :style="imageStyle"
              draggable="false"
              @load="markMediaLoaded"
              @error="markMediaFailed(t('mediaPreview.kindImage'))"
            />
          </div>
          <iframe
            v-else-if="item.kind === 'pdf'"
            :key="mediaRenderKey"
            class="media-preview-frame"
            :src="item.content"
            :title="t('mediaPreview.pdfTitle', { title })"
            @load="markMediaLoaded"
            @error="markMediaFailed('PDF')"
          ></iframe>
          <video
            v-else-if="item.kind === 'video'"
            :key="mediaRenderKey"
            class="media-preview-video"
            :src="item.content"
            :aria-label="t('mediaPreview.videoAria', { title })"
            controls
            playsinline
            @loadeddata="markMediaLoaded"
            @error="markMediaFailed(t('mediaPreview.kindVideo'))"
          ></video>
          <audio
            v-else-if="item.kind === 'audio'"
            :key="mediaRenderKey"
            class="media-preview-audio"
            :src="item.content"
            :aria-label="t('mediaPreview.audioAria', { title })"
            controls
            @loadeddata="markMediaLoaded"
            @error="markMediaFailed(t('mediaPreview.kindAudio'))"
          ></audio>
          <div
            v-else-if="item.kind === 'text'"
            ref="textWrapRef"
            class="media-preview-textwrap"
            @pointerdown="onSelectionPointerDown"
            @pointermove="onSelectionPointerMove"
            @pointerup="onTextPointerUp"
            @pointercancel="onTextPointerUp"
            @click="onTextClick"
          >
            <pre
              ref="textContentRef"
              class="media-preview-text"
              :class="'language-' + textLanguage"
              @keyup="onTextKeyup"
              @contextmenu="handleContextMenu"
            ><code v-if="skipSyntaxHighlight">{{ formattedTextContent }}</code><code v-else v-html="highlightedTextContent"></code></pre>
            <div
              v-if="textHasMore && item.kind === 'text'"
              class="media-preview-loadmore"
            >
              <button
                type="button"
                class="ghost tiny"
                :disabled="state.loadingMore"
                @click="loadMoreMediaText"
              >
                {{ state.loadingMore ? t('common.loading') : loadMoreLabel }}
              </button>
              <span class="media-preview-loadmore-hint muted">{{ t('mediaPreview.loadMoreHint') }}</span>
            </div>
            <div
              v-if="selectionAnchor.visible"
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
                :title="wholeFilePillMode
                  ? (sessionId ? t('mediaPreview.askFile') : t('mediaPreview.needSession'))
                  : (sessionId ? t('mediaPreview.askSelection') : t('mediaPreview.needSession'))"
                @click.prevent.stop="onPillAsk"
              >
                {{ t('mediaPreview.ask') }}
              </button>
              <button
                type="button"
                class="text-selection-pill"
                :title="wholeFilePillMode ? t('mediaPreview.referenceFile') : t('mediaPreview.referenceSnippet')"
                @click.prevent.stop="onPillReference"
              >
                ⤴ {{ t('mediaPreview.reference') }}
              </button>
              <button
                v-if="wholeFilePillMode"
                type="button"
                class="text-selection-pill subtle"
                :title="t('mediaPreview.copyPath')"
                @click.prevent.stop="onPillCopyReference"
              >
                {{ t('mediaPreview.copyReference') }}
              </button>
            </div>
            <div
              v-if="selectionMenu.visible"
              class="preview-selection-menu"
              :style="selectionMenuStyle"
              role="menu"
              @pointerdown.prevent.stop
              @pointerup.prevent.stop
              @click.stop
            >
              <button type="button" role="menuitem" @click.prevent.stop="copyTextSelection">{{ t('common.copy') }}</button>
              <button
                type="button"
                role="menuitem"
                :disabled="!sessionId"
                @click.prevent.stop="askTextSelection"
              >
                {{ t('mediaPreview.ask') }}
              </button>
              <button type="button" role="menuitem" @click.prevent.stop="referenceCurrentTextSelection">{{ t('mediaPreview.referenceSnippet') }}</button>
            </div>
          </div>
          <div v-else class="media-preview-state empty">
            <strong>{{ t('mediaPreview.unsupportedTitle') }}</strong>
            <p>{{ t('mediaPreview.unsupportedHint') }}</p>
          </div>
        </main>
        <footer v-if="!editingImage && previewHint" class="media-preview-status">
          <span>{{ previewHint }}</span>
          <button v-if="item.kind === 'image'" type="button" class="ghost tiny" @click="resetImageTransform">{{ t('mediaPreview.resetView') }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.media-preview-overlay {
  position: fixed;
  /* Leave space for the custom titlebar on frameless windows so the user
     never loses access to minimize / maximize / close. The variable falls
     back to 0 when the host is a normal browser tab. */
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  /* Layer stack: sandbox 40 < preview 44 < assistant FAB 45 < assistant panel 50 */
  z-index: 44;
  display: flex;
  align-items: stretch;
  justify-content: stretch;
  padding: 20px;
  background: rgba(8, 10, 14, 0.64);
  backdrop-filter: blur(8px);
}
.media-preview-shell {
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--fg);
  background: var(--panel);
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 14px;
  box-shadow: var(--shadow-md);
}
.media-preview-head {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 94%, var(--panel-2) 6%);
}
.media-preview-title {
  flex: 1 1 260px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.media-preview-kicker {
  color: var(--muted-2, var(--muted));
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
}
.media-preview-title h2 {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg-strong);
  font-size: 15px;
  font-weight: 600;
  line-height: 1.25;
}
.media-preview-meta {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
}
.media-preview-meta span {
  position: relative;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
.media-preview-meta span + span::before {
  position: absolute;
  left: -6px;
  content: "·";
  color: var(--muted-2, var(--muted));
}
.media-preview-source {
  max-width: min(68ch, 68vw);
  overflow: hidden;
  color: var(--muted-2, var(--muted));
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.media-preview-actions {
  flex: 0 1 auto;
  min-width: 0;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
}
.media-preview-close {
  min-width: 52px;
  height: 30px;
  padding: 0 10px;
  border-color: var(--border-strong, var(--border));
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--fg);
  font-size: 12px;
}
.tiny {
  padding: 5px 9px;
  font-size: 12px;
}
.tiny.active {
  border-color: var(--accent, #4e8cff);
  color: var(--accent, #4e8cff);
}
.media-preview-body {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background:
    linear-gradient(45deg, rgba(127, 127, 127, 0.06) 25%, transparent 25%),
    linear-gradient(-45deg, rgba(127, 127, 127, 0.06) 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, rgba(127, 127, 127, 0.06) 75%),
    linear-gradient(-45deg, transparent 75%, rgba(127, 127, 127, 0.06) 75%);
  background-size: 22px 22px;
  background-position: 0 0, 0 11px, 11px -11px, -11px 0;
}
.media-preview-status {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 7px 12px;
  border-top: 1px solid var(--border);
  background: var(--panel);
  color: var(--muted);
  font-size: 11px;
}
.media-preview-status span {
  min-width: 0;
  overflow-wrap: anywhere;
}
.media-preview-status button {
  flex: 0 0 auto;
  margin-left: auto;
}
.media-preview-body.editing {
  background: none;
  align-items: stretch;
  justify-content: stretch;
  padding: 0;
}
.media-preview-image-stage {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
}
.media-preview-image-stage.dragging {
  cursor: grabbing;
}
.media-preview-image-stage img {
  max-width: 96%;
  max-height: 96%;
  object-fit: contain;
  user-select: none;
  transition: transform 60ms linear;
  transform-origin: center center;
}
.media-preview-frame,
.media-preview-video {
  width: 100%;
  height: 100%;
  border: 0;
  background: #000;
}
.media-preview-audio {
  width: min(720px, calc(100% - 32px));
}
.media-preview-textwrap {
  position: absolute;
  inset: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.media-preview-text {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  margin: 0;
  padding: 18px;
  overflow: auto;
  color: var(--fg);
  background: var(--bg-sunken);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 4;
  /* Force selectable text. The MediaPreview overlay's ancestor tree
     (n-config-provider / dialog-provider) sometimes injects
     ``user-select: none`` on its root, which makes the highlighted
     spans inside <code> unselectable on click+drag. Forcing the value
     here and on every descendant (via :deep()) restores the expected
     "click+drag to select" behaviour. */
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.media-preview-text code {
  display: block;
  font: inherit;
  color: inherit;
  min-width: 100%;
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.media-preview-text :deep(*) {
  user-select: text;
  -webkit-user-select: text;
}
.media-preview-text ::selection,
.media-preview-text :deep(*)::selection {
  background: color-mix(in srgb, var(--accent) 38%, transparent);
  color: var(--fg-strong, var(--fg));
}
/* Syntax tokens — palette matches the AssistantCard's in-card preview. */
.media-preview-text :deep(.syntax-comment) {
  color: var(--syntax-comment, color-mix(in srgb, var(--muted) 82%, transparent));
  font-style: italic;
}
.media-preview-text :deep(.syntax-string) { color: var(--syntax-string, #7dd3fc); }
.media-preview-text :deep(.syntax-keyword) {
  color: var(--syntax-keyword, #c084fc);
  font-weight: 650;
}
.media-preview-text :deep(.syntax-number) { color: var(--syntax-number, #fbbf24); }
.media-preview-text :deep(.syntax-function) { color: var(--syntax-function, var(--accent)); }
.media-preview-text :deep(.syntax-class) {
  color: var(--syntax-class, var(--ok));
  font-weight: 650;
}
.media-preview-text :deep(.syntax-builtin) { color: var(--syntax-builtin, var(--accent)); }

.media-preview-loadmore {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 88%, transparent);
}
.media-preview-loadmore-hint {
  font-size: 10px;
}

.selection-pill-row {
  position: absolute;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border-radius: 999px;
  background: transparent;
  border: 0;
  box-shadow: none;
}
.preview-selection-menu {
  position: absolute;
  z-index: 21;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 128px;
  padding: 4px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--card-bg, var(--panel)) 96%, transparent);
  box-shadow: var(--shadow-md);
}
.preview-selection-menu button {
  text-align: left;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font-size: 12px;
  cursor: pointer;
}
.preview-selection-menu button:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 12%, var(--panel));
}
.preview-selection-menu button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.media-preview-textwrap :deep(.sb-explain-popover) {
  z-index: 22;
}
/* Floating selection pills anchored near the pointer. */
.text-selection-pill {
  position: relative;
  z-index: 20;
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
.text-selection-pill:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.text-selection-pill.subtle {
  border-color: var(--border);
  background: color-mix(in srgb, var(--bg-sunken) 82%, var(--accent) 18%);
  color: var(--fg);
  font-weight: 500;
  box-shadow: var(--shadow-sm);
}
.text-selection-pill.subtle:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 16%, var(--panel));
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--fg-strong, var(--fg));
}

.media-preview-foot {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 94%, #000 6%);
}
.preview-foot-pill {
  padding: 6px 16px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  background: color-mix(in srgb, var(--accent-2, var(--accent)) 92%, #000 8%);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.24);
  white-space: nowrap;
}
.preview-foot-pill.muted {
  border-color: var(--border);
  background: color-mix(in srgb, var(--panel-2, var(--panel)) 88%, transparent);
  color: var(--fg);
  font-weight: 500;
  box-shadow: none;
}
.preview-foot-pill:hover:not(:disabled) {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.preview-foot-pill.muted:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 14%, var(--panel));
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--fg-strong, var(--fg));
}
.preview-foot-pill:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.media-preview-state {
  width: min(440px, calc(100% - 32px));
  display: grid;
  justify-items: center;
  gap: 8px;
  padding: 28px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel) 94%, transparent);
  color: var(--muted);
  text-align: center;
}
.media-preview-state strong {
  color: var(--fg-strong);
  font-size: 14px;
  font-weight: 600;
}
.media-preview-state p {
  max-width: 48ch;
  margin: 0;
  overflow-wrap: anywhere;
}
.media-preview-state.error {
  border-color: color-mix(in srgb, var(--err) 36%, var(--border));
  background: color-mix(in srgb, var(--err) 7%, var(--panel));
}
.media-preview-state.error strong {
  color: var(--err);
}
.media-preview-state.empty {
  border-style: dashed;
}
.media-preview-state-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 4px;
}
.media-preview-spinner {
  width: 22px;
  height: 22px;
  border: 2px solid color-mix(in srgb, var(--accent) 22%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: media-preview-spin 700ms linear infinite;
}
@keyframes media-preview-spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 760px) {
  .media-preview-overlay {
    padding: 8px;
  }
  .media-preview-shell {
    border-radius: 10px;
  }
  .media-preview-head {
    max-height: 46%;
    flex-direction: column;
    gap: 10px;
    padding: 10px;
    overflow-y: auto;
  }
  .media-preview-title,
  .media-preview-actions {
    width: 100%;
  }
  .media-preview-title {
    flex-basis: auto;
  }
  .media-preview-title h2 {
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .media-preview-source {
    max-width: 100%;
  }
  .media-preview-actions {
    justify-content: flex-start;
  }
  .media-preview-close {
    margin-left: auto;
  }
  .media-preview-body {
    min-height: 220px;
  }
  .media-preview-state {
    width: calc(100% - 20px);
    padding: 20px 16px;
  }
  .media-preview-status {
    align-items: flex-start;
  }
}

@media (max-width: 480px) {
  .media-preview-head {
    max-height: 54%;
  }
  .media-preview-actions .tiny {
    min-height: 34px;
  }
  .media-preview-status {
    display: none;
  }
  .media-preview-text {
    padding: 12px;
    font-size: 11px;
  }
  .media-preview-loadmore-hint {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .media-preview-spinner {
    animation-duration: 1.8s;
  }
}

@media (prefers-reduced-transparency: reduce) {
  .media-preview-overlay {
    background: rgba(8, 10, 14, 0.82);
    backdrop-filter: none;
  }
}

:global(:root[data-theme="dark"]) .media-preview-shell {
  box-shadow: none;
}
</style>
