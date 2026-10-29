<script setup>
import { ref } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  placeholder: { type: String, default: '' },
  /** Quick-add values shown under the field. */
  suggestions: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: '' },
  removeLabel: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const draft = ref('')
const inputEl = ref(null)

function splitValues(text) {
  return String(text || '').split(/[\s,;，；]+/).map((s) => s.trim()).filter(Boolean)
}

function add(values) {
  const next = [...props.modelValue]
  for (const value of values) {
    if (!next.includes(value)) next.push(value)
  }
  if (next.length !== props.modelValue.length) emit('update:modelValue', next)
}

function commit() {
  const values = splitValues(draft.value)
  draft.value = ''
  if (values.length) add(values)
}

function remove(value) {
  emit('update:modelValue', props.modelValue.filter((v) => v !== value))
}

function onKeydown(e) {
  if (e.key === 'Enter' || e.key === ',' || e.key === '，' || e.key === ' ') {
    if (draft.value.trim()) {
      e.preventDefault()
      commit()
    } else if (e.key !== 'Enter') {
      e.preventDefault()
    }
    return
  }
  if (e.key === 'Backspace' && !draft.value && props.modelValue.length) {
    remove(props.modelValue[props.modelValue.length - 1])
  }
}

function onPaste(e) {
  const text = e.clipboardData?.getData('text') || ''
  if (!/[\s,;，；]/.test(text)) return
  e.preventDefault()
  add(splitValues(`${draft.value} ${text}`))
  draft.value = ''
}
</script>

<template>
  <div class="proxy-tags">
    <div class="proxy-tags-field" @click="inputEl?.focus()">
      <span v-for="value in modelValue" :key="value" class="proxy-tag mono">
        {{ value }}
        <button
          type="button"
          class="proxy-tag-remove"
          :aria-label="`${removeLabel} ${value}`"
          @click.stop="remove(value)"
        >×</button>
      </span>
      <input
        ref="inputEl"
        v-model="draft"
        type="text"
        class="proxy-tags-input"
        :placeholder="modelValue.length ? '' : placeholder"
        :aria-label="ariaLabel || placeholder"
        @keydown="onKeydown"
        @paste="onPaste"
        @blur="commit"
      />
    </div>
    <div v-if="suggestions.length" class="proxy-tags-suggest">
      <button
        v-for="value in suggestions"
        :key="value"
        type="button"
        class="proxy-tags-suggest-btn mono"
        :disabled="modelValue.includes(value)"
        @click="add([value])"
      >+ {{ value }}</button>
    </div>
  </div>
</template>

<style scoped>
.proxy-tags-field {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-height: 38px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--r-control);
  background: var(--input-bg);
  cursor: text;
  transition: border-color 150ms, box-shadow 150ms;
}
.proxy-tags-field:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.proxy-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 2px 4px 2px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--fg-strong);
  font-size: 12px;
  line-height: 1.4;
  word-break: break-all;
}
.proxy-tag-remove {
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  line-height: 1;
}
.proxy-tag-remove:hover:not(:disabled) {
  background: var(--panel-hover);
  color: var(--err);
}
.proxy-tags-input {
  flex: 1 1 120px;
  min-width: 120px;
  width: auto !important;
  padding: 4px 2px !important;
  border: 0 !important;
  background: transparent !important;
  box-shadow: none !important;
}
.proxy-tags-input:focus-visible { outline: none; }
.proxy-tags-suggest {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.proxy-tags-suggest-btn {
  padding: 2px 8px;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 11px;
}
.proxy-tags-suggest-btn:hover:not(:disabled) { color: var(--fg); }
</style>
