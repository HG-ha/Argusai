<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  NButton,
  NEmpty,
  NInput,
  NPopconfirm,
  NSelect,
  NTag,
  NSwitch,
  useMessage,
} from 'naive-ui'
import { api } from '../api.js'
import { route, navigate } from '../router.js'
import { t } from '../i18n.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import { showToast } from '../state.js'
import { emptyCreateDraft, emptyCustomHook } from './reverse/reverseUtils.js'
import ReverseRuntimeTab from './reverse/ReverseRuntimeTab.vue'
import ReverseArtifactsTab from './reverse/ReverseArtifactsTab.vue'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListSelect from './list/ListSelect.vue'
import ListToolbar from './list/ListToolbar.vue'
import PageEmpty from './list/PageEmpty.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'

const confirmDialog = useConfirm()
const msg = useMessage()

const jobs = ref([])
const loading = ref(false)
const root = ref('')
const error = ref('')

const queryText = ref('')
const statusFilter = ref('')
const includeTrashed = ref(false)

const selectedId = ref('')
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const activeTab = ref('overview')
const staticTab = ref('summary')
const dynamicTab = ref('runtime')
const evidenceTab = ref('findings')

const showCreateDialog = ref(false)
const createDraft = ref(emptyCreateDraft())
const creating = ref(false)

// Hook tab state
const recipes = ref([])
const selectedRecipeId = ref('')
const applyingRecipe = ref(false)
const draining = ref(false)
const reattaching = ref(false)
const liveTailing = ref(false)
const liveTailTimer = ref(null)
const expandedHookId = ref('')
const hookHits = ref({})
const hookHitsLoading = ref({})

const showCustomHookDialog = ref(false)
const customHookDraft = ref(emptyCustomHook())
const customHookSaving = ref(false)

const events = ref([])
const eventsLoading = ref(false)

const targetTypeOptions = computed(() => [
  { label: t('reverse.targetType.auto'), value: null },
  { label: t('reverse.targetType.web_js'), value: 'web_js' },
  { label: t('reverse.targetType.android'), value: 'android' },
  { label: t('reverse.targetType.ios'), value: 'ios' },
  { label: t('reverse.targetType.windows'), value: 'windows' },
  { label: t('reverse.targetType.native'), value: 'native' },
  { label: t('reverse.targetType.mixed'), value: 'mixed' },
])


const statusOptions = computed(() => [
  { label: t('reverse.status.all'), value: '' },
  { label: t('reverse.status.active'), value: 'active' },
  { label: t('reverse.status.paused'), value: 'paused' },
  { label: t('reverse.status.done'), value: 'done' },
  { label: t('reverse.status.archived'), value: 'archived' },
])

const filtered = computed(() => {
  const q = (queryText.value || '').trim().toLowerCase()
  return jobs.value.filter((j) => {
    if (q) {
      const hay = [
        j.title || '',
        j.target_url || '',
        j.scope || '',
        (j.tags || []).join(' '),
        j.id || '',
      ].join(' ').toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
})

const {
  page: revPage,
  pageSize: revPageSize,
  total: revFilteredTotal,
  pageCount: revPageCount,
  pageItems: revPageItems,
  selectedCount: revSelectedCount,
  selectedIds: revSelectedIds,
  isSelected: revIsSelected,
  toggle: revToggle,
  clearSelection: revClearSelection,
  selectPage: revSelectPage,
  selectAllFiltered: revSelectAllFiltered,
} = useClientList(filtered)
revPageSize.value = 20

const detailTabs = computed(() => [
  { name: 'overview', label: t('reverse.tab.overview') },
  { name: 'static', label: t('reverse.tab.static') },
  { name: 'dynamic', label: t('reverse.tab.dynamic') },
  { name: 'evidence', label: t('reverse.tab.evidence') },
])

const staticTabs = computed(() => [
  { name: 'summary', label: t('reverse.tab.staticSummary') },
  { name: 'artifacts', label: `${t('reverse.tab.artifacts')} (${detail.value?.artifacts?.length || 0})` },
])

const dynamicTabs = computed(() => [
  { name: 'runtime', label: t('reverse.tab.runtime') },
  { name: 'hooks', label: `${t('reverse.tab.hooks')} (${detail.value?.hooks?.length || 0})` },
])

const evidenceTabs = computed(() => [
  { name: 'findings', label: `${t('reverse.tab.findings')} (${detail.value?.findings?.length || 0})` },
  { name: 'reproduce', label: `${t('reverse.tab.reproduce')} (${detail.value?.reproduce_scripts?.length || 0})` },
  { name: 'events', label: t('reverse.tab.events') },
])

const selectedJobs = computed(() => jobs.value.filter((job) => revSelectedIds.value.includes(job.id)))
const selectedActiveCount = computed(() => selectedJobs.value.filter((job) => !job.trashed).length)
const selectedTrashedCount = computed(() => selectedJobs.value.filter((job) => job.trashed).length)

const stageSummary = computed(() => {
  const job = detail.value || {}
  const analysis = job.analysis_summary || {}
  const artifacts = Array.isArray(job.artifacts) ? job.artifacts : []
  const hooks = Array.isArray(job.hooks) ? job.hooks : []
  const runtimeSessions = Array.isArray(job.runtime_sessions) ? job.runtime_sessions : []
  const findings = Array.isArray(job.findings) ? job.findings : []
  const reproduce = Array.isArray(job.reproduce_scripts) ? job.reproduce_scripts : []
  const evidence = Array.isArray(job.evidence_refs) ? job.evidence_refs : []
  const replay = Array.isArray(job.replay_refs) ? job.replay_refs : []
  const staticTools = Array.isArray(analysis.static?.tools) ? analysis.static.tools : []
  const dynamicTools = Array.isArray(analysis.dynamic?.tools) ? analysis.dynamic.tools : []
  const hasSample = !!job.target_meta?.sha256 || artifacts.length > 0
  const hasStaticSignal = staticTools.length > 0 || artifacts.some((artifact) => (
    ['manifest', 'plist', 'capa_report', 'yara_match', 'strings'].includes(String(artifact?.kind || ''))
  ))
  const hasDynamicSignal = dynamicTools.length > 0 || runtimeSessions.length > 0 || hooks.length > 0
  const evidenceCount = findings.length + reproduce.length + evidence.length + replay.length
  return [
    {
      key: 'sample',
      label: t('reverse.stage.sample'),
      state: hasSample ? t('reverse.stage.recorded') : t('reverse.stage.unknown'),
      detail: hasSample
        ? `${t('reverse.stage.sampleDetail', { n: artifacts.length })}${job.target_meta?.sha256 ? ` · ${t('reverse.stage.sha256Recorded')}` : ''}`
        : t('reverse.stage.noSample'),
      tone: hasSample ? 'done' : 'unknown',
    },
    {
      key: 'static',
      label: t('reverse.tab.static'),
      state: analysis.static?.done === true
        ? t('reverse.runtime.done')
        : (hasStaticSignal ? t('reverse.stage.progress') : t('reverse.stage.unknown')),
      detail: staticTools.length
        ? staticTools.join(', ')
        : (hasStaticSignal ? t('reverse.stage.hasStatic') : t('reverse.stage.noPhase')),
      tone: analysis.static?.done === true ? 'done' : (hasStaticSignal ? 'progress' : 'unknown'),
    },
    {
      key: 'dynamic',
      label: t('reverse.tab.dynamic'),
      state: analysis.dynamic?.done === true
        ? t('reverse.runtime.done')
        : (hasDynamicSignal ? t('reverse.stage.progress') : t('reverse.stage.unknown')),
      detail: dynamicTools.length
        ? dynamicTools.join(', ')
        : (hasDynamicSignal
          ? t('reverse.stage.dynamicDetail', { sessions: runtimeSessions.length, hooks: hooks.length })
          : t('reverse.stage.noPhase')),
      tone: analysis.dynamic?.done === true ? 'done' : (hasDynamicSignal ? 'progress' : 'unknown'),
    },
    {
      key: 'evidence',
      label: t('reverse.tab.evidence'),
      state: evidenceCount ? t('reverse.stage.recorded') : t('reverse.stage.unknown'),
      detail: evidenceCount
        ? t('reverse.stage.evidenceDetail', {
            findings: findings.length,
            reproduce: reproduce.length,
            refs: evidence.length + replay.length,
          })
        : t('reverse.stage.noArchive'),
      tone: evidenceCount ? 'done' : 'unknown',
    },
  ]
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.reverseJobList({
      status: statusFilter.value,
      includeTrashed: includeTrashed.value,
    })
    if (res && res.ok) {
      jobs.value = res.jobs || []
      root.value = res.root || ''
      for (const id of revSelectedIds.value) {
        if (!jobs.value.some((job) => job.id === id)) revToggle(id, false)
      }
    } else {
      error.value = (res && res.error) || t('reverse.error.loadFailed')
    }
  } catch (e) {
    error.value = String((e && e.message) || e)
  } finally {
    loading.value = false
  }
}

async function loadDetail(id) {
  if (!id) {
    detail.value = null
    return
  }
  detailLoading.value = true
  detailError.value = ''
  try {
    const res = await api.reverseJobGet(id, { includeTrashed: includeTrashed.value })
    if (res && res.ok) {
      detail.value = res.job
    } else {
      detailError.value = (res && res.error) || t('reverse.error.loadFailed')
      detail.value = null
    }
  } catch (e) {
    detailError.value = String((e && e.message) || e)
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

function selectJob(id) {
  if (id === selectedId.value) return
  selectedId.value = id
  if (id) {
    navigate(`/reverse/${id}`)
  } else {
    navigate('/reverse')
  }
}

watch(() => route.params?.id, (newId) => {
  const id = newId || ''
  if (id !== selectedId.value) {
    selectedId.value = id
  }
  if (id) {
    loadDetail(id)
  } else {
    detail.value = null
  }
}, { immediate: true })

function openCreateDialog() {
  createDraft.value = emptyCreateDraft()
  showCreateDialog.value = true
}

function closeCreateDialog() {
  if (creating.value) return
  showCreateDialog.value = false
}

function parseTags(text) {
  return String(text || '')
    .split(/[\s,;]+/)
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
}

async function createJob() {
  const title = (createDraft.value.title || '').trim()
  if (!title) {
    msg.warning(t('reverse.create.titleRequired'))
    return
  }
  creating.value = true
  try {
    const payload = {
      title,
      target_url: (createDraft.value.target_url || '').trim(),
      scope: (createDraft.value.scope || '').trim(),
      tags: parseTags(createDraft.value.tagsText),
      intent: (createDraft.value.intent || '').trim(),
      target_type: (createDraft.value.target_type || '').trim(),
      platform: (createDraft.value.platform || '').trim(),
    }
    const res = await api.reverseJobCreate(payload)
    if (res && res.ok && res.job) {
      msg.success(t('reverse.create.success'))
      showCreateDialog.value = false
      await load()
      selectJob(res.job.id)
    } else {
      msg.error((res && res.error) || t('reverse.create.failed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    creating.value = false
  }
}

async function trashJob(id) {
  if (!id) return
  const title = detail.value?.id === id
    ? (detail.value.title || id)
    : (jobs.value.find((j) => j.id === id)?.title || id)
  const ok = await confirmDialog({
    title: t('reverse.action.trash'),
    content: t('reverse.action.trashConfirm', { title }),
    positiveText: t('reverse.action.trash'),
    negativeText: t('reverse.create.cancel'),
    type: 'warning',
  })
  if (!ok) return
  try {
    const res = await api.reverseJobTrash(id)
    if (res && res.ok) {
      msg.success(t('reverse.action.trashed'))
      revToggle(id, false)
      // Force-clear local state synchronously so the right pane and
      // the left list don't keep the trashed job around while the
      // route watcher catches up (pywebview's hashchange can lag a
      // frame and the watch ordering is not guaranteed before await).
      if (selectedId.value === id) {
        selectedId.value = ''
        detail.value = null
        navigate('/reverse')
      }
      jobs.value = jobs.value.filter((j) => j.id !== id)
      await load()
    } else {
      msg.error((res && res.error) || t('reverse.action.failed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  }
}

async function batchTrashJobs() {
  const ids = selectedJobs.value.filter((job) => !job.trashed).map((job) => job.id)
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('reverse.action.trash'),
    content: t('reverse.action.trashBatchConfirm', { n: ids.length }),
    positiveText: t('reverse.action.trash'),
    negativeText: t('reverse.create.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.reverseJobTrash(id)
      if (res?.ok) {
        okN++
        if (selectedId.value === id) {
          selectedId.value = ''
          detail.value = null
          navigate('/reverse')
        }
      } else fail++
    } catch { fail++ }
  }
  revClearSelection()
  await load()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
}

async function batchRestoreJobs() {
  const ids = selectedJobs.value.filter((job) => job.trashed).map((job) => job.id)
  if (!ids.length) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.reverseJobRestore(id)
      if (res?.ok) okN++
      else fail++
    } catch {
      fail++
    }
  }
  revClearSelection()
  await load()
  showToast(
    fail
      ? t('reverse.action.restoreBatchPartial', { ok: okN, fail })
      : t('reverse.action.restoreBatchDone', { n: okN }),
    fail ? 'error' : 'ok',
  )
}

async function batchHardDeleteJobs() {
  const ids = selectedJobs.value.filter((job) => job.trashed).map((job) => job.id)
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('reverse.action.hardDelete'),
    content: t('reverse.action.hardDeleteBatchConfirm', { n: ids.length }),
    positiveText: t('reverse.action.hardDelete'),
    negativeText: t('reverse.create.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.reverseJobHardDelete(id)
      if (res?.ok) {
        okN++
        if (selectedId.value === id) {
          selectedId.value = ''
          detail.value = null
          navigate('/reverse')
        }
      } else fail++
    } catch {
      fail++
    }
  }
  revClearSelection()
  await load()
  showToast(
    fail
      ? t('reverse.action.hardDeleteBatchPartial', { ok: okN, fail })
      : t('reverse.action.hardDeleteBatchDone', { n: okN }),
    fail ? 'error' : 'ok',
  )
}

async function restoreJob(id) {
  if (!id) return
  try {
    const res = await api.reverseJobRestore(id)
    if (res && res.ok) {
      msg.success(t('reverse.action.restored'))
      // After restore, drop the local 'trashed' badge so the right
      // pane stops showing the destructive buttons immediately.
      if (detail.value && detail.value.id === id) {
        detail.value = { ...detail.value, _trashed: false }
      }
      await load()
      if (id) await loadDetail(id)
    } else {
      msg.error((res && res.error) || t('reverse.action.failed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  }
}

async function hardDeleteJob(id) {
  if (!id) return
  const title = detail.value?.id === id
    ? (detail.value.title || id)
    : (jobs.value.find((j) => j.id === id)?.title || id)
  const ok = await confirmDialog({
    title: t('reverse.action.hardDelete'),
    content: t('reverse.action.hardDeleteConfirm', { title }),
    positiveText: t('reverse.action.hardDelete'),
    negativeText: t('reverse.create.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    const res = await api.reverseJobHardDelete(id)
    if (res && res.ok) {
      msg.success(t('reverse.action.deleted'))
      if (selectedId.value === id) {
        selectedId.value = ''
        detail.value = null
        navigate('/reverse')
      }
      jobs.value = jobs.value.filter((j) => j.id !== id)
      await load()
    } else {
      msg.error((res && res.error) || t('reverse.action.failed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  }
}

async function openJobDir(id) {
  if (!id) return
  try {
    const res = await api.reverseJobRevealDir(id)
    if (!res || res.ok === false) {
      msg.error((res && res.error) || t('reverse.action.openDirFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  }
}

const exportingJob = ref(false)
const showExportDialog = ref(false)
const exportDraft = ref({ includeDecompiled: false, asZip: true })

function openExportDialog() {
  exportDraft.value = { includeDecompiled: false, asZip: true }
  showExportDialog.value = true
}

function closeExportDialog() {
  if (exportingJob.value) return
  showExportDialog.value = false
}

async function runExportJob(id) {
  if (!id || exportingJob.value) return
  exportingJob.value = true
  try {
    const res = await api.reverseJobExport(id, {
      includeDecompiled: !!exportDraft.value.includeDecompiled,
      asZip: !!exportDraft.value.asZip,
    })
    if (res && res.ok) {
      msg.success(t('reverse.action.exportSuccess', { path: res.path || '' }))
      showExportDialog.value = false
    } else {
      msg.error((res && res.error) || t('reverse.action.exportFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    exportingJob.value = false
  }
}

function formatRelTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const now = new Date()
    const diff = (now - d) / 1000
    if (diff < 60) return t('reverse.time.justNow')
    if (diff < 3600) return t('reverse.time.minAgo', { n: Math.floor(diff / 60) })
    if (diff < 86400) return t('reverse.time.hourAgo', { n: Math.floor(diff / 3600) })
    if (diff < 86400 * 30) return t('reverse.time.dayAgo', { n: Math.floor(diff / 86400) })
    return d.toLocaleDateString()
  } catch {
    return ''
  }
}

function formatTargetBadge(job) {
  const tt = job?.target_type
  if (tt && tt !== 'unknown') return tt
  return t('reverse.unknown')
}

async function loadEvents(id) {
  if (!id) {
    events.value = []
    return
  }
  eventsLoading.value = true
  try {
    const res = await api.reverseJobEvents(id, 100)
    events.value = (res && res.ok && res.events) ? res.events : []
  } catch {
    events.value = []
  } finally {
    eventsLoading.value = false
  }
}

watch([activeTab, evidenceTab], ([tab, subTab]) => {
  if (tab === 'evidence' && subTab === 'events' && selectedId.value) loadEvents(selectedId.value)
})

watch(selectedId, (id) => {
  if (activeTab.value === 'evidence' && evidenceTab.value === 'events') loadEvents(id)
})

watch(includeTrashed, () => { load() })
watch(statusFilter, () => { load() })

// ---- Hook tab actions ----

async function loadRecipes() {
  try {
    const res = await api.reverseRecipeList()
    if (res && res.ok) {
      recipes.value = res.recipes || []
    }
  } catch (e) {
    /* swallow — recipes are optional */
  }
}

const recipeOptions = computed(() => {
  const out = [{ label: t('reverse.hooks.recipePicker'), value: '' }]
  for (const r of recipes.value) {
    out.push({ label: r.title, value: r.id })
  }
  return out
})

const selectedRecipe = computed(() => recipes.value.find((r) => r.id === selectedRecipeId.value) || null)

async function applyRecipe() {
  if (!selectedId.value || !selectedRecipeId.value) return
  applyingRecipe.value = true
  try {
    const res = await api.reverseRecipeApply(selectedId.value, selectedRecipeId.value)
    if (res && res.ok) {
      msg.success(t('reverse.hooks.recipeApplied', { n: (res.installed || []).length }))
      selectedRecipeId.value = ''
      await loadDetail(selectedId.value)
    } else {
      msg.error((res && res.error) || t('reverse.hooks.recipeFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    applyingRecipe.value = false
  }
}

function openCustomHookDialog() {
  customHookDraft.value = emptyCustomHook()
  showCustomHookDialog.value = true
}

function closeCustomHookDialog() {
  if (customHookSaving.value) return
  showCustomHookDialog.value = false
}

async function saveCustomHook() {
  if (!selectedId.value) return
  const target = (customHookDraft.value.target || '').trim()
  if (!target) {
    msg.warning(t('reverse.hooks.customTargetRequired'))
    return
  }
  customHookSaving.value = true
  try {
    const payload = {
      target,
      kind: customHookDraft.value.kind || 'function',
    }
    // Save via the lower-level reverseHookAdd; the extended behaviour
    // fields aren't exposed via this API yet, so we round-trip through
    // reverse_recipe_apply by injecting a one-off "ad-hoc" recipe-like
    // body. Simpler path: just persist the basic hook via reverseHookAdd
    // and let the user fine-tune via SKILL-driven AI tool calls.
    const res = await api.reverseHookAdd(selectedId.value, payload)
    if (res && res.ok) {
      msg.success(t('reverse.hooks.customSaved'))
      showCustomHookDialog.value = false
      await loadDetail(selectedId.value)
    } else {
      msg.error((res && res.error) || t('reverse.hooks.customFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    customHookSaving.value = false
  }
}

async function drainAllHooks() {
  if (!selectedId.value) return
  draining.value = true
  try {
    const res = await api.reverseHookDrainRun(selectedId.value, '')
    if (res && res.ok) {
      msg.success(t('reverse.hooks.drainDone', { n: res.drained || 0, dropped: res.dropped || 0 }))
      await loadDetail(selectedId.value)
    } else {
      msg.error((res && res.error) || t('reverse.hooks.drainFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    draining.value = false
  }
}

async function reattachHooks() {
  if (!selectedId.value) return
  reattaching.value = true
  try {
    const res = await api.reverseHookReattachRun(selectedId.value)
    if (res && res.ok) {
      msg.success(t('reverse.hooks.reattached', { n: res.reattached || 0 }))
    } else {
      msg.error((res && res.error) || t('reverse.hooks.reattachFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    reattaching.value = false
  }
}

async function removeHook(hookId) {
  if (!selectedId.value || !hookId) return
  try {
    const res = await api.reverseHookRemove(selectedId.value, hookId)
    if (res && res.ok) {
      msg.success(t('reverse.hooks.removed'))
      if (expandedHookId.value === hookId) expandedHookId.value = ''
      await loadDetail(selectedId.value)
    } else {
      msg.error((res && res.error) || t('reverse.hooks.removeFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  }
}

async function toggleHookExpansion(hookId) {
  if (expandedHookId.value === hookId) {
    expandedHookId.value = ''
    return
  }
  expandedHookId.value = hookId
  await loadHookHits(hookId)
}

async function loadHookHits(hookId) {
  if (!selectedId.value || !hookId) return
  hookHitsLoading.value = { ...hookHitsLoading.value, [hookId]: true }
  try {
    const res = await api.reverseHookHits(selectedId.value, hookId, 200)
    if (res && res.ok) {
      hookHits.value = { ...hookHits.value, [hookId]: res.hits || [] }
    }
  } catch (e) {
    /* swallow */
  } finally {
    hookHitsLoading.value = { ...hookHitsLoading.value, [hookId]: false }
  }
}

function toggleLiveTail() {
  if (liveTailing.value) {
    if (liveTailTimer.value) {
      clearInterval(liveTailTimer.value)
      liveTailTimer.value = null
    }
    liveTailing.value = false
    return
  }
  liveTailing.value = true
  liveTailTimer.value = setInterval(async () => {
    if (!selectedId.value || activeTab.value !== 'dynamic' || dynamicTab.value !== 'hooks') return
    try {
      await api.reverseHookDrainRun(selectedId.value, '')
      await loadDetail(selectedId.value)
      if (expandedHookId.value) {
        await loadHookHits(expandedHookId.value)
      }
    } catch (e) {
      /* swallow */
    }
  }, 2500)
}

function formatHitTime(ts) {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleTimeString()
  } catch {
    return ts
  }
}

function formatHitSummary(hit) {
  if (!hit) return ''
  if (hit.kind === 'function') {
    var args = Array.isArray(hit.args) ? hit.args : []
    return args.length
      ? args.map((a) => JSON.stringify(a)).join(', ').slice(0, 200)
      : '(no args)'
  }
  if (hit.kind === 'property-get') {
    return 'get → ' + JSON.stringify(hit.ret).slice(0, 200)
  }
  if (hit.kind === 'property-set') {
    return 'set = ' + JSON.stringify(hit.value).slice(0, 200)
  }
  return JSON.stringify(hit).slice(0, 200)
}

function clearReverseFilters() {
  queryText.value = ''
  statusFilter.value = ''
}

function handleReverseEmptyAction() {
  if (jobs.value.length || queryText.value || statusFilter.value) clearReverseFilters()
  else openCreateDialog()
}

function handleJobKeydown(event, index, job) {
  if (event.target !== event.currentTarget) return
  if (event.key === 'Enter') {
    event.preventDefault()
    selectJob(job.id)
    return
  }
  if (event.key === ' ') {
    event.preventDefault()
    revToggle(job.id, !revIsSelected(job.id))
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  event.preventDefault()
  const rows = event.currentTarget.parentElement?.querySelectorAll('.rev-list-item') || []
  const next = event.key === 'ArrowDown'
    ? Math.min(index + 1, rows.length - 1)
    : Math.max(index - 1, 0)
  rows[next]?.focus()
}

function chooseDetailTab(group, name) {
  if (group === 'primary') activeTab.value = name
  else if (group === 'static') staticTab.value = name
  else if (group === 'dynamic') dynamicTab.value = name
  else if (group === 'evidence') evidenceTab.value = name
}

async function handleTabKeydown(event, index, tabs, group) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  let next = index
  if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  chooseDetailTab(group, tabs[next].name)
  await nextTick()
  event.currentTarget.parentElement?.querySelectorAll('[role="tab"]')[next]?.focus()
}

// Live push: reverse jobs / findings / artifacts / hooks progress in the
// background (agent- or runtime-driven), so refresh the list and the open
// detail the instant the store emits an event.
useLiveChannel('reverse', (payload) => {
  load()
  const openId = route.params?.id
  if (openId && (!payload?.job_id || String(payload.job_id) === String(openId))) {
    loadDetail(openId)
    loadEvents(openId)
  }
}, { debounceMs: 250 })

onMounted(() => {
  load()
  loadRecipes()
})

onUnmounted(() => {
  if (liveTailTimer.value) {
    clearInterval(liveTailTimer.value)
    liveTailTimer.value = null
  }
})
</script>

<template>
  <div class="reverse-panel">
    <header class="page-head">
      <div>
        <h2>{{ t('reverse.title') }}</h2>
        <p>{{ t('reverse.subtitle') }}</p>
      </div>
      <div class="row-gap-sm">
        <NButton size="small" quaternary :loading="loading" @click="load">
          {{ t('reverse.toolbar.refresh') }}
        </NButton>
        <NButton type="primary" size="small" @click="openCreateDialog">
          {{ t('reverse.create.button') }}
        </NButton>
      </div>
    </header>

    <ListToolbar v-model="queryText" :placeholder="t('reverse.search.placeholder')">
      <template #filters>
        <ListSelect v-model="statusFilter" :options="statusOptions" min-width="130px" />
        <label class="rev-toggle">
          <NSwitch v-model:value="includeTrashed" size="small" />
          <span>{{ t('reverse.toolbar.includeTrashed') }}</span>
        </label>
      </template>
      <button v-if="queryText || statusFilter" type="button" class="ghost sm" @click="clearReverseFilters">{{ t('common.clearFilters') }}</button>
    </ListToolbar>

    <div v-if="error" class="rev-error">{{ error }}</div>

    <div class="rev-body">
      <aside class="rev-list">
        <ListBatchBar
          :count="revSelectedCount"
          :total="revFilteredTotal"
          @select-page="revSelectPage(true)"
          @select-all="revSelectAllFiltered(true)"
          @clear="revClearSelection()"
        >
          <button v-if="selectedActiveCount" type="button" class="ghost tiny danger" @click="batchTrashJobs">{{ t('reverse.action.trash') }}</button>
          <button v-if="selectedTrashedCount" type="button" class="ghost tiny" @click="batchRestoreJobs">{{ t('reverse.action.restore') }}</button>
          <button v-if="selectedTrashedCount" type="button" class="danger tiny" @click="batchHardDeleteJobs">{{ t('reverse.action.hardDelete') }}</button>
        </ListBatchBar>
        <div v-if="loading" class="rev-list-loading" role="status">{{ t('common.loading') }}</div>
        <PageEmpty
          v-else-if="revFilteredTotal === 0"
          :title="queryText || statusFilter ? t('common.noMatch') : t('reverse.empty.list')"
          :description="queryText || statusFilter ? t('common.noMatchHint') : t('reverse.empty.importHint')"
          :action-label="queryText || statusFilter ? t('common.clearFilters') : t('reverse.create.button')"
          @action="handleReverseEmptyAction"
        />
        <template v-else>
          <ul class="rev-list-ul list-body">
            <li
              v-for="(job, index) in revPageItems"
              :key="job.id"
              :class="{ 'rev-list-item': true, active: job.id === selectedId, selected: revIsSelected(job.id), trashed: job.trashed }"
              tabindex="0"
              role="option"
              :aria-selected="job.id === selectedId"
              @click="selectJob(job.id)"
              @keydown="handleJobKeydown($event, index, job)"
            >
              <div class="rev-list-row">
                <div class="rev-list-title">
                  <input
                    class="list-row-check"
                    type="checkbox"
                    :checked="revIsSelected(job.id)"
                    @click.stop
                    @change="revToggle(job.id, $event.target.checked)"
                  />
                  {{ job.title || '(untitled)' }}
                  <span v-if="formatTargetBadge(job)" class="rev-target-pill">{{ formatTargetBadge(job) }}</span>
                  <span v-if="job.trashed" class="rev-trashed-pill">{{ t('reverse.trashed') }}</span>
                </div>
                <div class="rev-list-meta">
                  <span class="rev-list-status" :data-status="job.status">{{ job.status }}</span>
                  <span class="rev-list-time">{{ formatRelTime(job.updated_at || job.created_at) }}</span>
                </div>
              </div>
              <div v-if="job.target_url" class="rev-list-url" :title="job.target_url">{{ job.target_url }}</div>
              <div v-if="(job.tags || []).length" class="rev-list-tags">
                <NTag v-for="tg in job.tags" :key="tg" size="tiny" :bordered="false">{{ tg }}</NTag>
              </div>
              <div class="rev-list-counters">
                <span :title="t('reverse.counters.artifacts')">
                  <span class="rev-counter-icon">A</span>{{ job.artifact_count || 0 }}
                </span>
                <span :title="t('reverse.counters.hooks')">
                  <span class="rev-counter-icon">H</span>{{ job.hook_count || 0 }}
                </span>
                <span :title="t('reverse.counters.findings')">
                  <span class="rev-counter-icon">F</span>{{ job.finding_count || 0 }}
                </span>
              </div>
            </li>
          </ul>
          <ListPager
            v-model:page="revPage"
            v-model:page-size="revPageSize"
            :page-count="revPageCount"
            :total="revFilteredTotal"
          />
        </template>
      </aside>

      <section class="rev-detail">
        <div v-if="!selectedId" class="rev-detail-empty">
          <NEmpty :description="t('reverse.empty.detail')" />
        </div>
        <div v-else-if="detailLoading" class="rev-detail-loading">{{ t('reverse.detail.loading') }}</div>
        <div v-else-if="detailError" class="rev-error">{{ detailError }}</div>
        <div v-else-if="detail" class="rev-detail-inner">
          <header class="rev-detail-head">
            <div class="rev-detail-head-left">
              <h3>{{ detail.title }}</h3>
              <div class="rev-detail-meta">
                <span class="rev-list-status" :data-status="detail.status">{{ detail.status }}</span>
                <span class="rev-target-pill">{{ detail.target_type && detail.target_type !== 'unknown' ? detail.target_type : t('reverse.overview.typeUnknown') }}</span>
                <span class="rev-platform-pill">{{ detail.platform && detail.platform !== 'unknown' ? detail.platform : t('reverse.overview.platformUnknown') }}</span>
                <span v-if="detail.target_url" class="rev-detail-url">{{ detail.target_url }}</span>
                <span v-if="detail.scope" class="rev-detail-scope">scope: <code>{{ detail.scope }}</code></span>
              </div>
              <div v-if="(detail.tags || []).length" class="rev-detail-tags">
                <NTag v-for="tg in detail.tags" :key="tg" size="small" :bordered="false">{{ tg }}</NTag>
              </div>
            </div>
            <div class="rev-detail-head-right">
              <NButton
                size="small"
                quaternary
                :title="t('reverse.action.openDirTip')"
                @click="openJobDir(detail.id)"
              >
                {{ t('reverse.action.openDir') }}
              </NButton>
              <NButton
                size="small"
                quaternary
                :title="t('reverse.action.exportTip')"
                @click="openExportDialog"
              >
                {{ t('reverse.action.export') }}
              </NButton>
              <NButton
                v-if="!detail._trashed"
                size="small"
                type="error"
                ghost
                @click="trashJob(detail.id)"
              >
                {{ t('reverse.action.trash') }}
              </NButton>
              <template v-else>
                <NButton size="small" type="primary" @click="restoreJob(detail.id)">
                  {{ t('reverse.action.restore') }}
                </NButton>
                <NButton size="small" type="error" @click="hardDeleteJob(detail.id)">
                  {{ t('reverse.action.hardDelete') }}
                </NButton>
              </template>
            </div>
          </header>

          <nav class="rev-tabs" role="tablist" :aria-label="t('reverse.aria.primaryTabs')">
            <button
              v-for="(tab, index) in detailTabs"
              :key="tab.name"
              :class="{ active: activeTab === tab.name }"
              type="button"
              role="tab"
              :aria-selected="activeTab === tab.name"
              :tabindex="activeTab === tab.name ? 0 : -1"
              @click="chooseDetailTab('primary', tab.name)"
              @keydown="handleTabKeydown($event, index, detailTabs, 'primary')"
            >{{ tab.label }}</button>
          </nav>

          <div class="rev-stage-strip" :aria-label="t('reverse.aria.stageStrip')">
            <div
              v-for="stage in stageSummary"
              :key="stage.key"
              class="rev-stage-step"
              :data-tone="stage.tone"
            >
              <span>{{ stage.label }}</span>
              <strong>{{ stage.state }}</strong>
              <small :title="stage.detail">{{ stage.detail }}</small>
            </div>
          </div>

          <nav v-if="activeTab === 'static'" class="rev-subtabs" role="tablist" :aria-label="t('reverse.aria.staticTabs')">
            <button
              v-for="(tab, index) in staticTabs"
              :key="tab.name"
              type="button"
              role="tab"
              :aria-selected="staticTab === tab.name"
              :tabindex="staticTab === tab.name ? 0 : -1"
              :class="{ active: staticTab === tab.name }"
              @click="chooseDetailTab('static', tab.name)"
              @keydown="handleTabKeydown($event, index, staticTabs, 'static')"
            >{{ tab.label }}</button>
          </nav>
          <nav v-else-if="activeTab === 'dynamic'" class="rev-subtabs" role="tablist" :aria-label="t('reverse.aria.dynamicTabs')">
            <button
              v-for="(tab, index) in dynamicTabs"
              :key="tab.name"
              type="button"
              role="tab"
              :aria-selected="dynamicTab === tab.name"
              :tabindex="dynamicTab === tab.name ? 0 : -1"
              :class="{ active: dynamicTab === tab.name }"
              @click="chooseDetailTab('dynamic', tab.name)"
              @keydown="handleTabKeydown($event, index, dynamicTabs, 'dynamic')"
            >{{ tab.label }}</button>
          </nav>
          <nav v-else-if="activeTab === 'evidence'" class="rev-subtabs" role="tablist" :aria-label="t('reverse.aria.evidenceTabs')">
            <button
              v-for="(tab, index) in evidenceTabs"
              :key="tab.name"
              type="button"
              role="tab"
              :aria-selected="evidenceTab === tab.name"
              :tabindex="evidenceTab === tab.name ? 0 : -1"
              :class="{ active: evidenceTab === tab.name }"
              @click="chooseDetailTab('evidence', tab.name)"
              @keydown="handleTabKeydown($event, index, evidenceTabs, 'evidence')"
            >{{ tab.label }}</button>
          </nav>

          <div class="rev-tab-body">
            <section v-if="activeTab === 'overview'" class="rev-tab-pane">
              <div class="rev-overview-grid">
                <div class="rev-info-card">
                  <h4>{{ t('reverse.overview.stack') }}</h4>
                  <dl>
                    <dt>{{ t('reverse.stack.framework') }}</dt><dd>{{ detail.stack?.framework || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.stack.bundler') }}</dt><dd>{{ detail.stack?.bundler || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.stack.obfuscation') }}</dt><dd>{{ detail.stack?.obfuscation || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.stack.antibot') }}</dt><dd>{{ (detail.stack?.antibot || []).join(', ') || t('reverse.unknown') }}</dd>
                  </dl>
                </div>
                <div class="rev-info-card">
                  <h4>{{ t('reverse.overview.identity') }}</h4>
                  <dl>
                    <dt>ID</dt><dd><code>{{ detail.id }}</code></dd>
                    <dt>{{ t('reverse.overview.created') }}</dt><dd>{{ detail.created_at || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.overview.updated') }}</dt><dd>{{ detail.updated_at || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.overview.linkedTask') }}</dt><dd>{{ detail.linked_task_id || t('reverse.overview.unlinked') }}</dd>
                  </dl>
                </div>
                <div class="rev-info-card">
                  <h4>{{ t('reverse.overview.recordedData') }}</h4>
                  <dl>
                    <dt>{{ t('reverse.tab.artifacts') }}</dt><dd>{{ (detail.artifacts || []).length }}</dd>
                    <dt>{{ t('reverse.tab.hooks') }}</dt><dd>{{ (detail.hooks || []).length }}</dd>
                    <dt>{{ t('reverse.tab.findings') }}</dt><dd>{{ (detail.findings || []).length }}</dd>
                    <dt>{{ t('reverse.tab.reproduce') }}</dt><dd>{{ (detail.reproduce_scripts || []).length }}</dd>
                  </dl>
                </div>
                <div class="rev-info-card">
                  <h4>{{ t('reverse.runtime.risk') }}</h4>
                  <dl>
                    <dt>{{ t('reverse.overview.riskOverall') }}</dt><dd>{{ detail.risk_summary?.overall || t('reverse.unknown') }}</dd>
                    <dt>{{ t('reverse.overview.riskTags') }}</dt><dd>{{ (detail.risk_summary?.tags || []).join(', ') || t('reverse.unknown') }}</dd>
                  </dl>
                </div>
                <div class="rev-info-card rev-info-intent">
                  <h4>{{ t('reverse.overview.intent') }}</h4>
                  <p v-if="detail.intent" class="rev-intent-body">{{ detail.intent }}</p>
                  <p v-else class="rev-muted">{{ t('reverse.overview.noIntent') }}</p>
                </div>
              </div>
              <div v-if="detail.risk_summary?.notes" class="rev-skeleton-notice">
                {{ detail.risk_summary.notes }}
              </div>
              <div v-else class="rev-skeleton-notice">
                {{ t('reverse.overview.riskNotesEmpty') }}
              </div>
            </section>

            <section v-if="activeTab === 'static' && staticTab === 'summary'" class="rev-tab-pane">
              <ReverseRuntimeTab
                :job-id="selectedId"
                :detail="detail"
                mode="static"
                @refresh="loadDetail(selectedId)"
              />
            </section>

            <section v-if="activeTab === 'static' && staticTab === 'artifacts'" class="rev-tab-pane">
              <ReverseArtifactsTab
                :job-id="selectedId"
                :artifacts="detail.artifacts || []"
              />
            </section>

            <section v-if="activeTab === 'dynamic' && dynamicTab === 'runtime'" class="rev-tab-pane">
              <ReverseRuntimeTab
                :job-id="selectedId"
                :detail="detail"
                mode="dynamic"
                @refresh="loadDetail(selectedId)"
              />
            </section>

            <section v-if="activeTab === 'dynamic' && dynamicTab === 'hooks'" class="rev-tab-pane">
              <div class="rev-hooks-toolbar">
                <div class="rev-hooks-recipe-row">
                  <NSelect
                    v-model:value="selectedRecipeId"
                    :options="recipeOptions"
                    :placeholder="t('reverse.hooks.recipePicker')"
                    size="small"
                    class="rev-recipe-select"
                  />
                  <NButton
                    size="small"
                    type="primary"
                    :disabled="!selectedRecipeId"
                    :loading="applyingRecipe"
                    @click="applyRecipe"
                  >
                    {{ t('reverse.hooks.applyRecipe') }}
                  </NButton>
                  <NButton size="small" quaternary @click="openCustomHookDialog">
                    {{ t('reverse.hooks.addCustom') }}
                  </NButton>
                </div>
                <div v-if="selectedRecipe" class="rev-recipe-info">
                  <strong>{{ selectedRecipe.title }}</strong>
                  <span class="rev-recipe-coverage" v-if="(selectedRecipe.coverage || []).length">
                    <span v-for="cv in selectedRecipe.coverage" :key="cv" class="rev-coverage-tag">{{ cv }}</span>
                  </span>
                  <p class="rev-recipe-description">{{ selectedRecipe.description }}</p>
                  <details class="rev-recipe-targets">
                    <summary>{{ t('reverse.hooks.recipeTargets', { n: (selectedRecipe.hooks || []).length }) }}</summary>
                    <ul>
                      <li v-for="(h, idx) in selectedRecipe.hooks" :key="idx">
                        <span class="rev-pill">{{ h.kind }}</span>
                        <code>{{ h.target }}</code>
                        <span v-if="h.tag" class="rev-tag-inline">#{{ h.tag }}</span>
                      </li>
                    </ul>
                  </details>
                </div>
              </div>

              <div class="rev-hooks-live-bar" v-if="(detail.hooks || []).length">
                <NButton size="small" :loading="draining" @click="drainAllHooks">
                  {{ t('reverse.hooks.drainNow') }}
                </NButton>
                <NButton size="small" :loading="reattaching" quaternary @click="reattachHooks">
                  {{ t('reverse.hooks.reattach') }}
                </NButton>
                <label class="rev-toggle">
                  <NSwitch :value="liveTailing" size="small" @update:value="toggleLiveTail" />
                  <span>{{ t('reverse.hooks.liveTail') }}</span>
                </label>
              </div>

              <div v-if="!(detail.hooks || []).length" class="rev-muted">
                {{ t('reverse.empty.hooks') }}
                <div class="rev-recipe-hint">{{ t('reverse.hooks.recipeHint') }}</div>
              </div>
              <ul v-else class="rev-hooks-list">
                <li v-for="h in detail.hooks" :key="h.id" class="rev-hook-card">
                  <div class="rev-hook-head" @click="toggleHookExpansion(h.id)">
                    <span class="rev-pill">{{ h.kind }}</span>
                    <code class="rev-hook-target">{{ h.target }}</code>
                    <span v-if="h.recipe" class="rev-tag-inline">{{ h.recipe }}</span>
                    <span class="rev-hook-hits">
                      <strong>{{ h.hits_count || 0 }}</strong>
                      <span v-if="h.hits_dropped" class="rev-hook-dropped">+{{ h.hits_dropped }} dropped</span>
                    </span>
                    <span class="rev-hook-id">{{ h.id }}</span>
                    <NPopconfirm @positive-click="removeHook(h.id)">
                      <template #trigger>
                        <NButton size="tiny" quaternary @click.stop>
                          {{ t('reverse.hooks.remove') }}
                        </NButton>
                      </template>
                      {{ t('reverse.hooks.removeConfirm') }}
                    </NPopconfirm>
                  </div>
                  <div v-if="expandedHookId === h.id" class="rev-hook-body">
                    <div v-if="hookHitsLoading[h.id]" class="rev-muted">{{ t('reverse.hooks.loadingHits') }}</div>
                    <div v-else-if="!(hookHits[h.id] || []).length" class="rev-muted">{{ t('reverse.hooks.noHitsYet') }}</div>
                    <ul v-else class="rev-hits-list">
                      <li v-for="(hit, idx) in (hookHits[h.id] || [])" :key="idx" class="rev-hit">
                        <span class="rev-hit-time">{{ formatHitTime(hit.ts) }}</span>
                        <span class="rev-hit-kind">{{ hit.kind || 'function' }}</span>
                        <code class="rev-hit-summary">{{ formatHitSummary(hit) }}</code>
                      </li>
                    </ul>
                  </div>
                </li>
              </ul>
            </section>

            <section v-if="activeTab === 'evidence' && evidenceTab === 'findings'" class="rev-tab-pane">
              <div v-if="(detail.evidence_refs || []).length || (detail.replay_refs || []).length" class="rev-reference-grid">
                <section v-if="(detail.evidence_refs || []).length" class="rev-info-card">
                  <h4>{{ t('reverse.runtime.evidence') }}</h4>
                  <ul class="rev-reference-list">
                    <li v-for="(refItem, index) in detail.evidence_refs" :key="refItem.id || refItem.ref_id || index">
                      <span class="rev-pill">{{ refItem.type || 'evidence' }}</span>
                      <code>{{ refItem.ref_id || refItem.id || JSON.stringify(refItem) }}</code>
                    </li>
                  </ul>
                </section>
                <section v-if="(detail.replay_refs || []).length" class="rev-info-card">
                  <h4>{{ t('reverse.runtime.replay') }}</h4>
                  <ul class="rev-reference-list">
                    <li v-for="(refItem, index) in detail.replay_refs" :key="refItem.id || refItem.ref_id || index">
                      <span class="rev-pill">{{ refItem.type || 'replay' }}</span>
                      <code>{{ refItem.ref_id || refItem.id || JSON.stringify(refItem) }}</code>
                    </li>
                  </ul>
                </section>
              </div>
              <div v-if="!(detail.findings || []).length" class="rev-muted">{{ t('reverse.empty.findings') }}</div>
              <ul v-else class="rev-finding-list">
                <li v-for="f in detail.findings" :key="f.id" class="rev-finding-card">
                  <div class="rev-finding-head">
                    <span class="rev-pill">{{ f.kind || t('reverse.finding.unknownKind') }}</span>
                    <span class="rev-finding-title">{{ f.title || t('reverse.finding.untitled') }}</span>
                    <span v-if="f.coverage_id" class="rev-coverage-tag">{{ f.coverage_id }}</span>
                    <span class="rev-confidence" :data-conf="f.confidence">{{ f.confidence || 'unknown' }}</span>
                  </div>
                </li>
              </ul>
            </section>

            <section v-if="activeTab === 'evidence' && evidenceTab === 'reproduce'" class="rev-tab-pane">
              <div v-if="!(detail.reproduce_scripts || []).length" class="rev-muted">{{ t('reverse.empty.reproduce') }}</div>
              <table v-else class="rev-table">
                <thead>
                  <tr>
                    <th>{{ t('reverse.tableHeader.name') }}</th>
                    <th>{{ t('reverse.tableHeader.lang') }}</th>
                    <th>{{ t('reverse.tableHeader.verified') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in detail.reproduce_scripts" :key="r.id">
                    <td>{{ r.name || t('reverse.script.untitled') }}</td>
                    <td>{{ r.lang || t('reverse.unknown') }}</td>
                    <td>{{ r.verified === true ? t('reverse.tableHeader.verified') : (r.verified === false ? t('reverse.script.unverified') : t('reverse.unknown')) }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="!(detail.reproduce_scripts || []).length" class="rev-phase-notice">{{ t('reverse.script.noPlaceholder') }}</div>
            </section>

            <section v-if="activeTab === 'evidence' && evidenceTab === 'events'" class="rev-tab-pane">
              <div v-if="eventsLoading" class="rev-muted">{{ t('reverse.detail.loading') }}</div>
              <div v-else-if="!events.length" class="rev-muted">{{ t('reverse.events.empty') }}</div>
              <ul v-else class="rev-events-list">
                <li v-for="(ev, idx) in events" :key="idx">
                  <span class="rev-event-time">{{ ev.ts }}</span>
                  <span class="rev-pill">{{ ev.kind || 'unknown' }}</span>
                  <code class="rev-event-payload">{{ JSON.stringify(ev.payload || {}) }}</code>
                </li>
              </ul>
            </section>
          </div>
        </div>
      </section>
    </div>

    <WorkbenchModal
      :open="showCustomHookDialog"
      size="sm"
      :title="t('reverse.hooks.customTitle')"
      :busy="customHookSaving"
      @close="closeCustomHookDialog"
    >
      <div
          class="rev-modal-body"
          @keydown.ctrl.enter.stop.prevent="saveCustomHook"
          @keydown.meta.enter.stop.prevent="saveCustomHook"
        >
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.hooks.customTargetLabel') }} *</span>
              <NInput
                v-model:value="customHookDraft.target"
                :placeholder="t('reverse.hooks.customTargetPlaceholder')"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.hooks.customKindLabel') }}</span>
              <NSelect
                v-model:value="customHookDraft.kind"
                :options="[{ label: 'function', value: 'function' }, { label: 'property', value: 'property' }]"
                size="small"
              />
            </div>
            <p class="rev-muted">{{ t('reverse.hooks.customAdvancedHint') }}</p>
      </div>
      <template #footer>
        <div class="rev-modal-foot">
            <NButton size="small" quaternary :disabled="customHookSaving" @click="closeCustomHookDialog">
              {{ t('reverse.create.cancel') }}
            </NButton>
            <NButton size="small" type="primary" :loading="customHookSaving" @click="saveCustomHook">
              {{ t('reverse.hooks.customConfirm') }}
            </NButton>
        </div>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showCreateDialog"
      size="sm"
      :title="t('reverse.create.title')"
      :busy="creating"
      @close="closeCreateDialog"
    >
      <div
          class="rev-modal-body"
          @keydown.ctrl.enter.stop.prevent="createJob"
          @keydown.meta.enter.stop.prevent="createJob"
        >
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.titleLabel') }} *</span>
              <NInput
                v-model:value="createDraft.title"
                :placeholder="t('reverse.create.titlePlaceholder')"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.urlLabel') }}</span>
              <NInput
                v-model:value="createDraft.target_url"
                placeholder="https://target.example/login"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.scopeLabel') }}</span>
              <NInput
                v-model:value="createDraft.scope"
                placeholder="target.example"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.tagsLabel') }}</span>
              <NInput
                v-model:value="createDraft.tagsText"
                :placeholder="t('reverse.create.tagsPlaceholder')"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.targetTypeLabel') }}</span>
              <NSelect
                v-model:value="createDraft.target_type"
                :options="targetTypeOptions"
                size="small"
              />
            </div>
            <div class="rev-field">
              <span class="rev-field-label">{{ t('reverse.create.intentLabel') }}</span>
              <NInput
                v-model:value="createDraft.intent"
                type="textarea"
                :rows="3"
                :placeholder="t('reverse.create.intentPlaceholder')"
                size="small"
              />
            </div>
      </div>
      <template #footer>
        <div class="rev-modal-foot">
            <NButton size="small" quaternary :disabled="creating" @click="closeCreateDialog">
              {{ t('reverse.create.cancel') }}
            </NButton>
            <NButton size="small" type="primary" :loading="creating" @click="createJob">
              {{ t('reverse.create.confirm') }}
            </NButton>
        </div>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showExportDialog"
      size="sm"
      :title="t('reverse.export.title')"
      :description="t('reverse.export.intro')"
      :busy="exportingJob"
      @close="closeExportDialog"
    >
      <div
          class="rev-modal-body"
          @keydown.ctrl.enter.stop.prevent="runExportJob(selectedId)"
          @keydown.meta.enter.stop.prevent="runExportJob(selectedId)"
        >
            <div class="rev-option-list">
              <label class="rev-option-row">
                <NSwitch v-model:value="exportDraft.asZip" size="small" />
                <span class="rev-option-copy">
                  <strong>{{ t('reverse.export.asZip') }}</strong>
                </span>
              </label>
              <label class="rev-option-row">
                <NSwitch v-model:value="exportDraft.includeDecompiled" size="small" />
                <span class="rev-option-copy">
                  <strong>{{ t('reverse.export.includeDecompiled') }}</strong>
                  <small>{{ t('reverse.export.decompiledHint') }}</small>
                </span>
              </label>
            </div>
      </div>
      <template #footer>
        <div class="rev-modal-foot">
            <NButton size="small" quaternary :disabled="exportingJob" @click="closeExportDialog">
              {{ t('reverse.create.cancel') }}
            </NButton>
            <NButton size="small" type="primary" :loading="exportingJob" @click="runExportJob(selectedId)">
              {{ t('reverse.export.confirm') }}
            </NButton>
        </div>
      </template>
    </WorkbenchModal>
  </div>
</template>

<style scoped>
.reverse-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  padding: 18px 20px;
  gap: 14px;
}
.rev-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.18));
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel-2, #252526) 72%, transparent);
}
.rev-search { width: 260px; }
.rev-status-select { width: 130px; }
.rev-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted, #888);
}
.rev-refresh { margin-left: auto; }
.rev-error {
  padding: 8px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger, #f55) 12%, transparent);
  color: var(--danger, #f55);
  font-size: 12px;
}
.rev-body {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(280px, 310px) minmax(0, 1fr);
  gap: 14px;
  overflow: hidden;
  min-height: 0;
}
.rev-list {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  border-radius: 14px;
  background: color-mix(in srgb, var(--panel-2, #252526) 78%, transparent);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.18));
  padding: 8px;
}
.rev-list > .list-batch {
  flex-shrink: 0;
}
.rev-list-loading {
  flex: 1;
  display: grid;
  place-items: center;
  color: var(--muted, #888);
  font-size: 12px;
}
.rev-list-ul {
  list-style: none;
  padding: 0;
  margin: 0;
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.rev-list > .list-pager {
  flex-shrink: 0;
  margin-top: 8px;
}
.rev-list-item {
  padding: 10px 11px;
  border-radius: 9px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 120ms ease, border-color 120ms ease;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.rev-list-item:hover {
  background: color-mix(in srgb, var(--fg, #fff) 4%, transparent);
}
.rev-list-item:focus-visible {
  outline: 2px solid var(--accent, #4e8cff);
  outline-offset: -2px;
}
.rev-list-item.active {
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 30%, transparent);
}
.rev-list-item.selected:not(.active) {
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 28%, transparent);
  background: color-mix(in srgb, var(--accent, #4e8cff) 8%, transparent);
}
.rev-list-item.trashed {
  opacity: 0.65;
}
.rev-list-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.rev-list-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg-strong, inherit);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rev-trashed-pill {
  display: inline-block;
  margin-left: 6px;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 9.5px;
  text-transform: uppercase;
  background: color-mix(in srgb, var(--danger, #f55) 18%, transparent);
  color: var(--danger, #f55);
}
.rev-list-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 10.5px;
  color: var(--muted, #888);
  flex-shrink: 0;
}
.rev-list-status {
  text-transform: uppercase;
  letter-spacing: 0.3px;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 9px;
  background: color-mix(in srgb, var(--fg, #fff) 8%, transparent);
}
.rev-list-status[data-status="active"] {
  background: color-mix(in srgb, var(--success, #4ad27a) 16%, transparent);
  color: var(--success, #4ad27a);
}
.rev-list-status[data-status="done"] {
  background: color-mix(in srgb, var(--accent, #4e8cff) 16%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-list-status[data-status="paused"] {
  background: color-mix(in srgb, var(--warning, #d29c4a) 16%, transparent);
  color: var(--warning, #d29c4a);
}
.rev-list-url {
  font-size: 11px;
  color: var(--muted, #888);
  font-family: "Cascadia Code", Consolas, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rev-list-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.rev-list-counters {
  display: inline-flex;
  gap: 10px;
  font-size: 10.5px;
  color: var(--muted, #888);
  margin-top: 2px;
}
.rev-counter-icon {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  margin-right: 3px;
  text-align: center;
  line-height: 14px;
  font-size: 9px;
  font-weight: 600;
  background: color-mix(in srgb, var(--fg, #fff) 10%, transparent);
}
.rev-detail {
  overflow-y: auto;
  border-radius: 14px;
  background: color-mix(in srgb, var(--panel-2, #252526) 68%, transparent);
  border: 1px solid var(--border, rgba(127, 127, 127, 0.18));
  padding: 18px 20px;
  min-height: 0;
}
.rev-detail-empty,
.rev-detail-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--muted, #888);
}
.rev-detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 14px;
}
.rev-detail-head-left {
  flex: 1 1 220px;
  min-width: 0;
}
.rev-detail-head h3 {
  margin: 0 0 6px;
  font-size: 16px;
  color: var(--fg-strong, inherit);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rev-detail-meta {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11.5px;
  color: var(--muted, #888);
  max-width: 100%;
}
.rev-detail-url {
  font-family: "Cascadia Code", Consolas, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.rev-detail-scope code {
  font-size: 11px;
}
.rev-detail-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.rev-detail-head-right {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  flex: 0 0 auto;
}
.rev-tabs {
  display: flex;
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  gap: 2px;
  margin-bottom: 14px;
  overflow-x: auto;
}
.rev-tabs button {
  padding: 8px 14px;
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--muted, #888);
  font-size: 12px;
  cursor: pointer;
  transition: color 100ms, border-color 100ms;
}
.rev-tabs button:hover {
  color: var(--fg, inherit);
}
.rev-tabs button.active {
  color: var(--accent, #4e8cff);
  border-bottom-color: var(--accent, #4e8cff);
}
.rev-stage-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 12px;
  overflow: hidden;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  border-radius: 9px;
}
.rev-stage-step {
  min-width: 0;
  padding: 8px 10px;
  border-left: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
}
.rev-stage-step:first-child { border-left: 0; }
.rev-stage-step span,
.rev-stage-step small {
  display: block;
  overflow: hidden;
  color: var(--muted, #888);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rev-stage-step strong {
  display: block;
  margin: 2px 0;
  color: var(--muted, #888);
  font-size: 11px;
}
.rev-stage-step[data-tone="done"] strong { color: var(--success, #4ad27a); }
.rev-stage-step[data-tone="progress"] {
  background: color-mix(in srgb, var(--accent, #4e8cff) 8%, transparent);
}
.rev-stage-step[data-tone="progress"] strong { color: var(--accent, #4e8cff); }
.rev-subtabs {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: -2px 0 12px;
  overflow-x: auto;
}
.rev-subtabs button {
  flex-shrink: 0;
  min-height: 28px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--muted, #888);
  font-size: 11px;
  cursor: pointer;
}
.rev-subtabs button:hover {
  background: color-mix(in srgb, var(--fg, #fff) 5%, transparent);
  color: var(--fg, inherit);
}
.rev-subtabs button.active {
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 28%, transparent);
  background: color-mix(in srgb, var(--accent, #4e8cff) 10%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-tab-pane {
  font-size: 12px;
}
.rev-overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.rev-info-card {
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
}
.rev-info-card h4 {
  margin: 0 0 8px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  color: var(--muted, #888);
}
.rev-info-card dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 11.5px;
}
.rev-info-card dt {
  color: var(--muted, #888);
}
.rev-info-card dd {
  margin: 0;
  color: var(--fg, inherit);
  word-break: break-all;
}
.rev-info-card code {
  font-size: 11px;
}
.rev-info-intent {
  grid-column: 1 / -1;
}
.rev-intent-body {
  margin: 0;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.5;
}
.rev-muted {
  color: var(--muted, #888);
  font-size: 12px;
  padding: 14px 0;
}
.rev-skeleton-notice {
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 6%, transparent);
  border: 1px dashed color-mix(in srgb, var(--accent, #4e8cff) 35%, transparent);
  color: var(--muted, #888);
  font-size: 11.5px;
}
.rev-phase-notice {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--warning, #d29c4a) 8%, transparent);
  color: var(--warning, #d29c4a);
  font-size: 11px;
}
.rev-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}
.rev-table th,
.rev-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.15));
}
.rev-table th {
  color: var(--muted, #888);
  font-weight: 500;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.rev-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-finding-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rev-finding-card {
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
}
.rev-finding-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rev-finding-title {
  flex: 1;
  font-size: 12.5px;
  color: var(--fg-strong, inherit);
}
.rev-coverage-tag {
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10.5px;
  padding: 1px 5px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 18%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-confidence {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  padding: 1px 6px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--fg, #fff) 8%, transparent);
}
.rev-confidence[data-conf="confirmed"] {
  background: color-mix(in srgb, var(--success, #4ad27a) 18%, transparent);
  color: var(--success, #4ad27a);
}
.rev-confidence[data-conf="hypothesis"],
.rev-confidence[data-conf="experimental"] {
  background: color-mix(in srgb, var(--warning, #d29c4a) 18%, transparent);
  color: var(--warning, #d29c4a);
}
.rev-reference-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.rev-reference-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}
.rev-reference-list li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.rev-reference-list code {
  overflow-wrap: anywhere;
  font-size: 10.5px;
}

/* Create / export / hook modals (teleported to body — keep z-index below
   Naive UI popup layer ~2000 so NSelect menus render above the dialog). */
.rev-modal-backdrop {
  position: fixed;
  /* Leave space for the custom titlebar (see theme.css `--titlebar-h`). */
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1500;
}
.rev-modal {
  width: 480px;
  max-width: 92vw;
  background: var(--bg, #fff);
  border-radius: 14px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.25));
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.3);
  display: flex;
  flex-direction: column;
  max-height: 92vh;
}
.rev-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.2));
}
.rev-modal-head h3 {
  margin: 0;
  font-size: 14px;
}
.rev-modal-close {
  background: transparent;
  border: 0;
  font-size: 22px;
  line-height: 1;
  color: var(--muted, #888);
  cursor: pointer;
  padding: 0 4px;
}
.rev-modal-body {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
}
.rev-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.rev-field-label {
  font-size: 11.5px;
  color: var(--muted, #888);
}
.rev-modal-intro {
  margin: 0 0 2px;
  color: var(--muted, #888);
  font-size: 12px;
  line-height: 1.5;
}
.rev-option-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rev-option-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  border-radius: 10px;
  background: color-mix(in srgb, var(--panel-2, #252526) 60%, transparent);
  cursor: pointer;
  transition: border-color 120ms ease, background 120ms ease;
}
.rev-option-row:hover {
  border-color: var(--border-strong, rgba(127, 127, 127, 0.4));
}
.rev-option-row :deep(.n-switch) {
  margin-top: 1px;
  flex-shrink: 0;
}
.rev-option-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.rev-option-copy strong {
  color: var(--fg-strong, inherit);
  font-size: 12.5px;
  font-weight: 600;
}
.rev-option-copy small {
  color: var(--muted, #888);
  font-size: 11px;
  line-height: 1.45;
}
.rev-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 0;
  border: 0;
}

/* Hooks tab */
.rev-hooks-toolbar {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
  margin-bottom: 12px;
}
.rev-hooks-recipe-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rev-recipe-select {
  flex: 1;
  max-width: 320px;
}
.rev-recipe-info {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 6%, transparent);
  font-size: 11.5px;
}
.rev-recipe-info strong { font-size: 12.5px; }
.rev-recipe-coverage {
  display: inline-flex;
  gap: 4px;
  margin-left: 8px;
}
.rev-recipe-description {
  margin: 6px 0 0;
  color: var(--muted, #888);
}
.rev-recipe-targets {
  margin-top: 8px;
  font-size: 11px;
}
.rev-recipe-targets summary {
  cursor: pointer;
  color: var(--muted, #888);
  user-select: none;
}
.rev-recipe-targets ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.rev-recipe-targets li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.rev-recipe-targets code {
  font-size: 11px;
  font-family: "Cascadia Code", Consolas, monospace;
}
.rev-tag-inline {
  font-size: 10.5px;
  color: var(--muted, #888);
  font-family: "Cascadia Code", Consolas, monospace;
}
.rev-hooks-live-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.15));
}
.rev-hooks-live-bar .rev-toggle { margin-left: auto; }
.rev-recipe-hint {
  margin-top: 6px;
  font-size: 11.5px;
  color: var(--muted, #888);
}
.rev-hooks-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rev-hook-card {
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  border-radius: 8px;
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
  overflow: hidden;
}
.rev-hook-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  transition: background 100ms;
}
.rev-hook-head:hover {
  background: color-mix(in srgb, var(--fg, #fff) 4%, transparent);
}
.rev-hook-target {
  flex: 1;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rev-hook-hits {
  font-size: 11px;
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.rev-hook-hits strong {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.rev-hook-dropped {
  color: var(--warning, #d29c4a);
  font-size: 10px;
}
.rev-hook-id {
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10.5px;
  color: var(--muted, #888);
}
.rev-hook-body {
  padding: 0 10px 10px;
  border-top: 1px solid var(--border, rgba(127, 127, 127, 0.15));
  background: color-mix(in srgb, var(--fg, #fff) 1%, transparent);
}
.rev-hits-list {
  list-style: none;
  margin: 0;
  padding: 8px 0 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 360px;
  overflow-y: auto;
}
.rev-hit {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border, rgba(127, 127, 127, 0.12));
}
.rev-hit:last-child { border-bottom: 0; }
.rev-hit-time {
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10.5px;
  color: var(--muted, #888);
  min-width: 80px;
}
.rev-hit-kind {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  padding: 1px 5px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-target-pill {
  display: inline-block;
  margin-left: 6px;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 9px;
  text-transform: uppercase;
  background: color-mix(in srgb, var(--accent, #4e8cff) 18%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-platform-pill {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--fg, #fff) 8%, transparent);
  color: var(--muted, #888);
}
.rev-events-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 480px;
  overflow-y: auto;
}
.rev-events-list li {
  display: grid;
  grid-template-columns: 140px max-content 1fr;
  gap: 8px;
  align-items: baseline;
  font-size: 11px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--border, rgba(127, 127, 127, 0.12));
}
.rev-event-time {
  font-family: "Cascadia Code", Consolas, monospace;
  color: var(--muted, #888);
  font-size: 10.5px;
}
.rev-event-payload {
  font-size: 10.5px;
  word-break: break-all;
  white-space: pre-wrap;
}
.rev-hit-summary {
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 11px;
  flex: 1;
  word-break: break-all;
  color: var(--fg, inherit);
}
@media (max-width: 980px) {
  .reverse-panel {
    padding: 18px;
  }
  .rev-body {
    grid-template-columns: minmax(220px, 270px) minmax(0, 1fr);
    gap: 10px;
  }
  .rev-subtitle {
    max-width: 520px;
  }
}
@media (max-width: 760px) {
  .reverse-panel {
    padding: 12px;
    gap: 10px;
  }
  .reverse-panel .page-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .reverse-panel .page-head .row-gap-sm {
    width: 100%;
  }
  .reverse-panel .page-head .row-gap-sm :deep(.n-button) {
    flex: 1;
  }
  .rev-body {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(230px, 42%) minmax(300px, 1fr);
    gap: 10px;
  }
  .rev-list,
  .rev-detail { border-radius: 10px; }
  .rev-detail { padding: 14px; }
  .rev-detail-head-right {
    width: 100%;
    justify-content: flex-start;
  }
  .rev-tabs button {
    flex-shrink: 0;
    min-height: 38px;
    padding-inline: 11px;
  }
  .rev-stage-strip {
    grid-template-columns: 1fr 1fr;
  }
  .rev-stage-step:nth-child(3) {
    border-top: 1px solid var(--border, rgba(127, 127, 127, 0.2));
    border-left: 0;
  }
  .rev-stage-step:nth-child(4) {
    border-top: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  }
  .rev-hooks-recipe-row,
  .rev-hooks-live-bar {
    align-items: stretch;
    flex-direction: column;
  }
  .rev-recipe-select { max-width: none; }
  .rev-hooks-live-bar .rev-toggle { margin-left: 0; }
  .rev-events-list li {
    grid-template-columns: 1fr;
  }
  .rev-modal {
    width: calc(100vw - 20px);
    max-width: none;
    max-height: calc(100vh - var(--titlebar-h, 0px) - 16px);
  }
}
</style>
