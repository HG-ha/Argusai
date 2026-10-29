<script setup>
import { computed, onMounted, onUnmounted, provide, ref } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { useProxyWorkbench } from '../composables/useProxyWorkbench.js'
import ProxyActionMenu from './proxy/ProxyActionMenu.vue'
import ProxySettingsView from './proxy/ProxySettingsView.vue'
import ProxyTrafficView from './proxy/ProxyTrafficView.vue'
import ProxyBreakpoints from './proxy/ProxyBreakpoints.vue'

const wb = useProxyWorkbench()
provide('proxyWorkbench', wb)
const { status, busy } = wb

const view = ref('traffic')
const settingsNeedAttention = computed(() => !wb.caTrusted.value || wb.configDirty.value)

const ioItems = computed(() => [
  { key: 'exportHar', label: t('proxy.exportHar'), disabled: !status.flow_count },
  { key: 'importHar', label: t('proxy.importHar') },
  { key: 'importBurp', label: t('proxy.importBurpLog') },
  { divider: true },
  { key: 'openExports', label: t('proxy.openExportsDir') },
  { key: 'openCaptures', label: t('proxy.openCapturesDir') },
])

function onIo(key) {
  if (key === 'exportHar') {
    wb.runFileAction(() => api.exportProxyHar(0, status.flow_cap || 500, ''), (r) => t('proxy.harExported', { n: r.entry_count || 0 }))
  } else if (key === 'importHar') {
    wb.runFileAction(() => api.importProxyHar(''), (r) => t('proxy.harImported', { n: r.entry_count || 0 }))
  } else if (key === 'importBurp') {
    wb.runFileAction(() => api.importProxyBurpLog(''), (r) => t('proxy.burpImported', { n: r.entry_count || 0 }))
  } else if (key === 'openExports') {
    wb.runFileAction(() => api.openProxyExportsDir())
  } else if (key === 'openCaptures') {
    wb.runFileAction(() => api.openProxyCapturesDir())
  }
}

onMounted(() => { wb.init() })
onUnmounted(() => { wb.stopPolling() })
</script>

<template>
  <div class="page proxy-page">
    <header class="proxy-head">
      <div class="proxy-head-title">
        <h1>{{ t('proxy.title') }}</h1>
        <span class="proxy-state" :class="{ on: status.running }">
          <span class="proxy-state-dot" aria-hidden="true"></span>
          {{ status.running ? t('proxy.stateRunning', { endpoint: wb.endpoint.value }) : t('proxy.stateStopped') }}
        </span>
        <span v-if="status.system_proxy_active" class="proxy-head-chip">{{ t('proxy.systemProxyOn') }}</span>
        <button
          v-if="status.ca && !wb.caTrusted.value"
          type="button"
          class="proxy-head-chip warn"
          @click="view = 'settings'"
        >{{ t('proxy.caNeedsTrust') }}</button>
      </div>
      <div class="proxy-head-actions">
        <ProxyActionMenu :label="t('proxy.importExport')" :items="ioItems" :disabled="busy.io" @select="onIo" />
        <button v-if="!status.running" type="button" class="primary" :disabled="busy.start" @click="wb.start()">{{ t('proxy.start') }}</button>
        <button v-else type="button" class="danger" :disabled="busy.stop" @click="wb.stop()">{{ t('proxy.stop') }}</button>
      </div>
    </header>

    <nav class="proxy-tabs" role="tablist" :aria-label="t('proxy.title')">
      <button
        type="button"
        role="tab"
        class="proxy-tab"
        :class="{ on: view === 'traffic' }"
        :aria-selected="view === 'traffic'"
        @click="view = 'traffic'"
      >
        {{ t('proxy.tabTraffic') }}
        <span class="proxy-tab-count mono">{{ status.flow_count }}</span>
      </button>
      <button
        type="button"
        role="tab"
        class="proxy-tab"
        :class="{ on: view === 'settings' }"
        :aria-selected="view === 'settings'"
        @click="view = 'settings'"
      >
        {{ t('proxy.tabSettings') }}
        <span v-if="settingsNeedAttention" class="proxy-tab-alert" :title="t('proxy.settingsNeedAttention')"></span>
      </button>
    </nav>

    <ProxyBreakpoints />

    <ProxyTrafficView v-show="view === 'traffic'" @open-settings="view = 'settings'" />
    <ProxySettingsView v-if="view === 'settings'" />
  </div>
</template>

<style scoped>
.proxy-page {
  height: 100%;
  min-height: 0;
  overflow: hidden !important;
  padding-bottom: 14px;
}
.proxy-head {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
}
.proxy-head-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  min-width: 0;
}
.proxy-head-title h1 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 20px;
  font-weight: 650;
}
.proxy-state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
}
.proxy-state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted-2);
}
.proxy-state.on { color: var(--fg); }
.proxy-state.on .proxy-state-dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 20%, transparent);
}
.proxy-head-chip {
  height: 22px;
  padding: 0 8px;
  border: 0;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  line-height: 22px;
}
.proxy-head-chip.warn {
  background: color-mix(in srgb, var(--warn) 14%, transparent);
  color: var(--warn);
}
.proxy-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.proxy-tabs {
  flex: 0 0 auto;
  display: flex;
  gap: 4px;
  margin: 14px 0 12px;
  border-bottom: 1px solid var(--border);
}
.proxy-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 34px;
  margin-bottom: -1px;
  padding: 0 12px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
}
.proxy-tab:hover:not(:disabled) {
  background: transparent;
  color: var(--fg);
}
.proxy-tab.on {
  border-bottom-color: var(--accent);
  color: var(--fg-strong);
  font-weight: 600;
}
.proxy-tab-count {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
  font-weight: 500;
}
.proxy-tab-alert {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warn);
}
</style>
