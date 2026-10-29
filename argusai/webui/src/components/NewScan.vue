<script setup>
import { computed, reactive, ref, onMounted } from 'vue'
import { api } from '../api.js'
import { navigate } from '../router.js'
import { t } from '../i18n.js'
import { state, showToast } from '../state.js'
import { NSelect } from 'naive-ui'

const props = defineProps({
  /** When true, render inside TaskList as embedded card. */
  embedded: { type: Boolean, default: false },
})
const emit = defineEmits(['created', 'close'])

const form = reactive({
  url: '',
  name: '',
  scope: '',
  paths: '',
  headers: '',
  headless: true,
  observe_xss: false,
  cdp_url: '',
  ai_endpoint: '',
  checks: new Set(),
})
const submitting = ref(false)
const launchingChrome = ref(false)
const expandedCheckGroups = ref(new Set())
const aiSettings = reactive({ enabled: true, endpoints: [] })

const CHECK_GROUP_DEFS = [
  {
    id: 'exposure',
    titleKey: 'newScan.groupExposure',
    helpKey: 'newScan.groupExposureHelp',
    checkIds: ['recon', 'headers', 'tls', 'secrets', 'http_desync'],
  },
  {
    id: 'identity',
    titleKey: 'newScan.groupIdentity',
    helpKey: 'newScan.groupIdentityHelp',
    checkIds: ['auth', 'business', 'access_control', 'identity'],
  },
  {
    id: 'input',
    titleKey: 'newScan.groupInput',
    helpKey: 'newScan.groupInputHelp',
    checkIds: ['fuzzer', 'injection', 'scanner', 'sqli_deep', 'xss', 'upload'],
  },
  {
    id: 'analysis',
    titleKey: 'newScan.groupAnalysis',
    helpKey: 'newScan.groupAnalysisHelp',
    checkIds: ['js_reverse', 'ai_recon', 'ai_poc'],
  },
]
const GROUP_BY_CHECK_ID = Object.fromEntries(
  CHECK_GROUP_DEFS.flatMap(group => group.checkIds.map(id => [id, group.id])),
)
const ACTIVE_CHECK_IDS = new Set([
  'auth', 'business', 'access_control', 'identity', 'http_desync',
  'fuzzer', 'injection', 'scanner', 'sqli_deep', 'xss',
])
const AI_CHECK_IDS = new Set(['ai_recon', 'ai_poc'])

const aiEndpointOptions = computed(() => {
  const opts = [{ label: t('newScan.aiEndpointAuto'), value: '' }]
  if (!aiSettings.enabled) return opts
  aiSettings.endpoints.filter(ep => ep.enabled !== false).forEach((ep, idx) => {
    const name = ep.name || ep.model || ep.base_url || `#${idx + 1}`
    const model = ep.model ? ` · ${ep.model}` : ''
    opts.push({
      label: `${name}${model}`,
      value: `${ep.name || ''}|${ep.base_url || ''}|${ep.model || ''}`,
    })
  })
  return opts
})

const browserMode = computed({
  get() {
    if (form.cdp_url) return 'cdp'
    return form.headless ? 'headless' : 'headed'
  },
  set(val) {
    if (val === 'headless') {
      form.headless = true
      form.cdp_url = ''
    } else if (val === 'headed') {
      form.headless = false
      form.cdp_url = ''
    } else if (val === 'cdp') {
      if (!form.cdp_url) form.cdp_url = 'http://127.0.0.1:9222'
    }
  },
})

const observeXssMode = computed({
  get() { return form.observe_xss ? 'on' : 'off' },
  set(val) { form.observe_xss = (val === 'on') },
})
const browserModeOptions = computed(() => [
  { label: t('newScan.browserHeadless'), value: 'headless' },
  { label: t('newScan.browserHeaded'), value: 'headed' },
  { label: t('newScan.browserCdp'), value: 'cdp' },
])
const observeXssOptions = computed(() => [
  { label: t('newScan.observeXssOff'), value: 'off' },
  { label: t('newScan.observeXssOn'), value: 'on' },
])

const totalChecksCount = computed(() => state.checks.length)
const selectedChecksCount = computed(() => form.checks.size)
const checkGroups = computed(() => {
  const grouped = new Map(CHECK_GROUP_DEFS.map(group => [group.id, []]))
  for (const check of state.checks) {
    const groupId = GROUP_BY_CHECK_ID[check.id] || 'exposure'
    grouped.get(groupId).push(check)
  }
  return CHECK_GROUP_DEFS.map(group => {
    const checks = grouped.get(group.id)
    return {
      ...group,
      title: t(group.titleKey),
      help: t(group.helpKey),
      checks,
      selectedCount: checks.filter(check => form.checks.has(check.id)).length,
      activeCount: checks.filter(check => ACTIVE_CHECK_IDS.has(check.id)).length,
      browserCount: checks.filter(check => check.needs_browser).length,
      aiCount: checks.filter(check => AI_CHECK_IDS.has(check.id)).length,
    }
  })
})
const selectedChecks = computed(() => state.checks.filter(check => form.checks.has(check.id)))
const selectedActiveCount = computed(() =>
  selectedChecks.value.filter(check => ACTIVE_CHECK_IDS.has(check.id)).length,
)
const selectedBrowserCount = computed(() =>
  selectedChecks.value.filter(check => check.needs_browser).length,
)
const selectedAiCount = computed(() =>
  selectedChecks.value.filter(check => AI_CHECK_IDS.has(check.id)).length,
)
const aiAvailable = computed(() => aiSettings.enabled && aiSettings.endpoints.length > 0)
const browserModeLabel = computed(() => {
  if (form.observe_xss) return t('newScan.observeXssMode')
  if (browserMode.value === 'cdp') return t('newScan.attachCdp', { url: form.cdp_url || t('newScan.cdpUrlUnset') })
  return browserMode.value === 'headed' ? t('newScan.browserHeaded') : t('newScan.browserHeadless')
})
const scopeSummary = computed(() => {
  const explicit = form.scope.split(',').map(item => item.trim()).filter(Boolean)
  if (explicit.length) return explicit.join(', ')
  try { return new URL(form.url.trim()).hostname || t('newScan.scopeFromUrl') }
  catch { return t('newScan.scopeFromUrl') }
})
const parsedHeaders = computed(() => parseHeaders(form.headers))
const aiRouteSummary = computed(() => {
  if (!selectedAiCount.value) return t('newScan.noAiChecks')
  if (!aiAvailable.value) return t('newScan.noAiEndpoints')
  const option = aiEndpointOptions.value.find(item => item.value === form.ai_endpoint)
  return option?.label || t('newScan.aiEndpointAuto')
})

async function loadChecks() {
  state.checks = await api.listChecks()
  selectAll()
}
async function loadSettings() {
  const data = await api.getSettings()
  aiSettings.enabled = data.ai?.enabled ?? true
  aiSettings.endpoints = (data.ai?.endpoints || []).filter(ep => ep.enabled !== false)
}
function toggleCheck(id) {
  if (form.checks.has(id)) form.checks.delete(id)
  else form.checks.add(id)
}
function selectAll() {
  state.checks.forEach(c => form.checks.add(c.id))
}
function clearAll() {
  form.checks.clear()
}
function toggleGroup(group, checked) {
  for (const check of group.checks) {
    if (checked) form.checks.add(check.id)
    else form.checks.delete(check.id)
  }
}
function toggleGroupDetails(groupId) {
  const next = new Set(expandedCheckGroups.value)
  if (next.has(groupId)) next.delete(groupId)
  else next.add(groupId)
  expandedCheckGroups.value = next
}
function selectRecommended() {
  form.checks.clear()
  state.checks.forEach((c) => {
    if (!c.needs_browser) form.checks.add(c.id)
  })
}
function parseHeaders(s) {
  const value = (s || '').trim()
  if (!value) return { ok: true, value: {}, count: 0 }
  try {
    const parsed = JSON.parse(value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      return { ok: false, value: {}, count: 0 }
    }
    return { ok: true, value: parsed, count: Object.keys(parsed).length }
  } catch {
    return { ok: false, value: {}, count: 0 }
  }
}

async function launchChrome() {
  launchingChrome.value = true
  try {
    const r = await api.launchChrome(9222)
    if (r.ok) {
      form.cdp_url = r.cdp_url
      showToast(t('newScan.cdpLaunched', { port: r.port }))
    } else {
      showToast(t('toast.error', { msg: r.error }), 'error')
    }
  } catch (e) { showToast(t('toast.error', { msg: e.message || e }), 'error') }
  finally { launchingChrome.value = false }
}

async function submit() {
  if (!form.url.trim()) { showToast(t('toast.error', { msg: t('newScan.urlRequired') }), 'error'); return }
  const headers = parseHeaders(form.headers)
  if (!headers.ok) {
    showToast(t('toast.error', { msg: t('newScan.headersMustBeObject') }), 'error')
    return
  }
  if (!form.checks.size) {
    showToast(t('toast.error', { msg: t('newScan.needOneCheck') }), 'error')
    return
  }
  submitting.value = true
  try {
    const params = {
      url: form.url.trim(),
      name: form.name.trim(),
      scope: form.scope.split(',').map(s=>s.trim()).filter(Boolean),
      paths: form.paths.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),
      headers: headers.value,
      headless: form.observe_xss ? false : form.headless,
      cdp_url: form.cdp_url.trim() || null,
      xss_observe_delay_ms: form.observe_xss ? 1200 : 0,
      browser_slow_mo: form.observe_xss ? 250 : 0,
      ai_endpoint: form.ai_endpoint || null,
      checks: Array.from(form.checks),
    }
    const r = await api.createTask(params)
    if (!r.ok) { showToast(t('toast.error', { msg: r.error || 'failed' }), 'error'); return }
    showToast(t('toast.taskCreated'))
    emit('created', r.id)
    navigate(`/task/${r.id}`)
  } catch (e) { showToast(t('toast.error', { msg: e.message || e }), 'error') }
  finally { submitting.value = false }
}

function reset() {
  form.url = ''
  form.name = ''
  form.scope = ''
  form.paths = ''
  form.headers = ''
  form.headless = true
  form.observe_xss = false
  form.cdp_url = ''
  form.ai_endpoint = ''
  selectAll()
}

onMounted(async () => { await Promise.all([loadChecks(), loadSettings()]) })
</script>

<template>
  <div :class="embedded ? 'new-scan-card new-scan-embedded' : 'page'">
    <div class="card new-scan-inner">
      <div class="head">
        <div>
          <h3>{{ embedded ? t('newScan.configTitle') : t('newScan.title') }}</h3>
          <p>{{ t('newScan.pageHint') }}</p>
        </div>
        <div class="row-actions">
          <button class="ghost" type="button" @click="reset">{{ t('newScan.resetBtn') }}</button>
        </div>
      </div>

      <form class="form new-scan-form" @submit.prevent="submit">
        <div class="scan-grid">
          <div class="scan-stack">
            <fieldset class="scan-group">
              <legend>{{ t('newScan.targetLegend') }}</legend>
              <p class="group-help">{{ t('newScan.targetHelp') }}</p>
              <div class="grid2">
                <div class="field">
                  <label for="scan-url">{{ t('newScan.urlLabel') }} *</label>
                  <input id="scan-url" v-model="form.url" type="url" required :placeholder="t('newScan.urlPlaceholder')" />
                </div>
                <div class="field">
                  <label for="scan-name">{{ t('newScan.nameLabel') }}</label>
                  <input id="scan-name" v-model="form.name" type="text" />
                </div>
              </div>
              <div class="field field-spaced">
                <label for="scan-scope">{{ t('newScan.scopeLabel') }}</label>
                <input id="scan-scope" v-model="form.scope" type="text" :placeholder="t('newScan.scopePlaceholder')" />
                <div class="hint">{{ t('newScan.scopeHint') }}</div>
              </div>
              <div class="field field-spaced">
                <label for="scan-headers">{{ t('newScan.headersLabel') }}</label>
                <textarea
                  id="scan-headers"
                  v-model="form.headers"
                  rows="3"
                  :aria-invalid="form.headers.trim() && !parsedHeaders.ok ? 'true' : 'false'"
                  :placeholder="t('newScan.headersPlaceholder')"
                ></textarea>
                <div v-if="form.headers.trim() && !parsedHeaders.ok" class="hint field-error">
                  {{ t('newScan.headersInvalid') }}
                </div>
              </div>
            </fieldset>

            <fieldset class="scan-group">
              <legend>{{ t('newScan.envLegend') }}</legend>
              <p class="group-help">{{ t('newScan.envHelp') }}</p>
              <div class="grid3">
                <div class="field">
                  <label for="scan-browser">{{ t('newScan.browserMode') }}</label>
                  <NSelect
                    id="scan-browser"
                    v-model:value="browserMode"
                    size="medium"
                    :options="browserModeOptions"
                  />
                </div>
                <div class="field">
                  <label for="scan-xss">{{ t('newScan.observeXssLabel') }}</label>
                  <NSelect
                    id="scan-xss"
                    v-model:value="observeXssMode"
                    size="medium"
                    :options="observeXssOptions"
                  />
                </div>
                <div class="field">
                  <label for="scan-ai">{{ t('newScan.aiEndpointLabel') }}</label>
                  <NSelect
                    id="scan-ai"
                    v-model:value="form.ai_endpoint"
                    size="medium"
                    :options="aiEndpointOptions"
                  />
                </div>
              </div>
              <div v-if="browserMode === 'cdp'" class="field field-spaced">
                <label for="scan-cdp">{{ t('newScan.cdpLabel') }}</label>
                <div class="inline-field">
                  <input id="scan-cdp" v-model="form.cdp_url" type="text" :placeholder="t('newScan.cdpPlaceholder')" />
                  <button class="ghost sm" type="button" :disabled="launchingChrome" @click="launchChrome">
                    {{ launchingChrome ? t('common.loading') : `${t('newScan.cdpLaunchBtn')} Chrome` }}
                  </button>
                </div>
              </div>
              <div class="field field-spaced">
                <label for="scan-paths">{{ t('newScan.pathsLabel') }}</label>
                <textarea id="scan-paths" v-model="form.paths" rows="3" :placeholder="t('newScan.pathsPlaceholder')"></textarea>
              </div>
            </fieldset>
          </div>

          <fieldset class="scan-group check-plan">
            <legend>{{ t('newScan.planLegend') }}</legend>
            <p class="group-help">{{ t('newScan.planHelp') }}</p>

            <div class="check-groups">
              <section v-for="group in checkGroups" :key="group.id" class="check-group-card">
                <div class="check-group-head">
                  <label>
                    <input
                      type="checkbox"
                      :checked="group.checks.length > 0 && group.selectedCount === group.checks.length"
                      :indeterminate="group.selectedCount > 0 && group.selectedCount < group.checks.length"
                      :aria-label="t('newScan.selectGroupAria', { title: group.title })"
                      @change="toggleGroup(group, $event.target.checked)"
                    />
                    <span>
                      <strong>{{ group.title }}</strong>
                      <small>{{ group.help }}</small>
                    </span>
                  </label>
                  <div class="check-group-actions">
                    <span class="check-count">{{ t('newScan.itemCount', { selected: group.selectedCount, total: group.checks.length }) }}</span>
                    <button
                      type="button"
                      class="check-group-toggle"
                      :aria-expanded="expandedCheckGroups.has(group.id)"
                      @click="toggleGroupDetails(group.id)"
                    >
                      {{ expandedCheckGroups.has(group.id) ? t('common.collapse') : t('common.expand') }}
                    </button>
                  </div>
                </div>
                <div class="group-flags">
                  <span v-if="group.activeCount" class="check-flag active">{{ t('newScan.groupActive', { n: group.activeCount }) }}</span>
                  <span v-if="group.browserCount" class="check-flag browser">{{ t('newScan.groupBrowser', { n: group.browserCount }) }}</span>
                  <span v-if="group.aiCount" class="check-flag ai">{{ t('newScan.groupAi', { n: group.aiCount }) }}</span>
                </div>
                <div v-if="expandedCheckGroups.has(group.id)" class="group-check-list">
                  <label
                    v-for="check in group.checks"
                    :key="check.id"
                    class="check-row"
                    :class="{ on: form.checks.has(check.id) }"
                  >
                    <input
                      type="checkbox"
                      :checked="form.checks.has(check.id)"
                      @change="toggleCheck(check.id)"
                    />
                    <span class="check-copy">
                      <strong>{{ t(`check.${check.id}`, {}, check.name || check.id) }}</strong>
                      <small>{{ t(`check.${check.id}.desc`, {}, check.description || check.id) }}</small>
                      <span class="check-row-flags">
                        <span v-if="ACTIVE_CHECK_IDS.has(check.id)" class="check-flag active">{{ t('newScan.flagActive') }}</span>
                        <span v-if="check.needs_browser" class="check-flag browser">{{ t('newScan.flagBrowser') }}</span>
                        <span v-if="AI_CHECK_IDS.has(check.id)" class="check-flag ai">{{ t('newScan.flagAi') }}</span>
                        <span v-if="!GROUP_BY_CHECK_ID[check.id]" class="check-flag">{{ t('newScan.flagUncategorized') }}</span>
                      </span>
                    </span>
                    <code>{{ check.id }}</code>
                  </label>
                </div>
              </section>
            </div>

            <div class="row-actions plan-actions">
              <button class="ghost sm" type="button" @click="selectRecommended">{{ t('newScan.recommended') }}</button>
              <button class="ghost sm" type="button" @click="selectAll">{{ t('newScan.selectAll') }}</button>
              <button class="ghost sm" type="button" @click="clearAll">{{ t('newScan.clearAll') }}</button>
            </div>

            <div v-if="selectedActiveCount" class="scan-note">
              <strong>{{ t('newScan.activeNoteTitle') }}</strong>
              {{ t('newScan.activeNote', { n: selectedActiveCount }) }}
            </div>
            <div v-if="selectedAiCount && !aiAvailable" class="scan-note warning">
              <strong>{{ t('newScan.aiMissingTitle') }}</strong>
              {{ t('newScan.aiMissingNote', { n: selectedAiCount }) }}
            </div>

            <div class="launch-summary" :aria-label="t('newScan.launchSummaryAria')">
              <div><span>{{ t('newScan.summaryChecks') }}</span><strong>{{ selectedChecksCount }} / {{ totalChecksCount }}</strong></div>
              <div><span>{{ t('newScan.summaryActive') }}</span><strong>{{ t('newScan.headerCount', { n: selectedActiveCount }) }}</strong></div>
              <div><span>{{ t('newScan.summaryBrowser') }}</span><strong>{{ t('newScan.headerCount', { n: selectedBrowserCount }) }}</strong></div>
              <div><span>{{ t('newScan.summaryScope') }}</span><strong class="mono">{{ scopeSummary }}</strong></div>
              <div><span>{{ t('newScan.summaryHeaders') }}</span><strong>{{ parsedHeaders.ok ? t('newScan.headerCount', { n: parsedHeaders.count }) : t('newScan.headersInvalidShort') }}</strong></div>
              <div><span>{{ t('newScan.summaryAi') }}</span><strong>{{ aiRouteSummary }}</strong></div>
            </div>

            <div class="launch-actions">
              <span class="environment-summary">{{ browserModeLabel }}</span>
              <div class="row-actions">
                <button
                  v-if="browserMode !== 'cdp'"
                  class="ghost sm"
                  type="button"
                  :disabled="launchingChrome"
                  @click="launchChrome"
                >{{ launchingChrome ? t('common.loading') : t('newScan.launchChromeCdp') }}</button>
                <button class="primary btn" type="submit" :disabled="submitting || !form.checks.size">
                  {{ submitting ? t('common.loading') : t('newScan.startBtn') }}
                </button>
              </div>
            </div>
          </fieldset>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.new-scan-card {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.new-scan-inner {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.new-scan-form {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}
.new-scan-embedded,
.new-scan-embedded .new-scan-inner {
  height: auto;
}
.new-scan-embedded .new-scan-inner,
.new-scan-embedded .new-scan-form {
  overflow: visible;
}
.new-scan-embedded .new-scan-form {
  padding-right: 0;
}
.scan-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.92fr) minmax(380px, 1.08fr);
  gap: 12px;
  align-items: start;
}
.scan-stack {
  display: grid;
  gap: 12px;
}
.scan-group {
  min-width: 0;
  margin: 0;
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel-2) 46%, var(--card-bg));
}
.scan-group legend {
  padding: 0 6px;
  color: var(--fg-strong);
  font-size: 12px;
  font-weight: 700;
}
.group-help {
  margin: -2px 0 10px;
  color: var(--muted);
  font-size: 11px;
}
.field-spaced {
  margin-top: 10px;
}
.field-error {
  color: var(--err) !important;
}
.inline-field {
  display: flex;
  gap: 6px;
}
.inline-field input {
  min-width: 0;
  flex: 1;
}
.inline-field button {
  flex: 0 0 auto;
}
.check-plan {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.check-groups {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.check-group-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
}
.check-group-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px;
}
.check-group-head > label {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 8px;
  margin: 0;
  color: var(--fg);
  cursor: pointer;
}
.check-group-head input,
.check-row input {
  width: 15px;
  height: 15px;
  margin: 2px 0 0;
  flex: 0 0 auto;
  accent-color: var(--accent);
}
.check-group-head strong,
.check-group-head small,
.check-copy strong,
.check-copy small {
  display: block;
}
.check-group-head strong,
.check-copy strong {
  color: var(--fg-strong);
  font-size: 11px;
}
.check-group-head small,
.check-copy small {
  margin-top: 2px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 400;
  line-height: 1.35;
}
.check-count {
  flex: 0 0 auto;
  padding: 2px 5px;
  border-radius: 5px;
  background: var(--panel-2);
  color: var(--muted);
  font: 10px/1.4 Consolas, monospace;
  white-space: nowrap;
}
.check-group-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
}
.check-group-toggle {
  min-height: 24px;
  padding: 0 6px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--accent);
  font-size: 10px;
  cursor: pointer;
}
.check-group-toggle:hover {
  background: var(--accent-soft);
}
.check-group-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.group-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 0 9px 8px 32px;
}
.group-check-list {
  border-top: 1px solid var(--border);
}
.check-row {
  display: grid !important;
  grid-template-columns: 15px minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  margin: 0 !important;
  padding: 8px 9px;
  border-top: 1px solid var(--border);
  color: var(--fg);
  cursor: pointer;
}
.check-row:first-child {
  border-top: 0;
}
.check-row:hover {
  background: var(--panel-hover);
}
.check-row.on {
  background: color-mix(in srgb, var(--accent) 7%, var(--card-bg));
}
.check-copy {
  min-width: 0;
}
.check-row code {
  overflow: hidden;
  max-width: 132px;
  padding: 2px 5px;
  border-radius: 5px;
  background: var(--panel-2);
  color: var(--muted);
  font: 9px/1.4 Consolas, monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.check-row-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.check-flag {
  padding: 2px 5px;
  border-radius: 5px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 9px;
  font-weight: 600;
}
.check-flag.active {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, var(--card-bg));
}
.check-flag.browser {
  color: var(--accent);
  background: var(--accent-soft);
}
.check-flag.ai {
  color: var(--ok);
  background: color-mix(in srgb, var(--ok) 11%, var(--card-bg));
}
.plan-actions {
  justify-content: flex-start;
  margin-top: 10px;
}
.scan-note {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--warn) 32%, var(--border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--warn) 8%, var(--card-bg));
  color: var(--fg);
  font-size: 11px;
}
.scan-note strong {
  flex: 0 0 auto;
  color: var(--warn);
}
.scan-note.warning {
  border-color: color-mix(in srgb, var(--err) 32%, var(--border));
  background: color-mix(in srgb, var(--err) 8%, var(--card-bg));
}
.scan-note.warning strong {
  color: var(--err);
}
.launch-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
  padding: 10px 0;
  border-top: 1px solid var(--border);
}
.launch-summary div {
  min-width: 0;
}
.launch-summary span,
.launch-summary strong {
  display: block;
}
.launch-summary span {
  color: var(--muted-2);
  font-size: 9px;
}
.launch-summary strong {
  overflow: hidden;
  margin-top: 2px;
  color: var(--fg-strong);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.launch-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.environment-summary {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font: 10px/1.4 Consolas, monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 1200px) {
  .scan-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 760px) {
  .new-scan-inner {
    height: auto;
    overflow: visible;
  }
  .new-scan-form {
    overflow: visible;
  }
  .grid2,
  .grid3,
  .check-groups {
    grid-template-columns: 1fr;
  }
  .inline-field,
  .launch-actions {
    align-items: stretch;
    flex-direction: column;
  }
  .launch-summary {
    grid-template-columns: 1fr 1fr;
  }
  .environment-summary {
    white-space: normal;
  }
}
</style>
