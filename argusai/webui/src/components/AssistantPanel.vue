<script setup>
import { computed, ref, reactive, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { NSelect } from 'naive-ui'
import { navigate } from '../router.js'
import { api } from '../api.js'
import { showToast, state } from '../state.js'
import { t } from '../i18n.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useAssistantTabs } from '../composables/useAssistantTabs.js'
import { useMediaPreview } from '../composables/useMediaPreview.js'
import { useAssistantVoiceInput } from '../composables/useAssistantVoiceInput.js'
import { useSandboxBrowser, SANDBOX_MUTATING_TOOLS } from '../composables/useSandboxBrowser.js'
import { useSandboxExplain } from '../composables/useSandboxExplain.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import AssistantCard from './assistant/cards/AssistantCard.vue'
import SubAgentGroupCard from './assistant/SubAgentGroupCard.vue'
import TokenUsageDetail from './assistant/TokenUsageDetail.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'
import SandboxContextMenu from './SandboxContextMenu.vue'
import { ATTACHMENT_ACCEPT_MIMES, ATTACHMENT_AUDIO_ACCEPT_MIMES, ATTACHMENT_FILE_MAX_BYTES, ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT, ATTACHMENT_TEXT_MAX_BYTES, ATTACHMENT_VIDEO_ACCEPT_MIMES, ATTACHMENT_VIDEO_MAX_BYTES, INPUT_PATH_TOKEN_RE, RENDER_WINDOW_SIZE, RENDER_WINDOW_STEP, RENDER_WINDOW_TRIGGER, bytesToBase64, fileNameExt, formatVoiceMs, formatVoiceSecondsLabel, humanSize, isAudioFile, isImageFile, isTextLikeFile, isVideoFile, looksLikeInputPath, normalizeAudioMime, normalizeVideoMime, pathDisplayName, trimPathToken, voiceBubbleWidth } from './assistant/panelUtils.js'
import MarkdownText from './assistant/MarkdownText.vue'
import {
  deriveSelectionReferenceFromContent,
  normalizeAttachment,
  normalizeBlock,
  normalizeBlocks,
  normalizeSelectionReference,
  normalizeVoice,
  stripSelectionReferenceFromContent,
} from '../assistantProtocol.js'
import argusIcon from '../../../../assets/brand/argusai-icon-Transparency.png'

const confirmDialog = useConfirm()
const mediaPreview = useMediaPreview()
const voiceInput = useAssistantVoiceInput()
const showNewSessionModal = ref(false)
const agentRuntimeInventory = ref([])
const agentRuntimesLoading = ref(false)
const newSessionForm = reactive({
  runtime_id: 'argusai-native',
  workspace_mode: 'sandbox',
  project_dir: '',
})
const agentRuntimeOptions = computed(() => (
  agentRuntimeInventory.value
    .filter(item => item.enabled !== false)
    .map(item => ({
      label: `${item.name}${item.probe?.available ? '' : ` · ${t('assistant.runtimeUnavailable')}`}`,
      value: item.id,
      disabled: !item.probe?.available,
    }))
))
const selectedNewRuntime = computed(() => (
  agentRuntimeInventory.value.find(item => item.id === newSessionForm.runtime_id) || null
))
const showModelModal = ref(false)
const modelsLoading = ref(false)
const sessionModels = ref([])
const modelError = ref('')
const collapsedModelGroups = ref(new Set())
const modelSearch = ref('')
const modelSearchInput = ref(null)
// Refs must be top-level in setup so the template auto-unwraps them.
const voiceState = voiceInput.state
const voiceElapsedSec = voiceInput.elapsedSec
const voiceIsSupported = voiceInput.isSupported
const voiceLevel = voiceInput.level
const voiceLevels = voiceInput.levels
const voiceProgress = voiceInput.progress
const voiceMaxSeconds = voiceInput.maxSeconds
const voiceRecordingTime = computed(() => voiceInput.formatVoiceDuration(voiceElapsedSec.value))
const voiceRecordingMaxLabel = computed(() => voiceInput.formatVoiceDuration(voiceMaxSeconds))
const sandboxBrowser = useSandboxBrowser()
const { openAsk: openSelectionAsk } = useSandboxExplain()

const props = defineProps({ visible: Boolean, page: String, context: Object, avoidTitlebar: Boolean })
const emit = defineEmits(['close', 'toggle'])

const reverseJobId = computed(() => {
  const fromRoute = props.page === 'reverse_job'
    ? String(props.context?.route_params?.id || '').trim()
    : ''
  return fromRoute || String(props.context?.reverse_job_id || '').trim()
})

async function openReverseJobDir() {
  const id = reverseJobId.value
  if (!id) return
  try {
    const res = await api.reverseJobRevealDir(id)
    if (res && res.ok === false) showToast(res.error || t('assistant.openDirectoryFailed'), 'error')
  } catch (e) {
    showToast(String(e.message || e), 'error')
  }
}

function goReverseJobPanel() {
  const id = reverseJobId.value
  if (id) navigate(`/reverse/${id}`)
  else navigate('/reverse')
}

// ────────────────────────────────────────────────────────────────────────────
// Multi-tab integration
//
// All conversation state (messages, sending, runSeq, pendingTools, …) used to
// live in component-level refs. With multi-session tabs that state moves into
// the shared store (`useAssistantTabs`) — each tab carries its own copy. We
// expose THIN computed forwarders below so the template can keep using the
// old names (`messages.value`, `sending.value`, …) without thousands of
// edits.
//
// When the user clicks a different tab, `activeTab.value` swaps and every
// forwarder reads from the new tab. Pollers keep running for background
// tabs so a long-running scan doesn't stall the moment you click away.
// ────────────────────────────────────────────────────────────────────────────
const {
  tabs,
  activeTabId,
  activeTab,
  sessions,
  anyTabRunning: _anyTabRunningRef,
  openTab,
  closeTab,
  setActiveTab,
  replaceActiveTabSession,
  refreshSessions,
  preferLastConversation,
  persistTabs,
  newMessageUid,
  recomputeBadge,
  MAX_TABS,
  POLL_INTERVAL_MS,
  POLL_STALL_HINT_MS,
} = useAssistantTabs()

// Help linter understand we read the import (used by FAB via App.vue).
void _anyTabRunningRef


// Per-tab forwarders. We `get`/`set` against `activeTab.value.xxx` so the
// rest of the file can treat them as if they were plain refs — the template
// keeps `v-model="input"`, `:disabled="sending"`, etc. working unchanged.
const sessionId = computed(() => activeTab.value?.sessionId || '')
const messages = makeTabRef('messages', [])
const renderWindowStart = makeTabRef('renderWindowStart', 0)
const showAllMessages = makeTabRef('showAllMessages', false)
const input = makeTabRef('inputDraft', '')
const queuedInputs = makeTabRef('queuedInputs', [])
const pendingAttachments = makeTabRef('pendingAttachments', [])
const pendingVoice = makeTabRef('pendingVoice', null)
const sending = makeTabRef('sending', false)
const activeStreamMessage = makeTabRef('activeStreamMessage', false)
const activeToolName = makeTabRef('activeToolName', '')
const activeToolTimeoutSeconds = makeTabRef('activeToolTimeoutSeconds', 0)
const pendingTool = makeTabRef('pendingTool', null)
const pendingTools = makeTabRef('pendingTools', [])
const pendingDecisions = makeTabRef('pendingDecisions', {})
const pendingAutoAllowNames = makeTabRef('pendingAutoAllowNames', [])
// Fine-grained MCP auto-allow keys (e.g. ``mcp:<server>:<tool>``). Kept
// separate from ``pendingAutoAllowNames`` because tool-name granularity
// would auto-allow every future ``mcp_server_call_tool`` invocation,
// which is exactly what the per-permission scheme was meant to avoid.
const pendingAutoAllowPermissions = makeTabRef('pendingAutoAllowPermissions', [])
const confirmingAction = makeTabRef('confirmingAction', false)
const inputPathRefs = makeTabRef('inputPathRefs', {})
const previewSelectionContext = makeTabRef('previewSelectionContext', null)
const autoFollowScroll = makeTabRef('autoFollowScroll', true)
const webSearchAllowed = makeTabRef('webSearchAllowed', true)
const thinkingEnabled = makeTabRef('thinkingEnabled', false)
const thinkingEffort = makeTabRef('thinkingEffort', 'high')
const engagement = makeTabRef('engagement', null)
const autoAllowTools = makeTabRef('autoAllowTools', [])
const autoApproveMode = makeTabRef('autoApproveMode', false)
const liveJob = makeTabRef('liveJob', null)
const sessionRuntime = makeTabRef('runtime', null)
// Every runtime other than the built-in one speaks ACP, and only those can be
// asked which models they offer.
const isAcpSession = computed(() => {
  const runtimeId = String(sessionRuntime.value?.runtime_id || '')
  return Boolean(runtimeId) && runtimeId !== 'argusai-native'
})
const acpImageCapabilityState = computed(() => {
  if (!isAcpSession.value) return 'supported'
  const capabilities = sessionRuntime.value?.acp_prompt_capabilities
  if (!capabilities || !Object.prototype.hasOwnProperty.call(capabilities, 'image')) {
    return 'unknown'
  }
  return capabilities.image ? 'supported' : 'unsupported'
})
const attachmentButtonTitle = computed(() => (
  acpImageCapabilityState.value === 'unsupported'
    ? t('assistant.acpImagesUnsupportedFilesAllowed')
    : acpImageCapabilityState.value === 'unknown'
      ? t('assistant.acpImagesCheckedOnSend')
      : t('assistant.addImageOrFile')
))
function applyAcpImageFallback(att) {
  if (att?.type !== 'image' || acpImageCapabilityState.value !== 'unsupported') return att
  const hostPath = String(att.host_path || att.path || '').trim()
  if (hostPath) {
    return {
      ...att,
      type: 'file',
      fallback_mode: 'path',
      host_path: hostPath,
      path: hostPath,
      caption: t('assistant.acpImagePathFallbackPrompt'),
      data_base64: '',
    }
  }
  return {
    ...att,
    type: 'file',
    fallback_mode: 'path',
    caption: t('assistant.acpImagePathFallbackPrompt'),
  }
}
function isModelAcpOption(option) {
  const rawCategory = option?.category
  const category = String(
    typeof rawCategory === 'object'
      ? (rawCategory?.category || rawCategory?.id || '')
      : (rawCategory || ''),
  ).toLowerCase()
  const id = String(option?.id || option?.configId || '').toLowerCase()
  const name = String(option?.name || '').toLowerCase()
  return ['model', 'models'].includes(category)
    || ['model', 'models'].includes(id)
    || (name.includes('model') && Array.isArray(option?.options))
}
// Read the effective model straight off the persisted binding so the composer
// chip is right on restore, without probing the agent process.
const activeAcpModel = computed(() => {
  const runtime = sessionRuntime.value || {}
  const preferred = runtime.acp_config || {}
  const options = runtime.acp_config_options || []
  const explicitModelOptions = options.filter(isModelAcpOption)
  const candidateOptions = explicitModelOptions.length
    ? explicitModelOptions
    : options.filter(option => Array.isArray(option?.options))
  const label = (option, value) => {
    const choice = (option?.options || []).find(item => item.value === value)
    return String(choice?.name || value)
  }
  // ACP runtimes commonly expose several selects (mode, model, reasoning).
  // Only read the model select; otherwise Cursor's mode value "default" wins
  // simply because it happens to be the first entry in acp_config.
  for (const option of candidateOptions) {
    const value = preferred[option.id]
    if (value) return label(option, value)
  }
  for (const option of candidateOptions) {
    if (option.current_value) return label(option, option.current_value)
  }
  // Before the first probe there may be no option snapshot yet. Keep the
  // explicit model preference usable when a runtime calls the id "model".
  for (const [id, value] of Object.entries(preferred)) {
    if (['model', 'models'].includes(String(id).toLowerCase()) && value) {
      return String(value)
    }
  }
  return ''
})
// Each conversation restores its own model; async refreshes must target that tab.
const nativeModel = makeTabRef('nativeModel', '')
const activeModelLabel = computed(() => (
  isAcpSession.value ? activeAcpModel.value : nativeModel.value
))
const agentThreads = makeTabRef('agentThreads', [])
const agentPanelOpen = ref(false)
// `/btw`: a one-shot question answered beside the conversation. It is never
// written to history, so it lives here rather than in `tab.messages`.
const sideQuestion = reactive({ open: false, loading: false, question: '', answer: '', error: '' })
const agentDetails = reactive({})
const agentDecisionBusy = reactive({})
const focusedAgentId = ref('')
const selectedAgentIds = ref([])
const agentPendingCount = computed(() => (
  agentThreads.value.reduce((total, agent) => total + agentPendingRequestCount(agent), 0)
))
const removableAgentCount = computed(() => (
  agentThreads.value.filter(agent => !agentIsActive(agent)).length
))
const activeAgentThreads = computed(() => agentThreads.value.filter(agentIsActive))
// The docked live strip only exists to surface *current* activity — threads
// that are running or blocked on the user. Finished-and-idle threads stay
// out of it: they're already permanently reachable from the header's Agent
// Center icon (and, once the completion-notice feature lands a message in
// history, from the transcript itself). Without this filter the strip would
// keep re-rendering after every subsequent chat turn forever, since it has
// nowhere else to "finish" and disappear to.
const liveAgentThreads = computed(() => agentThreads.value.filter(agent => (
  agentIsActive(agent) || agentPendingRequestCount(agent) > 0
)))
// Expand when something needs the user; otherwise stay a one-line strip so
// running threads don't steal conversation height either.
const subagentStreamExpanded = ref(false)
const subagentStreamOpen = computed(() => (
  agentPendingCount.value > 0 || subagentStreamExpanded.value
))
const subagentStreamSummary = computed(() => {
  const waiting = agentPendingCount.value
  const active = liveAgentThreads.value.filter(agentIsActive).length
  const parts = []
  if (waiting) parts.push(t('assistant.subagentPendingCount', { n: waiting }))
  if (active) parts.push(t('assistant.subagentRunningCount', { n: active }))
  return parts.join(' · ') || t('assistant.threadCount', { n: liveAgentThreads.value.length })
})
watch(agentPendingCount, (count) => {
  if (count > 0) subagentStreamExpanded.value = true
})
watch(liveAgentThreads, (list) => {
  if (!list.length) subagentStreamExpanded.value = false
})
const focusedAgentDetail = computed(() => {
  const id = focusedAgentId.value
  if (!id) return null
  return agentDetails[id] || agentThreads.value.find(agent => agent.agent_id === id) || null
})
const selectedAgentThreads = computed(() => {
  const selected = new Set(selectedAgentIds.value)
  return agentThreads.value.filter(agent => selected.has(agent.agent_id))
})
const selectedActiveAgents = computed(() => selectedAgentThreads.value.filter(agentIsActive))
const selectedDeletableAgents = computed(() => selectedAgentThreads.value.filter(agentCanDelete))
// `sending` can intentionally drop while a decision card is rendered, but the
// stream may still be polling its terminal event. Keep the primary action in
// cancel mode until that run has actually been released.
const assistantRunActive = computed(() => Boolean(
  sending.value || activeStreamMessage.value || activeTab.value?.activeRunId,
))
const assistantTools = makeTabRef('assistantTools', [])
const toolsLoading = makeTabRef('toolsLoading', false)
const desktopControl = makeTabRef('desktopControl', { active: false, expires_at: 0, scope: '', remaining_seconds: 0, macos_permissions: null })
let desktopControlPollTimer = null
let agentPollTimer = null
let tokenUsagePollTimer = null
let tokenUsagePollInFlight = false
const inputPathRefEntries = computed(() => Object.entries(inputPathRefs.value || {}).filter(([label, path]) => label && path))
const previewSelectionLabel = computed(() => {
  const sel = previewSelectionContext.value
  if (!sel?.text) return ''
  return selectionReferenceLabel(sel) || t('assistant.previewSnippet')
})
const desktopControlActive = computed(() => Boolean(desktopControl.value?.active))
const desktopControlRemainingLabel = computed(() => {
  const sec = Number(desktopControl.value?.remaining_seconds || 0)
  if (!sec) return ''
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m > 0
    ? t('assistant.durationMinutesSeconds', { minutes: m, seconds: s })
    : t('assistant.durationSeconds', { seconds: s })
})
const macosDesktopPermMissing = computed(() => {
  if (state.platform !== 'darwin') return []
  return (desktopControl.value?.macos_permissions?.permissions || []).filter((p) => p && !p.granted)
})

const engagementActive = computed(() => Boolean(engagement.value?.active))
const greenlightAvailable = computed(() => !isAcpSession.value)
const engagementScopeSummary = computed(() => {
  if (!engagementActive.value) return t('assistant.off')
  const raw = String(engagement.value?.scope || '').trim()
  return raw || t('assistant.unknownScope')
})
const greenlightBusy = ref(false)
const messageContextMenu = reactive({ open: false, x: 0, y: 0, uid: null })
const editingUserMessageUid = ref(null)
const editingUserMessageText = ref('')
const messageContextActions = computed(() => {
  const disabled = sending.value || confirmingAction.value || pendingTools.value.length > 0
  return [
    { id: 'edit-regenerate', label: t('assistant.editAndRegenerate'), disabled },
    { id: 'review-bypass', label: t('assistant.reviewBypass'), disabled: disabled || greenlightBusy.value || !greenlightAvailable.value },
    { id: 'copy', label: t('assistant.copy') },
  ]
})

async function openMacosPrivacySettings(pane) {
  try {
    const r = await api.openMacosPrivacySettings(pane || 'accessibility')
    if (r?.ok) showToast(t('assistant.macosSettingsOpened'), 'ok')
    else showToast(r?.error || t('assistant.openSystemSettingsFailed'), 'error')
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function refreshDesktopControl() {
  const tab = activeTab.value
  if (!tab?.sessionId) return
  try {
    const r = await api.getDesktopActionStatus(tab.sessionId)
    if (r?.ok) {
      tab.desktopControl = {
        active: Boolean(r.active),
        expires_at: Number(r.expires_at || 0),
        scope: String(r.scope || ''),
        remaining_seconds: Number(r.remaining_seconds || 0),
        state: r.state || (r.active ? 'active_desktop_control' : 'idle'),
        macos_permissions: r.macos_permissions || null,
      }
    }
  } catch (_) { /* ignore */ }
}

function syncDesktopControlFromMessages(msgs) {
  const tab = activeTab.value
  if (!tab || !Array.isArray(msgs)) return
  for (let i = msgs.length - 1; i >= 0; i--) {
    const blocks = msgs[i]?.blocks || []
    for (let j = blocks.length - 1; j >= 0; j--) {
      const b = blocks[j]
      if (b?.type !== 'desktop_control') continue
      // Timed desktop authorization is retired. Historical active flags
      // must not turn the banner back on.
      tab.desktopControl = {
        active: false,
        expires_at: 0,
        scope: '',
        remaining_seconds: 0,
        state: 'idle',
        macos_permissions: b.macos_permissions || tab.desktopControl?.macos_permissions || null,
      }
      return
    }
  }
}

async function stopDesktopControl() {
  const tab = activeTab.value
  if (!tab?.sessionId) return
  try {
    await api.stopDesktopAction(tab.sessionId)
    tab.desktopControl = { active: false, expires_at: 0, scope: '', remaining_seconds: 0, state: 'idle' }
    showToast(t('assistant.desktopAuthorizationStopped'), 'ok')
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function scheduleDesktopControlPoll() {
  if (desktopControlPollTimer) clearInterval(desktopControlPollTimer)
  desktopControlPollTimer = setInterval(() => {
    if (!desktopControlActive.value) {
      clearInterval(desktopControlPollTimer)
      desktopControlPollTimer = null
      return
    }
    refreshDesktopControl()
  }, 1000)
}

watch(desktopControlActive, (on) => {
  if (on) {
    refreshDesktopControl()
    scheduleDesktopControlPoll()
  } else if (desktopControlPollTimer) {
    clearInterval(desktopControlPollTimer)
    desktopControlPollTimer = null
  }
})

watch(sessionId, () => {
  refreshDesktopControl()
})

const assistantRunStatus = computed(() => {
  // The generic "正在处理" state is intentionally omitted: the input bar
  // already turns into a 取消 button and tokens stream visibly into the
  // bubble, so a third "AI is busy" badge is just noise. We keep the
  // *specific* states (desktop authorization, confirm round-trip) because
  // they tell the user something the chat surface doesn't already show.
  // Tool execution is omitted: the active bubble already shows
  // 「正在执行工具：…」 in its status row.
  if (desktopControlActive.value) {
    return {
      label: t('assistant.desktopAuthorizationActive'),
      title: t('assistant.desktopAuthorizationActiveTitle'),
    }
  }
  if (confirmingAction.value) {
    return {
      label: t('assistant.confirming'),
      title: t('assistant.confirmingTitle'),
    }
  }
  return null
})

function formatJobProgress(job) {
  const progress = job?.progress || {}
  const current = Number(progress.current || 0)
  const total = Number(progress.total || 0)
  return total > 0 ? `${current} / ${total}` : (current > 0 ? String(current) : '')
}

async function refreshRunJobs(tab = activeTab.value) {
  if (!tab?.sessionId) return []
  try {
    const result = await api.listAssistantRunJobs(tab.sessionId)
    if (!result?.ok) return []
    const jobs = Array.isArray(result.jobs) ? result.jobs : []
    // While a stream is still polling, trust stream snapshots over disk list
    // results so a refresh cannot demote a live running job to paused.
    if (tab.liveJob?.job_id && !tab.activeRunId) {
      const latest = jobs.find(job => job.job_id === tab.liveJob.job_id)
      if (latest) tab.liveJob = latest
    }
    return jobs
  } catch (_) {
    return []
  }
}

function agentStatusLabel(status) {
  return {
    queued: t('assistant.agentStatusQueued'),
    running: t('assistant.agentStatusRunning'),
    paused: t('assistant.agentStatusPaused'),
    completed: t('assistant.agentStatusCompleted'),
    failed: t('assistant.agentStatusFailed'),
    closing: t('assistant.agentStatusStopping'),
    closed: t('assistant.agentStatusClosed'),
  }[status] || String(status || t('assistant.unknown'))
}

function scrollHeaderTools(event) {
  const el = event?.currentTarget
  if (!el || el.scrollWidth <= el.clientWidth) return
  event.preventDefault()
  el.scrollLeft += Number(event.deltaY || event.deltaX || 0)
}

function agentIsActive(agent) {
  return ['queued', 'running', 'closing'].includes(agent?.status)
}

function agentCanDelete(agent) {
  return Boolean(agent?.agent_id) && !agentIsActive(agent)
}

function agentPendingTools(agent) {
  const tools = agent?.pending_confirmation?.pending_tools
  return Array.isArray(tools) ? tools : []
}

function agentPendingBlocks(agent) {
  const blocks = agent?.pending_confirmation?.pending_blocks
  return Array.isArray(blocks) ? blocks : []
}

function agentPendingRequestCount(agent) {
  const tools = agentPendingTools(agent)
  const blocks = agentPendingBlocks(agent).filter(block =>
    ['confirm', 'command_plan', 'user_decision', 'user_decision_batch'].includes(block?.type),
  )
  return Math.max(tools.length, blocks.length)
}

function agentAllowsSessionApproval(agent) {
  const blocks = agentPendingBlocks(agent)
  return Boolean(blocks.length) && blocks.every(block =>
    Array.isArray(block?.actions) && block.actions.some(action => action?.value === 'allow_session'),
  )
}

function agentToolArgs(tool) {
  try {
    return JSON.stringify(tool?.arguments || {})
  } catch (_) {
    return '{}'
  }
}

async function confirmAgentTools(agent, verdict) {
  const id = String(agent?.agent_id || '')
  const tools = agentPendingTools(agent)
  if (!id || !tools.length || agentDecisionBusy[id]) return
  const binding = {
    pending_id: String(tools[0]?.pending_id || ''),
    version: Number(tools[0]?.version || 0),
  }
  if (!binding.pending_id || !Number.isInteger(binding.version) || binding.version < 1) {
    showToast(t('assistant.agentConfirmationExpired'), 'error')
    return
  }
  let confirmation
  if (verdict === 'deny') {
    confirmation = { type: 'confirmation_decision', verdict: 'deny', ...binding }
  } else if (tools.length === 1) {
    confirmation = {
      type: 'confirmation_decision',
      call_id: String(tools[0]?.id || ''),
      verdict: verdict === 'allow_session' ? 'allow_session' : 'allow',
      ...binding,
    }
  } else {
    confirmation = {
      type: 'batch_confirmation_decision',
      decisions: Object.fromEntries(tools.map(tool => [
        String(tool?.id || ''),
        verdict === 'allow_session' ? 'allow_session' : 'allow',
      ])),
      auto_allow_names: verdict === 'allow_session' ? tools.map(tool => String(tool?.name || '')) : [],
      auto_allow_permissions: [],
      ...binding,
    }
  }
  agentDecisionBusy[id] = true
  // Optimistic clear: native confirm used to keep pending_confirmation until
  // the resume turn finished, so the Authorize buttons lingered for seconds.
  const previousPending = agent.pending_confirmation
  const tab = activeTab.value
  const applyAgent = (next) => {
    if (!next || !tab) return
    const list = Array.isArray(tab.agentThreads) ? [...tab.agentThreads] : []
    const index = list.findIndex(item => item?.agent_id === id)
    if (index >= 0) list[index] = { ...list[index], ...next }
    else list.unshift(next)
    tab.agentThreads = list
  }
  applyAgent({
    ...agent,
    pending_confirmation: null,
    status: verdict === 'deny' ? 'paused' : 'queued',
  })
  try {
    const result = await api.confirmAssistantAgentAction(sessionId.value, id, confirmation)
    if (!result?.ok) throw new Error(result?.error || t('assistant.agentConfirmationFailed'))
    await refreshAgentThreads()
  } catch (e) {
    applyAgent({ ...agent, pending_confirmation: previousPending })
    showToast(String(e?.message || e), 'error')
  } finally {
    agentDecisionBusy[id] = false
  }
}

async function refreshAgentThreads(tab = activeTab.value) {
  if (!tab?.sessionId) return []
  try {
    const result = await api.listAssistantAgents(tab.sessionId, false)
    if (!result?.ok) return []
    tab.agentThreads = Array.isArray(result.agents) ? result.agents : []
    const known = new Set(tab.agentThreads.map(agent => agent.agent_id))
    selectedAgentIds.value = selectedAgentIds.value.filter(id => known.has(id))
    return tab.agentThreads
  } catch (_) {
    return []
  }
}

// Live push: refresh the sub-agent dock the instant a child Agent changes
// state, instead of waiting for the 2s/8s fallback poll above. Scoped to the
// active session so a background session's churn doesn't reload this view.
useLiveChannel('assistant_agents', () => {
  const tab = activeTab.value
  if (tab?.sessionId) refreshAgentThreads(tab)
}, { sessionId: () => activeTab.value?.sessionId, debounceMs: 150 })

function closeAgentCenter() {
  agentPanelOpen.value = false
  focusedAgentId.value = ''
  selectedAgentIds.value = []
}

function closeSideQuestion() {
  sideQuestion.open = false
  sideQuestion.loading = false
}

async function askSideQuestion(rawQuestion) {
  const tab = activeTab.value
  if (!tab) return
  const question = String(rawQuestion || '').trim()
  tab.inputDraft = ''
  tab.inputPathRefs = {}
  nextTick(resizeAssistantInput)
  Object.assign(sideQuestion, { open: true, loading: false, question, answer: '', error: '' })
  if (!question) {
    sideQuestion.error = t('assistant.sideUsage')
    return
  }
  sideQuestion.loading = true
  try {
    const res = await api.assistantSideQuestion(tab.sessionId, question)
    if (!res || !res.ok) throw new Error((res && res.error) || t('assistant.sideFailed'))
    sideQuestion.answer = String(res.text || '')
    if (!sideQuestion.answer) sideQuestion.error = t('assistant.sideEmpty')
  } catch (e) {
    sideQuestion.error = String(e.message || e)
  } finally {
    sideQuestion.loading = false
  }
}

async function openAgentCenter(agent = null) {
  agentPanelOpen.value = true
  showToolList.value = false
  showSessionList.value = false
  showTokenUsage.value = false
  showThinkingMenu.value = false
  if (agent) await openAgentContext(agent)
}

async function openAgentContext(agent) {
  const id = String(agent?.agent_id || '')
  if (!id) return
  agentPanelOpen.value = true
  focusedAgentId.value = id
  try {
    const result = await api.getAssistantAgent(sessionId.value, id)
    if (result?.ok && result.agent) agentDetails[id] = result.agent
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function previewAgentFromActivity(agent) {
  openAgentContext(agent)
}

function toggleAgentSelection(agent) {
  const id = String(agent?.agent_id || '')
  if (!id) return
  selectedAgentIds.value = selectedAgentIds.value.includes(id)
    ? selectedAgentIds.value.filter(item => item !== id)
    : [...selectedAgentIds.value, id]
}

function selectFinishedAgents() {
  selectedAgentIds.value = agentThreads.value.filter(agentCanDelete).map(agent => agent.agent_id)
}

async function stopSelectedAgents() {
  const targets = selectedActiveAgents.value.filter(agent => agent.status !== 'closing')
  if (!targets.length) return
  const ok = await confirmDialog({
    title: t('assistant.stopSelectedAgentsTitle'),
    content: t('assistant.stopSelectedAgentsContent', { count: targets.length }),
    positiveText: t('assistant.stop'),
    negativeText: t('assistant.cancel'),
  })
  if (!ok) return
  await Promise.all(targets.map(agent => api.closeAssistantAgent(sessionId.value, agent.agent_id)))
  await refreshAgentThreads()
}

async function deleteSelectedAgents() {
  const targets = selectedDeletableAgents.value
  if (!targets.length) return
  const ok = await confirmDialog({
    title: t('assistant.deleteSelectedAgentsTitle'),
    content: t('assistant.deleteSelectedAgentsContent', { count: targets.length }),
    positiveText: t('assistant.delete'),
    negativeText: t('assistant.cancel'),
  })
  if (!ok) return
  const results = await Promise.all(
    targets.map(agent => api.deleteAssistantAgent(sessionId.value, agent.agent_id)),
  )
  selectedAgentIds.value = []
  await refreshAgentThreads()
  const failed = results.filter(result => !result?.ok)
  if (failed.length) showToast(t('assistant.agentsDeleteFailed', { count: failed.length }), 'error')
}

async function closeAgentThread(agent) {
  const id = String(agent?.agent_id || '')
  if (!id) return
  try {
    const result = await api.closeAssistantAgent(sessionId.value, id)
    if (!result?.ok) throw new Error(result?.error || t('assistant.stopAgentFailed'))
    await refreshAgentThreads()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function deleteAgentThread(agent, ask = true) {
  const id = String(agent?.agent_id || '')
  if (!id || !agentCanDelete(agent)) return
  if (ask) {
    const ok = await confirmDialog({
      title: t('assistant.deleteAgentTitle'),
      content: t('assistant.deleteAgentContent', { name: agent.role || id }),
      positiveText: t('assistant.delete'),
      negativeText: t('assistant.cancel'),
    })
    if (!ok) return
  }
  try {
    const result = await api.deleteAssistantAgent(sessionId.value, id)
    if (!result?.ok) throw new Error(result?.error || t('assistant.deleteAgentFailed'))
    delete agentDetails[id]
    if (focusedAgentId.value === id) focusedAgentId.value = ''
    selectedAgentIds.value = selectedAgentIds.value.filter(item => item !== id)
    await refreshAgentThreads()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function clearFinishedAgents() {
  const targets = agentThreads.value.filter(agentCanDelete)
  if (!targets.length) return
  const ok = await confirmDialog({
    title: t('assistant.clearFinishedAgentsTitle'),
    content: t('assistant.clearFinishedAgentsContent', { count: targets.length }),
    positiveText: t('assistant.clearAll'),
    negativeText: t('assistant.cancel'),
  })
  if (!ok) return
  try {
    const results = await Promise.all(
      targets.map(agent => api.deleteAssistantAgent(sessionId.value, agent.agent_id)),
    )
    const failed = results.filter(result => !result?.ok)
    await refreshAgentThreads()
    if (failed.length) showToast(t('assistant.agentsDeleteFailed', { count: failed.length }), 'error')
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function pauseAgentThread(agent) {
  const id = String(agent?.agent_id || '')
  if (!id) return
  try {
    const result = await api.pauseAssistantAgent(sessionId.value, id)
    if (!result?.ok) throw new Error(result?.error || t('assistant.pauseAgentFailed'))
    await refreshAgentThreads()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function expandPathRefs(text, refs = inputPathRefs.value) {
  let out = String(text || '')
  const entries = Object.entries(refs || {})
    .filter(([label, path]) => label && path)
    .sort((a, b) => b[0].length - a[0].length)
  for (const [label, path] of entries) {
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    out = out.replace(new RegExp(`(^|\\s)${escaped}`, 'g'), (_m, prefix) => `${prefix}${path}`)
  }
  return out
}

function composeInputForSend(htmlDraft, refs = inputPathRefs.value) {
  const temp = document.createElement('div')
  temp.innerHTML = htmlDraft || ''
  
  const chips = temp.querySelectorAll('.input-inline-path-chip')
  for (const chip of Array.from(chips)) {
    const label = chip.getAttribute('data-label')
    if (label && refs[label]) {
      chip.replaceWith(refs[label])
    }
  }
  
  let text = ''
  for (const node of Array.from(temp.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.nodeName === 'BR') text += '\n'
      else if (node.nodeName === 'DIV' || node.nodeName === 'P') text += '\n' + node.innerText
      else text += node.innerText
    }
  }
  
  return text.trim()
}

function addInputPathRefs(paths) {
  const next = { ...(inputPathRefs.value || {}) }
  const labels = []
  for (const raw of paths || []) {
    const path = String(raw || '').trim()
    if (!path) continue
    let label = pathDisplayName(path) || path
    if (next[label] && next[label] !== path) {
      const stem = label
      let n = 2
      while (next[`${stem}#${n}`] && next[`${stem}#${n}`] !== path) n += 1
      label = `${stem}#${n}`
    }
    next[label] = path
    labels.push(label)
  }
  inputPathRefs.value = next
  return labels
}

function removeInputPathRef(label) {
  const next = { ...(inputPathRefs.value || {}) }
  delete next[label]
  inputPathRefs.value = next
}

function makeTabRef(field, fallback) {
  return computed({
    get() {
      const tab = activeTab.value
      if (!tab) return fallback
      const v = tab[field]
      return v === undefined ? fallback : v
    },
    set(v) {
      const tab = activeTab.value
      if (!tab) return
      tab[field] = v
    },
  })
}

const inputEl = ref(null)
const fileInput = ref(null)
const dragOver = ref(false)
const msgContainer = ref(null)
const isComposingInput = ref(false)

const THINKING_EFFORTS = ['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
function normalizeThinkingEffort(value) {
  const text = String(value || '').trim().toLowerCase()
  return THINKING_EFFORTS.includes(text) ? text : 'high'
}
const thinkingEffortOptions = computed(() => [
  { value: 'auto', label: t('assistant.thinkingEffortAuto') },
  { value: 'none', label: t('assistant.thinkingEffortNone') },
  { value: 'minimal', label: t('assistant.thinkingEffortMinimal') },
  { value: 'low', label: t('assistant.thinkingEffortLow') },
  { value: 'medium', label: t('assistant.thinkingEffortMedium') },
  { value: 'high', label: t('assistant.thinkingEffortHigh') },
  { value: 'xhigh', label: t('assistant.thinkingEffortXhigh') },
  { value: 'max', label: t('assistant.thinkingEffortMax') },
])
const thinkingMenuSteps = computed(() => [
  { value: 'off', label: t('assistant.off') },
  ...thinkingEffortOptions.value,
])
const thinkingEffortLabel = computed(() => {
  const match = thinkingEffortOptions.value.find(item => item.value === thinkingEffort.value)
  return match ? match.label : thinkingEffort.value
})
const thinkingControlTitle = computed(() => t('assistant.thinkingEffortTitle', {
  effort: thinkingEnabled.value ? thinkingEffortLabel.value : t('assistant.off'),
}))
const thinkingSliderIndex = computed(() => {
  if (!thinkingEnabled.value) return 0
  const idx = thinkingEffortOptions.value.findIndex(item => item.value === thinkingEffort.value)
  return idx >= 0 ? idx + 1 : 6
})

const PANEL_MARGIN = 12
const DEFAULT_PANEL_RIGHT = 18
const DEFAULT_PANEL_TOP_OFFSET = 18
const MAXIMIZED_TOP_WITH_TITLEBAR = 40
const MIN_PANEL_WIDTH = 360
const MIN_PANEL_HEIGHT = 420
const DEFAULT_PANEL_WIDTH = 440
const minPanelLeft = computed(() => PANEL_MARGIN)
const minPanelTop = computed(() => props.avoidTitlebar ? 54 : 18)
const panelWidth = ref(Math.min(DEFAULT_PANEL_WIDTH, maxPanelWidth()))
const panelHeight = ref(Math.min(Math.max(520, window.innerHeight - 120), maxPanelHeight()))
const panelLeft = ref(defaultPanelLeft())
const panelTop = ref(defaultPanelTop())
const isMaximized = ref(false)
const panelMovedByUser = ref(false)
const showSessionList = ref(false)
const showToolList = ref(false)
const showTokenUsage = ref(false)
const showThinkingMenu = ref(false)
const defaultWebSearchAllowed = ref(false)
const defaultAutoApproveMode = ref(false)
const defaultThinkingEnabled = ref(false)
const defaultThinkingEffort = ref('high')
const autoApproveBusy = ref(false)

const restoredPanel = ref({
  width: panelWidth.value,
  height: panelHeight.value,
  left: panelLeft.value,
  top: panelTop.value,
})
const panelStyle = computed(() => ({
  top: panelTop.value + 'px',
  left: panelLeft.value + 'px',
  width: panelWidth.value + 'px',
  height: panelHeight.value + 'px',
}))

function clearPendingTools() {
  const tab = activeTab.value
  if (!tab) return
  tab.pendingTools = []
  tab.pendingTool = null
  tab.pendingDecisions = {}
  tab.pendingAutoAllowNames = []
  tab.pendingAutoAllowPermissions = []
  clearActiveTool(tab)
  recomputeBadge(tab)
}

function setPendingFromResponse(r, tab = activeTab.value) {
  if (!tab) return
  const tools = Array.isArray(r?.pending_tools) && r.pending_tools.length
    ? r.pending_tools
    : (r?.pending_tool ? [r.pending_tool] : [])
  tab.pendingTools = tools
  tab.pendingTool = tools[0] || null
  const liveIds = new Set(tools.map(t => t.id).filter(Boolean))
  const liveNames = new Set(tools.map(t => t.name).filter(Boolean))
  const nextDecisions = {}
  for (const [k, v] of Object.entries(tab.pendingDecisions || {})) {
    if (liveIds.has(k)) nextDecisions[k] = v
  }
  tab.pendingDecisions = nextDecisions
  tab.pendingAutoAllowNames = (tab.pendingAutoAllowNames || []).filter(n => liveNames.has(n))
  // Permission keys are scoped to specific MCP server+tool combos, so
  // we filter them by the live (name, arguments)-derived keys instead
  // of plain tool names.
  const livePermKeys = new Set(
    tools
      .map(t => mcpPermissionKey(t.name, t.arguments || {}))
      .filter(Boolean)
  )
  tab.pendingAutoAllowPermissions = (tab.pendingAutoAllowPermissions || []).filter(k => livePermKeys.has(k))
  recomputeBadge(tab)
}

// Cache/token usage from a getAssistantSession / getAssistantTokenUsage
// snapshot or a turn's done event — same shape either way (see
// argusai/gui/assistant_sessions.py and argusai/gui/assistant_run.py).
function applySessionTokenUsage(r, tab = activeTab.value) {
  if (!tab) return
  tab.tokenUsage = r?.token_usage && typeof r.token_usage === 'object' ? { ...r.token_usage } : null
  tab.lastTokenUsage = r?.last_token_usage && typeof r.last_token_usage === 'object' ? { ...r.last_token_usage } : null
  tab.cacheHitRate = typeof r?.cache_hit_rate === 'number' ? r.cache_hit_rate : null
  const promptTokens = Number(r?.prompt_tokens)
  const promptBytes = Number(r?.prompt_bytes)
  const contextWindow = Number(r?.context_window)
  if (Number.isFinite(promptTokens) && promptTokens >= 0) tab.promptTokens = promptTokens
  if (Number.isFinite(promptBytes) && promptBytes >= 0) tab.promptBytes = promptBytes
  if (Number.isFinite(contextWindow) && contextWindow >= 0) tab.contextWindow = contextWindow
}

async function refreshActiveTokenUsage(tab = activeTab.value) {
  if (!tab?.sessionId || tokenUsagePollInFlight) return
  tokenUsagePollInFlight = true
  try {
    const r = await withTimeout(api.getAssistantTokenUsage(tab.sessionId), 1500, null)
    if (r?.ok && activeTab.value?.sessionId === tab.sessionId) {
      applySessionTokenUsage(r, tab)
    }
  } catch (_) { /* best-effort live counters */ }
  finally {
    tokenUsagePollInFlight = false
  }
}

function hydrateExternalPendingBlock(block, tab = activeTab.value) {
  if (
    !tab
    || !block?.external_confirmation
    || !(block?.confirm_value || block?.cancel_value)
  ) return
  const id = String(block.confirm_id || '')
  const name = String(block.name || '')
  const pendingId = String(block.pending_id || '')
  const version = Number(block.pending_version || block.version || 0)
  if (!id || !name || !pendingId || !Number.isInteger(version) || version < 1) return
  const tool = {
    id,
    name,
    arguments: plainJson(block.arguments || {}),
    pending_id: pendingId,
    version,
    external_confirmation: true,
  }
  const existing = Array.isArray(tab.pendingTools) ? tab.pendingTools : []
  const index = existing.findIndex(item => item.id === id)
  tab.pendingTools = index >= 0
    ? existing.map((item, i) => i === index ? { ...item, ...tool } : item)
    : [...existing, tool]
  tab.pendingTool = tab.pendingTools[0] || null
  recomputeBadge(tab)
}

function restorePendingProjection(r, tab = activeTab.value) {
  if (!tab) return
  setPendingFromResponse(r, tab)
  const blocks = Array.isArray(r?.pending_blocks) ? r.pending_blocks : []
  if (!blocks.length) return
  const existingConfirmIds = new Set(
    (tab.messages || []).flatMap(m => (m.blocks || []))
      .map(b => String(b?.confirm_id || ''))
      .filter(Boolean),
  )
  const missing = blocks.filter(b => {
    const id = String(b?.confirm_id || '')
    return !id || !existingConfirmIds.has(id)
  })
  if (missing.length) {
    addMsg('assistant', '', null, null, missing, null, null, tab, {
      runtimeEvent: true,
      contextPolicy: 'exclude',
    })
  }
}

/**
 * Mirror of ``AssistantSession._mcp_permission_key`` so the frontend can
 * derive the same fine-grained key when batching multiple confirmations.
 * Keep in sync with ``argusai/core/assistant/session.py``.
 */
function mcpPermissionKey(name, args) {
  if (!args || typeof args !== 'object') return ''
  const sid = String(args.server_id || '').trim()
  if (!sid) return ''
  if (name === 'mcp_server_call_tool') {
    const t = String(args.tool_name || '').trim()
    return t ? `mcp:${sid}:${t}` : ''
  }
  if (name === 'mcp_server_read_resource') return `mcp:${sid}:resource_read`
  if (name === 'mcp_server_list_tools') return `mcp:${sid}:list_tools`
  if (name === 'mcp_server_list_resources') return `mcp:${sid}:list_resources`
  if (name === 'mcp_server_health') return `mcp:${sid}:health`
  return ''
}

const pendingBatchSize = computed(() => pendingTools.value.length)
const pendingDecidedCount = computed(() => {
  if (!pendingTools.value.length) return 0
  const ids = new Set(pendingTools.value.map(t => t.id).filter(Boolean))
  let n = 0
  for (const id of ids) {
    if (pendingDecisions.value[id]) n += 1
  }
  return n
})
const pendingAllDecided = computed(() => pendingBatchSize.value > 0 && pendingDecidedCount.value >= pendingBatchSize.value)
const hasAwaitingUserDecision = computed(() => {
  // "Awaiting" means the most recent decision card is *still pending* —
  // a block that already carries ``submitted_response`` was answered by
  // the user and must not freeze the input affordances. Without this
  // guard, clicking an option on the card silently keeps the composer
  // stuck on "发送" + "请在上方决策卡片中选择..." even though the
  // backend has already moved on into a new agent loop (which the user
  // can no longer cancel via the primary button). See
  // ``handleCardAction`` + ``markUserDecisionSubmitted``.
  let lastPendingDecisionIndex = -1
  let lastUserIndex = -1
  messages.value.forEach((m, idx) => {
    if (m?.role === 'user') lastUserIndex = idx
    if (
      m?.role === 'assistant'
      && Array.isArray(m?.blocks)
      && m.blocks.some(b => isPendingUserDecisionBlock(b))
    ) {
      lastPendingDecisionIndex = idx
    }
  })
  return lastPendingDecisionIndex >= 0 && lastPendingDecisionIndex > lastUserIndex
})

function maxPanelWidth() {
  return Math.max(280, window.innerWidth - minPanelLeft.value - PANEL_MARGIN)
}

function maxPanelHeight() {
  return Math.max(320, window.innerHeight - minPanelTop.value - PANEL_MARGIN)
}

function minPanelWidth() {
  return Math.min(MIN_PANEL_WIDTH, maxPanelWidth())
}

function minPanelHeight() {
  return Math.min(MIN_PANEL_HEIGHT, maxPanelHeight())
}

function maxPanelLeft() {
  return Math.max(minPanelLeft.value, window.innerWidth - panelWidth.value - PANEL_MARGIN)
}

function maxPanelTop() {
  return Math.max(minPanelTop.value, window.innerHeight - panelHeight.value - PANEL_MARGIN)
}

function maximizedPanelLeft() {
  return PANEL_MARGIN
}

function maximizedPanelTop() {
  return props.avoidTitlebar ? MAXIMIZED_TOP_WITH_TITLEBAR : PANEL_MARGIN
}

function maximizedPanelWidth() {
  return Math.max(320, window.innerWidth - (PANEL_MARGIN * 2))
}

function maximizedPanelHeight() {
  return Math.max(320, window.innerHeight - maximizedPanelTop() - PANEL_MARGIN)
}

function defaultPanelLeft() {
  return Math.min(maxPanelLeft(), Math.max(minPanelLeft.value, window.innerWidth - panelWidth.value - DEFAULT_PANEL_RIGHT))
}

function defaultPanelTop() {
  return Math.min(maxPanelTop(), Math.max(minPanelTop.value, minPanelTop.value + DEFAULT_PANEL_TOP_OFFSET))
}

function alignPanelToDefault() {
  clampPanelSize()
  panelLeft.value = defaultPanelLeft()
  panelTop.value = defaultPanelTop()
}

function clampPanelPosition() {
  panelLeft.value = Math.min(maxPanelLeft(), Math.max(minPanelLeft.value, panelLeft.value))
  panelTop.value = Math.min(maxPanelTop(), Math.max(minPanelTop.value, panelTop.value))
}

function clampPanelSize() {
  panelWidth.value = Math.min(maxPanelWidth(), Math.max(minPanelWidth(), panelWidth.value))
  panelHeight.value = Math.min(maxPanelHeight(), Math.max(minPanelHeight(), panelHeight.value))
  clampPanelPosition()
}

async function loadSessions() {
  await refreshSessions(true)
}

function toggleSessionList() {
  showSessionList.value = !showSessionList.value
  showToolList.value = false
  showTokenUsage.value = false
  showThinkingMenu.value = false
  if (showSessionList.value) {
    sessionFilter.value = ''
    loadSessions().catch(() => {})
  }
}

function manageAllSessions() {
  showSessionList.value = false
  emit('close')
  navigate('/assistant')
}

function formatSessionTime(session) {
  const ts = session?.updated_at || session?.created_at || 0
  if (!ts) return ''
  try {
    const d = new Date(ts * 1000)
    if (Number.isNaN(d.getTime())) return ''
    const now = new Date()
    const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate())
    const dayDiff = Math.round((startOf(now) - startOf(d)) / 86400000)
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (dayDiff === 0) return t('assistant.sessionTimeToday', { time: hm })
    if (dayDiff === 1) return t('assistant.sessionTimeYesterday', { time: hm })
    if (dayDiff > 1 && dayDiff < 7) return t('assistant.sessionTimeDaysAgo', { n: dayDiff })
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + hm
  } catch {
    return ''
  }
}

function tabDisplayName(tab) {
  if (!tab) return ''
  if (tab.name && tab.name !== '加载中...' && tab.name !== '新会话') return tab.name
  if (tab.name === '加载中...') return t('assistant.sessionLoading')
  if (tab.name === '新会话') return t('assistant.sessionDefaultName')
  return t('assistant.sessionFallbackName', { n: tabs.indexOf(tab) + 1 })
}

function isDefaultTabName(name) {
  return !name || name === '新会话' || name === '加载中...'
}

/** Empty "新会话" tab with nothing drafted — safe to replace when opening history. */
function isDiscardableNewTab(tab) {
  if (!tab) return false
  if (tab.sending || tab.confirmingAction) return false
  if (tab.pendingTool || (tab.pendingTools?.length > 0)) return false
  if (tab.queuedInputs?.length > 0) return false
  if (tab.pendingAttachments?.length > 0) return false
  if (tab.pendingVoice) return false
  if (tab.previewSelectionContext) return false
  if (Array.isArray(tab.messages) && tab.messages.length > 0) return false
  if (!isDefaultTabName(tab.name)) return false
  const draftText = composeInputForSend(tab.inputDraft || '', tab.inputPathRefs || {})
  return !draftText
}

function deriveSessionName(text, attachments = [], voice = null) {
  const cleaned = String(text || '').replace(/\s+/g, ' ').trim()
  if (cleaned) return cleaned.slice(0, 30) + (cleaned.length > 30 ? '...' : '')
  if (voice) return t('assistant.voiceMessage')
  const firstAttachment = Array.isArray(attachments) ? attachments.find(a => a?.name || a?.type) : null
  if (firstAttachment?.name) {
    const name = String(firstAttachment.name).trim()
    return name.slice(0, 30) + (name.length > 30 ? '...' : '')
  }
  return '新会话'
}

function tabBadgeStatus(tab) {
  return tab?.badge?.status || ''
}

function tabBadgeUnread(tab) {
  return tab?.badge?.unread || 0
}

const canOpenNewTab = computed(() => tabs.length < MAX_TABS)
const editingSessionId = ref('')
const sessionEditName = ref('')
const sessionEditNote = ref('')
const savingSessionMeta = ref(false)
const sessionFilter = ref('')
const filteredSessions = computed(() => {
  const q = sessionFilter.value.trim().toLowerCase()
  const list = sessions.value || []
  if (!q) return list
  return list.filter((s) => {
    const name = String(s?.name || '').toLowerCase()
    const note = String(s?.note || '').toLowerCase()
    const id = String(s?.id || '').toLowerCase()
    return name.includes(q) || note.includes(q) || id.includes(q)
  })
})

function addMsg(role, text, toolCalls, options, blocks, toolTrace, attachments, tab = activeTab.value, extra = {}) {
  if (!tab) return -1
  const list = Array.isArray(tab.messages) ? tab.messages : []
  const { selectionReference, voice, ...restExtra } = extra || {}
  // AI UI Protocol v2: stamp every block/attachment with id/version/source so
  // singleton dedup and v-for keys stay stable for the lifetime of the chat.
  const normalizedBlocks = normalizeBlocks(blocks || [])
  const normalizedAttachments = (attachments || []).map(a => normalizeAttachment(a))
  const normalizedVoice = voice ? normalizeVoice(voice) : null
  list.push({
    _uid: newMessageUid(),
    role,
    text,
    status: '',
    startedAt: null,
    toolCalls: toolCalls || [],
    options: options || [],
    blocks: normalizedBlocks,
    toolTrace: toolTrace || [],
    attachments: normalizedAttachments,
    voice: normalizedVoice,
    ts: Date.now(),
    ...(selectionReference ? { selectionReference } : {}),
    ...restExtra,
  })
  tab.messages = list
  adjustRenderWindowAfterAppendTab(tab)
  if (tab === activeTab.value) {
    scrollBottom({ force: role === 'user' || autoFollowScroll.value })
  } else {
    tab.badge.unread = (tab.badge.unread || 0) + 1
    recomputeBadge(tab)
  }
  return list.length - 1
}

function isUserDecisionBlock(block) {
  return block?.type === 'user_decision' || block?.type === 'user_decision_batch'
}

function isPendingUserDecisionBlock(block) {
  return isUserDecisionBlock(block) && !block.submitted_response
}

function messageHasUserDecisionBlock(message) {
  return Array.isArray(message?.blocks) && message.blocks.some(b => isUserDecisionBlock(b))
}

// All reference-context block headers `buildPreviewSelectionContext` may
// emit. Keep this list in sync with that builder so a reloaded session
// never shows the raw context dump in place of the user's prompt.
const REFERENCE_CONTEXT_HEADERS = ['选区上下文', '图片引用上下文', '对话引用上下文']
const REFERENCE_CONTEXT_TAIL_RE = new RegExp(
  `\\n{0,2}\\[(?:${REFERENCE_CONTEXT_HEADERS.join('|')})\\][\\s\\S]*$`,
  'u',
)
const MULTIMODAL_ENRICHMENT_TAIL_RE = /\n{0,2}---\n\*\*多模态辅助理解\*\*[\s\S]*$/u

function decisionCardQuestion(message) {
  const block = (message?.blocks || []).find(b => b && isUserDecisionBlock(b))
  return String(block?.question || block?.title || '').trim()
}

function stripDuplicateDecisionPrompt(cleaned, message) {
  if (!cleaned || !messageHasUserDecisionBlock(message)) return cleaned
  // Synthetic hard-stop bodies that only repeat the card.
  if (/^已暂停，等待你的选择。$/.test(cleaned)) return ''
  if (/^已执行工具：.+。等待你的选择。$/.test(cleaned)) return ''
  const q = decisionCardQuestion(message)
  if (!q) return cleaned
  if (cleaned === q) return ''
  if (cleaned.endsWith('\n' + q)) return cleaned.slice(0, -(q.length + 1)).trim()
  if (cleaned.endsWith(q) && cleaned.length > q.length) {
    const before = cleaned.slice(0, -q.length).trim()
    if (before) return before
  }
  return cleaned
}

function visibleMessageText(message) {
  const text = String(message?.text || '')
  if (!text.trim()) return ''
  // Hard-stop prefix is a history marker, not user-facing copy. The essay
  // that belongs with the decision card (lists, titles, analysis) lives in
  // this same message — dropping the whole body left users staring at
  // "等你决定" with nothing to decide on.
  let cleaned = text.replace(/^\s*\[等待用户决定\]\s*/u, '')
  cleaned = cleaned.replace(REFERENCE_CONTEXT_TAIL_RE, '').replace(MULTIMODAL_ENRICHMENT_TAIL_RE, '').trim()
  if (messageVoice(message)) return ''
  const atts = message?.attachments || []
  const hasImage = atts.some(a => (a?.type || a?.kind) === 'image')
  const hasVideo = atts.some(a => (a?.type || a?.kind) === 'video')
  if (hasImage) cleaned = cleaned.replace(/\[(?:image|img)\]/gi, '')
  if (hasVideo) cleaned = cleaned.replace(/\[media\]/gi, '')
  cleaned = cleaned.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim()
  return stripDuplicateDecisionPrompt(cleaned, message)
}

function selectionReferenceLabel(selection) {
  if (!selection) return ''
  if (selection.kind === 'message') {
    const role = selection.role === 'assistant'
      ? t('assistant.assistantReply')
      : t('assistant.userMessage')
    const ordinal = Number.isFinite(selection.message_index) ? `#${selection.message_index + 1}` : ''
    const range = selection.start_index != null
      ? t('assistant.characterRange', { start: selection.start_index + 1, end: selection.end_index })
      : ''
    return [role + ordinal, range].filter(Boolean).join(' · ')
  }
  if (selection.kind === 'image_attachment') {
    // Compact display: "图片 · screenshot.png" or just the filename when
    // there's no room.
    return selection.attachment_name
      ? t('assistant.imageReferenceNamed', { name: selection.attachment_name })
      : t('assistant.imageReference')
  }
  const file = String(selection.file_name || selection.file_path || '').trim()
  const range = selection.start_line
    ? `L${selection.start_line}:C${selection.start_column} - L${selection.end_line}:C${selection.end_column}`
    : ''
  return [file, range].filter(Boolean).join(' · ')
}

function selectionReferenceFromText(text) {
  const raw = String(text || '')
  // Image references — produced by `[图片引用上下文]`, no file/range fields,
  // just the `图片:` line plus an optional `来源:` ordinal.
  if (raw.includes('[图片引用上下文]')) {
    const name = raw.match(/^图片:\s*(.+)$/mu)?.[1]?.trim() || ''
    return name
      ? t('assistant.imageReferenceNamed', { name })
      : t('assistant.imageReference')
  }
  // Conversation-text references — produced by `[对话引用上下文]`. We only
  // surface the source line so the chip stays short ("来源: user" → "用户消息").
  if (raw.includes('[对话引用上下文]')) {
    const ref = raw.match(/^引用:\s*(.+)$/mu)?.[1]?.trim() || ''
    if (ref) return ref
    const role = raw.match(/^来源:\s*(.+)$/mu)?.[1]?.trim() || ''
    if (role === 'assistant') return t('assistant.assistantReply')
    if (role === 'user') return t('assistant.userMessage')
    return t('assistant.conversationReference')
  }
  // File / sandbox-preview selection — original `[选区上下文]` format.
  if (raw.includes('[选区上下文]')) {
    const file = raw.match(/^文件:\s*(.+)$/mu)?.[1]?.trim() || ''
    const range = raw.match(/^范围:\s*(.+)$/mu)?.[1]?.trim() || ''
    return [file, range].filter(Boolean).join(' · ')
  }
  return ''
}

function messageSelectionReference(message) {
  const ref = message?.selectionReference
  // AI UI Protocol v2: `selection_reference` is structured metadata, so the
  // chip label is derived from the typed fields. The string branch keeps
  // working in case some in-memory site still hands us a pre-rendered label.
  if (ref && typeof ref === 'object') return selectionReferenceLabel(ref)
  if (typeof ref === 'string' && ref) return ref
  // Legacy v1 sessions stashed the dump inside ``text``. Fall back to the
  // text-parsing heuristic so the chip still shows up after restart.
  return selectionReferenceFromText(message?.text) || ''
}

function enterUserDecisionWaitState(tab, index = -1) {
  if (!tab) return
  const target = index >= 0 && index < tab.messages.length ? index : tab.messages.length - 1
  if (target >= 0) {
    const msg = tab.messages[target]
    tab.messages[target] = { ...msg, status: '', startedAt: null }
  }
  tab.sending = false
  tab.activeStreamMessage = false
  recomputeBadge(tab)
}

function markUserDecisionSubmitted(block, response) {
  const tab = activeTab.value
  if (!tab) return
  const text = String(response || '').trim()
  const targetKey = userDecisionBlockKey(block)
  let updated = false
  for (let mi = tab.messages.length - 1; mi >= 0; mi -= 1) {
    const msg = tab.messages[mi]
    const blocks = Array.isArray(msg?.blocks) ? msg.blocks.slice() : []
    let mutated = false
    for (let bi = 0; bi < blocks.length; bi += 1) {
      const b = blocks[bi]
      const sameDecision = b === block || (
        isUserDecisionBlock(b)
        && !b.submitted_response
        && (
          (targetKey && userDecisionBlockKey(b) === targetKey)
          || (b.question === block?.question && b.context === block?.context)
        )
      )
      if (!sameDecision) continue
      blocks[bi] = {
        ...b,
        submitted_response: text,
        submitted_at: Date.now(),
      }
      mutated = true
      updated = true
    }
    if (mutated) tab.messages[mi] = { ...msg, blocks }
  }
  if (updated) recomputeBadge(tab)
}

const nowMs = ref(Date.now())
let elapsedTicker = null

function hasAnyActiveStatusMessage() {
  for (const tab of tabs) {
    for (const msg of tab.messages || []) {
      if (msg?.status && msg?.startedAt) return true
    }
  }
  return false
}

function startElapsedTicker() {
  if (elapsedTicker) return
  elapsedTicker = setInterval(() => { nowMs.value = Date.now() }, 1000)
}
function stopElapsedTicker() {
  if (elapsedTicker) { clearInterval(elapsedTicker); elapsedTicker = null }
}

function messageAttachmentMemo(m) {
  return (m.attachments || []).map(a => attachmentThumbUrl(a) || a.id || a.sandbox_rel || '').join('|')
}

function messageRenderMemo(m, absoluteIndex) {
  // Even a sealed bubble must re-render when its timeline step is expanded /
  // collapsed (manual toggle, or it stops being the live round), so the open
  // state is part of the memo key.
  const isEditing = editingUserMessageUid.value === m._uid
  if (isMessageSealed(m, absoluteIndex)) {
    return [m._uid, stepOpenState(m, absoluteIndex), isEditing, isEditing ? editingUserMessageText.value : '', messageAttachmentMemo(m)]
  }
  const deps = [
    m._uid,
    m.text,
    m.status,
    m.blocks,
    m.attachments?.length,
    messageAttachmentMemo(m),
    m.voice?.id,
    m.voice?.duration_ms,
    m.voice?.sandbox_rel,
    sending.value,
    confirmingAction.value,
    pendingTools.value.length,
    stepOpenState(m, absoluteIndex),
    isEditing,
    isEditing ? editingUserMessageText.value : '',
  ]
  // v-memo must depend on nowMs while a status row is live; otherwise the
  // elapsed label only jumps when status text changes (~5s stall hints).
  if (m.status && m.startedAt) deps.push(nowMs.value)
  if (m.statusTimeoutSeconds) deps.push(m.statusTimeoutSeconds)
  return deps
}

function formatElapsed(startedAt, now) {
  if (!startedAt) return ''
  const ms = Math.max(0, (now || Date.now()) - startedAt)
  if (ms < 1000) return ''
  const s = Math.floor(ms / 1000)
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  const rs = s % 60
  return m + 'm' + (rs ? ' ' + rs + 's' : '')
}

function formatToolElapsed(startedAt, now, limitSeconds) {
  const elapsed = formatElapsed(startedAt, now)
  if (!elapsed) return ''
  const limit = Number(limitSeconds || 0)
  if (limit >= 30) return `${elapsed} / ${limit}s`
  return elapsed
}

function adjustRenderWindowAfterAppendTab(tab) {
  if (!tab) return
  if (tab.showAllMessages) {
    tab.renderWindowStart = 0
    return
  }
  const len = tab.messages.length
  if (len <= RENDER_WINDOW_TRIGGER) {
    tab.renderWindowStart = 0
    return
  }
  const desired = Math.max(0, len - RENDER_WINDOW_SIZE)
  if (desired > tab.renderWindowStart) {
    tab.renderWindowStart = desired
  }
}

function loadEarlierMessages() {
  if (renderWindowStart.value <= 0) return
  renderWindowStart.value = Math.max(0, renderWindowStart.value - RENDER_WINDOW_STEP)
}

function showAllEarlierMessages() {
  showAllMessages.value = true
  renderWindowStart.value = 0
}

const renderedMessages = computed(() => {
  const all = messages.value
  if (renderWindowStart.value <= 0) return all
  return all.slice(renderWindowStart.value)
})

const hiddenEarlierCount = computed(() => renderWindowStart.value)

function isMessageSealed(message, indexWithinWindow) {
  if (!message) return true
  if (pendingTools.value.length || confirmingAction.value) {
    if (indexWithinWindow === renderedMessages.value.length - 1) return false
  }
  if (activeStreamMessage.value && message.role === 'assistant') {
    const all = messages.value
    if (message === all[all.length - 1]) return false
  }
  if (sending.value && indexWithinWindow === renderedMessages.value.length - 1) return false
  return true
}

const TASK_MUTATING_TOOLS = new Set([
  'create_scan', 'rescan_task', 'cancel_task', 'delete_task',
])

function dispatchTasksChangedIfNeeded(result) {
  const trace = result?.tool_trace || []
  if (!trace.some(t => TASK_MUTATING_TOOLS.has(t.name))) return
  try {
    window.dispatchEvent(new CustomEvent('argusai:tasks-changed', {
      detail: { source: 'assistant' },
    }))
  } catch (_) { /* ignore */ }
}

function dispatchSandboxFilesChangedIfNeeded(result, tab) {
  if (!tab?.sessionId) return
  const trace = result?.tool_trace || []
  if (!trace.some(t => SANDBOX_MUTATING_TOOLS.has(t.name))) return
  sandboxBrowser.notifySandboxFilesChanged(tab.sessionId, 'assistant')
}

function notifySandboxTreeMaybeChanged(tab, source) {
  if (!tab?.sessionId) return
  sandboxBrowser.notifySandboxFilesChanged(tab.sessionId, source || 'assistant')
}

const attachmentPreviewUrls = ref({})

function attachmentPreviewKey(att) {
  if (!att) return ''
  return String(att.id || att.sandbox_rel || att.sandbox_path || att.path || att.name || '')
}

function attachmentThumbUrl(att) {
  if (!att) return ''
  if (att.preview_url) return att.preview_url
  const key = attachmentPreviewKey(att)
  if (key && attachmentPreviewUrls.value[key]) return attachmentPreviewUrls.value[key]
  if (att.data_base64 && att.mime) return `data:${att.mime};base64,${att.data_base64}`
  if (att.data_url && String(att.data_url).startsWith('data:')) return att.data_url
  if (att.url && /^(https?:|data:)/i.test(String(att.url))) return String(att.url)
  return ''
}

async function ensureAttachmentPreviewUrl(att, sid) {
  const existing = attachmentThumbUrl(att)
  if (existing) return existing
  const path = String(att?.sandbox_rel || att?.sandbox_path || att?.path || att?.url || '').trim()
  if (!path || /^(javascript:|vbscript:|file:)/i.test(path)) return ''
  if (/^https?:\/\//i.test(path)) {
    const key = attachmentPreviewKey(att)
    if (key) attachmentPreviewUrls.value = { ...attachmentPreviewUrls.value, [key]: path }
    return path
  }
  try {
    const r = await api.readMediaFile(path, sid || sessionId.value || null, 0, 65536)
    if (r?.ok && r.content && (r.kind === 'image' || String(r.content).startsWith('data:image/'))) {
      const key = attachmentPreviewKey(att)
      if (key) attachmentPreviewUrls.value = { ...attachmentPreviewUrls.value, [key]: r.content }
      return r.content
    }
  } catch { /* thumb stays empty until the user opens the preview */ }
  return ''
}

function hydrateHistoryAttachments(list, sid) {
  for (const message of list || []) {
    for (const att of message.attachments || []) {
      if ((att.type === 'image' || att.kind === 'image') && !attachmentThumbUrl(att)) {
        ensureAttachmentPreviewUrl(att, sid)
      }
    }
  }
}

const voicePreviewUrls = ref({})

function voicePreviewCacheKey(voice) {
  if (!voice) return ''
  return String(voice.id || voice.sandbox_rel || voice.sandbox_path || '')
}

function cachedVoicePreviewUrl(voice) {
  const key = voicePreviewCacheKey(voice)
  if (!key) return ''
  return voicePreviewUrls.value[key] || ''
}

async function ensureVoicePreviewUrl(voice) {
  const normalized = normalizeVoice(voice)
  if (!normalized) return ''
  const key = voicePreviewCacheKey(normalized)
  if (!key) return ''
  const cached = voicePreviewUrls.value[key]
  if (cached) return cached
  if (normalized.data_base64 && normalized.mime) {
    const url = `data:${normalized.mime};base64,${normalized.data_base64}`
    voicePreviewUrls.value = { ...voicePreviewUrls.value, [key]: url }
    return url
  }
  const rel = String(normalized.sandbox_rel || '').trim()
  const sid = sessionId.value
  if (!rel || !sid) return ''
  try {
    const r = await api.readAssistantSandboxFile(sid, rel, 0, true)
    if (r?.ok && r.content) {
      const mime = normalized.mime || 'audio/webm'
      const url = `data:${mime};base64,${r.content}`
      voicePreviewUrls.value = { ...voicePreviewUrls.value, [key]: url }
      return url
    }
  } catch {
    // ignore — bubble stays disabled until a retry succeeds
  }
  return ''
}

function voiceBubblePlayable(voice) {
  const normalized = normalizeVoice(voice)
  if (!normalized) return false
  return Boolean(
    cachedVoicePreviewUrl(normalized)
    || normalized.data_base64
    || normalized.sandbox_rel,
  )
}

function messageVoice(message) {
  // Both `addMsg` and `normalizeHistory` already pipe voice through
  // `normalizeVoice`, so we MUST return the same object reference each call.
  // Re-normalizing inline here would mint a fresh `{...voice}` (and a new id
  // when missing) on every template read — `messageVoice(m)` is invoked 4-6
  // times per voice bubble per render, which used to defeat `v-memo` and
  // poison the `voiceAudioEls` Map with new keys every tick.
  if (message?.voice) return message.voice
  const atts = message?.attachments || []
  if (atts.length === 1 && atts[0]?.type === 'audio') {
    const text = visibleMessageText(message)
    if (!text) return atts[0]
  }
  return null
}

function messageDisplayAttachments(message) {
  const voice = messageVoice(message)
  const atts = message?.attachments || []
  if (!voice) return atts
  return atts.filter(a => a?.type !== 'audio' && a?.id !== voice.id)
}

function wireOutboundVoice(voice) {
  const normalized = normalizeVoice(voice)
  if (!normalized) return null
  const payload = {
    mime: normalized.mime,
    data_base64: normalized.data_base64,
  }
  if (normalized.duration_ms) payload.duration_ms = normalized.duration_ms
  return payload
}

function clearPendingVoice(tab = activeTab.value) {
  if (!tab) return
  tab.pendingVoice = null
}

const playingVoiceKey = ref(null)
const voiceAudioEls = new Map()

function voiceAttachmentKey(scope, index, att) {
  if (att?.id) return String(att.id)
  return `${scope}-${index}`
}

function registerVoiceAudioEl(key, el) {
  if (el) voiceAudioEls.set(key, el)
  else voiceAudioEls.delete(key)
}

function stopOtherVoicePlayers(exceptKey) {
  for (const [k, audio] of voiceAudioEls) {
    if (k === exceptKey || !audio) continue
    try {
      audio.pause()
      audio.currentTime = 0
    } catch {
      // ignore
    }
  }
}

async function toggleVoicePlayback(key, src, voice = null) {
  let playSrc = src || cachedVoicePreviewUrl(voice)
  if (!playSrc && voice) {
    playSrc = await ensureVoicePreviewUrl(voice)
  }
  const audio = voiceAudioEls.get(key)
  if (!audio || !playSrc) return
  if (playingVoiceKey.value === key && !audio.paused) {
    audio.pause()
    playingVoiceKey.value = null
    return
  }
  stopOtherVoicePlayers(key)
  if (audio.src !== playSrc) audio.src = playSrc
  playingVoiceKey.value = key
  audio.onended = () => {
    if (playingVoiceKey.value === key) playingVoiceKey.value = null
  }
  try {
    await audio.play()
  } catch {
    playingVoiceKey.value = null
  }
}

function newAttachmentId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2)
}

function normalizeImageMime(raw) {
  let mime = String(raw || '').toLowerCase()
  if (mime === 'image/jpg') mime = 'image/jpeg'
  return mime
}

async function fileToImageAttachment(file, hintedMime = '') {
  let mime = normalizeImageMime(file.type || hintedMime)
  if (!mime && isImageFile(file)) {
    const ext = fileNameExt(file.name || '')
    mime = ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg' : `image/${ext}`
  }
  // Clipboard screenshots often have empty type/name in WebView/Chromium.
  if (!mime) mime = 'image/png'
  if (!ATTACHMENT_ACCEPT_MIMES.includes(mime)) {
    console.warn('AssistantPanel: unsupported image type', mime)
    return null
  }
  if (file.size > ATTACHMENT_MAX_BYTES) {
    console.warn('AssistantPanel: image too large', file.size)
    return null
  }
  const buf = await file.arrayBuffer()
  const b64 = bytesToBase64(new Uint8Array(buf))
  return {
    id: newAttachmentId(),
    type: 'image',
    mime,
    data_base64: b64,
    name: file.name || '',
    size: file.size,
  }
}

async function fileToAudioAttachment(file, hintedMime = '', extra = {}) {
  let mime = normalizeAudioMime(file.type || hintedMime)
  if (!mime && isAudioFile(file)) {
    const ext = fileNameExt(file.name || '')
    if (ext === 'mp3') mime = 'audio/mpeg'
    else if (ext === 'm4a') mime = 'audio/mp4'
    else if (ext) mime = `audio/${ext}`
  }
  if (!mime) mime = 'audio/webm'
  if (!ATTACHMENT_AUDIO_ACCEPT_MIMES.includes(mime) && !mime.startsWith('audio/')) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.audioUnsupportedType', { mime }),
    }
  }
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.audioTooLarge', {
        size: humanSize(file.size),
        max: humanSize(ATTACHMENT_MAX_BYTES),
      }),
    }
  }
  const buf = await file.arrayBuffer()
  const b64 = bytesToBase64(new Uint8Array(buf))
  return {
    id: newAttachmentId(),
    type: 'audio',
    mime,
    data_base64: b64,
    name: file.name || extra.name || 'recording.webm',
    size: file.size,
    duration_ms: Number(extra.duration_ms || extra.durationMs || 0) || undefined,
  }
}

const VIDEO_DURATION_PROBE_TIMEOUT_MS = 1500

// Duration is a nice-to-have label, not part of the payload contract: some
// WebView builds ship no decoder for a given container and `loadedmetadata`
// then never fires. Resolve 0 on timeout/error so attaching never stalls.
function probeVideoDurationMs(file) {
  return new Promise(resolve => {
    let settled = false
    let url = ''
    let el = null
    let timer = null
    const done = (ms) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      if (el) {
        el.onloadedmetadata = null
        el.onerror = null
        try {
          el.removeAttribute('src')
          el.load()
        } catch {
          // ignore
        }
      }
      if (url) URL.revokeObjectURL(url)
      resolve(ms)
    }
    try {
      url = URL.createObjectURL(file)
      el = document.createElement('video')
      el.preload = 'metadata'
      el.muted = true
      el.onloadedmetadata = () => {
        const sec = Number(el?.duration)
        done(Number.isFinite(sec) && sec > 0 ? Math.round(sec * 1000) : 0)
      }
      el.onerror = () => done(0)
      timer = setTimeout(() => done(0), VIDEO_DURATION_PROBE_TIMEOUT_MS)
      el.src = url
    } catch {
      done(0)
    }
  })
}

async function fileToVideoAttachment(file, hintedMime = '') {
  const mime = normalizeVideoMime(file.type || hintedMime, file.name || '')
  if (!ATTACHMENT_VIDEO_ACCEPT_MIMES.includes(mime)) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.videoUnsupportedType', { mime: mime || t('assistant.attachmentTypeUnknown') }),
    }
  }
  if (file.size > ATTACHMENT_VIDEO_MAX_BYTES) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.videoTooLarge', { size: humanSize(file.size), max: humanSize(ATTACHMENT_VIDEO_MAX_BYTES) }),
    }
  }
  let durationMs = 0
  try {
    durationMs = await probeVideoDurationMs(file)
  } catch {
    durationMs = 0
  }
  const buf = await file.arrayBuffer()
  const b64 = bytesToBase64(new Uint8Array(buf))
  return {
    id: newAttachmentId(),
    type: 'video',
    mime,
    data_base64: b64,
    name: file.name || '',
    size: file.size,
    duration_ms: durationMs || undefined,
  }
}

function videoAttachmentMeta(att) {
  const parts = []
  if (att?.size != null) parts.push(humanSize(att.size))
  if (att?.duration_ms) parts.push(formatVoiceMs(att.duration_ms))
  return parts.filter(Boolean).join(' · ')
}

async function blobToAudioAttachment(blob, { mime = '', name = '', durationMs = 0 } = {}) {
  if (!blob?.size) return null
  const file = new File([blob], name || 'recording.webm', { type: mime || blob.type || 'audio/webm' })
  return fileToAudioAttachment(file, mime || blob.type, { name, duration_ms: durationMs })
}

async function fileToTextAttachment(file) {
  if (file.size > ATTACHMENT_TEXT_MAX_BYTES) {
    console.warn('AssistantPanel: text file too large', file.size)
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.textFileTooLarge', {
        size: humanSize(file.size),
        max: humanSize(ATTACHMENT_TEXT_MAX_BYTES),
      }),
    }
  }
  let text = ''
  try {
    text = await file.text()
  } catch (e) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.utf8DecodeFailed'),
    }
  }
  return {
    id: newAttachmentId(),
    type: 'text',
    mime: (file.type || '').toLowerCase() || guessMimeFromExt(file.name || ''),
    text,
    name: file.name || '',
    size: file.size,
  }
}

function guessMimeFromExt(name) {
  const ext = fileNameExt(name)
  if (!ext) return 'text/plain'
  if (ext === 'md' || ext === 'markdown' || ext === 'mdx') return 'text/markdown'
  if (ext === 'json' || ext === 'jsonl' || ext === 'ndjson') return 'application/json'
  if (ext === 'yaml' || ext === 'yml') return 'application/yaml'
  if (ext === 'js' || ext === 'mjs' || ext === 'cjs' || ext === 'jsx') return 'application/javascript'
  if (ext === 'ts' || ext === 'tsx') return 'application/typescript'
  if (ext === 'py' || ext === 'pyw') return 'text/x-python'
  if (ext === 'sh' || ext === 'bash' || ext === 'zsh') return 'application/x-shellscript'
  if (ext === 'html' || ext === 'htm') return 'text/html'
  if (ext === 'xml' || ext === 'svg') return 'application/xml'
  if (ext === 'css' || ext === 'scss' || ext === 'sass') return 'text/css'
  if (ext === 'csv' || ext === 'tsv') return 'text/csv'
  return 'text/plain'
}

async function fileToBinaryAttachment(file) {
  if (file.size > ATTACHMENT_FILE_MAX_BYTES) {
    return {
      id: newAttachmentId(),
      type: 'error',
      name: file.name || '',
      size: file.size,
      error: t('assistant.fileTooLarge', {
        size: humanSize(file.size),
        max: humanSize(ATTACHMENT_FILE_MAX_BYTES),
      }),
    }
  }
  const buf = await file.arrayBuffer()
  return {
    id: newAttachmentId(),
    type: 'file',
    mime: (file.type || '').toLowerCase() || 'application/octet-stream',
    data_base64: bytesToBase64(new Uint8Array(buf)),
    name: file.name || '',
    size: file.size,
  }
}

async function fileToAttachment(file, hintedMime = '') {
  if (!file) return null
  const mime = normalizeImageMime(file.type || hintedMime)
  if (mime.startsWith('image/') || isImageFile(file)) return fileToImageAttachment(file, mime)
  // Must precede the audio/text branches: `.webm` is shared with audio and
  // `isTextLikeFile` would otherwise claim extension-less containers.
  if (isVideoFile(file) || normalizeVideoMime(file.type || hintedMime, file.name || '').startsWith('video/')) {
    return fileToVideoAttachment(file, hintedMime)
  }
  if (isAudioFile(file) || normalizeAudioMime(file.type || hintedMime).startsWith('audio/')) {
    return fileToAudioAttachment(file, hintedMime)
  }
  if (isTextLikeFile(file)) return fileToTextAttachment(file)
  return fileToBinaryAttachment(file)
}

function pathRefAttachment(file, path) {
  return {
    id: newAttachmentId(),
    type: 'file',
    mime: (file?.type || '').toLowerCase() || 'application/octet-stream',
    name: file?.name || pathDisplayName(path) || path,
    size: Number(file?.size) || 0,
    host_path: path,
    path,
    fallback_mode: 'path',
  }
}

async function addFiles(files, options = {}) {
  if (!files || !files.length) return
  const hintMimes = options.hintMimes || []
  const preferPath = options.preferPath !== false
  let added = 0
  for (let i = 0; i < files.length; i += 1) {
    const file = files[i]
    const hostPath = preferPath ? fileSystemPathFromFile(file) : ''
    const media = isImageFile(file) || isVideoFile(file) || isAudioFile(file)
    if (hostPath && !media) {
      if (pendingAttachments.value.length >= ATTACHMENT_MAX_COUNT) {
        console.warn('AssistantPanel: attachment limit reached')
        break
      }
      pendingAttachments.value = [...pendingAttachments.value, pathRefAttachment(file, hostPath)]
      added += 1
      continue
    }
    let att = await fileToAttachment(file, hintMimes[i] || '')
    if (!att) continue
    if (hostPath) {
      att.host_path = hostPath
      att.path = hostPath
    }
    att = applyAcpImageFallback(att)
    if (att.type === 'audio') {
      tabPendingVoiceFromAttachment(att)
      added += 1
      continue
    }
    if (pendingAttachments.value.length >= ATTACHMENT_MAX_COUNT) {
      console.warn('AssistantPanel: attachment limit reached')
      break
    }
    pendingAttachments.value = [...pendingAttachments.value, att]
    added += 1
  }
  return added
}

function tabPendingVoiceFromAttachment(att) {
  if (!att || att.type === 'error') {
    showToast(att?.error || t('assistant.voiceFailed'), 'error')
    return
  }
  pendingVoice.value = normalizeVoice(att)
  showToast(t('assistant.voiceReady'), 'ok')
}

function removeAttachment(id) {
  pendingAttachments.value = pendingAttachments.value.filter(a => a.id !== id)
}

async function onMicClick() {
  if (!voiceIsSupported.value) {
    showToast(t('assistant.voiceUnsupported'), 'error')
    return
  }
  // The mic button now only STARTS recording. While a recording is in
  // progress, the user finishes/cancels through the inline capsule controls
  // (the X / ✓ buttons rendered in their place), not by re-clicking the
  // mic. This keeps the affordance unambiguous.
  if (voiceState.value !== 'idle') return
  if (pendingVoice.value) {
    showToast(t('assistant.voiceReplace'), 'info')
  }
  const started = await voiceInput.start()
  if (!started.ok) {
    if (started.error === 'unsupported') showToast(t('assistant.voiceUnsupported'), 'error')
    else showToast(t('assistant.voiceMicDenied'), 'error')
  }
}

async function finishVoiceRecording() {
  if (voiceState.value !== 'recording') return
  const result = await voiceInput.stop()
  if (!result.ok) {
    const errKey = result.error === 'empty' ? 'assistant.voiceEmpty' : 'assistant.voiceFailed'
    showToast(t(errKey), 'error')
    return
  }
  if (pendingVoice.value) {
    showToast(t('assistant.voiceReplace'), 'info')
  }
  const att = await blobToAudioAttachment(result.blob, {
    mime: result.mime,
    name: result.name,
    durationMs: result.durationMs,
  })
  if (!att || att.type === 'error') {
    showToast(att?.error || t('assistant.voiceFailed'), 'error')
    return
  }
  pendingVoice.value = normalizeVoice(att)
  // "Tap ✓ = send now" matches the historical product intent for the mic
  // affordance. Only when there's already typed text / other attachments
  // queued, or the assistant is mid-turn, do we fall back to leaving the
  // voice bubble in the composer so the user can decide what to do next.
  const tab = activeTab.value
  const hasOtherInput = Boolean(
    (tab?.inputDraft && composeInputForSend(tab.inputDraft, tab.inputPathRefs || {}).trim())
    || (tab?.pendingAttachments || []).length > 0,
  )
  if (hasOtherInput) {
    showToast(t('assistant.voiceReady'), 'ok')
    return
  }
  if (tab?.sending || tab?.confirmingAction) {
    showToast(t('assistant.voiceWaitTurn'), 'info')
    return
  }
  await nextTick()
  await send()
}

function cancelVoiceRecording() {
  if (voiceState.value !== 'recording') return
  voiceInput.cancel()
  showToast(t('assistant.voiceCancelled'), 'info')
}

function openMessageAttachment(att) {
  if (!att || att.type !== 'image') return
  const source = attachmentThumbUrl(att)
  if (source) {
    mediaPreview.openPayload({
      kind: 'image',
      content: source,
      name: att.name || 'image.png',
      mime: att.mime || 'image/png',
      size: att.size || 0,
      sessionId: sessionId.value || '',
    })
    return
  }
  const path = String(att.sandbox_rel || att.sandbox_path || att.path || att.url || '').trim()
  if (path && !/^(javascript:|vbscript:)/i.test(path)) {
    mediaPreview.openFile(path, { name: att.name || path, sessionId: sessionId.value || '' })
  }
}

// Resolve the raw base64 body (no `data:` prefix) of an image attachment.
// Returns an empty string when the attachment carries neither inline base64
// nor a fetchable URL.
function attachmentRawBase64(att) {
  if (!att) return ''
  if (att.data_base64) return String(att.data_base64)
  const dataUrl = att.data_url || ''
  if (typeof dataUrl === 'string' && dataUrl.startsWith('data:')) {
    const comma = dataUrl.indexOf(',')
    return comma > -1 ? dataUrl.slice(comma + 1) : ''
  }
  return ''
}

async function fetchAttachmentAsBase64(url) {
  const blob = await (await fetch(url)).blob()
  const base64 = await new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => {
      const result = String(r.result || '')
      const comma = result.indexOf(',')
      resolve(comma > -1 ? result.slice(comma + 1) : '')
    }
    r.onerror = () => reject(r.error || new Error('FileReader error'))
    r.readAsDataURL(blob)
  })
  return { base64, mime: blob.type || '' }
}

// Option B: copy a chat-message image into the input as a new attachment.
// The AI sees the image as a fresh upload alongside the next user prompt.
async function copyMessageImageToInput(att) {
  if (!att) return
  if (att.type !== 'image' && att.kind !== 'image') {
    showToast(t('assistant.imageAttachmentsOnly'), 'error')
    return
  }
  let b64 = attachmentRawBase64(att)
  let mime = att.mime || 'image/png'
  if (!b64) {
    const url = att.url || att.preview_url || ''
    if (!url) {
      showToast(t('assistant.imageDataUnavailable'), 'error')
      return
    }
    try {
      const fetched = await fetchAttachmentAsBase64(url)
      b64 = fetched.base64
      if (fetched.mime) mime = fetched.mime
    } catch (e) {
      showToast(t('assistant.imageReadFailed', { error: e?.message || e }), 'error')
      return
    }
  }
  if (!b64) {
    showToast(t('assistant.imageDataEmpty'), 'error')
    return
  }
  window.dispatchEvent(new CustomEvent('argusai:add-assistant-attachment', {
    detail: {
      attachment: {
        type: 'image',
        mime,
        data_base64: b64,
        name: att.name || 'referenced-image.png',
        size: att.size || 0,
      },
    },
  }))
}

// Option C: mark a chat-message image as a *reference* — no re-upload, just
// a tag in the prompt envelope telling the AI "the image you saw in turn #N".
function referenceMessageImage(att, message, absoluteIndex) {
  if (!att) return
  if (att.type !== 'image' && att.kind !== 'image') return
  const name = String(att.name || '').trim() || t('assistant.imageDefaultName')
  previewSelectionContext.value = {
    kind: 'image_attachment',
    role: message?.role === 'assistant' ? 'assistant' : 'user',
    message_index: Number.isFinite(absoluteIndex) ? absoluteIndex : null,
    attachment_id: String(att.id || ''),
    attachment_name: name,
    attachment_mime: att.mime || '',
    // `text` is the canary the rest of the codebase checks to decide whether
    // a selection is "active". For images we keep it human-readable so the
    // legacy `selectionReferenceFromText` fallback still produces something
    // sensible if it ever runs.
    text: name,
  }
  showToast(t('assistant.imageReferenced'), 'ok')
}

// Generic image-reference entry point dispatched by `MediaPreview` for any
// image opened in the preview pane — covers markdown `<img>` clicks, block
// thumbnails, sandbox files, etc. We don't know which chat message the image
// came from in this path, so the resulting reference omits `message_index`
// and `role`.
function onReferenceImage(e) {
  const detail = e?.detail || {}
  const name = String(detail.name || '').trim() || t('assistant.imageDefaultName')
  previewSelectionContext.value = {
    kind: 'image_attachment',
    role: '',
    message_index: null,
    attachment_id: '',
    attachment_name: name,
    attachment_mime: detail.mime || '',
    attachment_source: detail.source || '',
    text: name,
  }
  showToast(t('assistant.imageReferenced'), 'ok')
}

async function editAttachment(att) {
  const tab = activeTab.value
  if (!tab || !att?.id || att.type !== 'image') return
  const source = attachmentThumbUrl(att)
  if (!source) return
  mediaPreview.openPayload({
    kind: 'image',
    content: source,
    name: att.name || 'image.png',
    mime: att.mime || 'image/png',
    size: att.size || 0,
    edit: true,
    applyTarget: 'attachment',
    context: { attachmentId: att.id },
    onApply: ({ mode, result }) => {
      if (!result?.data_base64) return
      if (result.size > ATTACHMENT_MAX_BYTES) {
        showToast(t('assistant.editedImageTooLarge', {
          size: humanSize(result.size),
          max: humanSize(ATTACHMENT_MAX_BYTES),
        }), 'error')
        return
      }
      if (mode === 'duplicate' && (tab.pendingAttachments || []).length >= ATTACHMENT_MAX_COUNT) {
        showToast(t('assistant.attachmentLimitReached', { n: ATTACHMENT_MAX_COUNT }), 'error')
        return
      }
      const nextAttachment = {
        id: mode === 'duplicate' ? newAttachmentId() : att.id,
        type: 'image',
        mime: result.mime || 'image/png',
        data_base64: result.data_base64,
        name: result.name || `${att.name || 'image'}-edited.png`,
        size: result.size,
      }
      if (mode === 'duplicate') {
        tab.pendingAttachments = [...(tab.pendingAttachments || []), nextAttachment]
        showToast(t('assistant.editedImageAdded'), 'ok')
        return
      }
      tab.pendingAttachments = (tab.pendingAttachments || []).map(a => a.id === att.id ? nextAttachment : a)
      showToast(t('assistant.editedImageReplaced'), 'ok')
    },
  })
}

function onAddAssistantAttachment(e) {
  const detail = e?.detail || {}
  const att = detail.attachment || {}
  const tab = activeTab.value
  if (!tab || att.type !== 'image' || !att.data_base64) return
  if ((tab.pendingAttachments || []).length >= ATTACHMENT_MAX_COUNT) {
    showToast(t('assistant.attachmentLimitReached', { n: ATTACHMENT_MAX_COUNT }), 'error')
    return
  }
  const size = Number(att.size || Math.round(String(att.data_base64).length * 3 / 4))
  if (size > ATTACHMENT_MAX_BYTES) {
    showToast(t('assistant.imageTooLarge', {
      size: humanSize(size),
      max: humanSize(ATTACHMENT_MAX_BYTES),
    }), 'error')
    return
  }
  const nextAttachment = applyAcpImageFallback({
    id: newAttachmentId(),
    type: 'image',
    mime: att.mime || 'image/png',
    data_base64: String(att.data_base64),
    name: att.name || 'annotated-image.png',
    size,
  })
  tab.pendingAttachments = [...(tab.pendingAttachments || []), nextAttachment]
  if (detail.appendText === true && detail.text) appendTextToInput(String(detail.text))
  if (detail.openAssistant && !props.visible) emit('toggle')
  showToast(t('assistant.annotatedImageAdded'), 'ok')
}

function onPickFiles() {
  fileInput.value?.click()
}

function onFileInput(e) {
  addFiles(Array.from(e.target.files || []))
  if (e.target) e.target.value = ''
}

function imageMimeFromClipboardItem(item) {
  const mime = normalizeImageMime(item?.type || '')
  return mime.startsWith('image/') ? mime : ''
}

function fileFromClipboardItem(item) {
  if (!item || typeof item.getAsFile !== 'function') return null
  try {
    return item.getAsFile()
  } catch {
    return null
  }
}

function collectClipboardFiles(clipboardData) {
  const files = []
  const hintMimes = []
  if (!clipboardData) return { files, hintMimes }
  const seen = new Set()
  const push = (file, hintMime = '') => {
    if (!file) return
    const key = `${file.name}|${file.size}|${file.type}|${file.lastModified}`
    if (seen.has(key)) return
    seen.add(key)
    files.push(file)
    hintMimes.push(hintMime || normalizeImageMime(file.type) || '')
  }
  const items = clipboardData.items
  if (items) {
    for (const it of items) {
      const imageMime = imageMimeFromClipboardItem(it)
      if (it.kind === 'file' || imageMime) {
        push(fileFromClipboardItem(it), imageMime)
      }
    }
  }
  if (!files.length && clipboardData.files?.length) {
    for (const f of clipboardData.files) {
      push(f, normalizeImageMime(f.type))
    }
  }
  return { files, hintMimes }
}

function extractImageDataUrlFromHtml(html) {
  const raw = String(html || '')
  const m = raw.match(/<img[^>]+src=["'](data:image\/[^"']+)["']/i)
  return m ? m[1] : ''
}

function parseDataUrlImage(dataUrl) {
  const m = String(dataUrl || '').match(/^data:(image\/[a-z0-9+.-]+);base64,([a-z0-9+/=\s]+)$/i)
  if (!m) return null
  const mime = normalizeImageMime(m[1])
  if (!ATTACHMENT_ACCEPT_MIMES.includes(mime)) return null
  return { mime, data_base64: m[2].replace(/\s+/g, '') }
}

async function addDataUrlImageAttachment(dataUrl) {
  const parsed = parseDataUrlImage(dataUrl)
  if (!parsed) return 0
  if (pendingAttachments.value.length >= ATTACHMENT_MAX_COUNT) {
    console.warn('AssistantPanel: attachment limit reached')
    return 0
  }
  const nextAttachment = applyAcpImageFallback({
    id: newAttachmentId(),
    type: 'image',
    mime: parsed.mime,
    data_base64: parsed.data_base64,
    name: 'paste.png',
    size: Math.round((parsed.data_base64.length * 3) / 4),
  })
  pendingAttachments.value = [...pendingAttachments.value, nextAttachment]
  return 1
}

async function onPasteInput(e) {
  const clipboard = e.clipboardData
  if (!clipboard) return

  const { files, hintMimes } = collectClipboardFiles(clipboard)
  if (files.length) {
    e.preventDefault()
    const added = await addFiles(files, { hintMimes })
    if (added > 0) {
      showToast(
        added > 1
          ? t('assistant.attachmentsPasted', { count: added })
          : t('assistant.imagePasted'),
        'ok',
      )
    }
    return
  }

  const dataUrl = extractImageDataUrlFromHtml(clipboard.getData?.('text/html') || '')
  if (dataUrl) {
    const added = await addDataUrlImageAttachment(dataUrl)
    if (added > 0) {
      e.preventDefault()
      showToast(t('assistant.imagePasted'), 'ok')
      return
    }
  }

  const text = clipboard.getData?.('text/plain') || ''
  if (text) {
    e.preventDefault()
    insertHTMLAtCursor(text.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>'))
    onCEInput()
  }
}

function dataTransferItemEntry(item) {
  try {
    if (item && typeof item.webkitGetAsEntry === 'function') return item.webkitGetAsEntry()
  } catch {
    return null
  }
  return null
}

function isAbsoluteFilePath(value) {
  const path = String(value || '').trim()
  if (!path) return false
  return /^[A-Za-z]:[\\/]/.test(path) || path.startsWith('\\\\') || (path.startsWith('/') && path.length > 1)
}

function fileSystemPathFromFile(file) {
  if (!file) return ''
  const path = String(file.path || '').trim()
  return isAbsoluteFilePath(path) ? path : ''
}

function fileSystemPathFromEntry(entry) {
  if (!entry) return ''
  // Chromium exposes only "/folder" for webkit entries in normal browser
  // contexts. Desktop WebView builds may still attach an absolute path on
  // File.path; prefer that when available.
  const p = String(entry.fullPath || '').trim()
  if (/^[A-Za-z]:[\\/]/.test(p) || p.startsWith('\\\\')) return p
  if (p.startsWith('/')) {
    const parts = p.split('/').filter(Boolean)
    return parts.length > 1 ? p : ''
  }
  return ''
}

function appendTextToInput(text) {
  const value = String(text || '').trim()
  if (!value) return
  const el = inputEl.value
  if (!el) {
    input.value = `${input.value || ''}${input.value ? ' ' : ''}${value}`
    nextTick(resizeAssistantInput)
    return
  }
  const current = String(input.value || '')
  const start = Number.isFinite(el.selectionStart) ? el.selectionStart : current.length
  const end = Number.isFinite(el.selectionEnd) ? el.selectionEnd : start
  const prefix = current.slice(0, start)
  const suffix = current.slice(end)
  const leftPad = prefix && !/\s$/.test(prefix) ? ' ' : ''
  const rightPad = !suffix ? ' ' : (/^\s/.test(suffix) ? '' : ' ')
  input.value = `${prefix}${leftPad}${value}${rightPad}${suffix}`
  nextTick(() => {
    const pos = prefix.length + leftPad.length + value.length + rightPad.length
    el.focus()
    try {
      el.setSelectionRange(pos, pos)
    } catch {
      // Ignore selection failures in older WebViews.
    }
    resizeAssistantInput()
  })
}

function clearPreviewSelectionContext() {
  previewSelectionContext.value = null
}

function buildPreviewSelectionContext(selection) {
  if (!selection?.text) return ''
  if (selection.kind === 'message') {
    const role = selection.role === 'assistant' ? 'assistant' : 'user'
    const label = selectionReferenceLabel(selection)
    return [
      '[对话引用上下文]',
      `来源: ${role}`,
      label ? `引用: ${label}` : '',
      '内容:',
      '```text',
      String(selection.text || '').slice(0, 4000),
      '```',
    ].filter(Boolean).join('\n')
  }
  if (selection.kind === 'image_attachment') {
    // Picture references travel as metadata only — the image bytes are NOT
    // re-uploaded. We trust the AI to recall the image it already saw in the
    // referenced message (multimodal models keep that turn in context).
    const role = selection.role === 'assistant' ? 'AI 回复' : '用户消息'
    const ordinal = Number.isFinite(selection.message_index) ? `#${selection.message_index + 1}` : ''
    return [
      '[图片引用上下文]',
      `来源: ${role}${ordinal}`,
      selection.attachment_name ? `图片: ${selection.attachment_name}` : '',
      selection.attachment_mime ? `类型: ${selection.attachment_mime}` : '',
      '说明: 用户引用了上述消息中已展示的图片。请结合此前对话里看到的图像内容回答，不需要再次请求图片。',
    ].filter(Boolean).join('\n')
  }
  const filePath = String(selection.file_path || selection.file_name || '')
  const range = selection.start_line
    ? `L${selection.start_line}:C${selection.start_column} - L${selection.end_line}:C${selection.end_column}`
    : ''
  return [
    '[选区上下文]',
    filePath ? `文件: ${filePath}` : '',
    range ? `范围: ${range}` : '',
    '内容:',
    '```text',
    String(selection.text || '').slice(0, 4000),
    '```',
  ].filter(Boolean).join('\n')
}

function onCEInput(e) {
  const el = inputEl.value
  if (!el) return
  if (isComposingInput.value || e?.isComposing) {
    resizeAssistantInput()
    return
  }
  if (isInputVisuallyEmpty(el)) {
    if (el.innerHTML) el.innerHTML = ''
    input.value = ''
  } else {
    input.value = el.innerHTML
  }
  resizeAssistantInput()
  schedulePersistInputDraft()
}

let persistDraftTimer = null
function schedulePersistInputDraft() {
  if (persistDraftTimer) clearTimeout(persistDraftTimer)
  persistDraftTimer = setTimeout(() => {
    persistDraftTimer = null
    persistTabs()
  }, 400)
}

/** contenteditable is destroyed when the panel hides; restore draft HTML on reopen. */
function syncInputDraftToDom() {
  const el = inputEl.value
  const tab = activeTab.value
  if (!el || !tab) return
  let draft = String(tab.inputDraft || '')
  const plain = draft.replace(/<[^>]+>/g, '').replace(/\u200B/g, '').trim()
  if (!plain && !draft.includes('input-inline-path-chip')) {
    draft = ''
    if (tab.inputDraft) tab.inputDraft = ''
  }
  if ((el.innerHTML || '') !== draft) {
    el.innerHTML = draft
  }
  nextTick(resizeAssistantInput)
}

function onCECompositionStart() {
  isComposingInput.value = true
}

function onCECompositionEnd() {
  isComposingInput.value = false
  nextTick(() => onCEInput())
}

function insertHTMLAtCursor(html) {
  const el = inputEl.value
  if (!el) return
  el.focus()
  const sel = window.getSelection()
  if (!sel || !sel.rangeCount) return
  let range = sel.getRangeAt(0)
  if (!el.contains(range.commonAncestorContainer)) {
    range.selectNodeContents(el)
    range.collapse(false)
  }
  range.deleteContents()
  const elNode = document.createElement("div")
  elNode.innerHTML = html
  const frag = document.createDocumentFragment()
  let node, lastNode
  while ((node = elNode.firstChild)) {
    lastNode = frag.appendChild(node)
  }
  range.insertNode(frag)
  if (lastNode) {
    range = range.cloneRange()
    range.setStartAfter(lastNode)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)
  }
}

function appendPathsToInput(paths) {
  const labels = addInputPathRefs(paths)
  if (labels.length) {
    const html = labels.map((label) => {
      const safe = String(label).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
      return `<span class="input-inline-path-chip" contenteditable="false" data-label="${safe}">\u200B${safe}\u200B</span>&nbsp;`
    }).join('')
    insertHTMLAtCursor(html)
    onCEInput()
  }
  return labels
}

async function handleDroppedDirectoryItems(items) {
  const dirs = []
  for (const item of items || []) {
    if (item.kind !== 'file') continue
    const entry = dataTransferItemEntry(item)
    if (!entry?.isDirectory) continue
    const file = item.getAsFile?.()
    const path = fileSystemPathFromFile(file) || fileSystemPathFromEntry(entry)
    dirs.push({ name: entry.name || file?.name || '', path })
  }
  if (!dirs.length) return false
  const inserted = []
  for (const d of dirs) {
    if (d.path) inserted.push(d.path)
  }
  if (inserted.length) {
    const labels = appendPathsToInput(inserted)
    showToast(t('assistant.directoryInserted', { names: labels.join('、') }), 'ok')
  } else {
    showToast(t('assistant.dropFolderPathUnavailable'), 'info')
  }
  return true
}

let nativeDropStamp = 0

function insertDroppedHostPaths(paths) {
  const cleaned = (paths || []).map(p => String(p || '').trim()).filter(isAbsoluteFilePath)
  if (!cleaned.length) return []
  return appendPathsToInput(cleaned)
}

function handleDroppedPathFiles(files) {
  const paths = []
  for (const file of files || []) {
    const path = fileSystemPathFromFile(file)
    if (path) paths.push(path)
  }
  if (!paths.length) return false
  const labels = insertDroppedHostPaths(paths)
  showToast(t('assistant.pathsInserted', { names: labels.join('、') }), 'ok')
  return true
}

function onNativeFileDrop(e) {
  const paths = Array.isArray(e?.detail?.paths) ? e.detail.paths.map(x => String(x || '').trim()).filter(Boolean) : []
  if (!paths.length) return
  nativeDropStamp = Date.now()
  const labels = insertDroppedHostPaths(paths)
  showToast(t('assistant.pathsInserted', { names: labels.join('、') }), 'ok')
}

function waitForNativeFileDrop(ms) {
  const started = Date.now()
  return new Promise((resolve) => {
    const tick = () => {
      if (Date.now() - nativeDropStamp < 800 || Date.now() - started >= ms) {
        resolve()
        return
      }
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
}

let dragLeaveTimer = null
let lastOverlayAt = 0

function clearDragOverlay() {
  if (dragLeaveTimer) {
    clearTimeout(dragLeaveTimer)
    dragLeaveTimer = null
  }
  if (dragOver.value) dragOver.value = false
}

function dataTransferLooksDroppable(dt) {
  const types = dt?.types
  if (!types) return false
  const has = (name) => (
    typeof types.contains === 'function'
      ? types.contains(name)
      : Array.prototype.indexOf.call(types, name) !== -1
  )
  return has('Files') || has('application/x-argusai-sandbox-paths')
}

function onPanelDragOver(e) {
  if (!dataTransferLooksDroppable(e.dataTransfer)) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  if (!dragOver.value) dragOver.value = true
  const now = performance.now()
  if (dragLeaveTimer && now - lastOverlayAt < 200) return
  lastOverlayAt = now
  if (dragLeaveTimer) clearTimeout(dragLeaveTimer)
  dragLeaveTimer = setTimeout(() => {
    dragOver.value = false
    dragLeaveTimer = null
  }, 250)
}

function onPanelDragLeave(_e) { /* heartbeat-based; see onPanelDragOver */ }

async function onPanelDrop(e) {
  clearDragOverlay()
  const sandboxPayload = e.dataTransfer?.getData?.('application/x-argusai-sandbox-paths') || ''
  if (sandboxPayload) {
    e.preventDefault()
    try {
      const paths = JSON.parse(sandboxPayload)
      if (Array.isArray(paths) && paths.length) {
        onSandboxReferenceFile({ files: paths.map(p => ({ file_path: p, file_name: String(p).split('/').pop() || p })) })
        return
      }
    } catch (_) {}
  }
  if (!e.dataTransfer?.files?.length && !e.dataTransfer?.items?.length) return
  e.preventDefault()
  const files = Array.from(e.dataTransfer.files || [])
  if (handleDroppedPathFiles(files)) return
  if (Date.now() - nativeDropStamp < 800) return
  // pywebview delivers absolute paths on a separate native drop event.
  // Wait one short beat so we don't read file bytes as a fallback first.
  await waitForNativeFileDrop(80)
  if (Date.now() - nativeDropStamp < 800) return
  if (await handleDroppedDirectoryItems(Array.from(e.dataTransfer.items || []))) return
  // Browser/WebView drops that hide absolute paths still need an uploaded
  // copy. Native desktop drops already inserted the original path above.
  if (files.length) {
    const added = await addFiles(files)
    if (added > 0) {
      showToast(
        added > 1
          ? t('assistant.attachmentsAdded', { count: added })
          : t('assistant.attachmentAdded'),
        'ok',
      )
      return
    }
  }
  showToast(t('assistant.dropFilePathUnavailable'), 'info')
}

function onGlobalDragEnd() { clearDragOverlay() }
function onGlobalDrop() { clearDragOverlay() }
function onGlobalPointerUp() { if (dragOver.value) clearDragOverlay() }

function updateMsg(index, patch, tab = activeTab.value) {
  if (!tab) return
  if (index < 0 || index >= tab.messages.length) return
  const shouldFollow = tab === activeTab.value && (autoFollowScroll.value || isNearBottom())
  const prev = tab.messages[index]
  const next = { ...prev, ...patch }
  if (Object.prototype.hasOwnProperty.call(patch, 'status')) {
    if (patch.status && !prev.startedAt) {
      next.startedAt = Date.now()
      startElapsedTicker()
      const limit = Number(tab.activeToolTimeoutSeconds || 0)
      if (limit >= 30) next.statusTimeoutSeconds = limit
    } else if (!patch.status) {
      next.startedAt = null
      next.statusTimeoutSeconds = 0
    }
  }
  tab.messages[index] = next
  if (Object.prototype.hasOwnProperty.call(patch, 'status') && !patch.status && !hasAnyActiveStatusMessage()) {
    stopElapsedTicker()
  }
  if (tab === activeTab.value) scrollBottom({ force: shouldFollow })
}

function appendMsgText(index, text, tab = activeTab.value) {
  if (!tab || !text) return
  if (index < 0 || index >= tab.messages.length) return
  const shouldFollow = tab === activeTab.value && (autoFollowScroll.value || isNearBottom())
  tab.messages[index] = {
    ...tab.messages[index],
    text: (tab.messages[index].text || '') + text,
  }
  if (tab === activeTab.value) {
    scrollBottom({ force: shouldFollow })
  } else {
    tab.badge.unread = (tab.badge.unread || 0) + 1
    recomputeBadge(tab)
  }
}

const SINGLETON_BLOCK_TYPES = new Set(['todo_list'])

function userDecisionBlockKey(block) {
  if (!block || !isUserDecisionBlock(block)) return ''
  if (block.type === 'user_decision_batch') {
    let questions = ''
    try {
      questions = JSON.stringify((Array.isArray(block.questions) ? block.questions : []).map(q => ({
        id: String(q?.id || ''),
        question: String(q?.question || ''),
        options: Array.isArray(q?.options) ? q.options : [],
        required: q?.required !== false,
        context: String(q?.context || ''),
      })))
    } catch (_) {
      questions = String(block.questions || '')
    }
    return [
      'user_decision_batch',
      String(block.title || ''),
      String(block.context || ''),
      questions,
    ].join('\u001f')
  }
  let options = ''
  try {
    options = JSON.stringify(Array.isArray(block.options) ? block.options : [])
  } catch (_) {
    options = String(block.options || '')
  }
  return [
    'user_decision',
    String(block.question || ''),
    String(block.context || ''),
    options,
    String(block.input_placeholder || ''),
    block.input_required ? '1' : '0',
  ].join('\u001f')
}

function singletonBlockId(block) {
  if (!block || typeof block !== 'object') return ''
  if (!SINGLETON_BLOCK_TYPES.has(block.type)) return ''
  if (block.type === 'todo_list') return 'todo_list'
  if (block.type === 'task_progress') return `task_progress:${block.task_id || ''}`
  return ''
}

function stripSingletonFromOtherMessages(tab, singletonId, keepIndex) {
  if (!tab || !singletonId) return
  for (let i = 0; i < tab.messages.length; i++) {
    if (i === keepIndex) continue
    const msg = tab.messages[i]
    if (!msg || !Array.isArray(msg.blocks) || msg.blocks.length === 0) continue
    let mutated = false
    const filtered = msg.blocks.filter(b => {
      const id = singletonBlockId(b)
      if (id === singletonId) {
        mutated = true
        return false
      }
      return true
    })
    if (mutated) {
      tab.messages[i] = { ...msg, blocks: filtered }
    }
  }
}

function stripOlderPendingUserDecisions(tab, keepIndex) {
  if (!tab) return
  for (let i = 0; i < tab.messages.length; i++) {
    if (i === keepIndex) continue
    const msg = tab.messages[i]
    if (!msg || !Array.isArray(msg.blocks) || msg.blocks.length === 0) continue
    let mutated = false
    const filtered = msg.blocks.filter(b => {
      if (isPendingUserDecisionBlock(b)) {
        mutated = true
        return false
      }
      return true
    })
    if (mutated) tab.messages[i] = { ...msg, blocks: filtered }
  }
}

function stripDuplicatePendingUserDecisions(tab, decisionKey, keepIndex) {
  // The final result replays cards already streamed in, so the same unanswered
  // card can land on two messages. Match on key rather than dropping every
  // pending card, which would also discard an unrelated one.
  if (!tab || !decisionKey) return
  for (let i = 0; i < tab.messages.length; i++) {
    if (i === keepIndex) continue
    const msg = tab.messages[i]
    if (!msg || !Array.isArray(msg.blocks) || msg.blocks.length === 0) continue
    let mutated = false
    const filtered = msg.blocks.filter(b => {
      if (isPendingUserDecisionBlock(b) && userDecisionBlockKey(b) === decisionKey) {
        mutated = true
        return false
      }
      return true
    })
    if (mutated) tab.messages[i] = { ...msg, blocks: filtered }
  }
}

function appendMsgBlock(index, block, tab = activeTab.value) {
  if (!tab || !block) return
  if (index < 0 || index >= tab.messages.length) return
  const shouldFollow = tab === activeTab.value && (autoFollowScroll.value || isNearBottom())
  const prev = tab.messages[index]
  let blocks = Array.isArray(prev.blocks) ? prev.blocks.slice() : []
  block = normalizeBlock(block)
  if (isUserDecisionBlock(block)) {
    // A session may wait on only one user decision. A newer card supersedes
    // every unanswered predecessor, even when its wording/count changed.
    stripOlderPendingUserDecisions(tab, index)
    blocks = blocks.filter(b => !isPendingUserDecisionBlock(b))
    blocks.push(block)
    tab.messages[index] = { ...prev, blocks, status: '' }
    enterUserDecisionWaitState(tab, index)
    if (tab === activeTab.value) {
      scrollBottom({ force: shouldFollow })
    } else {
      tab.badge.unread = (tab.badge.unread || 0) + 1
      recomputeBadge(tab)
    }
    return
  }
  const singletonId = singletonBlockId(block)
  if (singletonId) {
    stripSingletonFromOtherMessages(tab, singletonId, index)
    const existingIdx = blocks.findIndex(b => singletonBlockId(b) === singletonId)
    if (existingIdx >= 0) {
      blocks[existingIdx] = block
    } else {
      blocks.push(block)
    }
  } else {
    blocks.push(block)
  }
  tab.messages[index] = { ...prev, blocks, status: '' }
  if (isUserDecisionBlock(block)) enterUserDecisionWaitState(tab, index)
  if (tab === activeTab.value) {
    scrollBottom({ force: shouldFollow })
  } else {
    tab.badge.unread = (tab.badge.unread || 0) + 1
    recomputeBadge(tab)
  }
}

function blockKey(block) {
  if (!block || typeof block !== 'object') return ''
  const type = block.type || ''
  const name = block.name || block.title || ''
  const task = block.task_id || block.id || ''
  const path = block.relative_path || block.path || ''
  const url = block.url || block.final_url || ''
  const code = block.exit_code ?? ''
  const elapsed = block.elapsed_ms || block.duration_ms || ''
  return [type, name, task, path, url, code, elapsed].join('|')
}

function mergeBlocks(existing, incoming, tab, ownerIndex = -1) {
  const out = Array.isArray(existing) ? existing.slice() : []
  const seen = new Set(out.map(blockKey).filter(Boolean))
  for (let block of incoming || []) {
    block = normalizeBlock(block)
    if (isUserDecisionBlock(block)) {
      const decisionKey = userDecisionBlockKey(block)
      stripDuplicatePendingUserDecisions(tab, decisionKey, ownerIndex)
      const existingIdx = out.findIndex(b =>
        isUserDecisionBlock(b)
        && !b.submitted_response
        && userDecisionBlockKey(b) === decisionKey,
      )
      if (existingIdx >= 0) {
        out[existingIdx] = block
      } else {
        out.push(block)
      }
      continue
    }
    const singletonId = singletonBlockId(block)
    if (singletonId) {
      stripSingletonFromOtherMessages(tab, singletonId, ownerIndex)
      const existingIdx = out.findIndex(b => singletonBlockId(b) === singletonId)
      if (existingIdx >= 0) {
        out[existingIdx] = block
        continue
      }
      out.push(block)
      continue
    }
    const key = blockKey(block)
    if (key && seen.has(key)) continue
    out.push(block)
    if (key) seen.add(key)
  }
  return out
}

function finalizeAssistantMessage(index, result, extra = {}, tab = activeTab.value) {
  if (!tab) return
  const prev = tab.messages[index] || {}
  const authoritativeText = String(result?.text || '')
  updateMsg(index, {
    // The final response is assembled by the backend in protocol order. Use
    // it whenever present so delayed/dropped stream chunks cannot leave a
    // partial answer or reverse neighboring paragraphs.
    text: authoritativeText || prev.text || '',
    status: '',
    toolCalls: result.tool_calls || [],
    options: result.options || [],
    blocks: mergeBlocks(prev.blocks, result.blocks || [], tab, index),
    toolTrace: result.tool_trace || [],
    ...extra,
  }, tab)
  syncDesktopControlFromMessages(tab.messages)
  if (tab.desktopControl?.active) refreshDesktopControl()
}

function appendStreamText(index, text, tab = activeTab.value) {
  if (!tab || !text) return index
  if (index < 0 || index >= tab.messages.length) {
    if (index >= 0 && index < tab.messages.length) {
      const prev = tab.messages[index]
      if (prev && prev.status) {
        tab.messages[index] = { ...prev, status: '', startedAt: null }
      }
    }
    index = addMsg('assistant', '', null, null, null, null, null, tab)
  }
  appendMsgText(index, text, tab)
  return index
}

function appendStreamBlock(index, block, tab = activeTab.value) {
  if (!tab || !block) return index
  if (index < 0 || index >= tab.messages.length) {
    if (index >= 0 && index < tab.messages.length) {
      const prev = tab.messages[index]
      if (prev && prev.status) {
        tab.messages[index] = { ...prev, status: '', startedAt: null }
      }
    }
    index = addMsg('assistant', '', null, null, null, null, null, tab)
  }
  hydrateExternalPendingBlock(block, tab)
  appendMsgBlock(index, block, tab)
  return index
}

function toolActivityBlockFromStatus(text) {
  const m = String(text || '').match(/^正在执行工具[:：]\s*(.+)$/)
  if (!m) return null
  const name = m[1].trim()
  if (!name) return null
  return name
}

function clearActiveTool(tab = activeTab.value) {
  if (!tab) return
  tab.activeToolName = ''
  tab.activeToolTimeoutSeconds = 0
}

/** True when an assistant bubble has no prose and only runtime/tool result blocks. */
function isToolOnlyAssistantMessage(message, absoluteIndex = -1) {
  if (!message || message.hidden || message.role !== 'assistant') return false
  if (visibleMessageText(message)) return false
  if (message.status) return false
  if (message.attachments?.length) return false
  if (message.options?.length) return false
  const blocks = visibleBlocks(message, absoluteIndex)
  if (!blocks.length) return true
  return blocks.every(b => isToolResultBlock(b))
}

/** Agent loop round > 0: seal the current bubble and open a new assistant message. */
function beginAssistantStreamSegment(tab, assistantIndex, roundIndex) {
  if (!tab || roundIndex <= 0) return assistantIndex
  const current = assistantIndex >= 0 && assistantIndex < tab.messages.length
    ? tab.messages[assistantIndex]
    : null
  if (current
    && !visibleMessageText(current)
    && !(current.blocks || []).length
    && !(current.toolCalls || []).length
  ) {
    // Nothing to seal. Clearing this bubble's status and opening a second empty
    // one leaves the panel showing no assistant bubble at all until something
    // arrives -- several seconds of apparent nothing while a local Agent boots
    // its subprocess, since that round opens before the agent has answered.
    updateMsg(assistantIndex, { agentRound: roundIndex, agentStep: true }, tab)
    return assistantIndex
  }
  if (current) {
    updateMsg(assistantIndex, { status: '' }, tab)
  }
  // Always open a new bubble per agent round. Tool-only rounds still fold into
  // the collapsible timeline row (isAgentTimelineStep); merging them here was
  // what produced one mega-bubble with "工具调用 · N 项" and all narration stacked.
  return addMsg('assistant', '', null, null, null, null, null, tab, {
    agentRound: roundIndex,
    agentStep: true,
  })
}

function messageGroupsWithPrevious(prevMsg, msg) {
  if (!prevMsg || !msg) return false
  if (prevMsg.role !== msg.role) return false
  // Agent-loop steps + the turn's final answer form ONE visual block sharing a
  // single avatar column, so a long autonomous run reads like a Cursor/Claude
  // style timeline (a stack of collapsible steps) instead of N stacked bubbles.
  return true
}

/**
 * Whether the message at `absoluteIndex` opens a new visual group (shows its
 * avatar + role label). Grouping must be decided against the previous *rendered*
 * message, skipping `hidden` / empty ones — otherwise a hidden assistant row
 * persisted before e.g. an "等你决定" decision card would make the card look like
 * a continuation and swallow its avatar after a session reload.
 */
function messageStartsVisualGroup(m, absoluteIndex) {
  if (absoluteIndex <= 0) return true
  const list = messages.value
  let prev = null
  for (let j = absoluteIndex - 1; j >= 0; j -= 1) {
    const p = list[j]
    if (!p || p.hidden) continue
    if (!hasVisibleMessagePayload(p, j)) continue
    prev = p
    break
  }
  if (!prev) return true
  return !messageGroupsWithPrevious(prev, m)
}

// ── Agent step timeline ──────────────────────────────────────────────────────
// A long task used to dump every round's prose + folded tool panel into one
// ever-growing bubble. Instead we render each intermediate agent round as a
// collapsible "step": open while it's the live round, auto-collapsed to a
// one-line summary once the round finishes (the trailing answer stays open).
// `stepOpenOverrides` records explicit user toggles, keyed by the (globally
// unique) message uid.
const stepOpenOverrides = reactive({})

/**
 * True when an assistant message should render as a collapsible timeline row.
 *
 * Cursor/Claude keep the assistant's PROSE always visible and only collapse the
 * noisy parts (tool calls, reasoning). Collapsing prose felt like "everything
 * disappeared", so we only fold *tool-only* rounds (a round whose entire payload
 * is tool results, no narration) into a one-line step like Cursor's
 * "Explored / Ran command" rows. Any round that carries prose renders in full.
 */
function isAgentTimelineStep(m, absoluteIndex) {
  if (!m || m.role !== 'assistant' || m.hidden) return false
  if (absoluteIndex <= 0) return false
  return isToolOnlyAssistantMessage(m, absoluteIndex)
}

/** The currently-streaming round (last assistant message while a run is live). */
function isActiveAssistantStep(absoluteIndex) {
  return sending.value && absoluteIndex === lastAssistantIndex()
}

/** Effective open/closed state of a timeline step (manual toggle wins). */
function stepOpenState(m, absoluteIndex) {
  const uid = m && m._uid
  if (uid && Object.prototype.hasOwnProperty.call(stepOpenOverrides, uid)) {
    return stepOpenOverrides[uid]
  }
  // Live round stays open; the turn's last message also stays open so a run that
  // ends on a tool result (no final prose) doesn't hide everything behind a fold.
  if (isActiveAssistantStep(absoluteIndex)) return true
  if (!sending.value && absoluteIndex === lastAssistantIndex()) return true
  return false
}

function toggleStepOpen(m, absoluteIndex) {
  if (!m || !m._uid) return
  stepOpenOverrides[m._uid] = !stepOpenState(m, absoluteIndex)
}

function stepBlockToolName(b) {
  if (!b) return ''
  if (b.type === 'tool_trace') return b.name || ''
  if (b.type === 'command_result') return b.command || 'command'
  if (b.type === 'sandbox_run') return b.title || 'run'
  return b.source_tool || b.title || ''
}

/** One-line summary shown on a collapsed step header. */
function stepSummaryLabel(m, absoluteIndex) {
  const prose = visibleMessageText(m)
  const tools = messageBlockGroups(m, absoluteIndex).toolResults
  if (prose) {
    const firstLine = prose.split('\n').map(s => s.trim()).find(Boolean) || ''
    if (firstLine) return firstLine.length > 56 ? firstLine.slice(0, 56) + '…' : firstLine
  }
  if (tools.length) {
    const names = []
    for (const b of tools) {
      const n = stepBlockToolName(b)
      if (n && !names.includes(n)) names.push(n)
      if (names.length >= 3) break
    }
    if (names.length) {
      const head = names.join('、')
      return tools.length > names.length
        ? t('assistant.moreItems', { names: head, count: tools.length })
        : head
    }
    return t('assistant.toolCallsCount', { count: tools.length })
  }
  return t('assistant.thinkingProcess')
}

function stepToolCount(m, absoluteIndex) {
  return messageBlockGroups(m, absoluteIndex).toolResults.length
}

function isGenericEmptyDataTable(block) {
  if (block?.type !== 'data_table') return false
  if ((block.rows || []).length) return false
  const title = String(block.title || '').trim().toLowerCase()
  return !title || ['结果列表', 'results', 'result list'].includes(title)
}

const _blockSigCache = new WeakMap()

function dataTableSignature(block) {
  if (block?.type !== 'data_table') return ''
  const cached = _blockSigCache.get(block)
  if (cached !== undefined) return cached
  const rows = (block.rows || []).map(row => {
    const id = String(row?.id || '')
    const cells = row?.cells && typeof row.cells === 'object' ? row.cells : {}
    return id + ':' + Object.keys(cells).sort().map(key => `${key}=${cells[key]}`).join('|')
  })
  const sig = rows.length ? rows.join('||') : ''
  _blockSigCache.set(block, sig)
  return sig
}

function isDuplicateDataTable(block, absoluteIndex = -1) {
  const sig = dataTableSignature(block)
  if (!sig || absoluteIndex <= 0) return false
  for (let i = 0; i < absoluteIndex && i < messages.value.length; i += 1) {
    for (const prev of Array.isArray(messages.value[i]?.blocks) ? messages.value[i].blocks : []) {
      if (prev?.type === 'data_table' && dataTableSignature(prev) === sig) return true
    }
  }
  return false
}

function structuredCardSignature(block) {
  if (block?.type !== 'structured_card') return ''
  const cached = _blockSigCache.get(block)
  if (cached !== undefined) return cached
  try {
    const sig = JSON.stringify({
      title: block.title || '',
      subtitle: block.subtitle || '',
      sections: block.sections || [],
    })
    _blockSigCache.set(block, sig)
    return sig
  } catch (_) {
    return ''
  }
}

// The backend now dedupes identical show_structured_card calls within a turn
// at the source (see _emitted_card_signatures), so freshly-saved sessions stay
// clean. This stays as a display guard for sessions persisted before that fix
// (and any cross-call straggler): each call has its own block id, so id-based
// dedup misses it; we compare rendered content and keep only the first.
function isDuplicateStructuredCard(block, absoluteIndex = -1) {
  const sig = structuredCardSignature(block)
  if (!sig || absoluteIndex <= 0) return false
  for (let i = 0; i < absoluteIndex && i < messages.value.length; i += 1) {
    for (const prev of Array.isArray(messages.value[i]?.blocks) ? messages.value[i].blocks : []) {
      if (prev?.type === 'structured_card' && structuredCardSignature(prev) === sig) return true
    }
  }
  return false
}

function fileResourceSignature(block) {
  if (block?.type !== 'file_resource_view') return ''
  const items = Array.isArray(block.items) ? block.items : []
  return items.map(item => String(item?.relative_path || item?.path || item?.name || '')).filter(Boolean).sort().join('|')
}

const TOOL_RESULT_BLOCK_TYPES = new Set([
  'probe_result',
  'command_result',
  'channel_delivery',
  'tool_trace',
  'tool_skipped',
  'reverse_result',
  'runtime_trace',
  'sandbox_file',
  'sandbox_run',
  'sandbox_env',
  'vulnerability_evidence',
  'attack_chain',
  'request_replay',
  'oob_event',
  'structured_card',
  'desktop_observe',
])

function isToolResultBlock(block) {
  if (!block) return false
  // A model-authored card (e.g. show_structured_card) is the assistant's own
  // presentation, not a tool dump. The backend persists it in the same final
  // assistant message as the turn's tool_trace blocks, so if we classified it
  // as a tool result it would get folded into the collapsed "工具调用" panel on
  // session reload — even though it renders prominently live (where it sits
  // alone in the final bubble). Keep it primary so it survives a restart.
  if (block.render_policy === 'model_authored' || block.source === 'model') return false
  return TOOL_RESULT_BLOCK_TYPES.has(block.type)
}

const _msgBlockGroupCache = new WeakMap()

function messageBlockGroups(message, absoluteIndex = -1) {
  if (!message || typeof message !== 'object') {
    return { primary: [], toolResults: [], hasAny: false, foldTools: false }
  }
  const rawBlocks = Array.isArray(message.blocks) ? message.blocks : []
  const hasText = Boolean(visibleMessageText(message))
  const cached = _msgBlockGroupCache.get(message)
  if (
    cached
    && cached.blocksRef === rawBlocks
    && cached.blocksLen === rawBlocks.length
    && cached.lastBlock === rawBlocks[rawBlocks.length - 1]
    && cached.hasText === hasText
    && cached.absoluteIndex === absoluteIndex
  ) {
    return cached.value
  }
  const blocks = visibleBlocks(message, absoluteIndex)
  const primary = []
  const toolResults = []
  for (const block of blocks) {
    if (isToolResultBlock(block)) toolResults.push(block)
    else primary.push(block)
  }
  const toolOnly = primary.length === 0 && toolResults.length > 0
  const value = {
    primary,
    toolResults,
    hasAny: primary.length + toolResults.length > 0,
    foldTools: toolOnly ? toolResults.length >= 1 : toolResults.length >= 2,
  }
  _msgBlockGroupCache.set(message, {
    blocksRef: rawBlocks,
    blocksLen: rawBlocks.length,
    lastBlock: rawBlocks[rawBlocks.length - 1],
    hasText,
    absoluteIndex,
    value,
  })
  return value
}

function visibleBlocks(message, absoluteIndex = -1) {
  const blocks = Array.isArray(message?.blocks) ? message.blocks : []
  // When model explicitly called show_file_resource_view, suppress all auto-generated
  // sandbox_list_files resource views (they are either file_resource_view or sandbox_file type).
  const hasModelFileResourceView = blocks.some(b => b?.type === 'file_resource_view' && b.source_tool === 'show_file_resource_view')
  // Only show the first file_resource_view per message to avoid duplicates.
  let fileResourceViewCount = 0
  const seenFileResourceSigs = new Set()
  let lastTodoIndex = -1
  let lastPendingDecisionIndex = -1
  for (let index = 0; index < blocks.length; index += 1) {
    if (blocks[index]?.type === 'todo_list') lastTodoIndex = index
    if (isPendingUserDecisionBlock(blocks[index])) lastPendingDecisionIndex = index
  }
  return blocks.filter((block, blockIndex) => {
    if (block?.type === 'tool_activity') return false
    // Old sessions and interrupted live streams may contain one projection per
    // todo mutation. Rendering only the newest one prevents plan-card floods.
    if (block?.type === 'todo_list' && blockIndex !== lastTodoIndex) return false
    if (isPendingUserDecisionBlock(block) && blockIndex !== lastPendingDecisionIndex) return false
    // Desktop observe: once the model wrote a normal-language summary, hide
    // the permission card and the structured result card from the thread.
    if (
      (block?.type === 'desktop_observe' || (block?.type === 'confirm' && block?.name === 'desktop_observe'))
      && visibleMessageText(message)
    ) {
      return false
    }
    if (hasModelFileResourceView && block?.type === 'file_resource_view' && block.source_tool !== 'show_file_resource_view') return false
    if (hasModelFileResourceView && block?.type === 'sandbox_file' && block.title === 'sandbox_list_files') return false
    if (block?.type === 'file_resource_view') {
      const sig = fileResourceSignature(block)
      if (sig && seenFileResourceSigs.has(sig)) return false
      if (sig) seenFileResourceSigs.add(sig)
      fileResourceViewCount += 1
      if (fileResourceViewCount > 1 && block.source_tool !== 'show_file_resource_view') return false
    }
    if (isGenericEmptyDataTable(block)) return false
    if (isDuplicateDataTable(block, absoluteIndex)) return false
    if (isDuplicateStructuredCard(block, absoluteIndex)) return false
    return true
  })
}

function hasVisibleMessagePayload(message, absoluteIndex = -1) {
  if (message?.hidden) return false
  return Boolean(
    visibleMessageText(message)
    || messageVoice(message)
    || message?.status
    || visibleBlocks(message, absoluteIndex).length
    || messageDisplayAttachments(message).length
    || message?.options?.length
  )
}

// Block types that the runtime only attaches at turn boundaries (e.g. the
// "等待你决定" card emitted by `_commit_auto_limit_decision_stop` /
// `_commit_user_decision_stop` / `_commit_implicit_user_question_stop`).
// Streaming events never carry these mid-turn, so we MUST keep them when
// folding `result.blocks` into the assistant bubble — otherwise the card
// only appears after the user restarts the app and the session reloads
// from disk (where ChatMessage.blocks still holds it). See
// argusai/core/assistant/session_loop_control.py.
const FINAL_ONLY_BLOCK_TYPES = new Set(['user_decision', 'user_decision_batch'])

function finalizeStreamAssistantMessage(index, result, { streamedBlocks = false, extra = {} } = {}, tab = activeTab.value) {
  if (!tab) return index
  const targetIndex = index
  const hasPendingTools = Boolean(result?.pending_tool || (Array.isArray(result?.pending_tools) && result.pending_tools.length))
  const awaitingDecision = result?.status === 'awaiting_user_decision'
  let finalResult = result
  if (streamedBlocks && !hasPendingTools && !awaitingDecision) {
    const blocks = Array.isArray(result?.blocks) ? result.blocks : []
    const finalOnly = blocks.filter(b => b && FINAL_ONLY_BLOCK_TYPES.has(b.type))
    finalResult = { ...result, blocks: finalOnly }
  }
  finalizeAssistantMessage(targetIndex, finalResult, extra, tab)
  return targetIndex
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

// ────────────────────────────────────────────────────────────────────────────
// Stream runner shared by send / regenerate / submitPendingBatch.
//
// Bound to a SPECIFIC tab (not the active one), so the user can click away to
// another tab mid-stream without dropping progress. When the bound tab is
// closed or its runSeq advances (cancelRun / newSession), this loop bails
// out at the next chunk boundary.
// ────────────────────────────────────────────────────────────────────────────
async function runAssistantStream(
  tab,
  startedRunId,
  assistantIndex,
  { allowFinalize = true, regenerate = false, userIndex = -1, initialOffset = 0 } = {},
) {
  const ownSeq = tab.runSeq
  tab.activeRunId = startedRunId
  let offset = Math.max(0, Number(initialOffset || 0))
  let finished = false
  let lastEventAt = Date.now()
  let streamedBlocks = false
  try {
    while (!finished) {
      if (tab.runSeq !== ownSeq) return assistantIndex
      const chunk = await api.getAssistantStreamEvents(startedRunId, offset)
      if (!chunk.ok) throw new Error(chunk.error || 'Assistant stream failed')
      offset = chunk.next || offset
      if (chunk.job) tab.liveJob = chunk.job
      if ((chunk.events || []).length) lastEventAt = Date.now()
      for (const ev of chunk.events || []) {
        if (tab.runSeq !== ownSeq) return assistantIndex
        // AI UI Protocol v2: prefer the typed envelope (ev.kind + ev.data);
        // fall back to the legacy `type` field for events that haven't been
        // upgraded yet. The two are emitted side-by-side from the backend.
        const kind = ev.kind || ev.type
        const data = ev.data || null
        if (kind === 'text_chunk' || kind === 'delta') {
          const text = (data && data.text) || ev.text || ''
          assistantIndex = appendStreamText(assistantIndex, text, tab)
        } else if (kind === 'tool_activity') {
          const tool = data && data.tool
          const status = data && data.status
          if (status === 'start' && tool) {
            tab.activeToolName = tool
            tab.activeToolTimeoutSeconds = Number((data && data.timeout_seconds) || 0)
          } else if (status === 'end') {
            if (tool && SANDBOX_MUTATING_TOOLS.has(tool)) {
              notifySandboxTreeMaybeChanged(tab, tool)
            }
            clearActiveTool(tab)
          }
        } else if (kind === 'stage' || kind === 'status') {
          const label = (data && data.label) || ev.text || ''
          // Legacy classifier kept here only to handle v1 events from a
          // future older backend; new backend already upgraded these.
          const toolName = toolActivityBlockFromStatus(label)
          if (toolName) {
            tab.activeToolName = toolName
          } else if (String(label).includes('工具执行完成')) {
            clearActiveTool(tab)
          } else {
            updateMsg(assistantIndex, { status: label }, tab)
          }
        } else if (kind === 'block_upsert' || kind === 'block') {
          streamedBlocks = true
          const block = (data && data.block) || ev.block
          if (block && ['sandbox_run', 'sandbox_file', 'file_resource_view'].includes(block.type)) {
            notifySandboxTreeMaybeChanged(tab, block.type)
          }
          assistantIndex = appendStreamBlock(assistantIndex, block, tab)
        } else if (
          kind === 'agent_round_start'
          || (kind === 'segment' && Number((data && data.round) ?? ev.round ?? 0) > 0)
        ) {
          const round = Number((data && data.round) ?? ev.round ?? 0)
          if (round > 0) {
            assistantIndex = beginAssistantStreamSegment(tab, assistantIndex, round)
          }
        } else if (kind === 'run_done' || kind === 'done') {
          const r = ev.result || (data && data.result) || {}
          if (data?.job) tab.liveJob = data.job
          if (r.runtime && typeof r.runtime === 'object') {
            tab.runtime = { ...r.runtime }
            persistTabs()
          }
          applySessionTokenUsage(r, tab)
          if (r.ok) {
            const persistedUserTimestamp = Number(r.user_timestamp || 0)
            if (
              persistedUserTimestamp > 0
              && userIndex >= 0
              && userIndex < tab.messages.length
              && tab.messages[userIndex]?.role === 'user'
            ) {
              updateMsg(userIndex, { ts: persistedUserTimestamp * 1000 }, tab)
            }
            if (allowFinalize) {
              const ext = regenerate ? { ts: Date.now() } : {}
              assistantIndex = finalizeStreamAssistantMessage(assistantIndex, r, { streamedBlocks, extra: ext }, tab)
            }
            setPendingFromResponse(r, tab)
            // The backend owns the persisted session title. Keep the tab in
            // sync even if we showed a temporary first-message title earlier.
            if (r.session_name && tab.name !== r.session_name) {
              tab.name = r.session_name
              persistTabs()
            }
            refreshSessions(true).catch(() => {})
            dispatchTasksChangedIfNeeded(r)
            dispatchSandboxFilesChangedIfNeeded(r, tab)
          } else {
            const errorText = r.error_code === 'acp_image_unsupported'
              ? t('assistant.acpImagesUnsupportedSend')
              : (r.error || 'Error')
            updateMsg(assistantIndex, { text: errorText, status: '' }, tab)
            tab.lastError = errorText
          }
        } else if (kind === 'run_error' || kind === 'error') {
          const message = (data && data.message) || ev.error || 'Assistant stream failed'
          updateMsg(assistantIndex, { text: 'Error: ' + message, status: '' }, tab)
          tab.lastError = message
        }
      }
      finished = !!chunk.done
      if (!finished && Date.now() - lastEventAt > POLL_STALL_HINT_MS) {
        const limit = Number(tab.activeToolTimeoutSeconds || 0)
        const startedAt = tab.messages[assistantIndex]?.startedAt
        let stallLabel = t('assistant.waitingForModel')
        if (tab.activeToolName) {
          if (startedAt && limit >= 30) {
            stallLabel = t('assistant.executingToolElapsed', {
              name: tab.activeToolName,
              elapsed: formatToolElapsed(startedAt, nowMs.value, limit),
            })
          } else {
            stallLabel = t('assistant.executingTool', { name: tab.activeToolName })
          }
        }
        updateMsg(assistantIndex, { status: stallLabel }, tab)
        lastEventAt = Date.now()
      }
      if (!finished) await sleep(POLL_INTERVAL_MS)
    }
  } catch (e) {
    if (tab.runSeq !== ownSeq) return assistantIndex
    updateMsg(assistantIndex, { text: 'Error: ' + (e.message || e), status: '' }, tab)
    tab.lastError = String(e.message || e)
    recomputeBadge(tab)
  } finally {
    if (tab.runSeq === ownSeq) {
      tab.sending = false
      tab.activeStreamMessage = false
      tab.confirmingAction = false
      tab.activeRunId = ''
      clearActiveTool(tab)
      refreshRunJobs(tab).catch(() => {})
      refreshAgentThreads(tab).catch(() => {})
      recomputeBadge(tab)
      if (tab === activeTab.value) {
        scrollBottom({ force: autoFollowScroll.value || isNearBottom() })
        drainQueuedInput()
        if (tab.autoApproveMode && !(tab.queuedInputs || []).length) {
          nextTick(() => drainPendingAfterAutoApprove(tab))
        }
      }
    }
  }
  return assistantIndex
}

function attachExternalStream(tab, projection) {
  const runId = String(projection?.run_id || '').trim()
  if (!tab || !runId || tab.activeRunId === runId || tab.sending || tab.confirmingAction) return
  tab.runSeq += 1
  tab.sending = true
  tab.activeStreamMessage = true
  tab.activeRunId = runId
  tab.liveJob = projection?.job || null
  tab.lastError = ''
  const assistantIndex = addMsg('assistant', '', null, null, null, null, null, tab)
  updateMsg(assistantIndex, {
    status: projection?.cron_job_id
      ? t('assistant.scheduledTaskRunning')
      : t('assistant.backgroundTaskRunning'),
  }, tab)
  recomputeBadge(tab)
  const eventCount = Math.max(0, Number(projection?.event_count || 0))
  void runAssistantStream(tab, runId, assistantIndex, {
    initialOffset: Math.max(0, eventCount - 1),
  })
}

function startPanelResize(edge, e) {
  e.preventDefault()
  if (isMaximized.value) {
    isMaximized.value = false
  }
  const startX = e.clientX
  const startY = e.clientY
  const startW = panelWidth.value
  const startH = panelHeight.value
  const startLeft = panelLeft.value
  const startTop = panelTop.value
  let rafId = 0
  let latestEv = null
  document.body.classList.add('panel-dragging')
  const apply = () => {
    rafId = 0
    if (!latestEv) return
    const dx = latestEv.clientX - startX
    const dy = latestEv.clientY - startY
    if (edge.includes('w')) {
      const minW = minPanelWidth()
      const maxW = Math.min(maxPanelWidth(), startLeft + startW - minPanelLeft.value)
      const nextW = Math.min(maxW, Math.max(minW, startW - dx))
      panelLeft.value = startLeft + (startW - nextW)
      panelWidth.value = nextW
    }
    if (edge.includes('e')) {
      const minW = minPanelWidth()
      const maxW = Math.min(maxPanelWidth(), window.innerWidth - startLeft - PANEL_MARGIN)
      panelWidth.value = Math.min(maxW, Math.max(minW, startW + dx))
    }
    if (edge.includes('n')) {
      const minH = minPanelHeight()
      const maxH = Math.min(maxPanelHeight(), startTop + startH - minPanelTop.value)
      const nextH = Math.min(maxH, Math.max(minH, startH - dy))
      panelTop.value = startTop + (startH - nextH)
      panelHeight.value = nextH
    }
    if (edge.includes('s')) {
      panelHeight.value = Math.min(maxPanelHeight(), Math.max(minPanelHeight(), startH + dy))
    }
  }
  const onMove = (ev) => {
    latestEv = ev
    if (!rafId) rafId = requestAnimationFrame(apply)
  }
  const stop = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
    if (rafId) {
      cancelAnimationFrame(rafId)
      apply()
    }
    document.body.classList.remove('panel-dragging')
    panelMovedByUser.value = true
    clampPanelSize()
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', stop)
  window.addEventListener('pointercancel', stop)
}

function startPanelDrag(e) {
  if (isMaximized.value) return
  if (e.button !== undefined && e.button !== 0) return
  if (e.target?.closest?.('button, input, textarea, select, a, [data-no-drag]')) return
  e.preventDefault()
  const startX = e.clientX
  const startY = e.clientY
  const startLeft = panelLeft.value
  const startTop = panelTop.value
  let rafId = 0
  let latestEv = null
  document.body.classList.add('panel-dragging')
  const apply = () => {
    rafId = 0
    if (!latestEv) return
    const dx = latestEv.clientX - startX
    const dy = latestEv.clientY - startY
    panelLeft.value = Math.min(maxPanelLeft(), Math.max(minPanelLeft.value, startLeft + dx))
    panelTop.value = Math.min(maxPanelTop(), Math.max(minPanelTop.value, startTop + dy))
  }
  const onMove = (ev) => {
    latestEv = ev
    if (!rafId) rafId = requestAnimationFrame(apply)
  }
  const stop = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
    if (rafId) {
      cancelAnimationFrame(rafId)
      apply()
    }
    document.body.classList.remove('panel-dragging')
    panelMovedByUser.value = true
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', stop)
  window.addEventListener('pointercancel', stop)
}

function toggleMaximizePanel() {
  if (isMaximized.value) {
    panelWidth.value = restoredPanel.value.width
    panelHeight.value = restoredPanel.value.height
    panelLeft.value = restoredPanel.value.left
    panelTop.value = restoredPanel.value.top
    isMaximized.value = false
    clampPanelSize()
    return
  }
  restoredPanel.value = {
    width: panelWidth.value,
    height: panelHeight.value,
    left: panelLeft.value,
    top: panelTop.value,
  }
  panelWidth.value = maximizedPanelWidth()
  panelHeight.value = maximizedPanelHeight()
  panelLeft.value = maximizedPanelLeft()
  panelTop.value = maximizedPanelTop()
  isMaximized.value = true
}

function applyDefaultThinking(tab) {
  if (!tab) return
  tab.thinkingEnabled = Boolean(defaultThinkingEnabled.value)
  tab.thinkingEffort = normalizeThinkingEffort(defaultThinkingEffort.value)
}

function applySessionThinking(r, tab) {
  if (!tab || !r) return
  if (r.thinking_enabled !== undefined) {
    tab.thinkingEnabled = Boolean(r.thinking_enabled)
  }
  if (r.thinking_effort !== undefined && r.thinking_effort !== null && String(r.thinking_effort).trim()) {
    tab.thinkingEffort = normalizeThinkingEffort(r.thinking_effort)
  }
}

async function persistSessionThinking(tab, enabled, effort) {
  if (!tab) return
  const previousEnabled = Boolean(tab.thinkingEnabled)
  const previousEffort = normalizeThinkingEffort(tab.thinkingEffort)
  tab.thinkingEnabled = Boolean(enabled)
  tab.thinkingEffort = normalizeThinkingEffort(effort)
  try {
    const r = await api.setAssistantThinking(tab.sessionId, tab.thinkingEnabled, tab.thinkingEffort)
    if (!r?.ok) throw new Error(r?.error || 'Failed to update thinking')
    applySessionThinking(r, tab)
  } catch (e) {
    tab.thinkingEnabled = previousEnabled
    tab.thinkingEffort = previousEffort
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function applyThinkingStep(step) {
  const tab = activeTab.value
  if (!tab || tab.sending) return
  if (step === 'off') {
    void persistSessionThinking(tab, false, tab.thinkingEffort)
    return
  }
  void persistSessionThinking(tab, true, step)
}

function toggleThinkingMenu() {
  showThinkingMenu.value = !showThinkingMenu.value
  if (showThinkingMenu.value) {
    showToolList.value = false
    showTokenUsage.value = false
    showSessionList.value = false
  }
}

function onThinkingSliderInput(event) {
  const idx = Number(event.target.value)
  const step = thinkingMenuSteps.value[idx]
  if (step) applyThinkingStep(step.value)
}

async function loadAssistantDefaults() {
  // ``defaultWebSearchAllowed`` is ONLY the seed for brand-new sessions; it must
  // faithfully mirror the global ``assistant.enable_web_search`` setting. On a
  // timeout / error we keep the conservative initial value (false) instead of
  // fabricating ``true`` — fabricating on would otherwise make the toolbar lie
  // (show 开 while the backend is off) and, now that the toolbar value is sent
  // as an authoritative override, would silently enable web search. We also do
  // NOT reset an already-loaded tab's toggle here: a loaded session's value is
  // the highest-priority truth and comes from the backend (r.allow_web_search).
  try {
    const settings = await withTimeout(api.getSettings(), 3000, null)
    if (settings && settings.assistant) {
      defaultWebSearchAllowed.value = settings.assistant.enable_web_search !== false
      defaultAutoApproveMode.value = settings.assistant.auto_approve === true
      defaultThinkingEffort.value = normalizeThinkingEffort(settings.assistant.thinking_effort)
    }
  } catch (_) {
    /* keep the conservative default; the backend still uses the real setting */
  }
}

async function setWebSearchAllowed(enabled) {
  const tab = activeTab.value
  if (!tab || tab.sending) return
  const previous = tab.webSearchAllowed
  tab.webSearchAllowed = Boolean(enabled)
  tab.assistantTools = []
  try {
    const r = await api.setAssistantWebSearch(tab.sessionId, tab.webSearchAllowed)
    if (!r?.ok) throw new Error(r?.error || 'Failed to update web search')
    tab.webSearchAllowed = Boolean(r.allow_web_search)
    if (Array.isArray(r.tools)) tab.assistantTools = r.tools
  } catch (e) {
    tab.webSearchAllowed = previous
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function toggleWebSearch() {
  setWebSearchAllowed(!webSearchAllowed.value)
}

async function toggleGreenlightMode() {
  const tab = activeTab.value
  if (!tab || greenlightBusy.value || !greenlightAvailable.value) return
  const enabled = !engagementActive.value
  greenlightBusy.value = true
  try {
    const declaration = enabled
      ? { active: true, mode: 'greenlight', auth_type: 'other', scope: '*' }
      : { active: false }
    const r = await api.setAssistantEngagement(tab.sessionId, declaration)
    if (!r?.ok) throw new Error(r?.error || t('assistant.greenlightToggleFailed'))
    tab.engagement = enabled ? (r.engagement_declaration || { active: true, mode: 'greenlight', scope: '*' }) : null
    showToast(
      enabled ? t('assistant.greenlightEnabled') : t('assistant.greenlightDisabled'),
      'ok',
    )
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    greenlightBusy.value = false
  }
}

function applySessionAssistantPolicy(r, tab) {
  if (!tab || !r) return
  tab.autoAllowTools = Array.isArray(r.auto_allow_tools) ? r.auto_allow_tools : []
  if (r.auto_approve_mode !== undefined) {
    tab.autoApproveMode = Boolean(r.auto_approve_mode)
  }
  applySessionThinking(r, tab)
}

function findPendingUserDecisionBlock(tab = activeTab.value) {
  if (!tab) return null
  for (let i = (tab.messages || []).length - 1; i >= 0; i -= 1) {
    const msg = tab.messages[i]
    if (msg?.role === 'user') return null
    if (msg?.role !== 'assistant' || !Array.isArray(msg.blocks)) continue
    for (let j = msg.blocks.length - 1; j >= 0; j -= 1) {
      if (isPendingUserDecisionBlock(msg.blocks[j])) return msg.blocks[j]
    }
  }
  return null
}

function autoPickDecisionResponse(_block) {
  return t('assistant.autoApproveAgentDecide')
}

async function drainPendingAfterAutoApprove(tab = activeTab.value) {
  if (!tab || !tab.autoApproveMode || tab.confirmingAction) return
  if (activeTab.value !== tab) return
  if (tab.pendingTools?.length) {
    const decisions = { ...tab.pendingDecisions }
    for (const tool of tab.pendingTools) {
      const id = tool.id || tool.name
      decisions[id] = 'allow_session'
      const permKey = mcpPermissionKey(tool.name, tool.arguments || {})
      if (permKey) {
        const keys = tab.pendingAutoAllowPermissions || []
        if (!keys.includes(permKey)) {
          tab.pendingAutoAllowPermissions = [...keys, permKey]
        }
      } else if (tool.name && !(tab.pendingAutoAllowNames || []).includes(tool.name)) {
        tab.pendingAutoAllowNames = [...(tab.pendingAutoAllowNames || []), tool.name]
      }
    }
    tab.pendingDecisions = decisions
    await submitPendingBatch('allow')
    return
  }
  const block = findPendingUserDecisionBlock(tab)
  if (block) handleCardAction(autoPickDecisionResponse(block), block)
}

async function toggleAutoApproveMode() {
  const tab = activeTab.value
  if (!tab || autoApproveBusy.value) return
  const enabled = !tab.autoApproveMode
  if (enabled) {
    const ok = await confirmDialog({
      title: t('assistant.autoApproveConfirmTitle'),
      content: t('assistant.autoApproveConfirmContent'),
      positiveText: t('assistant.autoApproveConfirmAction'),
      negativeText: t('common.cancel'),
      type: 'warning',
    })
    if (!ok) return
  }
  autoApproveBusy.value = true
  const previous = Boolean(tab.autoApproveMode)
  tab.autoApproveMode = enabled
  try {
    const r = await api.setAssistantAutoApproveMode(tab.sessionId, enabled, false)
    if (!r?.ok) throw new Error(r?.error || t('assistant.autoApproveToggleFailed'))
    applySessionAssistantPolicy(r, tab)
    tab.autoApproveMode = r.auto_approve_mode !== undefined ? Boolean(r.auto_approve_mode) : enabled
    showToast(
      tab.autoApproveMode ? t('assistant.autoApproveEnabled') : t('assistant.autoApproveDisabled'),
      'ok',
    )
    if (tab.autoApproveMode) {
      await drainPendingAfterAutoApprove(tab)
    }
  } catch (e) {
    tab.autoApproveMode = previous
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    autoApproveBusy.value = false
  }
}

function riskText(risk) {
  const map = {
    high: t('assistant.riskHigh'),
    'medium-high': t('assistant.riskMediumHigh'),
    medium: t('assistant.riskMedium'),
    'medium-low': t('assistant.riskMediumLow'),
    low: t('assistant.riskLow'),
  }
  return map[risk] || risk || t('assistant.riskLow')
}

async function loadAssistantTools() {
  const tab = activeTab.value
  if (!tab) return
  if (tab.assistantTools.length || tab.toolsLoading) return
  tab.toolsLoading = true
  try {
    const r = await api.listAssistantTools(tab.sessionId)
    if (r.ok) tab.assistantTools = r.tools || []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    tab.toolsLoading = false
  }
}

async function toggleToolList() {
  showToolList.value = !showToolList.value
  showTokenUsage.value = false
  showThinkingMenu.value = false
  if (showToolList.value) {
    showSessionList.value = false
    await loadAssistantTools()
  }
}

function toggleTokenUsage() {
  showTokenUsage.value = !showTokenUsage.value
  if (showTokenUsage.value) {
    showToolList.value = false
    showSessionList.value = false
    showThinkingMenu.value = false
    void refreshActiveTokenUsage()
  }
}

function formatTokenCount(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n <= 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}K`
  return String(Math.round(n))
}

const CONTEXT_RING_R = 9
const CONTEXT_RING_C = 2 * Math.PI * CONTEXT_RING_R
const DEFAULT_CONTEXT_WINDOW = 131072
const _utf8Encoder = typeof TextEncoder === 'function' ? new TextEncoder() : null

function utf8ByteLength(text) {
  const value = String(text || '')
  if (!value) return 0
  if (_utf8Encoder) return _utf8Encoder.encode(value).length
  return value.length
}

function estimateStreamingExtraTokens(tab) {
  if (!tab?.sending && !tab?.activeStreamMessage) return 0
  const messages = tab?.messages || []
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role !== 'assistant') continue
    return Math.floor(utf8ByteLength(message?.text || message?.content || '') / 4)
  }
  return 0
}

const contextUsage = computed(() => {
  const tab = activeTab.value
  const windowSize = Number(tab?.contextWindow || 0) || DEFAULT_CONTEXT_WINDOW
  const backendUsed = Number(tab?.promptTokens || 0)
  const used = backendUsed + estimateStreamingExtraTokens(tab)
  const backendBytes = Number(tab?.promptBytes || 0)
  const percent = windowSize > 0 ? Math.min(100, (used / windowSize) * 100) : 0
  return {
    used,
    window: windowSize,
    bytes: Math.max(backendBytes, used * 4),
    percent,
    circ: CONTEXT_RING_C,
    offset: CONTEXT_RING_C * (1 - percent / 100),
    tone: percent >= 90 ? 'danger' : percent >= 70 ? 'warn' : 'ok',
    percentLabel: `${Math.round(percent)}%`,
    usedLabel: formatTokenCount(used),
    windowLabel: formatTokenCount(windowSize),
  }
})

// Canonical shape: argusai/core/ai.py's _normalize_*_cache_usage() /
// argusai/core/assistant/session.py's _TOKEN_USAGE_FIELDS.
function buildTokenUsageView(u) {
  if (!u) return null
  const input = Number(u.input_tokens || 0)
  const output = Number(u.output_tokens || 0)
  const hit = Number(u.cache_hit_tokens || 0)
  const miss = Number(u.cache_miss_tokens || 0)
  const write = Number(u.cache_write_tokens || 0)
  const reasoning = Number(u.reasoning_tokens || 0)
  const reply = Number(u.reply_tokens || Math.max(0, output - reasoning))
  const denom = hit + miss || 1
  return {
    total: input + output,
    input,
    output,
    hit,
    miss,
    write,
    reasoning,
    reply,
    calls: Number(u.calls || 0),
    hitPct: Math.round((hit / denom) * 1000) / 10,
    missPct: Math.round((miss / denom) * 1000) / 10,
    writePct: Math.round((write / denom) * 1000) / 10,
    hitRateLabel: (hit || miss) ? `${Math.round((hit / denom) * 1000) / 10}%` : '—',
  }
}

const tokenUsageView = computed(() => buildTokenUsageView(activeTab.value?.tokenUsage))

function isToolAutoAllowed(name) {
  return autoAllowTools.value.includes(name)
}

function setLocalToolAutoAllow(name, enabled, tab = activeTab.value) {
  if (!tab) return
  const set = new Set(tab.autoAllowTools)
  if (enabled) set.add(name)
  else set.delete(name)
  tab.autoAllowTools = Array.from(set).sort()
}

function isHighRiskTool(name) {
  const tool = assistantTools.value.find(t => t.name === name)
  return !!(tool && tool.requires_double_confirm)
}

function toolNeedsConfirmation(tool) {
  if (!tool) return false
  return tool.needs_confirmation !== false
}

const confirmableToolCount = computed(() =>
  assistantTools.value.reduce(
    (count, tool) => count + (toolNeedsConfirmation(tool) ? 1 : 0),
    0,
  ),
)

async function setAutoAllowTool(toolName, enabled) {
  const tab = activeTab.value
  if (!tab || tab.sending || tab.confirmingAction) return
  let force = false
  if (enabled && isHighRiskTool(toolName)) {
    const ok = await confirmDialog({
      title: t('assistant.toolHighRiskTitle'),
      content: t('assistant.toolHighRiskConfirm', { name: toolName }),
      positiveText: t('assistant.toolHighRiskAllow'),
      negativeText: t('common.cancel'),
      type: 'warning',
    })
    if (!ok) return
    force = true
  }
  const previous = [...tab.autoAllowTools]
  setLocalToolAutoAllow(toolName, enabled, tab)
  try {
    if (enabled && tab.pendingTool?.name === toolName) {
      await confirmPerm(true, true)
      return
    }
    const r = await api.setAssistantAutoAllow(tab.sessionId, toolName, enabled, force)
    if (!r.ok) throw new Error(r.error || 'Failed to update auto allow')
    tab.autoAllowTools = Array.isArray(r.auto_allow_tools) ? r.auto_allow_tools : []
  } catch (e) {
    tab.autoAllowTools = previous
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function setAllAutoAllow(enabled) {
  const tab = activeTab.value
  if (!tab || tab.sending || tab.confirmingAction) return
  let includeReverseDynamic = false
  let includeHighRisk = false
  if (enabled) {
    includeReverseDynamic = await confirmDialog({
      title: t('assistant.toolAllowAllReverseTitle'),
      content: t('assistant.toolAllowAllIncludeReverseDynamic'),
      positiveText: t('assistant.toolAllowAllIncludeReverseYes'),
      negativeText: t('assistant.toolAllowAllIncludeReverseNo'),
      type: 'warning',
    })
    includeHighRisk = await confirmDialog({
      title: t('assistant.toolAllowAllTitle'),
      content: t('assistant.toolAllowAllIncludeHighRisk'),
      positiveText: t('assistant.toolAllowAllIncludeYes'),
      negativeText: t('assistant.toolAllowAllIncludeNo'),
      type: 'warning',
    })
  }
  const previous = [...tab.autoAllowTools]
  try {
    const r = await api.setAssistantAutoAllowAll(tab.sessionId, enabled, includeHighRisk, includeReverseDynamic)
    if (!r.ok) throw new Error(r.error || 'Failed to update auto allow')
    tab.autoAllowTools = Array.isArray(r.auto_allow_tools) ? r.auto_allow_tools : []
    showToast(
      enabled
        ? t('assistant.toolAllowedAllToast', { n: tab.autoAllowTools.length })
        : t('assistant.toolManualAllToast'),
      'ok',
    )
  } catch (e) {
    tab.autoAllowTools = previous
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function isUiHiddenMessage(m) {
  if (m?.ui_hidden || m?.uiHidden || m?.hidden) return true
  // Typed injects use source=… (not content markers). Loom22 §9.
  const source = String(m?.source || '').toLowerCase()
  return source === 'cron' || source === 'confirmation'
}

function normalizeHistory(items) {
  const normalized = (items || []).map(m => {
    let text = m.content || m.text || ''
    // AI UI Protocol v2 §5.1: SelectionReference is structured metadata.
    // Backend `from_dict` migrates legacy v1 sessions on load, but we also
    // defensively re-derive here in case we're talking to an older backend
    // (e.g. during a rolling restart).
    let selectionReference = m.selection_reference || m.selectionReference || null
    if (selectionReference && typeof selectionReference === 'object') {
      selectionReference = normalizeSelectionReference(selectionReference)
    }
    if (!selectionReference && m.role === 'user' && typeof text === 'string') {
      const derived = deriveSelectionReferenceFromContent(text)
      if (derived) {
        selectionReference = derived
        text = stripSelectionReferenceFromContent(text)
      }
    }
    const uiHidden = isUiHiddenMessage(m)
    return {
      _uid: newMessageUid(),
      role: m.role,
      text,
      toolCalls: m.tool_calls || m.toolCalls || [],
      options: m.options || [],
      // AI UI Protocol v2: backfill id/version/source on legacy persisted blocks.
      blocks: normalizeBlocks(Array.isArray(m.blocks) ? m.blocks : []),
      toolTrace: m.tool_trace || m.toolTrace || [],
      attachments: (m.attachments || []).map(a => normalizeAttachment(a)),
      voice: m.voice ? normalizeVoice(m.voice) : null,
      selectionReference,
      source: m.source || '',
      hidden: Boolean(m.hidden) || uiHidden,
      ui_hidden: uiHidden,
      ts: m.timestamp ? m.timestamp * 1000 : Date.now(),
      tokenUsage: m.token_usage && typeof m.token_usage === 'object' ? { ...m.token_usage } : null,
    }
  }).filter(m => !m.hidden && !m.ui_hidden)
  const seen = new Set()
  for (let i = normalized.length - 1; i >= 0; i--) {
    const msg = normalized[i]
    if (!Array.isArray(msg.blocks) || msg.blocks.length === 0) continue
    const filtered = []
    for (const block of msg.blocks) {
      const id = singletonBlockId(block)
      if (id) {
        if (seen.has(id)) continue
        seen.add(id)
      }
      filtered.push(block)
    }
    if (filtered.length !== msg.blocks.length) msg.blocks = filtered
  }
  let pendingDecisionSeen = false
  for (let i = normalized.length - 1; i >= 0; i--) {
    const msg = normalized[i]
    if (!Array.isArray(msg.blocks) || msg.blocks.length === 0) continue
    const filtered = []
    for (let bi = msg.blocks.length - 1; bi >= 0; bi--) {
      const block = msg.blocks[bi]
      if (block?.superseded) continue
      if (isPendingUserDecisionBlock(block)) {
        if (pendingDecisionSeen) continue
        pendingDecisionSeen = true
      }
      filtered.unshift(block)
    }
    if (filtered.length !== msg.blocks.length) msg.blocks = filtered
  }
  const merged = mergeToolOnlyAssistantMessages(normalized)
  let lastVisibleRole = null
  for (const msg of merged) {
    if (msg.hidden) continue
    if (msg.role === 'assistant' && lastVisibleRole === 'assistant') {
      msg.agentStep = true
    }
    lastVisibleRole = msg.role
  }
  return merged
}

function mergeToolOnlyAssistantMessages(items) {
  const out = []
  for (const msg of items) {
    if (
      out.length
      && msg.role === 'assistant'
      && !msg.hidden
      && isToolOnlyAssistantMessage(msg)
      && out[out.length - 1].role === 'assistant'
      && !out[out.length - 1].hidden
    ) {
      const prev = out[out.length - 1]
      prev.blocks = [...(prev.blocks || []), ...(msg.blocks || [])]
      prev.toolCalls = [...(prev.toolCalls || []), ...(msg.toolCalls || [])]
      prev.toolTrace = [...(prev.toolTrace || []), ...(msg.toolTrace || [])]
      continue
    }
    out.push(msg)
  }
  return out
}

function scrollToBottomNow() {
  if (!msgContainer.value) return
  msgContainer.value.scrollTop = msgContainer.value.scrollHeight
}

function isNearBottom(threshold = 80) {
  const el = msgContainer.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
}

function onMessageScroll() {
  autoFollowScroll.value = isNearBottom(80)
}

function scrollBottom({ force = false } = {}) {
  if (!force) return
  nextTick(() => {
    scrollToBottomNow()
    requestAnimationFrame(() => {
      scrollToBottomNow()
      setTimeout(scrollToBottomNow, 80)
    })
  })
}

function scrollBottomAfterRender() {
  const targetSessionId = activeTabId.value
  const targetTab = tabs.find(tab => tab.sessionId === targetSessionId)
  if (targetTab) targetTab.autoFollowScroll = true
  const scrollIfCurrent = () => {
    if (activeTabId.value !== targetSessionId) return
    if (!autoFollowScroll.value) return
    scrollToBottomNow()
  }
  nextTick(() => {
    scrollIfCurrent()
    requestAnimationFrame(() => {
      scrollIfCurrent()
      requestAnimationFrame(() => {
        scrollIfCurrent()
      })
    })
    // Markdown, cards and virtualized history can change height after their
    // first paint. Keep the selected conversation pinned through settling.
    for (const delay of [80, 180, 350, 650]) {
      setTimeout(scrollIfCurrent, delay)
    }
  })
}

let bodyResizeObserver = null
function bindBodyResizeObserver() {
  bodyResizeObserver?.disconnect()
  bodyResizeObserver = null
  const el = msgContainer.value
  if (!el || typeof ResizeObserver === 'undefined') return
  bodyResizeObserver = new ResizeObserver(() => {
    if (!autoFollowScroll.value) return
    scrollToBottomNow()
  })
  bodyResizeObserver.observe(el)
  // Observe the first child too — height growth often comes from content,
  // not the scroll container itself.
  if (el.firstElementChild) bodyResizeObserver.observe(el.firstElementChild)
}

function getInputPlainText(el) {
  return String(el?.textContent || '').replace(/\u200B/g, '').trim()
}

/** CE often keeps a lone <br>; scrollHeight then inflates to 2+ lines while visually empty. */
function isInputVisuallyEmpty(el) {
  if (!el) return true
  if (el.querySelector?.('.input-inline-path-chip')) return false
  return !getInputPlainText(el)
}

function resizeAssistantInput() {
  const el = inputEl.value
  if (!el) return
  const style = window.getComputedStyle(el)
  const lineHeight = parseFloat(style.lineHeight) || 20
  const paddingY = parseFloat(style.paddingTop || '0') + parseFloat(style.paddingBottom || '0')
  const minHeight = Math.round(lineHeight + paddingY)
  const maxHeight = Math.round(lineHeight * 5 + paddingY)

  if (isInputVisuallyEmpty(el)) {
    el.style.height = `${minHeight}px`
    el.style.overflowY = 'hidden'
    return
  }

  el.style.height = `${minHeight}px`
  const measured = Math.min(Math.max(el.scrollHeight, minHeight), maxHeight)
  el.style.height = `${measured}px`
  el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden'
}

function withTimeout(promise, ms, fallback = null) {
  return Promise.race([
    promise,
    new Promise(resolve => setTimeout(() => resolve(fallback), ms)),
  ])
}

async function send(msg, options = {}) {
  const tab = activeTab.value
  if (!tab) return
  const displayText = (msg || tab.inputDraft || '').trim()
  const pathRefsSnapshot = { ...(tab.inputPathRefs || {}) }
  const text = composeInputForSend(displayText, tab.inputPathRefs || {})
  // AI UI Protocol v2 §5.1: the selection rides as a top-level field on the
  // ChatMessage, never spliced into `content`. Backend renders the prompt
  // block at LLM-call time so persistence stays clean.
  const selectionReference = normalizeSelectionReference(tab.previewSelectionContext)
  const outboundText = text
  // Checked before the busy guard below: asking while the agent works is the
  // entire reason `/btw` exists.
  const sideMatch = /^\/(?:btw|side)\b([\s\S]*)$/i.exec(displayText)
  if (sideMatch) {
    void askSideQuestion(sideMatch[1] || '')
    return
  }
  if (voiceState.value === 'recording') voiceInput.cancel()

  const voicePayload = wireOutboundVoice(tab.pendingVoice)
  const attachments = (tab.pendingAttachments || [])
    .filter(a => ['image', 'video', 'text', 'file'].includes(a.type))
    .map(a => {
      const base = {
        id: a.id,
        type: a.type,
        mime: a.mime,
        name: a.name,
        size: a.size,
      }
      if (a.type === 'image' && a.data_base64) base.data_base64 = a.data_base64
      else if (a.type === 'video') {
        base.data_base64 = a.data_base64
        if (a.duration_ms) base.duration_ms = a.duration_ms
      } else if (a.type === 'text') base.text = a.text
      else if (a.type === 'file') {
        if (a.data_base64) base.data_base64 = a.data_base64
        if (a.host_path || a.path) base.host_path = a.host_path || a.path
        if (a.sha256) base.sha256 = a.sha256
        if (a.caption) base.caption = a.caption
      }
      if ((a.type === 'image' || a.type === 'video' || a.type === 'text') && (a.host_path || a.path)) {
        base.host_path = a.host_path || a.path
      }
      return base
    })
  if (tab.sending || tab.confirmingAction) {
    queueUserInput(displayText)
    return
  }
  if (!outboundText && !attachments.length && !voicePayload) return
  tab._historyLoaded = true
  if (!options.hideUserBubble && isDefaultTabName(tab.name)) {
    tab.name = deriveSessionName(text || outboundText, attachments, tab.pendingVoice)
    persistTabs()
  }
  tab.inputDraft = ''
  tab.inputPathRefs = {}
  tab.previewSelectionContext = null
  nextTick(resizeAssistantInput)
  const pendingVoiceSnapshot = tab.pendingVoice
  tab.pendingAttachments = []
  tab.pendingVoice = null
  const msgExtra = {}
  if (selectionReference) msgExtra.selectionReference = selectionReference
  if (pendingVoiceSnapshot) msgExtra.voice = pendingVoiceSnapshot
  let userIndex = -1
  if (!options.hideUserBubble) {
    userIndex = addMsg('user', text, null, null, null, null, attachments, tab, msgExtra)
  }
  // attach the structured reference on the wire; backend persists it as
  // ChatMessage.selection_reference instead of mixing it into content.
  const wireSelectionReference = selectionReference ? JSON.parse(JSON.stringify(selectionReference)) : null
  let assistantIndex = addMsg('assistant', '', null, null, null, null, null, tab)
  updateMsg(assistantIndex, { status: t('assistant.startingAssistant') }, tab)

  tab.runSeq += 1
  tab.sending = true
  tab.activeStreamMessage = true
  tab.lastError = ''
  clearPendingTools()
  recomputeBadge(tab)
  try {
    updateMsg(assistantIndex, { status: t('assistant.preparingContext') }, tab)
    const context = await buildContextSnapshot()
    updateMsg(assistantIndex, { status: t('assistant.startingAssistant') }, tab)
    const started = await api.startAssistantStream({
      message: outboundText,
      session_id: tab.sessionId,
      page: props.page || '',
      context: plainJson(context),
      thinking_enabled: thinkingEnabled.value,
      thinking_effort: thinkingEffort.value,
      attachments,
      voice: voicePayload,
      selection_reference: wireSelectionReference,
      hide_user_message: Boolean(options.hideUserBubble),
      allow_web_search: Boolean(tab.webSearchAllowed),
      runtime: plainJson(tab.runtime),
    })
    if (!started.ok && /already running/i.test(String(started.error || ''))) {
      // A channel message or a cron job owns this conversation right now. Roll
      // the bubbles back and hand the text to the composer, so nothing is lost
      // and no second turn races the running one.
      tab.messages.splice(assistantIndex, 1)
      if (userIndex >= 0) tab.messages.splice(userIndex, 1)
      tab.inputDraft = displayText
      tab.inputPathRefs = pathRefsSnapshot
      tab.sending = false
      tab.activeStreamMessage = false
      recomputeBadge(tab)
      nextTick(resizeAssistantInput)
      showToast(t('assistant.busyElsewhere'), 'error')
      return
    }
    if (!started.ok) throw new Error(started.error || 'Failed to start assistant stream')
    if (started.resolved_external) {
      tab.messages.splice(assistantIndex, 1)
      tab.confirmingAction = false
      recomputeBadge(tab)
      return
    }
    tab.liveJob = started.job || null
    if (tab === activeTab.value) autoFollowScroll.value = true
    await runAssistantStream(tab, started.run_id, assistantIndex, { userIndex })
  } catch (e) {
    updateMsg(assistantIndex, { text: 'Error: ' + (e.message || e) }, tab)
    tab.lastError = String(e.message || e)
    tab.sending = false
    tab.activeStreamMessage = false
    recomputeBadge(tab)
  }
}

function queueUserInput(raw = '') {
  const tab = activeTab.value
  if (!tab) return
  const displayText = String(raw || tab.inputDraft || '').trim()
  const pathRefs = { ...(tab.inputPathRefs || {}) }
  const text = composeInputForSend(displayText, pathRefs)
  if (!text) return
  tab.queuedInputs = [...(tab.queuedInputs || []), { text, displayText, pathRefs, ts: Date.now() }]
  tab.inputDraft = ''
  tab.inputPathRefs = {}
  if (tab.sending) {
    api.pauseAssistantAfterCurrentResponse(tab.sessionId).catch(() => {})
  }
  nextTick(resizeAssistantInput)
  scrollBottom({ force: true })
}

function removeQueuedInput(index) {
  const tab = activeTab.value
  if (!tab) return
  tab.queuedInputs = (tab.queuedInputs || []).filter((_, i) => i !== index)
}

function updateQueuedDisplayText(index, displayText) {
  const tab = activeTab.value
  if (!tab) return
  const items = [...(tab.queuedInputs || [])]
  const prev = items[index]
  if (!prev) return
  const nextDisplay = String(displayText ?? '')
  const pathRefs = { ...(prev.pathRefs || {}) }
  items[index] = {
    ...prev,
    displayText: nextDisplay,
    text: composeInputForSend(nextDisplay, pathRefs),
    pathRefs,
  }
  tab.queuedInputs = items
}

function queuedItemDisplayText(item) {
  if (!item) return ''
  if (item.displayText != null && String(item.displayText).trim()) return String(item.displayText)
  return String(item.text || '')
}

function drainQueuedInput() {
  const tab = activeTab.value
  if (!tab) return
  if (tab.sending || tab.confirmingAction || tab.pendingTools.length) return
  const next = (tab.queuedInputs || []).shift()
  if (!next || !String(next.text || '').trim()) return
  setTimeout(() => {
    if (!tab.sending && !tab.confirmingAction && !tab.pendingTools.length) {
      send(next.text)
    } else {
      tab.queuedInputs = [next, ...tab.queuedInputs]
    }
  }, 0)
}

/** Drop the current assistant turn and send a queued message immediately. */
async function sendQueuedNow(index) {
  const tab = activeTab.value
  if (!tab) return
  const items = [...(tab.queuedInputs || [])]
  const item = items[index]
  if (!item) return
  const text = String(item.text || '').trim()
  if (!text) return

  tab.queuedInputs = items.filter((_, i) => i !== index)

  const busy = tab.sending || tab.confirmingAction || (tab.pendingTools?.length > 0)
  if (busy) {
    cancelRun()
    await nextTick()
    await new Promise(resolve => setTimeout(resolve, 0))
  }

  if (tab.sending || tab.confirmingAction || tab.pendingTools?.length > 0) {
    tab.queuedInputs = [...(tab.queuedInputs || []), item]
    showToast(t('assistant.assistantClosingWait'), 'info')
    return
  }

  await send(text)
}

function previousUserMessage(index) {
  for (let i = index - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m?.role === 'user' && (m.text || '').trim()) return m
  }
  return null
}

function lastAssistantIndex() {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i]?.role === 'assistant') return i
  }
  return -1
}

function canContinueFrom(index) {
  if (sending.value || confirmingAction.value) return false
  if (pendingTools.value.length) return false
  if (index !== lastAssistantIndex()) return false
  const m = messages.value[index]
  if (!m || m.role !== 'assistant') return false
  return !!String(m.text || '').trim()
}

function continueFromHere() {
  if (sending.value || confirmingAction.value) return
  send(t('assistant.continue'))
}

function copyMessageText(text) {
  if (!text) return
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
    showToast(t('assistant.copied'), 'ok')
  }
}

function closeMessageContextMenu() {
  messageContextMenu.open = false
  messageContextMenu.uid = null
}

function messageByUid(uid, tab = activeTab.value) {
  if (!tab || uid == null) return null
  return (tab.messages || []).find(message => message?._uid === uid) || null
}

function openMessageContextMenu(event, message) {
  if (!message || message.role !== 'user') return
  event.preventDefault()
  event.stopPropagation()
  messageContextMenu.open = true
  messageContextMenu.x = Number(event.clientX || 0)
  messageContextMenu.y = Number(event.clientY || 0)
  messageContextMenu.uid = message._uid
}

function isEditingUserMessage(message) {
  return Boolean(message && editingUserMessageUid.value === message._uid)
}

function beginEditUserMessage(message) {
  if (!message || message.role !== 'user') return
  editingUserMessageUid.value = message._uid
  editingUserMessageText.value = String(message.text || '')
  nextTick(() => {
    document.querySelector('.message-edit-textarea')?.focus()
  })
}

function cancelEditUserMessage() {
  editingUserMessageUid.value = null
  editingUserMessageText.value = ''
}

async function regenerateFromUser(userMessage, replacementText) {
  const tab = activeTab.value
  if (!tab || tab.sending || tab.confirmingAction) return
  if (tab.pendingTools?.length) return
  const text = String(replacementText || '').trim()
  const targetIndex = tab.messages.findIndex(message => message?._uid === userMessage?._uid)
  const targetTimestamp = Number(userMessage?.ts || 0) / 1000
  if (!text || targetIndex < 0 || !Number.isFinite(targetTimestamp) || targetTimestamp <= 0) {
    showToast(t('assistant.locateMessageFailed'), 'error')
    return
  }

  tab.runSeq += 1
  tab.sending = true
  tab.activeStreamMessage = false
  tab.lastError = ''
  recomputeBadge(tab)
  let assistantIndex = -1
  try {
    const context = await buildContextSnapshot()
    const started = await api.startAssistantStream({
      message: text,
      session_id: tab.sessionId,
      page: props.page || '',
      context: plainJson(context),
      regenerate: true,
      regenerate_user_before: targetTimestamp,
      thinking_enabled: thinkingEnabled.value,
      thinking_effort: thinkingEffort.value,
      allow_web_search: Boolean(tab.webSearchAllowed),
      runtime: plainJson(tab.runtime),
    })
    if (!started.ok) throw new Error(started.error || 'Failed to start assistant stream')
    tab.liveJob = started.job || null

    const replacement = {
      ...tab.messages[targetIndex],
      text,
      status: '',
      startedAt: null,
      toolCalls: [],
      options: [],
      blocks: [],
      toolTrace: [],
    }
    tab.messages.splice(targetIndex, tab.messages.length - targetIndex, replacement)
    if (tab.renderWindowStart > targetIndex) {
      tab.renderWindowStart = Math.max(0, targetIndex)
    }
    cancelEditUserMessage()
    clearPendingTools()
    tab.confirmingAction = false
    tab.activeStreamMessage = true
    assistantIndex = addMsg('assistant', '', null, null, null, null, null, tab)
    updateMsg(assistantIndex, { status: t('assistant.startingAssistant') }, tab)
    if (tab === activeTab.value) autoFollowScroll.value = true
    await runAssistantStream(
      tab,
      started.run_id,
      assistantIndex,
      { regenerate: true, userIndex: targetIndex },
    )
  } catch (e) {
    if (assistantIndex >= 0) {
      updateMsg(assistantIndex, { text: 'Error: ' + (e.message || e), status: '' }, tab)
    } else {
      showToast(String(e.message || e), 'error')
    }
    tab.lastError = String(e.message || e)
    tab.sending = false
    tab.activeStreamMessage = false
    recomputeBadge(tab)
  }
}

async function regenerateMessage(index) {
  const original = messages.value[index]
  const user = previousUserMessage(index)
  if (!original || original.role !== 'assistant' || !user) return
  await regenerateFromUser(user, user.text)
}

async function submitEditedUserMessage(message) {
  if (!isEditingUserMessage(message)) return
  const text = editingUserMessageText.value.trim()
  if (!text) return
  await regenerateFromUser(message, text)
}

async function reviewBypassUserMessage(message) {
  const tab = activeTab.value
  if (!tab || !message || tab.sending || tab.confirmingAction || tab.pendingTools?.length) return
  greenlightBusy.value = true
  try {
    const r = await api.setAssistantEngagement(tab.sessionId, {
      active: true,
      mode: 'greenlight',
      goal_text: String(message.text || ''),
    })
    if (!r?.ok) throw new Error(r?.error || t('assistant.reviewBypassFailed'))
    tab.engagement = r.engagement_declaration || { active: true, mode: 'greenlight', scope: '*' }
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
    return
  } finally {
    greenlightBusy.value = false
  }
  await regenerateFromUser(message, message.text)
}

async function handleMessageContextAction(actionId) {
  const message = messageByUid(messageContextMenu.uid)
  closeMessageContextMenu()
  if (!message) return
  if (actionId === 'copy') {
    copyMessageText(message.text)
  } else if (actionId === 'edit-regenerate') {
    beginEditUserMessage(message)
  } else if (actionId === 'review-bypass') {
    await reviewBypassUserMessage(message)
  }
}

async function buildContextSnapshot() {
  const ctx = plainJson(props.context || {})
  try {
    const snap = await api.getAppSnapshot()
    if (snap && typeof snap === 'object' && !snap.error) {
      return {
        ...ctx,
        screen_summary: snap.screen_summary,
        page_label: snap.page_label,
        route: snap.route || ctx.route,
        current_view: snap.current_view,
        current_task: snap.current_task || snap.current_view?.current_task,
        ui_theme: snap.ui_theme || snap.current_view?.ui_theme || ctx.ui_theme,
        overlays: snap.overlays || ctx.overlays,
        visible_tasks: snap.visible_tasks,
        proxy_status: snap.proxy,
      }
    }
  } catch (_) {
    /* fallback to route-only context */
  }
  return ctx
}

function plainJson(value) {
  try {
    return JSON.parse(JSON.stringify(value ?? {}))
  } catch {
    return {}
  }
}

function pendingKeyForBlock(block) {
  return block?.confirm_id || `${block?.name || ''}:${JSON.stringify(block?.arguments || {})}`
}

function matchingPendingTool(block) {
  const id = block?.confirm_id
  if (id) return pendingTools.value.find(t => t.id === id)
  return pendingTools.value.find(t => t.name === block?.name)
}

function isExternalPendingTool(tool) {
  return Boolean(tool?.external_confirmation)
}

function canResolvePendingWhileSending(tab) {
  return Boolean(
    tab?.pendingTools?.length
    && tab.pendingTools.every(isExternalPendingTool)
  )
}

function markExternalConfirmationResolved(tab, confirmation) {
  const decisions = confirmation?.type === 'batch_confirmation_decision'
    ? plainJson(confirmation.decisions || {})
    : { [String(confirmation?.call_id || '')]: String(confirmation?.verdict || '') }
  tab.messages = (tab.messages || []).map(message => ({
    ...message,
    blocks: (message.blocks || []).map(block => {
      const id = String(block?.confirm_id || '')
      const decision = decisions[id]
      return block?.external_confirmation && decision
        ? { ...block, decision }
        : block
    }),
  }))
}

function cardDecision(block) {
  if (!block?.confirm_value && !block?.cancel_value) return ''
  const direct = pendingDecisions.value[pendingKeyForBlock(block)]
  if (direct) return direct
  const pending = matchingPendingTool(block)
  if (!pending) return block?.decision || ''
  return pendingDecisions.value[pending.id || pending.name] || ''
}

function cardActionsDisabled(block) {
  if (isUserDecisionBlock(block)) return confirmingAction.value || Boolean(block.submitted_response)
  if (block?.type === 'data_table') return false
  if (!(block?.confirm_value || block?.cancel_value)) return sending.value || confirmingAction.value
  if (cardDecision(block)) return true
  const pending = matchingPendingTool(block)
  return confirmingAction.value || !pending || (sending.value && !isExternalPendingTool(pending))
}

async function confirmPerm(approved, autoAllow = false, block = null) {
  const tab = activeTab.value
  if (!tab || tab.confirmingAction || !tab.pendingTool) return
  const target = block ? matchingPendingTool(block) : tab.pendingTool
  if (!target || (tab.sending && !isExternalPendingTool(target))) return
  const targetId = target.id || target.name
  const key = block ? pendingKeyForBlock(block) : targetId

  if (!approved) {
    tab.pendingDecisions = { ...tab.pendingDecisions, [key]: 'deny' }
    await submitPendingBatch('deny')
    return
  }

  const verdict = autoAllow ? 'allow_session' : 'allow'
  tab.pendingDecisions = { ...tab.pendingDecisions, [key]: verdict }
  if (autoAllow) {
    // Prefer the fine-grained MCP permission key when the confirm card
    // carries one (set by ``_confirm_block`` for ``mcp_server_*`` calls).
    // Falling back to the tool-name list for legacy / non-MCP tools
    // keeps the previous behaviour intact.
    const permKey = block?.permission_key || mcpPermissionKey(target.name, target.arguments || {})
    if (permKey) {
      if (!tab.pendingAutoAllowPermissions.includes(permKey)) {
        tab.pendingAutoAllowPermissions = [...tab.pendingAutoAllowPermissions, permKey]
      }
    } else {
      setLocalToolAutoAllow(target.name, true, tab)
      if (!tab.pendingAutoAllowNames.includes(target.name)) {
        tab.pendingAutoAllowNames = [...tab.pendingAutoAllowNames, target.name]
      }
    }
  }

  if (tab.pendingTools.length > 1 && !pendingAllDecided.value) {
    return
  }

  await submitPendingBatch('allow')
}

function approveAllPending() {
  const tab = activeTab.value
  if (!tab || tab.confirmingAction || !tab.pendingTools.length) return
  if (tab.sending && !canResolvePendingWhileSending(tab)) return
  const decisions = { ...tab.pendingDecisions }
  for (const tool of tab.pendingTools) {
    const id = tool.id || tool.name
    if (!decisions[id]) decisions[id] = 'allow'
  }
  tab.pendingDecisions = decisions
  submitPendingBatch('allow')
}

function denyAllPending() {
  const tab = activeTab.value
  if (!tab || tab.confirmingAction || !tab.pendingTools.length) return
  if (tab.sending && !canResolvePendingWhileSending(tab)) return
  submitPendingBatch('deny')
}

async function submitPendingBatch(intent) {
  const tab = activeTab.value
  if (!tab || tab.confirmingAction || !tab.pendingTools.length) return
  const resolvesExternal = tab.sending && canResolvePendingWhileSending(tab)
  if (tab.sending && !resolvesExternal) return
  // Loom22 §9: typed confirmation RPC — never encode decisions in chat text.
  const pending = tab.pendingTools[0]
  const binding = {
    pending_id: String(pending?.pending_id || ''),
    version: Number(pending?.version || 0),
  }
  if (!binding.pending_id || !Number.isInteger(binding.version) || binding.version < 1) {
    showToast(t('assistant.confirmationExpired'), 'error')
    return
  }
  let confirmation
  if (intent === 'deny') {
    confirmation = { type: 'confirmation_decision', verdict: 'deny', ...binding }
  } else if (tab.pendingTools.length === 1) {
    const onlyId = tab.pendingTools[0].id || tab.pendingTools[0].name
    const verdict = tab.pendingDecisions[onlyId] || 'allow'
    confirmation = {
      type: 'confirmation_decision',
      call_id: String(tab.pendingTools[0].id || ''),
      verdict: verdict === 'allow_session' ? 'allow_session' : 'allow',
      ...binding,
    }
  } else {
    const decisions = {}
    for (const tool of tab.pendingTools) {
      const id = tool.id || tool.name
      decisions[tool.id || ''] = tab.pendingDecisions[id] || 'allow'
    }
    confirmation = {
      type: 'batch_confirmation_decision',
      decisions,
      auto_allow_names: tab.pendingAutoAllowNames,
      auto_allow_permissions: tab.pendingAutoAllowPermissions,
      ...binding,
    }
  }

  tab.confirmingAction = true
  if (resolvesExternal) {
    try {
      const resolved = await api.startAssistantActionStream({
        session_id: tab.sessionId,
        action: confirmation,
      })
      if (!resolved?.ok) throw new Error(resolved?.error || t('assistant.confirmationExpired'))
      if (!resolved.resolved_external) {
        throw new Error(t('assistant.confirmationExpired'))
      }
      markExternalConfirmationResolved(tab, confirmation)
      clearPendingTools()
    } catch (error) {
      tab.lastError = String(error?.message || error)
      showToast(tab.lastError, 'error')
    } finally {
      tab.confirmingAction = false
      recomputeBadge(tab)
    }
    return
  }
  clearPendingTools()
  let assistantIndex = addMsg('assistant', '', null, null, null, null, null, tab)
  updateMsg(assistantIndex, { status: t('assistant.startingAssistant') }, tab)
  tab.runSeq += 1
  tab.sending = true
  tab.activeStreamMessage = true
  tab.lastError = ''
  recomputeBadge(tab)
  try {
    updateMsg(assistantIndex, { status: t('assistant.preparingContext') }, tab)
    const context = await buildContextSnapshot()
    updateMsg(assistantIndex, { status: t('assistant.startingAssistant') }, tab)
    const started = await api.startAssistantActionStream({
      session_id: tab.sessionId,
      job_id: String(tab.liveJob?.job_id || ''),
      page: props.page || '',
      context: plainJson(context),
      thinking_enabled: thinkingEnabled.value,
      thinking_effort: thinkingEffort.value,
      action: confirmation,
      allow_web_search: Boolean(tab.webSearchAllowed),
      runtime: plainJson(tab.runtime),
    })
    if (!started.ok) throw new Error(started.error || 'Failed to start assistant stream')
    tab.liveJob = started.job || null
    if (tab === activeTab.value) autoFollowScroll.value = true
    await runAssistantStream(tab, started.run_id, assistantIndex)
  } catch (e) {
    updateMsg(assistantIndex, { text: 'Error: ' + (e.message || e) }, tab)
    tab.lastError = String(e.message || e)
    tab.sending = false
    tab.activeStreamMessage = false
    tab.confirmingAction = false
    recomputeBadge(tab)
  }
}

async function openSandboxFolder() {
  const tab = activeTab.value
  if (!tab?.sessionId) return
  try {
    const r = await api.openAssistantSandbox(tab.sessionId)
    if (!r || !r.ok) throw new Error(r?.error || 'Failed to open sandbox folder')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

// Toggle the in-app sandbox file browser (right-side overlay). This is the
// "stay inside the assistant" alternative to ``openSandboxFolder`` which
// shells out to the OS file manager.
function toggleSandboxBrowser() {
  const tab = activeTab.value
  if (!tab?.sessionId) return
  sandboxBrowser.togglePanel(tab.sessionId)
}

// Cards (notably ``file_resource_view``) dispatch this when the user clicks
// "在沙盒中浏览". We open the panel, jump to the file, and flash-highlight it.
async function onOpenSandboxPanelEvent(e) {
  const tab = activeTab.value
  if (!tab?.sessionId) return
  const detail = e?.detail || {}
  const filePath = String(detail.file_path || detail.path || '').trim()
  if (filePath) {
    await sandboxBrowser.jumpToFile(tab.sessionId, filePath)
  } else {
    sandboxBrowser.openPanel(tab.sessionId)
  }
}

// Global `argusai:reference-file` bridge — cards that aren't a direct child of
// `<SandboxPanel>` (e.g. `file_resource_view` block inside a chat bubble,
// or the global MediaPreview's text snippet picker) dispatch a window event
// instead of the Vue emit. Routing both into `onSandboxReferenceFile` keeps
// the selection-context contract uniform.
// Other pages (e.g. the passive proxy's "Ask AI") drop a prepared prompt into the composer.
function onAssistantInsertTextEvent(e) {
  const detail = e?.detail || {}
  const text = String(detail.text || '').trim()
  if (!text) return
  if (detail.open && !props.visible) emit('toggle')
  nextTick(() => appendTextToInput(text))
}

function onReferenceFileEvent(e) {
  const detail = e?.detail || {}
  if (Array.isArray(detail.files) && detail.files.length) {
    onSandboxReferenceFile({ files: detail.files })
    return
  }
  if (!detail.file_path) return
  onSandboxReferenceFile({
    file_path: detail.file_path,
    file_name: detail.file_name || '',
    // The text-snippet path also forwards line / column range and the
    // selected text so the LLM sees the actual excerpt — not just the
    // file name — and the chip can label it as "file · L5:C1 - L8:C20".
    start_line: detail.start_line,
    start_column: detail.start_column,
    end_line: detail.end_line,
    end_column: detail.end_column,
    text: detail.text,
  })
}

// Sandbox panel emits "reference-file" — wire it into the existing selection
// pipeline used by message text / image references. The chip then appears
// above the input and the next ``send()`` ships it as ``selection_reference``.
function onSandboxReferenceFile(payload) {
  const tab = activeTab.value
  if (!tab) return
  const files = Array.isArray(payload?.files) ? payload.files : []
  if (files.length) {
    const refs = files
      .map(f => String(f.file_path || f.path || '').replace(/^\/+|\/+$/g, ''))
      .filter(Boolean)
      .slice(0, 50)
    if (!refs.length) return
    tab.previewSelectionContext = {
      kind: 'file_selection',
      file_path: refs[0],
      file_name: refs.length === 1
        ? refs[0].split('/').pop() || refs[0]
        : t('assistant.sandboxItemsCount', { count: refs.length }),
      files: refs.map(p => ({ file_path: p, file_name: p.split('/').pop() || p })),
      text: refs.map(p => `- ${p}`).join('\n'),
    }
    showToast(t('assistant.sandboxItemsReferenced', { count: refs.length }), 'ok')
    return
  }
  if (!payload?.file_path) return
  const filePath = String(payload.file_path).replace(/^\/+|\/+$/g, '')
  const fileName = payload.file_name || filePath.split('/').pop() || filePath
  // When the user selected a span of text (line range present), keep the
  // snippet so the AI sees exactly what was highlighted. Otherwise fall
  // back to the file name so the chip is still meaningful before any LLM
  // call (the backend later renders the structured ref into a prompt block).
  const hasSpan = Number(payload.start_line) > 0 && Number(payload.end_line) > 0
  const ctx = {
    kind: 'file_selection',
    file_path: filePath,
    file_name: fileName,
    text: hasSpan && payload.text ? payload.text : fileName,
  }
  if (hasSpan) {
    ctx.start_line = Number(payload.start_line)
    ctx.end_line = Number(payload.end_line)
    if (Number(payload.start_column) > 0) ctx.start_column = Number(payload.start_column)
    if (Number(payload.end_column) > 0) ctx.end_column = Number(payload.end_column)
  }
  tab.previewSelectionContext = ctx
  showToast(
    hasSpan
      ? t('assistant.fileLinesReferenced', {
          name: fileName,
          start: ctx.start_line,
          end: ctx.end_line,
        })
      : t('assistant.fileReferenced'),
    'ok',
  )
}

async function loadAgentRuntimeInventory() {
  agentRuntimesLoading.value = true
  try {
    const result = await api.listAgentRuntimes()
    if (!result?.ok) throw new Error(result?.error || t('assistant.runtimeLoadFailed'))
    agentRuntimeInventory.value = Array.isArray(result.runtimes) ? result.runtimes : []
    if (!agentRuntimeInventory.value.some(item => item.id === newSessionForm.runtime_id && item.probe?.available)) {
      newSessionForm.runtime_id = agentRuntimeInventory.value.find(item => item.probe?.available)?.id || 'argusai-native'
    }
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    agentRuntimesLoading.value = false
  }
}

async function chooseAgentProjectDirectory() {
  const result = await api.chooseAgentProjectDirectory(newSessionForm.project_dir || '')
  if (result?.ok) newSessionForm.project_dir = result.path || ''
  else if (!result?.cancelled) showToast(result?.error || t('assistant.projectDirectoryFailed'), 'error')
}

function modelGroupKey(model) {
  if (isAcpSession.value) {
    return `acp:${model.config_id || model.host || 'model'}`
  }
  return `ep:${model.index ?? (model.name || 'endpoint')}`
}

function modelGroupTitle(model) {
  if (isAcpSession.value) {
    return String(model.host || t('assistant.acpModel'))
  }
  return String(model.name || t('assistant.acpModel'))
}

function modelDisplayName(model) {
  if (isAcpSession.value) {
    return String(model.name || model.value || '')
  }
  return String(model.model || model.name || model.value || '')
}

const sessionModelGroups = computed(() => {
  const groups = []
  const byKey = new Map()
  for (const model of sessionModels.value) {
    const key = modelGroupKey(model)
    let group = byKey.get(key)
    if (!group) {
      group = { key, title: modelGroupTitle(model), models: [], active: false }
      byKey.set(key, group)
      groups.push(group)
    }
    group.models.push(model)
    if (model.active) group.active = true
  }
  return groups
})

const modelSearchQuery = computed(() => String(modelSearch.value || '').trim().toLowerCase())

function modelSearchHaystack(group, model) {
  return [
    group.title,
    modelDisplayName(model),
    model.model,
    model.name,
    model.value,
    model.host,
  ].filter(Boolean).join(' ').toLowerCase()
}

function modelMatchesQuery(group, model, query) {
  if (!query) return true
  const haystack = modelSearchHaystack(group, model)
  return query.split(/\s+/).every(token => haystack.includes(token))
}

const visibleSessionModelGroups = computed(() => {
  const query = modelSearchQuery.value
  return sessionModelGroups.value
    .map(group => {
      const models = query
        ? group.models.filter(model => modelMatchesQuery(group, model, query))
        : group.models
      return { ...group, models }
    })
    .filter(group => group.models.length)
})

function resetModelGroupCollapse() {
  const next = new Set()
  const groups = sessionModelGroups.value
  if (groups.length > 2) {
    for (const group of groups) {
      if (!group.active) next.add(group.key)
    }
  }
  collapsedModelGroups.value = next
}

function isModelGroupCollapsed(key) {
  if (modelSearchQuery.value) return false
  return collapsedModelGroups.value.has(key)
}

function clearModelSearch() {
  modelSearch.value = ''
}

watch(
  () => showModelModal.value && sessionModels.value.length > 0 && !modelsLoading.value,
  async (ready) => {
    if (!ready) return
    await nextTick()
    modelSearchInput.value?.focus?.()
  },
)

function onModelSearchKeydown(event) {
  if (event.key === 'Escape' && modelSearch.value) {
    event.preventDefault()
    clearModelSearch()
    return
  }
  if (event.key !== 'Enter' || event.isComposing) return
  const first = visibleSessionModelGroups.value[0]?.models[0]
  if (!first) return
  event.preventDefault()
  pickModel(first)
}

function toggleModelGroup(key) {
  const next = new Set(collapsedModelGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedModelGroups.value = next
}

function modelErrorText(code, detail) {
  const key = {
    acp_model_none: 'assistant.acpModelNone',
    acp_model_unavailable: 'assistant.acpModelUnavailable',
    acp_model_probe_failed: 'assistant.acpModelProbeFailed',
    acp_model_not_found: 'assistant.acpModelNotFound',
    acp_model_changed: 'assistant.acpModelChanged',
    endpoint_none: 'assistant.modelNoneNative',
    endpoint_not_found: 'assistant.acpModelNotFound',
    endpoint_changed: 'assistant.acpModelChanged',
  }[String(code || '')] || 'assistant.acpModelFailed'
  const text = t(key)
  return detail ? `${text} · ${detail}` : text
}

async function refreshNativeModel() {
  const tab = activeTab.value
  if (!tab || isAcpSession.value) return
  try {
    const result = await api.listSessionModels(tab.sessionId, false, '')
    tab.nativeModel = result?.ok ? String(result.active?.model || '') : ''
  } catch {
    tab.nativeModel = ''
  }
}

async function openModelPicker(refresh = false) {
  const tab = activeTab.value
  if (!tab) return
  const acp = isAcpSession.value
  showModelModal.value = true
  modelsLoading.value = true
  modelError.value = ''
  modelSearch.value = ''
  try {
    const result = await api.listSessionModels(
      tab.sessionId,
      refresh,
      acp ? (tab.runtime?.runtime_id || '') : '',
      acp ? (tab.runtime || {}) : {},
    )
    if (result?.ok) {
      sessionModels.value = Array.isArray(result.models) ? result.models : []
      resetModelGroupCollapse()
      if (acp && result.runtime && typeof result.runtime === 'object') {
        tab.runtime = { ...result.runtime }
        persistTabs()
      }
      if (result.stale) showToast(t('assistant.acpModelCached'), 'info')
      return
    }
    sessionModels.value = []
    collapsedModelGroups.value = new Set()
    modelError.value = modelErrorText(result?.code, result?.error)
  } catch (error) {
    sessionModels.value = []
    collapsedModelGroups.value = new Set()
    modelError.value = String(error?.message || error)
  } finally {
    modelsLoading.value = false
  }
}

async function pickModel(model) {
  const tab = activeTab.value
  if (!tab || !model || modelsLoading.value) return
  modelsLoading.value = true
  try {
    if (!isAcpSession.value) {
      const result = await api.switchSessionModel(tab.sessionId, model)
      if (!result?.ok) {
        showToast(modelErrorText(result?.code, ''), 'error')
        return
      }
      tab.nativeModel = String(result.model?.model || model.model || model.value || '')
    } else {
      // A conversation that has not run a turn yet has nothing on disk to
      // update. Its runtime dict is sent verbatim with the first turn, so
      // keeping the choice on the tab is what actually binds it.
      if (tab.runtime?.locked) {
        const result = await api.switchSessionModel(tab.sessionId, model)
        if (!result?.ok) {
          showToast(modelErrorText(result?.code, ''), 'error')
          return
        }
        if (result.runtime && typeof result.runtime === 'object') {
          tab.runtime = { ...result.runtime }
        }
      }
      const runtime = { ...(tab.runtime || {}) }
      runtime.acp_config = { ...(runtime.acp_config || {}), [model.config_id]: model.value }
      const options = runtime.acp_config_options || []
      runtime.acp_config_options = options.some(option => option.id === model.config_id)
        ? options.map(option => (
          option.id === model.config_id ? { ...option, current_value: model.value } : option
        ))
        // No snapshot yet on a conversation that has not run a turn. Record
        // enough of one that the composer can name the choice it just made.
        : [...options, {
          id: model.config_id,
          name: 'Model',
          type: 'select',
          category: 'model',
          current_value: model.value,
          options: [{ value: model.value, name: model.name }],
        }]
      tab.runtime = runtime
    }
    persistTabs()
    sessionModels.value = sessionModels.value.map(row => ({
      ...row,
      active: row.index === model.index && row.value === model.value,
    }))
    showToast(t('assistant.acpModelSwitched', {
      name: isAcpSession.value ? model.name : (model.model || model.name),
    }), 'ok')
    showModelModal.value = false
  } finally {
    modelsLoading.value = false
  }
}

async function addTab() {
  if (!canOpenNewTab.value) {
    showToast(t('assistant.tabMaxReached', { n: MAX_TABS }), 'info')
    return
  }
  newSessionForm.runtime_id = 'argusai-native'
  newSessionForm.workspace_mode = 'sandbox'
  newSessionForm.project_dir = ''
  await loadAgentRuntimeInventory()
  showNewSessionModal.value = true
}

function createAgentSessionTab() {
  if (newSessionForm.workspace_mode === 'project' && !newSessionForm.project_dir) {
    showToast(t('assistant.projectDirectoryRequired'), 'error')
    return
  }
  openTab({
    name: t('assistant.newSession'),
    runtime: {
      runtime_id: newSessionForm.runtime_id,
      workspace_mode: newSessionForm.workspace_mode,
      project_dir: newSessionForm.workspace_mode === 'project' ? newSessionForm.project_dir : '',
      locked: false,
    },
  })
  // Reset web search default to match the user's global preference.
  const tab = activeTab.value
  if (tab) {
    tab.webSearchAllowed = defaultWebSearchAllowed.value
    tab.autoApproveMode = defaultAutoApproveMode.value
    applyDefaultThinking(tab)
  }
  showNewSessionModal.value = false
  showSessionList.value = false
  showToolList.value = false
  showThinkingMenu.value = false
  showTokenUsage.value = false
  nextTick(() => document.getElementById('assistant-input')?.focus())
}

function closeTabSafe(targetSessionId, event) {
  event?.stopPropagation?.()
  const tab = tabs.find(t => t.sessionId === targetSessionId)
  if (!tab) return
  const keepsRunning = Boolean(tab.sending || tab.confirmingAction || tab.activeRunId)
  closeTab(targetSessionId)
  if (keepsRunning) showToast(t('assistant.tabClosedRunContinues'), 'info')
}

function selectTab(sessionId) {
  if (sessionId === activeTabId.value) return
  setActiveTab(sessionId)
  // Always re-anchor scroll after a tab swap so the user lands at the
  // newest message in the freshly-selected conversation.
  scrollBottomAfterRender()
  nextTick(() => document.getElementById('assistant-input')?.focus())
}

async function newSession() {
  // "New session" is now "open a new tab", aligning with the multi-tab UX.
  // Falling back to in-place reset if we've hit MAX_TABS.
  editingSessionId.value = ''
  sessionFilter.value = ''
  if (canOpenNewTab.value) {
    await addTab()
    showSessionList.value = false
    showToolList.value = false
    return
  }
  const tab = activeTab.value
  if (!tab) return
  const keepsRunning = Boolean(tab.sending || tab.activeRunId || tab.confirmingAction)
  tab.runSeq += 1
  tab.sending = false
  tab.activeStreamMessage = false
  tab.activeRunId = ''
  tab.liveJob = null
  tab.agentThreads = []
  tab.sessionId = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36)
  tab.name = '新会话'
  tab.webSearchAllowed = defaultWebSearchAllowed.value
  applyDefaultThinking(tab)
  tab.runtime = { runtime_id: 'argusai-native', workspace_mode: 'sandbox', project_dir: '', locked: false }
  tab.messages = []
  tab.renderWindowStart = 0
  tab.showAllMessages = false
  clearPendingTools()
  tab.queuedInputs = []
  tab.confirmingAction = false
  tab.autoAllowTools = []
  tab.autoApproveMode = defaultAutoApproveMode.value
  tab.nativeModel = ''
  tab.assistantTools = []
  tab._historyLoaded = false
  tab.lastError = ''
  recomputeBadge(tab)
  showSessionList.value = false
  showToolList.value = false
  persistTabs()
  refreshSessions(true)
  if (keepsRunning) showToast(t('assistant.tabClosedRunContinues'), 'info')
}

async function switchSession(id) {
  const tab = activeTab.value
  if (tab && isDiscardableNewTab(tab)) {
    replaceActiveTabSession(id)
    showSessionList.value = false
    showToolList.value = false
    await loadHistory()
    return
  }
  await openSessionInNewTab(id)
}

async function openSessionInNewTab(id, e) {
  e?.stopPropagation?.()
  const existing = tabs.find(t => t.sessionId === id)
  if (existing) {
    setActiveTab(id)
    showSessionList.value = false
    showToolList.value = false
    return
  }
  if (!canOpenNewTab.value) {
    showToast(t('assistant.tabMaxReached', { n: MAX_TABS }), 'info')
    return
  }
  const tab = openTab({ sessionId: id, name: '加载中...' })
  if (!tab) return
  showSessionList.value = false
  showToolList.value = false
  await loadHistory()
}

async function loadHistory() {
  const tab = activeTab.value
  if (!tab) return
  try {
    const r = await withTimeout(api.getAssistantSession(tab.sessionId), 1500, null)
    if (!r) throw new Error(t('assistant.historyLoadTimeout'))
    if (r.ok) {
      tab.messages = normalizeHistory(r.history)
      hydrateHistoryAttachments(tab.messages, tab.sessionId)
      restorePendingProjection(r, tab)
      applySessionTokenUsage(r, tab)
      applySessionAssistantPolicy(r, tab)
      tab.webSearchAllowed = Boolean(r.allow_web_search)
      tab.engagement = r.engagement_declaration || null
      if (r.runtime && typeof r.runtime === 'object') tab.runtime = { ...r.runtime }
      if (r.name) tab.name = r.name
      tab._historyLoaded = true
      tab.showAllMessages = false
      const len = tab.messages.length
      tab.renderWindowStart = len > RENDER_WINDOW_TRIGGER ? Math.max(0, len - RENDER_WINDOW_SIZE) : 0
      syncDesktopControlFromMessages(tab.messages)
      refreshDesktopControl()
      refreshRunJobs(tab)
      recomputeBadge(tab)
      persistTabs()
      scrollBottomAfterRender()
      attachExternalStream(tab, r.active_stream)
      if (tab.autoApproveMode) nextTick(() => drainPendingAfterAutoApprove(tab))
    }
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function cancelRun() {
  const tab = activeTab.value
  if (!tab) return
  const sid = String(tab.sessionId || '')
  tab.runSeq += 1
  tab.sending = false
  tab.activeStreamMessage = false
  tab.activeRunId = ''
  tab.liveJob = null
  tab.confirmingAction = false
  clearPendingTools()
  recomputeBadge(tab)

  const cancelLabel = t('assistant.cancelled')
  const cancelPrefix = t('assistant.cancelledPrefix')
  const legacyCancelPrefix = '[已中止]'
  const lastIdx = tab.messages.length - 1
  if (lastIdx >= 0 && tab.messages[lastIdx].role === 'assistant') {
    const prev = tab.messages[lastIdx]
    const prevText = String(prev.text || '')
    const unprefixedText = prevText.startsWith(legacyCancelPrefix)
      ? prevText.slice(legacyCancelPrefix.length).trimStart()
      : prevText
    const next = {
      ...prev,
      text: prevText
        ? (prevText.startsWith(cancelPrefix) ? prevText : `${cancelPrefix} ${unprefixedText}`)
        : cancelLabel,
      status: '',
      startedAt: null,
      cancelled: true,
    }
    tab.messages[lastIdx] = next
  } else {
    addMsg('assistant', cancelLabel, null, null, null, null, null, tab)
  }

  setTimeout(() => {
    api.cancelAssistantRun(sid)
      .catch(e => console.warn('cancelAssistantRun failed', e))
      .finally(() => { refreshRunJobs(tab).catch(() => {}) })
  }, 0)
}

function handleCardAction(value, block) {
  if (isUserDecisionBlock(block)) {
    if (block.submitted_response || confirmingAction.value) return
    markUserDecisionSubmitted(block, value)
    send(value, { hideUserBubble: true })
    return
  }
  if (value === 'yes' || value === 'allow') {
    if (!matchingPendingTool(block)) return
    confirmPerm(true, false, block)
  } else if (value === 'allow_session') {
    if (!matchingPendingTool(block)) return
    confirmPerm(true, true, block)
  } else if (value === 'no' || value === 'deny') {
    if (!matchingPendingTool(block)) return
    confirmPerm(false, false, block)
  }
  else send(value)
}

function handleToolDecision(payload, block) {
  const decision = String(payload?.decision || payload?.verdict || '').toLowerCase()
  if (['deny', 'decline', 'cancel'].includes(decision)) {
    confirmPerm(false, false, block)
    return
  }
  if (['allow_session', 'acceptforsession', 'accept_for_session'].includes(decision)) {
    confirmPerm(true, true, block)
    return
  }
  if (['allow', 'allow_once', 'accept'].includes(decision)) {
    confirmPerm(true, false, block)
  }
}

function handlePreviewSelectionEvent(payload) {
  if (!payload || !payload.text) {
    clearPreviewSelectionContext()
    return
  }
  previewSelectionContext.value = {
    file_path: String(payload.file_path || ''),
    file_name: String(payload.file_name || ''),
    start_line: Number(payload.start_line || 0),
    start_column: Number(payload.start_column || 0),
    end_line: Number(payload.end_line || 0),
    end_column: Number(payload.end_column || 0),
    text: String(payload.text || ''),
  }
}

function handleMessageTextSelection(event, message, absoluteIndex) {
  const root = event?.currentTarget
  const sel = window.getSelection?.()
  if (!root || !sel || !sel.rangeCount || sel.isCollapsed) return
  const range = sel.getRangeAt(0)
  if (!root.contains(range.commonAncestorContainer)) return
  const selectedText = String(sel.toString() || '').trim()
  if (!selectedText) return

  const startProbe = document.createRange()
  startProbe.selectNodeContents(root)
  startProbe.setEnd(range.startContainer, range.startOffset)
  const startIndex = startProbe.toString().length
  const endProbe = document.createRange()
  endProbe.selectNodeContents(root)
  endProbe.setEnd(range.endContainer, range.endOffset)
  const endIndex = endProbe.toString().length

  previewSelectionContext.value = {
    kind: 'message',
    role: message?.role === 'assistant' ? 'assistant' : 'user',
    message_index: absoluteIndex,
    start_index: startIndex,
    end_index: endIndex,
    text: selectedText,
  }
}

async function deleteSession(id) {
  const ok = await confirmDialog({
    title: t('assistant.sessionDeleteTitle'),
    content: t('assistant.sessionDeleteConfirm'),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  await api.deleteAssistantSession(id)
  // Close any open tab that pinned this session.
  const tab = tabs.find(item => item.sessionId === id)
  if (tab) closeTab(id)
  refreshSessions(true)
}

function resetTabToFreshSession(tab) {
  if (!tab) return
  tab.runSeq += 1
  tab.sending = false
  tab.activeStreamMessage = false
  tab.activeRunId = ''
  tab.sessionId = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36)
  tab.name = '新会话'
  tab.webSearchAllowed = defaultWebSearchAllowed.value
  applyDefaultThinking(tab)
  tab.runtime = { runtime_id: 'argusai-native', workspace_mode: 'sandbox', project_dir: '', locked: false }
  tab.messages = []
  tab.renderWindowStart = 0
  tab.showAllMessages = false
  clearPendingTools()
  tab.queuedInputs = []
  tab.confirmingAction = false
  tab.autoAllowTools = []
  tab.autoApproveMode = defaultAutoApproveMode.value
  tab.nativeModel = ''
  tab.assistantTools = []
  tab._historyLoaded = false
  tab.lastError = ''
  recomputeBadge(tab)
}

async function clearAllSessions() {
  const ok = await confirmDialog({
    title: t('assistant.clearAllSessionsTitle'),
    content: t('assistant.clearAllSessionsConfirm'),
    positiveText: t('assistant.clearAllSessionsOk'),
    negativeText: t('common.cancel'),
  })
  if (!ok) return
  try {
    const r = await api.clearAllAssistantSessions()
    if (!r?.ok) throw new Error(r?.error || t('assistant.clearFailed'))
    const n = Number(r.deleted_count || 0)
    for (const tab of [...tabs]) {
      resetTabToFreshSession(tab)
    }
    persistTabs()
    showSessionList.value = false
    await refreshSessions(true)
    showToast(t('assistant.clearAllSessionsDone', { n }), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function startEditSession(s, event) {
  event?.stopPropagation?.()
  editingSessionId.value = s.id
  sessionEditName.value = s.name || ''
  sessionEditNote.value = s.note || ''
  nextTick(() => {
    const el = document.querySelector('.session-edit-input')
    el?.focus?.()
    el?.select?.()
  })
}

function cancelEditSession(event) {
  event?.stopPropagation?.()
  editingSessionId.value = ''
  sessionEditName.value = ''
  sessionEditNote.value = ''
}

async function saveSessionMeta(s, event) {
  event?.stopPropagation?.()
  if (!s?.id || savingSessionMeta.value) return
  savingSessionMeta.value = true
  try {
    const r = await api.updateAssistantSession(s.id, {
      name: sessionEditName.value,
      note: sessionEditNote.value,
    })
    if (!r?.ok) throw new Error(r?.error || t('assistant.saveFailed'))
    const updated = r.session || {}
    s.name = updated.name || sessionEditName.value || s.name
    s.note = updated.note ?? sessionEditNote.value
    s.updated_at = updated.updated_at || s.updated_at
    const tab = tabs.find(t => t.sessionId === s.id)
    if (tab && updated.name) tab.name = updated.name
    editingSessionId.value = ''
    showToast(t('assistant.sessionMetaSaved'), 'ok')
    await refreshSessions(true)
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    savingSessionMeta.value = false
  }
}

function onKeydown(e) {
  if (voiceState.value === 'recording') {
    if (e.key === 'Escape') {
      e.preventDefault()
      cancelVoiceRecording()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void finishVoiceRecording()
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

function onGlobalKey(e) {
  if (e.ctrlKey && e.shiftKey && e.key === 'A') { e.preventDefault(); props.visible ? emit('close') : emit('toggle') }
  if (e.key === 'Escape' && sideQuestion.open) {
    e.preventDefault()
    closeSideQuestion()
    return
  }
  if (e.key === 'Escape' && agentPanelOpen.value) {
    e.preventDefault()
    closeAgentCenter()
    return
  }
  if (e.key === 'Escape' && (showToolList.value || showSessionList.value || showTokenUsage.value || showThinkingMenu.value)) {
    e.preventDefault()
    showToolList.value = false
    showSessionList.value = false
    showTokenUsage.value = false
    showThinkingMenu.value = false
    return
  }
  if (e.key === 'Escape' && messageContextMenu.open) {
    e.preventDefault()
    closeMessageContextMenu()
    return
  }
  // Ctrl+T (or Cmd+T) opens a new tab when the panel is visible. We don't
  // hijack the browser shortcut unless the assistant panel currently has
  // focus, so users can still open IDE/OS-level tabs elsewhere.
  if (props.visible && (e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === 't' || e.key === 'T')) {
    if (document.activeElement && (document.activeElement.id === 'assistant-input' || document.activeElement.closest?.('.assistant-panel'))) {
      e.preventDefault()
      addTab()
    }
  }
  // While recording, the contenteditable input is replaced by the capsule UI,
  // so per-input @keydown can't catch Esc/Enter. Promote them here so the
  // shortcuts work regardless of where focus lives inside the panel.
  if (props.visible && voiceState.value === 'recording') {
    if (e.key === 'Escape') {
      e.preventDefault()
      cancelVoiceRecording()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault()
      void finishVoiceRecording()
    }
  }
}

function onWindowResize() {
  if (isMaximized.value) {
    panelWidth.value = maximizedPanelWidth()
    panelHeight.value = maximizedPanelHeight()
    panelLeft.value = maximizedPanelLeft()
    panelTop.value = maximizedPanelTop()
    return
  }
  if (!panelMovedByUser.value) {
    alignPanelToDefault()
    return
  }
  clampPanelSize()
}

// Auto-hydrate history for any restored tab that doesn't yet have messages
// the first time the user clicks on it. We do this lazily — restoring 8 tabs
// at boot shouldn't trigger 8 simultaneous /assistant_session reads.
async function hydrateIfNeeded(tab) {
  if (!tab || tab._historyLoaded) return
  // Never clobber in-flight UI state — a background hydrate would replace the
  // local message list mid-stream and leave the panel looking empty/broken.
  if (tab.sending || tab.activeStreamMessage || tab.activeRunId) return
  if ((tab.messages || []).length > 0) {
    tab._historyLoaded = true
    return
  }
  // A "fresh" tab (never sent anything) has no backend session yet.
  // Only hydrate when the sessionId was saved from a previous run.
  const seen = sessions.value.some(s => s.id === tab.sessionId)
  if (!seen) {
    tab._historyLoaded = true
    return
  }
  try {
    const r = await withTimeout(api.getAssistantSession(tab.sessionId), 1500, null)
    if (r && r.ok) {
      tab.messages = normalizeHistory(r.history)
      hydrateHistoryAttachments(tab.messages, tab.sessionId)
      restorePendingProjection(r, tab)
      applySessionTokenUsage(r, tab)
      applySessionAssistantPolicy(r, tab)
      tab.webSearchAllowed = Boolean(r.allow_web_search)
      tab.engagement = r.engagement_declaration || null
      if (r.runtime && typeof r.runtime === 'object') tab.runtime = { ...r.runtime }
      if (r.name) tab.name = r.name
      tab._historyLoaded = true
      tab.showAllMessages = false
      const len = tab.messages.length
      tab.renderWindowStart = len > RENDER_WINDOW_TRIGGER ? Math.max(0, len - RENDER_WINDOW_SIZE) : 0
      syncDesktopControlFromMessages(tab.messages)
      refreshDesktopControl()
      refreshRunJobs(tab)
      recomputeBadge(tab)
      persistTabs()
      attachExternalStream(tab, r.active_stream)
      if (activeTab.value?.sessionId === tab.sessionId) scrollBottomAfterRender()
      if (tab.autoApproveMode) nextTick(() => drainPendingAfterAutoApprove(tab))
    }
  } catch (_) { /* ignore */ }
}

async function onAssistantSessionUpdatedEvent(ev) {
  const sessionId = String(ev?.detail?.sessionId || '').trim()
  if (!sessionId) return
  const tab = tabs.find(item => item.sessionId === sessionId)
  if (!tab) return
  if (tab.sending || tab.confirmingAction || tab.activeRunId) {
    setTimeout(() => {
      void onAssistantSessionUpdatedEvent({ detail: { sessionId } })
    }, 750)
    return
  }
  try {
    const r = await withTimeout(api.getAssistantSession(sessionId), 1500, null)
    if (!r?.ok) return
    tab.messages = normalizeHistory(r.history)
    hydrateHistoryAttachments(tab.messages, tab.sessionId)
    restorePendingProjection(r, tab)
    applySessionTokenUsage(r, tab)
    applySessionAssistantPolicy(r, tab)
    tab.webSearchAllowed = Boolean(r.allow_web_search)
    tab.engagement = r.engagement_declaration || null
    if (r.runtime && typeof r.runtime === 'object') tab.runtime = { ...r.runtime }
    if (r.name) tab.name = r.name
    tab._historyLoaded = true
    tab.showAllMessages = false
    const len = tab.messages.length
    tab.renderWindowStart = len > RENDER_WINDOW_TRIGGER ? Math.max(0, len - RENDER_WINDOW_SIZE) : 0
    recomputeBadge(tab)
    persistTabs()
    attachExternalStream(tab, r.active_stream)
    if (activeTab.value?.sessionId === sessionId) {
      syncDesktopControlFromMessages(tab.messages)
      refreshDesktopControl()
      refreshRunJobs(tab)
      refreshAgentThreads(tab)
      scrollBottomAfterRender()
      if (tab.autoApproveMode) nextTick(() => drainPendingAfterAutoApprove(tab))
    }
  } catch (_) { /* best-effort live refresh */ }
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKey)
  window.addEventListener('resize', onWindowResize)
  window.addEventListener('argusai:native-file-drop', onNativeFileDrop)
  window.addEventListener('argusai:add-assistant-attachment', onAddAssistantAttachment)
  window.addEventListener('argusai:reference-image', onReferenceImage)
  window.addEventListener('argusai:reference-file', onReferenceFileEvent)
  window.addEventListener('argusai:assistant-insert-text', onAssistantInsertTextEvent)
  window.addEventListener('argusai:open-sandbox-panel', onOpenSandboxPanelEvent)
  window.addEventListener('argusai:assistant-session-updated', onAssistantSessionUpdatedEvent)
  window.addEventListener('dragend', onGlobalDragEnd)
  window.addEventListener('drop', onGlobalDrop)
  window.addEventListener('pointerup', onGlobalPointerUp)
  window.addEventListener('mouseup', onGlobalPointerUp)
  window.addEventListener('blur', clearDragOverlay)
  refreshNativeModel()
  nextTick(() => requestAnimationFrame(alignPanelToDefault))
  loadAssistantDefaults()
  refreshSessions(true).then(() => {
    // Empty/orphan tabs yield to the most recent saved conversation.
    preferLastConversation()
    // After the session list is in, lazily hydrate the visible tab. Other
    // tabs hydrate on first switch.
    if (activeTab.value) hydrateIfNeeded(activeTab.value)
    if (activeTab.value) refreshRunJobs(activeTab.value)
    if (activeTab.value) refreshAgentThreads(activeTab.value)
    if (state.platform === 'darwin') refreshDesktopControl()
  })
  let agentIdleTick = 0
  agentPollTimer = setInterval(() => {
    const tab = activeTab.value
    if (!props.visible || document.visibilityState === 'hidden' || !tab?.sessionId) return
    const hasBusy = tab.sending || tab.activeRunId || agentPanelOpen.value || (tab.agentThreads || []).some(agentIsActive)
    if (!hasBusy) {
      agentIdleTick += 1
      if (agentIdleTick % 4 !== 0) return
    } else {
      agentIdleTick = 0
    }
    refreshAgentThreads(tab)
  }, 2000)
  // Live-refresh cache/token counters while a turn is running (or the
  // popover is open). Backend accumulates every AI round; the done-event
  // path alone left the toolbar stuck for long tool-heavy turns.
  tokenUsagePollTimer = setInterval(() => {
    const tab = activeTab.value
    if (!props.visible || !tab?.sessionId) return
    if (!(tab.sending || tab.activeRunId || showTokenUsage.value)) return
    void refreshActiveTokenUsage(tab)
  }, 3000)
  nextTick(bindBodyResizeObserver)
})
onUnmounted(() => {
  bodyResizeObserver?.disconnect()
  bodyResizeObserver = null
  window.removeEventListener('keydown', onGlobalKey)
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('argusai:native-file-drop', onNativeFileDrop)
  window.removeEventListener('argusai:add-assistant-attachment', onAddAssistantAttachment)
  window.removeEventListener('argusai:reference-image', onReferenceImage)
  window.removeEventListener('argusai:reference-file', onReferenceFileEvent)
  window.removeEventListener('argusai:assistant-insert-text', onAssistantInsertTextEvent)
  window.removeEventListener('argusai:open-sandbox-panel', onOpenSandboxPanelEvent)
  window.removeEventListener('argusai:assistant-session-updated', onAssistantSessionUpdatedEvent)
  window.removeEventListener('dragend', onGlobalDragEnd)
  window.removeEventListener('drop', onGlobalDrop)
  window.removeEventListener('pointerup', onGlobalPointerUp)
  window.removeEventListener('mouseup', onGlobalPointerUp)
  window.removeEventListener('blur', clearDragOverlay)
  if (dragLeaveTimer) {
    clearTimeout(dragLeaveTimer)
    dragLeaveTimer = null
  }
  if (persistDraftTimer) {
    clearTimeout(persistDraftTimer)
    persistDraftTimer = null
  }
  if (agentPollTimer) {
    clearInterval(agentPollTimer)
    agentPollTimer = null
  }
  if (tokenUsagePollTimer) {
    clearInterval(tokenUsagePollTimer)
    tokenUsagePollTimer = null
  }
  persistTabs()
  stopElapsedTicker()
})

watch(() => props.visible, (v) => {
  if (v) {
    if (panelMovedByUser.value) clampPanelSize()
    else nextTick(() => requestAnimationFrame(alignPanelToDefault))
    // Opening Agent should land on last conversation when the active tab is
    // still an empty "新会话" / orphaned localStorage id.
    refreshSessions(true)
      .then(async () => {
        preferLastConversation()
        if (activeTab.value) await hydrateIfNeeded(activeTab.value)
        if (activeTab.value) refreshRunJobs(activeTab.value)
        scrollBottomAfterRender()
      })
      .catch(async () => {
        if (activeTab.value) await hydrateIfNeeded(activeTab.value)
        scrollBottomAfterRender()
      })
    nextTick(() => {
      bindBodyResizeObserver()
      syncInputDraftToDom()
      document.getElementById('assistant-input')?.focus()
    })
  } else {
    closeMessageContextMenu()
    cancelEditUserMessage()
    persistTabs()
  }
})

watch(activeTabId, async (sid) => {
  showModelModal.value = false
  closeMessageContextMenu()
  cancelEditUserMessage()
  showThinkingMenu.value = false
  showTokenUsage.value = false
  agentPanelOpen.value = false
  focusedAgentId.value = ''
  selectedAgentIds.value = []
  const tab = tabs.find(t => t.sessionId === sid)
  if (tab) {
    await hydrateIfNeeded(tab)
    refreshAgentThreads(tab)
    if (props.visible) scrollBottomAfterRender()
  }
  refreshNativeModel()
  if (props.visible) nextTick(syncInputDraftToDom)
})

watch(inputEl, (el) => {
  if (el && props.visible) nextTick(syncInputDraftToDom)
})

watch(minPanelTop, onWindowResize)

watch([sending, pendingTool, confirmingAction], () => {
  scrollBottom({ force: autoFollowScroll.value || isNearBottom() })
}, { flush: 'post' })

watch(input, (newVal) => {
  const el = inputEl.value
  if (!el) return
  const current = el.innerHTML || ''
  if (current !== newVal) {
    el.innerHTML = newVal || ''
  }
  nextTick(resizeAssistantInput)
}, { flush: 'post' })
</script>

<template>
  <Transition name="slide">
    <div
      v-if="visible"
      class="assistant-panel"
      :class="{ maximized: isMaximized }"
      :style="panelStyle"
      @dragover="onPanelDragOver"
      @dragleave="onPanelDragLeave"
      @drop="onPanelDrop"
    >
      <div v-if="!isMaximized" class="assistant-resize-handle north" @pointerdown="startPanelResize('n', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle northwest" @pointerdown="startPanelResize('nw', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle northeast" @pointerdown="startPanelResize('ne', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle west" @pointerdown="startPanelResize('w', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle east" @pointerdown="startPanelResize('e', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle south" @pointerdown="startPanelResize('s', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle southwest" @pointerdown="startPanelResize('ws', $event)"></div>
      <div v-if="!isMaximized" class="assistant-resize-handle southeast" @pointerdown="startPanelResize('se', $event)"></div>
      <div
        class="assistant-head"
        :class="{ draggable: !isMaximized }"
        @pointerdown="startPanelDrag"
        @dblclick="toggleMaximizePanel"
      >
        <div class="assistant-title">
          <span class="assistant-title-logo"><img :src="argusIcon" alt="" /></span>
          <span>AI</span>
          <span v-if="reverseJobId" class="assistant-reverse-job-chip" :title="reverseJobId">
            {{ reverseJobId }}
          </span>
        </div>
        <div class="assistant-head-actions">
          <div class="assistant-head-tools" @wheel="scrollHeaderTools">
          <div
            v-if="assistantRunStatus"
            class="assistant-live-indicator"
            :title="assistantRunStatus.title"
            aria-live="polite"
          >
            <span class="assistant-live-dot" aria-hidden="true"></span>
            <span>{{ assistantRunStatus.label }}</span>
          </div>
          <button
            class="icon-btn"
            :class="{ active: sandboxBrowser.state.open }"
            :title="t('assistant.sandboxWorkbench')"
            @click="toggleSandboxBrowser"
          >
            <span class="assistant-icon icon-files" aria-hidden="true"></span>
          </button>
          <button class="icon-btn" :title="t('assistant.sessionHistory')" @click.stop="toggleSessionList">
            <span class="assistant-icon icon-clock" aria-hidden="true"></span>
          </button>
          <button
            class="icon-btn assistant-agent-head-btn"
            :class="{ active: agentPanelOpen, pending: agentPendingCount > 0 }"
            :title="agentPendingCount
              ? t('assistant.subagentHeadPending', { count: agentPendingCount })
              : t('assistant.subagentHeadThreads', { count: agentThreads.length })"
            :aria-expanded="agentPanelOpen"
            aria-haspopup="dialog"
            @click.stop="agentPanelOpen ? closeAgentCenter() : openAgentCenter()"
          >
            <span class="assistant-icon icon-agents" aria-hidden="true"></span>
            <span v-if="agentPendingCount" class="assistant-agent-head-badge">
              {{ agentPendingCount > 9 ? '9+' : agentPendingCount }}
            </span>
          </button>
          </div>
          <div class="assistant-window-actions">
            <button
              class="icon-btn"
              :title="isMaximized ? t('assistant.restoreAssistant') : t('assistant.maximizeAssistant')"
              @click="toggleMaximizePanel"
            >
              <span v-if="isMaximized" class="assistant-icon icon-restore" aria-hidden="true"></span>
              <span v-else class="assistant-icon icon-maximize" aria-hidden="true"></span>
            </button>
            <button class="icon-btn" :title="t('assistant.closeAssistant')" @click="$emit('close')">
              <span class="assistant-icon icon-close" aria-hidden="true"></span>
            </button>
          </div>
        </div>
      </div>

      <!-- Tab bar: browser-style multi-session chips along the top edge.
           Each chip shows the conversation title plus a status dot
           reflecting the underlying tab.badge.status, so the user can see
           background runs even when not focused on that tab. -->
      <div class="assistant-tabbar" data-no-drag>
        <div class="assistant-tabbar-scroll">
          <button
            v-for="(tab, idx) in tabs"
            :key="tab.sessionId"
            class="assistant-tab"
            :class="{
              active: tab.sessionId === activeTabId,
              running: tabBadgeStatus(tab) === 'running',
              awaiting: tabBadgeStatus(tab) === 'awaiting',
              error: tabBadgeStatus(tab) === 'error',
              unread: tabBadgeStatus(tab) === 'unread',
            }"
            :title="tabDisplayName(tab)"
            @click="selectTab(tab.sessionId)"
          >
            <span
              v-if="tabBadgeStatus(tab)"
              class="assistant-tab-dot"
              :class="'assistant-tab-dot-' + tabBadgeStatus(tab)"
              aria-hidden="true"
            ></span>
            <span class="assistant-tab-label">{{ tabDisplayName(tab) }}</span>
            <span
              v-if="tabBadgeUnread(tab) > 0 && tab.sessionId !== activeTabId"
              class="assistant-tab-badge"
              :title="t('assistant.tabUnreadHint', { n: tabBadgeUnread(tab) })"
            >{{ tabBadgeUnread(tab) > 99 ? '99+' : tabBadgeUnread(tab) }}</span>
            <button
              v-if="tabs.length > 1"
              class="assistant-tab-close"
              :title="t('assistant.tabCloseTitle')"
              @click="closeTabSafe(tab.sessionId, $event)"
            >&times;</button>
          </button>
        </div>
        <button
          class="assistant-tab-add"
          :disabled="!canOpenNewTab"
          :title="canOpenNewTab ? t('assistant.tabNewTitle') : t('assistant.tabMaxReached', { n: MAX_TABS })"
          @click="addTab"
        >
          <span class="assistant-icon icon-plus" aria-hidden="true"></span>
        </button>
      </div>

      <!-- Tools -->
      <div
        id="assistant-tools-popover"
        v-if="showToolList"
        class="tools-popover"
        role="dialog"
        :aria-label="t('assistant.toolsAndAutoAllow')"
        @click.stop
      >
        <div class="tools-popover-head">
          <div>
            <span>{{ t('assistant.tools') }}</span>
            <small>{{ t('assistant.toolCountSummary', {
              total: assistantTools.length,
              confirmable: confirmableToolCount,
              autoAllowed: autoAllowTools.length,
              readOnly: assistantTools.length - confirmableToolCount,
            }) }}</small>
          </div>
          <div class="tools-popover-actions">
            <div class="tools-bulk-actions">
              <button
                class="ghost"
                :disabled="sending || confirmingAction"
                :title="t('assistant.toolAllowAllHint')"
                @click="setAllAutoAllow(true)"
              >{{ t('assistant.toolAllowAll') }}</button>
              <button
                class="ghost"
                :disabled="sending || confirmingAction || autoAllowTools.length === 0"
                :title="t('assistant.toolManualAllHint')"
                @click="setAllAutoAllow(false)"
              >{{ t('assistant.toolManualAll') }}</button>
            </div>
            <button
              type="button"
              class="usage-popover-close"
              :title="t('assistant.close')"
              :aria-label="t('assistant.close')"
              @click="showToolList = false"
            >
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <path d="M3.5 3.5l9 9m0-9-9 9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </div>
        </div>
        <div v-if="toolsLoading" class="tools-empty">{{ t('assistant.toolsLoading') }}</div>
        <div v-else-if="!assistantTools.length" class="tools-empty">{{ t('assistant.toolsEmpty') }}</div>
        <div v-else class="tools-list">
          <div v-for="tool in assistantTools" :key="tool.name" class="tool-item">
            <div class="tool-main">
              <div class="tool-title">
                <code>{{ tool.name }}</code>
                <span class="risk-pill" :class="'risk-' + (tool.risk || 'low')">{{ riskText(tool.risk) }}</span>
                <span
                  v-if="tool.requires_double_confirm"
                  class="double-confirm-pill"
                  :title="t('assistant.toolDoubleConfirmHint')"
                >{{ t('assistant.toolDoubleConfirmPill') }}</span>
              </div>
              <div class="tool-desc">{{ tool.description }}</div>
              <div v-if="tool.parameters?.length" class="tool-params">
                {{ t('assistant.parameters') }}: {{ tool.parameters.join(', ') }}
              </div>
            </div>
            <span
              v-if="!toolNeedsConfirmation(tool)"
              class="tool-noconfirm-pill"
              :title="t('assistant.toolNoConfirmHint')"
            >{{ t('assistant.toolNoConfirmPill') }}</span>
            <button
              v-else
              class="switch-btn tool-switch"
              :class="{ active: isToolAutoAllowed(tool.name) }"
              :disabled="sending || confirmingAction"
              :title="tool.requires_double_confirm && !isToolAutoAllowed(tool.name)
                ? t('assistant.toolDoubleConfirmHint')
                : ''"
              @click="setAutoAllowTool(tool.name, !isToolAutoAllowed(tool.name))"
            >
              {{ isToolAutoAllowed(tool.name) ? t('assistant.autoAllow') : t('assistant.manualConfirm') }}
            </button>
          </div>
        </div>
      </div>

      <!-- Thinking intensity -->
      <div
        id="assistant-thinking-popover"
        v-if="showThinkingMenu"
        class="tools-popover thinking-popover"
        role="dialog"
        :aria-label="t('assistant.thinkingEffort')"
        @click.stop
      >
        <div class="tools-popover-head">
          <div>
            <span>{{ t('assistant.thinkingEffort') }}</span>
            <small>{{ t('assistant.thinkingMenuHint') }}</small>
          </div>
          <button class="ghost" @click="showThinkingMenu = false" :aria-label="t('assistant.close')">×</button>
        </div>
        <div class="thinking-menu-body">
          <input
            class="thinking-slider"
            type="range"
            min="0"
            :max="thinkingMenuSteps.length - 1"
            :value="thinkingSliderIndex"
            :disabled="sending"
            :aria-label="t('assistant.thinkingEffort')"
            @input="onThinkingSliderInput"
          >
          <div class="thinking-steps">
            <button
              v-for="step in thinkingMenuSteps"
              :key="step.value"
              type="button"
              class="thinking-step"
              :class="{ active: step.value === 'off' ? !thinkingEnabled : thinkingEnabled && thinkingEffort === step.value }"
              :disabled="sending"
              @click="applyThinkingStep(step.value)"
            >{{ step.label }}</button>
          </div>
        </div>
      </div>

      <!-- Token usage / cache hit rate -->
      <div
        id="assistant-token-usage-popover"
        v-if="showTokenUsage"
        class="tools-popover usage-popover"
        role="dialog"
        :aria-label="t('assistant.contextUsagePopoverTitle')"
        @click.stop
      >
        <div class="tools-popover-head usage-popover-head">
          <div>
            <span>{{ t('assistant.contextUsagePopoverTitle') }}</span>
            <small>{{ t('assistant.contextUsagePopoverHint') }}</small>
          </div>
          <button class="usage-popover-close" @click="showTokenUsage = false" :aria-label="t('assistant.close')">
            <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
              <path d="M3.5 3.5l9 9m0-9-9 9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </div>
        <TokenUsageDetail
          :usage="tokenUsageView"
          :context="contextUsage"
        />
      </div>

      <!-- Session history -->
      <div v-if="showSessionList" class="session-popover" @click.stop>
        <div class="session-popover-head">
          <span>{{ t('assistant.sessionHistory') }}</span>
          <div class="session-popover-actions">
            <button class="ghost" @click="newSession">{{ t('assistant.sessionNew') }}</button>
          </div>
        </div>
        <div class="session-filter-wrap">
          <input
            v-model="sessionFilter"
            class="session-filter"
            type="search"
            :placeholder="t('assistant.sessionFilterPlaceholder')"
            @keydown.esc.stop.prevent="sessionFilter = ''"
          />
        </div>
        <div v-if="!filteredSessions.length" class="session-empty">
          {{ sessions.length ? t('assistant.sessionNoMatch') : t('assistant.sessionEmpty') }}
        </div>
        <div v-else class="session-list">
          <div
            v-for="s in filteredSessions" :key="s.id"
            class="session-item"
            :class="{ active: s.id === sessionId }"
            @click="switchSession(s.id)"
          >
            <div class="session-main">
              <span class="session-name" :title="s.name || s.id">{{ s.name || s.id.slice(0,8) }}</span>
              <span class="session-meta">{{ formatSessionTime(s) }} · {{ t('assistant.sessionMsgCount', { n: s.msg_count || 0 }) }}</span>
              <span v-if="s.note" class="session-note" :title="s.note">{{ s.note }}</span>
            </div>
          </div>
        </div>
        <button type="button" class="session-manage-all" @click="manageAllSessions">
          {{ t('assistant.manageAllConversations') }}
          <span aria-hidden="true">→</span>
        </button>
      </div>

      <div v-if="macosDesktopPermMissing.length" class="macos-perm-banner">
        <div class="macos-perm-banner-main">
          <strong>{{ t('assistant.macosDesktopPermissions') }}</strong>
          <ul class="macos-perm-list">
            <li v-for="p in macosDesktopPermMissing" :key="p.id">
              <span>{{ p.label }}：{{ p.hint }}</span>
              <button class="ghost tiny" type="button" @click="openMacosPrivacySettings(p.id)">
                {{ t('assistant.openSettings') }}
              </button>
            </li>
          </ul>
        </div>
      </div>

      <div v-if="desktopControlActive" class="desktop-control-banner">
        <div class="desktop-control-banner-main">
          <strong>{{ t('assistant.desktopContinuousAuthorization') }}</strong>
          <span v-if="desktopControlRemainingLabel">
            {{ t('assistant.remainingTime', { time: desktopControlRemainingLabel }) }}
          </span>
          <span v-if="desktopControl.scope" class="desktop-control-scope">· {{ desktopControl.scope }}</span>
        </div>
        <button
          class="ghost tiny danger"
          type="button"
          :disabled="sending || confirmingAction"
          @click="stopDesktopControl"
        >
          {{ t('assistant.stopDesktopAuthorization') }}
        </button>
      </div>

      <!-- Messages -->
      <div class="assistant-body" ref="msgContainer" @scroll.passive="onMessageScroll">
        <div v-if="!messages.length" class="muted" style="text-align:center;padding:40px 20px">
          {{ t('assistant.placeholder') }}
        </div>

        <!-- "Load earlier" sentinel: shown only when the render window is
             clipping older messages. Two buttons: load one more page, or
             reveal everything. -->
        <div v-if="hiddenEarlierCount > 0" class="load-earlier">
          <button class="ghost tiny" @click="loadEarlierMessages">
            {{ t('assistant.loadEarlier', { n: Math.min(hiddenEarlierCount, RENDER_WINDOW_STEP) }) }}
          </button>
          <button class="ghost tiny" @click="showAllEarlierMessages">
            {{ t('assistant.showAllEarlier', { n: hiddenEarlierCount }) }}
          </button>
        </div>

        <div
          v-for="(m, i) in renderedMessages"
          :key="m._uid"
          v-memo="messageRenderMemo(m, renderWindowStart + i)"
          class="msg"
          :class="[m.role, {
            grouped: !messageStartsVisualGroup(m, renderWindowStart + i),
            hidden: !hasVisibleMessagePayload(m, renderWindowStart + i),
          }]"
          @contextmenu="openMessageContextMenu($event, m)"
        >
          <div
            v-if="messageStartsVisualGroup(m, renderWindowStart + i) && m.role === 'assistant'"
            class="msg-avatar"
            aria-hidden="true"
          >AI</div>
          <div
            v-else-if="messageStartsVisualGroup(m, renderWindowStart + i)"
            class="msg-avatar"
            aria-hidden="true"
          >U</div>
          <div v-else class="msg-avatar-spacer" aria-hidden="true"></div>
          <div class="msg-content">
            <div v-if="messageStartsVisualGroup(m, renderWindowStart + i)" class="msg-label">
              {{ m.role === 'user' ? 'You' : 'AI' }}
            </div>
            <button
              v-if="messageVoice(m)"
              type="button"
              class="voice-bubble"
              :class="{ 'is-playing': playingVoiceKey === voiceAttachmentKey(m._uid, 'voice', messageVoice(m)) }"
              :style="{ width: voiceBubbleWidth(messageVoice(m).duration_ms || 1000) }"
              :title="t('assistant.playVoice')"
              :disabled="!voiceBubblePlayable(messageVoice(m))"
              @click="toggleVoicePlayback(voiceAttachmentKey(m._uid, 'voice', messageVoice(m)), attachmentThumbUrl(messageVoice(m)), messageVoice(m))"
            >
              <span class="voice-bubble-duration">{{ formatVoiceSecondsLabel(messageVoice(m).duration_ms || 1000) }}</span>
              <span class="voice-bubble-waves" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="22" height="22">
                  <circle cx="19" cy="12" r="1.6" fill="currentColor" />
                  <path d="M15.5 8.5c2.2 1.6 2.2 5.4 0 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                  <path class="voice-wave-outer" d="M11.5 5.5c3.8 2.6 3.8 10.4 0 13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity="0.85" />
                </svg>
              </span>
              <audio
                v-if="voiceBubblePlayable(messageVoice(m))"
                :ref="(el) => registerVoiceAudioEl(voiceAttachmentKey(m._uid, 'voice', messageVoice(m)), el)"
                class="voice-bubble-audio"
                preload="none"
              ></audio>
            </button>
            <div v-if="messageDisplayAttachments(m).length" class="msg-attachments">
              <template v-for="(att, ai) in messageDisplayAttachments(m)" :key="att.id || ai">
                <div v-if="att.type === 'image'" class="msg-attachment-wrap">
                  <button
                    type="button"
                    class="msg-attachment"
                    :title="att.name
                      ? t('assistant.viewAttachmentNamed', { name: att.name })
                      : t('assistant.viewImage')"
                    @click="openMessageAttachment(att)"
                  >
                    <img :src="attachmentThumbUrl(att)" :alt="att.name || 'image'" />
                  </button>
                  <div
                    class="msg-attachment-actions"
                    role="toolbar"
                    :aria-label="t('assistant.imageReferenceActions')"
                  >
                    <button
                      type="button"
                      class="msg-attachment-action"
                      :title="t('assistant.referenceImageTitle')"
                      @click.stop="referenceMessageImage(att, m, renderWindowStart + i)"
                    >
                      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                        <path d="M5 3h6a2 2 0 0 1 2 2v6M5 3v10a2 2 0 0 0 2 2h6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                      </svg>
                      <span>{{ t('assistant.reference') }}</span>
                    </button>
                    <button
                      type="button"
                      class="msg-attachment-action"
                      :title="t('assistant.copyImageToInputTitle')"
                      @click.stop="copyMessageImageToInput(att)"
                    >
                      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                        <path d="M10 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
                        <path d="M6 5h6a1 1 0 0 1 1 1v8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" opacity="0.6"/>
                      </svg>
                      <span>{{ t('assistant.copy') }}</span>
                    </button>
                  </div>
                </div>
                <div v-else class="msg-attachment-file" :title="att.name || ''">
                  <span class="msg-attachment-file-icon">📄</span>
                  <span class="msg-attachment-file-name">{{ att.name || '(file)' }}</span>
                  <span v-if="att.size != null" class="msg-attachment-file-size">{{ humanSize(att.size) }}</span>
                </div>
              </template>
            </div>
            <button
              v-if="isAgentTimelineStep(m, renderWindowStart + i)"
              type="button"
              class="agent-step-toggle"
              :class="{ open: stepOpenState(m, renderWindowStart + i), running: isActiveAssistantStep(renderWindowStart + i) }"
              @click="toggleStepOpen(m, renderWindowStart + i)"
            >
              <svg class="agent-step-chevron" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span class="agent-step-summary">{{ stepSummaryLabel(m, renderWindowStart + i) }}</span>
              <span v-if="stepToolCount(m, renderWindowStart + i)" class="agent-step-count">
                · {{ t('assistant.itemsCount', { count: stepToolCount(m, renderWindowStart + i) }) }}
              </span>
              <span v-if="m.status" class="agent-step-running-dot" aria-hidden="true"></span>
            </button>
            <div v-if="isEditingUserMessage(m)" class="message-edit">
              <textarea
                v-model="editingUserMessageText"
                class="message-edit-textarea"
                rows="4"
                maxlength="50000"
                :aria-label="t('assistant.editUserMessage')"
                @keydown.esc.stop.prevent="cancelEditUserMessage"
                @keydown.ctrl.enter.stop.prevent="submitEditedUserMessage(m)"
                @keydown.meta.enter.stop.prevent="submitEditedUserMessage(m)"
              ></textarea>
              <div class="message-edit-actions">
                <span class="message-edit-hint">{{ t('assistant.sendShortcut') }}</span>
                <button class="ghost tiny" type="button" @click="cancelEditUserMessage">
                  {{ t('assistant.cancel') }}
                </button>
                <button
                  class="primary tiny"
                  type="button"
                  :disabled="sending || confirmingAction || !editingUserMessageText.trim()"
                  @click="submitEditedUserMessage(m)"
                >
                  {{ t('assistant.saveAndRegenerate') }}
                </button>
              </div>
            </div>
            <div
              v-else-if="(visibleMessageText(m) || m.status) && (!isAgentTimelineStep(m, renderWindowStart + i) || stepOpenState(m, renderWindowStart + i))"
              class="msg-text"
              @mouseup="handleMessageTextSelection($event, m, renderWindowStart + i)"
            >
              <MarkdownText v-if="visibleMessageText(m)" :text="visibleMessageText(m)" :session-id="sessionId" />
              <div v-if="m.status" class="stream-status-row">
                <span class="stream-status-dot" aria-hidden="true"></span>
                <span class="stream-status-text">{{ m.status }}</span>
                <span v-if="m.startedAt" class="stream-status-elapsed">{{ formatToolElapsed(m.startedAt, nowMs, m.statusTimeoutSeconds) }}</span>
              </div>
            </div>
            <div v-if="m.role === 'user' && messageSelectionReference(m)" class="msg-reference">
              {{ t('assistant.referenceLabel', { label: messageSelectionReference(m) }) }}
            </div>
            <div
              v-if="messageBlockGroups(m, renderWindowStart + i).hasAny && (!isAgentTimelineStep(m, renderWindowStart + i) || stepOpenState(m, renderWindowStart + i))"
              class="message-blocks"
            >
              <AssistantCard
                v-for="(block, bi) in messageBlockGroups(m, renderWindowStart + i).primary"
                :key="block.id || 'p-' + bi"
                :block="block"
                :decision="cardDecision(block)"
                :session-id="sessionId"
                :actions-disabled="cardActionsDisabled(block)"
                @action="handleCardAction($event, block)"
                @tool-decision="handleToolDecision($event, block)"
                @preview-selection="handlePreviewSelectionEvent"
              />
              <details
                v-if="messageBlockGroups(m, renderWindowStart + i).foldTools && !isAgentTimelineStep(m, renderWindowStart + i)"
                class="tool-calls tool-calls-panel"
              >
                <summary>
                  {{ t('assistant.toolCallsCount', {
                    count: messageBlockGroups(m, renderWindowStart + i).toolResults.length,
                  }) }}
                </summary>
                <div class="tool-call-list tool-call-list-stack">
                  <AssistantCard
                    v-for="(block, bi) in messageBlockGroups(m, renderWindowStart + i).toolResults"
                    :key="block.id || 't-' + bi"
                    :block="block"
                    :decision="cardDecision(block)"
                    :session-id="sessionId"
                    :actions-disabled="cardActionsDisabled(block)"
                    @action="handleCardAction($event, block)"
                    @tool-decision="handleToolDecision($event, block)"
                    @preview-selection="handlePreviewSelectionEvent"
                  />
                </div>
              </details>
              <template v-else>
                <AssistantCard
                  v-for="(block, bi) in messageBlockGroups(m, renderWindowStart + i).toolResults"
                  :key="block.id || 't-' + bi"
                  :block="block"
                  :decision="cardDecision(block)"
                  :session-id="sessionId"
                  :actions-disabled="cardActionsDisabled(block)"
                  @action="handleCardAction($event, block)"
                  @tool-decision="handleToolDecision($event, block)"
                  @preview-selection="handlePreviewSelectionEvent"
                />
              </template>
            </div>

          <!-- Interactive options -->
          <div v-if="m.options?.length" class="options-row">
            <button
              v-for="opt in m.options" :key="opt.value"
              class="opt-btn"
              @click="send(opt.value)"
            >{{ opt.label }}</button>
          </div>

            <div
              v-if="m.role === 'assistant' && (renderWindowStart + i) > 0 && !isToolOnlyAssistantMessage(m, renderWindowStart + i) && !isAgentTimelineStep(m, renderWindowStart + i)"
              class="message-actions"
            >
              <button class="ghost tiny" :disabled="sending || confirmingAction" @click="regenerateMessage(renderWindowStart + i)">
                {{ t('assistant.regenerate') }}
              </button>
              <button
                v-if="canContinueFrom(renderWindowStart + i)"
                class="ghost tiny"
                :disabled="sending || confirmingAction"
                :title="t('assistant.continueTitle')"
                @click="continueFromHere()"
              >
                {{ t('assistant.continue') }}
              </button>
              <button class="ghost tiny" :title="t('assistant.copyAnswer')" @click="copyMessageText(m.text)">
                {{ t('assistant.copy') }}
              </button>
            </div>
            <div v-else-if="m.role === 'user' && !isEditingUserMessage(m)" class="message-actions user-actions">
              <button class="ghost tiny" :title="t('assistant.copyUserMessage')" @click="copyMessageText(m.text)">
                {{ t('assistant.copy') }}
              </button>
            </div>
          </div>
        </div>

        <!-- Loading -->
        <div v-if="sending && !pendingTool && !activeStreamMessage" class="msg assistant">
          <div class="msg-avatar" aria-hidden="true">AI</div>
          <div class="msg-content">
          <div class="msg-label">AI</div>
          <div class="msg-text" style="opacity:0.5">{{ t('common.loading') }}</div>
          </div>
        </div>

          </div>

      <div
        v-if="sideQuestion.open"
        class="assistant-side-layer"
        role="presentation"
        @pointerdown.self="closeSideQuestion"
      >
        <section
          class="assistant-side-card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="assistant-side-title"
          @keydown.esc.stop.prevent="closeSideQuestion"
        >
          <header class="assistant-side-head">
            <div>
              <strong id="assistant-side-title">{{ t('assistant.sideTitle') }}</strong>
              <span>{{ t('assistant.sideSubtitle') }}</span>
            </div>
            <button type="button" class="ghost tiny" autofocus @click="closeSideQuestion">
              {{ t('assistant.close') }}
            </button>
          </header>
          <div class="assistant-side-body">
            <p v-if="sideQuestion.question" class="assistant-side-question">{{ sideQuestion.question }}</p>
            <div v-if="sideQuestion.loading" class="assistant-side-state">{{ t('assistant.sideThinking') }}</div>
            <div v-else-if="sideQuestion.error" class="assistant-side-state is-error">{{ sideQuestion.error }}</div>
            <MarkdownText v-else :text="sideQuestion.answer" :session-id="sessionId" />
          </div>
          <footer class="assistant-side-foot">{{ t('assistant.sideFooter') }}</footer>
        </section>
      </div>

      <div
        v-if="agentPanelOpen"
        class="assistant-agent-center-layer"
        role="presentation"
        @pointerdown.self="closeAgentCenter"
      >
        <section
          class="assistant-agent-center"
          role="dialog"
          aria-modal="true"
          aria-labelledby="assistant-agent-center-title"
          @keydown.esc.stop.prevent="closeAgentCenter"
        >
          <header class="assistant-agent-center-head">
            <div>
              <strong id="assistant-agent-center-title">{{ t('assistant.agentCenterTitle') }}</strong>
              <span>{{ t('assistant.agentCenterSubtitle') }}</span>
            </div>
            <div class="assistant-agent-center-summary" aria-live="polite">
              <span>{{ t('assistant.agentCenterRunning', { count: activeAgentThreads.length }) }}</span>
              <span v-if="agentPendingCount" class="pending">
                {{ t('assistant.agentCenterPending', { count: agentPendingCount }) }}
              </span>
              <span>{{ t('assistant.agentCenterThreads', { count: agentThreads.length }) }}</span>
            </div>
            <button type="button" class="ghost tiny" autofocus @click="closeAgentCenter">
              {{ t('assistant.close') }}
            </button>
          </header>

          <div class="assistant-agent-batchbar">
            <button
              v-if="!selectedAgentIds.length && removableAgentCount"
              type="button"
              class="ghost tiny"
              @click="selectFinishedAgents"
            >{{ t('assistant.selectFinished') }}</button>
            <span v-if="selectedAgentIds.length">
              {{ t('assistant.selectedCount', { count: selectedAgentIds.length }) }}
            </span>
            <button
              v-if="selectedActiveAgents.length"
              type="button"
              class="ghost tiny"
              @click="stopSelectedAgents"
            >{{ t('assistant.stopSelected') }}</button>
            <button
              v-if="selectedDeletableAgents.length"
              type="button"
              class="ghost tiny danger"
              @click="deleteSelectedAgents"
            >{{ t('assistant.deleteSelected') }}</button>
            <button
              v-if="selectedAgentIds.length"
              type="button"
              class="ghost tiny"
              @click="selectedAgentIds = []"
            >{{ t('assistant.cancelSelection') }}</button>
            <button
              v-if="removableAgentCount"
              type="button"
              class="ghost tiny danger assistant-agent-clear-finished"
              @click="clearFinishedAgents"
            >{{ t('assistant.clearAllFinished') }}</button>
          </div>

          <div class="assistant-agent-center-body">
            <div
              class="assistant-agent-center-list"
              role="list"
              :aria-label="t('assistant.agentThreadsAria')"
            >
              <div v-if="!agentThreads.length" class="assistant-agent-center-empty">
                {{ t('assistant.noAgentThreads') }}
              </div>
              <article
                v-for="agent in agentThreads"
                :key="agent.agent_id"
                class="assistant-agent-center-row"
                :class="{ focused: focusedAgentId === agent.agent_id }"
                role="listitem"
              >
                <input
                  type="checkbox"
                  :checked="selectedAgentIds.includes(agent.agent_id)"
                  :aria-label="t('assistant.selectAgent', {
                    name: agent.role || t('assistant.subagent'),
                  })"
                  @change="toggleAgentSelection(agent)"
                />
                <button type="button" class="assistant-agent-center-row-main" @click="openAgentContext(agent)">
                  <span
                    class="assistant-agent-card-status"
                    :class="`is-${agent.status || 'unknown'}`"
                    aria-hidden="true"
                  ></span>
                  <span>
                    <strong>{{ agent.role || t('assistant.unknownRole') }}</strong>
                    <small>{{ agent.objective || t('assistant.unknownObjective') }}</small>
                  </span>
                  <span class="assistant-agent-card-state">{{ agentStatusLabel(agent.status) }}</span>
                </button>
                <div class="assistant-agent-card-actions">
                  <button
                    v-if="agentIsActive(agent) && agent.status !== 'closing'"
                    type="button"
                    class="assistant-agent-icon-action"
                    :title="t('assistant.pause')"
                    :aria-label="t('assistant.pauseAgent')"
                    @click="pauseAgentThread(agent)"
                  ><span class="assistant-icon icon-pause" aria-hidden="true"></span></button>
                  <button
                    v-if="agentIsActive(agent) && agent.status !== 'closing'"
                    type="button"
                    class="assistant-agent-icon-action danger"
                    :title="t('assistant.stop')"
                    :aria-label="t('assistant.stopAgent')"
                    @click="closeAgentThread(agent)"
                  ><span class="assistant-icon icon-stop" aria-hidden="true"></span></button>
                  <button
                    v-if="agentCanDelete(agent)"
                    type="button"
                    class="assistant-agent-icon-action danger"
                    :title="t('assistant.deleteThreadTitle')"
                    :aria-label="t('assistant.deleteAgentAria')"
                    @click="deleteAgentThread(agent)"
                  ><span class="assistant-icon icon-trash" aria-hidden="true"></span></button>
                </div>
              </article>
            </div>

            <div class="assistant-agent-center-detail">
              <div v-if="!focusedAgentDetail" class="assistant-agent-center-empty">
                {{ t('assistant.chooseAgentThread') }}
              </div>
              <template v-else>
                <SubAgentGroupCard
                  :agents="[focusedAgentDetail]"
                  :details="agentDetails"
                  :decision-busy="agentDecisionBusy"
                  compact
                  @confirm="confirmAgentTools"
                  @open-agent="openAgentContext"
                  @open-center="openAgentCenter()"
                />
                <!-- 目标(objective) is already shown by SubAgentGroupCard's row above;
                     only additive delegation facts belong here to avoid showing the
                     same field twice in different formats. Labels are kept in Chinese
                     to match 委派上下文/当前摘要 below instead of mixing in raw snake_case
                     keys, which previously looked bolted-on next to the card. -->
                <dl class="assistant-agent-detail-facts">
                  <div>
                    <dt>{{ t('assistant.expectedOutput') }}</dt>
                    <dd>{{ focusedAgentDetail.expected_output || t('assistant.unknown') }}</dd>
                  </div>
                  <div>
                    <dt>{{ t('assistant.delegatedPermission') }}</dt>
                    <dd>{{ focusedAgentDetail.permission || t('assistant.unknown') }}</dd>
                  </div>
                  <div>
                    <dt>{{ t('assistant.delegatedScope') }}</dt>
                    <dd>{{ focusedAgentDetail.scope_ref || t('assistant.unknown') }}</dd>
                  </div>
                  <div>
                    <dt>{{ t('assistant.requestedTools') }}</dt>
                    <dd>{{ (focusedAgentDetail.requested_tools || []).join(', ') || t('assistant.unknown') }}</dd>
                  </div>
                  <div>
                    <dt>{{ t('assistant.toolCallCount') }}</dt>
                    <dd>{{ focusedAgentDetail.tool_calls ?? t('assistant.unknown') }}</dd>
                  </div>
                  <div v-if="focusedAgentDetail.context">
                    <dt>{{ t('assistant.delegationContext') }}</dt>
                    <dd><pre>{{ focusedAgentDetail.context }}</pre></dd>
                  </div>
                  <div v-if="focusedAgentDetail.summary">
                    <dt>{{ t('assistant.currentSummary') }}</dt>
                    <dd><pre>{{ focusedAgentDetail.summary }}</pre></dd>
                  </div>
                </dl>
                <div v-if="(focusedAgentDetail.events || []).length" class="assistant-agent-events">
                  <strong>{{ t('assistant.recentEvents') }}</strong>
                  <div
                    v-for="event in (focusedAgentDetail.events || []).slice(-8).reverse()"
                    :key="event.id || event.timestamp"
                  >
                    <code>{{ event.type || event.kind || t('assistant.unknownEvent') }}</code>
                    <span>{{ event.status || event.message || event.timestamp || t('assistant.unknown') }}</span>
                  </div>
                </div>
              </template>
            </div>
          </div>
        </section>
      </div>

      <!-- Live sub-agent dock: a status bar docked above the composer, not a chat
           bubble. It only exists while a thread is running or blocked on the
           user; once every thread is finished it disappears on its own (see
           liveAgentThreads) instead of trailing the conversation forever. -->
      <div v-if="liveAgentThreads.length" class="subagent-dock" :class="{ expanded: subagentStreamOpen }">
        <button
          v-if="!subagentStreamOpen"
          type="button"
          class="subagent-dock-strip"
          :aria-expanded="false"
          @click="subagentStreamExpanded = true"
        >
          <span class="state-dot" :class="agentPendingCount ? 'waiting' : 'active'" aria-hidden="true"></span>
          <span class="subagent-dock-copy">
            <strong>{{ t('assistant.subagentsCount', { count: liveAgentThreads.length }) }}</strong>
            <span>{{ subagentStreamSummary }}</span>
          </span>
          <span class="subagent-dock-action">{{ t('assistant.expand') }}</span>
        </button>
        <template v-else>
          <div class="subagent-dock-toolbar">
            <span class="state-dot" :class="agentPendingCount ? 'waiting' : 'active'" aria-hidden="true"></span>
            <strong>{{ t('assistant.subagentsCount', { count: liveAgentThreads.length }) }}</strong>
            <span class="subagent-dock-summary">{{ subagentStreamSummary }}</span>
            <button
              v-if="!agentPendingCount"
              type="button"
              class="ghost tiny"
              @click="subagentStreamExpanded = false"
            >{{ t('assistant.collapse') }}</button>
          </div>
          <div class="subagent-dock-body">
            <SubAgentGroupCard
              :agents="liveAgentThreads"
              :details="agentDetails"
              :decision-busy="agentDecisionBusy"
              hide-header
              @confirm="confirmAgentTools"
              @open-agent="openAgentContext"
              @open-center="openAgentCenter()"
            />
          </div>
        </template>
      </div>

      <!-- Pending batch banner: only when AI requested multiple confirmations at once -->
      <!-- Ordinary queued/running turns already surface status in the composer
           ("AI 正在执行…") and the assistant bubble — do not duplicate that with
           a "任务进行中" strip. Only show when the durable job needs the user. -->
      <div
        v-if="liveJob && liveJob.state === 'waiting_user'"
        class="assistant-job-banner is-live"
      >
        <span class="assistant-job-dot" aria-hidden="true"></span>
        <div class="assistant-job-copy">
          <strong>{{ t('assistant.jobWaitingInput') }}</strong>
          <span>{{ liveJob.status || t('assistant.jobWaitingInputStatus') }}</span>
        </div>
        <span v-if="formatJobProgress(liveJob)" class="assistant-job-progress">
          {{ formatJobProgress(liveJob) }}
        </span>
      </div>
      <div v-if="pendingBatchSize > 1" class="pending-batch-bar">
        <div class="pending-batch-bar-text">
          {{ t('assistant.pendingBatchInstructions', { count: pendingBatchSize }) }}
          <span class="muted">
            {{ t('assistant.pendingBatchProgress', {
              decided: pendingDecidedCount,
              total: pendingBatchSize,
            }) }}
          </span>
          </div>
        <div class="pending-batch-bar-actions">
          <button
            class="ghost tiny"
            :disabled="sending || confirmingAction"
            @click="approveAllPending()"
          >{{ t('assistant.toolAllowAll') }}</button>
          <button
            class="ghost tiny danger"
            :disabled="sending || confirmingAction"
            @click="denyAllPending()"
          >{{ t('assistant.denyAll') }}</button>
          <button
            class="ghost tiny pending-execute"
            :disabled="sending || confirmingAction || !pendingAllDecided"
            @click="submitPendingBatch('allow')"
          >{{ t('assistant.executeSelected') }}</button>
        </div>
      </div>

      <!-- Input -->
      <div class="assistant-foot">
        <div v-if="pendingAttachments.length" class="attach-strip">
          <template v-for="att in pendingAttachments" :key="att.id">
            <div
              v-if="att.type === 'image'"
              class="attach-chip attach-chip-clickable"
              :title="att.name
                ? t('assistant.editAttachmentNamed', { name: att.name })
                : t('assistant.editImage')"
              role="button"
              tabindex="0"
              @click="editAttachment(att)"
              @keydown.enter.prevent="editAttachment(att)"
              @keydown.space.prevent="editAttachment(att)"
            >
              <img :src="attachmentThumbUrl(att)" :alt="att.name || 'image'" />
              <span class="attach-edit-hint">{{ t('assistant.sessionEdit') }}</span>
              <button
                class="attach-remove"
                :title="t('assistant.remove')"
                @click.stop="removeAttachment(att.id)"
              >&times;</button>
            </div>
            <div v-else-if="['text', 'file', 'video'].includes(att.type)" class="attach-chip attach-chip-file" :title="att.name || 'file'">
              <div class="attach-file-icon">{{ att.type === 'video' ? '🎬' : '📄' }}</div>
              <div class="attach-file-meta">
                <div class="attach-file-name">{{ att.name || '(untitled)' }}</div>
                <div class="attach-file-size">{{ att.host_path || att.path || humanSize(att.size) }}</div>
              </div>
              <button class="attach-remove" :title="t('assistant.remove')" @click="removeAttachment(att.id)">&times;</button>
            </div>
            <div v-else class="attach-chip attach-chip-error" :title="att.error || t('assistant.error')">
              <div class="attach-file-icon">⚠</div>
              <div class="attach-file-meta">
                <div class="attach-file-name">{{ att.name || '(untitled)' }}</div>
                <div class="attach-file-size">{{ att.error || t('assistant.unsupported') }}</div>
              </div>
              <button class="attach-remove" :title="t('assistant.remove')" @click="removeAttachment(att.id)">&times;</button>
            </div>
          </template>
        </div>
        <div v-if="previewSelectionContext?.text" class="preview-selection-chip" :title="previewSelectionContext.text">
          <span class="preview-selection-label">
            {{ t('assistant.selectedReference', { label: previewSelectionLabel }) }}
          </span>
          <button
            type="button"
            class="ghost tiny preview-selection-close"
            :title="t('assistant.cancelReference')"
            :aria-label="t('assistant.cancelReference')"
            @click="clearPreviewSelectionContext"
          >&times;</button>
        </div>
        <div v-if="queuedInputs.length" class="queued-strip">
          <div
            v-for="(item, qi) in queuedInputs"
            :key="item.ts + '-' + qi"
            class="queued-chip"
          >
            <span
              class="queued-chip-badge"
              :title="t('assistant.queuedTitle')"
              :aria-label="t('assistant.queued')"
            >
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4" />
                <path d="M8 4.6V8l2.4 1.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <input
              type="text"
              class="queued-chip-input"
              :value="queuedItemDisplayText(item)"
              :placeholder="sending ? t('assistant.queueAfterFinish') : t('assistant.sessionEdit')"
              @input="updateQueuedDisplayText(qi, $event.target.value)"
              @keydown.enter.prevent="sendQueuedNow(qi)"
            />
            <button
              type="button"
              class="queued-chip-send"
              :title="t('assistant.queuedSendNowTitle')"
              :aria-label="t('assistant.sendNow')"
              :disabled="!queuedItemDisplayText(item).trim()"
              @click="sendQueuedNow(qi)"
            >
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                <path d="M2 8.2 13.5 3 9 14l-2.3-4.5L2 8.2Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
            <button
              type="button"
              class="queued-chip-remove"
              :title="t('assistant.removeQueue')"
              :aria-label="t('assistant.removeQueue')"
              @click="removeQueuedInput(qi)"
            >&times;</button>
          </div>
        </div>
        <div class="assistant-foot-row" :class="{ 'is-recording': voiceState === 'recording' }">
          <input
            ref="fileInput"
            type="file"
            multiple
            style="display:none"
            @change="onFileInput"
          />
          <template v-if="voiceState === 'recording'">
            <button
              class="ghost icon voice-cap-btn voice-cap-cancel"
              :title="t('assistant.voiceCancelTitle')"
              :aria-label="t('assistant.voiceCancel')"
              @click="cancelVoiceRecording"
            >
              <span class="assistant-icon icon-close" aria-hidden="true"></span>
            </button>
            <div class="voice-capsule" role="status" :aria-label="t('assistant.voiceRecording', { time: voiceRecordingTime })">
              <span
                class="voice-cap-dot"
                :style="{ transform: `scale(${1 + Math.min(0.6, voiceLevel * 1.2)})` }"
                aria-hidden="true"
              ></span>
              <span class="voice-cap-time">{{ voiceRecordingTime }}<span class="voice-cap-time-max"> / {{ voiceRecordingMaxLabel }}</span></span>
              <span class="voice-cap-bars" aria-hidden="true">
                <span
                  v-for="(lv, idx) in voiceLevels"
                  :key="idx"
                  class="voice-cap-bar"
                  :style="{ height: Math.max(2, Math.round(lv * 18)) + 'px' }"
                ></span>
              </span>
              <span class="voice-cap-hint">{{ t('assistant.voiceCapsuleHint') }}</span>
              <span
                class="voice-cap-progress"
                aria-hidden="true"
                :style="{ width: (voiceProgress * 100) + '%' }"
              ></span>
            </div>
            <button
              class="primary voice-cap-btn voice-cap-finish"
              :title="t('assistant.voiceFinishTitle')"
              :aria-label="t('assistant.voiceFinish')"
              @click="finishVoiceRecording"
            >
              <span class="assistant-icon icon-check" aria-hidden="true"></span>
            </button>
          </template>
          <template v-else>
          <div
            class="assistant-input-wrap"
            :class="{ 'is-voice-message': pendingVoice && !input.trim() && !pendingAttachments.length }"
          >
            <div
              v-if="pendingVoice && !input.trim() && !pendingAttachments.length"
              class="assistant-voice-message"
            >
              <button
                type="button"
                class="voice-bubble voice-bubble-draft"
                :class="{ 'is-playing': playingVoiceKey === voiceAttachmentKey('draft', 'voice', pendingVoice) }"
                :style="{ width: voiceBubbleWidth(pendingVoice.duration_ms || 1000) }"
                :title="t('assistant.playVoice')"
                :disabled="!voiceBubblePlayable(pendingVoice)"
                @click="toggleVoicePlayback(voiceAttachmentKey('draft', 'voice', pendingVoice), attachmentThumbUrl(pendingVoice), pendingVoice)"
              >
                <span class="voice-bubble-duration">{{ formatVoiceSecondsLabel(pendingVoice.duration_ms || 1000) }}</span>
                <span class="voice-bubble-waves" aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="22" height="22">
                    <circle cx="19" cy="12" r="1.6" fill="currentColor" />
                    <path d="M15.5 8.5c2.2 1.6 2.2 5.4 0 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                    <path class="voice-wave-outer" d="M11.5 5.5c3.8 2.6 3.8 10.4 0 13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity="0.85" />
                  </svg>
                </span>
                <audio
                  v-if="voiceBubblePlayable(pendingVoice)"
                  :ref="(el) => registerVoiceAudioEl(voiceAttachmentKey('draft', 'voice', pendingVoice), el)"
                  class="voice-bubble-audio"
                  preload="none"
                ></audio>
              </button>
              <button
                type="button"
                class="ghost tiny assistant-voice-remove"
                :title="t('assistant.removeVoice')"
                @click="clearPendingVoice()"
              >&times;</button>
            </div>
            <div
              v-else
              id="assistant-input"
              ref="inputEl"
              contenteditable="true"
              class="assistant-input-ce"
              :data-placeholder="hasAwaitingUserDecision
                ? t('assistant.decisionPlaceholder')
                : (sending ? t('assistant.busyPlaceholder') : t('assistant.placeholder'))"
              @input="onCEInput"
              @compositionstart="onCECompositionStart"
              @compositionend="onCECompositionEnd"
              @keydown="onKeydown"
              @paste="onPasteInput"
            ></div>
            <div class="assistant-input-toolbar">
              <div
                class="assistant-composer-controls"
                role="toolbar"
                :aria-label="t('assistant.composerControls')"
              >
                <button
                  type="button"
                  class="composer-control"
                  :class="{ active: thinkingEnabled || showThinkingMenu }"
                  :aria-pressed="thinkingEnabled"
                  aria-haspopup="dialog"
                  aria-controls="assistant-thinking-popover"
                  :aria-expanded="showThinkingMenu"
                  :disabled="sending"
                  :title="thinkingControlTitle"
                  @click.stop="toggleThinkingMenu"
                >
                  <span class="assistant-icon icon-thinking" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.thinking') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control"
                  :class="{ active: showToolList || autoAllowTools.length }"
                  aria-haspopup="dialog"
                  aria-controls="assistant-tools-popover"
                  :aria-expanded="showToolList"
                  :title="t('assistant.toolPopoverTitle', { count: autoAllowTools.length })"
                  @click.stop="toggleToolList"
                >
                  <span class="assistant-icon icon-tools" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.tools') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control caution"
                  :class="{ active: greenlightAvailable && engagementActive }"
                  :aria-pressed="greenlightAvailable && engagementActive"
                  :disabled="sending || greenlightBusy || !greenlightAvailable"
                  :title="!greenlightAvailable
                    ? t('assistant.authorizationUnavailableAcp')
                    : engagementActive
                      ? t('assistant.authorizationDisableTitle', { scope: engagementScopeSummary })
                      : t('assistant.authorizationEnableTitle')"
                  @click.stop="toggleGreenlightMode"
                >
                  <span class="assistant-icon icon-authorization" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.authorization') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control caution"
                  :class="{ active: autoApproveMode }"
                  :aria-pressed="autoApproveMode"
                  :disabled="autoApproveBusy"
                  :title="autoApproveMode
                    ? t('assistant.autoApproveDisableTitle')
                    : t('assistant.autoApproveEnableTitle')"
                  @click.stop="toggleAutoApproveMode"
                >
                  <span class="assistant-icon icon-bolt" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.autoApprove') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control"
                  :class="{ active: webSearchAllowed }"
                  :aria-pressed="webSearchAllowed"
                  :disabled="sending"
                  :title="webSearchAllowed
                    ? t('assistant.webSearchDisable')
                    : t('assistant.webSearchEnable')"
                  @click.stop="toggleWebSearch"
                >
                  <span class="assistant-icon icon-web" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.online') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control"
                  :disabled="sending"
                  :title="t('assistant.acpModelTitle')"
                  @click.stop="openModelPicker(false)"
                >
                  <span class="assistant-icon icon-model" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.acpModel') }}</span>
                  <span class="composer-control-value">
                    {{ activeModelLabel || t('assistant.acpModelUnset') }}
                  </span>
                </button>
                <button
                  v-if="reverseJobId"
                  type="button"
                  class="composer-control"
                  :title="t('assistant.openReverseTaskDir')"
                  @click="openReverseJobDir"
                >
                  <span class="assistant-icon icon-folder" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.taskDirectory') }}</span>
                </button>
                <button
                  v-if="reverseJobId"
                  type="button"
                  class="composer-control"
                  :title="t('assistant.openReverseWorkbench')"
                  @click="goReverseJobPanel"
                >
                  <span class="assistant-icon icon-workbench" aria-hidden="true"></span>
                  <span class="composer-control-label">{{ t('assistant.workbench') }}</span>
                </button>
                <button
                  type="button"
                  class="composer-control composer-context"
                  :class="{ active: showTokenUsage }"
                  :aria-pressed="showTokenUsage"
                  :aria-label="t('assistant.contextUsageTitle', {
                    percent: contextUsage.percentLabel,
                    used: contextUsage.usedLabel,
                    window: contextUsage.windowLabel,
                  })"
                  aria-haspopup="dialog"
                  aria-controls="assistant-token-usage-popover"
                  :aria-expanded="showTokenUsage"
                  :title="t('assistant.contextUsageTitle', {
                    percent: contextUsage.percentLabel,
                    used: contextUsage.usedLabel,
                    window: contextUsage.windowLabel,
                  })"
                  @click.stop="toggleTokenUsage"
                >
                  <span class="context-ring" :class="`tone-${contextUsage.tone}`" aria-hidden="true">
                    <svg class="context-ring-svg" viewBox="0 0 24 24">
                      <circle class="context-ring-track" cx="12" cy="12" r="9" />
                      <circle
                        class="context-ring-fill"
                        cx="12"
                        cy="12"
                        r="9"
                        :stroke-dasharray="contextUsage.circ"
                        :stroke-dashoffset="contextUsage.offset"
                      />
                    </svg>
                  </span>
                </button>
                <button
                  type="button"
                  class="composer-control attach-btn"
                  :class="{
                    'is-capability-unsupported': acpImageCapabilityState === 'unsupported',
                  }"
                  :disabled="sending"
                  :aria-label="pendingAttachments.length
                    ? t('assistant.attachmentsAria', { count: pendingAttachments.length })
                    : t('assistant.addImageOrFile')"
                  :title="attachmentButtonTitle"
                  @click="onPickFiles"
                >
                  <span class="assistant-icon icon-attach" aria-hidden="true"></span>
                  <span v-if="pendingAttachments.length" class="composer-control-count" aria-hidden="true">{{ pendingAttachments.length }}</span>
                  <span
                    v-else-if="isAcpSession && acpImageCapabilityState === 'unsupported'"
                    class="composer-capability-badge"
                    aria-hidden="true"
                  >×</span>
                </button>
                <button
                  v-if="voiceIsSupported"
                  type="button"
                  class="composer-control mic-btn"
                  :class="{ processing: voiceState === 'processing' }"
                  :disabled="sending || voiceState === 'processing'"
                  :aria-label="voiceState === 'processing' ? t('assistant.voiceProcessing') : t('assistant.voiceStartTitle')"
                  :title="voiceState === 'processing' ? t('assistant.voiceProcessing') : t('assistant.voiceStartTitle')"
                  @click="onMicClick"
                >
                  <span class="assistant-icon icon-mic" aria-hidden="true"></span>
                </button>
              </div>
              <div class="assistant-input-actions">
                <button
                  v-if="assistantRunActive && !hasAwaitingUserDecision && input.trim()"
                  type="button"
                  class="primary queue-send"
                  @click="queueUserInput()"
                >
                  {{ t('assistant.queue') }}
                </button>
                <button
                  type="button"
                  :class="assistantRunActive && !hasAwaitingUserDecision ? 'danger' : 'primary'"
                  :disabled="!(assistantRunActive && !hasAwaitingUserDecision) && !input.trim() && !pendingAttachments.length && !pendingVoice && !inputPathRefEntries.length"
                  @click="assistantRunActive && !hasAwaitingUserDecision ? cancelRun() : send()"
                >
                  {{ assistantRunActive && !hasAwaitingUserDecision ? t('common.cancel') : t('assistant.send') }}
                </button>
              </div>
            </div>
          </div>
          </template>
      </div>
      </div>
      <div class="drop-overlay" :class="{ visible: dragOver }" aria-hidden="true">{{ t('assistant.dropOverlay') }}</div>
    </div>
  </Transition>

  <WorkbenchModal
    :open="showNewSessionModal"
    size="md"
    :title="t('assistant.newAgentSession')"
    :close-label="t('common.close')"
    @close="showNewSessionModal = false"
  >
    <div class="new-agent-session">
      <div class="new-agent-flow">
        <span>{{ t('assistant.conversation') }}</span><b>→</b>
        <span class="accent">{{ selectedNewRuntime?.backend === 'native' ? 'Native' : 'ACP' }}</span><b>→</b>
        <span>{{ selectedNewRuntime?.name || t('assistant.nativeRuntime') }}</span>
      </div>
      <label class="new-agent-field">
        <span>{{ t('assistant.agentRuntime') }}</span>
        <NSelect
          v-model:value="newSessionForm.runtime_id"
          :options="agentRuntimeOptions"
          :loading="agentRuntimesLoading"
          :placeholder="t('assistant.chooseRuntime')"
        />
      </label>
      <div class="new-agent-field">
        <span>{{ t('assistant.workspace') }}</span>
        <div class="workspace-choices">
          <label :class="{ active: newSessionForm.workspace_mode === 'sandbox' }">
            <input v-model="newSessionForm.workspace_mode" type="radio" value="sandbox" />
            <span><strong>{{ t('assistant.isolatedSandbox') }}</strong><small>{{ t('assistant.isolatedSandboxHint') }}</small></span>
          </label>
          <label :class="{ active: newSessionForm.workspace_mode === 'project' }">
            <input v-model="newSessionForm.workspace_mode" type="radio" value="project" />
            <span><strong>{{ t('assistant.localProject') }}</strong><small>{{ t('assistant.localProjectHint') }}</small></span>
          </label>
        </div>
      </div>
      <div v-if="newSessionForm.workspace_mode === 'project'" class="project-picker">
        <code>{{ newSessionForm.project_dir || t('assistant.noProjectSelected') }}</code>
        <button class="ghost" type="button" @click="chooseAgentProjectDirectory">{{ t('assistant.chooseDirectory') }}</button>
      </div>
      <p class="runtime-lock-note">{{ t('assistant.runtimeLockHint') }}</p>
    </div>
    <template #footer>
      <button class="ghost" type="button" @click="showNewSessionModal = false">{{ t('common.cancel') }}</button>
      <button class="primary" type="button" :disabled="agentRuntimesLoading" @click="createAgentSessionTab">{{ t('assistant.createConversation') }}</button>
    </template>
  </WorkbenchModal>

  <WorkbenchModal
    :open="showModelModal"
    size="md"
    :title="t('assistant.acpModelTitle')"
    :close-label="t('common.close')"
    :close-on-esc="!modelSearchQuery"
    @close="showModelModal = false"
  >
    <div class="acp-model-picker">
      <label v-if="sessionModels.length" class="acp-model-search">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="7" cy="7" r="4.2" />
          <path d="m10.2 10.2 3.3 3.3" />
        </svg>
        <input
          ref="modelSearchInput"
          v-model="modelSearch"
          type="search"
          autofocus
          autocomplete="off"
          spellcheck="false"
          :placeholder="t('assistant.acpModelSearchPlaceholder')"
          :aria-label="t('assistant.acpModelSearchPlaceholder')"
          @keydown="onModelSearchKeydown"
        />
        <button
          v-if="modelSearch"
          type="button"
          class="acp-model-search-clear"
          :title="t('common.close')"
          :aria-label="t('assistant.acpModelSearchClear')"
          @click="clearModelSearch"
        >
          ×
        </button>
      </label>
      <p class="acp-model-hint">
        {{ isAcpSession ? t('assistant.acpModelHint') : t('assistant.modelHintNative') }}
      </p>
      <p v-if="modelsLoading && !sessionModels.length" class="acp-model-hint">
        {{ t('assistant.acpModelLoading') }}
      </p>
      <p v-else-if="modelError" class="acp-model-error">{{ modelError }}</p>
      <div v-else-if="visibleSessionModelGroups.length" class="acp-model-groups">
        <section
          v-for="(group, groupIndex) in visibleSessionModelGroups"
          :key="group.key"
          class="acp-model-group"
          :class="{
            active: group.active,
            collapsed: isModelGroupCollapsed(group.key),
          }"
        >
          <button
            type="button"
            class="acp-model-group-toggle"
            :aria-expanded="!isModelGroupCollapsed(group.key)"
            :aria-controls="`acp-model-group-${groupIndex}`"
            :title="isModelGroupCollapsed(group.key)
              ? t('assistant.acpModelExpand', { name: group.title })
              : t('assistant.acpModelCollapse', { name: group.title })"
            @click="toggleModelGroup(group.key)"
          >
            <span class="acp-model-group-chevron" aria-hidden="true"></span>
            <span class="acp-model-group-title">{{ group.title }}</span>
            <span class="acp-model-group-meta">{{ t('assistant.acpModelCount', { n: group.models.length }) }}</span>
            <span v-if="group.active" class="acp-model-current">{{ t('assistant.acpModelCurrent') }}</span>
          </button>
          <ul
            v-show="!isModelGroupCollapsed(group.key)"
            :id="`acp-model-group-${groupIndex}`"
            class="acp-model-list"
          >
            <li v-for="model in group.models" :key="`${model.config_id || model.index}:${model.value}`">
              <button
                type="button"
                :class="{ active: model.active }"
                :disabled="modelsLoading"
                @click="pickModel(model)"
              >
                <strong>{{ modelDisplayName(model) }}</strong>
                <span v-if="model.active" class="acp-model-current">{{ t('assistant.acpModelCurrent') }}</span>
              </button>
            </li>
          </ul>
        </section>
      </div>
      <p v-else-if="sessionModelGroups.length && modelSearchQuery" class="acp-model-empty">
        {{ t('assistant.acpModelNoMatch') }}
      </p>
      <p v-else class="acp-model-hint">
        {{ isAcpSession ? t('assistant.acpModelNone') : t('assistant.modelNoneNative') }}
      </p>
    </div>
    <template #footer>
      <button class="ghost" type="button" :disabled="modelsLoading" @click="openModelPicker(true)">
        {{ t('assistant.acpModelRefresh') }}
      </button>
      <button class="ghost" type="button" @click="showModelModal = false">{{ t('common.close') }}</button>
    </template>
  </WorkbenchModal>

  <SandboxContextMenu
    :open="messageContextMenu.open"
    :x="messageContextMenu.x"
    :y="messageContextMenu.y"
    :actions="messageContextActions"
    @close="closeMessageContextMenu"
    @action="handleMessageContextAction"
  />
</template>

<style scoped>
.new-agent-session{display:flex;flex-direction:column;gap:16px}.new-agent-flow{display:flex;align-items:center;gap:8px;overflow:auto;padding:11px 12px;border:1px solid var(--border);border-radius:9px;background:var(--bg-sunken)}.new-agent-flow span{flex:none;padding:6px 9px;border:1px solid var(--border);border-radius:7px;background:var(--card-bg);font:11px/1.2 var(--mono)}.new-agent-flow span.accent{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));color:var(--accent)}.new-agent-flow b{color:var(--muted-2);font-weight:400}.new-agent-field{display:flex;flex-direction:column;gap:7px;color:var(--muted);font-size:12px}.workspace-choices{display:grid;grid-template-columns:1fr 1fr;gap:9px}.workspace-choices>label{display:flex;align-items:flex-start;gap:9px;padding:12px;border:1px solid var(--border);border-radius:9px;background:var(--card-bg);cursor:pointer}.workspace-choices>label.active{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 12%,transparent)}.workspace-choices input{margin-top:2px;accent-color:var(--accent)}.workspace-choices span{display:flex;flex-direction:column;gap:4px}.workspace-choices strong{color:var(--fg-strong);font-size:12px}.workspace-choices small{color:var(--muted-2);font-size:11px;line-height:1.4}.project-picker{display:flex;align-items:center;gap:8px}.project-picker code{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:9px 10px;border:1px solid var(--border);border-radius:8px;background:var(--input-bg);font:11px/1.2 var(--mono)}.runtime-lock-note{margin:0;padding:10px 12px;border-left:2px solid var(--accent);background:var(--accent-soft);color:var(--muted);font-size:11px;line-height:1.5}
@media(max-width:620px){.workspace-choices{grid-template-columns:1fr}.project-picker{align-items:stretch;flex-direction:column}.project-picker button{width:100%}}
.acp-model-picker { display: flex; flex-direction: column; gap: 12px; }
.acp-model-search {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--input-bg, var(--bg-sunken));
  color: var(--muted-2);
}
.acp-model-search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 16%, transparent);
}
.acp-model-search > svg {
  width: 14px;
  height: 14px;
  flex: none;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
}
.acp-model-search input {
  min-width: 0;
  flex: 1;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 13px;
}
.acp-model-search input::placeholder { color: var(--muted-2); }
.acp-model-search input::-webkit-search-cancel-button { display: none; }
.acp-model-search-clear {
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.acp-model-search-clear:hover {
  background: var(--panel-hover);
  color: var(--fg-strong);
}
.acp-model-hint { margin: 0; color: var(--muted-2); font-size: 11px; line-height: 1.5; }
.acp-model-empty {
  margin: 0;
  padding: 22px 12px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.acp-model-error {
  margin: 0;
  padding: 10px 12px;
  border-left: 2px solid var(--danger, #e05252);
  background: color-mix(in srgb, var(--danger, #e05252) 10%, transparent);
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}
.acp-model-groups {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 420px;
  overflow: auto;
}
.acp-model-group {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken, var(--panel-2));
}
.acp-model-group.active {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
}
.acp-model-group-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 11px;
  border: 0;
  background: transparent;
  color: var(--fg);
  text-align: left;
  cursor: pointer;
}
.acp-model-group-toggle:hover {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.acp-model-group-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.acp-model-group-chevron {
  flex: none;
  width: 8px;
  height: 8px;
  border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted);
  transform: rotate(45deg);
  transition: transform .16s ease;
}
.acp-model-group.collapsed .acp-model-group-chevron {
  transform: rotate(-45deg);
}
.acp-model-group-title {
  min-width: 0;
  flex: 1;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.acp-model-group-meta {
  flex: none;
  color: var(--muted-2);
  font-size: 10px;
}
.acp-model-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0 8px 8px;
  list-style: none;
}
.acp-model-list button {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card-bg);
  color: var(--fg);
  text-align: left;
  cursor: pointer;
}
.acp-model-list button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.acp-model-list button:disabled { opacity: .6; cursor: default; }
.acp-model-list button.active {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent);
}
.acp-model-list strong {
  min-width: 0;
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.acp-model-current { flex: none; margin-top: 2px; color: var(--accent); font-size: 10px; }
.acp-model-group-toggle .acp-model-current { margin-top: 0; }
@media (prefers-reduced-motion: reduce) {
  .acp-model-group-chevron { transition: none; }
}
.desktop-control-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0 12px 8px;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--warning, #e6a23c) 45%, var(--border));
  background: color-mix(in srgb, var(--warning, #e6a23c) 12%, var(--panel-2));
  font-size: 12px;
}
.desktop-control-banner-main { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.desktop-control-scope { color: var(--muted); }

.macos-perm-banner {
  margin: 0 12px 8px;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--warning, #e6a23c) 40%, var(--border));
  background: color-mix(in srgb, var(--warning, #e6a23c) 10%, var(--panel-2));
  font-size: 12px;
}
.macos-perm-list { margin: 6px 0 0; padding-left: 0; list-style: none; }
.macos-perm-list li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 6px; color: var(--muted); }

.assistant-panel {
  position: fixed;
  max-width: calc(100vw - 24px);
  max-height: calc(100vh - var(--titlebar-h, 0px) - 24px);
  background: var(--card-bg);
  border-left: 1px solid var(--border);
  border: 1px solid var(--border);
  border-radius: 20px;
  display: flex; flex-direction: column;
  z-index: 50;
  box-shadow: var(--shadow-md);
  overflow: hidden;
}
.assistant-panel.maximized {
  border-radius: 16px;
}
.assistant-resize-handle {
  position: absolute;
  z-index: 3;
  touch-action: none;
}
.assistant-resize-handle.north {
  left: 12px;
  right: 12px;
  top: -4px;
  height: 8px;
  cursor: ns-resize;
}
.assistant-resize-handle.northwest {
  left: -4px;
  top: -4px;
  width: 14px;
  height: 14px;
  cursor: nwse-resize;
}
.assistant-resize-handle.northeast {
  right: -4px;
  top: -4px;
  width: 14px;
  height: 14px;
  cursor: nesw-resize;
}
.assistant-resize-handle.west {
  left: -4px;
  top: 12px;
  bottom: 12px;
  width: 8px;
  cursor: ew-resize;
}
.assistant-resize-handle.east {
  right: -4px;
  top: 12px;
  bottom: 12px;
  width: 8px;
  cursor: ew-resize;
}
.assistant-resize-handle.south {
  left: 12px;
  right: 12px;
  bottom: -4px;
  height: 8px;
  cursor: ns-resize;
}
.assistant-resize-handle.southwest {
  left: -4px;
  bottom: -4px;
  width: 14px;
  height: 14px;
  cursor: nesw-resize;
}
.assistant-resize-handle.southeast {
  right: -4px;
  bottom: -4px;
  width: 14px;
  height: 14px;
  cursor: nwse-resize;
}
.assistant-head {
  display: flex; align-items: center; justify-content: space-between; gap: 4px;
  min-height: 42px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  user-select: none;
  background: var(--chrome);
  overflow: hidden;
}
.assistant-head.draggable { cursor: grab; }
.assistant-head.draggable:active { cursor: grabbing; }
.assistant-title {
  flex: 0 1 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--fg-strong);
  font-size: 14px;
  font-weight: 750;
  letter-spacing: -0.01em;
  pointer-events: none;
}
.assistant-reverse-job-chip {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(74, 158, 255, 0.12);
  color: var(--text-muted, #64748b);
}
.assistant-reverse-job-btn {
  font-size: 11px;
  padding: 2px 8px;
}
.assistant-title-logo {
  width: 21px;
  height: 21px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  background: var(--brand-logo-bg, var(--panel-2));
  border: 1px solid var(--brand-logo-border, var(--border));
  overflow: hidden;
  flex: 0 0 auto;
}
.assistant-title-logo img {
  width: 16px;
  height: 16px;
  display: block;
  object-fit: contain;
}
.assistant-head-actions {
  display: flex;
  min-width: 0;
  flex: 1 1 auto;
  justify-content: flex-end;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  overflow: hidden;
  cursor: default;
}
.assistant-head-tools {
  display: flex;
  min-width: 0;
  flex: 0 1 auto;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
}
.assistant-head-tools::-webkit-scrollbar {
  display: none;
}
.assistant-window-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
}
.assistant-live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 120px;
  height: 24px;
  padding: 0 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
.assistant-live-indicator span:last-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.assistant-live-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent-2, #4f8cff);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 14%, transparent);
  animation: assistant-status-pulse 1.2s ease-in-out infinite;
}

/* ── Tab bar ──────────────────────────────────────────────────────────── */
.assistant-tabbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--chrome);
  flex: 0 0 auto;
  min-height: 36px;
}
.assistant-tabbar-scroll {
  flex: 1 1 auto;
  display: flex;
  gap: 4px;
  overflow-x: auto;
  scrollbar-width: thin;
}
.assistant-tabbar-scroll::-webkit-scrollbar {
  height: 4px;
}
.assistant-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 180px;
  min-width: 80px;
  height: 26px;
  padding: 0 6px 0 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11.5px;
  cursor: pointer;
  white-space: nowrap;
  flex: 0 0 auto;
  transition: background 120ms, color 120ms, border-color 120ms;
}
.assistant-tab:hover {
  background: var(--panel-hover);
  color: var(--fg-strong);
  transform: none;
}
.assistant-tab.active {
  background: var(--card-bg);
  color: var(--fg-strong);
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.assistant-tab-label {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 600;
}
.assistant-tab-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: var(--muted-2, #9ca3af);
}
.assistant-tab-dot-running {
  background: #3b82f6;
  animation: assistant-tab-pulse 1.4s ease-in-out infinite;
}
.assistant-tab-dot-awaiting {
  background: #f59e0b;
}
.assistant-tab-dot-error {
  background: #ef4444;
}
.assistant-tab-dot-unread {
  background: #10b981;
}
.assistant-tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 5px;
  border-radius: 8px;
  background: var(--accent);
  color: white;
  font-size: 9.5px;
  font-weight: 700;
  flex: 0 0 auto;
}
.assistant-tab-close {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--muted-2);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  flex: 0 0 auto;
  opacity: 0.55;
}
.assistant-tab-close:hover {
  background: color-mix(in srgb, #ef4444 22%, transparent);
  color: #ef4444;
  opacity: 1;
  transform: none;
}
.assistant-tab-add {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  flex: 0 0 auto;
  opacity: 0.75;
  transition: opacity 120ms, background 120ms, border-color 120ms;
}
.assistant-tab-add:hover:not(:disabled) {
  opacity: 1;
  background: var(--panel-2);
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  transform: none;
}
.assistant-tab-add:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
@keyframes assistant-tab-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.tool-toggle {
  height: 26px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  cursor: pointer;
  font-size: 10.5px;
  white-space: nowrap;
}
.tool-toggle.active {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 28%, transparent);
}
.tool-toggle:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.assistant-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 5px 0 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  cursor: pointer;
  font-size: 10.5px;
  white-space: nowrap;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.assistant-switch.on {
  color: var(--fg-strong, var(--fg));
  border-color: color-mix(in srgb, var(--accent) 32%, transparent);
  background: color-mix(in srgb, var(--accent) 8%, var(--panel-2));
}
.assistant-switch-ok.on {
  color: var(--ok, #32c878);
  border-color: color-mix(in srgb, var(--ok, #32c878) 42%, transparent);
  background: color-mix(in srgb, var(--ok, #32c878) 12%, var(--panel-2));
}
.assistant-switch:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.assistant-switch-label {
  line-height: 1;
}
.assistant-switch-track {
  position: relative;
  width: 28px;
  height: 16px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--fg) 16%, transparent);
  flex-shrink: 0;
  transition: background 0.15s;
}
.assistant-switch.on .assistant-switch-track {
  background: var(--accent);
}
.assistant-switch-ok.on .assistant-switch-track {
  background: var(--ok, #32c878);
}
.assistant-switch-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.28);
  transition: left 0.15s ease;
}
.assistant-switch.on .assistant-switch-thumb {
  left: 14px;
}
.icon-btn {
  width: 26px; height: 24px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid transparent; border-radius: 8px;
  background: transparent; color: var(--fg);
  cursor: pointer; font-size: 13px; line-height: 1;
  opacity: 0.7;
  transition: opacity 0.15s, background 0.15s, border-color 0.15s;
}
.assistant-icon {
  width: 14px;
  height: 14px;
  display: block;
  flex: 0 0 auto;
  background: currentColor;
  pointer-events: none;
  -webkit-mask-position: center;
          mask-position: center;
  -webkit-mask-repeat: no-repeat;
          mask-repeat: no-repeat;
  -webkit-mask-size: contain;
          mask-size: contain;
}
.icon-folder {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.4 1.5h4.6A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5v-7z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.4 1.5h4.6A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5v-7z'/%3E%3C/svg%3E");
}
.icon-files {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 3h2v2H2V3zm0 4h2v2H2V7zm0 4h2v2H2v-2zm3.5-8H14v2H5.5V3zm0 4H14v2H5.5V7zm0 4H14v2H5.5v-2z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 3h2v2H2V3zm0 4h2v2H2V7zm0 4h2v2H2v-2zm3.5-8H14v2H5.5V3zm0 4H14v2H5.5V7zm0 4H14v2H5.5v-2z'/%3E%3C/svg%3E");
}
.icon-thinking {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25a4.75 4.75 0 0 0-2.86 8.54c.46.35.7.72.76 1.08L6.08 12h3.84l.18-1.13c.06-.36.3-.73.76-1.08A4.75 4.75 0 0 0 8 1.25zM6.25 13h3.5v1h-3.5v-1z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25a4.75 4.75 0 0 0-2.86 8.54c.46.35.7.72.76 1.08L6.08 12h3.84l.18-1.13c.06-.36.3-.73.76-1.08A4.75 4.75 0 0 0 8 1.25zM6.25 13h3.5v1h-3.5v-1z'/%3E%3C/svg%3E");
}
.icon-tools {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M10.2 1.4a4 4 0 0 0-4.66 5.15L1.7 10.4a2.2 2.2 0 1 0 3.1 3.1l3.85-3.84a4 4 0 0 0 5.14-4.67l-2.3 2.3-1.9-.5-.5-1.9 2.3-2.3a4 4 0 0 0-1.19-1.19z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M10.2 1.4a4 4 0 0 0-4.66 5.15L1.7 10.4a2.2 2.2 0 1 0 3.1 3.1l3.85-3.84a4 4 0 0 0 5.14-4.67l-2.3 2.3-1.9-.5-.5-1.9 2.3-2.3a4 4 0 0 0-1.19-1.19z'/%3E%3C/svg%3E");
}
.icon-authorization {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25 13.5 3v4.1c0 3.35-2.15 6.23-5.5 7.65C4.65 13.33 2.5 10.45 2.5 7.1V3L8 1.25zm0 2.1L4.25 4.54V7.1c0 2.42 1.42 4.55 3.75 5.73 2.33-1.18 3.75-3.31 3.75-5.73V4.54L8 3.35z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25 13.5 3v4.1c0 3.35-2.15 6.23-5.5 7.65C4.65 13.33 2.5 10.45 2.5 7.1V3L8 1.25zm0 2.1L4.25 4.54V7.1c0 2.42 1.42 4.55 3.75 5.73 2.33-1.18 3.75-3.31 3.75-5.73V4.54L8 3.35z'/%3E%3C/svg%3E");
}
.icon-web {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 1.25a6.75 6.75 0 1 0 0 13.5 6.75 6.75 0 0 0 0-13.5zM3.1 7.25h2.08c.08-1.45.39-2.75.87-3.75A5.27 5.27 0 0 0 3.1 7.25zm3.58 0h2.64C9.2 5.28 8.62 2.75 8 2.75s-1.2 2.53-1.32 4.5zm4.14 0h2.08a5.27 5.27 0 0 0-2.95-3.75c.48 1 .79 2.3.87 3.75zm2.08 1.5h-2.08c-.08 1.45-.39 2.75-.87 3.75a5.27 5.27 0 0 0 2.95-3.75zm-3.58 0H6.68c.12 1.97.7 4.5 1.32 4.5s1.2-2.53 1.32-4.5zm-4.14 0H3.1a5.27 5.27 0 0 0 2.95 3.75c-.48-1-.79-2.3-.87-3.75z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 1.25a6.75 6.75 0 1 0 0 13.5 6.75 6.75 0 0 0 0-13.5zM3.1 7.25h2.08c.08-1.45.39-2.75.87-3.75A5.27 5.27 0 0 0 3.1 7.25zm3.58 0h2.64C9.2 5.28 8.62 2.75 8 2.75s-1.2 2.53-1.32 4.5zm4.14 0h2.08a5.27 5.27 0 0 0-2.95-3.75c.48 1 .79 2.3.87 3.75zm2.08 1.5h-2.08c-.08 1.45-.39 2.75-.87 3.75a5.27 5.27 0 0 0 2.95-3.75zm-3.58 0H6.68c.12 1.97.7 4.5 1.32 4.5s1.2-2.53 1.32-4.5zm-4.14 0H3.1a5.27 5.27 0 0 0 2.95 3.75c-.48-1-.79-2.3-.87-3.75z'/%3E%3C/svg%3E");
}
.icon-model {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M6 1h1.5v1.5h1V1H10v1.5h1.25c.69 0 1.25.56 1.25 1.25V5H14v1.5h-1.5v1H14V9h-1.5v1.25c0 .69-.56 1.25-1.25 1.25H10V13H8.5v-1.5h-1V13H6v-1.5H4.75c-.69 0-1.25-.56-1.25-1.25V9H2V7.5h1.5v-1H2V5h1.5V3.75c0-.69.56-1.25 1.25-1.25H6V1zM5 4v6h6V4H5zm1.5 1.5h3v3h-3v-3z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M6 1h1.5v1.5h1V1H10v1.5h1.25c.69 0 1.25.56 1.25 1.25V5H14v1.5h-1.5v1H14V9h-1.5v1.25c0 .69-.56 1.25-1.25 1.25H10V13H8.5v-1.5h-1V13H6v-1.5H4.75c-.69 0-1.25-.56-1.25-1.25V9H2V7.5h1.5v-1H2V5h1.5V3.75c0-.69.56-1.25 1.25-1.25H6V1zM5 4v6h6V4H5zm1.5 1.5h3v3h-3v-3z'/%3E%3C/svg%3E");
}
.icon-bolt {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8.5 1 3 9h3.4l-.9 6L13 7H9.4l.9-6z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8.5 1 3 9h3.4l-.9 6L13 7H9.4l.9-6z'/%3E%3C/svg%3E");
}
.icon-workbench {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 2h5v5H2V2zm7 0h5v5H9V2zM2 9h5v5H2V9zm7 0h5v5H9V9z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M2 2h5v5H2V2zm7 0h5v5H9V2zM2 9h5v5H2V9zm7 0h5v5H9V9z'/%3E%3C/svg%3E");
}
.icon-attach {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M10.9 2.45a3.05 3.05 0 0 0-4.31 0L2.95 6.09a4.55 4.55 0 1 0 6.43 6.43l3.16-3.16-1.06-1.06-3.16 3.16a3.05 3.05 0 0 1-4.31-4.31l3.64-3.64a1.55 1.55 0 1 1 2.19 2.19L6.32 9.22a.65.65 0 0 1-.92-.92l3.26-3.26L7.6 3.98 4.34 7.24a2.15 2.15 0 0 0 3.04 3.04L10.9 6.76a3.05 3.05 0 0 0 0-4.31z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M10.9 2.45a3.05 3.05 0 0 0-4.31 0L2.95 6.09a4.55 4.55 0 1 0 6.43 6.43l3.16-3.16-1.06-1.06-3.16 3.16a3.05 3.05 0 0 1-4.31-4.31l3.64-3.64a1.55 1.55 0 1 1 2.19 2.19L6.32 9.22a.65.65 0 0 1-.92-.92l3.26-3.26L7.6 3.98 4.34 7.24a2.15 2.15 0 0 0 3.04 3.04L10.9 6.76a3.05 3.05 0 0 0 0-4.31z'/%3E%3C/svg%3E");
}
.icon-mic {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25a2.25 2.25 0 0 0-2.25 2.25v4.5A2.25 2.25 0 0 0 8 10.25a2.25 2.25 0 0 0 2.25-2.25v-4.5A2.25 2.25 0 0 0 8 1.25zM5.25 3.5a2.75 2.75 0 0 1 5.5 0v4.5a2.75 2.75 0 0 1-5.5 0v-4.5zM3.5 7.25h1v1.25a3.5 3.5 0 0 0 7 0V7.25h1v1.25a4.5 4.5 0 0 1-4 4.47V14h-1v-1.03a4.5 4.5 0 0 1-4-4.47V7.25z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M8 1.25a2.25 2.25 0 0 0-2.25 2.25v4.5A2.25 2.25 0 0 0 8 10.25a2.25 2.25 0 0 0 2.25-2.25v-4.5A2.25 2.25 0 0 0 8 1.25zM5.25 3.5a2.75 2.75 0 0 1 5.5 0v4.5a2.75 2.75 0 0 1-5.5 0v-4.5zM3.5 7.25h1v1.25a3.5 3.5 0 0 0 7 0V7.25h1v1.25a4.5 4.5 0 0 1-4 4.47V14h-1v-1.03a4.5 4.5 0 0 1-4-4.47V7.25z'/%3E%3C/svg%3E");
}
.mic-btn.processing .assistant-icon {
  opacity: 0.5;
}
/* ─────────── Voice recording capsule ───────────
   While recording is active, the foot row swaps the composer for
   [X cancel] [── live capsule ──] [✓ finish]. The capsule shows: a
   level-driven pulsing dot, mm:ss / max time, a sliding RMS waveform,
   and a bottom progress bar that ticks up to the 120 s hard cap.
*/
.assistant-foot-row.is-recording {
  gap: 6px;
  align-items: center;
}
.voice-cap-btn {
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
}
.voice-cap-cancel {
  border-color: color-mix(in srgb, var(--danger, #e74c3c) 50%, var(--border));
  color: var(--danger, #e74c3c);
}
.voice-cap-cancel .assistant-icon { background: var(--danger, #e74c3c); }
.voice-cap-finish {
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  border: none;
  color: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.18);
}
.voice-cap-finish .assistant-icon { background: #fff; }
.voice-cap-finish:hover { filter: brightness(1.05); }
.voice-capsule {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  height: 36px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--danger, #e74c3c) 35%, var(--border));
  background: color-mix(in srgb, var(--danger, #e74c3c) 6%, var(--panel-2));
  overflow: hidden;
}
.voice-cap-dot {
  flex: 0 0 10px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--danger, #e74c3c);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--danger, #e74c3c) 50%, transparent);
  transform-origin: center;
  transition: transform 60ms linear;
  animation: voice-cap-dot-pulse 1.4s ease-in-out infinite;
}
@keyframes voice-cap-dot-pulse {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--danger, #e74c3c) 60%, transparent); }
  50% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--danger, #e74c3c) 0%, transparent); }
}
.voice-cap-time {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--fg);
  letter-spacing: 0.02em;
}
.voice-cap-time-max { color: var(--muted); }
.voice-cap-bars {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 2px;
  height: 22px;
  overflow: hidden;
}
.voice-cap-bar {
  flex: 1 1 0;
  min-width: 2px;
  max-width: 4px;
  border-radius: 2px;
  background: color-mix(in srgb, var(--danger, #e74c3c) 75%, var(--accent, #4f7df0) 25%);
  transition: height 60ms linear;
}
.voice-cap-hint {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.voice-cap-progress {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--danger, #e74c3c), color-mix(in srgb, var(--danger, #e74c3c) 40%, var(--accent, #4f7df0)));
  transition: width 200ms linear;
  pointer-events: none;
}
@media (max-width: 540px) {
  .voice-cap-hint { display: none; }
}
.msg.user .msg-attachments {
  justify-content: flex-end;
  width: 100%;
}
.msg.user .msg-content > .voice-bubble {
  margin-left: auto;
}
.assistant-input-wrap.is-voice-message {
  min-height: 44px;
}
.assistant-voice-message {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  width: 100%;
  min-height: 36px;
  padding: 2px 0 4px;
}
.assistant-voice-remove {
  flex-shrink: 0;
  min-width: 28px;
  height: 28px;
  padding: 0;
  line-height: 1;
}
.voice-bubble {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 42px;
  padding: 9px 14px 9px 16px;
  border: 1px solid var(--border);
  border-radius: 16px;
  cursor: pointer;
  font: inherit;
  line-height: 1;
  color: var(--fg);
  background: var(--panel-2);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.04) inset;
  transition: filter 120ms ease, transform 80ms ease;
  flex-shrink: 0;
}
.msg.user .voice-bubble,
.voice-bubble-draft {
  color: #fff;
  border: none;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  border-bottom-right-radius: 5px;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.08) inset;
}
.msg.user .voice-bubble::after,
.voice-bubble-draft::after {
  content: '';
  position: absolute;
  right: -5px;
  bottom: 12px;
  width: 0;
  height: 0;
  border-top: 5px solid transparent;
  border-bottom: 5px solid transparent;
  border-left: 6px solid var(--accent-2, var(--accent));
}
.voice-bubble:hover:not(:disabled) {
  filter: brightness(1.03);
}
.msg.user .voice-bubble:hover:not(:disabled),
.voice-bubble-draft:hover:not(:disabled) {
  filter: brightness(1.06);
}
.voice-bubble:active:not(:disabled) {
  transform: scale(0.98);
}
.voice-bubble:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.voice-bubble-duration {
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0.02em;
  user-select: none;
}
.voice-bubble-waves {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transform: scaleX(-1);
}
.voice-bubble.is-playing .voice-wave-outer {
  animation: voice-wave-pulse 0.9s ease-in-out infinite;
}
.voice-bubble.is-playing .voice-bubble-waves path:not(.voice-wave-outer) {
  animation: voice-wave-pulse 0.9s ease-in-out infinite 0.15s;
}
@keyframes voice-wave-pulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}
.voice-bubble-audio {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
}
.voice-bubble-draft {
  margin: 0;
}
.voice-bubble-draft .attach-remove {
  position: absolute;
  top: -7px;
  right: -7px;
  width: 18px;
  height: 18px;
  padding: 0;
  border-radius: 9px;
  border: none;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 14px;
  line-height: 16px;
  cursor: pointer;
  z-index: 2;
}
.voice-bubble-draft .attach-remove:hover {
  background: rgba(0, 0, 0, 0.78);
}
.msg.assistant .voice-bubble {
  background: var(--panel-2);
  color: var(--fg);
  border: 1px solid var(--border);
  box-shadow: none;
}
.msg.assistant .voice-bubble::after {
  right: auto;
  left: -5px;
  border-left: none;
  border-right: 6px solid var(--panel-2);
  border-top: 5px solid transparent;
  border-bottom: 5px solid transparent;
}
.msg.assistant .voice-bubble-waves {
  transform: none;
}
.icon-plus {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M7 2h2v5h5v2H9v5H7V9H2V7h5V2z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M7 2h2v5h5v2H9v5H7V9H2V7h5V2z'/%3E%3C/svg%3E");
}
.icon-clock {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 1.75a6.25 6.25 0 1 0 0 12.5A6.25 6.25 0 0 0 8 1.75zM3.25 8a4.75 4.75 0 1 1 9.5 0 4.75 4.75 0 0 1-9.5 0zM7.25 4.5h1.5v3.1l2.1 1.3-.8 1.25-2.8-1.75V4.5z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 1.75a6.25 6.25 0 1 0 0 12.5A6.25 6.25 0 0 0 8 1.75zM3.25 8a4.75 4.75 0 1 1 9.5 0 4.75 4.75 0 0 1-9.5 0zM7.25 4.5h1.5v3.1l2.1 1.3-.8 1.25-2.8-1.75V4.5z'/%3E%3C/svg%3E");
}
.icon-agents {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='3.5' r='2.25' fill='black'/%3E%3Ccircle cx='4' cy='12' r='2' fill='black'/%3E%3Ccircle cx='12' cy='12' r='2' fill='black'/%3E%3Cpath d='M8 5.75v2.1M4 9.9V8h8v1.9' fill='none' stroke='black' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='3.5' r='2.25' fill='black'/%3E%3Ccircle cx='4' cy='12' r='2' fill='black'/%3E%3Ccircle cx='12' cy='12' r='2' fill='black'/%3E%3Cpath d='M8 5.75v2.1M4 9.9V8h8v1.9' fill='none' stroke='black' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
}
.icon-eye {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 2.5c-3.15 0-5.85 2.05-7.15 5.1a1 1 0 0 0 0 .8C2.15 11.45 4.85 13.5 8 13.5s5.85-2.05 7.15-5.1a1 1 0 0 0 0-.8C13.85 4.55 11.15 2.5 8 2.5zm0 9.5c-2.35 0-4.45-1.48-5.62-4C3.55 5.48 5.65 4 8 4s4.45 1.48 5.62 4C12.45 10.52 10.35 12 8 12zm0-6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M8 2.5c-3.15 0-5.85 2.05-7.15 5.1a1 1 0 0 0 0 .8C2.15 11.45 4.85 13.5 8 13.5s5.85-2.05 7.15-5.1a1 1 0 0 0 0-.8C13.85 4.55 11.15 2.5 8 2.5zm0 9.5c-2.35 0-4.45-1.48-5.62-4C3.55 5.48 5.65 4 8 4s4.45 1.48 5.62 4C12.45 10.52 10.35 12 8 12zm0-6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'/%3E%3C/svg%3E");
}
.icon-pause {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='3' y='2' width='4' height='12' rx='1' fill='black'/%3E%3Crect x='9' y='2' width='4' height='12' rx='1' fill='black'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='3' y='2' width='4' height='12' rx='1' fill='black'/%3E%3Crect x='9' y='2' width='4' height='12' rx='1' fill='black'/%3E%3C/svg%3E");
}
.icon-stop {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='2.5' y='2.5' width='11' height='11' rx='2' fill='black'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='2.5' y='2.5' width='11' height='11' rx='2' fill='black'/%3E%3C/svg%3E");
}
.icon-trash {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M6 1.5h4l.75 1.5H14v1.5H2V3h3.25L6 1.5zM3.25 5.5h9.5l-.65 8.1a1 1 0 0 1-1 .9H4.9a1 1 0 0 1-1-.9l-.65-8.1zM6 7v5h1.25V7H6zm2.75 0v5H10V7H8.75z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M6 1.5h4l.75 1.5H14v1.5H2V3h3.25L6 1.5zM3.25 5.5h9.5l-.65 8.1a1 1 0 0 1-1 .9H4.9a1 1 0 0 1-1-.9l-.65-8.1zM6 7v5h1.25V7H6zm2.75 0v5H10V7H8.75z'/%3E%3C/svg%3E");
}
.icon-maximize {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M3 3h10v10H3V3zm1.5 1.5v7h7v-7h-7z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M3 3h10v10H3V3zm1.5 1.5v7h7v-7h-7z'/%3E%3C/svg%3E");
}
.icon-restore {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M5 3h8v8h-2V5H5V3zM3 5h8v8H3V5zm1.5 1.5v5h5v-5h-5z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' fill-rule='evenodd' d='M5 3h8v8h-2V5H5V3zM3 5h8v8H3V5zm1.5 1.5v5h5v-5h-5z'/%3E%3C/svg%3E");
}
.icon-close {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M4.2 3.1 8 6.9l3.8-3.8 1.1 1.1L9.1 8l3.8 3.8-1.1 1.1L8 9.1l-3.8 3.8-1.1-1.1L6.9 8 3.1 4.2l1.1-1.1z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M4.2 3.1 8 6.9l3.8-3.8 1.1 1.1L9.1 8l3.8 3.8-1.1 1.1L8 9.1l-3.8 3.8-1.1-1.1L6.9 8 3.1 4.2l1.1-1.1z'/%3E%3C/svg%3E");
}
.icon-check {
  -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M13.485 4.515 6.5 11.5 2.515 7.515 3.93 6.101l2.57 2.57 5.57-5.571 1.415 1.415z'/%3E%3C/svg%3E");
          mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M13.485 4.515 6.5 11.5 2.515 7.515 3.93 6.101l2.57 2.57 5.57-5.571 1.415 1.415z'/%3E%3C/svg%3E");
}
.icon-btn:hover {
  opacity: 1;
  background: var(--panel-2);
  border-color: var(--border);
}

.tools-popover {
  position: absolute;
  top: 58px; right: 12px;
  width: min(420px, calc(100% - 24px));
  max-height: min(520px, calc(100% - 86px));
  z-index: 6;
  display: flex; flex-direction: column;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: var(--shadow-md);
  overflow: hidden;
}
.tools-popover-head {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 11px 12px;
  color: var(--fg-strong);
  border-bottom: 1px solid var(--border);
  background: var(--chrome);
}
.tools-popover-head span {
  display: block;
  font-size: 13px;
  font-weight: 750;
}
.tools-popover-head small {
  display: block;
  margin-top: 2px;
  color: var(--muted-2);
  font-size: 10px;
}
.tools-popover-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
.thinking-popover,
.usage-popover {
  top: auto;
  bottom: 86px;
  max-height: min(380px, calc(100% - 110px));
}
.thinking-popover {
  left: 12px;
  right: auto;
  width: min(440px, calc(100% - 24px));
}
.usage-popover {
  left: auto;
  right: 12px;
  width: min(340px, calc(100% - 24px));
  max-height: min(460px, calc(100% - 110px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--fg) 15%, transparent);
  border-radius: 12px;
  background: var(--card-bg);
  box-shadow: 0 14px 36px rgba(0, 0, 0, .28);
}
.usage-popover-head {
  flex: 0 0 auto;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--panel-2) 72%, var(--card-bg));
}
.usage-popover-close {
  display: inline-flex;
  width: 26px;
  height: 26px;
  flex: 0 0 26px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.usage-popover-close:hover {
  border-color: var(--border);
  background: var(--panel-hover);
  color: var(--fg);
}
.usage-popover-close:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.thinking-menu-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 14px 16px 16px;
}
.thinking-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 4px;
  margin: 8px 0 2px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 22%, var(--border));
  outline: none;
}
.thinking-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px;
  height: 16px;
  border: 2px solid var(--card-bg);
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent);
  cursor: pointer;
}
.thinking-slider::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border: 2px solid var(--card-bg);
  border-radius: 50%;
  background: var(--accent);
  cursor: pointer;
}
.thinking-slider:disabled {
  opacity: 0.45;
}
.thinking-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.thinking-step {
  height: 26px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.thinking-step:hover:not(:disabled) {
  color: var(--fg);
  border-color: color-mix(in srgb, var(--accent) 36%, var(--border));
}
.thinking-step.active {
  color: var(--accent-2, var(--accent));
  border-color: color-mix(in srgb, var(--accent) 42%, transparent);
  background: var(--accent-soft);
  font-weight: 650;
}
.thinking-step:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.tools-bulk-actions {
  display: flex; gap: 6px; flex-shrink: 0;
}
.tools-bulk-actions .ghost {
  height: 24px;
  padding: 0 10px;
  font-size: 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  cursor: pointer;
  transition: background 120ms, color 120ms, border-color 120ms;
}
.tools-bulk-actions .ghost:hover:not(:disabled) {
  background: var(--panel-hover);
  color: var(--fg-strong);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
}
.tools-bulk-actions .ghost:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.switch-btn {
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  cursor: pointer;
  font-size: 11px;
  white-space: nowrap;
}
.switch-btn.active {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 28%, transparent);
}
.switch-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.tools-empty { padding: 18px; color: var(--muted); font-size: 12px; text-align: center; }
.tools-list { overflow-y: auto; padding: 8px; }
.tool-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: 12px;
}
.tool-item:hover {
  background: var(--panel-2);
  border-color: var(--border);
}
.tool-title {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  margin-bottom: 5px;
}
.tool-main {
  min-width: 0;
  flex: 1;
}
.tool-switch {
  flex: 0 0 auto;
  margin-top: 2px;
}
.tool-title code {
  color: var(--fg-strong);
  font-size: 12px;
}
.tool-desc {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}
.tool-params {
  margin-top: 5px;
  color: var(--muted-2);
  font-size: 10px;
  word-break: break-word;
}
.risk-pill {
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 10px;
  background: var(--panel);
  color: var(--muted);
  border: 1px solid var(--border);
}
.risk-high,
.risk-medium-high {
  color: var(--err);
  background: color-mix(in srgb, var(--err) 10%, transparent);
  border-color: color-mix(in srgb, var(--err) 24%, transparent);
}
.risk-medium,
.risk-medium-low {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  border-color: color-mix(in srgb, var(--warn) 24%, transparent);
}
.double-confirm-pill {
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 10px;
  color: var(--err);
  background: color-mix(in srgb, var(--err) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--err) 38%, transparent);
  cursor: help;
}
.tool-noconfirm-pill {
  flex-shrink: 0;
  align-self: flex-start;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--muted);
  background: color-mix(in srgb, var(--muted) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--muted) 24%, transparent);
  cursor: help;
  white-space: nowrap;
}

.session-popover {
  position: absolute;
  top: 58px; right: 12px;
  width: min(320px, calc(100% - 24px));
  max-height: min(420px, calc(100% - 86px));
  z-index: 5;
  display: flex; flex-direction: column;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-md);
  overflow: hidden;
}
.session-popover-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  color: var(--fg-strong);
  font-size: 13px; font-weight: 700;
  border-bottom: 1px solid var(--border);
  background: var(--chrome);
}
.session-popover-head .ghost { padding: 3px 8px; font-size: 12px; }
.session-popover-actions { display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; justify-content: flex-end; }
.session-popover-actions .ghost.danger { color: var(--danger, #c0392b); }
.session-popover-actions .ghost.danger:disabled { opacity: 0.45; cursor: not-allowed; }
.session-filter-wrap {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}
.session-filter {
  width: 100%;
  box-sizing: border-box;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--input-bg);
  color: var(--fg);
  padding: 0 10px;
  font: inherit;
  font-size: 12px;
  outline: none;
}
.session-filter::placeholder { color: var(--muted-2); }
.session-filter:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
}
.session-empty { padding: 22px 16px; color: var(--muted); font-size: 12px; text-align: center; }
.session-list { overflow-y: auto; padding: 6px; }
.session-manage-all {
  display: flex;
  width: 100%;
  min-height: 38px;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  border: 0;
  border-top: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--accent);
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
}
.session-manage-all:hover { background: var(--panel-hover); }
.session-manage-all:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.session-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 9px 8px 9px 10px; border-radius: 10px;
  cursor: pointer; color: var(--muted);
  border: 1px solid transparent;
  margin-bottom: 2px;
}
.session-item.editing,
.session-item:has(.session-edit) {
  grid-template-columns: 1fr;
  cursor: default;
  background: var(--panel-2);
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
}
.session-item.active:not(.editing) {
  background: var(--accent-soft); color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 24%, transparent);
}
.session-item:hover:not(.editing) { color: var(--fg); background: var(--panel-2); }
.session-item.active:hover:not(.editing) {
  background: var(--accent-soft);
  color: var(--accent);
}
.session-main { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 2px; }
.session-name {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--fg);
}
.session-item.active .session-name { color: var(--accent); }
.session-meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; color: var(--muted-2); }
.session-note {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--muted);
}
.session-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  opacity: 0;
  transition: opacity .12s ease;
}
.session-item:hover .session-actions,
.session-item.active .session-actions,
.session-item:focus-within .session-actions { opacity: 1; }
@media (hover: none) {
  .session-actions { opacity: .85; }
}
.session-edit-btn {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: 11px;
  padding: 3px 6px;
  line-height: 1.2;
  border-radius: 6px;
}
.session-edit-btn:hover {
  color: var(--fg);
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}
.session-del { background: none; border: none; color: var(--muted-2); cursor: pointer; font-size: 15px; padding: 2px 6px; line-height:1; border-radius: 6px; }
.session-del:hover { color: var(--err); background: color-mix(in srgb, var(--err) 10%, transparent); }
.session-edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.session-edit-input,
.session-edit-note {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--input-bg);
  color: var(--fg);
  padding: 6px 8px;
  font: inherit;
  font-size: 12px;
  outline: none;
}
.session-edit-note {
  resize: vertical;
  min-height: 56px;
}
.session-edit-input:focus,
.session-edit-note:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
}
.session-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.assistant-body {
  position: relative;
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 18px;
  display: flex; flex-direction: column; gap: 14px;
}
.preview-selection-chip {
  display: inline-flex;
  align-self: flex-start;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 4px 6px 4px 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 12px;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.preview-selection-label {
  flex: 0 1 auto;
  min-width: 0;
  max-width: min(420px, 100%);
  color: var(--fg);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preview-selection-close {
  flex: 0 0 auto;
  padding: 0 6px;
  font-size: 14px;
  line-height: 1;
}
.load-earlier {
  display: flex;
  justify-content: center;
  gap: 8px;
  padding: 4px 0 8px;
  border-bottom: 1px dashed color-mix(in srgb, var(--border) 70%, transparent);
}
.load-earlier .ghost.tiny {
  font-size: 11px;
  opacity: 0.75;
}
.load-earlier .ghost.tiny:hover {
  opacity: 1;
}
.pending-batch-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--accent-soft, rgba(96, 165, 250, 0.12));
  font-size: 12px;
}
.pending-batch-bar-text {
  flex: 1 1 auto;
  min-width: 0;
}
.pending-batch-bar-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}
.pending-batch-bar-actions .danger {
  border-color: color-mix(in srgb, var(--err) 40%, transparent);
  color: var(--err);
}
.pending-batch-bar-actions .primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.pending-batch-bar-actions .primary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.assistant-foot {
  position: relative;
  display: flex; flex-direction: column; gap: 7px; padding: 10px 12px;
  border-top: 1px solid var(--border);
  background: var(--chrome);
}
.assistant-foot-row {
  display: flex;
  align-items: stretch;
  gap: 6px;
}
.assistant-input-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
  container-type: inline-size;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--input-bg);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 2px;
  padding: 8px 10px 6px;
  overflow: hidden;
}
.assistant-input-ce {
  flex: 1;
  min-width: 0;
  max-width: 100%;
  min-height: 44px;
  max-height: 140px;
  padding: 2px 4px;
  font-size: 13px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--fg);
  overflow-x: hidden;
  overflow-y: auto;
  line-height: 1.5;
  outline: none;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.assistant-input-ce:empty::before {
  content: attr(data-placeholder);
  color: var(--muted);
  pointer-events: none;
  display: inline;
}
.assistant-input-wrap:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
.assistant-input-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 8px;
  min-height: 34px;
  padding-top: 2px;
}
.assistant-composer-controls {
  display: flex;
  min-width: 0;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.composer-control {
  position: relative;
  display: inline-flex;
  min-width: 0;
  max-width: 150px;
  height: 30px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 0 8px;
  overflow: hidden;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 11.5px;
  line-height: 1;
  white-space: nowrap;
}
button.composer-control {
  cursor: pointer;
}
button.composer-control:hover:not(:disabled) {
  border-color: var(--border);
  background: var(--panel-hover);
  color: var(--fg);
}
button.composer-control:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.composer-control.active {
  border-color: color-mix(in srgb, var(--accent) 28%, transparent);
  background: var(--accent-soft);
  color: var(--accent-2, var(--accent));
}
.composer-control.caution.active {
  border-color: color-mix(in srgb, var(--warning, #d99100) 34%, transparent);
  background: color-mix(in srgb, var(--warning, #d99100) 10%, transparent);
  color: var(--warning, #d99100);
}
.composer-control:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.composer-control-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.composer-control-value {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: 10.5px;
  font-weight: 600;
  text-overflow: ellipsis;
}
.context-ring {
  position: relative;
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  color: var(--muted);
}
.composer-control.composer-context {
  width: 28px;
  max-width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 8px;
}
.context-ring.tone-ok { color: var(--success, #3ba55d); }
.context-ring.tone-warn { color: var(--warning, #e6a23c); }
.context-ring.tone-danger { color: var(--danger, #e74c3c); }
.context-ring-svg {
  display: block;
  width: 18px;
  height: 18px;
  transform: rotate(-90deg);
}
.context-ring-track,
.context-ring-fill {
  fill: none;
  stroke-width: 2.4;
}
.context-ring-track {
  stroke: color-mix(in srgb, var(--fg) 14%, transparent);
}
.context-ring-fill {
  stroke: currentColor;
  stroke-linecap: round;
  transition: stroke-dashoffset 180ms ease;
}
.composer-control-count {
  position: absolute;
  top: 1px;
  right: 1px;
  min-width: 14px;
  height: 14px;
  padding: 0 3px;
  border-radius: 7px;
  background: var(--accent);
  color: #fff;
  font-size: 8.5px;
  font-weight: 700;
  line-height: 14px;
  text-align: center;
}
.assistant-input-tools,
.assistant-input-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}
@container (max-width: 520px) {
  .assistant-composer-controls .composer-control {
    width: 30px;
    max-width: 30px;
    padding: 0;
  }
  .assistant-composer-controls .composer-control-label,
  .assistant-composer-controls .composer-control-value {
    display: none;
  }
  .assistant-composer-controls button.composer-control.active::after {
    content: "";
    position: absolute;
    right: 3px;
    bottom: 3px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: currentColor;
  }
  .assistant-composer-controls button.composer-control.composer-context.active::after {
    display: none;
  }
  .assistant-composer-controls .composer-control.composer-context {
    width: 28px;
    max-width: 28px;
  }
}
.assistant-active-agents {
  display: flex;
  min-width: 0;
  flex: 1 1 auto;
  align-items: center;
  gap: 5px;
  overflow-x: auto;
  padding: 2px 1px;
  scrollbar-width: none;
}
.assistant-active-agents::-webkit-scrollbar {
  display: none;
}
.assistant-active-agent {
  display: inline-flex;
  min-width: 0;
  max-width: 145px;
  height: 25px;
  flex: 0 1 auto;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: 7px;
  background: color-mix(in srgb, var(--accent) 8%, var(--input-bg));
  color: var(--fg);
  cursor: pointer;
}
.assistant-active-agent:hover {
  border-color: color-mix(in srgb, var(--accent) 68%, var(--border));
  background: color-mix(in srgb, var(--accent) 14%, var(--input-bg));
}
.assistant-active-agent-name {
  overflow: hidden;
  min-width: 0;
  font-size: 10.5px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-active-agent-pulse {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 50%, transparent);
  animation: assistant-agent-breathe 1.8s ease-in-out infinite;
}
.assistant-active-agent.is-queued .assistant-active-agent-pulse {
  background: var(--muted);
  animation-duration: 2.5s;
}
.assistant-active-agent.is-closing .assistant-active-agent-pulse {
  background: var(--warning, #d99100);
}
@keyframes assistant-agent-breathe {
  0%, 100% {
    opacity: 0.55;
    box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 38%, transparent);
    transform: scale(0.82);
  }
  50% {
    opacity: 1;
    box-shadow: 0 0 0 4px transparent;
    transform: scale(1);
  }
}
@media (prefers-reduced-motion: reduce) {
  .assistant-active-agent-pulse {
    animation: none;
  }
}
.assistant-input-actions .primary,
.assistant-input-actions .danger,
.assistant-input-actions .queue-send {
  height: 30px;
  padding: 0 12px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  white-space: nowrap;
  font-size: 12.5px;
}
:deep(.input-inline-path-chip) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 2px 7px 2px 21px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
  background: color-mix(in srgb, var(--accent) 12%, var(--input-bg));
  color: var(--fg-strong);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  position: relative;
  vertical-align: baseline;
  user-select: none;
}
:deep(.input-inline-path-chip::before) {
  content: '';
  position: absolute;
  left: 7px;
  top: 50%;
  width: 10px;
  height: 10px;
  margin-top: -5px;
  background: currentColor;
  opacity: 0.58;
  -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.5 2.75A.75.75 0 0 1 2.25 2h3.69a.75.75 0 0 1 .53.22l1.06 1.06H13.5A.75.75 0 0 1 14.25 4v8.5a.75.75 0 0 1-.75.75H2.25a.75.75 0 0 1-.75-.75v-9.75z' fill='black'/></svg>") center / contain no-repeat;
          mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.5 2.75A.75.75 0 0 1 2.25 2h3.69a.75.75 0 0 1 .53.22l1.06 1.06H13.5A.75.75 0 0 1 14.25 4v8.5a.75.75 0 0 1-.75.75H2.25a.75.75 0 0 1-.75-.75v-9.75z' fill='black'/></svg>") center / contain no-repeat;
}
.assistant-foot textarea:focus { outline: none; }
.assistant-foot-row > .primary,
.assistant-foot-row > .danger,
.assistant-foot-row > .queue-send {
  flex: 0 0 auto;
  height: 36px;
  padding: 0 14px;
  border-radius: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  white-space: nowrap;
}
.queue-send {
  padding: 0 12px;
}
.queued-strip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 5px;
  width: 100%;
  padding: 0 2px 4px;
}
.queued-chip {
  display: inline-flex;
  align-items: stretch;
  max-width: min(100%, 340px);
  min-height: 28px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  overflow: hidden;
}
.queued-chip-badge {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 7px;
  color: var(--muted);
  background: color-mix(in srgb, var(--chrome) 88%, var(--panel-2));
  border-right: 1px solid var(--border);
  user-select: none;
}
.queued-chip-badge svg { display: block; }
.queued-chip-input {
  flex: 1 1 auto;
  min-width: 100px;
  width: min(240px, 42vw);
  margin: 0;
  padding: 0 6px;
  border: 0;
  background: transparent;
  color: var(--fg);
  font-size: 12px;
  line-height: 28px;
  outline: none;
}
.queued-chip-input::placeholder {
  color: var(--muted-2);
}
.queued-chip:focus-within {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
}
.queued-chip-send {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: auto;
  margin: 0;
  padding: 0 10px;
  border: 0;
  border-left: 1px solid var(--border);
  border-radius: 0;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent-2, var(--accent));
  cursor: pointer;
  white-space: nowrap;
}
.queued-chip-send svg { display: block; }
.queued-chip-send:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 28%, transparent);
  color: var(--fg);
}
.queued-chip-send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.queued-chip-remove {
  flex: 0 0 auto;
  width: 26px;
  margin: 0;
  padding: 0;
  border: 0;
  border-left: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.queued-chip-remove:hover {
  color: var(--fg);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}
.attach-btn,
.mic-btn {
  flex: 0 0 auto;
  height: 30px;
  width: 30px;
  padding: 0;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.mic-btn .assistant-icon,
.attach-btn .assistant-icon {
  width: 16px;
  height: 16px;
}
.attach-btn.is-capability-unsupported {
  color: var(--danger, #e05252);
}
.composer-capability-badge {
  position: absolute;
  top: 0;
  right: 0;
  min-width: 12px;
  height: 12px;
  padding: 0 2px;
  border-radius: 999px;
  background: var(--warning, #d99100);
  color: #fff;
  font-size: 9px;
  font-weight: 800;
  line-height: 12px;
  text-align: center;
}
.attach-btn.is-capability-unsupported .composer-capability-badge {
  background: var(--danger, #e05252);
}
.attach-strip {
  display: flex; flex-wrap: wrap; gap: 8px;
  align-items: flex-end;
}
.attach-chip {
  position: relative;
  width: 56px; height: 56px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--input-bg);
}
.attach-chip img {
  width: 100%; height: 100%; object-fit: cover; display: block;
}
/* Text/file attachments aren't square thumbs 鈥?they show name + size
 * inline so the user can verify the right file landed. */
.attach-chip-file,
.attach-chip-error {
  width: auto;
  min-width: 140px;
  max-width: 220px;
  height: 56px;
  padding: 6px 28px 6px 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.attach-file-icon {
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
}
.attach-file-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.2;
}
.attach-file-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.attach-file-size {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.attach-chip-error {
  border-color: var(--danger, #e0606e);
}
.attach-chip-error .attach-file-icon {
  color: var(--danger, #e0606e);
}
.attach-remove {
  position: absolute;
  top: 2px; right: 2px;
  width: 18px; height: 18px;
  padding: 0;
  border-radius: 9px;
  border: none;
  background: rgba(0,0,0,0.55);
  color: #fff;
  font-size: 14px; line-height: 16px;
  cursor: pointer;
}
.attach-remove:hover { background: rgba(0,0,0,0.78); }
.attach-chip-clickable {
  cursor: pointer;
}
.attach-chip-clickable:hover,
.attach-chip-clickable:focus-visible {
  border-color: var(--accent, #4e8cff);
  outline: none;
}
.attach-edit-hint {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 3px 4px;
  color: #fff;
  background: rgba(0,0,0,0.62);
  font-size: 11px;
  line-height: 14px;
  text-align: center;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transform: translateY(100%);
  transition: opacity 120ms ease, transform 120ms ease;
}
.attach-chip-clickable:hover .attach-edit-hint,
.attach-chip-clickable:focus-visible .attach-edit-hint {
  opacity: 1;
  transform: translateY(0);
}
.msg-attachments {
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px;
}
.msg-attachment-wrap {
  position: relative;
  display: inline-block;
  border-radius: 10px;
}
.msg-attachment {
  display: block;
  width: 120px;
  max-height: 160px;
  padding: 0;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--border);
  background: var(--input-bg);
  cursor: pointer;
  transition: border-color 120ms ease, transform 120ms ease;
}
.msg-attachment:hover,
.msg-attachment:focus-visible {
  border-color: var(--accent, #4e8cff);
  outline: none;
  transform: translateY(-1px);
}
.msg-attachment img {
  display: block;
  width: 100%;
  height: auto;
  max-height: 160px;
  object-fit: cover;
  pointer-events: none;
}
/* Hover/focus reveal — keeps the thumbnail clean by default and only shows
   the reference + copy actions when the user hovers or tabs to the chip. */
.msg-attachment-actions {
  position: absolute;
  top: 4px;
  right: 4px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 140ms ease;
}
.msg-attachment-wrap:hover .msg-attachment-actions,
.msg-attachment-wrap:focus-within .msg-attachment-actions {
  opacity: 1;
  pointer-events: auto;
}
.msg-attachment-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px 2px 5px;
  border-radius: 9px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(0, 0, 0, 0.62);
  color: #f6f7f9;
  font-size: 10.5px;
  line-height: 1.2;
  cursor: pointer;
  transition: background 100ms ease, color 100ms ease, border-color 100ms ease;
  backdrop-filter: blur(4px);
}
.msg-attachment-action:hover {
  background: rgba(0, 0, 0, 0.82);
  border-color: rgba(255, 255, 255, 0.16);
}
.msg-attachment-action:focus-visible {
  outline: 2px solid var(--accent, #4e8cff);
  outline-offset: 1px;
}
.msg-attachment-action svg { opacity: 0.85; }
.msg-attachment-file {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--input-bg);
  font-size: 12px;
  color: var(--text);
  max-width: 280px;
}
.msg-attachment-file-icon { flex-shrink: 0; }
.msg-attachment-file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.msg-attachment-file-size {
  color: var(--muted);
  flex-shrink: 0;
}
.drop-overlay {
  position: absolute;
  inset: 0;
  display: flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--accent) 22%, transparent);
  color: var(--fg);
  font-weight: 600;
  pointer-events: none;
  border-radius: inherit;
  z-index: 60;
  opacity: 0;
  visibility: hidden;
}
.drop-overlay.visible {
  opacity: 1;
  visibility: visible;
}

.msg {
  max-width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 9px;
}
.msg.hidden {
  display: none;
}
.msg.grouped {
  margin-top: -8px;
}
.msg.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}
.msg-content {
  flex: 1 1 auto;
  min-width: 0;
  max-width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.msg.user .msg-content { align-items: flex-end; }
.msg-avatar {
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: -0.02em;
  user-select: none;
  box-shadow: 0 1px 0 rgba(255,255,255,0.08) inset, 0 8px 18px rgba(0,0,0,0.12);
}
.msg-avatar-spacer {
  width: 30px;
  flex: 0 0 30px;
}
.msg.assistant .msg-avatar {
  background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 82%, #fff), var(--accent-2));
  color: #fff;
}
.msg.user .msg-avatar {
  background: var(--panel-2);
  color: var(--fg-strong);
  border: 1px solid var(--border);
}
.msg-label { font-size: 10px; color: var(--muted-2); margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.05em; }
.msg-text {
  max-width: min(920px, 100%);
  min-width: 0;
  padding: 11px 14px; border-radius: 16px;
  font-size: 13px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
  user-select: text;
  -webkit-user-select: text;
  overflow-x: auto;
}
.msg.user .msg-text {
  --md-link-color: #fff;
  --md-link-underline: rgba(255, 255, 255, 0.72);
  --fg: #fff;
  --fg-strong: #fff;
  --muted: rgba(255, 255, 255, 0.86);
  --muted-2: rgba(255, 255, 255, 0.72);
  --bg-sunken: rgba(0, 0, 0, 0.18);
  --border: rgba(255, 255, 255, 0.28);
  --panel-2: rgba(0, 0, 0, 0.16);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  border-bottom-right-radius: 5px;
}
.msg.user .msg-text :deep(a.md-link) {
  color: #fff;
  text-decoration: none;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.72);
}
.msg.user .msg-text :deep(a.md-link:hover) {
  border-bottom-style: solid;
}
.msg.assistant .msg-text { background: var(--panel-2); color: var(--fg); border: 1px solid var(--border); border-bottom-left-radius: 5px; }
.message-edit {
  width: min(720px, 100%);
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 55%, var(--border));
  border-radius: 14px 14px 5px 14px;
  background: var(--panel-2);
  box-shadow: var(--shadow-sm);
}
.message-edit-textarea {
  display: block;
  width: 100%;
  min-height: 92px;
  max-height: 320px;
  resize: vertical;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg);
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  line-height: 1.55;
  outline: none;
}
.message-edit-textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
}
.message-edit-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 7px;
  margin-top: 8px;
}
.message-edit-hint {
  margin-right: auto;
  color: var(--muted-2);
  font-size: 10px;
}
.msg-reference {
  max-width: min(520px, 100%);
  margin-top: 4px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.msg.user .msg-reference {
  align-self: flex-end;
  text-align: right;
}

/* Live status row 鈥?sits beneath or alongside any streamed text so the
   user can see what the assistant is doing (which tool, which step, how
   many seconds in) without losing visibility of the actual reply. */
.stream-status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  color: var(--muted);
  font-size: 11.5px;
  font-style: italic;
}
.msg.assistant .msg-text > .stream-status-row:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}
.stream-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent-2, #4f8cff);
  flex-shrink: 0;
  animation: assistant-status-pulse 1.2s ease-in-out infinite;
}
.stream-status-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stream-status-elapsed {
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
  opacity: 0.7;
}
.assistant-agent-head-btn {
  position: relative;
}
.assistant-agent-head-btn.pending {
  color: var(--warning, #d99100);
}
.assistant-agent-head-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 15px;
  height: 15px;
  padding: 0 3px;
  border-radius: 999px;
  background: var(--warning, #d99100);
  color: #111;
  font-size: 9px;
  font-weight: 800;
  line-height: 15px;
}
/* Live sub-agent dock: a docked status bar (same family as .assistant-job-banner
   / .pending-batch-bar), NOT a chat bubble. It never gets an avatar or the
   markdown-callout treatment message content uses — this is transient chrome
   about what's happening right now, so it should read as a status strip, not
   as another piece of conversation content. */
.subagent-dock {
  margin: 8px 14px 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-soft);
  overflow: hidden;
}
.subagent-dock-strip {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 8px 10px;
  border: 0;
  background: transparent;
  color: var(--fg);
  text-align: left;
  cursor: pointer;
}
.subagent-dock-strip:hover {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.subagent-dock .state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: var(--accent);
}
.subagent-dock .state-dot.waiting {
  background: var(--warn);
  animation: assistant-job-pulse 1.4s ease-in-out infinite;
}
.subagent-dock-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1 1 auto;
}
.subagent-dock-copy strong {
  font-size: 12px;
  font-weight: 650;
  color: var(--fg-strong);
}
.subagent-dock-copy span {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.subagent-dock-action {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--accent);
  font-weight: 600;
}
.subagent-dock-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.subagent-dock-toolbar strong {
  font-weight: 650;
  color: var(--fg-strong);
}
.subagent-dock-summary {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.subagent-dock-body {
  max-height: min(46vh, 420px);
  overflow-y: auto;
  padding: 4px 6px 8px;
}
/* Dock already supplies the border/background/eyebrow via .subagent-dock-toolbar;
   strip the shared card's own box chrome here so it doesn't nest one bordered
   panel inside another. */
.subagent-dock-body :deep(.subagent-group) {
  margin: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

/* 子 Agent 运行中心 overlay chrome. Sizing/radius/spacing/tokens intentionally
   mirror SubAgentGroupCard.vue's .subagent-group so the inline stream card and
   this full focus layer read as the same design system (see AGENTS task: unify
   AssistantPanel overlay chrome with the shared card component). */
/* `/btw` overlay. Deliberately transient chrome: it holds an answer that was
   never written to the conversation, and dismissing it is the only exit. */
.assistant-side-layer {
  position: absolute;
  inset: 0;
  z-index: 66;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: color-mix(in srgb, #000 40%, transparent);
  border-radius: inherit;
}
.assistant-side-card {
  display: flex;
  width: min(560px, 100%);
  max-height: min(520px, 100%);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
  border-radius: 16px;
  background: var(--card-bg);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.34);
}
.assistant-side-head {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}
.assistant-side-head > div {
  min-width: 0;
  flex: 1;
}
.assistant-side-head strong {
  display: block;
  color: var(--fg-strong);
  font-size: 13px;
}
.assistant-side-head span {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
}
.assistant-side-body {
  flex: 1;
  overflow: auto;
  padding: 14px;
}
.assistant-side-question {
  margin: 0 0 10px;
  padding-left: 10px;
  border-left: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
  color: var(--muted);
  font-size: 12px;
}
.assistant-side-state {
  color: var(--muted);
  font-size: 12px;
}
.assistant-side-state.is-error {
  color: var(--danger, #e5534b);
}
.assistant-side-foot {
  flex: 0 0 auto;
  padding: 9px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
}
@media (max-width: 640px) {
  .assistant-side-card {
    width: 100%;
    max-height: 100%;
  }
}
.assistant-agent-center-layer {
  position: absolute;
  inset: 0;
  z-index: 65;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: color-mix(in srgb, #000 44%, transparent);
  border-radius: inherit;
}
.assistant-agent-center {
  display: flex;
  width: min(760px, 100%);
  height: min(580px, 100%);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--card-bg);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.34);
}
.assistant-agent-center-head {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}
.assistant-agent-center-head > div {
  min-width: 0;
  flex: 1;
}
.assistant-agent-center-head strong {
  display: block;
  color: var(--fg-strong);
  font-size: 13px;
}
.assistant-agent-center-head > div span {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 10.5px;
}
.assistant-agent-center-summary {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}
.assistant-agent-center-summary span {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 2px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--fg) 8%, transparent);
  color: var(--muted);
  font-size: 10.5px;
  font-weight: 650;
  white-space: nowrap;
}
.assistant-agent-center-summary .pending {
  color: var(--danger, #e45b5b);
  background: color-mix(in srgb, var(--danger, #e45b5b) 14%, transparent);
}
.assistant-agent-batchbar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
  color: var(--muted);
  font-size: 11px;
}
.assistant-agent-clear-finished {
  margin-left: auto;
}
.assistant-agent-center-body {
  display: flex;
  min-height: 0;
  flex: 1;
}
.assistant-agent-center-list {
  display: flex;
  width: min(280px, 38%);
  flex: 0 0 auto;
  flex-direction: column;
  overflow-y: auto;
  border-right: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 55%, var(--card-bg));
}
.assistant-agent-center-empty {
  padding: 24px 16px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.assistant-agent-center-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.assistant-agent-center-row input[type="checkbox"] {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
  margin-top: 3px;
  accent-color: var(--accent);
}
.assistant-agent-center-row:hover {
  background: color-mix(in srgb, var(--fg) 4%, transparent);
}
.assistant-agent-center-row.focused {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.assistant-agent-center-row-main:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.assistant-agent-center-row-main {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.assistant-agent-center-row-main > span:not(.assistant-agent-card-status):not(.assistant-agent-card-state) {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}
.assistant-agent-center-row-main strong {
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-agent-center-row-main small {
  overflow: hidden;
  color: var(--muted);
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-agent-center-detail {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
  padding: 12px 14px;
  background: color-mix(in srgb, var(--panel-2) 32%, var(--card-bg));
}
.assistant-agent-detail-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 12px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.assistant-agent-detail-facts div {
  display: grid;
  min-width: 0;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 6px;
  align-items: start;
}
.assistant-agent-detail-facts div:has(pre) {
  grid-column: 1 / -1;
  grid-template-columns: 1fr;
}
.assistant-agent-detail-facts dt {
  color: var(--muted);
  font-size: 10px;
}
.assistant-agent-detail-facts dd {
  min-width: 0;
  overflow: hidden;
  margin: 0;
  color: var(--fg);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-agent-detail-facts dd pre {
  max-height: 150px;
  overflow: auto;
  margin: 4px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--fg);
  font: 11px/1.5 var(--font-mono, "Cascadia Code", Consolas, monospace);
}
.assistant-agent-events {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.assistant-agent-events strong {
  color: var(--fg-strong);
  font-size: 11px;
}
.assistant-agent-events > div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 8px;
  font-size: 10.5px;
}
.assistant-agent-events code {
  flex: 0 0 auto;
  color: var(--accent);
  font-family: var(--font-mono, "Cascadia Code", Consolas, monospace);
}
.assistant-agent-events span {
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 640px) {
  .assistant-agent-center {
    width: 100%;
    height: 100%;
    border-radius: 0;
  }
  .assistant-agent-center-body {
    flex-direction: column;
  }
  .assistant-agent-center-list {
    width: 100%;
    max-height: 40%;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .assistant-agent-detail-facts {
    grid-template-columns: 1fr;
  }
}
.assistant-agent-card-status {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--muted);
}
.assistant-agent-card-status.is-running,
.assistant-agent-card-status.is-queued {
  background: var(--accent);
  animation: assistant-agent-breathe 1.8s ease-in-out infinite;
}
.assistant-agent-card-status.is-completed {
  background: var(--ok, #32c878);
}
.assistant-agent-card-status.is-failed {
  background: var(--danger, #e45b5b);
}
.assistant-agent-card-status.is-paused {
  background: var(--warning, #d99100);
}
.assistant-agent-card-state {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 10px;
}
.assistant-agent-card-actions {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 2px;
}
.assistant-agent-icon-action {
  display: inline-flex;
  width: 22px;
  height: 22px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.assistant-agent-icon-action:hover {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--fg);
}
.assistant-agent-icon-action.danger:hover {
  background: color-mix(in srgb, var(--danger, #e45b5b) 14%, transparent);
  color: var(--danger, #e45b5b);
}
.assistant-agent-icon-action .assistant-icon {
  width: 12px;
  height: 12px;
}
.assistant-job-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 14px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-soft);
  font-size: 12px;
}
.assistant-job-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--primary);
  animation: assistant-job-pulse 1.4s ease-in-out infinite;
}
.assistant-job-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}
.assistant-job-copy span {
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-job-progress {
  color: var(--muted);
  white-space: nowrap;
}
@keyframes assistant-job-pulse {
  50% { opacity: 0.35; }
}
@keyframes assistant-status-pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50%      { opacity: 1;    transform: scale(1.1); }
}

.message-blocks {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 4px 8px;
  margin-top: 4px;
  overflow: hidden;
}
.message-blocks :deep(.assistant-card.full),
.message-blocks :deep(.assistant-card.user_decision),
.message-blocks :deep(.assistant-card.user_decision_batch) {
  flex-basis: 100%;
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.agent-step-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  margin: 2px 0;
  padding: 3px 11px 3px 7px;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
}
.agent-step-toggle:hover {
  color: var(--fg);
  background: color-mix(in srgb, var(--panel-2) 65%, var(--accent) 14%);
}
.agent-step-chevron {
  flex: 0 0 auto;
  opacity: 0.65;
  transition: transform 0.15s ease;
}
.agent-step-toggle.open .agent-step-chevron { transform: rotate(90deg); }
.agent-step-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.agent-step-count { flex: 0 0 auto; opacity: 0.65; }
.agent-step-toggle.running {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.agent-step-running-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: agentStepPulse 1s ease-in-out infinite;
}
@keyframes agentStepPulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.tool-calls {
  flex-basis: 100%;
  width: 100%;
  margin-top: 6px;
  max-width: 100%;
  color: var(--muted);
  font-size: 11px;
}
.tool-calls summary {
  cursor: pointer;
  user-select: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  list-style: none;
}
.tool-calls summary::-webkit-details-marker {
  display: none;
}
.tool-calls-panel[open] > summary {
  margin-bottom: 6px;
}
.tool-call-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.tool-call-list-stack {
  flex-direction: column;
  flex-wrap: nowrap;
  gap: 4px;
  width: 100%;
}
.tool-calls-panel .tool-call-list-stack :deep(.assistant-card.collapsed),
.tool-calls-panel .tool-call-list-stack :deep(.assistant-card.structured_card) {
  display: flex;
  align-items: center;
  width: 100%;
  max-width: 100%;
  height: auto;
  min-height: 26px;
  padding: 2px 0;
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel-2) 70%, transparent);
}
.tool-calls-panel .tool-call-list-stack :deep(.assistant-card.collapsed .card-title) {
  max-width: none;
}
.tool-calls-panel .tool-call-list-stack :deep(.assistant-card.full) {
  width: 100%;
  flex-basis: 100%;
}
.tool-badge {
  font-size: 10px; padding: 2px 8px; border-radius: 999px;
  background: var(--accent-soft); color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 20%, transparent);
}
.message-actions {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  position: relative;
}
.message-actions .tiny {
  padding: 3px 9px;
  font-size: 11px;
  color: var(--muted);
}
.message-actions .tiny:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.message-actions .tiny.active {
  color: var(--fg-strong);
  border-color: var(--accent, #4f8ef7);
}

.options-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.opt-btn {
  font-size: 12px; padding: 6px 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--panel-2);
  color: var(--fg); cursor: pointer; transition: all 120ms;
}
.opt-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }

.perm-card { align-self: stretch; }
.perm-card .msg-text { border: 1px solid var(--warn); background: color-mix(in srgb, var(--warn) 8%, transparent); }
.perm-actions { display: flex; gap: 8px; margin-top: 10px; }

.slide-enter-active, .slide-leave-active { transition: transform 220ms ease, opacity 220ms ease; }
.slide-enter-from, .slide-leave-to { transform: translateX(24px); opacity: 0; }
</style>
