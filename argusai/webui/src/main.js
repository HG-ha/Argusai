import { createApp } from 'vue'
import { darkTheme, lightTheme, NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import App from './components/App.vue'
import { api } from './api.js'
import { i18n, setLocale } from './i18n.js'
import { state } from './state.js'
import { theme } from './theme.js'
import './theme.js'
import './assets/theme.css'
// Side-effect import: installs window.__argusPush so backend live pushes are
// received even before any panel subscribes.
import './livebus.js'

function applyAppInfo(info) {
  if (!info || typeof info !== 'object') return
  state.version = info.version || state.version || ''
  state.platform = info.platform || state.platform || ''
  state.framelessWindow = !!info.frameless_window
  // custom_titlebar is newer; fall back to frameless so older hosts behave.
  state.customTitlebar = info.custom_titlebar ?? !!info.frameless_window
  state.nativeWindowControls = !!info.native_window_controls
  state.titlebarInsetLeft = Number(info.titlebar_inset_left) || 0
}

// Packaged macOS can finish resolving the native NSWindow just after the
// bridge becomes available. Register before the first appInfo call so neither
// side of that startup race can lose the final titlebar state.
window.addEventListener('argusai:app-info-updated', event => {
  applyAppInfo(event?.detail)
})

async function boot() {
  try {
    const info = await api.appInfo()
    let stored = ''
    try { stored = localStorage.getItem('argusai.locale') || '' } catch (_) {}
    // Browser/catalog detection already ran in i18n.js. The Python host
    // currently only distinguishes zh vs en, so it must not clobber ja/ko/….
    if (!stored && info?.locale === 'zh' && i18n.locale === 'en') setLocale('zh')
    api.setLocale(i18n.locale).catch(() => {})
    applyAppInfo(info)
  } catch (_) {}

  const app = createApp(App)

  // Persisted last-error breadcrumb so a renderer crash isn't silent in
  // pywebview/WebView2 where DevTools isn't always reachable.
  const remember = (where, payload) => {
    try {
      localStorage.setItem('argusai.lastError', JSON.stringify({
        ts: Date.now(), where, ...payload,
      }))
    } catch (_) { /* ignore */ }
  }
  app.config.errorHandler = (err, instance, info) => {
    const name = instance?.$options?.name || instance?.type?.name || 'unknown'
    const msg = err?.message || String(err)
    console.error('[argusai] vue error', { name, info, err })
    remember('vue', { name, info, msg, stack: err?.stack || '' })
  }
  app.config.warnHandler = (msg, _instance, trace) => {
    console.warn('[argusai] vue warn', msg, trace)
  }
  window.addEventListener('error', (e) => {
    console.error('[argusai] window error', e?.error || e?.message)
    remember('window', { msg: String(e?.message || e), stack: e?.error?.stack || '' })
  })
  window.addEventListener('unhandledrejection', (e) => {
    console.error('[argusai] unhandled promise', e?.reason)
    remember('promise', { msg: String(e?.reason?.message || e?.reason), stack: e?.reason?.stack || '' })
  })

  // Wrap with Naive UI config provider for theme sync, plus dialog/message
  // providers so any descendant can call useDialog() / useMessage() and get
  // themed modals instead of the WebView's native window.confirm / alert.
  app.component('NConfigProvider', NConfigProvider)
  app.component('NDialogProvider', NDialogProvider)
  app.component('NMessageProvider', NMessageProvider)
  app.config.globalProperties.$naiveTheme = theme

  app.mount('#app')
}

boot()
