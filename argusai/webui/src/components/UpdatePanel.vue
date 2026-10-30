<script setup>
import { computed, h, onMounted } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { state } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import { useUpdates } from '../composables/useUpdates.js'

const { updateState: update, progress, checkForUpdates, downloadUpdate, cancelUpdateDownload,
  installUpdate, refreshUpdateStatus, setAutoCheck } = useUpdates()
const confirm = useConfirm()
const busy = computed(() => ['checking', 'downloading', 'installing'].includes(update.status))
const hasUpdate = computed(() => ['available', 'downloading', 'downloaded', 'installing'].includes(update.status))
const canRetryDownload = computed(() => update.status === 'error' && update.install_mode !== 'source'
  && !!update.version && Number(update.total_bytes) > 0)
const statusText = computed(() => t(`updates.status.${update.status}`, { version: update.version }))
const errorText = computed(() => ['app_busy', 'busy', 'active_tasks'].includes(update.error_code)
  ? t('updates.busyError') : update.error)
const noteBlocks = computed(() => {
  const blocks = []
  let paragraph = null
  for (const line of String(update.notes || '').split(/\r?\n/)) {
    if (!line.trim()) { paragraph = null; continue }
    const heading = line.match(/^#{1,6}\s+(.+)$/)
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/)
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (heading) { blocks.push({ kind: 'heading', text: heading[1] }); paragraph = null; continue }
    if (bullet || numbered) {
      const kind = numbered ? 'ordered' : 'unordered'
      const previous = blocks[blocks.length - 1]
      if (previous?.kind === kind) previous.items.push((numbered || bullet)[1])
      else blocks.push({ kind, items: [(numbered || bullet)[1]] })
      paragraph = null
      continue
    }
    if (paragraph) paragraph.text += `\n${line}`
    else { paragraph = { kind: 'paragraph', text: line }; blocks.push(paragraph) }
  }
  return blocks
})

// Release notes only need headings, lists, and links. Build text VNodes rather
// than HTML, so raw tags and unsupported Markdown stay inert and visible.
const ReleaseLine = ({ text }) => {
  const source = String(text || '')
  const children = []
  const links = /\[([^\]\r\n]+)\]\((https?:\/\/[^\s)]+)\)/gi
  let offset = 0
  for (const match of source.matchAll(links)) {
    children.push(source.slice(offset, match.index))
    let url = null
    try {
      const parsed = new URL(match[2])
      if (['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password) url = parsed.href
    } catch (_) {}
    children.push(url ? h('a', {
      href: url, target: '_blank', rel: 'noopener noreferrer',
      onClick: event => {
        event.preventDefault()
        api.openExternalUrl(url).catch(() => {})
      },
    }, match[1]) : match[0])
    offset = match.index + match[0].length
  }
  children.push(source.slice(offset))
  return h('span', children)
}

function bytes(value) {
  const size = Math.max(0, Number(value) || 0)
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`
}
async function openRelease() {
  const url = update.release_url
  if (url && /^https:\/\/github\.com\/HG-ha\/Argusai\/releases\/tag\/v[0-9]/.test(url)) {
    try { await api.openExternalUrl(url) } catch (_) {}
  }
}
async function confirmInstall() {
  if (update.status !== 'downloaded' || update.install_mode !== 'automatic') return
  const accepted = await confirm({
    title: t('updates.confirmTitle', { version: update.version }),
    content: t('updates.confirmBody'),
    positiveText: t('updates.installRestart'),
    negativeText: t('updates.later'),
    type: 'info',
  })
  if (accepted) await installUpdate()
}
async function openFolder() {
  try {
    const result = await api.openUpdateFolder()
    if (result?.error) update.error = result.error
  } catch (error) { update.error = error?.message || t('updates.folderFailed') }
}
async function retryDownload() {
  // Revalidate the release before reusing metadata from a failed download.
  await checkForUpdates(true)
  if (update.status === 'available' && update.install_mode !== 'source') await downloadUpdate()
}
onMounted(() => refreshUpdateStatus())
</script>

<template>
  <section class="update-panel" aria-labelledby="app-update-heading">
    <div class="update-heading">
      <div>
        <h2 id="app-update-heading">{{ t('updates.title') }}</h2>
        <p>{{ t('updates.currentVersion', { version: update.current_version || state.version || '—' }) }}</p>
      </div>
      <button class="ghost" type="button" :disabled="busy" @click="checkForUpdates(true)">
        {{ update.status === 'checking' ? t('updates.checking') : t('updates.check') }}
      </button>
    </div>
    <p class="update-status" role="status" aria-live="polite">{{ statusText }}</p>
    <p v-if="hasUpdate && update.install_mode === 'source'" class="update-hint">{{ t('updates.sourceHint') }}</p>
    <p v-else-if="hasUpdate && update.install_mode === 'manual'" class="update-hint">{{ t('updates.manualHint') }}</p>
    <div v-if="update.status === 'downloading'" class="update-progress">
      <progress :value="progress" max="100" :aria-label="t('updates.downloadProgress')"></progress>
      <span>{{ bytes(update.downloaded_bytes) }} / {{ bytes(update.total_bytes) }} · {{ Math.floor(progress) }}%</span>
    </div>
    <p v-if="errorText" class="update-error" role="alert">{{ errorText }}</p>
    <div v-if="hasUpdate || canRetryDownload || update.release_url" class="update-actions">
      <button v-if="update.status === 'available' && update.install_mode !== 'source'" class="primary" type="button" @click="downloadUpdate">
        {{ t('updates.download') }}
      </button>
      <button v-if="canRetryDownload" class="primary" type="button" @click="retryDownload">{{ t('updates.retryDownload') }}</button>
      <button v-if="update.status === 'downloading'" class="ghost" type="button" @click="cancelUpdateDownload">{{ t('updates.cancelDownload') }}</button>
      <button v-if="update.status === 'downloaded' && update.install_mode === 'automatic'" class="primary" type="button" @click="confirmInstall">{{ t('updates.installRestart') }}</button>
      <button v-if="update.status === 'downloaded'" class="ghost" type="button" @click="openFolder">{{ t('updates.openFolder') }}</button>
      <button v-if="update.release_url" class="ghost" type="button" @click="openRelease">{{ t('updates.releasePage') }}</button>
    </div>
    <details v-if="hasUpdate && update.notes" class="update-notes" open>
      <summary>{{ t('updates.releaseNotes') }}</summary>
      <div class="update-note-body">
        <template v-for="(block, index) in noteBlocks" :key="index">
          <h3 v-if="block.kind === 'heading'"><ReleaseLine :text="block.text" /></h3>
          <component :is="block.kind === 'ordered' ? 'ol' : 'ul'" v-else-if="block.items">
            <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><ReleaseLine :text="item" /></li>
          </component>
          <p v-else><ReleaseLine :text="block.text" /></p>
        </template>
      </div>
    </details>
    <label class="update-preference">
      <input type="checkbox" :checked="update.autoCheck" @change="setAutoCheck($event.target.checked)">
      <span>{{ t('updates.autoCheck') }}</span>
    </label>
    <p class="update-hint">{{ t('updates.behaviorHint') }}</p>
  </section>
</template>

<style scoped>
.update-panel { margin: 0 0 30px; padding: 22px 24px; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); }
.update-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.update-heading h2 { margin: 0 0 5px; color: var(--fg-strong); font-size: 20px; }
.update-heading p, .update-hint { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.65; }
.update-status { margin: 16px 0 8px; font-size: 14px; color: var(--fg-strong); }
.update-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.update-error { margin: 10px 0; color: var(--danger, #dc5353); font-size: 13px; overflow-wrap: anywhere; }
.update-notes { margin: 16px 0; border-top: 1px solid var(--border); padding-top: 14px; }
.update-notes summary { cursor: pointer; color: var(--fg-strong); font-size: 13px; }
.update-note-body { max-height: 260px; overflow: auto; overflow-wrap: anywhere; margin-top: 10px; font-size: 13px; line-height: 1.7; }
.update-note-body h3 { margin: 0 0 8px; color: var(--fg-strong); font-size: 15px; font-weight: 600; }
.update-note-body p { white-space: pre-wrap; margin: 0 0 10px; }
.update-note-body ul, .update-note-body ol { margin: 0 0 10px; padding-left: 22px; }
.update-note-body li { padding-left: 2px; margin: 3px 0; }
.update-note-body :deep(a) { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
.update-progress { display: grid; gap: 6px; margin: 14px 0; }
.update-progress progress { appearance: none; width: 100%; height: 7px; border: 0; border-radius: 5px; overflow: hidden; background: var(--panel-2); accent-color: var(--accent); }
.update-progress progress::-webkit-progress-bar { background: var(--panel-2); }
.update-progress progress::-webkit-progress-value { background: var(--accent); border-radius: 5px; }
.update-progress span { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.update-preference { display: flex; align-items: center; gap: 8px; margin: 18px 0 5px; font-size: 13px; color: var(--fg); }
.update-preference input { accent-color: var(--accent); }
@media (max-width: 620px) { .update-panel { padding: 18px; } .update-heading { flex-wrap: wrap; align-items: flex-start; } }
</style>
