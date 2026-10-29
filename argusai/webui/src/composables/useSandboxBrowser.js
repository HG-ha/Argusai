// Global state for the assistant sandbox browser (right-side overlay panel).
//
// Why this lives outside of the AssistantPanel component:
// - It needs to react to events from other components — most importantly
//   `argusai:open-sandbox-panel`, dispatched by `file_resource_view` cards
//   when the user clicks "在沙盒中浏览".
// - The panel renders as a fixed overlay rooted at the app shell, so the
//   visibility flag has to be reachable from `App.vue` without prop drilling
//   through the assistant tree.
//
// The composable keeps a per-directory cache plus a live-refresh loop while
// the workspace panel is open (poll + ``argusai:sandbox-files-changed`` events).

import { reactive } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'

/** Per-request text window size (chars). Large files load in chunks like VS Code. */
export const SANDBOX_PREVIEW_CHUNK_CHARS = 65536

const emptyPreview = () => ({
  loading: false,
  loadingMore: false,
  error: '',
  path: '',
  name: '',
  kind: '',       // '' | 'text' | 'image'
  content: '',
  mime: '',
  size: 0,
  truncated: false,
  hasMore: false,
  offsetChars: 0,
  endOffset: 0,
  totalChars: 0,
})

const state = reactive({
  open: false,
  sessionId: '',
  // Map<dirRelativePath, { loading, error, items }>
  // The root directory is keyed by the empty string ''. Caching means
  // re-opening the panel is instant; `refresh()` invalidates and re-fetches.
  directories: {},
  expanded: new Set([''], ),       // dir paths whose children are currently shown
  selectedPath: '',                 // file path the user has focused (used by chip)
  selectedPaths: new Set(),          // multi-select for batch operations
  flashPath: '',                    // pulsed for ~1s after a "jump to" trigger
  dropTargetPath: '',               // directory currently highlighted as DnD drop target
  allDirectoriesLoaded: false,      // flat/search mode has walked the full directory tree
  // Inline preview pane (right column of the sandbox workspace).
  preview: emptyPreview(),
})

let flashTimer = null
let livePollTimer = null
let livePollInFlight = false
let liveRefreshDebounce = null
let liveRefreshActive = false

/** Poll interval while the workspace panel is open. */
const LIVE_REFRESH_MS = 2500

/** Tools whose completion may change files visible in the tree. */
export const SANDBOX_MUTATING_TOOLS = new Set([
  'sandbox_write_file',
  'sandbox_delete_file',
  'sandbox_reset_files',
  'run_python_script',
  'run_shell_script',
  'run_python_snippet',
  'run_shell_snippet',
  'export_workspace_bundle',
])

export const SANDBOX_FILES_CHANGED_EVENT = 'argusai:sandbox-files-changed'

function dispatchSandboxFilesChanged(sessionId, source = '') {
  const sid = String(sessionId || '').trim()
  if (!sid) return
  try {
    window.dispatchEvent(new CustomEvent(SANDBOX_FILES_CHANGED_EVENT, {
      detail: { session_id: sid, sessionId: sid, source },
    }))
  } catch (_) {}
}

export function notifySandboxFilesChanged(sessionId, source = '') {
  dispatchSandboxFilesChanged(sessionId, source)
}

function ensureSession(sessionId) {
  const sid = String(sessionId || '').trim()
  if (!sid) return false
  if (state.sessionId !== sid) {
    // New session — old cache is meaningless. Reset everything.
    state.sessionId = sid
    state.directories = {}
    state.expanded = new Set([''])
    state.selectedPath = ''
    state.selectedPaths = new Set()
    state.flashPath = ''
    state.dropTargetPath = ''
    state.allDirectoriesLoaded = false
    Object.assign(state.preview, emptyPreview())
  }
  return true
}

function normalizeSandboxRel(path) {
  return String(path || '').replace(/^\/+|\/+$/g, '')
}

function setDropTargetPath(path) {
  state.dropTargetPath = normalizeSandboxRel(path)
}

function clearDropTargetPath(path = null) {
  if (path == null || state.dropTargetPath === normalizeSandboxRel(path)) {
    state.dropTargetPath = ''
  }
}

function parentSandboxPath(path) {
  const rel = normalizeSandboxRel(path)
  const idx = rel.lastIndexOf('/')
  return idx < 0 ? '' : rel.slice(0, idx)
}

function collapseNestedSandboxPaths(paths) {
  const normalized = [...new Set((paths || []).map(normalizeSandboxRel).filter(Boolean))]
    .sort((a, b) => a.split('/').length - b.split('/').length || a.localeCompare(b))
  const roots = []
  for (const path of normalized) {
    if (!roots.some((root) => path.startsWith(`${root}/`))) roots.push(path)
  }
  return roots
}

/**
 * Move dragged sandbox paths into ``destination`` (a directory relative path).
 * Uses ``moveAssistantSandboxItems`` when available; refreshes the tree on success.
 */
async function moveDroppedItems(destination, paths) {
  const sid = String(state.sessionId || '').trim()
  const dest = normalizeSandboxRel(destination)
  const selected = collapseNestedSandboxPaths(paths)
    .filter((p) => p !== dest && parentSandboxPath(p) !== dest && !dest.startsWith(`${p}/`))
  state.dropTargetPath = ''
  if (!sid || !selected.length) return { ok: false, skipped: true }
  try {
    const r = await api.moveAssistantSandboxItems(sid, selected, dest, 'MOVE_SANDBOX_ITEMS')
    if (!r?.ok) throw new Error(r?.error || t('explorer.moveFailed'))
    state.selectedPaths = new Set(
      [...state.selectedPaths].filter((path) => (
        !selected.some((root) => path === root || path.startsWith(`${root}/`))
      )),
    )
    const focusedPath = normalizeSandboxRel(state.preview.path || state.selectedPath)
    if (focusedPath && selected.some((path) => focusedPath === path || focusedPath.startsWith(`${path}/`))) {
      state.selectedPath = ''
      clearPreview()
    }
    showToast(t('explorer.movedItems', { n: r.count || 0 }), 'ok')
    notifySandboxFilesChanged(sid, 'move_drop')
    await refresh()
    if (dest) await expandDirectory(dest)
    return r
  } catch (e) {
    showToast(t('explorer.moveFailedWith', { msg: e?.message || e }), 'error')
    return { ok: false, error: e?.message || String(e) }
  }
}

function clearPreview() {
  Object.assign(state.preview, emptyPreview())
}

function setSelectedPath(path, selected = true) {
  const rel = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  const next = new Set(state.selectedPaths)
  if (selected) next.add(rel)
  else next.delete(rel)
  state.selectedPaths = next
}

function toggleSelectedPath(path) {
  const rel = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  setSelectedPath(rel, !state.selectedPaths.has(rel))
}

function clearSelection() {
  state.selectedPaths = new Set()
}

function directoryItemsSignature(items) {
  if (!Array.isArray(items)) return ''
  return items.map((item) => [
    item.kind || '',
    item.relative_path || item.path || item.name || '',
    item.size ?? '',
    item.mtime ?? '',
    item.previewable ? '1' : '0',
  ].join(':')).join('|')
}

async function collectDescendantPaths(dirPath) {
  const rel = String(dirPath || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return []
  const paths = [rel]
  await loadDirectory(rel, { force: false })
  const entry = state.directories[rel]
  const items = Array.isArray(entry?.items) ? entry.items : []
  for (const child of items) {
    const childPath = String(child.relative_path || child.path || child.name || '').replace(/^\/+|\/+$/g, '')
    if (!childPath) continue
    if (child.kind === 'dir') {
      const nested = await collectDescendantPaths(childPath)
      paths.push(...nested)
    } else {
      paths.push(childPath)
    }
  }
  return paths
}

async function setSelectedPathTree(path, selected = true, kind = '') {
  const rel = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  const next = new Set(state.selectedPaths)
  let targets = [rel]
  if (kind === 'dir') {
    if (selected) {
      const expanded = new Set(state.expanded)
      expanded.add(rel)
      state.expanded = expanded
    }
    targets = await collectDescendantPaths(rel)
  }
  for (const p of targets) {
    if (selected) next.add(p)
    else next.delete(p)
  }
  state.selectedPaths = next
}

async function previewFile(sessionId, path, { append = false } = {}) {
  const sid = String(sessionId || state.sessionId || '').trim()
  const rel = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!sid || !rel) return
  if (!ensureSession(sid)) return
  state.selectedPath = rel
  const p = state.preview
  const openingNew = !append || p.path !== rel
  if (openingNew) {
    Object.assign(p, emptyPreview())
    p.path = rel
    p.name = rel.split('/').pop() || rel
  }
  p.loading = !append
  p.loadingMore = append
  p.error = ''
  const ext = rel.split('.').pop()?.toLowerCase() || ''
  const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'].includes(ext)
  const offset = append ? Number(p.endOffset || p.content.length || 0) : 0
  try {
    const r = await api.readAssistantSandboxFile(
      sid,
      rel,
      SANDBOX_PREVIEW_CHUNK_CHARS,
      isImage,
      offset,
    )
    if (!r?.ok) throw new Error(r?.error || t('explorer.readFailed'))
    if (isImage && r.content) {
      const raw = String(r.content)
      // Backend read_file(as_base64=True) already returns a full data: URL.
      if (raw.startsWith('data:') || r.is_base64) {
        p.kind = 'image'
        p.content = raw
        p.mime = r.mime_type || r.mime || ''
      } else {
        const mime = ext === 'svg' ? 'image/svg+xml' : `image/${ext === 'jpg' ? 'jpeg' : ext}`
        p.kind = 'image'
        p.content = `data:${mime};base64,${raw}`
        p.mime = mime
      }
      p.hasMore = false
      p.truncated = false
    } else {
      p.kind = 'text'
      const chunk = r.content || ''
      p.content = append ? `${p.content || ''}${chunk}` : chunk
      p.mime = r.mime || 'text/plain'
      p.offsetChars = Number(r.offset_chars ?? (append ? offset : 0))
      p.endOffset = Number(r.end_offset ?? (p.offsetChars + chunk.length))
      p.totalChars = Number(r.total_chars ?? r.size ?? 0)
      p.hasMore = Boolean(r.more ?? r.truncated)
      p.truncated = p.hasMore
    }
    p.size = r.size || 0
    p.error = ''
  } catch (e) {
    if (!append) {
      p.kind = ''
      p.content = ''
    }
    p.error = e?.message || String(e)
  } finally {
    p.loading = false
    p.loadingMore = false
  }
}

async function loadMorePreview() {
  const sid = state.sessionId
  const rel = state.preview.path
  if (!sid || !rel || !state.preview.hasMore || state.preview.loadingMore) return
  await previewFile(sid, rel, { append: true })
}

async function loadDirectory(path = '', { force = false, quiet = false } = {}) {
  if (!state.sessionId) return null
  const key = String(path || '').replace(/^\/+|\/+$/g, '')
  const cached = state.directories[key]
  if (cached && !force && !cached.loading) return cached
  const entry = cached || reactive({
    loading: true,
    error: '',
    items: [],
    _signature: '',
  })
  if (!cached) state.directories = { ...state.directories, [key]: entry }
  if (!quiet) entry.loading = true
  try {
    const r = await api.listAssistantSandboxDirectory(state.sessionId, key)
    if (!r?.ok) throw new Error(r?.error || t('explorer.dirReadFailed'))
    const nextItems = Array.isArray(r.items) ? r.items : []
    const previousSignature = entry._signature || directoryItemsSignature(entry.items)
    const nextSignature = directoryItemsSignature(nextItems)
    if (!cached || previousSignature !== nextSignature) {
      entry.items = nextItems
      entry._signature = nextSignature
    }
    entry.error = ''
  } catch (e) {
    if (!quiet || !cached) {
      entry.error = e?.message || String(e)
      entry.items = []
      entry._signature = ''
    }
  } finally {
    if (!quiet || !cached) entry.loading = false
  }
  return entry
}

/**
 * Populate every directory cache entry for flat view and full-path search.
 * It deliberately reuses the existing directory-list API and bounded cache.
 */
async function loadAllDirectories(path = '', { force = false, quiet = false } = {}) {
  const visited = new Set()
  const queue = [normalizeSandboxRel(path)]
  while (queue.length) {
    const key = queue.shift()
    if (visited.has(key)) continue
    visited.add(key)
    // Directory reads are intentionally sequential: pywebview calls are local,
    // and an unbounded Promise.all can flood the bridge for large sandboxes.
    // eslint-disable-next-line no-await-in-loop
    const entry = await loadDirectory(key, { force, quiet })
    const childDirs = (entry?.items || [])
      .filter((item) => item.kind === 'dir')
      .map((item) => normalizeSandboxRel(item.relative_path || item.path || item.name))
      .filter(Boolean)
    queue.push(...childDirs)
  }
  state.allDirectoriesLoaded = true
  return visited.size
}

function openPanel(sessionId) {
  if (!ensureSession(sessionId)) return
  state.open = true
  loadDirectory('', { force: true })
  startLiveRefresh()
}

function closePanel() {
  state.open = false
  stopLiveRefresh()
  clearPreview()
}

function togglePanel(sessionId) {
  if (state.open) {
    closePanel()
  } else {
    openPanel(sessionId)
  }
}

async function expandDirectory(path) {
  const key = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!state.expanded.has(key)) {
    state.expanded = new Set([...state.expanded, key])
  }
  await loadDirectory(key, { force: true })
}

function collapseDirectory(path) {
  const key = String(path || '').replace(/^\/+|\/+$/g, '')
  if (!state.expanded.has(key)) return
  const next = new Set(state.expanded)
  next.delete(key)
  state.expanded = next
}

function toggleDirectory(path) {
  const key = String(path || '').replace(/^\/+|\/+$/g, '')
  if (state.expanded.has(key)) {
    collapseDirectory(key)
  } else {
    expandDirectory(key)
  }
}

async function refreshExpandedDirectories({ force = true, quiet = false } = {}) {
  if (!state.open || !state.sessionId) return
  const expandedSnapshot = state.allDirectoriesLoaded
    ? [...new Set([...state.expanded, ...Object.keys(state.directories)])]
    : [...state.expanded]
  await Promise.all(expandedSnapshot.map(p => loadDirectory(p, { force, quiet })))
  if (state.allDirectoriesLoaded) {
    await loadAllDirectories('', { force: false, quiet })
  }
}

async function refresh() {
  // Wipe cache except for the open state and expanded set, then reload every
  // currently-expanded directory. This is the user-facing "刷新" button.
  const reloadAll = state.allDirectoriesLoaded
  state.directories = {}
  if (reloadAll) {
    await loadAllDirectories('', { force: true })
  } else {
    await refreshExpandedDirectories({ force: true })
  }
}

function scheduleLivePoll(delay = LIVE_REFRESH_MS) {
  if (!liveRefreshActive) return
  if (livePollTimer) clearTimeout(livePollTimer)
  livePollTimer = setTimeout(runLivePollTick, delay)
}

async function runLivePollTick() {
  livePollTimer = null
  if (!liveRefreshActive || !state.open || !state.sessionId) return
  if (livePollInFlight) {
    scheduleLivePoll()
    return
  }
  livePollInFlight = true
  try {
    await refreshExpandedDirectories({ force: true, quiet: true })
  } catch (_) {
    // Best-effort background sync; manual refresh still available.
  } finally {
    livePollInFlight = false
    if (liveRefreshActive && state.open) scheduleLivePoll()
  }
}

function startLiveRefresh() {
  if (liveRefreshActive) return
  liveRefreshActive = true
  scheduleLivePoll(LIVE_REFRESH_MS)
}

function stopLiveRefresh() {
  liveRefreshActive = false
  if (livePollTimer) {
    clearTimeout(livePollTimer)
    livePollTimer = null
  }
  if (liveRefreshDebounce) {
    clearTimeout(liveRefreshDebounce)
    liveRefreshDebounce = null
  }
}

function requestLiveRefresh() {
  if (!state.open || !state.sessionId) return
  if (liveRefreshDebounce) clearTimeout(liveRefreshDebounce)
  liveRefreshDebounce = setTimeout(async () => {
    liveRefreshDebounce = null
    if (!state.open || !state.sessionId) return
    try {
      await refreshExpandedDirectories({ force: true, quiet: true })
    } catch (_) {}
  }, 150)
}

function onSandboxFilesChangedEvent(event) {
  const detail = event?.detail || {}
  const sid = String(detail.session_id || detail.sessionId || '').trim()
  if (sid && sid !== state.sessionId) return
  requestLiveRefresh()
}

if (typeof window !== 'undefined') {
  window.addEventListener(SANDBOX_FILES_CHANGED_EVENT, onSandboxFilesChangedEvent)
}

/**
 * Jump the panel to a specific file. Opens the panel if it's closed,
 * expands every ancestor directory, scrolls to the target row, and
 * triggers a brief highlight pulse.
 *
 * Expected payload: `{ sessionId, file_path | relative_path }`.
 */
/**
 * True when the root sandbox directory for the current ``state.sessionId`` has
 * been loaded and contains no files or folders (excluding load errors).
 */
async function isCurrentWorkspaceEmpty() {
  if (!state.sessionId) return true
  const entry = await loadDirectory('', { force: false })
  if (!entry || entry.loading) return false
  if (entry.error) return false
  return !Array.isArray(entry.items) || entry.items.length === 0
}

/**
 * When the workspace panel is open on an empty sandbox, follow the user's
 * active conversation instead of leaving a stale empty tree visible.
 */
async function followSessionIfWorkspaceEmpty(nextSessionId) {
  const next = String(nextSessionId || '').trim()
  if (!state.open || !next) return false
  const current = String(state.sessionId || '').trim()
  if (!current || current === next) return false
  if (!(await isCurrentWorkspaceEmpty())) return false
  openPanel(next)
  return true
}

async function jumpToFile(sessionId, filePath) {
  if (!ensureSession(sessionId)) return
  state.open = true
  const rel = String(filePath || '').replace(/^\/+|\/+$/g, '')
  if (!rel) {
    await loadDirectory('', { force: true })
    startLiveRefresh()
    return
  }
  // Walk down ancestors so the target row is actually mounted.
  const parts = rel.split('/').filter(Boolean)
  let prefix = ''
  await loadDirectory('', { force: true })
  startLiveRefresh()
  for (let i = 0; i < parts.length - 1; i += 1) {
    prefix = prefix ? `${prefix}/${parts[i]}` : parts[i]
    if (!state.expanded.has(prefix)) {
      state.expanded = new Set([...state.expanded, prefix])
    }
    // eslint-disable-next-line no-await-in-loop
    await loadDirectory(prefix, { force: true })
  }
  state.selectedPath = rel
  // Pulse it briefly so the user's eye lands on the right row even if it's
  // already on screen. The CSS reads .pulse off the row when this is set.
  state.flashPath = rel
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => {
    if (state.flashPath === rel) state.flashPath = ''
    flashTimer = null
  }, 1600)
  // Auto-load preview for files (directories just expand).
  const last = parts[parts.length - 1] || ''
  if (last && !last.includes('.')) return
  await previewFile(sessionId, rel)
}

export function useSandboxBrowser() {
  return {
    state,
    openPanel,
    closePanel,
    togglePanel,
    loadDirectory,
    expandDirectory,
    collapseDirectory,
    toggleDirectory,
    refresh,
    loadAllDirectories,
    refreshExpandedDirectories,
    requestLiveRefresh,
    jumpToFile,
    previewFile,
    loadMorePreview,
    clearPreview,
    setSelectedPath,
    toggleSelectedPath,
    setSelectedPathTree,
    collectDescendantPaths,
    clearSelection,
    setDropTargetPath,
    clearDropTargetPath,
    moveDroppedItems,
    isCurrentWorkspaceEmpty,
    followSessionIfWorkspaceEmpty,
    notifySandboxFilesChanged,
  }
}
