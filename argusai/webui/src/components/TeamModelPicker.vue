<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { t } from '../i18n.js'
import WorkbenchModal from './workbench/WorkbenchModal.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  models: { type: Array, default: () => [] },
  current: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'select', 'refresh'])

const search = ref('')
const searchInput = ref(null)
const collapsed = ref(new Set())

const groups = computed(() => {
  const rows = []
  const byEndpoint = new Map()
  for (const model of props.models || []) {
    const endpoint = String(model.endpoint || model.name || t('team.modelOtherEndpoint'))
    if (!byEndpoint.has(endpoint)) {
      const group = { key: endpoint, title: endpoint, models: [], current: false }
      byEndpoint.set(endpoint, group)
      rows.push(group)
    }
    const group = byEndpoint.get(endpoint)
    group.models.push(model)
    if (String(model.value || '') === props.current) group.current = true
  }
  return rows
})

const query = computed(() => search.value.trim().toLowerCase())
const visibleGroups = computed(() => groups.value
  .map(group => ({
    ...group,
    models: query.value
      ? group.models.filter(model => (
          `${group.title} ${model.label || ''} ${model.model || ''} ${model.value || ''}`
            .toLowerCase()
            .includes(query.value)
        ))
      : group.models,
  }))
  .filter(group => group.models.length))

function resetCollapse() {
  const next = new Set()
  if (groups.value.length > 2) {
    for (const group of groups.value) {
      if (!group.current) next.add(group.key)
    }
  }
  collapsed.value = next
}

function isCollapsed(key) {
  return !query.value && collapsed.value.has(key)
}

function toggle(key) {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}

function pick(value) {
  emit('select', value)
  emit('close')
}

function onSearchKeydown(event) {
  if (event.key === 'Escape' && search.value) {
    event.preventDefault()
    search.value = ''
    return
  }
  if (event.key !== 'Enter' || event.isComposing) return
  const first = visibleGroups.value[0]?.models?.[0]
  if (!first) return
  event.preventDefault()
  pick(String(first.value || ''))
}

watch(
  () => props.open,
  async open => {
    if (!open) return
    search.value = ''
    resetCollapse()
    await nextTick()
    searchInput.value?.focus?.()
  },
)
</script>

<template>
  <WorkbenchModal
    :open="open"
    size="md"
    :title="t('assistant.acpModelTitle')"
    :close-label="t('common.close')"
    :close-on-esc="!query"
    @close="emit('close')"
  >
    <div class="team-model-picker">
      <label v-if="models.length" class="team-model-search">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="7" cy="7" r="4.2"/>
          <path d="m10.2 10.2 3.3 3.3"/>
        </svg>
        <input
          ref="searchInput"
          v-model="search"
          type="search"
          autofocus
          autocomplete="off"
          spellcheck="false"
          :placeholder="t('assistant.acpModelSearchPlaceholder')"
          @keydown="onSearchKeydown"
        />
        <button v-if="search" type="button" class="search-clear" @click="search = ''">×</button>
      </label>

      <p class="model-hint">{{ t('team.memberModelPickerHint') }}</p>

      <button type="button" class="default-model" :class="{ active: !current }" @click="pick('')">
        <strong>{{ t('team.modelDefault') }}</strong>
        <span v-if="!current" class="current-label">{{ t('assistant.acpModelCurrent') }}</span>
      </button>

      <p v-if="loading && !models.length" class="model-hint">{{ t('common.loading') }}</p>
      <div v-else-if="visibleGroups.length" class="model-groups">
        <section
          v-for="group in visibleGroups"
          :key="group.key"
          class="model-group"
          :class="{ current: group.current, collapsed: isCollapsed(group.key) }"
        >
          <button type="button" class="group-toggle" @click="toggle(group.key)">
            <span class="group-chevron" aria-hidden="true"></span>
            <span class="group-title">{{ group.title }}</span>
            <span class="group-meta">{{ t('assistant.acpModelCount', { n: group.models.length }) }}</span>
            <span v-if="group.current" class="current-label">{{ t('assistant.acpModelCurrent') }}</span>
          </button>
          <ul v-show="!isCollapsed(group.key)" class="model-list">
            <li v-for="model in group.models" :key="model.value">
              <button
                type="button"
                :class="{ active: String(model.value || '') === current }"
                :disabled="loading"
                @click="pick(String(model.value || ''))"
              >
                <strong>{{ model.label || model.model || model.value }}</strong>
                <span v-if="String(model.value || '') === current" class="current-label">
                  {{ t('assistant.acpModelCurrent') }}
                </span>
              </button>
            </li>
          </ul>
        </section>
      </div>
      <p v-else class="model-empty">
        {{ query ? t('assistant.acpModelNoMatch') : t('assistant.modelNoneNative') }}
      </p>
    </div>
    <template #footer>
      <button type="button" class="ghost" :disabled="loading" @click="emit('refresh')">
        {{ t('assistant.acpModelRefresh') }}
      </button>
      <button type="button" class="ghost" @click="emit('close')">{{ t('common.close') }}</button>
    </template>
  </WorkbenchModal>
</template>

<style scoped>
.team-model-picker { display: flex; flex-direction: column; gap: 12px; }
.team-model-search {
  height: 34px; display: flex; align-items: center; gap: 8px; padding: 0 10px;
  border: 1px solid var(--border); border-radius: 9px; background: var(--input-bg);
  color: var(--muted-2);
}
.team-model-search:focus-within { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.team-model-search svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.4; }
.team-model-search input { min-width: 0; flex: 1; padding: 0; border: 0; outline: 0; background: transparent; color: var(--fg); }
.search-clear { width: 20px; height: 20px; padding: 0; border: 0; background: transparent; color: var(--muted); }
.model-hint { margin: 0; color: var(--muted-2); font-size: 11px; line-height: 1.5; }
.default-model {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 9px 11px;
  border: 1px solid var(--border); border-radius: 9px; background: var(--card-bg); text-align: left;
}
.default-model strong { flex: 1; color: var(--fg-strong); font-size: 12px; }
.default-model.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.model-groups { display: flex; max-height: 420px; flex-direction: column; gap: 8px; overflow-y: auto; }
.model-group { border: 1px solid var(--border); border-radius: 10px; background: var(--bg-sunken); }
.model-group.current { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); }
.group-toggle {
  width: 100%; display: flex; align-items: center; gap: 8px; padding: 9px 11px;
  border: 0; background: transparent; color: var(--fg); text-align: left;
}
.group-toggle:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
.group-chevron {
  width: 8px; height: 8px; flex: none; border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted); transform: rotate(45deg); transition: transform .16s ease;
}
.model-group.collapsed .group-chevron { transform: rotate(-45deg); }
.group-title { min-width: 0; flex: 1; color: var(--fg-strong); font-size: 12px; font-weight: 600; }
.group-meta { color: var(--muted-2); font-size: 10px; }
.current-label { flex: none; color: var(--accent); font-size: 10px; }
.model-list { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 0 8px 8px; list-style: none; }
.model-list button {
  width: 100%; display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--card-bg); color: var(--fg); text-align: left;
}
.model-list button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.model-list button.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.model-list strong { min-width: 0; flex: 1; font-size: 12px; overflow-wrap: anywhere; }
.model-empty { padding: 22px 12px; color: var(--muted); font-size: 12px; text-align: center; }
</style>
