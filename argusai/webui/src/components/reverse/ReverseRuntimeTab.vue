<script setup>
import { computed, ref, watch } from 'vue'
import { NButton, NTag } from 'naive-ui'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { staticReportArtifacts, staticReportLabel } from './reverseUtils.js'

const props = defineProps({
  jobId: { type: String, default: '' },
  detail: { type: Object, default: null },
  mode: { type: String, default: 'all' },
})
const emit = defineEmits(['refresh'])

const scanning = ref(false)
const scanError = ref('')
const fridaSessions = ref([])
const fridaLoading = ref(false)
const stoppingId = ref('')

const toolchain = computed(() => props.detail?.toolchain_status || {})
const toolEntries = computed(() => {
  const tools = toolchain.value?.tools || {}
  return Object.entries(tools).map(([name, info]) => ({ name, ...(info || {}) }))
})

const targetMeta = computed(() => props.detail?.target_meta || {})
const analysis = computed(() => props.detail?.analysis_summary || {})
const decompileEntries = computed(() => {
  const raw = analysis.value?.static?.decompile || {}
  return Object.entries(raw).map(([tool, info]) => ({
    tool,
    ...(info || {}),
  }))
})
const risk = computed(() => props.detail?.risk_summary || {})
const runtimeSessions = computed(() => props.detail?.runtime_sessions || [])
const evidenceRefs = computed(() => props.detail?.evidence_refs || [])
const replayRefs = computed(() => props.detail?.replay_refs || [])
const staticReports = computed(() => staticReportArtifacts(props.detail?.artifacts || []))
const showStatic = computed(() => props.mode !== 'dynamic')
const showDynamic = computed(() => props.mode !== 'static')

async function scanToolchain() {
  if (!props.jobId) return
  scanning.value = true
  scanError.value = ''
  try {
    const res = await api.reverseToolchainScan(props.jobId, [])
    if (!res?.ok) scanError.value = res?.error || t('reverse.runtime.scanFailed')
    emit('refresh')
  } catch (e) {
    scanError.value = String(e?.message || e)
  } finally {
    scanning.value = false
  }
}

async function loadFridaSessions() {
  fridaLoading.value = true
  try {
    const res = await api.fridaSessionList()
    if (res?.ok) fridaSessions.value = res.sessions || []
  } catch {
    fridaSessions.value = []
  } finally {
    fridaLoading.value = false
  }
}

async function stopFridaSession(sessionId) {
  if (!sessionId) return
  stoppingId.value = sessionId
  try {
    const res = await api.fridaSessionStop(sessionId, props.jobId || '')
    if (res?.ok) {
      await loadFridaSessions()
      emit('refresh')
    }
  } finally {
    stoppingId.value = ''
  }
}

function decompileLabel(tool) {
  const key = String(tool || '')
  if (key.includes('jadx')) return 'JADX'
  if (key.includes('apktool')) return 'APKTool'
  return key
}

async function openOutputDir(dir) {
  const path = String(dir || '').trim()
  if (!path) return
  try {
    const res = await api.openFolder(path)
    if (!res?.ok) scanError.value = res?.error || t('reverse.runtime.openDirFailed')
  } catch (e) {
    scanError.value = String(e?.message || e)
  }
}

watch(() => props.jobId, () => {
  scanError.value = ''
  if (props.jobId) loadFridaSessions()
}, { immediate: true })
</script>

<template>
  <div class="rev-runtime">
    <div class="rev-runtime-grid">
      <section class="rev-info-card">
        <div class="rev-card-head">
          <h4>{{ t('reverse.runtime.target') }}</h4>
        </div>
        <dl>
          <dt>{{ t('reverse.runtime.targetType') }}</dt>
          <dd><span class="rev-pill">{{ detail?.target_type || 'unknown' }}</span></dd>
          <dt>{{ t('reverse.runtime.platform') }}</dt>
          <dd><span class="rev-pill">{{ detail?.platform || 'unknown' }}</span></dd>
          <dt v-if="targetMeta.package">{{ t('reverse.runtime.package') }}</dt>
          <dd v-if="targetMeta.package"><code>{{ targetMeta.package }}</code></dd>
          <dt v-if="targetMeta.bundle_id">{{ t('reverse.runtime.bundleId') }}</dt>
          <dd v-if="targetMeta.bundle_id"><code>{{ targetMeta.bundle_id }}</code></dd>
          <dt v-if="targetMeta.sha256">SHA-256</dt>
          <dd v-if="targetMeta.sha256"><code class="rev-hash">{{ targetMeta.sha256 }}</code></dd>
          <dt v-if="targetMeta.format">{{ t('reverse.runtime.format') }}</dt>
          <dd v-if="targetMeta.format">{{ targetMeta.format }}</dd>
        </dl>
      </section>

      <section class="rev-info-card">
        <div class="rev-card-head">
          <h4>{{ t('reverse.runtime.analysis') }}</h4>
        </div>
        <dl>
          <dt v-if="showStatic">{{ t('reverse.runtime.static') }}</dt>
          <dd v-if="showStatic">
            <NTag size="tiny" :bordered="false" :type="analysis.static?.done ? 'success' : 'default'">
              {{ analysis.static?.done ? t('reverse.runtime.done') : ((analysis.static?.tools || []).length ? t('reverse.runtime.progress') : t('reverse.runtime.unknown')) }}
            </NTag>
            <span v-if="(analysis.static?.tools || []).length" class="rev-inline-tools">{{ (analysis.static.tools || []).join(', ') }}</span>
          </dd>
          <dt v-if="showDynamic">{{ t('reverse.runtime.dynamic') }}</dt>
          <dd v-if="showDynamic">
            <NTag size="tiny" :bordered="false" :type="analysis.dynamic?.done ? 'success' : 'default'">
              {{ analysis.dynamic?.done ? t('reverse.runtime.done') : ((analysis.dynamic?.tools || []).length ? t('reverse.runtime.progress') : t('reverse.runtime.unknown')) }}
            </NTag>
            <span v-if="(analysis.dynamic?.tools || []).length" class="rev-inline-tools">{{ (analysis.dynamic.tools || []).join(', ') }}</span>
          </dd>
          <dt>{{ t('reverse.runtime.risk') }}</dt>
          <dd>
            {{ risk.overall || 'unknown' }}
            <span v-for="tag in (risk.tags || [])" :key="tag" class="rev-risk-tag">{{ tag }}</span>
          </dd>
        </dl>
      </section>
    </div>

    <section v-if="showStatic && decompileEntries.length" class="rev-info-card">
      <div class="rev-card-head">
        <h4>{{ t('reverse.runtime.decompile') }}</h4>
      </div>
      <ul class="rev-decompile-list">
        <li v-for="entry in decompileEntries" :key="entry.tool">
          <div class="rev-decompile-head">
            <span class="rev-pill">{{ decompileLabel(entry.tool) }}</span>
            <span class="rev-muted-inline">{{ entry.file_count || 0 }} {{ t('reverse.runtime.files') }}</span>
            <span v-if="entry.finished_at" class="rev-muted-inline">{{ entry.finished_at }}</span>
          </div>
          <div class="rev-decompile-actions">
            <code v-if="entry.output_dir" class="rev-decompile-path">{{ entry.output_dir }}</code>
            <NButton
              v-if="entry.output_dir"
              size="tiny"
              quaternary
              @click="openOutputDir(entry.output_dir)"
            >{{ t('reverse.runtime.openDir') }}</NButton>
          </div>
          <div v-if="entry.index_artifact_ref" class="rev-muted-inline">
            {{ entry.index_artifact_ref }}
          </div>
        </li>
      </ul>
    </section>

    <section v-if="showStatic && staticReports.length" class="rev-info-card">
      <div class="rev-card-head">
        <h4>{{ t('reverse.runtime.staticReports') }}</h4>
      </div>
      <ul class="rev-decompile-list">
        <li v-for="art in staticReports" :key="art.id">
          <div class="rev-decompile-head">
            <span class="rev-pill">{{ staticReportLabel(art.kind) }}</span>
            <code>{{ art.id }}</code>
            <span v-if="art.created_at" class="rev-muted-inline">{{ art.created_at }}</span>
          </div>
          <div class="rev-muted-inline">{{ art.tool || art.note || art.filename || '' }}</div>
        </li>
      </ul>
      <p class="rev-muted-inline">{{ t('reverse.runtime.staticReportsHint') }}</p>
    </section>

    <section v-if="showStatic" class="rev-info-card rev-toolchain-card">
      <div class="rev-card-head">
        <h4>{{ t('reverse.runtime.toolchain') }}</h4>
        <NButton size="tiny" :loading="scanning" @click="scanToolchain">{{ t('reverse.runtime.scan') }}</NButton>
      </div>
      <p v-if="toolchain.scanned_at" class="rev-muted-inline">{{ t('reverse.runtime.scannedAt') }}: {{ toolchain.scanned_at }}</p>
      <p v-if="scanError" class="rev-inline-error">{{ scanError }}</p>
      <div v-if="!toolEntries.length" class="rev-muted">{{ t('reverse.runtime.noToolchain') }}</div>
      <ul v-else class="rev-tool-list">
        <li v-for="tool in toolEntries" :key="tool.name" :class="{ ok: tool.available, miss: !tool.available }">
          <span class="rev-tool-name">{{ tool.name }}</span>
          <span class="rev-tool-state">
            {{ tool.available ? (tool.version || t('reverse.runtime.available')) : t('reverse.runtime.missing') }}
            <span v-if="tool.path" class="rev-tool-path" :title="tool.path">· {{ tool.path }}</span>
          </span>
        </li>
      </ul>
    </section>

    <section v-if="showDynamic" class="rev-info-card">
      <div class="rev-card-head">
        <h4>{{ t('reverse.runtime.sessions') }}</h4>
        <NButton size="tiny" quaternary :loading="fridaLoading" @click="loadFridaSessions">{{ t('reverse.toolbar.refresh') }}</NButton>
      </div>
      <div v-if="!runtimeSessions.length" class="rev-muted">{{ t('reverse.runtime.noSessions') }}</div>
      <ul v-else class="rev-session-list">
        <li v-for="s in runtimeSessions" :key="s.id">
          <span class="rev-pill">{{ s.kind }}</span>
          <code>{{ s.target }}</code>
          <span class="rev-session-state" :data-state="s.state">{{ s.state }}</span>
          <span class="rev-muted-inline">{{ s.tool }}</span>
        </li>
      </ul>
      <div v-if="fridaSessions.length" class="rev-frida-live">
        <h5>{{ t('reverse.runtime.fridaLive') }}</h5>
        <ul class="rev-session-list">
          <li v-for="s in fridaSessions" :key="s.session_id">
            <code>{{ s.session_id }}</code>
            <span>{{ s.mode }} → {{ s.target }}</span>
            <NButton
              v-if="s.state === 'running'"
              size="tiny"
              quaternary
              :loading="stoppingId === s.session_id"
              @click="stopFridaSession(s.session_id)"
            >{{ t('reverse.runtime.stop') }}</NButton>
          </li>
        </ul>
      </div>
    </section>

    <section v-if="props.mode === 'all' && (evidenceRefs.length || replayRefs.length)" class="rev-info-card">
      <h4>{{ t('reverse.runtime.links') }}</h4>
      <div v-if="evidenceRefs.length">
        <div class="rev-link-label">{{ t('reverse.runtime.evidence') }}</div>
        <ul class="rev-link-list">
          <li v-for="ev in evidenceRefs" :key="ev.id"><code>{{ ev.type }}</code> {{ ev.ref_id }}</li>
        </ul>
      </div>
      <div v-if="replayRefs.length">
        <div class="rev-link-label">{{ t('reverse.runtime.replay') }}</div>
        <ul class="rev-link-list">
          <li v-for="rp in replayRefs" :key="rp.id"><code>{{ rp.type }}</code> {{ rp.ref_id }}</li>
        </ul>
      </div>
    </section>

    <p v-if="showDynamic" class="rev-agent-hint">{{ t('reverse.runtime.agentHint') }}</p>
  </div>
</template>

<style scoped>
.rev-runtime { display: flex; flex-direction: column; gap: 12px; }
.rev-runtime-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.rev-info-card {
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
}
.rev-info-card h4, .rev-info-card h5 { margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px; color: var(--muted, #888); }
.rev-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.rev-info-card dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 11.5px;
}
.rev-info-card dt { color: var(--muted, #888); }
.rev-info-card dd { margin: 0; word-break: break-all; }
.rev-hash { font-size: 10px; }
.rev-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  color: var(--accent, #4e8cff);
}
.rev-muted { color: var(--muted, #888); font-size: 12px; padding: 8px 0; }
.rev-muted-inline { color: var(--muted, #888); font-size: 11px; }
.rev-inline-error { color: var(--danger, #f55); font-size: 11px; margin: 4px 0; }
.rev-inline-tools { margin-left: 6px; font-size: 11px; color: var(--muted, #888); }
.rev-risk-tag {
  display: inline-block;
  margin-left: 4px;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 10px;
  background: color-mix(in srgb, var(--warning, #d29c4a) 16%, transparent);
  color: var(--warning, #d29c4a);
}
.rev-tool-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 4px; }
.rev-tool-list li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 11px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.15));
}
.rev-tool-list li.ok { border-color: color-mix(in srgb, var(--success, #4ad27a) 35%, transparent); }
.rev-tool-list li.miss { opacity: 0.7; }
.rev-tool-name { font-family: "Cascadia Code", Consolas, monospace; }
.rev-tool-state { color: var(--muted, #888); font-size: 10px; }
.rev-tool-path { opacity: 0.75; word-break: break-all; }
.rev-decompile-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.rev-decompile-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.rev-decompile-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.rev-decompile-path { font-size: 10px; word-break: break-all; max-width: 100%; }
.rev-session-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; font-size: 11.5px; }
.rev-session-list li { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.rev-session-state { font-size: 10px; text-transform: uppercase; padding: 1px 5px; border-radius: 3px; background: color-mix(in srgb, var(--fg, #fff) 8%, transparent); }
.rev-session-state[data-state="running"] { color: var(--success, #4ad27a); }
.rev-session-state[data-state="failed"] { color: var(--danger, #f55); }
.rev-frida-live { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border, rgba(127, 127, 127, 0.2)); }
.rev-link-label { font-size: 11px; color: var(--muted, #888); margin: 6px 0 4px; }
.rev-link-list { list-style: none; margin: 0 0 8px; padding: 0; font-size: 11px; }
.rev-link-list li { padding: 2px 0; }
.rev-agent-hint {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 11.5px;
  color: var(--muted, #888);
  border: 1px dashed color-mix(in srgb, var(--accent, #4e8cff) 30%, transparent);
  background: color-mix(in srgb, var(--accent, #4e8cff) 5%, transparent);
}
</style>
