<script setup>
// Decoded / decrypted views of one body part: zero-config decode results,
// decryption-rule output, and ciphertext the decoder could not open.
import { computed } from 'vue'
import { t } from '../../i18n.js'
import { formatJson } from './bodyFormat.js'
import { formatSize } from './proxyFormat.js'

const props = defineProps({
  part: { type: Object, default: null },
  wrap: { type: Boolean, default: true },
  ruleActions: { type: Boolean, default: true },
})
const emit = defineEmits(['copy', 'create-rule'])

const items = computed(() => {
  const list = Array.isArray(props.part?.items) ? props.part.items : []
  // Rule output first: it is what the user configured; then plaintext, then hints.
  const rank = (i) => (i.source === 'rule' ? 0 : i.text ? 1 : 2)
  return list
    .map((item, idx) => ({
      ...item,
      key: `${item.source}-${item.rule_id || ''}-${item.location}-${idx}`,
      html: highlighted(item),
      steps: chain(item),
    }))
    .sort((a, b) => rank(a) - rank(b))
})

function locationLabel(loc) {
  return !loc || loc === '<body>' ? t('proxy.decode.wholeBody') : loc
}

function chain(item) {
  if (item.source === 'rule') {
    return (item.trace || []).map((s) => ({ op: s.label || s.op, ok: s.ok !== false }))
  }
  return (item.layers || []).filter((l) => l.op !== 'utf8').map((l) => ({ op: l.op, ok: true }))
}

function highlighted(item) {
  if (item.kind !== 'json' || !item.text) return null
  const view = formatJson(item.text)
  return view.ok ? view.html : null
}

function hexPreview(hex) {
  const pairs = String(hex || '').match(/.{1,2}/g) || []
  const lines = []
  for (let i = 0; i < pairs.length; i += 16) lines.push(pairs.slice(i, i + 16).join(' '))
  return lines.join('\n')
}

function hintLabel(hint) {
  const key = `proxy.decode.hint.${hint}`
  const label = t(key)
  return label === key ? hint : label
}
</script>

<template>
  <div class="pd">
    <article
      v-for="item in items"
      :key="item.key"
      class="pd-item"
      :class="{ 'is-error': item.error, 'is-cipher': item.cipher_hint && !item.text }"
    >
      <header class="pd-head">
        <span class="pd-source" :class="`is-${item.source}`">
          {{ item.source === 'rule' ? t('proxy.decode.sourceRule', { name: item.rule_name || item.rule_id }) : t('proxy.decode.sourceAuto') }}
        </span>
        <code class="pd-loc mono" :title="item.location">{{ locationLabel(item.location) }}</code>
        <span v-if="item.steps.length" class="pd-chain mono">
          <template v-for="(s, idx) in item.steps" :key="idx">
            <span v-if="idx" class="pd-arrow">→</span>
            <span :class="{ bad: !s.ok }">{{ s.op }}</span>
          </template>
        </span>
        <span class="pd-actions">
          <button v-if="item.text" type="button" class="ghost tiny" @click="emit('copy', item.text)">{{ t('proxy.copyContent') }}</button>
          <button v-if="ruleActions && (item.cipher_hint || item.error)" type="button" class="ghost tiny" @click="emit('create-rule', item)">
            {{ item.source === 'rule' ? t('proxy.decode.editRule') : t('proxy.decode.createRule') }}
          </button>
        </span>
      </header>
      <p v-if="item.source === 'auto' && item.from && !item.text" class="pd-from mono">{{ item.from }}</p>
      <p v-if="item.error" class="pd-error">{{ item.error }}</p>
      <div v-if="item.cipher_hint && !item.text" class="pd-cipher">
        <strong>{{ hintLabel(item.cipher_hint) }}</strong>
        <span class="mono">{{ formatSize(item.size) }} · {{ t('proxy.decode.entropy', { v: item.entropy }) }}</span>
      </div>
      <!-- formatJson HTML-escapes every token before it reaches v-html. -->
      <pre v-if="item.html" class="pd-text pd-json selectable" :class="{ nowrap: !wrap }" v-html="item.html"></pre>
      <pre v-else-if="item.text" class="pd-text selectable" :class="{ nowrap: !wrap }">{{ item.text }}</pre>
      <pre v-else-if="item.hex_preview" class="pd-text pd-hex selectable">{{ hexPreview(item.hex_preview) }}</pre>
      <p v-if="item.truncated" class="pd-note">{{ t('proxy.decode.truncated') }}</p>
    </article>
  </div>
</template>

<style scoped>
.pd {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 12px 14px;
}
.pd-item {
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
  overflow: hidden;
}
.pd-item.is-error { border-color: color-mix(in srgb, var(--err) 40%, var(--border)); }
.pd-item.is-cipher { border-style: dashed; }
.pd-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
}
.pd-source {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font-weight: 600;
}
.pd-source.is-rule {
  background: color-mix(in srgb, var(--ok) 15%, transparent);
  color: var(--ok);
}
.pd-loc {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg-strong);
}
.pd-chain { color: var(--muted); }
.pd-chain .bad { color: var(--err); text-decoration: line-through; }
.pd-arrow { margin: 0 4px; color: var(--muted-2); }
.pd-actions {
  display: inline-flex;
  gap: 4px;
  margin-left: auto;
}
.pd-from {
  margin: 0;
  padding: 6px 10px 0;
  overflow: hidden;
  color: var(--muted-2);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pd-error {
  margin: 0;
  padding: 7px 10px;
  color: var(--err);
  font-size: 12px;
  line-height: 1.5;
}
.pd-cipher {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  padding: 8px 10px;
  font-size: 12px;
}
.pd-cipher strong { color: var(--warn); font-weight: 600; }
.pd-cipher span { color: var(--muted); font-size: 11px; }
.pd-text {
  max-height: 420px;
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  color: var(--fg);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
}
.pd-text.nowrap { white-space: pre; word-break: normal; }
.pd-hex { color: var(--muted); white-space: pre; }
.pd-json :deep(.j-k) { color: var(--syntax-builtin); }
.pd-json :deep(.j-s) { color: var(--syntax-string); }
.pd-json :deep(.j-n) { color: var(--syntax-number); }
.pd-json :deep(.j-l) { color: var(--syntax-keyword); }
.pd-json :deep(.j-p) { color: var(--muted-2); }
.pd-note {
  margin: 0;
  padding: 0 10px 8px;
  color: var(--muted-2);
  font-size: 11px;
}
.selectable {
  user-select: text;
  -webkit-user-select: text;
  cursor: text;
}
</style>
