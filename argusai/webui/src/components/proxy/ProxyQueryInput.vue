<script setup>
// Field-style traffic filter (host:/re/ status:4xx resp.body:/token/ …),
// evaluated by the backend. Shows the parse error with a caret and a
// clickable cheat sheet.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { t } from '../../i18n.js'

const props = defineProps({
  modelValue: { type: String, default: '' },
  error: { type: String, default: '' },
  position: { type: Number, default: -1 },
  running: { type: Boolean, default: false },
  timedOut: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'apply'])

const EXAMPLES = [
  { q: 'host:/^api\\./ method:POST', key: 'apiPost' },
  { q: 'status:4xx,5xx', key: 'errors' },
  { q: 'resp.body:/"(access_)?token"\\s*:/', key: 'token' },
  { q: 'req.header:authorization=/^Bearer /', key: 'bearer' },
  { q: 'url:/[?&](uid|user_?id)=\\d+/i', key: 'idor' },
  { q: 'size:>500k -type:img,media', key: 'large' },
  { q: 'has:decrypted dec.resp:/"phone":/', key: 'decrypted' },
  { q: '(path:/login OR path:/oauth) -host:*.cdn.*', key: 'login' },
]
const FIELDS = [
  'url', 'host', 'path', 'method', 'status', 'type', 'ct', 'size', 'duration',
  'header', 'req.header', 'resp.header', 'body', 'req.body', 'resp.body',
  'dec', 'dec.resp', 'finding', 'has', 'tls',
]

const rootEl = ref(null)
const inputEl = ref(null)
const helpOpen = ref(false)

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
const showError = computed(() => Boolean(props.error && props.modelValue.trim()))
const caret = computed(() => {
  const src = props.modelValue
  const pos = Math.max(0, Math.min(props.position, src.length))
  if (props.position < 0 || !src) return null
  return { before: src.slice(0, pos), at: src.slice(pos, pos + 1) || ' ', after: src.slice(pos + 1) }
})

function onKeydown(e) {
  if (e.key === 'Enter') {
    e.preventDefault()
    emit('apply', props.modelValue)
  } else if (e.key === 'Escape') {
    if (helpOpen.value) helpOpen.value = false
    else if (props.modelValue) value.value = ''
  }
}

function useExample(q) {
  value.value = q
  emit('apply', q)
  helpOpen.value = false
  inputEl.value?.focus()
}

function insertField(name) {
  const src = props.modelValue
  value.value = `${src}${src && !/\s$/.test(src) ? ' ' : ''}${name}:`
  inputEl.value?.focus()
}

function onDocPointer(e) {
  if (rootEl.value && !rootEl.value.contains(e.target)) helpOpen.value = false
}
function toggleHelp() {
  helpOpen.value = !helpOpen.value
}
watch(helpOpen, (open) => {
  if (open) document.addEventListener('pointerdown', onDocPointer, true)
  else document.removeEventListener('pointerdown', onDocPointer, true)
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocPointer, true))

defineExpose({ focus: () => inputEl.value?.focus() })
</script>

<template>
  <div ref="rootEl" class="pq" :class="{ 'has-error': showError, 'is-running': running }">
    <div class="pq-box">
      <svg class="pq-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
        <path d="M2 3h12l-4.6 5.4V13l-2.8-1.4V8.4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
      </svg>
      <input
        ref="inputEl"
        v-model="value"
        type="text"
        class="pq-input mono"
        spellcheck="false"
        autocomplete="off"
        :placeholder="t('proxy.query.placeholder')"
        :aria-label="t('proxy.query.aria')"
        :aria-invalid="showError"
        @keydown="onKeydown"
      />
      <span v-if="running" class="pq-spin" aria-hidden="true"></span>
      <span v-else-if="timedOut && !showError" class="pq-timeout" :title="t('proxy.query.timedOut')">!</span>
      <button
        v-if="modelValue"
        type="button"
        class="pq-btn"
        :title="t('proxy.query.clear')"
        :aria-label="t('proxy.query.clear')"
        @click="value = ''; emit('apply', '')"
      >×</button>
      <button
        type="button"
        class="pq-btn pq-help-btn"
        :class="{ on: helpOpen }"
        :aria-expanded="helpOpen"
        :title="t('proxy.query.help')"
        @click="toggleHelp"
      >?</button>
    </div>
    <div v-if="showError && !helpOpen" class="pq-error" role="alert">
      <code v-if="caret" class="pq-caret mono">{{ caret.before }}<mark>{{ caret.at }}</mark>{{ caret.after }}</code>
      <span>{{ error }}</span>
    </div>
    <div v-if="helpOpen" class="pq-help" role="dialog" :aria-label="t('proxy.query.help')">
      <p class="pq-lead">{{ t('proxy.query.helpLead') }}</p>
      <div class="pq-help-grid">
        <section>
          <h4>{{ t('proxy.query.grammar') }}</h4>
          <dl class="pq-rules">
            <dt><code>a b</code></dt><dd>{{ t('proxy.query.ruleAnd') }}</dd>
            <dt><code>a OR b</code></dt><dd>{{ t('proxy.query.ruleOr') }}</dd>
            <dt><code>-a</code></dt><dd>{{ t('proxy.query.ruleNot') }}</dd>
            <dt><code>( … )</code></dt><dd>{{ t('proxy.query.ruleGroup') }}</dd>
            <dt><code>host:/re/i</code></dt><dd>{{ t('proxy.query.ruleRegex') }}</dd>
            <dt><code>method:GET,POST</code></dt><dd>{{ t('proxy.query.ruleAny') }}</dd>
            <dt><code>host:*.x.com</code></dt><dd>{{ t('proxy.query.ruleGlob') }}</dd>
            <dt><code>status:&gt;=400</code></dt><dd>{{ t('proxy.query.ruleCompare') }}</dd>
          </dl>
          <h4>{{ t('proxy.query.fields') }}</h4>
          <div class="pq-fields">
            <button v-for="f in FIELDS" :key="f" type="button" class="pq-field mono" @click="insertField(f)">{{ f }}:</button>
          </div>
        </section>
        <section>
          <h4>{{ t('proxy.query.examples') }}</h4>
          <button v-for="ex in EXAMPLES" :key="ex.key" type="button" class="pq-example" @click="useExample(ex.q)">
            <code class="mono">{{ ex.q }}</code>
            <span>{{ t(`proxy.query.ex.${ex.key}`) }}</span>
          </button>
        </section>
      </div>
      <p class="pq-foot">{{ t('proxy.query.helpFoot') }}</p>
    </div>
  </div>
</template>

<style scoped>
.pq {
  position: relative;
  flex: 1 1 260px;
  min-width: 220px;
  max-width: 520px;
}
.pq-box {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 4px 0 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--input-bg);
  transition: border-color 0.12s, box-shadow 0.12s;
}
.pq-box:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.pq.has-error .pq-box {
  border-color: color-mix(in srgb, var(--err) 65%, var(--border));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--err) 14%, transparent);
}
.pq-icon { flex: 0 0 auto; color: var(--muted-2); }
.pq-input {
  flex: 1 1 auto;
  min-width: 0;
  height: 100%;
  padding: 0 !important;
  border: 0 !important;
  outline: none !important;
  background: transparent !important;
  box-shadow: none !important;
  font-size: 12px !important;
}
.pq-btn {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
}
.pq-btn:hover, .pq-help-btn.on { background: var(--panel-hover); color: var(--fg-strong); }
.pq-help-btn { font-weight: 600; font-size: 12px; }
.pq-spin {
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: pq-spin 0.7s linear infinite;
}
@keyframes pq-spin { to { transform: rotate(360deg); } }
.pq-timeout {
  display: inline-grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--warn) 22%, transparent);
  color: var(--warn);
  font-size: 11px;
  font-weight: 700;
}
.pq-error,
.pq-help {
  position: absolute;
  z-index: 30;
  top: calc(100% + 6px);
  left: 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--input-bg);
  box-shadow: var(--shadow-md);
}
.pq-error {
  display: grid;
  gap: 6px;
  width: max(100%, 360px);
  padding: 9px 11px;
  border-color: color-mix(in srgb, var(--err) 45%, var(--border));
  color: var(--err);
  font-size: 12px;
}
.pq-caret {
  overflow-x: auto;
  color: var(--fg);
  font-size: 11.5px;
  white-space: pre;
}
.pq-caret mark {
  border-radius: 2px;
  background: color-mix(in srgb, var(--err) 35%, transparent);
  color: var(--fg-strong);
  text-decoration: underline wavy var(--err);
}
.pq-help {
  width: min(720px, calc(100vw - 48px));
  max-height: min(70vh, 560px);
  overflow-y: auto;
  padding: 14px 16px 12px;
  color: var(--fg);
  font-size: 12px;
}
.pq-lead { margin: 0 0 10px; color: var(--muted); }
.pq-help-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
  gap: 18px;
}
.pq-help h4 {
  margin: 0 0 7px;
  color: var(--muted-2);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.pq-help h4 + * { margin-bottom: 14px; }
.pq-rules {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 5px 10px;
  margin: 0;
}
.pq-rules dt { margin: 0; }
.pq-rules dd { margin: 0; color: var(--muted); }
.pq-help code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--panel-2);
  color: var(--fg-strong);
  font-family: var(--mono);
  font-size: 11.5px;
}
.pq-fields { display: flex; flex-wrap: wrap; gap: 4px; }
.pq-field {
  height: 22px;
  padding: 0 7px;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font-size: 11.5px;
}
.pq-field:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--border)); color: var(--fg-strong); }
.pq-example {
  display: grid;
  gap: 3px;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  text-align: left;
}
.pq-example + .pq-example { margin-top: 2px; }
.pq-example:hover { background: var(--panel-hover); }
.pq-example code { justify-self: start; word-break: break-all; }
.pq-example span { color: var(--muted); font-size: 11.5px; }
.pq-foot {
  margin: 10px 0 0;
  padding-top: 9px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 11.5px;
}
@media (max-width: 760px) {
  .pq-help-grid { grid-template-columns: 1fr; }
}
</style>
