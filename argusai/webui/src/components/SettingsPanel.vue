<script setup>
import { ref, reactive, onMounted, onUnmounted, computed, watch } from 'vue'
import { NInput, NInputNumber, NSelect, NButton, NSwitch, NText, NSpace, NEmpty, NTag, NRadioGroup, NRadio, NAlert, NProgress } from 'naive-ui'
import { t } from '../i18n.js'
import { api } from '../api.js'
import { showToast } from '../state.js'
import { theme, setPreferred } from '../theme.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { navigate } from '../router.js'
import StorageDirField from './settings/StorageDirField.vue'
import AIProviderPicker from './settings/AIProviderPicker.vue'
import ListToolbar from './list/ListToolbar.vue'
import ListPager from './list/ListPager.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'
import { formatBytes, joinPath, pathSep } from './settings/settingsUtils.js'
import {
  TOOLCHAIN_PATH_KEYS,
  defaultToolchainPaths,
  mergeToolchainPaths,
  toolchainPathRows,
} from './settings/toolchainPaths.js'

const confirmDialog = useConfirm()

const DEFAULT_TOOL_ALWAYS_ALLOW = [
  'desktop_observe',
  'grep_text',
  'read_text_slice',
  'stat_text_file',
  'read_text_file',
  'list_assistant_sandbox_directory',
  'path_exists',
  'search_all_assistant_memory',
  'list_assistant_skills',
]
const DESKTOP_INPUT_ALWAYS_ALLOW_TOOLS = [
  'desktop_launch_app',
  'desktop_mouse_move',
  'desktop_click',
  'desktop_type',
  'desktop_hotkey',
]

function defaultAuxiliaryEp() {
  return {
    enabled: false,
    name: '',
    base_url: '',
    api_key: '',
    model: '',
    max_tokens: 4096,
    timeout: 120,
    temperature: 0.2,
    send_temperature: true,
    protocol: 'openai',
    extra_headers: {},
    extra_body: {},
  }
}

// Default storage paths supplied by the backend on load.
const defaultDirs = reactive({
  data_root: '',
  tasks_dir: '',
  sessions_dir: '',
  poc_pages_dir: '',
  knowledge_base_dir: '',
  reverse_jobs_dir: '',
  subdir_names: { tasks: 'tasks', sessions: 'assistant_sessions', poc_pages: 'poc_pages', knowledge_base: 'knowledge', reverse_jobs: 'reverse_jobs' },
})

const settings = reactive({
  ai_enabled: true,
  ai_endpoints: [],
  ai_auxiliary: {
    vision: defaultAuxiliaryEp(),
    asr: defaultAuxiliaryEp(),
  },
  data_root: '',
  kb_trash_retention_days: 30,
  security: { oob_callback_base: '', max_active_requests: 80, max_time_delay_seconds: 5, enable_protocol_tests: false },
  browser: { channel: '', executable_path: '', window_width: 1920, window_height: 1080 },
  toolchain: {
    paths: defaultToolchainPaths(),
  },
  runtime: {
    mirrors: { python: '', node: '', npm: '' },
  },
  assistant: {
    max_agent_rounds: 200,
    max_auto_continue: 10,
    max_toolless_continue_decisions: 0,
    max_budget_restarts: 10,
    round_signature_hard_stop: 4,
    tool_result_keep_last: 20,
    tool_result_summary_cap: 500,
    turn_deadline_seconds: 604800,
    desktop_notifications: true,
    cron_webhook_host: '127.0.0.1',
    cron_webhook_port: 8765,
    default_shell: 'auto',
    enable_web_search: true,
    team_max_parallel: 12,
    thinking_effort: 'high',
    auto_archive_tools: [],
    tool_always_allow: [...DEFAULT_TOOL_ALWAYS_ALLOW],
    auto_approve: false,
  },
  user_habits: {
    speaking_style: '',
    task_focus: '',
    audit_domains: [],
    risk_preference: '',
    output_format: '',
    avoid: '',
    notes: '',
  },
  logging: {
    level: 'INFO',
    assistant_level: 'DEBUG',
    backup_count: 10,
    max_bytes: 5 * 1024 * 1024,
    redact_payloads: true,
    log_dir: '',
  },
  document_parse: {
    max_output_chars: 200000,
    mineru: {
      enabled: false,
      api_key: '',
      base_url: 'https://mineru.net',
      model_version: 'vlm',
      timeout_seconds: 300,
    },
  },
})
// Independent from the loading spinner: no reactive form mutation performed
// during hydration may schedule an autosave, even if load() grows more awaits.
const settingsHydrating = ref(true)
const closeToTray = ref(true)

const loggingStatus = reactive({ log_dir: '', files: [] })
const loggingLevels = [
  { label: 'DEBUG', value: 'DEBUG' },
  { label: 'INFO', value: 'INFO' },
  { label: 'WARNING', value: 'WARNING' },
  { label: 'ERROR', value: 'ERROR' },
]
async function refreshLoggingStatus() {
  try {
    const r = await api.loggingStatus()
    if (!r || r.ok === false) return
    loggingStatus.log_dir = String(r.log_dir || '')
    loggingStatus.files = Array.isArray(r.files) ? r.files : []
  } catch (_) {
    // settings panel must still render even if the backend cannot reach
    // the logging subsystem for some reason (e.g. early bootstrap fault).
  }
}
async function openLogDir() {
  try {
    const r = await api.openLogDir()
    if (!r || r.ok === false) {
      showToast(String((r && r.error) || t('settings.logging.openFailed')), 'error')
    }
  } catch (e) {
    showToast(String((e && e.message) || e), 'error')
  }
}
const assistantToolOptions = ref([])
const assistantArchiveRecommendedTools = [
  'http_curl',
  'http_request_impersonate',
  'raw_http_send',
  'browser_use_tab',
  'browser_trace',
  'browser_probe',
  'sqli_probe_param',
]

const assistantTurnDeadlineOptions = computed(() => {
  const base = [
    { label: t('settings.assistantTurnDeadline12h'), value: 43200 },
    { label: t('settings.assistantTurnDeadline1d'), value: 86400 },
    { label: t('settings.assistantTurnDeadline3d'), value: 259200 },
    { label: t('settings.assistantTurnDeadline7d'), value: 604800 },
    { label: t('settings.assistantTurnDeadlineUnlimited'), value: 0 },
  ]
  const cur = Number(settings.assistant?.turn_deadline_seconds)
  if (Number.isFinite(cur) && !base.some(opt => opt.value === cur)) {
    const hours = Math.max(1, Math.round(cur / 3600))
    base.unshift({
      label: t('settings.assistantTurnDeadlineCustom', { hours }),
      value: cur,
    })
  }
  return base
})

// Derived sub-paths surfaced by the backend (read-only preview).
// Updated every time settings reload or data_root changes locally.
const derivedDirs = reactive({
  tasks_dir: '',
  sessions_dir: '',
  poc_pages_dir: '',
  knowledge_base_dir: '',
  reverse_jobs_dir: '',
})

// Committed root from last load / successful migrate. Draft edits to
// settings.data_root do not persist until apply_data_root_change runs.
const committedDataRoot = ref('')
const migrateModalOpen = ref(false)
const migrateMode = ref('copy')
const migrateBusy = ref(false)
const migratePreview = ref(null)
const migrateError = ref('')
const migrateProgress = ref(null)
let migrateProgressTimer = null

function resolvedDataRoot(val) {
  return String(val || '').trim() || defaultDirs.data_root || ''
}

function sameDataRoot(a, b) {
  const left = resolvedDataRoot(a).replace(/[\\/]+$/, '')
  const right = resolvedDataRoot(b).replace(/[\\/]+$/, '')
  if (!left && !right) return true
  if (pathSep(left) === '\\' || pathSep(right) === '\\') {
    return left.toLowerCase() === right.toLowerCase()
  }
  return left === right
}

const dataRootPending = computed(() => !sameDataRoot(settings.data_root, committedDataRoot.value))

// Managed reverse-tool installs always land under <current data_root>/tools/reverse/
// (see argusai/core/reverse/toolchain_install.py::managed_reverse_root) — show the
// resolved path instead of the literal "data_root" placeholder so it's unambiguous.
const toolchainInstallDir = computed(() => joinPath(joinPath(resolvedDataRoot(committedDataRoot.value), 'tools'), 'reverse'))

const migrateModeOptions = computed(() => ([
  { value: 'copy', label: t('settings.storage.migrateModeCopy'), hint: t('settings.storage.migrateModeCopyHint') },
  { value: 'move', label: t('settings.storage.migrateModeMove'), hint: t('settings.storage.migrateModeMoveHint') },
  { value: 'switch', label: t('settings.storage.migrateModeSwitch'), hint: t('settings.storage.migrateModeSwitchHint') },
]))

function migrateIssueText(issue) {
  const code = String(issue?.code || '')
  const keyByCode = {
    same_path: 'migrateIssueSamePath',
    dest_inside_source: 'migrateIssueDestInsideSource',
    source_inside_dest: 'migrateIssueSourceInsideDest',
    switch_destination_missing: 'migrateIssueSwitchMissing',
    switch_destination_not_directory: 'migrateIssueSwitchNotDirectory',
    dest_subdir_nonempty: 'migrateIssueDestNonempty',
    insufficient_space: 'migrateIssueInsufficientSpace',
    absolute_paths: 'migrateIssueAbsolutePaths',
    rewrite_destination_missing: 'migrateIssueRewriteMissing',
    log_dir_not_transferred: 'migrateIssueLogDirNotTransferred',
    in_use_runtime_files: 'migrateIssueInUseRuntimeFiles',
  }
  const key = keyByCode[code]
  if (!key) return issue?.message || t('settings.storage.migrateFailed')
  const rewrites = migratePreview.value?.path_rewrites || {}
  return t(`settings.storage.${key}`, {
    subdir: issue?.subdir || '',
    count: code === 'rewrite_destination_missing'
      ? Number(rewrites.missing_destination_count || 0)
      : Number(rewrites.count || 0),
  })
}

function migrateApplyErrorText(error) {
  const msg = String(error || '')
  if (!msg) return t('settings.storage.migrateFailed')
  if (/Permission denied|Errno 13|WinError 5|WinError 32/i.test(msg)) {
    return t('settings.storage.migrateIssuePermissionDenied')
  }
  if (msg.startsWith('data_root_busy:')) {
    return t('settings.storage.migrateIssueBusy')
  }
  return msg
}

const migrateProgressLabel = computed(() => {
  const phase = String(migrateProgress.value?.phase || 'preparing')
  const keyByPhase = {
    preparing: 'migrateProgressPreparing',
    copying: 'migrateProgressCopying',
    verifying: 'migrateProgressVerifying',
    publishing: 'migrateProgressPublishing',
    rewriting: 'migrateProgressRewriting',
    cleaning: 'migrateProgressCleaning',
    completed: 'migrateProgressCompleted',
    failed: 'migrateProgressFailed',
  }
  const label = t(`settings.storage.${keyByPhase[phase] || 'migrateProgressPreparing'}`)
  const subdir = String(migrateProgress.value?.current_subdir || '')
  return subdir ? `${label} · ${subdir}` : label
})

async function refreshMigrateProgress() {
  try {
    const result = await api.getDataRootChangeProgress()
    if (result?.ok && result.status !== 'idle') migrateProgress.value = result
  } catch {
    // The apply RPC remains authoritative; transient polling failures are harmless.
  }
}

function startMigrateProgressPolling() {
  if (migrateProgressTimer) clearInterval(migrateProgressTimer)
  migrateProgress.value = { status: 'running', phase: 'preparing', percent: 0 }
  migrateProgressTimer = setInterval(refreshMigrateProgress, 250)
}

function stopMigrateProgressPolling() {
  if (migrateProgressTimer) {
    clearInterval(migrateProgressTimer)
    migrateProgressTimer = null
  }
}

// Recompute the derived paths whenever the user edits data_root so the
// read-only preview reflects what the runtime will actually use.
// We don't wait for save — the user shouldn't have to "commit" to see
// the effect.
watch(() => settings.data_root, (val) => {
  const root = resolvedDataRoot(val)
  if (!root) return
  const names = defaultDirs.subdir_names || {}
  derivedDirs.tasks_dir = joinPath(root, names.tasks || 'tasks')
  derivedDirs.sessions_dir = joinPath(root, names.sessions || 'assistant_sessions')
  derivedDirs.poc_pages_dir = joinPath(root, names.poc_pages || 'poc_pages')
  derivedDirs.knowledge_base_dir = joinPath(root, names.knowledge_base || 'knowledge')
  derivedDirs.reverse_jobs_dir = joinPath(root, names.reverse_jobs || 'reverse_jobs')
})

async function openSubdir(path) {
  if (!path) return
  try {
    const r = await api.openFolder(path)
    if (!r || r.ok === false) {
      showToast(t('settings.storage.openFailed'), 'error')
    }
  } catch (e) {
    showToast(String((e && e.message) || e), 'error')
  }
}

function revertDataRootDraft() {
  settings.data_root = committedDataRoot.value
  migrateError.value = ''
  migratePreview.value = null
}

function closeMigrateModal() {
  if (migrateBusy.value) return
  migrateModalOpen.value = false
}

async function openDataRootMigrateModal() {
  if (!dataRootPending.value || migrateBusy.value) return
  migrateError.value = ''
  migrateBusy.value = true
  migrateMode.value = 'copy'
  try {
    const preview = await api.previewDataRootChange(resolvedDataRoot(settings.data_root))
    migratePreview.value = preview || null
    if (preview && preview.ok === false && Array.isArray(preview.blockers) && preview.blockers.length) {
      // Still open the modal so the user can see blockers / pick switch after fixing dest.
    }
    migrateModalOpen.value = true
  } catch (e) {
    migrateError.value = String((e && e.message) || e)
    showToast(migrateError.value, 'error')
  } finally {
    migrateBusy.value = false
  }
}

async function confirmDataRootMigrate() {
  if (!dataRootPending.value || migrateBusy.value) return
  const mode = migrateMode.value || 'copy'
  if (mode !== 'switch') {
    const blockers = migratePreview.value?.blockers || []
    if (blockers.length) {
      migrateError.value = migrateIssueText(blockers[0])
      return
    }
    const conflicts = (migratePreview.value?.subdirs || []).filter((row) => row.conflict)
    if (conflicts.length) {
      migrateError.value = t('settings.storage.migrateDestConflict')
      return
    }
  }
  const modeLabel = (migrateModeOptions.value.find((opt) => opt.value === mode) || {}).label || mode
  const ok = await confirmDialog({
    title: t('settings.storage.migrateConfirmTitle'),
    content: t('settings.storage.migrateConfirmBody', {
      mode: modeLabel,
      source: migratePreview.value?.source || resolvedDataRoot(committedDataRoot.value),
      dest: resolvedDataRoot(settings.data_root),
    }),
    positiveText: t('settings.storage.migrateApply'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  migrateBusy.value = true
  migrateError.value = ''
  startMigrateProgressPolling()
  try {
    const result = await api.applyDataRootChange(resolvedDataRoot(settings.data_root), mode)
    if (!result?.ok) {
      throw new Error(migrateApplyErrorText(result?.error))
    }
    committedDataRoot.value = result.destination || resolvedDataRoot(settings.data_root)
    settings.data_root = committedDataRoot.value
    if (result.settings) {
      derivedDirs.tasks_dir = result.settings.tasks_dir || derivedDirs.tasks_dir
      derivedDirs.sessions_dir = result.settings.sessions_dir || derivedDirs.sessions_dir
      derivedDirs.poc_pages_dir = result.settings.poc_pages_dir || derivedDirs.poc_pages_dir
      derivedDirs.knowledge_base_dir = result.settings.knowledge_base_dir || derivedDirs.knowledge_base_dir
      derivedDirs.reverse_jobs_dir = result.settings.reverse_jobs_dir || derivedDirs.reverse_jobs_dir
      if (result.settings.toolchain?.paths) {
        settings.toolchain.paths = mergeToolchainPaths(result.settings.toolchain.paths)
      }
      if (result.settings.logging) {
        settings.logging = { ...settings.logging, ...result.settings.logging }
      }
    }
    lastSavedSignature = settingsPayloadSignature(buildSettingsPayload())
    autoSaveState.value = 'saved'
    migrateModalOpen.value = false
    const rewriteCount = (result.path_rewrites || []).length
    showToast(
      rewriteCount
        ? t('settings.storage.migrateDoneRewrites', { count: rewriteCount })
        : t('settings.storage.migrateDone'),
      'ok',
    )
    showToast(t('settings.storage.migrateRestart'), 'info')
  } catch (e) {
    migrateError.value = migrateApplyErrorText((e && e.message) || e)
    showToast(migrateError.value, 'error')
  } finally {
    await refreshMigrateProgress()
    stopMigrateProgressPolling()
    migrateBusy.value = false
  }
}
const loading = ref(true)
const autoSaveReady = ref(false)
const autoSaveState = ref('idle')
const autoSaveError = ref('')
let autoSaveTimer = null
let autoSaveInFlight = false
let autoSaveQueued = false
let lastSavedSignature = ''
let lastSavedPayload = null
let settingsVersion = ''
const pendingAuxiliaryKeyClears = new Set()
let pendingMineruKeyClear = false
const mineruModelOptions = [
  { label: 'vlm', value: 'vlm' },
  { label: 'pipeline', value: 'pipeline' },
]
const testingId = ref(null)
const modelCache = reactive({})
const endpointHealth = ref([])
const selectedEndpointIndex = ref(null)
const endpointEditorOpen = ref(false)
const providerPickerOpen = ref(false)
// A newly added endpoint stays local to the editor until the user explicitly
// saves it. It is excluded from routing and settings autosave in the meantime.
const draftEndpointIndex = ref(null)

// Provider preset catalog (loaded once from the read-only backend endpoint).
const providerPresets = ref([])
const providerCategories = ref([])
const providerPresetsLoaded = ref(false)
// Pending preset that the user picked; shown as a change preview when it would
// overwrite user-tuned values (never applied silently, never touches api_key).
const pendingProviderApply = ref(null)
const testingAllEndpoints = ref(false)
// Reordering only starts from the drag handle (see startEndpointDrag), so a
// stray drag on the row body (e.g. selecting the URL text) can't reorder the
// fallback chain.
const draggingEndpointIndex = ref(null)
const dragOverEndpointIndex = ref(null)

const themePrefOptions = computed(() => [
  { value: 'auto', label: t('theme.auto') },
  { value: 'light', label: t('theme.light') },
  { value: 'dark', label: t('theme.dark') },
])
const browserInventory = reactive({
  loading: false,
  loaded: false,
  error: '',
  system_browsers: [],
  playwright_bundle: { engines: {}, exists: false, root: '' },
  recommended: null,
})
const installingBrowser = ref(false)
const installLog = ref('')
const attachState = reactive({
  loading: false,
  attached: false,
  endpoint: '',
  browser: '',
  alive: false,
  tabs: [],
  discovered: [],
  channel: 'msedge',
  port: 9222,
  manualEndpoint: 'http://127.0.0.1:9222',
  busy: false,
  message: '',
})

const toolchainPathRowGroups = toolchainPathRows()

function toolchainPathLabel(key) {
  const labels = t('settings.toolchainLabels')
  if (labels && typeof labels === 'object' && labels[key]) return labels[key]
  return key
}

const toolchainScanState = reactive({
  loading: false,
  loaded: false,
  message: '',
  error: '',
  missing: [],
  tools: {},
  installing: {},
})

const runtimeState = reactive({
  loading: false,
  loaded: false,
  installing: '',
  error: '',
  root: '',
  python: {},
  node: {},
  wabt: {},
})

const RUNTIME_LABELS = {
  python: 'Python',
  node: 'Node.js + webcrack/restringer',
  wabt: 'WABT (wasm2wat / wasm-decompile)',
}

function runtimeInfo(name) {
  return runtimeState[name] || {}
}

function runtimeStatusType(name) {
  const info = runtimeInfo(name)
  if (info.healthy) return 'success'
  return info.installed ? 'error' : 'warning'
}

function runtimeStatusLabel(name) {
  const info = runtimeInfo(name)
  if (info.healthy) return t('settings.runtimeReady')
  return info.installed ? t('settings.runtimeDamaged') : t('settings.runtimeMissing')
}

async function refreshRuntimeStatus() {
  runtimeState.loading = true
  runtimeState.error = ''
  try {
    const res = await api.sandboxRuntimeStatus()
    if (!res?.ok) throw new Error(res?.error || t('settings.runtimeStatusFailed'))
    runtimeState.root = String(res.root || '')
    runtimeState.python = res.python || {}
    runtimeState.node = res.node || {}
    runtimeState.wabt = res.wabt || {}
    runtimeState.loaded = true
  } catch (e) {
    runtimeState.loaded = false
    runtimeState.error = String(e?.message || e)
  } finally {
    runtimeState.loading = false
  }
}

async function installSandboxRuntime(name) {
  if (runtimeState.installing) return
  await performSave()
  const info = runtimeInfo(name)
  const label = RUNTIME_LABELS[name] || name
  const source = String(info.download_sources?.[0] || '')
  // WABT is a set of CLI binaries, not a sandbox, so it uses its own wording.
  const kind = name === 'wabt' ? 'runtimeTool' : 'runtime'
  const ok = await confirmDialog({
    title: t(`settings.${kind}${info.installed ? 'Repair' : 'Install'}ConfirmTitle`, { runtime: label }),
    content: t(`settings.${kind}InstallConfirmContent`, {
      runtime: label,
      version: info.expected_version || '',
      size: formatBytes(info.download_size_bytes || 0),
      source,
      root: info.root || runtimeState.root,
    }),
    positiveText: t(info.installed ? 'settings.runtimeRepair' : 'settings.runtimeInstall'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  runtimeState.installing = name
  runtimeState.error = ''
  try {
    const res = await api.installSandboxRuntime(name, { force: !!info.installed })
    if (!res?.ok) throw new Error(res?.error || t('settings.runtimeInstallFailed'))
    showToast(t(`settings.${kind}InstallDone`, { runtime: label }), 'ok')
    await refreshRuntimeStatus()
  } catch (e) {
    runtimeState.error = String(e?.message || e)
    showToast(runtimeState.error, 'error')
  } finally {
    runtimeState.installing = ''
  }
}

function toolchainToolInfo(key) {
  return toolchainScanState.tools?.[key] || {}
}

function toolchainToolAvailable(key) {
  return !!toolchainToolInfo(key).available
}

function toolchainCanInstall(key) {
  return !!toolchainToolInfo(key).can_install && !toolchainToolAvailable(key)
}

function toolchainDocsUrl(key) {
  return String(toolchainToolInfo(key).docs_url || '').trim()
}

function toolchainInstalling(key) {
  return !!toolchainScanState.installing[key]
}

async function load() {
  loading.value = true
  settingsHydrating.value = true
  draftEndpointIndex.value = null
  autoSaveReady.value = false
  autoSaveState.value = 'idle'
  autoSaveError.value = ''
  lastSavedPayload = null
  settingsVersion = ''
  pendingAuxiliaryKeyClears.clear()
  pendingMineruKeyClear = false
  try {
    // Load defaults in parallel with the active settings — they never block
    // the rest of the form even if the call fails (placeholders just stay
    // blank in that case).
    api.getDefaultDirs().then((d) => {
      if (d && d.ok) {
        defaultDirs.data_root = d.data_root || ''
        defaultDirs.tasks_dir = d.tasks_dir || ''
        defaultDirs.sessions_dir = d.sessions_dir || ''
        defaultDirs.poc_pages_dir = d.poc_pages_dir || ''
        defaultDirs.knowledge_base_dir = d.knowledge_base_dir || ''
        defaultDirs.reverse_jobs_dir = d.reverse_jobs_dir || ''
        if (d.subdir_names) defaultDirs.subdir_names = { ...defaultDirs.subdir_names, ...d.subdir_names }
      }
    }).catch(() => {})
    const data = await api.getSettings()
    const settingsMeta = data?._settings_meta || {}
    settingsVersion = String(settingsMeta.version || '')
    if (settingsMeta.recovered) {
      showToast(t('settings.recoveredFromBackup'), 'warning')
    }
    closeToTray.value = data.gui?.close_to_tray !== false
    settings.ai_enabled = data.ai?.enabled ?? true
    const aux = data.ai?.auxiliary || {}
    settings.ai_auxiliary = {
      vision: { ...defaultAuxiliaryEp(), ...(aux.vision || {}) },
      asr: { ...defaultAuxiliaryEp(), ...(aux.asr || {}) },
    }
    settings.ai_endpoints = (data.ai?.endpoints || []).map(ep => ({
      enabled: true,
      protocol: 'openai',
      priority: 0,
      timeout: 300,
      max_tokens: 16384,
      context_window: 131072,
      temperature: 0.7,
      send_temperature: true,
      image_in: false,
      video_in: false,
      audio_in: false,
      extra_headers: {},
      extra_body: {},
      model_limits: {},
      model_capabilities: {},
      ...ep,
      image_in: !!(ep.image_in ?? ep.modalities?.image_in),
      video_in: !!(ep.video_in ?? ep.modalities?.video_in),
      audio_in: !!(ep.audio_in ?? ep.modalities?.audio_in),
      model_capabilities: (ep.model_capabilities && typeof ep.model_capabilities === 'object')
        ? ep.model_capabilities
        : {},
      extra_headers_json: formatJsonField(ep.extra_headers || {}),
      extra_body_json: formatJsonField(ep.extra_body || {}),
    }))
    endpointHealth.value = settings.ai_endpoints.map(() => null)
    settings.data_root = data.data_root || ''
    committedDataRoot.value = data.data_root || ''
    derivedDirs.tasks_dir = data.tasks_dir || ''
    derivedDirs.sessions_dir = data.sessions_dir || ''
    derivedDirs.poc_pages_dir = data.poc_pages_dir || ''
    derivedDirs.knowledge_base_dir = data.knowledge_base_dir || ''
    derivedDirs.reverse_jobs_dir = data.reverse_jobs_dir || ''
    settings.kb_trash_retention_days = Number.isFinite(data.kb_trash_retention_days)
      ? Math.max(0, Math.min(3650, parseInt(data.kb_trash_retention_days)))
      : 30
    settings.security = { oob_callback_base: '', max_active_requests: 80, max_time_delay_seconds: 5, enable_protocol_tests: false, ...(data.security || {}) }
    settings.browser = { channel: '', executable_path: '', window_width: 1920, window_height: 1080, ...(data.browser || {}) }
    const paths = (data.toolchain && data.toolchain.paths) || {}
    settings.toolchain.paths = mergeToolchainPaths(paths)
    settings.runtime = {
      mirrors: {
        python: '',
        node: '',
        npm: '',
        ...((data.runtime && data.runtime.mirrors) || {}),
      },
    }
    settings.assistant = {
      max_agent_rounds: 200,
      max_auto_continue: 10,
      max_toolless_continue_decisions: 0,
      max_budget_restarts: 10,
      round_signature_hard_stop: 4,
      tool_result_keep_last: 20,
      tool_result_summary_cap: 500,
      turn_deadline_seconds: 604800,
      desktop_notifications: true,
      cron_webhook_host: '127.0.0.1',
      cron_webhook_port: 8765,
      default_shell: 'auto',
      enable_web_search: true,
      team_max_parallel: 12,
      thinking_effort: 'high',
      auto_archive_tools: [],
      tool_always_allow: [...DEFAULT_TOOL_ALWAYS_ALLOW],
      auto_approve: false,
      ...(data.assistant || {}),
    }
    if (!settings.assistant.default_shell) settings.assistant.default_shell = 'auto'
    const allowedThinkingEffort = new Set(['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
    if (!allowedThinkingEffort.has(String(settings.assistant.thinking_effort || ''))) {
      settings.assistant.thinking_effort = 'high'
    }
    if (!Array.isArray(settings.assistant.tool_always_allow) || !settings.assistant.tool_always_allow.length) {
      settings.assistant.tool_always_allow = [...DEFAULT_TOOL_ALWAYS_ALLOW]
    }
    settings.assistant.auto_approve = settings.assistant.auto_approve === true
    settings.user_habits = {
      speaking_style: '',
      task_focus: '',
      audit_domains: [],
      risk_preference: '',
      output_format: '',
      avoid: '',
      notes: '',
      ...(data.user_habits || {}),
    }
    if (typeof settings.user_habits.audit_domains === 'string') {
      settings.user_habits.audit_domains = settings.user_habits.audit_domains
        .split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
    }
    settings.logging = {
      level: 'INFO',
      assistant_level: 'DEBUG',
      backup_count: 10,
      max_bytes: 5 * 1024 * 1024,
      redact_payloads: true,
      log_dir: '',
      ...(data.logging || {}),
    }
    const dp = data.document_parse || {}
    const mineru = dp.mineru || {}
    settings.document_parse = {
      max_output_chars: Number.isFinite(dp.max_output_chars)
        ? Math.max(1000, Math.min(2000000, parseInt(dp.max_output_chars)))
        : 200000,
      mineru: {
        enabled: !!mineru.enabled,
        api_key: String(mineru.api_key || ''),
        base_url: String(mineru.base_url || 'https://mineru.net'),
        model_version: String(mineru.model_version || 'vlm') || 'vlm',
        timeout_seconds: Number.isFinite(Number(mineru.timeout_seconds))
          ? Math.max(30, Math.min(1800, Number(mineru.timeout_seconds)))
          : 300,
      },
    }
    loadAssistantToolOptions()
    refreshLoggingStatus()
    lastSavedPayload = buildSettingsPayload()
    lastSavedSignature = settingsPayloadSignature(lastSavedPayload)
    autoSaveState.value = 'saved'
    autoSaveError.value = ''
    autoSaveReady.value = true
    scanToolchainPaths({ fillBlanks: false, toast: false }).catch(() => {})
    refreshRuntimeStatus().catch(() => {})
  } catch (e) {
    console.error(e)
    autoSaveState.value = 'error'
    autoSaveError.value = String((e && e.message) || e || t('toast.error'))
  }
  finally {
    settingsHydrating.value = false
    loading.value = false
  }
  refreshBrowserInventory()
  refreshAttachStatus()
}

async function loadAssistantToolOptions() {
  try {
    const r = await api.listAssistantTools()
    if (r?.ok) {
      const selected = new Set(settings.assistant.auto_archive_tools || [])
      const opts = (r.tools || []).map(tool => ({
        label: `${tool.name}${tool.risk ? ' · ' + tool.risk : ''}`,
        value: tool.name,
        name: tool.name,
        risk: tool.risk || '',
        description: tool.description || '',
        policy_tier: tool.policy_tier || '',
        always_allow_default: !!tool.always_allow_default,
      }))
      for (const name of selected) {
        if (!opts.some(opt => opt.value === name)) {
          opts.push({ label: name, value: name, name, risk: '', description: '' })
        }
      }
      assistantToolOptions.value = opts
    }
  } catch (e) {
    console.error(e)
  }
}

function matchAssistantTool(tool, q) {
  if (!q) return true
  return [tool.name, tool.risk, tool.description].some(value => String(value || '').toLowerCase().includes(q))
}

const {
  query: archiveToolQuery,
  page: archiveToolPage,
  pageSize: archiveToolPageSize,
  total: archiveToolTotal,
  pageCount: archiveToolPageCount,
  pageItems: archiveToolPageItems,
} = useClientList(assistantToolOptions, matchAssistantTool)
archiveToolPageSize.value = 20

const alwaysAllowToolSource = computed(() =>
  assistantToolOptions.value.filter(tool => (
    tool.policy_tier === 'extension' || DESKTOP_INPUT_ALWAYS_ALLOW_TOOLS.includes(tool.value)
  )),
)

const {
  query: alwaysAllowToolQuery,
  page: alwaysAllowToolPage,
  pageSize: alwaysAllowToolPageSize,
  total: alwaysAllowToolTotal,
  pageCount: alwaysAllowToolPageCount,
  pageItems: alwaysAllowToolPageItems,
} = useClientList(alwaysAllowToolSource, matchAssistantTool)
alwaysAllowToolPageSize.value = 20

function assistantToolSelected(name) {
  return (settings.assistant.auto_archive_tools || []).includes(name)
}

function toggleAssistantArchiveTool(name, checked) {
  const current = new Set(settings.assistant.auto_archive_tools || [])
  if (checked) current.add(name)
  else current.delete(name)
  settings.assistant.auto_archive_tools = [...current]
}

async function ensureAssistantToolOptions() {
  if (!assistantToolOptions.value.length) await loadAssistantToolOptions()
}

function uniqueToolNames(names) {
  return [...new Set((names || []).map(name => String(name || '').trim()).filter(Boolean))]
}

async function setAssistantArchiveRecommended() {
  await ensureAssistantToolOptions()
  settings.assistant.auto_archive_tools = uniqueToolNames(assistantArchiveRecommendedTools)
}

async function addAssistantArchiveByKind(kind) {
  await ensureAssistantToolOptions()
  const names = assistantToolOptions.value.map(opt => opt.value)
  let picked = []
  if (kind === 'http') {
    picked = names.filter(name => name.startsWith('http_') || name.startsWith('raw_http_') || name.startsWith('web_request_') || name.startsWith('web_response_'))
  } else if (kind === 'browser') {
    picked = names.filter(name => name.startsWith('browser_'))
  } else if (kind === 'sqli') {
    picked = names.filter(name => name.includes('sqli') || name.includes('sqlmap'))
  }
  settings.assistant.auto_archive_tools = uniqueToolNames([...(settings.assistant.auto_archive_tools || []), ...picked])
}

async function selectAllAssistantArchiveTools() {
  await ensureAssistantToolOptions()
  settings.assistant.auto_archive_tools = uniqueToolNames(assistantToolOptions.value.map(opt => opt.value))
}

function clearAssistantArchiveTools() {
  settings.assistant.auto_archive_tools = []
}

function assistantAlwaysAllowSelected(name) {
  return (settings.assistant.tool_always_allow || []).includes(name)
}

function toggleAssistantAlwaysAllowTool(name, checked) {
  const current = new Set(settings.assistant.tool_always_allow || [])
  if (checked) current.add(name)
  else current.delete(name)
  settings.assistant.tool_always_allow = [...current]
}

function setAssistantAlwaysAllowRecommended() {
  settings.assistant.tool_always_allow = uniqueToolNames(DEFAULT_TOOL_ALWAYS_ALLOW)
}

async function refreshBrowserInventory() {
  browserInventory.loading = true
  browserInventory.error = ''
  try {
    const data = await api.scanSystemBrowsers()
    if (data?.ok) {
      browserInventory.system_browsers = data.system_browsers || []
      browserInventory.playwright_bundle = data.playwright_bundle || { engines: {}, exists: false, root: '' }
      browserInventory.recommended = data.recommended || null
      browserInventory.loaded = true
    } else {
      browserInventory.loaded = false
      browserInventory.error = String(data?.error || '')
    }
  } catch (e) {
    console.error(e)
    browserInventory.loaded = false
    browserInventory.error = String(e?.message || e)
  }
  finally { browserInventory.loading = false }
}

async function scanToolchainPaths({ fillBlanks = true, toast = true } = {}) {
  toolchainScanState.loading = true
  toolchainScanState.error = ''
  if (fillBlanks) toolchainScanState.message = ''
  try {
    const res = await api.scanToolchainPaths()
    if (!res?.ok) {
      toolchainScanState.loaded = false
      toolchainScanState.error = res?.error || t('settings.toolchainScanPathsFailed')
      if (toast) showToast(toolchainScanState.error, 'error')
      return
    }
    toolchainScanState.missing = Array.isArray(res.missing) ? res.missing : []
    toolchainScanState.tools = (res.tools && typeof res.tools === 'object') ? res.tools : {}
    toolchainScanState.loaded = true
    if (!fillBlanks) return
    const suggestions = res.suggestions || {}
    const names = []
    for (const key of TOOLCHAIN_PATH_KEYS) {
      const path = String(suggestions[key] || '').trim()
      if (!path) continue
      if (!String(settings.toolchain.paths[key] || '').trim()) {
        settings.toolchain.paths[key] = path
        names.push(key)
      }
    }
    if (names.length) {
      toolchainScanState.message = t('settings.toolchainScanPathsDone', {
        count: names.length,
        names: names.join(', '),
      })
      if (toast) showToast(toolchainScanState.message, 'ok')
    } else {
      toolchainScanState.message = t('settings.toolchainScanPathsNone')
      if (toast) showToast(toolchainScanState.message, 'info')
    }
  } catch (e) {
    toolchainScanState.loaded = false
    toolchainScanState.error = String(e?.message || e)
    if (toast) showToast(toolchainScanState.error, 'error')
  } finally {
    toolchainScanState.loading = false
  }
}

async function installToolchainTool(key) {
  const tool = String(key || '').trim()
  if (!tool || toolchainInstalling(tool)) return
  toolchainScanState.installing[tool] = true
  try {
    const res = await api.installReverseToolchain(tool)
    if (!res?.ok) {
      const msg = res?.error || t('settings.toolchainInstallFailed')
      showToast(msg, 'error')
      return
    }
    const saved = (res.paths_saved && typeof res.paths_saved === 'object') ? res.paths_saved : {}
    for (const [k, v] of Object.entries(saved)) {
      if (String(v || '').trim()) settings.toolchain.paths[k] = String(v).trim()
    }
    if (String(res.path || '').trim() && !String(saved[tool] || '').trim()) {
      settings.toolchain.paths[tool] = String(res.path).trim()
    }
    await scanToolchainPaths({ fillBlanks: true, toast: false })
    showToast(t('settings.toolchainInstallDone', { tool }), 'ok')
  } catch (e) {
    showToast(String(e?.message || e || t('settings.toolchainInstallFailed')), 'error')
  } finally {
    toolchainScanState.installing[tool] = false
  }
}

const browserChannelOptions = computed(() => {
  const opts = [{ label: t('settings.browserAuto'), value: '' }]
  const seen = new Set()
  for (const b of browserInventory.system_browsers) {
    if (!b.channel || seen.has(b.channel)) continue
    seen.add(b.channel)
    opts.push({ label: `${b.label} (${b.channel}${b.version ? ' · ' + b.version : ''})`, value: b.channel })
  }
  for (const ch of ['chrome', 'msedge', 'chrome-beta', 'msedge-beta', 'chrome-canary', 'msedge-canary']) {
    if (!seen.has(ch)) opts.push({ label: ch + ' ' + t('settings.browserNotInstalled'), value: ch, disabled: true })
  }
  return opts
})

async function refreshAttachStatus() {
  attachState.loading = true
  try {
    const r = await api.attachBrowserStatus()
    if (r?.ok) {
      attachState.attached = !!r.attached
      attachState.endpoint = r.endpoint || ''
      attachState.browser = r.browser || ''
      attachState.alive = !!r.alive
      attachState.discovered = r.discovered || []
    }
  } catch (e) { console.error(e) }
  finally { attachState.loading = false }
}

async function launchAndAttach() {
  if (attachState.busy) return
  const channel = attachState.channel || 'msedge'
  const ok = await confirmDialog({
    title: t('settings.attachConfirmTitle'),
    content: t('settings.attachConfirmKill', { channel }),
    positiveText: t('settings.attachConfirmProceed'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  attachState.busy = true
  attachState.message = t('settings.attachLaunching')
  try {
    const r = await api.launchBrowserForAttach({ channel, port: attachState.port, use_my_profile: true, kill_existing: true })
    if (r?.ok) {
      attachState.attached = true
      attachState.endpoint = r.endpoint
      attachState.browser = r.browser
      attachState.alive = true
      attachState.tabs = r.tabs || []
      attachState.message = t('settings.attachAttached')
      showToast(t('settings.attachAttached'), 'ok')
    } else {
      attachState.message = r?.error || t('toast.error')
      showToast(r?.error || t('toast.error'), 'error')
    }
  } catch (e) {
    attachState.message = String(e)
    showToast(String(e), 'error')
  } finally { attachState.busy = false }
}

async function attachExistingEndpoint() {
  if (attachState.busy) return
  attachState.busy = true
  attachState.message = ''
  try {
    const r = await api.attachExistingBrowser(attachState.manualEndpoint)
    if (r?.ok) {
      attachState.attached = true
      attachState.endpoint = r.endpoint
      attachState.browser = r.browser
      attachState.alive = true
      attachState.tabs = r.tabs || []
      showToast(t('settings.attachAttached'), 'ok')
    } else {
      attachState.message = r?.error || t('toast.error')
      showToast(r?.error || t('toast.error'), 'error')
    }
  } catch (e) {
    attachState.message = String(e)
    showToast(String(e), 'error')
  } finally { attachState.busy = false }
}

async function detachBrowser() {
  if (attachState.busy) return
  attachState.busy = true
  try {
    await api.detachBrowser()
    attachState.attached = false
    attachState.endpoint = ''
    attachState.browser = ''
    attachState.alive = false
    attachState.tabs = []
    showToast(t('settings.attachDetached'), 'ok')
  } catch (e) {
    showToast(String(e), 'error')
  } finally { attachState.busy = false }
}

async function refreshAttachedTabs() {
  if (!attachState.attached) return
  try {
    const r = await api.attachBrowserStatus()
    if (r?.ok && r.attached) {
      attachState.alive = !!r.alive
      attachState.browser = r.browser || attachState.browser
    }
  } catch (e) { console.error(e) }
}

async function installPlaywrightChromium() {
  if (installingBrowser.value) return
  installingBrowser.value = true
  installLog.value = ''
  try {
    const r = await api.installPlaywrightEngine('chromium')
    installLog.value = r?.log || (r?.ok ? 'ok' : (r?.error || 'failed'))
    if (r?.ok) showToast(t('settings.browserInstalled'), 'ok')
    else showToast(r?.error || t('toast.error'), 'error')
    refreshBrowserInventory()
  } catch (e) {
    installLog.value = String(e)
    showToast(String(e), 'error')
  } finally { installingBrowser.value = false }
}

const TOKEN_K = 1024
const DEFAULT_MAX_K = 16
const DEFAULT_CONTEXT_K = 128
const DEFAULT_TIMEOUT = 300
const DEFAULT_TEMPERATURE = 0.7
const maxTokensDraft = reactive({})
const contextWindowDraft = reactive({})

function resolvedModelLimit(idx, key, fallback) {
  const ep = settings.ai_endpoints[idx]
  if (!ep) return fallback
  const model = String(ep.model || '').trim()
  const entry = ep.model_limits && model ? ep.model_limits[model] : null
  const fromModel = Number(entry?.[key])
  if (Number.isFinite(fromModel) && fromModel > 0) return fromModel
  const fromEp = Number(ep[key])
  if (Number.isFinite(fromEp) && fromEp > 0) return fromEp
  return fallback
}

function rememberUserModelLimit(idx, key, tokens) {
  const ep = settings.ai_endpoints[idx]
  if (!ep) return
  const model = String(ep.model || '').trim()
  if (!model) {
    ep[key] = tokens
    return
  }
  if (!ep.model_limits || typeof ep.model_limits !== 'object') ep.model_limits = {}
  ep.model_limits[model] = {
    ...(ep.model_limits[model] || {}),
    [key]: tokens,
    source: 'user',
  }
}

function mergeIncomingModelLimits(existing, incoming, source = 'api') {
  const rank = { preset: 0, api: 1, learned: 2, user: 3 }
  const out = { ...(existing && typeof existing === 'object' ? existing : {}) }
  for (const [id, raw] of Object.entries(incoming || {})) {
    const model = String(id || '').trim()
    if (!model || !raw || typeof raw !== 'object') continue
    const prev = out[model] || {}
    if ((rank[prev.source] || 0) > (rank[source] || 0)) continue
    const next = { ...prev, source }
    const cw = Number(raw.context_window)
    const mt = Number(raw.max_tokens)
    if (Number.isFinite(cw) && cw > 0) next.context_window = Math.floor(cw)
    if (Number.isFinite(mt) && mt > 0) next.max_tokens = Math.floor(mt)
    if (next.context_window || next.max_tokens) out[model] = next
  }
  return out
}

// Keep in sync with argusai/core/ai_model_capabilities.py
const VISION_MODEL_HINTS = [
  'gpt-4o', 'gpt-4.1', 'gpt-5', 'claude-3', 'claude-sonnet', 'claude-opus',
  'claude-haiku', 'gemini', 'vision', '-vl', 'llava', 'qwen-vl',
  'glm-4v', 'glm-4.5v', 'glm-4.6v', 'omni', 'internvl', 'pixtral',
  'deepseek-flash', 'deepseek-v4-flash',
]
const DEEPSEEK_PRO_VISION_ON = Date.UTC(2026, 8, 14)

function officialDeepSeekEndpoint(ep) {
  if (String(ep?.provider_id || '').trim() === 'deepseek') return true
  try {
    return new URL(String(ep?.base_url || '')).hostname.toLowerCase() === 'api.deepseek.com'
  } catch {
    return false
  }
}

function officialDeepSeekImageIn(model, ep) {
  if (!officialDeepSeekEndpoint(ep)) return false
  const name = String(model || '').toLowerCase()
  if (!name) return false
  if (name.includes('deepseek-v4-pro')) return Date.now() >= DEEPSEEK_PRO_VISION_ON
  return true
}
const VIDEO_MODEL_HINTS = [
  'gemini', 'qwen-vl', 'qwen2-vl', 'qwen2.5-vl', 'qwen3-vl', 'omni', 'glm-4v', 'video',
]
const AUDIO_MODEL_HINTS = ['audio', 'whisper', 'gpt-4o-realtime', 'gpt-4o-audio']

function guessModelCapabilities(model, ep) {
  const name = String(model || '').toLowerCase()
  const video = VIDEO_MODEL_HINTS.some(hint => name.includes(hint))
  const image = video || VISION_MODEL_HINTS.some(hint => name.includes(hint)) || officialDeepSeekImageIn(name, ep)
  const audio = AUDIO_MODEL_HINTS.some(hint => name.includes(hint))
  return {
    image_in: image,
    video_in: video,
    audio_in: audio,
    source: 'guess',
    sources: { image_in: 'guess', video_in: 'guess', audio_in: 'guess' },
  }
}

function resolveEndpointCapabilities(ep, model) {
  const chosen = String(model ?? ep?.model ?? '').trim()
  const guessed = guessModelCapabilities(chosen, ep)
  const stored = ep?.model_capabilities?.[chosen]
  if (!stored || typeof stored !== 'object') return guessed
  const storedSource = (stored.source === 'user' || stored.source === 'learned' || stored.source === 'api')
    ? stored.source
    : 'api'
  const result = {
    image_in: guessed.image_in,
    video_in: guessed.video_in,
    audio_in: guessed.audio_in,
  }
  const sources = { ...guessed.sources }
  for (const key of ['image_in', 'video_in', 'audio_in']) {
    if (typeof stored[key] === 'boolean') {
      result[key] = stored[key]
      sources[key] = storedSource
    }
  }
  if (result.video_in) {
    result.image_in = true
    if (sources.image_in === 'guess') sources.image_in = sources.video_in
  }
  return { ...result, sources, source: sources.image_in }
}

function mergeIncomingModelCapabilities(existing, incoming, source = 'api') {
  const rank = { guess: 0, api: 1, learned: 2, user: 3 }
  const out = { ...(existing && typeof existing === 'object' ? existing : {}) }
  for (const [id, raw] of Object.entries(incoming || {})) {
    const model = String(id || '').trim()
    if (!model || !raw || typeof raw !== 'object') continue
    const prev = out[model] || {}
    if ((rank[prev.source] || 0) > (rank[source] || 0)) {
      const filled = { ...prev }
      for (const key of ['image_in', 'video_in', 'audio_in']) {
        if (typeof filled[key] !== 'boolean' && typeof raw[key] === 'boolean') filled[key] = raw[key]
      }
      if (filled.video_in) filled.image_in = true
      out[model] = filled
      continue
    }
    const next = { ...prev, source }
    for (const key of ['image_in', 'video_in', 'audio_in']) {
      if (typeof raw[key] === 'boolean') next[key] = raw[key]
    }
    if (next.video_in) next.image_in = true
    if (typeof next.image_in === 'boolean' || typeof next.video_in === 'boolean' || typeof next.audio_in === 'boolean') {
      out[model] = next
    }
  }
  return out
}

function capabilityGlyph(ep, model) {
  const caps = resolveEndpointCapabilities(ep, model)
  const bits = []
  if (caps.image_in) bits.push(t('settings.aiCapabilityGlyphImage'))
  if (caps.video_in) bits.push(t('settings.aiCapabilityGlyphVideo'))
  if (caps.audio_in) bits.push(t('settings.aiCapabilityGlyphAudio'))
  return bits.join('/')
}

function maxTokensInK(idx) {
  const raw = resolvedModelLimit(idx, 'max_tokens', DEFAULT_MAX_K * TOKEN_K)
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_MAX_K
  return Math.max(1, Math.round(raw / TOKEN_K))
}

function maxTokensInputValue(idx) {
  return Object.prototype.hasOwnProperty.call(maxTokensDraft, idx)
    ? maxTokensDraft[idx]
    : String(maxTokensInK(idx))
}

function setMaxTokensInK(idx, value) {
  const k = Number(value)
  const safe = Number.isFinite(k) && k >= 1 ? Math.floor(k) : DEFAULT_MAX_K
  rememberUserModelLimit(idx, 'max_tokens', safe * TOKEN_K)
}

function updateMaxTokensDraft(idx, value) {
  const text = String(value ?? '')
  maxTokensDraft[idx] = text
  const k = Number(text)
  if (text.trim() && Number.isFinite(k) && k >= 1) {
    rememberUserModelLimit(idx, 'max_tokens', Math.floor(k) * TOKEN_K)
  }
}

function commitMaxTokensDraft(idx) {
  if (!Object.prototype.hasOwnProperty.call(maxTokensDraft, idx)) return
  setMaxTokensInK(idx, maxTokensDraft[idx])
  delete maxTokensDraft[idx]
}

function contextWindowInK(idx) {
  const raw = resolvedModelLimit(idx, 'context_window', DEFAULT_CONTEXT_K * TOKEN_K)
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_CONTEXT_K
  return Math.max(1, Math.round(raw / TOKEN_K))
}

function contextWindowInputValue(idx) {
  return Object.prototype.hasOwnProperty.call(contextWindowDraft, idx)
    ? contextWindowDraft[idx]
    : String(contextWindowInK(idx))
}

function setContextWindowInK(idx, value) {
  const k = Number(value)
  const safe = Number.isFinite(k) && k >= 1 ? Math.floor(k) : DEFAULT_CONTEXT_K
  rememberUserModelLimit(idx, 'context_window', safe * TOKEN_K)
}

function updateContextWindowDraft(idx, value) {
  const text = String(value ?? '')
  contextWindowDraft[idx] = text
  const k = Number(text)
  if (text.trim() && Number.isFinite(k) && k >= 1) {
    rememberUserModelLimit(idx, 'context_window', Math.floor(k) * TOKEN_K)
  }
}

function commitContextWindowDraft(idx) {
  if (!Object.prototype.hasOwnProperty.call(contextWindowDraft, idx)) return
  setContextWindowInK(idx, contextWindowDraft[idx])
  delete contextWindowDraft[idx]
}

function commitTokenDrafts() {
  for (const idx of Object.keys(maxTokensDraft)) commitMaxTokensDraft(Number(idx))
  for (const idx of Object.keys(contextWindowDraft)) commitContextWindowDraft(Number(idx))
}

function formatJsonField(value) {
  if (!value || (typeof value === 'object' && Object.keys(value).length === 0)) return ''
  try {
    return JSON.stringify(value, null, 2)
  } catch (_) {
    return ''
  }
}

function parseJsonObjectField(value, label) {
  const text = String(value || '').trim()
  if (!text) return {}
  const parsed = JSON.parse(text)
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${label} must be a JSON object`)
  }
  return parsed
}

function normalizeEndpoint(ep) {
  const mt = Number(ep.max_tokens)
  const cw = Number(ep.context_window)
  const to = Number(ep.timeout)
  const pr = Number(ep.priority)
  const temp = Number(ep.temperature)
  const resolved = resolveEndpointCapabilities(ep)
  const normalized = {
    ...ep,
    enabled: ep.enabled !== false,
    max_tokens: Number.isFinite(mt) && mt >= TOKEN_K ? Math.floor(mt) : DEFAULT_MAX_K * TOKEN_K,
    context_window: Number.isFinite(cw) && cw >= TOKEN_K ? Math.floor(cw) : DEFAULT_CONTEXT_K * TOKEN_K,
    timeout: Number.isFinite(to) && to > 0 ? Math.min(Math.floor(to), 1800) : DEFAULT_TIMEOUT,
    priority: Number.isFinite(pr) ? Math.floor(pr) : 0,
    temperature: Number.isFinite(temp) ? Math.max(0, Math.min(2, temp)) : DEFAULT_TEMPERATURE,
    send_temperature: ep.send_temperature !== false,
    image_in: !!resolved.image_in,
    // Keyframes extracted from a video are sent as images, so a video-capable
    // model is always image-capable.
    video_in: !!resolved.video_in,
    audio_in: !!resolved.audio_in,
    // The active model always belongs to its own catalog, otherwise the model
    // picker would show nothing as selected.
    models: [...new Set([String(ep.model || '').trim(), ...(Array.isArray(ep.models) ? ep.models : [])]
      .map(value => String(value || '').trim())
      .filter(Boolean))],
    model_limits: (ep.model_limits && typeof ep.model_limits === 'object') ? ep.model_limits : {},
    model_capabilities: (ep.model_capabilities && typeof ep.model_capabilities === 'object')
      ? ep.model_capabilities
      : {},
    extra_headers: parseJsonObjectField(ep.extra_headers_json, 'extra_headers'),
    extra_body: parseJsonObjectField(ep.extra_body_json, 'extra_body'),
  }
  delete normalized.extra_headers_json
  delete normalized.extra_body_json
  return normalized
}

function normalizeAuxiliaryEp(ep, auxKey = '') {
  const normalized = {
    ...ep,
    base_url: String(ep?.base_url || '').trim(),
    model: String(ep?.model || '').trim(),
    // Pure WYSIWYG: the switch in the UI is the only source of truth.
    // Filling credentials no longer auto-flips it on.
    enabled: !!ep?.enabled,
    protocol: ep?.protocol || 'openai',
    max_tokens: Number(ep?.max_tokens) > 0 ? Number(ep.max_tokens) : 4096,
    timeout: Number(ep?.timeout) > 0 ? Number(ep.timeout) : 120,
    temperature: Number.isFinite(Number(ep?.temperature)) ? Number(ep.temperature) : 0.2,
    send_temperature: ep?.send_temperature !== false,
    extra_headers: ep?.extra_headers || {},
    extra_body: ep?.extra_body || {},
  }
  // Auxiliary secrets are patch-like: a blank field means "unchanged".
  // The backend additionally enforces this and only clear_api_key=true may
  // erase a stored key.
  if (String(ep?.api_key || '').trim()) normalized.api_key = ep.api_key
  else delete normalized.api_key
  if (pendingAuxiliaryKeyClears.has(auxKey)) normalized.clear_api_key = true
  else delete normalized.clear_api_key
  return normalized
}

async function clearAuxiliaryApiKey(auxKey) {
  const endpoint = settings.ai_auxiliary[auxKey]
  if (!endpoint || !String(endpoint.api_key || '').trim()) return
  const ok = await confirmDialog({
    title: t('settings.aiAuxiliaryClearKey'),
    message: t('settings.aiAuxiliaryClearKeyConfirm'),
    confirmText: t('settings.aiAuxiliaryClearKey'),
    danger: true,
  })
  if (!ok) return
  pendingAuxiliaryKeyClears.add(auxKey)
  endpoint.api_key = ''
  await performSave({ manual: true })
}

async function clearMineruApiKey() {
  const mineru = settings.document_parse?.mineru
  if (!mineru || !String(mineru.api_key || '').trim()) return
  const ok = await confirmDialog({
    title: t('settings.documentParse.clearKey'),
    message: t('settings.documentParse.clearKeyConfirm'),
    confirmText: t('settings.documentParse.clearKey'),
    danger: true,
  })
  if (!ok) return
  pendingMineruKeyClear = true
  mineru.api_key = ''
  await performSave({ manual: true })
}

function buildSettingsPayload({ commitDrafts = false } = {}) {
  if (commitDrafts) commitTokenDrafts()
  const endpoints = settings.ai_endpoints
    .filter((_, idx) => idx !== draftEndpointIndex.value)
    .map(normalizeEndpoint)
  const rawDeadline = Number(settings.assistant?.turn_deadline_seconds)
  const assistant = {
    ...settings.assistant,
    turn_deadline_seconds: Number.isFinite(rawDeadline)
      ? Math.max(0, Math.min(604800, Math.trunc(rawDeadline)))
      : 604800,
    desktop_notifications: settings.assistant?.desktop_notifications !== false,
    team_max_parallel: Math.max(1, Math.min(64, Math.trunc(Number(settings.assistant?.team_max_parallel) || 12))),
    default_shell: String(settings.assistant?.default_shell || 'auto'),
    auto_archive_tools: settings.assistant.auto_archive_tools || [],
    tool_always_allow: settings.assistant.tool_always_allow || [],
    auto_approve: settings.assistant.auto_approve === true,
  }
  const loggingPayload = {
    level: String(settings.logging?.level || 'INFO').toUpperCase(),
    assistant_level: String(settings.logging?.assistant_level || 'DEBUG').toUpperCase(),
    backup_count: Math.max(1, Math.min(200, parseInt(settings.logging?.backup_count) || 10)),
    max_bytes: Math.max(64 * 1024, Math.min(256 * 1024 * 1024, parseInt(settings.logging?.max_bytes) || 5 * 1024 * 1024)),
    redact_payloads: !!settings.logging?.redact_payloads,
    log_dir: String(settings.logging?.log_dir || '').trim(),
  }
  return {
    ai: {
      enabled: settings.ai_enabled,
      endpoints,
      auxiliary: {
        vision: normalizeAuxiliaryEp(settings.ai_auxiliary.vision, 'vision'),
        asr: normalizeAuxiliaryEp(settings.ai_auxiliary.asr, 'asr'),
      },
    },
    // Persist only the committed root; draft path changes go through migrate.
    data_root: committedDataRoot.value,
    kb_trash_retention_days: Math.max(0, Math.min(3650, parseInt(settings.kb_trash_retention_days) || 0)),
    security: settings.security,
    browser: settings.browser,
    toolchain: {
      paths: Object.fromEntries(
        Object.entries(settings.toolchain?.paths || {})
          .map(([k, v]) => [k, String(v || '').trim()])
          .filter(([, v]) => v),
      ),
    },
    runtime: {
      mirrors: {
        python: String(settings.runtime?.mirrors?.python || '').trim(),
        node: String(settings.runtime?.mirrors?.node || '').trim(),
        npm: String(settings.runtime?.mirrors?.npm || '').trim(),
      },
    },
    assistant,
    user_habits: {
      ...settings.user_habits,
      audit_domains: (settings.user_habits.audit_domains || []).filter(Boolean),
    },
    logging: loggingPayload,
    document_parse: (() => {
      const mineru = settings.document_parse?.mineru || {}
      const payload = {
        max_output_chars: Math.max(
          1000,
          Math.min(2000000, parseInt(settings.document_parse?.max_output_chars) || 200000),
        ),
        mineru: {
          enabled: !!mineru.enabled,
          base_url: String(mineru.base_url || 'https://mineru.net').trim() || 'https://mineru.net',
          model_version: String(mineru.model_version || 'vlm').trim() || 'vlm',
          timeout_seconds: Math.max(30, Math.min(1800, Number(mineru.timeout_seconds) || 300)),
        },
      }
      if (String(mineru.api_key || '').trim()) payload.mineru.api_key = mineru.api_key
      if (pendingMineruKeyClear) payload.mineru.clear_api_key = true
      return payload
    })(),
  }
}

function settingsPayloadSignature(payload) {
  return JSON.stringify(payload)
}

function changedSettingsPatch(payload) {
  if (!lastSavedPayload) return { ...payload }
  const patch = {}
  for (const [key, value] of Object.entries(payload)) {
    if (settingsPayloadSignature(value) !== settingsPayloadSignature(lastSavedPayload[key])) {
      patch[key] = value
    }
  }
  return patch
}

function savedPayloadBaseline(payload) {
  const baseline = JSON.parse(JSON.stringify(payload))
  for (const auxKey of ['vision', 'asr']) {
    const endpoint = baseline.ai?.auxiliary?.[auxKey]
    if (endpoint) delete endpoint.clear_api_key
  }
  if (baseline.document_parse?.mineru) delete baseline.document_parse.mineru.clear_api_key
  return baseline
}

async function performSave({ manual = false } = {}) {
  let payload
  let patch
  let signature
  try {
    payload = buildSettingsPayload({ commitDrafts: manual })
    signature = settingsPayloadSignature(payload)
    patch = changedSettingsPatch(payload)
  } catch (e) {
    autoSaveState.value = 'error'
    autoSaveError.value = String((e && e.message) || e)
    if (manual) showToast(autoSaveError.value, 'error')
    return
  }
  if (signature === lastSavedSignature) {
    autoSaveState.value = 'saved'
    autoSaveError.value = ''
    if (manual) showToast(t('settings.saved'), 'ok')
    return
  }
  if (autoSaveInFlight) {
    autoSaveQueued = true
    return
  }
  autoSaveInFlight = true
  autoSaveState.value = 'saving'
  autoSaveError.value = ''
  try {
    const maxAttempts = 3
    let r = null
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      r = await api.saveSettings({
        ...patch,
        _settings_version: settingsVersion,
      })
      if (r?.ok) break
      if (r?.code === 'settings_version_conflict' && r.settings_version && attempt + 1 < maxAttempts) {
        settingsVersion = String(r.settings_version)
        continue
      }
      break
    }
    if (r?.ok) {
      settingsVersion = String(r.settings_version || settingsVersion)
      pendingAuxiliaryKeyClears.clear()
      pendingMineruKeyClear = false
      lastSavedPayload = savedPayloadBaseline(payload)
      lastSavedSignature = settingsPayloadSignature(lastSavedPayload)
      autoSaveState.value = 'saved'
      autoSaveError.value = ''
      if (manual) showToast(t('settings.saved'), 'ok')
      refreshLoggingStatus()
    } else {
      if (r?.code === 'settings_version_conflict') {
        if (r.settings_version) settingsVersion = String(r.settings_version)
        throw new Error(t('settings.versionConflict'))
      }
      throw new Error(r?.error || t('toast.error'))
    }
  } catch (e) {
    autoSaveState.value = 'error'
    autoSaveError.value = String((e && e.message) || e || t('toast.error'))
    if (manual) showToast(autoSaveError.value, 'error')
  } finally {
    autoSaveInFlight = false
    if (autoSaveQueued) {
      autoSaveQueued = false
      scheduleAutoSave(0)
    }
  }
}

function scheduleAutoSave(delay = 900) {
  if (!autoSaveReady.value || loading.value || settingsHydrating.value) return
  if (autoSaveTimer) clearTimeout(autoSaveTimer)
  autoSaveState.value = 'dirty'
  autoSaveTimer = setTimeout(() => {
    autoSaveTimer = null
    performSave()
  }, delay)
}

async function save() {
  if (autoSaveTimer) {
    clearTimeout(autoSaveTimer)
    autoSaveTimer = null
  }
  await performSave({ manual: true })
}

async function retrySettingsState() {
  if (autoSaveReady.value) {
    await save()
    return
  }
  await load()
}

const autoSaveLabel = computed(() => {
  if (autoSaveState.value === 'saving') return t('settings.autoSaveSaving')
  if (autoSaveState.value === 'dirty') return t('settings.autoSavePending')
  if (autoSaveState.value === 'error') return t('settings.autoSaveError', { msg: autoSaveError.value || t('toast.error') })
  if (autoSaveState.value === 'saved') return t('settings.autoSaveSaved')
  return ''
})

watch(settings, () => {
  if (settingsHydrating.value) return
  scheduleAutoSave()
}, { deep: true })

async function onCloseToTrayChange(next) {
  const previous = closeToTray.value
  closeToTray.value = !!next
  try {
    const r = await api.setGuiCloseToTray(closeToTray.value)
    if (!r?.ok) throw new Error(r?.error || t('toast.error'))
  } catch (e) {
    closeToTray.value = previous
    showToast(String((e && e.message) || e), 'error')
  }
}

onUnmounted(() => {
  if (autoSaveTimer) clearTimeout(autoSaveTimer)
  stopMigrateProgressPolling()
})

function addEndpoint(baseUrl) {
  discardEndpointDraft()
  const idx = settings.ai_endpoints.length
  draftEndpointIndex.value = idx
  settings.ai_endpoints.push({
    enabled: true,
    name: '',
    base_url: baseUrl,
    model: '',
    api_key: '',
    priority: 0,
    timeout: DEFAULT_TIMEOUT,
    protocol: 'openai',
    provider_id: '',
    max_tokens: DEFAULT_MAX_K * TOKEN_K,
    context_window: DEFAULT_CONTEXT_K * TOKEN_K,
    temperature: DEFAULT_TEMPERATURE,
    send_temperature: true,
    image_in: false,
    video_in: false,
    audio_in: false,
    extra_headers: {},
    extra_body: {},
    extra_headers_json: '',
    extra_body_json: '',
    model_limits: {},
    model_capabilities: {},
  })
  endpointHealth.value.push(null)
  selectEndpoint(idx)
  providerPickerOpen.value = true
}

function discardEndpointDraft() {
  const idx = draftEndpointIndex.value
  if (!Number.isInteger(idx)) return
  settings.ai_endpoints.splice(idx, 1)
  endpointHealth.value.splice(idx, 1)
  delete modelCache[idx]
  delete maxTokensDraft[idx]
  delete contextWindowDraft[idx]
  draftEndpointIndex.value = null
  if (selectedEndpointIndex.value === idx) selectedEndpointIndex.value = null
}

const canCommitEndpointDraft = computed(() => {
  const idx = draftEndpointIndex.value
  const endpoint = Number.isInteger(idx) ? settings.ai_endpoints[idx] : null
  return Boolean(
    endpoint
    && String(endpoint.base_url || '').trim()
    && String(endpoint.model || '').trim(),
  )
})

function commitEndpointDraft() {
  const idx = draftEndpointIndex.value
  if (!Number.isInteger(idx) || !canCommitEndpointDraft.value) return
  const endpoint = settings.ai_endpoints[idx]
  endpoint.base_url = String(endpoint.base_url || '').trim()
  endpoint.model = String(endpoint.model || '').trim()
  endpoint.name = String(endpoint.name || '').trim()
    || endpointProviderLabel(endpoint.provider_id)
    || endpoint.model
  if (!endpoint.provider_id) endpoint.provider_id = 'custom'
  draftEndpointIndex.value = null
  endpointEditorOpen.value = false
  providerPickerOpen.value = false
  selectedEndpointIndex.value = idx
  pendingProviderApply.value = null
  scheduleAutoSave(0)
}
async function removeEndpoint(idx) {
  const endpoint = settings.ai_endpoints[idx]
  if (!endpoint) return
  const order = sortedIndices.value
  const position = order.indexOf(idx)
  const nextIndex = order[position + 1] ?? order[position - 1] ?? null
  const ok = await confirmDialog({
    title: t('settings.aiEndpointDeleteTitle'),
    content: t('settings.aiEndpointDeleteContent', { name: endpoint.name || t('settings.aiEndpointUnnamed') }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  settings.ai_endpoints.splice(idx, 1)
  endpointHealth.value.splice(idx, 1)
  for (const key of Object.keys(modelCache)) delete modelCache[key]
  for (const key of Object.keys(maxTokensDraft)) delete maxTokensDraft[key]
  for (const key of Object.keys(contextWindowDraft)) delete contextWindowDraft[key]
  if (selectedEndpointIndex.value === idx) endpointEditorOpen.value = false
  selectedEndpointIndex.value = nextIndex == null
    ? null
    : nextIndex > idx ? nextIndex - 1 : nextIndex
}
// Row order is derived from `priority` (descending), so reordering means
// rewriting the whole ladder rather than swapping two fields.
function applyEndpointOrder(order) {
  order.forEach((endpointIndex, orderIndex) => {
    settings.ai_endpoints[endpointIndex].priority = (order.length - orderIndex) * 10
  })
}

function reorderEndpoint(fromIdx, toIdx) {
  const order = [...sortedIndices.value]
  const fromPosition = order.indexOf(fromIdx)
  const toPosition = order.indexOf(toIdx)
  if (fromPosition < 0 || toPosition < 0 || fromPosition === toPosition) return
  const [moving] = order.splice(fromPosition, 1)
  order.splice(toPosition, 0, moving)
  applyEndpointOrder(order)
}

function moveEndpoint(idx, dir) {
  const order = sortedIndices.value
  const position = order.indexOf(idx)
  const targetPosition = position + dir
  if (position < 0 || targetPosition < 0 || targetPosition >= order.length) return
  reorderEndpoint(idx, order[targetPosition])
}

// Pointer-based drag instead of native HTML5 drag-and-drop: the pywebview
// desktop shell runs on WebView2 (Edge/Chromium) on Windows, where the native
// dragstart/dragover/drop events are unreliable inside an embedded webview
// (a well-known WebView2 limitation) — dragstart frequently never fires, so
// rows just don't move. Pointer Events (pointerdown/move/up + setPointerCapture)
// work everywhere, including WebView2, since they don't depend on the OS-level
// native drag session.
let endpointDragCleanup = null

function startEndpointDrag(idx, ev) {
  if (sortedIndices.value.length < 2) return
  if (ev.button != null && ev.button !== 0) return
  ev.preventDefault()
  endEndpointDrag()
  draggingEndpointIndex.value = idx
  dragOverEndpointIndex.value = idx
  const pointerId = ev.pointerId
  const handle = ev.currentTarget
  try { handle.setPointerCapture(pointerId) } catch { /* ignore */ }

  function onMove(moveEv) {
    if (moveEv.pointerId !== pointerId) return
    const target = document.elementFromPoint(moveEv.clientX, moveEv.clientY)
    const row = target?.closest?.('.endpoint-route-row')
    const overIdx = row ? Number(row.dataset.endpointIdx) : NaN
    if (!Number.isNaN(overIdx)) dragOverEndpointIndex.value = overIdx
  }
  function onUp(upEv) {
    if (upEv.pointerId !== pointerId) return
    const from = draggingEndpointIndex.value
    const to = dragOverEndpointIndex.value
    endEndpointDrag()
    if (from != null && to != null && from !== to) reorderEndpoint(from, to)
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
  endpointDragCleanup = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  }
}

function endEndpointDrag() {
  if (endpointDragCleanup) {
    endpointDragCleanup()
    endpointDragCleanup = null
  }
  draggingEndpointIndex.value = null
  dragOverEndpointIndex.value = null
}

async function fetchModels(idx) {
  const ep = settings.ai_endpoints[idx]
  if (!ep.base_url) return
  modelCache[idx] = { loading: true, models: [], loaded: false }
  try {
    const extraHeaders = parseJsonObjectField(ep.extra_headers_json, 'extra_headers')
    const r = await api.listAIModels({
      base_url: ep.base_url,
      api_key: ep.api_key,
      timeout: ep.timeout,
      extra_headers: extraHeaders,
      provider_id: ep.provider_id || '',
      protocol: ep.protocol || 'openai',
    })
    if (r.ok) {
      // A curated fallback still arrives as a usable list, so keep whatever
      // came back and let `manual` decide whether to nudge towards typing.
      const models = r.models || []
      modelCache[idx] = { loading: false, models, loaded: true, manual: !!r.manual, source: r.source }
      if (r.model_limits && typeof r.model_limits === 'object') {
        ep.model_limits = mergeIncomingModelLimits(ep.model_limits, r.model_limits, 'api')
      }
      if (r.model_capabilities && typeof r.model_capabilities === 'object') {
        ep.model_capabilities = mergeIncomingModelCapabilities(
          ep.model_capabilities,
          r.model_capabilities,
          'api',
        )
      }
      if (!ep.model && models.length === 1) ep.model = models[0]
    } else {
      modelCache[idx] = { loading: false, models: [], loaded: true, error: r.error }
    }
  } catch (e) { modelCache[idx] = { loading: false, models: [], loaded: true, error: String(e) } }
}

async function testEndpoint(idx) {
  testingId.value = idx
  const signature = endpointTestSignature(settings.ai_endpoints[idx])
  const startedAt = performance.now()
  endpointHealth.value[idx] = { status: 'testing', signature, checkedAt: Date.now() }
  try {
    const r = await api.testAIEndpoint(settings.ai_endpoints[idx])
    const latencyMs = Math.max(0, Math.round(performance.now() - startedAt))
    if (r.ok) {
      endpointHealth.value[idx] = { status: 'success', signature, checkedAt: Date.now(), latencyMs }
      showToast(t('settings.testOk') + (r.response ? ' — ' + r.response.slice(0, 80) : ''), 'ok')
    } else {
      endpointHealth.value[idx] = { status: 'error', signature, checkedAt: Date.now(), latencyMs }
      showToast(t('settings.testFail') + ': ' + (r.error||'').slice(0, 120), 'error')
    }
  } catch {
    endpointHealth.value[idx] = {
      status: 'error',
      signature,
      checkedAt: Date.now(),
      latencyMs: Math.max(0, Math.round(performance.now() - startedAt)),
    }
    showToast(t('settings.testFail'), 'error')
  }
  finally { testingId.value = null }
}

async function testAllEndpoints() {
  if (testingAllEndpoints.value) return
  testingAllEndpoints.value = true
  try {
    for (const idx of sortedIndices.value) {
      if (settings.ai_endpoints[idx]?.enabled !== false) await testEndpoint(idx)
    }
  } finally {
    testingAllEndpoints.value = false
  }
}
function onBaseUrlChange(idx) {
  endpointHealth.value[idx] = null
  delete modelCache[idx]
}

function endpointModelOptions(idx) {
  const endpoint = settings.ai_endpoints[idx]
  const current = String(endpoint?.model || '').trim()
  const catalog = Array.isArray(endpoint?.models) ? endpoint.models : []
  const discovered = Array.isArray(modelCache[idx]?.models) ? modelCache[idx].models : []
  return [...new Set([current, ...catalog, ...discovered].map(value => String(value || '').trim()).filter(Boolean))]
    .map(value => {
      const cw = Number(endpoint?.model_limits?.[value]?.context_window)
      const caps = capabilityGlyph(endpoint, value)
      const parts = [value]
      if (Number.isFinite(cw) && cw >= TOKEN_K) parts.push(`${Math.round(cw / TOKEN_K)}K`)
      if (caps) parts.push(caps)
      return { label: parts.join(' · '), value }
    })
}

// Deselecting the active model would leave the picker with no live row, so the
// active one is pinned back into the catalog on every edit.
function setEndpointCatalog(idx, values) {
  const endpoint = settings.ai_endpoints[idx]
  if (!endpoint) return
  const chosen = (Array.isArray(values) ? values : [])
    .map(value => String(value || '').trim())
    .filter(Boolean)
  if (!chosen.length) {
    endpoint.models = endpoint.model ? [endpoint.model] : []
    return
  }
  if (endpoint.model && !chosen.includes(endpoint.model)) endpoint.model = chosen[0]
  endpoint.models = [...new Set(chosen)]
}

function endpointTestSignature(endpoint) {
  return JSON.stringify({
    base_url: endpoint?.base_url || '',
    api_key: endpoint?.api_key || '',
    model: endpoint?.model || '',
    protocol: endpoint?.protocol || '',
    timeout: endpoint?.timeout || 0,
    extra_headers_json: endpoint?.extra_headers_json || '',
  })
}

const defaultShellOptions = computed(() => [
  { label: t('settings.assistantDefaultShellAuto'), value: 'auto' },
  { label: 'PowerShell', value: 'powershell' },
  { label: 'pwsh', value: 'pwsh' },
  { label: 'cmd', value: 'cmd' },
  { label: 'bash', value: 'bash' },
])

const assistantThinkingEffortOptions = computed(() => [
  { label: t('assistant.thinkingEffortAuto'), value: 'auto' },
  { label: t('assistant.thinkingEffortNone'), value: 'none' },
  { label: t('assistant.thinkingEffortMinimal'), value: 'minimal' },
  { label: t('assistant.thinkingEffortLow'), value: 'low' },
  { label: t('assistant.thinkingEffortMedium'), value: 'medium' },
  { label: t('assistant.thinkingEffortHigh'), value: 'high' },
  { label: t('assistant.thinkingEffortXhigh'), value: 'xhigh' },
  { label: t('assistant.thinkingEffortMax'), value: 'max' },
])

const cronJobs = ref([])
const cronLoading = ref(false)
const cronLoaded = ref(false)
const cronExpanded = ref('')
const cronRuns = reactive({})
const cronRunsLoading = ref('')

const hostService = reactive({
  loaded: false,
  loading: false,
  busy: false,
  installed: false,
  active: false,
  state: 'not_installed',
  platform: '',
  boot_policy: 'logon',
  boot_policies: [],
  cron_only: false,
  channel_host_held: false,
  notes: [],
  unit: null,
  name: null,
  elevated: false,
  needs_elevation: false,
  kind: '',
  requires_approval: false,
  login_item_available: false,
})

const hostServiceBootOptions = computed(() => (
  (Array.isArray(hostService.boot_policies) ? hostService.boot_policies : [])
    .filter((row) => row && row.id)
    .map((row) => ({
      label: row.needs_admin
        ? `${String(row.label || row.id)} · ${t('settings.hostService.needsAdminShort')}`
        : String(row.label || row.id),
      value: String(row.id),
    }))
))

const hostServicePolicyHint = computed(() => {
  const row = (Array.isArray(hostService.boot_policies) ? hostService.boot_policies : [])
    .find((item) => item && String(item.id) === String(hostService.boot_policy))
  return String(row?.hint || '').trim()
})

const hostServiceNeedsAdmin = computed(() => (
  String(hostService.platform || '').startsWith('win')
  && hostService.boot_policy === 'startup'
  && !hostService.elevated
))

const hostServiceIsWindows = computed(() => String(hostService.platform || '').startsWith('win'))
const hostServiceIsDarwin = computed(() => String(hostService.platform || '') === 'darwin')
const hostServiceIsLinux = computed(() => String(hostService.platform || '').startsWith('linux'))
const hostServiceHintKey = computed(() => {
  if (hostServiceIsDarwin.value) return 'settings.hostService.hintMac'
  if (hostServiceIsLinux.value) return 'settings.hostService.hintLinux'
  return 'settings.hostService.hint'
})
const hostServiceAdvancedOpen = ref(false)
const hostServiceShowAdvanced = computed(() => {
  if (hostServiceIsLinux.value) return true
  if (!hostServiceIsWindows.value && !hostServiceIsDarwin.value) return true
  return (
    hostServiceAdvancedOpen.value
    || !!hostService.cron_only
    || hostService.boot_policy === 'startup'
  )
})

const hostServiceNotes = computed(() => (
  (Array.isArray(hostService.notes) ? hostService.notes : [])
    .map((note) => String(note || '').trim())
    .filter(Boolean)
))

function applyHostServiceStatus(status) {
  if (!status || typeof status !== 'object') return
  const policies = Array.isArray(status.boot_policies) ? status.boot_policies : hostService.boot_policies
  hostService.loaded = true
  hostService.installed = !!status.installed
  hostService.active = !!status.active
  hostService.state = status.state || (status.installed ? 'stopped' : 'not_installed')
  hostService.platform = status.platform || hostService.platform || ''
  hostService.boot_policy = String(status.boot_policy || hostService.boot_policy || 'logon')
  hostService.boot_policies = Array.isArray(policies) ? policies : []
  hostService.cron_only = !!status.cron_only
  hostService.channel_host_held = !!status.channel_host_held
  hostService.notes = Array.isArray(status.notes) ? status.notes.map((n) => String(n ?? '')) : []
  hostService.unit = status.unit || null
  hostService.name = status.name || null
  hostService.elevated = !!status.elevated
  hostService.needs_elevation = !!status.needs_elevation
  hostService.kind = String(status.kind || '')
  hostService.requires_approval = !!status.requires_approval
  hostService.login_item_available = !!status.login_item_available
}

function hostServiceErrorText(result, fallback) {
  const code = String(result?.error_code || '')
  const keyByCode = {
    serve_err_startup_needs_admin: 'settings.hostService.startupNeedsAdmin',
    serve_err_access_denied_logon: 'settings.hostService.accessDeniedLogon',
    serve_err_access_denied_delete: 'settings.hostService.accessDeniedDelete',
    serve_err_unknown: 'settings.hostService.unknownError',
    serve_err_macos_login_item: 'settings.hostService.macosNeedsApproval',
    channel_host_held: 'settings.hostService.conflict',
  }
  if (keyByCode[code]) return t(keyByCode[code])
  const msg = String(result?.error || fallback || '')
  if (/startup_needs_admin|以管理员身份|Run as administrator|system startup/i.test(msg)) {
    return t('settings.hostService.startupNeedsAdmin')
  }
  if (/access_denied_delete|删不掉|Could not remove/i.test(msg)) {
    return t('settings.hostService.accessDeniedDelete')
  }
  if (/拒绝访问|access is denied|0x80070005|access_denied/i.test(msg)) {
    return t('settings.hostService.accessDeniedLogon')
  }
  return msg || t('settings.hostService.unknownError')
}

async function loadHostService() {
  hostService.loading = true
  try {
    const r = await api.getHostServiceStatus()
    applyHostServiceStatus(r && typeof r === 'object' ? r : {})
  } catch (_) {
    hostService.loaded = false
  } finally {
    hostService.loading = false
  }
}

async function onHostServiceToggle(enabled) {
  if (hostService.busy) return
  hostService.busy = true
  try {
    const r = await api.setHostService({
      enabled: !!enabled,
      cronOnly: !!hostService.cron_only,
      bootPolicy: hostService.boot_policy,
      startNow: true,
    })
    if (!r?.ok) throw new Error(hostServiceErrorText(r, r?.error || 'failed'))
    if (r.status) applyHostServiceStatus(r.status)
    else await loadHostService()
    if (r.warning) showToast(String(r.warning), 'info')
    else showToast(enabled ? t('settings.hostService.Enabled') : t('settings.hostService.Disabled'), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
    await loadHostService()
  } finally {
    hostService.busy = false
  }
}

async function openMacosLoginItems() {
  try {
    const r = await api.openMacosLoginItems()
    if (!r?.ok) throw new Error(r?.error || 'failed')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function onHostServiceBootChange(policy) {
  const next = String(policy || '').trim()
  if (!next || next === hostService.boot_policy) return
  if (hostService.busy) return
  hostService.boot_policy = next
  if (!hostService.installed) return
  // Re-install with the new boot policy.
  await onHostServiceToggle(true)
}

async function loadCronJobs() {
  cronLoading.value = true
  try {
    const r = await api.listAssistantCronJobs()
    cronJobs.value = r?.ok && Array.isArray(r.jobs) ? r.jobs : []
    cronLoaded.value = !!r?.ok
  } catch (_) {
    cronJobs.value = []
    cronLoaded.value = false
  } finally {
    cronLoading.value = false
  }
}

async function toggleCronRuns(jobId) {
  if (cronExpanded.value === jobId) {
    cronExpanded.value = ''
    return
  }
  cronExpanded.value = jobId
  cronRunsLoading.value = jobId
  try {
    const r = await api.listAssistantCronRuns(jobId, 20)
    cronRuns[jobId] = r?.ok && Array.isArray(r.runs) ? r.runs : []
  } catch (_) {
    cronRuns[jobId] = []
  } finally {
    cronRunsLoading.value = ''
  }
}

async function setCronEnabled(job, enabled) {
  try {
    const r = await api.setAssistantCronEnabled(job.id, enabled)
    if (!r?.ok) throw new Error(r?.error || 'failed')
    await loadCronJobs()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function deleteCronJob(job) {
  const ok = await confirmDialog({
    title: t('settings.cronDeleteTitle'),
    content: t('settings.cronDeleteConfirm', { name: job.name || job.id }),
  })
  if (!ok) return
  try {
    const r = await api.deleteAssistantCronJob(job.id)
    if (!r?.ok) throw new Error(r?.error || 'failed')
    if (cronExpanded.value === job.id) cronExpanded.value = ''
    await loadCronJobs()
    showToast(t('settings.cronDeleted'), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function formatCronWhen(ts) {
  const n = Number(ts || 0)
  if (!n) return '—'
  try {
    return new Date(n * 1000).toLocaleString()
  } catch (_) {
    return String(n)
  }
}

function cronScheduleLabel(job) {
  const sch = job?.schedule || {}
  if (sch.kind === 'cron') return sch.expr || 'cron'
  const sec = Number(sch.every_seconds || 0)
  if (sec >= 86400 && sec % 86400 === 0) return `every ${sec / 86400}d`
  if (sec >= 3600 && sec % 3600 === 0) return `every ${sec / 3600}h`
  if (sec >= 60 && sec % 60 === 0) return `every ${sec / 60}m`
  return `every ${sec || '?'}s`
}

function onGlobalKeydown(ev) {
  if (ev.key !== 'Escape') return
  if (providerPickerOpen.value) {
    providerPickerOpen.value = false
    return
  }
  if (endpointEditorOpen.value) {
    closeEndpointEditor()
    return
  }
  if (migrateModalOpen.value && !migrateBusy.value) closeMigrateModal()
}

onMounted(() => {
  load()
  loadCronJobs()
  loadHostService()
  loadProviderPresets()
  window.addEventListener('keydown', onGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
})

const sortedIndices = computed(() =>
  [...settings.ai_endpoints]
    .map((ep, i) => ({ i, p: Number(ep.priority) || 0 }))
    .filter(item => item.i !== draftEndpointIndex.value)
    .sort((a, b) => (b.p - a.p) || (a.i - b.i))
    .map(x => x.i)
)
const committedEndpointCount = computed(() => sortedIndices.value.length)
const enabledEndpointCount = computed(() => sortedIndices.value
  .filter(idx => settings.ai_endpoints[idx]?.enabled !== false)
  .length)
const primaryEndpointIndex = computed(() => (
  sortedIndices.value.find(idx => settings.ai_endpoints[idx]?.enabled !== false) ?? null
))
const latestEndpointProbe = computed(() => endpointHealth.value
  .filter(state => state?.checkedAt)
  .sort((a, b) => b.checkedAt - a.checkedAt)[0] || null)
const selectedEndpoint = computed(() => {
  const idx = selectedEndpointIndex.value
  return Number.isInteger(idx) ? settings.ai_endpoints[idx] || null : null
})
const currentModelCapabilities = computed(() => resolveEndpointCapabilities(selectedEndpoint.value))
const currentModelCapabilitySource = computed(() => {
  const source = currentModelCapabilities.value?.source || 'guess'
  return t(`settings.aiCapabilitySource.${source}`)
})
const currentModelCapabilitySummary = computed(() => {
  const caps = currentModelCapabilities.value || {}
  const parts = []
  if (caps.image_in) parts.push(t('settings.aiCapabilityGlyphImage'))
  if (caps.video_in) parts.push(t('settings.aiCapabilityGlyphVideo'))
  if (caps.audio_in) parts.push(t('settings.aiCapabilityGlyphAudio'))
  if (!parts.length) return t('settings.aiCapabilityStatusNone')
  return t('settings.aiCapabilityStatusHas', { caps: parts.join(' / ') })
})
const currentModelHasUserCapabilityOverride = computed(() => {
  const ep = selectedEndpoint.value
  const model = String(ep?.model || '').trim()
  return Boolean(model && ep?.model_capabilities?.[model]?.source === 'user')
})

watch(sortedIndices, (order) => {
  if (selectedEndpointIndex.value === draftEndpointIndex.value) return
  if (!order.length) {
    selectedEndpointIndex.value = null
    return
  }
  if (!order.includes(selectedEndpointIndex.value)) {
    selectedEndpointIndex.value = order[0]
  }
}, { immediate: true })

function selectEndpoint(idx) {
  selectedEndpointIndex.value = idx
  pendingProviderApply.value = null
  providerPickerOpen.value = false
  endpointEditorOpen.value = true
}

function closeEndpointEditor() {
  if (Number.isInteger(draftEndpointIndex.value)) {
    discardEndpointDraft()
  }
  endpointEditorOpen.value = false
  providerPickerOpen.value = false
  pendingProviderApply.value = null
}

// --- provider preset selector ---------------------------------------------

async function loadProviderPresets() {
  if (providerPresetsLoaded.value) return
  try {
    const r = await api.listAIProviderPresets()
    if (r && r.ok) {
      providerPresets.value = r.presets || []
      providerCategories.value = r.categories || []
    }
  } catch (e) {
    // Presets are a convenience only; keep the editor usable without them.
    console.warn('Failed to load AI provider presets', e)
  } finally {
    providerPresetsLoaded.value = true
  }
}

function providerById(key) {
  return providerPresets.value.find((p) => p.id === key) || null
}

function endpointProviderLabel(id) {
  const p = providerById(id)
  if (p) return p.name
  return t('settings.aiProviderCustom')
}

function endpointProviderRegion(id) {
  const p = providerById(id)
  return p ? p.region : ''
}

function regionLabel(region) {
  const map = { global: t('settings.aiRegionGlobal'), cn: t('settings.aiRegionChina'), apac: t('settings.aiRegionApac') }
  return map[region] || region || ''
}

function endpointHasProvider(idx) {
  const ep = settings.ai_endpoints[idx]
  if (!ep) return false
  const pid = ep.provider_id
  return !!pid && pid !== 'custom' && !!providerById(pid)
}

function formatContextWindow(n) {
  if (!n) return ''
  const k = n / 1024
  return k >= 1 ? `${Math.round(k)}K` : String(n)
}

// Compute which fields applying a preset would change on the live endpoint,
// excluding api_key (never auto-touched) and empty target values that only
// overwrite a pre-existing user value.
function providerChangeRows(ep, preset) {
  const rows = []
  if (preset.name && (!ep.name || ep.name !== preset.name)) {
    rows.push({ key: t('settings.aiEndpointName'), before: ep.name || '—', after: preset.name })
  }
  if (preset.protocol && ep.protocol !== preset.protocol) {
    rows.push({ key: t('settings.aiEndpointProtocol'), before: ep.protocol || '—', after: preset.protocol })
  }
  if (preset.base_url) {
    const cur = (ep.base_url || '').trim()
    const target = preset.base_url.trim()
    if (cur && cur !== target) {
      rows.push({ key: t('settings.aiEndpointUrl'), before: cur || '—', after: target })
    }
  }
  if (preset.default_model && ep.model !== preset.default_model && rows.filter(r => r.key === t('settings.aiEndpointModel')).length === 0) {
    rows.push({ key: t('settings.aiEndpointModel'), before: ep.model || '—', after: preset.default_model })
  }
  if (preset.context_window && ep.context_window !== preset.context_window) {
    rows.push({ key: t('settings.aiEndpointContextWindow'), before: formatContextWindow(ep.context_window) || '—', after: formatContextWindow(preset.context_window) })
  }
  if (preset.max_tokens && ep.max_tokens !== preset.max_tokens) {
    rows.push({ key: t('settings.aiEndpointMaxTokens'), before: formatTokens(ep.max_tokens) || '—', after: formatTokens(preset.max_tokens) })
  }
  return rows
}

function formatTokens(n) {
  if (!n) return ''
  const k = (n || 0) / 1024
  return k >= 1 ? `${Math.round(k)}K` : String(n)
}

function isFreshEndpoint(ep) {
  // A brand-new blank endpoint gets prefilled without a confirmation prompt.
  return !ep.name && !(ep.base_url || '').trim() && !ep.model && !ep.api_key
}

function openProviderPicker() {
  pendingProviderApply.value = null
  providerPickerOpen.value = true
}

function selectProviderFromPicker(providerId) {
  applyProviderPrefill(providerId)
  providerPickerOpen.value = false
}

function applyProviderPrefill(presetId) {
  const ep = selectedEndpoint.value
  if (!ep) return
  pendingProviderApply.value = null

  if (presetId === 'custom' || presetId === undefined || presetId === null || presetId === '') {
    ep.provider_id = 'custom'
    return
  }
  const preset = providerById(presetId)
  if (!preset) {
    ep.provider_id = 'custom'
    return
  }

  // Brand-new endpoint: prefill directly (never sets api_key).
  if (isFreshEndpoint(ep)) {
    ep.provider_id = preset.id
    if (preset.name && !ep.name) ep.name = preset.name
    if (preset.protocol) ep.protocol = preset.protocol
    if (preset.base_url) ep.base_url = preset.base_url
    if (preset.default_model && !ep.model) ep.model = preset.default_model
    if (preset.context_window) ep.context_window = preset.context_window
    if (preset.max_tokens) ep.max_tokens = preset.max_tokens
    seedProviderModels(preset)
    return
  }

  // Editing an existing endpoint: only apply after the user accepts the preview.
  pendingProviderApply.value = { presetId, preset, rows: providerChangeRows(ep, preset) }
}

function applyProviderNow() {
  const pending = pendingProviderApply.value
  const ep = selectedEndpoint.value
  if (!pending || !ep) return
  const preset = pending.preset
  const idx = selectedEndpointIndex.value
  if (idx != null) {
    endpointHealth.value[idx] = null
    delete modelCache[idx]
  }
  ep.provider_id = preset.id
  if (preset.name) ep.name = preset.name
  if (preset.protocol) ep.protocol = preset.protocol
  if (preset.base_url) ep.base_url = preset.base_url
  if (preset.default_model) ep.model = preset.default_model
  if (preset.context_window) ep.context_window = preset.context_window
  if (preset.max_tokens) ep.max_tokens = preset.max_tokens
  seedProviderModels(preset)
  pendingProviderApply.value = null
}

function cancelProviderApply() {
  pendingProviderApply.value = null
}

// Make the preset's suggested models selectable without hitting /models.
function seedProviderModels(preset) {
  const idx = selectedEndpointIndex.value
  if (idx == null) return
  const suggested = preset?.suggested_models || []
  if (!suggested.length) return
  // Seeding only fills the picker. Whether the endpoint publishes a catalogue
  // is unknown until the fetch runs, and most OpenAI-compatible relays do, so
  // announcing that it does not would be wrong for the majority of presets.
  modelCache[idx] = {
    loading: false,
    models: suggested,
    loaded: true,
    manual: preset?.model_list_strategy === 'manual',
    source: 'preset',
  }
}


function setEndpointModelCapability(key, value) {
  const endpoint = selectedEndpoint.value
  if (!endpoint) return
  const model = String(endpoint.model || '').trim()
  if (!model) {
    endpoint[key] = value
    if (key === 'image_in' && !value) endpoint.video_in = false
    if (key === 'video_in' && value) endpoint.image_in = true
    return
  }
  if (!endpoint.model_capabilities || typeof endpoint.model_capabilities !== 'object') {
    endpoint.model_capabilities = {}
  }
  const next = { ...(endpoint.model_capabilities[model] || {}), [key]: value, source: 'user' }
  if (key === 'image_in' && !value) next.video_in = false
  if (key === 'video_in' && value) next.image_in = true
  endpoint.model_capabilities[model] = next
  const resolved = resolveEndpointCapabilities(endpoint)
  endpoint.image_in = resolved.image_in
  endpoint.video_in = resolved.video_in
  endpoint.audio_in = resolved.audio_in
}

function clearEndpointModelCapabilityOverrides() {
  const endpoint = selectedEndpoint.value
  const model = String(endpoint?.model || '').trim()
  if (!endpoint || !model || !endpoint.model_capabilities) return
  delete endpoint.model_capabilities[model]
  const resolved = resolveEndpointCapabilities(endpoint)
  endpoint.image_in = resolved.image_in
  endpoint.video_in = resolved.video_in
  endpoint.audio_in = resolved.audio_in
}

function endpointEnabled(idx) {
  return settings.ai_endpoints[idx]?.enabled !== false
}

function setEndpointEnabled(idx, value) {
  const endpoint = settings.ai_endpoints[idx]
  if (endpoint) endpoint.enabled = value
}

function endpointOrder(idx) {
  const position = sortedIndices.value.indexOf(idx)
  return position < 0 ? '—' : String(position + 1).padStart(2, '0')
}

function endpointProbeState(idx) {
  const endpoint = settings.ai_endpoints[idx]
  const state = endpointHealth.value[idx]
  if (!state || state.signature !== endpointTestSignature(endpoint)) return 'unknown'
  return state.status || 'unknown'
}

function endpointProbeLabel(idx) {
  return t(`settings.aiEndpointState.${endpointProbeState(idx)}`)
}

function endpointHealthState(idx) {
  if (settings.ai_endpoints[idx]?.enabled === false) return 'disabled'
  return endpointProbeState(idx)
}

function endpointHealthLabel(idx) {
  return t(`settings.aiEndpointState.${endpointHealthState(idx)}`)
}

function endpointProtocolLabel(endpoint) {
  return endpoint?.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI Compatible'
}

function formatEndpointProbeTime(timestamp) {
  if (!timestamp) return t('settings.aiEndpointNeverTested')
  return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatEndpointLatency(latencyMs) {
  if (!Number.isFinite(latencyMs)) return '—'
  return latencyMs >= 1000 ? `${(latencyMs / 1000).toFixed(2)} s` : `${latencyMs} ms`
}

const settingsSearch = ref('')
const activeSettingsSection = ref('ai')
const settingsContentEl = ref(null)

const SETTINGS_CATEGORY_DEFINITIONS = [
  { id: 'appearance', group: 'workspace', labelKey: 'settings.appearanceTitle', keywords: 'theme appearance light dark auto tray 主题 外观 浅色 深色 自动 托盘' },
  { id: 'ai', group: 'workspace', labelKey: 'settings.aiTitle', keywords: 'ai model endpoint api key vision asr tokens 模型 端点 密钥 视觉 语音' },
  { id: 'assistant', group: 'workspace', labelKey: 'settings.assistantRuntimeTitle', keywords: 'assistant runtime tools policy archive allow shell rounds thinking effort reasoning 助手 工具 确认 归档 shell 轮次 思考 强度' },
  { id: 'user_habits', group: 'workspace', labelKey: 'settings.control.userHabitsTitle', keywords: 'profile habits preference style output risk 用户 习惯 偏好 风格 输出 风险' },
  { id: 'automation', group: 'operations', labelKey: 'settings.control.automationTitle', keywords: 'cron mcp schedule integrations automation mineru pdf docx document parse tray autostart 定时 计划 服务 集成 自动化 文档解析 开机 自启 托盘' },
  { id: 'security', group: 'operations', labelKey: 'settings.securityTitle', keywords: 'security oob active requests protocol delay 安全 回连 请求 协议 延迟' },
  { id: 'browser', group: 'local', labelKey: 'settings.browserTitle', keywords: 'browser chrome edge playwright attach cdp 浏览器 附加' },
  { id: 'runtime', group: 'local', labelKey: 'settings.runtimeTitle', keywords: 'runtime python node wabt npm mirror sandbox 运行时 镜像 沙盒' },
  { id: 'toolchain', group: 'local', labelKey: 'settings.toolchainTitle', keywords: 'reverse toolchain jadx ghidra frida apktool adb 逆向 工具链' },
  { id: 'storage', group: 'local', labelKey: 'settings.storageTitle', keywords: 'storage data root migration tasks sessions knowledge 存储 数据 迁移 目录' },
  { id: 'logging', group: 'local', labelKey: 'settings.logging.title', keywords: 'logging diagnostics debug redact files 日志 诊断 脱敏 文件' },
]

function configuredHabitCount() {
  return Object.values(settings.user_habits || {}).filter((value) => (
    Array.isArray(value) ? value.length > 0 : String(value || '').trim().length > 0
  )).length
}

function categorySummary(id) {
  if (id === 'appearance') {
    return themePrefOptions.value.find(opt => opt.value === theme.preferred)?.label || t('settings.control.statusUnknown')
  }
  if (id === 'ai') {
    return t('settings.control.endpointSummary', {
      enabled: enabledEndpointCount.value,
      total: committedEndpointCount.value,
    })
  }
  if (id === 'assistant') {
    return t('settings.control.toolPolicySummary', {
      count: (settings.assistant.tool_always_allow || []).length,
    })
  }
  if (id === 'user_habits') {
    return t('settings.control.habitSummary', { count: configuredHabitCount() })
  }
  if (id === 'automation') {
    if (!cronLoaded.value) return t('settings.control.statusUnknown')
    return t('settings.control.cronSummary', {
      enabled: cronJobs.value.filter(job => job.enabled).length,
      total: cronJobs.value.length,
    })
  }
  if (id === 'security') {
    return t('settings.control.securitySummary', { count: settings.security.max_active_requests })
  }
  if (id === 'browser') {
    if (attachState.attached && attachState.alive) return t('settings.control.browserAttached')
    if (!browserInventory.loaded) return t('settings.control.statusUnknown')
    return t('settings.control.browserSummary', { count: browserInventory.system_browsers.length })
  }
  if (id === 'runtime') {
    if (!runtimeState.loaded) return t('settings.control.statusUnknown')
    const healthy = ['python', 'node', 'wabt'].filter(name => runtimeInfo(name).healthy).length
    return t('settings.control.runtimeSummary', { healthy, total: 3 })
  }
  if (id === 'toolchain') {
    if (!toolchainScanState.loaded) return t('settings.control.statusUnknown')
    const tools = Object.values(toolchainScanState.tools || {})
    return t('settings.control.toolchainSummary', {
      ready: tools.filter(tool => tool.available).length,
      total: tools.length,
    })
  }
  if (id === 'storage') {
    return dataRootPending.value
      ? t('settings.control.storagePending')
      : t('settings.control.storageLocal')
  }
  if (id === 'logging') {
    return t('settings.control.loggingSummary', { level: settings.logging.level || 'INFO' })
  }
  return t('settings.control.statusUnknown')
}

function categoryHealth(id) {
  if (id === 'ai') {
    const states = endpointHealth.value
      .map((state, idx) => {
        const endpoint = settings.ai_endpoints[idx]
        if (!state || endpoint?.enabled === false) return 'neutral'
        return state.signature === endpointTestSignature(endpoint) ? state.status : 'neutral'
      })
    if (states.includes('error')) return 'error'
    if (states.includes('testing')) return 'warning'
    if (states.includes('success')) return 'success'
    return 'neutral'
  }
  if (id === 'browser') {
    if (browserInventory.error) return 'error'
    if (attachState.attached) return attachState.alive ? 'success' : 'error'
    if (!browserInventory.loaded) return 'neutral'
    const bundled = !!browserInventory.playwright_bundle?.engines?.chromium?.installed
    return browserInventory.system_browsers.length || bundled ? 'success' : 'warning'
  }
  if (id === 'runtime') {
    if (runtimeState.error) return 'error'
    if (!runtimeState.loaded) return 'neutral'
    const runtimes = ['python', 'node', 'wabt'].map(runtimeInfo)
    if (runtimes.some(info => info.installed && !info.healthy)) return 'error'
    return runtimes.every(info => info.healthy) ? 'success' : 'warning'
  }
  if (id === 'toolchain') {
    if (toolchainScanState.error) return 'error'
    if (!toolchainScanState.loaded) return 'neutral'
    return toolchainScanState.missing.length ? 'warning' : 'success'
  }
  return 'neutral'
}

const settingsCategories = computed(() => SETTINGS_CATEGORY_DEFINITIONS.map(category => ({
  ...category,
  label: t(category.labelKey),
  summary: categorySummary(category.id),
  health: categoryHealth(category.id),
})))

const visibleSettingsCategories = computed(() => {
  const query = settingsSearch.value.trim().toLocaleLowerCase()
  if (!query) return settingsCategories.value
  return settingsCategories.value.filter(category => (
    `${category.label} ${category.summary} ${category.keywords}`.toLocaleLowerCase().includes(query)
  ))
})

const settingsCategoryGroups = computed(() => {
  const groups = settingsSearch.value.trim()
    ? [{ id: 'results', label: t('settings.control.searchResults') }]
    : [
        { id: 'workspace', label: t('settings.control.groupWorkspace') },
        { id: 'operations', label: t('settings.control.groupOperations') },
        { id: 'local', label: t('settings.control.groupLocal') },
      ]
  return groups
    .map(group => ({
      ...group,
      items: visibleSettingsCategories.value.filter(category => (
        group.id === 'results' || category.group === group.id
      )),
    }))
    .filter(group => group.items.length)
})

function selectSettingsSection(id) {
  activeSettingsSection.value = id
  requestAnimationFrame(() => {
    settingsContentEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
  })
}
</script>

<template>
  <div class="settings-page page">
    <header class="settings-control-head">
      <div class="settings-control-title">
        <h1>{{ t('settings.title') }}</h1>
        <p>{{ t('settings.subtitle') }}</p>
      </div>
      <div class="settings-search-wrap">
        <label class="sr-only" for="settings-control-search">{{ t('settings.control.searchLabel') }}</label>
        <input
          id="settings-control-search"
          v-model="settingsSearch"
          class="settings-search"
          type="search"
          name="settings-search"
          :placeholder="t('settings.control.searchPlaceholder')"
          autocomplete="off"
        />
      </div>
      <div
        class="settings-autosave"
        :class="'state-' + autoSaveState"
        role="status"
        aria-live="polite"
        :title="autoSaveError || autoSaveLabel"
      >
        <span class="settings-autosave-dot" aria-hidden="true"></span>
        <span>{{ autoSaveLabel || t('settings.control.loading') }}</span>
        <NButton v-if="autoSaveState === 'error'" size="tiny" quaternary @click="retrySettingsState">
          {{ t('settings.autoSaveRetry') }}
        </NButton>
      </div>
    </header>

    <div class="settings-control-body">
      <nav class="settings-rail" :aria-label="t('settings.control.categoriesLabel')">
        <template v-if="settingsCategoryGroups.length">
          <div v-for="group in settingsCategoryGroups" :key="group.id" class="settings-rail-group">
            <div class="settings-rail-label">{{ group.label }}</div>
            <button
              v-for="category in group.items"
              :key="category.id"
              class="settings-nav"
              :class="{ active: activeSettingsSection === category.id }"
              type="button"
              :aria-current="activeSettingsSection === category.id ? 'page' : undefined"
              @click="selectSettingsSection(category.id)"
            >
              <span class="settings-nav-copy">
                <strong>{{ category.label }}</strong>
                <small>{{ category.summary }}</small>
              </span>
              <i class="settings-health-dot" :class="'status-' + category.health" aria-hidden="true"></i>
            </button>
          </div>
        </template>
        <div v-else class="settings-rail-empty">
          {{ t('settings.control.noSearchResults') }}
        </div>
      </nav>

      <main ref="settingsContentEl" class="settings-content">

    <!-- Appearance -->
    <section v-show="activeSettingsSection === 'appearance'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.appearanceTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.appearanceDomainHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
        <div class="settings-field">
          <label>{{ t('theme.label') }}</label>
          <NSelect
            :value="theme.preferred"
            :options="themePrefOptions"
            style="max-width: 220px"
            @update:value="setPreferred"
          />
          <div class="hint">{{ t('settings.themeHint') }}</div>
        </div>
        <div class="settings-field">
          <label>{{ t('settings.closeToTray') }}</label>
          <NSwitch
            :value="closeToTray"
            :aria-label="t('settings.closeToTray')"
            @update:value="onCloseToTrayChange"
          />
          <div class="hint">{{ t('settings.closeToTrayHint') }}</div>
        </div>
      </div>
    </section>

    <!-- AI Section -->
    <section v-show="activeSettingsSection === 'ai'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.aiTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.aiDomainHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
          <NSpace align="center" :size="8">
            <NText depth="3" style="font-size:13px">{{ t('settings.aiEnabled') }}</NText>
            <NSwitch v-model:value="settings.ai_enabled" :aria-label="t('settings.aiEnabled')" />
          </NSpace>
        </div>
      </div>

      <div class="settings-section-content">
        <NEmpty v-if="!settings.ai_enabled" :description="t('settings.aiDisabled')" style="padding:32px 0" />

        <div v-else class="ai-control-plane">
          <dl class="ai-route-health" aria-label="AI route status">
            <div>
              <dt>{{ t('settings.aiCurrentRoute') }}</dt>
              <dd>{{ primaryEndpointIndex == null ? '—' : settings.ai_endpoints[primaryEndpointIndex]?.name || t('settings.aiEndpointUnnamed') }}</dd>
            </div>
            <div>
              <dt>{{ t('settings.aiAvailableEndpoints') }}</dt>
              <dd>{{ enabledEndpointCount }} / {{ committedEndpointCount }}</dd>
            </div>
            <div>
              <dt>{{ t('settings.aiRecentProbe') }}</dt>
              <dd class="mono">{{ formatEndpointProbeTime(latestEndpointProbe?.checkedAt) }}</dd>
            </div>
            <div>
              <dt>{{ t('settings.aiRecentLatency') }}</dt>
              <dd class="mono">{{ formatEndpointLatency(latestEndpointProbe?.latencyMs) }}</dd>
            </div>
          </dl>

          <section class="settings-group ai-route-group">
            <div class="settings-group-head">
              <div>
                <h3>{{ t('settings.aiRouteOrder') }}</h3>
                <p>{{ t('settings.aiRouteHint') }}</p>
              </div>
              <div class="settings-group-actions">
                <NButton size="small" secondary :loading="testingAllEndpoints" @click="testAllEndpoints">
                  {{ t('settings.aiProbeAll') }}
                </NButton>
                <NButton size="small" type="primary" @click="addEndpoint('')">
                  {{ t('settings.aiEndpointAdd') }}
                </NButton>
              </div>
            </div>

            <NEmpty
              v-if="!sortedIndices.length"
              :description="t('settings.aiNoEndpoints')"
              class="endpoint-route-empty"
            />
            <div v-else class="endpoint-route-list" role="list">
              <article
                v-for="idx in sortedIndices"
                :key="'endpoint-route-' + idx"
                class="endpoint-route-row"
                :data-endpoint-idx="idx"
                :class="{
                  selected: endpointEditorOpen && selectedEndpointIndex === idx,
                  disabled: !endpointEnabled(idx),
                  dragging: draggingEndpointIndex === idx,
                  'drop-target': dragOverEndpointIndex === idx && draggingEndpointIndex !== idx,
                }"
                role="listitem"
              >
                <span class="endpoint-route-lead">
                  <button
                    type="button"
                    class="endpoint-drag-handle"
                    :aria-label="t('settings.aiReorderHandle')"
                    :title="t('settings.aiReorderHandle')"
                    :disabled="sortedIndices.length < 2"
                    @pointerdown="startEndpointDrag(idx, $event)"
                    @keydown.up.prevent="moveEndpoint(idx, -1)"
                    @keydown.down.prevent="moveEndpoint(idx, 1)"
                  >
                    <svg viewBox="0 0 10 16" width="10" height="16" aria-hidden="true">
                      <circle cx="2.5" cy="3" r="1.3" /><circle cx="7.5" cy="3" r="1.3" />
                      <circle cx="2.5" cy="8" r="1.3" /><circle cx="7.5" cy="8" r="1.3" />
                      <circle cx="2.5" cy="13" r="1.3" /><circle cx="7.5" cy="13" r="1.3" />
                    </svg>
                  </button>
                  <span class="endpoint-route-order">{{ endpointOrder(idx) }}</span>
                </span>
                <button
                  type="button"
                  class="endpoint-route-identity"
                  :aria-pressed="endpointEditorOpen && selectedEndpointIndex === idx"
                  @click="selectEndpoint(idx)"
                >
                  <span class="endpoint-route-title-line">
                    <i class="endpoint-state-dot" :class="'state-' + endpointHealthState(idx)" aria-hidden="true"></i>
                    <strong>{{ settings.ai_endpoints[idx].name || t('settings.aiEndpointUnnamed') }}</strong>
                    <span v-if="endpointHasProvider(idx)" class="endpoint-provider-tag" :title="settings.ai_endpoints[idx].base_url">{{ endpointProviderLabel(settings.ai_endpoints[idx].provider_id) }}<em v-if="endpointProviderRegion(settings.ai_endpoints[idx].provider_id)"> · {{ regionLabel(endpointProviderRegion(settings.ai_endpoints[idx].provider_id)) }}</em></span>
                    <em class="endpoint-inline-state">{{ endpointHealthLabel(idx) }}</em>
                  </span>
                  <small class="mono">{{ settings.ai_endpoints[idx].base_url || t('settings.aiEndpointUrlMissing') }}</small>
                </button>
                <div class="endpoint-route-model">
                  <strong>{{ settings.ai_endpoints[idx].model || t('settings.aiEndpointModelMissing') }}</strong>
                  <small>{{ endpointProtocolLabel(settings.ai_endpoints[idx]) }}</small>
                </div>
                <div class="endpoint-route-probe">
                  <strong>{{ endpointProbeLabel(idx) }}</strong>
                  <small class="mono">{{ formatEndpointLatency(endpointHealth[idx]?.latencyMs) }}</small>
                </div>
                <label class="endpoint-route-enable">
                  <NSwitch
                    size="small"
                    :value="endpointEnabled(idx)"
                    :aria-label="t('settings.aiEndpointEnabled')"
                    @update:value="setEndpointEnabled(idx, $event)"
                  />
                </label>
                <div class="endpoint-route-actions">
                  <NButton size="tiny" quaternary :loading="testingId === idx" @click="testEndpoint(idx)">
                    {{ t('settings.testBtn') }}
                  </NButton>
                  <NButton size="tiny" quaternary @click="selectEndpoint(idx)">
                    {{ t('settings.aiEditEndpoint') }}
                  </NButton>
                  <button
                    type="button"
                    class="endpoint-delete-btn"
                    :aria-label="t('settings.aiDeleteEndpoint')"
                    :title="t('settings.aiDeleteEndpoint')"
                    @click="removeEndpoint(idx)"
                  >
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                      <path
                        d="M6.5 2.5h3M2.5 4.5h11M4.5 4.5l.6 8.2a1 1 0 0 0 1 .8h3.8a1 1 0 0 0 1-.8l.6-8.2M6.6 7v4M9.4 7v4"
                        fill="none" stroke="currentColor" stroke-width="1.2"
                        stroke-linecap="round" stroke-linejoin="round"
                      />
                    </svg>
                  </button>
                </div>
              </article>
            </div>
          </section>

          <WorkbenchModal
            :open="endpointEditorOpen && Boolean(selectedEndpoint)"
            size="lg"
            :close-label="t('common.close')"
            :close-on-esc="!providerPickerOpen"
            @close="closeEndpointEditor"
          >
            <template #heading>
              <div v-if="providerPickerOpen" class="endpoint-editor-head">
                <div>
                  <span class="settings-group-kicker">{{ t('settings.aiProviderSection') }}</span>
                  <h2>{{ t('settings.aiProviderPickerTitle') }}</h2>
                  <p>{{ t('settings.aiProviderPickerHint') }}</p>
                </div>
              </div>
              <div v-else class="endpoint-editor-head">
                <div>
                  <span class="settings-group-kicker">{{ t('settings.aiEndpointEditor') }}</span>
                  <h2>
                    {{ selectedEndpoint.name || t('settings.aiEndpointUnnamed') }}
                  </h2>
                  <p>{{ t('settings.aiEndpointEditorHint') }}</p>
                </div>
                <label class="endpoint-enabled-control">
                  <span>{{ selectedEndpoint.enabled === false ? t('settings.aiEndpointDisabled') : t('settings.aiEndpointEnabled') }}</span>
                  <NSwitch v-model:value="selectedEndpoint.enabled" size="small" />
                </label>
              </div>
            </template>

            <AIProviderPicker
              v-if="providerPickerOpen"
              :presets="providerPresets"
              :categories="providerCategories"
              :selected-id="selectedEndpoint.provider_id || ''"
              :loading="!providerPresetsLoaded"
              @select="selectProviderFromPicker"
            />
            <div v-else class="endpoint-editor-body">
            <div class="endpoint-editor-section endpoint-provider-section">
              <h4>
                <span>{{ t('settings.aiProviderSection') }}</span>
                <span v-if="selectedEndpoint.provider_id && selectedEndpoint.provider_id !== 'custom'" class="endpoint-provider-badge">
                  {{ regionLabel(endpointProviderRegion(selectedEndpoint.provider_id)) }}
                </span>
              </h4>
              <div class="endpoint-provider-current">
                <div class="endpoint-provider-current-copy">
                  <strong>{{ endpointProviderLabel(selectedEndpoint.provider_id) }}</strong>
                  <span v-if="providerById(selectedEndpoint.provider_id)">
                    {{ selectedEndpoint.protocol === 'anthropic' ? 'Anthropic Messages' : 'OpenAI Chat' }}
                    · {{ regionLabel(endpointProviderRegion(selectedEndpoint.provider_id)) }}
                  </span>
                  <span v-else>{{ t('settings.aiProviderCustomHint') }}</span>
                  <small class="mono">
                    {{ selectedEndpoint.base_url || t('settings.aiEndpointUrlMissing') }}
                  </small>
                </div>
                <NButton size="small" secondary @click="openProviderPicker">
                  {{ t('settings.aiProviderChoose') }}
                </NButton>
              </div>
              <div v-if="providerById(selectedEndpoint.provider_id)" class="endpoint-provider-meta">
                <span v-if="providerById(selectedEndpoint.provider_id).website_url" class="endpoint-provider-console">
                  <a :href="providerById(selectedEndpoint.provider_id).website_url" target="_blank" rel="noopener noreferrer">{{ t('settings.aiProviderWebsite') }}</a>
                </span>
                <span v-if="providerById(selectedEndpoint.provider_id).api_key_url" class="endpoint-provider-console">
                  <a :href="providerById(selectedEndpoint.provider_id).api_key_url" target="_blank" rel="noopener noreferrer">{{ t('settings.aiProviderGetKey') }}</a>
                </span>
              </div>
              <div v-if="providerById(selectedEndpoint.provider_id)?.requires_base_url_edit" class="endpoint-provider-meta">
                <span class="endpoint-provider-warn">{{ t('settings.aiProviderTemplateNeedsUrl') }}</span>
              </div>
              <div v-if="providerById(selectedEndpoint.provider_id) && providerById(selectedEndpoint.provider_id).note" class="endpoint-provider-meta">
                <span class="endpoint-provider-note">{{ providerById(selectedEndpoint.provider_id).note }}</span>
              </div>
              <div v-if="!providerById(selectedEndpoint.provider_id) && selectedEndpoint.provider_id && selectedEndpoint.provider_id !== 'custom'" class="endpoint-provider-meta">
                <span class="endpoint-provider-warn">{{ t('settings.aiProviderUnsupported') }}</span>
              </div>

              <!-- Change preview: applying a preset would overwrite user values -->
              <div v-if="pendingProviderApply" class="endpoint-provider-preview">
                <p class="endpoint-provider-preview-title">{{ t('settings.aiProviderPreviewTitle') }} — {{ pendingProviderApply.preset.name }}</p>
                <ul v-if="pendingProviderApply.rows.length" class="endpoint-provider-preview-rows">
                  <li v-for="row in pendingProviderApply.rows" :key="row.key">
                    <em>{{ row.key }}</em>
                    <span class="mono">{{ row.before }}</span>
                    <i>→</i>
                    <span class="mono endpoint-provider-preview-after">{{ row.after }}</span>
                  </li>
                </ul>
                <p v-else class="endpoint-provider-preview-rows-none">{{ t('settings.aiProviderPreviewNoOp') }}</p>
                <p class="endpoint-provider-preview-key">{{ t('settings.aiProviderPreserveKey') }}</p>
                <p v-if="pendingProviderApply.preset.note" class="endpoint-provider-preview-note">{{ pendingProviderApply.preset.note }}</p>
                <div class="endpoint-provider-preview-actions">
                  <NButton size="small" type="primary" @click="applyProviderNow">{{ t('settings.aiProviderApply') }}</NButton>
                  <NButton size="small" quaternary @click="cancelProviderApply">{{ t('settings.aiProviderCancel') }}</NButton>
                </div>
              </div>
            </div>

            <div class="endpoint-editor-section">
              <h4>{{ t('settings.aiConnectionSection') }}</h4>
              <div class="form-row">
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointName') }}</label>
                  <NInput
                    v-model:value="selectedEndpoint.name"
                    :aria-label="t('settings.aiEndpointName')"
                    placeholder="e.g. DeepSeek, Ollama"
                  />
                </div>
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointProtocol') }}</label>
                  <NSelect
                    v-model:value="selectedEndpoint.protocol"
                    :aria-label="t('settings.aiEndpointProtocol')"
                    :options="[{ label: 'OpenAI Compatible', value: 'openai' }, { label: 'Anthropic', value: 'anthropic' }]"
                  />
                </div>
              </div>
              <div class="form-row">
                <div class="form-col-full">
                  <label>{{ t('settings.aiEndpointUrl') }}</label>
                  <NInput
                    v-model:value="selectedEndpoint.base_url"
                    :aria-label="t('settings.aiEndpointUrl')"
                    placeholder="https://api.example.com/v1"
                    @input="onBaseUrlChange(selectedEndpointIndex)"
                  />
                </div>
              </div>
              <div class="form-row">
                <div class="form-col-full">
                  <label>{{ t('settings.aiEndpointKey') }}</label>
                  <NInput
                    v-model:value="selectedEndpoint.api_key"
                    :aria-label="t('settings.aiEndpointKey')"
                    type="password"
                    show-password-on="click"
                    autocomplete="off"
                    placeholder="sk-..."
                  />
                </div>
              </div>
            </div>

            <div class="endpoint-editor-section">
              <h4>{{ t('settings.aiModelSection') }}</h4>
              <div class="form-row">
                <div class="form-col endpoint-model-field">
                  <label>{{ t('settings.aiEndpointModel') }}</label>
                  <div class="endpoint-model-control">
                    <NSelect
                      v-if="(modelCache[selectedEndpointIndex] || {}).models?.length"
                      v-model:value="selectedEndpoint.model"
                      :aria-label="t('settings.aiEndpointModel')"
                      :options="endpointModelOptions(selectedEndpointIndex)"
                      :placeholder="t('settings.selectModel')"
                      filterable
                      tag
                    />
                    <NInput
                      v-else
                      v-model:value="selectedEndpoint.model"
                      :aria-label="t('settings.aiEndpointModel')"
                      placeholder="gpt-5.5 / claude-sonnet-5 / kimi-k3"
                    />
                    <NButton
                      size="small"
                      secondary
                      :loading="(modelCache[selectedEndpointIndex] || {}).loading"
                      @click="fetchModels(selectedEndpointIndex)"
                    >{{ t('settings.fetchModels') }}</NButton>
                  </div>
                  <NText v-if="(modelCache[selectedEndpointIndex] || {}).manual" type="info" class="endpoint-field-hint">
                    {{ t('settings.fetchModelsManual') }}
                  </NText>
                  <NText v-if="(modelCache[selectedEndpointIndex] || {}).error" type="error" class="endpoint-field-error">
                    {{ t('settings.fetchModelsFail') }}: {{ (modelCache[selectedEndpointIndex] || {}).error }}
                  </NText>
                  <label class="endpoint-catalog-label">{{ t('settings.aiEndpointModels') }}</label>
                  <NSelect
                    :value="selectedEndpoint.models || []"
                    :aria-label="t('settings.aiEndpointModels')"
                    :options="endpointModelOptions(selectedEndpointIndex)"
                    :placeholder="t('settings.aiEndpointModelsPlaceholder')"
                    multiple
                    filterable
                    tag
                    @update:value="setEndpointCatalog(selectedEndpointIndex, $event)"
                  />
                  <NText type="info" class="endpoint-field-hint">
                    {{ t('settings.aiEndpointModelsHint') }}
                  </NText>
                  <div class="endpoint-capability-status">
                    <strong>{{ currentModelCapabilitySummary }}</strong>
                    <span>{{ currentModelCapabilitySource }} · {{ t('settings.aiCapabilityAutoHint') }}</span>
                    <NButton
                      v-if="currentModelHasUserCapabilityOverride"
                      size="tiny"
                      quaternary
                      @click="clearEndpointModelCapabilityOverrides"
                    >
                      {{ t('settings.aiCapabilityResetAuto') }}
                    </NButton>
                  </div>
                </div>
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointPriority') }}</label>
                  <NInputNumber
                    v-model:value="selectedEndpoint.priority"
                    :aria-label="t('settings.aiEndpointPriority')"
                    :show-button="false"
                    placeholder="0"
                    style="width:100%"
                  />
                  <div class="hint">{{ t('settings.aiPriorityHint') }}</div>
                </div>
              </div>
              <div class="form-row">
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointMaxTokens') }}</label>
                  <NInput
                    :value="maxTokensInputValue(selectedEndpointIndex)"
                    :aria-label="t('settings.aiEndpointMaxTokens')"
                    type="number"
                    placeholder="8"
                    @update:value="updateMaxTokensDraft(selectedEndpointIndex, $event)"
                    @blur="commitMaxTokensDraft(selectedEndpointIndex)"
                  />
                  <div class="hint">{{ t('settings.aiEndpointMaxTokensHint') }}</div>
                </div>
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointContextWindow') }}</label>
                  <NInput
                    :value="contextWindowInputValue(selectedEndpointIndex)"
                    :aria-label="t('settings.aiEndpointContextWindow')"
                    type="number"
                    placeholder="128"
                    @update:value="updateContextWindowDraft(selectedEndpointIndex, $event)"
                    @blur="commitContextWindowDraft(selectedEndpointIndex)"
                  />
                  <div class="hint">{{ t('settings.aiEndpointContextWindowHint') }}</div>
                </div>
              </div>
              <div class="form-row">
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointTimeout') }}</label>
                  <NInputNumber
                    v-model:value="selectedEndpoint.timeout"
                    :aria-label="t('settings.aiEndpointTimeout')"
                    :min="1"
                    :show-button="false"
                    placeholder="300"
                    style="width:100%"
                  />
                  <div class="hint">{{ t('settings.aiEndpointTimeoutHint') }}</div>
                </div>
                <div class="form-col">
                  <label>{{ t('settings.aiEndpointTemperature') }}</label>
                  <NInputNumber
                    v-model:value="selectedEndpoint.temperature"
                    :aria-label="t('settings.aiEndpointTemperature')"
                    :step="0.1"
                    :show-button="false"
                    placeholder="0.7"
                    :disabled="selectedEndpoint.send_temperature === false"
                    style="width:100%"
                  />
                  <label class="inline-switch">
                    <NSwitch v-model:value="selectedEndpoint.send_temperature" />
                    <span>{{ t('settings.aiEndpointSendTemperature') }}</span>
                  </label>
                  <div class="hint">{{ t('settings.aiEndpointSendTemperatureHint') }}</div>
                </div>
              </div>
            </div>

            <details class="endpoint-advanced">
              <summary>{{ t('settings.aiAdvancedSection') }}</summary>
              <div class="endpoint-advanced-body">
                <div class="form-row">
                  <div class="form-col-full">
                    <label>{{ t('settings.aiCapabilityOverrideTitle') }}</label>
                    <div class="hint">{{ t('settings.aiCapabilityOverrideHint', { model: selectedEndpoint.model || '—' }) }}</div>
                    <div class="capability-override-row">
                      <label class="inline-switch">
                        <NSwitch
                          :value="!!currentModelCapabilities.image_in"
                          @update:value="value => setEndpointModelCapability('image_in', value)"
                        />
                        <span>{{ t('settings.aiEndpointImageIn') }}</span>
                      </label>
                      <label class="inline-switch">
                        <NSwitch
                          :value="!!currentModelCapabilities.video_in"
                          @update:value="value => setEndpointModelCapability('video_in', value)"
                        />
                        <span>{{ t('settings.aiEndpointVideoIn') }}</span>
                      </label>
                      <label class="inline-switch">
                        <NSwitch
                          :value="!!currentModelCapabilities.audio_in"
                          @update:value="value => setEndpointModelCapability('audio_in', value)"
                        />
                        <span>{{ t('settings.aiEndpointAudioIn') }}</span>
                      </label>
                    </div>
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-col-full">
                    <label>{{ t('settings.aiEndpointExtraBody') }}</label>
                    <NInput
                      v-model:value="selectedEndpoint.extra_body_json"
                      :aria-label="t('settings.aiEndpointExtraBody')"
                      type="textarea"
                      :autosize="{ minRows: 3, maxRows: 8 }"
                      placeholder='{"top_p": 0.9}'
                    />
                    <div class="hint">{{ t('settings.aiEndpointExtraBodyHint') }}</div>
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-col-full">
                    <label>{{ t('settings.aiEndpointExtraHeaders') }}</label>
                    <NInput
                      v-model:value="selectedEndpoint.extra_headers_json"
                      :aria-label="t('settings.aiEndpointExtraHeaders')"
                      type="textarea"
                      :autosize="{ minRows: 2, maxRows: 6 }"
                      placeholder='{"HTTP-Referer": "https://argusai.local"}'
                    />
                    <div class="hint">{{ t('settings.aiEndpointExtraHeadersHint') }}</div>
                  </div>
                </div>
              </div>
            </details>
            </div>
            <template #footer>
              <div class="modal-actions endpoint-editor-actions">
                <NButton v-if="providerPickerOpen" size="small" @click="providerPickerOpen = false">
                  {{ t('common.back') }}
                </NButton>
                <template v-else-if="Number.isInteger(draftEndpointIndex)">
                  <NButton size="small" @click="closeEndpointEditor">{{ t('common.cancel') }}</NButton>
                  <NButton
                    size="small"
                    type="primary"
                    :disabled="!canCommitEndpointDraft"
                    :title="!canCommitEndpointDraft ? t('settings.aiEndpointDraftIncomplete') : undefined"
                    @click="commitEndpointDraft"
                  >
                    {{ t('settings.aiSaveEndpoint') }}
                  </NButton>
                </template>
                <NButton v-else size="small" @click="closeEndpointEditor">{{ t('common.close') }}</NButton>
              </div>
            </template>
          </WorkbenchModal>

          <section class="settings-group auxiliary-group">
            <div class="settings-group-head">
              <div>
                <h3>{{ t('settings.aiAuxiliaryTitle') }}</h3>
                <p>{{ t('settings.aiAuxiliaryHint') }}</p>
              </div>
            </div>
            <div class="auxiliary-grid">
              <section v-for="auxKey in ['vision', 'asr']" :key="auxKey" class="auxiliary-endpoint">
                <header>
                  <div>
                    <h4>{{ auxKey === 'vision' ? t('settings.aiAuxiliaryVision') : t('settings.aiAuxiliaryAsr') }}</h4>
                    <small>{{ settings.ai_auxiliary[auxKey].model || t('settings.aiEndpointModelMissing') }}</small>
                  </div>
                  <NSwitch v-model:value="settings.ai_auxiliary[auxKey].enabled" size="small" />
                </header>
                <p
                  v-if="settings.ai_auxiliary[auxKey].base_url && settings.ai_auxiliary[auxKey].model && settings.ai_auxiliary[auxKey].api_key && !settings.ai_auxiliary[auxKey].enabled"
                  class="auxiliary-warning"
                >
                  {{ t('settings.aiAuxiliaryDisabledHint') }}
                </p>
                <div class="auxiliary-fields">
                  <div class="form-row">
                    <div class="form-col">
                      <label>{{ t('settings.aiEndpointName') }}</label>
                      <NInput
                        v-model:value="settings.ai_auxiliary[auxKey].name"
                        :aria-label="`${auxKey === 'vision' ? t('settings.aiAuxiliaryVision') : t('settings.aiAuxiliaryAsr')} · ${t('settings.aiEndpointName')}`"
                      />
                    </div>
                    <div class="form-col">
                      <label>{{ t('settings.aiEndpointModel') }}</label>
                      <NInput
                        v-model:value="settings.ai_auxiliary[auxKey].model"
                        :aria-label="`${auxKey === 'vision' ? t('settings.aiAuxiliaryVision') : t('settings.aiAuxiliaryAsr')} · ${t('settings.aiEndpointModel')}`"
                      />
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-col-full">
                      <label>{{ t('settings.aiEndpointUrl') }}</label>
                      <NInput
                        v-model:value="settings.ai_auxiliary[auxKey].base_url"
                        :aria-label="`${auxKey === 'vision' ? t('settings.aiAuxiliaryVision') : t('settings.aiAuxiliaryAsr')} · ${t('settings.aiEndpointUrl')}`"
                        placeholder="https://api.example.com/v1"
                      />
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-col-full">
                      <label>{{ t('settings.aiEndpointKey') }}</label>
                      <div class="auxiliary-key-field">
                        <NInput
                          v-model:value="settings.ai_auxiliary[auxKey].api_key"
                          :aria-label="`${auxKey === 'vision' ? t('settings.aiAuxiliaryVision') : t('settings.aiAuxiliaryAsr')} · ${t('settings.aiEndpointKey')}`"
                          type="password"
                          show-password-on="click"
                          autocomplete="off"
                        />
                        <NButton
                          size="small"
                          secondary
                          type="error"
                          :disabled="!settings.ai_auxiliary[auxKey].api_key"
                          @click="clearAuxiliaryApiKey(auxKey)"
                        >
                          {{ t('settings.aiAuxiliaryClearKey') }}
                        </NButton>
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          </section>
        </div>
      </div>
    </section>

    <!-- Assistant runtime strategy -->
    <section v-show="activeSettingsSection === 'assistant'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.assistantRuntimeTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.assistantRuntimeAdvice') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.assistantAutoApprove') }}</label>
          <div style="display:flex;align-items:center;min-height:34px">
            <NSwitch v-model:value="settings.assistant.auto_approve" />
          </div>
          <div class="hint">{{ t('settings.assistantAutoApproveHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.teamMaxParallel') }}</label>
          <NInputNumber
            v-model:value="settings.assistant.team_max_parallel"
            :min="1"
            :max="64"
            :step="1"
            style="width:140px"
          />
          <div class="hint">{{ t('settings.teamMaxParallelHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.assistantTurnDeadline') }}</label>
          <NSelect
            v-model:value="settings.assistant.turn_deadline_seconds"
            :options="assistantTurnDeadlineOptions"
          />
          <div class="hint">{{ t('settings.assistantTurnDeadlineHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.assistantDesktopNotifications') }}</label>
          <div style="display:flex;align-items:center;min-height:34px">
            <NSwitch v-model:value="settings.assistant.desktop_notifications" />
          </div>
          <div class="hint">{{ t('settings.assistantDesktopNotificationsHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.assistantCronWebhookHost') }}</label>
          <input v-model="settings.assistant.cron_webhook_host" type="text" placeholder="127.0.0.1" />
          <div class="hint">{{ t('settings.assistantCronWebhookHostHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.assistantCronWebhookPort') }}</label>
          <input v-model.number="settings.assistant.cron_webhook_port" type="number" min="1" max="65535" />
          <div class="hint">{{ t('settings.assistantCronWebhookPortHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.assistantThinkingEffort') }}</label>
          <NSelect
            v-model:value="settings.assistant.thinking_effort"
            :options="assistantThinkingEffortOptions"
          />
          <div class="hint">{{ t('settings.assistantThinkingEffortHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.assistantDefaultShell') }}</label>
          <NSelect
            v-model:value="settings.assistant.default_shell"
            :options="defaultShellOptions"
          />
          <div class="hint">{{ t('settings.assistantDefaultShellHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col-full">
          <label>{{ t('settings.assistantAutoArchiveTools') }}</label>
          <div class="assistant-tool-actions">
            <NButton size="tiny" @click="setAssistantArchiveRecommended">{{ t('settings.assistantArchiveRecommended') }}</NButton>
            <NButton size="tiny" quaternary @click="addAssistantArchiveByKind('http')">{{ t('settings.assistantArchiveAddHttp') }}</NButton>
            <NButton size="tiny" quaternary @click="addAssistantArchiveByKind('browser')">{{ t('settings.assistantArchiveAddBrowser') }}</NButton>
            <NButton size="tiny" quaternary @click="addAssistantArchiveByKind('sqli')">{{ t('settings.assistantArchiveAddSqli') }}</NButton>
            <NButton size="tiny" quaternary @click="selectAllAssistantArchiveTools">{{ t('settings.assistantArchiveSelectAll') }}</NButton>
            <NButton size="tiny" quaternary @click="clearAssistantArchiveTools">{{ t('settings.assistantArchiveClear') }}</NButton>
            <span class="hint">{{ t('settings.assistantArchiveSelected', { count: (settings.assistant.auto_archive_tools || []).length }) }}</span>
          </div>
          <ListToolbar
            v-model="archiveToolQuery"
            :placeholder="t('settings.assistantAutoArchiveToolsPlaceholder')"
            @focusin="loadAssistantToolOptions"
          />
          <div class="assistant-tool-panel" @focusin="loadAssistantToolOptions">
            <label
              v-for="tool in archiveToolPageItems"
              :key="tool.value"
              class="assistant-tool-row"
              :class="{ selected: assistantToolSelected(tool.value) }"
            >
              <input
                type="checkbox"
                :name="'assistant-archive-' + tool.value"
                :aria-label="tool.name || tool.value"
                :checked="assistantToolSelected(tool.value)"
                @change="toggleAssistantArchiveTool(tool.value, $event.target.checked)"
              />
              <span class="assistant-tool-main">
                <span class="assistant-tool-name">{{ tool.name || tool.value }}</span>
                <span v-if="tool.description" class="assistant-tool-desc">{{ tool.description }}</span>
              </span>
              <NTag v-if="tool.risk" size="small" :bordered="false" :type="tool.risk === 'high' ? 'error' : tool.risk === 'medium' ? 'warning' : 'default'">
                {{ tool.risk }}
              </NTag>
            </label>
            <div v-if="!archiveToolTotal" class="assistant-tool-empty">
              {{ t('settings.assistantArchiveNoTools') }}
            </div>
          </div>
          <ListPager
            v-model:page="archiveToolPage"
            v-model:page-size="archiveToolPageSize"
            :page-count="archiveToolPageCount"
            :total="archiveToolTotal"
          />
          <div class="hint">{{ t('settings.assistantAutoArchiveToolsHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col-full">
          <label>{{ t('settings.assistantToolAlwaysAllow') }}</label>
          <div class="assistant-tool-actions">
            <NButton size="tiny" @click="setAssistantAlwaysAllowRecommended">{{ t('settings.assistantAlwaysAllowRecommended') }}</NButton>
            <span class="hint">{{ t('settings.assistantAlwaysAllowSelected', { count: (settings.assistant.tool_always_allow || []).length }) }}</span>
          </div>
          <ListToolbar
            v-model="alwaysAllowToolQuery"
            :placeholder="t('settings.assistantAutoArchiveToolsPlaceholder')"
            @focusin="loadAssistantToolOptions"
          />
          <div class="assistant-tool-panel" @focusin="loadAssistantToolOptions">
            <label
              v-for="tool in alwaysAllowToolPageItems"
              :key="'allow-' + tool.value"
              class="assistant-tool-row"
              :class="{ selected: assistantAlwaysAllowSelected(tool.value) }"
            >
              <input
                type="checkbox"
                :name="'assistant-always-allow-' + tool.value"
                :aria-label="tool.name || tool.value"
                :checked="assistantAlwaysAllowSelected(tool.value)"
                @change="toggleAssistantAlwaysAllowTool(tool.value, $event.target.checked)"
              />
              <span class="assistant-tool-main">
                <span class="assistant-tool-name">{{ tool.name || tool.value }}</span>
                <span v-if="tool.description" class="assistant-tool-desc">{{ tool.description }}</span>
              </span>
            </label>
            <div v-if="!alwaysAllowToolTotal" class="assistant-tool-empty">
              {{ t('settings.assistantArchiveNoTools') }}
            </div>
          </div>
          <ListPager
            v-model:page="alwaysAllowToolPage"
            v-model:page-size="alwaysAllowToolPageSize"
            :page-count="alwaysAllowToolPageCount"
            :total="alwaysAllowToolTotal"
          />
          <div class="hint">{{ t('settings.assistantToolAlwaysAllowHint') }}</div>
        </div>
      </div>
      </div>
    </section>

    <section v-show="activeSettingsSection === 'automation'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.control.automationTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.control.automationHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
        <div class="integration-summary">
          <section class="host-service-block" :aria-labelledby="'host-service-summary'">
            <div class="host-service-top">
              <div class="integration-copy">
                <h3 id="host-service-summary">{{ t('settings.hostService.title') }}</h3>
                <p>{{ t(hostServiceHintKey) }}</p>
              </div>
              <div class="integration-facts" role="status" aria-live="polite">
                <div>
                  <span>{{ t('settings.hostService.status') }}</span>
                  <strong>
                    {{
                      !hostService.loaded ? '—'
                        : !hostService.installed ? t('settings.hostService.notInstalled')
                          : hostService.requires_approval ? t('settings.hostService.macosPending')
                          : hostService.state === 'disabled_in_settings' ? t('settings.hostService.disabledInSettings')
                          : hostService.active ? t('settings.hostService.active')
                            : t('settings.hostService.stopped')
                    }}
                  </strong>
                </div>
              </div>
              <div class="host-service-enable">
                <span class="hint">{{ t('settings.hostService.enable') }}</span>
                <NSwitch
                  :value="hostService.installed"
                  :loading="hostService.busy || hostService.loading"
                  @update:value="onHostServiceToggle"
                />
                <NButton size="tiny" quaternary :loading="hostService.loading" @click="loadHostService">
                  {{ t('settings.hostService.refresh') }}
                </NButton>
              </div>
            </div>

            <div v-if="(hostServiceIsWindows || hostServiceIsDarwin) && !hostServiceShowAdvanced" class="host-service-advanced-toggle">
              <NButton size="tiny" quaternary @click="hostServiceAdvancedOpen = true">
                {{ t('settings.hostService.advanced') }}
              </NButton>
            </div>
            <div v-if="hostServiceShowAdvanced" class="host-service-controls">
              <label v-if="hostServiceBootOptions.length > 1" class="host-service-field">
                <span>{{ t('settings.hostService.bootPolicy') }}</span>
                <NSelect
                  size="small"
                  :value="hostService.boot_policy"
                  :options="hostServiceBootOptions"
                  :disabled="hostService.busy || !hostServiceBootOptions.length"
                  @update:value="onHostServiceBootChange"
                />
                <span v-if="hostServicePolicyHint" class="hint">{{ hostServicePolicyHint }}</span>
              </label>
              <label class="host-service-field host-service-field--switch">
                <span>{{ t('settings.hostService.cronOnly') }}</span>
                <NSwitch
                  :value="hostService.cron_only"
                  :disabled="hostService.busy"
                  @update:value="v => { hostService.cron_only = !!v; if (hostService.installed) onHostServiceToggle(true) }"
                />
              </label>
              <NButton
                v-if="(hostServiceIsWindows || hostServiceIsDarwin) && hostServiceAdvancedOpen && hostService.boot_policy !== 'startup' && !hostService.cron_only"
                size="tiny"
                quaternary
                @click="hostServiceAdvancedOpen = false"
              >
                {{ t('settings.hostService.advancedHide') }}
              </NButton>
            </div>

            <p v-if="hostServiceNeedsAdmin" class="host-service-alert">
              {{ t('settings.hostService.needsAdminStartup') }}
              {{ t('settings.hostService.needsAdminStartupAction') }}
            </p>
            <p v-if="hostService.requires_approval" class="host-service-alert">
              {{ t('settings.hostService.macosNeedsApproval') }}
              <NButton size="tiny" quaternary @click="openMacosLoginItems">
                {{ t('settings.hostService.macosOpenLoginItems') }}
              </NButton>
            </p>
            <p v-if="hostService.channel_host_held" class="host-service-alert">
              {{ t('settings.hostService.conflict') }}
            </p>
            <ul v-if="hostServiceNotes.length" class="host-service-notes">
              <li v-for="(note, idx) in hostServiceNotes" :key="'hsn-'+idx">{{ note }}</li>
            </ul>
          </section>

          <section class="integration-row" :aria-labelledby="'cron-settings-summary'">
            <div class="integration-copy">
              <h3 id="cron-settings-summary">{{ t('settings.cronTitle') }}</h3>
              <p>{{ t('settings.cronHint') }}</p>
            </div>
            <div class="integration-facts" role="status" aria-live="polite">
              <div>
                <span>{{ t('settings.control.totalLabel') }}</span>
                <strong>{{ cronLoaded ? cronJobs.length : '—' }}</strong>
              </div>
              <div>
                <span>{{ t('settings.cronEnabled') }}</span>
                <strong>{{ cronLoaded ? cronJobs.filter(job => job.enabled).length : '—' }}</strong>
              </div>
              <div>
                <span>{{ t('settings.cronDisabled') }}</span>
                <strong>{{ cronLoaded ? cronJobs.filter(job => !job.enabled).length : '—' }}</strong>
              </div>
            </div>
            <div class="integration-actions">
              <NButton size="small" secondary @click="navigate('/cron')">{{ t('settings.cronOpenPanel') }}</NButton>
              <NButton size="small" quaternary :loading="cronLoading" @click="loadCronJobs">{{ t('settings.cronRefresh') }}</NButton>
            </div>
          </section>

          <section class="integration-row" :aria-labelledby="'mcp-settings-summary'">
            <div class="integration-copy">
              <h3 id="mcp-settings-summary">{{ t('mcp.title') }}</h3>
              <p>{{ t('settings.control.mcpSummary') }}</p>
            </div>
            <div class="integration-actions">
              <NButton size="small" secondary @click="navigate('/mcp')">{{ t('settings.control.openMcp') }}</NButton>
            </div>
          </section>

          <section class="integration-row document-parse-block" :aria-labelledby="'document-parse-summary'">
            <div class="integration-copy">
              <h3 id="document-parse-summary">{{ t('settings.documentParse.title') }}</h3>
              <p>{{ t('settings.documentParse.hint') }}</p>
            </div>
            <div class="host-service-enable">
              <span class="hint">{{ t('settings.documentParse.mineruEnabled') }}</span>
              <NSwitch v-model:value="settings.document_parse.mineru.enabled" size="small" />
            </div>
            <div v-if="settings.document_parse.mineru.enabled" class="document-parse-fields">
              <div class="form-row">
                <div class="form-col-full">
                  <label>{{ t('settings.documentParse.baseUrl') }}</label>
                  <NInput v-model:value="settings.document_parse.mineru.base_url" placeholder="https://mineru.net" />
                  <div class="hint">{{ t('settings.documentParse.baseUrlHint') }}</div>
                </div>
              </div>
              <div class="form-row">
                <div class="form-col-full">
                  <label>{{ t('settings.documentParse.apiKey') }}</label>
                  <div class="auxiliary-key-field">
                    <NInput
                      v-model:value="settings.document_parse.mineru.api_key"
                      type="password"
                      show-password-on="click"
                      :placeholder="t('settings.documentParse.apiKeyPlaceholder')"
                    />
                    <NButton
                      size="tiny"
                      quaternary
                      :disabled="!settings.document_parse.mineru.api_key"
                      @click="clearMineruApiKey"
                    >
                      {{ t('settings.documentParse.clearKey') }}
                    </NButton>
                  </div>
                  <div class="hint">{{ t('settings.documentParse.apiKeyHint') }}</div>
                </div>
              </div>
              <div class="form-row">
                <div class="form-col">
                  <label>{{ t('settings.documentParse.modelVersion') }}</label>
                  <NSelect v-model:value="settings.document_parse.mineru.model_version" :options="mineruModelOptions" />
                </div>
                <div class="form-col">
                  <label>{{ t('settings.documentParse.timeout') }}</label>
                  <NInput v-model:value.number="settings.document_parse.mineru.timeout_seconds" type="number" placeholder="300" />
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    </section>

    <section v-show="activeSettingsSection === 'user_habits'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.control.userHabitsTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.control.userHabitsHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
        <div class="form-row"><div class="form-col-full"><label>{{ t('settings.control.speakingStyle') }}</label><NInput v-model:value="settings.user_habits.speaking_style" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" /></div></div>
        <div class="form-row"><div class="form-col-full"><label>{{ t('settings.control.taskFocus') }}</label><NInput v-model:value="settings.user_habits.task_focus" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" /></div></div>
        <div class="form-row"><div class="form-col-full"><label>{{ t('settings.control.auditDomains') }}</label><NInput :value="(settings.user_habits.audit_domains||[]).join('\n')" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" @update:value="v => settings.user_habits.audit_domains = String(v||'').split(/[\n,]+/).map(s=>s.trim()).filter(Boolean)" /></div></div>
        <div class="form-row"><div class="form-col"><label>{{ t('settings.control.riskPreference') }}</label><NInput v-model:value="settings.user_habits.risk_preference" /></div><div class="form-col"><label>{{ t('settings.control.outputFormat') }}</label><NInput v-model:value="settings.user_habits.output_format" /></div></div>
        <div class="form-row"><div class="form-col-full"><label>{{ t('settings.control.avoid') }}</label><NInput v-model:value="settings.user_habits.avoid" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" /></div></div>
        <div class="form-row"><div class="form-col-full"><label>{{ t('settings.control.notes') }}</label><NInput v-model:value="settings.user_habits.notes" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" /></div></div>
      </div>
    </section>

    <!-- Logging -->
    <section v-show="activeSettingsSection === 'logging'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.logging.title') }}</h2>
          <p class="settings-head-desc">{{ t('settings.logging.domainHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.logging.level') }}</label>
          <NSelect v-model:value="settings.logging.level" :options="loggingLevels" />
          <div class="hint">{{ t('settings.logging.levelHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.logging.assistantLevel') }}</label>
          <NSelect v-model:value="settings.logging.assistant_level" :options="loggingLevels" />
          <div class="hint">{{ t('settings.logging.assistantLevelHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.logging.backupCount') }}</label>
          <NInput v-model:value.number="settings.logging.backup_count" type="number" placeholder="10" />
          <div class="hint">{{ t('settings.logging.backupCountHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.logging.maxBytes') }}</label>
          <NInput v-model:value.number="settings.logging.max_bytes" type="number" placeholder="5242880" />
          <div class="hint">{{ t('settings.logging.maxBytesHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.logging.redact') }}</label>
          <NSwitch v-model:value="settings.logging.redact_payloads" />
          <div class="hint">{{ t('settings.logging.redactHint') }}</div>
        </div>
        <div class="form-col">
          <label>{{ t('settings.logging.logDir') }}</label>
          <NInput v-model:value="settings.logging.log_dir" :placeholder="loggingStatus.log_dir || t('settings.logging.logDirPlaceholder')" />
          <div class="hint">{{ t('settings.logging.logDirHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col-full logging-actions">
          <NButton @click="openLogDir">{{ t('settings.logging.openDir') }}</NButton>
          <NButton quaternary @click="refreshLoggingStatus">{{ t('settings.logging.refresh') }}</NButton>
          <span class="hint">{{ t('settings.logging.currentDir', { path: loggingStatus.log_dir || '-' }) }}</span>
        </div>
      </div>
      <div v-if="loggingStatus.files && loggingStatus.files.length" class="logging-files">
        <div class="hint" style="margin-bottom:4px">{{ t('settings.logging.filesHeader') }}</div>
        <div v-for="file in loggingStatus.files" :key="file.name" class="logging-file-row">
          <span class="logging-file-name">{{ file.name }}</span>
          <span class="logging-file-size">{{ formatBytes(file.size) }}</span>
        </div>
      </div>
      </div>
    </section>

    <!-- Security -->
    <section v-show="activeSettingsSection === 'security'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.securityTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.securityDomainHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
      <div class="form-row">
        <div class="form-col-full">
          <label>{{ t('settings.oobCallbackBase') }}</label>
          <NInput v-model:value="settings.security.oob_callback_base" placeholder="https://interact.example/session-token" />
          <div class="hint">{{ t('settings.oobCallbackBaseHint') }}</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-col">
          <label>{{ t('settings.maxActiveRequests') }}</label>
          <NInput v-model:value.number="settings.security.max_active_requests" type="number" placeholder="80" />
        </div>
        <div class="form-col">
          <label>{{ t('settings.maxTimeDelaySeconds') }}</label>
          <NInput v-model:value.number="settings.security.max_time_delay_seconds" type="number" placeholder="5" />
        </div>
      </div>
      <div class="form-row">
        <div class="form-col-full">
          <label>{{ t('settings.enableProtocolTests') }}</label>
          <NSwitch v-model:value="settings.security.enable_protocol_tests" />
          <div class="hint">{{ t('settings.enableProtocolTestsHint') }}</div>
        </div>
      </div>
      </div>
    </section>

    <!-- Browser config (managed headless + attach to user's browser) -->
    <section v-show="activeSettingsSection === 'browser'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.browserTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.browserDomainHint') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
      <!-- Current mode banner -->
      <div
        class="browser-mode-banner"
        :class="attachState.attached ? 'mode-attached' : 'mode-headless'"
      >
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <NTag :type="attachState.attached ? 'success' : 'info'" size="small">
            {{ attachState.attached ? t('settings.browserModeAttached') : t('settings.browserModeHeadless') }}
          </NTag>
          <span class="hint" style="margin-left:2px">
            {{ attachState.attached ? t('settings.browserModeAttachedHint') : t('settings.browserModeHeadlessHint') }}
          </span>
        </div>
        <div v-if="attachState.attached" style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <code style="font-size:12px">{{ attachState.endpoint }}</code>
          <span v-if="attachState.browser" class="hint">· {{ attachState.browser }}</span>
          <NButton size="tiny" @click="refreshAttachedTabs">{{ t('settings.attachRefresh') }}</NButton>
          <NButton size="tiny" type="error" :loading="attachState.busy" @click="detachBrowser">{{ t('settings.attachDetach') }}</NButton>
        </div>
        <div v-if="attachState.attached && attachState.tabs && attachState.tabs.length" style="margin-top:8px;max-height:140px;overflow:auto;font-size:12px">
          <div v-for="tab in attachState.tabs" :key="tab.index" class="hint" style="display:flex;gap:6px">
            <span style="opacity:0.5;min-width:20px">#{{ tab.index }}</span>
            <span style="flex:1">{{ tab.title || '(untitled)' }}</span>
            <code style="opacity:0.6">{{ tab.url }}</code>
          </div>
        </div>
      </div>

      <!-- Detected browsers — shared by both modes -->
      <div class="form-row" style="margin-top:14px">
        <div class="form-col-full">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <NText depth="3">{{ t('settings.browserDetected') }}</NText>
            <NButton size="tiny" @click="refreshBrowserInventory" :loading="browserInventory.loading">{{ t('settings.browserRescan') }}</NButton>
          </div>
          <div v-if="browserInventory.system_browsers.length === 0 && !browserInventory.loading" class="hint">{{ t('settings.browserNoneFound') }}</div>
          <div v-else style="display:flex;flex-direction:column;gap:4px">
            <div v-for="b in browserInventory.system_browsers" :key="b.executable_path" class="hint">
              <NTag size="small" :type="b.channel ? 'success' : 'default'">{{ b.label }}</NTag>
              <span style="margin-left:6px;opacity:0.7">{{ b.channel || '(no Playwright channel)' }}</span>
              <span v-if="b.version" style="margin-left:6px;opacity:0.6">· {{ b.version }}</span>
              <code style="margin-left:6px;opacity:0.6;font-size:11px">{{ b.executable_path }}</code>
            </div>
          </div>
        </div>
      </div>

      <section class="settings-group">
        <div class="settings-group-head">
          <div>
            <h3>{{ t('settings.browserSectionHeadless') }}</h3>
            <p>{{ t('settings.browserSectionHeadlessHint') }}</p>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col">
            <label>{{ t('settings.browserChannel') }}</label>
            <NSelect v-model:value="settings.browser.channel" :options="browserChannelOptions" filterable />
            <div class="hint">{{ t('settings.browserChannelHint') }}</div>
          </div>
          <div class="form-col">
            <label>{{ t('settings.browserExecutablePath') }}</label>
            <NInput v-model:value="settings.browser.executable_path" :placeholder="t('settings.browserExecutablePathPlaceholder')" />
            <div class="hint">{{ t('settings.browserExecutablePathHint') }}</div>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col">
            <label>{{ t('settings.browserWindowSize') }}</label>
            <div style="display:flex;align-items:center;gap:8px">
              <NInputNumber v-model:value="settings.browser.window_width" :min="320" :max="7680" :step="10" style="width:120px" />
              <span style="opacity:0.6">×</span>
              <NInputNumber v-model:value="settings.browser.window_height" :min="240" :max="4320" :step="10" style="width:120px" />
              <NButton size="small" tertiary @click="settings.browser.window_width = 1920; settings.browser.window_height = 1080">1920 × 1080</NButton>
            </div>
            <div class="hint">{{ t('settings.browserWindowSizeHint') }}</div>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col-full">
            <label>{{ t('settings.browserBundle') }}</label>
            <div class="hint" style="margin-bottom:6px">
              <span v-if="browserInventory.playwright_bundle.engines?.chromium?.installed">
                {{ t('settings.browserBundleInstalled') }}: <code>{{ browserInventory.playwright_bundle.engines.chromium.latest }}</code>
              </span>
              <span v-else>{{ t('settings.browserBundleMissing') }}</span>
            </div>
            <NButton size="small" @click="installPlaywrightChromium" :loading="installingBrowser" :disabled="installingBrowser">
              {{ t('settings.browserInstallChromium') }}
            </NButton>
            <pre v-if="installLog" style="margin-top:8px;max-height:160px;overflow:auto;background:rgba(0,0,0,0.25);padding:6px;font-size:11px;border-radius:4px">{{ installLog }}</pre>
          </div>
        </div>
      </section>

      <section class="settings-group">
        <div class="settings-group-head">
          <div>
            <h3>{{ t('settings.browserSectionAttach') }}</h3>
            <p>{{ t('settings.attachIntro') }}</p>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col">
            <label>{{ t('settings.attachChannel') }}</label>
            <NSelect v-model:value="attachState.channel" :options="browserChannelOptions" filterable />
          </div>
          <div class="form-col">
            <label>{{ t('settings.attachPort') }}</label>
            <NInput v-model:value.number="attachState.port" type="number" placeholder="9222" />
          </div>
        </div>
        <div class="form-row">
          <div class="form-col-full" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <NButton type="primary" size="small" :loading="attachState.busy" @click="launchAndAttach">
              {{ t('settings.attachLaunchAndAttach') }}
            </NButton>
            <span class="hint">{{ t('settings.attachLaunchHint') }}</span>
          </div>
        </div>
      </section>

      <section class="settings-group">
        <div class="settings-group-head">
          <div>
            <h3>{{ t('settings.attachOrManual') }}</h3>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col">
            <label>{{ t('settings.attachEndpoint') }}</label>
            <NInput v-model:value="attachState.manualEndpoint" placeholder="http://127.0.0.1:9222" />
            <div class="hint">{{ t('settings.attachEndpointHint') }}</div>
          </div>
          <div class="form-col" style="display:flex;align-items:flex-end">
            <NButton size="small" :loading="attachState.busy" @click="attachExistingEndpoint">
              {{ t('settings.attachConnect') }}
            </NButton>
          </div>
        </div>

        <div v-if="attachState.discovered && attachState.discovered.length" class="hint" style="margin-top:6px">
          <span>{{ t('settings.attachDiscovered') }}:</span>
          <span v-for="ep in attachState.discovered" :key="ep.endpoint" style="margin-left:6px">
            <code @click="attachState.manualEndpoint = ep.endpoint" style="cursor:pointer;text-decoration:underline">{{ ep.endpoint }}</code>
            <span v-if="ep.browser" style="opacity:0.6">({{ ep.browser }})</span>
          </span>
        </div>

        <div v-if="attachState.message" class="hint" style="margin-top:8px">{{ attachState.message }}</div>
      </section>
      </div>
    </section>

    <!-- Managed sandbox runtimes -->
    <section v-show="activeSettingsSection === 'runtime'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.runtimeTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.runtimeIntro') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
          <NButton size="tiny" secondary :loading="runtimeState.loading" @click="refreshRuntimeStatus">
            {{ t('settings.runtimeRefresh') }}
          </NButton>
        </div>
      </div>
      <div class="settings-section-content">
        <div v-if="runtimeState.root" class="hint runtime-root">
          {{ t('settings.runtimeSharedRoot') }}: <code>{{ runtimeState.root }}</code>
        </div>
        <section class="settings-group">
        <div class="runtime-grid">
          <div v-for="name in ['python', 'node', 'wabt']" :key="name" class="runtime-card">
            <div class="runtime-card-head">
              <strong>{{ RUNTIME_LABELS[name] }}</strong>
              <NTag size="small" :type="runtimeStatusType(name)">{{ runtimeStatusLabel(name) }}</NTag>
            </div>
            <div class="runtime-meta">
              <span>{{ t('settings.runtimeVersion') }}: {{ runtimeInfo(name).version || runtimeInfo(name).expected_version || '—' }}</span>
              <span>{{ t('settings.runtimeDiskUsage') }}: {{ formatBytes(runtimeInfo(name).size_bytes || 0) }}</span>
              <span>{{ t('settings.runtimeDownloadSize') }}: {{ formatBytes(runtimeInfo(name).download_size_bytes || 0) }}</span>
            </div>
            <div class="hint runtime-path">{{ runtimeInfo(name).root || '—' }}</div>
            <div v-if="runtimeInfo(name).source" class="hint">
              {{ t('settings.runtimeLastSource') }}: {{ runtimeInfo(name).source }}
            </div>
            <div v-if="runtimeInfo(name).npm_source" class="hint">
              {{ t('settings.runtimeLastNpmSource') }}: {{ runtimeInfo(name).npm_source }}
            </div>
            <div v-if="runtimeInfo(name).error" class="runtime-error">{{ runtimeInfo(name).error }}</div>
            <NButton
              size="small"
              :type="runtimeInfo(name).healthy ? 'default' : 'primary'"
              :secondary="runtimeInfo(name).healthy"
              :loading="runtimeState.installing === name"
              :disabled="!!runtimeState.installing"
              @click="installSandboxRuntime(name)"
            >
              {{ t(runtimeInfo(name).installed ? 'settings.runtimeRepair' : 'settings.runtimeInstall') }}
            </NButton>
          </div>
        </div>
        </section>
        <section class="settings-group">
        <div class="settings-group-head">
          <div>
            <h3>{{ t('settings.runtimeMirrorsTitle') }}</h3>
            <p>{{ t('settings.runtimeMirrorsHint') }}</p>
          </div>
        </div>
        <div class="form-row">
          <div class="form-col">
            <label>{{ t('settings.runtimePythonMirrors') }}</label>
            <NInput v-model:value="settings.runtime.mirrors.python" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :placeholder="t('settings.runtimeMirrorPlaceholder')" />
          </div>
          <div class="form-col">
            <label>{{ t('settings.runtimeNodeMirrors') }}</label>
            <NInput v-model:value="settings.runtime.mirrors.node" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :placeholder="t('settings.runtimeMirrorPlaceholder')" />
          </div>
        </div>
        <div class="form-row">
          <div class="form-col-full">
            <label>{{ t('settings.runtimeNpmMirrors') }}</label>
            <NInput v-model:value="settings.runtime.mirrors.npm" :placeholder="t('settings.runtimeNpmMirrorPlaceholder')" />
          </div>
        </div>
        <div v-if="runtimeState.error" class="runtime-error">{{ runtimeState.error }}</div>
        </section>
      </div>
    </section>

    <!-- Reverse toolchain paths -->
    <section v-show="activeSettingsSection === 'toolchain'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.toolchainTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.toolchainIntro', { dir: toolchainInstallDir }) }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
        <div class="toolchain-head">
          <NButton
            class="toolchain-scan-btn"
            size="small"
            secondary
            :loading="toolchainScanState.loading"
            @click.stop="scanToolchainPaths"
          >
            {{ t('settings.toolchainScanPaths') }}
          </NButton>
        </div>

        <div v-if="toolchainScanState.error" class="toolchain-status is-error">
          {{ toolchainScanState.error }}
        </div>

        <div v-if="toolchainScanState.missing.length" class="toolchain-missing">
          <span class="toolchain-missing-label">{{ t('settings.toolchainMissingLabel') }}</span>
          <div class="toolchain-missing-tags">
            <NTag v-for="name in toolchainScanState.missing" :key="name" size="small" :bordered="false" type="warning">
              {{ name }}
            </NTag>
          </div>
          <p class="hint toolchain-missing-hint">{{ t('settings.toolchainMissingHint', { dir: toolchainInstallDir }) }}</p>
        </div>

        <div class="toolchain-fields">
          <div v-for="(row, ri) in toolchainPathRowGroups" :key="'tc-row-' + ri" class="form-row">
            <div v-for="key in row" :key="key" class="form-col toolchain-field-col">
              <label>{{ toolchainPathLabel(key) }}</label>
              <div class="toolchain-field-row">
                <NInput
                  class="toolchain-field-input"
                  v-model:value="settings.toolchain.paths[key]"
                  :placeholder="t('settings.toolchainPathPlaceholder', { cmd: key })"
                />
                <div class="toolchain-field-actions">
                  <NTag v-if="toolchainToolAvailable(key)" size="small" type="success" :bordered="false">
                    {{ t('settings.toolchainStatusOk') }}
                  </NTag>
                  <NButton
                    v-else-if="toolchainCanInstall(key)"
                    size="tiny"
                    secondary
                    :loading="toolchainInstalling(key)"
                    @click.stop="installToolchainTool(key)"
                  >
                    {{ toolchainInstalling(key) ? t('settings.toolchainInstalling') : t('settings.toolchainInstall') }}
                  </NButton>
                  <NButton
                    v-else-if="toolchainDocsUrl(key)"
                    size="tiny"
                    tag="a"
                    :href="toolchainDocsUrl(key)"
                    target="_blank"
                    rel="noopener"
                  >
                    {{ t('settings.toolchainOpenDocs') }}
                  </NButton>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="hint toolchain-foot">{{ t('settings.toolchainPathHint', { dir: toolchainInstallDir }) }}</div>
      </div>
    </section>

    <!-- Storage -->
    <section v-show="activeSettingsSection === 'storage'" class="settings-section-card settings-domain">
      <div class="settings-section-header">
        <div class="settings-head-text">
          <h2 class="settings-section-title">{{ t('settings.storageTitle') }}</h2>
          <p class="settings-head-desc">{{ t('settings.storage.intro') }}</p>
        </div>
        <div class="settings-section-extra">
          <span class="settings-scope">{{ t('settings.aiScopeGlobal') }}</span>
        </div>
      </div>
      <div class="settings-section-content">
      <StorageDirField
        v-model:value="settings.data_root"
        :label="t('settings.storage.dataRoot')"
        :hint="t('settings.storage.dataRootHint')"
        :default-path="defaultDirs.data_root"
        field-key="data_root"
        pickable
        @directory-picked="openDataRootMigrateModal"
      />

      <div v-if="dataRootPending" class="storage-migrate-banner">
        <div class="storage-migrate-banner-text">
          <div class="storage-migrate-banner-title">{{ t('settings.storage.migratePendingTitle') }}</div>
          <div class="hint">{{ t('settings.storage.migratePendingHint') }}</div>
        </div>
        <div class="storage-migrate-banner-actions">
          <NButton size="small" quaternary :disabled="migrateBusy" @click="revertDataRootDraft">
            {{ t('settings.storage.migrateRevert') }}
          </NButton>
          <NButton size="small" type="primary" :loading="migrateBusy" @click="openDataRootMigrateModal">
            {{ t('settings.storage.migrateOpen') }}
          </NButton>
        </div>
      </div>

      <WorkbenchModal
        :open="migrateModalOpen"
        size="lg"
        :title="t('settings.storage.migrateTitle')"
        :busy="migrateBusy"
        :close-label="t('common.cancel')"
        @close="closeMigrateModal"
      >
        <div>
          <p class="hint">{{ t('settings.storage.migrateIntro') }}</p>
          <div class="storage-migrate-paths">
            <div><span class="muted">{{ t('settings.storage.migrateFrom') }}</span> <code>{{ migratePreview?.source || resolvedDataRoot(committedDataRoot) }}</code></div>
            <div><span class="muted">{{ t('settings.storage.migrateTo') }}</span> <code>{{ resolvedDataRoot(settings.data_root) }}</code></div>
            <div v-if="migratePreview">
              <span class="muted">{{ t('settings.storage.migrateSize') }}</span>
              {{ formatBytes(migratePreview.bytes_to_transfer || 0) }}
              <span v-if="migratePreview.free_bytes != null" class="muted">
                · {{ t('settings.storage.migrateFree', { size: formatBytes(migratePreview.free_bytes) }) }}
              </span>
            </div>
          </div>

          <NRadioGroup v-model:value="migrateMode" class="storage-migrate-modes" :disabled="migrateBusy">
            <div v-for="opt in migrateModeOptions" :key="opt.value" class="storage-migrate-mode">
              <NRadio :value="opt.value">{{ opt.label }}</NRadio>
              <div class="hint">{{ opt.hint }}</div>
            </div>
          </NRadioGroup>

          <div v-if="migrateBusy && migrateProgress" class="storage-migrate-progress">
            <div class="storage-migrate-progress-head">
              <span>{{ migrateProgressLabel }}</span>
              <strong>{{ Math.round(Number(migrateProgress.percent || 0)) }}%</strong>
            </div>
            <NProgress
              type="line"
              :percentage="Math.round(Number(migrateProgress.percent || 0))"
              :show-indicator="false"
              :processing="migrateProgress.status === 'running'"
              :status="migrateProgress.status === 'failed' ? 'error' : 'default'"
            />
            <div
              v-if="Number(migrateProgress.total_work_bytes || 0) > 0"
              class="hint storage-migrate-progress-bytes"
            >
              {{ formatBytes(migrateProgress.completed_bytes || 0) }}
              /
              {{ formatBytes(migrateProgress.total_work_bytes || 0) }}
              {{ t('settings.storage.migrateProgressWorkload') }}
            </div>
          </div>

          <NAlert
            v-if="(migratePreview?.blockers || []).length && migrateMode !== 'switch'"
            type="error"
            :bordered="false"
            style="margin-top: 10px"
          >
            {{ migrateIssueText(migratePreview.blockers[0]) }}
          </NAlert>
          <NAlert
            v-else-if="(migratePreview?.warnings || []).length"
            type="warning"
            :bordered="false"
            style="margin-top: 10px"
          >
            <div v-for="(w, idx) in migratePreview.warnings.slice(0, 3)" :key="idx">{{ migrateIssueText(w) }}</div>
          </NAlert>
          <NAlert v-if="migrateError" type="error" :bordered="false" style="margin-top: 10px">
            {{ migrateError }}
          </NAlert>
          <p class="hint" style="margin-top: 10px">{{ t('settings.storage.migrateRestart') }}</p>
        </div>
        <template #footer>
          <div class="modal-actions">
          <NButton :disabled="migrateBusy" @click="closeMigrateModal">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="migrateBusy" @click="confirmDataRootMigrate">
            {{ t('settings.storage.migrateApply') }}
          </NButton>
          </div>
        </template>
      </WorkbenchModal>

      <div class="storage-subdirs">
        <div class="storage-subdirs-head">{{ t('settings.storage.subdirsTitle') }}</div>
        <ul class="storage-subdirs-list">
          <li class="storage-subdir-row">
            <span class="storage-subdir-name">{{ t('settings.storage.subTasks') }}</span>
            <code class="storage-subdir-path" :title="derivedDirs.tasks_dir">{{ derivedDirs.tasks_dir || '—' }}</code>
            <NButton size="tiny" quaternary :disabled="!derivedDirs.tasks_dir" @click="openSubdir(derivedDirs.tasks_dir)">{{ t('settings.storage.openButton') }}</NButton>
          </li>
          <li class="storage-subdir-row">
            <span class="storage-subdir-name">{{ t('settings.storage.subKnowledge') }}</span>
            <code class="storage-subdir-path" :title="derivedDirs.knowledge_base_dir">{{ derivedDirs.knowledge_base_dir || '—' }}</code>
            <NButton size="tiny" quaternary :disabled="!derivedDirs.knowledge_base_dir" @click="openSubdir(derivedDirs.knowledge_base_dir)">{{ t('settings.storage.openButton') }}</NButton>
          </li>
          <li class="storage-subdir-row">
            <span class="storage-subdir-name">{{ t('settings.storage.subSessions') }}</span>
            <code class="storage-subdir-path" :title="derivedDirs.sessions_dir">{{ derivedDirs.sessions_dir || '—' }}</code>
            <NButton size="tiny" quaternary :disabled="!derivedDirs.sessions_dir" @click="openSubdir(derivedDirs.sessions_dir)">{{ t('settings.storage.openButton') }}</NButton>
          </li>
          <li class="storage-subdir-row">
            <span class="storage-subdir-name">{{ t('settings.storage.subPocPages') }}</span>
            <code class="storage-subdir-path" :title="derivedDirs.poc_pages_dir">{{ derivedDirs.poc_pages_dir || '—' }}</code>
            <NButton size="tiny" quaternary :disabled="!derivedDirs.poc_pages_dir" @click="openSubdir(derivedDirs.poc_pages_dir)">{{ t('settings.storage.openButton') }}</NButton>
          </li>
          <li class="storage-subdir-row">
            <span class="storage-subdir-name">{{ t('settings.storage.subReverseJobs') }}</span>
            <code class="storage-subdir-path" :title="derivedDirs.reverse_jobs_dir">{{ derivedDirs.reverse_jobs_dir || '—' }}</code>
            <NButton size="tiny" quaternary :disabled="!derivedDirs.reverse_jobs_dir" @click="openSubdir(derivedDirs.reverse_jobs_dir)">{{ t('settings.storage.openButton') }}</NButton>
          </li>
        </ul>
        <div class="hint">{{ t('settings.storage.subdirsHint') }}</div>
      </div>

      <div class="storage-divider"></div>

      <div class="storage-numeric">
        <div class="storage-numeric-head">
          <label class="storage-field-label">{{ t('settings.kbTrashRetentionDays') }}</label>
          <NInputNumber
            v-model:value="settings.kb_trash_retention_days"
            :min="0"
            :max="3650"
            :step="1"
            :placeholder="t('settings.kbTrashRetentionDaysPlaceholder')"
            size="small"
            style="width:160px"
          />
        </div>
        <div class="hint storage-numeric-hint">{{ t('settings.kbTrashRetentionDaysHint') }}</div>
      </div>
      </div>
    </section>

      </main>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  width: 100%;
  max-width: none;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  overscroll-behavior: contain;
  padding-right: 0;
}
.sr-only {
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
.settings-control-head {
  flex: 0 0 auto;
  display: grid;
  grid-template-columns: minmax(190px, 1fr) minmax(240px, 420px) auto;
  align-items: center;
  gap: 16px;
  padding: 0 2px 14px;
}
.settings-control-title {
  min-width: 0;
}
.settings-control-title h1 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.015em;
}
.settings-control-title p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.settings-search-wrap {
  min-width: 0;
}
.settings-search {
  width: 100%;
  height: 34px;
  padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: 9px;
  outline: none;
  background: var(--panel);
  color: var(--fg);
  font: inherit;
  font-size: 12px;
  transition: border-color 150ms ease, box-shadow 150ms ease, background 150ms ease;
}
.settings-search::placeholder {
  color: var(--muted-2);
}
.settings-search:hover {
  border-color: var(--border-strong);
}
.settings-search:focus-visible {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.settings-control-body {
  min-height: 0;
  flex: 1;
  display: grid;
  grid-template-columns: 236px minmax(0, 1fr);
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--card-bg);
}
.settings-rail {
  min-width: 0;
  overflow: auto;
  padding: 12px 9px;
  border-right: 1px solid var(--border);
  background: var(--panel-2);
  scrollbar-width: thin;
}
.settings-rail-group + .settings-rail-group {
  margin-top: 14px;
}
.settings-rail-label {
  padding: 0 9px 5px;
  color: var(--muted-2);
  font-family: var(--mono-font, ui-monospace, monospace);
  font-size: 10px;
  line-height: 1.4;
  letter-spacing: 0.06em;
}
.settings-nav {
  width: 100%;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 9px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 150ms ease, color 150ms ease;
}
.settings-nav + .settings-nav {
  margin-top: var(--menu-item-gap, 4px);
}
.settings-nav:hover {
  background: var(--panel-hover);
  color: var(--fg);
}
.settings-nav:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.settings-nav.active {
  background: var(--accent-soft);
  color: var(--fg-strong);
}
.settings-nav-copy {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.settings-nav-copy strong {
  overflow: hidden;
  color: inherit;
  font-size: 12px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-nav-copy small {
  overflow: hidden;
  color: var(--muted-2);
  font-size: 10px;
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-health-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--muted-2);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--muted-2) 14%, transparent);
}
.settings-health-dot.status-success { background: var(--ok); }
.settings-health-dot.status-warning { background: var(--warn); }
.settings-health-dot.status-error { background: var(--err); }
.settings-rail-empty {
  padding: 18px 10px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.settings-content {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--card-bg);
}
.settings-section-card {
  min-height: 100%;
  overflow: visible;
}
.settings-section-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px 13px;
  border-bottom: 1px solid var(--border);
  background: transparent;
}
.settings-head-text {
  min-width: 0;
}
.settings-section-title {
  display: flex;
  align-items: center;
  min-width: 0;
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--fg-strong);
}
.settings-head-desc {
  max-width: 70ch;
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.settings-section-extra {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  padding-top: 2px;
}
.settings-scope {
  flex: 0 0 auto;
  display: inline-flex;
  min-height: 24px;
  align-items: center;
  padding: 4px 8px;
  border-radius: 7px;
  background: var(--panel-2);
  color: var(--muted);
  font: 10px/1.5 var(--mono-font, ui-monospace, monospace);
  white-space: nowrap;
}
.settings-section-content {
  padding: 18px 20px 24px;
}
.settings-autosave {
  justify-self: end;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: 280px;
  min-height: 30px;
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  color: var(--muted);
  font-size: 11px;
}
.settings-autosave > span:nth-child(2) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-autosave-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted-2);
}
.settings-autosave.state-dirty .settings-autosave-dot,
.settings-autosave.state-saving .settings-autosave-dot {
  background: var(--warn);
}
.settings-autosave.state-saving .settings-autosave-dot {
  animation: pulse 1.2s ease-in-out infinite;
}
.settings-autosave.state-saved .settings-autosave-dot {
  background: var(--ok);
}
.settings-autosave.state-error {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err) 42%, var(--border));
}
.settings-autosave.state-error .settings-autosave-dot {
  background: var(--err);
}
.ai-control-plane {
  min-width: 0;
}
.ai-route-health {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0 0 20px;
  border-block: 1px solid var(--border);
}
.ai-route-health > div {
  min-width: 0;
  padding: 10px 12px;
  border-left: 1px solid var(--border);
}
.ai-route-health > div:first-child {
  padding-left: 0;
  border-left: 0;
}
.ai-route-health dt,
.ai-route-health dd {
  overflow: hidden;
  margin: 0;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ai-route-health dt {
  color: var(--muted-2);
  font-size: 10px;
}
.ai-route-health dd {
  margin-top: 3px;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 600;
}
.settings-group {
  padding-bottom: 22px;
}
.settings-group + .settings-group {
  padding-top: 20px;
  border-top: 1px solid var(--border);
}
.settings-group-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 11px;
}
.settings-group-head > div:first-child {
  min-width: 0;
}
.settings-group-head h3,
.settings-group-head h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 600;
}
.settings-group-head p {
  max-width: 68ch;
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}
.settings-group-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 7px;
}
.settings-group-kicker {
  display: block;
  margin-bottom: 3px;
  color: var(--accent);
  font: 10px/1.4 var(--mono-font, ui-monospace, monospace);
  letter-spacing: 0.04em;
}
.ai-route-group {
  container-type: inline-size;
}
.endpoint-route-list {
  position: relative;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.endpoint-route-row {
  position: relative;
  display: grid;
  grid-template-columns: 54px minmax(150px, 1.25fr) minmax(110px, .8fr) minmax(82px, .55fr) auto auto;
  align-items: center;
  min-width: 0;
  min-height: 62px;
  gap: 10px;
  padding: 9px 10px;
  border-top: 1px solid var(--border);
  transition: background 140ms ease;
}
.endpoint-route-row:first-child {
  border-top: 0;
  border-radius: 9px 9px 0 0;
}
.endpoint-route-row:last-child {
  border-radius: 0 0 9px 9px;
}
.endpoint-route-row:only-child {
  border-radius: 9px;
}
.endpoint-route-row:hover {
  background: color-mix(in srgb, var(--fg) 3.5%, transparent);
}
.endpoint-route-row.selected {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.endpoint-route-row.disabled .endpoint-route-identity,
.endpoint-route-row.disabled .endpoint-route-model,
.endpoint-route-row.disabled .endpoint-route-probe {
  opacity: .55;
}
.endpoint-route-row.dragging {
  opacity: .45;
  user-select: none;
}
.endpoint-route-row.drop-target {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  box-shadow: inset 0 2px 0 0 var(--accent);
}
.endpoint-route-lead {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
}
.endpoint-drag-handle {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 6px;
  appearance: none;
  background: transparent;
  color: var(--muted-2);
  fill: currentColor;
  line-height: 0;
  cursor: grab;
  touch-action: none;
  transition: background 140ms ease, color 140ms ease;
}
.endpoint-drag-handle:hover:not(:disabled) {
  background: var(--panel-hover);
  color: var(--fg);
}
.endpoint-drag-handle:active:not(:disabled) {
  cursor: grabbing;
}
.endpoint-drag-handle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.endpoint-drag-handle:disabled {
  opacity: .3;
  cursor: default;
}
.endpoint-drag-handle svg {
  display: block;
  flex: 0 0 auto;
}
.endpoint-route-order {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 7px;
  background: var(--panel-2);
  color: var(--muted);
  font: 10px/1 var(--mono-font, ui-monospace, monospace);
}
.endpoint-route-identity {
  display: block;
  min-width: 0;
  padding: 2px 0;
  border: 0;
  outline: none;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.endpoint-route-identity:focus-visible {
  border-radius: 5px;
  box-shadow: 0 0 0 2px var(--accent);
}
.endpoint-route-identity > span {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
}
.endpoint-inline-state {
  display: none;
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 9.5px;
  font-style: normal;
  font-weight: 500;
}
.endpoint-route-identity strong,
.endpoint-route-model strong,
.endpoint-route-probe strong {
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 11.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.endpoint-route-identity small,
.endpoint-route-model small,
.endpoint-route-probe small {
  display: block;
  overflow: hidden;
  margin-top: 3px;
  color: var(--muted-2);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.endpoint-route-model,
.endpoint-route-probe {
  min-width: 0;
}
.endpoint-route-enable {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  padding-right: 2px;
  cursor: pointer;
}
.endpoint-state-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--muted-2);
}
.endpoint-state-dot.state-testing {
  background: var(--warn);
  animation: pulse 1.2s ease-in-out infinite;
}
.endpoint-state-dot.state-success {
  background: var(--ok);
}
.endpoint-state-dot.state-error {
  background: var(--err);
}
.endpoint-state-dot.state-disabled {
  background: var(--muted-2);
  opacity: .55;
}
.endpoint-route-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  white-space: nowrap;
}
.endpoint-delete-btn {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 7px;
  appearance: none;
  background: transparent;
  color: var(--muted);
  line-height: 0;
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease;
}
.endpoint-delete-btn:hover {
  background: color-mix(in srgb, var(--err) 12%, transparent);
  color: var(--err);
}
.endpoint-delete-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.endpoint-delete-btn svg {
  display: block;
  flex: 0 0 auto;
}
.endpoint-route-empty {
  padding: 26px 0;
  border: 1px dashed var(--border);
  border-radius: 10px;
}
.endpoint-editor-modal {
  width: min(720px, 94vw);
  max-height: 90vh;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.endpoint-editor-modal .panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex: 0 0 auto;
  margin: 0;
  padding: 16px 20px 14px;
  border-bottom: 1px solid var(--border);
}
.endpoint-editor-modal .panel-head h3 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 15px;
  font-weight: 600;
}
.endpoint-editor-modal .panel-head p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11px;
}
.endpoint-editor-body {
  flex: 1 1 auto;
  min-height: 0;
  padding: 0;
}
.endpoint-editor-head {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.endpoint-editor-head h2 {
  margin: 2px 0 0;
  color: var(--fg-strong);
  font-size: 15px;
}
.endpoint-editor-head p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11px;
}
.endpoint-editor-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--control-cluster-gap, 8px);
}
.endpoint-editor-body > .endpoint-editor-section:first-child {
  border-top: 0;
}
.endpoint-editor-modal .modal-actions {
  display: flex;
  flex: 0 0 auto;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px 14px;
  border-top: 1px solid var(--border);
}
.endpoint-enabled-control {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 11px;
}
.endpoint-editor-section {
  padding: 16px 0;
  border-top: 1px solid var(--border);
}
.endpoint-editor-section > h4 {
  margin: 0 0 12px;
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 600;
}
.endpoint-model-field {
  min-width: 0;
}
.endpoint-model-control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.endpoint-field-error {
  display: block;
  margin-top: 2px;
  font-size: 11px;
}
.endpoint-field-hint {
  display: block;
  margin-top: 2px;
  font-size: 11px;
}
.endpoint-catalog-label {
  display: block;
  margin-top: 10px;
  margin-bottom: 4px;
}
.endpoint-provider-section h4 {
  display: flex;
  align-items: center;
  gap: 8px;
}
.endpoint-provider-badge {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--accent-soft, rgba(0, 122, 255, 0.12));
  color: var(--accent, #2369d1);
  font-size: 10px;
  font-weight: 600;
}
.endpoint-provider-current {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.endpoint-provider-current-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.endpoint-provider-current-copy strong {
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.endpoint-provider-current-copy > span {
  color: var(--muted);
  font-size: 10px;
}
.endpoint-provider-current-copy small {
  overflow: hidden;
  color: var(--muted-2);
  font-size: 9.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.endpoint-provider-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--muted);
}
.endpoint-provider-console a {
  color: var(--accent);
  text-decoration: none;
}
.endpoint-provider-console a:hover {
  text-decoration: underline;
}
.endpoint-provider-warn {
  color: var(--warn, #b26a00);
}
.endpoint-provider-note {
  color: var(--muted);
}
.endpoint-provider-preview {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn, #b26a00);
  border-radius: 8px;
  background: var(--bg-soft, rgba(128, 128, 128, 0.06));
}
.endpoint-provider-preview-title {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-strong);
}
.endpoint-provider-preview-rows {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.endpoint-provider-preview-rows li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 12px;
}
.endpoint-provider-preview-rows em {
  font-style: normal;
  color: var(--fg-strong);
  min-width: 84px;
}
.endpoint-provider-preview-rows i {
  color: var(--muted);
  font-style: normal;
}
.endpoint-provider-preview-after {
  color: var(--accent);
}
.endpoint-provider-preview-rows-none {
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 12px;
}
.endpoint-provider-preview-key {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--muted);
}
.endpoint-provider-preview-note {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--warn, #b26a00);
}
.endpoint-provider-preview-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.endpoint-provider-tag {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 180px;
  padding: 0 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--accent-soft, rgba(0, 122, 255, 0.12));
  color: var(--accent, #2369d1);
  font-size: 10px;
  font-weight: 600;
  white-space: nowrap;
  min-width: 0;
}
.endpoint-provider-tag em {
  font-style: normal;
  color: var(--muted);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (max-width: 640px) {
  .endpoint-provider-current {
    grid-template-columns: 1fr;
  }
  .endpoint-provider-current :deep(.n-button) {
    width: 100%;
  }
}
.endpoint-capability-status {
  margin-top: 10px;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
}
.endpoint-capability-status strong,
.endpoint-capability-status span {
  display: block;
}
.endpoint-capability-status strong {
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 600;
}
.endpoint-capability-status span {
  margin-top: 3px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.45;
}
.capability-override-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 18px;
  margin-top: 8px;
}
.endpoint-advanced {
  border-top: 1px solid var(--border);
}
.endpoint-advanced > summary {
  padding: 13px 0;
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}
.endpoint-advanced > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.endpoint-advanced-body {
  padding: 2px 0 4px;
}
.auxiliary-group {
  padding-bottom: 2px;
}
.auxiliary-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.auxiliary-endpoint {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.auxiliary-endpoint > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
}
.auxiliary-endpoint h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 11.5px;
  font-weight: 600;
}
.auxiliary-endpoint header small {
  display: block;
  overflow: hidden;
  max-width: 220px;
  margin-top: 2px;
  color: var(--muted-2);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.auxiliary-fields {
  padding: 12px;
}
.auxiliary-fields .form-row {
  grid-template-columns: 1fr;
  gap: 10px;
  margin-bottom: 10px;
}
.auxiliary-fields .form-col-full {
  grid-column: auto;
}
.auxiliary-key-field {
  display: flex;
  align-items: center;
  gap: 8px;
}
.auxiliary-key-field .n-input {
  min-width: 0;
}
.auxiliary-warning {
  margin: 10px 12px 0;
  padding: 7px 8px;
  border-radius: 7px;
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  color: var(--warn);
  font-size: 10px;
  line-height: 1.4;
}
@container (max-width: 700px) {
  .endpoint-route-row {
    grid-template-columns: 54px minmax(150px, 1fr) minmax(105px, .7fr) auto auto;
  }
  .endpoint-route-probe {
    display: none;
  }
  .endpoint-inline-state {
    display: inline;
  }
}
@container (max-width: 520px) {
  .endpoint-route-row {
    grid-template-columns: 24px minmax(0, 1fr) auto auto;
  }
  /* The handle survives; the position badge is the expendable half. */
  .endpoint-route-order {
    display: none;
  }
  .endpoint-route-model {
    display: none;
  }
  .endpoint-route-actions :deep(.n-button:first-child) {
    display: none;
  }
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 14px;
}
.form-row:last-child { margin-bottom: 0; }
.form-col { display: flex; flex-direction: column; gap: 6px; }
.form-col-full { display: flex; flex-direction: column; gap: 6px; grid-column: 1 / -1; }
.form-col label, .form-col-full label {
  font-size: 12px; font-weight: 500; color: var(--muted);
}
.hint {
  font-size: 11px; color: var(--muted); opacity: 0.78;
  margin-top: 2px; line-height: 1.4;
}
.logging-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.logging-files { margin-top: 10px; }
.logging-file-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 11px; color: var(--muted);
  padding: 2px 8px; border-radius: 4px;
}
.logging-file-row:nth-child(odd) { background: rgba(255, 255, 255, 0.03); }
.logging-file-name { font-family: var(--mono-font, monospace); }
.logging-file-size { white-space: nowrap; }
.assistant-tool-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.assistant-tool-panel {
  max-height: 260px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
  padding: 6px;
}
.integration-summary {
  border-top: 1px solid var(--border);
}
.host-service-block {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 0;
  border-bottom: 1px solid var(--border);
}
.host-service-top {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 16px 18px;
}
.host-service-enable {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  white-space: nowrap;
}
.host-service-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px 20px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--panel) 55%, transparent);
}
.host-service-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 200px;
  max-width: 280px;
  flex: 1 1 200px;
}
.host-service-field > span,
.host-service-field--switch > span {
  color: var(--muted-2);
  font-size: 10px;
  line-height: 1.3;
}
.host-service-field--switch {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  min-width: 220px;
  max-width: 320px;
  padding-bottom: 2px;
}
.host-service-alert {
  margin: 0;
  color: var(--warning, #e6a23c);
  font-size: 11px;
  line-height: 1.45;
}
.host-service-notes {
  margin: 0;
  padding: 0 0 0 16px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}
.host-service-notes li + li {
  margin-top: 4px;
}
.integration-row {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(230px, auto) auto;
  align-items: center;
  gap: 18px;
  padding: 18px 0;
  border-bottom: 1px solid var(--border);
}
.integration-row.document-parse-block {
  grid-template-columns: minmax(220px, 1.4fr) auto;
  align-items: start;
}
.document-parse-fields {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 4px;
}
.integration-copy {
  min-width: 0;
}
.integration-copy h3 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 600;
}
.integration-copy p {
  max-width: 62ch;
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}
.integration-facts {
  display: flex;
  align-items: stretch;
}
.integration-facts > div {
  min-width: 72px;
  padding: 0 14px;
  border-left: 1px solid var(--border);
}
.integration-facts span,
.integration-facts strong {
  display: block;
}
.integration-facts span {
  color: var(--muted-2);
  font-size: 10px;
}
.integration-facts strong {
  margin-top: 2px;
  color: var(--fg-strong);
  font-family: var(--mono-font, ui-monospace, monospace);
  font-size: 14px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.integration-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}
.assistant-tool-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s;
}
.assistant-tool-row:hover {
  background: var(--panel-2);
}
.assistant-tool-row.selected {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.assistant-tool-row input {
  margin-top: 2px;
  flex: 0 0 auto;
}
.assistant-tool-main {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.assistant-tool-name {
  font-size: 12px;
  color: var(--fg);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.assistant-tool-desc {
  font-size: 11px;
  line-height: 1.35;
  color: var(--muted);
  opacity: 0.82;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.assistant-tool-empty {
  padding: 18px;
  text-align: center;
  color: var(--muted);
  font-size: 12px;
}
.storage-migrate-banner {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 0 14px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: 12px;
  background: color-mix(in srgb, var(--accent) 8%, var(--panel-2));
}
.storage-migrate-banner-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 2px;
}
.storage-migrate-banner-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  flex: 0 0 auto;
}
.modal-overlay {
  position: fixed;
  top: var(--titlebar-h, 0);
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}
.storage-migrate-modal {
  width: min(560px, 94vw);
  max-height: 90vh;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.storage-migrate-modal .panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex: 0 0 auto;
  margin: 0;
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--border);
}
.storage-migrate-modal .panel-head h2 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 16px;
  font-weight: 600;
  text-transform: none;
  letter-spacing: normal;
}
.storage-migrate-modal .modal-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px 20px;
}
.storage-migrate-modal .modal-actions {
  display: flex;
  flex: 0 0 auto;
  justify-content: flex-end;
  gap: 8px;
  padding: 14px 20px 16px;
  border-top: 1px solid var(--border);
}
.modal-close-btn {
  padding: 4px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 16px;
  cursor: pointer;
}
.modal-close-btn:hover {
  background: var(--panel-hover);
  color: var(--fg);
}
.modal-close-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.storage-migrate-modal .storage-migrate-paths {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0 14px;
  font-size: 12px;
}
.storage-migrate-modal code {
  font-size: 11px;
  word-break: break-all;
}
.storage-migrate-modes {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}
.storage-migrate-mode {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
}
.storage-migrate-progress {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent) 7%, var(--panel));
}
.storage-migrate-progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 12px;
}
.storage-migrate-progress-head strong {
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.storage-migrate-progress-bytes {
  margin-top: 6px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.storage-migrate-modal .muted {
  color: var(--muted);
  margin-right: 6px;
}
.storage-subdirs {
  margin-top: 16px;
  padding: 12px 14px;
  background: color-mix(in srgb, var(--fg, #fff) 4%, transparent);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.storage-subdirs-head {
  font-size: 11.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--muted);
  margin-bottom: 8px;
}
.storage-subdirs-list {
  list-style: none;
  margin: 0 0 6px 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.storage-subdir-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.storage-subdir-name {
  flex: 0 0 110px;
  color: var(--fg);
  font-weight: 500;
}
.storage-subdir-path {
  flex: 1 1 auto;
  min-width: 0;
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 11px;
  color: var(--muted);
  background: transparent;
  padding: 2px 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.storage-divider {
  height: 1px;
  background: var(--border);
  margin: 14px 0;
  opacity: 0.6;
}
.storage-numeric {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.storage-numeric-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.storage-numeric-head .storage-field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg);
}
.storage-numeric-hint {
  margin-top: 2px;
}
.browser-mode-banner {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 70%, transparent);
}
.browser-mode-banner.mode-attached {
  background: rgba(64, 158, 96, 0.10);
  border-color: rgba(64, 158, 96, 0.45);
}
.browser-mode-banner.mode-headless {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
}
.settings-page :deep(.n-divider__title) {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.02em;
}
.inline-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--fg);
  cursor: pointer;
}
.toolchain-head {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 14px;
  margin-bottom: 14px;
}
.toolchain-scan-btn {
  flex-shrink: 0;
}
.toolchain-status {
  margin: -4px 0 12px;
  padding: 8px 12px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.45;
}
.toolchain-status.is-error {
  color: var(--error-color);
  background: color-mix(in srgb, var(--error-color) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--error-color) 35%, transparent);
}
.toolchain-missing {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 14px;
}
.toolchain-missing-label {
  font-size: 11px;
  color: var(--muted);
  flex-shrink: 0;
}
.toolchain-missing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.toolchain-missing-hint {
  margin: 0;
  width: 100%;
}
.toolchain-fields {
  margin-top: 2px;
}
.toolchain-field-col {
  min-width: 0;
}
.toolchain-field-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.toolchain-field-input {
  flex: 1;
  min-width: 0;
}
.toolchain-field-actions {
  flex-shrink: 0;
  display: flex;
  gap: 6px;
  align-items: center;
}
.toolchain-foot {
  margin-top: 10px;
}
.runtime-root {
  margin: 8px 0 12px;
}
.runtime-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.runtime-card {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel-2) 75%, transparent);
}
.runtime-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.runtime-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--muted);
}
.runtime-path {
  overflow-wrap: anywhere;
  margin-bottom: 8px;
}
.runtime-error {
  margin: 8px 0;
  font-size: 12px;
  color: var(--error-color);
  overflow-wrap: anywhere;
}
@media (max-width: 1060px) {
  .settings-control-head {
    grid-template-columns: minmax(170px, 1fr) minmax(220px, 340px) auto;
    gap: 10px;
  }
  .host-service-top {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .host-service-enable {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
  .integration-row {
    grid-template-columns: minmax(180px, 1fr) auto;
  }
  .integration-facts {
    grid-column: 1 / -1;
    grid-row: 2;
  }
}
@media (max-width: 900px) {
  .settings-control-body {
    grid-template-columns: 1fr;
    grid-template-rows: auto minmax(0, 1fr);
  }
  .settings-rail {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 8px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
    scrollbar-width: thin;
  }
  .settings-rail-group {
    display: flex;
    flex: 0 0 auto;
    gap: 6px;
  }
  .settings-rail-group + .settings-rail-group {
    margin-top: 0;
  }
  .settings-rail-label {
    display: none;
  }
  .settings-nav {
    width: 180px;
    flex: 0 0 180px;
    margin-top: 0;
    padding: 7px 9px;
    background: color-mix(in srgb, var(--panel) 72%, transparent);
  }
  .settings-rail-empty {
    width: 100%;
    padding: 8px 10px;
    text-align: left;
  }
}
@media (max-width: 800px) {
  .settings-control-head {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .settings-search-wrap {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .settings-autosave {
    grid-column: 2;
    grid-row: 1;
  }
  .runtime-grid {
    grid-template-columns: 1fr;
  }
  .ai-route-health {
    grid-template-columns: 1fr 1fr;
  }
  .ai-route-health > div:nth-child(3) {
    padding-left: 0;
    border-top: 1px solid var(--border);
    border-left: 0;
  }
  .ai-route-health > div:nth-child(4) {
    border-top: 1px solid var(--border);
  }
  .auxiliary-grid {
    grid-template-columns: 1fr;
  }
  .host-service-top {
    grid-template-columns: 1fr;
  }
  .host-service-enable {
    grid-column: auto;
    justify-content: flex-start;
  }
  .integration-row {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .integration-facts,
  .integration-actions {
    grid-column: auto;
    grid-row: auto;
  }
  .integration-actions {
    justify-content: flex-start;
  }
}
@media (max-width: 620px) {
  .settings-control-title p {
    display: none;
  }
  .settings-control-head {
    padding-bottom: 10px;
  }
  .settings-control-body {
    border-radius: 11px;
  }
  .settings-section-header {
    align-items: flex-start;
    padding: 14px 14px 11px;
  }
  .settings-section-content {
    padding: 14px 14px 20px;
  }
  .form-row {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .form-col-full {
    grid-column: auto;
  }
  .settings-group-head {
    flex-direction: column;
  }
  .modal-overlay {
    padding: 8px;
  }
  .storage-migrate-modal,
  .endpoint-editor-modal {
    width: 100%;
    max-height: calc(100dvh - var(--titlebar-h, 0px) - 16px);
  }
  .settings-group-actions {
    width: 100%;
    flex-wrap: wrap;
  }
  .endpoint-model-control {
    grid-template-columns: 1fr;
  }
  .toolchain-head,
  .storage-migrate-banner {
    flex-direction: column;
  }
  .toolchain-scan-btn {
    align-self: flex-start;
  }
  .toolchain-field-row {
    align-items: stretch;
    flex-direction: column;
  }
  .toolchain-field-actions {
    justify-content: flex-start;
  }
  .storage-subdir-row {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .storage-subdir-name {
    flex-basis: 100%;
  }
  .storage-subdir-path {
    flex-basis: 0;
  }
  .storage-numeric-head {
    align-items: flex-start;
    flex-direction: column;
  }
  .integration-facts {
    width: 100%;
    overflow-x: auto;
  }
  .integration-facts > div:first-child {
    border-left: 0;
    padding-left: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .settings-search,
  .settings-nav,
  .endpoint-route-row {
    transition: none;
  }
  .settings-autosave.state-saving .settings-autosave-dot,
  .endpoint-state-dot.state-testing {
    animation: none;
  }
}
</style>
