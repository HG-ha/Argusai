import { ref } from 'vue'

const STORAGE_KEY = 'argusai.sidebar.collapsed.v1'
const NARROW_QUERY = '(max-width: 1100px)'

function isNarrow() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.(NARROW_QUERY).matches)
}

function readStored() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch (_) {}
  return null
}

function persist(value) {
  try {
    localStorage.setItem(STORAGE_KEY, value ? '1' : '0')
  } catch (_) {}
}

const stored = readStored()
const userPinned = ref(stored !== null)
const sidebarCollapsed = ref(stored !== null ? stored : isNarrow())

if (typeof window !== 'undefined' && window.matchMedia) {
  const mq = window.matchMedia(NARROW_QUERY)
  const onChange = (event) => {
    if (!userPinned.value) sidebarCollapsed.value = event.matches
  }
  if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange)
  else mq.addListener(onChange)
}

export function useSidebarCollapse() {
  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
    userPinned.value = true
    persist(sidebarCollapsed.value)
  }

  return { sidebarCollapsed, toggleSidebar }
}
