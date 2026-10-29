// Module-level store for AI assistant tabs (browser-tab-style multi-session).
//
// Why this lives OUTSIDE any component: AssistantPanel.vue uses `v-if="visible"`
// so the panel is destroyed when the user hides it via the FAB. If tab state
// lived inside the panel component, every running session would be torn down
// with it — background tabs would stop polling deltas and their unfinished work
// would silently vanish after the 60 s server-side GC of _assistant_streams.
//
// By holding `tabs`, `activeTabId`, and each tab's poller in this module the
// state survives panel unmount/remount, the App-level FAB can read aggregate
// run state (blue breathing dot), and we get a single source of truth that
// the panel only renders.

import { computed, reactive, ref } from 'vue'
import { api } from '../api.js'

const STORAGE_KEY = 'argusai.assistant.tabs.v1'
const MAX_TABS = 8
const POLL_INTERVAL_MS = 300
const POLL_STALL_HINT_MS = 5000
const SESSION_LIST_REFRESH_MS = 1000

// Reactive state owned by this module.
const tabs = reactive([])
const activeTabId = ref('')
const sessions = ref([])           // mirror of api.listAssistantSessions()
let nextMessageUid = 1
let sessionsRefreshPromise = null
let lastSessionsRefreshAt = 0

// Aggregate signal for the FAB (any tab currently running?).
const anyTabRunning = computed(() => tabs.some(t => t.sending || t.confirmingAction))
const anyTabUnread = computed(() => tabs.some(t => t.badge.unread > 0))
const totalUnread = computed(() => tabs.reduce((sum, t) => sum + (t.badge.unread || 0), 0))

const activeTab = computed(() => tabs.find(t => t.sessionId === activeTabId.value) || null)

function newSessionId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
}

function newMessageUid() {
  return nextMessageUid++
}

/** Status string surfaced on the tab badge. */
function recomputeBadge(tab) {
  if (tab.lastError) {
    tab.badge.status = 'error'
  } else if (tab.pendingTools.length > 0) {
    tab.badge.status = 'awaiting'
  } else if (tab.sending || tab.confirmingAction) {
    tab.badge.status = 'running'
  } else if (tab.badge.unread > 0) {
    tab.badge.status = 'unread'
  } else {
    tab.badge.status = ''
  }
}

function makeTab(sessionId, opts = {}) {
  const tab = reactive({
    sessionId: sessionId || newSessionId(),
    name: opts.name || '新会话',
    createdAt: opts.createdAt || Date.now(),
    updatedAt: opts.updatedAt || Date.now(),
    // Message stream
    messages: [],
    renderWindowStart: 0,
    showAllMessages: false,
    // Run state
    sending: false,
    activeStreamMessage: false,
    activeToolName: '',
    runSeq: 0,
    activeRunId: '',
    streamOffset: 0,
    liveJob: null,
    agentThreads: [],
    autoFollowScroll: true,
    // Tool confirmation state
    pendingTool: null,
    pendingTools: [],
    pendingDecisions: {},
    pendingAutoAllowNames: [],
    confirmingAction: false,
    // Per-tab user inputs / queue
    inputDraft: '',
    inputPathRefs: {},
    queuedInputs: [],
    pendingAttachments: [],
    pendingVoice: null,
    previewSelectionContext: null,
    // Per-session toggles (kept in sync with backend's session.json)
    webSearchAllowed: true,
    thinkingEnabled: false,
    thinkingEffort: 'high',
    engagement: null,
    runtime: opts.runtime && typeof opts.runtime === 'object'
      ? { ...opts.runtime }
      : { runtime_id: 'argusai-native', workspace_mode: 'sandbox', project_dir: '', locked: false },
    autoAllowTools: [],
    autoApproveMode: Boolean(opts.autoApproveMode),
    nativeModel: '',
    assistantTools: [],
    toolsLoading: false,
    // Cumulative session cache/token usage (Token 消耗明细 panel), synced from
    // getAssistantSession / getAssistantTokenUsage / turn done — see applySessionTokenUsage.
    tokenUsage: null,
    lastTokenUsage: null,
    cacheHitRate: null,
    promptTokens: 0,
    promptBytes: 0,
    contextWindow: 0,
    // UI presentation
    badge: { unread: 0, status: '' },
    lastError: '',
    // Worker bookkeeping
    _pollerRunning: false,
    _historyLoaded: false,
    _everReceivedDelta: false,
  })
  return tab
}

const MAX_PERSISTED_DRAFT_CHARS = 32_000

function persistTabs() {
  try {
    const payload = {
      tabIds: tabs.map(t => t.sessionId),
      activeTabId: activeTabId.value,
      names: tabs.reduce((acc, t) => {
        if (t.name) acc[t.sessionId] = t.name
        return acc
      }, {}),
      // Input drafts survive panel close and app restart (contenteditable DOM is torn down).
      inputDrafts: tabs.reduce((acc, t) => {
        const draft = String(t.inputDraft || '')
        if (draft) {
          acc[t.sessionId] = draft.length > MAX_PERSISTED_DRAFT_CHARS
            ? draft.slice(0, MAX_PERSISTED_DRAFT_CHARS)
            : draft
        }
        return acc
      }, {}),
      runtimes: tabs.reduce((acc, t) => {
        if (t.runtime && typeof t.runtime === 'object') acc[t.sessionId] = { ...t.runtime }
        return acc
      }, {}),
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch (_) {
    // localStorage can throw on private-mode / quota; non-fatal.
  }
}

function loadPersisted() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || !Array.isArray(parsed.tabIds)) return null
    return parsed
  } catch (_) {
    return null
  }
}

function findTab(sessionId) {
  if (!sessionId) return null
  return tabs.find(t => t.sessionId === sessionId) || null
}

function setActiveTab(sessionId) {
  if (!sessionId) return
  const tab = findTab(sessionId)
  if (!tab) return
  activeTabId.value = sessionId
  // Clearing unread is the user's signal that they've seen the latest delta.
  if (tab.badge.unread) {
    tab.badge.unread = 0
    recomputeBadge(tab)
  }
  if (tab.lastError) {
    tab.lastError = ''
    recomputeBadge(tab)
  }
  persistTabs()
}

/** Add a brand-new tab (or re-focus if sessionId already exists). */
function openTab(opts = {}) {
  const requestedId = opts.sessionId || ''
  if (requestedId) {
    const existing = findTab(requestedId)
    if (existing) {
      setActiveTab(requestedId)
      return existing
    }
  }
  if (tabs.length >= MAX_TABS) {
    return null
  }
  const tab = makeTab(requestedId || newSessionId(), opts)
  tabs.push(tab)
  activeTabId.value = tab.sessionId
  persistTabs()
  return tab
}

/**
 * Replace the currently active tab's session in place. Used by the
 * legacy "switch session" semantic from the history popover where the
 * user picks a saved session and expects it to take over the current
 * panel slot (unless they hold the shortcut for "new tab").
 *
 * The previous session in this tab is NOT cancelled — its session id
 * simply leaves the visible tab list. If it's still running in the
 * backend it will keep running (visible from the history popover).
 */
function replaceActiveTabSession(sessionId) {
  if (!sessionId) return null
  // Already focused, nothing to do.
  if (activeTabId.value === sessionId) return findTab(sessionId)
  // Already open in another tab — just focus it instead of creating a duplicate.
  const existing = findTab(sessionId)
  if (existing) {
    setActiveTab(sessionId)
    return existing
  }
  let target = activeTab.value
  if (!target) {
    return openTab({ sessionId })
  }
  // Swap the session id in place, drop runtime state. History is loaded lazily
  // by the panel via ensureHistoryLoaded().
  target.sessionId = sessionId
  target.name = '加载中...'
  target.messages = []
  target.renderWindowStart = 0
  target.showAllMessages = false
  target.sending = false
  target.activeStreamMessage = false
  target.runSeq = 0
  target.activeRunId = ''
  target.streamOffset = 0
  target.liveJob = null
  target.agentThreads = []
  target.autoFollowScroll = true
  target.pendingTool = null
  target.pendingTools = []
  target.pendingDecisions = {}
  target.pendingAutoAllowNames = []
  target.confirmingAction = false
  target.inputDraft = ''
  target.inputPathRefs = {}
  target.previewSelectionContext = null
  target.queuedInputs = []
  target.pendingAttachments = []
  target.pendingVoice = null
  target.webSearchAllowed = true
  target.thinkingEnabled = false
  target.thinkingEffort = 'high'
  target.runtime = { runtime_id: 'argusai-native', workspace_mode: 'sandbox', project_dir: '', locked: false }
  target.autoAllowTools = []
  target.autoApproveMode = false
  target.nativeModel = ''
  target.assistantTools = []
  target.toolsLoading = false
  target.tokenUsage = null
  target.lastTokenUsage = null
  target.cacheHitRate = null
  target.promptTokens = 0
  target.promptBytes = 0
  target.contextWindow = 0
  target.badge = { unread: 0, status: '' }
  target.lastError = ''
  target._pollerRunning = false
  target._historyLoaded = false
  target._everReceivedDelta = false
  activeTabId.value = sessionId
  persistTabs()
  return target
}

function closeTab(sessionId) {
  const idx = tabs.findIndex(t => t.sessionId === sessionId)
  if (idx < 0) return
  const tab = tabs[idx]
  // Stop polling — the poller loop checks _pollerRunning each tick.
  tab._pollerRunning = false
  tab.activeRunId = ''
  tabs.splice(idx, 1)
  if (activeTabId.value === sessionId) {
    activeTabId.value = tabs[Math.max(0, idx - 1)]?.sessionId || ''
  }
  if (tabs.length === 0) {
    openTab()  // never leave the panel without at least one tab
  }
  persistTabs()
}

function isDefaultTabName(name) {
  return !name || name === '新会话' || name === '加载中...'
}

/** Empty draft tab that never got real chat content — safe to swap for last conversation. */
function isEmptyUnsavedTab(tab) {
  if (!tab) return false
  if (tab.sending || tab.confirmingAction) return false
  if (tab.pendingTool || (tab.pendingTools?.length > 0)) return false
  if (tab.queuedInputs?.length > 0) return false
  if (tab.pendingAttachments?.length > 0) return false
  if (tab.pendingVoice) return false
  if (tab.previewSelectionContext) return false
  if (Array.isArray(tab.messages) && tab.messages.length > 0) return false
  if (!isDefaultTabName(tab.name)) return false
  const draft = String(tab.inputDraft || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  return !draft
}

/**
 * Prefer the most recent saved conversation when Agent opens onto an empty
 * "新会话" (or an orphaned localStorage tab that never hit disk).
 * Returns true when the active tab was switched/replaced.
 */
function preferLastConversation() {
  const list = Array.isArray(sessions.value) ? sessions.value : []
  if (!list.length) {
    if (tabs.length === 0) openTab()
    return false
  }
  const knownIds = new Set(list.map(s => s.id).filter(Boolean))
  const last = list[0]
  if (!last?.id) return false

  const active = activeTab.value
  // Keep a restored/active tab that still exists on disk or already has content.
  if (active && knownIds.has(active.sessionId)) return false
  if (active && !isEmptyUnsavedTab(active)) return false

  const existing = findTab(last.id)
  if (existing) {
    setActiveTab(last.id)
    return true
  }
  if (active && isEmptyUnsavedTab(active)) {
    replaceActiveTabSession(last.id)
    if (last.name && activeTab.value) activeTab.value.name = last.name
    return true
  }
  if (tabs.length === 0 || tabs.every(isEmptyUnsavedTab)) {
    tabs.splice(0, tabs.length)
    openTab({ sessionId: last.id, name: last.name || '加载中...' })
    return true
  }
  return false
}

async function refreshSessions(force = false) {
  const now = Date.now()
  if (!force && now - lastSessionsRefreshAt < SESSION_LIST_REFRESH_MS) {
    return sessions.value
  }
  if (sessionsRefreshPromise) return sessionsRefreshPromise
  sessionsRefreshPromise = (async () => {
    try {
      const data = await api.listAssistantSessions()
      if (Array.isArray(data)) sessions.value = data
      lastSessionsRefreshAt = Date.now()
      // Backfill tab names from disk so freshly-restored tabs get a real name.
      for (const s of sessions.value) {
        const tab = findTab(s.id)
        if (tab && s.name && tab.name !== s.name) {
          tab.name = s.name || tab.name
        }
        if (tab && s.runtime_id) {
          const prev = tab.runtime && typeof tab.runtime === 'object' ? tab.runtime : {}
          tab.runtime = {
            ...prev,
            runtime_id: s.runtime_id,
            workspace_mode: s.workspace_mode || prev.workspace_mode || 'sandbox',
            project_dir: s.project_dir || prev.project_dir || '',
            locked: true,
          }
        }
      }
    } catch (_) {
      /* ignore — list is best-effort */
    } finally {
      sessionsRefreshPromise = null
    }
    return sessions.value
  })()
  return sessionsRefreshPromise
}

/**
 * Initialise from localStorage on first import. Creates at least one tab.
 * Safe to call multiple times — subsequent calls are no-ops.
 */
let restored = false
function ensureRestored() {
  if (restored) return
  restored = true
  const saved = loadPersisted()
  if (saved && saved.tabIds.length > 0) {
    for (const sid of saved.tabIds.slice(0, MAX_TABS)) {
      if (!sid || findTab(sid)) continue
      const name = (saved.names && saved.names[sid]) || '加载中...'
      const tab = makeTab(sid, {
        name,
        runtime: saved.runtimes?.[sid],
      })
      const draft = saved.inputDrafts && saved.inputDrafts[sid]
      if (draft) tab.inputDraft = String(draft)
      tabs.push(tab)
    }
    activeTabId.value = saved.activeTabId && findTab(saved.activeTabId)
      ? saved.activeTabId
      : (tabs[0]?.sessionId || '')
  }
  if (tabs.length === 0) {
    openTab()
  }
  // After the on-disk session list arrives, swap empty/orphan tabs for the
  // most recent real conversation so opening Agent lands on last chat.
  refreshSessions(true)
    .then(() => {
      preferLastConversation()
    })
    .catch(() => {})
}

// Eagerly restore on module import — App boot order doesn't guarantee a
// component will call us before the FAB asks for `anyTabRunning`.
ensureRestored()

export function useAssistantTabs() {
  ensureRestored()
  return {
    // State
    tabs,
    activeTabId,
    activeTab,
    sessions,
    anyTabRunning,
    anyTabUnread,
    totalUnread,
    // Mutations
    openTab,
    closeTab,
    setActiveTab,
    replaceActiveTabSession,
    refreshSessions,
    preferLastConversation,
    persistTabs,
    // Helpers (exported for the panel)
    newMessageUid,
    recomputeBadge,
    // Constants
    MAX_TABS,
    POLL_INTERVAL_MS,
    POLL_STALL_HINT_MS,
  }
}
