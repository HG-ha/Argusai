<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { NSelect } from 'naive-ui'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import ListToolbar from './list/ListToolbar.vue'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListSelect from './list/ListSelect.vue'
import ListRow from './list/ListRow.vue'
import PageEmpty from './list/PageEmpty.vue'
import WorkbenchModal from './workbench/WorkbenchModal.vue'

const confirmDialog = useConfirm()
const items = ref([])
const loading = ref(false)
const showCreateModal = ref(false)
const editingId = ref('')
const saving = ref(false)
const titleInput = ref(null)
const draftTitle = ref('')
const draftBody = ref('')
const draftDueAt = ref('')
const draftTags = ref('')
const draftStatus = ref('pending')

const statusOptions = computed(() => [
  { label: t('userTodos.allStatuses'), value: '' },
  { label: t('userTodos.statusInProgress'), value: 'in_progress' },
  { label: t('userTodos.statusPending'), value: 'pending' },
  { label: t('userTodos.statusCompleted'), value: 'completed' },
  { label: t('userTodos.statusCancelled'), value: 'cancelled' },
])

const editStatusOptions = computed(() => [
  { label: t('userTodos.statusPending'), value: 'pending' },
  { label: t('userTodos.statusInProgress'), value: 'in_progress' },
  { label: t('userTodos.statusCompleted'), value: 'completed' },
  { label: t('userTodos.statusCancelled'), value: 'cancelled' },
])

const {
  query: todoQuery,
  filters: todoFilters,
  page: todoPage,
  pageSize: todoPageSize,
  total: todoFilteredTotal,
  pageCount: todoPageCount,
  pageItems: todoPageItems,
  selectedCount: todoSelectedCount,
  selectedIds: todoSelectedIds,
  isSelected: todoIsSelected,
  toggle: todoToggle,
  clearSelection: todoClearSelection,
  selectPage: todoSelectPage,
  selectAllFiltered: todoSelectAllFiltered,
} = useClientList(
  computed(() => items.value),
  (item, q, f) => {
    const st = String(f.status || '')
    if (st && item.status !== st) return false
    if (!q) return true
    const hay = `${item.title || ''} ${item.body || ''} ${(item.tags || []).join(' ')}`.toLowerCase()
    return hay.includes(q)
  },
)
todoFilters.value = { status: '' }
todoPageSize.value = 20

// Live push: the agent or a cron job can create/complete todos; this panel
// has no poll, so without this it would only update on remount.
useLiveChannel('user_todos', () => { reload() }, { debounceMs: 200 })

async function reload() {
  loading.value = true
  try {
    items.value = await api.listUserTodos()
    for (const id of todoSelectedIds.value) {
      if (!items.value.some((item) => item.id === id)) todoToggle(id, false)
    }
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    loading.value = false
  }
}

async function openCreateModal() {
  editingId.value = ''
  draftTitle.value = ''
  draftBody.value = ''
  draftDueAt.value = ''
  draftTags.value = ''
  draftStatus.value = 'pending'
  showCreateModal.value = true
  await nextTick()
  titleInput.value?.focus()
}

async function openEditModal(item) {
  editingId.value = item.id
  draftTitle.value = item.title || ''
  draftBody.value = item.body || ''
  draftDueAt.value = toDateTimeLocal(item.due_at)
  draftTags.value = (item.tags || []).join(', ')
  draftStatus.value = item.status || 'pending'
  showCreateModal.value = true
  await nextTick()
  titleInput.value?.focus()
}

function closeEditor() {
  if (saving.value) return
  showCreateModal.value = false
}

async function saveTodo() {
  if (!draftTitle.value.trim()) return
  saving.value = true
  try {
    const tagsArr = draftTags.value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean)
    const dueAt = draftDueAt.value ? Math.floor(new Date(draftDueAt.value).getTime() / 1000) : null
    const payload = {
      title: draftTitle.value.trim(),
      body: draftBody.value.trim(),
      tags: tagsArr,
      status: draftStatus.value,
    }
    if (dueAt && Number.isFinite(dueAt)) payload.due_at = dueAt
    if (editingId.value) {
      if (!dueAt) payload.clear_due_at = true
      await api.updateUserTodo(editingId.value, payload)
    } else {
      await api.createUserTodo(payload)
    }
    showCreateModal.value = false
    showToast(t(editingId.value ? 'userTodos.saved' : 'userTodos.added'))
    await reload()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  } finally {
    saving.value = false
  }
}

async function setStatus(item, st) {
  try {
    await api.updateUserTodo(item.id, { status: st })
    await reload()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function remove(item) {
  const ok = await confirmDialog({
    title: t('userTodos.deleteTitle'),
    content: t('userTodos.deleteContent', { title: item.title }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    await api.deleteUserTodo(item.id)
    showToast(t('userTodos.deleted'))
    await reload()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function batchComplete() {
  const selected = items.value.filter((item) => todoSelectedIds.value.includes(item.id))
  const ids = selected.filter((item) => item.status !== 'completed').map((item) => item.id)
  if (!ids.length) return
  try {
    for (const id of ids) {
      await api.updateUserTodo(id, { status: 'completed' })
    }
    todoClearSelection()
    showToast(t('userTodos.batchCompleted'))
    await reload()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

async function batchDelete() {
  const ids = Array.from(todoSelectedIds.value)
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('userTodos.batchDeleteTitle'),
    content: t('userTodos.batchDeleteContent', { count: ids.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    for (const id of ids) {
      await api.deleteUserTodo(id)
    }
    todoClearSelection()
    showToast(t('userTodos.batchDeleted'))
    await reload()
  } catch (e) {
    showToast(t('toast.error', { msg: e.message || e }), 'error')
  }
}

function formatTs(ts) {
  if (!ts) return ''
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch {
    return String(ts)
  }
}

function toDateTimeLocal(ts) {
  if (!ts) return ''
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function statusPillClass(st) {
  if (st === 'completed') return 'pill done'
  if (st === 'in_progress') return 'pill warn'
  if (st === 'cancelled') return 'pill err'
  return 'pill'
}

function statusText(st) {
  if (st === 'completed') return t('userTodos.statusCompleted')
  if (st === 'in_progress') return t('userTodos.statusInProgress')
  if (st === 'cancelled') return t('userTodos.statusCancelled')
  return t('userTodos.statusPending')
}

function clearFilters() {
  todoQuery.value = ''
  todoFilters.value = { status: '' }
}

function handleEmptyAction() {
  if (items.value.length) clearFilters()
  else openCreateModal()
}

function handleRowKeydown(event, index, item) {
  if (event.target !== event.currentTarget) return
  if (event.key === 'Enter') {
    event.preventDefault()
    openEditModal(item)
    return
  }
  if (event.key === ' ') {
    event.preventDefault()
    todoToggle(item.id, !todoIsSelected(item.id))
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  event.preventDefault()
  const rows = event.currentTarget.parentElement?.querySelectorAll('.todo-row') || []
  const next = event.key === 'ArrowDown'
    ? Math.min(index + 1, rows.length - 1)
    : Math.max(index - 1, 0)
  rows[next]?.focus()
}

onMounted(reload)
</script>

<template>
  <div class="page list-page user-todos-page">
    <div class="page-head">
      <div>
        <h1>{{ t('userTodos.title') }}</h1>
        <p>{{ t('userTodos.subtitle', { count: items.length }) }}</p>
      </div>
      <div class="row-gap-sm">
        <button type="button" class="ghost" :disabled="loading" @click="reload">{{ t('common.refresh') }}</button>
        <button type="button" class="primary" @click="openCreateModal">
          + {{ t('userTodos.newTodo') }}
        </button>
      </div>
    </div>

    <WorkbenchModal
      :open="showCreateModal"
      size="md"
      :title="t(editingId ? 'userTodos.editTitle' : 'userTodos.newTitle')"
      :description="t(editingId ? 'userTodos.editDescription' : 'userTodos.newDescription')"
      :busy="saving"
      :close-label="t('userTodos.closeEditor')"
      @close="closeEditor"
    >
      <div
        class="form"
        @keydown.ctrl.enter.stop.prevent="saveTodo"
        @keydown.meta.enter.stop.prevent="saveTodo"
      >
          <div class="field">
            <label>{{ t('userTodos.titleLabel') }}</label>
            <input ref="titleInput" type="text" v-model="draftTitle" :placeholder="t('userTodos.titlePlaceholder')" />
          </div>
          <div class="field">
            <label>{{ t('userTodos.bodyLabel') }}</label>
            <textarea v-model="draftBody" rows="4" :placeholder="t('userTodos.bodyPlaceholder')"></textarea>
          </div>
          <div class="todo-form-grid">
            <div class="field">
              <label>{{ t('userTodos.statusLabel') }}</label>
              <NSelect
                v-model:value="draftStatus"
                size="medium"
                :options="editStatusOptions"
              />
            </div>
            <div class="field">
              <label>{{ t('userTodos.dueLabel') }}</label>
              <input type="datetime-local" v-model="draftDueAt" />
            </div>
          </div>
          <div class="field">
            <label>{{ t('userTodos.tagsLabel') }}</label>
            <input type="text" v-model="draftTags" :placeholder="t('userTodos.tagsPlaceholder')" />
          </div>
      </div>
      <template #footer>
        <div class="todo-modal-actions">
            <span class="todo-key-hint">{{ t('userTodos.saveShortcut') }}</span>
            <button class="ghost sm" type="button" :disabled="saving" @click="closeEditor">{{ t('common.cancel') }}</button>
            <button class="primary sm btn" type="button" :disabled="!draftTitle.trim() || saving" @click="saveTodo">
              {{ saving ? t('userTodos.saving') : t(editingId ? 'userTodos.saveChanges' : 'userTodos.addTodo') }}
            </button>
        </div>
      </template>
    </WorkbenchModal>

    <ListToolbar v-model="todoQuery" :placeholder="t('userTodos.searchPlaceholder')">
      <template #filters>
        <ListSelect v-model="todoFilters.status" :options="statusOptions" min-width="120px" />
      </template>
    </ListToolbar>

    <ListBatchBar
      :count="todoSelectedCount"
      :total="todoFilteredTotal"
      @select-page="todoSelectPage(true)"
      @select-all="todoSelectAllFiltered(true)"
      @clear="todoClearSelection()"
    >
      <button type="button" class="ghost list-batch-btn" @click="batchComplete">{{ t('common.batchComplete') }}</button>
      <button type="button" class="danger list-batch-btn" @click="batchDelete">{{ t('common.delete') }}</button>
    </ListBatchBar>

    <PageEmpty
      v-if="!todoFilteredTotal && !loading"
      :title="items.length ? t('common.noMatch') : t('userTodos.emptyTitle')"
      :description="items.length ? t('common.noMatchHint') : t('userTodos.emptyDescription')"
      :action-label="t(items.length ? 'userTodos.clearFilters' : 'userTodos.addTodo')"
      @action="handleEmptyAction"
    />

    <div v-else-if="loading" class="todo-loading" role="status">{{ t('common.loading') }}</div>

    <template v-else>
      <div class="list-body">
        <ListRow
          v-for="(item, index) in todoPageItems"
          :key="item.id"
          class="todo-row"
          :selected="todoIsSelected(item.id)"
          :checked="todoIsSelected(item.id)"
          :aria-label="t('userTodos.selectAria', { title: item.title })"
          tabindex="0"
          @update:checked="todoToggle(item.id, $event)"
          @open="openEditModal(item)"
          @keydown="handleRowKeydown($event, index, item)"
        >
          <div class="todo-item-main">
            <div class="t">{{ item.title }}</div>
            <div v-if="item.body" class="m">{{ item.body }}</div>
            <div class="meta">
              <span :class="statusPillClass(item.status)">{{ statusText(item.status) }}</span>
              <span v-if="item.tags && item.tags.length" class="mono">{{ t('userTodos.tagLabel') }}: {{ item.tags.join(', ') }}</span>
              <span v-if="item.due_at" class="mono">{{ t('userTodos.dueAt', { time: formatTs(item.due_at) }) }}</span>
              <span v-if="item.updated_at" class="mono">{{ t('userTodos.updatedAt', { time: formatTs(item.updated_at) }) }}</span>
            </div>
          </div>
          <template #actions>
            <button v-if="item.status === 'pending'" class="ghost sm" type="button" @click.stop="setStatus(item, 'in_progress')">{{ t('userTodos.start') }}</button>
            <button v-else-if="item.status === 'in_progress'" class="ghost sm" type="button" @click.stop="setStatus(item, 'completed')">{{ t('userTodos.complete') }}</button>
            <button v-else class="ghost sm" type="button" @click.stop="setStatus(item, 'pending')">{{ t('userTodos.reopen') }}</button>
            <details class="todo-more" @click.stop>
              <summary :aria-label="t('userTodos.moreActions')">•••</summary>
              <div class="todo-more-menu">
                <button v-if="item.status === 'pending'" type="button" @click="setStatus(item, 'completed')">{{ t('userTodos.markComplete') }}</button>
                <button type="button" @click="openEditModal(item)">{{ t('userTodos.edit') }}</button>
                <button type="button" class="danger" @click="remove(item)">{{ t('common.delete') }}</button>
              </div>
            </details>
          </template>
        </ListRow>
      </div>
      <ListPager
        v-model:page="todoPage"
        v-model:page-size="todoPageSize"
        :page-count="todoPageCount"
        :total="todoFilteredTotal"
      />
    </template>
  </div>
</template>

<style scoped>
.user-todos-page { width: 100%; max-width: none; }
.todo-loading {
  flex: 1;
  display: grid;
  place-items: center;
  color: var(--muted);
}
.todo-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.todo-item-main { min-width: 0; }
.todo-item-main .m {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  white-space: pre-wrap;
}
.todo-more {
  position: relative;
}
.todo-more > summary {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  letter-spacing: 1px;
  list-style: none;
}
.todo-more > summary::-webkit-details-marker { display: none; }
.todo-more > summary:hover,
.todo-more[open] > summary {
  background: var(--panel-hover);
  color: var(--fg-strong);
}
.todo-more-menu {
  position: absolute;
  z-index: 30;
  top: calc(100% + 4px);
  right: 0;
  display: grid;
  min-width: 126px;
  padding: 5px;
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  background: var(--card-bg);
  box-shadow: var(--shadow-sm);
}
.todo-more-menu button {
  min-height: 30px;
  padding: 0 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  text-align: left;
}
.todo-more-menu button:hover { background: var(--panel-hover); }
.todo-more-menu button.danger { color: var(--err); }
.todo-modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(4px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
}
.todo-modal {
  width: min(560px, calc(100vw - 32px));
  max-height: min(760px, calc(100vh - 48px));
  overflow: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 0;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
}
.todo-modal-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--border);
}
.todo-modal-head h3 { margin: 0; font-size: 16px; }
.todo-modal-head p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
.todo-modal .form { padding: 16px 18px 18px; }
.todo-form-grid {
  display: grid;
  grid-template-columns: minmax(130px, .7fr) minmax(210px, 1.3fr);
  gap: 12px;
}
.todo-modal select {
  width: 100%;
  min-height: 34px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-sunken);
  color: var(--fg);
  padding: 0 9px;
}
.todo-modal-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}
.todo-key-hint {
  margin-right: auto;
  color: var(--muted);
  font-size: 11px;
}
@media (max-width: 720px) {
  .user-todos-page { padding: 14px; }
  .user-todos-page .page-head { align-items: flex-start; }
  .user-todos-page .page-head .row-gap-sm { width: 100%; }
  .todo-modal-backdrop { align-items: flex-end; }
  .todo-modal {
    width: 100%;
    max-height: calc(100vh - var(--titlebar-h, 0px) - 12px);
    border-radius: 14px 14px 0 0;
  }
  .todo-form-grid { grid-template-columns: 1fr; gap: 0; }
  .todo-modal-actions { flex-wrap: wrap; }
  .todo-key-hint { flex-basis: 100%; }
  .todo-modal-actions .btn,
  .todo-modal-actions .ghost { flex: 1; }
}
</style>
