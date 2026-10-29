<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { NButton, NEmpty, NInput, NSelect, NTag, useDialog, useMessage } from 'naive-ui'
import { api } from '../api.js'
import { route } from '../router.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useClientList } from '../composables/useClientList.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import ListBatchBar from './list/ListBatchBar.vue'
import ListPager from './list/ListPager.vue'
import ListToolbar from './list/ListToolbar.vue'
import ListSelect from './list/ListSelect.vue'
import PageEmpty from './list/PageEmpty.vue'
import MarkdownText from './assistant/MarkdownText.vue'

const confirmDialog = useConfirm()

const entries = ref([])
const loading = ref(false)
const root = ref('')
const error = ref('')

// Filter state
const queryText = ref('')
const tagFilter = ref('')
const includeSuperseded = ref(false)
const includeTrashed = ref(false)

// Selection / edit state
const selectedId = ref('')
const editing = ref(false)
const draft = ref(emptyDraft())
const fullEntry = ref(null)
const saving = ref(false)
const dialog = useDialog()
const msg = useMessage()

// Trash statistics (lazy — populated when user toggles trash view)
const trashStats = ref({ count: 0, total_bytes: 0 })
const purging = ref(false)
// Passive banner shown when ``_trash/`` is larger than the warning threshold
// AND the user hasn't already toggled the trash view (otherwise the inline
// "Empty trash" button is right there and we don't need a banner too).
const TRASH_WARN_BYTES = 20 * 1024 * 1024
const trashBanner = ref({ visible: false, count: 0, total_bytes: 0 })

function emptyDraft() {
  return {
    title: '',
    body: '',
    tagsText: '',
    triggerText: '',
    appliesText: '',
    confidence: 'probable',
    reason: '',
  }
}

const confidenceOptions = computed(() => [
  { label: t('kb.confidence.confirmed'), value: 'confirmed' },
  { label: t('kb.confidence.probable'), value: 'probable' },
  { label: t('kb.confidence.hypothesis'), value: 'hypothesis' },
  { label: t('kb.confidence.experimental'), value: 'experimental' },
])

const allTags = computed(() => {
  const seen = new Set()
  for (const e of entries.value) {
    for (const tg of (e.tags || [])) seen.add(tg)
  }
  return Array.from(seen).sort()
})

const filtered = computed(() => {
  const q = (queryText.value || '').trim().toLowerCase()
  const tg = (tagFilter.value || '').trim().toLowerCase()
  return entries.value.filter((e) => {
    if (tg && !(e.tags || []).includes(tg)) return false
    if (!q) return true
    const hay = [
      e.title || '',
      (e.tags || []).join(' '),
      (e.trigger_terms || []).join(' '),
      (e.applies_to || []).join(' '),
      e.id || '',
    ].join(' ').toLowerCase()
    return hay.includes(q)
  })
})

const {
  page: kbPage,
  pageSize: kbPageSize,
  total: kbFilteredTotal,
  pageCount: kbPageCount,
  pageItems: kbPageItems,
  selectedCount: kbSelectedCount,
  selectedIds: kbSelectedIds,
  isSelected: kbIsSelected,
  toggle: kbToggle,
  clearSelection: kbClearSelection,
  selectPage: kbSelectPage,
  selectAllFiltered: kbSelectAllFiltered,
} = useClientList(filtered)
kbPageSize.value = 20

const selectedEntries = computed(() => entries.value.filter((entry) => kbSelectedIds.value.includes(entry.id)))
const selectedActiveCount = computed(() => selectedEntries.value.filter((entry) => !entry.trashed).length)
const selectedTrashedCount = computed(() => selectedEntries.value.filter((entry) => entry.trashed).length)
const realChangelog = computed(() => {
  const raw = fullEntry.value?.changelog
  if (!Array.isArray(raw)) return []
  return raw.filter((entry) => entry && typeof entry === 'object' && (
    entry.v != null || entry.at || entry.action || entry.reason
  ))
})

// Live push: the agent writes KB entries via tools; this panel has no poll,
// so without this new/updated entries would only show on remount.
useLiveChannel('knowledge', () => { load() }, { debounceMs: 200 })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.kbList({ includeSuperseded: includeSuperseded.value, includeTrashed: includeTrashed.value })
    if (res && res.ok) {
      entries.value = res.entries || []
      root.value = res.root || ''
      for (const id of kbSelectedIds.value) {
        if (!entries.value.some((entry) => entry.id === id)) kbToggle(id, false)
      }
    } else {
      entries.value = []
      error.value = (res && res.error) || 'failed'
    }
  } catch (e) {
    entries.value = []
    error.value = String(e && e.message || e)
  } finally {
    loading.value = false
  }
  // Refresh trash stats whenever the trash view is on so the "Empty trash"
  // button can show an accurate count + size next to it.
  if (includeTrashed.value) {
    try {
      const r = await api.kbListTrash()
      if (r && r.ok) {
        trashStats.value = { count: r.count || 0, total_bytes: r.total_bytes || 0 }
      }
    } catch {
      trashStats.value = { count: 0, total_bytes: 0 }
    }
  } else {
    trashStats.value = { count: 0, total_bytes: 0 }
  }
  if (selectedId.value && !entries.value.some((entry) => entry.id === selectedId.value) && route.name !== 'knowledge_entry') {
    selectedId.value = ''
    fullEntry.value = null
  }
  // If a route param points to an entry, select it.
  if (route.name === 'knowledge_entry' && route.params && route.params.id) {
    await selectEntry(route.params.id)
  } else if (!editing.value && !selectedId.value && entries.value.length) {
    await selectEntry(entries.value[0].id)
  }
}

function formatBytes(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / 1024 / 1024).toFixed(2)} MB`
}

async function purgeTrash() {
  if (purging.value) return
  const stats = trashStats.value || { count: 0, total_bytes: 0 }
  if (!stats.count) {
    msg.info(t('kb.purge.empty'))
    return
  }
  const confirmText = t('kb.purge.confirm', { count: stats.count, size: formatBytes(stats.total_bytes) })
  // Inline confirm via naive-ui dialog
  await new Promise((resolve) => {
    dialog.warning({
      title: t('kb.purge.title'),
      content: confirmText,
      positiveText: t('kb.purge.yes'),
      negativeText: t('kb.purge.no'),
      onPositiveClick: async () => {
        purging.value = true
        try {
          const res = await api.kbPurgeTrash({ olderThanDays: null })
          if (res && res.ok) {
            msg.success(t('kb.purge.done', { count: (res.removed || []).length, size: formatBytes(res.freed_bytes || 0) }))
            await load()
          } else {
            msg.error((res && res.error) || 'failed')
          }
        } catch (e) {
          msg.error(String(e && e.message || e))
        } finally {
          purging.value = false
          resolve()
        }
      },
      onNegativeClick: () => resolve(),
      onClose: () => resolve(),
      onMaskClick: () => resolve(),
      onEsc: () => resolve(),
    })
  })
}

async function selectEntry(id) {
  if (!id) {
    selectedId.value = ''
    fullEntry.value = null
    editing.value = false
    return
  }
  error.value = ''
  selectedId.value = id
  editing.value = false
  fullEntry.value = null
  try {
    const res = await api.kbGet(id)
    if (res && res.ok) {
      fullEntry.value = res.entry || null
    } else {
      error.value = (res && res.error) || 'failed'
    }
  } catch (e) {
    error.value = String(e && e.message || e)
  }
}

function startEdit() {
  if (!fullEntry.value) return
  const e = fullEntry.value
  draft.value = {
    title: e.title || '',
    body: e.body || '',
    tagsText: (e.tags || []).join(', '),
    triggerText: (e.trigger_terms || []).join(', '),
    appliesText: (e.applies_to || []).join('\n'),
    confidence: e.confidence || 'probable',
    reason: '',
  }
  editing.value = true
}

function startNew() {
  selectedId.value = ''
  fullEntry.value = null
  draft.value = emptyDraft()
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  if (selectedId.value) {
    selectEntry(selectedId.value)
  } else {
    draft.value = emptyDraft()
  }
}

function splitCSV(s) {
  return String(s || '')
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean)
}

async function saveDraft() {
  const d = draft.value
  if (!d.title.trim()) {
    msg.error(t('kb.form.titleRequired'))
    return
  }
  if ((d.body || '').trim().length < 24) {
    msg.error(t('kb.form.bodyTooShort'))
    return
  }
  saving.value = true
  try {
    const payload = {
      title: d.title.trim(),
      body: d.body,
      tags: splitCSV(d.tagsText),
      trigger_terms: splitCSV(d.triggerText),
      applies_to: splitCSV(d.appliesText),
      confidence: d.confidence,
      reason: d.reason || t('kb.form.defaultReasonUI'),
    }
    let res
    if (fullEntry.value && fullEntry.value.id) {
      res = await api.kbUpdate(fullEntry.value.id, payload)
    } else {
      res = await api.kbSave(payload)
    }
    if (!res || !res.ok) {
      msg.error((res && res.error) || 'failed')
      return
    }
    msg.success(t('kb.form.saved'))
    editing.value = false
    await load()
    if (res.entry && res.entry.id) {
      await selectEntry(res.entry.id)
    }
  } catch (e) {
    msg.error(String(e && e.message || e))
  } finally {
    saving.value = false
  }
}

async function deleteEntry(entry) {
  if (!entry || !entry.id) return
  dialog.warning({
    title: t('kb.deleteConfirm.title'),
    content: t('kb.deleteConfirm.body', { title: entry.title || entry.id }),
    positiveText: t('kb.deleteConfirm.ok'),
    negativeText: t('kb.deleteConfirm.cancel'),
    onPositiveClick: async () => {
      try {
        const res = await api.kbDelete(entry.id, t('kb.deleteConfirm.reason'))
        if (res && res.ok) {
          msg.success(t('kb.deleteConfirm.done'))
          kbToggle(entry.id, false)
          if (selectedId.value === entry.id) {
            selectedId.value = ''
            fullEntry.value = null
          }
          await load()
        } else {
          msg.error((res && res.error) || 'failed')
        }
      } catch (e) {
        msg.error(String(e && e.message || e))
      }
    },
  })
}

async function batchDeleteEntries() {
  const ids = selectedEntries.value.filter((entry) => !entry.trashed).map((entry) => entry.id)
  if (!ids.length) return
  const ok = await confirmDialog({
    title: t('kb.deleteConfirm.title'),
    content: t('kb.deleteConfirm.batch', { n: ids.length }),
    positiveText: t('kb.deleteConfirm.ok'),
    negativeText: t('kb.deleteConfirm.cancel'),
    type: 'error',
  })
  if (!ok) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.kbDelete(id, t('kb.deleteConfirm.reason'))
      if (res?.ok) {
        okN++
        if (selectedId.value === id) {
          selectedId.value = ''
          fullEntry.value = null
        }
      } else fail++
    } catch { fail++ }
  }
  kbClearSelection()
  await load()
  showToast(fail ? t('common.batchPartial', { ok: okN, fail }) : t('common.batchDone', { ok: okN, total: ids.length }), fail ? 'error' : 'ok')
}

async function batchRestoreEntries() {
  const ids = selectedEntries.value.filter((entry) => entry.trashed).map((entry) => entry.id)
  if (!ids.length) return
  let okN = 0
  let fail = 0
  for (const id of ids) {
    try {
      const res = await api.kbRestore(id)
      if (res?.ok) okN++
      else fail++
    } catch {
      fail++
    }
  }
  kbClearSelection()
  await load()
  showToast(fail ? t('kb.restore.partial', { ok: okN, fail }) : t('kb.restore.batchDone', { n: okN }), fail ? 'error' : 'ok')
}

async function hardDeleteEntries(ids) {
  const safeIds = Array.from(new Set((ids || []).filter(Boolean)))
  if (!safeIds.length) return
  const ok = await confirmDialog({
    title: t('kb.hardDelete.title'),
    content: t('kb.hardDelete.content', { n: safeIds.length }),
    positiveText: t('kb.hardDelete.action'),
    negativeText: t('common.cancel'),
    type: 'error',
  })
  if (!ok) return
  try {
    const res = await api.kbPurgeTrash({ ids: safeIds })
    if (!res?.ok) {
      msg.error(res?.error || 'failed')
      return
    }
    if (safeIds.includes(selectedId.value)) {
      selectedId.value = ''
      fullEntry.value = null
    }
    kbClearSelection()
    msg.success(t('kb.hardDelete.done', { n: (res.removed || []).length }))
    await load()
  } catch (e) {
    msg.error(String(e?.message || e))
  }
}

async function restoreEntry(entry) {
  if (!entry || !entry.id) return
  try {
    const res = await api.kbRestore(entry.id)
    if (res && res.ok) {
      msg.success(t('kb.restore.done'))
      await load()
      await selectEntry(entry.id)
    } else {
      msg.error((res && res.error) || 'failed')
    }
  } catch (e) {
    msg.error(String(e && e.message || e))
  }
}

function clearKnowledgeFilters() {
  queryText.value = ''
  tagFilter.value = ''
}

function handleKnowledgeEmptyAction() {
  if (entries.value.length) clearKnowledgeFilters()
  else startNew()
}

function handleEntryKeydown(event, index, entry) {
  if (event.target !== event.currentTarget) return
  if (event.key === 'Enter') {
    event.preventDefault()
    selectEntry(entry.id)
    return
  }
  if (event.key === ' ') {
    event.preventDefault()
    kbToggle(entry.id, !kbIsSelected(entry.id))
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  event.preventDefault()
  const rows = event.currentTarget.parentElement?.querySelectorAll('.kb-list-item') || []
  const next = event.key === 'ArrowDown'
    ? Math.min(index + 1, rows.length - 1)
    : Math.max(index - 1, 0)
  rows[next]?.focus()
}

function openInFolder() {
  if (!root.value) return
  api.openFolder(root.value).catch(() => {})
}

async function checkTrashBanner() {
  // Only check when the user is on the regular active-view (no trash toggle)
  // so we don't double-prompt.
  if (includeTrashed.value) {
    trashBanner.value = { visible: false, count: 0, total_bytes: 0 }
    return
  }
  try {
    const r = await api.kbListTrash()
    if (r && r.ok && (r.total_bytes || 0) >= TRASH_WARN_BYTES) {
      trashBanner.value = { visible: true, count: r.count || 0, total_bytes: r.total_bytes || 0 }
    } else {
      trashBanner.value = { visible: false, count: 0, total_bytes: 0 }
    }
  } catch {
    trashBanner.value = { visible: false, count: 0, total_bytes: 0 }
  }
}

function openTrashView() {
  includeTrashed.value = true
  trashBanner.value = { visible: false, count: 0, total_bytes: 0 }
  load()
}

onMounted(() => {
  load()
  checkTrashBanner()
})

watch(() => route.params && route.params.id, (newId) => {
  if (route.name === 'knowledge_entry' && newId) {
    selectEntry(newId)
  }
})
</script>

<template>
  <section class="kb-page">
    <header class="page-head">
      <div>
        <h2>{{ t('kb.title') }}</h2>
        <p v-if="root" class="kb-root">
          <code @click="openInFolder" :title="t('kb.openInFolder')">{{ root }}</code>
        </p>
      </div>
      <div class="kb-actions row-gap-sm">
        <NButton size="small" @click="load">{{ t('kb.refresh') }}</NButton>
        <NButton size="small" type="primary" @click="startNew">{{ t('kb.new') }}</NButton>
      </div>
    </header>

    <div v-if="trashBanner.visible" class="kb-trash-banner">
      <span class="kb-trash-banner-icon">!</span>
      <span class="kb-trash-banner-text">
        {{ t('kb.purge.bannerText', { count: trashBanner.count, size: formatBytes(trashBanner.total_bytes) }) }}
      </span>
      <NButton size="tiny" type="warning" ghost @click="openTrashView">{{ t('kb.purge.bannerCta') }}</NButton>
      <button class="kb-trash-banner-dismiss" :title="t('kb.purge.bannerDismiss')" @click="trashBanner.visible = false">×</button>
    </div>

    <ListToolbar v-model="queryText" :placeholder="t('kb.searchPlaceholder')">
      <template #filters>
        <ListSelect
          v-model="tagFilter"
          :options="[{ label: t('kb.allTags'), value: '' }, ...allTags.map((tg) => ({ label: `tag: ${tg}`, value: tg }))]"
          min-width="130px"
        />
        <label class="kb-check">
          <input type="checkbox" v-model="includeSuperseded" @change="load" />
          <span>{{ t('kb.includeSuperseded') }}</span>
        </label>
        <label class="kb-check">
          <input type="checkbox" v-model="includeTrashed" @change="load" />
          <span>{{ t('kb.includeTrashed') }}</span>
        </label>
        <button
          v-if="includeTrashed && trashStats.count > 0"
          type="button"
          class="ghost sm danger"
          :disabled="purging"
          @click="purgeTrash"
        >{{ t('kb.purge.button') }}</button>
      </template>
    </ListToolbar>

    <div v-if="error" class="kb-error" role="alert">{{ error }}</div>

    <div class="kb-body">
      <div class="kb-list">
        <ListBatchBar
          :count="kbSelectedCount"
          :total="kbFilteredTotal"
          @select-page="kbSelectPage(true)"
          @select-all="kbSelectAllFiltered(true)"
          @clear="kbClearSelection()"
        >
          <button v-if="selectedActiveCount" type="button" class="ghost tiny danger" @click="batchDeleteEntries">{{ t('kb.deleteButton') }}</button>
          <button v-if="selectedTrashedCount" type="button" class="ghost tiny" @click="batchRestoreEntries">{{ t('kb.restore.selected') }}</button>
          <button v-if="selectedTrashedCount" type="button" class="danger tiny" @click="hardDeleteEntries(selectedEntries.filter((entry) => entry.trashed).map((entry) => entry.id))">{{ t('kb.hardDelete.selected') }}</button>
        </ListBatchBar>
        <div v-if="loading" class="hint">{{ t('kb.loading') }}</div>
        <PageEmpty
          v-else-if="kbFilteredTotal === 0"
          :title="entries.length ? t('kb.noMatch') : t('kb.empty')"
          :description="entries.length ? t('kb.noMatchHint') : t('kb.emptyHint')"
          :action-label="entries.length ? t('kb.clearFilters') : t('kb.new')"
          @action="handleKnowledgeEmptyAction"
        />
        <template v-else>
          <div class="kb-list-items list-body">
            <div
              v-for="(e, index) in kbPageItems"
              :key="e.id"
              :class="['kb-list-item', { active: e.id === selectedId, selected: kbIsSelected(e.id), trashed: e.trashed, superseded: e.superseded_by }]"
              tabindex="0"
              role="option"
              :aria-selected="e.id === selectedId"
              @click="selectEntry(e.id)"
              @keydown="handleEntryKeydown($event, index, e)"
            >
              <div class="kb-list-title">
                <input
                  class="list-row-check"
                  type="checkbox"
                  :checked="kbIsSelected(e.id)"
                  @click.stop
                  @change="kbToggle(e.id, $event.target.checked)"
                />
                <span>{{ e.title }}</span>
                <NTag v-if="e.trashed" size="tiny" type="error">{{ t('kb.list.trashed') }}</NTag>
                <NTag v-else-if="e.superseded_by" size="tiny" type="warning">{{ t('kb.list.superseded') }}</NTag>
                <NTag v-else size="tiny" :type="e.confidence === 'confirmed' ? 'success' : e.confidence === 'experimental' ? 'warning' : 'default'">{{ e.confidence }}</NTag>
              </div>
              <div class="kb-list-meta">
                <code class="kb-id">{{ e.id }}</code>
                <span v-if="e.updated_at" class="hint">· {{ e.updated_at }}</span>
              </div>
              <div class="kb-list-tags" v-if="e.tags && e.tags.length">
                <NTag v-for="tg in e.tags.slice(0, 5)" :key="tg" size="tiny" :bordered="false">{{ tg }}</NTag>
              </div>
            </div>
          </div>
          <ListPager
            v-model:page="kbPage"
            v-model:page-size="kbPageSize"
            :page-count="kbPageCount"
            :total="kbFilteredTotal"
          />
        </template>
      </div>

      <div class="kb-detail">
        <div v-if="!editing && fullEntry" class="kb-view">
          <div class="kb-view-head">
            <h3>{{ fullEntry.title }}</h3>
            <div class="kb-view-actions">
              <NButton size="small" v-if="fullEntry.trashed" @click="restoreEntry(fullEntry)">{{ t('kb.restoreButton') }}</NButton>
              <NButton size="small" v-if="fullEntry.trashed" type="error" ghost @click="hardDeleteEntries([fullEntry.id])">{{ t('kb.hardDelete.action') }}</NButton>
              <template v-else>
                <NButton size="small" @click="startEdit">{{ t('kb.editButton') }}</NButton>
                <NButton size="small" type="error" ghost @click="deleteEntry(fullEntry)">{{ t('kb.deleteButton') }}</NButton>
              </template>
            </div>
          </div>
          <div class="kb-view-meta">
            <code class="kb-id">{{ fullEntry.id }}</code>
            <span class="hint" v-if="fullEntry.version != null && fullEntry.version !== ''">· v{{ fullEntry.version }}</span>
            <span class="hint" v-if="fullEntry.updated_at">· {{ t('kb.meta.updatedAt') }} {{ fullEntry.updated_at }}</span>
            <NTag size="tiny" :type="fullEntry.confidence === 'confirmed' ? 'success' : fullEntry.confidence === 'experimental' ? 'warning' : 'default'">{{ fullEntry.confidence }}</NTag>
            <NTag v-for="tg in (fullEntry.tags || [])" :key="tg" size="tiny" :bordered="false">{{ tg }}</NTag>
          </div>
          <div v-if="fullEntry.applies_to && fullEntry.applies_to.length" class="kb-applies">
            <div class="kb-applies-title">{{ t('kb.meta.appliesTo') }}</div>
            <ul>
              <li v-for="(x, i) in fullEntry.applies_to" :key="i">{{ x }}</li>
            </ul>
          </div>
          <div v-if="fullEntry.trigger_terms && fullEntry.trigger_terms.length" class="kb-triggers">
            <span class="kb-applies-title">{{ t('kb.meta.triggerTerms') }}: </span>
            <code v-for="(x, i) in fullEntry.trigger_terms" :key="i">{{ x }}</code>
          </div>
          <MarkdownText :text="fullEntry.body || ''" class="kb-md" />
          <details v-if="realChangelog.length" class="kb-changelog">
            <summary>{{ t('kb.meta.changelog') }} ({{ realChangelog.length }})</summary>
            <ul>
              <li v-for="(c, i) in realChangelog" :key="i">
                <span v-if="c.v != null">v{{ c.v }} · </span><span v-if="c.at">{{ c.at }} · </span>{{ c.action || t('kb.changelogRecord') }}<span v-if="c.reason"> — {{ c.reason }}</span>
              </li>
            </ul>
          </details>
        </div>

        <div
          v-else-if="editing"
          class="kb-edit"
          @keydown.esc.stop.prevent="cancelEdit"
          @keydown.ctrl.enter.stop.prevent="saveDraft"
          @keydown.meta.enter.stop.prevent="saveDraft"
        >
          <div class="kb-view-head">
            <h3>{{ fullEntry && fullEntry.id ? t('kb.edit.titleExisting') : t('kb.edit.titleNew') }}</h3>
            <div class="kb-view-actions">
              <NButton size="small" @click="cancelEdit">{{ t('kb.edit.cancel') }}</NButton>
              <NButton size="small" type="primary" :loading="saving" @click="saveDraft">{{ t('kb.edit.save') }}</NButton>
            </div>
          </div>
          <div class="kb-form">
            <div class="kb-field">
              <label>{{ t('kb.edit.fieldTitle') }} *</label>
              <NInput v-model:value="draft.title" :placeholder="t('kb.edit.titlePlaceholder')" />
            </div>
            <div class="kb-field kb-field-row">
              <div style="flex:1">
                <label>{{ t('kb.edit.fieldTags') }}</label>
                <NInput v-model:value="draft.tagsText" :placeholder="t('kb.edit.tagsPlaceholder')" />
              </div>
              <div style="width:180px">
                <label>{{ t('kb.edit.fieldConfidence') }}</label>
                <NSelect v-model:value="draft.confidence" :options="confidenceOptions" />
              </div>
            </div>
            <div class="kb-field">
              <label>{{ t('kb.edit.fieldTrigger') }}</label>
              <NInput v-model:value="draft.triggerText" :placeholder="t('kb.edit.triggerPlaceholder')" />
            </div>
            <div class="kb-field">
              <label>{{ t('kb.edit.fieldApplies') }}</label>
              <NInput type="textarea" v-model:value="draft.appliesText" :placeholder="t('kb.edit.appliesPlaceholder')" :autosize="{ minRows: 2, maxRows: 4 }" />
            </div>
            <div class="kb-field">
              <label>{{ t('kb.edit.fieldBody') }} * ({{ t('kb.edit.markdown') }})</label>
              <NInput type="textarea" v-model:value="draft.body" :autosize="{ minRows: 12, maxRows: 30 }" :placeholder="t('kb.edit.bodyPlaceholder')" />
            </div>
            <div class="kb-field" v-if="fullEntry && fullEntry.id">
              <label>{{ t('kb.edit.fieldReason') }}</label>
              <NInput v-model:value="draft.reason" :placeholder="t('kb.edit.reasonPlaceholder')" />
            </div>
          </div>
        </div>

        <div v-else class="kb-placeholder">
          <NEmpty :description="t('kb.selectPlaceholder')" />
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.kb-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  gap: 14px;
  padding: 18px 20px;
}
.kb-toolbar,
.kb-trash-banner {
  flex-shrink: 0;
}
.kb-error {
  flex-shrink: 0;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--danger, #f55) 35%, var(--border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger, #f55) 8%, transparent);
  color: var(--danger, #f55);
  font-size: 12px;
}
.kb-root code {
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  font-size: 12px;
}
.kb-actions {
  display: flex;
  gap: 8px;
}
.kb-toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel-2, #252526) 72%, transparent);
}
.kb-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--muted, #888);
  cursor: pointer;
}
.kb-body {
  display: grid;
  grid-template-columns: minmax(280px, 310px) minmax(0, 1fr);
  gap: 14px;
  flex: 1;
  min-height: 0;
}
.kb-list,
.kb-detail {
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 14px;
  background: color-mix(in srgb, var(--panel-2, #252526) 72%, transparent);
  padding: 12px;
  min-height: 0;
}
.kb-list {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.kb-list > .list-batch {
  flex-shrink: 0;
}
.kb-list > .list-body,
.kb-list-items {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kb-list > .list-pager {
  flex-shrink: 0;
  margin-top: 8px;
}
.kb-detail {
  display: flex;
  flex-direction: column;
  overflow: auto;
  padding: 18px 20px;
}
.kb-list-item {
  padding: 10px 11px;
  border-radius: 9px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 120ms ease, border-color 120ms ease;
}
.kb-list-item:focus-visible {
  outline: 2px solid var(--accent, #4e8cff);
  outline-offset: -2px;
}
.kb-list-item:hover {
  background: color-mix(in srgb, var(--accent, #4e8cff) 8%, transparent);
}
.kb-list-item.active {
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 50%, transparent);
}
.kb-list-item.selected:not(.active) {
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 35%, transparent);
  background: color-mix(in srgb, var(--accent, #4e8cff) 8%, transparent);
}
.kb-list-item.trashed {
  opacity: 0.55;
}
.kb-list-item.superseded {
  opacity: 0.7;
}
.kb-list-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  font-size: 13px;
}
.kb-list-title span {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-list-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  margin-top: 2px;
}
.kb-list-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.kb-view-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}
.kb-view-head h3 {
  margin: 0;
  font-size: 16px;
}
.kb-view-actions {
  display: flex;
  gap: 6px;
}
.kb-view-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 12px;
  color: var(--muted, #888);
}
.kb-id {
  font-family: ui-monospace, monospace;
  font-size: 11px;
  opacity: 0.85;
}
.kb-applies {
  border-left: 2px solid var(--accent, #4e8cff);
  background: color-mix(in srgb, var(--accent, #4e8cff) 6%, transparent);
  padding: 6px 10px;
  border-radius: 0 4px 4px 0;
  margin-bottom: 10px;
}
.kb-applies-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted, #888);
}
.kb-applies ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.kb-triggers {
  margin-bottom: 10px;
  font-size: 12px;
}
.kb-triggers code {
  margin: 0 4px 4px 0;
  display: inline-block;
}
.kb-md {
  font-size: 13px;
  line-height: 1.55;
}
.kb-md :deep(pre) {
  background: var(--bg-sunken);
  border-color: var(--border);
  color: var(--fg);
}
.kb-changelog {
  margin-top: 16px;
  font-size: 12px;
  color: var(--muted, #888);
}
.kb-changelog summary {
  cursor: pointer;
}
.kb-changelog ul {
  margin: 6px 0;
  padding-left: 18px;
}
.kb-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}
.kb-field label {
  display: block;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted, #888);
  margin-bottom: 4px;
}
.kb-field-row {
  display: flex;
  gap: 12px;
}
.kb-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

/* --- Trash / purge UI ------------------------------------------------ */
.kb-trash-stats {
  font-size: 12px;
  color: var(--muted, #888);
  margin-left: 4px;
}
.kb-trash-stats.warning {
  color: var(--warn, #f0a020);
  font-weight: 500;
}
.kb-trash-warn {
  font-weight: 600;
}
.kb-trash-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  margin: 8px 0;
  background: color-mix(in srgb, var(--warn, #f0a020) 12%, transparent);
  border-left: 3px solid var(--warn, #f0a020);
  border-radius: 4px;
  font-size: 12.5px;
}
.kb-trash-banner-icon {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--warn, #f0a020);
  color: #000;
  font-weight: 700;
  font-size: 12px;
  flex-shrink: 0;
}
.kb-trash-banner-text {
  flex: 1;
  min-width: 0;
}
.kb-trash-banner-dismiss {
  background: transparent;
  border: 0;
  color: var(--muted, #888);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.kb-trash-banner-dismiss:hover {
  color: var(--fg, #fff);
}
@media (max-width: 980px) {
  .kb-page {
    padding: 18px;
  }
  .kb-body {
    grid-template-columns: minmax(220px, 270px) minmax(0, 1fr);
    gap: 10px;
  }
}
@media (max-width: 760px) {
  .kb-page {
    padding: 12px;
    gap: 10px;
  }
  .kb-page .page-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .kb-actions { width: 100%; }
  .kb-actions :deep(.n-button) { flex: 1; }
  .kb-trash-banner { align-items: flex-start; flex-wrap: wrap; }
  .kb-trash-banner-text { flex-basis: calc(100% - 32px); }
  .kb-body {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(230px, 42%) minmax(260px, 1fr);
    gap: 10px;
  }
  .kb-list,
  .kb-detail { border-radius: 10px; }
  .kb-detail { padding: 14px; }
  .kb-view-head { flex-direction: column; }
  .kb-view-actions {
    width: 100%;
    flex-wrap: wrap;
  }
  .kb-field-row { flex-direction: column; }
  .kb-field-row > div { width: 100% !important; }
}
</style>
