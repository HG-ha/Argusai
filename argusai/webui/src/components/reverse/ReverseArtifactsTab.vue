<script setup>
import { computed, ref, watch } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { formatBytes } from '../settings/settingsUtils.js'
import { formatArtifactPreview, formatDecompileIndexPreview, isDecompileIndexArtifact, parseDecompileIndex } from './reverseUtils.js'

const props = defineProps({
  jobId: { type: String, default: '' },
  artifacts: { type: Array, default: () => [] },
})

const msg = useMessage()
const revealingId = ref('')

const PREVIEW_KINDS = new Set([
  'trace', 'frida_log', 'frida_script', 'manifest', 'plist',
  'capa_report', 'yara_match', 'debug_trace', 'html', 'strings',
])

const selectedId = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const previewContent = ref('')
const previewTruncated = ref(false)
const decompileInfo = ref(null)
const openingDecompileDir = ref(false)

const sortedArtifacts = computed(() => {
  const list = [...(props.artifacts || [])]
  list.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
  return list
})

const selectedArtifact = computed(() => sortedArtifacts.value.find((a) => a.id === selectedId.value) || null)

async function loadPreview(artifact) {
  if (!artifact || !props.jobId) return
  selectedId.value = artifact.id
  if (!PREVIEW_KINDS.has(String(artifact.kind || ''))) {
    previewContent.value = ''
    previewError.value = t('reverse.artifacts.previewUnsupported')
    previewTruncated.value = false
    return
  }
  previewLoading.value = true
  previewError.value = ''
  previewContent.value = ''
  previewTruncated.value = false
  decompileInfo.value = null
  try {
    const res = await api.reverseArtifactRead(props.jobId, artifact.id, 128 * 1024)
    if (!res?.ok) {
      previewError.value = res?.error || t('reverse.artifacts.previewFailed')
      return
    }
    const art = res.artifact || {}
    const raw = art.content || art.text || ''
    if (isDecompileIndexArtifact(artifact)) {
      decompileInfo.value = parseDecompileIndex(raw)
      previewContent.value = formatDecompileIndexPreview(raw)
    } else {
      previewContent.value = formatArtifactPreview(artifact.kind, raw)
    }
    previewTruncated.value = !!art.truncated
  } catch (e) {
    previewError.value = String(e?.message || e)
  } finally {
    previewLoading.value = false
  }
}

watch(() => props.jobId, () => {
  selectedId.value = ''
  previewContent.value = ''
  previewError.value = ''
  previewTruncated.value = false
  decompileInfo.value = null
})

watch(sortedArtifacts, (list) => {
  if (!list.length) {
    selectedId.value = ''
    previewContent.value = ''
    return
  }
  if (!selectedId.value || !list.some((a) => a.id === selectedId.value)) {
    loadPreview(list[0])
  }
}, { immediate: true })

async function revealArtifact(artifact, event) {
  if (event) {
    event.stopPropagation()
  }
  if (!artifact || !artifact.id || !props.jobId) return
  revealingId.value = artifact.id
  try {
    const res = await api.reverseArtifactReveal(props.jobId, artifact.id)
    if (!res || res.ok === false) {
      msg.error((res && res.error) || t('reverse.artifacts.revealFailed'))
    }
  } catch (e) {
    msg.error(String((e && e.message) || e))
  } finally {
    revealingId.value = ''
  }
}

async function openDecompileDir() {
  const dir = String(decompileInfo.value?.output_dir || '').trim()
  if (!dir) return
  openingDecompileDir.value = true
  try {
    const res = await api.openFolder(dir)
    if (!res?.ok) msg.error(res?.error || t('reverse.artifacts.openDecompileFailed'))
  } catch (e) {
    msg.error(String(e?.message || e))
  } finally {
    openingDecompileDir.value = false
  }
}

</script>

<template>
  <div class="rev-artifacts">
    <div v-if="!sortedArtifacts.length" class="rev-muted">{{ t('reverse.empty.artifacts') }}</div>
    <template v-else>
      <div class="rev-artifacts-layout">
        <ul class="rev-artifact-rows">
          <li
            v-for="a in sortedArtifacts"
            :key="a.id"
            class="rev-artifact-row"
            :class="{ active: selectedId === a.id }"
            tabindex="0"
            role="option"
            :aria-selected="selectedId === a.id"
            @click="loadPreview(a)"
            @keydown.enter="loadPreview(a)"
          >
            <div class="rev-artifact-row-main">
              <span class="rev-artifact-name" :title="a.filename || a.id">{{ a.filename || a.id }}</span>
              <span class="rev-pill">{{ a.kind }}</span>
            </div>
            <div class="rev-artifact-row-meta">
              <span>{{ formatBytes(a.size) }}</span>
              <span v-if="a.tool || a.note">· {{ a.tool || a.note }}</span>
            </div>
          </li>
        </ul>

        <section class="rev-artifact-preview">
          <div class="rev-card-head">
            <h4>{{ t('reverse.artifacts.preview') }}</h4>
            <span v-if="selectedArtifact" class="rev-muted-inline">{{ selectedArtifact.filename || selectedArtifact.id }}</span>
          </div>
          <div v-if="previewLoading" class="rev-muted">{{ t('reverse.artifacts.loading') }}</div>
          <div v-else-if="previewError" class="rev-inline-error">{{ previewError }}</div>
          <pre v-else-if="previewContent" class="rev-artifact-pre">{{ previewContent }}</pre>
          <div v-else class="rev-muted">{{ t('reverse.artifacts.selectRow') }}</div>
          <p v-if="previewTruncated" class="rev-muted-inline">{{ t('reverse.artifacts.truncated') }}</p>
          <div v-if="selectedArtifact" class="rev-artifact-actions">
            <NButton
              size="tiny"
              :loading="revealingId === selectedArtifact.id"
              :title="t('reverse.artifacts.revealTip')"
              @click="revealArtifact(selectedArtifact)"
            >
              {{ t('reverse.artifacts.reveal') }}
            </NButton>
            <NButton
              v-if="decompileInfo?.output_dir"
              size="tiny"
              :loading="openingDecompileDir"
              :title="t('reverse.artifacts.openDecompileTip')"
              @click="openDecompileDir"
            >
              {{ t('reverse.artifacts.openDecompile') }}
            </NButton>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.rev-artifacts { display: flex; flex-direction: column; gap: 10px; }
.rev-artifacts-layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(260px, 1fr);
  gap: 12px;
  align-items: start;
}
@media (max-width: 900px) {
  .rev-artifacts-layout { grid-template-columns: 1fr; }
}
.rev-artifact-rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 420px;
  overflow-y: auto;
}
.rev-artifact-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease;
}
.rev-artifact-row:hover {
  background: color-mix(in srgb, var(--fg, #fff) 4%, transparent);
}
.rev-artifact-row:focus-visible {
  outline: 2px solid var(--accent, #4e8cff);
  outline-offset: -2px;
}
.rev-artifact-row.active {
  background: color-mix(in srgb, var(--accent, #4e8cff) 10%, transparent);
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 28%, transparent);
}
.rev-artifact-row-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.rev-artifact-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--fg-strong, inherit);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.rev-artifact-row-meta {
  display: flex;
  gap: 4px;
  font-size: 10.5px;
  color: var(--muted, #888);
}
.rev-artifact-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.rev-artifact-preview {
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.2));
  background: color-mix(in srgb, var(--fg, #fff) 2%, transparent);
  min-height: 180px;
}
.rev-artifact-preview h4 {
  margin: 0;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  color: var(--muted, #888);
}
.rev-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.rev-artifact-pre {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  padding: 10px;
  border-radius: 8px;
  font-size: 11px;
  line-height: 1.45;
  background: color-mix(in srgb, var(--fg, #fff) 4%, transparent);
  white-space: pre-wrap;
  word-break: break-word;
}
.rev-muted { color: var(--muted, #888); font-size: 12px; padding: 8px 0; }
.rev-muted-inline { color: var(--muted, #888); font-size: 11px; }
.rev-inline-error { color: var(--danger, #f55); font-size: 11px; margin: 4px 0; }
.rev-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 14%, transparent);
  color: var(--accent, #4e8cff);
}
</style>
