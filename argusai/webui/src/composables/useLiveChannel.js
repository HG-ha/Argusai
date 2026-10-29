// Subscribe a component to a backend live-push channel (see livebus.js).
//
// On each push for `channel`, `handler(payload)` runs. Pass `sessionId`
// (a string, ref, or getter) to ignore pushes tagged for a different session.
// Pass `debounceMs` to coalesce bursts (e.g. rapid agent status changes).

import { onMounted, onUnmounted } from 'vue'
import { onLiveChannel } from '../livebus.js'

function resolve(value) {
  if (typeof value === 'function') return value()
  if (value && typeof value === 'object' && 'value' in value) return value.value
  return value
}

export function useLiveChannel(channel, handler, options = {}) {
  let off = null
  let timer = null
  const debounceMs = Number(options.debounceMs) || 0

  function fire(payload) {
    const want = resolve(options.sessionId)
    if (want && payload && payload.session_id && String(payload.session_id) !== String(want)) return
    if (debounceMs > 0) {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = null; handler(payload || {}) }, debounceMs)
    } else {
      handler(payload || {})
    }
  }

  onMounted(() => { off = onLiveChannel(channel, fire) })
  onUnmounted(() => {
    if (off) off()
    if (timer) clearTimeout(timer)
  })
}
