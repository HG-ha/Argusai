<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useAssistantTabs } from '../composables/useAssistantTabs.js'
import { useClientList } from '../composables/useClientList.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListRow from './list/ListRow.vue'
import ListSelect from './list/ListSelect.vue'
import ListToolbar from './list/ListToolbar.vue'
import PageEmpty from './list/PageEmpty.vue'

const sessions = ref([])
const loading = ref(true)
const refreshing = ref(false)
const listError = ref('')
const selectedId = ref('')
const detailLoading = ref(false)
const detailError = ref('')
const details = ref(null)
const sandbox = ref(null)
const agents = ref([])
const runJobs = ref([])
const saving = ref(false)
const deleting = ref(false)
const deleteProgress = reactive({ done: 0, total: 0 })
const sandboxSummaries = reactive({})
const editForm = reactive({ name: '', note: '' })
let detailRequest = 0

const confirmDialog = useConfirm()
const { tabs, openTab, closeTab, setActiveTab } = useAssistantTabs()
const sandboxBrowser = useSandboxBrowser()

function liveTab(id) {
  return tabs.find(tab => tab.sessionId === id) || null
}

function sessionState(session) {
  const tab = liveTab(session?.id)
  if (tab?.pendingTools?.length || tab?.confirmingAction || Number(session?.pending_count || 0) > 0) return 'waiting'
  if (tab?.sending || tab?.activeRunId || tab?.activeStreamMessage || session?.is_running) return 'running'
  if ((tab?.badge?.unread || 0) > 0) return 'unread'
  return 'idle'
}

function stateLabel(session) {
  return t(`assistantCenter.state.${sessionState(session) || 'idle'}`)
}

function statePill(session) {
  return {
    running: 'run',
    waiting: 'warn',
    unread: 'info',
    idle: '',
  }[sessionState(session)] || ''
}

const listSource = computed(() => sessions.value.map(item => ({
  ...item,
  state: sessionState(item),
  sandbox: sandboxSummaries[item.id] || null,
})))

const clientList = useClientList(listSource, (item, query, filters) => {
  const status = String(filters.status || '')
  const type = String(filters.type || 'all')
  if (type !== 'all' && sessionType(item) !== type) return false
  if (status && status !== 'all') {
    if (status === 'sandbox' && !(item.sandbox?.exists || item.has_sandbox)) return false
    if (status !== 'sandbox' && item.state !== status) return false
  }
  if (!query) return true
  return [item.name, item.note, item.id]
    .filter(Boolean)
    .some(value => String(value).toLowerCase().includes(query))
})

const {
  query,
  filters,
  page,
  pageSize,
  pageItems,
  total,
  pageCount,
  selectedCount,
  selectedIds,
  isSelected,
  toggle,
  clearSelection,
  selectPage,
  selectAllFiltered,
} = clientList

filters.value = { status: 'all', type: 'all' }

const statusOptions = computed(() => [
  { label: t('assistantCenter.allStatuses'), value: 'all' },
  { label: t('assistantCenter.state.running'), value: 'running' },
  { label: t('assistantCenter.state.waiting'), value: 'waiting' },
  { label: t('assistantCenter.state.unread'), value: 'unread' },
  { label: t('assistantCenter.hasSandbox'), value: 'sandbox' },
  { label: t('assistantCenter.state.idle'), value: 'idle' },
])
const typeOptions = computed(() => [
  { label: t('assistantCenter.allTypes'), value: 'all' },
  { label: t('assistantCenter.type.native'), value: 'native' },
  { label: t('assistantCenter.type.localAgent'), value: 'local_agent' },
  { label: t('assistantCenter.type.channel'), value: 'channel' },
])

const selectedSession = computed(() =>
  sessions.value.find(item => item.id === selectedId.value) || null,
)

const activeAgents = computed(() => agents.value.filter(agent => {
  const status = String(agent?.status || '').toLowerCase()
  return ['running', 'queued', 'pending', 'waiting', 'paused'].includes(status)
}))

const activeJobs = computed(() => runJobs.value.filter(job => {
  const status = String(job?.status || '').toLowerCase()
  return ['running', 'queued', 'pending', 'waiting_user', 'waiting_confirmation', 'paused'].includes(status)
}))

function formatTime(value) {
  const numeric = Number(value)
  if (!numeric) return '—'
  const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

function originLabel(origin) {
  const key = ['gui', 'channel'].includes(origin) ? origin : 'gui'
  return t(`assistantCenter.origin.${key}`)
}

function sessionType(session) {
  if (['native', 'local_agent', 'channel'].includes(session?.session_type)) {
    return session.session_type
  }
  if (session?.origin === 'channel') return 'channel'
  return ['', 'native', 'argusai-native'].includes(String(session?.runtime_id || ''))
    ? 'native'
    : 'local_agent'
}

function typeLabel(session) {
  const key = sessionType(session)
  return t(`assistantCenter.type.${key === 'local_agent' ? 'localAgent' : key}`)
}

function formatBytes(value) {
  let size = Number(value || 0)
  if (!size) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

async function loadSessions({ preserveSelection = true } = {}) {
  if (refreshing.value) return
  const isInitialLoad = sessions.value.length === 0
  refreshing.value = true
  if (isInitialLoad) loading.value = true
  listError.value = ''
  try {
    const result = await api.listAssistantSessions()
    sessions.value = Array.isArray(result) ? result : (Array.isArray(result?.sessions) ? result.sessions : [])
    if (!preserveSelection || !sessions.value.some(item => item.id === selectedId.value)) {
      selectedId.value = sessions.value[0]?.id || ''
    }
  } catch (error) {
    listError.value = error?.message || String(error)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

async function loadDetail(sessionId = selectedId.value) {
  if (!sessionId) {
    details.value = null
    sandbox.value = null
    agents.value = []
    runJobs.value = []
    return
  }
  const request = ++detailRequest
  detailLoading.value = true
  detailError.value = ''
  try {
    const [sessionResult, sandboxResult, agentResult, jobResult] = await Promise.all([
      api.getAssistantSession(sessionId),
      api.getAssistantSandboxSummary(sessionId),
      api.listAssistantAgents(sessionId, true),
      api.listAssistantRunJobs(sessionId),
    ])
    if (request !== detailRequest) return
    if (!sessionResult?.ok) throw new Error(sessionResult?.error || t('assistantCenter.readFailed'))
    details.value = sessionResult
    sandbox.value = sandboxResult?.ok ? sandboxResult : { exists: false }
    sandboxSummaries[sessionId] = { ...sandbox.value, loaded: true }
    agents.value = Array.isArray(agentResult?.agents) ? agentResult.agents : []
    runJobs.value = Array.isArray(jobResult?.jobs) ? jobResult.jobs : []
    editForm.name = sessionResult.name || selectedSession.value?.name || ''
    editForm.note = sessionResult.note || ''
  } catch (error) {
    if (request === detailRequest) detailError.value = error?.message || String(error)
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

function selectSession(sessionId) {
  if (!sessionId || sessionId === selectedId.value) return
  selectedId.value = sessionId
}

function openConversation(sessionId = selectedId.value) {
  const session = sessions.value.find(item => item.id === sessionId)
  const tab = sessionId
    ? openTab({ sessionId, name: session?.name || t('assistantCenter.defaultConversationName') })
    : openTab({ name: '新会话' })
  if (!tab) {
    showToast(t('assistantCenter.tabLimitReached'), 'warning')
    return
  }
  setActiveTab(tab.sessionId)
  window.dispatchEvent(new CustomEvent('argusai:open-assistant-session', {
    detail: { sessionId: tab.sessionId },
  }))
}

function browseSandbox(sessionId = selectedId.value) {
  if (!sessionId) return
  sandboxBrowser.openPanel(sessionId)
}

async function saveMetadata() {
  if (!selectedId.value || saving.value) return
  saving.value = true
  try {
    const result = await api.updateAssistantSession(selectedId.value, {
      name: editForm.name,
      note: editForm.note,
    })
    if (!result?.ok) throw new Error(result?.error || t('assistantCenter.saveFailed'))
    const index = sessions.value.findIndex(item => item.id === selectedId.value)
    if (index >= 0) {
      sessions.value[index] = {
        ...sessions.value[index],
        name: result.session?.name || editForm.name,
        note: result.session?.note || editForm.note,
        updated_at: result.session?.updated_at || sessions.value[index].updated_at,
      }
    }
    if (details.value) {
      details.value = {
        ...details.value,
        name: result.session?.name || editForm.name,
        note: result.session?.note || editForm.note,
      }
    }
    showToast(t('assistantCenter.saved'), 'success')
  } catch (error) {
    showToast(t('assistantCenter.saveError', { error: error?.message || error }), 'error')
  } finally {
    saving.value = false
  }
}

// Each delete walks a session's `.sandbox` (can hold a `.venv` from the
// Python-snippet sandbox tool), `.runs`, and `_agents/<id>` directories with a
// synchronous shutil.rmtree on the Python side — cheap for one session, but a
// plain sequential loop turned "delete the 200 sessions a cron job spawned"
// into a multi-minute stall with no visible progress (looked hung). pywebview
// already runs each JS->Python call on its own thread (see
// webview.util.js_bridge_call), so a small concurrency pool lets several
// rmtrees overlap without opening hundreds of threads / hammering the disk
// (and antivirus real-time scanning) at once.
const DELETE_CONCURRENCY = 4

async function runLimited(items, limit, worker) {
  let cursor = 0
  async function lane() {
    while (cursor < items.length) {
      const index = cursor++
      await worker(items[index], index)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane))
}

async function deleteSessions(ids) {
  const targets = [...new Set((ids || []).filter(Boolean))]
  if (!targets.length || deleting.value) return
  const ok = await confirmDialog({
    title: t(targets.length === 1 ? 'assistantCenter.deleteOneTitle' : 'assistantCenter.deleteManyTitle', { count: targets.length }),
    content: t('assistantCenter.deleteContent'),
    positiveText: t('assistantCenter.deletePermanently'),
    type: 'error',
  })
  if (!ok) return
  deleting.value = true
  deleteProgress.done = 0
  deleteProgress.total = targets.length
  let failed = 0
  await runLimited(targets, DELETE_CONCURRENCY, async (id) => {
    try {
      const result = await api.deleteAssistantSession(id)
      if (!result?.ok) failed += 1
      else closeTab(id)
    } catch {
      failed += 1
    } finally {
      deleteProgress.done += 1
    }
  })
  clearSelection()
  await loadSessions({ preserveSelection: true })
  showToast(
    t(failed ? 'assistantCenter.deletePartial' : 'assistantCenter.deleted', { failed }),
    failed ? 'warning' : 'success',
  )
  deleting.value = false
  deleteProgress.total = 0
}

function onSessionUpdated() {
  loadSessions({ preserveSelection: true }).catch(() => {})
  if (selectedId.value) loadDetail(selectedId.value).catch(() => {})
}

watch(selectedId, id => {
  loadDetail(id).catch(() => {})
})

onMounted(() => {
  window.addEventListener('argusai:assistant-session-updated', onSessionUpdated)
  loadSessions({ preserveSelection: false }).catch(() => {})
})

onUnmounted(() => {
  window.removeEventListener('argusai:assistant-session-updated', onSessionUpdated)
})
</script>

<template>
  <div class="page list-page assistant-center-page">
    <header class="page-head assistant-center-head">
      <div>
        <h2>{{ t('assistantCenter.title') }}</h2>
        <p>{{ t('assistantCenter.subtitle') }}</p>
      </div>
      <div class="row-actions">
        <button type="button" class="ghost" :disabled="loading || refreshing" @click="loadSessions()">
          {{ refreshing && !loading ? t('assistantCenter.refreshing') : t('common.refresh') }}
        </button>
        <button type="button" class="primary" @click="openConversation('')">{{ t('assistantCenter.newConversation') }}</button>
      </div>
    </header>

    <div class="assistant-center-shell">
      <section class="assistant-center-list" :aria-label="t('assistantCenter.listAria')">
        <ListToolbar v-model="query" :placeholder="t('assistantCenter.searchPlaceholder')">
          <template #filters>
            <ListSelect
              :model-value="filters.type"
              :options="typeOptions"
              min-width="116px"
              @update:model-value="filters.type = $event"
            />
            <ListSelect
              :model-value="filters.status"
              :options="statusOptions"
              min-width="110px"
              @update:model-value="filters.status = $event"
            />
          </template>
        </ListToolbar>

        <ListBatchBar
          :count="selectedCount"
          :total="total"
          @select-page="selectPage(true)"
          @select-all="selectAllFiltered(true)"
          @clear="clearSelection"
        >
          <button type="button" class="ghost danger list-batch-btn" :disabled="deleting" @click="deleteSessions(selectedIds)">
            {{ deleting ? t('assistantCenter.deleteProgress', deleteProgress) : t('common.delete') }}
          </button>
        </ListBatchBar>

        <div v-if="loading" class="assistant-center-state">{{ t('common.loading') }}</div>
        <div v-else-if="listError" class="assistant-center-state error">
          <strong>{{ t('assistantCenter.listLoadFailed') }}</strong>
          <span>{{ listError }}</span>
          <button type="button" class="ghost" @click="loadSessions()">{{ t('assistantCenter.retry') }}</button>
        </div>
        <PageEmpty
          v-else-if="!pageItems.length"
          :title="sessions.length ? t('common.noMatch') : t('assistantCenter.emptyTitle')"
          :description="sessions.length ? t('common.noMatchHint') : t('assistantCenter.emptyDescription')"
          :action-label="sessions.length ? '' : t('assistantCenter.newConversation')"
          @action="openConversation('')"
        />
        <div v-else class="assistant-session-list" role="list">
          <ListRow
            v-for="session in pageItems"
            :key="session.id"
            :selected="selectedId === session.id"
            :checked="isSelected(session.id)"
            :aria-label="t('assistantCenter.selectAria', { name: session.name || session.id })"
            @update:checked="toggle(session.id, $event)"
            @open="openConversation(session.id)"
          >
            <button type="button" class="assistant-session-main" @click="selectSession(session.id)">
              <span class="assistant-session-title-line">
                <strong :title="session.name || session.id">{{ session.name || session.id }}</strong>
              </span>
              <span v-if="session.note" class="assistant-session-note" :title="session.note">{{ session.note }}</span>
              <span class="assistant-session-meta">
                <span class="session-origin">{{ typeLabel(session) }} · {{ session.runtime_id || 'argusai-native' }} · {{ originLabel(session.origin) }}</span>
                <span>{{ t('assistantCenter.messageCount', { count: session.msg_count || 0 }) }}</span>
                <span>{{ formatTime(session.updated_at) }}</span>
                <span v-if="session.sandbox?.exists">{{ t('assistantCenter.sandboxFileCount', { count: session.sandbox.file_count || 0 }) }}</span>
                <span v-else-if="session.has_sandbox">{{ t('assistantCenter.hasSandbox') }}</span>
              </span>
            </button>
            <template #actions>
              <span class="pill assistant-session-state" :class="statePill(session)">{{ stateLabel(session) }}</span>
              <button type="button" class="ghost sm" @click="openConversation(session.id)">{{ t('assistantCenter.enterConversation') }}</button>
            </template>
          </ListRow>
        </div>

        <ListPager
          v-model:page="page"
          v-model:page-size="pageSize"
          :page-count="pageCount"
          :total="total"
        />
      </section>

      <aside class="assistant-center-detail" :aria-label="t('assistantCenter.detailAria')">
        <PageEmpty
          v-if="!selectedSession"
          :title="t('assistantCenter.selectTitle')"
          :description="t('assistantCenter.selectDescription')"
        />
        <div v-else-if="detailLoading && !details" class="assistant-center-state">{{ t('common.loading') }}</div>
        <div v-else-if="detailError" class="assistant-center-state error">
          <strong>{{ t('assistantCenter.detailLoadFailed') }}</strong>
          <span>{{ detailError }}</span>
          <button type="button" class="ghost" @click="loadDetail()">{{ t('assistantCenter.retry') }}</button>
        </div>
        <template v-else>
          <div class="assistant-detail-head">
            <div>
              <span class="assistant-detail-kicker">{{ t('assistantCenter.detailKicker') }}</span>
              <h3>{{ details?.name || selectedSession.name || selectedSession.id }}</h3>
              <span class="mono assistant-detail-id">{{ selectedSession.id }}</span>
            </div>
            <div class="assistant-detail-head-actions">
              <span class="pill" :class="statePill(selectedSession)">{{ stateLabel(selectedSession) }}</span>
              <button type="button" class="primary sm" @click="openConversation()">{{ t('assistantCenter.enterConversation') }}</button>
              <button type="button" class="ghost sm" @click="browseSandbox()">{{ t('assistantCenter.browseSandbox') }}</button>
            </div>
          </div>

          <dl class="assistant-detail-facts">
            <div><dt>{{ t('assistantCenter.messages') }}</dt><dd>{{ details?.message_count ?? selectedSession.msg_count ?? 0 }}</dd></div>
            <div><dt>{{ t('assistantCenter.updated') }}</dt><dd>{{ formatTime(selectedSession.updated_at) }}</dd></div>
            <div><dt>{{ t('assistantCenter.activeSubagents') }}</dt><dd>{{ activeAgents.length }}</dd></div>
            <div><dt>{{ t('assistantCenter.activeRunJobs') }}</dt><dd>{{ activeJobs.length }}</dd></div>
          </dl>

          <section class="assistant-detail-section">
            <div class="assistant-detail-section-head">
              <div>
                <h4>{{ t('assistantCenter.nameAndNote') }}</h4>
                <p>{{ t('assistantCenter.noteHint') }}</p>
              </div>
            </div>
            <div class="assistant-detail-form">
              <label>
                <span>{{ t('assistantCenter.name') }}</span>
                <input v-model="editForm.name" type="text" maxlength="120" />
              </label>
              <label>
                <span>{{ t('assistantCenter.note') }}</span>
                <textarea v-model="editForm.note" rows="3" maxlength="4000"></textarea>
              </label>
              <button type="button" class="ghost" :disabled="saving" @click="saveMetadata">
                {{ saving ? t('common.loading') : t('assistantCenter.saveMetadata') }}
              </button>
            </div>
          </section>

          <section class="assistant-detail-section">
            <div class="assistant-detail-section-head">
              <div>
                <h4>{{ t('assistantCenter.sandboxOverview') }}</h4>
                <p v-if="sandbox?.exists">
                  {{ t('assistantCenter.sandboxSummary', { files: sandbox.file_count || 0, dirs: sandbox.dir_count || 0, size: formatBytes(sandbox.total_size) }) }}
                </p>
                <p v-else>{{ t('assistantCenter.noSandboxFiles') }}</p>
              </div>
              <button type="button" class="ghost sm" @click="browseSandbox">{{ t('assistantCenter.open') }}</button>
            </div>
            <div v-if="sandbox?.root_items?.length" class="assistant-sandbox-items">
              <div v-for="item in sandbox.root_items.slice(0, 8)" :key="item.relative_path || item.path || item.name" class="assistant-sandbox-item">
                <span class="sandbox-kind" :class="item.kind === 'dir' || item.is_dir ? 'dir' : 'file'" aria-hidden="true"></span>
                <span :title="item.name">{{ item.name }}</span>
                <small>{{ item.kind === 'dir' || item.is_dir ? t('assistantCenter.directory') : formatBytes(item.size) }}</small>
              </div>
            </div>
          </section>

          <section class="assistant-detail-section">
            <div class="assistant-detail-section-head">
              <div>
                <h4>{{ t('assistantCenter.runtimeOverview') }}</h4>
                <p>{{ t('assistantCenter.runtimeSummary', { agents: agents.length, jobs: runJobs.length }) }}</p>
              </div>
            </div>
            <div class="assistant-activity-grid">
              <div>
                <span>{{ t('assistantCenter.waitingConfirmation') }}</span>
                <strong>{{ details?.pending_tools?.length || 0 }}</strong>
              </div>
              <div>
                <span>{{ t('assistantCenter.memoryEntries') }}</span>
                <strong>{{ details?.memory_count || 0 }}</strong>
              </div>
              <div>
                <span>{{ t('assistantCenter.sessionTodos') }}</span>
                <strong>{{ details?.todos?.length || 0 }}</strong>
              </div>
            </div>
          </section>

          <section class="assistant-detail-danger">
            <div>
              <strong>{{ t('assistantCenter.deleteSession') }}</strong>
              <span>{{ t('assistantCenter.deleteSessionHint') }}</span>
            </div>
            <button type="button" class="ghost danger" :disabled="deleting" @click="deleteSessions([selectedSession.id])">
              {{ t('common.delete') }}
            </button>
          </section>
        </template>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.assistant-center-page {
  container-type: inline-size;
  gap: 14px;
  padding: 18px 20px;
}
.assistant-center-head p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.assistant-center-shell {
  display: grid;
  min-height: 0;
  flex: 1 1 auto;
  grid-template-columns: minmax(280px, 34%) minmax(0, 1fr);
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card-bg);
}
.assistant-center-list,
.assistant-center-detail {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.assistant-center-list {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--card-bg);
}
.assistant-center-list :deep(.list-toolbar) {
  flex: 0 0 auto;
  padding: 10px;
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
}
.assistant-center-list :deep(.list-batch) {
  margin: 8px 10px 0;
}
.assistant-session-list {
  display: flex;
  min-height: 0;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
.assistant-session-list :deep(.workbench-list-row) {
  grid-template-columns: 18px minmax(0, 1fr) auto;
  gap: 9px;
  padding: 11px 12px;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
}
.assistant-session-list :deep(.workbench-list-row:hover) {
  background: var(--panel-hover);
}
.assistant-session-list :deep(.workbench-list-row.selected) {
  box-shadow: inset 3px 0 0 var(--accent);
  background: color-mix(in srgb, var(--accent) 9%, var(--card-bg));
}
.assistant-session-list :deep(.workbench-list-row-actions) {
  align-self: center;
  flex-wrap: nowrap;
}
.assistant-session-list :deep(.workbench-list-row-actions .ghost) {
  min-height: 28px;
  opacity: .76;
}
.assistant-session-list :deep(.workbench-list-row:hover .workbench-list-row-actions .ghost),
.assistant-session-list :deep(.workbench-list-row.selected .workbench-list-row-actions .ghost),
.assistant-session-list :deep(.workbench-list-row:focus-within .workbench-list-row-actions .ghost) {
  opacity: 1;
}
.assistant-center-list :deep(.list-pager) {
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
  background: var(--card-bg);
}
.assistant-center-list :deep(.page-empty) {
  margin: 12px;
  border: 0;
  background: transparent;
}
.assistant-session-main {
  display: flex;
  width: 100%;
  min-width: 0;
  flex-direction: column;
  align-items: stretch;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
  box-shadow: none;
  transform: none;
}
.assistant-session-main:hover,
.assistant-session-main:active {
  background: transparent;
  box-shadow: none;
  transform: none;
}
.assistant-session-main:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.assistant-session-state {
  display: inline-flex;
  min-width: 44px;
  min-height: 24px;
  align-items: center;
  justify-content: center;
  padding: 0 7px;
  line-height: 1;
  text-align: center;
}
.assistant-session-title-line {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
}
.assistant-session-title-line strong {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-session-note {
  overflow: hidden;
  margin-top: 3px;
  color: var(--muted);
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-session-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 10px;
  margin-top: 5px;
  color: var(--muted-2, var(--muted));
  font-size: 10.5px;
}
.session-origin {
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--muted);
  font: 10px/1.25 var(--mono);
}
.assistant-center-detail {
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  padding: 18px 20px 24px;
  background: var(--card-bg);
}
.assistant-center-detail > :deep(.page-empty) {
  min-height: 100%;
  border: 0;
  background: transparent;
}
.assistant-center-state {
  display: flex;
  min-height: 180px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 8px;
  padding: 20px;
  color: var(--muted);
}
.assistant-center-state.error strong { color: var(--err); }
.assistant-center-state.error span { max-width: 480px; text-align: center; }
.assistant-detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding-bottom: 16px;
}
.assistant-detail-kicker {
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.assistant-detail-head h3 {
  margin: 4px 0 3px;
  color: var(--fg-strong);
  font-size: 18px;
  font-weight: 650;
  line-height: 1.35;
}
.assistant-detail-id {
  display: block;
  max-width: 420px;
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-detail-head-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.assistant-detail-facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  margin: 0;
  padding: 12px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.assistant-detail-facts div {
  min-width: 0;
  padding: 0 12px;
  border-left: 1px solid var(--border);
}
.assistant-detail-facts div:first-child {
  padding-left: 0;
  border-left: 0;
}
.assistant-detail-facts dt {
  color: var(--muted);
  font-size: 9.5px;
}
.assistant-detail-facts dd {
  overflow: hidden;
  margin: 3px 0 0;
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-detail-section {
  margin-top: 0;
  padding: 16px 0;
  border-top: 1px solid var(--border);
}
.assistant-detail-facts + .assistant-detail-section {
  border-top: 0;
}
.assistant-detail-section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.assistant-detail-section h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 12.5px;
}
.assistant-detail-section p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 10.5px;
}
.assistant-detail-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px 8px;
  align-items: end;
  margin-top: 12px;
}
.assistant-detail-form label {
  display: grid;
  gap: 4px;
}
.assistant-detail-form label:first-child {
  grid-column: 1;
}
.assistant-detail-form label:nth-child(2) {
  grid-column: 1 / -1;
}
.assistant-detail-form label > span {
  color: var(--muted);
  font-size: 10.5px;
}
.assistant-detail-form button {
  grid-column: 2;
  grid-row: 1;
  align-self: end;
}
.assistant-sandbox-items {
  display: grid;
  gap: 0;
  margin-top: 10px;
  overflow: hidden;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.assistant-sandbox-item {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
  padding: 8px 2px;
  border-top: 1px solid var(--border);
  background: transparent;
  font-size: 11px;
}
.assistant-sandbox-item:first-child { border-top: 0; }
.assistant-sandbox-item > span:nth-child(2) {
  overflow: hidden;
  color: var(--fg);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-sandbox-item small {
  color: var(--muted);
  font-size: 9.5px;
}
.sandbox-kind {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--muted);
}
.sandbox-kind.dir { background: var(--accent); }
.assistant-activity-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  margin-top: 10px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.assistant-activity-grid div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border-left: 1px solid var(--border);
  background: transparent;
}
.assistant-activity-grid div:first-child {
  padding-left: 0;
  border-left: 0;
}
.assistant-activity-grid span {
  color: var(--muted);
  font-size: 10px;
}
.assistant-activity-grid strong {
  color: var(--fg-strong);
  font: 700 12px/1 var(--mono);
}
.assistant-detail-danger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 0;
  padding: 16px 0 4px;
  border-top: 1px solid var(--border);
}
.assistant-detail-danger div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.assistant-detail-danger strong {
  color: var(--fg-strong);
  font-size: 11px;
}
.assistant-detail-danger span {
  color: var(--muted);
  font-size: 10px;
}

@container (max-width: 720px) {
  .assistant-center-shell {
    display: block;
    overflow-x: hidden;
    overflow-y: auto;
    grid-template-columns: 1fr;
  }
  .assistant-center-list {
    height: min(58vh, 520px);
    min-height: 360px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .assistant-center-detail {
    overflow: visible;
  }
}

@container (max-width: 560px) {
  .assistant-center-page {
    padding: 12px;
  }
  .assistant-detail-head {
    flex-direction: column;
  }
  .assistant-detail-head-actions {
    width: 100%;
    justify-content: flex-start;
  }
  .assistant-detail-facts,
  .assistant-activity-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .assistant-detail-form {
    grid-template-columns: 1fr;
  }
  .assistant-detail-form label:first-child,
  .assistant-detail-form label:nth-child(2),
  .assistant-detail-form button {
    grid-column: 1;
    grid-row: auto;
  }
  .assistant-detail-head-actions,
  .assistant-detail-danger {
    align-items: stretch;
  }
}
</style>
