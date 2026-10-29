<script setup>
import { computed, inject } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { state } from '../../state.js'
import { useConfirm } from '../../composables/useConfirm.js'
import ProxyDecryptRules from './ProxyDecryptRules.vue'
import ProxyRewriteRules from './ProxyRewriteRules.vue'
import ProxyTagInput from './ProxyTagInput.vue'

const wb = inject('proxyWorkbench')
const { status, config, busy } = wb
const confirmDialog = useConfirm()

const MIME_SUGGESTIONS = ['api', 'document', 'script', 'image', 'video', 'application/json', 'video/*']

const systemProxySupported = computed(() => {
  const p = String(state.platform || '')
  return p === 'darwin' || p.startsWith('win')
})
const ca = computed(() => status.ca || {})
const caState = computed(() => {
  if (!ca.value.ca_exists) return 'missing'
  return ca.value.ca_trusted ? 'trusted' : 'untrusted'
})
const caStateLabel = computed(() => ({
  missing: t('proxy.caNotGenerated'),
  trusted: t('proxy.caGeneratedTrusted'),
  untrusted: t('proxy.caGeneratedUntrusted'),
}[caState.value]))
const formEndpoint = computed(() => `${config.host || '127.0.0.1'}:${config.port || 8866}`)
const dirty = computed(() => wb.configDirty.value)

async function uninstall() {
  const ok = await confirmDialog({
    title: t('proxy.uninstallCa'),
    content: t('proxy.caUninstallConfirm'),
    positiveText: t('proxy.uninstallCa'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (ok) await wb.uninstallCa()
}

function openCaDir() {
  wb.runFileAction(() => api.openProxyCaDir())
}
</script>

<template>
  <div class="proxy-settings">
    <div class="proxy-settings-grid">
      <section class="proxy-card">
        <header class="proxy-card-head">
          <h2>{{ t('proxy.caTitle') }}</h2>
          <span class="proxy-ca-state" :class="`is-${caState}`">{{ caStateLabel }}</span>
        </header>
        <p class="proxy-card-lead">{{ t('proxy.caNote') }}</p>
        <div v-if="ca.fingerprint_sha256" class="proxy-fingerprint">
          <span>SHA-256</span>
          <code class="mono selectable">{{ ca.fingerprint_sha256 }}</code>
        </div>
        <div class="proxy-card-actions">
          <button v-if="caState !== 'trusted'" type="button" class="primary" :disabled="busy.ca" @click="wb.installCa()">{{ t('proxy.installCa') }}</button>
          <button v-else type="button" class="danger" :disabled="busy.ca" @click="uninstall">{{ t('proxy.uninstallCa') }}</button>
          <button type="button" class="ghost" @click="openCaDir">{{ t('proxy.openCaDir') }}</button>
        </div>
      </section>

      <section class="proxy-card">
        <header class="proxy-card-head">
          <h2>{{ t('proxy.listenTitle') }}</h2>
        </header>
        <div class="proxy-form-pair">
          <label class="proxy-field">
            <span>{{ t('proxy.host') }}</span>
            <input v-model.trim="config.host" type="text" class="mono" spellcheck="false" />
          </label>
          <label class="proxy-field">
            <span>{{ t('proxy.port') }}</span>
            <input v-model.number="config.port" type="number" min="1" max="65535" class="mono" />
          </label>
        </div>
        <label v-if="systemProxySupported" class="proxy-toggle">
          <input v-model="config.useSystemProxy" type="checkbox" />
          <span>
            <strong>{{ t('proxy.useSystemProxy') }}</strong>
            <small>{{ t('proxy.systemProxyHint') }}</small>
          </span>
        </label>
        <p v-else class="proxy-card-lead">{{ t('proxy.systemProxyUnsupported') }}</p>
        <p class="proxy-manual">
          {{ t('proxy.manualProxyHint') }}
          <button type="button" class="proxy-endpoint mono" :title="t('proxy.copyEndpoint')" @click="wb.copy(formEndpoint, 'proxy.endpointCopied')">{{ formEndpoint }}</button>
        </p>
      </section>

      <section class="proxy-card proxy-card-wide">
        <header class="proxy-card-head">
          <h2>{{ t('proxy.scopeTitle') }}</h2>
        </header>
        <p class="proxy-card-lead">{{ t('proxy.scopeLead') }}</p>
        <div class="proxy-scope-grid">
          <div class="proxy-field">
            <span>{{ t('proxy.scopeInclude') }}</span>
            <ProxyTagInput
              v-model="config.scope"
              :placeholder="t('proxy.scopeIncludePlaceholder')"
              :aria-label="t('proxy.scopeInclude')"
              :remove-label="t('proxy.removeTag')"
            />
            <small>{{ t('proxy.scopeIncludeHint') }}</small>
          </div>
          <div class="proxy-field">
            <span>{{ t('proxy.scopeExclude') }}</span>
            <ProxyTagInput
              v-model="config.denyHosts"
              :placeholder="t('proxy.scopeExcludePlaceholder')"
              :aria-label="t('proxy.scopeExclude')"
              :remove-label="t('proxy.removeTag')"
            />
            <small>{{ t('proxy.scopeExcludeHint') }}</small>
          </div>
          <div class="proxy-field">
            <span>{{ t('proxy.mimeTitle') }}</span>
            <ProxyTagInput
              v-model="config.mimeAllow"
              :placeholder="t('proxy.mimePlaceholder')"
              :suggestions="MIME_SUGGESTIONS"
              :aria-label="t('proxy.mimeTitle')"
              :remove-label="t('proxy.removeTag')"
            />
            <small>{{ t('proxy.mimeHint') }}</small>
          </div>
        </div>
        <p class="proxy-bypass-note">{{ t('proxy.bypassNote') }}</p>
      </section>

      <ProxyDecryptRules />

      <ProxyRewriteRules />
    </div>

    <!-- Actions sit on the left: the assistant FAB floats over the bottom-right corner. -->
    <footer class="proxy-settings-footer" :class="{ dirty }">
      <button type="button" class="primary" :disabled="!dirty || busy.save || busy.start || busy.stop" @click="wb.saveAndApply()">
        {{ status.running ? t('proxy.saveAndRestart') : t('proxy.saveConfig') }}
      </button>
      <button type="button" class="ghost" :disabled="!dirty || busy.save" @click="wb.loadConfig()">{{ t('proxy.discardChanges') }}</button>
      <span class="proxy-settings-state">
        {{ dirty ? (status.running ? t('proxy.unsavedRunning') : t('proxy.unsaved')) : t('proxy.allSaved') }}
      </span>
    </footer>
  </div>
</template>

<style scoped>
.proxy-settings {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.proxy-settings-grid {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  align-content: start;
  gap: 14px;
  padding: 2px 2px 16px;
}
.proxy-card {
  min-width: 0;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card-bg);
}
.proxy-card-wide { grid-column: 1 / -1; }
.proxy-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}
.proxy-card-head h2 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 14px;
  font-weight: 600;
}
.proxy-card-lead {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.proxy-ca-state {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.proxy-ca-state.is-trusted { background: color-mix(in srgb, var(--ok) 14%, transparent); color: var(--ok); }
.proxy-ca-state.is-untrusted { background: color-mix(in srgb, var(--warn) 14%, transparent); color: var(--warn); }
.proxy-ca-state.is-missing { background: var(--panel-2); color: var(--muted); }
.proxy-fingerprint {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--bg-sunken);
}
.proxy-fingerprint span {
  color: var(--muted-2);
  font-size: 11px;
}
.proxy-fingerprint code {
  color: var(--fg);
  font-size: 11px;
  line-height: 1.5;
  word-break: break-all;
}
.proxy-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.proxy-form-pair {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(90px, 1fr);
  gap: 10px;
  margin-bottom: 12px;
}
.proxy-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.proxy-field > span {
  color: var(--fg);
  font-size: 12px;
  font-weight: 600;
}
.proxy-field > small {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}
.proxy-toggle {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
}
.proxy-toggle input { margin-top: 2px; }
.proxy-toggle span {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.proxy-toggle strong { color: var(--fg-strong); font-size: 12px; }
.proxy-toggle small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.proxy-manual {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.proxy-endpoint {
  display: inline;
  height: auto;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--fg-strong);
  font-size: 12px;
}
.proxy-scope-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}
.proxy-bypass-note {
  margin: 14px 0 0;
  color: var(--muted-2);
  font-size: 11px;
  line-height: 1.5;
}
.proxy-settings-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 2px 0;
  border-top: 1px solid var(--border);
}
.proxy-settings-state {
  margin-left: 6px;
  color: var(--muted);
  font-size: 12px;
}
.proxy-settings-footer.dirty .proxy-settings-state { color: var(--warn); }
.selectable {
  user-select: text;
  -webkit-user-select: text;
  cursor: text;
}
</style>
