<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

const tooltipEl = ref(null)
const visible = ref(false)
const text = ref('')
const placement = ref('top')
const position = ref({ left: '-9999px', top: '-9999px' })
const tooltipId = `workbench-tooltip-${Math.random().toString(36).slice(2)}`

let activeTrigger = null
let previousDescribedBy = null
let showTimer = null
let lastPointerDownAt = 0

function triggerFrom(node) {
  return node instanceof Element
    ? node.closest('[title], [data-workbench-tooltip]')
    : null
}

function claimTooltipText(trigger) {
  const title = trigger.getAttribute('title')
  if (title != null) {
    const normalized = title.trim()
    if (normalized) trigger.dataset.workbenchTooltip = normalized
    trigger.removeAttribute('title')
  }
  return String(trigger.dataset.workbenchTooltip || '').trim()
}

function clearShowTimer() {
  if (!showTimer) return
  window.clearTimeout(showTimer)
  showTimer = null
}

function restoreDescription() {
  if (!activeTrigger) return
  if (previousDescribedBy == null) activeTrigger.removeAttribute('aria-describedby')
  else activeTrigger.setAttribute('aria-describedby', previousDescribedBy)
  previousDescribedBy = null
}

function hideTooltip() {
  clearShowTimer()
  restoreDescription()
  activeTrigger = null
  visible.value = false
}

function updatePosition() {
  if (!activeTrigger || !tooltipEl.value || !document.contains(activeTrigger)) {
    hideTooltip()
    return
  }
  const triggerRect = activeTrigger.getBoundingClientRect()
  const tooltipRect = tooltipEl.value.getBoundingClientRect()
  const gap = 9
  const viewportPadding = 8
  const fitsAbove = triggerRect.top >= tooltipRect.height + gap + viewportPadding
  placement.value = fitsAbove ? 'top' : 'bottom'
  const idealLeft = triggerRect.left + (triggerRect.width - tooltipRect.width) / 2
  const left = Math.min(
    window.innerWidth - tooltipRect.width - viewportPadding,
    Math.max(viewportPadding, idealLeft),
  )
  const top = fitsAbove
    ? triggerRect.top - tooltipRect.height - gap
    : triggerRect.bottom + gap
  position.value = {
    left: `${Math.round(left)}px`,
    top: `${Math.round(Math.max(viewportPadding, top))}px`,
  }
}

function showTooltip(trigger, { immediate = false } = {}) {
  const content = claimTooltipText(trigger)
  if (!content) return
  if (activeTrigger !== trigger) {
    restoreDescription()
    activeTrigger = trigger
    previousDescribedBy = trigger.getAttribute('aria-describedby')
    trigger.setAttribute(
      'aria-describedby',
      [previousDescribedBy, tooltipId].filter(Boolean).join(' '),
    )
  }
  text.value = content
  clearShowTimer()
  showTimer = window.setTimeout(async () => {
    showTimer = null
    if (activeTrigger !== trigger) return
    visible.value = true
    await nextTick()
    updatePosition()
  }, immediate ? 0 : 280)
}

function onPointerOver(event) {
  const trigger = triggerFrom(event.target)
  if (!trigger || trigger === activeTrigger) return
  showTooltip(trigger)
}

function onPointerOut(event) {
  if (!activeTrigger) return
  const next = event.relatedTarget
  if (next instanceof Node && activeTrigger.contains(next)) return
  if (triggerFrom(next) === activeTrigger) return
  hideTooltip()
}

function onFocusIn(event) {
  if (performance.now() - lastPointerDownAt < 500) return
  const trigger = triggerFrom(event.target)
  if (trigger) showTooltip(trigger, { immediate: true })
}

function onFocusOut(event) {
  if (!activeTrigger) return
  const next = event.relatedTarget
  if (next instanceof Node && activeTrigger.contains(next)) return
  hideTooltip()
}

function onPointerDown() {
  lastPointerDownAt = performance.now()
  hideTooltip()
}

onMounted(() => {
  document.addEventListener('pointerover', onPointerOver, true)
  document.addEventListener('pointerout', onPointerOut, true)
  document.addEventListener('pointerdown', onPointerDown, true)
  document.addEventListener('focusin', onFocusIn, true)
  document.addEventListener('focusout', onFocusOut, true)
  window.addEventListener('resize', updatePosition)
  window.addEventListener('scroll', updatePosition, true)
})

onUnmounted(() => {
  document.removeEventListener('pointerover', onPointerOver, true)
  document.removeEventListener('pointerout', onPointerOut, true)
  document.removeEventListener('pointerdown', onPointerDown, true)
  document.removeEventListener('focusin', onFocusIn, true)
  document.removeEventListener('focusout', onFocusOut, true)
  window.removeEventListener('resize', updatePosition)
  window.removeEventListener('scroll', updatePosition, true)
  hideTooltip()
})
</script>

<template>
  <Teleport to="body">
    <Transition name="workbench-tooltip">
      <div
        v-if="visible"
        :id="tooltipId"
        ref="tooltipEl"
        class="workbench-tooltip"
        :class="`is-${placement}`"
        :style="position"
        role="tooltip"
      >
        {{ text }}
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.workbench-tooltip {
  position: fixed;
  z-index: 12000;
  width: max-content;
  max-width: min(320px, calc(100vw - 16px));
  padding: 6px 9px;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  background: var(--panel-2);
  color: var(--fg-strong);
  font: 500 11px/1.45 var(--ui);
  letter-spacing: .01em;
  overflow-wrap: anywhere;
  pointer-events: none;
  white-space: pre-line;
}

:global(:root[data-theme="light"]) .workbench-tooltip {
  box-shadow: var(--shadow-sm);
}

.workbench-tooltip-enter-active,
.workbench-tooltip-leave-active {
  transition: opacity 100ms ease, transform 100ms ease;
}

.workbench-tooltip-enter-from,
.workbench-tooltip-leave-to {
  opacity: 0;
}

.workbench-tooltip-enter-from.is-top,
.workbench-tooltip-leave-to.is-top {
  transform: translateY(2px);
}

.workbench-tooltip-enter-from.is-bottom,
.workbench-tooltip-leave-to.is-bottom {
  transform: translateY(-2px);
}

@media (prefers-reduced-motion: reduce) {
  .workbench-tooltip-enter-active,
  .workbench-tooltip-leave-active {
    transition: none;
  }
}
</style>
