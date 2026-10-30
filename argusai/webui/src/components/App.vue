<script setup>
import { computed, nextTick, onErrorCaptured, onMounted, onUnmounted, ref, watch } from 'vue'
import { darkTheme, lightTheme } from 'naive-ui'
import { route, navigate } from '../router.js'
import { toast, showToast, state } from '../state.js'
import { t } from '../i18n.js'
import LocaleSelect from './LocaleSelect.vue'
import { theme } from '../theme.js'
import { api } from '../api.js'
import Sidebar from './Sidebar.vue'
import TaskList from './TaskList.vue'
import TaskDetail from './TaskDetail.vue'
import ProxyPanel from './ProxyPanel.vue'
import BrowserPanel from './BrowserPanel.vue'
import TeamPanel from './TeamPanel.vue'
import McpPanel from './McpPanel.vue'
import AgentRuntimesPanel from './AgentRuntimesPanel.vue'
import ChannelsPanel from './ChannelsPanel.vue'
import SettingsPanel from './SettingsPanel.vue'
import AboutPanel from './AboutPanel.vue'
import AssistantPanel from './AssistantPanel.vue'
import AssistantSessionsPanel from './AssistantSessionsPanel.vue'
import KnowledgePanel from './KnowledgePanel.vue'
import UserTodosPanel from './UserTodosPanel.vue'
import CronPanel from './CronPanel.vue'
import MediaPreview from './MediaPreview.vue'
import SandboxExplainPopover from './SandboxExplainPopover.vue'
import { useSandboxExplain } from '../composables/useSandboxExplain.js'
import ReversePanel from './ReversePanel.vue'
import SandboxPanel from './SandboxPanel.vue'
import WorkbenchTooltipHost from './workbench/WorkbenchTooltipHost.vue'
import { useAssistantTabs } from '../composables/useAssistantTabs.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'
import { useMediaPreview } from '../composables/useMediaPreview.js'
import { useUiContextPublisher } from '../composables/useUiContextPublisher.js'
import { useSidebarCollapse } from '../composables/useSidebarCollapse.js'
import { useUpdates, startUpdateMonitor, stopUpdateMonitor } from '../composables/useUpdates.js'
import argusIcon from '../../../../assets/brand/argusai-icon-Transparency.png'

const ASSISTANT_FAB_POS_KEY = 'argusai.assistant.fab.position.v3'
const ONBOARDING_SEEN_KEY = 'argusai.onboarding.seen.v2'
const ASSISTANT_FAB_DEFAULT_RIGHT = 28
const ASSISTANT_FAB_DEFAULT_BOTTOM = 32

const currentView = computed(() => {
  switch (route.name) {
    case 'task': return TaskDetail
    case 'proxy': return ProxyPanel
    case 'browser': return BrowserPanel
    case 'team': return TeamPanel
    case 'mcp': return McpPanel
    case 'local_agents': return AgentRuntimesPanel
    case 'channels': return ChannelsPanel
    case 'user_todos': return UserTodosPanel
    case 'cron': return CronPanel
    case 'knowledge':
    case 'knowledge_entry': return KnowledgePanel
    case 'reverse':
    case 'reverse_job': return ReversePanel
    case 'assistant_sessions': return AssistantSessionsPanel
    case 'settings': return SettingsPanel
    case 'about': return AboutPanel
    default: return TaskList
  }
})

const showAssistant = ref(false)
const showOnboarding = ref(false)
const onboardingStepIndex = ref(0)
const onboardingDialogEl = ref(null)
const onboardingStepHeadingEl = ref(null)
const assistantFabEl = ref(null)
const assistantFabPos = ref({ left: 0, top: 0, ready: false })
const assistantFabDragging = ref(false)
let suppressAssistantFabClick = false
const {
  tabs,
  anyTabRunning,
  anyTabUnread,
  totalUnread,
  setActiveTab,
  activeTab,
  openTab,
  recomputeBadge,
  persistTabs,
  MAX_TABS,
} = useAssistantTabs()
const sandboxBrowser = useSandboxBrowser()
const mediaPreview = useMediaPreview()
const sandboxSessionId = computed(() =>
  sandboxBrowser.state.sessionId || activeTab.value?.sessionId || '',
)
const { state: explainState, closeExplain } = useSandboxExplain()
const { sidebarCollapsed } = useSidebarCollapse()
const { updateState: appUpdate, noticeVisible: updateNoticeVisible, dismissUpdateNotice } = useUpdates()

function onExplainSendToAssistant() {
  const pending = explainState.pending
  const ref = pending?.selectionReference
  if (!ref?.file_path) return
  try {
    window.dispatchEvent(new CustomEvent('argusai:reference-file', {
      detail: {
        file_path: ref.file_path,
        file_name: ref.file_name || '',
        start_line: ref.start_line,
        start_column: ref.start_column,
        end_line: ref.end_line,
        end_column: ref.end_column,
        text: ref.text,
      },
    }))
  } catch (_) {}
  closeExplain()
}
const assistantContext = computed(() => ({
  route: route.name,
  task_id: route.params?.id || '',
  reverse_job_id: route.name === 'reverse_job' ? (route.params?.id || '') : '',
  raw: route.raw || '',
  route_params: { ...(route.params || {}) },
  ui_theme: {
    preferred: theme.preferred,
    effective: theme.effective,
    label: theme.effective === 'dark' ? t('theme.dark') : t('theme.light'),
  },
  assistant_panel_open: showAssistant.value,
  sandbox_panel_open: sandboxBrowser.state.open,
}))

useUiContextPublisher(() => ({
  assistant_panel_open: showAssistant.value,
  sandbox_panel_open: sandboxBrowser.state.open,
}))

const showCustomTitlebar = computed(() => state.customTitlebar)
// macOS unified titlebar: native traffic lights own min/max/close + dragging,
// and the header leaves room for them on the left.
const nativeWindowControls = computed(() => state.nativeWindowControls)
const titlebarStyle = computed(() =>
  state.titlebarInsetLeft ? { paddingLeft: `${state.titlebarInsetLeft}px` } : {})

// Sync the custom-titlebar state to a body class so global overlays (image
// preview, modal dialogs, confirmations …) can reserve the top 36px via the
// `--titlebar-h` CSS variable instead of accidentally covering the window
// controls. Centralizing it here means no overlay component needs to know
// the titlebar exists.
watch(showCustomTitlebar, (on) => {
  try {
    const cls = 'app-has-titlebar'
    if (on) document.body.classList.add(cls)
    else document.body.classList.remove(cls)
  } catch (_) {}
}, { immediate: true })
const themeOverrides = computed(() => {
  const dark = theme.effective === 'dark'
  const primary = dark ? '#3794ff' : '#2563eb'
  return {
    common: {
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
      primaryColor: primary,
      primaryColorHover: dark ? '#5ba7ff' : '#1d4ed8',
      primaryColorPressed: dark ? '#2b7fd4' : '#1e40af',
      primaryColorSuppl: primary,
      borderRadius: '8px',
      borderColor: dark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(15, 23, 42, 0.10)',
      bodyColor: dark ? '#18191b' : '#f5f7fb',
      cardColor: dark ? '#191a1b' : 'rgba(255, 255, 255, 0.92)',
      modalColor: dark ? '#191a1b' : '#ffffff',
      popoverColor: dark ? '#191a1b' : '#ffffff',
      textColorBase: dark ? '#d7dce4' : '#334155',
      textColor1: dark ? '#f7f8f8' : '#0f172a',
      textColor2: dark ? '#d7dce4' : '#334155',
      textColor3: dark ? '#aab1bb' : '#526277',
    },
    Card: {
      borderRadius: '14px',
      paddingMedium: '16px 18px',
      titleFontWeight: '600',
    },
    Button: {
      borderRadiusMedium: '8px',
      borderRadiusLarge: '10px',
    },
    Dialog: {
      borderRadius: '14px',
      padding: '20px 22px',
    },
    Input: {
      borderRadius: '8px',
      color: dark ? '#25272a' : '#ffffff',
      colorFocus: dark ? '#25272a' : '#ffffff',
    },
    Select: {
      peers: {
        InternalSelection: {
          borderRadius: '8px',
          color: dark ? '#25272a' : '#ffffff',
          colorActive: dark ? '#25272a' : '#ffffff',
          placeholderColor: dark ? '#89919c' : '#596b82',
        },
        InternalSelectMenu: {
          borderRadius: '10px',
          color: dark ? '#191a1b' : '#ffffff',
          boxShadow: dark
            ? '0 16px 40px rgba(0, 0, 0, 0.55)'
            : '0 16px 40px rgba(15, 23, 42, 0.12)',
          optionHeightMedium: '34px',
          optionTextColor: dark ? '#d7dce4' : '#334155',
          optionTextColorActive: dark ? '#f7f8f8' : '#0f172a',
          optionColorPending: dark ? 'rgba(255, 255, 255, 0.06)' : 'rgba(15, 23, 42, 0.04)',
          optionColorActive: dark ? 'rgba(55, 148, 255, 0.14)' : 'rgba(37, 99, 235, 0.10)',
          optionTextColorPressed: dark ? '#f7f8f8' : '#0f172a',
        },
      },
    },
  }
})

const windowMaximized = ref(false)
let windowGeomSaveTimer = null

function captureWindowGeometry() {
  return {
    x: window.screenX,
    y: window.screenY,
    width: window.outerWidth || window.innerWidth,
    height: window.outerHeight || window.innerHeight,
    maximized: windowMaximized.value,
  }
}

function persistWindowGeometryNow() {
  if (!showCustomTitlebar.value) return
  if (typeof api.saveWindowGeometry !== 'function') return
  const g = captureWindowGeometry()
  api.saveWindowGeometry(g.x, g.y, g.width, g.height, g.maximized).catch(() => {})
}

function schedulePersistWindowGeometry() {
  if (!showCustomTitlebar.value) return
  if (windowGeomSaveTimer) clearTimeout(windowGeomSaveTimer)
  windowGeomSaveTimer = setTimeout(() => {
    windowGeomSaveTimer = null
    persistWindowGeometryNow()
  }, 400)
}

function minimize() { api.minimizeWindow().catch(e => console.warn(e)) }
async function maximize() {
  try {
    const r = await api.maximizeWindow()
    if (r?.maximized !== undefined) windowMaximized.value = !!r.maximized
    schedulePersistWindowGeometry()
  } catch (e) {
    console.warn(e)
  }
}
function closeWin() { api.closeWindow().catch(e => console.warn(e)) }
function toggleAssistant() { showAssistant.value = !showAssistant.value }
function closeAssistant() { showAssistant.value = false }

// Last-resort safety net: if the assistant panel throws during render/update
// (e.g. a malformed voice/attachment payload, a Vue v-memo edge case, …), the
// panel tree gets torn down and — because the FAB is `v-if="!showAssistant"`
// — the user is left with NO way back. Catching the error here collapses the
// panel into the FAB and toasts the reason so the session isn't "lost".
onErrorCaptured((err, instance, info) => {
  try {
    const name = instance?.$options?.name || instance?.type?.name || 'unknown'
    const msg = err?.message || String(err)
    console.error('[argusai] assistant panel render error', { name, info, err })
    try {
      const entry = { ts: Date.now(), where: 'assistant_panel', name, info, msg }
      const existing = JSON.parse(localStorage.getItem('argusai.lastError') || 'null')
      localStorage.setItem('argusai.lastError', JSON.stringify(entry))
      if (!existing) localStorage.setItem('argusai.lastError.prev', JSON.stringify(entry))
    } catch (_) { /* ignore */ }
    try { showToast(t('toast.assistantRenderError', { msg: msg.slice(0, 120) }), 'error') } catch (_) {}
    if (showAssistant.value) showAssistant.value = false
  } catch (_) {
    // Keep the captured-error handler itself bulletproof.
  }
  return false
})

// Opening the sandbox workspace takes over the main content area; collapse
// the assistant into the FAB so the user gets maximum space for files.
watch(() => sandboxBrowser.state.open, (open) => {
  if (open) showAssistant.value = false
})

// Same affordance for conversation file preview: collapse the assistant panel
// so MediaPreview gets the main area while the FAB stays reachable (z-index stack).
watch(() => mediaPreview.state.open, (open) => {
  if (open) showAssistant.value = false
})

// If the sandbox workspace is open but empty for the old session, switch it to
// match the conversation the user just focused (tab switch / history session).
watch(
  () => activeTab.value?.sessionId,
  (sid) => {
    if (!sid) return
    sandboxBrowser.followSessionIfWorkspaceEmpty(sid)
  },
)

function onSandboxReferenceFile(payload) {
  showAssistant.value = true
  try {
    window.dispatchEvent(new CustomEvent('argusai:reference-file', { detail: payload }))
  } catch (_) {}
}

function onAssistantFabDragOver(e) {
  if (!Array.from(e.dataTransfer?.types || []).includes('application/x-argusai-sandbox-paths')) return
  e.preventDefault()
}

function onAssistantFabDrop(e) {
  const raw = e.dataTransfer?.getData?.('application/x-argusai-sandbox-paths') || ''
  if (!raw) return
  e.preventDefault()
  try {
    const paths = JSON.parse(raw)
    if (Array.isArray(paths) && paths.length) {
      onSandboxReferenceFile({
        files: paths.map(p => ({ file_path: String(p), file_name: String(p).split('/').pop() || String(p) })),
      })
    }
  } catch (_) {}
}
const onboardingSteps = computed(() => [
  {
    title: t('onboarding.steps.settings.title'),
    body: t('onboarding.steps.settings.body'),
    action: t('onboarding.steps.settings.action'),
    run: () => navigate('/settings'),
  },
  {
    title: t('onboarding.steps.assistant.title'),
    body: t('onboarding.steps.assistant.body'),
    action: t('onboarding.steps.assistant.action'),
    run: () => { showAssistant.value = true },
  },
  {
    title: t('onboarding.steps.localAgents.title'),
    body: t('onboarding.steps.localAgents.body'),
    action: t('onboarding.steps.localAgents.action'),
    run: () => navigate('/local-agents'),
  },
  {
    title: t('onboarding.steps.channels.title'),
    body: t('onboarding.steps.channels.body'),
    action: t('onboarding.steps.channels.action'),
    run: () => navigate('/channels'),
  },
  {
    title: t('onboarding.steps.scan.title'),
    body: t('onboarding.steps.scan.body'),
    action: t('onboarding.steps.scan.action'),
    run: () => navigate('/new'),
  },
  {
    title: t('onboarding.steps.proxy.title'),
    body: t('onboarding.steps.proxy.body'),
    action: t('onboarding.steps.proxy.action'),
    run: () => navigate('/proxy'),
  },
  {
    title: t('onboarding.steps.reverse.title'),
    body: t('onboarding.steps.reverse.body'),
    action: t('onboarding.steps.reverse.action'),
    run: () => navigate('/reverse'),
  },
])
const currentOnboardingStep = computed(() => onboardingSteps.value[onboardingStepIndex.value] || onboardingSteps.value[0])
const onboardingStepProgress = computed(() => t('onboarding.stepProgress', {
  current: onboardingStepIndex.value + 1,
  total: onboardingSteps.value.length,
}))

function markOnboardingSeen() {
  try {
    localStorage.setItem(ONBOARDING_SEEN_KEY, '1')
  } catch (_) { /* ignore */ }
}

async function hasExistingWorkspaceActivity() {
  const [tasksResult, sessionsResult] = await Promise.allSettled([
    api.listTasks(),
    api.listAssistantSessions(),
  ])
  const tasks = tasksResult.status === 'fulfilled' && Array.isArray(tasksResult.value) ? tasksResult.value : []
  const sessions = sessionsResult.status === 'fulfilled' && Array.isArray(sessionsResult.value) ? sessionsResult.value : []
  return tasks.length > 0 || sessions.length > 0
}

async function maybeShowOnboarding() {
  let seen = false
  try {
    seen = localStorage.getItem(ONBOARDING_SEEN_KEY) === '1'
  } catch (_) {
    seen = false
  }
  if (seen) return

  try {
    if (await hasExistingWorkspaceActivity()) {
      markOnboardingSeen()
      return
    }
  } catch (_) {
    // If the backend is still waking up, fall through to the local first-run behavior.
  }

  onboardingStepIndex.value = 0
  showOnboarding.value = true
}

function openOnboarding() {
  onboardingStepIndex.value = 0
  showOnboarding.value = true
}

let onboardingReturnFocus = null
let onboardingShouldRestoreFocus = true

function closeOnboarding({ remember = true, restoreFocus = true } = {}) {
  onboardingShouldRestoreFocus = restoreFocus
  showOnboarding.value = false
  if (remember) markOnboardingSeen()
}

function nextOnboardingStep() {
  if (onboardingStepIndex.value < onboardingSteps.value.length - 1) {
    onboardingStepIndex.value += 1
  } else {
    closeOnboarding()
  }
}

function previousOnboardingStep() {
  onboardingStepIndex.value = Math.max(0, onboardingStepIndex.value - 1)
}

function selectOnboardingStep(index) {
  onboardingStepIndex.value = Math.max(0, Math.min(onboardingSteps.value.length - 1, Number(index) || 0))
}

function runOnboardingAction() {
  const step = currentOnboardingStep.value
  closeOnboarding({ restoreFocus: false })
  step?.run?.()
}

function trapOnboardingFocus(event) {
  if (event.key !== 'Tab' || !onboardingDialogEl.value) return
  const focusable = Array.from(onboardingDialogEl.value.querySelectorAll(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )).filter(el => el.getClientRects().length > 0)
  if (!focusable.length) {
    event.preventDefault()
    onboardingDialogEl.value.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function onOnboardingKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeOnboarding()
    return
  }
  trapOnboardingFocus(event)
}

watch(showOnboarding, async (open) => {
  if (open) {
    onboardingReturnFocus = document.activeElement
    onboardingShouldRestoreFocus = true
    await nextTick()
    onboardingStepHeadingEl.value?.focus()
    return
  }
  const returnTarget = onboardingReturnFocus
  onboardingReturnFocus = null
  if (!onboardingShouldRestoreFocus) return
  await nextTick()
  if (returnTarget?.isConnected && typeof returnTarget.focus === 'function') returnTarget.focus()
})

watch(onboardingStepIndex, async () => {
  if (!showOnboarding.value) return
  await nextTick()
  onboardingStepHeadingEl.value?.focus()
})
const assistantFabStyle = computed(() => {
  if (!assistantFabPos.value.ready) return {}
  return {
    left: `${assistantFabPos.value.left}px`,
    top: `${assistantFabPos.value.top}px`,
  }
})

function assistantFabBounds() {
  const rect = assistantFabEl.value?.getBoundingClientRect?.()
  const width = rect?.width || 128
  const height = rect?.height || 48
  const margin = 12
  const minTop = showCustomTitlebar.value ? 52 : margin
  return {
    minLeft: margin,
    minTop,
    maxLeft: Math.max(margin, window.innerWidth - width - margin),
    maxTop: Math.max(minTop, window.innerHeight - height - margin),
  }
}

function clampAssistantFabPosition(left, top) {
  const b = assistantFabBounds()
  return {
    left: Math.min(b.maxLeft, Math.max(b.minLeft, left)),
    top: Math.min(b.maxTop, Math.max(b.minTop, top)),
  }
}

function persistAssistantFabPosition() {
  if (!assistantFabPos.value.ready) return
  try {
    localStorage.setItem(ASSISTANT_FAB_POS_KEY, JSON.stringify({
      left: Math.round(assistantFabPos.value.left),
      top: Math.round(assistantFabPos.value.top),
    }))
  } catch (_) { /* ignore */ }
}

function initAssistantFabPosition() {
  let saved = null
  try {
    saved = JSON.parse(localStorage.getItem(ASSISTANT_FAB_POS_KEY) || 'null')
  } catch (_) {
    saved = null
  }
  const rect = assistantFabEl.value?.getBoundingClientRect?.()
  const width = rect?.width || 128
  const height = rect?.height || 48
  const initial = saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)
    ? { left: Number(saved.left), top: Number(saved.top) }
    : {
        left: window.innerWidth - width - ASSISTANT_FAB_DEFAULT_RIGHT,
        top: window.innerHeight - height - ASSISTANT_FAB_DEFAULT_BOTTOM,
      }
  assistantFabPos.value = { ...clampAssistantFabPosition(initial.left, initial.top), ready: true }
}

function clampAssistantFabToViewport() {
  if (!assistantFabPos.value.ready) return
  const next = clampAssistantFabPosition(assistantFabPos.value.left, assistantFabPos.value.top)
  assistantFabPos.value = { ...next, ready: true }
  persistAssistantFabPosition()
}

function onAssistantFabClick() {
  if (suppressAssistantFabClick) return
  toggleAssistant()
}

function startAssistantFabDrag(e) {
  if (e.pointerType === 'mouse' && e.button !== 0) return
  if (!assistantFabPos.value.ready) initAssistantFabPosition()
  e.preventDefault()
  e.stopPropagation()
  assistantFabDragging.value = true
  const startX = e.clientX
  const startY = e.clientY
  const startLeft = assistantFabPos.value.left
  const startTop = assistantFabPos.value.top
  let moved = false

  const move = (ev) => {
    const dx = ev.clientX - startX
    const dy = ev.clientY - startY
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true
    const next = clampAssistantFabPosition(startLeft + dx, startTop + dy)
    assistantFabPos.value = { ...next, ready: true }
  }
  const stop = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
    assistantFabDragging.value = false
    persistAssistantFabPosition()
    if (moved) {
      suppressAssistantFabClick = true
      setTimeout(() => { suppressAssistantFabClick = false }, 0)
    }
  }

  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop)
  window.addEventListener('pointercancel', stop)
}

function handleCloseAssistantEvent(e) {
  const sessionId = e?.detail?.session_id || e?.detail?.sessionId || ''
  if (sessionId) setActiveTab(sessionId)
  closeAssistant()
}
// ---------------------------------------------------------------------------
// Window-resize flicker mitigation.
//
// Frameless WebView2 windows on Win32 repaint poorly during resize: every
// pointermove triggers a JS→Python→SetWindowPos IPC round-trip, and any
// CSS ``backdrop-filter: blur(...)`` region has to re-rasterise its
// backdrop layer each frame. Combined with ``transition: all`` on
// chrome elements, that produces visible flicker / tearing.
//
// The mitigation is to tag ``<body>`` with ``window-resizing`` while a
// resize is in flight; theme.css turns transitions/animations/backdrop-
// filter off under that class. We clear the tag a short grace period
// after the last resize signal so the Chromium compositor has a stable
// frame before the heavy effects come back.
// ---------------------------------------------------------------------------
let resizeFlickerTimer = null
function markWindowResizing() {
  document.body.classList.add('window-resizing')
  if (resizeFlickerTimer) clearTimeout(resizeFlickerTimer)
  resizeFlickerTimer = setTimeout(() => {
    document.body.classList.remove('window-resizing')
    resizeFlickerTimer = null
  }, 200)
}
function onWindowResizeFlicker() {
  // Generic native resize (OS-triggered: snap, maximize, monitor change).
  markWindowResizing()
  schedulePersistWindowGeometry()
}

function onBeforeUnloadPersistGeometry() {
  persistWindowGeometryNow()
}

function handleOpenAssistantSessionEvent(ev) {
  const sessionId = String(ev?.detail?.sessionId || '').trim()
  if (!sessionId) return
  const tab = openTab({ sessionId, name: '加载中...' })
  if (!tab) {
    showToast(t('assistant.tabMaxReached', { n: MAX_TABS }), 'info')
    return
  }
  tab._historyLoaded = false
  showAssistant.value = true
  // Explicit session navigation must hydrate directly. The cached session
  // list may be up to a refresh interval behind a newly-created cron chat.
  queueMicrotask(() => {
    window.dispatchEvent(new CustomEvent('argusai:assistant-session-updated', {
      detail: { sessionId, reason: 'explicit_open' },
    }))
  })
}

function handleAssistantSessionUpdatedEvent(ev) {
  const sessionId = String(ev?.detail?.sessionId || '').trim()
  if (!sessionId) return
  const tab = tabs.find(item => item.sessionId === sessionId)
  if (!tab) return
  tab._historyLoaded = false
  tab.updatedAt = Date.now()
  if (!showAssistant.value || activeTab.value?.sessionId !== sessionId) {
    tab.badge.unread = Number(tab.badge.unread || 0) + 1
  }
  recomputeBadge(tab)
  persistTabs()
}

let attentionPublishTimer = null

function publishAttentionState() {
  if (attentionPublishTimer) {
    clearTimeout(attentionPublishTimer)
    attentionPublishTimer = null
  }
  api.setAppAttentionState({
    focused: document.hasFocus(),
    visible: document.visibilityState !== 'hidden',
    assistant_visible: Boolean(showAssistant.value),
    active_session_id: String(activeTab.value?.sessionId || ''),
    open_session_ids: tabs.map(tab => String(tab?.sessionId || '')).filter(Boolean),
  }).catch(() => {})
}

function scheduleAttentionStatePublish() {
  if (attentionPublishTimer) clearTimeout(attentionPublishTimer)
  attentionPublishTimer = setTimeout(publishAttentionState, 0)
}

watch(showAssistant, scheduleAttentionStatePublish)
watch(() => activeTab.value?.sessionId, scheduleAttentionStatePublish)
watch(() => tabs.map(tab => tab.sessionId), scheduleAttentionStatePublish)

onMounted(() => {
  startUpdateMonitor()
  // The native launcher starts this probe before WebView boot. Calling the
  // idempotent ensure endpoint here also covers dev/Vite and embedded hosts.
  api.ensureMcpStartupProbe().catch(() => {})
  window.addEventListener('argusai:close-assistant', handleCloseAssistantEvent)
  window.addEventListener('argusai:open-assistant-session', handleOpenAssistantSessionEvent)
  window.addEventListener('argusai:assistant-session-updated', handleAssistantSessionUpdatedEvent)
  window.addEventListener('resize', clampAssistantFabToViewport)
  window.addEventListener('resize', onWindowResizeFlicker)
  window.addEventListener('beforeunload', onBeforeUnloadPersistGeometry)
  window.addEventListener('focus', scheduleAttentionStatePublish)
  window.addEventListener('blur', scheduleAttentionStatePublish)
  document.addEventListener('visibilitychange', scheduleAttentionStatePublish)
  requestAnimationFrame(initAssistantFabPosition)
  setTimeout(maybeShowOnboarding, 260)
  scheduleAttentionStatePublish()
})
onUnmounted(() => {
  stopUpdateMonitor()
  window.removeEventListener('argusai:close-assistant', handleCloseAssistantEvent)
  window.removeEventListener('argusai:open-assistant-session', handleOpenAssistantSessionEvent)
  window.removeEventListener('argusai:assistant-session-updated', handleAssistantSessionUpdatedEvent)
  window.removeEventListener('resize', clampAssistantFabToViewport)
  window.removeEventListener('resize', onWindowResizeFlicker)
  window.removeEventListener('beforeunload', onBeforeUnloadPersistGeometry)
  window.removeEventListener('focus', scheduleAttentionStatePublish)
  window.removeEventListener('blur', scheduleAttentionStatePublish)
  document.removeEventListener('visibilitychange', scheduleAttentionStatePublish)
  if (attentionPublishTimer) { clearTimeout(attentionPublishTimer); attentionPublishTimer = null }
  if (resizeFlickerTimer) { clearTimeout(resizeFlickerTimer); resizeFlickerTimer = null }
  if (windowGeomSaveTimer) { clearTimeout(windowGeomSaveTimer); windowGeomSaveTimer = null }
  document.body.classList.remove('window-resizing')
})
function onTitlebarDblClick(e) {
  if (e.target?.closest?.('button')) return
  maximize()
}
function startWindowResize(edge, e) {
  if (!showCustomTitlebar.value) return
  e.preventDefault()
  const startX = e.screenX
  const startY = e.screenY
  const startW = window.outerWidth || window.innerWidth
  const startH = window.outerHeight || window.innerHeight
  const startLeft = window.screenX
  const startTop = window.screenY
  let pending = false
  let lastGeom = null
  // Detect whether the atomic geometry API is available in this build.
  // Older builds (or browsers running the dev server without rebuilding
  // the Python side) won't have it; we fall back to the two-step path.
  const hasAtomic = typeof api.setWindowGeometry === 'function'

  // Suspend transitions / backdrop-filter for the entire drag so the
  // window can repaint cleanly while the user drags the edge.
  document.body.classList.add('window-resizing')

  const applyResize = (ev) => {
    const dx = ev.screenX - startX
    const dy = ev.screenY - startY
    let width = startW
    let height = startH
    let left = startLeft
    let top = startTop

    if (edge.includes('e')) width = startW + dx
    if (edge.includes('s')) height = startH + dy
    if (edge.includes('w')) { width = startW - dx; left = startLeft + dx }
    if (edge.includes('n')) { height = startH - dy; top = startTop + dy }

    if (width < 1000) { if (edge.includes('w')) left -= 1000 - width; width = 1000 }
    if (height < 680) { if (edge.includes('n')) top -= 680 - height; height = 680 }

    lastGeom = { left, top, width, height }

    if (pending) return
    pending = true
    requestAnimationFrame(() => {
      pending = false
      const g = lastGeom
      if (!g) return
      lastGeom = null
      if (hasAtomic) {
        // One IPC, one SetWindowPos — no "move then resize" jitter.
        api.setWindowGeometry(g.left, g.top, g.width, g.height).catch(() => {})
      } else {
        if (edge.includes('w') || edge.includes('n')) api.moveWindow(g.left, g.top).catch(() => {})
        api.resizeWindow(g.width, g.height).catch(() => {})
      }
    })
  }
  const stop = () => {
    window.removeEventListener('pointermove', applyResize)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
    // Flush any pending frame that arrived after the last rAF tick.
    if (lastGeom) {
      const g = lastGeom
      lastGeom = null
      if (hasAtomic) {
        api.setWindowGeometry(g.left, g.top, g.width, g.height).catch(() => {})
      } else {
        if (edge.includes('w') || edge.includes('n')) api.moveWindow(g.left, g.top).catch(() => {})
        api.resizeWindow(g.width, g.height).catch(() => {})
      }
    }
    // Keep the suspension active for a short grace period so the final
    // SetWindowPos / WebView2 reflow can settle before the heavy effects
    // come back. Without this we get one last flicker on mouseup.
    markWindowResizing()
    schedulePersistWindowGeometry()
  }
  window.addEventListener('pointermove', applyResize)
  window.addEventListener('pointerup', stop)
  window.addEventListener('pointercancel', stop)
}

const naiveTheme = computed(() => theme.effective === 'dark' ? darkTheme : lightTheme)
</script>

<template>
<n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
 <n-message-provider>
 <n-dialog-provider>
  <template v-if="showCustomTitlebar && !nativeWindowControls">
    <div v-for="edge in ['n','e','s','w','ne','nw','se','sw']" :key="edge" :class="['window-resize-zone', edge]" @pointerdown="startWindowResize(edge, $event)"></div>
  </template>
  <div v-if="showCustomTitlebar" class="titlebar" :class="{ 'titlebar-native-controls': nativeWindowControls }" :style="titlebarStyle">
    <div class="titlebar-drag titlebar-drag-surface" @dblclick="onTitlebarDblClick">
      <span class="titlebar-title">{{ t('app.title') }}</span>
      <div class="titlebar-spacer"></div>
    </div>
    <div class="titlebar-btns" @pointerdown.stop @dblclick.stop>
      <button class="titlebar-btn" @click="openOnboarding" :title="t('onboarding.reopenTitle')">
        <span class="titlebar-help">?</span>
      </button>
      <LocaleSelect variant="titlebar" />
    </div>
    <div v-if="!nativeWindowControls" class="titlebar-win-controls" @pointerdown.stop @dblclick.stop>
      <button class="win-btn" @click="minimize" title="Minimize">
        <svg viewBox="0 0 12 12" width="12" height="12"><path d="M2 6h8" stroke="currentColor" stroke-width="1"/></svg>
      </button>
      <button class="win-btn" @click="maximize" title="Maximize">
        <svg viewBox="0 0 12 12" width="12" height="12"><rect x="2" y="2" width="8" height="8" rx="1" fill="none" stroke="currentColor" stroke-width="1"/></svg>
      </button>
      <button class="win-btn win-btn-close" @click="closeWin" title="Close">
        <svg viewBox="0 0 12 12" width="12" height="12"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
    </div>
  </div>
  <div
    class="shell"
    :class="{
      'has-titlebar': showCustomTitlebar,
      'sidebar-collapsed': sidebarCollapsed,
      'sidebar-expanded': !sidebarCollapsed,
    }"
  >
    <Sidebar />
    <main class="main">
      <div v-if="updateNoticeVisible && route.name !== 'about'" class="app-update-notice" role="status">
        <span>{{ appUpdate.status === 'downloaded' ? t('updates.readyNotice', { version: appUpdate.version }) : t('updates.availableNotice', { version: appUpdate.version }) }}</span>
        <button class="ghost tiny" type="button" @click="navigate('about')">{{ t('updates.viewUpdate') }}</button>
        <button class="app-update-dismiss" type="button" :aria-label="t('updates.dismiss')" @click="dismissUpdateNotice">×</button>
      </div>
      <div class="workbench-card">
        <component :is="currentView" :key="route?.raw || currentView" />
      </div>
    </main>
    <SandboxPanel
      v-if="sandboxBrowser.state.open"
      :session-id="sandboxSessionId"
      :page="assistantContext.route"
      :context="assistantContext"
      :avoid-titlebar="showCustomTitlebar"
      @reference-file="onSandboxReferenceFile"
    />
  </div>
  <button
    v-if="!showAssistant"
    ref="assistantFabEl"
    class="assistant-fab"
    :class="{
      dragging: assistantFabDragging,
      running: anyTabRunning,
      unread: anyTabUnread,
      'context-compact': true,
    }"
    :style="assistantFabStyle"
    :title="`${t('assistant.title')} · Ctrl+Shift+A`"
    @pointerdown="startAssistantFabDrag"
    @dragover="onAssistantFabDragOver"
    @drop="onAssistantFabDrop"
    @click="onAssistantFabClick"
  >
    <span class="assistant-fab-mark" aria-hidden="true">
      <img :src="argusIcon" alt="" />
    </span>
    <span v-if="anyTabRunning" class="assistant-fab-dot" aria-hidden="true"></span>
    <span
      v-else-if="anyTabUnread"
      class="assistant-fab-unread"
      :title="t('assistant.tabUnreadHint', { n: totalUnread })"
    >{{ totalUnread > 99 ? '99+' : totalUnread }}</span>
  </button>
  <AssistantPanel
    :visible="showAssistant"
    :page="route.name"
    :context="assistantContext"
    :avoid-titlebar="showCustomTitlebar"
    @close="closeAssistant"
    @toggle="toggleAssistant"
  />
  <MediaPreview />
  <SandboxExplainPopover
    :session-id="sandboxSessionId"
    @send-to-assistant="onExplainSendToAssistant"
  />
  <WorkbenchTooltipHost />
  <div v-if="showOnboarding" class="onboarding-overlay" @click.self="closeOnboarding()">
    <section
      ref="onboardingDialogEl"
      class="onboarding-card"
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-dialog-title"
      aria-describedby="onboarding-dialog-description"
      tabindex="-1"
      @keydown="onOnboardingKeydown"
    >
      <aside class="onboarding-rail">
        <div class="onboarding-intro">
          <div class="onboarding-kicker">{{ t('onboarding.kicker') }}</div>
          <h2 id="onboarding-dialog-title">{{ t('onboarding.title') }}</h2>
          <p id="onboarding-dialog-description">{{ t('onboarding.subtitle') }}</p>
        </div>
        <ol class="onboarding-steps" :aria-label="t('onboarding.kicker')">
          <li v-for="(step, idx) in onboardingSteps" :key="idx">
            <button
              type="button"
              class="onboarding-step"
              :class="{ active: idx === onboardingStepIndex, done: idx < onboardingStepIndex }"
              :aria-current="idx === onboardingStepIndex ? 'step' : undefined"
              :aria-label="`${idx + 1}. ${step.title}`"
              @click="selectOnboardingStep(idx)"
            >
              <span class="onboarding-step-index">{{ idx + 1 }}</span>
              <span class="onboarding-step-label">{{ step.title }}</span>
            </button>
          </li>
        </ol>
        <button type="button" class="ghost onboarding-exit" @click="closeOnboarding()">
          {{ t('onboarding.skip') }}
        </button>
      </aside>
      <div class="onboarding-content">
        <header class="onboarding-head">
          <div class="onboarding-progress">{{ onboardingStepProgress }}</div>
          <button
            type="button"
            class="ghost onboarding-close"
            :aria-label="`${t('common.cancel')} · ${t('onboarding.title')}`"
            :title="t('common.cancel')"
            @click="closeOnboarding()"
          >
            &times;
          </button>
        </header>
        <div class="onboarding-main">
          <h3
            id="onboarding-step-title"
            ref="onboardingStepHeadingEl"
            tabindex="-1"
          >
            {{ currentOnboardingStep.title }}
          </h3>
          <p>{{ currentOnboardingStep.body }}</p>
          <button type="button" class="primary onboarding-action" @click="runOnboardingAction">
            {{ currentOnboardingStep.action }}
          </button>
        </div>
        <footer class="onboarding-foot">
          <button type="button" class="ghost onboarding-skip" @click="closeOnboarding()">{{ t('onboarding.skip') }}</button>
          <nav class="onboarding-nav" :aria-label="t('onboarding.kicker')">
            <button type="button" class="ghost" :disabled="onboardingStepIndex === 0" @click="previousOnboardingStep">
              {{ t('onboarding.back') }}
            </button>
            <button type="button" class="ghost onboarding-next" @click="nextOnboardingStep">
              {{ onboardingStepIndex === onboardingSteps.length - 1 ? t('onboarding.done') : t('onboarding.next') }}
            </button>
          </nav>
        </footer>
      </div>
    </section>
  </div>
  <Teleport to="body">
    <div v-if="toast.visible" class="toast" :class="toast.kind">
      {{ toast.message }}
    </div>
  </Teleport>
 </n-dialog-provider>
 </n-message-provider>
</n-config-provider>
</template>

<style scoped>
.app-update-notice {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  color: var(--fg);
  font-size: 13px;
}
.app-update-notice > span { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.app-update-dismiss { background: transparent; border: 0; color: var(--muted); cursor: pointer; padding: 3px 6px; font-size: 20px; }
.window-resize-zone {
  position: fixed;
  /* Must sit ABOVE the titlebar (z=10500). The top edge / NW / NE corners
     overlap the first 8-12px of the titlebar; if the titlebar wins the
     stacking war, those pointerdowns get eaten as drags instead of resizes
     and the user can't grow/shrink the window from the top — which matches
     the bug report. Putting the zones above keeps Windows-standard behaviour:
     the very top edge is a resize handle, the rest of the titlebar is drag. */
  z-index: 10600;
  -webkit-app-region: no-drag;
}
.window-resize-zone.n,
.window-resize-zone.s {
  left: 8px;
  right: 8px;
  height: 8px;
  cursor: ns-resize;
}
.window-resize-zone.n { top: 0; }
.window-resize-zone.s { bottom: 0; }
.window-resize-zone.e,
.window-resize-zone.w {
  top: 8px;
  bottom: 8px;
  width: 8px;
  cursor: ew-resize;
}
.window-resize-zone.e { right: 0; }
.window-resize-zone.w { left: 0; }
.window-resize-zone.ne,
.window-resize-zone.nw,
.window-resize-zone.se,
.window-resize-zone.sw {
  width: 12px;
  height: 12px;
}
.window-resize-zone.ne { top: 0; right: 0; cursor: nesw-resize; }
.window-resize-zone.nw { top: 0; left: 0; cursor: nwse-resize; }
.window-resize-zone.se { bottom: 0; right: 0; cursor: nwse-resize; }
.window-resize-zone.sw { bottom: 0; left: 0; cursor: nesw-resize; }

.titlebar-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid currentColor;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.onboarding-overlay {
  position: fixed;
  /* Leave space for the custom titlebar (see theme.css `--titlebar-h`). */
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 5000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(8, 10, 14, 0.58);
  backdrop-filter: blur(8px);
}

.onboarding-card {
  width: min(760px, calc(100vw - 40px));
  max-height: min(620px, calc(100vh - var(--titlebar-h, 0px) - 48px));
  display: grid;
  grid-template-columns: minmax(200px, 224px) minmax(0, 1fr);
  overflow: hidden;
  color: var(--fg);
  background: var(--card-bg, var(--panel));
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 16px;
  box-shadow: var(--shadow-md);
}

.onboarding-rail {
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 18px 14px 14px;
  border-right: 1px solid var(--border);
  background: var(--panel-2);
}

.onboarding-intro {
  padding: 0 4px 16px;
}

.onboarding-kicker {
  color: var(--accent);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
}

.onboarding-intro h2 {
  margin: 6px 0 0;
  color: var(--fg-strong);
  font-size: 17px;
  font-weight: 600;
  line-height: 1.25;
}

.onboarding-intro p {
  margin: 7px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}

.onboarding-steps {
  min-height: 0;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.onboarding-step {
  width: 100%;
  min-width: 0;
  min-height: 38px;
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
  padding: 6px 8px;
  border-color: transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  text-align: left;
  white-space: normal;
}

.onboarding-step:hover:not(:disabled) {
  border-color: transparent;
  background: var(--panel-hover);
  color: var(--fg);
}

.onboarding-step.active {
  border-color: var(--border);
  background: var(--card-bg, var(--panel));
  color: var(--fg-strong);
}

.onboarding-step.done {
  color: var(--ok);
}

.onboarding-step-index {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--panel);
  color: inherit;
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 10px;
}

.onboarding-step.active .onboarding-step-index {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}

.onboarding-step-label {
  min-width: 0;
  overflow: hidden;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.35;
  text-overflow: ellipsis;
}

.onboarding-exit {
  align-self: flex-start;
  margin-top: auto;
  padding-inline: 8px;
  font-size: 11px;
}

.onboarding-content {
  min-width: 0;
  min-height: 360px;
  display: flex;
  flex-direction: column;
  background: var(--card-bg, var(--panel));
}

.onboarding-head {
  flex: 0 0 auto;
  min-height: 54px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px 10px 20px;
  border-bottom: 1px solid var(--border);
}

.onboarding-progress {
  color: var(--muted);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 11px;
  font-weight: 600;
}

.onboarding-close {
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 8px;
  color: var(--muted);
  font-size: 20px;
  line-height: 1;
}

.onboarding-main {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 34px 38px;
  overflow-y: auto;
}

.onboarding-main h3 {
  max-width: 28ch;
  margin: 0;
  color: var(--fg-strong);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.25;
  text-wrap: balance;
}

.onboarding-main h3:focus {
  outline: none;
}

.onboarding-main h3:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
}

.onboarding-main p {
  max-width: 58ch;
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.65;
  text-wrap: pretty;
}

.onboarding-action {
  align-self: flex-start;
  margin-top: 24px;
}

.onboarding-foot {
  flex: 0 0 auto;
  min-height: 58px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px 10px 20px;
  border-top: 1px solid var(--border);
  background: var(--panel-2);
}

.onboarding-nav {
  display: flex;
  align-items: center;
  gap: 6px;
}

.onboarding-next {
  border-color: var(--border-strong, var(--border));
  color: var(--fg);
}

.onboarding-skip {
  color: var(--muted);
}

@media (max-width: 720px) {
  .onboarding-overlay {
    padding: 12px;
  }
  .onboarding-card {
    width: 100%;
    max-height: calc(100vh - var(--titlebar-h, 0px) - 24px);
    grid-template-columns: 1fr;
    grid-template-rows: auto minmax(0, 1fr);
    border-radius: 12px;
  }
  .onboarding-rail {
    padding: 12px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .onboarding-intro {
    padding: 0 2px 10px;
  }
  .onboarding-intro p,
  .onboarding-exit {
    display: none;
  }
  .onboarding-steps {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    overflow-y: hidden;
  }
  .onboarding-steps li {
    flex: 0 0 auto;
  }
  .onboarding-step {
    width: auto;
    min-height: 34px;
    grid-template-columns: 22px;
    margin: 0;
    padding: 5px 7px;
  }
  .onboarding-step-label {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  .onboarding-content {
    min-height: 0;
  }
  .onboarding-main {
    justify-content: flex-start;
    padding: 26px 22px;
  }
  .onboarding-main h3 {
    font-size: 20px;
  }
  .onboarding-foot {
    padding: 10px 12px;
  }
}

@media (max-width: 460px) {
  .onboarding-skip {
    display: none;
  }
  .onboarding-foot {
    justify-content: flex-end;
  }
  .onboarding-nav {
    width: 100%;
    justify-content: space-between;
  }
}

@media (prefers-reduced-transparency: reduce) {
  .onboarding-overlay {
    background: rgba(8, 10, 14, 0.82);
    backdrop-filter: none;
  }
}

:global(:root[data-theme="dark"]) .onboarding-card {
  box-shadow: none;
}
</style>
