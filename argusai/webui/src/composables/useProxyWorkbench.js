// Shared state for the passive-proxy page: status, incremental flow buffer,
// findings, filters, config form and lifecycle actions.

import { computed, reactive, ref, shallowRef, watch } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useLiveChannel } from './useLiveChannel.js'
import { METHODS, ruleFallbackLabel, ruleKey, severityRank, statusBucket } from '../components/proxy/proxyFormat.js'

const POLL_MS = 1500
// While stopped only another surface (AI tool, CLI) can start capture.
const IDLE_SYNC_EVERY = 3
const STATUS_EVERY = 5
const FETCH_LIMIT = 5000
const QUERY_DEBOUNCE_MS = 300

function listFrom(value) {
  if (Array.isArray(value)) return value.map((s) => String(s).trim()).filter(Boolean)
  return String(value || '').split(',').map((s) => s.trim()).filter(Boolean)
}

function normalizeConfig(raw) {
  const mode = String(raw?.intercept_mode || 'all')
  let scope = listFrom(raw?.scope)
  let denyHosts = listFrom(raw?.deny_hosts)
  // Old denylist configs kept the blocked hosts in ``scope``.
  if (mode === 'denylist' && !denyHosts.length) {
    denyHosts = scope
    scope = []
  }
  return {
    host: String(raw?.host || raw?.listen_host || '127.0.0.1'),
    port: Number(raw?.port || raw?.listen_port || 8866) || 8866,
    scope,
    denyHosts,
    mimeAllow: listFrom(raw?.mime_allow),
    useSystemProxy: Boolean(raw?.use_system_proxy),
  }
}

function configSignature(cfg) {
  return JSON.stringify([cfg.host, Number(cfg.port), cfg.scope, cfg.denyHosts, cfg.mimeAllow, Boolean(cfg.useSystemProxy)])
}

export function useProxyWorkbench() {
  const status = reactive({
    running: false,
    config: null,
    flow_count: 0,
    flow_cap: 500,
    finding_count: 0,
    ca: null,
    system_proxy_active: false,
  })
  const flows = shallowRef([])
  const findings = ref([])
  const selectedId = ref(null)
  const busy = reactive({ start: false, stop: false, clear: false, ca: false, save: false, io: false })
  const filters = reactive({ query: '', method: '', statuses: [], kinds: [], host: '', rule: '' })
  // Expression filter, evaluated by the backend (regex + decrypted plaintext).
  // ``ids`` stays null while no valid expression is applied.
  const queryState = reactive({ applied: '', ids: null, error: '', position: -1, running: false, timedOut: false })
  const decryptedIds = shallowRef(new Set())
  // Backend decode sequence; bumps whenever any flow's decoded view changes.
  const decodeVersion = ref(0)
  // Prefilled decryption rule handed from the inspector to the settings tab.
  const ruleDraft = shallowRef(null)
  // Same, for an active-rewrite rule drafted from a flow.
  const rewriteDraft = shallowRef(null)
  const config = reactive(normalizeConfig({}))
  const savedSignature = ref(configSignature(config))

  let generation = null
  let lastId = 0
  let decodeSeq = null
  let syncing = false
  let timer = null
  let tick = 0
  let queryTimer = null
  let queryToken = 0
  // Live-sync matches that arrive while a full query is in flight.
  let deltaSinceFull = null

  const endpoint = computed(() => {
    const cfg = status.running && status.config ? status.config : null
    const host = cfg?.listen_host || config.host || '127.0.0.1'
    const port = cfg?.listen_port || config.port || 8866
    return `${host}:${port}`
  })
  const caTrusted = computed(() => Boolean(status.ca?.ca_trusted))
  const configDirty = computed(() => configSignature(config) !== savedSignature.value)

  const flowsById = computed(() => {
    const map = new Map()
    for (const flow of flows.value) map.set(flow.id, flow)
    return map
  })
  const selectedFlow = computed(() => (selectedId.value == null ? null : flowsById.value.get(selectedId.value) || null))

  // flow id → findings. Older findings without ``metadata.flow_id`` match by URL.
  const flowFindings = computed(() => {
    const map = new Map()
    let byUrl = null
    const push = (id, finding) => {
      const bucket = map.get(id)
      if (bucket) bucket.push(finding)
      else map.set(id, [finding])
    }
    for (const finding of findings.value) {
      const flowId = Number(finding?.metadata?.flow_id)
      if (flowId) {
        push(flowId, finding)
        continue
      }
      if (!finding?.url) continue
      if (!byUrl) {
        byUrl = new Map()
        for (const flow of flows.value) {
          const ids = byUrl.get(flow.url)
          if (ids) ids.push(flow.id)
          else byUrl.set(flow.url, [flow.id])
        }
      }
      for (const id of byUrl.get(finding.url) || []) push(id, finding)
    }
    return map
  })

  const ruleGroups = computed(() => {
    const groups = new Map()
    for (const finding of findings.value) {
      const id = String(finding?.id || 'proxy.unknown')
      let group = groups.get(id)
      if (!group) {
        const key = `proxy.rules.${ruleKey(id)}`
        const translated = t(key)
        group = {
          id,
          label: translated === key ? ruleFallbackLabel(finding.title) || id : translated,
          severity: String(finding.severity || 'info').toLowerCase(),
          count: 0,
          sample: finding,
        }
        groups.set(id, group)
      }
      group.count += 1
      if (severityRank(finding.severity) < severityRank(group.severity)) {
        group.severity = String(finding.severity).toLowerCase()
      }
    }
    return [...groups.values()].sort((a, b) =>
      severityRank(a.severity) - severityRank(b.severity) || b.count - a.count,
    )
  })
  const activeRule = computed(() => ruleGroups.value.find((g) => g.id === filters.rule) || null)

  const hosts = computed(() => {
    const map = new Map()
    for (const flow of flows.value) {
      const host = flow.host || '—'
      const entry = map.get(host)
      const failed = statusBucket(flow) === 'failed' || Number(flow.status) >= 400
      if (entry) {
        entry.count += 1
        if (failed) entry.failed += 1
      } else {
        map.set(host, { host, count: 1, failed: failed ? 1 : 0 })
      }
    }
    return [...map.values()].sort((a, b) => b.count - a.count || a.host.localeCompare(b.host))
  })

  const hasFilters = computed(() => Boolean(
    filters.query.trim() || filters.method || filters.statuses.length || filters.kinds.length
      || filters.host || filters.rule,
  ))

  const filteredFlows = computed(() => {
    const queryIds = queryState.ids
    const statuses = new Set(filters.statuses)
    const kinds = new Set(filters.kinds)
    const method = filters.method
    const host = filters.host
    const rule = filters.rule
    const ruleFlows = flowFindings.value
    return flows.value.filter((flow) => {
      if (host && (flow.host || '—') !== host) return false
      if (rule && !(ruleFlows.get(flow.id) || []).some((f) => f.id === rule)) return false
      if (method) {
        const m = String(flow.method || '').toUpperCase()
        if (method === 'OTHER' ? METHODS.includes(m) : m !== method) return false
      }
      if (statuses.size && !statuses.has(statusBucket(flow))) return false
      if (kinds.size && !kinds.has(flow.resource_kind || 'other')) return false
      if (queryIds && !queryIds.has(flow.id)) return false
      return true
    })
  })

  async function runFullQuery() {
    const source = queryState.applied
    if (!source) return
    const token = ++queryToken
    deltaSinceFull = new Set()
    queryState.running = true
    try {
      const r = await api.queryProxyTraffic(source)
      if (token !== queryToken) return
      if (r?.ok) {
        const ids = new Set(r.ids || [])
        for (const id of deltaSinceFull) ids.add(id)
        queryState.ids = ids
        queryState.error = ''
        queryState.position = -1
        queryState.timedOut = Boolean(r.timed_out)
      } else {
        queryState.ids = null
        queryState.error = String(r?.error || t('toast.error'))
        queryState.position = Number.isFinite(Number(r?.position)) ? Number(r.position) : -1
        queryState.timedOut = false
      }
    } catch {
      if (token === queryToken) queryState.error = t('toast.error')
    } finally {
      if (token === queryToken) {
        queryState.running = false
        deltaSinceFull = null
      }
    }
  }

  function applyQuery(text = filters.query) {
    clearTimeout(queryTimer)
    queryTimer = null
    const source = String(text || '').trim()
    if (source === queryState.applied && (queryState.ids || queryState.error)) return
    queryState.applied = source
    if (!source) {
      queryToken += 1
      Object.assign(queryState, { ids: null, error: '', position: -1, running: false, timedOut: false })
      return
    }
    runFullQuery()
  }

  watch(() => filters.query, (text) => {
    clearTimeout(queryTimer)
    queryTimer = setTimeout(() => applyQuery(text), QUERY_DEBOUNCE_MS)
  })

  function clearFilters() {
    filters.query = ''
    applyQuery('')
    filters.method = ''
    filters.statuses = []
    filters.kinds = []
    filters.host = ''
    filters.rule = ''
  }

  function toggleIn(list, value) {
    const idx = list.indexOf(value)
    if (idx >= 0) list.splice(idx, 1)
    else list.push(value)
  }

  async function refreshFindings() {
    try {
      const rows = await api.getProxyFindings()
      findings.value = Array.isArray(rows) ? rows : []
      status.finding_count = findings.value.length
    } catch {}
  }

  async function syncTraffic() {
    if (syncing) return
    syncing = true
    try {
      const querySource = queryState.applied
      const r = await api.getProxyTrafficAfter(lastId, FETCH_LIMIT, generation, querySource, decodeSeq)
      if (!r?.ok) return
      const incoming = Array.isArray(r.flows) ? r.flows : []
      let next = r.reset ? incoming : (incoming.length ? flows.value.concat(incoming) : flows.value)
      const firstId = Number(r.first_id) || 0
      if (!r.total) {
        if (next.length) next = []
      } else if (next.length && next[0].id < firstId) {
        // Evicted by the backend history cap.
        next = next.filter((f) => f.id >= firstId)
      }
      if (next !== flows.value) flows.value = next
      const findingsStale = r.reset || Number(r.finding_count) !== findings.value.length
      generation = r.generation
      lastId = Number(r.last_id) || 0
      status.running = Boolean(r.running)
      status.flow_count = Number(r.total) || 0
      status.flow_cap = Number(r.cap) || status.flow_cap
      if (selectedId.value != null && !flowsById.value.has(selectedId.value)) selectedId.value = null

      if (Array.isArray(r.decrypted_ids)) decryptedIds.value = new Set(r.decrypted_ids)
      decodeSeq = r.decode_seq ?? decodeSeq
      if (decodeSeq != null && decodeVersion.value !== decodeSeq) decodeVersion.value = decodeSeq
      if (querySource && querySource === queryState.applied && Array.isArray(r.query_matches)) {
        const rechecked = Array.isArray(r.query_rechecked) ? r.query_rechecked : []
        if (r.reset) {
          if (!r.query_error) queryState.ids = new Set(r.query_matches)
        } else if (queryState.ids && (r.query_matches.length || rechecked.length)) {
          const ids = new Set(queryState.ids)
          for (const id of rechecked) ids.delete(id)
          for (const id of r.query_matches) ids.add(id)
          queryState.ids = ids
        }
        if (deltaSinceFull) for (const id of r.query_matches) deltaSinceFull.add(id)
      }
      if (findingsStale) await refreshFindings()
      // Findings land after their flow; only finding-aware queries need a redo.
      if (findingsStale && !r.reset && /finding/i.test(queryState.applied) && !queryState.error) runFullQuery()
    } catch {
    } finally {
      syncing = false
    }
  }

  async function refreshStatus() {
    try {
      Object.assign(status, await api.getProxyStatus())
    } catch {}
  }

  async function loadConfig() {
    try {
      const settings = await api.getSettings()
      Object.assign(config, normalizeConfig(settings?.proxy || {}))
      savedSignature.value = configSignature(config)
    } catch {}
  }

  async function saveConfig() {
    busy.save = true
    try {
      const r = await api.saveSettings({
        proxy: {
          host: config.host,
          port: Number(config.port) || 8866,
          scope: [...config.scope],
          deny_hosts: [...config.denyHosts],
          intercept_mode: 'all',
          mime_allow: [...config.mimeAllow],
          use_system_proxy: Boolean(config.useSystemProxy),
        },
      })
      if (r && r.ok === false) {
        showToast(r.error || t('toast.error'), 'error')
        return false
      }
      savedSignature.value = configSignature(config)
      return true
    } catch {
      showToast(t('toast.error'), 'error')
      return false
    } finally {
      busy.save = false
    }
  }

  function startParams() {
    return {
      host: config.host,
      port: Number(config.port) || 8866,
      scope: [...config.scope],
      deny_hosts: [...config.denyHosts],
      intercept_mode: 'all',
      mime_allow: [...config.mimeAllow],
      use_system_proxy: Boolean(config.useSystemProxy),
    }
  }

  async function start() {
    busy.start = true
    try {
      const r = await api.startProxy(startParams())
      if (!r?.ok) {
        showToast(r?.error || t('toast.error'), 'error')
        return false
      }
      showToast(t('proxy.started'), 'ok')
      await refreshStatus()
      await syncTraffic()
      return true
    } catch {
      showToast(t('toast.error'), 'error')
      return false
    } finally {
      busy.start = false
    }
  }

  async function stop({ quiet = false } = {}) {
    busy.stop = true
    try {
      const r = await api.stopProxy()
      if (r?.ok && !quiet) showToast(t('proxy.stopped'), 'ok')
      await refreshStatus()
      return Boolean(r?.ok)
    } catch {
      showToast(t('toast.error'), 'error')
      return false
    } finally {
      busy.stop = false
    }
  }

  async function saveAndApply() {
    if (!(await saveConfig())) return false
    if (!status.running) {
      showToast(t('proxy.configSaved'), 'ok')
      return true
    }
    await stop({ quiet: true })
    const ok = await start()
    if (ok) showToast(t('proxy.configApplied'), 'ok')
    return ok
  }

  async function clearHistory() {
    busy.clear = true
    try {
      const r = await api.clearProxyTraffic()
      if (!r?.ok) {
        showToast(r?.error || t('toast.error'), 'error')
        return
      }
      flows.value = []
      findings.value = []
      decryptedIds.value = new Set()
      if (queryState.ids) queryState.ids = new Set()
      generation = r.generation
      lastId = 0
      selectedId.value = null
      status.flow_count = 0
      status.finding_count = 0
      filters.rule = ''
      filters.host = ''
      showToast(t('proxy.historyCleared'), 'ok')
    } catch {
      showToast(t('toast.error'), 'error')
    } finally {
      busy.clear = false
    }
  }

  async function runCaAction(fn, okKey) {
    busy.ca = true
    try {
      const r = await fn()
      if (r?.ok) showToast(t(okKey), 'ok')
      else if (r && !r.cancelled) showToast(r.error || t('toast.error'), 'error')
      await refreshStatus()
    } catch {
      showToast(t('toast.error'), 'error')
    } finally {
      busy.ca = false
    }
  }

  const installCa = () => runCaAction(() => api.installProxyCa(), 'proxy.caInstalled')
  const uninstallCa = () => runCaAction(() => api.uninstallProxyCa(), 'proxy.caUninstalled')

  async function copy(text, okKey = 'proxy.copiedContent') {
    if (!text) return false
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
      showToast(t(okKey), 'ok')
      return true
    } catch {
      showToast(t('toast.error'), 'error')
      return false
    }
  }

  async function runFileAction(fn, okMessage) {
    busy.io = true
    try {
      const r = await fn()
      if (r?.cancelled) return r
      if (r?.ok) {
        if (okMessage) showToast(typeof okMessage === 'function' ? okMessage(r) : okMessage, 'ok')
      } else {
        showToast(r?.error || t('toast.error'), 'error')
      }
      return r
    } catch {
      showToast(t('toast.error'), 'error')
      return null
    } finally {
      busy.io = false
    }
  }

  function startPolling() {
    stopPolling()
    timer = setInterval(() => {
      if (document.visibilityState === 'hidden') return
      tick += 1
      if (status.running || tick % IDLE_SYNC_EVERY === 0) syncTraffic()
      if (tick % STATUS_EVERY === 0) refreshStatus()
    }, POLL_MS)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
    clearTimeout(queryTimer)
    queryTimer = null
  }

  async function init() {
    await Promise.all([loadConfig(), refreshStatus()])
    await syncTraffic()
    startPolling()
  }

  // Live push: a captured flow / finding / proxy start-stop pings us, so new
  // traffic shows up at once instead of only on the 1.5s poll tick.
  useLiveChannel('proxy', () => { syncTraffic() }, { debounceMs: 200 })

  return {
    status,
    flows,
    findings,
    filteredFlows,
    flowsById,
    flowFindings,
    ruleGroups,
    activeRule,
    hosts,
    filters,
    hasFilters,
    queryState,
    applyQuery,
    decryptedIds,
    decodeVersion,
    ruleDraft,
    rewriteDraft,
    selectedId,
    selectedFlow,
    busy,
    config,
    configDirty,
    endpoint,
    caTrusted,
    clearFilters,
    toggleIn,
    syncTraffic,
    refreshStatus,
    loadConfig,
    saveConfig,
    saveAndApply,
    start,
    stop,
    clearHistory,
    installCa,
    uninstallCa,
    copy,
    runFileAction,
    init,
    stopPolling,
  }
}
