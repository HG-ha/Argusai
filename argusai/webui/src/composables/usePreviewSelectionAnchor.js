// Floating "引用此片段" / context-menu positioning for text preview panes.
// Tracks the pointer during drag-select and places UI near the release point
// (not at the selection bounding-box right edge).

import { computed, reactive, ref } from 'vue'

export function usePreviewSelectionAnchor(boundsRef, options = {}) {
  const pillWidth = options.pillWidth ?? 118
  const pillHeight = options.pillHeight ?? 30
  const menuWidth = options.menuWidth ?? 132
  const menuHeight = options.menuHeight ?? 72
  const ignoreSelector = options.ignoreSelector ?? '.text-selection-pill, .preview-selection-menu'

  // Use ``reactive`` (not ``ref``) so consumers can write
  // ``selectionUi.anchor.visible`` directly in templates without manually
  // unwrapping a ref via ``.value`` (Vue 3 only auto-unwraps top-level
  // setup bindings, not ref properties on plain objects).
  const anchor = reactive({ visible: false, left: 0, top: 0 })
  const menu = reactive({ visible: false, left: 0, top: 0 })
  const lastPointerClient = ref({ x: 0, y: 0, valid: false })
  let pointerSelecting = false

  const pillStyle = computed(() => ({
    left: `${anchor.left}px`,
    top: `${anchor.top}px`,
  }))
  const menuStyle = computed(() => ({
    left: `${menu.left}px`,
    top: `${menu.top}px`,
  }))

  function recordPointerClient(event) {
    if (!event || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return
    lastPointerClient.value = { x: event.clientX, y: event.clientY, valid: true }
  }

  function isPointerLikeEvent(event) {
    const t = String(event?.type || '')
    return t.startsWith('pointer') || t.startsWith('mouse') || t === 'contextmenu'
  }

  function caretClientPoint() {
    const sel = window.getSelection?.()
    if (!sel?.rangeCount) return null
    const range = sel.getRangeAt(0).cloneRange()
    range.collapse(false)
    const rects = range.getClientRects()
    const r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
    if (!r || (!r.width && !r.height && r.top === 0 && r.left === 0)) return null
    return { x: r.left + 4, y: r.bottom }
  }

  function uiPositionFromEvent(event, width, height) {
    const wrap = boundsRef.value
    if (!wrap) return { left: 8, top: 8 }
    const rect = wrap.getBoundingClientRect()
    let clientX
    let clientY
    if (lastPointerClient.value.valid) {
      clientX = lastPointerClient.value.x
      clientY = lastPointerClient.value.y
    } else if (isPointerLikeEvent(event) && Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) {
      clientX = event.clientX
      clientY = event.clientY
    } else {
      const caret = caretClientPoint()
      if (caret) {
        clientX = caret.x
        clientY = caret.y
      } else {
        clientX = rect.left + 24
        clientY = rect.top + 24
      }
    }
    const pad = 8
    const maxW = wrap.clientWidth || 0
    const maxH = wrap.clientHeight || 0
    const offsetX = 8
    let left = clientX - rect.left + offsetX
    let top = clientY - rect.top - height - 8
    if (left + width > maxW - pad) {
      left = clientX - rect.left - width - offsetX
    }
    if (top < pad) {
      top = clientY - rect.top + 12
    }
    if (top + height > maxH - pad) {
      top = clientY - rect.top - height - 8
    }
    return {
      left: Math.max(pad, Math.min(left, Math.max(pad, maxW - width - pad))),
      top: Math.max(pad, Math.min(top, Math.max(pad, maxH - height - pad))),
    }
  }

  function hideAnchor() {
    anchor.visible = false
    anchor.left = 0
    anchor.top = 0
    menu.visible = false
    menu.left = 0
    menu.top = 0
  }

  function resetPointer() {
    lastPointerClient.value = { x: 0, y: 0, valid: false }
    pointerSelecting = false
  }

  function showPill(event, overrides = {}) {
    const w = overrides.width ?? pillWidth
    const h = overrides.height ?? pillHeight
    const pos = uiPositionFromEvent(event, w, h)
    menu.visible = false
    menu.left = 0
    menu.top = 0
    anchor.left = pos.left
    anchor.top = pos.top
    anchor.visible = true
  }

  function showMenu(event, overrides = {}) {
    const w = overrides.width ?? menuWidth
    const h = overrides.height ?? menuHeight
    const pos = uiPositionFromEvent(event, w, h)
    anchor.visible = false
    anchor.left = 0
    anchor.top = 0
    menu.left = pos.left
    menu.top = pos.top
    menu.visible = true
  }

  function onPointerDown(event) {
    if (event.button !== 0) return
    recordPointerClient(event)
    pointerSelecting = true
  }

  function onPointerMove(event) {
    if (pointerSelecting || (event.buttons & 1)) {
      recordPointerClient(event)
    }
  }

  function createPointerUpHandler(onAfterSelection) {
    return (event) => {
      if (event.target?.closest?.(ignoreSelector)) return
      recordPointerClient(event)
      pointerSelecting = false
      requestAnimationFrame(() => onAfterSelection(event))
    }
  }

  return {
    anchor,
    menu,
    pillStyle,
    menuStyle,
    hideAnchor,
    resetPointer,
    showPill,
    showMenu,
    recordPointerClient,
    onPointerDown,
    onPointerMove,
    createPointerUpHandler,
  }
}

// NOTE: ``pillStyle`` / ``menuStyle`` are ComputedRefs. Destructure them at
// the call site (``const { pillStyle } = usePreviewSelectionAnchor(...)``) so
// Vue templates auto-unwrap them in ``:style`` bindings. Accessing
// ``selectionUi.pillStyle`` on a plain object does NOT unwrap.

/** Convert pane-local anchor coords (from ``uiPositionFromEvent``) to viewport pixels. */
export function localAnchorToViewport(boundsEl, local, offset = { left: 0, top: 0 }) {
  const rect = boundsEl?.getBoundingClientRect?.()
  const baseLeft = local?.left || 0
  const baseTop = local?.top || 0
  const dx = offset?.left || 0
  const dy = offset?.top || 0
  if (!rect) {
    return { left: baseLeft + dx, top: baseTop + dy }
  }
  return {
    left: rect.left + baseLeft + dx,
    top: rect.top + baseTop + dy,
  }
}
