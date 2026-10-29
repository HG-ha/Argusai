<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { api } from '../api.js'
import { route, navigate } from '../router.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import StatusBadge from './StatusBadge.vue'
import ProgressBar from './ProgressBar.vue'
import ListPager from './list/ListPager.vue'

const confirmDialog = useConfirm()

const SEVERITY_ORDER = { critical: 4, high: 3, medium: 2, low: 1, info: 0 }

const task = ref(null)
const events = ref([])
const sinceIdx = ref(0)
const selectedFindingKey = ref('')
const findingListEl = ref(null)
const loadError = ref('')
let pollTimer = null

const taskId = computed(() => route.params.id)

// Live push: refresh this task the instant the runner reports progress /
// findings / completion, instead of waiting for the 800ms event poll.
useLiveChannel('tasks', (payload) => {
  if (!payload || !payload.task_id || String(payload.task_id) === String(taskId.value)) {
    loadOnce()
    pollEvents()
  }
}, { debounceMs: 200 })

async function loadOnce() {
  const id = taskId.value; if (!id) return
  try {
    const result = await api.getTask(id)
    if (!result) {
      loadError.value = t('detail.notFound', { id })
      return
    }
    task.value = result
    loadError.value = ''
  } catch (e) {
    const msg = e.message || String(e)
    loadError.value = msg
    showToast(t('toast.error', { msg }), 'error')
  }
}

const TERMINAL_TASK_STATUSES = new Set(['done', 'error', 'cancelled', 'failed'])

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function ensurePollingIfActive() {
  const st = String(task.value?.meta?.status || '').toLowerCase()
  if (task.value && !task.value.is_running && TERMINAL_TASK_STATUSES.has(st)) {
    stopPolling()
    return
  }
  if (!pollTimer) {
    pollTimer = setInterval(pollEvents, 800)
  }
}

async function pollEvents() {
  const id = taskId.value; if (!id) return
  if (document.visibilityState === 'hidden') return
  try {
    const resp = await api.getTaskEvents(id, sinceIdx.value)
    if (resp.events?.length) { events.value = events.value.concat(resp.events); sinceIdx.value = resp.total }
    if (resp.status === 'running' || resp.status !== task.value?.meta?.status) await loadOnce()
    if (TERMINAL_TASK_STATUSES.has(String(resp.status || '').toLowerCase()) && !task.value?.is_running) {
      stopPolling()
    }
  } catch (e) { console.warn(e) }
}

async function cancelTask() { await api.cancelTask(taskId.value); showToast(t('toast.taskCancelled')) }
async function deleteTask() {
  const ok = await confirmDialog({
    title: t('tasks.confirmDeleteTitle'),
    content: t('tasks.confirmDelete'),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  await api.deleteTask(taskId.value); showToast(t('toast.taskDeleted')); navigate('/')
}
async function rescanTask() {
  try {
    const r = await api.rescanTask(taskId.value)
    if (!r.ok) { showToast(t('toast.error', { msg: r.error }), 'error'); return }
    showToast(t('toast.taskRescanned')); navigate(`/task/${r.id}`)
  } catch (e) { showToast(t('toast.error', { msg: e.message || e }), 'error') }
}

function openHtml() { const p = task.value?.reports?.html; if (p) api.openReport(p) }
function openMd() { const p = task.value?.reports?.md; if (p) api.openReport(p) }
function openJson() { const p = task.value?.reports?.json; if (p) api.openReport(p) }
function openDir() { const p = task.value?.reports?.dir || task.value?.meta?.dir; if (p) api.openFolder(p) }

function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—' }

const sortedFindings = computed(() => {
  const list = (task.value?.findings || []).slice()
  list.sort((a, b) => ((SEVERITY_ORDER[b.severity] ?? -1) - (SEVERITY_ORDER[a.severity] ?? -1)) || String(a.id || '').localeCompare(String(b.id || '')))
  return list
})

const {
  filters: findingFilters,
  page: findingPage,
  pageSize: findingPageSize,
  total: findingFilteredTotal,
  pageCount: findingPageCount,
  filtered: findingFilteredItems,
  pageItems: findingPageItems,
} = useClientList(
  sortedFindings,
  (f, _q, ftr) => {
    if (ftr.severity && ftr.severity !== 'all' && f.severity !== ftr.severity) return false
    return true
  },
)
findingFilters.value = { severity: 'all' }
findingPageSize.value = 20

const severityCounts = computed(() => {
  const out = { critical: 0, high: 0, medium: 0, low: 0, info: 0 }
  for (const f of task.value?.findings || []) out[f.severity] = (out[f.severity] || 0) + 1
  return out
})

const status = computed(() => task.value?.meta?.status || 'pending')
const isRunning = computed(() => task.value?.is_running)
const progress = computed(() => task.value?.meta?.progress || 0)
const checksDone = computed(() => task.value?.meta?.checks_done || 0)
const checksTotal = computed(() => task.value?.meta?.checks_total || 0)
const currentCheck = computed(() => task.value?.meta?.current_check || '')
const progressLabel = computed(() => {
  if (status.value === 'done') return t('detail.progressDone')
  if (status.value === 'cancelled') return t('detail.progressCancelled')
  if (status.value === 'error') return t('detail.progressError')
  if (!checksTotal.value) return t('detail.progressPending')
  if (currentCheck.value) return t('detail.progressRunning', { n: checksDone.value, total: checksTotal.value, current: currentCheck.value })
  return t('detail.progressCounted', { n: checksDone.value, total: checksTotal.value })
})
const taskScope = computed(() => {
  const values = [task.value?.meta?.params?.scope, task.value?.meta?.scope]
  for (const value of values) {
    const text = Array.isArray(value)
      ? value.filter(Boolean).join(', ')
      : (typeof value === 'string' ? value.trim() : '')
    if (text) return text
  }
  return ''
})
const taskChecksLabel = computed(() => {
  const checks = task.value?.meta?.checks || task.value?.meta?.params?.checks || []
  if (Array.isArray(checks)) return checks.filter(Boolean).join(', ') || '—'
  return typeof checks === 'string' ? checks.trim() || '—' : '—'
})

function findingKey(finding) {
  if (!finding) return ''
  return String(finding.uid || `${finding.id || ''}|${finding.url || ''}|${finding.title || ''}`)
}

const selectedFinding = computed(() =>
  findingFilteredItems.value.find(finding => findingKey(finding) === selectedFindingKey.value) || null,
)
const evidenceText = computed(() => {
  const evidence = selectedFinding.value?.evidence
  if (evidence == null) return ''
  if (typeof evidence === 'string') return evidence
  if (typeof evidence === 'object' && Object.keys(evidence).length === 0) return ''
  try { return JSON.stringify(evidence, null, 2) }
  catch { return String(evidence) }
})

const taskScopeHosts = computed(() => {
  const raw = task.value?.meta?.params?.scope ?? task.value?.meta?.scope
  if (Array.isArray(raw)) return raw.map(v => String(v || '').trim()).filter(Boolean)
  if (typeof raw === 'string' && raw.trim()) return raw.split(',').map(v => v.trim()).filter(Boolean)
  return []
})

function hostOf(url) {
  try { return new URL(url).hostname } catch { return '' }
}

// Derived from the same scope-allowlist semantics as Target.in_scope() (argusai/core/target.py);
// only meaningful when both the finding URL and the task's scope list are real data.
const findingInScope = computed(() => {
  const hosts = taskScopeHosts.value
  const host = hostOf(selectedFinding.value?.url)
  if (!hosts.length || !host) return null
  return hosts.some(h => host === h || host.endsWith(`.${h}`))
})

const evidenceRedacted = computed(() => Boolean(evidenceText.value) && /\[redacted\]/i.test(evidenceText.value))

const showEvidenceNote = computed(() => findingInScope.value !== null || evidenceRedacted.value)

const reproSteps = computed(() => {
  const actions = selectedFinding.value?.next_actions
  if (!Array.isArray(actions)) return []
  return actions.map(a => String(a || '').trim()).filter(Boolean)
})
const findingMetadataEntries = computed(() => {
  const finding = selectedFinding.value
  if (!finding) return []

  const entries = [
    ['ID', finding.id],
    [t('finding.check'), finding.check || finding.category],
    [t('finding.cwe'), finding.cwe],
    ['Confidence', finding.confidence],
    ['Kind', finding.kind],
    ['Domain', finding.domain],
    [t('detail.created'), formatFindingTimestamp(finding.created_at)],
  ]
  const metadata = finding.metadata
  if (metadata && typeof metadata === 'object' && !Array.isArray(metadata)) {
    entries.push(...Object.entries(metadata))
  } else if (metadata != null && metadata !== '') {
    entries.push(['Metadata', metadata])
  }
  return entries
    .filter(([, value]) => value != null && value !== '')
    .map(([label, value]) => ({ label, value: displayValue(value) }))
})

function displayValue(value) {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, null, 2) }
  catch { return String(value) }
}

function formatFindingTimestamp(value) {
  if (value == null || value === '') return ''
  if (typeof value === 'number') return fmtTime(value)
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) {
    return fmtTime(Number(value))
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function selectFinding(finding) {
  selectedFindingKey.value = findingKey(finding)
}

async function focusSelectedFinding() {
  await nextTick()
  const rows = findingListEl.value?.querySelectorAll?.('.finding-row') || []
  for (const row of rows) {
    if (row.dataset.findingKey === selectedFindingKey.value) {
      row.focus()
      break
    }
  }
}

function selectFindingAt(index) {
  const items = findingFilteredItems.value
  if (!items.length) return
  const next = Math.min(items.length - 1, Math.max(0, index))
  selectFinding(items[next])
  findingPage.value = Math.floor(next / findingPageSize.value) + 1
  focusSelectedFinding()
}

function moveFinding(delta) {
  const items = findingFilteredItems.value
  if (!items.length) return
  const current = items.findIndex(finding => findingKey(finding) === selectedFindingKey.value)
  const start = current < 0 ? (delta > 0 ? -1 : 0) : current
  selectFindingAt(start + delta)
}

function onFindingKeydown(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveFinding(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveFinding(-1)
  } else if (event.key === 'Home') {
    event.preventDefault()
    selectFindingAt(0)
  } else if (event.key === 'End') {
    event.preventDefault()
    selectFindingAt(findingFilteredItems.value.length - 1)
  }
}

async function copyEvidence() {
  if (!evidenceText.value) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(evidenceText.value)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = evidenceText.value
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
    }
    showToast(t('toast.copied'), 'ok')
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function levelClass(evt) {
  if (!evt) return ''
  if (evt.type === 'check.error' || evt.type === 'task.error') return 'error'
  if (evt.type === 'finding') return 'finding'
  if (evt.type === 'log') return evt.payload?.level || 'info'
  return 'info'
}

function summarize(evt) {
  const p = evt.payload || {}
  const map = {
    'run.start': () => `▶ ${p.target} [${(p.checks||[]).join(', ')}]`,
    'check.start': () => `… ${p.check}`,
    'check.done': () => `✓ ${p.check} (${p.count} ${t('common.loading')}, ${p.elapsed}s)`,
    'check.error': () => `✗ ${p.check}: ${p.error}`,
    'finding': () => `⚠ [${p.finding?.severity}] ${p.finding?.title}`,
    'log': () => `· ${p.msg || ''}`,
    'reports.ready': () => `■ reports ready`,
    'run.done': () => `■ done (${p.elapsed}s, ${p.total})`,
    'task.start': () => `▶ task start`,
    'task.done': () => `■ task end: ${p.status}`,
    'task.error': () => `✗ task error: ${p.error}`,
  }
  return (map[evt.type] || (() => evt.type))()
}

function fmtEvTs(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`
}

onMounted(async () => {
  await loadOnce()
  await pollEvents()
  ensurePollingIfActive()
})
onUnmounted(() => { stopPolling() })
watch(findingFilteredItems, (items) => {
  if (!items.length) {
    selectedFindingKey.value = ''
    return
  }
  if (!items.some(finding => findingKey(finding) === selectedFindingKey.value)) {
    selectFinding(items[0])
  }
}, { immediate: true })
watch(findingPage, () => {
  if (!findingPageItems.value.length) return
  if (!findingPageItems.value.some(finding => findingKey(finding) === selectedFindingKey.value)) {
    selectFinding(findingPageItems.value[0])
  }
})
watch(taskId, async () => {
  stopPolling()
  task.value = null
  events.value = []
  sinceIdx.value = 0
  loadError.value = ''
  selectedFindingKey.value = ''
  findingFilters.value.severity = 'all'
  findingPage.value = 1
  await loadOnce()
  await pollEvents()
  ensurePollingIfActive()
})
</script>

<template>
  <div class="page task-detail-page">
    <div class="page-head task-head">
      <div>
        <a href="#/" class="link back-link">{{ t('detail.backToList') }}</a>
        <h3>{{ task?.meta?.name || task?.meta?.target_url || taskId }}</h3>
        <p class="mono">{{ taskId }} · {{ t('detail.checks') }} {{ checksDone }}/{{ checksTotal || '—' }}</p>
      </div>
      <div class="row-actions">
        <button class="ghost" type="button" @click="loadOnce">{{ t('common.refresh') }}</button>
        <button v-if="isRunning" class="ghost danger" type="button" @click="cancelTask">{{ t('detail.cancelBtn') }}</button>
        <button class="btn primary" type="button" :disabled="isRunning" @click="rescanTask">{{ t('detail.rescanBtn') }}</button>
        <button class="ghost danger" type="button" @click="deleteTask">{{ t('detail.deleteBtn') }}</button>
      </div>
    </div>
    <div v-if="!task && !loadError" class="empty">{{ t('common.loading') }}</div>
    <div v-else-if="!task && loadError" class="empty task-load-error">
      <strong>{{ t('detail.loadFailed') }}</strong>
      <p>{{ loadError }}</p>
      <div class="row-actions load-error-actions">
        <button type="button" class="ghost" @click="loadOnce">{{ t('common.retry') }}</button>
        <button type="button" class="ghost" @click="navigate('/')">{{ t('detail.backToList') }}</button>
      </div>
    </div>
    <template v-else>
      <section class="task-meta-ribbon" :aria-label="t('detail.metaTitle')">
        <div class="meta-cell">
          <span>{{ t('detail.target') }}</span>
          <strong class="mono" :title="task.meta.target_url">{{ task.meta.target_url || '—' }}</strong>
        </div>
        <div class="meta-cell">
          <span>{{ t('detail.scope') }}</span>
          <strong class="mono" :title="taskScope">{{ taskScope || '—' }}</strong>
        </div>
        <div class="meta-cell">
          <span>{{ t('detail.checks') }}</span>
          <strong class="mono" :title="taskChecksLabel">{{ taskChecksLabel }}</strong>
        </div>
        <div class="meta-cell">
          <span>{{ t('detail.progressTitle') }}</span>
          <div class="meta-progress">
            <strong>{{ checksDone }} / {{ checksTotal || '—' }}</strong>
            <StatusBadge :status="status" />
          </div>
        </div>
        <div class="meta-cell">
          <span>{{ t('detail.started') }}</span>
          <strong :title="fmtTime(task.meta.started_at)">{{ fmtTime(task.meta.started_at) }}</strong>
        </div>
        <div class="meta-cell">
          <span>{{ t('detail.finished') }}</span>
          <strong :title="fmtTime(task.meta.finished_at)">{{ fmtTime(task.meta.finished_at) }}</strong>
        </div>
      </section>

      <ProgressBar
        class="task-progress"
        :value="progress"
        :label="progressLabel"
        :indeterminate="status==='running'&&!progress"
      />

      <div class="master-detail">
        <section class="detail-pane finding-pane" :aria-label="t('detail.findingsTitle')">
          <header class="pane-head">
            <h4>{{ t('detail.findingsTitle') }}</h4>
            <p>{{ t('detail.totalFindings', { n: (task.findings || []).length }) }}</p>
          </header>

          <div class="severity-filters" :aria-label="t('detail.findingsTitle')">
            <button
              type="button"
              class="severity-filter"
              :class="{ selected: findingFilters.severity === 'all' }"
              :aria-pressed="findingFilters.severity === 'all'"
              @click="findingFilters.severity = 'all'"
            >
              {{ t('tasks.filterAll') }} {{ (task.findings || []).length }}
            </button>
            <button
              v-for="severity in ['critical', 'high', 'medium', 'low', 'info']"
              :key="severity"
              type="button"
              class="severity-filter"
              :class="{ selected: findingFilters.severity === severity }"
              :aria-pressed="findingFilters.severity === severity"
              @click="findingFilters.severity = severity"
            >
              <span class="severity-dot" :class="severity" aria-hidden="true"></span>
              {{ t(`severity.${severity}`) }} {{ severityCounts[severity] }}
            </button>
          </div>

          <div v-if="findingFilteredTotal === 0" class="pane-empty">
            {{ t('detail.noFindings') }}
          </div>
          <div
            v-else
            ref="findingListEl"
            class="finding-list"
            role="listbox"
            :aria-label="t('detail.findingsTitle')"
          >
            <article
              v-for="finding in findingPageItems"
              :key="findingKey(finding)"
              class="finding-list-item"
              role="presentation"
            >
              <button
                type="button"
                role="option"
                class="finding-row"
                :class="{ selected: findingKey(finding) === selectedFindingKey }"
                :data-finding-key="findingKey(finding)"
                :aria-selected="findingKey(finding) === selectedFindingKey"
                :aria-label="`${t(`severity.${finding.severity}`)} · ${finding.title}`"
                aria-keyshortcuts="ArrowUp ArrowDown Home End"
                :tabindex="findingKey(finding) === selectedFindingKey ? 0 : -1"
                @click="selectFinding(finding)"
                @keydown="onFindingKeydown"
              >
                <span class="severity-dot" :class="finding.severity" aria-hidden="true"></span>
                <span class="finding-row-copy">
                  <strong>{{ finding.title || finding.id }}</strong>
                  <small class="mono">{{ finding.url || '—' }}</small>
                  <span>{{ finding.check || finding.category || finding.id }}</span>
                </span>
              </button>
            </article>
          </div>

          <ListPager
            v-if="findingFilteredTotal > 0"
            v-model:page="findingPage"
            v-model:page-size="findingPageSize"
            :page-count="findingPageCount"
            :total="findingFilteredTotal"
          />
        </section>

        <section class="detail-pane evidence-pane" :aria-label="t('finding.evidence')">
          <div class="evidence-scroll">
            <template v-if="selectedFinding">
              <header class="evidence-title">
                <div>
                  <h4>{{ selectedFinding.title || selectedFinding.id }}</h4>
                  <p>{{ selectedFinding.category || selectedFinding.check || selectedFinding.id }}</p>
                </div>
                <span class="severity-badge" :class="selectedFinding.severity">
                  {{ t(`severity.${selectedFinding.severity}`) }}
                </span>
              </header>

              <section class="finding-section">
                <h5>{{ t('finding.url') }}</h5>
                <p class="finding-url mono">{{ selectedFinding.url || '—' }}</p>
              </section>

              <section class="finding-section">
                <h5>{{ t('finding.description') }}</h5>
                <p class="detail-copy">{{ selectedFinding.description || '—' }}</p>
              </section>

              <section class="finding-section">
                <div class="section-heading">
                  <h5>{{ t('finding.evidence') }}</h5>
                  <button
                    type="button"
                    class="ghost sm"
                    :disabled="!evidenceText"
                    @click="copyEvidence"
                  >
                    {{ t('common.copy') }} {{ t('finding.evidence') }}
                  </button>
                </div>
                <pre v-if="evidenceText" class="evidence-code">{{ evidenceText }}</pre>
                <p v-else class="detail-copy">—</p>
                <p v-if="showEvidenceNote" class="evidence-note">
                  <span v-if="findingInScope !== null" :class="{ 'evidence-note-warn': !findingInScope }">
                    {{ findingInScope ? t('detail.scopeInLabel') : t('detail.scopeOutLabel') }}
                  </span>
                  <span v-if="evidenceRedacted">{{ t('detail.evidenceRedactedNote') }}</span>
                </p>
              </section>

              <section v-if="reproSteps.length" class="finding-section">
                <h5>{{ t('detail.reproTitle') }}</h5>
                <ol class="repro-steps">
                  <li v-for="(step, index) in reproSteps" :key="index">{{ step }}</li>
                </ol>
              </section>

              <section class="finding-section">
                <h5>{{ t('finding.remediation') }}</h5>
                <p class="remediation-copy">{{ selectedFinding.remediation || '—' }}</p>
              </section>

              <section class="finding-section">
                <h5>Metadata</h5>
                <dl class="metadata-grid">
                  <div v-for="(entry, index) in findingMetadataEntries" :key="`${entry.label}-${index}`" class="metadata-item">
                    <dt>{{ entry.label }}</dt>
                    <dd class="mono">{{ entry.value }}</dd>
                  </div>
                </dl>
              </section>
            </template>
            <div v-else class="pane-empty">
              {{ t('detail.noFindings') }}
            </div>

            <details class="secondary-log">
              <summary>{{ t('detail.eventsTitle') }} · {{ events.length }}</summary>
              <div class="event-log mono">
                <div v-for="(evt, index) in events" :key="index" class="line" :class="levelClass(evt)">
                  <span class="ts">{{ fmtEvTs(evt.ts) }}</span>{{ summarize(evt) }}
                </div>
                <div v-if="events.length === 0" class="line">—</div>
              </div>
            </details>
          </div>

          <footer class="reports-bar" :aria-label="t('detail.reportsTitle')">
            <span>{{ t('detail.reportsTitle') }}</span>
            <div class="row-actions">
              <button class="ghost sm" type="button" :disabled="!task.reports?.html" @click="openHtml">HTML</button>
              <button class="ghost sm" type="button" :disabled="!task.reports?.md" @click="openMd">MD</button>
              <button class="ghost sm" type="button" :disabled="!task.reports?.json" @click="openJson">JSON</button>
              <button class="ghost sm" type="button" @click="openDir">{{ t('common.openFolder') }}</button>
            </div>
          </footer>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.task-detail-page {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.task-head {
  align-items: flex-start;
  flex-shrink: 0;
}

.back-link {
  display: inline-block;
  height: auto;
  margin-bottom: 4px;
  padding: 0;
}

.task-meta-ribbon {
  display: grid;
  grid-template-columns:
    minmax(0, 1.35fr)
    minmax(0, 1.2fr)
    minmax(0, 1.1fr)
    minmax(128px, 0.85fr)
    minmax(126px, 0.9fr)
    minmax(126px, 0.9fr);
  margin-bottom: 10px;
  border-block: 1px solid var(--border);
  flex-shrink: 0;
}

.meta-cell {
  min-width: 0;
  padding: 8px 10px;
  border-left: 1px solid var(--border);
}

.meta-cell:first-child {
  padding-left: 0;
  border-left: 0;
}

.meta-cell > span {
  display: block;
  color: var(--muted-2);
  font-size: 10px;
}

.meta-cell > strong,
.meta-progress > strong {
  display: block;
  min-width: 0;
  margin-top: 2px;
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-progress {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.meta-progress > strong {
  flex: 0 0 auto;
}

.meta-progress :deep(.badge) {
  min-width: 0;
  padding: 2px 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-progress {
  margin-bottom: 12px;
  flex-shrink: 0;
}

.master-detail {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(260px, 32%) minmax(0, 1fr);
  gap: 12px;
}

.detail-pane {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card-bg);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}

.finding-pane,
.evidence-pane {
  display: flex;
  flex-direction: column;
}

.pane-head {
  padding: 11px 12px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 54%, var(--card-bg));
}

.pane-head h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 12px;
}

.pane-head p {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.severity-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 8px 8px 7px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.severity-filter {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 26px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 10px;
}

.severity-filter.selected {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  background: var(--accent-soft);
  color: var(--fg-strong);
}

.severity-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--info);
}

.severity-dot.critical { background: var(--crit); }
.severity-dot.high { background: var(--high); }
.severity-dot.medium { background: var(--med); }
.severity-dot.low { background: var(--low); }
.severity-dot.info { background: var(--info); }

.finding-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 6px;
}

.finding-list-item + .finding-list-item {
  margin-top: 4px;
}

.finding-row {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  gap: 9px;
  width: 100%;
  padding: 9px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--fg);
  text-align: left;
  white-space: normal;
}

.finding-row:hover:not(:disabled) {
  border-color: var(--border);
  background: var(--panel-2);
}

.finding-row.selected {
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
  background: var(--accent-soft);
}

.finding-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.finding-row .severity-dot {
  margin-top: 4px;
}

.finding-row-copy {
  min-width: 0;
}

.finding-row-copy strong,
.finding-row-copy small,
.finding-row-copy > span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.finding-row-copy strong {
  color: var(--fg-strong);
  font-size: 11px;
}

.finding-row-copy small {
  margin-top: 2px;
  color: var(--muted);
  font-size: 10px;
}

.finding-row-copy > span {
  margin-top: 3px;
  color: var(--muted-2);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.pane-empty {
  display: grid;
  flex: 1 1 auto;
  min-height: 140px;
  place-items: center;
  padding: 24px;
  color: var(--muted);
  text-align: center;
}

.finding-pane :deep(.list-pager) {
  margin: 0;
  padding: 9px 10px 10px;
  border-top: 1px solid var(--border);
}

.evidence-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 14px;
}

.evidence-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}

.evidence-title > div {
  min-width: 0;
}

.evidence-title h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 16px;
  line-height: 1.35;
}

.evidence-title p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.severity-badge {
  flex: 0 0 auto;
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--info);
  font-size: 10px;
  font-weight: 700;
}

.severity-badge.critical {
  border-color: color-mix(in srgb, var(--crit) 36%, var(--border));
  background: color-mix(in srgb, var(--crit) 10%, transparent);
  color: var(--crit);
}

.severity-badge.high {
  border-color: color-mix(in srgb, var(--high) 36%, var(--border));
  background: color-mix(in srgb, var(--high) 10%, transparent);
  color: var(--high);
}

.severity-badge.medium {
  border-color: color-mix(in srgb, var(--med) 36%, var(--border));
  background: color-mix(in srgb, var(--med) 10%, transparent);
  color: var(--med);
}

.severity-badge.low {
  border-color: color-mix(in srgb, var(--low) 36%, var(--border));
  background: color-mix(in srgb, var(--low) 10%, transparent);
  color: var(--low);
}

.finding-section {
  padding: 12px 0;
  border-top: 1px solid var(--border);
}

.finding-section:first-of-type {
  margin-top: 12px;
}

.finding-section h5 {
  margin: 0 0 6px;
  color: var(--fg-strong);
  font-size: 11px;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.section-heading h5 {
  margin: 0;
}

.finding-url,
.detail-copy,
.remediation-copy {
  margin: 0;
  color: var(--fg);
  font-size: 11px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.finding-url {
  color: var(--muted);
  word-break: break-all;
}

.evidence-code {
  max-height: 340px;
  margin: 0;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-sunken);
  color: var(--fg);
  font: 11px/1.55 var(--mono);
  white-space: pre-wrap;
  word-break: break-word;
}

.remediation-copy {
  padding: 10px 12px;
  border-left: 3px solid var(--accent);
  border-radius: 7px;
  background: var(--accent-soft);
}

.evidence-note {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.evidence-note-warn {
  color: var(--err);
  font-weight: 600;
}

.repro-steps {
  margin: 0;
  padding-left: 20px;
  color: var(--fg);
  font-size: 11px;
  line-height: 1.65;
}

.repro-steps li + li {
  margin-top: 4px;
}

.metadata-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.metadata-item {
  min-width: 0;
  padding: 8px 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel-2) 62%, transparent);
}

.metadata-item dt {
  color: var(--muted-2);
  font-size: 9px;
}

.metadata-item dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
  color: var(--fg);
  font-size: 10px;
  white-space: pre-wrap;
}

.secondary-log {
  margin-top: 12px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: color-mix(in srgb, var(--panel-2) 48%, transparent);
}

.secondary-log summary {
  padding: 8px 10px;
  color: var(--muted);
  font-size: 11px;
}

.secondary-log[open] summary {
  border-bottom: 1px solid var(--border);
}

.event-log {
  max-height: 220px;
  overflow-y: auto;
  padding: 9px 10px;
  border: 0;
  border-radius: 0;
  background: transparent;
  font-size: 11px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.event-log .line {
  color: var(--fg);
}
.event-log .line .ts {
  color: var(--muted);
  margin-right: 8px;
}

.event-log .line.error { color: var(--err); }
.event-log .line.warn { color: var(--warn); }
.event-log .line.info { color: var(--accent); }
.event-log .line.finding { color: var(--fg-strong); }

.reports-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 42%, var(--card-bg));
}

.reports-bar > span {
  margin-right: auto;
  color: var(--muted);
  font-size: 10px;
  font-weight: 600;
}

@media (max-width: 1200px) {
  .task-meta-ribbon {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .meta-cell:nth-child(4) {
    padding-left: 0;
    border-left: 0;
    border-top: 1px solid var(--border);
  }

  .meta-cell:nth-child(n + 5) {
    border-top: 1px solid var(--border);
  }
}

@media (max-width: 900px) {
  .task-detail-page {
    overflow-y: auto;
  }

  .master-detail {
    flex: 0 0 auto;
    grid-template-columns: minmax(0, 1fr);
    height: auto;
    min-height: 0;
  }

  .finding-pane {
    height: min(440px, 54vh);
    min-height: 320px;
  }

  .evidence-pane {
    min-height: 440px;
  }
}

@media (max-width: 620px) {
  .task-meta-ribbon {
    grid-template-columns: minmax(0, 1fr);
  }

  .meta-cell,
  .meta-cell:first-child,
  .meta-cell:nth-child(4) {
    padding: 8px 0;
    border-left: 0;
    border-top: 1px solid var(--border);
  }

  .meta-cell:first-child {
    border-top: 0;
  }

  .metadata-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .evidence-scroll {
    padding: 12px;
  }

  .reports-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .reports-bar .row-actions {
    width: 100%;
    justify-content: flex-start;
  }
}
</style>
