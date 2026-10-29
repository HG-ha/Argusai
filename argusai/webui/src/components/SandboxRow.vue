<template>
  <!--
    A single row in the sandbox tree. Directories recursively render their
    children when expanded; the recursion lives here (not in SandboxTree)
    because each row needs its own caret + level offset.
  -->
  <div
    class="sb-row"
    :class="{
      selected: selected,
      checked: checked,
      pulse: state.flashPath === relPath,
      dir: isDir,
      drop: state.dropTargetPath === relPath,
    }"
    :data-level="level"
    :style="{ '--sb-level': level, paddingLeft: (10 + level * 12) + 'px' }"
    role="treeitem"
    :aria-level="level + 1"
    :aria-expanded="isDir ? expanded : undefined"
    :aria-selected="checked"
    tabindex="0"
    @click="onClick"
    @dblclick="onDblClick"
    @keydown="onKeydown"
    @contextmenu.prevent="onContextMenu"
    draggable="true"
    @dragstart="onDragStart"
    @dragend="onDragEnd"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <input
      class="sb-check"
      type="checkbox"
      :checked="checked"
      :title="t('explorer.selectItem', { name: item.name })"
      :aria-label="t('explorer.selectItem', { name: item.name })"
      @click.stop
      @change="emit('toggle-select', { path: relPath, selected: $event.target.checked, kind: item.kind })"
    />
    <button
      v-if="isDir"
      type="button"
      class="sb-caret"
      :aria-label="expanded ? t('explorer.collapseItem', { name: item.name }) : t('explorer.expandItem', { name: item.name })"
      :title="expanded ? t('explorer.collapseDir') : t('explorer.expandDir')"
      @click.stop="toggleDirectory(relPath)"
    >
      {{ expanded ? '▾' : '▸' }}
    </button>
    <span class="sb-caret" v-else aria-hidden="true">&nbsp;</span>

    <span
      class="sb-chip"
      :class="{ dir: isDir }"
      :style="{ background: chipBg }"
      :title="chip.label"
    >{{ chip.label }}</span>

    <span class="sb-name" :title="item.name">{{ item.name }}</span>

    <span v-if="badge" class="sb-badge" :class="'tone-' + badge.tone" :title="badge.title">{{ badge.label }}</span>

    <span v-if="!isDir && item.size != null && !badge" class="sb-meta">{{ formatSize(item.size) }}</span>

    <span v-if="!isDir" class="sb-actions" @click.stop>
      <button
        type="button"
        class="ghost icon"
        :title="t('explorer.referenceToChat')"
        :aria-label="t('explorer.referenceItem', { name: item.name })"
        @click="emit('reference', { path: relPath })"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M9.5 3.5h3v3M12.3 3.7 7.8 8.2M7 5H4.5A1.5 1.5 0 0 0 3 6.5v5A1.5 1.5 0 0 0 4.5 13h5a1.5 1.5 0 0 0 1.5-1.5V9" />
        </svg>
      </button>
      <button
        type="button"
        class="ghost icon"
        :title="t('common.preview')"
        :aria-label="t('explorer.previewItem', { name: item.name })"
        @click="emit('preview', { path: relPath })"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M1.8 8s2.2-3.5 6.2-3.5S14.2 8 14.2 8 12 11.5 8 11.5 1.8 8 1.8 8Z" />
          <circle cx="8" cy="8" r="1.7" />
        </svg>
      </button>
    </span>
  </div>

  <template v-if="isDir && expanded">
    <div class="sb-children" role="group">
      <div v-if="childEntry?.loading" class="sb-loading" :style="{ paddingLeft: (8 + (level + 1) * 14) + 'px' }">
        {{ t('explorer.readingDir') }}
      </div>
      <div v-else-if="childEntry?.error" class="sb-loading error" :style="{ paddingLeft: (8 + (level + 1) * 14) + 'px' }">
        {{ childEntry.error }}
      </div>
      <template v-else>
        <SandboxRow
          v-for="child in (childEntry?.items || [])"
          :key="rowKey(child)"
          :item="child"
          :level="level + 1"
          :state="state"
          :toggle-directory="toggleDirectory"
          :expand-directory="expandDirectory"
          :collapse-directory="collapseDirectory"
          @select="emit('select', $event)"
          @toggle-select="emit('toggle-select', $event)"
          @preview="emit('preview', $event)"
          @reference="emit('reference', $event)"
          @contextmenu="emit('contextmenu', $event)"
        />
      </template>
    </div>
  </template>
</template>

<script setup>
import { computed, watch } from 'vue'
import { t } from '../i18n.js'
import { fileTypeIcon, fileTypeChipColor, sandboxItemBadge } from '../fileTypeIcon.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'

const sandbox = useSandboxBrowser()

const props = defineProps({
  item: { type: Object, required: true },
  level: { type: Number, default: 0 },
  state: { type: Object, required: true },
  toggleDirectory: { type: Function, required: true },
  expandDirectory: { type: Function, required: true },
  collapseDirectory: { type: Function, required: true },
})

const emit = defineEmits(['select', 'preview', 'reference', 'toggle-select', 'contextmenu'])

const isDir = computed(() => props.item.kind === 'dir')
const relPath = computed(() => String(props.item.relative_path || props.item.path || props.item.name || '').replace(/^\/+|\/+$/g, ''))
const expanded = computed(() => isDir.value && props.state.expanded.has(relPath.value))
const selected = computed(() => props.state.selectedPath === relPath.value)
const checked = computed(() => props.state.selectedPaths?.has(relPath.value))
const childEntry = computed(() => (isDir.value ? props.state.directories[relPath.value] : null))
const chip = computed(() => fileTypeIcon(props.item))
const chipBg = computed(() => fileTypeChipColor(props.item))
const badge = computed(() => sandboxItemBadge(props.item))

// Lazy-load when a directory becomes expanded for the first time.
watch(expanded, async (next) => {
  if (next && isDir.value && !childEntry.value) {
    await props.expandDirectory(relPath.value)
  }
})

function onClick(event) {
  if (event?.ctrlKey || event?.metaKey) {
    emit('toggle-select', { path: relPath.value, selected: !checked.value, kind: props.item.kind })
  }
  if (isDir.value) {
    props.toggleDirectory(relPath.value)
  }
  emit('select', { ...props.item, path: relPath.value })
}

function onDblClick() {
  if (isDir.value) return
  emit('preview', { ...props.item, path: relPath.value })
}

function onKeydown(event) {
  if (event.key === ' ') {
    event.preventDefault()
    emit('toggle-select', { path: relPath.value, selected: !checked.value, kind: props.item.kind })
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    if (isDir.value) {
      props.toggleDirectory(relPath.value)
      emit('select', { ...props.item, path: relPath.value })
    } else {
      emit('preview', { ...props.item, path: relPath.value })
    }
    return
  }
  if (isDir.value && event.key === 'ArrowRight' && !expanded.value) {
    event.preventDefault()
    props.expandDirectory(relPath.value)
  } else if (isDir.value && event.key === 'ArrowLeft' && expanded.value) {
    event.preventDefault()
    props.collapseDirectory(relPath.value)
  }
}

function onContextMenu(event) {
  emit('contextmenu', {
    item: props.item,
    path: relPath.value,
    kind: props.item.kind,
    x: event.clientX,
    y: event.clientY,
  })
}

function onDragStart(event) {
  const selected = props.state.selectedPaths?.has(relPath.value)
    ? [...props.state.selectedPaths]
    : [relPath.value]
  event.dataTransfer.effectAllowed = 'copyMove'
  event.dataTransfer.setData('application/x-argusai-sandbox-paths', JSON.stringify(selected))
  event.dataTransfer.setData('text/plain', selected.join('\n'))
}

function onDragEnd() {
  sandbox.clearDropTargetPath()
}

function hasSandboxDrag(event) {
  return Array.from(event?.dataTransfer?.types || []).includes('application/x-argusai-sandbox-paths')
}

function onDragOver(event) {
  if (!isDir.value || !hasSandboxDrag(event)) return
  event.preventDefault()
  event.stopPropagation()
  event.dataTransfer.dropEffect = 'move'
  sandbox.setDropTargetPath(relPath.value)
}

function onDragLeave(event) {
  if (!isDir.value) return
  const related = event.relatedTarget
  if (related && event.currentTarget?.contains?.(related)) return
  sandbox.clearDropTargetPath(relPath.value)
}

async function onDrop(event) {
  if (!isDir.value || !hasSandboxDrag(event)) return
  event.preventDefault()
  event.stopPropagation()
  let paths = []
  try {
    const raw = event.dataTransfer?.getData?.('application/x-argusai-sandbox-paths') || '[]'
    const parsed = JSON.parse(raw)
    paths = Array.isArray(parsed) ? parsed : []
  } catch (_) {
    paths = []
  }
  await sandbox.moveDroppedItems(relPath.value, paths)
}

function rowKey(item) {
  return String(item.relative_path || item.path || item.name || '')
}

function formatSize(bytes) {
  const n = Number(bytes || 0)
  if (n <= 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`
}
</script>

<style scoped>
/* VS Code–like explorer row: compact, indent guides, full-row selection */
.sb-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px 0 10px;
  height: 22px;
  cursor: pointer;
  color: var(--text-1, var(--fg));
  position: relative;
  border-radius: 0;
}
.sb-row::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: calc(10px + (var(--sb-level, 0) * 12px));
  pointer-events: none;
  background-image: repeating-linear-gradient(
    to right,
    transparent 0 11px,
    color-mix(in srgb, var(--fg) 10%, transparent) 11px 12px
  );
  background-size: 12px 100%;
  background-position: 10px 0;
  opacity: 0.85;
}
.sb-row:hover { background: var(--panel-hover, color-mix(in srgb, var(--fg) 6%, transparent)); }
.sb-row:focus-visible {
  outline: 1px solid color-mix(in srgb, var(--accent) 74%, transparent);
  outline-offset: -1px;
  z-index: 2;
}
.sb-row.checked {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.sb-row.selected {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
}
.sb-row.selected.checked {
  background: color-mix(in srgb, var(--accent) 22%, transparent);
}
.sb-row.drop {
  outline: 1px dashed color-mix(in srgb, var(--accent) 55%, transparent);
  outline-offset: -1px;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.sb-row.selected .sb-name { color: var(--fg-strong, var(--fg)); font-weight: 600; }
.sb-row.pulse { animation: sb-pulse 1.2s ease 1; }
@keyframes sb-pulse {
  0%, 100% { background: var(--panel-hover, transparent); }
  20%, 60% { background: color-mix(in srgb, var(--accent) 28%, transparent); }
}

.sb-caret {
  width: 14px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: inherit;
  text-align: center;
  font-size: 10px;
  opacity: 0.55;
  flex-shrink: 0;
  line-height: 1;
  position: relative;
  z-index: 1;
}
.sb-check {
  width: 13px;
  height: 13px;
  margin: 0;
  flex-shrink: 0;
  opacity: 0;
  position: relative;
  z-index: 1;
  transition: opacity 80ms ease;
}
.sb-row:hover .sb-check,
.sb-row.checked .sb-check,
.sb-row.selected .sb-check,
.sb-row:focus-within .sb-check { opacity: 1; }
.sb-row.dir .sb-caret { cursor: pointer; }
.sb-row.dir .sb-caret:hover { opacity: 1; }

.sb-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 26px;
  height: 16px;
  padding: 0 4px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.3px;
  color: #fff;
  border-radius: 2px;
  flex-shrink: 0;
}
.sb-chip.dir {
  background: hsl(210deg 18% 38%);
}

.sb-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
}
.sb-meta {
  font-size: 10px;
  color: var(--text-2, rgba(255,255,255,0.45));
  flex-shrink: 0;
  margin-left: auto;
}
.sb-row:hover .sb-meta,
.sb-row:focus-within .sb-meta { display: none; }

.sb-badge {
  font-size: 9px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 8px;
  letter-spacing: 0.3px;
  flex-shrink: 0;
  margin-left: auto;
  white-space: nowrap;
}
.sb-badge.tone-accent {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
}
.sb-badge.tone-muted {
  background: color-mix(in srgb, var(--muted, #888) 18%, transparent);
  color: var(--text-2, rgba(255,255,255,0.55));
  border: 1px solid color-mix(in srgb, var(--muted, #888) 25%, transparent);
}
.sb-badge.tone-dim {
  background: transparent;
  color: var(--text-2, rgba(255,255,255,0.42));
  border: 1px dashed color-mix(in srgb, var(--muted, #888) 30%, transparent);
}
.sb-row:hover .sb-badge,
.sb-row:focus-within .sb-badge { display: none; }

.sb-actions {
  display: none;
  gap: 2px;
  margin-left: auto;
  flex-shrink: 0;
}
.sb-row:hover .sb-actions,
.sb-row:focus-within .sb-actions { display: inline-flex; }
.sb-actions button {
  width: 20px;
  height: 20px;
  padding: 0;
  font-size: 11px;
  line-height: 1;
}
.sb-actions svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.25;
}

.sb-loading {
  padding: 4px 8px;
  font-size: 11px;
  color: var(--text-2, rgba(255,255,255,0.5));
  font-style: italic;
}
.sb-loading.error {
  color: #f48771;
  font-style: normal;
}
@media (prefers-reduced-motion: reduce) {
  .sb-row.pulse { animation: none; }
}
</style>
