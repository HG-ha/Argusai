<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { NSelect } from 'naive-ui'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import ListToolbar from './list/ListToolbar.vue'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListSelect from './list/ListSelect.vue'
import PageEmpty from './list/PageEmpty.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'

const confirmDialog = useConfirm()
const servers = ref([])
const loading = ref(false)
const saving = ref(false)
const testing = reactive({})
const health = reactive({})
const editingId = ref('')
const form = reactive(emptyForm())
const showForm = ref(false)
const showAdvanced = ref(false)
const probingAll = ref(false)
const startupProbe = ref(null)
const showRegistry = ref(false)
const registryQuery = ref('')
const registryServers = ref([])
const registryLoading = ref(false)
const registryError = ref('')
const registryNextCursor = ref('')
const registryPage = ref(1)
const registryMaxPages = ref(1)
const registryCursorHistory = ref([''])
const registryScrollEl = ref(null)
const registrySource = ref('modelscope')
const registryCategory = ref('')
const registryHosted = ref('')
const registryTotal = ref(0)
const registryImportingId = ref('')
const showTools = ref(false)
const toolsServer = ref(null)
const serverTools = ref([])
const toolsLoading = ref(false)
const toolsError = ref('')
const toolsQuery = ref('')
const toolsTotal = ref(0)
const toolsElapsedMs = ref(0)
let registryRequestSeq = 0
let toolsRequestSeq = 0
let pollTimer = null
let pollDeadline = 0

const enabledCount = computed(() => servers.value.filter(s => s.enabled !== false).length)
const startupProbeLabel = computed(() => {
  const probe = startupProbe.value
  if (!probe || typeof probe !== 'object') return ''
  if (probe.status === 'running' || probe.status === 'starting') return t('mcp.startupProbing')
  if (probe.status === 'failed') return t('mcp.startupProbeFailed')
  if (probe.status === 'done') {
    return t('mcp.startupProbeDone', { alive: Number(probe.alive || 0), dead: Number(probe.dead || 0) })
  }
  return ''
})

const transportFilterOptions = computed(() => [
  { label: t('tasks.filterAll'), value: '' },
  { label: 'stdio', value: 'stdio' },
  { label: 'HTTP', value: 'http' },
])
const statusFilterOptions = computed(() => [
  { label: t('mcp.allStatuses'), value: '' },
  { label: t('mcp.enabledYes'), value: 'enabled' },
  { label: t('mcp.enabledNo'), value: 'disabled' },
  { label: t('mcp.dead'), value: 'dead' },
])
const registrySources = computed(() => [
  {
    value: 'modelscope',
    label: t('mcp.marketSourceModelscope'),
    hint: t('mcp.marketSourceModelscopeHint'),
  },
  {
    value: 'official',
    label: t('mcp.marketSourceOfficial'),
    hint: t('mcp.marketSourceOfficialHint'),
  },
])
const registryCategoryOptions = computed(() => [
  { label: t('mcp.allCategories'), value: '' },
  { label: t('mcp.catDeveloperTools'), value: 'developer-tools' },
  { label: t('mcp.catSearch'), value: 'search' },
  { label: t('mcp.catBrowserAutomation'), value: 'browser-automation' },
  { label: t('mcp.catFileSystems'), value: 'file-systems' },
  { label: t('mcp.catKnowledge'), value: 'knowledge-and-memory' },
  { label: t('mcp.catResearch'), value: 'research-and-data' },
  { label: t('mcp.catDatabases'), value: 'databases' },
  { label: t('mcp.catFinance'), value: 'finance' },
  { label: t('mcp.catLocation'), value: 'location-services' },
  { label: t('mcp.catCommunication'), value: 'communication' },
])
const registryHostedOptions = computed(() => [
  { label: t('mcp.allRunModes'), value: '' },
  { label: t('mcp.runHosted'), value: 'hosted' },
  { label: t('mcp.runLocal'), value: 'local' },
])
const activeRegistrySource = computed(
  () => registrySources.value.find(item => item.value === registrySource.value) || registrySources.value[0],
)
const registryPageSize = 20
const registryPageCount = computed(() => {
  if (registrySource.value === 'modelscope') {
    const backendMax = Number(registryMaxPages.value || 0)
    if (backendMax > 0) return backendMax
    const cappedTotal = Math.min(Number(registryTotal.value || 0), 100)
    return Math.max(1, Math.ceil(cappedTotal / registryPageSize))
  }
  const known = registryCursorHistory.value.length
  return registryNextCursor.value ? Math.max(known, registryPage.value + 1) : Math.max(1, known)
})
const registryPageButtons = computed(() => {
  const total = Math.max(1, registryPageCount.value)
  const cur = Math.min(Math.max(1, registryPage.value), total)
  if (total <= 7) return Array.from({ length: total }, (_, idx) => idx + 1)
  const set = new Set([1, total, cur, cur - 1, cur + 1, cur - 2, cur + 2])
  const nums = [...set].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b)
  const out = []
  let prev = 0
  for (const n of nums) {
    if (prev && n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
})
const registryCanPrev = computed(() => registryPage.value > 1 && !registryLoading.value)
const registryCanNext = computed(() => {
  if (registryLoading.value) return false
  if (registrySource.value === 'modelscope') {
    return registryPage.value < registryPageCount.value
  }
  return Boolean(registryNextCursor.value) || registryPage.value < registryCursorHistory.value.length
})
const filteredServerTools = computed(() => {
  const query = toolsQuery.value.trim().toLowerCase()
  if (!query) return serverTools.value
  return serverTools.value.filter((tool) => {
    const haystack = `${tool.mcp_name || tool.name || ''} ${tool.description || ''}`.toLowerCase()
    return haystack.includes(query)
  })
})
const toolsModalDescription = computed(() => {
  const total = toolsTotal.value
  const shown = serverTools.value.length
  if (!total) return t('mcp.toolsHintEmpty')
  const bounded = total > shown ? t('mcp.toolsHintBounded', { shown }) : ''
  return `${t('mcp.toolsHint', { total })}${bounded}`
})

const {
  query: mcpQuery,
  filters: mcpFilters,
  page: mcpPage,
  pageSize: mcpPageSize,
  total: mcpFilteredTotal,
  pageCount: mcpPageCount,
  pageItems: mcpPageItems,
  selectedCount: mcpSelectedCount,
  selectedIds: mcpSelectedIds,
  isSelected: mcpIsSelected,
  toggle: mcpToggle,
  clearSelection: mcpClearSelection,
  selectPage: mcpSelectPage,
  selectAllFiltered: mcpSelectAllFiltered,
} = useClientList(
  computed(() => servers.value),
  (server, q, f) => {
    const tr = String(f.transport || '')
    const statusFilter = String(f.status || '')
    const st = String(server.transport || 'stdio')
    if (tr === 'stdio' && st !== 'stdio') return false
    if (tr === 'http' && st === 'stdio') return false
    if (statusFilter === 'enabled' && server.enabled === false) return false
    if (statusFilter === 'disabled' && server.enabled !== false) return false
    if (statusFilter === 'dead' && effectiveStatus(server) !== 'dead') return false
    if (!q) return true
    const hay = `${server.name || ''} ${server.id || ''} ${server.transport || ''} ${server.command || ''} ${server.url || ''}`.toLowerCase()
    return hay.includes(q)
  },
)
mcpFilters.value = { transport: '', status: '' }
mcpPageSize.value = 20

/**
 * Effective probe status for a server card. Priority:
 * 1. Local manual-test result for this session (most recent click).
 * 2. ``last_probe`` from backend cache (startup probe / previous test).
 * 3. ``unknown`` when neither is available.
 */
function effectiveStatus(server) {
  if (server.enabled === false) return 'disabled'
  if (testing[server.id]) return 'pending'
  const local = health[server.id]
  if (local && typeof local.ok === 'boolean') return local.ok ? 'alive' : 'dead'
  const backend = server.last_probe
  if (backend && typeof backend.status === 'string') return backend.status
  return 'unknown'
}

function statusLabel(server) {
  const s = effectiveStatus(server)
  if (s === 'alive') return t('mcp.alive')
  if (s === 'dead') return t('mcp.dead')
  if (s === 'pending') return t('mcp.probing')
  if (s === 'disabled') return t('mcp.disabled')
  return t('mcp.unknown')
}

function hasPending() {
  return startupProbe.value?.status === 'running'
    || servers.value.some(s => s.enabled !== false && (effectiveStatus(s) === 'pending' || effectiveStatus(s) === 'unknown'))
}

function emptyForm() {
  return {
    id: '',
    name: '',
    enabled: true,
    transport: 'stdio',
    // stdio
    command: '',
    args_text: '',
    cwd: '',
    env_text: '{}',
    // http / sse
    url: '',
    headers_text: '{}',
    // shared
    timeout_seconds: 20,
    notes: '',
    // policy
    policy_default_risk: 'high',
    policy_allow_auto_approve: false,
    policy_require_confirmation_for_all: true,
    policy_allow_insecure_http: false,
    policy_max_timeout_seconds: 60,
    policy_max_result_bytes: 200000,
    policy_allow_tools_text: '',
    policy_deny_tools_text: '',
    policy_tool_risks_text: '{}',
    provenance: null,
  }
}

function resetForm() {
  Object.assign(form, emptyForm())
  editingId.value = ''
  showForm.value = false
  showAdvanced.value = false
}

async function searchRegistry({ page = 1 } = {}) {
  const targetPage = Math.max(1, Number(page || 1))
  const requestSeq = ++registryRequestSeq
  registryLoading.value = true
  registryError.value = ''
  const source = registrySource.value
  if (targetPage === 1) {
    registryCursorHistory.value = ['']
  }
  try {
    let cursor = ''
    if (source === 'modelscope') {
      cursor = targetPage > 1 ? String(targetPage) : ''
    } else {
      cursor = String(registryCursorHistory.value[targetPage - 1] || '')
    }
    const r = await api.searchMcpMarketplace(
      source,
      registryQuery.value.trim(),
      cursor,
      registryPageSize,
      source === 'modelscope' ? registryCategory.value : '',
      source === 'modelscope' ? registryHosted.value : '',
    )
    if (requestSeq !== registryRequestSeq) return
    if (!r?.ok) throw new Error(r?.error || t('mcp.marketFailed'))
    const rows = Array.isArray(r.servers) ? r.servers : []
    registryServers.value = rows
    registryPage.value = targetPage
    const nextCursor = String(r.next_cursor || '')
    registryNextCursor.value = nextCursor
    if (source !== 'modelscope' && nextCursor && registryCursorHistory.value.length <= targetPage) {
      registryCursorHistory.value = [...registryCursorHistory.value.slice(0, targetPage), nextCursor]
    }
    registryTotal.value = Number.isFinite(Number(r.total)) ? Number(r.total) : 0
    registryMaxPages.value = Number.isFinite(Number(r.max_pages)) && Number(r.max_pages) > 0
      ? Number(r.max_pages)
      : (nextCursor ? Math.max(targetPage + 1, registryMaxPages.value) : targetPage)
    if (registryScrollEl.value) registryScrollEl.value.scrollTop = 0
  } catch (e) {
    if (requestSeq !== registryRequestSeq) return
    registryError.value = String(e?.message || e)
  } finally {
    if (requestSeq === registryRequestSeq) registryLoading.value = false
  }
}

function goRegistryPage(page) {
  const target = Math.max(1, Number(page || 1))
  if (target === registryPage.value || registryLoading.value) return
  if (registrySource.value !== 'modelscope' && target > registryCursorHistory.value.length) return
  void searchRegistry({ page: target })
}

async function openRegistry() {
  showRegistry.value = true
  if (!registryServers.value.length) await searchRegistry({ page: 1 })
}

async function selectRegistrySource(source) {
  if (source === registrySource.value) return
  registrySource.value = source
  registryCategory.value = ''
  registryHosted.value = ''
  registryServers.value = []
  registryNextCursor.value = ''
  registryPage.value = 1
  registryMaxPages.value = 1
  registryCursorHistory.value = ['']
  registryTotal.value = 0
  await searchRegistry({ page: 1 })
}

async function applyRegistryFilters() {
  registryServers.value = []
  registryNextCursor.value = ''
  registryPage.value = 1
  registryMaxPages.value = 1
  registryCursorHistory.value = ['']
  await searchRegistry({ page: 1 })
}

async function importRegistryServer(entry) {
  if (registryImportingId.value) return
  let candidate = entry?.config
  let importError = entry?.import_error
  registryImportingId.value = String(entry?.name || '')
  try {
    if ((!candidate || typeof candidate !== 'object') && entry?.catalog_source === 'modelscope') {
      const r = await api.getMcpMarketplaceEntry('modelscope', entry.name)
      if (!r?.ok) throw new Error(r?.error || t('mcp.modelscopeDetailFailed'))
      const detail = r.server
      candidate = detail?.config
      importError = detail?.import_error
      const index = registryServers.value.findIndex(item => item.name === entry.name)
      if (index >= 0 && detail) registryServers.value.splice(index, 1, detail)
    }
    if (!candidate || typeof candidate !== 'object') {
      throw new Error(
        importError
        || t('mcp.noImportable'),
      )
    }
    serverToForm(candidate)
    // Marketplace imports are new, disabled configs. Saving only records
    // settings; enable/test stays a separate high-risk action.
    editingId.value = ''
    form.enabled = false
    showRegistry.value = false
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  } finally {
    registryImportingId.value = ''
  }
}

function registryTransportLabel(entry) {
  if (entry?.catalog_source === 'modelscope') {
    if (entry?.config?.transport) return transportLabel(entry.config.transport)
    return entry?.is_hosted ? t('mcp.runHosted') : t('mcp.runLocal')
  }
  if (entry?.remotes?.some(item => item.secure)) return t('mcp.runRemoteHttps')
  const kinds = [...new Set((entry?.packages || []).map(item => item.registry_type).filter(Boolean))]
  return kinds.length ? kinds.join(' / ') : t('mcp.runMetadataOnly')
}

function openRegistryLink(entry) {
  const url = entry?.website_url || entry?.repository_url
  if (url) api.openExternalUrl(url)
}

function formatRegistryViews(value) {
  const count = Number(value || 0)
  if (count >= 10000) return t('mcp.heatWan', { n: (count / 1000).toFixed(count >= 100000 ? 0 : 1) })
  return t('mcp.heat', { n: count.toLocaleString() })
}

function registryCategoryLabel(value) {
  return registryCategoryOptions.value.find(item => item.value === value)?.label || value
}

function openNewForm() {
  Object.assign(form, emptyForm())
  editingId.value = ''
  showForm.value = true
  showAdvanced.value = false
}

function serverToForm(server) {
  editingId.value = server.id || ''
  const policy = server.policy || {}
  Object.assign(form, {
    id: server.id || '',
    name: server.name || '',
    enabled: server.enabled !== false,
    transport: server.transport || 'stdio',
    command: server.command || '',
    args_text: Array.isArray(server.args) ? server.args.join('\n') : '',
    cwd: server.cwd || '',
    env_text: JSON.stringify(server.env || {}, null, 2),
    url: server.url || '',
    headers_text: JSON.stringify(server.headers || {}, null, 2),
    timeout_seconds: Number(server.timeout_seconds || 20),
    notes: server.notes || '',
    policy_default_risk: policy.default_risk || 'high',
    policy_allow_auto_approve: !!policy.allow_auto_approve,
    policy_require_confirmation_for_all: policy.require_confirmation_for_all !== false,
    policy_allow_insecure_http: !!policy.allow_insecure_http,
    policy_max_timeout_seconds: Number(policy.max_timeout_seconds || 60),
    policy_max_result_bytes: Number(policy.max_result_bytes || 200000),
    policy_allow_tools_text: (policy.allow_tools || []).join('\n'),
    policy_deny_tools_text: (policy.deny_tools || []).join('\n'),
    policy_tool_risks_text: JSON.stringify(policy.tool_risks || {}, null, 2),
    provenance: server.provenance && typeof server.provenance === 'object'
      ? JSON.parse(JSON.stringify(server.provenance))
      : null,
  })
  showForm.value = true
  showAdvanced.value = !!(policy.allow_tools?.length || policy.deny_tools?.length || policy.allow_auto_approve)
}

function parseLines(text) {
  return String(text || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean)
}

function formPayload() {
  let env = {}
  try {
    env = JSON.parse(form.env_text || '{}')
    if (!env || Array.isArray(env) || typeof env !== 'object') throw new Error('env must be object')
  } catch (e) {
    throw new Error(t('mcp.envJsonInvalid'))
  }
  let headers = {}
  try {
    headers = JSON.parse(form.headers_text || '{}')
    if (!headers || Array.isArray(headers) || typeof headers !== 'object') throw new Error('headers must be object')
  } catch (e) {
    throw new Error(t('mcp.headersJsonInvalid'))
  }
  let toolRisks = {}
  try {
    toolRisks = JSON.parse(form.policy_tool_risks_text || '{}')
    if (!toolRisks || Array.isArray(toolRisks) || typeof toolRisks !== 'object') throw new Error('tool_risks must be object')
  } catch (e) {
    throw new Error(t('mcp.toolRisksJsonInvalid'))
  }
  const payload = {
    id: form.id,
    name: form.name,
    enabled: !!form.enabled,
    transport: form.transport,
    command: form.transport === 'stdio' ? form.command : '',
    args: form.transport === 'stdio' ? parseLines(form.args_text) : [],
    env: form.transport === 'stdio' ? env : {},
    cwd: form.transport === 'stdio' ? form.cwd : '',
    url: form.transport === 'stdio' ? '' : form.url,
    headers: form.transport === 'stdio' ? {} : headers,
    timeout_seconds: Number(form.timeout_seconds || 20),
    notes: form.notes,
    policy: {
      default_risk: form.policy_default_risk,
      allow_auto_approve: !!form.policy_allow_auto_approve,
      require_confirmation_for_all: !!form.policy_require_confirmation_for_all,
      allow_insecure_http: !!form.policy_allow_insecure_http,
      max_timeout_seconds: Number(form.policy_max_timeout_seconds || 60),
      max_result_bytes: Number(form.policy_max_result_bytes || 200000),
      allow_tools: parseLines(form.policy_allow_tools_text),
      deny_tools: parseLines(form.policy_deny_tools_text),
      tool_risks: toolRisks,
    },
  }
  if (form.provenance && typeof form.provenance === 'object') {
    payload.provenance = form.provenance
  }
  return payload
}

async function loadServers({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const r = await api.listMcpServers()
    if (!r || r.ok === false) throw new Error(r?.error || t('toast.error'))
    servers.value = Array.isArray(r.servers) ? r.servers : []
    startupProbe.value = r.startup_probe && typeof r.startup_probe === 'object' ? r.startup_probe : null
  } catch (e) {
    if (!silent) showToast(String(e?.message || e), 'error')
  } finally {
    if (!silent) loading.value = false
  }
}

function startPollingIfNeeded() {
  if (pollTimer) return
  // Keep badges live for larger MCP sets as well; probes are bounded on the
  // backend and this timer still self-terminates as soon as the batch ends.
  pollDeadline = Date.now() + 120_000
  const tick = async () => {
    await loadServers({ silent: true })
    if (Date.now() >= pollDeadline || !hasPending()) {
      clearInterval(pollTimer)
      pollTimer = null
      return
    }
  }
  pollTimer = setInterval(tick, 2000)
}

async function probeAll() {
  if (probingAll.value) return
  probingAll.value = true
  try {
    for (const server of servers.value) delete health[server.id]
    const r = await api.probeMcpServers()
    if (!r || r.ok === false) throw new Error(r?.error || t('toast.error'))
    await loadServers({ silent: true })
    startPollingIfNeeded()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  } finally {
    probingAll.value = false
  }
}

async function saveServer(opts = { andTest: false }) {
  saving.value = true
  let savedId = ''
  try {
    const payload = formPayload()
    const r = await api.saveMcpServer(payload)
    if (!r || r.ok === false) throw new Error(r?.error || t('toast.error'))
    savedId = r.server?.id || form.id
    showToast(
      r.marketplace_prepared
        ? t('mcp.npmPinned')
        : t(editingId.value ? 'mcp.updated' : 'mcp.created'),
      'ok',
    )
    if (!opts.andTest) {
      resetForm()
    } else {
      // Keep form open so the user sees the health update inline.
      if (r.marketplace_prepared && r.server) serverToForm(r.server)
      else {
        editingId.value = savedId
        form.id = savedId
      }
    }
    await loadServers()
    if (opts.andTest && savedId) {
      const server = servers.value.find(s => s.id === savedId)
      if (server) await testServer(server)
    }
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  } finally {
    saving.value = false
  }
}

async function setServerEnabled(server, enabled) {
  const r = await api.saveMcpServer({ ...server, enabled: !!enabled })
  if (!r || r.ok === false) throw new Error(r?.error || t('toast.error'))
  return r
}

async function toggleServerEnabled(server) {
  const enabling = server.enabled === false
  if (enabling && isRiskyServer(server)) {
    const ok = await confirmDialog({
      title: t('mcp.enableRiskyTitle'),
      content: t('mcp.enableRiskyContent', {
        name: server.name || server.id,
        risk: server.policy?.default_risk || t('mcp.unknown'),
        summary: permissionSummary(server),
      }),
      positiveText: t('mcp.enableAfterReview'),
      negativeText: t('common.cancel'),
      type: 'warning',
    })
    if (!ok) return
  }
  try {
    await setServerEnabled(server, enabling)
    await loadServers()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function deleteServer(server) {
  const ok = await confirmDialog({
    title: t('mcp.deleteTitle'),
    content: t('mcp.deleteConfirm', { name: server.name || server.id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  try {
    const r = await api.deleteMcpServer(server.id)
    if (!r || r.ok === false) throw new Error(r?.error || t('toast.error'))
    showToast(t('mcp.deleted'), 'ok')
    mcpToggle(server.id, false)
    if (editingId.value === server.id) resetForm()
    await loadServers()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

async function batchSetEnabled(enabled) {
  const ids = mcpSelectedIds.value
  if (!ids.length) return
  const selectedServers = ids.map(id => servers.value.find(server => server.id === id)).filter(Boolean)
  if (enabled) {
    const risky = selectedServers.filter(isRiskyServer)
    if (risky.length) {
      const ok = await confirmDialog({
        title: t('mcp.batchEnableRiskyTitle'),
        content: t('mcp.batchEnableRiskyContent', { n: risky.length }),
        positiveText: t('mcp.enableAfterReview'),
        negativeText: t('common.cancel'),
        type: 'warning',
      })
      if (!ok) return
    }
  }
  let okN = 0
  let fail = 0
  for (const id of ids) {
    const server = servers.value.find((s) => s.id === id)
    if (!server) { fail++; continue }
    try {
      await setServerEnabled(server, enabled)
      okN++
    } catch { fail++ }
  }
  mcpClearSelection()
  await loadServers()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
}

async function batchDeleteServers() {
  const ids = mcpSelectedIds.value
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('mcp.deleteTitle'),
    content: t('mcp.batchDeleteConfirm', { n: ids.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const r = await api.deleteMcpServer(id)
      if (r?.ok !== false) {
        okN++
        if (editingId.value === id) resetForm()
      } else fail++
    } catch { fail++ }
  }
  mcpClearSelection()
  await loadServers()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
}

async function testServer(server) {
  testing[server.id] = true
  try {
    const r = await api.testMcpServer(server.id)
    health[server.id] = r || {}
    showToast(r?.ok ? t('mcp.alive') : `${t('mcp.dead')}: ${r?.error || ''}`, r?.ok ? 'ok' : 'error')
    await loadServers({ silent: true })
  } catch (e) {
    health[server.id] = { ok: false, status: 'dead', error: String(e?.message || e) }
    showToast(String(e?.message || e), 'error')
  } finally {
    testing[server.id] = false
  }
}

async function loadServerTools() {
  const server = toolsServer.value
  if (!server?.id || toolsLoading.value) return
  const requestSeq = ++toolsRequestSeq
  toolsLoading.value = true
  toolsError.value = ''
  try {
    const result = await api.listMcpServerTools(server.id)
    if (requestSeq !== toolsRequestSeq) return
    if (!result?.ok) {
      throw new Error(result?.error || t('mcp.toolsListFailed'))
    }
    serverTools.value = Array.isArray(result.tools) ? result.tools : []
    toolsTotal.value = Number(result.total_count || serverTools.value.length)
    toolsElapsedMs.value = Number(result.elapsed_ms || 0)
  } catch (error) {
    if (requestSeq !== toolsRequestSeq) return
    toolsError.value = String(error?.message || error)
    serverTools.value = []
    toolsTotal.value = 0
    toolsElapsedMs.value = 0
  } finally {
    if (requestSeq === toolsRequestSeq) toolsLoading.value = false
  }
}

async function openServerTools(server) {
  toolsServer.value = server
  serverTools.value = []
  toolsQuery.value = ''
  toolsError.value = ''
  toolsTotal.value = 0
  toolsElapsedMs.value = 0
  showTools.value = true
  if (server.enabled === false) {
    toolsError.value = t('mcp.toolsDisabled')
    return
  }
  await loadServerTools()
}

function closeServerTools() {
  toolsRequestSeq += 1
  showTools.value = false
  toolsServer.value = null
  serverTools.value = []
  toolsQuery.value = ''
  toolsError.value = ''
  toolsLoading.value = false
}

function toolParameterEntries(tool) {
  const parameters = tool?.parameters
  if (!parameters || Array.isArray(parameters) || typeof parameters !== 'object') return []
  const required = new Set(Array.isArray(tool.required) ? tool.required : [])
  return Object.entries(parameters).map(([name, schema]) => ({
    name,
    schema: schema && typeof schema === 'object' ? schema : {},
    required: required.has(name),
  }))
}

function toolParameterType(schema) {
  const type = String(schema?.type || 'any')
  if (type === 'array' && schema?.items?.type) return `${schema.items.type}[]`
  return type
}

function toolRiskLabel(value) {
  if (value === 'low') return t('mcp.riskLow')
  if (value === 'medium-low') return t('mcp.riskMediumLow')
  if (value === 'medium') return t('mcp.riskMedium')
  return t('mcp.riskHigh')
}

function transportLabel(value) {
  return value === 'streamable_http'
    ? t('mcp.transportHttp')
    : value === 'sse'
      ? t('mcp.transportSse')
      : t('mcp.transportStdio')
}

function nodeSourceLabel(value) {
  if (value === 'managed') return t('mcp.runtimeVendor')
  if (value === 'vendor') return t('mcp.runtimeVendor')
  if (value === 'system') return t('mcp.runtimeSystem')
  if (value === 'user-bin') return t('mcp.runtimeUser')
  if (value === 'missing') return t('mcp.runtimeMissing')
  return t('mcp.runtimePassthrough')
}

function isRiskyServer(server) {
  const p = server?.policy || {}
  return String(p.default_risk || '').toLowerCase() === 'high'
    || p.allow_auto_approve === true
    || p.require_confirmation_for_all === false
    || p.allow_insecure_http === true
}

function permissionSummary(server) {
  const p = server?.policy
  if (!p || typeof p !== 'object') return t('mcp.unknown')
  const parts = [
    t('mcp.permissionRisk', { risk: p.default_risk || t('mcp.unknown') }),
    p.require_confirmation_for_all ? t('mcp.confirmEach') : t('mcp.notConfirmEach'),
  ]
  if (p.allow_auto_approve) parts.push(t('mcp.allowSessionAuth'))
  if (Array.isArray(p.allow_tools) && p.allow_tools.length) parts.push(t('mcp.allowCount', { n: p.allow_tools.length }))
  if (Array.isArray(p.deny_tools) && p.deny_tools.length) parts.push(t('mcp.denyCount', { n: p.deny_tools.length }))
  return parts.join(' · ')
}

function provenanceSource(server) {
  const p = server?.provenance
  if (!p || typeof p !== 'object') return t('mcp.unknown')
  if (p.source === 'github') return p.repository?.full_name ? `GitHub · ${p.repository.full_name}` : 'GitHub'
  if (p.source === 'npm') return p.package?.name ? `npm · ${p.package.name}` : 'npm'
  if (p.source === 'mcp_registry') return t('mcp.sourceOfficial')
  if (p.source === 'modelscope') return t('mcp.marketSourceModelscope')
  if (p.source === 'local') return t('mcp.sourceLocal')
  if (p.source === 'manual') return t('mcp.sourceManual')
  return p.source || t('mcp.unknown')
}

function detectedVersion(server) {
  const p = server?.provenance || {}
  return server?.server_info?.version
    || p.version
    || p.package?.version
    || p.release?.version
    || p.release?.tag
    || t('mcp.unknown')
}

function envSummary(server) {
  const keys = Array.isArray(server?.env_keys)
    ? server.env_keys
    : Object.keys(server?.env && typeof server.env === 'object' ? server.env : {})
  return keys.length ? keys.join(', ') : t('mcp.none')
}

function headerSummary(server) {
  const keys = Array.isArray(server?.header_keys)
    ? server.header_keys
    : Object.keys(server?.headers && typeof server.headers === 'object' ? server.headers : {})
  return keys.length ? keys.join(', ') : t('mcp.none')
}

function probeMeta(server) {
  const probe = server?.last_probe
  if (!probe || typeof probe !== 'object') return t('mcp.lastProbeNone')
  const time = probe.ts ? new Date(Number(probe.ts) * 1000).toLocaleString() : t('mcp.timeUnknown')
  const elapsed = Number.isFinite(Number(probe.elapsed_ms)) ? ` · ${probe.elapsed_ms} ms` : ''
  return t('mcp.lastProbe', { time, elapsed })
}

function probeError(server) {
  const local = health[server.id]
  if (local?.ok === false) return local
  const probe = server?.last_probe
  return probe?.status === 'dead' ? probe : null
}

function riskSummaryForForm() {
  const parts = [
    t('mcp.formRisk', { risk: form.policy_default_risk || t('mcp.unknown') }),
    form.policy_require_confirmation_for_all ? t('mcp.formConfirmAll') : t('mcp.formNotConfirmAll'),
  ]
  if (form.policy_allow_auto_approve) parts.push(t('mcp.formAutoApprove'))
  if (form.transport === 'stdio') {
    const keys = Object.keys(safeJsonObject(form.env_text))
    parts.push(keys.length ? t('mcp.formEnv', { keys: keys.join(', ') }) : t('mcp.formNoEnv'))
  } else if (form.policy_allow_insecure_http) {
    parts.push(t('mcp.formAllowHttp'))
  }
  return parts.join(' · ')
}

function safeJsonObject(text) {
  try {
    const value = JSON.parse(text || '{}')
    return value && !Array.isArray(value) && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

function onKeydown(event) {
  if (event.key === 'Escape' && showTools.value && !toolsLoading.value) closeServerTools()
  else if (event.key === 'Escape' && showForm.value && !saving.value) resetForm()
  else if (event.key === 'Escape' && showRegistry.value && !registryLoading.value) showRegistry.value = false
}

onMounted(async () => {
  await loadServers()
  // The Python side fires a startup probe in a daemon thread the
  // moment the GUI boots. By the time the user opens this panel the
  // probe may still be in flight — start polling so the badges turn
  // from "unknown" / "pending" into "alive" / "dead" on their own.
  if (hasPending()) startPollingIfNeeded()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="page list-page mcp-page">
    <div class="page-head">
      <div>
        <h1>{{ t('mcp.title') }}</h1>
        <p>
          {{ t('mcp.subtitle', { total: servers.length, enabled: enabledCount }) }}
          <span v-if="startupProbeLabel" class="mcp-startup-probe" :class="startupProbe?.status">
            · {{ startupProbeLabel }}
          </span>
        </p>
      </div>
      <div class="actions row-gap-sm">
        <button type="button" class="ghost" :disabled="loading || probingAll" @click="loadServers">{{ t('common.refresh') }}</button>
        <button type="button" class="ghost" :disabled="probingAll || !servers.length" @click="probeAll">
          {{ probingAll ? t('mcp.probingAll') : t('mcp.probeAll') }}
        </button>
        <button type="button" class="ghost registry-trigger" @click="openRegistry">{{ t('mcp.market') }}</button>
        <button type="button" class="primary" @click="openNewForm">{{ t('mcp.addServer') }}</button>
      </div>
    </div>

    <ListToolbar v-model="mcpQuery" :placeholder="t('common.search')">
      <template #filters>
        <ListSelect v-model="mcpFilters.transport" :options="transportFilterOptions" min-width="120px" />
        <ListSelect v-model="mcpFilters.status" :options="statusFilterOptions" min-width="120px" />
      </template>
    </ListToolbar>
    <div class="inline-note"><strong>{{ t('mcp.permissionBoundaryTitle') }}</strong>{{ t('mcp.permissionBoundaryBody') }}</div>
    <ListBatchBar
      :count="mcpSelectedCount"
      :total="mcpFilteredTotal"
      @select-page="mcpSelectPage(true)"
      @select-all="mcpSelectAllFiltered(true)"
      @clear="mcpClearSelection()"
    >
      <button type="button" class="ghost list-batch-btn" @click="batchSetEnabled(true)">{{ t('mcp.enabledYes') }}</button>
      <button type="button" class="ghost list-batch-btn" @click="batchSetEnabled(false)">{{ t('mcp.enabledNo') }}</button>
      <button type="button" class="danger list-batch-btn" @click="batchDeleteServers">{{ t('common.delete') }}</button>
    </ListBatchBar>
    <div v-if="loading" class="muted" style="padding: 20px; text-align: center">{{ t('common.loading') }}</div>
    <PageEmpty
      v-else-if="!mcpFilteredTotal"
      :title="servers.length ? t('common.noMatch') : t('mcp.empty')"
      :description="servers.length ? t('common.noMatchHint') : ''"
      :action-label="servers.length ? '' : t('mcp.addServer')"
      @action="openNewForm"
    />
    <template v-else>
      <div class="list-body mcp-server-list">
        <article
          v-for="server in mcpPageItems"
          :key="server.id"
          class="list-item mcp-server-card"
          :class="{ sel: mcpIsSelected(server.id), selected: mcpIsSelected(server.id) }"
        >
          <div class="mcp-server-head">
            <div class="mcp-server-identity">
              <div class="mcp-server-name">
                <input
                  class="list-row-check"
                  type="checkbox"
                  :checked="mcpIsSelected(server.id)"
                  @click.stop
                  @change="mcpToggle(server.id, $event.target.checked)"
                />
                {{ server.name || server.id }}
                <span class="mcp-transport-badge">{{ transportLabel(server.transport) }}</span>
                <span v-if="isRiskyServer(server)" class="mcp-risk-badge">{{ t('mcp.highRiskBadge') }}</span>
              </div>
              <code>{{ server.id }}</code>
              <div v-if="server.server_info?.name" class="mcp-real-name">
                <span class="muted">{{ t('mcp.serverInfoLabel') }}:</span>
                <code>{{ server.server_info.name }}<span v-if="server.server_info.version">@{{ server.server_info.version }}</span></code>
              </div>
            </div>
            <span :class="['mcp-status', effectiveStatus(server)]">
              <span v-if="effectiveStatus(server) === 'pending'" class="dot-spinner"></span>
              {{ statusLabel(server) }}
              <span v-if="server.last_probe?.elapsed_ms && effectiveStatus(server) === 'alive'" class="muted">
                ({{ server.last_probe.elapsed_ms }}ms)
              </span>
            </span>
          </div>

          <div v-if="server.transport === 'stdio'" class="mcp-command" :title="t('mcp.actualCommand')">
            <code>{{ server.command || t('mcp.unknownCommand') }}</code>
            <span v-for="arg in server.args || []" :key="arg">{{ arg }}</span>
          </div>
          <div v-else class="mcp-command">
            <code>{{ server.url || t('mcp.unknownUrl') }}</code>
          </div>

          <div class="mcp-service-facts">
            <span><small>{{ t('mcp.source') }}</small><b :title="provenanceSource(server)">{{ provenanceSource(server) }}</b></span>
            <span><small>{{ t('mcp.version') }}</small><b>{{ detectedVersion(server) }}</b></span>
            <span><small>{{ t('mcp.permissions') }}</small><b :title="permissionSummary(server)">{{ permissionSummary(server) }}</b></span>
            <span>
              <small>{{ server.transport === 'stdio' ? t('mcp.envVars') : t('mcp.authHeadersLabel') }}</small>
              <b :title="server.transport === 'stdio' ? envSummary(server) : headerSummary(server)">
                {{ server.transport === 'stdio' ? envSummary(server) : headerSummary(server) }}
              </b>
            </span>
          </div>

          <div class="mcp-meta">
            <span>{{ t('mcp.timeout') }}: {{ server.timeout_seconds || t('mcp.unknown') }}<template v-if="server.timeout_seconds">s</template></span>
            <span v-if="server.transport === 'stdio' && server.cwd">{{ t('mcp.cwd') }}: {{ server.cwd }}</span>
            <span>{{ probeMeta(server) }}</span>
          </div>

          <div v-if="health[server.id]?.runtime" class="mcp-runtime">
            <span v-if="health[server.id].runtime.node_source" :title="health[server.id].runtime.runtime_dir || ''">
              Node: {{ nodeSourceLabel(health[server.id].runtime.node_source) }}
            </span>
            <span v-if="health[server.id].runtime.url_origin">
              {{ t('mcp.endpointOrigin') }}: {{ health[server.id].runtime.url_origin }}
            </span>
            <span v-if="health[server.id].runtime.auth_header_keys?.length">
              {{ t('mcp.authHeaders') }}: {{ health[server.id].runtime.auth_header_keys.join(', ') }}
            </span>
          </div>

          <p v-if="server.notes" class="mcp-notes" :title="server.notes">{{ server.notes }}</p>
          <div v-if="probeError(server)" class="mcp-error">
            <div>
              <strong>{{ t('mcp.probeFailedTitle') }}</strong>
              <span>{{ probeError(server).error_code || 'unknown_error' }}: {{ probeError(server).error || t('mcp.unknownError') }}</span>
              <small v-if="probeError(server).suggested_fix">{{ probeError(server).suggested_fix }}</small>
            </div>
            <div class="mcp-error-actions">
              <button type="button" class="ghost small" :disabled="testing[server.id] || server.enabled === false" @click="testServer(server)">{{ t('mcp.retest') }}</button>
              <button type="button" class="ghost small" @click="serverToForm(server)">{{ t('mcp.editFix') }}</button>
            </div>
          </div>
          <div class="mcp-actions">
            <button
              type="button"
              class="ghost small mcp-tools-trigger"
              :disabled="toolsLoading && toolsServer?.id === server.id"
              :title="server.enabled === false ? t('mcp.toolsDisabledTitle') : t('mcp.toolsConnectTitle')"
              @click="openServerTools(server)"
            >
              {{ toolsLoading && toolsServer?.id === server.id ? t('mcp.readingTools') : t('mcp.toolsList') }}
            </button>
            <button type="button" class="ghost small" @click="serverToForm(server)">{{ t('mcp.edit') }}</button>
            <button type="button" class="ghost small" :disabled="testing[server.id] || server.enabled === false" @click="testServer(server)">
              {{ testing[server.id] ? t('mcp.testing') : t('mcp.test') }}
            </button>
            <button
              type="button"
              :class="[server.enabled === false && isRiskyServer(server) ? 'danger' : 'ghost', 'small']"
              @click="toggleServerEnabled(server)"
            >
              {{ server.enabled === false && isRiskyServer(server) ? t('mcp.reviewAndEnable') : (server.enabled === false ? t('mcp.enabledYes') : t('mcp.enabledNo')) }}
            </button>
            <button type="button" class="danger small" @click="deleteServer(server)">{{ t('common.delete') }}</button>
          </div>
        </article>
      </div>
      <ListPager
        v-model:page="mcpPage"
        v-model:page-size="mcpPageSize"
        :page-count="mcpPageCount"
        :total="mcpFilteredTotal"
      />
    </template>

    <WorkbenchModal
      :open="showTools"
      size="lg"
      :title="t('mcp.toolsModalTitle', { name: toolsServer?.name || toolsServer?.id || t('mcp.title') })"
      :description="toolsModalDescription"
      :busy="toolsLoading"
      :close-label="t('mcp.closeTools')"
      @close="closeServerTools"
    >
      <div class="mcp-tools-body">
        <div class="mcp-tools-toolbar">
          <input
            v-model="toolsQuery"
            type="search"
            :placeholder="t('mcp.searchToolsPlaceholder')"
            :aria-label="t('mcp.searchToolsAria')"
            :disabled="toolsLoading || !serverTools.length"
          />
          <span v-if="serverTools.length" class="mcp-tools-count">
            {{ t('mcp.showingTools', { shown: filteredServerTools.length, total: toolsTotal }) }}
            <template v-if="toolsElapsedMs"> · {{ toolsElapsedMs }}ms</template>
          </span>
          <button
            type="button"
            class="ghost small"
            :disabled="toolsLoading || toolsServer?.enabled === false"
            @click="loadServerTools"
          >
            {{ toolsLoading ? t('mcp.refreshingCatalog') : t('mcp.refreshCatalog') }}
          </button>
        </div>

        <div v-if="toolsError" class="mcp-tools-error">
          <div>
            <strong>{{ t('mcp.toolsUnavailable') }}</strong>
            <span>{{ toolsError }}</span>
          </div>
          <button
            v-if="toolsServer?.enabled !== false"
            type="button"
            class="ghost small"
            :disabled="toolsLoading"
            @click="loadServerTools"
          >
            {{ t('mcp.reread') }}
          </button>
        </div>
        <div v-else-if="toolsLoading && !serverTools.length" class="mcp-tools-empty">
          {{ t('mcp.connectingTools', { name: toolsServer?.name || toolsServer?.id }) }}
        </div>
        <div v-else-if="!serverTools.length" class="mcp-tools-empty">
          {{ t('mcp.noTools') }}
        </div>
        <div v-else-if="!filteredServerTools.length" class="mcp-tools-empty">
          {{ t('mcp.noMatchingTools') }}
        </div>
        <div v-else class="mcp-tools-list">
          <details
            v-for="tool in filteredServerTools"
            :key="tool.mcp_name || tool.name"
            class="mcp-tool-row"
          >
            <summary>
              <div class="mcp-tool-summary">
                <div class="mcp-tool-title">
                  <code>{{ tool.mcp_name || tool.name }}</code>
                  <span :class="['mcp-tool-risk', `risk-${tool.risk || 'high'}`]">
                    {{ toolRiskLabel(tool.risk) }}
                  </span>
                  <span v-if="tool.allowed === false" class="mcp-tool-blocked">{{ t('mcp.policyBlocked') }}</span>
                </div>
                <p>{{ tool.description || t('mcp.noToolDesc') }}</p>
              </div>
              <span class="mcp-tool-param-count">
                {{ t('mcp.paramCount', { n: toolParameterEntries(tool).length }) }}
              </span>
            </summary>
            <div class="mcp-tool-detail">
              <div v-if="toolParameterEntries(tool).length" class="mcp-tool-params">
                <div
                  v-for="parameter in toolParameterEntries(tool)"
                  :key="parameter.name"
                  class="mcp-tool-param"
                >
                  <div>
                    <code>{{ parameter.name }}</code>
                    <span>{{ toolParameterType(parameter.schema) }}</span>
                    <b v-if="parameter.required">{{ t('mcp.required') }}</b>
                  </div>
                  <p>{{ parameter.schema.description || t('mcp.noParamDesc') }}</p>
                </div>
              </div>
              <p v-else class="mcp-tool-no-params">{{ t('mcp.noParams') }}</p>
            </div>
          </details>
        </div>
        <div v-if="toolsTotal > serverTools.length" class="mcp-tools-limit">
          {{ t('mcp.toolsTruncated', { total: toolsTotal, shown: serverTools.length }) }}
        </div>
      </div>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showForm"
      size="lg"
      :title="editingId ? t('mcp.editServer') : t('mcp.addServer')"
      :description="t('mcp.transportHint')"
      :busy="saving"
      :close-label="t('common.close')"
      @close="resetForm"
    >
      <div class="form">
          <div class="row">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
              <label style="margin: 0;">{{ t('mcp.name') }}</label>
              <label class="mcp-switch" style="min-height: auto; margin: 0; font-size: 12px;">
                <input v-model="form.enabled" type="checkbox" /> 
                {{ form.enabled ? t('mcp.enabledYes') : t('mcp.enabledNo') }}
              </label>
            </div>
            <input v-model="form.name" type="text" :placeholder="t('mcp.namePlaceholder')" />
          </div>

          <div class="row">
            <label>{{ t('mcp.transport') }}</label>
            <NSelect
              v-model:value="form.transport"
              size="medium"
              :options="[
                { label: t('mcp.transportStdio'), value: 'stdio' },
                { label: t('mcp.transportHttp'), value: 'streamable_http' },
                { label: t('mcp.transportSse'), value: 'sse' },
              ]"
            />
            <p class="hint">{{ t('mcp.transportHint') }}</p>
          </div>

          <template v-if="form.transport === 'stdio'">
            <div class="row">
              <label>{{ t('mcp.command') }}</label>
              <input v-model="form.command" type="text" placeholder="npx" />
              <p class="hint">{{ t('mcp.commandHint') }}</p>
            </div>
            <div class="row">
              <label>{{ t('mcp.args') }}</label>
              <textarea v-model="form.args_text" rows="3" :placeholder="t('mcp.argsPlaceholder')"></textarea>
            </div>
            <div class="row split">
              <div>
                <label>{{ t('mcp.cwd') }}</label>
                <input v-model="form.cwd" type="text" :placeholder="t('mcp.cwdPlaceholder')" />
              </div>
              <div>
                <label>{{ t('mcp.timeout') }}</label>
                <input v-model.number="form.timeout_seconds" type="number" min="1" max="120" />
              </div>
            </div>
            <div class="row">
              <label>{{ t('mcp.env') }}</label>
              <textarea v-model="form.env_text" rows="3" placeholder='{"TOKEN":"..."}'></textarea>
              <p class="hint">{{ t('mcp.envHint') }}</p>
            </div>
          </template>

          <template v-else>
            <div class="row">
              <label>{{ t('mcp.url') }}</label>
              <input v-model="form.url" type="text" :placeholder="form.transport === 'sse' ? 'http://127.0.0.1:3000/sse' : 'https://mcp.example.com/mcp'" />
              <p class="hint">{{ t('mcp.urlHint') }}</p>
            </div>
            <div class="row split">
              <div>
                <label>{{ t('mcp.timeout') }}</label>
                <input v-model.number="form.timeout_seconds" type="number" min="1" max="120" />
              </div>
              <div style="display: flex; align-items: center; padding-top: 18px;">
                <label class="mcp-switch" style="margin: 0;">
                  <input v-model="form.policy_allow_insecure_http" type="checkbox" />
                  {{ t('mcp.allowInsecureHttp') }}
                </label>
              </div>
            </div>
            <div class="row">
              <label>{{ t('mcp.headers') }}</label>
              <textarea v-model="form.headers_text" rows="3" placeholder='{"Authorization":"Bearer ..."}'></textarea>
              <p class="hint">{{ t('mcp.headersHint') }}</p>
            </div>
          </template>

          <div class="row">
            <label>{{ t('mcp.notes') }}</label>
            <textarea v-model="form.notes" rows="2"></textarea>
          </div>

          <div class="row policy-toggle">
            <button class="ghost small" type="button" @click="showAdvanced = !showAdvanced">
              {{ showAdvanced ? t('mcp.hideAdvanced') : t('mcp.showAdvanced') }}
            </button>
          </div>

          <div v-if="showAdvanced" class="policy-block">
            <div class="row split">
              <div>
                <label>{{ t('mcp.policyDefaultRisk') }}</label>
                <NSelect
                  v-model:value="form.policy_default_risk"
                  size="medium"
                  :options="[
                    { label: 'high', value: 'high' },
                    { label: 'medium', value: 'medium' },
                    { label: 'medium-low', value: 'medium-low' },
                    { label: 'low', value: 'low' },
                  ]"
                />
              </div>
              <div>
                <label>{{ t('mcp.policyMaxTimeout') }}</label>
                <input v-model.number="form.policy_max_timeout_seconds" type="number" min="1" max="600" />
              </div>
            </div>
            <div class="row split">
              <div>
                <label>{{ t('mcp.policyMaxResultBytes') }}</label>
                <input v-model.number="form.policy_max_result_bytes" type="number" min="1024" max="2000000" step="1000" />
              </div>
              <div class="policy-flags">
                <label class="mcp-switch">
                  <input v-model="form.policy_require_confirmation_for_all" type="checkbox" />
                  {{ t('mcp.policyRequireConfirm') }}
                </label>
                <label class="mcp-switch">
                  <input v-model="form.policy_allow_auto_approve" type="checkbox" />
                  {{ t('mcp.policyAllowAutoApprove') }}
                </label>
              </div>
            </div>
            <div class="row split">
              <div>
                <label>{{ t('mcp.policyAllowTools') }}</label>
                <textarea v-model="form.policy_allow_tools_text" rows="3" placeholder="read_file"></textarea>
                <p class="hint">{{ t('mcp.policyAllowToolsHint') }}</p>
              </div>
              <div>
                <label>{{ t('mcp.policyDenyTools') }}</label>
                <textarea v-model="form.policy_deny_tools_text" rows="3" placeholder="write_file"></textarea>
                <p class="hint">{{ t('mcp.policyDenyToolsHint') }}</p>
              </div>
            </div>
            <div class="row">
              <label>{{ t('mcp.policyToolRisks') }}</label>
              <textarea v-model="form.policy_tool_risks_text" rows="3" placeholder='{"read_file":"medium","write_file":"high"}'></textarea>
              <p class="hint">{{ t('mcp.policyToolRisksHint') }}</p>
            </div>
          </div>

          <div class="mcp-security-summary">
            <strong>{{ t('mcp.riskSummaryTitle') }}</strong>
            <span>{{ riskSummaryForForm() }}</span>
          </div>

      </div>
      <template #footer>
        <button
          type="button"
          class="primary"
          :disabled="saving || !form.name || (form.transport === 'stdio' ? !form.command : !form.url)"
          @click="saveServer({ andTest: false })"
        >
          {{ saving ? t('common.loading') : t('mcp.save') }}
        </button>
        <button
          type="button"
          class="ghost"
          :disabled="saving || !form.name || (form.transport === 'stdio' ? !form.command : !form.url)"
          @click="saveServer({ andTest: true })"
        >
          {{ t('mcp.saveAndTest') }}
        </button>
        <button type="button" class="ghost" @click="resetForm">{{ t('common.cancel') }}</button>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showRegistry"
      size="lg"
      :title="t('mcp.market')"
      :description="`${activeRegistrySource.label} · ${t('mcp.marketDescription')}`"
      :busy="registryLoading"
      :close-label="t('mcp.marketClose')"
      @close="showRegistry = false"
    >
      <div class="registry-body">
        <div class="registry-top">
          <div class="registry-source-switch" role="tablist" :aria-label="t('mcp.marketSourceAria')">
            <button
              v-for="source in registrySources"
              :key="source.value"
              type="button"
              role="tab"
              class="registry-source"
              :class="{ active: registrySource === source.value }"
              :aria-selected="registrySource === source.value"
              :disabled="registryLoading && registrySource === source.value"
              @click="selectRegistrySource(source.value)"
            >
              <strong>{{ source.label }}</strong>
              <span>{{ source.hint }}</span>
            </button>
          </div>
          <div class="registry-search">
            <input
              v-model="registryQuery"
              name="mcp-marketplace-search"
              type="search"
              :placeholder="registrySource === 'modelscope' ? t('mcp.searchZh') : t('mcp.searchEn')"
              :aria-label="t('mcp.searchMarketAria')"
              @keydown.enter.prevent="searchRegistry({ page: 1 })"
            />
            <button type="button" class="primary" :disabled="registryLoading" @click="searchRegistry({ page: 1 })">
              {{ registryLoading ? t('mcp.searching') : t('mcp.searchAction') }}
            </button>
          </div>
          <div v-if="registrySource === 'modelscope'" class="registry-filters">
            <NSelect
              v-model:value="registryCategory"
              size="small"
              :options="registryCategoryOptions"
              :aria-label="t('mcp.categoryAria')"
              @update:value="applyRegistryFilters"
            />
            <NSelect
              v-model:value="registryHosted"
              size="small"
              :options="registryHostedOptions"
              :aria-label="t('mcp.runModeAria')"
              @update:value="applyRegistryFilters"
            />
          </div>
          <div class="registry-notice">
            <strong>{{ registrySource === 'modelscope' ? t('mcp.zhFirst') : t('mcp.standardMeta') }}</strong>
            <span>
              {{ registrySource === 'modelscope'
                ? t('mcp.modelscopeNote')
                : t('mcp.registryNote') }}
            </span>
          </div>
          <div class="registry-safety">
            {{ t('mcp.thirdPartyNote') }}
          </div>
          <div v-if="registryServers.length" class="registry-results-head">
            <span>{{ t('mcp.shownCount', { shown: registryServers.length }) }}<template v-if="registryTotal"> / {{ registryTotal.toLocaleString() }}</template></span>
            <span v-if="registrySource === 'modelscope'">{{ t('mcp.modelscopeCatalogNote') }}</span>
            <span v-else>{{ t('mcp.officialLatest') }}</span>
          </div>
        </div>

        <div ref="registryScrollEl" class="registry-scroll">
          <div v-if="registryError" class="registry-error">
            <span>{{ registryError }}</span>
            <button type="button" class="ghost small" @click="searchRegistry({ page: registryPage })">{{ t('common.retry') }}</button>
          </div>
          <div v-else-if="registryLoading && !registryServers.length" class="registry-loading">
            {{ t('mcp.readingSource', { label: activeRegistrySource.label }) }}
          </div>
          <div v-else-if="!registryServers.length" class="registry-empty">{{ t('mcp.noMatchingServers') }}</div>
          <div v-else class="registry-list">
            <article
              v-for="entry in registryServers"
              :key="`${entry.catalog_source}:${entry.name}@${entry.version}`"
              class="registry-row"
            >
              <div class="registry-row-main">
                <div class="registry-row-title">
                  <strong>{{ entry.title || entry.name }}</strong>
                  <span>{{ registryTransportLabel(entry) }}</span>
                  <span v-if="entry.version">v{{ entry.version }}</span>
                  <span v-if="entry.is_verified" class="verified">{{ t('mcp.verified') }}</span>
                </div>
                <code>{{ entry.name }}</code>
                <p>{{ entry.description }}</p>
                <div v-if="entry.categories?.length || entry.tags?.length" class="registry-tags">
                  <span v-for="category in (entry.categories || []).slice(0, 2)" :key="`c:${category}`">
                    {{ registryCategoryLabel(category) }}
                  </span>
                  <span v-for="tag in (entry.tags || []).slice(0, 2)" :key="`t:${tag}`">{{ tag }}</span>
                </div>
                <div class="registry-row-meta">
                  <span v-if="entry.view_count">{{ formatRegistryViews(entry.view_count) }}</span>
                  <span v-if="entry.publisher">{{ t('mcp.publisher', { name: entry.publisher }) }}</span>
                  <span v-if="entry.remotes?.length">{{ t('mcp.remoteCount', { n: entry.remotes.length }) }}</span>
                  <span v-if="entry.packages?.length">{{ t('mcp.packageCount', { n: entry.packages.length }) }}</span>
                  <span v-if="entry.updated_at">{{ t('mcp.updated', { date: new Date(entry.updated_at).toLocaleDateString() }) }}</span>
                </div>
              </div>
              <div class="registry-row-actions">
                <button
                  v-if="entry.website_url || entry.repository_url"
                  type="button"
                  class="ghost small"
                  @click="openRegistryLink(entry)"
                >
                  {{ t('mcp.viewSource') }}
                </button>
                <button
                  type="button"
                  class="ghost small registry-import"
                  :disabled="!!registryImportingId"
                  :title="t('mcp.importDisabledTitle')"
                  @click="importRegistryServer(entry)"
                >
                  {{ registryImportingId === entry.name ? t('mcp.readingDetail') : t('mcp.importConfig') }}
                </button>
              </div>
            </article>
          </div>
        </div>

        <div v-if="registryServers.length" class="list-pager registry-pager">
          <span class="list-pager-meta">
            {{ t('tasks.pageOf', { page: registryPage, pages: registryPageCount }) }}
            <template v-if="registryTotal"> · {{ registryTotal.toLocaleString() }}</template>
          </span>
          <div class="list-pager-pages">
            <button
              type="button"
              class="ghost list-pager-btn"
              :disabled="!registryCanPrev"
              @click="goRegistryPage(registryPage - 1)"
            >{{ t('common.prevPage') }}</button>
            <button
              v-for="(p, idx) in registryPageButtons"
              :key="`${p}-${idx}`"
              type="button"
              class="ghost list-pager-btn list-pager-num"
              :class="{ on: p === registryPage, ellipsis: p === '…' }"
              :disabled="p === '…' || registryLoading || (registrySource !== 'modelscope' && p > registryCursorHistory.length)"
              @click="typeof p === 'number' && goRegistryPage(p)"
            >{{ p }}</button>
            <button
              type="button"
              class="ghost list-pager-btn"
              :disabled="!registryCanNext"
              @click="goRegistryPage(registryPage + 1)"
            >{{ t('common.nextPage') }}</button>
          </div>
        </div>
      </div>
    </WorkbenchModal>
  </div>
</template>

<style scoped>
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 16px; }
.row { margin-bottom: 12px; }
.row.split { display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }
.row label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.row input:not([type="checkbox"]), .row select, .row textarea { width: 100%; box-sizing: border-box; }
.row textarea { resize: vertical; min-height: 72px; font-family: var(--font-mono, monospace); font-size: 12px; }
.mcp-switch { display: flex; align-items: center; gap: 8px; min-height: 36px; color: var(--text-muted); }
.policy-flags { display: flex; flex-direction: column; gap: 4px; }
.policy-toggle { margin-bottom: 4px; }
.policy-block { border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: rgba(127, 127, 127, 0.04); margin-bottom: 12px; }
.hint { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; }
.actions, .mcp-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.small { padding: 6px 10px; font-size: 12px; }
.danger { border: 1px solid rgba(239, 68, 68, 0.35); background: rgba(239, 68, 68, 0.10); color: #ef4444; border-radius: 10px; }
.mcp-server-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-content: start;
}
.mcp-server-card { border: 1px solid var(--border); border-radius: 10px; padding: 9px 12px; background: var(--card-bg, rgba(127, 127, 127, 0.04)); display: flex; flex-direction: column; box-shadow: none; }
.mcp-server-card.selected { border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); background: color-mix(in srgb, var(--accent) 8%, var(--card-bg, rgba(127, 127, 127, 0.04))); }
.mcp-server-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 5px; }
.mcp-server-identity { min-width: 0; }
.mcp-server-name { font-weight: 600; font-size: 13px; margin-bottom: 3px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mcp-transport-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(99, 102, 241, 0.14); color: #6366f1; font-weight: 600; }
.mcp-risk-badge {
  padding: 2px 7px; border-radius: 6px;
  color: var(--err); background: color-mix(in srgb, var(--err) 11%, transparent);
  font-size: 10px; font-weight: 600;
}
.mcp-status { border-radius: 999px; padding: 4px 9px; font-size: 12px; background: rgba(100, 116, 139, 0.16); color: var(--text-muted); white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; }
.mcp-status.alive { background: rgba(34, 197, 94, 0.16); color: #22c55e; }
.mcp-status.dead { background: rgba(239, 68, 68, 0.16); color: #ef4444; }
.mcp-status.pending { background: rgba(245, 158, 11, 0.16); color: #f59e0b; }
.mcp-status.unknown { background: rgba(100, 116, 139, 0.16); color: var(--text-muted); }
.mcp-status.disabled { background: rgba(148, 163, 184, 0.14); color: var(--text-muted); }
.mcp-status .muted { opacity: 0.65; font-size: 11px; }
.mcp-startup-probe.running,
.mcp-startup-probe.starting { color: var(--warn); }
.mcp-startup-probe.failed { color: var(--err); }
.mcp-startup-probe.done { color: var(--muted); }
.mcp-real-name { margin-top: 4px; display: flex; gap: 6px; align-items: center; font-size: 11px; }
.mcp-real-name code { font-size: 11px; background: rgba(34, 197, 94, 0.10); color: #22c55e; padding: 1px 6px; border-radius: 6px; }
.dot-spinner { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: mcpDotPulse 1s ease-in-out infinite; }
@keyframes mcpDotPulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }
.mcp-command { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 4px; color: var(--text-muted); }
.mcp-command span, .mcp-command code, .mcp-server-head code { font-size: 11px; background: rgba(100, 116, 139, 0.12); border-radius: 7px; padding: 1px 6px; word-break: break-all; }
.mcp-service-facts {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 5px;
  margin: 4px 0 5px;
}
@media (max-width: 1100px) {
  .mcp-service-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .registry-list {
    grid-template-columns: 1fr;
  }
}
.mcp-service-facts > span {
  min-width: 0; padding: 4px 7px; border-radius: 6px; background: var(--panel-2);
}
.mcp-service-facts small { display: block; color: var(--muted-2); font-size: 9px; }
.mcp-service-facts b {
  display: block; overflow: hidden; margin-top: 2px; color: var(--fg-strong);
  font-size: 10px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap;
}
.mcp-meta { display: flex; gap: 10px; flex-wrap: wrap; color: var(--text-muted); font-size: 11px; }
/* Secondary, less-critical detail — kept to one wrapped line so it reads
   as a muted footnote rather than another stacked block. */
.mcp-runtime { margin-top: 4px; display: flex; flex-direction: row; flex-wrap: wrap; gap: 3px 10px; font-size: 11px; color: var(--muted-2); }
.mcp-notes {
  margin: 5px 0 0; color: var(--text-muted); font-size: 11px;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mcp-error {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  margin: 6px 0 0; padding: 6px 9px; border-radius: 8px;
  color: var(--err); background: color-mix(in srgb, var(--err) 8%, transparent);
  font-size: 11px; word-break: break-word;
}
.mcp-error > div:first-child { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.mcp-error small { color: var(--muted); }
.mcp-error-actions { display: flex; flex: 0 0 auto; gap: 6px; }
.mcp-actions { margin-top: auto; padding-top: 8px; }
.mcp-tools-trigger {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
}
.mcp-tools-body { display: flex; min-height: 360px; flex-direction: column; gap: 10px; }
.mcp-tools-toolbar {
  display: grid; grid-template-columns: minmax(180px, 1fr) auto auto; align-items: center; gap: 8px;
}
.mcp-tools-toolbar input { min-width: 0; }
.mcp-tools-count { color: var(--muted-2); font-size: 10px; white-space: nowrap; }
.mcp-tools-error {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; border-radius: 9px;
  background: color-mix(in srgb, var(--err) 8%, var(--panel-2)); color: var(--err);
}
.mcp-tools-error > div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.mcp-tools-error span { color: var(--muted); font-size: 11px; word-break: break-word; }
.mcp-tools-empty { padding: 64px 16px; color: var(--muted); font-size: 12px; text-align: center; }
.mcp-tools-list {
  overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--panel);
}
.mcp-tool-row + .mcp-tool-row { border-top: 1px solid color-mix(in srgb, var(--border) 72%, transparent); }
.mcp-tool-row summary {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px;
  padding: 10px 12px; cursor: pointer; list-style-position: inside;
}
.mcp-tool-row summary::marker { color: var(--muted-2); }
.mcp-tool-row[open] summary { background: color-mix(in srgb, var(--accent) 4%, var(--panel)); }
.mcp-tool-summary { min-width: 0; }
.mcp-tool-title { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: 6px; }
.mcp-tool-title code {
  overflow: hidden; max-width: 100%; color: var(--fg-strong); font-size: 12px;
  font-weight: 600; text-overflow: ellipsis; white-space: nowrap;
}
.mcp-tool-title span { padding: 2px 6px; border-radius: 5px; font-size: 9px; font-weight: 600; }
.mcp-tool-risk { color: var(--muted); background: var(--panel-2); }
.mcp-tool-risk.risk-low { color: var(--ok); background: color-mix(in srgb, var(--ok) 9%, transparent); }
.mcp-tool-risk.risk-medium-low,
.mcp-tool-risk.risk-medium { color: var(--warn); background: color-mix(in srgb, var(--warn) 9%, transparent); }
.mcp-tool-risk.risk-high,
.mcp-tool-blocked { color: var(--err); background: color-mix(in srgb, var(--err) 9%, transparent); }
.mcp-tool-summary > p {
  overflow: hidden; margin: 4px 0 0; color: var(--muted); font-size: 11px;
  text-overflow: ellipsis; white-space: nowrap;
}
.mcp-tool-param-count { color: var(--muted-2); font-size: 10px; white-space: nowrap; }
.mcp-tool-detail {
  padding: 0 12px 12px; background: color-mix(in srgb, var(--panel-2) 65%, var(--panel));
}
.mcp-tool-params { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; padding-top: 10px; }
.mcp-tool-param { min-width: 0; padding: 7px 8px; border-radius: 7px; background: var(--panel); }
.mcp-tool-param > div { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: 6px; }
.mcp-tool-param code { color: var(--fg-strong); font-size: 11px; word-break: break-word; }
.mcp-tool-param span { color: var(--muted-2); font-size: 9px; }
.mcp-tool-param b {
  padding: 1px 5px; border-radius: 4px; color: var(--warn);
  background: color-mix(in srgb, var(--warn) 9%, transparent); font-size: 9px;
}
.mcp-tool-param p { margin: 4px 0 0; color: var(--muted); font-size: 10px; line-height: 1.45; }
.mcp-tool-no-params { margin: 0; padding-top: 10px; color: var(--muted); font-size: 11px; }
.mcp-tools-limit {
  padding: 7px 10px; border-left: 3px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 7%, var(--panel-2)); color: var(--muted); font-size: 10px;
}
.mcp-security-summary {
  display: flex; flex-direction: column; gap: 3px; margin-top: 12px; padding: 10px 12px;
  border-left: 3px solid var(--warn); background: color-mix(in srgb, var(--warn) 8%, var(--panel-2));
  color: var(--muted); font-size: 11px;
}
.mcp-security-summary strong { color: var(--fg-strong); }
.inline-note {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; margin: 0 0 8px;
  flex-shrink: 0;
  border: 1px solid color-mix(in srgb, var(--warn) 32%, var(--border));
  border-radius: 9px; background: color-mix(in srgb, var(--warn) 8%, var(--panel));
  color: var(--fg); font-size: 11px;
}
.inline-note strong { color: var(--warn); flex-shrink: 0; }
.registry-trigger { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.registry-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: min(640px, calc(100dvh - var(--titlebar-h, 0px) - 170px));
  min-height: 380px;
  overflow: hidden;
}
.registry-top {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
.registry-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 4px;
}
.registry-pager {
  flex: 0 0 auto;
  margin-top: 0;
  padding-top: 8px;
}
.registry-source-switch { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.registry-source {
  display: flex; min-width: 0; flex-direction: column; align-items: flex-start; gap: 2px;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 9px;
  background: var(--panel-2); color: var(--muted); text-align: left;
}
.registry-source strong { color: var(--fg); font-size: 12px; font-weight: 600; }
.registry-source span { font-size: 10px; }
.registry-source.active {
  border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
  background: color-mix(in srgb, var(--accent) 9%, var(--panel-2));
}
.registry-source.active strong { color: var(--accent); }
.registry-search { display: flex; gap: 8px; }
.registry-search input { flex: 1 1 auto; min-width: 0; }
.registry-search button { flex: 0 0 auto; }
.registry-filters { display: grid; grid-template-columns: repeat(2, minmax(0, 180px)); gap: 8px; }
.registry-notice {
  display: flex; align-items: baseline; gap: 8px; padding: 8px 10px;
  border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, var(--panel-2));
  color: var(--muted); font-size: 11px;
}
.registry-notice strong { flex: 0 0 auto; color: var(--accent); }
.registry-safety {
  color: var(--muted-2); font-size: 10px; line-height: 1.5;
}
.registry-error {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px; color: var(--err); background: color-mix(in srgb, var(--err) 8%, transparent);
  border-radius: 8px; font-size: 12px;
}
.registry-loading, .registry-empty { padding: 56px 16px; color: var(--muted); text-align: center; font-size: 12px; }
.registry-results-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  color: var(--muted-2); font-size: 10px;
}
.registry-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.registry-row {
  display: flex; min-width: 0; flex-direction: column; justify-content: space-between; gap: 10px;
  padding: 11px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--panel-2);
  transition: border-color 160ms ease, background 160ms ease;
}
.registry-row:hover {
  border-color: color-mix(in srgb, var(--accent) 25%, var(--border));
  background: color-mix(in srgb, var(--accent) 3%, var(--panel-2));
}
.registry-row-main { min-width: 0; flex: 1 1 auto; }
.registry-row-title { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.registry-row-title strong { color: var(--fg-strong); font-size: 13px; }
.registry-row-title span {
  padding: 1px 6px; border-radius: 5px; background: var(--panel-2);
  color: var(--muted); font-size: 10px; font-weight: 600;
}
.registry-row-title span.verified {
  color: var(--ok); background: color-mix(in srgb, var(--ok) 10%, transparent);
}
.registry-row code { display: block; margin-top: 3px; color: var(--muted-2); font-size: 10px; word-break: break-all; }
.registry-row p {
  display: -webkit-box; overflow: hidden; margin: 6px 0 0; color: var(--muted);
  font-size: 11px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
}
.registry-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.registry-tags span {
  overflow: hidden; max-width: 150px; padding: 2px 6px; border-radius: 5px;
  background: color-mix(in srgb, var(--accent) 7%, transparent); color: var(--muted);
  font-size: 9px; text-overflow: ellipsis; white-space: nowrap;
}
.registry-row-meta { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 6px; color: var(--muted-2); font-size: 10px; }
.registry-row-actions {
  display: flex; flex: 0 0 auto; justify-content: flex-end; gap: 6px;
  padding-top: 8px; border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
}
.registry-import { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); }
.registry-more { grid-column: 1 / -1; justify-self: center; margin: 4px 0 2px; }

/* Modal Overlay */
.modal-overlay {
  position: fixed;
  /* Leave space for the custom titlebar (see theme.css `--titlebar-h`). */
  top: var(--titlebar-h, 0);
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 40px 20px;
}
.modal-content {
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 0;
  box-shadow: var(--shadow-md);
}
.modal-content .panel-head {
  flex: 0 0 auto;
  margin: 0;
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  border-radius: 16px 16px 0 0;
}
.modal-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px 20px;
}
.modal-close-btn {
  background: transparent;
  border: none;
  font-size: 16px;
  padding: 4px 8px;
  cursor: pointer;
  color: var(--text-muted);
  border-radius: 6px;
}
.modal-close-btn:hover {
  background: var(--panel-hover);
  color: var(--fg);
}
@media (max-width: 820px) {
  .mcp-service-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .registry-list { grid-template-columns: 1fr; }
}
@media (max-width: 620px) {
  .mcp-server-head, .mcp-error { align-items: stretch; flex-direction: column; }
  .mcp-status { align-self: flex-start; }
  .mcp-service-facts { grid-template-columns: 1fr; }
  .mcp-error-actions { width: 100%; }
  .mcp-error-actions button { flex: 1 1 auto; }
  .mcp-tools-toolbar { grid-template-columns: 1fr auto; }
  .mcp-tools-count { grid-column: 1 / -1; grid-row: 2; }
  .mcp-tool-row summary { align-items: start; grid-template-columns: minmax(0, 1fr); }
  .mcp-tool-param-count { margin-left: 0; }
  .mcp-tool-params { grid-template-columns: 1fr; }
  .modal-overlay { padding: 8px; }
  .row.split { grid-template-columns: 1fr; }
  .registry-source-switch, .registry-filters { grid-template-columns: 1fr; }
  .registry-notice { align-items: stretch; flex-direction: column; }
  .registry-row-actions button { flex: 1 1 auto; }
  .modal-content { max-height: calc(100dvh - var(--titlebar-h, 0px) - 16px); }
}
</style>
