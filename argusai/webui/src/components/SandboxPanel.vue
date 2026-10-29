<template>
  <!-- Floating sandbox workspace: draggable/resizable panel + split tree/preview. -->
  <Teleport to="body">
    <aside
      v-if="state.open"
      class="sandbox-workspace"
      :class="{ maximized: isMaximized }"
      :style="panelStyle"
      role="dialog"
      aria-modal="false"
      aria-labelledby="sandbox-workspace-title"
    >
      <div class="sb-resize-handle north" @pointerdown="startPanelResize('n', $event)"></div>
      <div class="sb-resize-handle south" @pointerdown="startPanelResize('s', $event)"></div>
      <div class="sb-resize-handle east" @pointerdown="startPanelResize('e', $event)"></div>
      <div class="sb-resize-handle west" @pointerdown="startPanelResize('w', $event)"></div>
      <div class="sb-resize-handle northwest" @pointerdown="startPanelResize('nw', $event)"></div>
      <div class="sb-resize-handle northeast" @pointerdown="startPanelResize('ne', $event)"></div>
      <div class="sb-resize-handle southwest" @pointerdown="startPanelResize('sw', $event)"></div>
      <div class="sb-resize-handle southeast" @pointerdown="startPanelResize('se', $event)"></div>

      <header
        class="sb-head sb-head-draggable"
        :title="t('explorer.dragTitle')"
        @pointerdown="startPanelDrag"
      >
        <div class="sb-head-title">
          <span class="sb-head-icon" aria-hidden="true">
            <svg viewBox="0 0 18 18">
              <path d="M2.5 5.2h5l1.4 1.5h6.6v7.1a1.7 1.7 0 0 1-1.7 1.7H4.2a1.7 1.7 0 0 1-1.7-1.7V5.2Z" />
              <path d="M2.5 5.2V4.3a1.7 1.7 0 0 1 1.7-1.7h3l1.3 1.5h5.3a1.7 1.7 0 0 1 1.7 1.7v.9" />
            </svg>
          </span>
          <span class="sb-head-copy">
            <strong id="sandbox-workspace-title">{{ t('explorer.title') }}</strong>
            <small>{{ t('explorer.subtitle') }}</small>
          </span>
          <span v-if="rootSummary" class="sb-head-count">{{ rootSummary }}</span>
        </div>
        <div class="sb-head-actions" data-no-drag>
          <button
            type="button"
            class="ghost icon sb-head-iconbtn"
            :title="isMaximized ? t('explorer.restoreWindow') : t('explorer.maximizeWindow')"
            :aria-label="isMaximized ? t('explorer.restoreWindowAria') : t('explorer.maximizeWindowAria')"
            @click="togglePanelMaximized"
          >
            <svg v-if="!isMaximized" viewBox="0 0 16 16" aria-hidden="true">
              <rect x="3.5" y="3.5" width="9" height="9" rx="1.2"/>
            </svg>
            <svg v-else viewBox="0 0 16 16" aria-hidden="true">
              <rect x="4.5" y="5.5" width="7" height="7" rx="1"/>
              <path d="M6.5 5.5v-2h6v6h-1"/>
            </svg>
          </button>
          <button
            type="button"
            class="ghost icon sb-head-iconbtn"
            :title="t('explorer.close')"
            :aria-label="t('explorer.closeAria')"
            @click="closeWorkspace"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8"/>
            </svg>
          </button>
        </div>
      </header>

      <div
        class="sb-contextbar"
        :class="{ drop: rootDropActive }"
        :title="t('explorer.dropToRootTitle')"
        @dragover="onRootDragOver"
        @dragleave="onRootDragLeave"
        @drop="onRootDrop"
      >
        <span class="sb-context-session">
          <span class="sb-context-label">{{ t('explorer.session') }}</span>
          <code :title="activeSessionId">{{ shortSessionId || t('explorer.disconnected') }}</code>
        </span>
        <span class="sb-context-divider" aria-hidden="true"></span>
        <span class="sb-context-path">
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M2 4.5h4.2l1.2 1.3H14v6.7H2z" />
          </svg>
          <code :title="currentPath">{{ currentPath }}</code>
        </span>
        <span v-if="rootDropActive" class="sb-root-drop-hint">{{ t('explorer.dropToRoot') }}</span>
      </div>

      <div ref="workspaceBodyRef" class="sb-workspace-body">
        <section
          class="sb-workspace-tree"
          :style="treePaneStyle"
          :aria-label="t('explorer.fileListAria')"
        >
          <div class="sb-toolbar">
            <label class="sb-searchbox">
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="7" cy="7" r="4.2" />
                <path d="m10.2 10.2 3.3 3.3" />
              </svg>
              <input
                v-model="searchTerm"
                type="search"
                :aria-label="t('explorer.searchAria')"
                :placeholder="t('explorer.searchPlaceholder')"
                @keydown.escape.stop="searchTerm = ''"
              />
              <button
                v-if="searchTerm"
                type="button"
                class="sb-search-clear"
                :title="t('explorer.clearSearch')"
                :aria-label="t('explorer.clearSearch')"
                @click="searchTerm = ''"
              >
                ×
              </button>
            </label>
            <div class="sb-toolbar-actions">
              <div class="sb-view-toggle" role="group" :aria-label="t('explorer.viewAria')">
                <button
                  type="button"
                  :class="{ active: viewMode === 'tree' }"
                  :aria-pressed="viewMode === 'tree'"
                  :title="t('explorer.viewTreeTitle')"
                  @click="setViewMode('tree')"
                >
                  {{ t('explorer.viewTree') }}
                </button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'flat' }"
                  :aria-pressed="viewMode === 'flat'"
                  :title="t('explorer.viewFlatTitle')"
                  @click="setViewMode('flat')"
                >
                  {{ t('explorer.viewFlat') }}
                </button>
              </div>
              <button
                type="button"
                class="ghost tiny sb-tool-btn"
                :title="refreshing ? t('explorer.refreshing') : t('explorer.refreshList')"
                :disabled="refreshing"
                @click="onRefresh"
              >
                <svg :class="{ spinning: refreshing }" viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M3 8a5 5 0 0 1 8.5-3.5L13 6M13 3v3h-3M13 8a5 5 0 0 1-8.5 3.5L3 10M3 13v-3h3"/>
                </svg>
                {{ t('common.refresh') }}
              </button>
              <button
                type="button"
                class="ghost tiny sb-tool-btn"
                :title="openingOs ? t('explorer.opening') : t('explorer.openInOsTitle')"
                :disabled="openingOs || !activeSessionId"
                @click="onOpenInOs"
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M2 4.5h4.2l1.2 1.3H14v6.7H2z" />
                </svg>
                {{ t('explorer.openInOs') }}
              </button>
            </div>
          </div>

          <div
            v-if="selectedCount"
            class="sb-bulkbar"
            role="toolbar"
            :aria-label="t('explorer.bulkAria')"
            :aria-busy="bulkBusy"
          >
            <div class="sb-bulk-status">
              <strong>{{ selectedCount }}</strong>
              <span>{{ t('explorer.selectedItems') }}</span>
            </div>
            <div class="sb-bulk-actions">
              <button type="button" class="ghost tiny" @click="referenceSelected">{{ t('explorer.referenceToChat') }}</button>
              <button type="button" class="ghost tiny" :disabled="bulkBusy" @click="exportSelected">{{ t('explorer.exportZip') }}</button>
              <div class="sb-move-row">
                <input
                  v-model="moveDestination"
                  :aria-label="t('explorer.moveDestAria')"
                  :placeholder="t('explorer.moveDestPlaceholder')"
                  @keydown.enter.stop="moveSelected"
                />
                <button type="button" class="ghost tiny" :disabled="bulkBusy" @click="moveSelected">{{ t('common.move') }}</button>
              </div>
              <span class="sb-danger-divider" aria-hidden="true"></span>
              <button type="button" class="ghost tiny danger" :disabled="bulkBusy" @click="deleteSelected">{{ t('common.delete') }}</button>
              <button type="button" class="ghost tiny" :title="t('common.clearSelection')" @click="clearSelection">{{ t('explorer.clear') }}</button>
            </div>
          </div>

          <div class="sb-tree-scroll" ref="bodyRef">
            <SandboxTree
              v-if="hasContent"
              :state="state"
              :search="searchTerm"
              :view-mode="viewMode"
              :loading="flatLoading"
              :collapse-directory="collapseDirectory"
              :expand-directory="expandDirectory"
              :toggle-directory="toggleDirectory"
              @select="onSelectFile"
              @toggle-select="onToggleSelect"
              @preview="onPreviewFile"
              @reference="onReferenceFile"
              @contextmenu="onRowContextMenu"
            />
            <div v-else-if="rootEntry?.loading" class="sb-empty" role="status">{{ t('explorer.readingSandbox') }}</div>
            <div v-else-if="rootEntry?.error" class="sb-empty error" role="alert">
              {{ rootEntry.error }}
              <button type="button" class="link tiny" @click="onRefresh">{{ t('common.retry') }}</button>
            </div>
            <div v-else class="sb-empty">
              <p>{{ t('explorer.empty') }}</p>
              <p class="muted">{{ t('explorer.emptyHint') }}</p>
            </div>
          </div>
        </section>

        <div
          class="sb-splitter"
          role="separator"
          aria-orientation="vertical"
          :aria-label="t('explorer.splitterAria')"
          :title="t('explorer.splitterTitle')"
          @pointerdown="startTreeSplitDrag"
        ></div>

        <section ref="previewPaneRef" class="sb-workspace-preview" :aria-label="t('explorer.previewAria')">
          <SandboxInlinePreview
            :session-id="activeSessionId"
            :page="props.page"
            :context="props.context"
            @reference-file="onReferenceFile"
          />
        </section>
      </div>
    </aside>
    <SandboxContextMenu
      :open="ctxMenu.open"
      :x="ctxMenu.x"
      :y="ctxMenu.y"
      :actions="ctxMenuActions"
      @close="closeContextMenu"
      @action="onContextMenuAction"
    />
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'
import { useSandboxExplain } from '../composables/useSandboxExplain.js'
import { useSandboxPanelGeometry } from '../composables/useSandboxPanelGeometry.js'
import SandboxTree from './SandboxTree.vue'
import SandboxInlinePreview from './SandboxInlinePreview.vue'
import SandboxContextMenu from './SandboxContextMenu.vue'

const props = defineProps({
  sessionId: { type: String, default: '' },
  page: { type: String, default: '' },
  context: { type: Object, default: null },
  avoidTitlebar: { type: Boolean, default: false },
})

const emit = defineEmits(['reference-file'])

const sandbox = useSandboxBrowser()
const confirmDialog = useConfirm()
const { closeExplain } = useSandboxExplain()
const {
  state,
  closePanel,
  refresh,
  loadAllDirectories,
  expandDirectory,
  collapseDirectory,
  toggleDirectory,
  previewFile,
  clearPreview,
  toggleSelectedPath,
  setSelectedPathTree,
  clearSelection,
  moveDroppedItems,
} = sandbox

const refreshing = ref(false)
const openingOs = ref(false)
const bulkBusy = ref(false)
const flatLoading = ref(false)
const rootDropActive = ref(false)
const viewMode = ref('tree')
const moveDestination = ref('')
const searchTerm = ref('')
const bodyRef = ref(null)
const previewPaneRef = ref(null)
const ctxMenu = ref({
  open: false,
  x: 0,
  y: 0,
  path: '',
  kind: 'file',
  name: '',
})

const {
  panelStyle,
  treePaneStyle,
  startPanelDrag,
  startPanelResize,
  startTreeSplitDrag,
  workspaceBodyRef,
  isMaximized,
  togglePanelMaximized,
} = useSandboxPanelGeometry({ avoidTitlebar: computed(() => props.avoidTitlebar) })

const rootEntry = computed(() => state.directories[''] || null)
const hasContent = computed(() => Array.isArray(rootEntry.value?.items) && rootEntry.value.items.length > 0)
const selectedPaths = computed(() => [...(state.selectedPaths || new Set())])
const selectedCount = computed(() => selectedPaths.value.length)
const activeSessionId = computed(() => String(props.sessionId || state.sessionId || '').trim())
const shortSessionId = computed(() => {
  const sid = activeSessionId.value
  if (sid.length <= 20) return sid
  return `${sid.slice(0, 9)}…${sid.slice(-7)}`
})
const currentPath = computed(() => {
  const path = String(state.preview?.path || state.selectedPath || '').replace(/^\/+|\/+$/g, '')
  return path ? `/${path}` : '/'
})

function pathCoveredBy(path, roots) {
  const rel = String(path || '').replace(/^\/+|\/+$/g, '')
  return Boolean(rel && (roots || []).some((root) => rel === root || rel.startsWith(`${root}/`)))
}

const rootSummary = computed(() => {
  const r = rootEntry.value
  if (!r?.items?.length) return ''
  let files = 0
  let dirs = 0
  for (const it of r.items) {
    if (it.kind === 'dir') dirs += 1
    else files += 1
  }
  if (dirs && files) return t('explorer.dirFileCount', { dirs, files })
  if (dirs) return t('explorer.dirCount', { n: dirs })
  return t('explorer.fileCount', { n: files })
})

async function prepareFlatData() {
  if (!activeSessionId.value || flatLoading.value || state.allDirectoriesLoaded) return
  flatLoading.value = true
  try {
    await loadAllDirectories()
  } finally {
    flatLoading.value = false
  }
}

function setViewMode(mode) {
  if (mode !== 'tree' && mode !== 'flat') return
  viewMode.value = mode
  if (mode === 'flat') prepareFlatData()
}

function closeWorkspace() {
  closeContextMenu()
  closeExplain()
  rootDropActive.value = false
  closePanel()
}

async function onRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await refresh()
    if (state.preview?.path) {
      await previewFile(activeSessionId.value, state.preview.path)
    }
  } finally {
    refreshing.value = false
  }
}

async function onOpenInOs() {
  if (openingOs.value || !activeSessionId.value) return
  openingOs.value = true
  // Prefer the highlighted tree/preview path so selecting mockloc-project
  // opens that folder in Explorer, not only the sandbox root.
  const rel = String(state.preview?.path || state.selectedPath || '')
    .replace(/^\/+|\/+$/g, '')
  try {
    const r = await api.openAssistantSandbox(activeSessionId.value, rel)
    if (!r?.ok) throw new Error(r?.error || t('explorer.openFailed'))
  } catch (e) {
    showToast(t('explorer.openFailedWith', { msg: e?.message || e }), 'error')
  } finally {
    openingOs.value = false
  }
}

function onSelectFile(item) {
  if (!item) return
  const rel = String(item.path || item.relative_path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  state.selectedPath = rel
  if (item.kind === 'dir') {
    clearPreview()
    return
  }
  previewFile(activeSessionId.value, rel)
}

async function onToggleSelect(payload) {
  const rel = String(payload?.path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  if (typeof payload.selected === 'boolean') {
    await setSelectedPathTree(rel, payload.selected, payload.kind || '')
  } else {
    toggleSelectedPath(rel)
  }
}

function hasSandboxDrag(event) {
  return Array.from(event?.dataTransfer?.types || []).includes('application/x-argusai-sandbox-paths')
}

function draggedSandboxPaths(event) {
  try {
    const raw = event?.dataTransfer?.getData?.('application/x-argusai-sandbox-paths') || '[]'
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch (_) {
    return []
  }
}

function onRootDragOver(event) {
  if (!hasSandboxDrag(event)) return
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
  rootDropActive.value = true
}

function onRootDragLeave(event) {
  const related = event.relatedTarget
  if (related && event.currentTarget?.contains?.(related)) return
  rootDropActive.value = false
}

async function onRootDrop(event) {
  if (!hasSandboxDrag(event)) return
  event.preventDefault()
  rootDropActive.value = false
  await moveDroppedItems('', draggedSandboxPaths(event))
}

function onPreviewFile(item) {
  const rel = String(item?.path || item?.relative_path || state.selectedPath || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return Promise.resolve()
  const sid = activeSessionId.value
  if (!sid) {
    showToast(t('explorer.previewNoSession'), 'error')
    return Promise.resolve()
  }
  state.selectedPath = rel
  return previewFile(sid, rel).then(async () => {
    if (state.preview?.error) {
      showToast(t('explorer.previewFailed', { msg: state.preview.error }), 'error')
      return
    }
    await nextTick()
    previewPaneRef.value?.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'smooth' })
  })
}

function onReferenceFile(payload) {
  const rel = String(payload?.file_path || payload?.path || state.selectedPath || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  state.selectedPath = rel
  emit('reference-file', {
    file_path: rel,
    file_name: payload?.file_name || rel.split('/').pop() || rel,
    start_line: payload?.start_line,
    start_column: payload?.start_column,
    end_line: payload?.end_line,
    end_column: payload?.end_column,
    text: payload?.text,
  })
}

function closeContextMenu() {
  ctxMenu.value = { ...ctxMenu.value, open: false }
}

function onRowContextMenu(payload) {
  const rel = String(payload?.path || '').replace(/^\/+|\/+$/g, '')
  if (!rel) return
  state.selectedPath = rel
  ctxMenu.value = {
    open: true,
    x: Number(payload?.x) || 0,
    y: Number(payload?.y) || 0,
    path: rel,
    kind: payload?.kind === 'dir' ? 'dir' : 'file',
    name: payload?.item?.name || rel.split('/').pop() || rel,
  }
}

const ctxMenuActions = computed(() => {
  const rel = ctxMenu.value.path
  if (!rel) return []
  const isDir = ctxMenu.value.kind === 'dir'
  const expanded = state.expanded.has(rel)
  const checked = state.selectedPaths?.has(rel)
  const actions = []
  if (!isDir) {
    actions.push({ id: 'preview', label: t('common.preview') })
  }
  actions.push({ id: 'reference', label: isDir ? t('explorer.referenceDir') : t('explorer.referenceToChat') })
  if (isDir) {
    actions.push({
      id: expanded ? 'collapse' : 'expand',
      label: expanded ? t('explorer.collapseDir') : t('explorer.expandDir'),
    })
    actions.push({
      id: checked ? 'deselect-tree' : 'select-tree',
      label: checked ? t('explorer.deselectAll') : t('explorer.selectAll'),
    })
  } else {
    actions.push({
      id: checked ? 'deselect' : 'select',
      label: checked ? t('explorer.deselect') : t('explorer.select'),
    })
    actions.push({ id: 'export-file', label: t('explorer.exportFile') })
  }
  actions.push({ id: 'copy-path', label: t('explorer.copyRelPath') })
  if (!isDir) {
    actions.push({ id: 'open-os', label: t('explorer.openInSystem') })
  }
  if (isDir) actions.push({ id: 'export-zip', label: t('explorer.exportDirZip') })
  actions.push({ id: 'delete', label: t('common.delete'), danger: true, separatorBefore: true })
  return actions
})

async function onContextMenuAction(actionId) {
  const rel = ctxMenu.value.path
  const kind = ctxMenu.value.kind
  const name = ctxMenu.value.name
  closeContextMenu()
  if (!rel) return
  if (actionId === 'preview') {
    await onPreviewFile({ path: rel, relative_path: rel, kind: 'file' })
    return
  }
  if (actionId === 'reference') {
    onReferenceFile({ path: rel, file_name: name })
    return
  }
  if (actionId === 'expand') {
    await expandDirectory(rel)
    return
  }
  if (actionId === 'collapse') {
    collapseDirectory(rel)
    return
  }
  if (actionId === 'select' || actionId === 'select-tree') {
    await setSelectedPathTree(rel, true, kind)
    return
  }
  if (actionId === 'deselect' || actionId === 'deselect-tree') {
    await setSelectedPathTree(rel, false, kind)
    return
  }
  if (actionId === 'copy-path') {
    try {
      await navigator.clipboard.writeText(rel)
      showToast(t('explorer.copiedPath'), 'ok')
    } catch (e) {
      showToast(t('explorer.copyFailed', { msg: e.message || e }), 'error')
    }
    return
  }
  if (actionId === 'export-file') {
    await exportSingleFile(rel)
    return
  }
  if (actionId === 'open-os') {
    try {
      const r = await api.openUserPath(rel, activeSessionId.value)
      if (!r?.ok) throw new Error(r?.error || t('explorer.openFailed'))
    } catch (e) {
      showToast(t('explorer.openFailedWith', { msg: e.message || e }), 'error')
    }
    return
  }
  if (actionId === 'export-zip') {
    await exportPaths([rel])
    return
  }
  if (actionId === 'delete') {
    await deletePaths([rel])
  }
}

function referenceSelected() {
  const paths = selectedPaths.value
  if (!paths.length) return
  emit('reference-file', {
    files: paths.map(p => ({ file_path: p, file_name: p.split('/').pop() || p })),
  })
}

async function revealExportFolder(folder, filePath) {
  const dir = String(folder || '').trim()
  const full = String(filePath || '').trim()
  if (full) {
    try {
      await navigator.clipboard.writeText(full)
    } catch (_) {}
  }
  if (dir) {
    try {
      await api.openFolder(dir)
    } catch (_) {}
  }
}

async function exportSingleFile(relPath) {
  const rel = String(relPath || '').replace(/^\/+|\/+$/g, '')
  if (!rel || bulkBusy.value || !activeSessionId.value) return
  bulkBusy.value = true
  try {
    const r = await api.exportAssistantSandboxFile(activeSessionId.value, rel)
    if (r?.cancelled) return
    if (!r?.ok) throw new Error(r?.error || t('explorer.exportFailed'))
    showToast(t('explorer.exported', { path: r.path }), 'ok')
    await revealExportFolder(r.folder, r.path)
  } catch (e) {
    showToast(t('explorer.exportFailedWith', { msg: e.message || e }), 'error')
  } finally {
    bulkBusy.value = false
  }
}

async function exportPaths(paths) {
  const list = (paths || []).filter(Boolean)
  if (!list.length || bulkBusy.value || !activeSessionId.value) return
  bulkBusy.value = true
  try {
    const r = await api.exportAssistantSandboxItems(activeSessionId.value, list)
    if (r?.cancelled) return
    if (!r?.ok) throw new Error(r?.error || t('explorer.exportFailed'))
    showToast(t('explorer.exportedZip', { path: r.path || r.relative_path }), 'ok')
    await revealExportFolder(r.folder, r.path)
  } catch (e) {
    showToast(t('explorer.exportFailedWith', { msg: e.message || e }), 'error')
  } finally {
    bulkBusy.value = false
  }
}

async function exportSelected() {
  await exportPaths(selectedPaths.value)
}

async function deletePaths(paths) {
  const list = (paths || []).filter(Boolean)
  if (!list.length || bulkBusy.value || !activeSessionId.value) return
  const ok = await confirmDialog({
    title: t('explorer.deleteTitle'),
    content: t('explorer.deleteContent', { n: list.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  bulkBusy.value = true
  try {
    const r = await api.deleteAssistantSandboxItems(activeSessionId.value, list, 'DELETE_SANDBOX_ITEMS')
    if (!r?.ok) throw new Error(r?.error || t('explorer.deleteFailed'))
    state.selectedPaths = new Set(
      [...state.selectedPaths].filter((path) => !pathCoveredBy(path, list)),
    )
    if (pathCoveredBy(state.preview?.path, list) || pathCoveredBy(state.selectedPath, list)) {
      state.selectedPath = ''
      clearPreview()
    }
    showToast(t('explorer.deletedItems', { n: r.count || 0 }), 'ok')
    await onRefresh()
  } catch (e) {
    showToast(t('explorer.deleteFailedWith', { msg: e.message || e }), 'error')
  } finally {
    bulkBusy.value = false
  }
}
async function deleteSelected() {
  await deletePaths(selectedPaths.value)
}

async function moveSelected() {
  const paths = selectedPaths.value
  const destination = String(moveDestination.value || '').trim()
  if (!paths.length || bulkBusy.value || !activeSessionId.value) return
  const ok = await confirmDialog({
    title: t('explorer.moveTitle'),
    content: t('explorer.moveContent', { n: paths.length, dest: destination || '/' }),
    positiveText: t('common.move'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  bulkBusy.value = true
  try {
    const r = await api.moveAssistantSandboxItems(activeSessionId.value, paths, destination, 'MOVE_SANDBOX_ITEMS')
    if (!r?.ok) throw new Error(r?.error || t('explorer.moveFailed'))
    clearSelection()
    if (pathCoveredBy(state.preview?.path, paths) || pathCoveredBy(state.selectedPath, paths)) {
      state.selectedPath = ''
      clearPreview()
    }
    showToast(t('explorer.movedItems', { n: r.count || 0 }), 'ok')
    await onRefresh()
  } catch (e) {
    showToast(t('explorer.moveFailedWith', { msg: e.message || e }), 'error')
  } finally {
    bulkBusy.value = false
  }
}

watch(() => props.sessionId, (sid) => {
  if (!state.open || !sid) return
  if (sid !== state.sessionId) sandbox.openPanel(sid)
  if (viewMode.value === 'flat' || searchTerm.value.trim()) prepareFlatData()
})

watch(searchTerm, (term) => {
  if (String(term || '').trim()) prepareFlatData()
})
</script>

<style scoped>
.sandbox-workspace {
  position: fixed;
  /* Below assistant fab (45) and panel (50) — sandbox is a tool pane, not a global launcher. */
  z-index: 40;
  display: flex;
  flex-direction: column;
  container-name: sandbox-panel;
  container-type: inline-size;
  background: color-mix(in srgb, var(--panel) 96%, var(--bg-sunken));
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 14px;
  overflow: hidden;
  box-shadow: var(--shadow-md), 0 18px 56px rgba(0, 0, 0, 0.24);
  max-width: calc(100vw - 24px);
  max-height: calc(100vh - var(--titlebar-h, 0px) - 24px);
}
.sandbox-workspace.maximized .sb-resize-handle {
  display: none;
}

.sb-resize-handle {
  position: absolute;
  z-index: 4;
}
.sb-resize-handle.north,
.sb-resize-handle.south {
  left: 10px;
  right: 10px;
  height: 8px;
}
.sb-resize-handle.north { top: -3px; cursor: ns-resize; }
.sb-resize-handle.south { bottom: -3px; cursor: ns-resize; }
.sb-resize-handle.east,
.sb-resize-handle.west {
  top: 10px;
  bottom: 10px;
  width: 8px;
}
.sb-resize-handle.east { right: -3px; cursor: ew-resize; }
.sb-resize-handle.west { left: -3px; cursor: ew-resize; }
.sb-resize-handle.northwest,
.sb-resize-handle.northeast,
.sb-resize-handle.southwest,
.sb-resize-handle.southeast {
  width: 14px;
  height: 14px;
}
.sb-resize-handle.northwest { top: -2px; left: -2px; cursor: nwse-resize; }
.sb-resize-handle.northeast { top: -2px; right: -2px; cursor: nesw-resize; }
.sb-resize-handle.southwest { bottom: -2px; left: -2px; cursor: nesw-resize; }
.sb-resize-handle.southeast { bottom: -2px; right: -2px; cursor: nwse-resize; }

.sb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 48px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  position: relative;
  z-index: 2;
}
.sb-head-draggable {
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
}
.sb-head-draggable:active {
  cursor: grabbing;
}
.sb-head-title {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 9px;
  color: var(--fg-strong);
}
.sb-head-icon {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent) 10%, var(--panel-2));
}
.sb-head-icon svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.25;
}
.sb-head-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}
.sb-head-copy strong {
  overflow: hidden;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-head-copy small {
  color: var(--muted-2);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.sb-head-count {
  font-weight: 400;
  font-size: 11px;
  color: var(--muted-2);
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-sunken);
  white-space: nowrap;
}
.sb-head-actions { display: flex; gap: 4px; }
.sb-head-iconbtn {
  width: 26px;
  height: 26px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  border-color: transparent;
}
.sb-head-iconbtn svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
}
.sb-head-iconbtn:hover:not(:disabled) {
  color: var(--fg-strong);
  background: var(--panel-hover);
}
.sb-head-iconbtn:disabled {
  color: var(--muted-2);
}

.sb-contextbar {
  min-height: 32px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 11px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-sunken);
  color: var(--muted);
  font-size: 10px;
  transition: background 120ms ease, box-shadow 120ms ease;
}
.sb-contextbar.drop {
  background: color-mix(in srgb, var(--accent) 16%, var(--bg-sunken));
  box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--accent) 78%, transparent);
}
.sb-context-session,
.sb-context-path {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.sb-context-session {
  flex: 0 1 auto;
}
.sb-context-path {
  flex: 1 1 auto;
}
.sb-context-label {
  color: var(--muted-2);
}
.sb-contextbar code {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-context-divider {
  width: 1px;
  height: 14px;
  flex: 0 0 auto;
  background: var(--border-strong, var(--border));
}
.sb-context-path svg {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
  fill: none;
  stroke: var(--muted-2);
  stroke-width: 1.2;
}
.sb-root-drop-hint {
  flex: 0 0 auto;
  color: var(--accent);
  font-weight: 650;
}

.sb-workspace-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: row;
  overflow: hidden;
}

.sb-workspace-tree {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--panel);
}

.sb-splitter {
  flex: 0 0 5px;
  width: 5px;
  cursor: col-resize;
  background: transparent;
  position: relative;
  touch-action: none;
}
.sb-splitter::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 1px;
  background: var(--border);
  transition: background 120ms ease, width 120ms ease, left 120ms ease;
}
.sb-splitter:hover::after,
.sb-splitter:active::after {
  left: 1px;
  width: 3px;
  background: color-mix(in srgb, var(--accent) 55%, var(--border));
}

.sb-toolbar {
  padding: 8px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.sb-searchbox {
  height: 30px;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 0 8px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg-sunken);
  color: var(--muted-2);
}
.sb-searchbox:focus-within {
  outline: none;
  border-color: color-mix(in srgb, var(--accent) 62%, var(--border));
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent);
}
.sb-searchbox > svg {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.3;
}
.sb-searchbox input {
  min-width: 0;
  flex: 1;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 11px;
}
.sb-searchbox input::-webkit-search-cancel-button {
  display: none;
}
.sb-search-clear {
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.sb-search-clear:hover {
  background: var(--panel-hover);
  color: var(--fg-strong);
}
.sb-toolbar-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 5px;
}
.sb-view-toggle {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg-sunken);
}
.sb-view-toggle button {
  padding: 3px 8px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--muted);
  font-size: 10px;
  cursor: pointer;
}
.sb-view-toggle button:hover {
  color: var(--fg);
}
.sb-view-toggle button.active {
  background: color-mix(in srgb, var(--accent) 18%, var(--panel-2));
  color: var(--fg-strong);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 28%, transparent);
}
.sb-tool-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  color: var(--muted);
  white-space: nowrap;
}
.sb-tool-btn svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.3;
}
.sb-tool-btn svg.spinning {
  animation: sb-spin 700ms linear infinite;
}
@keyframes sb-spin {
  to { transform: rotate(360deg); }
}
.sb-bulkbar {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
  background: color-mix(in srgb, var(--accent) 8%, var(--panel));
  font-size: 11px;
  color: var(--muted);
}
.sb-bulk-status {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  padding-right: 3px;
  white-space: nowrap;
}
.sb-bulk-status strong {
  color: var(--accent);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.sb-bulk-actions {
  min-width: 0;
  flex: 1 1 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.sb-bulkbar .tiny {
  font-size: 11px;
  padding: 3px 7px;
}
.sb-bulkbar .danger {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err) 38%, var(--border));
}
.sb-danger-divider {
  width: 1px;
  height: 18px;
  margin: 0 2px;
  background: var(--border-strong, var(--border));
}
.sb-move-row {
  min-width: 180px;
  flex: 1 1 210px;
  display: flex;
  gap: 4px;
}
.sb-move-row input {
  min-width: 0;
  flex: 1;
  padding: 4px 7px;
  font-size: 11px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--fg);
}
.sb-move-row input:focus {
  outline: none;
  border-color: color-mix(in srgb, var(--accent) 58%, var(--border));
}

.sb-tree-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 4px 0 8px;
}

.sb-workspace-preview {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.sb-empty {
  padding: 20px 12px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
.sb-empty p {
  margin: 0;
}
.sb-empty.error { color: var(--err); }
.sb-empty .muted { margin-top: 4px; opacity: 0.6; font-size: 11px; }
.sb-empty .link {
  margin-top: 8px;
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 11px;
}

@container sandbox-panel (max-width: 680px) {
  .sb-contextbar {
    gap: 7px;
  }
  .sb-context-session {
    max-width: 42%;
  }
  .sb-root-drop-hint {
    display: none;
  }
  .sb-workspace-body {
    flex-direction: column;
  }
  .sb-workspace-tree {
    width: 100% !important;
    min-width: 0 !important;
    min-height: 180px;
    flex: 0 0 43% !important;
    border-bottom: 1px solid var(--border);
  }
  .sb-splitter {
    display: none;
  }
  .sb-workspace-preview {
    min-height: 0;
    flex: 1 1 57%;
  }
}

@container sandbox-panel (max-width: 560px) {
  .sb-head-count {
    display: none;
  }
  .sb-toolbar-actions {
    flex-wrap: wrap;
  }
  .sb-bulkbar {
    align-items: flex-start;
  }
  .sb-bulk-status {
    padding-top: 3px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sb-tool-btn svg.spinning {
    animation-duration: 1.5s;
  }
  .sb-contextbar,
  .sb-splitter::after {
    transition: none;
  }
}
</style>
