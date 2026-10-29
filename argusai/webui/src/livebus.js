// Backend -> frontend live push receiver.
//
// Python (argusai/gui/live_push.py) calls
//   window.__argusPush({ channel, payload })
// via pywebview's evaluate_js whenever some backend state changes. We fan that
// out to per-channel subscribers so panels can refresh on demand instead of
// polling. Panels keep a slow fallback poll, so a dropped push never leaves a
// panel permanently stale.

import { reactive } from 'vue'

const listeners = new Map() // channel -> Set<fn>

// Reactive "last push timestamp" per channel. Components can also just `watch`
// liveBus.ts[channel] if they prefer reactivity over an explicit callback.
export const liveBus = reactive({ ts: {} })

export function onLiveChannel(channel, fn) {
  if (!channel || typeof fn !== 'function') return () => {}
  if (!listeners.has(channel)) listeners.set(channel, new Set())
  listeners.get(channel).add(fn)
  return () => {
    const set = listeners.get(channel)
    if (set) set.delete(fn)
  }
}

function dispatch(evt) {
  const channel = evt && evt.channel
  if (!channel) return
  liveBus.ts[channel] = Date.now()
  const payload = (evt && evt.payload) || {}
  const set = listeners.get(channel)
  if (set) {
    for (const fn of Array.from(set)) {
      try { fn(payload) } catch (e) { /* a bad subscriber must not kill the bus */ }
    }
  }
  const wildcard = listeners.get('*')
  if (wildcard) {
    for (const fn of Array.from(wildcard)) {
      try { fn({ channel, payload }) } catch (e) { /* ignore */ }
    }
  }
}

// Install the global receiver once. Idempotent.
if (typeof window !== 'undefined' && !window.__argusPush) {
  window.__argusPush = (evt) => {
    try { dispatch(typeof evt === 'string' ? JSON.parse(evt) : evt) } catch (e) { /* ignore malformed */ }
  }
}
