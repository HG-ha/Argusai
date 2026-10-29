import { computed, onMounted, onUnmounted, ref, nextTick } from 'vue'

const GEOMETRY_KEY = 'argusai.sandbox.panelGeometry.v2'
const TREE_WIDTH_KEY = 'argusai.sandbox.treeSplitWidth'

const PANEL_MARGIN = 12
const MIN_PANEL_WIDTH = 520
const MIN_PANEL_HEIGHT = 360
const MIN_TREE_WIDTH = 180
const MIN_PREVIEW_WIDTH = 240
const DEFAULT_TREE_WIDTH = 280
const SPLITTER_WIDTH = 5

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : fallback
  } catch {
    return fallback
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch (_) {}
}

function titlebarTop(avoidTitlebar) {
  return avoidTitlebar ? 48 : PANEL_MARGIN
}

function defaultPanelGeometry(avoidTitlebar) {
  const top = titlebarTop(avoidTitlebar)
  const width = Math.max(MIN_PANEL_WIDTH, window.innerWidth - PANEL_MARGIN * 2)
  const height = Math.max(MIN_PANEL_HEIGHT, window.innerHeight - top - PANEL_MARGIN)
  return {
    left: PANEL_MARGIN,
    top,
    width,
    height,
  }
}

function defaultRestoredGeometry(avoidTitlebar) {
  const top = titlebarTop(avoidTitlebar)
  const width = Math.min(
    Math.max(MIN_PANEL_WIDTH, Math.round(window.innerWidth * 0.82)),
    Math.max(MIN_PANEL_WIDTH, window.innerWidth - PANEL_MARGIN * 2),
  )
  const height = Math.min(
    Math.max(MIN_PANEL_HEIGHT, Math.round(window.innerHeight * 0.78)),
    Math.max(MIN_PANEL_HEIGHT, window.innerHeight - top - PANEL_MARGIN),
  )
  return {
    left: Math.max(PANEL_MARGIN, Math.round((window.innerWidth - width) / 2)),
    top: Math.max(top, Math.round(top + (window.innerHeight - top - height) / 2)),
    width,
    height,
  }
}

function loadPanelGeometry(avoidTitlebar) {
  const saved = readJson(GEOMETRY_KEY, null)
  const fallback = defaultPanelGeometry(avoidTitlebar)
  if (!saved) return fallback
  return {
    left: Number.isFinite(saved.left) ? saved.left : fallback.left,
    top: Number.isFinite(saved.top) ? saved.top : fallback.top,
    width: Number.isFinite(saved.width) ? saved.width : fallback.width,
    height: Number.isFinite(saved.height) ? saved.height : fallback.height,
  }
}

function isSamePanelGeometry(a, b) {
  if (!a || !b) return false
  const tolerance = 2
  return Math.abs(Number(a.left) - Number(b.left)) <= tolerance
    && Math.abs(Number(a.top) - Number(b.top)) <= tolerance
    && Math.abs(Number(a.width) - Number(b.width)) <= tolerance
    && Math.abs(Number(a.height) - Number(b.height)) <= tolerance
}

function loadTreeWidth() {
  const n = Number(localStorage.getItem(TREE_WIDTH_KEY))
  return Number.isFinite(n) && n >= MIN_TREE_WIDTH ? n : DEFAULT_TREE_WIDTH
}

export function useSandboxPanelGeometry(options = {}) {
  const avoidTitlebar = () => Boolean(options.avoidTitlebar?.value ?? options.avoidTitlebar ?? false)

  const panelLeft = ref(0)
  const panelTop = ref(0)
  const panelWidth = ref(MIN_PANEL_WIDTH)
  const panelHeight = ref(MIN_PANEL_HEIGHT)
  const treeWidth = ref(DEFAULT_TREE_WIDTH)
  const workspaceBodyRef = ref(null)
  const isMaximized = ref(false)
  const restoreGeometry = ref(null)

  const panelStyle = computed(() => ({
    left: `${panelLeft.value}px`,
    top: `${panelTop.value}px`,
    width: `${panelWidth.value}px`,
    height: `${panelHeight.value}px`,
  }))

  const treePaneStyle = computed(() => ({
    flex: `0 0 ${treeWidth.value}px`,
    width: `${treeWidth.value}px`,
    minWidth: `${MIN_TREE_WIDTH}px`,
  }))

  function minPanelTop() {
    return titlebarTop(avoidTitlebar())
  }

  function maxPanelWidth() {
    return Math.max(MIN_PANEL_WIDTH, window.innerWidth - PANEL_MARGIN * 2)
  }

  function maxPanelHeight() {
    return Math.max(MIN_PANEL_HEIGHT, window.innerHeight - minPanelTop() - PANEL_MARGIN)
  }

  function maxPanelLeft() {
    return Math.max(PANEL_MARGIN, window.innerWidth - panelWidth.value - PANEL_MARGIN)
  }

  function maxPanelTop() {
    return Math.max(minPanelTop(), window.innerHeight - panelHeight.value - PANEL_MARGIN)
  }

  function maxTreeWidth() {
    const bodyW = workspaceBodyRef.value?.clientWidth || panelWidth.value
    return Math.max(MIN_TREE_WIDTH, bodyW - MIN_PREVIEW_WIDTH - SPLITTER_WIDTH)
  }

  function clampPanelGeometry() {
    panelWidth.value = Math.min(maxPanelWidth(), Math.max(MIN_PANEL_WIDTH, panelWidth.value))
    panelHeight.value = Math.min(maxPanelHeight(), Math.max(MIN_PANEL_HEIGHT, panelHeight.value))
    panelLeft.value = Math.min(maxPanelLeft(), Math.max(PANEL_MARGIN, panelLeft.value))
    panelTop.value = Math.min(maxPanelTop(), Math.max(minPanelTop(), panelTop.value))
    treeWidth.value = Math.min(maxTreeWidth(), Math.max(MIN_TREE_WIDTH, treeWidth.value))
  }

  function persistGeometry() {
    if (isMaximized.value) return
    writeJson(GEOMETRY_KEY, {
      left: panelLeft.value,
      top: panelTop.value,
      width: panelWidth.value,
      height: panelHeight.value,
    })
  }

  function persistTreeWidth() {
    try {
      localStorage.setItem(TREE_WIDTH_KEY, String(Math.round(treeWidth.value)))
    } catch (_) {}
  }

  function initGeometry() {
    const saved = readJson(GEOMETRY_KEY, null)
    const geo = loadPanelGeometry(avoidTitlebar())
    panelLeft.value = geo.left
    panelTop.value = geo.top
    panelWidth.value = geo.width
    panelHeight.value = geo.height
    treeWidth.value = loadTreeWidth()
    clampPanelGeometry()
    const maximizedGeometry = defaultPanelGeometry(avoidTitlebar())
    if (!saved || isSamePanelGeometry(snapshotGeometry(), maximizedGeometry)) {
      isMaximized.value = true
      restoreGeometry.value = defaultRestoredGeometry(avoidTitlebar())
      panelLeft.value = maximizedGeometry.left
      panelTop.value = maximizedGeometry.top
      panelWidth.value = maximizedGeometry.width
      panelHeight.value = maximizedGeometry.height
    } else {
      isMaximized.value = false
      restoreGeometry.value = null
    }
  }

  function snapshotGeometry() {
    return {
      left: panelLeft.value,
      top: panelTop.value,
      width: panelWidth.value,
      height: panelHeight.value,
    }
  }

  function applyGeometry(geo) {
    if (!geo) return
    panelLeft.value = Number.isFinite(geo.left) ? geo.left : panelLeft.value
    panelTop.value = Number.isFinite(geo.top) ? geo.top : panelTop.value
    panelWidth.value = Number.isFinite(geo.width) ? geo.width : panelWidth.value
    panelHeight.value = Number.isFinite(geo.height) ? geo.height : panelHeight.value
    clampPanelGeometry()
  }

  function togglePanelMaximized() {
    if (isMaximized.value) {
      isMaximized.value = false
      applyGeometry(restoreGeometry.value || defaultRestoredGeometry(avoidTitlebar()))
      restoreGeometry.value = null
      persistGeometry()
      return
    }
    restoreGeometry.value = snapshotGeometry()
    isMaximized.value = true
    const geo = defaultPanelGeometry(avoidTitlebar())
    panelLeft.value = geo.left
    panelTop.value = geo.top
    panelWidth.value = geo.width
    panelHeight.value = geo.height
    clampPanelGeometry()
  }

  function startPanelDrag(event) {
    if (isMaximized.value) return
    if (event.button !== undefined && event.button !== 0) return
    if (event.target?.closest?.('button, input, textarea, select, a, [data-no-drag]')) return
    event.preventDefault()
    const startX = event.clientX
    const startY = event.clientY
    const startLeft = panelLeft.value
    const startTop = panelTop.value
    let rafId = 0
    let latestEv = null
    document.body.classList.add('panel-dragging')
    const apply = () => {
      rafId = 0
      if (!latestEv) return
      panelLeft.value = Math.min(maxPanelLeft(), Math.max(PANEL_MARGIN, startLeft + latestEv.clientX - startX))
      panelTop.value = Math.min(maxPanelTop(), Math.max(minPanelTop(), startTop + latestEv.clientY - startY))
    }
    const onMove = (ev) => {
      latestEv = ev
      if (!rafId) rafId = requestAnimationFrame(apply)
    }
    const stop = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      if (rafId) {
        cancelAnimationFrame(rafId)
        apply()
      }
      document.body.classList.remove('panel-dragging')
      persistGeometry()
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
  }

  function startPanelResize(edge, event) {
    if (isMaximized.value) return
    event.preventDefault()
    const startX = event.clientX
    const startY = event.clientY
    const startW = panelWidth.value
    const startH = panelHeight.value
    const startLeft = panelLeft.value
    const startTop = panelTop.value
    let rafId = 0
    let latestEv = null
    document.body.classList.add('panel-dragging')
    const apply = () => {
      rafId = 0
      if (!latestEv) return
      const dx = latestEv.clientX - startX
      const dy = latestEv.clientY - startY
      if (edge.includes('w')) {
        const maxW = Math.min(maxPanelWidth(), startLeft + startW - PANEL_MARGIN)
        const nextW = Math.min(maxW, Math.max(MIN_PANEL_WIDTH, startW - dx))
        panelLeft.value = startLeft + (startW - nextW)
        panelWidth.value = nextW
      }
      if (edge.includes('e')) {
        panelWidth.value = Math.min(maxPanelWidth(), Math.max(MIN_PANEL_WIDTH, startW + dx))
      }
      if (edge.includes('n')) {
        const maxH = Math.min(maxPanelHeight(), startTop + startH - minPanelTop())
        const nextH = Math.min(maxH, Math.max(MIN_PANEL_HEIGHT, startH - dy))
        panelTop.value = startTop + (startH - nextH)
        panelHeight.value = nextH
      }
      if (edge.includes('s')) {
        panelHeight.value = Math.min(maxPanelHeight(), Math.max(MIN_PANEL_HEIGHT, startH + dy))
      }
      clampPanelGeometry()
    }
    const onMove = (ev) => {
      latestEv = ev
      if (!rafId) rafId = requestAnimationFrame(apply)
    }
    const stop = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      if (rafId) {
        cancelAnimationFrame(rafId)
        apply()
      }
      document.body.classList.remove('panel-dragging')
      persistGeometry()
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
  }

  function startTreeSplitDrag(event) {
    event.preventDefault()
    const startX = event.clientX
    const startW = treeWidth.value
    const onMove = (ev) => {
      treeWidth.value = Math.min(
        maxTreeWidth(),
        Math.max(MIN_TREE_WIDTH, startW + ev.clientX - startX),
      )
    }
    const stop = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      persistTreeWidth()
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
  }

  function onWindowResize() {
    clampPanelGeometry()
    if (isMaximized.value) {
      const geo = defaultPanelGeometry(avoidTitlebar())
      panelLeft.value = geo.left
      panelTop.value = geo.top
      panelWidth.value = geo.width
      panelHeight.value = geo.height
    }
  }

  onMounted(async () => {
    initGeometry()
    await nextTick()
    clampPanelGeometry()
    window.addEventListener('resize', onWindowResize)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', onWindowResize)
  })

  return {
    panelStyle,
    treePaneStyle,
    workspaceBodyRef,
    isMaximized,
    togglePanelMaximized,
    startPanelDrag,
    startPanelResize,
    startTreeSplitDrag,
    clampPanelGeometry,
    initGeometry,
  }
}
