<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import { useLiveChannel } from '../../composables/useLiveChannel.js'

// Interactive request breakpoints: park matching requests so a human (or the
// agent) can edit or drop them before they go upstream.

const cfg = reactive({ enabled: false, url_contains: '', host_contains: '', methods: '' })
const paused = ref([])
const edited = reactive({}) // id -> { method, url, headers, body_text }
const busy = reactive({})
let destroyed = false

async function loadConfig() {
  try {
    const r = await api.call('get_proxy_breakpoint_config')
    const c = (r && r.config) || {}
    cfg.enabled = !!c.enabled
    cfg.url_contains = c.url_contains || ''
    cfg.host_contains = c.host_contains || ''
    cfg.methods = (c.methods || []).join(', ')
  } catch (e) { /* ignore */ }
}

async function applyConfig() {
  try {
    const methods = cfg.methods.split(',').map((m) => m.trim()).filter(Boolean)
    await api.call('set_proxy_breakpoint_config', {
      enabled: cfg.enabled,
      url_contains: cfg.url_contains,
      host_contains: cfg.host_contains,
      methods,
    })
    loadPaused()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  }
}

function toggle() {
  cfg.enabled = !cfg.enabled
  applyConfig()
}

async function loadPaused() {
  try {
    const r = await api.call('list_proxy_breakpoints')
    if (destroyed) return
    paused.value = (r && r.paused) || []
    for (const p of paused.value) {
      if (!edited[p.id]) {
        edited[p.id] = {
          method: p.method,
          url: p.url,
          headers: (p.headers || []).map((kv) => `${kv[0]}: ${kv[1]}`).join('\n'),
          body_text: p.body_is_text ? (p.body_text || '') : '',
          body_is_text: p.body_is_text !== false,
        }
      }
    }
  } catch (e) { /* ignore */ }
}

function parseHeaders(text) {
  const out = []
  for (const line of String(text || '').split('\n')) {
    const idx = line.indexOf(':')
    if (idx <= 0) continue
    const name = line.slice(0, idx).trim()
    const value = line.slice(idx + 1).trim()
    if (name) out.push([name, value])
  }
  return out
}

async function resolve(p, action) {
  if (busy[p.id]) return
  busy[p.id] = true
  try {
    let edits
    if (action === 'forward') {
      const e = edited[p.id] || {}
      edits = { method: e.method, url: e.url, headers: parseHeaders(e.headers) }
      if (e.body_is_text) edits.body_text = e.body_text
    }
    const r = await api.call('resolve_proxy_breakpoint', p.id, action, edits || {})
    if (r && r.ok === false) showToast(r.error || 'failed', 'error')
    delete edited[p.id]
    loadPaused()
  } catch (e) {
    showToast(String(e?.message || e), 'error')
  } finally {
    busy[p.id] = false
  }
}

// Live push fires when a request parks or resolves.
useLiveChannel('proxy_breakpoint', () => { if (!destroyed) loadPaused() })

onMounted(() => {
  loadConfig()
  loadPaused()
})
onUnmounted(() => { destroyed = true })
</script>

<template>
  <section class="bp" :class="{ 'bp-active': paused.length }">
    <div class="bp-bar">
      <label class="bp-toggle">
        <input type="checkbox" :checked="cfg.enabled" @change="toggle" />
        <span>{{ t('proxy.breakpoint.enable') }}</span>
      </label>
      <span class="bp-hint">{{ t('proxy.breakpoint.hint') }}</span>
      <template v-if="cfg.enabled">
        <input v-model="cfg.url_contains" class="bp-filter" :placeholder="t('proxy.breakpoint.urlContains')" @change="applyConfig" />
        <input v-model="cfg.host_contains" class="bp-filter" :placeholder="t('proxy.breakpoint.hostContains')" @change="applyConfig" />
        <input v-model="cfg.methods" class="bp-filter" :placeholder="t('proxy.breakpoint.methods')" @change="applyConfig" />
      </template>
    </div>

    <div v-if="paused.length" class="bp-held">
      <div class="bp-held-head">{{ t('proxy.breakpoint.held', { n: paused.length }) }} · {{ t('proxy.breakpoint.autoForward') }}</div>
      <div v-for="p in paused" :key="p.id" class="bp-card">
        <div class="bp-row">
          <input v-model="edited[p.id].method" class="bp-method mono" />
          <input v-model="edited[p.id].url" class="bp-url mono" />
        </div>
        <label class="bp-field">
          <span>{{ t('proxy.breakpoint.headersLabel') }}</span>
          <textarea v-model="edited[p.id].headers" class="mono" rows="4"></textarea>
        </label>
        <label v-if="edited[p.id].body_is_text" class="bp-field">
          <span>{{ t('proxy.breakpoint.bodyLabel') }}</span>
          <textarea v-model="edited[p.id].body_text" class="mono" rows="3"></textarea>
        </label>
        <div class="bp-actions">
          <button type="button" class="primary" :disabled="busy[p.id]" @click="resolve(p, 'forward')">{{ t('proxy.breakpoint.forward') }}</button>
          <button type="button" class="danger" :disabled="busy[p.id]" @click="resolve(p, 'drop')">{{ t('proxy.breakpoint.drop') }}</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.bp { flex: 0 0 auto; margin-bottom: 10px; }
.bp-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; }
.bp-toggle { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; color: var(--fg); cursor: pointer; }
.bp-hint { color: var(--muted); }
.bp-filter { height: 26px; padding: 0 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--input-bg, var(--panel-2)); color: var(--fg); font-size: 12px; max-width: 160px; }
.bp-held { margin-top: 10px; border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--border)); border-radius: 10px; background: color-mix(in srgb, var(--warn) 7%, transparent); padding: 10px; }
.bp-held-head { font-size: 12px; font-weight: 700; color: var(--warn); margin-bottom: 8px; }
.bp-card { border: 1px solid var(--border); border-radius: 9px; background: var(--card-bg, var(--panel)); padding: 9px; margin-top: 8px; }
.bp-row { display: flex; gap: 6px; }
.bp-method { width: 78px; }
.bp-url { flex: 1; min-width: 0; }
.bp-row input { height: 28px; padding: 0 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--input-bg, var(--panel-2)); color: var(--fg); font-size: 12px; }
.bp-field { display: flex; flex-direction: column; gap: 4px; margin-top: 7px; font-size: 11px; color: var(--muted); }
.bp-field textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 7px; background: var(--input-bg, var(--panel-2)); color: var(--fg); padding: 6px 8px; font-size: 12px; resize: vertical; }
.bp-actions { display: flex; gap: 8px; margin-top: 8px; justify-content: flex-end; }
</style>
