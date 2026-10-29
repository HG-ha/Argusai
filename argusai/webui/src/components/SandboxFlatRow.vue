<template>
  <!--
    A simplified "flat list" row used while a search term is active.
    No indentation, no caret, but otherwise the same affordances as the
    tree row (hover toolbar for preview + reference).
  -->
  <div
    class="sb-row flat"
    :class="{
      selected: selected,
      checked: checked,
      pulse: state.flashPath === relPath,
      dir: isDir,
      drop: state.dropTargetPath === relPath,
    }"
    role="option"
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
    <span
      class="sb-chip"
      :class="{ dir: isDir }"
      :style="{ background: chipBg }"
      :title="chip.label"
    >{{ chip.label }}</span>

    <div class="sb-flat-text">
      <span class="sb-name" :title="item.name">{{ item.name }}</span>
      <span class="sb-path">{{ parentPath || '/' }}</span>
    </div>

    <span v-if="badge" class="sb-badge" :class="'tone-' + badge.tone" :title="badge.title">{{ badge.label }}</span>
    <span v-else-if="item.size != null" class="sb-meta">{{ formatSize(item.size) }}</span>

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
</template>

<script setup>
import { computed } from 'vue'
import { t } from '../i18n.js'
import { fileTypeIcon, fileTypeChipColor, sandboxItemBadge } from '../fileTypeIcon.js'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'

const sandbox = useSandboxBrowser()

const props = defineProps({
  item: { type: Object, required: true },
  state: { type: Object, required: true },
})

const emit = defineEmits(['select', 'preview', 'reference', 'toggle-select', 'contextmenu'])

const relPath = computed(() => String(props.item.relative_path || props.item.path || props.item.name || '').replace(/^\/+|\/+$/g, ''))
const isDir = computed(() => props.item.kind === 'dir')
const parentPath = computed(() => {
  const idx = relPath.value.lastIndexOf('/')
  return idx > 0 ? relPath.value.slice(0, idx) : ''
})
const selected = computed(() => props.state.selectedPath === relPath.value)
const checked = computed(() => props.state.selectedPaths?.has(relPath.value))
const chip = computed(() => fileTypeIcon(props.item))
const chipBg = computed(() => fileTypeChipColor(props.item))
const badge = computed(() => sandboxItemBadge(props.item))

function onClick(event) {
  if (event?.ctrlKey || event?.metaKey) {
    emit('toggle-select', { path: relPath.value, selected: !checked.value, kind: props.item.kind })
  }
  emit('select', { ...props.item, path: relPath.value })
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

function onDblClick() {
  if (isDir.value) return
  emit('preview', { ...props.item, path: relPath.value })
}

function onKeydown(event) {
  if (event.key === ' ') {
    event.preventDefault()
    emit('toggle-select', { path: relPath.value, selected: !checked.value, kind: props.item.kind })
  } else if (event.key === 'Enter') {
    event.preventDefault()
    if (isDir.value) emit('select', { ...props.item, path: relPath.value })
    else emit('preview', { ...props.item, path: relPath.value })
  }
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
    const parsed = JSON.parse(event.dataTransfer?.getData?.('application/x-argusai-sandbox-paths') || '[]')
    paths = Array.isArray(parsed) ? parsed : []
  } catch (_) {
    paths = []
  }
  await sandbox.moveDroppedItems(relPath.value, paths)
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
.sb-row.flat {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  height: 36px;
  cursor: pointer;
}
.sb-row.flat:hover { background: var(--panel-hover, color-mix(in srgb, var(--fg) 6%, transparent)); }
.sb-row.flat:focus-visible {
  outline: 1px solid color-mix(in srgb, var(--accent) 74%, transparent);
  outline-offset: -1px;
}
.sb-row.flat.checked { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.sb-row.flat.selected { background: color-mix(in srgb, var(--accent) 18%, transparent); }
.sb-row.flat.selected.checked { background: color-mix(in srgb, var(--accent) 22%, transparent); }
.sb-row.flat.drop {
  outline: 1px dashed color-mix(in srgb, var(--accent) 60%, transparent);
  outline-offset: -2px;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.sb-row.flat.pulse { animation: sb-pulse 1.2s ease 1; }
@keyframes sb-pulse {
  0%, 100% { background: var(--panel-hover, transparent); }
  20%, 60% { background: color-mix(in srgb, var(--accent) 28%, transparent); }
}

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
.sb-check {
  width: 13px;
  height: 13px;
  margin: 0;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 80ms ease;
}
.sb-row.flat:hover .sb-check,
.sb-row.flat.checked .sb-check,
.sb-row.flat.selected .sb-check,
.sb-row.flat:focus-within .sb-check { opacity: 1; }

.sb-flat-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.sb-name {
  font-size: 12px;
  color: var(--text-1, var(--fg));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sb-path {
  font-size: 10px;
  color: var(--text-2, rgba(255,255,255,0.45));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sb-meta {
  font-size: 10px;
  color: var(--text-2, rgba(255,255,255,0.45));
  flex-shrink: 0;
}
.sb-row.flat:hover .sb-meta,
.sb-row.flat:focus-within .sb-meta { display: none; }

.sb-badge {
  font-size: 9px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 8px;
  letter-spacing: 0.3px;
  flex-shrink: 0;
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
.sb-row.flat:hover .sb-badge,
.sb-row.flat:focus-within .sb-badge { display: none; }

.sb-actions {
  display: none;
  gap: 2px;
  flex-shrink: 0;
}
.sb-row.flat:hover .sb-actions,
.sb-row.flat:focus-within .sb-actions { display: inline-flex; }
.sb-actions button {
  width: 22px;
  height: 22px;
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
@media (prefers-reduced-motion: reduce) {
  .sb-row.flat.pulse { animation: none; }
}
</style>
