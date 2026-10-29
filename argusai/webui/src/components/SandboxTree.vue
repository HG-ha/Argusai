<template>
  <!--
    Recursive sandbox file tree.

    Rendering rules:
    - Directories show a ▾ / ▸ caret; clicking the row toggles expansion AND
      selects the directory (single click is enough — we don't want to force
      double clicks like Finder, this isn't a file manager).
    - Files render their type chip + name + size. Click selects, hover shows
      an action toolbar (preview / reference). Double-click is the same as
      "preview".
    - When `search` is non-empty, the tree is flattened to a filtered list —
      directory hierarchy is hidden because the user is already in "find by
      name" mode, not "browse" mode.
  -->
  <div
    class="sb-tree"
    :class="{ flat: showFlat }"
    :role="showFlat ? 'listbox' : 'tree'"
    :aria-label="showFlat ? t('explorer.flatAria') : t('explorer.treeAria')"
    aria-multiselectable="true"
    @dragleave="onTreeDragLeave"
  >
    <template v-if="!showFlat">
      <SandboxRow
        v-for="item in rootItems"
        :key="rowKey(item)"
        :item="item"
        :level="0"
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
    <template v-else>
      <div v-if="loading && !filteredFlatItems.length" class="sb-empty">
        {{ t('explorer.readingFullTree') }}
      </div>
      <SandboxFlatRow
        v-for="item in filteredFlatItems"
        :key="item.path"
        :item="item"
        :state="state"
        @select="emit('select', $event)"
        @toggle-select="emit('toggle-select', $event)"
        @preview="emit('preview', $event)"
        @reference="emit('reference', $event)"
        @contextmenu="emit('contextmenu', $event)"
      />
      <div v-if="!loading && !filteredFlatItems.length" class="sb-empty">
        {{ search ? t('explorer.noMatch') : t('explorer.empty') }}
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { t } from '../i18n.js'
import SandboxRow from './SandboxRow.vue'
import SandboxFlatRow from './SandboxFlatRow.vue'
import { useSandboxBrowser } from '../composables/useSandboxBrowser.js'

const sandbox = useSandboxBrowser()

const props = defineProps({
  state: { type: Object, required: true },
  search: { type: String, default: '' },
  viewMode: { type: String, default: 'tree' },
  loading: { type: Boolean, default: false },
  toggleDirectory: { type: Function, required: true },
  expandDirectory: { type: Function, required: true },
  collapseDirectory: { type: Function, required: true },
})

const emit = defineEmits(['select', 'toggle-select', 'preview', 'reference', 'contextmenu'])

const rootItems = computed(() => {
  const root = props.state.directories['']
  return Array.isArray(root?.items) ? root.items : []
})

const showFlat = computed(() => props.viewMode === 'flat' || Boolean(String(props.search || '').trim()))

const allFlatItems = computed(() => {
  const out = []
  const seen = new Set()
  const visit = (dirPath) => {
    const entry = props.state.directories[dirPath]
    if (!entry?.items) return
    for (const item of entry.items) {
      const rel = String(item.relative_path || item.path || item.name || '').replace(/^\/+|\/+$/g, '')
      if (!rel || seen.has(rel)) continue
      seen.add(rel)
      out.push({ ...item, path: rel })
      if (item.kind === 'dir') visit(rel)
    }
  }
  visit('')
  return out.sort((a, b) => {
    const pathA = String(a.path || '')
    const pathB = String(b.path || '')
    return pathA.localeCompare(pathB, 'zh-CN', { numeric: true, sensitivity: 'base' })
  })
})

const filteredFlatItems = computed(() => {
  const term = String(props.search || '').trim().toLowerCase()
  if (!term) return allFlatItems.value
  return allFlatItems.value.filter((item) => {
    const name = String(item.name || '').toLowerCase()
    const path = String(item.path || '').toLowerCase()
    return name.includes(term) || path.includes(term)
  })
})

function rowKey(item) {
  return String(item.relative_path || item.path || item.name || '')
}

function onTreeDragLeave(event) {
  const related = event.relatedTarget
  if (related && event.currentTarget?.contains?.(related)) return
  sandbox.clearDropTargetPath()
}
</script>

<style scoped>
.sb-tree {
  display: block;
  font-size: 12px;
  user-select: none;
  padding: 2px 0 8px;
}
.sb-tree.flat {
  padding-top: 4px;
}
.sb-empty {
  padding: 20px 12px;
  text-align: center;
  font-size: 11px;
  color: var(--text-2, rgba(255,255,255,0.5));
}
</style>
