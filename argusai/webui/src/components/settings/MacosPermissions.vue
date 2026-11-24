<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'

const rows = ref([])
const loading = ref(false)
const error = ref('')
const pending = ref('')
let pollTimer = null
let disposed = false
let requestGeneration = 0

async function refresh() {
  if (loading.value || disposed) return
  loading.value = true
  error.value = ''
  try {
    const result = await api.getMacosPermissions()
    if (disposed) return
    if (!result?.ok) throw new Error()
    rows.value = result.permissions || []
    if (pending.value && rows.value.find(row => row.id === pending.value)?.status !== 'not_determined') {
      stopPolling()
    }
  } catch (_) {
    if (!disposed) error.value = t('macosPermissions.loadFailed')
  } finally { loading.value = false }
}

function stopPolling() {
  clearTimeout(pollTimer)
  pollTimer = null
  pending.value = ''
}

async function request(row) {
  if (pending.value) return
  const generation = ++requestGeneration
  pending.value = row.id
  try {
    const result = await api.requestMacosPermission(row.id)
    if (disposed || generation !== requestGeneration) return
    if (!result?.ok) {
      const key = result?.error_code === 'macos_usage_description_missing' ? 'needsBuild' : 'requestFailed'
      showToast(t(`macosPermissions.${key}`), 'error')
      stopPolling()
      await refresh()
      return
    }
    await refresh()
    if (!result.pending) stopPolling()
    let attempts = 0
    const poll = async () => {
      if (disposed || !pending.value || generation !== requestGeneration) return
      await refresh()
      if (pending.value && ++attempts < 120) pollTimer = setTimeout(poll, 1000)
      else stopPolling()
    }
    if (pending.value) pollTimer = setTimeout(poll, 1000)
  } catch (_) {
    stopPolling()
    if (!disposed) showToast(t('macosPermissions.requestFailed'), 'error')
  }
}

async function openSettings(id) {
  try {
    const result = await api.openMacosPrivacySettings(id)
    if (!result?.ok) throw new Error()
  } catch (_) { showToast(t('macosPermissions.openFailed'), 'error') }
}

async function dragApp(id) {
  try {
    const result = await api.showMacosPermissionCard(id, {
      title: t('assistant.macosPermissionDragTitle'),
      hint: t('assistant.macosPermissionDragHint'),
      reveal: t('assistant.macosPermissionReveal'),
    })
    if (!result?.ok) {
      showToast(t(result?.error_code === 'macos_app_bundle_unavailable'
        ? 'assistant.macosPermissionDragUnavailable' : 'macosPermissions.openFailed'), 'error')
    }
  } catch (_) { showToast(t('macosPermissions.openFailed'), 'error') }
}

onMounted(() => {
  void refresh()
  window.addEventListener('focus', refresh)
})
onUnmounted(() => {
  disposed = true
  requestGeneration++
  stopPolling()
  window.removeEventListener('focus', refresh)
})
</script>

<template>
  <div class="macos-permissions">
    <div class="permissions-toolbar">
      <p class="muted">{{ t('macosPermissions.hint') }}</p>
      <button type="button" class="ghost tiny" :disabled="loading" @click="refresh">{{ t('common.refresh') }}</button>
    </div>
    <p v-if="error" role="alert" class="permission-error">{{ error }}</p>
    <p v-if="loading && !rows.length" role="status" class="muted">{{ t('common.loading') }}</p>
    <ul class="permissions-list">
      <li v-for="row in rows" :key="row.id" :data-permission="row.id">
        <div class="permission-heading">
          <strong>{{ t(`macosPermissions.labels.${row.id}`) }}</strong>
          <span class="permission-status" :class="{ granted: row.status === 'granted' }">
            {{ t(`macosPermissions.status.${row.status}`) }}
          </span>
        </div>
        <p>{{ t(`macosPermissions.hints.${row.id}`) }}</p>
        <p v-if="row.status === 'unavailable'" class="muted">{{ t('macosPermissions.unavailableHint') }}</p>
        <div class="permission-actions">
          <button v-if="row.requestable" type="button" class="primary tiny" :disabled="!!pending" @click="request(row)">
            {{ t(pending === row.id ? 'macosPermissions.waiting' : 'macosPermissions.request') }}
          </button>
          <button v-if="['accessibility', 'screen_capture', 'listen_event'].includes(row.id) && row.status === 'denied'"
            type="button" class="ghost tiny" @click="dragApp(row.id)">{{ t('assistant.macosPermissionDrag') }}</button>
          <button type="button" class="ghost tiny" @click="openSettings(row.id)">{{ t('assistant.openSettings') }}</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.permissions-toolbar { display: flex; align-items: flex-start; gap: 16px; justify-content: space-between; margin-bottom: 18px; }
.permissions-toolbar p { margin: 0; line-height: 1.6; }
.permissions-toolbar button { flex-shrink: 0; }
.permissions-list { list-style: none; padding: 0; margin: 0; }
.permissions-list li { padding: 18px 0; border-top: 1px solid var(--border); }
.permission-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.permission-status { font-size: 12px; color: var(--muted); flex-shrink: 0; }
.permission-status.granted { color: var(--success, #269782); }
.permissions-list p { margin: 8px 0 12px; font-size: 13px; line-height: 1.65; color: var(--muted); }
.permission-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.permission-error { color: var(--danger, #d54c4c); }
@media (max-width: 480px) { .permission-heading { flex-wrap: wrap; gap: 4px 12px; } }
</style>
