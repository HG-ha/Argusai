<script setup>
import { computed, ref } from 'vue'
import { NButton, NInput } from 'naive-ui'
import { api } from '../../api.js'
import { t } from '../../i18n.js'

/**
 * Unified renderer for a single "storage directory" setting.
 *
 * Provides a consistent three-part layout for every path the user can
 * configure (tasks / sessions / poc pages / knowledge base):
 *
 *   1. Label + (optional) hint line above the field.
 *   2. Input row with three actions:
 *        - "打开目录" — opens the resolved path in the OS file manager
 *          (no-op if the path does not exist yet, with a small inline
 *           notice rather than throwing).
 *        - "恢复默认" — clears the input back to the empty string. The
 *          backend treats empty as "use the canonical default", and we
 *          additionally surface the default path as placeholder.
 *   3. Resolved/effective path read-out beneath the field — shows the
 *      backend-supplied default when the user blanks the field, so it
 *      is always clear what the runtime will actually use.
 *
 * Props are designed to be model-bound (``v-model:value``); the parent
 * settings page owns the underlying string and gets reactivity for free.
 */
const props = defineProps({
  value: { type: String, default: '' },
  // Visible label, e.g. t('settings.storage.tasksDir')
  label: { type: String, required: true },
  // Optional descriptive text shown beneath the input row.
  hint: { type: String, default: '' },
  // Effective default path supplied by the backend; shown as placeholder
  // AND surfaced in the read-out when the user empties the field.
  defaultPath: { type: String, default: '' },
  // Identifier-only — used by the parent in toasts ("knowledge_base_dir")
  fieldKey: { type: String, default: '' },
  pickable: { type: Boolean, default: false },
})

const emit = defineEmits(['update:value', 'directory-picked'])

const localValue = computed({
  get: () => props.value,
  set: (v) => emit('update:value', v),
})

const effectivePath = computed(() => {
  const v = String(props.value || '').trim()
  return v || props.defaultPath || ''
})

const isUsingDefault = computed(() => !String(props.value || '').trim())

const opening = ref(false)
const picking = ref(false)
const openError = ref('')

async function openInExplorer() {
  const path = effectivePath.value
  if (!path) return
  opening.value = true
  openError.value = ''
  try {
    const res = await api.openFolder(path)
    if (!res || res.ok === false) {
      openError.value = (res && res.error) || t('settings.storage.openFailed')
      setTimeout(() => { openError.value = '' }, 3000)
    }
  } catch (e) {
    openError.value = String((e && e.message) || e)
    setTimeout(() => { openError.value = '' }, 3000)
  } finally {
    opening.value = false
  }
}

function restoreDefault() {
  emit('update:value', '')
}

async function chooseDirectory() {
  if (picking.value) return
  picking.value = true
  openError.value = ''
  try {
    const res = await api.chooseDataRootDirectory(effectivePath.value)
    if (res?.cancelled) return
    if (!res?.ok || !res.path) {
      openError.value = res?.error || t('settings.storage.chooseFailed')
      setTimeout(() => { openError.value = '' }, 3000)
      return
    }
    emit('update:value', res.path)
    emit('directory-picked', res.path)
  } catch (e) {
    openError.value = String((e && e.message) || e)
    setTimeout(() => { openError.value = '' }, 3000)
  } finally {
    picking.value = false
  }
}
</script>

<template>
  <div class="storage-field">
    <div class="storage-field-head">
      <label class="storage-field-label">{{ label }}</label>
      <div class="storage-field-actions">
        <NButton
          v-if="pickable"
          size="tiny"
          quaternary
          :loading="picking"
          :disabled="picking"
          :title="t('settings.storage.chooseTooltip')"
          @click="chooseDirectory"
        >{{ t('settings.storage.chooseButton') }}</NButton>
        <NButton
          size="tiny"
          quaternary
          :disabled="!effectivePath || opening"
          :loading="opening"
          :title="t('settings.storage.openTooltip')"
          @click="openInExplorer"
        >{{ t('settings.storage.openButton') }}</NButton>
        <NButton
          size="tiny"
          quaternary
          :disabled="isUsingDefault"
          :title="t('settings.storage.restoreTooltip')"
          @click="restoreDefault"
        >{{ t('settings.storage.restoreButton') }}</NButton>
      </div>
    </div>
    <NInput
      v-model:value="localValue"
      :placeholder="defaultPath || t('settings.storage.defaultPlaceholder')"
      size="small"
    />
    <div class="storage-field-meta">
      <code
        class="storage-field-effective"
        :class="{ 'using-default': isUsingDefault }"
        :title="effectivePath"
      >{{ effectivePath || '—' }}</code>
      <span v-if="isUsingDefault" class="storage-field-badge">{{ t('settings.storage.usingDefault') }}</span>
    </div>
    <div v-if="hint" class="storage-field-hint">{{ hint }}</div>
    <div v-if="openError" class="storage-field-error">{{ openError }}</div>
  </div>
</template>

<style scoped>
.storage-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.2));
}
.storage-field:last-child {
  border-bottom: 0;
}
.storage-field-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.storage-field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg, inherit);
  margin: 0;
}
.storage-field-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.storage-field-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--muted, #888);
  min-height: 18px;
}
.storage-field-effective {
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--fg, #fff) 6%, transparent);
  color: var(--fg, inherit);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
  flex: 1 1 auto;
  min-width: 0;
}
.storage-field-effective.using-default {
  opacity: 0.65;
}
.storage-field-badge {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  padding: 1px 6px;
  border-radius: 2px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 16%, transparent);
  color: color-mix(in srgb, var(--accent, #4e8cff) 80%, var(--fg, #fff));
  flex-shrink: 0;
}
.storage-field-hint {
  font-size: 11.5px;
  color: var(--muted, #888);
  line-height: 1.5;
}
.storage-field-error {
  font-size: 11.5px;
  color: var(--danger, #f55);
}
</style>
