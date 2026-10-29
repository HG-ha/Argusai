<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import ListToolbar from './list/ListToolbar.vue'
import ListSelect from './list/ListSelect.vue'
import PageEmpty from './list/PageEmpty.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'
import MarkdownText from './assistant/MarkdownText.vue'
import AssistantCard from './assistant/cards/AssistantCard.vue'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'
import TeamModelPicker from './TeamModelPicker.vue'

const confirmDialog = useConfirm()
const sandboxBrowser = useSandboxBrowser()

const teams = ref([])
const loading = ref(false)
const searchQuery = ref('')
const statusFilter = ref('all')

const activeTeamId = ref('')
const teamDetail = ref(null)
const teamChat = ref([])
const teamChatLoading = ref(false)
const activeMemberId = ref('')
const memberDetail = ref(null)
const memberSearch = ref('')
const chatTargetId = ref('all')
const decisionBusy = ref(false)
const steerDraft = ref('')
const sendingSteer = ref(false)
const chatBodyEl = ref(null)
const chatStreamEl = ref(null)
const chatFollowScroll = ref(true)
const composerEl = ref(null)
const mentionOpen = ref(false)
const mentionQuery = ref('')
const mentionIndex = ref(0)
const fileInputEl = ref(null)
const pendingAttachments = ref([])
const teamWebSearch = ref(true)
const teamApprovalMode = ref('leader_auto')
const teamGreenlight = ref(false)
const greenlightBusy = ref(false)
const stoppingTeam = ref(false)
const avatarMenu = ref(null)
const showTokenStats = ref(false)
const tokenStats = ref(null)
const tokenStatsLoading = ref(false)

// Create-team modal
const showCreateTeam = ref(false)
const newTeamName = ref('')
const newTeamObjective = ref('')
const newTeamPreset = ref('')
const creatingTeam = ref(false)

// Add-member modal
const showAddMember = ref(false)
const addMemberTargetTeamId = ref('')
const editingMemberId = ref('')
const memberRole = ref('')
const memberObjective = ref('')
const memberPermission = ref('readonly')
const memberModel = ref('')
const memberApprovalMode = ref('leader_auto')
const memberWebSearch = ref(true)
const addingMember = ref(false)
const modelCatalog = ref([])
const modelsLoading = ref(false)
const showMemberModelPicker = ref(false)
const showTeamSandboxes = ref(false)
const teamSandboxes = ref([])
const teamSandboxesLoading = ref(false)

// Tool picker (scoped members): load the real catalog, no manual typing.
const toolCatalog = ref([])
const toolsLoading = ref(false)
const toolSearch = ref('')
const toolRiskFilter = ref('all')
const selectedTools = ref(new Set())
// Delegation-control / team tools can never be granted to a leaf member —
// the backend strips them; hide them from the picker too.
const HIDDEN_TOOL_PREFIXES = ['team_', 'spawn_', 'run_subagent', 'spawn_agent']
const HIDDEN_TOOL_NAMES = new Set([
  'send_agent_input', 'pause_agent', 'resume_agent', 'close_agent', 'wait_for_agents',
  'list_agents', 'get_agent',
])

// Suggested tool presets per common role so the user rarely types anything.
const ROLE_TOOL_SUGGESTIONS = {
  recon: ['http_probe', 'fetch_page_source', 'network_discover_hosts', 'dns_lookup'],
  'vuln-analyst': ['http_probe', 'sqli_probe_param', 'ssrf_probe_url_param', 'idor_compare_request'],
  reporter: ['export_task_report', 'show_structured_card'],
  'surface-mapper': ['network_discover_hosts', 'network_scan_ports', 'network_fingerprint_services'],
  'static-analyst': ['binary_list_functions', 'binary_disassemble', 'binary_decompile'],
  'crypto-spotter': ['sandbox_eval_js', 'js_eval_unpack'],
}

const statusFilterOptions = computed(() => [
  { label: t('team.filterAll'), value: 'all' },
  { label: t('team.statusRunning'), value: 'running' },
  { label: t('team.statusAttention'), value: 'attention' },
  { label: t('team.statusIdle'), value: 'idle' },
  { label: t('team.statusFailed'), value: 'failed' },
  { label: t('team.statusEmpty'), value: 'empty' },
])

const presetOptions = computed(() => [
  { value: '', label: t('team.presetCustom'), desc: t('team.presetCustomDesc') },
  { value: 'web_audit', label: t('team.presetWebAudit'), desc: 'recon · vuln-analyst · reporter' },
  { value: 'recon_sweep', label: t('team.presetReconSweep'), desc: 'surface-mapper · tech-fingerprint' },
  { value: 'reverse_triage', label: t('team.presetReverseTriage'), desc: 'static-analyst · crypto-spotter' },
])

const roleQuickChips = ['recon', 'vuln-analyst', 'reporter', 'surface-mapper', 'static-analyst', 'crypto-spotter']

function riskRank(risk) {
  return { high: 0, 'medium-high': 1, medium: 2, 'medium-low': 3, low: 4 }[risk] ?? 5
}
function riskLabel(risk) {
  return {
    high: t('team.riskHigh'),
    'medium-high': t('team.riskHigh'),
    medium: t('team.riskMedium'),
    'medium-low': t('team.riskMedium'),
    low: t('team.riskLow'),
  }[risk] || t('team.riskLow')
}
function riskClass(risk) {
  if (risk === 'high' || risk === 'medium-high') return 'high'
  if (risk === 'medium' || risk === 'medium-low') return 'medium'
  return 'low'
}

async function loadToolCatalog() {
  if (toolCatalog.value.length || toolsLoading.value) return
  toolsLoading.value = true
  try {
    const res = await api.call('list_assistant_tools', '')
    const tools = (res && res.tools) || []
    toolCatalog.value = tools
      .filter(tl => {
        const n = String(tl.name || '')
        if (HIDDEN_TOOL_NAMES.has(n)) return false
        return !HIDDEN_TOOL_PREFIXES.some(p => n.startsWith(p))
      })
      .sort((a, b) => riskRank(a.risk) - riskRank(b.risk) || String(a.name).localeCompare(String(b.name)))
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    toolsLoading.value = false
  }
}

const selectedMemberModelLabel = computed(() => {
  if (!memberModel.value) return t('team.modelDefault')
  const found = modelCatalog.value.find(item => String(item.value || '') === memberModel.value)
  return found?.label || found?.model || memberModel.value
})

async function loadModelCatalog(refresh = false) {
  if (!refresh && (modelCatalog.value.length || modelsLoading.value)) return
  modelsLoading.value = true
  try {
    const res = await api.call('list_team_member_models', !!refresh)
    modelCatalog.value = res?.ok && Array.isArray(res.models) ? res.models : []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    modelsLoading.value = false
  }
}

const toolRiskFilterOptions = computed(() => [
  { value: 'all', label: t('team.riskAll') },
  { value: 'low', label: t('team.riskLow') },
  { value: 'medium', label: t('team.riskMedium') },
  { value: 'high', label: t('team.riskHigh') },
])

const filteredToolCatalog = computed(() => {
  const q = toolSearch.value.trim().toLowerCase()
  const rf = toolRiskFilter.value
  return toolCatalog.value.filter(tl => {
    if (rf !== 'all' && riskClass(tl.risk) !== rf) return false
    if (!q) return true
    return `${tl.name || ''} ${tl.description || ''}`.toLowerCase().includes(q)
  })
})

const allFilteredSelected = computed(() =>
  filteredToolCatalog.value.length > 0
  && filteredToolCatalog.value.every(tl => selectedTools.value.has(tl.name)),
)

function isToolSelected(name) {
  return selectedTools.value.has(name)
}
function toggleTool(name) {
  const next = new Set(selectedTools.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  selectedTools.value = next
}
function toggleSelectAllFiltered() {
  const next = new Set(selectedTools.value)
  if (allFilteredSelected.value) {
    for (const tl of filteredToolCatalog.value) next.delete(tl.name)
  } else {
    for (const tl of filteredToolCatalog.value) next.add(tl.name)
  }
  selectedTools.value = next
}
function applyRoleSuggestion() {
  const picks = ROLE_TOOL_SUGGESTIONS[memberRole.value.trim()] || []
  if (!picks.length) return
  const avail = new Set(toolCatalog.value.map(tl => tl.name))
  const next = new Set(selectedTools.value)
  for (const p of picks) if (avail.has(p)) next.add(p)
  selectedTools.value = next
}
function clearSelectedTools() {
  selectedTools.value = new Set()
}

const inDetail = computed(() => !!activeTeamId.value && !!teamDetail.value)
const members = computed(() => (teamDetail.value?.members) || [])

const filteredMembers = computed(() => {
  const q = memberSearch.value.trim().toLowerCase()
  if (!q) return members.value
  return members.value.filter(m =>
    [m.role, m.objective, m.summary, m.status_message]
      .filter(Boolean)
      .some(v => String(v).toLowerCase().includes(q)),
  )
})

const activeFilterMember = computed(() =>
  members.value.find(m => m.agent_id === activeMemberId.value) || null,
)

const chatTargetOptions = computed(() => [
  {
    value: 'all',
    label: t('team.targetAll', { n: members.value.length }),
  },
  ...members.value.map(m => ({
    value: String(m.agent_id || ''),
    label: `@${m.role || 'member'}`,
  })),
])

const chatTargetMember = computed(() =>
  members.value.find(m => m.agent_id === chatTargetId.value) || null,
)

function fuzzyMentionScore(label, query) {
  const source = String(label || '').toLowerCase()
  const q = String(query || '').toLowerCase()
  if (!q) return 0
  if (source.startsWith(q)) return 0
  const includes = source.indexOf(q)
  if (includes >= 0) return 10 + includes
  let cursor = 0
  let score = 50
  for (const ch of q) {
    const found = source.indexOf(ch, cursor)
    if (found < 0) return -1
    score += found - cursor
    cursor = found + 1
  }
  return score
}

const mentionCandidates = computed(() => {
  const candidates = [
    { agent_id: 'all', role: 'all', label: t('team.targetEveryone') },
    ...members.value.map(m => ({
      agent_id: String(m.agent_id || ''),
      role: String(m.role || 'member'),
      label: String(m.role || 'member'),
      status: m.status,
    })),
  ]
  return candidates
    .map(item => ({ ...item, score: fuzzyMentionScore(item.label, mentionQuery.value) }))
    .filter(item => item.score >= 0)
    .sort((a, b) => a.score - b.score || a.label.localeCompare(b.label))
    .slice(0, 8)
})

function updateMentionState(event) {
  if (activeMemberId.value) {
    mentionOpen.value = false
    return
  }
  const el = event?.target || composerEl.value
  const cursor = Number(el?.selectionStart ?? steerDraft.value.length)
  const before = steerDraft.value.slice(0, cursor)
  // Match @ anywhere before the caret (Chinese text commonly has no space,
  // e.g. “请让@surface-mapper复核”). The token itself still ends at whitespace.
  const match = before.match(/@([^\s@]*)$/)
  if (!match) {
    mentionOpen.value = false
    mentionQuery.value = ''
    return
  }
  mentionQuery.value = match[1] || ''
  mentionIndex.value = 0
  mentionOpen.value = true
}

async function pickMention(candidate) {
  const el = composerEl.value
  const cursor = Number(el?.selectionStart ?? steerDraft.value.length)
  const before = steerDraft.value.slice(0, cursor)
  const match = before.match(/@([^\s@]*)$/)
  if (!match) return
  const atIndex = cursor - (match[1]?.length || 0) - 1
  const token = candidate.agent_id === 'all' ? '@all ' : `@${candidate.role} `
  steerDraft.value = steerDraft.value.slice(0, atIndex) + token + steerDraft.value.slice(cursor)
  chatTargetId.value = candidate.agent_id === 'all' ? 'all' : candidate.agent_id
  mentionOpen.value = false
  mentionQuery.value = ''
  await nextTick()
  el?.focus()
  const caret = atIndex + token.length
  el?.setSelectionRange?.(caret, caret)
}

function onComposerKeydown(event) {
  if (mentionOpen.value && mentionCandidates.value.length) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      mentionIndex.value = (mentionIndex.value + 1) % mentionCandidates.value.length
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      mentionIndex.value = (mentionIndex.value - 1 + mentionCandidates.value.length) % mentionCandidates.value.length
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      pickMention(mentionCandidates.value[mentionIndex.value])
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      mentionOpen.value = false
      return
    }
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendSteer()
  }
}

function mentionRecipientIds(text) {
  const lower = String(text || '').toLowerCase()
  if (["@all", "@everyone", "@全体", "@全员"].some(token => lower.includes(token))) {
    return []
  }
  const ids = members.value
    .filter(m => {
      const role = String(m.role || '').trim().toLowerCase()
      return role && lower.includes(`@${role}`)
    })
    .map(m => String(m.agent_id || ''))
  return [...new Set(ids)]
}

const runningTeamMembers = computed(() =>
  members.value.filter(m => ['running', 'queued', 'closing'].includes(String(m.status || ''))),
)

// Same set the main assistant folds behind "工具调用 · N 项". Model-authored
// cards stay visible; only the tool-result rows collapse.
const TEAM_TOOL_BLOCK_TYPES = new Set([
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
  'desktop_observe',
])

function isTeamToolBlock(block) {
  if (!block || typeof block !== 'object') return false
  if (block.render_policy === 'model_authored' || block.source === 'model') return false
  return TEAM_TOOL_BLOCK_TYPES.has(block.type)
}

function partitionTeamBlocks(item) {
  const blocks = Array.isArray(item?.blocks) ? item.blocks : []
  const primary = []
  const toolResults = []
  for (const block of blocks) {
    if (isTeamToolBlock(block)) toolResults.push(block)
    else primary.push(block)
  }
  const hasText = Boolean(String(item?.content || '').trim())
  const toolOnly = !hasText && primary.length === 0 && toolResults.length > 0
  return {
    primary,
    toolResults,
    // One stray trace can sit inline. Two or more (or a tool-only turn)
    // collapse to the same chip the assistant uses.
    foldTools: toolOnly ? toolResults.length >= 1 : toolResults.length >= 2,
  }
}

const visibleTeamChat = computed(() => {
  const selected = activeMemberId.value
  const rows = !selected
    ? teamChat.value.filter(item => String(item.visibility || 'group') !== 'private')
    : teamChat.value.filter(item => {
        if (String(item.visibility || 'group') !== 'private') return false
        const participants = Array.isArray(item.participant_agent_ids)
          ? item.participant_agent_ids
          : []
        return participants.includes(selected)
      })

  // Consecutive tool-only turns from the same member are one compact activity
  // row instead of a wall of separate pills.
  const merged = []
  for (const raw of rows) {
    const item = { ...raw }
    const previous = merged[merged.length - 1]
    if (
      item.type === 'activity'
      && previous?.type === 'activity'
      && previous.agent_id === item.agent_id
    ) {
      previous.tool_names = [
        ...new Set([...(previous.tool_names || []), ...(item.tool_names || [])]),
      ]
      previous.activity_count = Number(previous.activity_count || 1) + 1
      previous.timestamp = Math.max(Number(previous.timestamp || 0), Number(item.timestamp || 0))
      continue
    }
    if (item.type === 'activity') item.activity_count = 1
    else item.blockGroups = partitionTeamBlocks(item)
    merged.push(item)
  }
  return merged
})

const filteredTeams = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  const sf = statusFilter.value
  return teams.value.filter(tm => {
    const overall = tm.status?.overall || 'empty'
    if (sf && sf !== 'all' && overall !== sf) return false
    if (!q) return true
    const roles = (tm.members_preview || []).map(m => m.role || '').join(' ')
    return `${tm.name || ''} ${tm.objective || ''} ${roles}`.toLowerCase().includes(q)
  })
})

const overviewStats = computed(() => {
  let activeTeams = 0
  let runningMembers = 0
  let attentionCount = 0
  for (const tm of teams.value) {
    const st = tm.status || {}
    if (st.overall === 'running' || st.overall === 'attention') activeTeams += 1
    runningMembers += Number(st.active || 0)
    attentionCount += Number(st.attention || 0)
  }
  return {
    totalTeams: teams.value.length,
    activeTeams,
    runningMembers,
    attentionCount,
  }
})

function monogram(text) {
  const s = String(text || '').trim()
  if (!s) return 'TM'
  const parts = s.split(/[\s\-_/]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return s.slice(0, 2).toUpperCase()
}

function statusKind(overall) {
  if (overall === 'running') return 'running'
  if (overall === 'attention') return 'attention'
  if (overall === 'failed') return 'failed'
  if (overall === 'empty') return 'empty'
  return 'idle'
}

function statusPillClass(overall) {
  return {
    running: 'run',
    attention: 'warn',
    failed: 'err',
    idle: 'info',
    empty: '',
  }[overall] || ''
}

function statusLabel(summary) {
  const o = summary?.overall || 'empty'
  const key = {
    running: 'team.statusRunning',
    attention: 'team.statusAttention',
    failed: 'team.statusFailed',
    idle: 'team.statusIdle',
    empty: 'team.statusEmpty',
  }[o] || 'team.statusIdle'
  return t(key)
}

function memberStatusKind(m) {
  if (m?.deferred_start) return 'idle'
  const pending = m?.pending_confirmation?.pending_tools
  if (Array.isArray(pending) && pending.length) return 'attention'
  const status = String(m?.status || '')
  if (status === 'running' || status === 'queued' || status === 'closing') return 'running'
  if (status === 'failed') return 'failed'
  if (status === 'paused') return 'paused'
  if (status === 'completed') return 'done'
  return 'idle'
}

function memberPillClass(m) {
  return {
    running: 'run',
    attention: 'warn',
    paused: 'warn',
    failed: 'err',
    done: 'info',
    idle: '',
  }[memberStatusKind(m)] || ''
}

function memberStatusLabel(m) {
  if (m?.deferred_start) return t('team.mStatusStandby')
  const kind = memberStatusKind(m)
  if (kind === 'attention') return t('team.statusAttention')
  const status = String(m?.status || '')
  const key = {
    running: 'team.mStatusRunning',
    queued: 'team.mStatusQueued',
    closing: 'team.mStatusRunning',
    paused: 'team.mStatusPaused',
    completed: 'team.mStatusCompleted',
    failed: 'team.mStatusFailed',
    closed: 'team.mStatusClosed',
  }[status] || 'team.mStatusIdle'
  return t(key)
}

function latestPrivatePreview(agentId) {
  const id = String(agentId || '')
  if (!id) return ''
  const rows = teamChat.value
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const item = rows[i]
    if (String(item?.visibility || '') !== 'private') continue
    if (item?.type === 'activity') continue
    const participants = Array.isArray(item?.participant_agent_ids) ? item.participant_agent_ids : []
    const involved = participants.includes(id)
      || String(item?.agent_id || '') === id
      || String(item?.sender_agent_id || '') === id
    if (!involved) continue
    const text = String(item?.content || '').replace(/\s+/g, ' ').trim()
    if (text) return text
  }
  return ''
}

function memberActivityLine(m) {
  if (!m) return ''
  const pending = m.pending_confirmation?.pending_tools
  if (Array.isArray(pending) && pending.length) {
    return t('team.pendingConfirm', { name: pending[0]?.name || 'tool' })
  }
  if (m.status === 'running' || m.status === 'queued') {
    return m.status_message || m.objective || t('team.mStatusRunning')
  }
  // The member row opens a private thread. The agent summary is the last
  // thing they said in any room, so using it here pasted group replies
  // onto the private entry.
  return latestPrivatePreview(m.agent_id) || m.objective || ''
}

function formatTime(value) {
  const numeric = Number(value)
  if (!numeric) return '—'
  const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric)
  if (Number.isNaN(date.getTime())) return '—'
  const now = new Date()
  const delta = now.getTime() - date.getTime()
  if (delta >= 0 && delta < 60_000) return t('team.timeJustNow')
  if (delta >= 0 && delta < 3_600_000) {
    return t('team.timeMinutesAgo', { n: Math.max(1, Math.floor(delta / 60_000)) })
  }
  const clock = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (date.toDateString() === now.toDateString()) return clock
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) {
    return `${t('team.timeYesterday')} ${clock}`
  }
  const sameYear = date.getFullYear() === now.getFullYear()
  const day = date.toLocaleDateString([], sameYear
    ? { month: '2-digit', day: '2-digit' }
    : { year: 'numeric', month: '2-digit', day: '2-digit' })
  return `${day} ${clock}`
}

function formatFullTime(value) {
  const numeric = Number(value)
  if (!numeric) return ''
  const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}

function formatBytes(value) {
  const n = Number(value || 0)
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`
}

function formatTokens(value) {
  const n = Number(value || 0)
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}K`
  return `${(n / 1_000_000).toFixed(1)}M`
}

function bytesToBase64(bytes) {
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

async function addAttachmentFiles(files) {
  const next = [...pendingAttachments.value]
  for (const file of [...(files || [])]) {
    if (next.length >= 8) break
    if (file.size > 32 * 1024 * 1024) {
      showToast(t('team.attachmentTooLarge', { name: file.name }), 'error')
      continue
    }
    const raw = new Uint8Array(await file.arrayBuffer())
    next.push({
      id: `team-att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      type: 'file',
      name: file.name || 'attachment',
      mime: file.type || 'application/octet-stream',
      size: file.size,
      data_base64: bytesToBase64(raw),
    })
  }
  pendingAttachments.value = next
}

async function onAttachmentFiles(event) {
  const files = [...(event?.target?.files || [])]
  if (event?.target) event.target.value = ''
  await addAttachmentFiles(files)
}

async function onComposerPaste(event) {
  const clipboard = event?.clipboardData
  if (!clipboard) return
  const imageFiles = []
  for (const item of [...(clipboard.items || [])]) {
    if (item.kind !== 'file' || !String(item.type || '').startsWith('image/')) continue
    const file = item.getAsFile?.()
    if (file) imageFiles.push(file)
  }
  // Some WebView2 builds expose clipboard files but omit DataTransfer.items.
  if (!imageFiles.length) {
    for (const file of [...(clipboard.files || [])]) {
      if (String(file.type || '').startsWith('image/')) imageFiles.push(file)
    }
  }
  if (!imageFiles.length) return // preserve normal Ctrl+V text behavior
  event.preventDefault()
  await addAttachmentFiles(imageFiles)
}

function removeAttachment(id) {
  pendingAttachments.value = pendingAttachments.value.filter(item => item.id !== id)
}

async function openTokenStats() {
  if (!activeTeamId.value) return
  showTokenStats.value = true
  tokenStatsLoading.value = true
  try {
    const res = await api.call('get_team_token_usage', activeTeamId.value)
    if (!res?.ok) throw new Error(res?.error || t('team.tokenLoadFailed'))
    tokenStats.value = res
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    tokenStatsLoading.value = false
  }
}

async function loadTeams() {
  loading.value = true
  try {
    const res = await api.call('list_teams')
    teams.value = (res && res.teams) || []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    loading.value = false
  }
}

async function loadTeamDetail(teamId, { quiet = false } = {}) {
  if (!teamId) return
  try {
    const res = await api.call('get_team', teamId)
    if (res && res.ok) {
      teamDetail.value = res.team
      teamGreenlight.value = Boolean(res.team?.greenlight)
      const ms = res.team?.members || []
      if (activeMemberId.value && !ms.some(m => m.agent_id === activeMemberId.value)) {
        activeMemberId.value = ''
        memberDetail.value = null
        chatTargetId.value = 'all'
      }
    } else if (!quiet) {
      showToast(res?.error || t('team.loadFailed'), 'error')
    }
  } catch (e) {
    if (!quiet) showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function isTeamChatNearBottom(threshold = 80) {
  const el = chatBodyEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
}

function scrollTeamChatToBottom() {
  const el = chatBodyEl.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

function pinTeamChatToBottom() {
  nextTick(() => {
    scrollTeamChatToBottom()
    requestAnimationFrame(() => {
      scrollTeamChatToBottom()
      setTimeout(scrollTeamChatToBottom, 80)
    })
  })
}

function onTeamChatScroll() {
  chatFollowScroll.value = isTeamChatNearBottom(80)
}

function jumpTeamChatToBottom() {
  chatFollowScroll.value = true
  pinTeamChatToBottom()
}

let chatResizeObserver = null
function bindTeamChatFollow() {
  chatResizeObserver?.disconnect()
  chatResizeObserver = null
  const stream = chatStreamEl.value
  if (!stream || typeof ResizeObserver === 'undefined') return
  chatResizeObserver = new ResizeObserver(() => {
    if (chatFollowScroll.value) scrollTeamChatToBottom()
  })
  chatResizeObserver.observe(stream)
}

async function loadTeamChat(teamId, { quiet = false, scrollBottom = false } = {}) {
  if (!teamId) return
  const stick = scrollBottom || chatFollowScroll.value
  teamChatLoading.value = true
  try {
    const res = await api.call('get_team_chat', teamId)
    if (res?.ok) {
      teamChat.value = Array.isArray(res.messages) ? res.messages : []
      if (stick) {
        chatFollowScroll.value = true
        await nextTick()
        bindTeamChatFollow()
        pinTeamChatToBottom()
      }
    } else if (!quiet) {
      showToast(res?.error || t('team.loadFailed'), 'error')
    }
  } catch (e) {
    if (!quiet) showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    teamChatLoading.value = false
  }
}

async function openTeamSandboxes() {
  if (!activeTeamId.value) return
  showTeamSandboxes.value = true
  teamSandboxesLoading.value = true
  try {
    const res = await api.call('get_team_sandboxes', activeTeamId.value)
    if (!res?.ok) throw new Error(res?.error || t('team.sandboxLoadFailed'))
    teamSandboxes.value = Array.isArray(res.sandboxes) ? res.sandboxes : []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    teamSandboxesLoading.value = false
  }
}

function browseMemberSandbox(row) {
  const sessionId = String(row?.session_id || '')
  if (!sessionId) return
  showTeamSandboxes.value = false
  sandboxBrowser.openPanel(sessionId)
}

async function loadMember(agentId, { quiet = false, scrollBottom = false } = {}) {
  if (!activeTeamId.value || !agentId) return
  try {
    const res = await api.call('get_team_member', activeTeamId.value, agentId)
    if (res && res.ok) {
      const payload = (res.agent && typeof res.agent === 'object') ? res.agent : res
      memberDetail.value = payload
      if (scrollBottom) {
        await nextTick()
        if (chatBodyEl.value) chatBodyEl.value.scrollTop = chatBodyEl.value.scrollHeight
      }
    } else if (!quiet) {
      showToast(res?.error || t('team.loadFailed'), 'error')
    }
  } catch (e) {
    if (!quiet) showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function openTeam(teamId) {
  activeTeamId.value = teamId
  activeMemberId.value = ''
  memberDetail.value = null
  teamDetail.value = null
  teamChat.value = []
  chatTargetId.value = 'all'
  chatFollowScroll.value = true
  Promise.all([
    loadTeamDetail(teamId),
    loadTeamChat(teamId, { scrollBottom: true }),
  ])
}

function backToTeams() {
  activeTeamId.value = ''
  teamDetail.value = null
  teamChat.value = []
  activeMemberId.value = ''
  memberDetail.value = null
  chatTargetId.value = 'all'
  loadTeams()
}

function openGroupChat() {
  activeMemberId.value = ''
  memberDetail.value = null
  chatTargetId.value = 'all'
  teamApprovalMode.value = 'leader_auto'
  teamWebSearch.value = true
  nextTick(() => {
    if (chatBodyEl.value) chatBodyEl.value.scrollTop = chatBodyEl.value.scrollHeight
  })
}

function openMember(agentId) {
  activeMemberId.value = agentId
  memberDetail.value = null
  chatTargetId.value = agentId
  const member = members.value.find(item => item.agent_id === agentId)
  teamApprovalMode.value = String(member?.approval_mode || 'leader_auto')
  teamWebSearch.value = member?.allow_web_search !== false
  loadMember(agentId)
  nextTick(() => {
    if (chatBodyEl.value) chatBodyEl.value.scrollTop = chatBodyEl.value.scrollHeight
  })
}

function openCreateModal() {
  newTeamName.value = ''
  newTeamObjective.value = ''
  newTeamPreset.value = ''
  showCreateTeam.value = true
}

function selectPreset(preset) {
  newTeamPreset.value = preset.value
  if (preset.value && !newTeamName.value.trim()) {
    newTeamName.value = preset.label
  }
}

async function createTeam() {
  const name = newTeamName.value.trim()
  if (!name) { showToast(t('team.nameRequired'), 'error'); return }
  creatingTeam.value = true
  try {
    const res = await api.call('create_team', {
      name,
      objective: newTeamObjective.value.trim(),
      preset: newTeamPreset.value || '',
    })
    if (!res || !res.ok) throw new Error(res?.error || t('team.createFailed'))
    showCreateTeam.value = false
    await loadTeams()
    if (res.team?.team_id) openTeam(res.team.team_id)
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    creatingTeam.value = false
  }
}

function openAddMemberModal(teamId = '') {
  addMemberTargetTeamId.value = teamId || activeTeamId.value
  editingMemberId.value = ''
  memberRole.value = ''
  memberObjective.value = ''
  memberPermission.value = 'readonly'
  memberModel.value = ''
  memberApprovalMode.value = 'leader_auto'
  memberWebSearch.value = true
  toolSearch.value = ''
  toolRiskFilter.value = 'all'
  selectedTools.value = new Set()
  showAddMember.value = true
  loadToolCatalog()
  loadModelCatalog()
}

function openEditMemberModal(member) {
  if (!member?.agent_id) return
  addMemberTargetTeamId.value = activeTeamId.value
  editingMemberId.value = String(member.agent_id)
  memberRole.value = String(member.role || '')
  memberObjective.value = String(member.objective || '')
  memberPermission.value = String(member.permission || 'readonly')
  memberModel.value = String(member.model || '')
  memberApprovalMode.value = String(member.approval_mode || 'leader_auto')
  memberWebSearch.value = member.allow_web_search !== false
  toolSearch.value = ''
  toolRiskFilter.value = 'all'
  selectedTools.value = new Set(
    Array.isArray(member.requested_tools) ? member.requested_tools : [],
  )
  showAddMember.value = true
  loadToolCatalog()
  loadModelCatalog()
}

function openMemberModelPicker() {
  showMemberModelPicker.value = true
  loadModelCatalog()
}

function selectMemberModel(value) {
  memberModel.value = String(value || '')
}

async function addMember() {
  const targetId = addMemberTargetTeamId.value || activeTeamId.value
  const role = memberRole.value.trim()
  const objective = memberObjective.value.trim()
  if (!targetId || !role || !objective) {
    showToast(t('team.memberFieldsRequired'), 'error')
    return
  }
  if (memberPermission.value === 'scoped' && selectedTools.value.size === 0) {
    showToast(t('team.scopedNeedsTools'), 'error')
    return
  }
  addingMember.value = true
  try {
    const payload = {
      role,
      objective,
      permission: memberPermission.value,
      requested_tools: memberPermission.value === 'scoped' ? [...selectedTools.value] : [],
      model: memberModel.value,
      approval_mode: memberApprovalMode.value,
      allow_web_search: memberWebSearch.value,
    }
    const res = editingMemberId.value
      ? await api.call('update_team_member', targetId, editingMemberId.value, payload)
      : await api.call('add_team_member', targetId, payload)
    if (!res || !res.ok) {
      throw new Error(res?.error || (editingMemberId.value ? t('team.updateMemberFailed') : t('team.addMemberFailed')))
    }
    showAddMember.value = false
    if (activeTeamId.value === targetId) {
      await loadTeamDetail(targetId, { quiet: true })
      const nextId = editingMemberId.value || res.agent?.agent_id
      if (nextId) openMember(nextId)
    } else {
      openTeam(targetId)
    }
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    addingMember.value = false
  }
}

async function deleteTeam(teamId) {
  const ok = await confirmDialog({
    title: t('team.deleteTitle'),
    content: t('team.deleteConfirm'),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    const res = await api.call('delete_team', teamId)
    if (!res || !res.ok) throw new Error(res?.error || t('team.deleteFailed'))
    if (activeTeamId.value === teamId) backToTeams()
    else await loadTeams()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function pauseMember(m) {
  if (!activeTeamId.value || !m?.agent_id) return
  try {
    const res = await api.call('pause_team_member', activeTeamId.value, m.agent_id)
    if (!res?.ok) throw new Error(res?.error || 'pause failed')
    await loadTeamDetail(activeTeamId.value, { quiet: true })
    if (activeMemberId.value === m.agent_id) await loadMember(m.agent_id, { quiet: true })
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function resumeMember(m, message = '') {
  if (!activeTeamId.value || !m?.agent_id) return
  try {
    const res = await api.call('resume_team_member', activeTeamId.value, m.agent_id, message || '继续执行当前任务')
    if (!res?.ok) throw new Error(res?.error || 'resume failed')
    await loadTeamDetail(activeTeamId.value, { quiet: true })
    if (activeMemberId.value === m.agent_id) await loadMember(m.agent_id, { quiet: true })
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function stopMember(m) {
  if (!activeTeamId.value || !m?.agent_id) return
  try {
    const res = await api.call('close_team_member', activeTeamId.value, m.agent_id)
    if (!res?.ok) throw new Error(res?.error || 'stop failed')
    await loadTeamDetail(activeTeamId.value, { quiet: true })
    if (activeMemberId.value === m.agent_id) await loadMember(m.agent_id, { quiet: true })
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function removeMember(m) {
  if (!activeTeamId.value || !m?.agent_id) return
  try {
    const res = await api.call('delete_team_member', activeTeamId.value, m.agent_id)
    if (!res?.ok) throw new Error(res?.error || 'delete failed')
    await loadTeamDetail(activeTeamId.value, { quiet: true })
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function sendSteer() {
  const msg = steerDraft.value.trim()
  if ((!msg && !pendingAttachments.value.length) || !activeTeamId.value || sendingSteer.value) return
  sendingSteer.value = true
  try {
    const lower = msg.toLowerCase()
    const explicitAll = ["@all", "@everyone", "@全体", "@全员"].some(token => lower.includes(token))
    const mentioned = mentionRecipientIds(msg)
    const privateMode = Boolean(activeMemberId.value)
    const recipients = privateMode
      ? [activeMemberId.value]
      : explicitAll
        ? []
        : mentioned.length
          ? mentioned
          : chatTargetId.value === 'all'
            ? []
            : [chatTargetId.value]
    const res = await api.call(
      'send_team_message',
      activeTeamId.value,
      msg,
      recipients,
      privateMode ? 'private' : 'group',
      pendingAttachments.value,
      {
        allow_web_search: teamWebSearch.value,
      },
    )
    if (!res || !res.ok) throw new Error(res?.error || t('team.steerFailed'))
    steerDraft.value = ''
    pendingAttachments.value = []
    mentionOpen.value = false
    await Promise.all([
      loadTeamDetail(activeTeamId.value, { quiet: true }),
      loadTeamChat(activeTeamId.value, { quiet: true, scrollBottom: true }),
    ])
    if (activeMemberId.value) {
      await loadMember(activeMemberId.value, { quiet: true })
    }
  } catch (e) {
    const message = String(e?.message || e)
    showToast(
      /positional arguments|takes from 2 to 4/i.test(message)
        ? t('team.backendRestartRequired')
        : t('toast.error', { msg: message }),
      'error',
    )
  } finally {
    sendingSteer.value = false
  }
}

const pendingTools = computed(() => {
  const p = memberDetail.value?.pending_confirmation
  return (p && Array.isArray(p.pending_tools)) ? p.pending_tools : []
})

async function decideMember(verdict) {
  const tools = pendingTools.value
  if (!tools.length || decisionBusy.value) return
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
      verdict: 'allow',
      ...binding,
    }
  } else {
    confirmation = {
      type: 'batch_confirmation_decision',
      decisions: Object.fromEntries(tools.map(tl => [String(tl?.id || ''), 'allow'])),
      ...binding,
    }
  }
  decisionBusy.value = true
  try {
    const res = await api.call('confirm_team_member_action', activeTeamId.value, activeMemberId.value, confirmation)
    if (!res || !res.ok) throw new Error(res?.error || t('assistant.agentConfirmationFailed'))
    await loadTeamDetail(activeTeamId.value, { quiet: true })
    await loadMember(activeMemberId.value, { quiet: true })
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    decisionBusy.value = false
  }
}

useLiveChannel('teams', (payload) => {
  if (!activeTeamId.value) {
    loadTeams()
    return
  }
  if (!payload?.team_id || payload.team_id === activeTeamId.value) {
    loadTeamDetail(activeTeamId.value, { quiet: true })
    loadTeamChat(activeTeamId.value, { quiet: true })
    if (activeMemberId.value) loadMember(activeMemberId.value, { quiet: true })
  }
  loadTeams()
}, { debounceMs: 250 })

function memberByRole(role) {
  const name = String(role || '').trim().toLowerCase()
  if (!name) return null
  return members.value.find(m => String(m.role || '').trim().toLowerCase() === name) || null
}

function openAvatarMenu(event, source) {
  const role = String(source?.role_name || source?.role || '').trim()
  const agentId = String(source?.agent_id || '')
  const member = (agentId && members.value.find(m => m.agent_id === agentId)) || memberByRole(role)
  if (!member) return
  const menuWidth = 168
  const menuHeight = 132
  const x = Math.min(event.clientX, window.innerWidth - menuWidth - 8)
  const y = Math.min(event.clientY, window.innerHeight - menuHeight - 8)
  avatarMenu.value = {
    x: Math.max(8, x),
    y: Math.max(8, y),
    agentId: member.agent_id,
    role: member.role,
  }
}

function closeAvatarMenu() {
  avatarMenu.value = null
}

function mentionFromMenu() {
  const role = String(avatarMenu.value?.role || '').trim()
  closeAvatarMenu()
  if (!role) return
  if (activeMemberId.value) openGroupChat()
  const token = `@${role} `
  const current = String(steerDraft.value || '')
  steerDraft.value = current && !/\s$/.test(current) ? `${current} ${token}` : `${current}${token}`
  chatTargetId.value = 'all'
  nextTick(() => composerEl.value?.focus())
}

function privateChatFromMenu() {
  const agentId = String(avatarMenu.value?.agentId || '')
  closeAvatarMenu()
  if (agentId) openMember(agentId)
}

function directTargetFromMenu() {
  const agentId = String(avatarMenu.value?.agentId || '')
  closeAvatarMenu()
  if (!agentId) return
  if (activeMemberId.value) openGroupChat()
  chatTargetId.value = agentId
  nextTick(() => composerEl.value?.focus())
}

async function toggleTeamGreenlight() {
  if (!activeTeamId.value || greenlightBusy.value) return
  const enabled = !teamGreenlight.value
  greenlightBusy.value = true
  teamGreenlight.value = enabled
  try {
    const res = await api.call('set_team_greenlight', activeTeamId.value, enabled)
    if (!res?.ok) throw new Error(res?.error || t('team.greenlightFailed'))
    teamGreenlight.value = Boolean(res.greenlight)
    if (res.team) teamDetail.value = res.team
    showToast(teamGreenlight.value ? t('team.greenlightOn') : t('team.greenlightOff'))
  } catch (e) {
    teamGreenlight.value = !enabled
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    greenlightBusy.value = false
  }
}

async function stopAllMembers() {
  if (!activeTeamId.value || stoppingTeam.value || !runningTeamMembers.value.length) return
  const ok = await confirmDialog({
    title: t('team.stopAllTitle'),
    content: t('team.stopAllConfirm'),
    positiveText: t('team.stopAll'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  stoppingTeam.value = true
  try {
    const res = await api.call('stop_team_members', activeTeamId.value)
    if (!res?.ok) throw new Error(res?.error || t('team.stopAllFailed'))
    showToast(t('team.stopAllDone', { n: res.stopped_count || 0 }))
    await Promise.all([
      loadTeamDetail(activeTeamId.value, { quiet: true }),
      loadTeamChat(activeTeamId.value, { quiet: true }),
    ])
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    stoppingTeam.value = false
  }
}

function onAvatarMenuDismiss() {
  if (avatarMenu.value) closeAvatarMenu()
}

let pollTimer = null
onMounted(() => {
  document.addEventListener('pointerdown', onAvatarMenuDismiss)
  window.addEventListener('blur', onAvatarMenuDismiss)
  loadTeams()
  pollTimer = setInterval(() => {
    if (!activeTeamId.value) {
      if (teams.value.some(tm => tm.status?.overall === 'running')) loadTeams()
      return
    }
    const hasRunning = members.value.some(x => ['running', 'queued', 'closing'].includes(x.status))
    if (hasRunning) {
      loadTeamDetail(activeTeamId.value, { quiet: true })
      loadTeamChat(activeTeamId.value, { quiet: true })
    }
    if (activeMemberId.value) {
      const m = members.value.find(x => x.agent_id === activeMemberId.value)
      const st = String(m?.status || memberDetail.value?.status || '')
      if (['running', 'queued', 'closing'].includes(st)) {
        loadMember(activeMemberId.value, { quiet: true })
      }
    }
  }, 2200)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  chatResizeObserver?.disconnect()
  document.removeEventListener('pointerdown', onAvatarMenuDismiss)
  window.removeEventListener('blur', onAvatarMenuDismiss)
})
</script>

<template>
  <div class="page list-page team-page">
    <!-- ===================== Team Grid View ===================== -->
    <template v-if="!inDetail">
      <header class="page-head team-page-head">
        <div>
          <h2>{{ t('team.title') }}</h2>
          <p>{{ t('team.subtitle') }}</p>
        </div>
        <div class="row-actions">
          <button type="button" class="ghost" :disabled="loading" @click="loadTeams">
            {{ t('common.refresh') }}
          </button>
          <button type="button" class="primary" @click="openCreateModal">
            + {{ t('team.create') }}
          </button>
        </div>
      </header>

      <!-- Workbench live status strip -->
      <div class="status-strip">
        <div class="status-cell">
          <span class="status-cell-label">{{ t('team.statTotalTeams') }}</span>
          <strong class="status-cell-value">{{ overviewStats.totalTeams }}</strong>
        </div>
        <div class="status-cell">
          <span class="status-cell-label">{{ t('team.statActiveTeams') }}</span>
          <strong class="status-cell-value" :class="{ 'tone-accent': overviewStats.activeTeams > 0 }">
            {{ overviewStats.activeTeams }}
          </strong>
        </div>
        <div class="status-cell">
          <span class="status-cell-label">{{ t('team.statRunningMembers') }}</span>
          <strong class="status-cell-value" :class="{ 'tone-ok': overviewStats.runningMembers > 0 }">
            <span v-if="overviewStats.runningMembers > 0" class="inline-live-dot" aria-hidden="true"></span>
            {{ overviewStats.runningMembers }}
          </strong>
        </div>
        <div class="status-cell">
          <span class="status-cell-label">{{ t('team.statPendingConfirm') }}</span>
          <strong class="status-cell-value" :class="{ 'tone-warn': overviewStats.attentionCount > 0 }">
            {{ overviewStats.attentionCount }}
          </strong>
        </div>
      </div>

      <ListToolbar v-model="searchQuery" :placeholder="t('team.searchPlaceholder')">
        <template #filters>
          <ListSelect
            :model-value="statusFilter"
            :options="statusFilterOptions"
            min-width="120px"
            @update:model-value="statusFilter = $event"
          />
        </template>
      </ListToolbar>

      <div v-if="loading && !teams.length" class="team-state-box">{{ t('common.loading') }}</div>

      <PageEmpty
        v-else-if="!filteredTeams.length"
        :title="teams.length ? t('common.noMatch') : t('team.emptyTitle')"
        :description="teams.length ? t('common.noMatchHint') : t('team.emptyHint')"
        :action-label="teams.length ? '' : t('team.create')"
        @action="openCreateModal"
      />

      <div v-else class="team-grid-scroll">
        <div class="team-grid">
          <article
            v-for="team in filteredTeams"
            :key="team.team_id"
            class="team-card"
            :class="`is-${statusKind(team.status?.overall)}`"
            role="button"
            tabindex="0"
            @click="openTeam(team.team_id)"
            @keydown.enter.prevent="openTeam(team.team_id)"
          >
            <!-- Top header -->
            <div class="team-card-head">
              <div class="team-badge-icon" :class="`is-${statusKind(team.status?.overall)}`">
                <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="9" cy="8" r="3"/>
                  <circle cx="17" cy="9.5" r="2.3"/>
                  <path d="M3.5 19a5.5 5.5 0 0 1 11 0"/>
                  <path d="M15 19a4 4 0 0 1 6 0"/>
                </svg>
                <span
                  v-if="team.status?.overall === 'running' || team.status?.overall === 'attention'"
                  class="team-badge-ring"
                  aria-hidden="true"
                ></span>
              </div>
              <div class="team-card-title-wrap">
                <div class="team-card-title-row">
                  <strong class="team-card-title" :title="team.name">{{ team.name }}</strong>
                </div>
                <div class="team-card-meta">
                  <span>{{ t('team.memberCount', { n: team.status?.member_count || 0 }) }}</span>
                  <span v-if="team.status?.active">· {{ t('team.activeCount', { n: team.status.active }) }}</span>
                  <span>· {{ formatTime(team.updated_at) }}</span>
                </div>
              </div>
              <div class="team-card-head-right" @click.stop>
                <span class="pill team-state-pill" :class="statusPillClass(team.status?.overall)">
                  <span
                    v-if="team.status?.overall === 'running' || team.status?.overall === 'attention'"
                    class="pill-live-dot"
                    aria-hidden="true"
                  ></span>
                  {{ statusLabel(team.status) }}
                </span>
                <button
                  type="button"
                  class="ghost icon-btn team-card-del"
                  :title="t('common.delete')"
                  @click.stop="deleteTeam(team.team_id)"
                >
                  <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6">
                    <path d="M3 4h10M6 4V2.8c0-.4.3-.8.8-.8h2.4c.5 0 .8.4.8.8V4m1.5 0-.6 9.2c0 .5-.4.8-.9.8H6c-.5 0-.9-.3-.9-.8L4.5 4"/>
                  </svg>
                </button>
              </div>
            </div>

            <!-- Objective -->
            <p class="team-card-objective" :class="{ muted: !team.objective }">
              {{ team.objective || t('team.noObjective') }}
            </p>

            <!-- Live member roster box -->
            <div class="team-roster-box">
              <template v-if="(team.members_preview || []).length">
                <div
                  v-for="mp in team.members_preview.slice(0, 4)"
                  :key="mp.agent_id"
                  class="team-roster-row"
                  :class="`is-${memberStatusKind(mp)}`"
                >
                  <span class="roster-dot" :class="memberStatusKind(mp)" aria-hidden="true"></span>
                  <strong class="roster-role">{{ mp.role }}</strong>
                  <span class="roster-activity">{{ mp.status_message || mp.summary || mp.objective || memberStatusLabel(mp) }}</span>
                  <span v-if="mp.tool_calls" class="roster-tools mono">{{ mp.tool_calls }} tools</span>
                  <span class="roster-state" :class="memberStatusKind(mp)">{{ memberStatusLabel(mp) }}</span>
                  <div v-if="memberStatusKind(mp) === 'running'" class="roster-progress-bar" aria-hidden="true">
                    <i></i>
                  </div>
                </div>
                <div v-if="(team.status?.member_count || 0) > 4" class="team-roster-more">
                  +{{ (team.status?.member_count || 0) - 4 }} {{ t('team.moreMembers') }}
                </div>
              </template>
              <div v-else class="team-roster-empty" @click.stop>
                <span>{{ t('team.noMembers') }}</span>
                <button type="button" class="ghost sm" @click.stop="openAddMemberModal(team.team_id)">
                  + {{ t('team.addMember') }}
                </button>
              </div>
            </div>

            <!-- Footer -->
            <div class="team-card-foot">
              <span class="mono team-id-tag">#{{ team.team_id.slice(0, 8) }}</span>
              <span class="team-enter-link">{{ t('team.enterWorkspace') }} →</span>
            </div>
          </article>
        </div>
      </div>
    </template>

    <!-- ===================== Team Detail View (Members + Chat) ===================== -->
    <template v-else>
      <header class="page-head team-detail-head">
        <div class="team-detail-head-left">
          <button type="button" class="ghost sm" @click="backToTeams">
            ← {{ t('team.backToTeams') }}
          </button>
          <div class="team-detail-title-block">
            <div class="team-detail-title-row">
              <h2>{{ teamDetail.name }}</h2>
              <span class="pill" :class="statusPillClass(teamDetail.status?.overall)">
                <span
                  v-if="teamDetail.status?.overall === 'running' || teamDetail.status?.overall === 'attention'"
                  class="pill-live-dot"
                  aria-hidden="true"
                ></span>
                {{ statusLabel(teamDetail.status) }}
              </span>
              <span class="pill">{{ t('team.memberCount', { n: teamDetail.status?.member_count || 0 }) }}</span>
            </div>
            <p>{{ teamDetail.objective || t('team.noObjective') }}</p>
          </div>
        </div>
        <div class="row-actions">
          <button type="button" class="ghost" @click="openTeamSandboxes">
            {{ t('team.teamSandbox') }}
          </button>
          <button type="button" class="ghost" @click="loadTeamDetail(activeTeamId); loadTeamChat(activeTeamId)">
            {{ t('common.refresh') }}
          </button>
          <button type="button" class="primary" @click="openAddMemberModal(activeTeamId)">
            + {{ t('team.addMember') }}
          </button>
        </div>
      </header>

      <div class="team-workbench-shell">
        <!-- Left: group + member roster -->
        <aside class="team-member-rail">
          <div class="team-rail-search">
            <input
              v-model="memberSearch"
              type="search"
              :placeholder="t('team.searchMembersPlaceholder')"
            />
          </div>

          <button
            type="button"
            class="team-group-entry"
            :class="{ active: !activeMemberId }"
            @click="openGroupChat"
          >
            <span class="team-group-mark">
              <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="9" cy="8" r="3"/>
                <circle cx="17" cy="9.5" r="2.3"/>
                <path d="M3.5 19a5.5 5.5 0 0 1 11 0"/>
                <path d="M15 19a4 4 0 0 1 6 0"/>
              </svg>
            </span>
            <span class="team-group-entry-copy">
              <strong>{{ t('team.groupChat') }}</strong>
              <small>{{ t('team.groupChatMeta', { n: members.length }) }}</small>
            </span>
            <span v-if="runningTeamMembers.length" class="group-live-count">
              {{ runningTeamMembers.length }}
            </span>
          </button>

          <div v-if="!filteredMembers.length" class="team-rail-empty">
            <p>{{ members.length ? t('common.noMatch') : t('team.noMembers') }}</p>
            <button v-if="!members.length" type="button" class="primary sm" @click="openAddMemberModal(activeTeamId)">
              + {{ t('team.addMember') }}
            </button>
          </div>

          <div v-else class="team-member-list" role="list">
            <div
              v-for="m in filteredMembers"
              :key="m.agent_id"
              class="team-member-item"
              :class="{
                active: m.agent_id === activeMemberId,
                [`is-${memberStatusKind(m)}`]: true,
              }"
              role="listitem"
              @click="openMember(m.agent_id)"
            >
              <div
                class="member-avatar"
                :class="`is-${memberStatusKind(m)}`"
                :title="t('team.avatarMenuHint')"
                @click.stop
                @contextmenu.prevent.stop="openAvatarMenu($event, m)"
              >
                <span>{{ monogram(m.role) }}</span>
                <i class="member-avatar-dot" :class="memberStatusKind(m)" aria-hidden="true"></i>
              </div>
              <div class="member-copy">
                <div class="member-top-line">
                  <strong :title="m.role">{{ m.role }}</strong>
                  <span class="pill member-mini-pill" :class="memberPillClass(m)">{{ memberStatusLabel(m) }}</span>
                </div>
                <div class="member-sub-line" :title="memberActivityLine(m)">
                  {{ memberActivityLine(m) || t('team.noMessages') }}
                </div>
                <div v-if="memberStatusKind(m) === 'running'" class="roster-progress-bar" aria-hidden="true">
                  <i></i>
                </div>
              </div>
            </div>
          </div>
        </aside>

        <!-- Right: true merged group chat (member click only filters it) -->
        <section class="team-chat-pane">
          <template v-if="members.length">
            <header class="team-chat-head">
              <div v-if="!activeMemberId" class="team-chat-identity">
                <div class="team-group-mark large">
                  <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="9" cy="8" r="3"/>
                    <circle cx="17" cy="9.5" r="2.3"/>
                    <path d="M3.5 19a5.5 5.5 0 0 1 11 0"/>
                    <path d="M15 19a4 4 0 0 1 6 0"/>
                  </svg>
                </div>
                <div class="team-chat-title-copy">
                  <div class="team-chat-title-row">
                    <strong>{{ t('team.groupChat') }}</strong>
                    <span class="pill">{{ t('team.memberCount', { n: members.length }) }}</span>
                    <button
                      type="button"
                      class="ghost danger sm stop-all-btn"
                      :disabled="stoppingTeam || !runningTeamMembers.length"
                      :title="t('team.stopAllHint')"
                      @click="stopAllMembers"
                    >{{ t('team.stopAll') }}</button>
                    <span v-if="runningTeamMembers.length" class="pill run">
                      <span class="pill-live-dot" aria-hidden="true"></span>
                      {{ t('team.activeCount', { n: runningTeamMembers.length }) }}
                    </span>
                  </div>
                  <p class="team-chat-objective">
                    {{ t('team.groupChatHint') }}
                  </p>
                </div>
              </div>

              <div v-else-if="activeFilterMember" class="team-chat-identity">
                <div class="member-avatar" :class="`is-${memberStatusKind(activeFilterMember)}`">
                  <span>{{ monogram(activeFilterMember.role) }}</span>
                  <i class="member-avatar-dot" :class="memberStatusKind(activeFilterMember)" aria-hidden="true"></i>
                </div>
                <div class="team-chat-title-copy">
                  <div class="team-chat-title-row">
                    <strong>{{ activeFilterMember.role }}</strong>
                    <span class="pill" :class="memberPillClass(activeFilterMember)">
                      {{ memberStatusLabel(activeFilterMember) }}
                    </span>
                    <span class="pill mono">{{ activeFilterMember.permission || 'readonly' }}</span>
                  </div>
                  <p class="team-chat-objective">
                    {{ t('team.privateChatHint', { role: activeFilterMember.role }) }}
                  </p>
                </div>
              </div>

              <div v-if="memberDetail && activeMemberId" class="team-chat-actions">
                <button
                  type="button"
                  class="ghost danger sm"
                  :disabled="stoppingTeam || !runningTeamMembers.length"
                  :title="t('team.stopAllHint')"
                  @click="stopAllMembers"
                >{{ t('team.stopAll') }}</button>
                <button
                  type="button"
                  class="ghost sm"
                  @click="openEditMemberModal(memberDetail)"
                >{{ t('common.edit') }}</button>
                <button
                  v-if="['running', 'queued'].includes(memberDetail.status)"
                  type="button"
                  class="ghost sm"
                  @click="pauseMember(memberDetail)"
                >{{ t('team.pauseMember') }}</button>
                <button
                  v-if="['paused', 'completed', 'failed'].includes(memberDetail.status)"
                  type="button"
                  class="ghost sm"
                  @click="resumeMember(memberDetail)"
                >{{ t('team.resumeMember') }}</button>
                <button
                  v-if="['running', 'queued', 'paused'].includes(memberDetail.status)"
                  type="button"
                  class="ghost danger sm"
                  @click="stopMember(memberDetail)"
                >{{ t('team.stopMember') }}</button>
                <button
                  v-if="!['running', 'queued', 'closing'].includes(memberDetail.status)"
                  type="button"
                  class="ghost danger sm"
                  @click="removeMember(memberDetail)"
                >{{ t('common.delete') }}</button>
              </div>
            </header>

            <!-- Merged chronological stream across every member -->
            <div ref="chatBodyEl" class="team-chat-body" @scroll="onTeamChatScroll">
              <div ref="chatStreamEl" class="team-chat-stream">
              <div v-if="teamChatLoading && !visibleTeamChat.length" class="team-chat-empty">
                <p>{{ t('common.loading') }}</p>
              </div>

              <div v-else-if="!visibleTeamChat.length" class="team-chat-empty">
                <span class="team-empty-chat-icon">
                  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7">
                    <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5A2.5 2.5 0 0 1 4 13.5z"/>
                  </svg>
                </span>
                <strong>{{ activeMemberId ? t('team.noPrivateMessages') : t('team.noGroupMessages') }}</strong>
                <p>{{ activeMemberId
                  ? t('team.noPrivateMessagesHint', { role: activeFilterMember?.role || 'member' })
                  : t('team.noGroupMessagesHint') }}</p>
              </div>

              <template v-for="item in visibleTeamChat" :key="item.id">
                <div v-if="item.type === 'activity'" class="group-activity-row">
                  <span class="group-activity-avatar">{{ monogram(item.role_name) }}</span>
                  <span class="group-activity-copy">
                    <strong>{{ item.role_name }}</strong>
                    {{ t('team.usedTools') }}
                    <code>{{ (item.tool_names || []).join(', ') }}</code>
                  </span>
                  <time :title="formatFullTime(item.timestamp)">{{ formatTime(item.timestamp) }}</time>
                </div>

                <div v-else class="msg" :class="[item.role, { 'is-summary': item.type === 'summary' }]">
                  <div
                    class="msg-avatar"
                    :class="{ actionable: item.role !== 'user' }"
                    :title="item.role === 'user' ? '' : t('team.avatarMenuHint')"
                    @contextmenu.prevent.stop="item.role !== 'user' && openAvatarMenu($event, item)"
                  >
                    {{ item.role === 'user' ? 'LD' : monogram(item.role_name) }}
                  </div>
                  <div class="msg-content">
                    <div class="msg-label">
                      <span>{{ item.role === 'user' ? t('team.roleLead') : item.role_name }}</span>
                      <span v-if="item.role === 'user' && (item.target_roles || []).length" class="msg-target">
                        → {{ (item.target_roles || []).length === members.length
                          ? t('team.targetEveryone')
                          : (item.target_roles || []).map(role => `@${role}`).join(', ') }}
                      </span>
                      <time :title="formatFullTime(item.timestamp)">{{ formatTime(item.timestamp) }}</time>
                    </div>
                    <div v-if="String(item.content || '').trim()" class="msg-text">
                      <MarkdownText :text="String(item.content || '')" />
                    </div>
                    <div v-if="(item.attachments || []).length" class="msg-attachment-list">
                      <span v-for="(att, ai) in item.attachments" :key="`${item.id}-att-${ai}`" class="msg-attachment-row">
                        <span>📎</span>
                        <strong>{{ att.name || 'attachment' }}</strong>
                        <small>{{ formatBytes(att.size || 0) }}</small>
                      </span>
                    </div>
                    <div v-if="item.blockGroups && (item.blockGroups.primary.length || item.blockGroups.toolResults.length)" class="msg-blocks">
                      <AssistantCard
                        v-for="(block, bi) in item.blockGroups.primary"
                        :key="block.id || `p-${bi}`"
                        :block="block"
                        :actions-disabled="true"
                      />
                      <details
                        v-if="item.blockGroups.foldTools"
                        class="tool-calls tool-calls-panel"
                      >
                        <summary>
                          {{ t('assistant.toolCallsCount', { count: item.blockGroups.toolResults.length }) }}
                        </summary>
                        <div class="tool-call-list tool-call-list-stack">
                          <AssistantCard
                            v-for="(block, bi) in item.blockGroups.toolResults"
                            :key="block.id || `t-${bi}`"
                            :block="block"
                            :actions-disabled="true"
                          />
                        </div>
                      </details>
                      <template v-else>
                        <AssistantCard
                          v-for="(block, bi) in item.blockGroups.toolResults"
                          :key="block.id || `t-${bi}`"
                          :block="block"
                          :actions-disabled="true"
                        />
                      </template>
                    </div>
                    <div v-if="(item.tool_names || []).length && !(item.blockGroups && item.blockGroups.toolResults.length)" class="msg-tool-line">
                      <span>{{ t('team.usedTools') }}</span>
                      <code>{{ item.tool_names.join(', ') }}</code>
                    </div>
                  </div>
                </div>
              </template>

              <div v-if="runningTeamMembers.length" class="group-typing-row">
                <span class="group-typing-avatars">
                  <span
                    v-for="m in runningTeamMembers.slice(0, 3)"
                    :key="m.agent_id"
                    class="group-typing-avatar"
                  >{{ monogram(m.role) }}</span>
                </span>
                <span class="group-typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>
                <span>{{ t('team.membersWorking', { n: runningTeamMembers.length }) }}</span>
              </div>
              </div>
            </div>
            <button
              v-if="!chatFollowScroll && visibleTeamChat.length"
              type="button"
              class="team-jump-bottom"
              @click="jumpTeamChatToBottom"
            >{{ t('team.jumpToBottom') }}</button>

            <!-- Pending confirmation only when filtering that member -->
            <div v-if="activeMemberId && pendingTools.length" class="team-pending-bar">
              <div class="team-pending-copy">
                <span class="pill warn">{{ t('team.statusAttention') }}</span>
                <strong>{{ pendingTools[0].name }}</strong>
                <span class="mono team-pending-args">
                  {{ JSON.stringify(pendingTools[0].arguments || {}) }}
                </span>
              </div>
              <div class="row-actions">
                <button type="button" class="ghost danger sm" :disabled="decisionBusy" @click="decideMember('deny')">
                  {{ t('team.deny') }}
                </button>
                <button type="button" class="primary sm" :disabled="decisionBusy" @click="decideMember('allow')">
                  {{ t('team.allow') }}
                </button>
              </div>
            </div>

            <!-- Group composer with explicit recipients -->
            <footer class="team-composer">
              <div class="team-input-wrap">
                <div v-if="mentionOpen" class="mention-menu">
                  <div class="mention-menu-head">
                    <strong>{{ t('team.mentionMembers') }}</strong>
                    <span>{{ t('team.fuzzyMatchHint') }}</span>
                  </div>
                  <button
                    v-for="(candidate, index) in mentionCandidates"
                    :key="candidate.agent_id"
                    type="button"
                    class="mention-option"
                    :class="{ active: index === mentionIndex }"
                    @mousedown.prevent="pickMention(candidate)"
                  >
                    <span class="mention-avatar" :class="candidate.agent_id === 'all' ? 'all' : memberStatusKind(candidate)">
                      {{ candidate.agent_id === 'all' ? '@' : monogram(candidate.role) }}
                    </span>
                    <span class="mention-copy">
                      <strong>{{ candidate.agent_id === 'all' ? t('team.targetEveryone') : `@${candidate.role}` }}</strong>
                      <small v-if="candidate.agent_id === 'all'">{{ t('team.mentionEveryoneHint') }}</small>
                      <small v-else>{{ memberStatusLabel(candidate) }}</small>
                    </span>
                  </button>
                  <div v-if="!mentionCandidates.length" class="mention-empty">{{ t('common.noMatch') }}</div>
                </div>
                <input
                  ref="fileInputEl"
                  class="team-file-input"
                  type="file"
                  multiple
                  @change="onAttachmentFiles"
                />
                <div v-if="pendingAttachments.length" class="team-attachment-strip">
                  <span v-for="att in pendingAttachments" :key="att.id" class="team-attachment-chip">
                    <span class="team-attachment-icon">📎</span>
                    <span class="team-attachment-name" :title="att.name">{{ att.name }}</span>
                    <small>{{ formatBytes(att.size) }}</small>
                    <button type="button" :title="t('common.delete')" @click="removeAttachment(att.id)">×</button>
                  </span>
                </div>
                <textarea
                  ref="composerEl"
                  v-model="steerDraft"
                  rows="2"
                  :placeholder="activeMemberId
                    ? t('team.privateComposerPlaceholder', { role: activeFilterMember?.role || 'member' })
                    : t('team.groupComposerPlaceholder')"
                  @input="updateMentionState"
                  @click="updateMentionState"
                  @keydown="onComposerKeydown"
                  @paste="onComposerPaste"
                ></textarea>
                <div class="team-input-bar">
                  <div class="team-composer-target">
                    <span>{{ t('team.sendTo') }}</span>
                    <ListSelect
                      v-if="!activeMemberId"
                      :model-value="chatTargetId"
                      :options="chatTargetOptions"
                      min-width="154px"
                      prefer-up
                      @update:model-value="chatTargetId = $event"
                    />
                    <span v-else class="private-target-pill">
                      @{{ activeFilterMember?.role || 'member' }} · {{ t('team.private') }}
                    </span>
                    <small>
                      {{ activeMemberId
                        ? t('team.privateOnlyHint')
                        : chatTargetId === 'all'
                        ? t('team.broadcastHint')
                        : t('team.directHint', { role: chatTargetMember?.role || 'member' }) }}
                    </small>
                  </div>
                  <div class="team-composer-controls">
                    <button
                      type="button"
                      class="composer-mini"
                      :class="{ active: pendingAttachments.length }"
                      :title="t('team.attachFiles')"
                      @click="fileInputEl?.click()"
                    >
                      <span>＋</span>
                      {{ pendingAttachments.length ? pendingAttachments.length : t('team.attach') }}
                    </button>
                    <button
                      type="button"
                      class="composer-mini"
                      :class="{ active: teamGreenlight }"
                      :disabled="greenlightBusy"
                      :title="teamGreenlight ? t('team.greenlightDisableTitle') : t('team.greenlightEnableTitle')"
                      @click="toggleTeamGreenlight"
                    >
                      {{ t('assistant.authorization') }}
                    </button>
                    <button
                      type="button"
                      class="composer-mini"
                      :class="{ active: teamWebSearch }"
                      @click="teamWebSearch = !teamWebSearch"
                    >
                      {{ t('team.webSearch') }}
                    </button>
                    <button type="button" class="composer-mini" @click="openTokenStats">
                      {{ t('team.tokens') }}
                    </button>
                  </div>
                  <button
                    type="button"
                    class="primary sm"
                    :disabled="(!steerDraft.trim() && !pendingAttachments.length) || sendingSteer"
                    @click="sendSteer"
                  >{{ t('team.send') }}</button>
                </div>
              </div>
            </footer>
          </template>

          <PageEmpty
            v-else
            :title="t('team.noMembers')"
            :description="t('team.emptyHint')"
            :action-label="t('team.addMember')"
            @action="openAddMemberModal(activeTeamId)"
          />
        </section>
      </div>
    </template>

    <!-- ===================== Team / member token statistics ===================== -->
    <WorkbenchModal
      :open="showTokenStats"
      :title="t('team.tokenStats')"
      :description="t('team.tokenStatsDesc')"
      size="md"
      @close="showTokenStats = false"
    >
      <div v-if="tokenStatsLoading" class="team-sandbox-state">{{ t('common.loading') }}</div>
      <template v-else-if="tokenStats">
        <div class="team-token-total">
          <div><span>{{ t('team.totalTokens') }}</span><strong>{{ formatTokens((tokenStats.total?.input_tokens || 0) + (tokenStats.total?.output_tokens || 0)) }}</strong></div>
          <div><span>{{ t('team.inputTokens') }}</span><strong>{{ formatTokens(tokenStats.total?.input_tokens || 0) }}</strong></div>
          <div><span>{{ t('team.outputTokens') }}</span><strong>{{ formatTokens(tokenStats.total?.output_tokens || 0) }}</strong></div>
          <div><span>{{ t('team.cacheHit') }}</span><strong>{{ tokenStats.cache_hit_rate == null ? '—' : `${tokenStats.cache_hit_rate}%` }}</strong></div>
        </div>
        <div class="team-token-list">
          <div v-for="row in tokenStats.members || []" :key="row.agent_id" class="team-token-row">
            <span class="member-avatar"><span>{{ monogram(row.role) }}</span></span>
            <span class="team-token-copy">
              <strong>{{ row.role }}</strong>
              <small>{{ row.model || t('team.modelDefault') }}</small>
            </span>
            <span><small>{{ t('team.inputTokens') }}</small><b>{{ formatTokens(row.usage?.input_tokens || 0) }}</b></span>
            <span><small>{{ t('team.outputTokens') }}</small><b>{{ formatTokens(row.usage?.output_tokens || 0) }}</b></span>
            <span><small>{{ t('team.cacheHit') }}</small><b>{{ row.cache_hit_rate == null ? '—' : `${row.cache_hit_rate}%` }}</b></span>
          </div>
        </div>
      </template>
    </WorkbenchModal>

    <!-- ===================== Team Sandboxes ===================== -->
    <WorkbenchModal
      :open="showTeamSandboxes"
      :title="t('team.teamSandbox')"
      :description="t('team.teamSandboxDesc')"
      size="md"
      @close="showTeamSandboxes = false"
    >
      <div v-if="teamSandboxesLoading" class="team-sandbox-state">{{ t('common.loading') }}</div>
      <div v-else-if="!teamSandboxes.length" class="team-sandbox-state">{{ t('team.noSandboxes') }}</div>
      <div v-else class="team-sandbox-list">
        <div v-for="row in teamSandboxes" :key="row.agent_id" class="team-sandbox-row">
          <span class="member-avatar">
            <span>{{ monogram(row.role) }}</span>
          </span>
          <span class="team-sandbox-copy">
            <strong>{{ row.kind === 'team' ? t('team.sharedSandbox') : row.role }}</strong>
            <small v-if="row.exists">
              {{ t('team.sandboxStats', { files: row.file_count || 0, size: formatBytes(row.total_size || row.size || 0) }) }}
            </small>
            <small v-else>{{ t('team.sandboxEmpty') }}</small>
          </span>
          <span class="mono team-sandbox-id">{{ row.session_id }}</span>
          <button type="button" class="ghost sm" @click="browseMemberSandbox(row)">
            {{ t('team.browseSandbox') }}
          </button>
        </div>
      </div>
    </WorkbenchModal>

    <!-- ===================== Create Team Modal ===================== -->
    <WorkbenchModal
      :open="showCreateTeam"
      :title="t('team.create')"
      :description="t('team.createModalDesc')"
      size="md"
      :busy="creatingTeam"
      @close="showCreateTeam = false"
    >
      <div class="form">
        <div class="field">
          <label>{{ t('team.presetLabel') }}</label>
          <div class="preset-grid">
            <button
              v-for="p in presetOptions"
              :key="p.value"
              type="button"
              class="preset-card"
              :class="{ active: newTeamPreset === p.value }"
              @click="selectPreset(p)"
            >
              <strong>{{ p.label }}</strong>
              <small class="mono">{{ p.desc }}</small>
            </button>
          </div>
        </div>
        <div class="field">
          <label>{{ t('team.name') }}</label>
          <input
            v-model="newTeamName"
            type="text"
            autofocus
            :placeholder="t('team.namePlaceholder')"
            @keydown.enter.prevent="createTeam"
          />
        </div>
        <div class="field">
          <label>{{ t('team.objective') }}</label>
          <textarea
            v-model="newTeamObjective"
            rows="3"
            :placeholder="t('team.objectivePlaceholder')"
          ></textarea>
          <div v-if="newTeamPreset" class="hint">{{ t('team.presetObjectiveHint') }}</div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="ghost" :disabled="creatingTeam" @click="showCreateTeam = false">
          {{ t('common.cancel') }}
        </button>
        <button type="button" class="primary" :disabled="creatingTeam || !newTeamName.trim()" @click="createTeam">
          {{ t('team.create') }}
        </button>
      </template>
    </WorkbenchModal>

    <!-- ===================== Add Member Modal ===================== -->
    <WorkbenchModal
      :open="showAddMember"
      :title="editingMemberId ? t('team.editMember') : t('team.addMember')"
      :description="editingMemberId ? t('team.editMemberModalDesc') : t('team.addMemberModalDesc')"
      size="md"
      :busy="addingMember"
      @close="showAddMember = false"
    >
      <div class="form">
        <div class="field">
          <label>{{ t('team.memberRole') }}</label>
          <input
            v-model="memberRole"
            type="text"
            autofocus
            :placeholder="t('team.memberRolePlaceholder')"
          />
          <div class="checks" style="margin-top: 6px">
            <span
              v-for="r in roleQuickChips"
              :key="r"
              class="chip"
              :class="{ on: memberRole === r }"
              @click="memberRole = r"
            >{{ r }}</span>
          </div>
        </div>
        <div class="field">
          <label>{{ t('team.memberObjective') }}</label>
          <textarea
            v-model="memberObjective"
            rows="3"
            :placeholder="t('team.memberObjectivePlaceholder')"
          ></textarea>
        </div>
        <div class="field">
          <label>{{ t('team.memberModel') }}</label>
          <button type="button" class="model-picker-trigger" @click="openMemberModelPicker">
            <span>
              <strong>{{ selectedMemberModelLabel }}</strong>
              <small>{{ memberModel || t('team.modelDefault') }}</small>
            </span>
            <span class="model-picker-chevron">›</span>
          </button>
          <div class="hint">
            {{ modelsLoading ? t('common.loading') : t('team.memberModelHint') }}
          </div>
        </div>
        <div class="field">
          <label>{{ t('team.memberRuntimePolicy') }}</label>
          <div class="member-policy-row">
            <button
              type="button"
              class="policy-toggle"
              :class="{ active: memberApprovalMode === 'leader_auto' }"
              @click="memberApprovalMode = memberApprovalMode === 'leader_auto' ? 'manual' : 'leader_auto'"
            >
              <span class="switch-mini" :class="{ on: memberApprovalMode === 'leader_auto' }"><i></i></span>
              {{ t('team.leaderAutoApprove') }}
            </button>
            <button
              type="button"
              class="policy-toggle"
              :class="{ active: memberWebSearch }"
              @click="memberWebSearch = !memberWebSearch"
            >
              <span class="switch-mini" :class="{ on: memberWebSearch }"><i></i></span>
              {{ t('team.webSearch') }}
            </button>
          </div>
        </div>
        <div class="field">
          <label>{{ t('team.permission') }}</label>
          <div class="perm-seg">
            <button
              type="button"
              class="perm-card"
              :class="{ active: memberPermission === 'readonly' }"
              @click="memberPermission = 'readonly'"
            >
              <span class="perm-card-top">
                <span class="perm-dot readonly" aria-hidden="true"></span>
                <strong>{{ t('team.permReadonlyShort') }}</strong>
              </span>
              <small>{{ t('team.permReadonlyDesc') }}</small>
            </button>
            <button
              type="button"
              class="perm-card"
              :class="{ active: memberPermission === 'scoped' }"
              @click="memberPermission = 'scoped'"
            >
              <span class="perm-card-top">
                <span class="perm-dot scoped" aria-hidden="true"></span>
                <strong>{{ t('team.permScopedShort') }}</strong>
              </span>
              <small>{{ t('team.permScopedDesc') }}</small>
            </button>
          </div>
          <div class="hint perm-hint">{{ t('team.leaderAutoApproveHint') }}</div>
        </div>

        <div v-if="memberPermission === 'scoped'" class="field">
          <div class="tool-picker-head">
            <label style="margin:0">{{ t('team.requestedTools') }}</label>
            <span class="tool-picker-count">{{ t('team.toolsSelected', { n: selectedTools.size }) }}</span>
          </div>
          <div class="tool-picker-bar">
            <input
              v-model="toolSearch"
              type="search"
              class="tool-picker-search"
              :placeholder="t('team.toolSearchPlaceholder')"
            />
            <button
              type="button"
              class="ghost sm"
              :disabled="!(ROLE_TOOL_SUGGESTIONS[memberRole.trim()] || []).length"
              :title="t('team.suggestByRoleHint')"
              @click="applyRoleSuggestion"
            >{{ t('team.suggestByRole') }}</button>
          </div>
          <div class="tool-picker-filters">
            <span
              v-for="opt in toolRiskFilterOptions"
              :key="opt.value"
              class="tool-risk-chip"
              :class="{ on: toolRiskFilter === opt.value, [opt.value]: true }"
              @click="toolRiskFilter = opt.value"
            >{{ opt.label }}</span>
            <span class="tool-picker-spacer"></span>
            <button type="button" class="ghost sm" :disabled="!filteredToolCatalog.length" @click="toggleSelectAllFiltered">
              {{ allFilteredSelected ? t('team.deselectAll') : t('team.selectAll') }}
            </button>
            <button type="button" class="ghost sm" :disabled="!selectedTools.size" @click="clearSelectedTools">
              {{ t('team.clearSelected') }}
            </button>
          </div>
          <div class="tool-picker">
            <div v-if="toolsLoading" class="tool-picker-state">{{ t('assistant.toolsLoading') }}</div>
            <div v-else-if="!filteredToolCatalog.length" class="tool-picker-state">{{ t('common.noMatch') }}</div>
            <button
              v-for="tl in filteredToolCatalog"
              :key="tl.name"
              type="button"
              class="tool-pick-row"
              :class="{ on: isToolSelected(tl.name) }"
              @click="toggleTool(tl.name)"
            >
              <span class="tool-pick-check" :class="{ on: isToolSelected(tl.name) }" aria-hidden="true">
                <svg v-if="isToolSelected(tl.name)" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8.5l3.2 3.2L13 5"/></svg>
              </span>
              <span class="tool-pick-copy">
                <span class="tool-pick-name-row">
                  <code>{{ tl.name }}</code>
                  <span class="tool-pick-risk" :class="riskClass(tl.risk)">{{ riskLabel(tl.risk) }}</span>
                </span>
                <span class="tool-pick-desc">{{ tl.description }}</span>
              </span>
            </button>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="ghost" :disabled="addingMember" @click="showAddMember = false">
          {{ t('common.cancel') }}
        </button>
        <button
          type="button"
          class="primary"
          :disabled="addingMember || !memberRole.trim() || !memberObjective.trim()"
          @click="addMember"
        >{{ editingMemberId ? t('common.save') : t('team.addMember') }}</button>
      </template>
    </WorkbenchModal>

    <TeamModelPicker
      :open="showMemberModelPicker"
      :models="modelCatalog"
      :current="memberModel"
      :loading="modelsLoading"
      @close="showMemberModelPicker = false"
      @select="selectMemberModel"
      @refresh="loadModelCatalog(true)"
    />

    <div
      v-if="avatarMenu"
      class="avatar-menu"
      :style="{ left: `${avatarMenu.x}px`, top: `${avatarMenu.y}px` }"
      @pointerdown.stop
    >
      <button type="button" @click="mentionFromMenu">@{{ avatarMenu.role }}</button>
      <button type="button" @click="privateChatFromMenu">{{ t('team.avatarPrivate') }}</button>
      <button type="button" @click="directTargetFromMenu">{{ t('team.avatarDirect') }}</button>
    </div>
  </div>
</template>

<style scoped>
.team-page {
  gap: 12px;
  padding: 18px 20px;
}

.tone-accent { color: var(--accent) !important; }
.tone-ok { color: var(--ok) !important; }
.tone-warn { color: var(--warn) !important; }

.inline-live-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 5px;
  border-radius: 50%;
  background: var(--ok);
  animation: team-pulse 1.2s ease-in-out infinite;
}

.team-state-box {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 13px;
}

/* ── Grid of Team Cards ─────────────────────────────────────────── */
.team-grid-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
}

.team-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
  align-content: start;
}

.team-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--card-bg);
  cursor: pointer;
  transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease;
}
.team-card:hover {
  border-color: var(--border-strong);
  background: color-mix(in srgb, var(--panel-2) 55%, var(--card-bg));
}
.team-card.is-running {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
}
.team-card.is-attention {
  border-color: color-mix(in srgb, var(--warn) 42%, var(--border));
}
.team-card.is-failed {
  border-color: color-mix(in srgb, var(--err) 38%, var(--border));
}

.team-card-head {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.team-badge-icon {
  position: relative;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--muted);
}
.team-badge-icon.is-running {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 32%, var(--border));
}
.team-badge-icon.is-attention {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  border-color: color-mix(in srgb, var(--warn) 34%, var(--border));
}
.team-badge-icon.is-failed {
  color: var(--err);
  background: color-mix(in srgb, var(--err) 12%, transparent);
  border-color: color-mix(in srgb, var(--err) 34%, var(--border));
}
.team-badge-ring {
  position: absolute;
  inset: -3px;
  border-radius: 12px;
  border: 1.5px solid currentColor;
  opacity: 0.45;
  animation: team-ring-pulse 1.6s ease-out infinite;
  pointer-events: none;
}

.team-card-title-wrap {
  flex: 1 1 auto;
  min-width: 0;
}
.team-card-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.team-card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--fg-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
  color: var(--muted-2);
  font-size: 11px;
}

.team-card-head-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}
.pill-live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: team-pulse 1.1s ease-in-out infinite;
}
.team-card-del {
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 7px;
  opacity: 0.6;
}
.team-card-del:hover {
  opacity: 1;
  color: var(--err);
}

.team-card-objective {
  margin: 0;
  color: var(--fg);
  font-size: 12px;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 36px;
}
.team-card-objective.muted {
  color: var(--muted-2);
}

/* Live roster box inside each card */
.team-roster-box {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
}
.team-roster-row {
  position: relative;
  display: grid;
  grid-template-columns: 8px auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 7px;
  padding: 4px 6px;
  border-radius: 6px;
  font-size: 11.5px;
}
.team-roster-row.is-running {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.roster-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted-2);
}
.roster-dot.running {
  background: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
  animation: team-pulse 1.1s ease-in-out infinite;
}
.roster-dot.attention,
.roster-dot.paused {
  background: var(--warn);
}
.roster-dot.done {
  background: var(--ok);
}
.roster-dot.failed {
  background: var(--err);
}
.roster-role {
  color: var(--fg-strong);
  font-weight: 600;
  white-space: nowrap;
}
.roster-activity {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.roster-tools {
  color: var(--muted-2);
  font-size: 10px;
}
.roster-state {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--muted);
  white-space: nowrap;
}
.roster-state.running { color: var(--accent); }
.roster-state.attention, .roster-state.paused { color: var(--warn); }
.roster-state.done { color: var(--ok); }
.roster-state.failed { color: var(--err); }

.roster-progress-bar {
  grid-column: 1 / -1;
  height: 2px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  overflow: hidden;
}
.roster-progress-bar > i {
  display: block;
  width: 38%;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: team-indet 1.35s ease-in-out infinite;
}

.team-roster-more {
  padding: 2px 6px 0;
  color: var(--muted-2);
  font-size: 10.5px;
}
.team-roster-empty {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 4px;
  color: var(--muted-2);
  font-size: 11.5px;
}
.team-roster-empty button.sm {
  padding: 3px 9px;
  font-size: 11px;
}

.team-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 2px;
  color: var(--muted-2);
  font-size: 11px;
}
.team-enter-link {
  color: var(--muted);
  font-weight: 500;
  transition: color 120ms ease;
}
.team-card:hover .team-enter-link {
  color: var(--accent);
}

/* ── Team Detail Workbench (Split Rail + Chat) ───────────────────── */
.team-detail-head-left {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}
.team-detail-title-block {
  min-width: 0;
}
.team-detail-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.team-workbench-shell {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(250px, 300px) minmax(0, 1fr);
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card-bg);
  overflow: hidden;
}

.team-member-rail {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: color-mix(in srgb, var(--card-bg) 94%, var(--page-bg));
}
.team-rail-search {
  padding: 10px;
  border-bottom: 1px solid var(--border);
}
.team-rail-search input {
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}
.team-group-entry {
  width: auto;
  margin: 8px 8px 4px;
  padding: 9px 10px;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--fg);
  text-align: left;
}
.team-group-entry:hover {
  background: var(--panel-hover);
}
.team-group-entry.active {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  background: color-mix(in srgb, var(--accent) 9%, var(--card-bg));
  box-shadow: inset 3px 0 0 var(--accent);
}
.team-group-mark {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 10px;
  background: var(--accent-soft);
  color: var(--accent);
}
.team-group-mark.large {
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
}
.team-group-entry-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.team-group-entry-copy strong {
  color: var(--fg-strong);
  font-size: 12.5px;
}
.team-group-entry-copy small {
  color: var(--muted);
  font-size: 10.5px;
}
.group-live-count {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: var(--accent);
  color: #fff;
  font: 700 10px/1 var(--mono);
}
.team-rail-empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 20px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.team-member-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.team-member-item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 120ms ease;
}
.team-member-item:hover {
  background: var(--panel-hover);
}
.team-member-item.active {
  background: color-mix(in srgb, var(--accent) 9%, var(--card-bg));
  box-shadow: inset 3px 0 0 var(--accent);
}

.member-avatar {
  position: relative;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--fg-strong);
  font: 700 11px/1 var(--mono);
}
.member-avatar.is-running {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  background: var(--accent-soft);
}
.member-avatar.is-attention {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 40%, var(--border));
}
.member-avatar-dot {
  position: absolute;
  right: -2px;
  bottom: -2px;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 2px solid var(--card-bg);
  background: var(--muted-2);
}
.member-avatar-dot.running {
  background: var(--accent);
  animation: team-pulse 1.1s ease-in-out infinite;
}
.member-avatar-dot.attention,
.member-avatar-dot.paused {
  background: var(--warn);
}
.member-avatar-dot.done {
  background: var(--ok);
}
.member-avatar-dot.failed {
  background: var(--err);
}
.msg-avatar.actionable,
.member-avatar {
  cursor: context-menu;
}
.stop-all-btn {
  margin-left: 4px;
  text-transform: none;
  letter-spacing: 0;
}
.avatar-menu {
  position: fixed;
  z-index: 80;
  min-width: 148px;
  display: flex;
  flex-direction: column;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--panel);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
}
.avatar-menu button {
  border: 0;
  background: transparent;
  color: var(--fg);
  text-align: left;
  font-size: 12px;
  padding: 7px 10px;
  border-radius: 7px;
  cursor: pointer;
}
.avatar-menu button:hover {
  background: var(--panel-hover);
}

.member-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.member-top-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.member-top-line strong {
  font-size: 12.5px;
  color: var(--fg-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.member-mini-pill {
  min-height: 18px;
  padding: 1px 6px;
  font-size: 10px;
}
.member-sub-line {
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Right chat column */
.team-chat-pane {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--card-bg);
}
.team-chat-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}
.team-chat-identity {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}
.team-chat-title-copy {
  min-width: 0;
}
.team-chat-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.team-chat-title-row strong {
  font-size: 14px;
  color: var(--fg-strong);
}
.team-chat-objective {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-chat-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

.team-chat-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px;
}
.team-chat-stream {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 100%;
}
.team-jump-bottom {
  position: absolute;
  left: 50%;
  bottom: 108px;
  transform: translateX(-50%);
  z-index: 4;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--panel);
  color: var(--fg);
  font-size: 12px;
  padding: 6px 12px;
  cursor: pointer;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
}
.team-jump-bottom:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--accent);
}
.team-chat-empty {
  margin: auto;
  color: var(--muted);
  font-size: 12.5px;
  display: flex;
  max-width: 340px;
  flex-direction: column;
  align-items: center;
  gap: 7px;
  text-align: center;
}
.team-chat-empty strong {
  color: var(--fg-strong);
  font-size: 13px;
}
.team-chat-empty p {
  margin: 0;
  line-height: 1.55;
}
.team-empty-chat-icon {
  width: 46px;
  height: 46px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 14px;
  background: var(--panel-2);
  color: var(--muted-2);
}

/* Match AssistantPanel message styling */
.msg {
  max-width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 9px;
}
.msg.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}
.msg-avatar {
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  font: 700 10.5px/1 var(--mono);
  background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 82%, #fff), var(--accent-2));
  color: #fff;
}
.msg.user .msg-avatar {
  background: var(--panel-2);
  color: var(--fg-strong);
  border: 1px solid var(--border);
}
.msg-content {
  flex: 1 1 auto;
  min-width: 0;
  max-width: min(820px, 88%);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}
.msg.user .msg-content {
  align-items: flex-end;
}
.msg-label {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--muted-2);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.msg-label time {
  margin-left: auto;
  color: var(--muted-2);
  font-size: 9.5px;
  font-variant-numeric: tabular-nums;
  text-transform: none;
  letter-spacing: 0;
}
.msg-target {
  color: var(--accent);
  text-transform: none;
  letter-spacing: 0;
}
.msg-text {
  min-width: 0;
  max-width: 100%;
  padding: 10px 14px;
  border-radius: 14px;
  font-size: 13px;
  line-height: 1.55;
  background: var(--panel-2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-bottom-left-radius: 5px;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.msg.user .msg-text {
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  border: 0;
  border-bottom-left-radius: 14px;
  border-bottom-right-radius: 5px;
}
.msg.is-summary .msg-text {
  border-color: color-mix(in srgb, var(--ok) 30%, var(--border));
  background: color-mix(in srgb, var(--ok) 7%, var(--panel-2));
}
.msg-blocks {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tool-calls {
  align-self: flex-start;
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
.tool-call-list-stack {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}
.tool-calls-panel .tool-call-list-stack :deep(.assistant-card.collapsed) {
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
}
.msg-attachment-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.msg-attachment-row {
  max-width: 260px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  font-size: 10.5px;
}
.msg-attachment-row strong {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.msg-attachment-row small {
  color: var(--muted-2);
}

.msg-tool-line {
  display: flex;
  max-width: 100%;
  align-items: center;
  gap: 6px;
  color: var(--muted-2);
  font-size: 10.5px;
}
.msg-tool-line code {
  overflow: hidden;
  color: var(--muted);
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-activity-row {
  align-self: flex-start;
  max-width: min(760px, 88%);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
}
.group-activity-avatar {
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font: 700 8px/1 var(--mono);
}
.group-activity-copy {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-activity-copy strong {
  color: var(--fg);
}
.group-activity-copy code {
  color: var(--accent);
  font-size: 10.5px;
}
.group-activity-row time {
  margin-left: auto;
  flex: 0 0 auto;
  color: var(--muted-2);
  font-size: 9.5px;
}

.group-typing-row {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
}
.group-typing-avatars {
  display: inline-flex;
  padding-left: 5px;
}
.group-typing-avatar {
  width: 22px;
  height: 22px;
  margin-left: -5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 2px solid var(--panel-2);
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font: 700 8px/1 var(--mono);
}
.group-typing-dots {
  display: inline-flex;
  gap: 3px;
}
.group-typing-dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent);
  animation: team-typing 1.2s ease-in-out infinite;
}
.group-typing-dots i:nth-child(2) { animation-delay: .15s; }
.group-typing-dots i:nth-child(3) { animation-delay: .3s; }

.team-live-turn-bar {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 7px 12px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
}
.stream-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: team-pulse 1.1s ease-in-out infinite;
}

.team-summary-card {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--ok) 30%, var(--border));
  background: color-mix(in srgb, var(--ok) 7%, var(--panel-2));
}
.team-summary-head {
  margin-bottom: 6px;
}
.team-summary-body {
  font-size: 12.5px;
  line-height: 1.55;
}

.team-pending-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid color-mix(in srgb, var(--warn) 32%, var(--border));
  background: color-mix(in srgb, var(--warn) 10%, var(--panel-2));
}
.team-pending-copy {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.team-pending-args {
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.team-composer {
  flex: 0 0 auto;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--chrome);
}
.team-input-wrap {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--input-bg);
  transition: border-color 150ms, box-shadow 150ms;
}
.team-input-wrap:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.team-input-wrap textarea,
.team-input-wrap textarea:hover,
.team-input-wrap textarea:focus,
.team-input-wrap textarea:focus-visible {
  border: 0 !important;
  outline: none !important;
  padding: 2px 4px;
  min-height: 38px;
  max-height: 110px;
  resize: none;
  background: transparent !important;
  box-shadow: none !important;
  font-family: var(--ui);
  font-size: 13px;
}
.team-file-input {
  display: none;
}
.team-attachment-strip {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 2px;
}
.team-attachment-chip {
  max-width: 240px;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 5px 3px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  font-size: 11px;
}
.team-attachment-name {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-attachment-chip small {
  flex: 0 0 auto;
  color: var(--muted-2);
  font-size: 9.5px;
}
.team-attachment-chip button {
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--muted);
}
.mention-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  z-index: 40;
  width: min(340px, calc(100vw - 80px));
  max-height: 280px;
  overflow-y: auto;
  padding: 5px;
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 11px;
  background: var(--card-bg);
  box-shadow: var(--shadow-md);
}
.mention-menu-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 8px 8px;
  color: var(--muted);
  font-size: 10.5px;
}
.mention-menu-head strong {
  color: var(--fg-strong);
  font-size: 11px;
}
.mention-option {
  width: 100%;
  min-height: 42px;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  align-items: center;
  gap: 9px;
  padding: 6px 8px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  text-align: left;
}
.mention-option:hover,
.mention-option.active {
  background: var(--accent-soft);
}
.mention-avatar {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
  font: 700 9px/1 var(--mono);
}
.mention-avatar.running,
.mention-avatar.all {
  background: var(--accent-soft);
  color: var(--accent);
}
.mention-avatar.attention,
.mention-avatar.paused {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
}
.mention-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.mention-copy strong {
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mention-copy small {
  color: var(--muted);
  font-size: 10.5px;
}
.mention-empty {
  padding: 20px;
  color: var(--muted);
  font-size: 11px;
  text-align: center;
}
.team-input-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.team-composer-controls {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.composer-mini {
  min-height: 28px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  font-size: 10.5px;
}
.composer-mini:hover {
  border-color: var(--border);
}
.composer-mini.active {
  border-color: color-mix(in srgb, var(--accent) 28%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
}
.team-composer-target {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 11px;
}
.team-composer-target > span {
  flex: 0 0 auto;
  font-weight: 600;
}
.private-target-pill {
  min-height: 28px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
}
.team-composer-target small {
  min-width: 0;
  overflow: hidden;
  color: var(--muted-2);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-composer-target :deep(.list-dd-trigger) {
  height: 28px;
  min-width: 154px;
  font-size: 11px;
}
.team-input-hint {
  color: var(--muted-2);
  font-size: 11px;
}

button.sm {
  padding: 5px 10px;
  font-size: 12px;
  border-radius: 8px;
}

/* Team sandbox selector */
.team-sandbox-state {
  padding: 48px 18px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.team-sandbox-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.team-sandbox-row {
  display: grid;
  grid-template-columns: 34px minmax(120px, 1fr) minmax(120px, auto) auto;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
}
.team-sandbox-row:first-child { border-top: 0; }
.team-sandbox-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.team-sandbox-copy strong {
  color: var(--fg-strong);
  font-size: 12.5px;
}
.team-sandbox-copy small {
  color: var(--muted);
  font-size: 10.5px;
}
.team-sandbox-id {
  overflow: hidden;
  color: var(--muted-2);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-token-total {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.team-token-total > div {
  padding: 10px 12px;
  border-left: 1px solid var(--border);
}
.team-token-total > div:first-child { border-left: 0; }
.team-token-total span,
.team-token-row small {
  display: block;
  color: var(--muted);
  font-size: 10px;
}
.team-token-total strong {
  display: block;
  margin-top: 3px;
  color: var(--fg-strong);
  font-size: 15px;
}
.team-token-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.team-token-row {
  display: grid;
  grid-template-columns: 34px minmax(120px, 1fr) repeat(3, minmax(70px, auto));
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
  border-top: 1px solid var(--border);
}
.team-token-row:first-child { border-top: 0; }
.team-token-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.team-token-copy strong { color: var(--fg-strong); font-size: 12px; }
.team-token-row b { display: block; margin-top: 2px; font-size: 11px; }

/* Modal presets */
.preset-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.preset-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel-2);
  text-align: left;
  white-space: normal;
}
.preset-card strong {
  font-size: 12.5px;
  color: var(--fg-strong);
}
.preset-card small {
  font-size: 10.5px;
  color: var(--muted);
}
.preset-card.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.model-picker-trigger {
  width: 100%;
  min-height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--input-bg);
  text-align: left;
}
.model-picker-trigger:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.model-picker-trigger > span:first-child {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.model-picker-trigger strong,
.model-picker-trigger small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model-picker-trigger strong {
  color: var(--fg-strong);
  font-size: 12px;
}
.model-picker-trigger small {
  color: var(--muted-2);
  font: 10px/1.3 var(--mono);
}
.model-picker-chevron {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 20px;
}

/* Permission segmented control */
.perm-seg {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.perm-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel-2);
  text-align: left;
  white-space: normal;
}
.perm-card-top {
  display: flex;
  align-items: center;
  gap: 7px;
}
.perm-card strong { font-size: 12.5px; color: var(--fg-strong); }
.perm-card small { font-size: 10.5px; color: var(--muted); line-height: 1.45; }
.perm-card.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.perm-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}
.perm-dot.readonly { background: var(--ok); }
.perm-dot.scoped { background: var(--warn); }
.perm-hint { margin-top: 7px; }
.member-policy-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.policy-toggle {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11.5px;
}
.policy-toggle.active {
  border-color: color-mix(in srgb, var(--accent) 32%, var(--border));
  background: var(--accent-soft);
  color: var(--accent);
}
.switch-mini {
  position: relative;
  width: 26px;
  height: 14px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--fg) 18%, transparent);
}
.switch-mini i {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #fff;
  transition: left 140ms ease;
}
.switch-mini.on { background: var(--accent); }
.switch-mini.on i { left: 14px; }

/* Tool picker */
.tool-picker-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.tool-picker-count {
  font-size: 11px;
  color: var(--accent);
  font-weight: 600;
}
.tool-picker-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.tool-picker-search {
  flex: 1 1 auto;
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}
.tool-picker-filters {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.tool-picker-spacer { flex: 1 1 auto; }
.tool-risk-chip {
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
  cursor: pointer;
  user-select: none;
}
.tool-risk-chip:hover { color: var(--fg); border-color: var(--border-strong); }
.tool-risk-chip.on { color: var(--accent); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 32%, transparent); }
.tool-risk-chip.on.low { color: var(--ok); background: color-mix(in srgb, var(--ok) 11%, transparent); border-color: color-mix(in srgb, var(--ok) 32%, transparent); }
.tool-risk-chip.on.medium { color: var(--warn); background: color-mix(in srgb, var(--warn) 11%, transparent); border-color: color-mix(in srgb, var(--warn) 32%, transparent); }
.tool-risk-chip.on.high { color: var(--err); background: color-mix(in srgb, var(--err) 11%, transparent); border-color: color-mix(in srgb, var(--err) 32%, transparent); }
.tool-picker {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
  padding: 4px;
}
.tool-picker-state {
  padding: 24px;
  text-align: center;
  color: var(--muted);
  font-size: 12px;
}
.tool-pick-row {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  align-items: start;
  gap: 9px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.tool-pick-row:hover { background: var(--panel-hover); }
.tool-pick-row.on {
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  border-color: color-mix(in srgb, var(--accent) 24%, var(--border));
}
.tool-pick-check {
  width: 16px;
  height: 16px;
  margin-top: 1px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 5px;
  color: #fff;
  background: var(--input-bg);
}
.tool-pick-check.on {
  border-color: var(--accent);
  background: var(--accent);
}
.tool-pick-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.tool-pick-name-row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.tool-pick-name-row code {
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-strong);
}
.tool-pick-risk {
  font-size: 9.5px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 5px;
  color: var(--muted);
  background: var(--panel-2);
}
.tool-pick-risk.high { color: var(--err); background: color-mix(in srgb, var(--err) 11%, transparent); }
.tool-pick-risk.medium { color: var(--warn); background: color-mix(in srgb, var(--warn) 11%, transparent); }
.tool-pick-risk.low { color: var(--ok); background: color-mix(in srgb, var(--ok) 11%, transparent); }
.tool-pick-desc {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.team-tool-steps {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  align-self: flex-start;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11.5px;
}
.team-tool-steps-ico { display: inline-flex; color: var(--muted-2); }

@keyframes team-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.38; transform: scale(0.84); }
}
@keyframes team-ring-pulse {
  0% { transform: scale(0.95); opacity: 0.65; }
  100% { transform: scale(1.22); opacity: 0; }
}
@keyframes team-indet {
  0% { margin-left: -38%; }
  100% { margin-left: 100%; }
}
@keyframes team-typing {
  0%, 60%, 100% { opacity: .3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-2px); }
}
</style>
