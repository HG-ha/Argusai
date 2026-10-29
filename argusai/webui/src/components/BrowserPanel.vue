<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'

// The agent drives hidden (headless / off-screen) browsers. This panel shows
// a grid of live thumbnails of every tab it is operating; click one to open a
// full-size mirror (CDP screencast) that you can watch and take over.

const FRAME_MS = 110 // ~9fps full mirror; bridge round-trip dominates below this
const THUMB_MS = 1500 // grid thumbnails: low-fps stills
const STATUS_MS = 1000
const MOVE_THROTTLE_MS = 45

const view = ref('grid') // 'grid' | 'single'
const status = reactive({
  available: false,
  attached: false,
  headless: true,
  resolvedLabel: '',
  mode: 'agent', // 'agent' | 'user'
  agentBusy: false,
  canTake: false,
  running: false,
})
const thumbs = ref([])
const frameSrc = ref('')
const meta = reactive({ deviceWidth: 0, deviceHeight: 0, pageScaleFactor: 1 })
const selectedTarget = ref('')
const selectedTitle = ref('')
const starting = ref(false)
const stageEl = ref(null)
const imgEl = ref(null)

let frameTimer = null
let thumbTimer = null
let statusTimer = null
let lastSeq = -1
let lastMoveSent = 0
let destroyed = false

const userHasControl = computed(() => status.mode === 'user')
const controlLabel = computed(() => {
  if (status.mode === 'user') return t('browser.stateUser')
  if (status.agentBusy) return t('browser.stateAgentBusy')
  return t('browser.stateAgentIdle')
})
const controlTone = computed(() => {
  if (status.mode === 'user') return 'user'
  return status.agentBusy ? 'busy' : 'idle'
})

async function refreshStatus() {
  try {
    const r = await api.call('browser_live_status')
    if (destroyed || !r) return
    status.available = !!r.available
    status.attached = !!r.attached
    status.headless = r.headless !== false
    status.resolvedLabel = r.resolved_label || ''
    status.mode = r.mode || 'agent'
    status.agentBusy = !!r.agent_busy
    status.canTake = !!r.can_take
    status.running = !!r.screencast_on
    if (status.available && view.value === 'grid' && !thumbTimer) startThumbPolling()
  } catch (e) {
    /* transient bridge errors are fine; next tick retries */
  }
}

// ---- grid (thumbnails of every tab the agent is driving) -----------

async function loadThumbnails() {
  try {
    const r = await api.call('browser_live_thumbnails')
    if (destroyed) return
    if (r && r.available !== false) thumbs.value = r.tabs || []
  } catch (e) {
    /* keep last thumbnails on a transient error */
  }
}

function startThumbPolling() {
  if (thumbTimer) return
  loadThumbnails()
  thumbTimer = setInterval(() => {
    if (!destroyed && view.value === 'grid') loadThumbnails()
  }, THUMB_MS)
}

function stopThumbPolling() {
  if (thumbTimer) {
    clearInterval(thumbTimer)
    thumbTimer = null
  }
}

function thumbSrc(tb) {
  return tb && tb.thumb ? 'data:image/jpeg;base64,' + tb.thumb : ''
}

// ---- single (full-size mirror of one tab) --------------------------

async function openTab(tb) {
  selectedTarget.value = tb.target_id || ''
  selectedTitle.value = tb.title || tb.url || ''
  view.value = 'single'
  stopThumbPolling()
  await startView()
}

async function backToGrid() {
  await stopView()
  view.value = 'grid'
  startThumbPolling()
}

async function startView() {
  starting.value = true
  try {
    const r = await api.call('browser_live_start', { target_id: selectedTarget.value || '' })
    if (!r || r.ok === false) {
      showToast(r?.error || t('browser.startFailed'), 'error')
      return
    }
    status.running = true
    lastSeq = -1
    startFramePolling()
  } catch (e) {
    showToast(String(e), 'error')
  } finally {
    starting.value = false
  }
}

async function stopView() {
  stopFramePolling()
  try {
    await api.call('browser_live_stop')
  } catch (e) {
    /* ignore */
  }
  status.running = false
  frameSrc.value = ''
}

function startFramePolling() {
  if (frameTimer) return
  const tick = async () => {
    if (destroyed) return
    try {
      const r = await api.call('browser_live_frame')
      if (r && r.available !== false) {
        status.mode = r.control || status.mode
        status.agentBusy = !!r.agent_busy
        status.canTake = !!r.can_take
        status.running = !!r.running
        if (r.frame && r.seq !== lastSeq) {
          lastSeq = r.seq
          frameSrc.value = 'data:image/jpeg;base64,' + r.frame
          const m = r.metadata || {}
          meta.deviceWidth = Number(m.deviceWidth) || meta.deviceWidth
          meta.deviceHeight = Number(m.deviceHeight) || meta.deviceHeight
          meta.pageScaleFactor = Number(m.pageScaleFactor) || 1
        }
      }
    } catch (e) {
      /* ignore and keep polling */
    }
    if (!destroyed && frameTimer) frameTimer = setTimeout(tick, FRAME_MS)
  }
  frameTimer = setTimeout(tick, FRAME_MS)
}

function stopFramePolling() {
  if (frameTimer) {
    clearTimeout(frameTimer)
    frameTimer = null
  }
}

async function takeControl(force) {
  try {
    const r = await api.call('browser_live_take_control', !!force)
    if (r && r.ok) {
      status.mode = r.mode || 'user'
      focusStage()
    } else {
      showToast(t('browser.takeBlocked'), 'warning')
    }
  } catch (e) {
    showToast(String(e), 'error')
  }
}

async function releaseControl() {
  try {
    const r = await api.call('browser_live_release_control')
    if (r && r.ok) status.mode = r.mode || 'agent'
  } catch (e) {
    showToast(String(e), 'error')
  }
}

function focusStage() {
  if (stageEl.value) stageEl.value.focus()
}

// ---- input forwarding (CSS-pixel page coords) ----------------------

function mapToPage(ev) {
  const img = imgEl.value
  if (!img) return null
  const rect = img.getBoundingClientRect()
  const natW = img.naturalWidth || meta.deviceWidth || rect.width
  const natH = img.naturalHeight || meta.deviceHeight || rect.height
  if (!natW || !natH || !rect.width || !rect.height) return null
  const imgAspect = natW / natH
  const boxAspect = rect.width / rect.height
  let dispW, dispH, offX, offY
  if (boxAspect > imgAspect) {
    dispH = rect.height
    dispW = dispH * imgAspect
    offX = (rect.width - dispW) / 2
    offY = 0
  } else {
    dispW = rect.width
    dispH = dispW / imgAspect
    offX = 0
    offY = (rect.height - dispH) / 2
  }
  const px = (ev.clientX - rect.left - offX) / dispW
  const py = (ev.clientY - rect.top - offY) / dispH
  if (px < 0 || px > 1 || py < 0 || py > 1) return null
  const cssW = meta.deviceWidth || natW
  const cssH = meta.deviceHeight || natH
  return { x: px * cssW, y: py * cssH }
}

function modMask(e) {
  let m = 0
  if (e.altKey) m |= 1
  if (e.ctrlKey) m |= 2
  if (e.metaKey) m |= 4
  if (e.shiftKey) m |= 8
  return m
}

function sendEvents(events) {
  if (!events.length) return
  api.call('browser_live_input', { events }).catch(() => {})
}

function onMouseDown(e) {
  if (!userHasControl.value) return
  focusStage()
  const pt = mapToPage(e)
  if (!pt) return
  e.preventDefault()
  sendEvents([
    { domain: 'mouse', kind: 'down', ...pt, button: e.button, buttons: e.buttons || 1, clickCount: e.detail || 1, modifiers: modMask(e) },
  ])
}

function onMouseUp(e) {
  if (!userHasControl.value) return
  const pt = mapToPage(e)
  if (!pt) return
  e.preventDefault()
  sendEvents([
    { domain: 'mouse', kind: 'up', ...pt, button: e.button, buttons: 0, clickCount: e.detail || 1, modifiers: modMask(e) },
  ])
}

function onMouseMove(e) {
  if (!userHasControl.value) return
  const now = performance.now()
  if (now - lastMoveSent < MOVE_THROTTLE_MS) return
  lastMoveSent = now
  const pt = mapToPage(e)
  if (!pt) return
  sendEvents([{ domain: 'mouse', kind: 'move', ...pt, buttons: e.buttons || 0, modifiers: modMask(e) }])
}

function onWheel(e) {
  if (!userHasControl.value) return
  const pt = mapToPage(e)
  if (!pt) return
  e.preventDefault()
  sendEvents([{ domain: 'mouse', kind: 'wheel', ...pt, deltaX: e.deltaX, deltaY: e.deltaY, buttons: 0, modifiers: modMask(e) }])
}

function onKeyDown(e) {
  if (!userHasControl.value) return
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault()
    sendEvents([{ domain: 'key', kind: 'text', text: e.key }])
    return
  }
  e.preventDefault()
  sendEvents([{ domain: 'key', kind: 'keydown', key: e.key, code: e.code, modifiers: modMask(e) }])
}

function onKeyUp(e) {
  if (!userHasControl.value) return
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) return
  e.preventDefault()
  sendEvents([{ domain: 'key', kind: 'keyup', key: e.key, code: e.code, modifiers: modMask(e) }])
}

function onContextMenu(e) {
  if (userHasControl.value) e.preventDefault()
}

// Live push: the moment the agent spawns / navigates a browser, refresh.
useLiveChannel('browser_live', () => {
  if (destroyed) return
  refreshStatus()
  if (view.value === 'grid') loadThumbnails()
}, { debounceMs: 120 })

onMounted(async () => {
  await refreshStatus()
  statusTimer = setInterval(refreshStatus, STATUS_MS)
  if (status.available) startThumbPolling()
})

onUnmounted(() => {
  destroyed = true
  stopFramePolling()
  stopThumbPolling()
  if (statusTimer) clearInterval(statusTimer)
  api.call('browser_live_stop').catch(() => {})
  if (status.mode === 'user') api.call('browser_live_release_control').catch(() => {})
})
</script>

<template>
  <div class="page browser-page">
    <div class="page-head">
      <div class="browser-head-left">
        <button v-if="view === 'single'" type="button" class="ghost browser-back" @click="backToGrid">← {{ t('browser.backToGrid') }}</button>
        <div>
          <h1>{{ view === 'single' ? (selectedTitle || t('browser.title')) : t('browser.title') }}</h1>
          <p>{{ t('browser.subtitle') }}</p>
        </div>
      </div>
      <div class="actions row-gap-sm">
        <span v-if="view === 'single' && status.running" class="browser-live-pill"><span class="live-dot" aria-hidden="true"></span>{{ t('browser.live') }}</span>
      </div>
    </div>

    <div v-if="!status.available" class="browser-empty">
      <div class="browser-empty-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>
      </div>
      <h2>{{ t('browser.emptyTitle') }}</h2>
      <p>{{ t('browser.emptyBody') }}</p>
    </div>

    <!-- Grid: every tab the agent is operating; click to enlarge + operate. -->
    <div v-else-if="view === 'grid'" class="browser-grid">
      <button
        v-for="tb in thumbs"
        :key="tb.target_id || tb.index"
        type="button"
        class="browser-cell"
        :title="tb.url || tb.title"
        @click="openTab(tb)"
      >
        <div class="browser-cell-shot">
          <img v-if="thumbSrc(tb)" :src="thumbSrc(tb)" alt="" draggable="false" />
          <div v-else class="browser-cell-blank">{{ t('browser.waitingFrame') }}</div>
          <span v-if="tb.ai_owned" class="browser-cell-badge">AI</span>
        </div>
        <div class="browser-cell-title">{{ tb.browser ? (tb.browser + ' · ') : '' }}{{ tb.title || tb.url || 'tab' }}</div>
      </button>
      <div v-if="!thumbs.length" class="browser-grid-empty">{{ t('browser.noTabs') }}</div>
    </div>

    <!-- Single: full-size mirror + control hand-off. -->
    <template v-else>
      <div class="browser-bar">
        <div class="browser-state" :class="controlTone">
          <span class="state-dot" aria-hidden="true"></span>
          <span>{{ controlLabel }}</span>
        </div>
        <div class="browser-controls">
          <button
            v-if="!userHasControl"
            type="button"
            class="ghost"
            :disabled="!status.canTake"
            :title="t('browser.takeHint')"
            @click="takeControl(false)"
          >
            {{ t('browser.take') }}
          </button>
          <button v-if="!userHasControl" type="button" class="danger" :title="t('browser.forceHint')" @click="takeControl(true)">
            {{ t('browser.force') }}
          </button>
          <button v-if="userHasControl" type="button" class="primary" @click="releaseControl">
            {{ t('browser.release') }}
          </button>
        </div>
      </div>

      <div
        ref="stageEl"
        class="browser-stage"
        :class="{ interactive: userHasControl }"
        tabindex="0"
        @mousedown="onMouseDown"
        @mouseup="onMouseUp"
        @mousemove="onMouseMove"
        @wheel="onWheel"
        @keydown="onKeyDown"
        @keyup="onKeyUp"
        @contextmenu="onContextMenu"
      >
        <img v-if="frameSrc" ref="imgEl" class="browser-frame" :src="frameSrc" alt="live browser" draggable="false" />
        <div v-else class="browser-stage-waiting">{{ t('browser.waitingFrame') }}</div>
        <div v-if="status.running && !userHasControl" class="browser-overlay">{{ t('browser.readOnly') }}</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.browser-page { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.browser-head-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.browser-back { flex: 0 0 auto; }
.browser-live-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: #2f7d32; }
.browser-live-pill .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #2f7d32; box-shadow: 0 0 0 3px color-mix(in srgb, #2f7d32 22%, transparent); animation: browser-live-pulse 1.6s ease-in-out infinite; }
@keyframes browser-live-pulse { 50% { opacity: .4; } }

/* Grid overview */
.browser-grid {
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  display: flex; flex-wrap: wrap; align-content: flex-start; gap: 14px; padding: 4px 2px;
}
.browser-cell {
  flex: 0 1 300px; display: flex; flex-direction: column; gap: 6px;
  border: 1px solid var(--border, #e3e3e8); border-radius: 12px; overflow: hidden;
  background: var(--surface, #fff); padding: 0; cursor: pointer; text-align: left;
}
.browser-cell:hover { border-color: var(--accent); box-shadow: 0 2px 12px rgba(0,0,0,.08); }
.browser-cell-shot { position: relative; aspect-ratio: 16 / 10; background: #0f1115; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.browser-cell-shot img { width: 100%; height: 100%; object-fit: cover; object-position: top center; }
.browser-cell-blank { color: #8a90a0; font-size: 12px; }
.browser-cell-badge { position: absolute; top: 8px; left: 8px; font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 999px; background: color-mix(in srgb, var(--accent) 85%, #000); color: #fff; }
.browser-cell-title { padding: 0 10px 10px; font-size: 12px; color: var(--text-primary, #1f2328); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.browser-grid-empty { color: var(--muted, #6b7280); font-size: 13px; padding: 24px; }

/* Single mirror */
.browser-bar {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap; flex: 0 0 auto;
  padding: 7px 12px; border: 1px solid var(--border, #e3e3e8); border-radius: 10px;
  background: var(--surface, #fff); margin-bottom: 10px;
}
.browser-state { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; font-size: 13px; }
.browser-state .state-dot { width: 9px; height: 9px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 22%, transparent); }
.browser-state.user { color: #2f7d32; }
.browser-state.busy { color: #c76a12; }
.browser-state.idle { color: #5b6270; }
.browser-controls { margin-left: auto; display: inline-flex; gap: 8px; }
.browser-stage {
  position: relative; flex: 1 1 auto; min-height: 0; border-radius: 12px; overflow: hidden;
  background: #0f1115; border: 1px solid var(--border, #e3e3e8);
  display: flex; align-items: center; justify-content: center; outline: none;
}
.browser-stage.interactive { cursor: crosshair; box-shadow: 0 0 0 2px #2f7d32 inset; }
.browser-frame { max-width: 100%; max-height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none; }
.browser-stage-waiting { color: #8a90a0; font-size: 13px; }
.browser-overlay {
  position: absolute; top: 10px; right: 12px; padding: 4px 10px; border-radius: 999px;
  background: rgba(15, 17, 21, 0.72); color: #e7eaf0; font-size: 12px; pointer-events: none;
}
.browser-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--muted, #6b7280); text-align: center; padding: 40px; }
.browser-empty-icon { opacity: 0.5; }
.browser-empty h2 { margin: 0; font-size: 16px; }
.browser-empty p { margin: 0; max-width: 420px; font-size: 13px; }
</style>
