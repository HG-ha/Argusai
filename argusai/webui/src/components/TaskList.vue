<script setup>
import { onMounted, onUnmounted, computed, ref, watch } from 'vue'
import { api } from '../api.js'
import { state, showToast } from '../state.js'
import { route, navigate } from '../router.js'
import { t } from '../i18n.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import StatusBadge from './StatusBadge.vue'
import ProgressBar from './ProgressBar.vue'
import NewScan from './NewScan.vue'
import ListToolbar from './list/ListToolbar.vue'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListSelect from './list/ListSelect.vue'
import ListRow from './list/ListRow.vue'
import PageEmpty from './list/PageEmpty.vue'

const confirmDialog = useConfirm()
const showCreate = ref(route.name === 'new')
const tasksError = ref('')

let timer = null
let inflight = false

const statusOptions = computed(() => [
  { label: t('tasks.filterAll'), value: '' },
  { label: t('status.pending'), value: 'pending' },
  { label: t('status.running'), value: 'running' },
  { label: t('status.done'), value: 'done' },
  { label: t('status.error'), value: 'error' },
  { label: t('status.cancelled'), value: 'cancelled' },
])

const {
  query: taskQuery,
  filters: taskFilters,
  page: taskPage,
  pageSize: taskPageSize,
  total: taskFilteredTotal,
  pageCount: taskPageCount,
  pageItems: taskPageItems,
  selectedCount: taskSelectedCount,
  selectedIds: taskSelectedIds,
  isSelected: taskIsSelected,
  toggle: taskToggle,
  clearSelection: taskClearSelection,
  selectPage: taskSelectPage,
  selectAllFiltered: taskSelectAllFiltered,
} = useClientList(
  computed(() => state.tasks),
  (task, q, f) => {
    if (f.status && task.status !== f.status) return false
    if (!q) return true
    const hay = `${task.name || ''} ${task.target_url || ''} ${task.id || ''}`.toLowerCase()
    return hay.includes(q)
  },
)
taskFilters.value = { status: '' }
taskPageSize.value = 20

useLiveChannel('tasks', () => { refresh() }, { debounceMs: 250 })

async function refresh() {
  if (inflight || showCreate.value) return
  inflight = true
  state.loadingTasks = true
  try { state.tasks = await api.listTasks(); tasksError.value = '' }
  catch (e) {
    tasksError.value = e.message || String(e)
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
  finally { state.loadingTasks = false; inflight = false }
}

function onTasksChanged() { refresh() }

async function onDelete(ev, id) {
  ev?.stopPropagation?.()
  const ok = await confirmDialog({
    title: t('tasks.confirmDeleteTitle'),
    content: t('tasks.confirmDelete'),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  try { await api.deleteTask(id); showToast(t('toast.taskDeleted')); taskToggle(id, false); await refresh() }
  catch (e) { showToast(t('toast.error', { msg: e.message || e }), 'error') }
}

async function onRescan(ev, id) {
  ev?.stopPropagation?.()
  try {
    const r = await api.rescanTask(id)
    if (!r.ok) { showToast(t('toast.error', { msg: r.error || 'failed' }), 'error'); return }
    showToast(t('toast.taskRescanned')); navigate(`/task/${r.id}`)
  } catch (e) { showToast(t('toast.error', { msg: e.message || e }), 'error') }
}

async function batchDelete() {
  const ids = taskSelectedIds.value
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('tasks.confirmDeleteTitle'),
    content: t('tasks.confirmDeleteBatch', { n: ids.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try { await api.deleteTask(id); okN++ } catch { fail++ }
  }
  taskClearSelection()
  await refresh()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
}

async function batchRescan() {
  const ids = taskSelectedIds.value
  if (!ids.length) return
  let okN = 0
  let fail = 0
  let lastId = ''
  for (const id of ids) {
    try {
      const r = await api.rescanTask(id)
      if (r?.ok) { okN++; lastId = r.id || id }
      else fail++
    } catch { fail++ }
  }
  taskClearSelection()
  await refresh()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
  if (okN === 1 && lastId) navigate(`/task/${lastId}`)
}

function openDetail(id) { navigate(`/task/${id}`) }

function openCreate() {
  showCreate.value = true
  if (route.name !== 'new') navigate('/new')
}

function closeCreate() {
  showCreate.value = false
  if (route.name === 'new') navigate('/')
}

function clearTaskFilters() {
  taskQuery.value = ''
  taskFilters.value.status = ''
  taskPage.value = 1
}

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = n => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function elapsed(task, now = nowSec.value) {
  if (!task.started_at) return ''
  const end = task.finished_at || now
  return t('tasks.elapsed', { s: Math.round(end - task.started_at) })
}

const totalTasks = computed(() => state.tasks.length)
const pageTitle = computed(() => (showCreate.value ? t('newScan.title') : t('tasks.title')))
const hasTaskFilters = computed(() => Boolean(taskQuery.value.trim() || taskFilters.value.status))
const taskListSummary = computed(() => {
  if (!totalTasks.value) return t('tasks.subtitle', { count: 0 })
  const pagePart = taskPageCount.value > 1
    ? ` · ${t('tasks.pageOf', { page: taskPage.value, pages: taskPageCount.value })}`
    : ''
  if (!hasTaskFilters.value) {
    return `${t('tasks.subtitle', { count: totalTasks.value })} · ${t('tasks.pageItems', { n: taskPageItems.value.length })}${pagePart}`
  }
  return `${t('tasks.filterHit', { hit: taskFilteredTotal.value, total: totalTasks.value })}${pagePart}`
})

function primarySeverity(task) {
  const severity = task?.severity || {}
  const levels = ['critical', 'high', 'medium', 'low', 'info']
  for (const key of levels) {
    const count = Number(severity[key] || 0)
    if (count > 0) return { key, text: t('tasks.severityCount', { label: t(`severity.${key}`), n: count }) }
  }
  return { key: 'info', text: t('tasks.findings', { n: task?.total || 0 }) }
}

watch(() => route.name, (name) => {
  showCreate.value = name === 'new'
}, { immediate: true })

let tickCounter = 0
const nowSec = ref(Math.floor(Date.now() / 1000))
let clockTimer = null
const hasRunningTask = computed(() => state.tasks.some(tsk => tsk.status === 'running' || tsk.status === 'pending'))
onMounted(() => {
  refresh()
  clockTimer = setInterval(() => {
    if (!showCreate.value && hasRunningTask.value) {
      nowSec.value = Math.floor(Date.now() / 1000)
    }
  }, 1000)
  timer = setInterval(() => {
    if (showCreate.value || document.visibilityState === 'hidden') return
    tickCounter++
    if (hasRunningTask.value || tickCounter % 5 === 0) refresh()
  }, 2000)
  window.addEventListener('argusai:tasks-changed', onTasksChanged)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (clockTimer) clearInterval(clockTimer)
  window.removeEventListener('argusai:tasks-changed', onTasksChanged)
})
</script>

<template>
  <div class="page tasks-page" :class="{ 'list-page': !showCreate }">
    <div class="page-head">
      <div>
        <h1>{{ pageTitle }}</h1>
        <p v-if="!showCreate">{{ taskListSummary }}</p>
      </div>
      <div class="row-gap-sm">
        <template v-if="showCreate">
          <button class="ghost" type="button" @click="closeCreate">{{ t('tasks.backToList') }}</button>
        </template>
        <template v-else>
          <button class="ghost" @click="refresh">{{ t('common.refresh') }}</button>
          <button class="primary" @click="openCreate">{{ t('nav.new') }}</button>
        </template>
      </div>
    </div>

    <div v-if="showCreate" class="create-scroll">
      <NewScan embedded />
    </div>

    <template v-else>
      <ListToolbar v-model="taskQuery" :placeholder="t('common.search')">
        <template #filters>
          <ListSelect v-model="taskFilters.status" :options="statusOptions" min-width="120px" />
        </template>
        <button
          v-if="hasTaskFilters"
          type="button"
          class="ghost list-clear-filter"
          @click="clearTaskFilters"
        >{{ t('common.clearFilters') }}</button>
      </ListToolbar>

      <ListBatchBar
        :count="taskSelectedCount"
        :total="taskFilteredTotal"
        @select-page="taskSelectPage(true)"
        @select-all="taskSelectAllFiltered(true)"
        @clear="taskClearSelection()"
      >
        <button type="button" class="ghost list-batch-btn" @click="batchRescan">{{ t('common.batchRescan') }}</button>
        <button type="button" class="danger list-batch-btn" @click="batchDelete">{{ t('common.delete') }}</button>
      </ListBatchBar>

      <PageEmpty
        v-if="state.loadingTasks && totalTasks === 0"
        :title="t('common.loading')"
      />

      <PageEmpty
        v-else-if="taskFilteredTotal === 0 && tasksError"
        :title="t('tasks.loadFailed')"
        :description="tasksError"
        :action-label="t('common.retry')"
        @action="refresh"
      />

      <PageEmpty
        v-else-if="taskFilteredTotal === 0"
        :title="state.tasks.length ? t('common.noMatch') : t('tasks.empty')"
        :description="state.tasks.length ? t('common.noMatchHint') : ''"
        :action-label="state.tasks.length ? '' : t('tasks.createFirst')"
        @action="openCreate"
      />

      <template v-else>
        <div class="list-body" role="list" :aria-label="t('tasks.listAria')">
          <ListRow
            v-for="task in taskPageItems"
            :key="task.id"
            class="task-card"
            :selected="taskIsSelected(task.id)"
            :checked="taskIsSelected(task.id)"
            :aria-label="t('tasks.selectAria', { name: task.name || task.target_url || task.id })"
            @update:checked="taskToggle(task.id, $event)"
            @open="openDetail(task.id)"
          >
            <div
              class="task-summary"
              role="button"
              tabindex="0"
              :aria-label="t('tasks.openAria', { name: task.name || task.target_url || task.id })"
              @click="openDetail(task.id)"
              @keydown.enter.prevent="openDetail(task.id)"
              @keydown.space.prevent="openDetail(task.id)"
            >
              <span class="task-copy">
                <span class="name">{{ task.name || task.target_url || task.id }}</span>
                <span class="target">{{ task.target_url || '—' }} · {{ task.id }}</span>
                <span class="meta">
                  <StatusBadge :status="task.status" />
                  <span class="task-severity">
                    <i class="sev-dot" :class="primarySeverity(task).key" aria-hidden="true"></i>
                    {{ primarySeverity(task).text }}
                  </span>
                  <span class="mono">{{ task.checks_done || 0 }}/{{ task.checks_total || (task.checks || []).length || 0 }}</span>
                  <span v-if="task.started_at">{{ elapsed(task, nowSec) }}</span>
                  <button
                    v-if="task.status === 'done' || task.status === 'error' || task.status === 'cancelled'"
                    type="button"
                    class="ghost sm task-inline-action"
                    :disabled="task.status === 'running'"
                    @click.stop="onRescan($event, task.id)"
                  >{{ t('tasks.rescan') }}</button>
                  <button
                    v-else
                    type="button"
                    class="ghost sm task-inline-action"
                    @click.stop="openDetail(task.id)"
                  >{{ t('common.open') }}</button>
                </span>
                <span v-if="task.status === 'running'" class="task-progress">
                  <ProgressBar :value="task.progress || 0" compact :indeterminate="!task.progress" />
                  <span class="task-progress-label">
                    {{ task.checks_done || 0 }}/{{ task.checks_total || 0 }}
                    <span v-if="task.current_check"> · {{ task.current_check }}</span>
                  </span>
                </span>
              </span>
            </div>
            <template #actions>
              <details class="task-more" @click.stop>
                <summary :aria-label="t('common.moreActions')">
                  <svg viewBox="0 0 16 4" width="14" height="4" aria-hidden="true">
                    <circle cx="2" cy="2" r="1.5" /><circle cx="8" cy="2" r="1.5" /><circle cx="14" cy="2" r="1.5" />
                  </svg>
                </summary>
                <div class="task-more-menu">
                  <button
                    type="button"
                    :disabled="task.status === 'running'"
                    @click="onRescan($event, task.id)"
                  >{{ t('tasks.rescan') }}</button>
                  <button class="danger" type="button" @click="onDelete($event, task.id)">{{ t('common.delete') }}</button>
                </div>
              </details>
            </template>
          </ListRow>
        </div>
        <ListPager
          v-model:page="taskPage"
          v-model:page-size="taskPageSize"
          :page-count="taskPageCount"
          :total="taskFilteredTotal"
        />
      </template>
    </template>
  </div>
</template>

<style scoped>
.tasks-page {
  width: 100%;
  max-width: none;
}
.tasks-page:not(.list-page) {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.create-scroll {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-bottom: 28px;
}
/* The scan workbench needs the full content width for its form + plan columns. */
.tasks-page :deep(.new-scan-embedded) {
  width: 100%;
  max-width: none;
}
.tasks-page :deep(.new-scan-embedded .form.panel) {
  max-width: none;
}
.task-card {
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px 14px;
  padding: 13px 14px;
}
.task-card.selected {
  background: color-mix(in srgb, var(--accent) 9%, var(--card-bg));
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
}
.task-summary {
  min-width: 0;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  text-align: left;
  box-shadow: none;
}
.task-summary:hover {
  background: transparent;
}
.task-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
}
.task-copy,
.task-copy > span {
  display: block;
  min-width: 0;
}
.task-progress {
  margin-top: 8px;
}
.task-progress-label {
  margin-top: 3px;
  color: var(--muted);
  font-family: Consolas, monospace;
  font-size: 11px;
}
.task-actions button,
.list-clear-filter {
  min-height: 28px;
  padding: 3px 10px;
  font-size: 12px;
}
.task-severity {
  display: inline-flex !important;
  align-items: center;
}
.task-severity .sev-dot {
  margin-right: 5px;
}
.task-inline-action {
  display: inline-flex !important;
  min-height: 24px;
  margin-left: 2px;
  padding: 2px 8px;
  opacity: 0;
  transform: none;
  transition: opacity 120ms ease;
}
.task-card:hover .task-inline-action,
.task-card.selected .task-inline-action,
.task-card:focus-within .task-inline-action {
  opacity: 1;
}
.task-more {
  position: relative;
  opacity: 0;
  transition: opacity 120ms ease;
}
.task-card:hover .task-more,
.task-card:focus-within .task-more,
.task-more[open] {
  opacity: 1;
}
.task-more > summary {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  color: var(--muted);
  cursor: pointer;
  list-style: none;
}
.task-more > summary::-webkit-details-marker { display: none; }
.task-more > summary svg { fill: currentColor; }
.task-more > summary:hover,
.task-more[open] > summary {
  background: var(--panel-hover);
  color: var(--fg-strong);
}
.task-more-menu {
  position: absolute;
  z-index: 30;
  top: calc(100% + 4px);
  right: 0;
  display: grid;
  min-width: 124px;
  padding: 5px;
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  background: var(--card-bg);
  box-shadow: var(--shadow-sm);
}
.task-more-menu button {
  min-height: 30px;
  padding: 0 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  text-align: left;
}
.task-more-menu button:hover:not(:disabled) { background: var(--panel-hover); }
.task-more-menu button.danger { color: var(--err); }
@media (max-width: 760px) {
  .task-card { --task-mobile: 1; }
}
</style>
