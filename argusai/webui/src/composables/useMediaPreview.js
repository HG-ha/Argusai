import { reactive } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'

/** Match sandbox inline preview chunk size (see useSandboxBrowser). */
export const MEDIA_PREVIEW_CHUNK_CHARS = 65_536

const state = reactive({
  open: false,
  loading: false,
  loadingMore: false,
  error: '',
  item: null,
})

function inferKindFromUrl(url, fallback = 'image') {
  const clean = String(url || '').split(/[?#]/, 1)[0].toLowerCase()
  if (/\.(png|jpe?g|gif|webp|svg|bmp)$/.test(clean)) return 'image'
  if (/\.pdf$/.test(clean)) return 'pdf'
  if (/\.(mp4|webm|mov|m4v|avi|mkv)$/.test(clean)) return 'video'
  if (/\.(mp3|wav|ogg|m4a|flac|aac)$/.test(clean)) return 'audio'
  return fallback
}

function openPayload(payload = {}) {
  state.open = true
  state.loading = false
  state.loadingMore = false
  state.error = ''
  state.item = {
    kind: payload.kind || inferKindFromUrl(payload.content || payload.url || payload.path || '', 'image'),
    content: payload.content || payload.url || '',
    name: payload.name || payload.title || payload.path || payload.url || t('mediaPreview.title'),
    path: payload.path || '',
    sessionId: payload.sessionId || '',
    url: payload.url || '',
    mime: payload.mime || '',
    size: Number(payload.size || 0),
    truncated: Boolean(payload.truncated),
    hasMore: Boolean(payload.more ?? payload.truncated),
    offsetChars: Number(payload.offset_chars ?? 0),
    endOffset: Number(payload.end_offset ?? (payload.content || '').length),
    totalChars: Number(payload.total_chars ?? payload.size ?? 0),
    encoding: payload.encoding || '',
    edit: Boolean(payload.edit),
    applyTarget: payload.applyTarget || '',
    context: payload.context || null,
    onApply: typeof payload.onApply === 'function' ? payload.onApply : null,
  }
}

function openUrl(url, opts = {}) {
  const u = String(url || '').trim()
  if (!u) return
  openPayload({
    kind: opts.kind || inferKindFromUrl(u, 'image'),
    content: u,
    url: u,
    name: opts.name || opts.title || u,
    mime: opts.mime || '',
    sessionId: opts.sessionId || '',
  })
}

async function openFile(path, opts = {}) {
  const p = String(path || '').trim()
  if (!p) return
  state.open = true
  state.loading = true
  state.loadingMore = false
  state.error = ''
  state.item = {
    kind: 'loading',
    content: '',
    name: opts.name || p,
    path: p,
    sessionId: opts.sessionId || '',
    url: '',
    mime: '',
    size: 0,
    truncated: false,
    hasMore: false,
    offsetChars: 0,
    endOffset: 0,
    totalChars: 0,
    encoding: '',
  }
  try {
    const result = await api.readMediaFile(p, opts.sessionId || null, 0, MEDIA_PREVIEW_CHUNK_CHARS)
    if (!result?.ok) throw new Error(result?.error || t('mediaPreview.readFailed'))
    openPayload({ ...result, sessionId: opts.sessionId || '' })
  } catch (e) {
    state.loading = false
    state.error = String(e?.message || e)
  }
}

async function loadMoreMediaText() {
  const item = state.item
  if (!item || item.kind !== 'text' || !item.path || !item.hasMore || state.loadingMore) return
  state.loadingMore = true
  state.error = ''
  const offset = Number(item.endOffset || item.content?.length || 0)
  try {
    const result = await api.readMediaFile(
      item.path,
      item.sessionId || null,
      offset,
      MEDIA_PREVIEW_CHUNK_CHARS,
    )
    if (!result?.ok) throw new Error(result?.error || t('mediaPreview.chunkFailed'))
    const chunk = result.content || ''
    state.item = {
      ...item,
      content: `${item.content || ''}${chunk}`,
      truncated: Boolean(result.truncated),
      hasMore: Boolean(result.more ?? result.truncated),
      offsetChars: Number(item.offsetChars ?? 0),
      endOffset: Number(result.end_offset ?? offset + chunk.length),
      totalChars: Number(result.total_chars ?? item.totalChars ?? item.size ?? 0),
      size: Number(result.size ?? item.size ?? 0),
    }
  } catch (e) {
    state.error = String(e?.message || e)
  } finally {
    state.loadingMore = false
  }
}

function closePreview() {
  state.open = false
  state.loading = false
  state.loadingMore = false
  state.error = ''
  state.item = null
}

// AI UI Protocol v2: open a preview directly from an `AttachmentRef`. This is
// the recommended entry point for new code; older call sites (raw URL / file
// path) keep using `openUrl` / `openFile` / `openPayload` unchanged.
function openAttachment(att, opts = {}) {
  if (!att || typeof att !== 'object') return
  const url = att.url || att.media_url || att.data_url || ''
  const content = att.data_url || att.url || att.media_url || att.content || ''
  openPayload({
    kind: att.kind || opts.kind,
    name: att.name || opts.name,
    mime: att.mime || opts.mime,
    size: att.size || opts.size,
    url,
    content,
    path: att.path || opts.path || '',
    sessionId: att.session_id || opts.sessionId || '',
    edit: Boolean(opts.edit),
    applyTarget: opts.applyTarget || '',
    context: opts.context || null,
    onApply: typeof opts.onApply === 'function' ? opts.onApply : null,
  })
}

export function useMediaPreview() {
  return {
    state,
    openPayload,
    openUrl,
    openFile,
    openAttachment,
    loadMoreMediaText,
    closePreview,
  }
}
