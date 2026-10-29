<script setup>
import { computed, ref } from 'vue'
import { NButton, NEmpty, NInput } from 'naive-ui'
import { t } from '../../i18n.js'

const props = defineProps({
  presets: { type: Array, default: () => [] },
  categories: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

const query = ref('')
const activeCategory = ref('all')

const categoryNames = computed(() => ({
  official: t('settings.aiProviderCategoryOfficial'),
  cn_official: t('settings.aiProviderCategoryChina'),
  aggregator: t('settings.aiProviderCategoryAggregator'),
  third_party: t('settings.aiProviderCategoryThirdParty'),
}))

const categoryTabs = computed(() => {
  const known = props.categories
    .map(category => category.id)
    .filter(id => id && id !== 'custom')
  const ids = known.length
    ? known
    : ['official', 'cn_official', 'aggregator', 'third_party']
  return ids.map(id => ({
    id,
    label: categoryNames.value[id] || id,
    count: props.presets.filter(provider => provider.category === id).length,
  }))
})

const filteredProviders = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase()
  return props.presets.filter(provider => {
    if (activeCategory.value !== 'all' && provider.category !== activeCategory.value) return false
    if (!needle) return true
    const haystack = [
      provider.name,
      provider.id,
      provider.base_url,
      provider.default_model,
      ...(provider.aliases || []),
    ].join(' ').toLocaleLowerCase()
    return haystack.includes(needle)
  })
})

const groupedProviders = computed(() => categoryTabs.value
  .map(category => ({
    ...category,
    providers: filteredProviders.value.filter(provider => provider.category === category.id),
  }))
  .filter(category => category.providers.length))

function protocolLabel(protocol) {
  return protocol === 'anthropic' ? 'Anthropic Messages' : 'OpenAI Chat'
}

function regionLabel(region) {
  const labels = {
    global: t('settings.aiRegionGlobal'),
    cn: t('settings.aiRegionChina'),
    apac: t('settings.aiRegionApac'),
  }
  return labels[region] || region || ''
}
</script>

<template>
  <section class="provider-picker" :aria-busy="loading">
    <div class="provider-picker-controls">
      <NInput
        v-model:value="query"
        autofocus
        clearable
        :placeholder="t('settings.aiProviderSearchPlaceholder')"
        :aria-label="t('settings.aiProviderSearchPlaceholder')"
      />
      <NButton secondary size="small" @click="emit('select', 'custom')">
        {{ t('settings.aiProviderCustom') }}
      </NButton>
    </div>

    <nav class="provider-category-tabs" :aria-label="t('settings.aiProviderCategoryLabel')">
      <button
        type="button"
        :class="{ active: activeCategory === 'all' }"
        @click="activeCategory = 'all'"
      >
        <span>{{ t('settings.aiProviderCategoryAll') }}</span>
        <em>{{ presets.length }}</em>
      </button>
      <button
        v-for="category in categoryTabs"
        :key="category.id"
        type="button"
        :class="{ active: activeCategory === category.id }"
        @click="activeCategory = category.id"
      >
        <span>{{ category.label }}</span>
        <em>{{ category.count }}</em>
      </button>
    </nav>

    <div class="provider-picker-results">
      <div v-if="loading" class="provider-picker-loading">
        {{ t('common.loading') }}
      </div>
      <NEmpty
        v-else-if="!groupedProviders.length"
        size="small"
        :description="t('settings.aiProviderNoResults')"
      />
      <section
        v-for="category in groupedProviders"
        v-else
        :key="category.id"
        class="provider-result-group"
      >
        <header>
          <h3>{{ category.label }}</h3>
          <span>{{ category.providers.length }}</span>
        </header>
        <div class="provider-result-grid">
          <button
            v-for="provider in category.providers"
            :key="provider.id"
            type="button"
            class="provider-result"
            :class="{ selected: provider.id === selectedId }"
            :aria-pressed="provider.id === selectedId"
            @click="emit('select', provider.id)"
          >
            <span class="provider-result-title">
              <strong>{{ provider.name }}</strong>
              <em v-if="provider.id === selectedId">{{ t('settings.aiProviderCurrent') }}</em>
            </span>
            <span class="provider-result-meta">
              <span>{{ protocolLabel(provider.protocol) }}</span>
              <span>{{ regionLabel(provider.region) }}</span>
              <span v-if="provider.requires_base_url_edit" class="needs-edit">
                {{ t('settings.aiProviderNeedsEdit') }}
              </span>
            </span>
            <span class="provider-result-url mono">{{ provider.base_url }}</span>
            <span class="provider-result-model">
              <span>{{ t('settings.aiProviderDefaultModel') }}</span>
              <strong class="mono">{{ provider.default_model || '—' }}</strong>
            </span>
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.provider-picker {
  display: flex;
  min-height: 0;
  flex-direction: column;
  gap: 12px;
}

.provider-picker-controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.provider-category-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  padding-bottom: 2px;
  scrollbar-width: thin;
}

.provider-category-tabs button {
  display: inline-flex;
  min-height: 30px;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.provider-category-tabs button:hover {
  background: var(--panel-hover);
  color: var(--fg);
}

.provider-category-tabs button.active {
  border-color: color-mix(in srgb, var(--accent) 32%, var(--border));
  background: var(--accent-soft);
  color: var(--accent);
}

.provider-category-tabs em {
  min-width: 17px;
  padding: 1px 5px;
  border-radius: 999px;
  background: color-mix(in srgb, currentColor 10%, transparent);
  color: inherit;
  font-size: 9px;
  font-style: normal;
  line-height: 1.35;
  text-align: center;
}

.provider-picker-results {
  min-height: 260px;
  max-height: min(590px, calc(100dvh - var(--titlebar-h, 0px) - 245px));
  overflow: auto;
  padding-right: 3px;
}

.provider-picker-loading {
  display: grid;
  min-height: 260px;
  place-items: center;
  color: var(--muted);
  font-size: 12px;
}

.provider-result-group + .provider-result-group {
  margin-top: 18px;
}

.provider-result-group > header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 7px;
}

.provider-result-group h3 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 11.5px;
  font-weight: 600;
}

.provider-result-group > header > span {
  color: var(--muted-2);
  font: 10px/1 var(--mono-font, ui-monospace, monospace);
}

.provider-result-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
}

.provider-result {
  display: flex;
  min-width: 0;
  min-height: 116px;
  flex-direction: column;
  align-items: stretch;
  gap: 7px;
  padding: 11px 12px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 140ms ease, box-shadow 140ms ease;
}

.provider-result:nth-child(odd) {
  border-right: 1px solid var(--border);
}

.provider-result:nth-last-child(-n + 2) {
  border-bottom: 0;
}

.provider-result:nth-last-child(2):nth-child(even) {
  border-bottom: 1px solid var(--border);
}

.provider-result:hover {
  background: var(--panel-hover);
}

.provider-result.selected {
  background: color-mix(in srgb, var(--accent) 8%, var(--card-bg));
  box-shadow: inset 3px 0 0 var(--accent);
}

.provider-result:focus-visible {
  position: relative;
  z-index: 1;
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.provider-result-title {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.provider-result-title strong {
  overflow: hidden;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.provider-result-title em {
  flex: 0 0 auto;
  color: var(--accent);
  font-size: 9.5px;
  font-style: normal;
  font-weight: 600;
}

.provider-result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 9px;
  color: var(--muted);
  font-size: 10px;
}

.provider-result-meta .needs-edit {
  color: var(--warn);
}

.provider-result-url {
  overflow: hidden;
  color: var(--muted-2);
  font-size: 9.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.provider-result-model {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 6px;
  margin-top: auto;
  color: var(--muted);
  font-size: 9.5px;
}

.provider-result-model strong {
  overflow: hidden;
  color: var(--fg);
  font-size: 10px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 640px) {
  .provider-picker-controls {
    grid-template-columns: 1fr;
  }

  .provider-result-grid {
    grid-template-columns: 1fr;
  }

  .provider-result {
    min-height: 108px;
    border-right: 0 !important;
    border-bottom: 1px solid var(--border) !important;
  }

  .provider-result:last-child {
    border-bottom: 0 !important;
  }

  .provider-picker-results {
    max-height: calc(100dvh - var(--titlebar-h, 0px) - 260px);
  }
}
</style>
