<script setup>
import { computed, inject, onUnmounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import {
  base64ToBytes,
  formatClock,
  formatDuration,
  formatHeaders,
  formatSize,
  headerEntries,
  hexDump,
  ruleFallbackLabel,
  ruleKey,
  statusBucket,
  toCurl,
} from './proxyFormat.js'
import { formatJson, looksLikeJson, parseFormBody } from './bodyFormat.js'
import { draftFromDecoded } from './decryptFormat.js'
import { draftFromFlow as draftRewriteFromFlow } from './rewriteFormat.js'
import ProxyDecodedPane from './ProxyDecodedPane.vue'

const props = defineProps({
  flow: { type: Object, required: true },
  findings: { type: Array, default: () => [] },
  decrypted: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'create-rule', 'create-rewrite'])

const wb = inject('proxyWorkbench')

const DETAIL_CACHE_LIMIT = 30
// Text sent across the bridge on first open; "show all" fetches the rest of the capture.
const UI_TEXT_LIMIT = 256 * 1024
const WRAP_KEY = 'argusai.proxy.wrapBody'
const detailCache = new Map()
const blobUrls = new Map()

const detail = ref(null)
const loading = ref(false)
const loadingFull = ref(false)
const error = ref('')
const part = ref('response')
const pane = ref('body')
const viewMode = ref('pretty')
const wrap = ref(readWrap())
let loadToken = 0

function readWrap() {
  try { return localStorage.getItem(WRAP_KEY) !== '0' } catch { return true }
}
function toggleWrap() {
  wrap.value = !wrap.value
  try { localStorage.setItem(WRAP_KEY, wrap.value ? '1' : '0') } catch {}
}

async function fetchDetail(id, textLimit) {
  const r = await api.getProxyFlowDetail(id, textLimit)
  if (!r?.ok) throw new Error(r?.error || t('proxy.detailLoadFailed'))
  detailCache.set(id, r.flow)
  if (detailCache.size > DETAIL_CACHE_LIMIT) detailCache.delete(detailCache.keys().next().value)
  return r.flow
}

async function load(force = false) {
  const id = props.flow?.id
  if (id == null) return
  const token = ++loadToken
  error.value = ''
  if (!force && detailCache.has(id)) {
    detail.value = detailCache.get(id)
    loading.value = false
    return
  }
  loading.value = true
  detail.value = null
  try {
    const flow = await fetchDetail(id, UI_TEXT_LIMIT)
    if (token === loadToken) detail.value = flow
  } catch (exc) {
    if (token === loadToken) error.value = exc?.message || t('proxy.detailLoadFailed')
  } finally {
    if (token === loadToken) loading.value = false
  }
}

async function loadFull() {
  const id = props.flow?.id
  if (id == null) return
  const token = loadToken
  loadingFull.value = true
  try {
    const flow = await fetchDetail(id, 0)
    if (token === loadToken) detail.value = flow
  } catch (exc) {
    if (token === loadToken) error.value = exc?.message || t('proxy.detailLoadFailed')
  } finally {
    loadingFull.value = false
  }
}

watch(() => props.flow?.id, () => {
  // WebSocket flows have no HTTP body; open on the frames view.
  pane.value = props.flow?.resource_kind === 'websocket' ? 'ws' : 'body'
  load()
}, { immediate: true })

// Decoded / decrypted views, fetched per flow and refreshed when the backend
// decode sequence moves (new rules, late decryption).
const decoded = ref(null)
const decodedLoading = ref(false)
let decodedToken = 0
async function loadDecoded() {
  const id = props.flow?.id
  if (id == null) return
  const token = ++decodedToken
  if (decoded.value?.flow_id !== id) {
    decoded.value = null
    decodedLoading.value = true
  }
  try {
    const r = await api.getProxyFlowDecoded(id)
    if (token !== decodedToken) return
    if (!r?.ok) {
      decoded.value = null
      return
    }
    // Unprocessed flows report seq 0 and are decoded on the fly; always take those.
    const seq = Number(r.decode_seq) || 0
    const current = decoded.value
    if (current?.flow_id === id && seq && current.seq === seq) return
    decoded.value = { flow_id: id, seq, ...(r.decoded || {}) }
  } catch {
    if (token === decodedToken) decoded.value = null
  } finally {
    if (token === decodedToken) decodedLoading.value = false
  }
}
const decodedPart = computed(() => decoded.value?.[part.value] || null)
const decodedCount = computed(() => decodedPart.value?.items?.length || 0)
const hasDecoded = computed(() => ['request', 'response'].some((p) => decoded.value?.[p]?.items?.length))

const isWebsocket = computed(() => (detail.value?.resource_kind || props.flow.resource_kind) === 'websocket')
const wsFrames = computed(() => detail.value?.websocket_frames || props.flow.websocket_frames || [])
const wsFrameCount = computed(() => detail.value?.websocket_frame_count ?? wsFrames.value.length)
watch(() => [props.flow?.id, props.decrypted], loadDecoded, { immediate: true })
// Rule edits can change existing output; flows without any keep quiet until
// ``decrypted`` flips.
watch(() => wb.decodeVersion.value, () => {
  if (hasDecoded.value) loadDecoded()
})
const hasPlaintext = computed(() => (decodedPart.value?.items || []).some((i) => i.text))
watch(hasDecoded, (has) => {
  if (!has && pane.value === 'decoded') pane.value = 'body'
})

function createRule(item) {
  if (item.source === 'rule') {
    emit('create-rule', { editId: item.rule_id, testFlowId: props.flow.id })
    return
  }
  emit('create-rule', { ...draftFromDecoded(props.flow, part.value, item), testFlowId: props.flow.id })
}

function createRewrite() {
  emit('create-rewrite', { ...draftRewriteFromFlow(props.flow), testFlowId: props.flow.id })
}

function releaseBlobUrls() {
  for (const url of blobUrls.values()) {
    try { URL.revokeObjectURL(url) } catch {}
  }
  blobUrls.clear()
}
onUnmounted(releaseBlobUrls)

const bucket = computed(() => statusBucket(props.flow))
const body = computed(() => (part.value === 'request' ? detail.value?.request_body : detail.value?.response_body) || null)
const headers = computed(() => headerEntries(part.value === 'request' ? detail.value?.request_headers : detail.value?.response_headers))
const bodyKind = computed(() => (body.value?.kind ? body.value.kind : 'empty'))
const rawText = computed(() => body.value?.text || '')
const textLimited = computed(() => Boolean(body.value?.text_limited))
const bodyHex = computed(() => hexDump(body.value?.hex))
const truncated = computed(() => Boolean(part.value === 'request'
  ? detail.value?.request_body_truncated
  : detail.value?.response_body_truncated))
const fullSize = computed(() => Number(part.value === 'request' ? detail.value?.request_size : detail.value?.response_size) || 0)
const evicted = computed(() => Boolean(detail.value?.body_evicted))

const TEXT_KINDS = new Set(['json', 'text', 'html', 'xml', 'javascript', 'css'])
const jsonView = computed(() => {
  if (!TEXT_KINDS.has(bodyKind.value) || !looksLikeJson(rawText.value)) return null
  return formatJson(rawText.value)
})
const formRows = computed(() => (TEXT_KINDS.has(bodyKind.value)
  ? parseFormBody(rawText.value, body.value?.content_type)
  : null))
const viewModes = computed(() => {
  const modes = []
  if (jsonView.value?.ok) modes.push('pretty')
  if (formRows.value) modes.push('form')
  if (modes.length) modes.push('raw')
  return modes
})
const activeMode = computed(() => (viewModes.value.includes(viewMode.value) ? viewMode.value : viewModes.value[0] || 'raw'))
const jsonNote = computed(() => {
  const view = jsonView.value
  if (!view) return ''
  if (!view.ok) return t('proxy.formatFailed')
  if (activeMode.value !== 'pretty') return ''
  if (view.partial) return t('proxy.formatPartial')
  return view.prefix ? t('proxy.xssiPrefixStripped', { prefix: view.prefix }) : ''
})
// Encoded bodies too big to inflate fully: the decoded total is unknown.
const sizeUnknown = computed(() => part.value === 'response' && detail.value?.response_size_exact === false)
const bodyText = computed(() => {
  if (activeMode.value === 'pretty' && jsonView.value?.ok) return jsonView.value.text
  if (activeMode.value === 'form' && formRows.value) return formRows.value.map(([k, v]) => `${k}=${v}`).join('\n')
  return rawText.value
})
const highlightedHtml = computed(() => (activeMode.value === 'pretty' && jsonView.value?.ok ? jsonView.value.html : null))
const mediaUrl = computed(() => {
  const b = body.value
  if (!b?.base64) return ''
  if (b.kind === 'image') return `data:${b.content_type || 'image/png'};base64,${b.base64}`
  if (b.kind === 'video' || b.kind === 'audio') {
    const key = `${props.flow.id}:${part.value}`
    if (!blobUrls.has(key)) {
      const bytes = base64ToBytes(b.base64)
      if (!bytes) return ''
      blobUrls.set(key, URL.createObjectURL(new Blob([bytes], { type: b.content_type || 'application/octet-stream' })))
    }
    return blobUrls.get(key)
  }
  return ''
})
const canSave = computed(() => Boolean(body.value && body.value.kind !== 'empty'))
const copyableText = computed(() => {
  if (pane.value === 'headers') {
    return formatHeaders(part.value === 'request' ? detail.value?.request_headers : detail.value?.response_headers)
  }
  if (pane.value === 'decoded') {
    return (decodedPart.value?.items || []).filter((i) => i.text).map((i) => i.text).join('\n\n')
  }
  return bodyText.value || bodyHex.value
})

function ruleLabel(finding) {
  const key = `proxy.rules.${ruleKey(finding.id)}`
  const label = t(key)
  return label === key ? ruleFallbackLabel(finding.title) : label
}

function evidenceSummary(finding) {
  const ev = finding?.evidence
  if (!ev || typeof ev !== 'object') return ''
  return Object.entries(ev)
    .filter(([k]) => k !== 'host')
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(' · ')
}

async function copyCurl() {
  if (!detail.value) return
  let source = detail.value
  if (source.request_body?.text_limited) {
    try {
      source = await fetchDetail(props.flow.id, 0)
    } catch {
      return
    }
  }
  await wb.copy(toCurl(source), 'proxy.curlCopied')
  if (source.request_body_truncated) {
    showToast(t('proxy.curlBodyTruncated', { size: formatSize(source.request_body?.size) }), 'warn')
  }
}

function askAssistant() {
  const f = props.flow
  let text = t('proxy.aiPrompt', { id: f.id, method: f.method, url: f.url, status: f.status || f.error || '—' })
  if (props.findings.length) {
    const rules = [...new Set(props.findings.map(ruleLabel))].join(t('proxy.listSeparator'))
    text += ` ${t('proxy.aiPromptFindings', { rules })}`
  }
  if (props.decrypted) text += ` ${t('proxy.aiPromptDecrypted')}`
  else if (hasDecoded.value) text += ` ${t('proxy.aiPromptEncrypted')}`
  window.dispatchEvent(new CustomEvent('argusai:assistant-insert-text', { detail: { text, open: true } }))
}

function saveBody() {
  wb.runFileAction(
    () => api.saveProxyFlowBody(props.flow.id, part.value),
    (r) => t('proxy.bodySaved', { size: formatSize(r.size_bytes) }),
  )
}

function saveBodyAs() {
  wb.runFileAction(
    () => api.saveProxyFlowBodyAs(props.flow.id, part.value),
    (r) => t('proxy.bodySaved', { size: formatSize(r.size_bytes) }),
  )
}
</script>

<template>
  <aside class="proxy-inspector" :aria-label="t('proxy.inspectorAria')">
    <header class="proxy-inspector-head">
      <div class="proxy-inspector-line">
        <span class="proxy-method-tag">{{ flow.method }}</span>
        <span class="proxy-status-tag" :class="`is-${bucket}`">{{ flow.error ? t('proxy.statusFailed') : flow.status }}</span>
        <span class="proxy-inspector-meta mono">
          {{ formatDuration(flow.duration_ms) }} ·
          <span :title="flow.response_size_exact === false ? t('proxy.wireSizeHint') : ''">{{ flow.response_size_exact === false ? '≥ ' : '' }}{{ formatSize(flow.response_size) }}</span>
          · {{ formatClock(flow.timestamp) }}
        </span>
        <button type="button" class="ghost proxy-inspector-close" :aria-label="t('common.close')" @click="emit('close')">×</button>
      </div>
      <p class="proxy-inspector-url mono selectable">{{ flow.url }}</p>
      <div class="proxy-inspector-actions">
        <button type="button" class="tiny" @click="wb.copy(flow.url, 'proxy.urlCopied')">{{ t('proxy.copyUrl') }}</button>
        <button type="button" class="tiny" :disabled="!detail" @click="copyCurl">{{ t('proxy.copyCurl') }}</button>
        <button v-if="!isWebsocket" type="button" class="tiny" @click="createRewrite">{{ t('proxy.rewrite.fromFlow') }}</button>
        <button type="button" class="tiny proxy-ask-ai" @click="askAssistant">{{ t('proxy.askAi') }}</button>
      </div>
    </header>

    <div v-if="flow.error" class="proxy-inspector-error">
      <strong>{{ t('proxy.requestFailed') }}</strong>
      <span class="mono selectable">{{ flow.error }}</span>
    </div>

    <section v-if="findings.length" class="proxy-inspector-findings" :aria-label="t('proxy.flowFindings')">
      <article v-for="finding in findings" :key="finding.uid" class="proxy-inspector-finding">
        <div class="proxy-inspector-finding-title">
          <span :class="['sev-dot', String(finding.severity || 'info').toLowerCase()]"></span>
          <strong>{{ ruleLabel(finding) }}</strong>
        </div>
        <p v-if="evidenceSummary(finding)" class="mono selectable">{{ evidenceSummary(finding) }}</p>
        <p v-if="finding.remediation" class="proxy-inspector-fix">{{ finding.remediation }}</p>
      </article>
    </section>

    <div class="proxy-inspector-tabs">
      <div class="proxy-seg" role="tablist" :aria-label="t('proxy.partAria')">
        <button type="button" role="tab" :aria-selected="part === 'request'" :class="{ on: part === 'request' }" @click="part = 'request'">{{ t('proxy.partRequest') }}</button>
        <button type="button" role="tab" :aria-selected="part === 'response'" :class="{ on: part === 'response' }" @click="part = 'response'">{{ t('proxy.partResponse') }}</button>
      </div>
      <div class="proxy-pane-tabs" role="tablist">
        <button type="button" role="tab" :aria-selected="pane === 'body'" :class="{ on: pane === 'body' }" @click="pane = 'body'">{{ t('proxy.paneBody') }}</button>
        <button type="button" role="tab" :aria-selected="pane === 'headers'" :class="{ on: pane === 'headers' }" @click="pane = 'headers'">
          {{ t('proxy.paneHeaders') }} <span class="proxy-pane-count">{{ headers.length }}</span>
        </button>
        <button
          v-if="hasDecoded"
          type="button"
          role="tab"
          :aria-selected="pane === 'decoded'"
          :class="{ on: pane === 'decoded' }"
          :title="t('proxy.decode.paneHint')"
          @click="pane = 'decoded'"
        >
          <span v-if="hasPlaintext" class="proxy-pane-dot" aria-hidden="true"></span>{{ t('proxy.decode.pane') }}
          <span class="proxy-pane-count">{{ decodedCount }}</span>
        </button>
        <button
          v-if="isWebsocket"
          type="button"
          role="tab"
          :aria-selected="pane === 'ws'"
          :class="{ on: pane === 'ws' }"
          @click="pane = 'ws'"
        >
          {{ t('proxy.ws.pane') }}
          <span class="proxy-pane-count">{{ wsFrameCount }}</span>
        </button>
      </div>
      <div class="proxy-pane-tools">
        <button type="button" class="ghost tiny" :disabled="!copyableText" @click="wb.copy(copyableText)">{{ t('proxy.copyContent') }}</button>
        <template v-if="pane === 'body'">
          <button type="button" class="ghost tiny" :disabled="!canSave || wb.busy.io" @click="saveBody">{{ t('proxy.saveBody') }}</button>
          <button type="button" class="ghost tiny" :disabled="!canSave || wb.busy.io" @click="saveBodyAs">{{ t('proxy.saveAs') }}</button>
        </template>
      </div>
    </div>

    <div v-if="pane === 'body' && !loading && !error && (viewModes.length || rawText)" class="proxy-view-bar">
      <div v-if="viewModes.length" class="proxy-seg proxy-seg-sm" role="tablist" :aria-label="t('proxy.viewModeAria')">
        <button
          v-for="mode in viewModes"
          :key="mode"
          type="button"
          role="tab"
          :aria-selected="activeMode === mode"
          :class="{ on: activeMode === mode }"
          @click="viewMode = mode"
        >{{ t(`proxy.view.${mode}`) }}</button>
      </div>
      <span v-if="jsonNote" class="proxy-view-note">{{ jsonNote }}</span>
      <label class="proxy-wrap-toggle">
        <input type="checkbox" :checked="wrap" @change="toggleWrap" />
        <span>{{ t('proxy.wrapLines') }}</span>
      </label>
    </div>

    <div v-if="pane === 'decoded' && decodedCount" class="proxy-view-bar">
      <span class="proxy-view-note">{{ t('proxy.decode.paneNote') }}</span>
      <label class="proxy-wrap-toggle">
        <input type="checkbox" :checked="wrap" @change="toggleWrap" />
        <span>{{ t('proxy.wrapLines') }}</span>
      </label>
    </div>

    <div v-if="pane === 'body' && evicted" class="proxy-truncated-note">
      {{ t('proxy.bodyEvictedNote') }}
    </div>
    <div v-else-if="pane === 'body' && truncated" class="proxy-truncated-note">
      {{ sizeUnknown
        ? t('proxy.captureUnknownNote', { captured: formatSize(body?.size), wire: formatSize(detail?.response_wire_size) })
        : t('proxy.captureTruncatedNote', { captured: formatSize(body?.size), full: formatSize(fullSize) }) }}
    </div>
    <div v-if="pane === 'body' && textLimited" class="proxy-limited-note">
      <span>{{ t('proxy.textLimitedNote', { shown: formatSize(rawText.length), total: formatSize(body?.size) }) }}</span>
      <button type="button" class="ghost tiny" :disabled="loadingFull" @click="loadFull">{{ t('proxy.loadFullBody') }}</button>
    </div>

    <div class="proxy-inspector-body" :class="`kind-${error ? 'error' : pane === 'body' ? bodyKind : pane}`">
      <template v-if="pane === 'ws'">
        <div v-if="!wsFrames.length" class="proxy-body-empty">{{ t('proxy.ws.empty') }}</div>
        <ul v-else class="proxy-ws-frames selectable">
          <li v-for="(f, idx) in wsFrames" :key="idx" class="proxy-ws-frame" :class="`dir-${f.dir}`">
            <span class="proxy-ws-dir" :title="f.dir === 'up' ? t('proxy.ws.up') : t('proxy.ws.down')">{{ f.dir === 'up' ? '▲' : '▼' }}</span>
            <span class="proxy-ws-kind mono">{{ f.kind }}</span>
            <span class="proxy-ws-data mono">{{ f.text != null ? f.text : f.hex }}</span>
            <span class="proxy-ws-size mono">{{ formatSize(f.size) }}<template v-if="f.truncated">+</template></span>
          </li>
        </ul>
      </template>
      <template v-else-if="pane === 'decoded'">
        <div v-if="decodedLoading" class="proxy-body-empty">{{ t('proxy.loadingDetail') }}</div>
        <ProxyDecodedPane
          v-else-if="decodedCount"
          :part="decodedPart"
          :wrap="wrap"
          @copy="wb.copy($event)"
          @create-rule="createRule"
        />
        <div v-else class="proxy-body-empty">{{ t('proxy.decode.emptyPart') }}</div>
      </template>
      <div v-else-if="loading" class="proxy-body-empty">{{ t('proxy.loadingDetail') }}</div>
      <div v-else-if="error" class="proxy-body-empty proxy-body-error">
        <span>{{ error }}</span>
        <button type="button" class="ghost tiny" @click="load(true)">{{ t('proxy.retryLoad') }}</button>
      </div>
      <template v-else-if="pane === 'headers'">
        <div v-if="!headers.length" class="proxy-body-empty">{{ t('proxy.noHeaders') }}</div>
        <dl v-else class="proxy-headers selectable">
          <template v-for="(h, idx) in headers" :key="`${h.name}-${idx}`">
            <dt class="mono">{{ h.name }}</dt>
            <dd class="mono">{{ h.value }}</dd>
          </template>
        </dl>
      </template>
      <div v-else-if="bodyKind === 'empty'" class="proxy-body-empty">{{ t('proxy.bodyEmpty') }}</div>
      <div v-else-if="bodyKind === 'image'" class="proxy-body-media">
        <img v-if="mediaUrl" :src="mediaUrl" :alt="flow.filename_hint || ''" class="proxy-body-image" />
        <p v-else class="proxy-body-empty">{{ t('proxy.bodyNotInline') }}</p>
        <p class="proxy-body-media-meta mono">{{ body?.content_type || 'image' }} · {{ formatSize(fullSize) }}</p>
      </div>
      <div v-else-if="bodyKind === 'video' || bodyKind === 'audio'" class="proxy-body-media">
        <video v-if="mediaUrl && bodyKind === 'video'" :src="mediaUrl" controls preload="metadata" class="proxy-body-video"></video>
        <audio v-else-if="mediaUrl" :src="mediaUrl" controls preload="metadata" class="proxy-body-audio"></audio>
        <p v-else class="proxy-body-empty">{{ t('proxy.bodyNotInline') }}</p>
        <p class="proxy-body-media-meta mono">{{ body?.content_type || bodyKind }} · {{ formatSize(fullSize) }}</p>
      </div>
      <pre v-else-if="bodyKind === 'binary'" class="proxy-body-text proxy-body-hex selectable">{{ bodyHex || t('proxy.bodyBinaryNoPreview') }}</pre>
      <table v-else-if="activeMode === 'form' && formRows" class="proxy-form-table selectable">
        <thead>
          <tr><th>{{ t('proxy.formParamName') }}</th><th>{{ t('proxy.formParamValue') }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="([name, value], idx) in formRows" :key="`${name}-${idx}`">
            <td class="mono">{{ name }}</td>
            <td class="mono">{{ value }}</td>
          </tr>
        </tbody>
      </table>
      <!-- Every token is HTML-escaped in bodyFormat.formatJson before it reaches v-html. -->
      <pre
        v-else-if="highlightedHtml"
        class="proxy-body-text proxy-json selectable"
        :class="{ nowrap: !wrap }"
        v-html="highlightedHtml"
      ></pre>
      <pre v-else class="proxy-body-text selectable" :class="{ nowrap: !wrap }">{{ bodyText }}</pre>
    </div>
  </aside>
</template>

<style scoped>
.proxy-inspector {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--card-bg);
}
.proxy-inspector-head {
  flex: 0 0 auto;
  padding: 12px 14px 10px;
  border-bottom: 1px solid var(--border);
}
.proxy-inspector-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.proxy-method-tag {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--accent);
}
.proxy-status-tag {
  --rail: var(--muted);
  padding: 1px 7px;
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  background: color-mix(in srgb, var(--rail) 14%, transparent);
  color: var(--rail);
}
.proxy-status-tag.is-2xx { --rail: var(--ok); }
.proxy-status-tag.is-3xx { --rail: var(--accent-3); }
.proxy-status-tag.is-4xx { --rail: var(--warn); }
.proxy-status-tag.is-5xx,
.proxy-status-tag.is-failed { --rail: var(--err); }
.proxy-inspector-meta {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.proxy-inspector-close {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  margin-left: auto;
  padding: 0;
  font-size: 18px;
  line-height: 1;
}
.proxy-inspector-url {
  margin: 8px 0 0;
  max-height: 4.6em;
  overflow: auto;
  color: var(--fg-strong);
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
}
.proxy-inspector-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.proxy-inspector-actions button {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  border-radius: 8px;
}
.proxy-ask-ai {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--accent);
}
.proxy-inspector-error {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 10px 14px 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
  font-size: 12px;
}
.proxy-inspector-findings {
  flex: 0 0 auto;
  max-height: 34%;
  overflow: auto;
  margin: 10px 14px 0;
  border: 1px solid color-mix(in srgb, var(--warn) 30%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--warn) 5%, transparent);
}
.proxy-inspector-finding {
  padding: 8px 10px;
}
.proxy-inspector-finding + .proxy-inspector-finding { border-top: 1px solid var(--border); }
.proxy-inspector-finding-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--fg-strong);
}
.proxy-inspector-finding p {
  margin: 4px 0 0 16px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
  word-break: break-word;
}
.proxy-inspector-fix { color: var(--fg) !important; }
.proxy-inspector-tabs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  flex: 0 0 auto;
  padding: 10px 14px 8px;
}
.proxy-seg {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-sunken);
}
.proxy-seg button {
  height: 26px;
  padding: 0 12px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}
.proxy-seg button.on {
  background: var(--panel-2);
  color: var(--fg-strong);
  font-weight: 600;
}
.proxy-ws-frames {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 8px 10px;
  list-style: none;
}
.proxy-ws-frame {
  display: grid;
  grid-template-columns: 16px auto minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 7px;
  font-size: 12px;
}
.proxy-ws-frame.dir-up { background: color-mix(in srgb, var(--accent) 7%, transparent); }
.proxy-ws-frame.dir-down { background: var(--bg-sunken); }
.proxy-ws-dir { color: var(--muted); font-size: 10px; }
.proxy-ws-frame.dir-up .proxy-ws-dir { color: var(--accent); }
.proxy-ws-kind { color: var(--muted-2); font-size: 11px; }
.proxy-ws-data { overflow: hidden; color: var(--fg); white-space: pre-wrap; word-break: break-all; }
.proxy-ws-size { color: var(--muted-2); font-size: 11px; white-space: nowrap; }
.proxy-pane-tabs {
  display: inline-flex;
  gap: 2px;
}
.proxy-pane-tabs button {
  height: 26px;
  padding: 0 8px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}
.proxy-pane-tabs button.on {
  border-bottom-color: var(--accent);
  color: var(--fg-strong);
}
.proxy-pane-count {
  margin-left: 2px;
  color: var(--muted-2);
  font-family: var(--mono);
  font-size: 10px;
}
.proxy-pane-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 5px;
  border-radius: 50%;
  background: var(--ok);
  vertical-align: 1px;
}
.proxy-pane-tools {
  display: inline-flex;
  gap: 2px;
  margin-left: auto;
}
.proxy-pane-tools button {
  height: 26px;
  padding: 0 8px;
  font-size: 12px;
}
.proxy-truncated-note {
  flex: 0 0 auto;
  margin: 0 14px 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
  font-size: 11px;
  line-height: 1.5;
}
.proxy-limited-note {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0 14px 8px;
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--fg);
  font-size: 11px;
}
.proxy-limited-note button {
  flex: 0 0 auto;
  height: 24px;
  padding: 0 8px;
  color: var(--accent);
  font-size: 11px;
}
.proxy-view-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 14px 8px;
  min-height: 26px;
}
.proxy-seg-sm button {
  height: 22px;
  padding: 0 10px;
  font-size: 11px;
}
.proxy-view-note {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.proxy-wrap-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  cursor: pointer;
}
.proxy-wrap-toggle input {
  width: 13px;
  height: 13px;
}
.proxy-body-text.nowrap {
  white-space: pre;
  word-break: normal;
}
.proxy-json :deep(.j-k) { color: var(--syntax-builtin); }
.proxy-json :deep(.j-s) { color: var(--syntax-string); }
.proxy-json :deep(.j-n) { color: var(--syntax-number); }
.proxy-json :deep(.j-l) { color: var(--syntax-keyword); }
.proxy-json :deep(.j-p) { color: var(--muted-2); }
.proxy-form-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.proxy-form-table th,
.proxy-form-table td {
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
.proxy-form-table th {
  position: sticky;
  top: 0;
  background: var(--bg-sunken);
  color: var(--muted-2);
  font-size: 11px;
  font-weight: 600;
}
.proxy-form-table td:first-child {
  width: 30%;
  color: var(--syntax-builtin);
  word-break: break-all;
}
.proxy-form-table td:last-child {
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-all;
}
.proxy-inspector-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  margin: 0 14px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
}
.proxy-inspector-body.kind-decoded { background: var(--card-bg); }
.proxy-inspector-body.kind-image,
.proxy-inspector-body.kind-video,
.proxy-inspector-body.kind-audio,
.proxy-inspector-body.kind-empty,
.proxy-inspector-body.kind-error {
  display: flex;
  align-items: center;
  justify-content: center;
}
.proxy-headers {
  display: grid;
  grid-template-columns: minmax(110px, max-content) minmax(0, 1fr);
  gap: 0;
  margin: 0;
  font-size: 12px;
}
.proxy-headers dt,
.proxy-headers dd {
  margin: 0;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
  line-height: 1.5;
}
.proxy-headers dt {
  color: var(--muted);
  white-space: nowrap;
}
.proxy-headers dd {
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-all;
}
.proxy-body-text {
  margin: 0;
  padding: 12px;
  color: var(--fg);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
.proxy-body-hex {
  white-space: pre;
  word-break: normal;
}
.proxy-body-empty {
  margin: 0;
  padding: 24px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.proxy-body-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--err);
}
.proxy-body-media {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 14px;
}
.proxy-body-image {
  max-width: 100%;
  max-height: 52vh;
  object-fit: contain;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: repeating-conic-gradient(rgba(255, 255, 255, 0.05) 0 25%, transparent 0 50%) 50% / 16px 16px;
}
.proxy-body-video,
.proxy-body-audio { width: 100%; max-width: 100%; }
.proxy-body-media-meta {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  word-break: break-all;
}
.selectable,
.selectable * {
  user-select: text;
  -webkit-user-select: text;
  cursor: text;
}
</style>
