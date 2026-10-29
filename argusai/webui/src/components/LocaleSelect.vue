<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { t, i18n, LOCALES, currentLocaleMeta } from '../i18n.js'
import { applyLocale } from '../composables/useLocale.js'

const props = defineProps({
  /** titlebar: compact chrome control. sidebar: full-width rail control. */
  variant: { type: String, default: 'sidebar' },
  collapsed: { type: Boolean, default: false },
})

const open = ref(false)
const root = ref(null)
const menuEl = ref(null)
const menuStyle = ref({})

const current = computed(() => {
  void i18n.locale
  return currentLocaleMeta()
})

const triggerLabel = computed(() => current.value?.native || 'English')

function pick(id) {
  applyLocale(id)
  open.value = false
}

function placeMenu() {
  const trigger = root.value?.querySelector?.('.locale-dd-trigger')
  if (!trigger) return
  const r = trigger.getBoundingClientRect()
  const width = Math.min(260, Math.max(r.width, props.variant === 'titlebar' ? 220 : 168))
  const menuH = Math.min(360, LOCALES.length * 46 + 16)
  const spaceBelow = window.innerHeight - r.bottom - 8
  const spaceAbove = r.top - 8
  const up = props.variant === 'sidebar'
    ? spaceAbove >= 120 || spaceAbove > spaceBelow
    : spaceBelow < menuH && spaceAbove > spaceBelow
  let left = props.variant === 'titlebar'
    ? r.right - width
    : r.left
  left = Math.min(Math.max(8, left), window.innerWidth - width - 8)
  menuStyle.value = {
    position: 'fixed',
    left: `${left}px`,
    width: `${width}px`,
    zIndex: 11000,
    maxHeight: `${Math.min(360, Math.max(spaceBelow, spaceAbove, 160))}px`,
    overflowY: 'auto',
    ...(up
      ? { bottom: `${window.innerHeight - r.top + 6}px`, top: 'auto' }
      : { top: `${r.bottom + 6}px`, bottom: 'auto' }),
  }
}

async function toggle() {
  open.value = !open.value
  if (open.value) {
    await nextTick()
    placeMenu()
  }
}

function onDoc(e) {
  if (!open.value) return
  const target = e.target
  if (root.value?.contains(target) || menuEl.value?.contains(target)) return
  open.value = false
}

function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

function onReposition() {
  if (open.value) placeMenu()
}

watch(open, (v) => {
  if (v) nextTick(placeMenu)
})

onMounted(() => {
  document.addEventListener('pointerdown', onDoc, true)
  document.addEventListener('keydown', onKey, true)
  window.addEventListener('resize', onReposition)
  window.addEventListener('scroll', onReposition, true)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', onDoc, true)
  document.removeEventListener('keydown', onKey, true)
  window.removeEventListener('resize', onReposition)
  window.removeEventListener('scroll', onReposition, true)
})
</script>

<template>
  <div
    ref="root"
    class="locale-dd"
    :class="[`variant-${variant}`, { open, collapsed }]"
  >
    <button
      type="button"
      class="locale-dd-trigger"
      :class="{ 'titlebar-btn': variant === 'titlebar' }"
      :title="t('lang.choose')"
      :aria-label="t('lang.choose')"
      :aria-expanded="open"
      aria-haspopup="listbox"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <circle cx="12" cy="12" r="9"/>
        <path d="M3 12h18M12 3c2.5 3 2.5 15 0 18M12 3c-2.5 3-2.5 15 0 18"/>
      </svg>
      <span class="locale-dd-label">{{ triggerLabel }}</span>
      <span class="locale-dd-caret" aria-hidden="true">▾</span>
    </button>
    <Teleport to="body">
      <div
        v-if="open"
        ref="menuEl"
        class="locale-dd-menu"
        role="listbox"
        :aria-label="t('lang.choose')"
        :style="menuStyle"
      >
        <button
          v-for="opt in LOCALES"
          :key="opt.id"
          type="button"
          class="locale-dd-opt"
          :class="{ on: opt.id === i18n.locale }"
          role="option"
          :aria-selected="opt.id === i18n.locale"
          @click="pick(opt.id)"
        >
          <span class="locale-dd-opt-copy">
            <strong>{{ opt.native }}</strong>
            <small v-if="opt.english !== opt.native">{{ opt.english }}</small>
          </span>
        </button>
      </div>
    </Teleport>
  </div>
</template>
