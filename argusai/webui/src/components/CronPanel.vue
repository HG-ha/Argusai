<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { NSelect, NSwitch } from 'naive-ui'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import ListToolbar from './list/ListToolbar.vue'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListSelect from './list/ListSelect.vue'
import ListRow from './list/ListRow.vue'
import PageEmpty from './list/PageEmpty.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'

const confirmDialog = useConfirm()
const jobs = ref([])
const loading = ref(false)
const runsJobId = ref('')
const runs = ref([])
const runsLoading = ref(false)
const runsQuery = ref('')
const togglingId = ref('')
const showForm = ref(false)
const editingId = ref('')
const saving = ref(false)
const assistantSessions = ref([])
const sessionsLoading = ref(false)
const webhookSecret = ref('')
const webhookUrlValue = ref('')
const form = reactive(emptyForm())
let refreshTimer = null

const tagOptions = computed(() => {
  const set = new Set()
  for (const j of jobs.value) {
    for (const tag of j.tags || []) {
      if (tag) set.add(tag)
    }
  }
  return [
    { label: t('cron.filterAll'), value: '' },
    ...Array.from(set).map((tg) => ({ label: t('cron.tagValue', { tag: tg }), value: tg })),
  ]
})
const statusOptions = computed(() => [
  { label: t('cron.allStatuses'), value: '' },
  { label: t('settings.cronEnabled'), value: 'enabled' },
  { label: t('settings.cronDisabled'), value: 'disabled' },
])
const scheduleKindOptions = computed(() => [
  { label: t('cron.scheduleInterval'), value: 'interval' },
  { label: t('cron.scheduleCron'), value: 'cron' },
  { label: t('cron.scheduleWebhook'), value: 'webhook' },
  { label: t('cron.scheduleFileChange'), value: 'file_change' },
])
const destinationOptions = computed(() => [
  { label: t('cron.destinationNewSession'), value: 'new_session' },
  { label: t('cron.destinationDedicatedSession'), value: 'dedicated_session' },
  { label: t('cron.destinationExistingSession'), value: 'session' },
])
const sessionOptions = computed(() => {
  const rows = assistantSessions.value.map(item => ({
    label: item.name && item.name !== item.id
      ? `${item.name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  if (form.sessionId && !rows.some(item => item.value === form.sessionId)) {
    rows.unshift({
      label: t('cron.missingSessionOption', { id: form.sessionId }),
      value: form.sessionId,
    })
  }
  return rows
})
const formInvalid = computed(() => (
  !form.name.trim()
  || !form.prompt.trim()
  || (form.scheduleKind === 'cron' && !form.cronExpr.trim())
  || (form.scheduleKind === 'file_change' && !form.watchPath.trim())
  || (form.destination === 'session' && !form.sessionId)
))

// Resolved from the live list so the 10s refresh keeps the open run-history
// modal in sync, and deleting the job elsewhere closes it.
const runsJob = computed(() => jobs.value.find(job => job.id === runsJobId.value) || null)

const filteredRuns = computed(() => {
  const q = runsQuery.value.trim().toLowerCase()
  if (!q) return runs.value
  return runs.value.filter((run) => {
    const hay = [
      formatWhen(run.started_at),
      formatWhen(run.finished_at),
      run.status || '',
      runStatusLabel(run.status),
      run.summary || '',
      run.error || '',
    ].join(' ').toLowerCase()
    return hay.includes(q)
  })
})

const {
  query: cronQuery,
  filters: cronFilters,
  page: cronPage,
  pageSize: cronPageSize,
  total: cronFilteredTotal,
  pageCount: cronPageCount,
  pageItems: cronPageItems,
  selectedCount: cronSelectedCount,
  selectedIds: cronSelectedIds,
  isSelected: cronIsSelected,
  toggle: cronToggle,
  clearSelection: cronClearSelection,
  selectPage: cronSelectPage,
  selectAllFiltered: cronSelectAllFiltered,
} = useClientList(
  computed(() => jobs.value),
  (job, q, f) => {
    const tg = String(f.tag || '')
    const st = String(f.status || '')
    if (tg && !(job.tags || []).includes(tg)) return false
    if (st === 'enabled' && !job.enabled) return false
    if (st === 'disabled' && job.enabled) return false
    if (!q) return true
    const hay = `${job.name || ''} ${job.id || ''} ${(job.tags || []).join(' ')} ${job.destination || ''}`.toLowerCase()
    return hay.includes(q)
  },
)
cronFilters.value = { tag: '', status: '' }
cronPageSize.value = 20

function emptyForm() {
  return {
    id: '',
    name: '',
    prompt: '',
    enabled: true,
    scheduleKind: 'interval',
    everySeconds: 3600,
    cronExpr: '0 9 * * 1',
    webhookDebounceSeconds: 0,
    watchPath: '',
    watchGlob: '*',
    watchRecursive: true,
    watchPollSeconds: 2,
    watchDebounceSeconds: 2,
    destination: 'new_session',
    sessionId: '',
    originSessionId: '',
    tagsText: '',
    maxRuns: 0,
    notifyOnSuccess: true,
    notifyOnChangeOnly: true,
    notifyOnFailure: true,
    reportToOrigin: true,
    context: {},
    deferLimitSeconds: 3600,
    allowHighRisk: true,
    highRiskAck: false,
  }
}

function resetForm() {
  Object.assign(form, emptyForm())
  editingId.value = ''
  showForm.value = false
  webhookSecret.value = ''
  webhookUrlValue.value = ''
}

function openNewForm() {
  Object.assign(form, emptyForm())
  editingId.value = ''
  showForm.value = true
  webhookSecret.value = ''
  webhookUrlValue.value = ''
  void loadAssistantSessions()
}

function editJob(job) {
  const schedule = job.schedule && typeof job.schedule === 'object' ? job.schedule : {}
  Object.assign(form, {
    ...emptyForm(),
    id: job.id || '',
    name: job.name || '',
    prompt: job.prompt || '',
    enabled: job.enabled !== false,
    scheduleKind: ['interval', 'cron', 'webhook', 'file_change'].includes(schedule.kind)
      ? schedule.kind
      : 'interval',
    everySeconds: Number(schedule.every_seconds || 3600),
    cronExpr: schedule.expr || '0 9 * * 1',
    webhookDebounceSeconds: Number(schedule.debounce_seconds || 0),
    watchPath: schedule.path || '',
    watchGlob: schedule.glob || '*',
    watchRecursive: schedule.recursive !== false,
    watchPollSeconds: Number(schedule.poll_seconds || 2),
    watchDebounceSeconds: Number(schedule.debounce_seconds || 2),
    destination: ['session', 'dedicated_session'].includes(job.destination)
      ? job.destination
      : 'new_session',
    sessionId: job.session_id || '',
    originSessionId: job.origin_session_id || '',
    tagsText: (job.tags || []).join(', '),
    maxRuns: Number(job.max_runs || 0),
    notifyOnSuccess: job.notify?.on_success !== false,
    notifyOnChangeOnly: job.notify?.on_change_only !== false,
    notifyOnFailure: job.notify?.on_failure !== false,
    reportToOrigin: job.notify?.report_to_origin !== false,
    context: job.context && typeof job.context === 'object'
      ? JSON.parse(JSON.stringify(job.context))
      : {},
    deferLimitSeconds: Number(job.defer_limit_seconds || 3600),
    allowHighRisk: Boolean(job.approval?.high_risk_ack),
    highRiskAck: Boolean(job.approval?.high_risk_ack),
  })
  editingId.value = job.id || ''
  webhookSecret.value = ''
  webhookUrlValue.value = job.webhook?.url || ''
  showForm.value = true
  void loadAssistantSessions()
}

async function loadAssistantSessions() {
  if (sessionsLoading.value) return
  sessionsLoading.value = true
  try {
    const result = await api.listAssistantSessions()
    assistantSessions.value = Array.isArray(result)
      ? result
      : (result?.sessions || [])
  } catch (_) {
    assistantSessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

function tagsFromText(value) {
  return String(value || '').split(',').map(tag => tag.trim()).filter(Boolean)
}

function jobPayload() {
  let schedule
  if (form.scheduleKind === 'cron') {
    schedule = { kind: 'cron', expr: form.cronExpr.trim() }
  } else if (form.scheduleKind === 'webhook') {
    schedule = {
      kind: 'webhook',
      debounce_seconds: Math.max(0, Number(form.webhookDebounceSeconds || 0)),
    }
  } else if (form.scheduleKind === 'file_change') {
    schedule = {
      kind: 'file_change',
      path: form.watchPath.trim(),
      glob: form.watchGlob.trim() || '*',
      recursive: Boolean(form.watchRecursive),
      poll_seconds: Math.max(1, Number(form.watchPollSeconds || 2)),
      debounce_seconds: Math.max(0, Number(form.watchDebounceSeconds || 0)),
    }
  } else {
    schedule = {
      kind: 'interval',
      every_seconds: Number(form.everySeconds || 3600),
    }
  }
  return {
    id: form.id || undefined,
    name: form.name.trim(),
    prompt: form.prompt.trim(),
    enabled: Boolean(form.enabled),
    destination: form.destination,
    // For new-session jobs the backend stores the latest run's session id here;
    // preserving it on edit keeps the existing "open session" recovery path.
    session_id: String(form.sessionId || '').trim(),
    origin_session_id: form.originSessionId,
    schedule,
    tags: tagsFromText(form.tagsText),
    max_runs: Math.max(0, Number(form.maxRuns || 0)),
    notify: {
      on_success: Boolean(form.notifyOnSuccess),
      on_change_only: Boolean(form.notifyOnChangeOnly),
      on_failure: Boolean(form.notifyOnFailure),
      report_to_origin: Boolean(form.reportToOrigin),
    },
    context: form.context,
    defer_limit_seconds: Math.max(1, Number(form.deferLimitSeconds || 3600)),
    approval: {
      mode: 'unattended_full',
      high_risk_ack: Boolean(form.allowHighRisk && form.highRiskAck),
    },
    reschedule: true,
  }
}

async function reload(silent = false) {
  if (!silent) loading.value = true
  try {
    const res = await api.listAssistantCronJobs()
    if (!res || res.ok === false) throw new Error(res?.error || t('cron.loadFailed'))
    jobs.value = Array.isArray(res) ? res : res.jobs || []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    if (!silent) loading.value = false
  }
}

async function saveJob() {
  if (!form.name.trim() || !form.prompt.trim()) return
  if (form.scheduleKind === 'file_change' && !form.watchPath.trim()) return
  if (form.destination === 'session' && !form.sessionId) return
  if (form.allowHighRisk && !form.highRiskAck) {
    const approved = await confirmDialog({
      title: t('cron.highRiskConfirmTitle'),
      content: t('cron.highRiskConfirmContent'),
      positiveText: t('cron.highRiskConfirmAction'),
      negativeText: t('common.cancel'),
      type: 'warning',
    })
    if (!approved) return
    form.highRiskAck = true
  }
  saving.value = true
  try {
    const res = await api.saveAssistantCronJob(jobPayload())
    if (!res || res.ok === false) throw new Error(res?.error || t('cron.saveFailed'))
    if (res.warning) showToast(String(res.warning), 'error')
    const wasEditing = Boolean(editingId.value)
    showToast(t(wasEditing ? 'cron.updated' : 'cron.created'), 'ok')
    if (res.webhook?.url) webhookUrlValue.value = res.webhook.url
    if (res.webhook?.signing_secret) webhookSecret.value = res.webhook.signing_secret
    if (res.webhook?.secret_created) {
      editingId.value = res.job?.id || editingId.value
      form.id = res.job?.id || form.id
    } else {
      resetForm()
    }
    await reload(true)
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    saving.value = false
  }
}

async function chooseWatchPath(directory = true) {
  try {
    const result = await api.chooseAssistantCronWatchPath(form.watchPath, directory)
    if (result?.ok && result.path) form.watchPath = result.path
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function rotateWebhookSecret() {
  if (!form.id) return
  const ok = await confirmDialog({
    title: t('cron.rotateSecretTitle'),
    content: t('cron.rotateSecretContent'),
    positiveText: t('cron.rotateSecret'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  try {
    const result = await api.rotateAssistantCronWebhookSecret(form.id)
    if (!result?.ok) throw new Error(result?.error || t('cron.rotateSecretFailed'))
    webhookUrlValue.value = result.url || webhookUrlValue.value
    webhookSecret.value = result.signing_secret || ''
    showToast(t('cron.secretRotated'), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function copyText(value) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    showToast(t('toast.copied'), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function setEnabled(job, enabled) {
  togglingId.value = job.id
  try {
    if (enabled && !job.approval?.high_risk_ack) {
      const approved = await confirmDialog({
        title: t('cron.highRiskConfirmTitle'),
        content: t('cron.highRiskConfirmContent'),
        positiveText: t('cron.highRiskConfirmAction'),
        negativeText: t('common.cancel'),
        type: 'warning',
      })
      if (!approved) return
      const grant = await api.grantAssistantCronHighRisk(
        job.id,
        true,
        'gui',
        job.origin_session_id || '',
      )
      if (!grant?.ok) throw new Error(grant?.error || t('cron.highRiskGrantFailed'))
      job.approval = grant.approval
    }
    const res = await api.setAssistantCronEnabled(job.id, enabled)
    if (!res || res.ok === false) throw new Error(res?.error || t('cron.updateStatusFailed'))
    Object.assign(job, res.job || { enabled })
    showToast(enabled ? t('settings.cronEnabled') : t('settings.cronDisabled'))
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    togglingId.value = ''
  }
}

async function removeJob(job) {
  const ok = await confirmDialog({
    title: t('cron.deleteTitle'),
    content: t('cron.deleteContent', { name: job.name || job.id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    const res = await api.deleteAssistantCronJob(job.id)
    if (!res || res.ok === false) throw new Error(res?.error || t('cron.deleteFailed'))
    showToast(t('cron.deleted'), 'ok')
    cronToggle(job.id, false)
    if (editingId.value === job.id) resetForm()
    if (runsJobId.value === job.id) closeRuns()
    await reload(true)
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function batchSetEnabled(enabled) {
  const ids = Array.from(cronSelectedIds.value)
  if (!ids.length) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.setAssistantCronEnabled(id, enabled)
      if (!res || res.ok === false) throw new Error(res?.error || t('cron.updateStatusFailed'))
      okN++
    } catch {
      fail++
    }
  }
  cronClearSelection()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
  await reload(true)
}

async function batchDeleteJobs() {
  const ids = Array.from(cronSelectedIds.value)
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('cron.batchDeleteTitle'),
    content: t('cron.batchDeleteContent', { count: ids.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.deleteAssistantCronJob(id)
      if (!res || res.ok === false) throw new Error(res?.error || t('cron.deleteFailed'))
      okN++
      if (editingId.value === id) resetForm()
      if (runsJobId.value === id) closeRuns()
    } catch {
      fail++
    }
  }
  cronClearSelection()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
  await reload(true)
}

async function openRuns(job) {
  runsJobId.value = job.id
  runsQuery.value = ''
  runs.value = []
  runsLoading.value = true
  try {
    const res = await api.listAssistantCronRuns(job.id, 50)
    if (!res || res.ok === false) throw new Error(res?.error || t('cron.loadRunsFailed'))
    runs.value = Array.isArray(res) ? res : res.runs || []
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    runsLoading.value = false
  }
}

function closeRuns() {
  runsJobId.value = ''
  runsQuery.value = ''
  runs.value = []
}

function openSession(sessionId) {
  if (!sessionId) return
  // There is no `/session/:id` route; the assistant is a global overlay that
  // hydrates a session tab from this event (see App.vue).
  window.dispatchEvent(new CustomEvent('argusai:open-assistant-session', {
    detail: { sessionId },
  }))
}

function openRunSession(sessionId) {
  if (!sessionId) return
  // The assistant overlay sits below `.modal-overlay`, so the dialog has to go.
  closeRuns()
  openSession(sessionId)
}

function scheduleLabel(job) {
  const schedule = job.schedule || {}
  if (schedule.kind === 'cron') return `cron ${schedule.expr || t('cron.unknown')}`
  if (schedule.kind === 'webhook') return t('cron.scheduleWebhook')
  if (schedule.kind === 'file_change') {
    return t('cron.fileChangeValue', {
      path: schedule.path || t('cron.notDetected'),
      glob: schedule.glob || '*',
    })
  }
  const seconds = Number(schedule.every_seconds)
  if (!Number.isFinite(seconds) || seconds <= 0) return t('cron.notDetected')
  if (seconds % 86400 === 0) return t('cron.everyDays', { n: seconds / 86400 })
  if (seconds % 3600 === 0) return t('cron.everyHours', { n: seconds / 3600 })
  if (seconds % 60 === 0) return t('cron.everyMinutes', { n: seconds / 60 })
  return t('cron.everySeconds', { n: seconds })
}

function formatWhen(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch {
    return String(ts)
  }
}

function destinationLabel(job) {
  if (job.destination === 'session') {
    return t('cron.existingSessionValue', { id: job.session_id || 'default' })
  }
  if (job.destination === 'dedicated_session') {
    return job.session_id
      ? t('cron.dedicatedSessionValue', { id: job.session_id })
      : t('cron.destinationDedicatedSession')
  }
  return t('cron.destinationNewSession')
}

// Collapses schedule / destination / tags into the single scannable meta line
// the list rows use, so row height stays constant as the job count grows.
function cronSummaryLine(job) {
  const parts = [scheduleLabel(job), t('cron.destinationValue', { destination: destinationLabel(job) })]
  const tags = (job.tags || []).filter(Boolean)
  if (tags.length) parts.push(t('cron.tagsValue', { tags: tags.join(' / ') }))
  return parts.join(' · ')
}

function runStatusLabel(status) {
  const map = {
    running: 'statusRunning',
    completed: 'statusCompleted',
    failed: 'statusFailed',
    deferred: 'statusDeferred',
    skipped_busy: 'statusSkippedBusy',
    waiting_user: 'statusWaitingUser',
    waiting_confirmation: 'statusWaitingConfirmation',
    cancelled: 'statusCancelled',
    blocked: 'statusBlocked',
    queued: 'statusQueued',
    watch_error: 'statusWatchError',
  }
  return map[status] ? t(`cron.${map[status]}`) : (status || t('cron.unknown'))
}

function runStatusClass(status) {
  if (status === 'completed') return 'ok'
  if (['failed', 'cancelled', 'blocked', 'watch_error'].includes(status)) return 'bad'
  if (['deferred', 'skipped_busy', 'waiting_user', 'waiting_confirmation', 'queued'].includes(status)) return 'warn'
  return ''
}

function runDuration(run) {
  const start = Number(run.started_at)
  const finish = Number(run.finished_at)
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return ''
  const seconds = Math.round(finish - start)
  return t('cron.runDurationSeconds', { n: seconds })
}

function disabledReason(job) {
  if (!job.disabled_reason || job.disabled_reason === 'disabled_by_user') return ''
  if (job.disabled_reason === 'max_runs_reached') return t('cron.maxRunsReached')
  return job.disabled_reason
}

function onKeydown(event) {
  if (event.key !== 'Escape') return
  if (runsJobId.value) {
    closeRuns()
    return
  }
  if (showForm.value && !saving.value) resetForm()
}

onMounted(() => {
  reload()
  refreshTimer = window.setInterval(() => {
    if (document.visibilityState === 'hidden') return
    reload(true)
  }, 15000)
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="page list-page cron-page">
    <div class="page-head">
      <div>
        <h1>{{ t('cron.title') }}</h1>
        <p>{{ t('cron.hint') }}</p>
      </div>
      <div class="row-gap-sm">
        <button type="button" class="ghost" :disabled="loading" @click="reload(false)">{{ t('settings.cronRefresh') }}</button>
        <button type="button" class="primary" @click="openNewForm">{{ t('cron.newJob') }}</button>
      </div>
    </div>

    <div class="panel cron-advice" style="margin-bottom:10px">
      <div class="hint">{{ t('cron.runtimeAdvice') }}</div>
    </div>

    <ListToolbar v-model="cronQuery" :placeholder="t('common.search')">
      <template #filters>
        <ListSelect
          v-model="cronFilters.tag"
          :options="tagOptions"
          :placeholder="t('cron.filterTag')"
          min-width="140px"
        />
        <ListSelect
          v-model="cronFilters.status"
          :options="statusOptions"
          min-width="126px"
        />
      </template>
    </ListToolbar>

    <ListBatchBar
      :count="cronSelectedCount"
      :total="cronFilteredTotal"
      @select-page="cronSelectPage(true)"
      @select-all="cronSelectAllFiltered(true)"
      @clear="cronClearSelection()"
    >
      <button type="button" class="ghost list-batch-btn" @click="batchSetEnabled(true)">{{ t('settings.cronEnabled') }}</button>
      <button type="button" class="ghost list-batch-btn" @click="batchSetEnabled(false)">{{ t('settings.cronDisabled') }}</button>
      <button type="button" class="danger list-batch-btn" @click="batchDeleteJobs">{{ t('common.delete') }}</button>
    </ListBatchBar>

    <PageEmpty
      v-if="!cronFilteredTotal && !loading"
      :title="jobs.length ? t('common.noMatch') : t('settings.cronEmpty')"
      :description="jobs.length ? t('common.noMatchHint') : t('cron.emptyDescription')"
      :action-label="jobs.length ? '' : t('cron.newJob')"
      @action="openNewForm"
    />

    <template v-else>
      <div class="list-body cron-job-list">
        <ListRow
          v-for="job in cronPageItems"
          :key="job.id"
          class="cron-job-card"
          :class="{ off: !job.enabled }"
          :selected="cronIsSelected(job.id)"
          :checked="cronIsSelected(job.id)"
          :aria-label="t('cron.selectAria', { name: job.name || job.id })"
          @update:checked="cronToggle(job.id, $event)"
        >
          <div class="cron-job-main">
            <div class="cron-job-head">
              <div class="cron-job-heading">
                <div class="cron-job-title" :title="job.prompt">{{ job.name || job.id }}</div>
                <div class="cron-job-line mono" :title="cronSummaryLine(job)">{{ cronSummaryLine(job) }}</div>
              </div>
              <NSwitch
                size="small"
                class="cron-switch"
                :value="Boolean(job.enabled)"
                :loading="togglingId === job.id"
                :aria-label="t('cron.toggleEnabled')"
                @click.stop
                @update:value="setEnabled(job, $event)"
              />
              <details class="cron-more" @click.stop>
                <summary :aria-label="t('cron.moreActions')">
                  <svg viewBox="0 0 16 4" width="14" height="4" aria-hidden="true">
                    <circle cx="2" cy="2" r="1.6" /><circle cx="8" cy="2" r="1.6" /><circle cx="14" cy="2" r="1.6" />
                  </svg>
                </summary>
                <div class="cron-more-menu" role="menu">
                  <button type="button" role="menuitem" @click="editJob(job)">{{ t('cron.editJob') }}</button>
                  <button type="button" role="menuitem" class="danger" @click="removeJob(job)">
                    {{ t('common.delete') }}
                  </button>
                </div>
              </details>
            </div>

            <div class="cron-meta">
              <span v-if="job.last_status" :class="['cron-run-status', runStatusClass(job.last_status)]">
                {{ runStatusLabel(job.last_status) }}
              </span>
              <span v-if="job.enabled && job.next_run_at" class="mono">
                {{ t('cron.nextRunAt', { when: formatWhen(job.next_run_at) }) }}
              </span>
              <span class="mono">
                {{ job.max_runs
                  ? t('cron.runLimit', { current: job.run_count || 0, max: job.max_runs })
                  : t('cron.runCount', { n: job.run_count || 0 }) }}
              </span>
              <button type="button" class="ghost sm cron-runs-btn" @click.stop="openRuns(job)">
                {{ t('settings.cronShowRuns') }}
              </button>
            </div>

            <div
              v-if="job.pending_fire || disabledReason(job) || !job.approval?.high_risk_ack"
              class="cron-notice"
              :class="{ warn: job.pending_fire || !job.approval?.high_risk_ack }"
            >
              {{ job.pending_fire
                ? t('cron.pendingBusy')
                : disabledReason(job) || t('cron.highRiskGrantMissing') }}
            </div>
          </div>
        </ListRow>
      </div>
      <ListPager
        v-model:page="cronPage"
        v-model:page-size="cronPageSize"
        :page-count="cronPageCount"
        :total="cronFilteredTotal"
      />
    </template>

    <WorkbenchModal
      :open="showForm"
      size="lg"
      :title="t(editingId ? 'cron.editJob' : 'cron.newJob')"
      :description="t('cron.formDescription')"
      :busy="saving"
      :close-label="t('common.close')"
      @close="resetForm"
    >
      <div class="cron-form-body form">
          <div class="row">
            <div class="cron-label-row">
              <label>{{ t('cron.nameLabel') }}</label>
              <label class="cron-check"><input v-model="form.enabled" type="checkbox" /> {{ t('cron.enableAfterSave') }}</label>
            </div>
            <input v-model="form.name" type="text" :placeholder="t('cron.namePlaceholder')" />
          </div>

          <div class="row">
            <label>{{ t('cron.promptLabel') }}</label>
            <textarea v-model="form.prompt" rows="5" :placeholder="t('cron.promptPlaceholder')"></textarea>
          </div>

          <div class="row split cron-schedule-row">
            <div>
              <label>{{ t('cron.scheduleType') }}</label>
              <NSelect
                v-model:value="form.scheduleKind"
                size="medium"
                filterable
                :options="scheduleKindOptions"
              />
            </div>
            <div v-if="form.scheduleKind === 'interval'">
              <label>{{ t('cron.intervalSeconds') }}</label>
              <input v-model.number="form.everySeconds" type="number" min="1" max="2592000" />
            </div>
            <div v-else-if="form.scheduleKind === 'cron'">
              <label>{{ t('cron.fiveFieldCron') }}</label>
              <input v-model="form.cronExpr" type="text" placeholder="0 9 * * 1" />
            </div>
            <div v-else-if="form.scheduleKind === 'webhook'">
              <label>{{ t('cron.debounceSeconds') }}</label>
              <input v-model.number="form.webhookDebounceSeconds" type="number" min="0" max="3600" />
            </div>
            <div v-else>
              <label>{{ t('cron.watchDebounce') }}</label>
              <input v-model.number="form.watchDebounceSeconds" type="number" min="0" max="3600" />
            </div>
          </div>
          <p class="cron-form-hint">
            {{ t(
              form.scheduleKind === 'cron'
                ? 'cron.cronFormatHint'
                : form.scheduleKind === 'webhook'
                  ? 'cron.webhookHint'
                  : form.scheduleKind === 'file_change'
                    ? 'cron.fileChangeHint'
                    : 'cron.intervalRangeHint'
            ) }}
          </p>

          <template v-if="form.scheduleKind === 'file_change'">
            <div class="row">
              <label>{{ t('cron.watchPath') }}</label>
              <div class="cron-path-picker">
                <input v-model="form.watchPath" type="text" :placeholder="t('cron.watchPathPlaceholder')" />
                <button type="button" class="ghost" @click="chooseWatchPath(true)">{{ t('cron.chooseDirectory') }}</button>
                <button type="button" class="ghost" @click="chooseWatchPath(false)">{{ t('cron.chooseFile') }}</button>
              </div>
            </div>
            <div class="row split">
              <div>
                <label>{{ t('cron.watchGlob') }}</label>
                <input v-model="form.watchGlob" type="text" placeholder="*.json" />
              </div>
              <div>
                <label>{{ t('cron.watchPoll') }}</label>
                <input v-model.number="form.watchPollSeconds" type="number" min="1" max="3600" />
              </div>
            </div>
            <label class="cron-check cron-standalone-check">
              <input v-model="form.watchRecursive" type="checkbox" />
              {{ t('cron.watchRecursive') }}
            </label>
          </template>

          <div v-if="form.scheduleKind === 'webhook'" class="cron-webhook-box">
            <div class="cron-webhook-line">
              <span>{{ t('cron.webhookUrl') }}</span>
              <code>{{ webhookUrlValue || t('cron.webhookUrlAfterSave') }}</code>
              <button v-if="webhookUrlValue" type="button" class="ghost sm" @click="copyText(webhookUrlValue)">{{ t('common.copy') }}</button>
            </div>
            <div class="cron-webhook-line">
              <span>{{ t('cron.signingSecret') }}</span>
              <code>{{ webhookSecret || t('cron.secretHidden') }}</code>
              <button v-if="webhookSecret" type="button" class="ghost sm" @click="copyText(webhookSecret)">{{ t('common.copy') }}</button>
              <button v-if="form.id" type="button" class="ghost sm" @click="rotateWebhookSecret">{{ t('cron.rotateSecret') }}</button>
            </div>
            <p class="cron-form-hint">{{ t('cron.webhookSigningHint') }}</p>
            <code class="cron-webhook-example">{{ t('cron.webhookSigningExample') }}</code>
          </div>

          <div class="row split">
            <div>
              <label>{{ t('cron.destination') }}</label>
              <NSelect
                v-model:value="form.destination"
                size="medium"
                :options="destinationOptions"
              />
            </div>
            <div>
              <label>{{ form.destination === 'dedicated_session' ? t('cron.dedicatedSession') : t('cron.sessionId') }}</label>
              <NSelect
                v-if="form.destination !== 'dedicated_session'"
                v-model:value="form.sessionId"
                size="medium"
                filterable
                clearable
                :disabled="form.destination !== 'session'"
                :loading="sessionsLoading"
                :options="sessionOptions"
                :placeholder="t('cron.chooseSession')"
              />
              <input
                v-else
                :value="form.sessionId || t('cron.dedicatedSessionPending')"
                type="text"
                disabled
              />
              <p v-if="form.destination === 'dedicated_session'" class="cron-form-hint">
                {{ t('cron.dedicatedSessionHint') }}
              </p>
            </div>
          </div>

          <div class="row split">
            <div>
              <label>{{ t('cron.tagsLabel') }}</label>
              <input v-model="form.tagsText" type="text" placeholder="recon, defense" />
            </div>
            <div>
              <label>{{ t('cron.maxRunsLabel') }}</label>
              <input v-model.number="form.maxRuns" type="number" min="0" max="1000000" />
              <p class="cron-form-hint">{{ t('cron.maxRunsHint') }}</p>
            </div>
          </div>

          <fieldset class="cron-notify">
            <legend>{{ t('cron.notifications') }}</legend>
            <label class="cron-check"><input v-model="form.notifyOnSuccess" type="checkbox" /> {{ t('cron.notifySuccess') }}</label>
            <label class="cron-check"><input v-model="form.notifyOnFailure" type="checkbox" /> {{ t('cron.notifyFailure') }}</label>
            <label class="cron-check"><input v-model="form.notifyOnChangeOnly" type="checkbox" /> {{ t('cron.notifyChangesOnly') }}</label>
            <label class="cron-check"><input v-model="form.reportToOrigin" type="checkbox" /> {{ t('cron.reportToOrigin') }}</label>
          </fieldset>
          <fieldset class="cron-approval">
            <legend>{{ t('cron.unattendedApproval') }}</legend>
            <label class="cron-check">
              <input
                v-model="form.allowHighRisk"
                type="checkbox"
                @change="form.highRiskAck = form.allowHighRisk ? form.highRiskAck : false"
              />
              {{ t('cron.allowHighRisk') }}
            </label>
            <p class="cron-form-hint">{{ t('cron.unattendedApprovalHint') }}</p>
          </fieldset>
      </div>
      <template #footer>
        <div class="cron-form-actions">
          <button type="button" class="ghost" :disabled="saving" @click="resetForm">{{ t('common.cancel') }}</button>
          <button
            type="button"
            class="primary"
            :disabled="saving || formInvalid"
            @click="saveJob"
          >{{ saving ? t('common.loading') : t(editingId ? 'cron.saveChanges' : 'cron.createJob') }}</button>
        </div>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="Boolean(runsJob)"
      size="lg"
      :title="t('cron.runsTitle')"
      :description="runsJob?.name || runsJob?.id || ''"
      :close-label="t('common.close')"
      @close="closeRuns"
    >
      <div class="cron-runs-search">
          <input
            v-model="runsQuery"
            type="search"
            class="cron-runs-search-input"
            :placeholder="t('cron.runsSearchPlaceholder')"
            :aria-label="t('cron.runsSearchPlaceholder')"
          />
          <span v-if="runs.length" class="cron-runs-count mono">
            {{ t('cron.runsCount', { shown: filteredRuns.length, total: runs.length }) }}
          </span>
      </div>

      <div class="cron-runs-body">
          <div v-if="runsLoading" class="hint">{{ t('settings.cronLoadingRuns') }}</div>
          <PageEmpty
            v-else-if="!runs.length"
            class="cron-runs-empty"
            :title="t('settings.cronNoRuns')"
            :description="t('cron.runsEmptyHint')"
          />
          <PageEmpty
            v-else-if="!filteredRuns.length"
            class="cron-runs-empty"
            :title="t('common.noMatch')"
            :description="t('common.noMatchHint')"
          />
          <div v-else class="cron-run-list">
            <article
              v-for="run in filteredRuns"
              :key="run.run_id || run.started_at"
              class="cron-run-item"
            >
              <div class="cron-run-head">
                <span class="mono cron-run-time">{{ formatWhen(run.started_at) }}</span>
                <span :class="['cron-run-status', runStatusClass(run.status)]">{{ runStatusLabel(run.status) }}</span>
                <span v-if="runDuration(run)" class="mono cron-run-dur">{{ runDuration(run) }}</span>
                <button
                  v-if="run.session_id"
                  type="button"
                  class="ghost sm cron-run-open"
                  @click="openRunSession(run.session_id)"
                >{{ t('cron.openSession') }}</button>
              </div>
              <p v-if="run.summary || run.error" class="cron-run-text" :class="{ bad: !run.summary && run.error }">
                {{ run.summary || run.error }}
              </p>
            </article>
          </div>
      </div>
      <template #footer>
        <div class="modal-actions">
          <button type="button" class="ghost" @click="closeRuns">{{ t('common.close') }}</button>
        </div>
      </template>
    </WorkbenchModal>
  </div>
</template>

<style scoped>
.cron-page { width: 100%; max-width: none; }
.cron-advice {
  border: 1px solid var(--border);
  background: var(--panel-2);
  padding: 8px 12px;
  border-radius: 10px;
}
.cron-job-list { gap: 10px; }
.cron-job-card { align-items: start; padding: 14px 16px; }
.cron-job-main { min-width: 0; }
.cron-job-head {
  display: flex; align-items: flex-start; gap: 10px;
}
.cron-job-heading { min-width: 0; flex: 1 1 auto; }
.cron-job-title {
  overflow: hidden; color: var(--fg-strong); font-size: 14px; font-weight: 650;
  text-overflow: ellipsis; white-space: nowrap;
}
.cron-job-line {
  overflow: hidden; margin-top: 4px; color: var(--muted); font-size: 11px;
  text-overflow: ellipsis; white-space: nowrap;
}
.cron-job-card.off .cron-job-title { color: var(--muted); }
.cron-notice {
  margin-top: 10px; padding: 6px 8px; border-radius: 7px;
  color: var(--muted); background: var(--panel-2); font-size: 11px;
}
.cron-notice.warn { color: var(--warn); background: color-mix(in srgb, var(--warn) 9%, transparent); }
.cron-meta {
  display: flex; align-items: center; gap: 10px; row-gap: 6px; flex-wrap: wrap; margin-top: 10px;
}
.cron-runs-btn { margin-left: auto; }
.cron-switch { flex: 0 0 auto; margin-top: 2px; }
.cron-more { position: relative; flex: 0 0 auto; }
.cron-more > summary {
  display: flex; width: 26px; height: 26px; align-items: center; justify-content: center;
  border-radius: 7px; color: var(--muted); cursor: pointer; list-style: none;
}
.cron-more > summary svg { fill: currentColor; }
.cron-more > summary::-webkit-details-marker { display: none; }
.cron-more > summary:hover,
.cron-more[open] > summary { background: var(--panel-hover); color: var(--fg); }
.cron-more > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.cron-more-menu {
  position: absolute; z-index: 20; top: calc(100% + 4px); right: 0;
  display: grid; min-width: 118px; padding: 5px;
  border: 1px solid var(--border-strong); border-radius: 9px;
  background: var(--card-bg); box-shadow: 0 10px 28px rgba(0, 0, 0, .18);
}
.cron-more-menu button {
  min-height: 30px; padding: 0 8px; border: 0; border-radius: 6px;
  background: transparent; color: var(--fg); font: inherit; font-size: 11px;
  text-align: left; cursor: pointer;
}
.cron-more-menu button:hover { background: var(--panel-hover); }
.cron-more-menu button.danger { color: var(--err); }
.cron-run-status {
  padding: 3px 7px; border-radius: 6px; color: var(--muted); font-weight: 600;
  background: var(--panel-2); font-size: 10.5px;
}
.cron-run-status.ok { color: var(--ok); background: color-mix(in srgb, var(--ok) 10%, transparent); }
.cron-run-status.warn { color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, transparent); }
.cron-run-status.bad { color: var(--err); background: color-mix(in srgb, var(--err) 10%, transparent); }
.cron-runs-modal {
  width: min(680px, 94vw);
  max-height: min(820px, calc(100dvh - var(--titlebar-h, 0px) - 40px));
  display: flex; flex-direction: column; overflow: hidden; margin: 0; padding: 0;
}
.cron-runs-modal .panel-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  flex: 0 0 auto; margin: 0; padding: 16px 20px 13px; border-bottom: 1px solid var(--border);
}
.cron-runs-heading h2 {
  margin: 0; color: var(--fg-strong); font-size: 16px; font-weight: 600;
  text-transform: none; letter-spacing: normal;
}
.cron-runs-heading p {
  overflow: hidden; margin: 3px 0 0; color: var(--muted); font-size: 11px;
  text-overflow: ellipsis; white-space: nowrap;
}
.modal-close-btn {
  padding: 4px 8px; border: none; border-radius: 6px;
  background: transparent; color: var(--muted); font-size: 16px; cursor: pointer;
}
.modal-close-btn:hover { background: var(--panel-hover); color: var(--fg); }
.cron-runs-search {
  display: flex; align-items: center; gap: 10px; flex: 0 0 auto;
  padding: 10px 20px; border-bottom: 1px solid var(--border); background: var(--panel-2);
}
.cron-runs-search-input { min-width: 0; flex: 1 1 auto; }
.cron-runs-count { flex: 0 0 auto; color: var(--muted); font-size: 10px; }
.cron-runs-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 12px 20px 16px; }
.cron-runs-empty :deep(.page-empty) { padding: 28px 16px; }
.cron-run-list { display: flex; flex-direction: column; gap: 6px; }
.cron-run-item {
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 9px;
  background: var(--panel-2);
}
.cron-run-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cron-run-time { color: var(--fg); font-size: 11px; }
.cron-run-dur { color: var(--muted); font-size: 10px; }
.cron-run-open { margin-left: auto; }
.cron-run-text {
  display: -webkit-box; overflow: hidden; margin: 6px 0 0; color: var(--muted);
  font-size: 11px; line-height: 1.5; word-break: break-word;
  -webkit-box-orient: vertical; -webkit-line-clamp: 4;
}
.cron-run-text.bad { color: var(--err); }
.cron-runs-modal .modal-actions {
  display: flex; justify-content: flex-end; gap: 8px; flex: 0 0 auto;
  padding: 13px 20px 16px; border-top: 1px solid var(--border); background: var(--panel);
}
.modal-overlay {
  position: fixed; top: var(--titlebar-h, 0); right: 0; bottom: 0; left: 0; z-index: 1000;
  display: flex; align-items: center; justify-content: center; padding: 28px 18px;
  background: rgba(0, 0, 0, .6); backdrop-filter: blur(4px);
}
.cron-form-panel {
  width: min(680px, 100%); max-height: min(860px, calc(100dvh - var(--titlebar-h, 0px) - 40px));
  display: flex; flex-direction: column; overflow: hidden; padding: 0;
}
.cron-form-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  padding: 16px 20px 13px; border-bottom: 1px solid var(--border); flex: 0 0 auto;
}
.cron-form-head h2 { margin: 0; color: var(--fg-strong); font-size: 16px; }
.cron-form-head p { margin: 3px 0 0; color: var(--muted); font-size: 11px; }
.cron-modal-close {
  width: 30px; height: 30px; padding: 0; border-color: transparent;
  background: transparent; color: var(--muted);
}
.cron-form-body {
  min-height: 0; flex: 1 1 auto; overflow-y: auto; padding: 16px 20px;
}
.cron-form-body .row { margin-bottom: 12px; }
.cron-form-body .row > label,
.cron-form-body .row > div > label,
.cron-label-row > label:first-child {
  display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px;
}
.cron-form-body textarea { resize: vertical; }
.cron-form-body .row.split {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px;
}
.cron-label-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.cron-check { display: inline-flex !important; align-items: center; gap: 6px; margin: 0 !important; }
.cron-form-hint { margin: -7px 0 12px; color: var(--muted); font-size: 10px; }
.cron-form-body .row .cron-form-hint { margin: 4px 0 0; }
.cron-notify,
.cron-approval {
  display: flex; flex-wrap: wrap; gap: 10px 18px; margin: 2px 0 0;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px;
}
.cron-approval { margin-top: 12px; }
.cron-notify legend,
.cron-approval legend { padding: 0 5px; color: var(--fg-strong); font-size: 11px; font-weight: 600; }
.cron-approval .cron-form-hint { flex: 1 0 100%; margin: 0; }
.cron-standalone-check { margin: -4px 0 12px !important; }
.cron-path-picker {
  display: flex; min-width: 0; align-items: center; gap: 6px;
}
.cron-path-picker input { min-width: 0; flex: 1 1 auto; }
.cron-path-picker button { flex: 0 0 auto; }
.cron-webhook-box {
  display: grid; gap: 8px; margin: 0 0 12px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 10px; background: var(--panel-2);
}
.cron-webhook-line {
  display: grid; grid-template-columns: 76px minmax(0, 1fr) auto auto;
  align-items: center; gap: 6px; color: var(--muted); font-size: 11px;
}
.cron-webhook-line code {
  overflow: hidden; color: var(--fg); text-overflow: ellipsis; white-space: nowrap;
}
.cron-webhook-box .cron-form-hint { margin: 0; }
.cron-webhook-example {
  overflow-x: auto; padding: 7px 8px; border-radius: 7px;
  background: var(--input-bg); color: var(--muted); font-size: 10px;
  white-space: nowrap;
}
.cron-form-actions {
  display: flex; width: 100%; justify-content: flex-end; gap: 8px; flex: 0 0 auto;
}
@media (max-width: 620px) {
  .cron-form-body .row.split { grid-template-columns: 1fr; }
  .modal-overlay { padding: 8px; }
  .cron-form-panel,
  .cron-runs-modal { max-height: calc(100dvh - var(--titlebar-h, 0px) - 16px); }
  .cron-form-actions button { flex: 1 1 auto; }
  .cron-path-picker { flex-wrap: wrap; }
  .cron-path-picker input { flex-basis: 100%; }
  .cron-webhook-line { grid-template-columns: 1fr auto; }
  .cron-webhook-line > span { grid-column: 1 / -1; }
  .cron-runs-search { flex-wrap: wrap; }
  .cron-run-open { margin-left: 0; }
}
</style>
