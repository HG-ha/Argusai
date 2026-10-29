<script setup>
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { t } from '../../i18n.js'
import { state } from '../../state.js'
import { useConfirm } from '../../composables/useConfirm.js'
import ListSelect from '../list/ListSelect.vue'
import ProxyFlowInspector from './ProxyFlowInspector.vue'
import ProxyQueryInput from './ProxyQueryInput.vue'
import {
  METHODS,
  RESOURCE_KINDS,
  STATUS_BUCKETS,
  flowPath,
  formatDuration,
  formatSize,
  severityRank,
  statusBucket,
} from './proxyFormat.js'

const emit = defineEmits(['open-settings'])
const wb = inject('proxyWorkbench')
const confirmDialog = useConfirm()
const { status, filters, config, busy } = wb

const RAIL_KEY = 'argusai.proxy.railCollapsed'
const INSPECTOR_KEY = 'argusai.proxy.inspectorWidth'
const INSPECTOR_MIN = 340

const systemProxySupported = computed(() => {
  const p = String(state.platform || '')
  return p === 'darwin' || p.startsWith('win')
})

const railCollapsed = ref(readBool(RAIL_KEY, typeof window !== 'undefined' && window.innerWidth < 1180))
const inspectorWidth = ref(readNumber(INSPECTOR_KEY, 460))
const rootEl = ref(null)
const listEl = ref(null)
const following = ref(true)
const unseen = ref(0)

function readBool(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    return raw == null ? fallback : raw === '1'
  } catch {
    return fallback
  }
}
function readNumber(key, fallback) {
  try {
    const n = Number(localStorage.getItem(key))
    return Number.isFinite(n) && n > 0 ? n : fallback
  } catch {
    return fallback
  }
}

function toggleRail() {
  railCollapsed.value = !railCollapsed.value
  try { localStorage.setItem(RAIL_KEY, railCollapsed.value ? '1' : '0') } catch {}
}

const hasHistory = computed(() => wb.flows.value.length > 0)
const visibleFlows = computed(() => wb.filteredFlows.value)

// Virtual scrolling: rows are a fixed 30px (see .proxy-row), so we render only
// the slice in view (+ overscan) and pad with spacers. Keeps the list smooth
// at thousands of flows (large captures / HAR imports) instead of mounting
// every row.
const OVERSCAN = 10
const scrollTop = ref(0)
const viewportH = ref(600)
const rowH = ref(30) // self-measured from a real row so border/box-sizing can't drift the math
let rowMeasured = false
let listResizeObserver = null

const windowed = computed(() => {
  const rows = visibleFlows.value
  const total = rows.length
  const h = rowH.value
  const start = Math.max(0, Math.floor(scrollTop.value / h) - OVERSCAN)
  const count = Math.ceil(viewportH.value / h) + OVERSCAN * 2
  const end = Math.min(total, start + count)
  return {
    items: rows.slice(start, end),
    padTop: start * h,
    padBottom: Math.max(0, (total - end) * h),
  }
})

function measureViewport() {
  const el = listEl.value
  if (!el) return
  viewportH.value = el.clientHeight || viewportH.value
  const row = el.querySelector('.proxy-row')
  if (row && row.offsetHeight > 0) {
    rowH.value = row.offsetHeight
    rowMeasured = true
  }
}

// Rows may not exist at mount; measure the row height once they first render
// (guarded so heavy capture doesn't trigger a layout read on every new flow).
watch(() => visibleFlows.value.length, (n) => {
  if (n && !rowMeasured) nextTick(measureViewport)
})
const selectedFlow = computed(() => wb.selectedFlow.value)
const selectedFindings = computed(() => (selectedFlow.value ? wb.flowFindings.value.get(selectedFlow.value.id) || [] : []))

const methodOptions = computed(() => [
  { label: t('proxy.allMethods'), value: '' },
  ...METHODS.map((m) => ({ label: m, value: m })),
  { label: t('proxy.otherMethods'), value: 'OTHER' },
])

function bucketLabel(bucket) {
  return bucket === 'failed' ? t('proxy.statusFailed') : bucket
}
function kindLabel(kind) {
  return t(`proxy.kinds.${kind || 'other'}`)
}

function rowFlag(flow) {
  const list = wb.flowFindings.value.get(flow.id)
  if (!list?.length) return ''
  let worst = 'info'
  for (const f of list) {
    if (severityRank(f.severity) < severityRank(worst)) worst = String(f.severity).toLowerCase()
  }
  return worst
}

function select(id) {
  wb.selectedId.value = id
}

function scrollSelectedIntoView() {
  const el = listEl.value
  if (!el) return
  const i = visibleFlows.value.findIndex((f) => f.id === wb.selectedId.value)
  if (i < 0) return
  const h = rowH.value
  const top = i * h
  if (top < el.scrollTop) el.scrollTop = top
  else if (top + h > el.scrollTop + el.clientHeight) el.scrollTop = top - el.clientHeight + h
  scrollTop.value = el.scrollTop
}

function onListKeydown(e) {
  const rows = visibleFlows.value
  if (!rows.length) return
  const idx = rows.findIndex((f) => f.id === wb.selectedId.value)
  let next = null
  if (e.key === 'ArrowDown') next = rows[Math.min(rows.length - 1, idx + 1)] || rows[0]
  else if (e.key === 'ArrowUp') next = idx <= 0 ? rows[0] : rows[idx - 1]
  else if (e.key === 'Home') next = rows[0]
  else if (e.key === 'End') next = rows[rows.length - 1]
  else if (e.key === 'Escape' && wb.selectedId.value != null) {
    e.preventDefault()
    wb.selectedId.value = null
    return
  }
  if (!next) return
  e.preventDefault()
  select(next.id)
  scrollSelectedIntoView()
}

function onListScroll() {
  const el = listEl.value
  if (!el) return
  scrollTop.value = el.scrollTop
  following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 32
  if (following.value) unseen.value = 0
}

function jumpToLatest() {
  const el = listEl.value
  if (!el) return
  el.scrollTop = el.scrollHeight
  scrollTop.value = el.scrollTop
  following.value = true
  unseen.value = 0
}

onMounted(() => {
  measureViewport()
  if (typeof ResizeObserver !== 'undefined' && listEl.value) {
    listResizeObserver = new ResizeObserver(() => measureViewport())
    listResizeObserver.observe(listEl.value)
  }
})
onUnmounted(() => {
  if (listResizeObserver) listResizeObserver.disconnect()
  listResizeObserver = null
})

watch(() => visibleFlows.value.length, async (n, old) => {
  if (following.value) {
    await nextTick()
    if (listEl.value) { listEl.value.scrollTop = listEl.value.scrollHeight; scrollTop.value = listEl.value.scrollTop }
  } else if (n > old) {
    unseen.value += n - old
  }
})

watch(() => filters.rule, (rule) => {
  if (!rule) return
  const first = visibleFlows.value[0]
  if (first && !visibleFlows.value.some((f) => f.id === wb.selectedId.value)) {
    select(first.id)
    scrollSelectedIntoView()
  }
})

function pickHost(host) {
  filters.host = filters.host === host ? '' : host
}
function pickRule(id) {
  filters.rule = filters.rule === id ? '' : id
}

async function confirmClear() {
  const ok = await confirmDialog({
    title: t('proxy.clearHistory'),
    content: t('proxy.clearHistoryConfirm', { n: status.flow_count }),
    positiveText: t('proxy.clearHistory'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (ok) await wb.clearHistory()
}

function onSystemProxyToggle() {
  wb.saveConfig()
}

function openRuleDraft(draft) {
  wb.ruleDraft.value = draft
  emit('open-settings')
}

function openRewriteDraft(draft) {
  wb.rewriteDraft.value = draft
  emit('open-settings')
}

// Inspector resize (rAF-throttled, same pattern as the assistant panel).
let dragFrame = 0
let dragState = null
function startResize(e) {
  const root = rootEl.value
  if (!root) return
  e.preventDefault()
  dragState = { startX: e.clientX, startWidth: inspectorWidth.value, maxWidth: Math.max(INSPECTOR_MIN, root.clientWidth * 0.62) }
  document.body.classList.add('panel-dragging')
  window.addEventListener('pointermove', onResizeMove)
  window.addEventListener('pointerup', stopResize, { once: true })
}
function onResizeMove(e) {
  if (!dragState) return
  const { startX, startWidth, maxWidth } = dragState
  const x = e.clientX
  if (dragFrame) return
  dragFrame = requestAnimationFrame(() => {
    dragFrame = 0
    inspectorWidth.value = Math.round(Math.min(maxWidth, Math.max(INSPECTOR_MIN, startWidth + (startX - x))))
  })
}
function stopResize() {
  dragState = null
  document.body.classList.remove('panel-dragging')
  window.removeEventListener('pointermove', onResizeMove)
  try { localStorage.setItem(INSPECTOR_KEY, String(inspectorWidth.value)) } catch {}
}
onUnmounted(() => {
  if (dragFrame) cancelAnimationFrame(dragFrame)
  window.removeEventListener('pointermove', onResizeMove)
  document.body.classList.remove('panel-dragging')
})

const gridStyle = computed(() => ({
  '--rail-w': railCollapsed.value || !hasHistory.value ? '0px' : '228px',
  '--inspector-w': hasHistory.value ? `${inspectorWidth.value}px` : '0px',
}))
</script>

<template>
  <div
    ref="rootEl"
    class="proxy-traffic"
    :class="{ 'has-history': hasHistory, 'rail-collapsed': railCollapsed, 'has-selection': !!selectedFlow }"
    :style="gridStyle"
  >
    <div v-if="hasHistory" class="proxy-filterbar">
      <button
        type="button"
        class="proxy-rail-toggle"
        :class="{ on: !railCollapsed }"
        :aria-pressed="!railCollapsed"
        :title="t('proxy.toggleRail')"
        @click="toggleRail"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M2 3h12M2 8h12M2 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
      </button>
      <ProxyQueryInput
        v-model="filters.query"
        :error="wb.queryState.error"
        :position="wb.queryState.position"
        :running="wb.queryState.running"
        :timed-out="wb.queryState.timedOut"
        @apply="wb.applyQuery($event)"
      />
      <ListSelect v-model="filters.method" :options="methodOptions" min-width="108px" />
      <div class="proxy-chips" role="group" :aria-label="t('proxy.statusFilterAria')">
        <button
          v-for="bucket in STATUS_BUCKETS"
          :key="bucket"
          type="button"
          class="proxy-chip"
          :class="[`is-${bucket}`, { on: filters.statuses.includes(bucket) }]"
          :aria-pressed="filters.statuses.includes(bucket)"
          @click="wb.toggleIn(filters.statuses, bucket)"
        >{{ bucketLabel(bucket) }}</button>
      </div>
      <div class="proxy-chips" role="group" :aria-label="t('proxy.kindFilterAria')">
        <button
          v-for="kind in RESOURCE_KINDS"
          :key="kind"
          type="button"
          class="proxy-chip"
          :class="{ on: filters.kinds.includes(kind) }"
          :aria-pressed="filters.kinds.includes(kind)"
          @click="wb.toggleIn(filters.kinds, kind)"
        >{{ kindLabel(kind) }}</button>
      </div>
      <div class="proxy-filterbar-end">
        <span class="proxy-count mono" :title="t('proxy.historyCapHint', { cap: status.flow_cap })">
          {{ t('proxy.shownCount', { shown: visibleFlows.length, total: wb.flows.value.length }) }}
        </span>
        <button v-if="wb.hasFilters.value" type="button" class="ghost proxy-bar-btn" @click="wb.clearFilters()">{{ t('proxy.clearFilters') }}</button>
        <button type="button" class="ghost proxy-bar-btn proxy-clear-btn" :disabled="!hasHistory || busy.clear" @click="confirmClear">{{ t('proxy.clearHistory') }}</button>
      </div>
    </div>

    <nav v-if="hasHistory && !railCollapsed" class="proxy-rail" :aria-label="t('proxy.railAria')">
      <section class="proxy-rail-section">
        <h3>{{ t('proxy.railHosts') }}</h3>
        <button type="button" class="proxy-rail-item" :class="{ on: !filters.host }" @click="filters.host = ''">
          <span class="proxy-rail-name">{{ t('proxy.allHosts') }}</span>
          <span class="proxy-rail-count mono">{{ wb.flows.value.length }}</span>
        </button>
        <button
          v-for="h in wb.hosts.value"
          :key="h.host"
          type="button"
          class="proxy-rail-item"
          :class="{ on: filters.host === h.host }"
          :title="h.host"
          @click="pickHost(h.host)"
        >
          <span class="proxy-rail-name mono">{{ h.host }}</span>
          <span v-if="h.failed" class="proxy-rail-failed mono" :title="t('proxy.hostFailedHint', { n: h.failed })">{{ h.failed }}</span>
          <span class="proxy-rail-count mono">{{ h.count }}</span>
        </button>
      </section>
      <section class="proxy-rail-section">
        <h3>{{ t('proxy.railFindings') }} <span v-if="wb.findings.value.length" class="mono">{{ wb.findings.value.length }}</span></h3>
        <p v-if="!wb.ruleGroups.value.length" class="proxy-rail-empty">{{ t('proxy.noFindingsYet') }}</p>
        <button
          v-for="rule in wb.ruleGroups.value"
          :key="rule.id"
          type="button"
          class="proxy-rail-item"
          :class="{ on: filters.rule === rule.id }"
          :title="rule.label"
          @click="pickRule(rule.id)"
        >
          <span :class="['sev-dot', rule.severity]"></span>
          <span class="proxy-rail-name">{{ rule.label }}</span>
          <span class="proxy-rail-count mono">{{ rule.count }}</span>
        </button>
      </section>
    </nav>

    <section class="proxy-table-area" :aria-label="t('proxy.tabTraffic')">
      <div v-if="hasHistory && !status.running" class="proxy-banner">
        <span>{{ t('proxy.stoppedHistoryNote') }}</span>
        <button type="button" class="ghost proxy-bar-btn" :disabled="busy.start" @click="wb.start()">{{ t('proxy.start') }}</button>
      </div>
      <div v-if="wb.activeRule.value" class="proxy-rule-banner">
        <div class="proxy-rule-banner-main">
          <span :class="['sev-dot', wb.activeRule.value.severity]"></span>
          <strong>{{ wb.activeRule.value.label }}</strong>
          <span class="muted">{{ t('proxy.ruleHits', { n: wb.activeRule.value.count }) }}</span>
          <button type="button" class="ghost proxy-bar-btn" @click="filters.rule = ''">{{ t('proxy.clearRuleFilter') }}</button>
        </div>
        <p v-if="wb.activeRule.value.sample?.remediation">{{ wb.activeRule.value.sample.remediation }}</p>
      </div>

      <div v-if="!hasHistory && !status.running" class="proxy-guide">
        <h2>{{ t('proxy.guideTitle') }}</h2>
        <p class="proxy-guide-lead">{{ t('proxy.guideLead') }}</p>
        <ol>
          <li :class="{ done: wb.caTrusted.value }">
            <span class="proxy-guide-step mono">1</span>
            <div class="proxy-guide-body">
              <strong>{{ t('proxy.guideTrustTitle') }}</strong>
              <p>{{ t('proxy.guideTrustBody') }}</p>
            </div>
            <span v-if="wb.caTrusted.value" class="proxy-guide-done">{{ t('proxy.guideTrusted') }}</span>
            <button v-else type="button" :disabled="busy.ca" @click="wb.installCa()">{{ t('proxy.installCa') }}</button>
          </li>
          <li>
            <span class="proxy-guide-step mono">2</span>
            <div class="proxy-guide-body">
              <strong>{{ t('proxy.guideRouteTitle') }}</strong>
              <label v-if="systemProxySupported" class="proxy-check">
                <input v-model="config.useSystemProxy" type="checkbox" @change="onSystemProxyToggle" />
                <span>{{ t('proxy.useSystemProxy') }}</span>
              </label>
              <p>
                {{ t('proxy.guideManual') }}
                <button type="button" class="proxy-endpoint mono" :title="t('proxy.copyEndpoint')" @click="wb.copy(wb.endpoint.value, 'proxy.endpointCopied')">{{ wb.endpoint.value }}</button>
              </p>
            </div>
          </li>
          <li>
            <span class="proxy-guide-step mono">3</span>
            <div class="proxy-guide-body">
              <strong>{{ t('proxy.guideStartTitle') }}</strong>
              <p>{{ t('proxy.guideStartBody') }}</p>
            </div>
            <button type="button" class="primary" :disabled="busy.start" @click="wb.start()">{{ t('proxy.start') }}</button>
          </li>
        </ol>
        <button type="button" class="ghost proxy-guide-more" @click="emit('open-settings')">{{ t('proxy.guideMoreSettings') }}</button>
      </div>

      <div v-else-if="!hasHistory" class="proxy-waiting">
        <span class="proxy-pulse" aria-hidden="true"></span>
        <strong>{{ t('proxy.waitingTitle', { endpoint: wb.endpoint.value }) }}</strong>
        <p v-if="status.system_proxy_active">{{ t('proxy.waitingSystem') }}</p>
        <p v-else>
          {{ t('proxy.waitingManual') }}
          <button type="button" class="proxy-endpoint mono" :title="t('proxy.copyEndpoint')" @click="wb.copy(wb.endpoint.value, 'proxy.endpointCopied')">{{ wb.endpoint.value }}</button>
        </p>
      </div>

      <template v-else>
        <div class="proxy-table-head" role="row">
          <span>#</span>
          <span>{{ t('proxy.colMethod') }}</span>
          <span>{{ t('proxy.colHost') }}</span>
          <span>{{ t('proxy.colPath') }}</span>
          <span>{{ t('proxy.colStatus') }}</span>
          <span>{{ t('proxy.colKind') }}</span>
          <span class="num">{{ t('proxy.colSize') }}</span>
          <span class="num">{{ t('proxy.colDuration') }}</span>
        </div>
        <div
          ref="listEl"
          class="proxy-rows"
          role="grid"
          tabindex="0"
          :aria-label="t('proxy.tabTraffic')"
          @keydown="onListKeydown"
          @scroll.passive="onListScroll"
        >
          <div v-if="!visibleFlows.length" class="proxy-filter-empty">
            <span>{{ t('proxy.noMatchingFlows') }}</span>
            <button type="button" class="ghost proxy-bar-btn" @click="wb.clearFilters()">{{ t('proxy.clearFilters') }}</button>
          </div>
          <div v-if="windowed.padTop" class="proxy-rows-pad" :style="{ height: windowed.padTop + 'px' }" aria-hidden="true"></div>
          <div
            v-for="flow in windowed.items"
            :key="flow.id"
            class="proxy-row"
            :class="[`is-${statusBucket(flow)}`, { selected: flow.id === wb.selectedId.value }]"
            :data-flow-id="flow.id"
            role="row"
            :aria-selected="flow.id === wb.selectedId.value"
            @click="select(flow.id)"
          >
            <span class="mono c-id">{{ flow.id }}</span>
            <span class="mono c-method">{{ flow.method }}</span>
            <span class="c-host" :title="flow.host">{{ flow.host }}</span>
            <span class="mono c-path" :title="flow.url">
              <span class="c-path-text">{{ flowPath(flow) }}</span>
              <span v-if="wb.decryptedIds.value.has(flow.id)" class="proxy-dec-mark" :title="t('proxy.decode.rowMark')">
                <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M4.5 7V5a3.5 3.5 0 0 1 6.8-1.2M3.5 7h9v6.5h-9z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" /></svg>
              </span>
              <span v-if="rowFlag(flow)" class="proxy-flag" :class="rowFlag(flow)" :title="t('proxy.flowHasFindings')">⚑</span>
            </span>
            <span class="mono c-status">{{ flow.error ? t('proxy.statusFailed') : flow.status }}</span>
            <span class="c-kind">{{ kindLabel(flow.resource_kind) }}</span>
            <span class="mono num">{{ flow.response_size_exact === false ? '≥ ' : '' }}{{ formatSize(flow.response_size) }}</span>
            <span class="mono num">{{ formatDuration(flow.duration_ms) }}</span>
          </div>
          <div v-if="windowed.padBottom" class="proxy-rows-pad" :style="{ height: windowed.padBottom + 'px' }" aria-hidden="true"></div>
        </div>
        <button v-if="unseen > 0" type="button" class="proxy-new-pill" @click="jumpToLatest">
          {{ t('proxy.newFlows', { n: unseen }) }}
        </button>
      </template>
    </section>

    <div
      v-if="hasHistory"
      class="proxy-resize-handle"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('proxy.resizeInspector')"
      @pointerdown="startResize"
    ></div>

    <div v-if="hasHistory" class="proxy-inspector-slot">
      <ProxyFlowInspector
        v-if="selectedFlow"
        :flow="selectedFlow"
        :findings="selectedFindings"
        :decrypted="wb.decryptedIds.value.has(selectedFlow.id)"
        @create-rule="openRuleDraft"
        @create-rewrite="openRewriteDraft"
        @close="wb.selectedId.value = null"
      />
      <div v-else class="proxy-inspector-placeholder">
        <strong>{{ t('proxy.inspectorEmptyTitle') }}</strong>
        <p>{{ t('proxy.inspectorEmptyBody') }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.proxy-traffic {
  --rail-w: 228px;
  --inspector-w: 460px;
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: var(--rail-w) minmax(0, 1fr) 0 0;
  grid-template-rows: auto minmax(0, 1fr);
  grid-template-areas:
    "bar bar bar bar"
    "rail table handle inspector";
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card-bg);
  overflow: hidden;
}
.proxy-traffic.has-history {
  grid-template-columns: var(--rail-w) minmax(0, 1fr) 6px var(--inspector-w);
}

/* ---------- filter bar ---------- */
.proxy-filterbar {
  grid-area: bar;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.proxy-rail-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border-radius: 8px;
  color: var(--muted);
}
.proxy-rail-toggle.on { color: var(--fg-strong); background: var(--panel-2); }
.proxy-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.proxy-chip {
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}
.proxy-chip.on {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  background: var(--accent-soft);
  color: var(--fg-strong);
}
.proxy-chip.is-2xx.on { border-color: color-mix(in srgb, var(--ok) 60%, var(--border)); background: color-mix(in srgb, var(--ok) 12%, transparent); }
.proxy-chip.is-3xx.on { border-color: color-mix(in srgb, var(--accent-3) 60%, var(--border)); background: color-mix(in srgb, var(--accent-3) 12%, transparent); }
.proxy-chip.is-4xx.on { border-color: color-mix(in srgb, var(--warn) 60%, var(--border)); background: color-mix(in srgb, var(--warn) 12%, transparent); }
.proxy-chip.is-5xx.on,
.proxy-chip.is-failed.on { border-color: color-mix(in srgb, var(--err) 60%, var(--border)); background: color-mix(in srgb, var(--err) 12%, transparent); }
.proxy-filterbar-end {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.proxy-count {
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
.proxy-bar-btn {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
}
.proxy-clear-btn:hover:not(:disabled) { color: var(--err); }

/* ---------- rail ---------- */
.proxy-rail {
  grid-area: rail;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 8px 14px;
  border-right: 1px solid var(--border);
  background: var(--bg-sunken);
}
.proxy-rail-section + .proxy-rail-section { margin-top: 16px; }
.proxy-rail-section h3 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 6px 6px;
  color: var(--muted-2);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.proxy-rail-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-height: 28px;
  padding: 4px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--fg);
  font-size: 12px;
  text-align: left;
}
.proxy-rail-item:hover:not(:disabled) { background: var(--panel-hover); }
.proxy-rail-item.on {
  background: var(--accent-soft);
  color: var(--fg-strong);
}
.proxy-rail-item .sev-dot { flex: 0 0 auto; }
.proxy-rail-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.proxy-rail-count {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 11px;
}
.proxy-rail-failed {
  flex: 0 0 auto;
  padding: 0 5px;
  border-radius: 5px;
  background: color-mix(in srgb, var(--err) 14%, transparent);
  color: var(--err);
  font-size: 10px;
}
.proxy-rail-empty {
  margin: 0 8px;
  color: var(--muted-2);
  font-size: 12px;
  line-height: 1.5;
}

/* ---------- table ---------- */
.proxy-table-area {
  grid-area: table;
  position: relative;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  container-type: inline-size;
}
.proxy-banner,
.proxy-rule-banner {
  flex: 0 0 auto;
  padding: 7px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.proxy-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--muted);
  background: var(--bg-sunken);
}
.proxy-rule-banner { background: color-mix(in srgb, var(--warn) 6%, transparent); }
.proxy-rule-banner-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.proxy-rule-banner-main .proxy-bar-btn { margin-left: auto; }
.proxy-rule-banner p {
  margin: 4px 0 0 16px;
  color: var(--fg);
  line-height: 1.45;
}
.proxy-table-head,
.proxy-row {
  display: grid;
  grid-template-columns: 40px 54px minmax(72px, 0.3fr) minmax(120px, 1fr) 44px 44px 64px 58px;
  align-items: center;
  gap: 6px;
  padding: 0 12px 0 14px;
}
/* Narrow table (wide inspector / open rail): drop type and duration first. */
@container (max-width: 620px) {
  .proxy-table-head,
  .proxy-row { grid-template-columns: 40px 54px minmax(64px, 0.3fr) minmax(100px, 1fr) 44px 64px; }
  .proxy-table-head > :nth-child(6),
  .proxy-row > .c-kind,
  .proxy-table-head > :nth-child(8),
  .proxy-row > :nth-child(8) { display: none; }
}
@container (max-width: 440px) {
  .proxy-table-head,
  .proxy-row { grid-template-columns: 40px 54px minmax(100px, 1fr) 44px; }
  .proxy-table-head > :nth-child(3),
  .proxy-row > .c-host,
  .proxy-table-head > :nth-child(7),
  .proxy-row > :nth-child(7) { display: none; }
}
.proxy-table-head {
  flex: 0 0 auto;
  height: 30px;
  border-bottom: 1px solid var(--border);
  color: var(--muted-2);
  font-size: 11px;
  font-weight: 600;
}
.proxy-table-head .num,
.proxy-row .num { text-align: right; }
.proxy-rows {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  overscroll-behavior: contain;
  outline: none;
}
.proxy-rows:focus-visible { box-shadow: inset 0 0 0 2px var(--accent-soft); }
.proxy-row {
  --rail: var(--muted-2);
  position: relative;
  height: 30px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
  color: var(--fg);
  font-size: 12px;
  cursor: pointer;
  content-visibility: auto;
  contain-intrinsic-size: auto 30px;
}
.proxy-row::before {
  content: "";
  position: absolute;
  top: 5px;
  bottom: 5px;
  left: 4px;
  width: 3px;
  border-radius: 2px;
  background: var(--rail);
}
.proxy-row.is-2xx { --rail: var(--ok); }
.proxy-row.is-3xx { --rail: var(--accent-3); }
.proxy-row.is-4xx { --rail: var(--warn); }
.proxy-row.is-5xx,
.proxy-row.is-failed { --rail: var(--err); }
.proxy-row:hover { background: var(--panel-hover); }
.proxy-row.selected {
  background: var(--accent-soft);
  color: var(--fg-strong);
}
.proxy-row > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.c-id { color: var(--muted-2); font-size: 11px; }
.c-method { color: var(--accent); font-weight: 600; font-size: 11px; }
.c-host { color: var(--muted); }
.c-path {
  display: flex;
  align-items: center;
  gap: 6px;
}
.c-path-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg-strong);
}
.c-status { color: var(--rail); font-weight: 600; }
.c-kind { color: var(--muted); font-size: 11px; }
.proxy-row .num { color: var(--muted); font-size: 11px; }
.proxy-flag {
  flex: 0 0 auto;
  font-size: 11px;
  line-height: 1;
  color: var(--info);
}
.proxy-flag.critical { color: var(--crit); }
.proxy-flag.high { color: var(--high); }
.proxy-flag.medium { color: var(--med); }
.proxy-flag.low { color: var(--low); }
.proxy-dec-mark {
  flex: 0 0 auto;
  display: inline-flex;
  color: var(--ok);
}
.proxy-filter-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 48px 16px;
  color: var(--muted);
  font-size: 13px;
}
.proxy-new-pill {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  height: 30px;
  padding: 0 14px;
  border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
  border-radius: 999px;
  background: var(--card-bg);
  color: var(--accent);
  font-size: 12px;
  box-shadow: var(--shadow-sm);
}
.proxy-new-pill:active:not(:disabled) { transform: translateX(-50%) translateY(1px); }

/* ---------- empty states ---------- */
.proxy-guide {
  width: min(640px, 100%);
  margin: 0 auto;
  padding: 36px 20px;
  overflow-y: auto;
}
.proxy-guide h2 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 18px;
}
.proxy-guide-lead {
  margin: 6px 0 18px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.55;
}
.proxy-guide ol {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}
.proxy-guide li {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 14px 16px;
}
.proxy-guide li + li { border-top: 1px solid var(--border); }
.proxy-guide li > button { flex: 0 0 auto; align-self: center; }
.proxy-guide-step {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 1px solid var(--border-strong);
  border-radius: 50%;
  color: var(--muted);
  font-size: 12px;
}
.proxy-guide li.done .proxy-guide-step {
  border-color: var(--ok);
  background: color-mix(in srgb, var(--ok) 14%, transparent);
  color: var(--ok);
}
.proxy-guide-body {
  flex: 1 1 auto;
  min-width: 0;
}
.proxy-guide-body strong { color: var(--fg-strong); font-size: 13px; }
.proxy-guide-body p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.proxy-guide-done {
  flex: 0 0 auto;
  align-self: center;
  color: var(--ok);
  font-size: 12px;
}
.proxy-guide-more { margin-top: 12px; font-size: 12px; }
.proxy-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  color: var(--fg);
  font-size: 12px;
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
.proxy-waiting {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex: 1 1 auto;
  padding: 24px;
  text-align: center;
}
.proxy-waiting strong { color: var(--fg-strong); font-size: 14px; }
.proxy-waiting p {
  margin: 0;
  max-width: 520px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.proxy-pulse {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 55%, transparent);
  animation: proxy-pulse 1.8s ease-out infinite;
}
@keyframes proxy-pulse {
  to { box-shadow: 0 0 0 12px transparent; }
}
@media (prefers-reduced-motion: reduce) {
  .proxy-pulse { animation: none; }
}

/* ---------- inspector ---------- */
.proxy-resize-handle {
  grid-area: handle;
  position: relative;
  cursor: col-resize;
  border-left: 1px solid var(--border);
  touch-action: none;
}
.proxy-resize-handle:hover,
.proxy-resize-handle:active { background: var(--accent-soft); }
.proxy-inspector-slot {
  grid-area: inspector;
  min-width: 0;
  min-height: 0;
}
.proxy-inspector-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 100%;
  padding: 24px;
  color: var(--muted);
  text-align: center;
}
.proxy-inspector-placeholder strong { color: var(--fg); font-size: 13px; }
.proxy-inspector-placeholder p { margin: 0; font-size: 12px; line-height: 1.55; }

@media (max-width: 980px) {
  .proxy-traffic.has-history { grid-template-columns: var(--rail-w) minmax(0, 1fr) 0 0; }
  .proxy-resize-handle { display: none; }
  .proxy-inspector-slot {
    grid-area: table;
    z-index: 2;
    display: none;
    border-left: 1px solid var(--border);
    box-shadow: var(--shadow-md);
  }
  .proxy-traffic.has-selection .proxy-inspector-slot { display: block; }
}
</style>
