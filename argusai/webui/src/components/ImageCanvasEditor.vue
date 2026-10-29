<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { t } from '../i18n.js'

const props = defineProps({
  source: { type: String, default: '' },
  name: { type: String, default: 'image.png' },
  applyLabel: { type: String, default: '' },
  duplicateLabel: { type: String, default: '' },
  cancelLabel: { type: String, default: '' },
  allowDuplicate: { type: Boolean, default: false },
})

const emit = defineEmits(['apply', 'duplicate', 'cancel', 'error', 'ready'])

const canvasRef = ref(null)
const overlayRef = ref(null)
const tool = ref('brush')
const shapeKind = ref('rect')
const color = ref('#ff3b30')
const brushSize = ref(8)
const mosaicSize = ref(18)
const brightness = ref(100)
const contrast = ref(100)
const history = ref([])
const imageInfo = ref({ width: 0, height: 0 })
const drawing = ref(false)
const dragStart = ref(null)
const cropRect = ref(null)
const cropDrag = ref(null)
const textDraft = ref('')
const textAnchor = ref(null)
const textInputRef = ref(null)
let originalDataUrl = ''

const editorTools = computed(() => [
  { id: 'brush', label: t('imageEditor.brush') },
  { id: 'text', label: t('imageEditor.text') },
  { id: 'shape', label: t('imageEditor.shape') },
  { id: 'mosaic', label: t('imageEditor.mosaic') },
  { id: 'crop', label: t('imageEditor.crop') },
  { id: 'rotate', label: t('imageEditor.rotate') },
  { id: 'filter', label: t('imageEditor.filter') },
])
const applyText = computed(() => props.applyLabel || t('imageEditor.apply'))
const cancelText = computed(() => props.cancelLabel || t('imageEditor.cancel'))
const duplicateText = computed(() => props.duplicateLabel || t('imageEditor.duplicate'))

const canEdit = computed(() => imageInfo.value.width > 0)
const canUndo = computed(() => history.value.length > 0)
const textBoxStyle = computed(() => {
  if (!textAnchor.value) return {}
  const canvas = canvasRef.value
  if (!canvas) return {}
  const rect = canvas.getBoundingClientRect()
  const scaleX = rect.width / Math.max(1, canvas.width)
  const scaleY = rect.height / Math.max(1, canvas.height)
  return {
    left: `${textAnchor.value.x * scaleX}px`,
    top: `${textAnchor.value.y * scaleY}px`,
    minWidth: `${Math.max(160, canvas.width * scaleX * 0.18)}px`,
    maxWidth: `${Math.max(180, canvas.width * scaleX - textAnchor.value.x * scaleX - 12)}px`,
    fontSize: `${Math.max(14, Number(brushSize.value || 8) * 4 * scaleY)}px`,
    color: color.value,
  }
})

function dataUrlToImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = reject
    img.src = src
  })
}

function context2d(canvas = canvasRef.value) {
  return canvas?.getContext('2d') || null
}

function clearOverlay() {
  const overlay = overlayRef.value
  const ctx = context2d(overlay)
  if (!overlay || !ctx) return
  ctx.clearRect(0, 0, overlay.width, overlay.height)
}

function clearPendingText() {
  textDraft.value = ''
  textAnchor.value = null
}

function syncOverlaySize() {
  const canvas = canvasRef.value
  const overlay = overlayRef.value
  if (!canvas || !overlay) return
  overlay.width = canvas.width
  overlay.height = canvas.height
  clearOverlay()
  if (tool.value === 'crop' && cropRect.value) drawCropOverlay(cropRect.value)
}

function pushHistory() {
  const canvas = canvasRef.value
  if (!canvas || !canvas.width || !canvas.height) return
  try {
    history.value = [...history.value.slice(-39), canvas.toDataURL('image/png')]
  } catch (_) {
    // Keep editing alive; export will surface any canvas encoding failure.
  }
}

async function restoreDataUrl(src, recordOriginal = false) {
  const img = await dataUrlToImage(src)
  const canvas = canvasRef.value
  const ctx = context2d()
  if (!canvas || !ctx) return
  canvas.width = img.naturalWidth || img.width
  canvas.height = img.naturalHeight || img.height
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  imageInfo.value = { width: canvas.width, height: canvas.height }
  if (recordOriginal) originalDataUrl = canvas.toDataURL('image/png')
  syncOverlaySize()
  emit('ready', imageInfo.value)
}

async function loadSource() {
  if (!props.source) return
  try {
    history.value = []
    cropRect.value = null
    cropDrag.value = null
    clearPendingText()
    brightness.value = 100
    contrast.value = 100
    await nextTick()
    await restoreDataUrl(props.source, true)
  } catch (e) {
    emit('error', e?.message || e || t('imageEditor.loadFailed'))
  }
}

function canvasPoint(e) {
  const canvas = canvasRef.value
  if (!canvas) return null
  const rect = canvas.getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  const x = (e.clientX - rect.left) * (canvas.width / rect.width)
  const y = (e.clientY - rect.top) * (canvas.height / rect.height)
  return {
    x: Math.max(0, Math.min(canvas.width, x)),
    y: Math.max(0, Math.min(canvas.height, y)),
  }
}

function drawBrushLine(a, b) {
  const ctx = context2d()
  if (!ctx || !a || !b) return
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.strokeStyle = color.value
  ctx.lineWidth = Number(brushSize.value || 8)
  ctx.beginPath()
  ctx.moveTo(a.x, a.y)
  ctx.lineTo(b.x, b.y)
  ctx.stroke()
}

function drawRect(ctx, a, b) {
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  const w = Math.abs(b.x - a.x)
  const h = Math.abs(b.y - a.y)
  ctx.strokeStyle = color.value
  ctx.lineWidth = Number(brushSize.value || 8)
  ctx.strokeRect(x, y, w, h)
}

function drawArrow(ctx, a, b) {
  const size = Math.max(10, Number(brushSize.value || 8) * 2.2)
  const angle = Math.atan2(b.y - a.y, b.x - a.x)
  ctx.strokeStyle = color.value
  ctx.fillStyle = color.value
  ctx.lineWidth = Number(brushSize.value || 8)
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(a.x, a.y)
  ctx.lineTo(b.x, b.y)
  ctx.stroke()
  ctx.beginPath()
  ctx.moveTo(b.x, b.y)
  ctx.lineTo(b.x - size * Math.cos(angle - Math.PI / 6), b.y - size * Math.sin(angle - Math.PI / 6))
  ctx.lineTo(b.x - size * Math.cos(angle + Math.PI / 6), b.y - size * Math.sin(angle + Math.PI / 6))
  ctx.closePath()
  ctx.fill()
}

function normalizedCropRect(rect) {
  const canvas = canvasRef.value
  if (!canvas || !rect) return null
  const minSize = Math.max(12, Math.min(canvas.width, canvas.height) * 0.025)
  let x = Number(rect.x || 0)
  let y = Number(rect.y || 0)
  let w = Number(rect.w || 0)
  let h = Number(rect.h || 0)
  if (w < 0) {
    x += w
    w = Math.abs(w)
  }
  if (h < 0) {
    y += h
    h = Math.abs(h)
  }
  const right = Math.max(0, Math.min(x + Math.max(w, minSize), canvas.width))
  const bottom = Math.max(0, Math.min(y + Math.max(h, minSize), canvas.height))
  x = Math.max(0, Math.min(x, right - minSize))
  y = Math.max(0, Math.min(y, bottom - minSize))
  return { x, y, w: right - x, h: bottom - y }
}

function setCropRect(rect) {
  const next = normalizedCropRect(rect)
  if (!next) return
  cropRect.value = next
  drawCropOverlay(next)
}

function resetCropRect() {
  const canvas = canvasRef.value
  if (!canvas?.width || !canvas?.height) return
  const w = canvas.width * 0.8
  const h = canvas.height * 0.8
  setCropRect({
    x: (canvas.width - w) / 2,
    y: (canvas.height - h) / 2,
    w,
    h,
  })
}

function drawCropOverlay(rect) {
  const overlay = overlayRef.value
  const ctx = context2d(overlay)
  const r = normalizedCropRect(rect)
  if (!overlay || !ctx || !r) return
  clearOverlay()
  ctx.save()
  ctx.fillStyle = 'rgba(0, 0, 0, 0.48)'
  ctx.fillRect(0, 0, overlay.width, overlay.height)
  ctx.clearRect(r.x, r.y, r.w, r.h)

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.92)'
  ctx.lineWidth = Math.max(2, overlay.width / 900)
  ctx.strokeRect(r.x, r.y, r.w, r.h)

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.58)'
  ctx.lineWidth = Math.max(1, overlay.width / 1600)
  for (let i = 1; i <= 2; i += 1) {
    const gx = r.x + (r.w * i) / 3
    const gy = r.y + (r.h * i) / 3
    ctx.beginPath()
    ctx.moveTo(gx, r.y)
    ctx.lineTo(gx, r.y + r.h)
    ctx.moveTo(r.x, gy)
    ctx.lineTo(r.x + r.w, gy)
    ctx.stroke()
  }

  ctx.strokeStyle = '#ffffff'
  ctx.lineWidth = Math.max(4, overlay.width / 400)
  const len = Math.max(16, overlay.width / 40)
  ctx.beginPath()
  ctx.moveTo(r.x, r.y + len)
  ctx.lineTo(r.x, r.y)
  ctx.lineTo(r.x + len, r.y)
  ctx.moveTo(r.x + r.w - len, r.y)
  ctx.lineTo(r.x + r.w, r.y)
  ctx.lineTo(r.x + r.w, r.y + len)
  ctx.moveTo(r.x + r.w, r.y + r.h - len)
  ctx.lineTo(r.x + r.w, r.y + r.h)
  ctx.lineTo(r.x + r.w - len, r.y + r.h)
  ctx.moveTo(r.x + len, r.y + r.h)
  ctx.lineTo(r.x, r.y + r.h)
  ctx.lineTo(r.x, r.y + r.h - len)
  ctx.moveTo(r.x + r.w / 2 - len / 2, r.y)
  ctx.lineTo(r.x + r.w / 2 + len / 2, r.y)
  ctx.moveTo(r.x + r.w / 2 - len / 2, r.y + r.h)
  ctx.lineTo(r.x + r.w / 2 + len / 2, r.y + r.h)
  ctx.moveTo(r.x, r.y + r.h / 2 - len / 2)
  ctx.lineTo(r.x, r.y + r.h / 2 + len / 2)
  ctx.moveTo(r.x + r.w, r.y + r.h / 2 - len / 2)
  ctx.lineTo(r.x + r.w, r.y + r.h / 2 + len / 2)
  ctx.stroke()
  ctx.restore()
}

function cropHandlePoints(rect) {
  const cx = rect.x + rect.w / 2
  const cy = rect.y + rect.h / 2
  const right = rect.x + rect.w
  const bottom = rect.y + rect.h
  return [
    { name: 'nw', x: rect.x, y: rect.y },
    { name: 'n', x: cx, y: rect.y },
    { name: 'ne', x: right, y: rect.y },
    { name: 'e', x: right, y: cy },
    { name: 'se', x: right, y: bottom },
    { name: 's', x: cx, y: bottom },
    { name: 'sw', x: rect.x, y: bottom },
    { name: 'w', x: rect.x, y: cy },
  ]
}

function cropHitTarget(point) {
  const rect = cropRect.value
  const canvas = canvasRef.value
  if (!rect || !canvas || !point) return { mode: 'new' }
  const tolerance = Math.max(12, Math.min(canvas.width, canvas.height) * 0.018)
  for (const handle of cropHandlePoints(rect)) {
    if (Math.abs(point.x - handle.x) <= tolerance && Math.abs(point.y - handle.y) <= tolerance) {
      return { mode: 'resize', handle: handle.name }
    }
  }
  if (
    point.x >= rect.x && point.x <= rect.x + rect.w
    && point.y >= rect.y && point.y <= rect.y + rect.h
  ) {
    return { mode: 'move' }
  }
  return { mode: 'new' }
}

function beginCropInteraction(point) {
  if (!cropRect.value) resetCropRect()
  const hit = cropHitTarget(point)
  cropDrag.value = {
    ...hit,
    start: point,
    origin: cropRect.value ? { ...cropRect.value } : { x: point.x, y: point.y, w: 1, h: 1 },
  }
  drawing.value = true
  if (hit.mode === 'new') setCropRect({ x: point.x, y: point.y, w: 1, h: 1 })
}

function updateCropInteraction(point) {
  const drag = cropDrag.value
  if (!drag) return
  const canvas = canvasRef.value
  if (!canvas) return
  const dx = point.x - drag.start.x
  const dy = point.y - drag.start.y
  const r = drag.origin
  if (drag.mode === 'move') {
    let nx = r.x + dx
    let ny = r.y + dy
    nx = Math.max(0, Math.min(nx, canvas.width - r.w))
    ny = Math.max(0, Math.min(ny, canvas.height - r.h))
    setCropRect({ x: nx, y: ny, w: r.w, h: r.h })
    return
  }
  if (drag.mode === 'new') {
    const nx = Math.min(drag.start.x, point.x)
    const ny = Math.min(drag.start.y, point.y)
    const nw = Math.abs(point.x - drag.start.x)
    const nh = Math.abs(point.y - drag.start.y)
    setCropRect({ x: nx, y: ny, w: nw, h: nh })
    return
  }

  let left = r.x
  let right = r.x + r.w
  let top = r.y
  let bottom = r.y + r.h
  const minSize = Math.max(12, Math.min(canvas.width, canvas.height) * 0.025)

  if (drag.handle.includes('w')) left = Math.max(0, Math.min(r.x + dx, right - minSize))
  if (drag.handle.includes('e')) right = Math.max(left + minSize, Math.min(r.x + r.w + dx, canvas.width))
  if (drag.handle.includes('n')) top = Math.max(0, Math.min(r.y + dy, bottom - minSize))
  if (drag.handle.includes('s')) bottom = Math.max(top + minSize, Math.min(r.y + r.h + dy, canvas.height))

  setCropRect({ x: left, y: top, w: right - left, h: bottom - top })
}

function drawShapePreview(a, b) {
  const overlay = overlayRef.value
  const ctx = context2d(overlay)
  if (!overlay || !ctx) return
  clearOverlay()
  if (shapeKind.value === 'rect') drawRect(ctx, a, b)
  if (shapeKind.value === 'arrow') drawArrow(ctx, a, b)
}

function pixelateAt(point) {
  const canvas = canvasRef.value
  const ctx = context2d()
  if (!canvas || !ctx || !point) return
  const size = Math.max(6, Number(mosaicSize.value || 18))
  const x = Math.max(0, Math.round(point.x - size / 2))
  const y = Math.max(0, Math.round(point.y - size / 2))
  const w = Math.min(size, canvas.width - x)
  const h = Math.min(size, canvas.height - y)
  if (w <= 1 || h <= 1) return
  const small = document.createElement('canvas')
  small.width = 1
  small.height = 1
  const sctx = small.getContext('2d')
  sctx.drawImage(canvas, x, y, w, h, 0, 0, 1, 1)
  ctx.imageSmoothingEnabled = false
  ctx.drawImage(small, 0, 0, 1, 1, x, y, w, h)
  ctx.imageSmoothingEnabled = true
}

function onPointerDown(e) {
  if (!canEdit.value) return
  if (textAnchor.value) return
  if (tool.value === 'rotate' || tool.value === 'filter') return
  const p = canvasPoint(e)
  if (!p) return
  e.preventDefault()
  e.currentTarget?.setPointerCapture?.(e.pointerId)
  if (tool.value === 'crop') {
    beginCropInteraction(p)
    return
  }
  if (tool.value === 'text') {
    textAnchor.value = p
    textDraft.value = ''
    nextTick(() => textInputRef.value?.focus?.())
    return
  }
  pushHistory()
  drawing.value = true
  dragStart.value = p
  if (tool.value === 'mosaic') pixelateAt(p)
}

function drawMultilineText(ctx, text, x, y) {
  const fontSize = Math.max(16, Number(brushSize.value || 8) * 4)
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n')
  ctx.fillStyle = color.value
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.92)'
  ctx.lineWidth = Math.max(2, fontSize / 12)
  ctx.font = `700 ${fontSize}px sans-serif`
  ctx.textBaseline = 'top'
  const lineHeight = fontSize * 1.28
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] || ' '
    const yy = y + i * lineHeight
    ctx.strokeText(line, x, yy)
    ctx.fillText(line, x, yy)
  }
}

function commitText() {
  const text = textDraft.value.trim()
  const anchor = textAnchor.value
  if (!text || !anchor) {
    clearPendingText()
    return
  }
  const ctx = context2d()
  if (!ctx) return
  pushHistory()
  drawMultilineText(ctx, text, anchor.x, anchor.y)
  clearPendingText()
}

function onTextKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault()
    commitText()
  }
  if (e.key === 'Escape') {
    e.preventDefault()
    clearPendingText()
  }
}

function onPointerMove(e) {
  const p = canvasPoint(e)
  if (!p) return
  if (tool.value === 'crop' && cropDrag.value) {
    e.preventDefault()
    updateCropInteraction(p)
    return
  }
  if (!drawing.value || !dragStart.value) return
  e.preventDefault()
  if (tool.value === 'brush') {
    drawBrushLine(dragStart.value, p)
    dragStart.value = p
  } else if (tool.value === 'mosaic') {
    pixelateAt(p)
  } else if (tool.value === 'shape') {
    drawShapePreview(dragStart.value, p)
  }
}

function onPointerUp(e) {
  if (tool.value === 'crop' && cropDrag.value) {
    drawing.value = false
    cropDrag.value = null
    return
  }
  if (!drawing.value || !dragStart.value) return
  const p = canvasPoint(e)
  const start = dragStart.value
  drawing.value = false
  dragStart.value = null
  if (!p) return
  if (tool.value === 'shape') {
    const ctx = context2d()
    clearOverlay()
    if (shapeKind.value === 'rect') drawRect(ctx, start, p)
    if (shapeKind.value === 'arrow') drawArrow(ctx, start, p)
  }
}

function undo() {
  const stack = history.value
  if (!stack.length) return
  const last = stack[stack.length - 1]
  history.value = stack.slice(0, -1)
  restoreDataUrl(last)
  cropRect.value = null
  cropDrag.value = null
  clearPendingText()
}

function resetToOriginal() {
  if (!originalDataUrl) return
  pushHistory()
  restoreDataUrl(originalDataUrl)
  cropRect.value = null
  cropDrag.value = null
  clearPendingText()
  brightness.value = 100
  contrast.value = 100
}

function applyCrop() {
  const rect = cropRect.value
  const canvas = canvasRef.value
  const ctx = context2d()
  if (!rect || !canvas || !ctx || rect.w < 4 || rect.h < 4) return
  pushHistory()
  const out = document.createElement('canvas')
  out.width = Math.round(rect.w)
  out.height = Math.round(rect.h)
  out.getContext('2d').drawImage(canvas, rect.x, rect.y, rect.w, rect.h, 0, 0, out.width, out.height)
  canvas.width = out.width
  canvas.height = out.height
  ctx.drawImage(out, 0, 0)
  imageInfo.value = { width: canvas.width, height: canvas.height }
  cropRect.value = null
  cropDrag.value = null
  syncOverlaySize()
  emit('ready', imageInfo.value)
  tool.value = 'brush'
}

function cancelCrop() {
  cropRect.value = null
  cropDrag.value = null
  clearOverlay()
  tool.value = 'brush'
}

function transformCanvas(kind) {
  const canvas = canvasRef.value
  const ctx = context2d()
  if (!canvas || !ctx) return
  pushHistory()
  const src = document.createElement('canvas')
  src.width = canvas.width
  src.height = canvas.height
  src.getContext('2d').drawImage(canvas, 0, 0)
  const rotate = kind === 'rotate-left' || kind === 'rotate-right'
  canvas.width = rotate ? src.height : src.width
  canvas.height = rotate ? src.width : src.height
  if (kind === 'rotate-right') {
    ctx.translate(canvas.width, 0)
    ctx.rotate(Math.PI / 2)
  } else if (kind === 'rotate-left') {
    ctx.translate(0, canvas.height)
    ctx.rotate(-Math.PI / 2)
  } else if (kind === 'flip-x') {
    ctx.translate(canvas.width, 0)
    ctx.scale(-1, 1)
  } else if (kind === 'flip-y') {
    ctx.translate(0, canvas.height)
    ctx.scale(1, -1)
  }
  ctx.drawImage(src, 0, 0)
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  imageInfo.value = { width: canvas.width, height: canvas.height }
  cropRect.value = null
  cropDrag.value = null
  syncOverlaySize()
  emit('ready', imageInfo.value)
}

function applyFilter() {
  const canvas = canvasRef.value
  const ctx = context2d()
  if (!canvas || !ctx) return
  if (brightness.value === 100 && contrast.value === 100) return
  pushHistory()
  const src = document.createElement('canvas')
  src.width = canvas.width
  src.height = canvas.height
  src.getContext('2d').drawImage(canvas, 0, 0)
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.filter = `brightness(${brightness.value}%) contrast(${contrast.value}%)`
  ctx.drawImage(src, 0, 0)
  ctx.filter = 'none'
  brightness.value = 100
  contrast.value = 100
}

function makeResult() {
  if (textAnchor.value) commitText()
  applyFilter()
  const canvas = canvasRef.value
  const dataUrl = canvas.toDataURL('image/png')
  const b64 = dataUrl.split(',', 2)[1] || ''
  const cleanBase = String(props.name || 'image').replace(/[\\/:*?"<>|]+/g, '_').replace(/\.[^.]+$/, '').slice(0, 90) || 'image'
  return {
    dataUrl,
    data_base64: b64,
    mime: 'image/png',
    name: `${cleanBase}-edited.png`,
    size: Math.round((b64.length * 3) / 4),
  }
}

function emitResult(kind = 'apply') {
  try {
    const result = makeResult()
    emit(kind, result)
  } catch (e) {
    emit('error', e?.message || e || t('imageEditor.exportFailed'))
  }
}

function onKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault()
    undo()
  }
  const tag = (e.target?.tagName || '').toLowerCase()
  if (tag === 'input' || tag === 'textarea') return
  const map = { b: 'brush', t: 'text', s: 'shape', m: 'mosaic', c: 'crop', r: 'rotate', f: 'filter' }
  if (!e.ctrlKey && !e.metaKey && !e.altKey && map[e.key.toLowerCase()]) {
    tool.value = map[e.key.toLowerCase()]
  }
}

watch(() => props.source, () => loadSource(), { immediate: true })

watch(tool, nextTool => {
  cropDrag.value = null
  drawing.value = false
  clearPendingText()
  if (nextTool === 'crop') {
    if (!cropRect.value) resetCropRect()
    else drawCropOverlay(cropRect.value)
  } else {
    clearOverlay()
  }
})

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', syncOverlaySize)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', syncOverlaySize)
})
</script>

<template>
  <section class="ice">
    <main class="ice-body">
      <div
        class="ice-stage"
        :class="[`tool-${tool}`]"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
        @pointerleave="onPointerUp"
      >
        <canvas ref="canvasRef" class="ice-canvas-base"></canvas>
        <canvas ref="overlayRef" class="ice-canvas-overlay"></canvas>
        <div
          v-if="textAnchor"
          class="ice-textbox"
          :style="textBoxStyle"
          @pointerdown.stop
          @pointermove.stop
          @pointerup.stop
        >
          <textarea
            ref="textInputRef"
            v-model="textDraft"
            :placeholder="t('imageEditor.textPlaceholder')"
            rows="3"
            @keydown="onTextKeydown"
          ></textarea>
          <div class="ice-textbox-actions">
            <button class="ghost tiny" @click="clearPendingText">{{ t('imageEditor.cancel') }}</button>
            <button class="primary tiny" @click="commitText">{{ t('imageEditor.confirm') }}</button>
          </div>
        </div>
      </div>
    </main>

    <footer class="ice-toolbar">
      <div class="ice-tools" role="toolbar" :aria-label="t('imageEditor.toolsAria')">
        <button
          v-for="toolItem in editorTools"
          :key="toolItem.id"
          type="button"
          class="ice-tool-btn"
          :class="{ active: tool === toolItem.id }"
          :title="toolItem.label"
          :aria-label="toolItem.label"
          :aria-pressed="tool === toolItem.id"
          @click="tool = toolItem.id"
        >
          <svg v-if="toolItem.id === 'brush'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4.5 19 9l-9.5 9.5-4.5.5.5-4.5z"/><path d="m13 6 5 5"/></svg>
          <svg v-else-if="toolItem.id === 'text'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 6V4h14v2"/><path d="M12 4v16"/><path d="M9 20h6"/></svg>
          <svg v-else-if="toolItem.id === 'shape'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="1.5"/></svg>
          <svg v-else-if="toolItem.id === 'mosaic'" viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><rect x="4" y="4" width="5" height="5"/><rect x="11" y="4" width="5" height="5" opacity="0.55"/><rect x="11" y="11" width="5" height="5"/><rect x="4" y="11" width="5" height="5" opacity="0.55"/><rect x="4" y="18" width="5" height="2.5" opacity="0.7"/><rect x="11" y="18" width="5" height="2.5" opacity="0.4"/></svg>
          <svg v-else-if="toolItem.id === 'crop'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M2 6h14a2 2 0 0 1 2 2v14"/></svg>
          <svg v-else-if="toolItem.id === 'rotate'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v6h6"/></svg>
          <svg v-else-if="toolItem.id === 'filter'" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>
        </button>
      </div>

      <span class="ice-sep" aria-hidden="true"></span>

      <div class="ice-params">
        <template v-if="tool === 'brush'">
          <label><input v-model="color" type="color" /><span>{{ t('imageEditor.color') }}</span></label>
          <label><input v-model.number="brushSize" type="range" min="2" max="36" /><span>{{ t('imageEditor.thickness', { n: brushSize }) }}</span></label>
        </template>
        <template v-else-if="tool === 'text'">
          <label><input v-model="color" type="color" /><span>{{ t('imageEditor.color') }}</span></label>
          <label><input v-model.number="brushSize" type="range" min="2" max="36" /><span>{{ t('imageEditor.fontSize') }}</span></label>
        </template>
        <template v-else-if="tool === 'shape'">
          <div class="ice-segment" role="radiogroup" :aria-label="t('imageEditor.shape')">
            <button type="button" :class="{ active: shapeKind === 'rect' }" @click="shapeKind = 'rect'">{{ t('imageEditor.rect') }}</button>
            <button type="button" :class="{ active: shapeKind === 'arrow' }" @click="shapeKind = 'arrow'">{{ t('imageEditor.arrow') }}</button>
          </div>
          <label><input v-model="color" type="color" /><span>{{ t('imageEditor.color') }}</span></label>
          <label><input v-model.number="brushSize" type="range" min="2" max="36" /><span>{{ t('imageEditor.thickness', { n: brushSize }) }}</span></label>
        </template>
        <template v-else-if="tool === 'mosaic'">
          <label><input v-model.number="mosaicSize" type="range" min="8" max="64" /><span>{{ t('imageEditor.grain', { n: mosaicSize }) }}</span></label>
        </template>
        <template v-else-if="tool === 'crop'">
          <button class="ghost tiny" type="button" @click="resetCropRect">{{ t('imageEditor.resetCrop') }}</button>
          <span v-if="cropRect" class="ice-hint">{{ Math.round(cropRect.w) }} × {{ Math.round(cropRect.h) }}</span>
        </template>
        <template v-else-if="tool === 'rotate'">
          <button class="ghost tiny" type="button" @click="transformCanvas('rotate-left')">{{ t('imageEditor.rotateLeft') }}</button>
          <button class="ghost tiny" type="button" @click="transformCanvas('rotate-right')">{{ t('imageEditor.rotateRight') }}</button>
          <span class="ice-sep" aria-hidden="true"></span>
          <button class="ghost tiny" type="button" @click="transformCanvas('flip-x')">{{ t('imageEditor.flipH') }}</button>
          <button class="ghost tiny" type="button" @click="transformCanvas('flip-y')">{{ t('imageEditor.flipV') }}</button>
        </template>
        <template v-else-if="tool === 'filter'">
          <label><input v-model.number="brightness" type="range" min="40" max="180" /><span>{{ t('imageEditor.brightness', { n: brightness }) }}</span></label>
          <label><input v-model.number="contrast" type="range" min="40" max="180" /><span>{{ t('imageEditor.contrast', { n: contrast }) }}</span></label>
          <button class="ghost tiny" type="button" :disabled="brightness === 100 && contrast === 100" @click="applyFilter">{{ t('imageEditor.applyFilter') }}</button>
        </template>
      </div>

      <div class="ice-actions">
        <template v-if="tool === 'crop'">
          <button class="ghost tiny" type="button" @click="cancelCrop">{{ t('imageEditor.cancelCrop') }}</button>
          <button class="primary tiny" type="button" :disabled="!cropRect" @click="applyCrop">{{ t('imageEditor.applyCrop') }}</button>
        </template>
        <template v-else>
          <button class="ghost tiny" type="button" :disabled="!canUndo" title="Ctrl+Z" @click="undo">{{ t('imageEditor.undo') }}</button>
          <button class="ghost tiny" type="button" @click="resetToOriginal">{{ t('imageEditor.reset') }}</button>
          <span class="ice-sep" aria-hidden="true"></span>
          <button class="ghost tiny" type="button" @click="emit('cancel')">{{ cancelText }}</button>
          <button v-if="allowDuplicate" class="ghost tiny" type="button" @click="emitResult('duplicate')">{{ duplicateText }}</button>
          <button class="primary tiny" type="button" @click="emitResult('apply')">{{ applyText }}</button>
        </template>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.ice {
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.ice-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: auto;
  padding: 18px;
  background:
    linear-gradient(45deg, rgba(127, 127, 127, 0.08) 25%, transparent 25%),
    linear-gradient(-45deg, rgba(127, 127, 127, 0.08) 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, rgba(127, 127, 127, 0.08) 75%),
    linear-gradient(-45deg, transparent 75%, rgba(127, 127, 127, 0.08) 75%);
  background-size: 22px 22px;
  background-position: 0 0, 0 11px, 11px -11px, -11px 0;
}
.ice-stage {
  position: relative;
  display: inline-grid;
  grid-template-columns: auto;
  grid-template-rows: auto;
  max-width: 100%;
  max-height: 100%;
  line-height: 0;
  touch-action: none;
  cursor: crosshair;
}
.ice-stage.tool-crop {
  cursor: move;
}
.ice-stage.tool-text {
  cursor: text;
}
.ice-stage.tool-rotate,
.ice-stage.tool-filter {
  cursor: default;
}
.ice-canvas-base,
.ice-canvas-overlay {
  grid-column: 1;
  grid-row: 1;
  display: block;
  max-width: calc(100vw - 72px);
  max-height: calc(100vh - 198px);
  object-fit: contain;
  user-select: none;
}
.ice-canvas-overlay {
  pointer-events: none;
}

.ice-textbox {
  position: absolute;
  z-index: 3;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid color-mix(in srgb, var(--accent, #4e8cff) 72%, #fff 18%);
  border-radius: 10px;
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  box-shadow: var(--shadow-md);
}
.ice-textbox textarea {
  width: 100%;
  min-height: 72px;
  resize: both;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  outline: none;
  color: currentColor;
  background: rgba(255, 255, 255, 0.92);
  font: inherit;
  font-weight: 700;
  line-height: 1.28;
  text-shadow: 0 1px 2px rgba(255,255,255,0.95);
}
.ice-textbox-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.ice-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  min-height: 52px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel-2) 80%, transparent);
}

.ice-tools {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  padding: 2px;
  background: color-mix(in srgb, var(--panel) 60%, transparent);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.ice-tool-btn {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border-radius: 7px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease;
}
.ice-tool-btn:hover {
  background: color-mix(in srgb, var(--accent, #4e8cff) 10%, transparent);
  color: var(--fg);
}
.ice-tool-btn.active {
  background: color-mix(in srgb, var(--accent, #4e8cff) 22%, transparent);
  color: var(--accent, #4e8cff);
}

.ice-sep {
  display: inline-block;
  width: 1px;
  height: 22px;
  background: var(--border);
  flex: 0 0 auto;
}

.ice-params {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  overflow-x: auto;
  padding-right: 4px;
}
.ice-params label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.ice-params label span {
  font-variant-numeric: tabular-nums;
}
.ice-params input[type="range"] {
  width: 110px;
}
.ice-params input[type="color"] {
  width: 26px;
  height: 22px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
}
.ice-hint {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.ice-segment {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
.ice-segment button {
  padding: 4px 10px;
  font-size: 12px;
  background: transparent;
  border: 0;
  color: var(--muted);
  cursor: pointer;
}
.ice-segment button.active {
  background: color-mix(in srgb, var(--accent, #4e8cff) 18%, transparent);
  color: var(--accent, #4e8cff);
}

.ice-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}
.tiny {
  padding: 5px 10px;
  font-size: 12px;
}
</style>
