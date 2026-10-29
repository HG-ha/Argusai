<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { t } from '../../i18n.js'
import { copyMindmapAsImage } from './mindmapCopyImage.js'
import { layoutMindmap, linkPath } from './mindmapParse.js'

const MIN_SCALE = 0.22
const MAX_SCALE = 3.6
const INLINE_MIN = 128
const INLINE_MAX = 248

const props = defineProps({
  tree: { type: Object, default: null },
  variant: { type: String, default: 'inline' },
})
const emit = defineEmits(['expand'])

const rootRef = ref(null)
const viewportRef = ref(null)
const copyState = ref('')
let copyBusy = false
let copyTimer = null
const scale = ref(1)
const panX = ref(0)
const panY = ref(0)
const viewportHeight = ref(INLINE_MAX)
const panning = ref(false)
const userMoved = ref(false)
let drag = null
let wheelBound = null
let resizeObs = null

const isOverlay = computed(() => props.variant === 'overlay')
const layout = computed(() => layoutMindmap(props.tree))
const scaleLabel = computed(() => `${Math.round(scale.value * 100)}%`)

const worldStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${scale.value})`,
}))

const viewportStyle = computed(() => (
  isOverlay.value
    ? { height: '100%' }
    : { height: `${viewportHeight.value}px` }
))

function clampScale(value) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))
}

function scheduleFitRetries() {
  ;[32, 120, 360].forEach((ms) => {
    window.setTimeout(() => {
      if (!userMoved.value) fitToView()
    }, ms)
  })
}

function fitToView() {
  const el = viewportRef.value
  const lay = layout.value
  if (!el || !lay || !lay.boxes.length) return
  const vw = el.clientWidth
  if (vw < 8) return

  let vh = isOverlay.value ? el.clientHeight : INLINE_MAX
  if (vh < 8) return

  const padX = isOverlay.value ? 28 : 16
  const padTop = 36
  const padBottom = isOverlay.value ? 20 : 12
  const fitted = clampScale(Math.min(
    (vw - padX * 2) / lay.width,
    (vh - padTop - padBottom) / lay.height,
    isOverlay.value ? 1.4 : 1.06,
  ))

  if (!isOverlay.value) {
    const contentH = lay.height * fitted + padTop + padBottom
    vh = Math.round(Math.min(INLINE_MAX, Math.max(INLINE_MIN, contentH)))
    viewportHeight.value = vh
  }

  scale.value = fitted
  panX.value = (vw - lay.width * fitted) / 2
  panY.value = padTop + (vh - padTop - padBottom - lay.height * fitted) / 2
}

function zoomAt(clientX, clientY, factor) {
  const el = viewportRef.value
  if (!el) return
  const next = clampScale(scale.value * factor)
  if (next === scale.value) return
  const rect = el.getBoundingClientRect()
  const cx = clientX - rect.left
  const cy = clientY - rect.top
  const ratio = next / scale.value
  panX.value = cx - (cx - panX.value) * ratio
  panY.value = cy - (cy - panY.value) * ratio
  scale.value = next
  userMoved.value = true
}

function zoomStep(direction) {
  const el = viewportRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, direction > 0 ? 1.18 : 1 / 1.18)
}

function resetView() {
  userMoved.value = false
  fitToView()
}

function onWheel(e) {
  const pinch = e.ctrlKey || e.metaKey || e.altKey
  if (!isOverlay.value && !pinch) return
  e.preventDefault()
  e.stopPropagation()
  const factor = Math.exp(-e.deltaY * 0.0016)
  zoomAt(e.clientX, e.clientY, factor)
}

function onPointerDown(e) {
  if (e.button != null && e.button !== 0) return
  if (e.target?.closest?.('button')) return
  drag = {
    x: e.clientX,
    y: e.clientY,
    panX: panX.value,
    panY: panY.value,
  }
  panning.value = true
  e.currentTarget?.setPointerCapture?.(e.pointerId)
}

function onPointerMove(e) {
  if (!drag) return
  const dx = e.clientX - drag.x
  const dy = e.clientY - drag.y
  if (Math.abs(dx) + Math.abs(dy) > 2) userMoved.value = true
  panX.value = drag.panX + dx
  panY.value = drag.panY + dy
}

function onPointerUp() {
  drag = null
  panning.value = false
}

function onDoubleClick(e) {
  if (e.target?.closest?.('button')) return
  resetView()
}

async function copyAsImage() {
  if (copyBusy) return copyState.value || 'fail'
  copyBusy = true
  try {
    const result = await copyMindmapAsImage(props.tree, rootRef.value)
    copyState.value = result
    return result
  } catch {
    copyState.value = 'fail'
    return 'fail'
  } finally {
    copyBusy = false
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copyState.value = '' }, 1400)
  }
}

function bindWheel() {
  const el = viewportRef.value
  if (!el || wheelBound) return
  el.addEventListener('wheel', onWheel, { passive: false })
  wheelBound = el
}

function unbindWheel() {
  if (!wheelBound) return
  wheelBound.removeEventListener('wheel', onWheel)
  wheelBound = null
}

watch(layout, async () => {
  await nextTick()
  if (!userMoved.value) {
    fitToView()
    scheduleFitRetries()
  }
})

onMounted(() => {
  bindWheel()
  nextTick(() => {
    fitToView()
    scheduleFitRetries()
  })
  if (typeof ResizeObserver !== 'undefined' && viewportRef.value) {
    resizeObs = new ResizeObserver(() => {
      if (!userMoved.value) fitToView()
    })
    resizeObs.observe(viewportRef.value)
  }
})

onUnmounted(() => {
  unbindWheel()
  if (resizeObs) {
    resizeObs.disconnect()
    resizeObs = null
  }
  if (copyTimer) {
    clearTimeout(copyTimer)
    copyTimer = null
  }
})

defineExpose({ fitToView, resetView, copyAsImage })
</script>

<template>
  <div ref="rootRef" class="mm-root" :class="{ 'is-overlay': isOverlay }">
    <div class="mm-toolbar">
      <div class="mm-zoom">
        <button type="button" class="mm-btn" :title="t('assistant.mindmapZoomOut')" @click="zoomStep(-1)">−</button>
        <span class="mm-scale" :title="t('assistant.mindmapHint')">{{ scaleLabel }}</span>
        <button type="button" class="mm-btn" :title="t('assistant.mindmapZoomIn')" @click="zoomStep(1)">+</button>
        <button type="button" class="mm-btn mm-fit" :title="t('assistant.mindmapFit')" @click="resetView">{{ t('assistant.mindmapFit') }}</button>
      </div>
      <div class="mm-actions">
        <button
          type="button"
          class="md-expand-btn mm-copy-btn"
          :class="{ 'is-ok': copyState === 'copied' || copyState === 'downloaded', 'is-fail': copyState === 'fail' }"
          :title="copyState === 'copied'
            ? t('assistant.tableCopiedImage')
            : copyState === 'downloaded'
              ? t('assistant.tableDownloadedImage')
              : copyState === 'fail'
                ? t('assistant.tableCopyImageFail')
                : t('assistant.copyTableImage')"
          :aria-label="t('assistant.copyTableImage')"
          @click="copyAsImage"
        >
          <svg v-if="copyState === 'copied' || copyState === 'downloaded'" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
            <path d="M3.4 8.3l3.05 3.05 6.15-6.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <svg v-else class="md-copy-image-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
            <rect x="2.25" y="3.25" width="11.5" height="9.5" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.35" />
            <circle cx="5.6" cy="6.35" r="1.05" fill="currentColor" />
            <path d="M3.4 11.15l3.05-3.05 1.85 1.85 2.15-2.45 2.15 3.65" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button
          v-if="!isOverlay"
          type="button"
          class="md-expand-btn"
          :title="t('assistant.expandView')"
          :aria-label="t('assistant.expandView')"
          @click="emit('expand')"
        >
          <svg class="md-expand-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
            <path d="M9.25 2.5h4.25V6.75M6.75 13.5H2.5V9.25M13.5 2.5l-4.2 4.2M2.5 13.5l4.2-4.2" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      </div>
    </div>
    <div
      ref="viewportRef"
      class="mm-viewport"
      :class="{ 'is-panning': panning }"
      :style="viewportStyle"
      :title="t('assistant.mindmapHint')"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @dblclick="onDoubleClick"
    >
      <div class="mm-world" :style="worldStyle">
        <svg
          class="mm-svg"
          :width="layout.width"
          :height="layout.height"
          :viewBox="`0 0 ${layout.width} ${layout.height}`"
        >
          <path
            v-for="(link, i) in layout.links"
            :key="'l' + i"
            class="mm-link"
            :d="linkPath(link)"
          />
          <g
            v-for="box in layout.boxes"
            :key="box.id"
            class="mm-node"
            :class="{ 'is-root': box.root, ['depth-' + Math.min(box.depth, 4)]: true }"
            :transform="`translate(${box.x}, ${box.y})`"
          >
            <rect :width="box.w" :height="box.h" rx="8" />
            <text
              :x="box.w / 2"
              :y="(box.h - box.lines.length * 16) / 2 + 12"
              text-anchor="middle"
            >
              <tspan
                v-for="(line, li) in box.lines"
                :key="li"
                :x="box.w / 2"
                :dy="li === 0 ? 0 : 16"
              >{{ line }}</tspan>
            </text>
          </g>
        </svg>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mm-root {
  position: relative;
  margin: 8px 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  background:
    radial-gradient(circle at 12% 20%, color-mix(in srgb, var(--accent) 8%, transparent), transparent 42%),
    var(--bg-sunken);
  overflow: hidden;
  min-width: 0;
}
.mm-root.is-overlay {
  margin: 0;
  height: 100%;
  border: 0;
  border-radius: 0;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(circle at 18% 12%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 36%),
    var(--bg-sunken);
}
.mm-toolbar {
  position: absolute;
  top: 6px;
  left: 6px;
  right: 6px;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  pointer-events: none;
}
.mm-zoom,
.mm-actions,
.md-expand-btn {
  pointer-events: auto;
}
.mm-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mm-zoom {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel-2) 88%, transparent);
}
.mm-btn {
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
}
.mm-btn:hover {
  color: var(--fg);
  background: var(--panel-hover);
}
.mm-fit {
  font-size: 11px;
}
.mm-scale {
  min-width: 38px;
  padding: 0 4px;
  color: var(--muted-2);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  text-align: center;
  user-select: none;
}
.md-expand-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 88%, transparent);
  color: var(--muted);
  cursor: pointer;
}
.md-expand-btn:hover {
  color: var(--fg);
  border-color: var(--border-strong);
}
.mm-copy-btn.is-ok {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.mm-copy-btn.is-fail {
  color: var(--danger, #e36b6b);
  border-color: color-mix(in srgb, #e36b6b 45%, var(--border));
}
.mm-viewport {
  width: 100%;
  min-width: 0;
  overflow: hidden;
  touch-action: none;
  overscroll-behavior: contain;
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
}
.mm-viewport.is-panning {
  cursor: grabbing;
}
.mm-root.is-overlay .mm-viewport {
  flex: 1;
  min-height: 0;
}
.mm-world {
  transform-origin: 0 0;
  will-change: transform;
}
.mm-svg {
  display: block;
  overflow: visible;
}
.mm-link {
  fill: none;
  stroke: color-mix(in srgb, var(--accent) 38%, var(--border-strong));
  stroke-width: 1.35;
  stroke-linecap: round;
}
.mm-node rect {
  fill: var(--panel-2);
  stroke: var(--border-strong);
  stroke-width: 1;
}
.mm-node text {
  fill: var(--fg);
  font: 12px/16px var(--ui);
}
.mm-node.is-root rect {
  fill: color-mix(in srgb, var(--accent) 18%, var(--panel-2));
  stroke: color-mix(in srgb, var(--accent) 55%, var(--border));
}
.mm-node.is-root text {
  fill: var(--fg-strong);
  font-weight: 650;
}
.mm-node.depth-2 rect,
.mm-node.depth-3 rect,
.mm-node.depth-4 rect {
  fill: var(--bg-sunken);
}
@media (prefers-reduced-motion: reduce) {
  .mm-world {
    transition: none;
  }
}
</style>
