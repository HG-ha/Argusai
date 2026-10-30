<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { NSelect } from 'naive-ui'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import WorkbenchModal from './workbench/WorkbenchModal.vue'
import PageEmpty from './list/PageEmpty.vue'

const confirmDialog = useConfirm()
const providers = ref([])
const channels = ref([])
const identities = ref([])
const sessions = ref([])
const selectedId = ref('')
const query = ref('')
const loading = ref(false)
const saving = ref(false)
const testing = reactive({})
const showEditor = ref(false)
const showPairing = ref(false)
const pairing = ref(null)
const form = reactive(emptyForm())
const showQqLogin = ref(false)
const qqLogin = ref(null)
const qqStatus = ref('')
const qqBusy = ref(false)
let qqPollTimer = 0
const showWechatLogin = ref(false)
const wechatQr = ref(null)
const wechatStatus = ref('')
const wechatBusy = ref(false)
let wechatPollTimer = 0

// `yields` names the credential a step produces, so the guide can point at the
// field further down instead of leaving the user to match console labels by eye.
const CHANNEL_GUIDES = Object.freeze({
  qq: {
    url: 'https://q.qq.com/',
    steps: [
      { id: 'qq1' },
      { id: 'qq2' },
      { id: 'qq3', yields: ['app_id', 'client_secret'] },
      { id: 'qq4' },
    ],
  },
  telegram: {
    url: 'https://t.me/BotFather',
    steps: [
      { id: 'telegram1' },
      { id: 'telegram2', yields: ['bot_token'] },
      { id: 'telegram3' },
    ],
  },
  discord: {
    url: 'https://discord.com/developers/applications',
    steps: [
      { id: 'discord1' },
      { id: 'discord2' },
      { id: 'discord3' },
      { id: 'discord4', yields: ['bot_token'] },
    ],
  },
  slack: {
    url: 'https://api.slack.com/apps',
    steps: [
      { id: 'slack1' },
      { id: 'slack2', yields: ['app_token'] },
      { id: 'slack3' },
      { id: 'slack4', yields: ['bot_token', 'app_token'] },
    ],
  },
  feishu: {
    url: 'https://open.feishu.cn/app',
    steps: [
      { id: 'feishu1' },
      { id: 'feishu2' },
      { id: 'feishu3' },
      { id: 'feishu4', yields: ['app_id', 'app_secret', 'verification_token'] },
    ],
  },
  dingtalk: {
    url: 'https://open-dev.dingtalk.com/fe/app',
    steps: [
      { id: 'dingtalk1' },
      { id: 'dingtalk2' },
      { id: 'dingtalk3', yields: ['client_id', 'client_secret'] },
    ],
  },
  wecom: {
    url: 'https://work.weixin.qq.com/wework_admin/frame#apps',
    steps: [
      { id: 'wecom1', yields: ['corp_id', 'agent_id', 'corp_secret'] },
      { id: 'wecom2', yields: ['token', 'aes_key'] },
      { id: 'wecom3' },
      { id: 'wecom4' },
    ],
  },
  wechat: {
    url: '',
    steps: [
      { id: 'wechat1' },
      { id: 'wechat2' },
      { id: 'wechat3' },
      { id: 'wechat4' },
    ],
  },
  webhook: {
    url: '',
    steps: [
      { id: 'webhook1' },
      { id: 'webhook2', yields: ['signing_secret'] },
      { id: 'webhook3' },
    ],
  },
})

const selected = computed(() =>
  channels.value.find(item => item.id === selectedId.value) || filtered.value[0] || null,
)
const selectedProvider = computed(() =>
  providers.value.find(item => item.id === form.provider) || null,
)
const activeGuide = computed(() => CHANNEL_GUIDES[form.provider] || CHANNEL_GUIDES.webhook)
const visibleSecrets = computed(() =>
  // WeChat's bot_token only ever comes out of the QR handshake, so offering a
  // text field for it would just invite a value that cannot work.
  form.provider === 'wechat' ? [] : (selectedProvider.value?.secrets || []),
)
const callbackPreview = computed(() => {
  if (!['webhook', 'wecom'].includes(form.provider)) return ''
  const host = String(form.host || '').trim() || '127.0.0.1'
  const port = String(form.port || '').trim()
  const path = String(form.path || '').trim() || '/channels/<id>'
  return `http://${host}:${port || '<port>'}${path.startsWith('/') ? path : `/${path}`}`
})
const wechatStatusText = computed(() => {
  const keys = { wait: 'wechatQrWaiting', scanned: 'wechatQrScanned', expired: 'wechatQrExpired' }
  const key = keys[wechatStatus.value]
  return key ? t(`channels.${key}`) : ''
})
const qqStatusText = computed(() => {
  const keys = {
    starting: 'qqQrStarting',
    wait: 'qqQrWaiting',
    expired: 'qqQrExpired',
    canceled: 'qqQrCanceled',
    error: 'qqQrFailed',
  }
  const key = keys[qqStatus.value]
  return key ? t(`channels.${key}`) : ''
})
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return channels.value
  return channels.value.filter(item =>
    `${item.name || ''} ${item.provider || ''} ${item.id || ''}`.toLowerCase().includes(q),
  )
})
const selectedIdentities = computed(() =>
  identities.value.filter(item => item.channel_id === selected.value?.id),
)
const connectedCount = computed(() =>
  channels.value.filter(item => item.status?.connected).length,
)
const sessionOptions = computed(() => [
  { label: t('channels.unbound'), value: '' },
  ...sessions.value.map(item => ({
    label: item.name || item.id,
    value: item.id,
  })),
])
const permissionOptions = computed(() => [
  { label: t('channels.permissionChat'), value: 'chat' },
  { label: t('channels.permissionStatus'), value: 'status' },
  { label: t('channels.permissionCancel'), value: 'cancel' },
  { label: t('channels.permissionConfirm'), value: 'confirm' },
  { label: t('channels.permissionConfigure'), value: 'configure' },
  { label: t('channels.permissionSend'), value: 'send' },
])

function emptyForm() {
  return {
    id: '',
    provider: 'telegram',
    name: '',
    enabled: false,
    host: '127.0.0.1',
    port: '',
    path: '',
    outbound_url: '',
    card_template_id: '',
    card_content_key: 'content',
    stream_protocol: '',
    extra_settings: {},
    proxy: '',
    api_base: '',
    secrets: {},
  }
}

async function loadAll() {
  loading.value = true
  try {
    const [providerResult, channelResult, identityResult, sessionResult] = await Promise.all([
      api.listChannelProviders(),
      api.listChannels(),
      api.listChannelIdentities(''),
      api.listAssistantSessions(),
    ])
    if (!providerResult?.ok) throw new Error(providerResult?.error || t('channels.loadFailed'))
    if (!channelResult?.ok) throw new Error(channelResult?.error || t('channels.loadFailed'))
    providers.value = providerResult.providers || []
    channels.value = channelResult.channels || []
    identities.value = identityResult?.identities || []
    sessions.value = Array.isArray(sessionResult) ? sessionResult : (sessionResult?.sessions || [])
    if (!selectedId.value || !channels.value.some(item => item.id === selectedId.value)) {
      selectedId.value = channels.value[0]?.id || ''
    }
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    loading.value = false
  }
}

function providerLabel(providerId) {
  return providers.value.find(item => item.id === providerId)?.name || providerId
}

function providerInitial(providerId) {
  const initials = { qq: 'QQ', telegram: 'TG', discord: 'DC', slack: 'SL', feishu: 'FS', dingtalk: 'DT', wecom: 'WW', wechat: 'WX', webhook: 'WH' }
  return initials[providerId] || '?'
}

function cardMode(providerId) {
  return providers.value.find(item => item.id === providerId)?.card_mode || 'structured_payload'
}

function cardModeLabel(providerId) {
  return t(`channels.cardModes.${cardMode(providerId)}`)
}

function secretLabel(secret) {
  return t(`channels.secretLabels.${secret}`, null, secret)
}

function openProviderPortal() {
  if (activeGuide.value.url) api.openExternalUrl(activeGuide.value.url)
}

function openNew() {
  Object.assign(form, emptyForm())
  showEditor.value = true
}

function openEdit(channel) {
  Object.assign(form, {
    id: channel.id,
    provider: channel.provider,
    name: channel.name,
    enabled: channel.enabled !== false,
    host: channel.settings?.host || '127.0.0.1',
    port: channel.settings?.port || '',
    path: channel.settings?.path || '',
    outbound_url: channel.settings?.outbound_url || '',
    card_template_id: channel.settings?.card_template_id || '',
    card_content_key: channel.settings?.card_content_key || 'content',
    stream_protocol: channel.settings?.stream_protocol || '',
    extra_settings: { ...(channel.settings || {}) },
    proxy: channel.settings?.proxy || '',
    api_base: channel.settings?.api_base || '',
    secrets: {},
  })
  showEditor.value = true
}

function chooseProvider(provider) {
  if (form.id) return
  form.provider = provider.id
}

async function saveChannel() {
  saving.value = true
  try {
    const secrets = Object.fromEntries(
      Object.entries(form.secrets || {}).filter(([, value]) => String(value || '').trim()),
    )
    let settings = { ...form.extra_settings }
    if (['webhook', 'wecom'].includes(form.provider)) {
      settings = {
        ...settings,
        host: form.host.trim() || '127.0.0.1',
        port: Number(form.port || 0),
        path: form.path.trim(),
      }
      if (form.provider === 'webhook') {
        settings.outbound_url = form.outbound_url.trim()
        settings.stream_protocol = form.stream_protocol
      }
    } else if (form.provider === 'telegram') {
      settings = {
        ...settings,
        proxy: form.proxy.trim(),
        api_base: form.api_base.trim(),
      }
    } else if (form.provider === 'dingtalk') {
      settings.card_template_id = form.card_template_id.trim()
      settings.card_content_key = form.card_content_key.trim() || 'content'
    }
    const result = await api.saveChannel({
      id: form.id,
      provider: form.provider,
      name: form.name.trim() || providerLabel(form.provider),
      enabled: !!form.enabled,
      transport: selectedProvider.value?.transport || '',
      settings,
      secrets,
    })
    if (!result?.ok) throw new Error(result?.error || t('channels.saveFailed'))
    selectedId.value = result.channel?.id || form.id
    showEditor.value = false
    await loadAll()
    showToast(t('channels.saved'), 'ok')
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    saving.value = false
  }
}

async function toggleChannel(channel) {
  const result = await api.setChannelEnabled(channel.id, channel.enabled === false)
  if (!result?.ok) {
    showToast(result?.error || t('channels.saveFailed'), 'error')
    return
  }
  await loadAll()
}

async function testChannel(channel) {
  testing[channel.id] = true
  try {
    const result = await api.testChannel(channel.id)
    showToast(
      result?.ok ? t('channels.testPassed') : `${t('channels.testFailed')}: ${result?.error || ''}`,
      result?.ok ? 'ok' : 'error',
    )
  } finally {
    testing[channel.id] = false
  }
}

async function deleteChannel(channel) {
  const ok = await confirmDialog({
    title: t('channels.deleteTitle'),
    content: t('channels.deleteContent', { name: channel.name || channel.id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  const result = await api.deleteChannel(channel.id)
  if (!result?.ok) {
    showToast(result?.error || t('channels.deleteFailed'), 'error')
    return
  }
  await loadAll()
}

async function createPairing(channel) {
  const result = await api.createChannelPairing(channel.id, '')
  if (!result?.ok) {
    showToast(result?.error || t('channels.pairFailed'), 'error')
    return
  }
  pairing.value = result
  showPairing.value = true
}

async function bindIdentity(identity, sessionId) {
  const result = await api.bindChannelIdentity(identity.id, sessionId || '')
  if (!result?.ok) {
    showToast(result?.error || t('channels.bindFailed'), 'error')
    return
  }
  await loadAll()
}

async function setIdentityPermissions(identity, permissions) {
  const result = await api.setChannelIdentityPermissions(identity.id, permissions || [])
  if (!result?.ok) {
    showToast(result?.error || t('channels.permissionsFailed'), 'error')
    return
  }
  await loadAll()
}

function stopQqPoll() {
  if (!qqPollTimer) return
  window.clearTimeout(qqPollTimer)
  qqPollTimer = 0
}

async function openQqLogin(channel) {
  if (!channel?.id) {
    showToast(t('channels.qqSaveFirst'), 'error')
    return
  }
  showQqLogin.value = true
  await requestQqLogin(channel.id)
}

async function closeQqLogin() {
  stopQqPoll()
  const loginId = qqLogin.value?.id
  const active = ['starting', 'wait'].includes(qqStatus.value)
  showQqLogin.value = false
  if (loginId && active) {
    try {
      await api.cancelQqLogin(loginId)
    } catch (_) {
      // Closing remains available if cancellation races process shutdown.
    }
  }
}

async function requestQqLogin(channelId) {
  stopQqPoll()
  if (qqLogin.value?.id && ['starting', 'wait'].includes(qqStatus.value)) {
    try { await api.cancelQqLogin(qqLogin.value.id) } catch (_) { /* best effort */ }
  }
  qqBusy.value = true
  qqLogin.value = null
  qqStatus.value = 'starting'
  try {
    const result = await api.startQqLogin(channelId)
    if (!result?.ok) throw new Error(result?.error || t('channels.qqQrFailed'))
    qqLogin.value = {
      id: String(result.login_id || ''),
      channelId,
      image: wechatQrSrc(result.image),
      link: String(result.qr_url || ''),
    }
    qqStatus.value = String(result.status || 'starting')
    qqPollTimer = window.setTimeout(pollQqLogin, 350)
  } catch (error) {
    qqStatus.value = 'error'
    showToast(String(error?.message || error), 'error')
  } finally {
    qqBusy.value = false
  }
}

async function pollQqLogin() {
  const login = qqLogin.value
  if (!login?.id || !showQqLogin.value) return
  let result
  try {
    result = await api.pollQqLogin(login.id, login.channelId)
  } catch (error) {
    qqStatus.value = 'error'
    showToast(String(error?.message || error), 'error')
    return
  }
  if (!showQqLogin.value) return
  if (!result?.ok) {
    qqStatus.value = 'error'
    showToast(result?.error || t('channels.qqQrFailed'), 'error')
    return
  }
  qqLogin.value = {
    ...login,
    image: wechatQrSrc(result.image),
    link: String(result.qr_url || ''),
  }
  qqStatus.value = String(result.status || 'starting')
  if (qqStatus.value === 'confirmed') {
    stopQqPoll()
    showQqLogin.value = false
    showToast(t('channels.qqQrDone'), 'ok')
    await loadAll()
    return
  }
  if (['expired', 'error', 'canceled'].includes(qqStatus.value)) {
    stopQqPoll()
    if (result.error) showToast(String(result.error), 'error')
    return
  }
  qqPollTimer = window.setTimeout(pollQqLogin, 700)
}

function openQqLoginLink() {
  const link = String(qqLogin.value?.link || '')
  if (link) api.openExternalUrl(link)
}

function wechatQrSrc(image) {
  const raw = String(image || '')
  if (!raw) return ''
  // The backend renders an SVG data URI; tolerate a bare base64 payload too.
  return raw.startsWith('data:') ? raw : `data:image/png;base64,${raw}`
}

function stopWechatPoll() {
  if (!wechatPollTimer) return
  window.clearTimeout(wechatPollTimer)
  wechatPollTimer = 0
}

async function openWechatLogin(channel) {
  if (!channel?.id) {
    showToast(t('channels.wechatSaveFirst'), 'error')
    return
  }
  showWechatLogin.value = true
  await requestWechatQr(channel.id)
}

function closeWechatLogin() {
  stopWechatPoll()
  showWechatLogin.value = false
}

async function requestWechatQr(channelId) {
  stopWechatPoll()
  wechatBusy.value = true
  wechatQr.value = null
  wechatStatus.value = ''
  try {
    const result = await api.startWechatLogin()
    if (!result?.ok) throw new Error(result?.error || t('channels.wechatQrFailed'))
    wechatQr.value = {
      code: result.qrcode,
      image: wechatQrSrc(result.image),
      link: String(result.link || ''),
    }
    wechatStatus.value = 'wait'
    wechatPollTimer = window.setTimeout(() => pollWechatLogin(channelId), 800)
  } catch (error) {
    wechatStatus.value = 'error'
    showToast(String(error?.message || error), 'error')
  } finally {
    wechatBusy.value = false
  }
}

async function pollWechatLogin(channelId) {
  const code = wechatQr.value?.code
  if (!code || !showWechatLogin.value) return
  let result
  try {
    result = await api.pollWechatLogin(code, channelId)
  } catch (error) {
    wechatStatus.value = 'error'
    showToast(String(error?.message || error), 'error')
    return
  }
  // The user may have closed the dialog while the long poll was in flight.
  if (!showWechatLogin.value) return
  if (!result?.ok) {
    wechatStatus.value = 'error'
    showToast(result?.error || t('channels.wechatQrFailed'), 'error')
    return
  }
  if (result.status === 'confirmed') {
    closeWechatLogin()
    showToast(t('channels.wechatQrDone'), 'ok')
    await loadAll()
    return
  }
  wechatStatus.value = String(result.status || 'wait')
  if (wechatStatus.value === 'expired') return
  wechatPollTimer = window.setTimeout(() => pollWechatLogin(channelId), 800)
}

async function deleteIdentity(identity) {
  const ok = await confirmDialog({
    title: t('channels.removeIdentityTitle'),
    content: t('channels.removeIdentityContent', { name: identity.display_name || identity.external_user_id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  const result = await api.deleteChannelIdentity(identity.id)
  if (!result?.ok) {
    showToast(result?.error || t('channels.removeIdentityFailed'), 'error')
    return
  }
  await loadAll()
}

onMounted(loadAll)
onUnmounted(() => {
  stopWechatPoll()
  stopQqPoll()
  if (qqLogin.value?.id && ['starting', 'wait'].includes(qqStatus.value)) {
    api.cancelQqLogin(qqLogin.value.id).catch(() => {})
  }
})
</script>

<template>
  <section class="channel-page">
    <header class="channel-head">
      <div>
        <p class="channel-kicker">{{ t('channels.kicker') }}</p>
        <h1>{{ t('channels.title') }}</h1>
        <p>{{ t('channels.hint') }}</p>
      </div>
      <div class="channel-head-actions">
        <span class="inventory-chip">{{ connectedCount }} / {{ channels.length }} {{ t('channels.connected') }}</span>
        <button class="btn ghost" type="button" :disabled="loading" @click="loadAll">{{ loading ? t('common.loading') : t('common.refresh') }}</button>
        <button class="btn primary" type="button" @click="openNew">{{ t('channels.add') }}</button>
      </div>
    </header>

    <div class="channel-shell">
      <aside class="channel-master">
        <label class="channel-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
          <input v-model="query" :placeholder="t('channels.search')" />
        </label>
        <div class="channel-list">
          <button
            v-for="channel in filtered"
            :key="channel.id"
            type="button"
            class="channel-row"
            :class="{ active: selected?.id === channel.id }"
            @click="selectedId = channel.id"
          >
            <span class="provider-mark" :class="channel.provider">{{ providerInitial(channel.provider) }}</span>
            <span class="channel-copy">
              <strong>{{ channel.name }}</strong>
              <small>{{ providerLabel(channel.provider) }} · {{ channel.identity_count || 0 }} {{ t('channels.identities') }}</small>
            </span>
            <span class="status-dot" :class="{ live: channel.status?.connected }"></span>
          </button>
          <PageEmpty v-if="!loading && !filtered.length" :title="t('channels.empty')" :hint="t('channels.emptyHint')" />
        </div>
      </aside>

      <article v-if="selected" class="channel-detail">
        <div class="channel-detail-head">
          <div class="channel-title">
            <span class="provider-mark large" :class="selected.provider">{{ providerInitial(selected.provider) }}</span>
            <div><h2>{{ selected.name }}</h2><p>{{ providerLabel(selected.provider) }} · {{ selected.transport }}</p></div>
          </div>
          <div class="detail-actions">
            <button v-if="selected.provider === 'qq'" class="btn ghost" type="button" @click="openQqLogin(selected)">{{ t('channels.qqSignIn') }}</button>
            <button v-if="selected.provider === 'wechat'" class="btn ghost" type="button" @click="openWechatLogin(selected)">{{ t('channels.wechatSignIn') }}</button>
            <button class="btn ghost" type="button" :disabled="testing[selected.id]" @click="testChannel(selected)">{{ testing[selected.id] ? t('channels.testing') : t('channels.test') }}</button>
            <button class="btn ghost" type="button" @click="openEdit(selected)">{{ t('common.edit') }}</button>
            <button type="button" class="switch" :class="{ on: selected.enabled !== false }" :aria-label="t('channels.toggle')" @click="toggleChannel(selected)"><span></span></button>
          </div>
        </div>

        <div class="delivery-route">
          <span>{{ providerLabel(selected.provider) }}</span><b>→</b><span>{{ t('channels.securityGateway') }}</span><b>→</b><span class="accent">{{ t('channels.boundAgent') }}</span>
        </div>
        <div class="card-capability" :class="cardMode(selected.provider)">
          <strong>{{ t('channels.cardCapability') }}</strong>
          <span>{{ cardModeLabel(selected.provider) }}</span>
          <small>{{ t(`channels.cardModeHints.${cardMode(selected.provider)}`) }}</small>
        </div>

        <section class="detail-section">
          <div class="section-head"><div><h3>{{ t('channels.connection') }}</h3><p>{{ t('channels.connectionHint') }}</p></div><span class="connection-badge" :class="{ live: selected.status?.connected }">{{ selected.status?.connected ? t('channels.online') : t('channels.offline') }}</span></div>
          <dl class="fact-rows">
            <div><dt>{{ t('channels.transport') }}</dt><dd><code>{{ selected.transport }}</code></dd></div>
            <div><dt>{{ t('channels.callback') }}</dt><dd><code>{{ selected.status?.listen || selected.settings?.path || '—' }}</code></dd></div>
            <div><dt>{{ t('channels.credentials') }}</dt><dd>{{ Object.keys(selected.configured_secrets || {}).length }} {{ t('channels.configured') }}</dd></div>
            <div v-if="selected.provider === 'telegram' && selected.settings?.proxy"><dt>{{ t('channels.proxy') }}</dt><dd><code>{{ selected.settings.proxy }}</code></dd></div>
            <div v-if="selected.provider === 'telegram' && selected.settings?.api_base"><dt>{{ t('channels.apiBase') }}</dt><dd><code>{{ selected.settings.api_base }}</code></dd></div>
            <div v-if="selected.status?.last_error"><dt>{{ t('channels.lastError') }}</dt><dd class="error-text">{{ selected.status.last_error }}</dd></div>
          </dl>
        </section>

        <section class="detail-section">
          <div class="section-head">
            <div><h3>{{ t('channels.pairedIdentities') }}</h3><p>{{ t('channels.pairedHint') }}</p></div>
            <button class="btn primary compact" type="button" @click="createPairing(selected)">{{ t('channels.pair') }}</button>
          </div>
          <div v-if="selectedIdentities.length" class="identity-list">
            <div v-for="identity in selectedIdentities" :key="identity.id" class="identity-row">
              <span class="identity-avatar">{{ (identity.display_name || identity.external_user_id || '?').slice(0, 1).toUpperCase() }}</span>
              <span class="identity-copy">
                <strong>{{ identity.display_name || identity.external_user_id }}</strong>
                <small>{{ identity.external_user_id }} · {{ identity.external_chat_id || '—' }}<template v-if="identity.external_thread_id"> / {{ identity.external_thread_id }}</template></small>
              </span>
              <NSelect
                :value="identity.bound_session_id || ''"
                :options="sessionOptions"
                size="small"
                class="session-select"
                @update:value="value => bindIdentity(identity, value)"
              />
              <NSelect
                :value="identity.permissions || []"
                :options="permissionOptions"
                multiple
                max-tag-count="responsive"
                size="small"
                class="permission-select"
                :placeholder="t('channels.permissions')"
                @update:value="value => setIdentityPermissions(identity, value)"
              />
              <button class="icon-btn danger" type="button" :title="t('common.delete')" @click="deleteIdentity(identity)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>
              </button>
            </div>
          </div>
          <div v-else class="identity-empty">{{ t('channels.noIdentities') }}</div>
        </section>

        <div class="danger-strip">
          <div><strong>{{ t('channels.removeTitle') }}</strong><span>{{ t('channels.removeHint') }}</span></div>
          <button class="btn danger" type="button" @click="deleteChannel(selected)">{{ t('common.delete') }}</button>
        </div>
      </article>
    </div>

    <WorkbenchModal
      :open="showEditor"
      size="lg"
      :title="form.id ? t('channels.editTitle') : t('channels.addTitle')"
      :busy="saving"
      :close-label="t('common.close')"
      @close="showEditor = false"
    >
      <form class="channel-form" @submit.prevent="saveChannel">
        <div class="provider-grid">
          <button v-for="provider in providers" :key="provider.id" type="button" :disabled="!!form.id" :class="{ active: form.provider === provider.id }" @click="chooseProvider(provider)">
            <span class="provider-mark" :class="provider.id">{{ providerInitial(provider.id) }}</span>
            <span><strong>{{ provider.name }}</strong><small>{{ provider.transport }}</small></span>
          </button>
        </div>
        <details class="setup-guide" :open="!form.id">
          <summary>
            <span class="guide-summary-copy">
              <small>{{ t('channels.guideEyebrow') }}</small>
              <strong>{{ t('channels.guideTitle', { provider: providerLabel(form.provider) }) }}</strong>
            </span>
            <span class="guide-toggle">{{ t('channels.guideToggle') }}</span>
          </summary>
          <div class="guide-body">
            <ol>
              <li v-for="(step, index) in activeGuide.steps" :key="step.id">
                <span>{{ index + 1 }}</span>
                <p>
                  {{ t(`channels.guides.${step.id}`) }}
                  <em v-if="step.yields">
                    {{ t('channels.guideCredential') }}
                    <code v-for="secret in step.yields" :key="secret">{{ secretLabel(secret) }}</code>
                  </em>
                </p>
              </li>
            </ol>
            <p class="guide-next-eyebrow">{{ t('channels.guideNextEyebrow') }}</p>
            <ol class="guide-next">
              <li><span>{{ activeGuide.steps.length + 1 }}</span><p>{{ t('channels.guideNext1') }}</p></li>
              <li><span>{{ activeGuide.steps.length + 2 }}</span><p>{{ t('channels.guideNext2') }}</p></li>
              <li><span>{{ activeGuide.steps.length + 3 }}</span><p>{{ t('channels.guideNext3') }}</p></li>
            </ol>
            <div class="guide-footer">
              <button v-if="activeGuide.url" class="guide-link" type="button" @click="openProviderPortal">
                {{ t('channels.openProviderPortal') }}
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5M19 5l-8 8"/><path d="M18 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
              </button>
              <span>{{ t('channels.guideFinish') }}</span>
            </div>
          </div>
        </details>
        <div class="card-capability compact" :class="cardMode(form.provider)">
          <strong>{{ t('channels.cardCapability') }}</strong>
          <span>{{ cardModeLabel(form.provider) }}</span>
          <small>{{ t(`channels.cardModeHints.${cardMode(form.provider)}`) }}</small>
        </div>
        <label><span>{{ t('channels.name') }}</span><input v-model="form.name" required /></label>
        <div v-if="['webhook','wecom'].includes(form.provider)" class="form-grid two">
          <label><span>{{ t('channels.listenHost') }}</span><input v-model="form.host" /></label>
          <label><span>{{ t('channels.listenPort') }}</span><input v-model="form.port" type="number" min="0" max="65535" :placeholder="t('channels.autoPort')" /></label>
        </div>
        <label v-if="['webhook','wecom'].includes(form.provider)"><span>{{ t('channels.callbackPath') }}</span><input v-model="form.path" placeholder="/channels/…" /></label>
        <div v-if="callbackPreview" class="callback-preview">
          <strong>{{ t('channels.callbackPreview') }}</strong>
          <code>{{ callbackPreview }}</code>
          <small>{{ t('channels.callbackPreviewHint') }}</small>
        </div>
        <label v-if="form.provider === 'webhook'"><span>{{ t('channels.outboundUrl') }}</span><input v-model="form.outbound_url" placeholder="https://…" /></label>
        <label v-if="form.provider === 'webhook'"><span>{{ t('channels.streamProtocol') }}</span><select v-model="form.stream_protocol"><option value="">{{ t('channels.streamStaged') }}</option><option value="v1">ArgusAI stream v1</option></select></label>
        <template v-if="form.provider === 'dingtalk'">
          <label><span>{{ t('channels.cardTemplateId') }}</span><input v-model="form.card_template_id" /></label>
          <label><span>{{ t('channels.cardContentKey') }}</span><input v-model="form.card_content_key" placeholder="content" /></label>
          <p class="qr-notice">{{ t('channels.dingStreamHint') }}</p>
        </template>
        <label v-if="form.provider === 'telegram'"><span>{{ t('channels.proxy') }}</span><input v-model="form.proxy" :placeholder="t('channels.proxyPlaceholder')" /></label>
        <label v-if="form.provider === 'telegram'"><span>{{ t('channels.apiBase') }}</span><input v-model="form.api_base" :placeholder="t('channels.apiBasePlaceholder')" /></label>
        <p v-if="form.provider === 'qq'" class="qr-notice">{{ t('channels.qqSignInHint') }}</p>
        <p v-if="form.provider === 'wechat'" class="qr-notice">{{ t('channels.wechatSignInHint') }}</p>
        <div v-if="visibleSecrets.length" class="secret-grid">
          <label v-for="secret in visibleSecrets" :key="secret">
            <span>{{ secretLabel(secret) }} <code>{{ secret }}</code></span>
            <input v-model="form.secrets[secret]" type="password" autocomplete="new-password" :placeholder="form.id ? t('channels.keepExisting') : ''" />
          </label>
        </div>
        <label class="check-row"><input v-model="form.enabled" type="checkbox" /><span>{{ t('channels.enableAfterSave') }}</span></label>
      </form>
      <template #footer>
        <button class="btn ghost" type="button" @click="showEditor = false">{{ t('common.cancel') }}</button>
        <button class="btn primary" type="button" :disabled="saving" @click="saveChannel">{{ saving ? t('channels.saving') : t('common.save') }}</button>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showPairing"
      size="sm"
      :title="t('channels.pairTitle')"
      :close-label="t('common.close')"
      @close="showPairing = false"
    >
      <div class="pairing-card">
        <p>{{ t('channels.pairInstructions') }}</p>
        <div class="pair-code">{{ pairing?.code || '------' }}</div>
        <code>/pair {{ pairing?.code || '------' }}</code>
        <small>{{ t('channels.pairExpiry') }}</small>
      </div>
      <template #footer><button class="btn primary" type="button" @click="showPairing = false">{{ t('common.done') }}</button></template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showQqLogin"
      size="sm"
      :title="t('channels.qqQrTitle')"
      :busy="qqBusy"
      :close-label="t('common.close')"
      @close="closeQqLogin"
    >
      <div class="qr-card">
        <p>{{ t('channels.qqQrHint') }}</p>
        <div class="qr-frame" :class="{ stale: ['expired', 'error', 'canceled'].includes(qqStatus) }">
          <img v-if="qqLogin?.image" :src="qqLogin.image" :alt="t('channels.qqQrTitle')" />
          <span v-else>{{ qqBusy || qqStatus === 'starting' ? t('common.loading') : t('channels.qqQrFailed') }}</span>
        </div>
        <small>{{ qqStatusText }}</small>
        <button v-if="qqLogin?.link" class="qr-browser-link" type="button" @click="openQqLoginLink">{{ t('channels.qqQrOpenBrowser') }}</button>
      </div>
      <template #footer>
        <button class="btn ghost" type="button" :disabled="qqBusy" @click="requestQqLogin(selected?.id)">{{ t('channels.qqQrRefresh') }}</button>
        <button class="btn primary" type="button" @click="closeQqLogin">{{ t('common.close') }}</button>
      </template>
    </WorkbenchModal>

    <WorkbenchModal
      :open="showWechatLogin"
      size="sm"
      :title="t('channels.wechatQrTitle')"
      :busy="wechatBusy"
      :close-label="t('common.close')"
      @close="closeWechatLogin"
    >
      <div class="qr-card">
        <p>{{ t('channels.wechatQrHint') }}</p>
        <div class="qr-frame" :class="{ stale: wechatStatus === 'expired' }">
          <img v-if="wechatQr?.image" :src="wechatQr.image" :alt="t('channels.wechatQrTitle')" />
          <span v-else>{{ wechatBusy ? t('common.loading') : t('channels.wechatQrFailed') }}</span>
        </div>
        <small>{{ wechatStatusText }}</small>
        <code v-if="wechatQr?.link && !wechatQr?.image" class="qr-link">{{ wechatQr.link }}</code>
      </div>
      <template #footer>
        <button class="btn ghost" type="button" :disabled="wechatBusy" @click="requestWechatQr(selected?.id)">{{ t('channels.wechatQrRefresh') }}</button>
        <button class="btn primary" type="button" @click="closeWechatLogin">{{ t('common.close') }}</button>
      </template>
    </WorkbenchModal>
  </section>
</template>

<style scoped>
.channel-page{height:100%;min-height:0;display:flex;flex-direction:column;padding:18px 20px;gap:14px;overflow:hidden;background:transparent}.channel-head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex:none}.channel-kicker{margin:0 0 7px;font:700 11px/1.2 var(--font-mono);letter-spacing:.14em;color:var(--accent);text-transform:uppercase}.channel-head h1{margin:0;font-size:20px;line-height:1.2;letter-spacing:-.015em}.channel-head p:not(.channel-kicker){margin:5px 0 0;color:var(--text-secondary);font-size:12px}.channel-head-actions{display:flex;align-items:center;gap:8px}.inventory-chip{padding:7px 10px;border:1px solid var(--border);border-radius:8px;color:var(--text-secondary);font-size:12px;background:var(--surface)}
.channel-shell{display:grid;grid-template-columns:minmax(240px,290px) minmax(0,1fr);min-height:0;flex:1;border:1px solid var(--border);border-radius:14px;overflow:hidden;background:var(--surface);box-shadow:none}.channel-master{min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--border);background:color-mix(in srgb,var(--surface) 95%,var(--page-bg))}.channel-search{margin:14px;display:flex;align-items:center;gap:8px;padding:0 10px;height:36px;border:1px solid var(--border);border-radius:8px;background:var(--input-bg)}.channel-search:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}.channel-search svg{width:15px;fill:none;stroke:var(--text-tertiary);stroke-width:2}.channel-search input{min-width:0;flex:1;border:0;outline:0;background:none;color:var(--text-primary)}.channel-list{min-height:0;overflow:auto;padding:0 8px 12px}.channel-row{width:100%;display:grid;grid-template-columns:36px minmax(0,1fr) 10px;align-items:center;gap:10px;padding:10px;border:0;border-radius:9px;background:transparent;color:inherit;text-align:left;cursor:pointer}.channel-row:hover{background:var(--surface-hover)}.channel-row.active{background:color-mix(in srgb,var(--accent) 11%,var(--surface));box-shadow:inset 2px 0 var(--accent)}
.provider-mark{width:34px;height:34px;flex:0 0 34px;flex-shrink:0;display:grid;place-items:center;border-radius:9px;color:white;background:#53657d;font:750 10px/1 var(--font-mono);letter-spacing:.02em}.provider-mark.qq{background:#12b7f5}.provider-mark.telegram{background:#168bd2}.provider-mark.discord{background:#5865f2}.provider-mark.slack{background:#6e3a73}.provider-mark.feishu{background:#3370ff}.provider-mark.dingtalk{background:#1888ff}.provider-mark.wecom{background:#1aad19}.provider-mark.wechat{background:#07c160}.provider-mark.webhook{background:#6d7480}.provider-mark.large{width:44px;height:44px;flex:0 0 44px;flex-shrink:0;border-radius:11px;font-size:12px}.channel-copy{min-width:0;display:flex;flex-direction:column;gap:3px}.channel-copy strong,.channel-copy small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.channel-copy strong{font-size:13px}.channel-copy small{color:var(--text-tertiary);font-size:11px}.status-dot{width:7px;height:7px;flex-shrink:0;border-radius:50%;background:var(--text-tertiary);opacity:.45}.status-dot.live{background:#2ba36a;opacity:1;box-shadow:0 0 0 3px color-mix(in srgb,#2ba36a 14%,transparent)}
.channel-detail{min-width:0;min-height:0;overflow:auto;padding:22px 24px}.channel-detail-head{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px}.channel-title{min-width:0;flex:1 1 220px;display:flex;align-items:center;gap:12px}.channel-title>div{min-width:0}.channel-title h2{margin:0;font-size:19px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.channel-title p{margin:5px 0 0;color:var(--text-secondary);font:11px/1.35 var(--font-mono);overflow-wrap:anywhere}.detail-actions{flex:0 0 auto;display:flex;align-items:center;flex-wrap:wrap;gap:8px}.delivery-route{margin:20px 0;display:flex;align-items:center;gap:8px;overflow:auto;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--page-bg)}.delivery-route span{flex:none;padding:6px 9px;border:1px solid var(--border);border-radius:7px;background:var(--surface);font-size:11px}.delivery-route span.accent{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));color:var(--accent)}.delivery-route b{color:var(--text-tertiary);font-weight:400}
.detail-section{margin-top:14px;border:1px solid var(--border);border-radius:11px;overflow:hidden}.section-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;border-bottom:1px solid var(--border)}.section-head h3{margin:0;font-size:13px}.section-head p{margin:4px 0 0;color:var(--text-tertiary);font-size:11px}.connection-badge{padding:5px 8px;border-radius:99px;background:var(--page-bg);color:var(--text-tertiary);font-size:11px}.connection-badge.live{background:color-mix(in srgb,#2ba36a 12%,var(--surface));color:#21885a}.fact-rows{margin:0}.fact-rows>div{display:grid;grid-template-columns:140px minmax(0,1fr);gap:14px;padding:10px 16px;border-top:1px solid var(--border-subtle)}.fact-rows>div:first-child{border-top:0}.fact-rows dt{color:var(--text-secondary);font-size:12px}.fact-rows dd{margin:0;min-width:0;font-size:12px}.fact-rows code{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:11px/1.4 var(--font-mono)}.error-text{color:var(--danger)}
.identity-list{padding:4px 10px}.identity-row{display:grid;grid-template-columns:34px minmax(110px,1fr) minmax(135px,200px) minmax(135px,200px) 32px;align-items:center;gap:10px;padding:9px 6px;border-top:1px solid var(--border-subtle)}.identity-row:first-child{border-top:0}.identity-avatar{width:30px;height:30px;display:grid;place-items:center;border-radius:50%;background:var(--page-bg);font-weight:700}.identity-copy{min-width:0;display:flex;flex-direction:column;gap:2px}.identity-copy strong,.identity-copy small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.identity-copy strong{font-size:12px}.identity-copy small{color:var(--text-tertiary);font:10px/1.2 var(--font-mono)}.identity-empty{padding:24px;text-align:center;color:var(--text-tertiary);font-size:12px}.session-select,.permission-select{min-width:0}.icon-btn{width:30px;height:30px;display:grid;place-items:center;border:0;border-radius:7px;background:transparent;color:var(--text-tertiary);cursor:pointer}.icon-btn:hover{background:var(--surface-hover)}.icon-btn.danger:hover{color:var(--danger)}.icon-btn svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8}
.danger-strip{margin-top:14px;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 15px;border:1px solid color-mix(in srgb,var(--danger) 28%,var(--border));border-radius:10px}.danger-strip div{display:flex;flex-direction:column;gap:3px}.danger-strip strong{font-size:12px}.danger-strip span{color:var(--text-tertiary);font-size:11px}.btn.danger{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 35%,var(--border))}.btn.compact{padding:6px 10px;font-size:11px}
.switch{width:38px;height:22px;padding:2px;border:0;border-radius:99px;background:var(--border-strong);cursor:pointer}.switch span{display:block;width:18px;height:18px;border-radius:50%;background:white;transition:transform .16s ease}.switch.on{background:var(--accent)}.switch.on span{transform:translateX(16px)}
.channel-form{display:flex;flex-direction:column;gap:14px}.provider-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.provider-grid button{display:flex;align-items:center;gap:8px;padding:9px;border:1px solid var(--border);border-radius:9px;background:var(--surface);color:inherit;text-align:left;cursor:pointer}.provider-grid button:hover:not(:disabled){border-color:var(--border-strong)}.provider-grid button.active{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 12%,transparent)}.provider-grid button:disabled{cursor:default}.provider-grid button>span:last-child{min-width:0;display:flex;flex-direction:column;gap:2px}.provider-grid strong,.provider-grid small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.provider-grid strong{font-size:11px}.provider-grid small{color:var(--text-tertiary);font:9px/1.2 var(--font-mono)}.channel-form label{display:flex;flex-direction:column;gap:6px;color:var(--text-secondary);font-size:12px}.channel-form input{width:100%;box-sizing:border-box;border:1px solid var(--border);border-radius:8px;background:var(--input-bg);color:var(--text-primary);padding:9px 10px;outline:0}.channel-form input:focus{border-color:var(--accent)}.form-grid.two,.secret-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.channel-form .check-row{flex-direction:row;align-items:center}.channel-form .check-row input{width:15px}
.setup-guide{border:1px solid color-mix(in srgb,var(--accent) 25%,var(--border));border-radius:10px;background:color-mix(in srgb,var(--accent) 4%,var(--surface));overflow:hidden}.setup-guide summary{min-height:52px;box-sizing:border-box;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 13px;cursor:pointer;list-style:none}.setup-guide summary::-webkit-details-marker{display:none}.guide-summary-copy{min-width:0;display:flex;flex-direction:column;gap:3px}.guide-summary-copy small{color:var(--accent);font:700 9px/1.2 var(--font-mono);letter-spacing:.1em}.guide-summary-copy strong{font-size:12px}.guide-toggle{flex:none;color:var(--text-tertiary);font-size:11px}.setup-guide[open] .guide-toggle{color:var(--accent)}.guide-body{padding:2px 13px 13px;border-top:1px solid color-mix(in srgb,var(--accent) 16%,var(--border))}.guide-body ol{margin:0;padding:10px 0;display:flex;flex-direction:column;gap:8px;list-style:none}.guide-body li{display:grid;grid-template-columns:22px minmax(0,1fr);align-items:start;gap:9px}.guide-body li>span{width:20px;height:20px;display:grid;place-items:center;border-radius:50%;background:color-mix(in srgb,var(--accent) 13%,var(--surface));color:var(--accent);font:700 10px/1 var(--font-mono)}.guide-body li p{margin:1px 0 0;color:var(--text-secondary);font-size:11px;line-height:1.55}.guide-footer{display:flex;align-items:center;gap:10px;padding-top:10px;border-top:1px solid var(--border-subtle)}.guide-footer>span{color:var(--text-tertiary);font-size:10px;line-height:1.45}.guide-link{flex:none;display:inline-flex;align-items:center;gap:5px;padding:6px 8px;border:1px solid color-mix(in srgb,var(--accent) 28%,var(--border));border-radius:7px;background:var(--surface);color:var(--accent);font-size:11px;cursor:pointer}.guide-link:hover{background:color-mix(in srgb,var(--accent) 8%,var(--surface))}.guide-link svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.8}.secret-grid label>span code{margin-left:4px;color:var(--text-tertiary);font:9px/1 var(--font-mono)}
.card-capability{display:grid;grid-template-columns:auto auto minmax(0,1fr);align-items:center;gap:8px;margin:-6px 0 20px;padding:10px 12px;border:1px solid var(--border);border-radius:9px;background:var(--page-bg)}.card-capability strong{font-size:11px}.card-capability>span{padding:3px 7px;border-radius:99px;background:color-mix(in srgb,var(--accent) 10%,var(--surface));color:var(--accent);font-size:10px}.card-capability small{color:var(--text-tertiary);font-size:10px;line-height:1.4}.card-capability.command_fallback>span,.card-capability.plain_text>span{background:color-mix(in srgb,#b57a18 12%,var(--surface));color:#9b6711}.card-capability.compact{margin:0}
.guide-body li p em{display:block;margin-top:4px;color:var(--text-tertiary);font-size:10px;font-style:normal}.guide-body li p em code{margin-right:4px;padding:1px 5px;border-radius:5px;background:color-mix(in srgb,var(--accent) 10%,var(--surface));color:var(--accent);font:10px/1.5 var(--font-mono)}.guide-next-eyebrow{margin:4px 0 0;padding-top:10px;border-top:1px solid var(--border-subtle);color:var(--text-tertiary);font:700 9px/1.2 var(--font-mono);letter-spacing:.1em}.guide-body ol.guide-next{padding-top:8px}.guide-body ol.guide-next li>span{background:var(--page-bg);color:var(--text-tertiary)}
.callback-preview{display:flex;flex-direction:column;gap:5px;padding:10px 12px;border:1px solid var(--border);border-radius:9px;background:var(--page-bg)}.callback-preview strong{font-size:11px}.callback-preview code{overflow:auto;color:var(--accent);font:11px/1.5 var(--font-mono);white-space:nowrap}.callback-preview small{color:var(--text-tertiary);font-size:10px;line-height:1.45}
.qr-notice{margin:0;padding:10px 12px;border:1px solid color-mix(in srgb,var(--accent) 25%,var(--border));border-radius:9px;background:color-mix(in srgb,var(--accent) 4%,var(--surface));color:var(--text-secondary);font-size:11px;line-height:1.55}
.qr-card{text-align:center;padding:8px 16px 18px}.qr-card p{margin:0 0 16px;color:var(--text-secondary);font-size:13px}.qr-frame{width:200px;height:200px;display:grid;place-items:center;margin:0 auto;padding:10px;box-sizing:border-box;border:1px solid var(--border);border-radius:12px;background:white}.qr-frame img{width:100%;height:100%;object-fit:contain}.qr-frame span{color:var(--text-tertiary);font-size:11px}.qr-frame.stale{opacity:.35}.qr-card small{display:block;margin-top:14px;color:var(--text-tertiary);font-size:11px}.qr-link{display:block;margin-top:10px;padding:8px 10px;border-radius:7px;background:var(--page-bg);color:var(--text-secondary);font:10px/1.5 var(--font-mono);word-break:break-all;user-select:all}.qr-browser-link{display:inline-flex;margin-top:12px;padding:7px 10px;border:1px solid color-mix(in srgb,var(--accent) 30%,var(--border));border-radius:7px;background:var(--surface);color:var(--accent);font-size:11px;cursor:pointer}.qr-browser-link:hover{background:color-mix(in srgb,var(--accent) 8%,var(--surface))}
.pairing-card{text-align:center;padding:8px 16px 20px}.pairing-card p{margin:0 0 18px;color:var(--text-secondary);font-size:13px}.pair-code{font:750 36px/1 var(--font-mono);letter-spacing:.18em;color:var(--accent)}.pairing-card code{display:block;width:max-content;margin:16px auto 10px;padding:8px 11px;border-radius:7px;background:var(--page-bg);font:12px/1.2 var(--font-mono)}.pairing-card small{color:var(--text-tertiary)}
@media(max-width:1280px){.provider-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.identity-row{grid-template-columns:34px minmax(0,1fr) 32px}.session-select,.permission-select{grid-column:2/4}}
@media(max-width:820px){.channel-page{padding:18px}.channel-head{align-items:flex-start;flex-direction:column}.channel-head-actions{width:100%;flex-wrap:wrap}.channel-shell{grid-template-columns:1fr;overflow:auto}.channel-master{max-height:260px;border-right:0;border-bottom:1px solid var(--border)}.channel-detail{overflow:visible}.form-grid.two,.secret-grid{grid-template-columns:1fr}.fact-rows>div{grid-template-columns:1fr;gap:4px}.guide-footer{align-items:flex-start;flex-direction:column}}
</style>
