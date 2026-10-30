import { computed, reactive } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'

const AUTO_CHECK_KEY = 'argusai.updates.autoCheck'
function readAutoCheck() {
  try { return localStorage.getItem(AUTO_CHECK_KEY) !== 'false' } catch (_) { return true }
}

// One shared state and one poller for the About page and the application notice.
export const updateState = reactive({
  status: 'idle', current_version: '', version: '', notes: '', release_url: '',
  platform: '', downloaded_bytes: 0, total_bytes: 0, error: '', error_code: '',
  install_mode: 'source', autoCheck: readAutoCheck(), dismissedVersion: '',
})
let monitorActive = false
let startupTimer = null
let pollTimer = null
let pendingAction = null

function mergeStatus(result) {
  if (result && typeof result === 'object') Object.assign(updateState, result)
  schedulePoll()
  return result
}

function schedulePoll() {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = null
  if (!monitorActive || !['checking', 'downloading', 'installing'].includes(updateState.status)) return
  pollTimer = window.setTimeout(() => {
    pollTimer = null
    refreshUpdateStatus().catch(() => {})
  }, 700)
}

async function runAction(action, fallbackKey) {
  if (pendingAction) return pendingAction
  updateState.error = ''
  updateState.error_code = ''
  pendingAction = (async () => {
    try { return mergeStatus(await action()) } catch (error) {
      updateState.error = error?.message || t(fallbackKey)
      if (updateState.status !== 'downloaded') updateState.status = 'error'
      return { error: updateState.error }
    } finally {
      pendingAction = null
      schedulePoll()
    }
  })()
  return pendingAction
}

export async function refreshUpdateStatus() {
  try { return mergeStatus(await api.updateStatus()) } catch (error) {
    // A background bridge failure should not replace an available update or
    // interrupt the user's task. The About page still permits a manual retry.
    if (['checking', 'downloading'].includes(updateState.status)) {
      updateState.error = error?.message || t('updates.statusUnavailable')
      updateState.status = 'error'
      if (pollTimer) window.clearTimeout(pollTimer)
      pollTimer = null
    }
    // The host may disappear during a successful restart. Keep this quiet;
    // if it stays open, the poller can still receive an installation rollback.
    if (updateState.status === 'installing') schedulePoll()
    return null
  }
}

export function checkForUpdates(force = true) {
  return runAction(() => api.checkForUpdates(force), 'updates.checkFailed')
}
export function downloadUpdate() {
  return runAction(() => api.downloadUpdate(), 'updates.downloadFailed')
}
export function cancelUpdateDownload() {
  return runAction(() => api.cancelUpdateDownload(), 'updates.cancelFailed')
}
export function installUpdate() {
  return runAction(() => api.installUpdate(), 'updates.installFailed')
}
export function setAutoCheck(enabled) {
  updateState.autoCheck = !!enabled
  try { localStorage.setItem(AUTO_CHECK_KEY, String(!!enabled)) } catch (_) {}
}
export function dismissUpdateNotice() {
  updateState.dismissedVersion = updateState.version
}

export function startUpdateMonitor() {
  if (monitorActive) return
  monitorActive = true
  startupTimer = window.setTimeout(async () => {
    startupTimer = null
    await refreshUpdateStatus()
    if (!monitorActive) return
    if (updateState.autoCheck && updateState.install_mode !== 'source'
        && !['downloading', 'downloaded', 'installing'].includes(updateState.status)) {
      await checkForUpdates(false)
    }
    schedulePoll()
  }, 3500)
}
export function stopUpdateMonitor() {
  monitorActive = false
  if (startupTimer) window.clearTimeout(startupTimer)
  if (pollTimer) window.clearTimeout(pollTimer)
  startupTimer = null
  pollTimer = null
}

export function useUpdates() {
  const progress = computed(() => {
    const total = Number(updateState.total_bytes) || 0
    return total > 0 ? Math.min(100, Math.max(0, (Number(updateState.downloaded_bytes) || 0) / total * 100)) : 0
  })
  const noticeVisible = computed(() =>
    ['available', 'downloaded'].includes(updateState.status)
      && updateState.install_mode !== 'source'
      && updateState.version && updateState.dismissedVersion !== updateState.version,
  )
  return { updateState, progress, noticeVisible, checkForUpdates, downloadUpdate, cancelUpdateDownload,
    installUpdate, refreshUpdateStatus, setAutoCheck, dismissUpdateNotice }
}
