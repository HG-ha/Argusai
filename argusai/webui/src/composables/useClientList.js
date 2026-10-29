import { computed, ref, unref, watch } from 'vue'

/**
 * Client-side filter + pagination + multi-select for design-gallery list chrome.
 *
 * @param {import('vue').Ref|import('vue').ComputedRef|Array} source
 * @param {(item: any, q: string, filters: Record<string, any>) => boolean} [matchFn]
 */
export function useClientList(source, matchFn) {
  const query = ref('')
  const filters = ref({})
  const page = ref(1)
  const pageSize = ref(20)
  const selected = ref(new Set())

  const items = computed(() => {
    const raw = unref(source)
    return Array.isArray(raw) ? raw : []
  })

  const filtered = computed(() => {
    const q = query.value.trim().toLowerCase()
    const f = filters.value || {}
    const list = items.value
    if (!matchFn && !q && !Object.values(f).some((v) => v !== '' && v != null && v !== false)) {
      return list
    }
    return list.filter((item) => {
      if (typeof matchFn === 'function') return matchFn(item, q, f)
      if (!q) return true
      return JSON.stringify(item).toLowerCase().includes(q)
    })
  })

  const total = computed(() => filtered.value.length)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value) || 1))

  const pageItems = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return filtered.value.slice(start, start + pageSize.value)
  })

  watch([filtered, pageSize], () => {
    if (page.value > pageCount.value) page.value = pageCount.value
    if (page.value < 1) page.value = 1
  })

  watch(query, () => { page.value = 1 })
  watch(filters, () => { page.value = 1 }, { deep: true })

  function isSelected(id) {
    return selected.value.has(id)
  }

  function toggle(id, on) {
    const next = new Set(selected.value)
    const want = on == null ? !next.has(id) : !!on
    if (want) next.add(id)
    else next.delete(id)
    selected.value = next
  }

  function clearSelection() {
    selected.value = new Set()
  }

  function selectPage(on = true) {
    const next = new Set(selected.value)
    for (const item of pageItems.value) {
      const id = item?.id
      if (id == null) continue
      if (on) next.add(id)
      else next.delete(id)
    }
    selected.value = next
  }

  function selectAllFiltered(on = true) {
    const next = on ? new Set() : new Set()
    if (on) {
      for (const item of filtered.value) {
        if (item?.id != null) next.add(item.id)
      }
    }
    selected.value = next
  }

  const selectedCount = computed(() => selected.value.size)
  const selectedIds = computed(() => [...selected.value])
  const pageAllSelected = computed(() => {
    const ids = pageItems.value.map((i) => i?.id).filter((id) => id != null)
    return ids.length > 0 && ids.every((id) => selected.value.has(id))
  })

  return {
    query,
    filters,
    page,
    pageSize,
    selected,
    items,
    filtered,
    total,
    pageCount,
    pageItems,
    selectedCount,
    selectedIds,
    pageAllSelected,
    isSelected,
    toggle,
    clearSelection,
    selectPage,
    selectAllFiltered,
  }
}
