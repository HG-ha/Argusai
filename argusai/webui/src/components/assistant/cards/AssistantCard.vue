<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../../../api.js'
import { showToast } from '../../../state.js'
import { navigate } from '../../../router.js'
import { t } from '../../../i18n.js'
import ProgressBar from '../../ProgressBar.vue'
import SeverityBar from '../../SeverityBar.vue'
import MarkdownText from '../MarkdownText.vue'
import { normalizeSelectionReference } from '../../../assistantProtocol.js'
import { useMediaPreview } from '../../../composables/useMediaPreview.js'
import { useSandboxExplain } from '../../../composables/useSandboxExplain.js'
import { compactList, compactMeta, escapeHtml, formatBytes, formatPreviewContent, highlightCode, kbActionLabel, lineAndColumnFromIndex, normalizeResourceItem, resourceExt, resourceId, resourcePath, resourceTypeLabel, tableCellValue } from './cardUtils.js'
import { fileTypeIcon, fileTypeChipColor } from '../../../fileTypeIcon.js'

const props = defineProps({
  block: { type: Object, required: true },
  actionsDisabled: { type: Boolean, default: false },
  decision: { type: String, default: '' },
  sessionId: { type: String, default: '' },
})
const emit = defineEmits(['action', 'preview-selection', 'tool-decision', 'media'])
const mediaPreview = useMediaPreview()
const { openAsk } = useSandboxExplain()
const taskSnapshot = ref(props.block.snapshot || {})
const decisionInput = ref('')
const localSubmittedResponse = ref('')
const localSelectedOption = ref('')
const batchAnswers = ref({})
const todoListRef = ref(null)
const collapsedTodoIds = ref(new Set())
const selectedResourceId = ref('')
const resourcePreview = ref({ loading: false, path: '', content: '', error: '', truncated: false, size: 0, is_base64: false })
const resourceDirPath = ref('')
const resourceDirItems = ref(null)
const resourceDirLoading = ref(false)
const resourceDirError = ref('')
const imagePreviewOpen = ref(false)
const filePreviewPaneRef = ref(null)
const filePreviewContentRef = ref(null)
const previewSelection = ref(null)
const previewSelectionAnchor = ref({ visible: false, left: 0, top: 0 })
let pollTimer = null

function isFileResourceViewExpandedByDefault(block) {
  return block?.type === 'file_resource_view'
}

const expanded = ref(isFileResourceViewExpandedByDefault(props.block))

const macosPermMissing = computed(() => {
  const perms = props.block?.macos_permissions?.permissions
  if (Array.isArray(perms) && perms.length) {
    return perms.filter((p) => p && !p.granted)
  }
  const single = props.block?.macos_permission
  if (single?.id && single.granted === false) return [single]
  return []
})

async function openMacosPrivacy(pane) {
  try {
    const r = await api.openMacosPrivacySettings(pane || 'accessibility')
    if (r?.ok) showToast(t('assistantCard.toast.macosSettingsOpened'), 'ok')
    else showToast(r?.error || t('assistantCard.error.openMacosSettingsFailed'), 'error')
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function proxyCardSummary(b) {
  const head = b.kind === 'search'
    ? (b.query || 'proxy_search_traffic')
    : b.kind === 'decode'
      ? 'proxy_decode_flow'
      : (b.kind === 'rewrite' ? t('proxy.rewrite.title') : t('proxy.decrypt.title'))
  if (b.error) return `${head} — ${b.error}`
  if (b.kind === 'search') return `${head} · ${b.matched}/${b.total}${b.timed_out ? ' ⏱' : ''}`
  if (b.kind === 'decode') return `${head} · ${(b.items || []).length}`
  const tail = b.action === 'list'
    ? `(${(b.rules || []).length})`
    : b.action === 'delete'
      ? '✕'
      : b.action === 'test'
        ? (b.matched ? '✓' : '—')
        : (b.rule?.name || '')
  return `${head} · ${tail}`.trim()
}

const collapsible = computed(() => {
  return ['probe_result', 'command_result', 'channel_delivery', 'tool_trace', 'tool_skipped', 'reverse_result', 'proxy_result', 'api_inventory', 'security_card', 'runtime_trace', 'sandbox_file', 'sandbox_run', 'sandbox_env', 'file_resource_view', 'vulnerability_evidence', 'case_evidence', 'attack_chain', 'request_replay', 'oob_event', 'desktop_observe'].includes(props.block.type)
})

const fullWidth = computed(() => {
  if (collapsible.value) return expanded.value
  return Boolean(props.block.confirm_value || props.block.cancel_value || props.block.actions?.length)
    || ['confirm', 'command_plan', 'scan_plan', 'task_progress', 'findings_summary', 'todo_list', 'kb_updated', 'data_table', 'structured_card', 'file_resource_view', 'user_todo_card', 'user_todo_list', 'desktop_control', 'user_decision', 'user_decision_batch'].includes(props.block.type)
})

const collapsedSummary = computed(() => {
  const b = props.block || {}
  if (b.type === 'probe_result') {
    return t('assistantCard.summary.probeResult', {
      method: b.method || 'GET',
      url: b.url || '',
      status: b.status_code || '-',
      elapsed: b.elapsed_ms || 0,
    })
  }
  if (b.type === 'command_result') {
    return t('assistantCard.summary.commandResult', {
      command: b.command || 'command',
      args: (b.args || []).join(' '),
      exitCode: b.exit_code ?? '-',
    })
  }
  if (b.type === 'channel_delivery') {
    const status = b.ok === false
      ? t('assistantCard.status.failed')
      : (b.queued ? t('assistantCard.status.queued') : t('assistantCard.status.completed'))
    return `${b.provider || b.channel_id || 'channel'} · ${b.file?.name || b.title || 'delivery'} · ${status}`
  }
  if (b.type === 'tool_skipped') {
    if (b.reason === 'unknown_tool') {
      return t('assistantCard.summary.unknownTool', { tool: b.tool_name || b.title || 'tool' })
    }
    return t('assistantCard.summary.duplicateTool', { tool: b.tool_name || b.title || 'tool' })
  }
  if (b.type === 'tool_trace') {
    const result = b.result || {}
    const status = result.error
      ? t('assistantCard.status.executionError')
      : t('assistantCard.status.executionCompleted')
    return t('assistantCard.summary.toolTrace', { tool: b.name || 'tool', status })
  }
  if (b.type === 'reverse_result') {
    const status = b.ok === false
      ? t('assistantCard.status.failed')
      : t('assistantCard.status.completed')
    if (b.output_dir) {
      const partialSuffix = b.partial ? t('assistantCard.summary.partialSuffix') : ''
      const countSuffix = b.file_count != null
        ? t('assistantCard.summary.fileCountSuffix', { count: b.file_count })
        : ''
      return t('assistantCard.summary.reverseResult', {
        title: b.title || 'reverse',
        status,
        partialSuffix,
        countSuffix,
      })
    }
    const count = b.count ?? b.module_count ?? b.source_count ?? b.entry_count ?? b.assets?.length ?? b.detected?.length ?? ''
    return t('assistantCard.summary.reverseResult', {
      title: b.title || 'reverse',
      status,
      partialSuffix: '',
      countSuffix: count !== '' ? t('assistantCard.summary.itemCountSuffix', { count }) : '',
    })
  }
  if (b.type === 'proxy_result') {
    return proxyCardSummary(b)
  }
  if (b.type === 'api_inventory') {
    if (b.error) return `API — ${b.error}`
    if (b.kind === 'diff') return `API diff · +${(b.added || []).length} / -${(b.removed || []).length}`
    return `${b.spec_kind ? b.spec_kind + ' · ' : ''}API · ${b.count}`
  }
  if (b.type === 'security_card') {
    return b.error ? `${b.tool} — ${b.error}` : (b.stat || b.tool)
  }
  if (b.type === 'runtime_trace') {
    return t('assistantCard.summary.runtimeTrace', {
      title: b.title || 'runtime trace',
      requests: (b.requests || []).length,
      traces: (b.trace || []).length,
      elapsed: b.elapsed_ms || 0,
    })
  }
  if (b.type === 'sandbox_file') {
    return t('assistantCard.summary.sandboxFile', {
      title: b.title || 'sandbox',
      path: b.relative_path || b.directory || '',
      status: b.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.completed'),
    })
  }
  if (b.type === 'file_resource_view') {
    const items = Array.isArray(b.items) ? b.items : []
    const root = b.root_label || 'session sandbox'
    return t('assistantCard.summary.fileResources', {
      title: b.title || t('assistantCard.default.sandboxFiles'),
      count: items.length,
      root,
    })
  }
  if (b.type === 'sandbox_run') {
    return t('assistantCard.summary.sandboxRun', {
      title: b.title || 'run',
      exitCode: b.exit_code ?? '-',
      duration: b.duration_ms || 0,
      timeoutSuffix: b.timed_out ? t('assistantCard.summary.timeoutSuffix') : '',
    })
  }
  if (b.type === 'sandbox_env') {
    return `${b.title || 'env'} · Python ${(b.pythons || []).length} · Shell ${(b.shells || []).length}`
  }
  if (b.type === 'vulnerability_evidence') {
    const ev = b.evidence || b
    const status = b.vulnerable || b.suspected
      ? t('assistantCard.status.signalDetected')
      : (b.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.completed'))
    return t('assistantCard.summary.vulnerabilityEvidence', {
      title: b.title || ev.kind || 'evidence',
      status,
    })
  }
  if (b.type === 'case_evidence') {
    const label = b.record_type === 'artifact'
      ? t('assistantCard.label.artifact')
      : t('assistantCard.label.evidence')
    return t('assistantCard.summary.caseEvidence', {
      title: b.title || b.kind || label,
      kind: b.kind || 'other',
      status: b.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.archived'),
    })
  }
  if (b.type === 'request_replay') {
    const status = b.suspected || b.reflected
      ? t('assistantCard.status.suspected')
      : (b.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.completed'))
    return t('assistantCard.summary.requestReplay', { title: b.title || 'request', status })
  }
  if (b.type === 'oob_event') {
    return t('assistantCard.summary.oobEvent', {
      title: b.title || 'oob',
      count: (b.hits || []).length,
    })
  }
  if (b.type === 'desktop_observe') {
    const foregroundSuffix = b.foreground?.title
      ? t('assistantCard.summary.foregroundSuffix', { title: b.foreground.title })
      : ''
    return t('assistantCard.summary.desktopObserve', {
      monitors: b.monitor_count || 0,
      windows: b.window_count || 0,
      foregroundSuffix,
    })
  }
  return ''
})

const isCompactConfirm = computed(() =>
  Boolean(props.block?.compact) || props.block?.name === 'desktop_observe'
)

const taskProgress = computed(() => {
  const status = taskSnapshot.value?.status
  if (status === 'done') return 100
  return Number(taskSnapshot.value?.progress || 0)
})
const taskProgressLabel = computed(() => {
  const s = taskSnapshot.value || {}
  if (s.status === 'done') return t('assistantCard.progress.scanCompleted')
  if (s.status === 'cancelled') return t('assistantCard.progress.scanCancelled')
  if (s.status === 'error') return t('assistantCard.progress.scanError')
  if (s.current_check) return `${s.checks_done || 0}/${s.checks_total || 0} · ${s.current_check}`
  if (s.checks_total) {
    return t('assistantCard.progress.checksCompleted', {
      completed: s.checks_done || 0,
      total: s.checks_total,
    })
  }
  return s.status || t('assistantCard.progress.waitingToStart')
})
const taskProgressIndeterminate = computed(() => {
  return taskSnapshot.value?.status === 'running' && taskProgress.value <= 0
})

const todoItems = computed(() => Array.isArray(props.block.todos) ? props.block.todos : [])
const flatTodoItems = computed(() => {
  const rows = []
  function visit(todo, parentId = '', level = 0) {
    if (!todo || typeof todo !== 'object') return
    const normalized = parentId && !todo.parent_id ? { ...todo, parent_id: parentId } : todo
    rows.push({ todo: normalized, level })
    for (const child of Array.isArray(todo.children) ? todo.children : []) {
      visit(child, String(todo.id || ''), level + 1)
    }
  }
  for (const todo of todoItems.value) visit(todo)
  return rows
})
const todoStats = computed(() => {
  const counts = { total: flatTodoItems.value.length, pending: 0, in_progress: 0, completed: 0, blocked: 0, cancelled: 0, branches: 0 }
  for (const row of flatTodoItems.value) {
    const todo = row.todo
    const status = todo?.status || 'pending'
    counts[status] = (counts[status] || 0) + 1
    if (row.level > 0 || todo?.parent_id) counts.branches += 1
  }
  return counts
})
const todoRows = computed(() => {
  const items = todoItems.value
  const ids = new Set(items.map(t => String(t?.id || '')).filter(Boolean))
  const children = new Map()
  const roots = []
  items.forEach((todo, index) => {
    for (const child of Array.isArray(todo?.children) ? todo.children : []) {
      const parent = String(todo?.id || '')
      if (!children.has(parent)) children.set(parent, [])
      children.get(parent).push({ todo: child, index: `${index}-${children.get(parent).length}`, parent })
    }
    const parent = String(todo?.parent_id || '')
    const node = { todo, index }
    if (parent && ids.has(parent)) {
      if (!children.has(parent)) children.set(parent, [])
      children.get(parent).push(node)
    } else {
      roots.push(node)
    }
  })
  const rows = []
  const seen = new Set()
  function visit(node, level) {
    const id = String(node.todo?.id || `idx-${node.index}`)
    if (seen.has(id)) return
    seen.add(id)
    const parentId = String(node.parent || node.todo?.parent_id || '')
    const todo = parentId && !node.todo?.parent_id ? { ...node.todo, parent_id: parentId } : node.todo
    rows.push({ ...node, todo, level, hasChildren: (children.get(id) || []).length > 0 })
    if (collapsedTodoIds.value.has(id)) return
    for (const child of children.get(id) || []) visit(child, level + 1)
  }
  roots.forEach(node => visit(node, 0))
  items.forEach((todo, index) => visit({ todo, index }, 0))
  return rows
})
const submittedDecisionResponse = computed(() => {
  return String(props.block.submitted_response || localSubmittedResponse.value || '').trim()
})
const selectedDecisionOption = computed(() => {
  const submitted = submittedDecisionResponse.value
  if (localSelectedOption.value) return localSelectedOption.value
  return (props.block.options || []).includes(submitted) ? submitted : ''
})
const decisionLocked = computed(() => {
  return Boolean(submittedDecisionResponse.value)
})
const batchQuestions = computed(() => {
  return (Array.isArray(props.block.questions) ? props.block.questions : [])
    .map((q, index) => ({
      id: String(q?.id || `q${index + 1}`),
      question: String(q?.question || '').trim(),
      options: Array.isArray(q?.options) ? q.options.map(opt => String(opt || '').trim()).filter(Boolean) : [],
      required: q?.required !== false,
      context: String(q?.context || '').trim(),
    }))
    .filter(q => q.question)
})
const submittedBatchResponse = computed(() => {
  const raw = submittedDecisionResponse.value
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') return parsed
  } catch (_) {}
  return null
})
const batchLocked = computed(() => Boolean(submittedDecisionResponse.value))
const batchCanSubmit = computed(() => {
  if (props.actionsDisabled || batchLocked.value) return false
  const answers = batchAnswers.value || {}
  return batchQuestions.value.every(q => !q.required || String(answers[q.id] || '').trim())
})
const initialFileResourceItems = computed(() => Array.isArray(props.block.items) ? props.block.items.map(normalizeResourceItem) : [])
const hasLocalDirectoryTree = computed(() => {
  return initialFileResourceItems.value.some(item => resourcePath(item).includes('/'))
})
const fileResourceItems = computed(() => {
  if (Array.isArray(resourceDirItems.value)) return resourceDirItems.value
  if (hasLocalDirectoryTree.value) return localDirectoryItems(resourceDirPath.value)
  return initialFileResourceItems.value
})
const selectedResourceItem = computed(() => {
  const items = fileResourceItems.value
  if (!items.length) return null
  const id = selectedResourceId.value
  if (!id) return null
  return items.find(item => resourceId(item) === id) || null
})
const resourceDirLabel = computed(() => {
  return resourceDirPath.value ? `${props.block.root_label || 'session sandbox'} / ${resourceDirPath.value}` : (props.block.root_label || 'session sandbox')
})
const resourceParentPath = computed(() => {
  const parts = resourceDirPath.value.split('/').filter(Boolean)
  parts.pop()
  return parts.join('/')
})
const filePreviewLanguage = computed(() => {
  const ext = resourceExt(selectedResourceItem.value).toLowerCase()
  if (ext === 'py') return 'python'
  if (['js', 'mjs', 'cjs'].includes(ext)) return 'javascript'
  if (['json', 'jsonl'].includes(ext)) return 'json'
  if (['md', 'markdown'].includes(ext)) return 'markdown'
  if (['html', 'htm'].includes(ext)) return 'html'
  if (ext === 'css') return 'css'
  if (['yaml', 'yml'].includes(ext)) return 'yaml'
  if (['ps1', 'bat', 'cmd', 'sh'].includes(ext)) return 'shell'
  return 'text'
})
const filePreviewMode = computed(() => {
  if (resourcePreview.value.is_base64) return 'image'
  const ext = resourceExt(selectedResourceItem.value).toLowerCase()
  if (['md', 'markdown'].includes(ext)) return 'markdown'
  if (['json', 'jsonl'].includes(ext)) return 'json'
  if (['py', 'js', 'mjs', 'cjs', 'html', 'htm', 'css', 'yaml', 'yml', 'ps1', 'bat', 'cmd', 'sh'].includes(ext)) return 'code'
  return 'text'
})
const formattedPreviewContent = computed(() => {
  return formatPreviewContent(resourcePreview.value.content || t('assistantCard.preview.emptyFile'), filePreviewMode.value)
})
const highlightedPreviewContent = computed(() => {
  return highlightCode(formattedPreviewContent.value, filePreviewLanguage.value)
})

function scrollTodoToCurrent() {
  if (props.block.type !== 'todo_list') return
  nextTick(() => {
    const root = todoListRef.value
    if (!root) return
    const current = root.querySelector('[data-todo-current="true"]')
    if (!current) return
    current.scrollIntoView({ block: 'center', inline: 'nearest' })
  })
}

function toggleTodoCollapse(id) {
  const key = String(id || '')
  if (!key) return
  const next = new Set(collapsedTodoIds.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedTodoIds.value = next
}

function todoRefCount(todo, key) {
  const refs = todo?.[key]
  return Array.isArray(refs) ? refs.length : 0
}

function todoRefs(todo, key) {
  const refs = todo?.[key]
  return Array.isArray(refs) ? refs.map(ref => String(ref || '').trim()).filter(Boolean).slice(0, 8) : []
}

function todoStatusShort(status) {
  const map = {
    pending: t('assistantCard.todo.status.pending'),
    in_progress: t('assistantCard.todo.status.inProgress'),
    completed: t('assistantCard.todo.status.completed'),
    blocked: t('assistantCard.todo.status.blocked'),
    cancelled: t('assistantCard.todo.status.cancelled'),
  }
  return map[status] || status || t('assistantCard.todo.status.pending')
}

function todoInlineChips(todo) {
  const chips = []
  for (const key of ['phase', 'kind', 'risk', 'tool_hint']) {
    const value = String(todo?.[key] || '').trim()
    if (value) chips.push({ key, value })
  }
  return chips
}

function todoDetailLine(todo) {
  const parts = []
  const title = String(todo?.title || '').trim()
  const content = String(todo?.content || '').trim()
  if (content && content !== title) parts.push(content)
  if (todo?.done_when) {
    parts.push(t('assistantCard.todo.detail.doneWhen', { value: compactMeta(todo.done_when, 72) }))
  }
  if (todo?.blocked_reason) {
    parts.push(t('assistantCard.todo.detail.blockedReason', { value: compactMeta(todo.blocked_reason, 72) }))
  }
  if (todo?.evidence) {
    parts.push(t('assistantCard.todo.detail.evidence', { value: compactMeta(todo.evidence, 72) }))
  }
  if (todo?.target_ref) {
    parts.push(t('assistantCard.todo.detail.target', { value: compactMeta(todo.target_ref, 48) }))
  }
  if (todo?.trigger) {
    parts.push(t('assistantCard.todo.detail.trigger', { value: compactMeta(todo.trigger, 48) }))
  }
  if (todo?.parent_id) {
    parts.push(t('assistantCard.todo.detail.parent', { value: compactMeta(todo.parent_id, 32) }))
  }
  return parts.join(' · ')
}

function todoHasRefs(todo) {
  return todoRefCount(todo, 'finding_refs') || todoRefCount(todo, 'evidence_refs') || todoRefCount(todo, 'artifact_refs')
}

function openTodoRef(ref) {
  const value = String(ref || '').trim()
  if (!value) return
  if (value.startsWith('#/')) {
    navigate(value.slice(1))
    return
  }
  const routePrefixes = [
    ['task:', '/task/'],
    ['reverse:', '/reverse/'],
    ['knowledge:', '/knowledge/'],
    ['kb:', '/knowledge/'],
  ]
  for (const [prefix, route] of routePrefixes) {
    if (value.startsWith(prefix)) {
      navigate(route + encodeURIComponent(value.slice(prefix.length)))
      return
    }
  }
  copy(value)
}

async function refreshTaskProgress() {
  if (props.block.type !== 'task_progress' || !props.block.task_id) return
  try {
    const task = await api.getTask(props.block.task_id)
    if (task?.meta) taskSnapshot.value = task.meta
    if (['done', 'cancelled', 'error'].includes(task?.meta?.status)) stopTaskPolling()
  } catch (_) {}
}

function startTaskPolling() {
  stopTaskPolling()
  if (props.block.type !== 'task_progress' || !props.block.task_id) return
  refreshTaskProgress()
  pollTimer = setInterval(refreshTaskProgress, 1500)
}

function stopTaskPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

function act(value) {
  if (props.actionsDisabled) return
  if (value) emit('action', value)
}

function normalizeInternalRoute(href) {
  const route = String(href || '').trim().replace(/^#/, '')
  // Older/model-authored cards occasionally classified ReverseJob ids as
  // knowledge entries. Route stable rev-* ids to their actual workbench.
  const mistakenReverse = route.match(/^\/knowledge\/(rev-[^/?#]+)\/?$/i)
  return mistakenReverse ? `/reverse/${mistakenReverse[1]}` : route
}

function openInternalRoute(href) {
  const route = normalizeInternalRoute(href)
  if (route.startsWith('/')) navigate(route)
  else if (route) location.hash = route
}

// AI UI Protocol v2: dispatch reads action.type/payload first, with a fallback
// to the legacy flat shape (`href` / `value` / `confirm_value`). Both can
// coexist on a single object because `normalizeAction` only *adds* fields.
function dispatchActionEnvelope(action) {
  if (!action || typeof action !== 'object') return false
  const type = action.type
  const payload = (action.payload && typeof action.payload === 'object') ? action.payload : null
  switch (type) {
    case 'send_text':
    case 'user_decision': {
      const value = (payload && payload.value) ?? action.value
      if (value) act(value)
      return true
    }
    case 'open_route': {
      const href = String((payload && payload.href) ?? action.href ?? '')
      if (!href) return true
      openInternalRoute(href)
      return true
    }
    case 'open_external': {
      const href = String((payload && payload.href) ?? action.href ?? '')
      if (href) {
        try { window.open(href, '_blank', 'noopener') } catch (_) { location.href = href }
      }
      return true
    }
    case 'tool_decision': {
      emit('tool-decision', payload || {})
      return true
    }
    case 'open_attachment':
    case 'open_media': {
      emit('media', payload || {})
      return true
    }
    case 'custom':
    default:
      return false
  }
}

function runAction(action) {
  if (props.actionsDisabled) return
  if (dispatchActionEnvelope(action)) return
  // Legacy fallback: pre-protocol blocks may still carry only `href` / `value`.
  if (action?.href) {
    openInternalRoute(action.href)
    return
  }
  act(action?.value)
}

function submitDecisionOption(option) {
  if (props.actionsDisabled || decisionLocked.value) return
  const value = String(option || '').trim()
  if (!value) return
  localSelectedOption.value = value
  localSubmittedResponse.value = value
  act(value)
}

function submitDecisionInput() {
  if (props.actionsDisabled || decisionLocked.value) return
  const text = (decisionInput.value || '').trim()
  if (!text && props.block.input_required) return
  if (!text) return
  localSelectedOption.value = ''
  localSubmittedResponse.value = text
  decisionInput.value = ''
  act(text)
}

function setBatchAnswer(questionId, option) {
  if (props.actionsDisabled || batchLocked.value) return
  const id = String(questionId || '')
  const value = String(option || '').trim()
  if (!id || !value) return
  batchAnswers.value = { ...batchAnswers.value, [id]: value }
}

function submittedBatchAnswer(questionId) {
  const submitted = submittedBatchResponse.value
  if (!submitted) return ''
  const answers = submitted.answers && typeof submitted.answers === 'object'
    ? submitted.answers
    : submitted
  return String(answers?.[questionId] || '')
}

function submitDecisionBatch() {
  if (!batchCanSubmit.value) return
  const answers = {}
  const labels = []
  for (const q of batchQuestions.value) {
    const value = String(batchAnswers.value?.[q.id] || '').trim()
    if (!value) continue
    answers[q.id] = value
    labels.push(`${q.question}: ${value}`)
  }
  const payload = JSON.stringify({
    type: 'user_decision_batch_response',
    title: props.block.title || '',
    answers,
    summary: labels.join('\n'),
  }, null, 2)
  localSubmittedResponse.value = payload
  act(payload)
}

function copy(text) {
  try { navigator.clipboard?.writeText(String(text || '')) } catch (_) {}
}

async function openReverseOutputDir(dir) {
  const path = String(dir || '').trim()
  if (!path) return
  try {
    const res = await api.openFolder(path)
    if (res?.ok === false) showToast(res.error || t('assistantCard.error.openDirectoryFailed'), 'error')
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function openReverseJob(jobId) {
  const id = String(jobId || '').trim()
  if (id) navigate(`/reverse/${encodeURIComponent(id)}`)
}


function clearPreviewSelection() {
  previewSelection.value = null
  previewSelectionAnchor.value = { visible: false, left: 0, top: 0 }
  emit('preview-selection', null)
}

function handlePreviewSelection() {
  const container = filePreviewContentRef.value
  const pane = filePreviewPaneRef.value
  if (!container || !pane || filePreviewMode.value === 'image') {
    clearPreviewSelection()
    return
  }
  const sel = window.getSelection?.()
  if (!sel || !sel.rangeCount || sel.isCollapsed) {
    clearPreviewSelection()
    return
  }
  const range = sel.getRangeAt(0)
  if (!container.contains(range.commonAncestorContainer)) {
    clearPreviewSelection()
    return
  }
  const selectedText = String(sel.toString() || '').trim()
  if (!selectedText) {
    clearPreviewSelection()
    return
  }
  const startProbe = document.createRange()
  startProbe.selectNodeContents(container)
  startProbe.setEnd(range.startContainer, range.startOffset)
  const startIndex = startProbe.toString().length
  const endProbe = document.createRange()
  endProbe.selectNodeContents(container)
  endProbe.setEnd(range.endContainer, range.endOffset)
  const endIndex = endProbe.toString().length
  const contentText = formattedPreviewContent.value || ''
  const startPos = lineAndColumnFromIndex(contentText, startIndex)
  const endPos = lineAndColumnFromIndex(contentText, endIndex)
  const rect = range.getBoundingClientRect()
  const paneRect = pane.getBoundingClientRect()
  const left = Math.max(8, Math.min(rect.right - paneRect.left + 8, pane.clientWidth - 96))
  const top = Math.max(8, Math.min(rect.top - paneRect.top - 30, pane.clientHeight - 34))
  const payload = {
    file_path: resourcePath(selectedResourceItem.value),
    file_name: selectedResourceItem.value?.name || '',
    start_index: startIndex,
    end_index: endIndex,
    start_line: startPos.line,
    start_column: startPos.column,
    end_line: endPos.line,
    end_column: endPos.column,
    text: selectedText,
  }
  previewSelection.value = payload
  previewSelectionAnchor.value = { visible: true, left, top }
  emit('preview-selection', { ...payload, intent: 'select' })
}


function openKbEntry(block) {
  const id = block?.id || block?.new_id
  if (!id) return
  navigate('/knowledge/' + encodeURIComponent(id))
}


function runUiAction(action) {
  if (!action) return
  if (dispatchActionEnvelope(action)) return
  if (action.href) {
    openInternalRoute(action.href)
    return
  }
  if (action.value) emit('action', action.value)
}

function uiActionDisabled(action) {
  if (!action || typeof action !== 'object') return true
  if (action.disabled === true) return true
  // ActionEnvelope: presence of a known type is enough; payload may be empty
  // for custom actions handled by listeners.
  if (action.type && action.type !== 'custom') return false
  return !action.href && !action.value
}

function tableRowActions(row) {
  if (Array.isArray(row?.actions)) return row.actions.filter(Boolean)
  return row?.action ? [row.action] : []
}

function tableHasActions(rows) {
  return (rows || []).some(row => tableRowActions(row).length)
}


function localDirectoryItems(path = '') {
  const prefix = String(path || '').replace(/^\/+|\/+$/g, '')
  const base = prefix ? `${prefix}/` : ''
  const dirs = new Map()
  const files = []
  for (const item of initialFileResourceItems.value) {
    const rel = resourcePath(item).replace(/^\/+/, '')
    if (!rel || (prefix && !rel.startsWith(base))) continue
    const rest = prefix ? rel.slice(base.length) : rel
    if (!rest || rest === rel && prefix && !rel.startsWith(base)) continue
    const slash = rest.indexOf('/')
    if (slash >= 0) {
      const name = rest.slice(0, slash)
      const dirPath = base + name
      if (!dirs.has(dirPath)) {
        dirs.set(dirPath, {
          id: `dir:${dirPath}`,
          name,
          path: dirPath,
          relative_path: dirPath,
          kind: 'dir',
          previewable: false,
          _local_dir: true,
        })
      }
      continue
    }
    files.push(item)
  }
  return [...dirs.values(), ...files].sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1
    return String(a.name || '').localeCompare(String(b.name || ''))
  })
}

async function enterResourceDirectory(path = '') {
  if (hasLocalDirectoryTree.value) {
    resourceDirPath.value = String(path || '').replace(/^\/+|\/+$/g, '')
    resourceDirItems.value = null
    selectedResourceId.value = ''
    imagePreviewOpen.value = false
    clearPreviewSelection()
    resourcePreview.value = { loading: false, path: '', content: '', error: '', truncated: false, size: 0, is_base64: false }
    return
  }
  await loadResourceDirectory(path)
}

async function loadResourceDirectory(path = '') {
  resourceDirLoading.value = true
  resourceDirError.value = ''
  selectedResourceId.value = ''
  imagePreviewOpen.value = false
  clearPreviewSelection()
  resourcePreview.value = { loading: false, path: '', content: '', error: '', truncated: false, size: 0, is_base64: false }
  try {
    const result = await api.listAssistantSandboxDirectory(props.sessionId, path)
    if (!result?.ok) throw new Error(result?.error || t('assistantCard.error.readDirectoryFailed'))
    resourceDirPath.value = String(result.relative_path || path || '')
    resourceDirItems.value = (Array.isArray(result.items) ? result.items : []).map(normalizeResourceItem)
  } catch (e) {
    resourceDirError.value = e?.message || String(e)
  } finally {
    resourceDirLoading.value = false
  }
}


async function selectFileResource(item) {
  const id = resourceId(item)
  selectedResourceId.value = id
  imagePreviewOpen.value = false
  clearPreviewSelection()
  const path = resourcePath(item)
  if (item?.kind === 'dir') {
    await enterResourceDirectory(path)
    return
  }
  if (!path || item?.previewable === false) {
    resourcePreview.value = { loading: false, path, content: '', error: t('assistantCard.error.fileNotPreviewable'), truncated: false, size: Number(item?.size || 0), is_base64: false }
    return
  }
  resourcePreview.value = { loading: true, path, content: '', error: '', truncated: false, size: Number(item?.size || 0), is_base64: false }
  try {
    const ext = resourceExt(item).toLowerCase()
    const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(ext)
    const result = await api.readAssistantSandboxFile(props.sessionId, path, 60000, isImage)
    if (!result?.ok) throw new Error(result?.error || t('assistantCard.error.readFileFailed'))
    resourcePreview.value = {
      loading: false,
      path: result.relative_path || path,
      content: String(result.content || ''),
      error: '',
      truncated: Boolean(result.truncated),
      size: Number(result.size || item?.size || 0),
      is_base64: Boolean(result.is_base64),
    }
  } catch (e) {
    resourcePreview.value = { loading: false, path, content: '', error: e?.message || String(e), truncated: false, size: Number(item?.size || 0), is_base64: false }
  }
}

function previewSelectedResourceFullscreen() {
  const item = selectedResourceItem.value
  const path = resourcePath(item)
  if (!item || !path) return
  if (resourcePreview.value.is_base64 && resourcePreview.value.content) {
    mediaPreview.openPayload({
      kind: 'image',
      content: resourcePreview.value.content,
      name: item.name || path,
      path,
      sessionId: props.sessionId,
      size: resourcePreview.value.size || item.size || 0,
    })
    return
  }
  mediaPreview.openFile(path, {
    sessionId: props.sessionId,
    name: item.name || path,
  })
}

// ---- file_resource_view (slim layout) handlers ------------------------
//
// Click on a chip → open the sandbox workspace (tree + inline preview) and
// jump to this path. Preview loads in the right pane — no fullscreen overlay.
function onFileResourceChipClick(item) {
  if (!item) return
  openInSandboxPanel(resourcePath(item))
}

// Hand the user off to the right-side sandbox browser. `AssistantPanel` is
// listening for this event and handles the actual panel open + scroll-to.
function openInSandboxPanel(path) {
  window.dispatchEvent(new CustomEvent('argusai:open-sandbox-panel', {
    detail: { file_path: String(path || '') },
  }))
}

async function openSandboxScreenshot(relPath) {
  if (!props.sessionId || !relPath) return
  try {
    const r = await api.readAssistantSandboxFile(props.sessionId, relPath, 60000, true)
    if (r?.ok && r.content) {
      mediaPreview.open({ src: r.content, name: relPath.split(/[/\\]/).pop() || 'screenshot.png', kind: 'image' })
    }
  } catch (_) { /* ignore */ }
}

// Use the same selection_reference channel that the sandbox panel uses, so
// the chip above the input area looks identical regardless of where the
// user picked the file from.
function referenceFileResource(item) {
  if (!item) return
  const path = resourcePath(item)
  if (!path) return
  window.dispatchEvent(new CustomEvent('argusai:reference-file', {
    detail: {
      file_path: path,
      file_name: item.name || path,
    },
  }))
}

async function askAboutFileResource(item) {
  if (!item || !props.sessionId) return
  const path = resourcePath(item)
  if (!path) return
  const name = item.name || path
  const ref = normalizeSelectionReference({
    kind: 'file_selection',
    file_path: path,
    file_name: name,
  })
  await openAsk({
    sessionId: props.sessionId,
    selectionReference: ref,
    page: 'assistant_card',
    context: { source: 'file_resource_view', file_path: path, file_name: name },
    anchor: { left: 8, top: 8 },
  })
}


onMounted(() => {
  startTaskPolling()
  scrollTodoToCurrent()
})
onUnmounted(stopTaskPolling)
watch(() => props.block.task_id, () => {
  taskSnapshot.value = props.block.snapshot || {}
  startTaskPolling()
})
watch(() => props.block, () => {
  expanded.value = isFileResourceViewExpandedByDefault(props.block)
  localSubmittedResponse.value = ''
  localSelectedOption.value = ''
  batchAnswers.value = {}
  decisionInput.value = ''
  selectedResourceId.value = ''
  resourcePreview.value = { loading: false, path: '', content: '', error: '', truncated: false, size: 0, is_base64: false }
  resourceDirPath.value = ''
  resourceDirItems.value = null
  resourceDirLoading.value = false
  resourceDirError.value = ''
  imagePreviewOpen.value = false
  clearPreviewSelection()
  scrollTodoToCurrent()
})
watch(
  () => todoRows.value.map(row => `${row.todo?.id || row.index}:${row.todo?.status || 'pending'}`).join('|'),
  scrollTodoToCurrent,
)
</script>

<template>
  <div
    class="assistant-card"
    :class="[
      block.type,
      block.variant ? 'variant-' + block.variant : '',
      {
        collapsed: collapsible && !expanded,
        full: fullWidth,
        compact: isCompactConfirm,
        'quiet-confirm': block.type === 'confirm' && isCompactConfirm,
      },
    ]"
  >
    <div v-if="block.type !== 'kb_updated' && block.type !== 'user_decision' && block.type !== 'user_decision_batch'" class="card-head">
      <div class="card-title">{{ block.title || block.name || block.type }}</div>
      <button v-if="collapsible" class="ghost tiny collapse-toggle" @click="expanded = !expanded">
        {{ expanded ? t('assistantCard.action.collapse') : t('assistantCard.action.expand') }}
      </button>
    </div>
    <div v-if="collapsible && !expanded" class="collapsed-summary">{{ collapsedSummary }}</div>

    <div v-if="block.type === 'scan_plan'" class="card-body">
      <div><strong>{{ t('assistantCard.label.target') }}</strong> {{ block.arguments?.url || block.target_url || '-' }}</div>
      <div><strong>{{ t('assistantCard.label.scope') }}</strong> {{ (block.arguments?.scope || block.scope || []).toString() || '-' }}</div>
      <div><strong>{{ t('assistantCard.label.risk') }}</strong> {{ block.risk || '-' }}</div>
      <div><strong>{{ t('assistantCard.label.checks') }}</strong> {{ (block.arguments?.checks || block.checks || []).join?.(', ') || '-' }}</div>
    </div>

    <div v-else-if="block.type === 'task_progress'" class="card-body">
      <div><strong>{{ t('assistantCard.label.task') }}</strong> {{ block.task_id }}</div>
      <ProgressBar :value="taskProgress" :label="taskProgressLabel" :indeterminate="taskProgressIndeterminate" />
      <a v-if="block.task_id" :href="'#/task/' + block.task_id">{{ t('assistantCard.action.openTaskDetails') }}</a>
    </div>

    <div v-else-if="block.type === 'findings_summary'" class="card-body">
      <SeverityBar :severity="block.counts || {}" />
      <div v-for="f in (block.top_findings || []).slice(0, 5)" :key="f.uid || f.id" class="mini-row">
        <span class="sev">{{ f.severity }}</span><span>{{ f.title || f.id }}</span>
      </div>
      <a v-if="block.task_id" :href="'#/task/' + block.task_id">{{ t('assistantCard.action.viewAllFindings') }}</a>
    </div>

    <div v-else-if="block.type === 'tool_skipped' && expanded" class="card-body tool-skipped-body">
      <div v-if="block.reason === 'unknown_tool'" class="tool-skipped-banner error">{{ t('assistantCard.toolSkipped.unknownTool') }}</div>
      <div v-else class="tool-skipped-banner">{{ t('assistantCard.toolSkipped.duplicateCall') }}</div>
      <div><strong>{{ block.tool_name || block.title }}</strong></div>
      <div class="muted small">{{ block.message || block.suggested_fix || (block.reason === 'unknown_tool' ? t('assistantCard.toolSkipped.unknownToolHint') : t('assistantCard.toolSkipped.duplicateCallHint')) }}</div>
      <ul v-if="(block.suggestions || []).length" class="tool-suggestion-list">
        <li v-for="(sug, si) in block.suggestions" :key="si">
          <code>{{ typeof sug === 'string' ? sug : (sug.tool || sug.name || sug) }}</code>
        </li>
      </ul>
      <div v-if="block.next_step" class="muted small">{{ t('assistantCard.label.nextStep') }}: <code>{{ block.next_step }}</code></div>
      <pre v-if="block.arguments && Object.keys(block.arguments).length" class="tool-skipped-args">{{ JSON.stringify(block.arguments, null, 2) }}</pre>
    </div>

    <div v-else-if="block.type === 'probe_result' && expanded" class="card-body">
      <div><strong>{{ block.method || 'GET' }}</strong> {{ block.url }}</div>
      <div>{{ t('assistantCard.metrics.statusDuration', { status: block.status_code || '-', duration: block.elapsed_ms || 0 }) }}</div>
      <pre v-if="block.snippet">{{ block.snippet }}</pre>
    </div>

    <div v-else-if="block.type === 'command_plan'" class="card-body">
      <div><strong>{{ t('assistantCard.label.command') }}</strong> {{ block.name }}</div>
      <div><strong>{{ t('assistantCard.label.risk') }}</strong> {{ block.risk }}</div>
      <pre>{{ JSON.stringify(block.arguments || {}, null, 2) }}</pre>
    </div>

    <div v-else-if="block.type === 'command_result' && expanded" class="card-body">
      <div><strong>{{ block.command }}</strong> {{ (block.args || []).join(' ') }}</div>
      <div>{{ t('assistantCard.metrics.exitCodeDuration', { exitCode: block.exit_code, duration: block.duration_ms }) }}</div>
      <pre>{{ block.stdout_excerpt || block.stderr_excerpt || block.error }}</pre>
      <button class="ghost tiny" @click="copy(block.stdout_excerpt || block.stderr_excerpt || '')">{{ t('assistantCard.action.copyOutput') }}</button>
    </div>

    <div v-else-if="block.type === 'channel_delivery' && expanded" class="card-body">
      <div><strong>{{ block.provider || block.channel_id || 'channel' }}</strong> · {{ block.queued ? t('assistantCard.status.queued') : t('assistantCard.status.completed') }}</div>
      <div v-if="block.delivery_id"><strong>ID</strong> {{ block.delivery_id }}</div>
      <div v-if="block.file?.relative_path"><strong>{{ t('assistantCard.label.file') }}</strong> {{ block.file.relative_path }}</div>
      <div v-if="block.file?.size !== undefined">{{ t('assistantCard.metrics.sizeBytes', { size: block.file.size }) }}</div>
      <div v-if="block.file?.sha256">SHA256: {{ block.file.sha256 }}</div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
    </div>

    <div v-else-if="block.type === 'todo_list'" class="card-body">
      <div v-if="!(block.todos || []).length" class="muted">{{ t('assistantCard.todo.empty') }}</div>
      <div v-else class="todo-summary">
        <span>{{ t('assistantCard.todo.summary.total', { count: todoStats.total }) }}</span>
        <span>{{ t('assistantCard.todo.summary.inProgress', { count: todoStats.in_progress }) }}</span>
        <span>{{ t('assistantCard.todo.summary.pending', { count: todoStats.pending }) }}</span>
        <span>{{ t('assistantCard.todo.summary.blocked', { count: todoStats.blocked }) }}</span>
        <span>{{ t('assistantCard.todo.summary.branches', { count: todoStats.branches }) }}</span>
      </div>
      <div v-if="todoRows.length" ref="todoListRef" class="todo-scroll">
        <div
          v-for="row in todoRows"
          :key="row.todo.id || row.index"
          class="todo-row"
          :class="[row.todo.status, { child: row.level > 0, parent: row.hasChildren }]"
          :style="{ '--todo-level': row.level }"
          :data-todo-current="row.todo.status === 'in_progress' ? 'true' : undefined"
        >
          <button
            v-if="row.hasChildren"
            class="todo-collapse"
            type="button"
            @click="toggleTodoCollapse(row.todo.id)"
          >
            {{ collapsedTodoIds.has(String(row.todo.id || '')) ? '▸' : '▾' }}
          </button>
          <span v-else class="todo-collapse-spacer"></span>
          <div class="todo-main">
            <div class="todo-title-line">
              <span class="todo-status-pill">{{ todoStatusShort(row.todo.status) }}</span>
              <code v-if="row.todo.id" class="todo-id" :title="row.todo.id">{{ row.todo.id }}</code>
              <span class="todo-content" :title="row.todo.title || row.todo.content">{{ row.todo.title || row.todo.content }}</span>
              <span
                v-for="chip in todoInlineChips(row.todo)"
                :key="chip.key + chip.value"
                class="todo-chip"
                :class="{ risk: chip.key === 'risk', tool: chip.key === 'tool_hint' }"
              >{{ chip.value }}</span>
              <span v-if="row.hasChildren" class="todo-progress">
                {{ (row.todo.children || []).filter(t => t.status === 'completed').length }}/{{ (row.todo.children || []).length }}
              </span>
            </div>
            <div v-if="todoDetailLine(row.todo)" class="todo-detail" :title="todoDetailLine(row.todo)">
              {{ todoDetailLine(row.todo) }}
            </div>
            <div v-if="todoHasRefs(row.todo)" class="todo-ref-line">
              <button
                v-for="ref in todoRefs(row.todo, 'finding_refs')"
                :key="'finding-' + ref"
                class="todo-ref-chip"
                type="button"
                :title="ref"
                @click="openTodoRef(ref)"
              >
                F {{ compactMeta(ref, 20) }}
              </button>
              <button
                v-for="ref in todoRefs(row.todo, 'evidence_refs')"
                :key="'evidence-' + ref"
                class="todo-ref-chip"
                type="button"
                :title="ref"
                @click="openTodoRef(ref)"
              >
                E {{ compactMeta(ref, 20) }}
              </button>
              <button
                v-for="ref in todoRefs(row.todo, 'artifact_refs')"
                :key="'artifact-' + ref"
                class="todo-ref-chip"
                type="button"
                :title="ref"
                @click="openTodoRef(ref)"
              >
                A {{ compactMeta(ref, 20) }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-else-if="block.type === 'tool_activity'" class="card-body tool-activity-body">
      <span class="activity-dot" :class="block.state"></span>
      <code>{{ block.name }}</code>
      <span class="activity-state">{{ block.state === 'done' ? t('assistantCard.status.activityCompleted') : t('assistantCard.status.inProgress') }}</span>
    </div>

    <div v-else-if="block.type === 'data_table'" class="card-body data-table-body">
      <div class="data-table-head">
        <div>
          <div class="data-table-title">{{ block.title || t('assistantCard.table.defaultTitle') }}</div>
          <div v-if="block.subtitle" class="data-table-subtitle">{{ block.subtitle }}</div>
        </div>
        <span class="data-table-count">{{ t('assistantCard.table.rowCount', { count: (block.rows || []).length }) }}</span>
      </div>
      <div v-if="!(block.rows || []).length" class="muted">{{ block.empty_text || t('assistantCard.table.empty') }}</div>
      <div v-else class="data-table-scroll">
        <table>
          <thead>
            <tr>
              <th v-for="col in (block.columns || [])" :key="col.key || col.label">{{ col.label || col.key }}</th>
              <th v-if="tableHasActions(block.rows || [])" class="data-table-action-col">{{ t('assistantCard.label.actions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in (block.rows || [])" :key="row.id || ri">
              <td v-for="col in (block.columns || [])" :key="col.key || col.label" :title="tableCellValue(row, col)">
                {{ tableCellValue(row, col) }}
              </td>
              <td v-if="tableHasActions(block.rows || [])" class="data-table-action-cell">
                <div class="data-table-actions">
                  <button
                    v-for="(action, ai) in tableRowActions(row)"
                    :key="ai"
                    class="data-table-action-btn"
                    :disabled="uiActionDisabled(action)"
                    @click="runUiAction(action)"
                  >
                    {{ action.label || t('assistantCard.action.view') }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div
      v-else-if="block.type === 'structured_card'"
      class="card-body structured-card-body"
      :class="'variant-' + (block.variant || 'neutral')"
    >
      <div v-if="block.subtitle" class="structured-card-subtitle">{{ block.subtitle }}</div>
      <div v-for="(section, si) in (block.sections || [])" :key="si" class="structured-section" :class="'section-' + section.type">
        <div v-if="section.title" class="structured-section-title">{{ section.title }}</div>
        <p v-if="section.type === 'text'" class="structured-text">{{ section.text }}</p>
        <div v-else-if="section.type === 'kv'" class="structured-kv-grid">
          <div v-for="(item, ii) in (section.items || [])" :key="ii" class="structured-kv-item">
            <span class="structured-kv-key">{{ item.label }}</span>
            <span class="structured-kv-value">{{ item.value }}</span>
          </div>
        </div>
        <div v-else-if="section.type === 'metrics'" class="structured-metrics">
          <div v-for="(item, ii) in (section.items || [])" :key="ii" class="structured-metric">
            <span class="structured-metric-value">{{ item.value }}</span>
            <span class="structured-metric-label">{{ item.label }}</span>
            <span v-if="item.note" class="structured-metric-note">{{ item.note }}</span>
          </div>
        </div>
        <ul v-else-if="section.type === 'list'" class="structured-list">
          <li v-for="(item, ii) in (section.items || [])" :key="ii">{{ item.label }}</li>
        </ul>
        <div v-else-if="section.type === 'badges'" class="structured-badges">
          <span
            v-for="(item, ii) in (section.items || [])"
            :key="ii"
            class="structured-badge"
            :class="'variant-' + (item.variant || 'neutral')"
          >{{ item.label }}</span>
        </div>
        <div v-else-if="section.type === 'steps'" class="structured-steps">
          <div v-for="(item, ii) in (section.items || [])" :key="ii" class="structured-step">
            <span class="structured-step-index">{{ ii + 1 }}</span>
            <span class="structured-step-main">
              <span class="structured-step-title">{{ item.title }}</span>
              <span v-if="item.body" class="structured-step-body">{{ item.body }}</span>
              <span v-if="item.status" class="structured-step-status">{{ item.status }}</span>
            </span>
          </div>
        </div>
        <div v-else-if="section.type === 'actions'" class="structured-actions">
          <button
            v-for="(action, ai) in (section.actions || [])"
            :key="ai"
            class="structured-action-btn"
            :disabled="uiActionDisabled(action)"
            @click="runUiAction(action)"
          >{{ action.label || t('assistantCard.action.open') }}</button>
        </div>
      </div>
    </div>

    <!--
      file_resource_view (v2 slim layout):

      The card is now an AI "shout-out" — it lists the files the model wants
      to draw the user's attention to and provides one-click affordances:
      - click a row  → open the file in the global MediaPreview overlay
      - click "Browse in sandbox" → dispatches ``argusai:open-sandbox-panel`` so the
        right-side sandbox browser opens AND scrolls to / pulses this file
      - click "Reference" → reference this file in the next AI message

      We deliberately removed the in-card directory navigator and preview
      pane: full browse / preview belongs in the side panel + global preview.
    -->
    <div v-else-if="block.type === 'file_resource_view' && expanded" class="card-body file-resource-body slim">
      <div class="file-resource-head">
        <div class="file-resource-head-text">
          <div v-if="block.subtitle" class="file-resource-subtitle">{{ block.subtitle }}</div>
          <div class="file-resource-root">{{ resourceDirLabel }}</div>
        </div>
        <div class="file-resource-head-actions">
          <span class="file-resource-count">{{ t('assistantCard.file.itemCount', { count: fileResourceItems.length }) }}</span>
          <button
            class="ghost tiny file-resource-browse"
            :title="t('assistantCard.file.browseDirectoryTitle')"
            @click="openInSandboxPanel('')"
          >{{ t('assistantCard.action.browseSandbox') }}</button>
        </div>
      </div>
      <div v-if="!fileResourceItems.length" class="muted">{{ block.empty_text || t('assistantCard.file.empty') }}</div>
      <div v-else class="file-resource-grid">
        <div
          v-for="item in fileResourceItems"
          :key="resourceId(item)"
          class="file-resource-chip"
          :class="{ dir: item.kind === 'dir' }"
          @click="onFileResourceChipClick(item)"
        >
          <span class="frc-icon" :style="{ background: fileTypeChipColor(item) }">{{ fileTypeIcon(item).label }}</span>
          <div class="frc-main">
            <span class="frc-name">{{ item.name || resourcePath(item) }}</span>
            <span class="frc-meta">
              <span v-if="item.kind === 'dir'">{{ t('assistantCard.label.directory') }}</span>
              <span v-else-if="formatBytes(item.size)">{{ formatBytes(item.size) }}</span>
            </span>
          </div>
          <span class="frc-actions" @click.stop>
            <button class="ghost icon" :title="t('assistantCard.action.locateInSandbox')" @click="openInSandboxPanel(resourcePath(item))">▤</button>
            <button class="ghost icon" :title="t('assistantCard.action.referenceInChat')" @click="referenceFileResource(item)">⤴</button>
            <button class="ghost icon" :title="t('assistantCard.action.askAboutFile')" @click="askAboutFileResource(item)">?</button>
          </span>
        </div>
      </div>
    </div>

    <div
      v-else-if="block.type === 'kb_updated'"
      class="card-body kb-updated-body"
      role="button"
      tabindex="0"
      :title="(block.title || block.id || block.new_id || '') + ' · ' + t('kb.notify.clickToOpen')"
      @click.stop="openKbEntry(block)"
      @keydown.enter.stop.prevent="openKbEntry(block)"
      @keydown.space.stop.prevent="openKbEntry(block)"
    >
      <div class="kb-updated-head">
        <span class="kb-icon" aria-hidden="true">KB</span>
        <span class="kb-action-tag" :class="block.action">{{ kbActionLabel(block.action) }}</span>
        <span class="kb-title">{{ block.title || t('assistantCard.kb.unnamedEntry') }}</span>
        <code class="kb-id-inline">{{ block.id || block.new_id || '' }}</code>
      </div>
      <div class="kb-updated-grid">
        <div><strong>{{ t('assistantCard.label.confidence') }}</strong><span>{{ block.confidence || '—' }}</span></div>
        <div><strong>{{ t('assistantCard.label.version') }}</strong><span>v{{ block.version || 1 }}</span></div>
        <div><strong>{{ t('assistantCard.label.tags') }}</strong><span>{{ compactList(block.tags) }}</span></div>
        <div><strong>{{ t('assistantCard.label.appliesTo') }}</strong><span>{{ compactList(block.applies_to) }}</span></div>
      </div>
      <div v-if="block.body_preview" class="kb-body-preview">
        <strong>{{ t('assistantCard.label.summary') }}</strong>
        <p>{{ block.body_preview }}{{ (block.body_chars || 0) > block.body_preview.length ? '…' : '' }}</p>
      </div>
      <div v-if="(block.near_matches || []).length" class="kb-near-matches">
        <strong>{{ t('assistantCard.kb.possibleDuplicates') }}</strong>
        <span v-for="m in block.near_matches" :key="m.id">
          <code>{{ m.id }}</code> {{ m.title }}
        </span>
      </div>
      <div class="kb-updated-foot">
        {{ t('assistantCard.kb.openHint') }}
      </div>
    </div>

    <div v-else-if="block.type === 'user_decision'" class="card-body user-decision-body">
      <div class="user-decision-head">
        <span class="user-decision-icon">⏸</span>
        <span class="user-decision-label">{{ t('assistant.userDecision.label') }}</span>
      </div>
      <div class="user-decision-question">{{ block.question }}</div>
      <div v-if="block.context" class="user-decision-context">{{ block.context }}</div>
      <div v-if="(block.options || []).length" class="user-decision-options">
        <button
          v-for="(opt, oi) in block.options"
          :key="oi"
          class="user-decision-opt"
          :class="{ selected: selectedDecisionOption === opt }"
          :aria-pressed="selectedDecisionOption === opt"
          :disabled="actionsDisabled || decisionLocked"
          @click="submitDecisionOption(opt)"
        >{{ opt }}</button>
      </div>
      <div v-if="submittedDecisionResponse" class="user-decision-submitted">
        <span class="submitted-label">{{ t('assistantCard.decision.replied') }}</span>
        <span
          v-if="submittedDecisionResponse !== selectedDecisionOption"
          class="submitted-text"
        >{{ submittedDecisionResponse }}</span>
      </div>
      <div v-if="!decisionLocked" class="user-decision-input">
        <textarea
          v-model="decisionInput"
          :disabled="actionsDisabled"
          :placeholder="block.input_placeholder || t('assistantCard.decision.placeholder')"
          rows="1"
          @keydown.ctrl.enter.prevent="submitDecisionInput"
          @keydown.meta.enter.prevent="submitDecisionInput"
        ></textarea>
        <div class="user-decision-input-actions">
          <span class="muted">{{ t('assistantCard.decision.keyboardHint') }}</span>
          <button
            class="user-decision-opt primary"
            :disabled="actionsDisabled || Boolean(block.submitted_response) || !decisionInput.trim()"
            @click="submitDecisionInput"
          >{{ t('assistantCard.action.sendReply') }}</button>
        </div>
      </div>
    </div>

    <div v-else-if="block.type === 'user_decision_batch'" class="card-body user-decision-body user-decision-batch-body">
      <div class="user-decision-head">
        <span class="user-decision-icon">⏸</span>
        <span class="user-decision-label">{{ t('assistantCard.decision.batchLabel') }}</span>
      </div>
      <div class="user-decision-question">{{ block.title || t('assistantCard.decision.batchTitle') }}</div>
      <div v-if="block.context" class="user-decision-context">{{ block.context }}</div>
      <div class="user-decision-batch-list">
        <section
          v-for="(q, qi) in batchQuestions"
          :key="q.id || qi"
          class="user-decision-batch-question"
        >
          <div class="batch-question-head">
            <span class="batch-question-index">{{ qi + 1 }}</span>
            <span class="batch-question-text">{{ q.question }}</span>
            <span v-if="q.required" class="batch-question-required">{{ t('assistantCard.decision.required') }}</span>
          </div>
          <div v-if="q.context" class="user-decision-context batch-question-context">{{ q.context }}</div>
          <div class="user-decision-options">
            <button
              v-for="(opt, oi) in q.options"
              :key="`${q.id}-${oi}`"
              class="user-decision-opt"
              :class="{ selected: (batchLocked ? submittedBatchAnswer(q.id) : batchAnswers[q.id]) === opt }"
              :aria-pressed="(batchLocked ? submittedBatchAnswer(q.id) : batchAnswers[q.id]) === opt"
              :disabled="actionsDisabled || batchLocked"
              @click="setBatchAnswer(q.id, opt)"
            >{{ opt }}</button>
          </div>
        </section>
      </div>
      <div v-if="submittedDecisionResponse" class="user-decision-submitted">
        <span class="submitted-label">{{ t('assistantCard.decision.replied') }}</span>
        <span class="submitted-text">{{ submittedBatchResponse?.summary || submittedDecisionResponse }}</span>
      </div>
      <div v-else class="user-decision-input-actions batch-submit-row">
        <span class="muted">{{ t('assistantCard.decision.batchHelp') }}</span>
        <button
          class="user-decision-opt primary"
          :disabled="!batchCanSubmit"
          @click="submitDecisionBatch"
        >{{ t('assistantCard.action.submitAll') }}</button>
      </div>
    </div>

    <div v-else-if="block.type === 'tool_trace' && expanded" class="card-body">
      <div><strong>{{ block.name }}</strong></div>
      <pre>{{ JSON.stringify(block.result || block, null, 2) }}</pre>
    </div>

    <div v-else-if="block.type === 'proxy_result' && expanded" class="card-body proxy-result-body">
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <template v-else-if="block.kind === 'search'">
        <div class="muted">{{ block.matched }} / {{ block.total }}<template v-if="block.timed_out"> · ⏱</template></div>
        <ul class="proxy-rc-list">
          <li v-for="f in block.flows" :key="f.id" class="proxy-rc-flow">
            <span class="mono proxy-rc-method">{{ f.method }}</span>
            <span class="mono proxy-rc-status">{{ f.status }}</span>
            <span class="mono proxy-rc-url" :title="f.url">{{ f.url }}</span>
          </li>
        </ul>
      </template>
      <template v-else-if="block.kind === 'decode'">
        <ul class="proxy-rc-list">
          <li v-for="(it, i) in block.items" :key="i" class="proxy-rc-item">
            <span class="mono proxy-rc-loc">{{ it.part }}·{{ it.location || '&lt;body&gt;' }}</span>
            <span v-if="it.cipher_hint" class="proxy-rc-hint">{{ it.cipher_hint }}</span>
            <span v-else class="mono proxy-rc-preview">{{ it.preview }}</span>
          </li>
        </ul>
      </template>
      <template v-else-if="block.action === 'list'">
        <div class="muted">{{ (block.rules || []).length }}<template v-if="block.active"> · ●</template></div>
        <ul class="proxy-rc-list">
          <li v-for="(r, i) in block.rules" :key="i" class="proxy-rc-rule" :class="{ off: r.enabled === false }">
            <strong>{{ r.name }}</strong> <code class="mono">{{ r.match }}</code>
          </li>
        </ul>
      </template>
      <template v-else-if="block.action === 'test'">
        <div class="muted">{{ block.matched ? '✓' : '—' }}<template v-if="block.targets != null"> · {{ block.targets }}</template></div>
        <div v-if="block.hint" class="muted">{{ block.hint }}</div>
        <pre v-if="block.request_preview" class="mono">req: {{ (block.request_trace || []).join(' · ') }}
{{ block.request_preview }}</pre>
        <pre v-if="block.response_preview" class="mono">resp: {{ (block.response_trace || []).join(' · ') }}
{{ block.response_preview }}</pre>
      </template>
      <template v-else-if="block.action === 'delete'">
        <div class="muted">{{ block.removed ? '✕' : '—' }}</div>
      </template>
      <template v-else-if="block.rule">
        <div><strong>{{ block.rule.name }}</strong> · {{ block.rule.apply_to }}</div>
        <code class="mono">{{ block.rule.match }}</code>
      </template>
    </div>

    <div v-else-if="block.type === 'security_card' && expanded" class="card-body proxy-result-body">
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <template v-else>
        <div v-if="block.chips && block.chips.length" class="sec-chips">
          <span v-for="(c, i) in block.chips" :key="i" class="sec-chip" :class="`sev-${c.sev || 'info'}`">{{ c.label }}</span>
        </div>
        <ul class="proxy-rc-list">
          <li v-for="(r, i) in block.rows" :key="i" class="proxy-rc-flow">
            <span v-if="r.sev" class="sec-dot" :class="`sev-${r.sev}`"></span>
            <span class="mono proxy-rc-url" :title="r.a">{{ r.a }}</span>
            <span class="mono proxy-rc-loc">{{ r.b }}</span>
          </li>
        </ul>
      </template>
    </div>

    <div v-else-if="block.type === 'api_inventory' && expanded" class="card-body proxy-result-body">
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <template v-else-if="block.kind === 'diff'">
        <div class="muted">+{{ (block.added || []).length }} / -{{ (block.removed || []).length }} · ={{ block.unchanged }}</div>
        <ul class="proxy-rc-list">
          <li v-for="(a, i) in block.added" :key="'a'+i" class="mono proxy-rc-url" style="color: var(--ok)">+ {{ a }}</li>
          <li v-for="(r, i) in block.removed" :key="'r'+i" class="mono proxy-rc-url" style="color: var(--err)">- {{ r }}</li>
        </ul>
      </template>
      <template v-else>
        <div class="muted">{{ block.spec_kind ? block.spec_kind + ' · ' : '' }}{{ block.count }}</div>
        <ul class="proxy-rc-list">
          <li v-for="(e, i) in block.endpoints" :key="i" class="proxy-rc-flow">
            <span class="mono proxy-rc-method">{{ e.method }}</span>
            <span v-if="e.secured" class="proxy-rc-hint" title="auth">🔒</span>
            <span class="mono proxy-rc-url" :title="e.url">{{ e.url }}</span>
            <span v-for="tg in e.tags" :key="tg" class="proxy-rc-loc">{{ tg }}</span>
          </li>
        </ul>
      </template>
    </div>

    <div v-else-if="block.type === 'reverse_result' && expanded" class="card-body reverse-result-body">
      <div>
        <strong>{{ block.title || 'reverse' }}</strong>
        · {{ block.ok === false ? t('assistantCard.status.failed') : (block.partial ? t('assistantCard.status.partialCompletedAvailable') : t('assistantCard.status.completed')) }}
      </div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <div v-if="block.summary" class="muted">{{ typeof block.summary === 'string' ? block.summary : JSON.stringify(block.summary) }}</div>
      <div v-if="block.output_dir" class="path-row">
        <code>{{ block.output_dir }}</code>
        <button class="ghost tiny" type="button" @click="openReverseOutputDir(block.output_dir)">{{ t('assistantCard.action.openDecompileDirectory') }}</button>
        <button class="ghost tiny" @click="copy(block.output_dir)">{{ t('assistantCard.action.copyPath') }}</button>
      </div>
      <button
        v-if="block.job_id"
        class="ghost tiny"
        type="button"
        @click="openReverseJob(block.job_id)"
      >
        {{ t('assistantCard.action.openReverseWorkbench') }}
      </button>
      <div v-if="block.index_artifact_ref" class="muted small">{{ t('assistantCard.label.index') }}: {{ block.index_artifact_ref }}</div>
      <div v-if="block.next_step" class="muted small">{{ t('assistantCard.label.nextStep') }}: {{ block.next_step }}</div>
      <details v-if="block.blocked || block.do_not_retry" class="reverse-result-details">
        <summary>{{ t('assistantCard.label.details') }}</summary>
        <pre>{{ JSON.stringify({ blocked: block.blocked, do_not_retry: block.do_not_retry, job_id: block.job_id }, null, 2) }}</pre>
      </details>
      <details v-else-if="!block.output_dir" class="reverse-result-details">
        <summary>{{ t('assistantCard.label.rawJson') }}</summary>
        <pre>{{ JSON.stringify(block, null, 2) }}</pre>
      </details>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyResult') }}</button>
    </div>

    <div v-else-if="block.type === 'runtime_trace' && expanded" class="card-body">
      <div><strong>{{ block.title || 'runtime trace' }}</strong> · {{ block.final_url || block.url }}</div>
      <div>{{ t('assistantCard.metrics.runtimeTrace', { requests: (block.requests || []).length, traces: (block.trace || []).length, duration: block.elapsed_ms || 0 }) }}</div>
      <pre>{{ JSON.stringify(block, null, 2) }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyTrace') }}</button>
    </div>

    <div v-else-if="block.type === 'sandbox_file' && expanded" class="card-body">
      <div><strong>{{ block.title || 'session sandbox' }}</strong> · {{ block.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.completed') }}</div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <div v-if="block.directory"><strong>{{ t('assistantCard.label.directory') }}</strong> {{ block.directory }}</div>
      <div v-if="block.relative_path"><strong>{{ t('assistantCard.label.file') }}</strong> {{ block.relative_path }}</div>
      <div v-if="block.path" class="path-row">
        <span>{{ block.path }}</span>
        <button class="ghost tiny" @click="copy(block.path)">{{ t('assistantCard.action.copyPath') }}</button>
      </div>
      <div v-if="block.size !== undefined">{{ t('assistantCard.metrics.sizeBytes', { size: block.size }) }}</div>
      <div v-if="block.sha256">SHA256: {{ block.sha256 }}</div>
      <pre v-if="block.content">{{ block.content }}</pre>
      <pre v-else-if="block.files">{{ JSON.stringify(block.files, null, 2) }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyResult') }}</button>
    </div>

    <div v-else-if="block.type === 'sandbox_run' && expanded" class="card-body">
      <div><strong>{{ block.title || 'run' }}</strong> · {{ t('assistantCard.metrics.runResult', { exitCode: block.exit_code ?? '-', duration: block.duration_ms || 0 }) }}<span v-if="block.timeout_seconds">{{ t('assistantCard.sandbox.timeoutLimitSuffix', { seconds: block.timeout_seconds }) }}</span></div>
      <div v-if="block.timed_out" class="muted" style="color:#f0a020">
        {{ t('assistantCard.sandbox.timeoutKilled', { seconds: Math.round((block.duration_ms || 0) / 1000) }) }}
        <span v-if="block.partial_output">{{ t('assistantCard.sandbox.partialOutputPreserved') }}</span>
        <span v-else>{{ t('assistantCard.sandbox.noOutputProduced') }}</span>
        {{ t('assistantCard.sandbox.timeoutAdvice') }}
      </div>
      <div v-if="block.cwd"><strong>{{ t('assistantCard.label.workingDirectory') }}</strong> {{ block.cwd }}</div>
      <div v-if="block.path"><strong>{{ t('assistantCard.label.script') }}</strong> {{ block.path }}</div>
      <pre v-if="block.argv">{{ (block.argv || []).join(' ') }}</pre>
      <pre v-if="block.stdout_excerpt">{{ block.stdout_excerpt }}</pre>
      <pre v-if="block.stderr_excerpt">{{ block.stderr_excerpt }}</pre>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <button class="ghost tiny" @click="copy((block.stdout_excerpt || '') + (block.stderr_excerpt ? '\n' + block.stderr_excerpt : ''))">{{ t('assistantCard.action.copyOutput') }}</button>
    </div>

    <div v-else-if="block.type === 'sandbox_env' && expanded" class="card-body">
      <div><strong>{{ block.title || 'environment' }}</strong></div>
      <div v-if="block.platform">{{ t('assistantCard.label.platform') }}: {{ block.platform }}</div>
      <div v-for="p in (block.pythons || [])" :key="p.name + p.path" class="mini-row">
        <span>{{ p.name }}</span><span>{{ p.path || '-' }}</span><span>{{ p.version }}</span>
      </div>
      <div v-for="s in (block.shells || [])" :key="s.name + s.path" class="mini-row">
        <span>{{ s.name }}</span><span>{{ s.available ? s.path : t('assistantCard.status.unavailable') }}</span><span>{{ s.version }}</span>
      </div>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyResult') }}</button>
    </div>

    <div v-else-if="block.type === 'vulnerability_evidence' && expanded" class="card-body">
      <div><strong>{{ block.title || block.kind || 'vulnerability evidence' }}</strong> · {{ block.vulnerable || block.suspected ? t('assistantCard.status.signalFound') : (block.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.checked')) }}</div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <div v-if="(block.evidence || block).url"><strong>URL</strong> {{ (block.evidence || block).url }}</div>
      <div v-if="(block.evidence || block).payload"><strong>Payload</strong> {{ (block.evidence || block).payload }}</div>
      <div v-if="(block.evidence || block).confidence"><strong>{{ t('assistantCard.label.confidence') }}</strong> {{ (block.evidence || block).confidence }}</div>
      <pre v-if="(block.evidence || block).test_case">{{ (block.evidence || block).test_case }}</pre>
      <pre v-if="(block.evidence || block).response_excerpt">{{ (block.evidence || block).response_excerpt }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyEvidence') }}</button>
    </div>

    <div v-else-if="block.type === 'case_evidence' && expanded" class="card-body">
      <div><strong>{{ block.title || (block.record_type === 'artifact' ? t('assistantCard.default.caseArtifact') : t('assistantCard.default.caseEvidence')) }}</strong> · {{ block.ok === false ? t('assistantCard.status.archiveFailed') : t('assistantCard.status.archived') }}</div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <div v-if="block.case_id"><strong>Case</strong> {{ block.case_id }}</div>
      <div v-if="block.ref_id"><strong>{{ t('assistantCard.label.reference') }}</strong> {{ block.ref_id }}</div>
      <div v-if="block.kind"><strong>{{ t('assistantCard.label.type') }}</strong> {{ block.kind }}</div>
      <div v-if="block.confidence"><strong>{{ t('assistantCard.label.confidence') }}</strong> {{ block.confidence }}</div>
      <div v-if="block.path"><strong>{{ t('assistantCard.label.path') }}</strong> {{ block.path }}</div>
      <div v-if="block.summary" class="muted">{{ block.summary }}</div>
      <div v-if="(block.artifact_refs || []).length"><strong>{{ t('assistantCard.label.artifactReferences') }}</strong> {{ block.artifact_refs.join(', ') }}</div>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyArchiveInfo') }}</button>
    </div>

    <div v-else-if="block.type === 'request_replay' && expanded" class="card-body">
      <div><strong>{{ block.title || 'request replay' }}</strong> · {{ block.suspected || block.reflected ? t('assistantCard.status.suspected') : (block.ok === false ? t('assistantCard.status.failed') : t('assistantCard.status.completed')) }}</div>
      <div v-if="block.error" class="muted">{{ block.error }}</div>
      <div v-if="block.url"><strong>URL</strong> {{ block.url }}</div>
      <div v-if="block.header"><strong>Header</strong> {{ block.header }}: {{ block.value }}</div>
      <pre v-if="block.response_excerpt">{{ block.response_excerpt }}</pre>
      <pre v-else>{{ JSON.stringify(block, null, 2) }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyResult') }}</button>
    </div>

    <div v-else-if="block.type === 'attack_chain' && expanded" class="card-body">
      <div><strong>{{ block.title || 'attack chain' }}</strong></div>
      <div v-for="(step, idx) in (block.steps || [])" :key="idx" class="mini-row">
        <span>{{ idx + 1 }}</span><span>{{ typeof step === 'string' ? step : JSON.stringify(step) }}</span>
      </div>
      <pre v-if="!(block.steps || []).length">{{ JSON.stringify(block, null, 2) }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyAttackChain') }}</button>
    </div>

    <div v-else-if="block.type === 'user_todo_card'" class="card-body user-todo-card-body">
      <div v-if="block.message" class="user-todo-msg">{{ block.message }}</div>
      <div v-if="block.todo" class="user-todo-item">
        <strong>{{ block.todo.title }}</strong>
        <span class="user-todo-status">{{ block.todo.status }}</span>
        <p v-if="block.todo.body" class="user-todo-body">{{ block.todo.body }}</p>
        <div v-if="block.todo.tags?.length" class="user-todo-tags">
          <span v-for="tag in block.todo.tags" :key="tag" class="user-todo-tag">{{ tag }}</span>
        </div>
      </div>
      <button class="ghost tiny" type="button" @click="navigate('/user-todos')">{{ t('assistantCard.action.openPersonalTodos') }}</button>
    </div>

    <div v-else-if="block.type === 'user_todo_list'" class="card-body user-todo-list-body">
      <div class="user-todo-list-head">{{ t('assistantCard.todo.personalCount', { count: block.count ?? (block.items || []).length }) }}</div>
      <ul v-if="(block.items || []).length" class="user-todo-list">
        <li v-for="item in (block.items || []).slice(0, 12)" :key="item.id">
          <span class="user-todo-list-title">{{ item.title }}</span>
          <span class="user-todo-status">{{ item.status }}</span>
        </li>
      </ul>
      <button class="ghost tiny" type="button" @click="navigate('/user-todos')">{{ t('assistantCard.action.viewAll') }}</button>
    </div>

    <div v-else-if="block.type === 'desktop_control'" class="card-body desktop-control-body">
      <p v-if="block.message">{{ block.message }}</p>
      <p v-else-if="block.active">{{ t('assistantCard.desktop.controlEnabled') }}<span v-if="block.scope"> · {{ block.scope }}</span></p>
      <p v-else>{{ t('assistantCard.desktop.controlEnded') }}</p>
      <div v-if="macosPermMissing.length" class="macos-perm-notice">
        <p v-for="p in macosPermMissing" :key="p.id" class="macos-perm-line">
          <strong>{{ p.label }}</strong>: {{ p.hint }}
          <button class="ghost tiny" type="button" @click="openMacosPrivacy(p.id)">{{ t('assistantCard.action.openSettings') }}</button>
        </p>
      </div>
    </div>

    <div v-else-if="block.type === 'desktop_observe'" class="card-body desktop-observe-body">
      <p v-if="block.sensitive_warning" class="desktop-sensitive-warn">{{ block.sensitive_warning }}</p>
      <p v-if="block.permission_notice" class="macos-perm-line">{{ block.permission_notice }}</p>
      <p v-if="block.screenshot_error" class="desktop-screenshot-err">{{ block.screenshot_error }}</p>
      <div v-if="macosPermMissing.length" class="macos-perm-notice">
        <p v-for="p in macosPermMissing" :key="p.id" class="macos-perm-line">
          <strong>{{ p.label }}</strong>: {{ p.hint }}
          <button class="ghost tiny" type="button" @click="openMacosPrivacy(p.id)">{{ t('assistantCard.action.openSettings') }}</button>
        </p>
      </div>
      <p v-if="block.monitor_count"><strong>{{ t('assistantCard.label.displays') }}</strong> {{ t('assistantCard.desktop.displayCount', { count: block.monitor_count }) }}</p>
      <p v-if="block.coordinate_hint" class="desktop-coord-hint">{{ block.coordinate_hint }}</p>
      <p v-if="block.foreground?.title"><strong>{{ t('assistantCard.label.foregroundWindow') }}</strong> {{ block.foreground.title }}</p>
      <button v-if="block.screenshot_path && sessionId" class="ghost tiny" type="button" @click="openSandboxScreenshot(block.screenshot_path)">{{ t('assistantCard.action.viewScreenshot') }}</button>
    </div>

    <div v-else-if="block.type === 'oob_event' && expanded" class="card-body">
      <div><strong>{{ block.title || 'oob event' }}</strong> · {{ t('assistantCard.metrics.hitCount', { count: (block.hits || []).length }) }}</div>
      <div v-if="block.token"><strong>Token</strong> {{ block.token }}</div>
      <pre>{{ JSON.stringify(block.hits || block.events || block, null, 2) }}</pre>
      <button class="ghost tiny" @click="copy(JSON.stringify(block, null, 2))">{{ t('assistantCard.action.copyOobEvent') }}</button>
    </div>

    <div v-else-if="!collapsible || expanded" class="card-body">
      <p>{{ block.body || block.text || '' }}</p>
      <pre v-if="block.arguments && !isCompactConfirm">{{ JSON.stringify(block.arguments, null, 2) }}</pre>
    </div>

    <div v-if="decision" class="decision-state" :class="decision">
      {{ decision === 'allow' ? t('assistantCard.decision.allowedWaiting') : t('assistantCard.decision.deniedWaiting') }}
    </div>

    <div v-if="!decision && (block.confirm_value || block.cancel_value || block.actions?.length)" class="card-actions">
      <button v-for="a in (block.actions || [])" :key="a.label" class="ghost tiny" :disabled="actionsDisabled" @click="runAction(a)">
        {{ a.label }}
      </button>
      <button v-if="block.confirm_value" class="primary tiny" :disabled="actionsDisabled" @click="act(block.confirm_value)">{{ t('assistantCard.action.allow') }}</button>
      <button v-if="block.cancel_value" class="ghost danger tiny" :disabled="actionsDisabled" @click="act(block.cancel_value)">{{ t('assistantCard.action.deny') }}</button>
    </div>
  </div>
</template>

<style scoped>
.assistant-card {
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 12px;
  margin-top: 8px;
  background: var(--card-bg);
}
.assistant-card.variant-desktop_control {
  border-color: color-mix(in srgb, var(--warning, #e6a23c) 40%, var(--border));
}
.macos-perm-notice {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--warning, #e6a23c) 12%, var(--card-bg));
  border: 1px solid color-mix(in srgb, var(--warning, #e6a23c) 35%, var(--border));
}
.macos-perm-line {
  margin: 4px 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--muted);
}
.desktop-screenshot-err {
  margin: 4px 0;
  font-size: 12px;
  color: var(--danger, #c45656);
}
.assistant-card.quiet-confirm {
  max-width: min(420px, 100%);
  padding: 8px 12px;
  border-radius: 12px;
  margin-top: 6px;
}
.assistant-card.quiet-confirm .card-body p {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.45;
}
.assistant-card.quiet-confirm .card-actions {
  margin-top: 8px;
}
.assistant-card.desktop_observe.collapsed {
  display: inline-flex;
}
.desktop-sensitive-warn {
  font-size: 12px;
  color: var(--warning, #c9a227);
  margin: 0 0 8px;
}
.user-todo-card-body .user-todo-status,
.user-todo-list .user-todo-status {
  font-size: 11px;
  margin-left: 8px;
  color: var(--muted);
}
.user-todo-list { margin: 8px 0; padding-left: 18px; font-size: 13px; }
.user-todo-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--panel-2);
  font-size: 11px;
}

.assistant-card.confirm,
.assistant-card.command_plan {
  max-width: min(620px, 100%);
  padding: 10px 12px;
  border-radius: 12px;
}

.assistant-card.tool_skipped .tool-skipped-banner {
  margin-bottom: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent, #3b82f6) 12%, transparent);
  color: var(--text, #e5e7eb);
  font-size: 12px;
  font-weight: 600;
}
.assistant-card.tool_skipped .tool-skipped-banner.error {
  background: rgba(239, 68, 68, 0.12);
  color: #ef4444;
}
.tool-suggestion-list {
  margin: 8px 0 0;
  padding-left: 18px;
}
.tool-suggestion-list code {
  font-size: 12px;
}
.assistant-card.tool_skipped .tool-skipped-args {
  margin-top: 8px;
  max-height: 120px;
  overflow: auto;
  font-size: 11px;
}
/* --- Inline tool chips (tool_activity + collapsed tool_result) --------- */
/* Both chip variants share strict 22px height + 22px line-height so that
   when several end up in the same wrapping flex row their dots/text all
   sit on the same baseline. */
.assistant-card.tool_activity,
.assistant-card.collapsed {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: fit-content;
  max-width: min(100%, 280px);
  height: 22px;
  min-height: 22px;
  padding: 0;
  margin: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--muted-2);
  font-size: 11px;
  line-height: 22px;
  vertical-align: middle;
}
.assistant-card.tool_activity > *,
.assistant-card.collapsed > * {
  line-height: 22px;
}
.assistant-card.tool_activity .card-head {
  display: none;
}
.assistant-card.tool_activity .tool-activity-body {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  height: 22px;
  min-height: 22px;
  padding: 0;
  margin: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 11px;
  line-height: 22px;
}
.assistant-card.tool_activity .tool-activity-body code {
  padding: 0;
  margin: 0;
  color: color-mix(in srgb, var(--fg-strong) 86%, var(--muted));
  background: transparent;
  border: 0;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10.5px;
  line-height: 22px;
}
.assistant-card.tool_activity .activity-dot,
.assistant-card.collapsed .card-head::before {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 14%, transparent);
  align-self: center;
}
.assistant-card.tool_activity .activity-dot.done,
.assistant-card.collapsed .card-head::before {
  background: var(--ok);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--ok) 14%, transparent);
}
.assistant-card.tool_activity .activity-state {
  color: var(--muted-2);
  white-space: nowrap;
  height: 22px;
  line-height: 22px;
}
.assistant-card.collapsed .card-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 22px;
  line-height: 22px;
  padding: 0;
  margin: 0;
}
.assistant-card.collapsed .card-head::before {
  content: "";
}
.assistant-card.collapsed .card-title {
  max-width: 190px;
  padding: 0;
  margin: 0;
  color: color-mix(in srgb, var(--fg-strong) 86%, var(--muted));
  background: transparent;
  border: 0;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10.5px;
  line-height: 22px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.card-title {
  color: var(--fg-strong);
  font-weight: 700;
  font-size: 13px;
  min-width: 0;
}
.card-head + .card-body,
.collapsed-summary + .card-body {
  margin-top: 8px;
}
.collapse-toggle {
  flex: 0 0 auto;
}
.assistant-card.collapsed .collapse-toggle {
  display: inline-flex;
  align-items: center;
  padding: 0;
  margin: 0;
  min-height: 22px;
  height: 22px;
  border: 0;
  background: transparent;
  font-size: 10.5px;
  line-height: 22px;
  color: color-mix(in srgb, var(--accent) 78%, var(--muted));
  vertical-align: middle;
  box-shadow: none;
}
.assistant-card.collapsed .collapse-toggle:hover {
  background: transparent;
  transform: none;
  text-decoration: underline;
}
.collapsed-summary {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-card.collapsed .collapsed-summary {
  display: none;
}
.card-body {
  color: var(--fg);
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
pre {
  max-height: 220px;
  overflow: auto;
  padding: 10px;
  border-radius: 10px;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  white-space: pre-wrap;
  word-break: break-word;
}
.assistant-card.confirm pre,
.assistant-card.command_plan pre {
  max-height: 88px;
  padding: 8px;
  margin: 2px 0;
  font-size: 11px;
}
.assistant-card.confirm .card-actions,
.assistant-card.command_plan .card-actions {
  margin-top: 6px;
}
.mini-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
  flex-wrap: wrap;
}
.proxy-rc-list { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.proxy-rc-flow, .proxy-rc-item, .proxy-rc-rule { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.proxy-rc-method { color: var(--muted-2); flex: 0 0 auto; }
.proxy-rc-status { color: var(--muted); flex: 0 0 auto; }
.proxy-rc-url, .proxy-rc-preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg); }
.proxy-rc-loc { color: var(--muted); flex: 0 0 auto; }
.proxy-rc-hint { color: var(--warn); }
.proxy-rc-rule.off { opacity: 0.55; }
.proxy-result-body pre { max-height: 160px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
.sec-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px; }
.sec-chip { padding: 1px 7px; border-radius: 999px; font-size: 11px; background: var(--panel-2); color: var(--muted); }
.sec-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; background: var(--muted); }
.sec-chip.sev-critical, .sec-dot.sev-critical { background: color-mix(in srgb, var(--err) 80%, #000); color: #fff; }
.sec-chip.sev-high, .sec-dot.sev-high { background: color-mix(in srgb, var(--err) 20%, transparent); color: var(--err); }
.sec-dot.sev-high { background: var(--err); }
.sec-chip.sev-medium, .sec-dot.sev-medium { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
.sec-dot.sev-medium { background: var(--warn); }
.sec-chip.sev-low, .sec-dot.sev-low { background: color-mix(in srgb, var(--accent-3, var(--accent)) 18%, transparent); color: var(--accent-3, var(--accent)); }
.sec-dot.sev-low { background: var(--accent-3, var(--accent)); }

.path-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  word-break: break-all;
}
.sev {
  color: var(--accent);
  min-width: 52px;
  font-weight: 700;
}
.todo-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  color: var(--muted);
  font-size: 11px;
}
.todo-summary span {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--border);
}
.todo-scroll {
  max-height: clamp(220px, 42vh, 420px);
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.todo-row {
  display: grid;
  grid-template-columns: 16px 1fr;
  gap: 4px;
  align-items: start;
  padding: 3px 6px;
  margin-left: calc(var(--todo-level, 0) * 12px);
  border-radius: 6px;
  background: var(--panel-2);
  scroll-margin-block: 16px;
}
.todo-row.parent {
  background: color-mix(in srgb, var(--panel-2) 76%, var(--accent) 6%);
}
.todo-row.child {
  border-left: 2px solid var(--border);
}
.todo-collapse,
.todo-collapse-spacer {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
}
.todo-collapse {
  border: 0;
  padding: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 10px;
  line-height: 1;
}
.todo-collapse:hover {
  background: color-mix(in srgb, var(--fg) 7%, transparent);
  color: var(--fg);
}
.todo-status-pill {
  flex: 0 0 auto;
  padding: 0 5px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.5;
  color: var(--muted);
  background: color-mix(in srgb, var(--panel) 80%, transparent);
  border: 1px solid var(--border);
}
.todo-row.in_progress .todo-status-pill { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.todo-row.in_progress {
  background: var(--accent-soft);
  outline: 1px solid color-mix(in srgb, var(--accent) 24%, transparent);
}
.todo-row.completed .todo-status-pill { color: var(--ok); }
.todo-row.blocked .todo-status-pill,
.todo-row.cancelled .todo-status-pill { color: var(--err); }
.todo-main {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.todo-title-line {
  display: flex;
  gap: 4px;
  align-items: center;
  min-width: 0;
  line-height: 1.35;
}
.todo-id {
  flex: 0 1 auto;
  max-width: 88px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-size: 10px;
}
.todo-content {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--fg);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.todo-detail {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.todo-progress {
  flex: 0 0 auto;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 10px;
  line-height: 1.4;
}
.todo-ref-line {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.todo-chip,
.todo-ref-chip {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  padding: 0 5px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 75%, transparent);
  color: var(--muted);
  font-size: 10px;
  line-height: 1.4;
}
.todo-ref-chip {
  cursor: pointer;
}
.todo-ref-chip:hover {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
}
.todo-chip.risk {
  color: var(--warn);
}
.todo-chip.tool {
  color: var(--accent);
}
.todo-meta {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.3;
  white-space: pre-wrap;
  word-break: break-word;
}
.card-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.assistant-card.data_table {
  width: 100%;
  max-width: 100%;
  margin: 6px 0;
  padding: 0;
  background: transparent;
  border: 0;
}
.assistant-card .data-table-body {
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--panel-2) 78%, transparent);
}
.assistant-card .data-table-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.assistant-card .data-table-title {
  color: var(--fg-strong);
  font-weight: 700;
  font-size: 13px;
}
.assistant-card .data-table-subtitle {
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
}
.assistant-card .data-table-count {
  flex: 0 0 auto;
  padding: 2px 7px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 11px;
  background: var(--panel);
}
.assistant-card .data-table-scroll {
  max-width: 100%;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.assistant-card .data-table-scroll table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 12px;
}
.assistant-card .data-table-scroll th,
.assistant-card .data-table-scroll td {
  padding: 7px 9px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: middle;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-card .data-table-scroll th {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  background: color-mix(in srgb, var(--panel) 76%, transparent);
}
.assistant-card .data-table-scroll tr:last-child td {
  border-bottom: 0;
}
.assistant-card .data-table-action-col,
.assistant-card .data-table-action-cell {
  width: 180px;
  min-width: 180px;
  text-align: right;
}
.assistant-card .data-table-action-cell {
  overflow: visible;
  text-overflow: clip;
  white-space: normal;
}
.assistant-card .data-table-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 4px 6px;
}
.assistant-card .data-table-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  padding: 3px 9px;
  border-radius: 7px;
  border: 1px solid color-mix(in srgb, var(--accent) 48%, var(--border));
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: color-mix(in srgb, var(--accent) 88%, var(--fg-strong));
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease, transform 0.15s ease;
}
.assistant-card .data-table-action-btn:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 72%, var(--border));
  background: color-mix(in srgb, var(--accent) 28%, transparent);
  color: var(--fg-strong);
  transform: translateY(-1px);
}
.assistant-card .data-table-action-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  transform: none;
}
.assistant-card.structured_card {
  width: 100%;
  max-width: 100%;
  padding: 0;
  background: transparent;
  border: 0;
  margin: 6px 0;
}
.assistant-card .structured-card-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 18px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 30%, transparent);
  box-shadow: 0 2px 6px color-mix(in srgb, #000 8%, transparent);
  position: relative;
  overflow: hidden;
}
.assistant-card .structured-card-body::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--border);
}
.assistant-card .structured-card-body.variant-info::before {
  background: var(--accent);
}
.assistant-card .structured-card-body.variant-success::before {
  background: var(--ok, #22c55e);
}
.assistant-card .structured-card-body.variant-warning::before {
  background: var(--warn, #d97706);
}
.assistant-card .structured-card-body.variant-danger::before {
  background: var(--danger);
}
.assistant-card .structured-card-subtitle {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: -4px;
}
.assistant-card .structured-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.assistant-card .structured-section + .structured-section {
  padding-top: 16px;
  border-top: 1px dashed color-mix(in srgb, var(--border) 80%, transparent);
}
.assistant-card .structured-section-title {
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.3px;
}
.assistant-card .structured-text {
  margin: 0;
  color: var(--fg);
  font-size: 13px;
  line-height: 1.55;
  white-space: pre-wrap;
}
.assistant-card .structured-kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}
.assistant-card .structured-kv-item {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel-2) 40%, transparent);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.assistant-card .structured-kv-key {
  color: var(--muted);
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.assistant-card .structured-kv-value {
  color: var(--fg-strong);
  font-size: 13.5px;
  font-weight: 600;
  word-break: break-word;
}
.assistant-card .structured-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
}
.assistant-card .structured-metric {
  min-width: 0;
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  border-radius: 10px;
  background: var(--bg-sunken);
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
  box-shadow: inset 0 1px 2px color-mix(in srgb, #000 2%, transparent);
}
.assistant-card .structured-metric::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--accent);
  opacity: 0.7;
}
.assistant-card .structured-metric-value {
  color: var(--fg-strong);
  font-size: 24px;
  font-weight: 800;
  line-height: 1.2;
  font-family: "Cascadia Code", Consolas, monospace;
}
.assistant-card .structured-metric-label {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}
.assistant-card .structured-metric-note {
  margin-top: 4px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
}
.assistant-card .structured-list {
  margin: 0;
  padding-left: 18px;
  color: var(--fg);
  font-size: 13px;
  line-height: 1.55;
}
.assistant-card .structured-list li + li {
  margin-top: 4px;
}
.assistant-card .structured-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.assistant-card .structured-badge {
  padding: 4px 10px;
  border: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
  border-radius: 6px;
  color: var(--muted);
  background: color-mix(in srgb, var(--panel-2) 60%, transparent);
  font-size: 11.5px;
  font-weight: 600;
}
.assistant-card .structured-badge.variant-info {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
}
.assistant-card .structured-badge.variant-success {
  color: var(--ok, #22c55e);
  background: color-mix(in srgb, var(--ok, #22c55e) 12%, transparent);
  border-color: color-mix(in srgb, var(--ok, #22c55e) 30%, transparent);
}
.assistant-card .structured-badge.variant-warning {
  color: var(--warn, #d97706);
  background: color-mix(in srgb, var(--warn, #d97706) 12%, transparent);
  border-color: color-mix(in srgb, var(--warn, #d97706) 30%, transparent);
}
.assistant-card .structured-badge.variant-danger {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border-color: color-mix(in srgb, var(--danger) 30%, transparent);
}
.assistant-card .structured-steps {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.assistant-card .structured-step {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  position: relative;
  padding-bottom: 18px;
}
.assistant-card .structured-step:last-child {
  padding-bottom: 0;
}
.assistant-card .structured-step:not(:last-child)::before {
  content: "";
  position: absolute;
  top: 24px;
  bottom: 0;
  left: 11px;
  width: 2px;
  background: color-mix(in srgb, var(--border) 60%, transparent);
}
.assistant-card .structured-step-index {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
  line-height: 24px;
  text-align: center;
  z-index: 1;
  border: 2px solid var(--card-bg);
}
.assistant-card .structured-step-main {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
  padding-top: 2px;
}
.assistant-card .structured-step-title {
  color: var(--fg-strong);
  font-size: 13.5px;
  font-weight: 600;
}
.assistant-card .structured-step-body {
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.55;
}
.assistant-card .structured-step-status {
  display: inline-block;
  margin-top: 4px;
  padding: 3px 8px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  width: fit-content;
}
.assistant-card .structured-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 4px;
}
.assistant-card .structured-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 28px;
  padding: 6px 14px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--border));
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}
.assistant-card .structured-action-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  border-color: color-mix(in srgb, var(--accent) 70%, var(--border));
  color: color-mix(in srgb, var(--accent) 80%, var(--fg-strong));
  transform: translateY(-1px);
}
.assistant-card .structured-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.assistant-card.file_resource_view {
  width: 100%;
  max-width: 100%;
}
.assistant-card .file-resource-body {
  gap: 12px;
}
.assistant-card .file-resource-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.assistant-card .file-resource-subtitle {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}
.assistant-card .file-resource-root {
  margin-top: 3px;
  color: var(--fg-strong);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 11px;
}
.assistant-card .file-resource-count {
  flex: 0 0 auto;
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
}
.assistant-card .file-resource-layout {
  display: grid;
  grid-template-columns: minmax(220px, 34%) minmax(0, 1fr);
  height: clamp(320px, 56vh, 560px);
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--bg-sunken);
  box-shadow: 0 2px 6px color-mix(in srgb, #000 8%, transparent);
}

/* -- slim file_resource_view -------------------------------------------
   The post-refactor layout: no embedded preview / nav, just a tidy chip
   grid pointing into the side sandbox panel + the global media preview. */
.assistant-card .file-resource-body.slim .file-resource-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.assistant-card .file-resource-body.slim .file-resource-head-text {
  min-width: 0;
}
.assistant-card .file-resource-body.slim .file-resource-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.assistant-card .file-resource-body.slim .file-resource-browse {
  white-space: nowrap;
}
.assistant-card .file-resource-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 6px;
}
.assistant-card .file-resource-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: color-mix(in srgb, var(--panel) 64%, transparent);
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
  min-width: 0;
}
.assistant-card .file-resource-chip:hover {
  background: color-mix(in srgb, var(--panel-hover) 70%, transparent);
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
}
.assistant-card .file-resource-chip.dir {
  border-style: dashed;
}
.assistant-card .frc-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 30px;
  height: 18px;
  padding: 0 5px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.3px;
  color: #fff;
  border-radius: 3px;
  flex-shrink: 0;
}
.assistant-card .frc-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.assistant-card .frc-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-strong, var(--fg));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.assistant-card .frc-meta {
  font-size: 10px;
  color: var(--muted, var(--text-2));
}
.assistant-card .frc-actions {
  display: inline-flex;
  gap: 2px;
  flex-shrink: 0;
  margin-left: auto;
}
.assistant-card .frc-actions button {
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: 11px;
  line-height: 1;
}
.assistant-card .file-resource-list {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  border-right: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 62%, transparent);
}
.assistant-card .file-resource-nav {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  backdrop-filter: blur(8px);
}
.assistant-card .file-resource-nav-state,
.assistant-card .file-resource-nav-error {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-size: 11px;
}
.assistant-card .file-resource-nav-error {
  color: var(--danger);
}
.assistant-card .file-resource-row {
  width: 100%;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 10px 12px;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: transparent;
  color: var(--fg);
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease;
}
.assistant-card .file-resource-row:hover {
  background: color-mix(in srgb, var(--panel-2) 40%, transparent);
}
.assistant-card .file-resource-row.selected {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  position: relative;
}
.assistant-card .file-resource-row.selected::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent);
}
.assistant-card .file-resource-kind {
  width: 34px;
  padding: 2px 4px;
  border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
  border-radius: 5px;
  color: var(--accent);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 9px;
  font-weight: 800;
  text-align: center;
}
.assistant-card .file-resource-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.assistant-card .file-resource-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 650;
}
.assistant-card .file-resource-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10px;
}
.assistant-card .file-resource-size {
  color: var(--muted);
  font-size: 10px;
  white-space: nowrap;
}
.assistant-card .file-preview-pane {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--panel-2);
}
.assistant-card .file-preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 68%, transparent);
}
.assistant-card .file-preview-title {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.assistant-card .file-preview-title span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 700;
}
.assistant-card .file-preview-title small {
  color: var(--muted);
  font-size: 10px;
}
.assistant-card .file-preview-actions {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}
.assistant-card .file-preview-empty,
.assistant-card .file-preview-error,
.assistant-card .file-preview-note {
  padding: 12px;
  color: var(--muted);
  font-size: 12px;
}
.assistant-card .file-preview-empty,
.assistant-card .file-preview-error {
  flex: 1 1 auto;
}
.assistant-card .file-preview-error {
  color: var(--danger);
}
.assistant-card .file-preview-content {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.assistant-card .file-preview-content.mode-code,
.assistant-card .file-preview-content.mode-json,
.assistant-card .file-preview-content.mode-text {
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.assistant-card .file-preview-code {
  flex: 1 1 auto;
  width: 100%;
  height: 100%;
  min-height: 0;
  max-height: none;
  box-sizing: border-box;
  margin: 0;
  padding: 12px;
  border: 0;
  border-radius: 0;
  background: transparent;
  font-size: 11px;
  line-height: 1.55;
  overflow: auto;
  white-space: pre;
  tab-size: 2;
  word-break: normal;
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.assistant-card .file-preview-code code {
  display: block;
  min-width: 100%;
  cursor: text;
  user-select: text;
  -webkit-user-select: text;
}
.assistant-card .file-preview-code :deep(*) {
  user-select: text;
  -webkit-user-select: text;
}
.assistant-card .file-preview-code ::selection,
.assistant-card .file-preview-code :deep(*)::selection {
  background: color-mix(in srgb, var(--accent) 38%, transparent);
  color: var(--fg-strong);
}
.assistant-card .file-preview-markdown {
  flex: 1 1 auto;
  min-height: 0;
  padding: 12px 14px;
  overflow: auto;
  color: var(--fg);
  font-size: 12px;
  line-height: 1.6;
}
.assistant-card .file-preview-markdown :deep(pre) {
  max-height: none;
  white-space: pre;
}
.assistant-card .file-preview-image {
  position: relative;
  flex: 1 1 auto;
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 12px;
  overflow: auto;
  background: repeating-conic-gradient(color-mix(in srgb, var(--border) 20%, transparent) 0 25%, transparent 0 50%) 0 / 20px 20px;
}
.assistant-card .file-preview-image img {
  cursor: zoom-in;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  box-shadow: 0 2px 8px color-mix(in srgb, #000 10%, transparent);
  border-radius: 4px;
}
.assistant-card .file-preview-zoom {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 1;
  padding: 5px 9px;
  border: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--panel) 86%, transparent);
  color: var(--fg);
  font-size: 11px;
  cursor: pointer;
  backdrop-filter: blur(8px);
}
.assistant-card .file-image-modal {
  position: fixed;
  /* Leave space for the custom titlebar (see theme.css `--titlebar-h`). */
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 52px 28px 28px;
  background: color-mix(in srgb, #000 76%, transparent);
}
.assistant-card .file-image-modal-toolbar {
  position: absolute;
  top: 14px;
  left: 18px;
  right: 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: #fff;
  font-size: 12px;
}
.assistant-card .file-image-modal-toolbar span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-card .file-image-modal img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 6px;
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.45);
}
.assistant-card :deep(.syntax-comment) {
  color: var(--syntax-comment, color-mix(in srgb, var(--muted) 82%, transparent));
  font-style: italic;
}
.assistant-card :deep(.syntax-string) {
  color: var(--syntax-string, #7dd3fc);
}
.assistant-card :deep(.syntax-keyword) {
  color: var(--syntax-keyword, #c084fc);
  font-weight: 650;
}
.assistant-card :deep(.syntax-number) {
  color: var(--syntax-number, #fbbf24);
}
.assistant-card :deep(.syntax-function) {
  color: var(--syntax-function, var(--accent));
}
.assistant-card :deep(.syntax-class) {
  color: var(--syntax-class, var(--ok));
  font-weight: 650;
}
.assistant-card :deep(.syntax-builtin) {
  color: var(--syntax-builtin, var(--accent));
}
@media (max-width: 760px) {
  .assistant-card .file-resource-layout {
    grid-template-columns: 1fr;
    height: clamp(420px, 72vh, 640px);
  }
  .assistant-card .file-resource-list {
    max-height: 220px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
}
.decision-state {
  width: fit-content;
  margin-top: 10px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--muted);
  background: color-mix(in srgb, var(--panel-2) 58%, transparent);
}
.decision-state.allow {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.decision-state.deny {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
}
.tiny {
  padding: 4px 10px;
  font-size: 12px;
}
.tiny:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.assistant-card.collapsed .collapse-toggle.tiny {
  height: 14px;
  min-height: 0;
  padding: 0 0 0 1px;
  border: 0;
  background: transparent;
  font-size: 10px;
  line-height: 14px;
}

/* --- User decision card (stop_for_user_decision) ----------------------- */
.assistant-card.user_decision {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  flex-basis: 100%;
  align-self: stretch;
  margin: 6px 0;
  padding: 0;
  background: transparent;
  border: 0;
  box-sizing: border-box;
}
.assistant-card.user_decision_batch {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  flex-basis: 100%;
  align-self: stretch;
  margin: 6px 0;
  padding: 0;
  background: transparent;
  border: 0;
  box-sizing: border-box;
}
.assistant-card .user-decision-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  max-width: 100%;
  padding: 12px;
  font-size: 13px;
  border: 1px solid color-mix(in srgb, var(--warn) 42%, var(--border));
  background: color-mix(in srgb, var(--warn) 6%, var(--card-bg));
  border-radius: 10px;
  overflow: hidden;
  box-sizing: border-box;
}
.assistant-card .user-decision-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--warn);
  font-weight: 600;
}
.assistant-card .user-decision-icon {
  font-size: 13px;
}
.assistant-card .user-decision-question {
  font-weight: 650;
  font-size: 14px;
  line-height: 1.55;
  color: var(--fg-strong);
  word-break: break-word;
  overflow-wrap: anywhere;
}
.assistant-card .user-decision-context {
  font-size: 12px;
  color: var(--muted, #888);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.assistant-card .user-decision-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
  min-width: 0;
  max-width: 100%;
}
/* Neutral surface + accent-on-select. The card already carries the amber
   "needs you" signal; tinting the options amber too made every state look
   alike and killed the contrast of the chosen one. */
.assistant-card .user-decision-opt {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  min-height: 30px;
  padding: 6px 13px;
  border-radius: 15px;
  border: 1px solid var(--border-strong);
  background: var(--panel-2);
  color: var(--fg);
  font-size: 12px;
  line-height: 1.45;
  text-align: left;
  white-space: normal;
  word-break: break-word;
  overflow-wrap: anywhere;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}
.assistant-card .user-decision-opt::before {
  content: "";
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 1.5px solid var(--muted-2);
  transition: background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}
.assistant-card .user-decision-opt:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--fg-strong);
}
.assistant-card .user-decision-opt.selected {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.assistant-card .user-decision-opt.selected::before {
  border-color: var(--accent);
  background: var(--accent);
  box-shadow: inset 0 0 0 2px var(--card-bg);
}
.assistant-card .user-decision-opt.primary {
  background: var(--accent);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.assistant-card .user-decision-opt.primary::before {
  display: none;
}
.assistant-card .user-decision-opt.primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 86%, var(--fg-strong));
  color: #fff;
}
.assistant-card .user-decision-opt:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.assistant-card .user-decision-opt.selected:disabled {
  opacity: 1;
  cursor: default;
}
/* The chosen option stays highlighted above, so this is a quiet receipt
   rather than a second copy of the answer. */
.assistant-card .user-decision-submitted {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.assistant-card .submitted-label {
  flex: 0 0 auto;
  color: var(--ok);
  font-size: 11px;
  font-weight: 600;
}
.assistant-card .submitted-text {
  min-width: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
.assistant-card .user-decision-input {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 2px;
  min-width: 0;
  max-width: 100%;
}
.assistant-card .user-decision-input textarea {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  box-sizing: border-box;
  min-height: 40px;
  max-height: 120px;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 10px;
  background: var(--panel-2);
  color: var(--fg);
  outline: none;
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.assistant-card .user-decision-input textarea:focus {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.assistant-card .user-decision-input textarea:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.assistant-card .user-decision-input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.assistant-card .user-decision-input-actions .muted {
  font-size: 11px;
  color: var(--muted);
}
.assistant-card .user-decision-batch-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.assistant-card .user-decision-batch-question {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
  max-width: 100%;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel-2);
  overflow: hidden;
  box-sizing: border-box;
}
.assistant-card .batch-question-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.assistant-card .batch-question-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}
.assistant-card .batch-question-text {
  min-width: 0;
  flex: 1 1 auto;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.45;
}
.assistant-card .batch-question-required {
  flex: 0 0 auto;
  padding: 2px 6px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--warn) 34%, var(--border));
  color: var(--warn);
  font-size: 10px;
  font-weight: 600;
}
.assistant-card .batch-question-context {
  margin-left: 28px;
}
.assistant-card .batch-submit-row {
  margin-top: 2px;
}

/* --- Knowledge-base update review card -------------------------------- */
.assistant-card.kb_updated {
  width: min(620px, 100%);
  margin: 4px 0;
  padding: 0;
  background: transparent;
  border: 0;
  align-self: flex-start;
}
.assistant-card .kb-updated-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  font-size: 12px;
  cursor: pointer;
  border-left: 3px solid color-mix(in srgb, var(--accent) 65%, var(--panel-2));
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: 5px;
  transition: background 0.15s ease;
  max-width: 100%;
}
.assistant-card .kb-updated-body:hover {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
}
.assistant-card .kb-updated-head {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.assistant-card .kb-updated-body .kb-icon {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: color-mix(in srgb, var(--accent) 75%, var(--fg));
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  padding: 1px 4px;
  border-radius: 2px;
  flex-shrink: 0;
}
.assistant-card .kb-action-tag {
  font-size: 9.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  padding: 1px 5px;
  border-radius: 2px;
  background: color-mix(in srgb, var(--accent) 28%, transparent);
  color: var(--fg);
  flex-shrink: 0;
}
.assistant-card .kb-action-tag.deleted {
  background: color-mix(in srgb, var(--danger, #ff6363) 28%, transparent);
}
.assistant-card .kb-action-tag.superseded {
  background: color-mix(in srgb, var(--warn, #f0a020) 28%, transparent);
}
.assistant-card .kb-title {
  font-weight: 600;
  font-size: 13px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}
.assistant-card .kb-id-inline {
  font-family: ui-monospace, monospace;
  font-size: 10px;
  opacity: 0.65;
  flex-shrink: 0;
}
.assistant-card .kb-updated-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 12px;
  color: var(--muted);
}
.assistant-card .kb-updated-grid > div {
  min-width: 0;
  display: flex;
  gap: 6px;
}
.assistant-card .kb-updated-grid strong,
.assistant-card .kb-body-preview strong,
.assistant-card .kb-near-matches strong {
  color: var(--fg);
  font-weight: 600;
  flex-shrink: 0;
}
.assistant-card .kb-updated-grid span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-card .kb-body-preview p {
  margin: 4px 0 0;
  color: var(--muted);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.assistant-card .kb-near-matches {
  display: flex;
  flex-direction: column;
  gap: 3px;
  color: var(--muted);
}
.assistant-card .kb-updated-foot {
  color: var(--accent);
  font-size: 11px;
}
</style>
