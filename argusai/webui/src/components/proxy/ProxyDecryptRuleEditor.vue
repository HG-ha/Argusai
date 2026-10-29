<script setup>
// Editor and live tester for one decryption rule: which flows (match
// expression), where the ciphertext sits (field) and the ordered steps.
import { computed, inject, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import ListSelect from '../list/ListSelect.vue'
import ProxyDecodedPane from './ProxyDecodedPane.vue'
import { flowPath } from './proxyFormat.js'
import {
  DECODE_CODECS,
  DECOMPRESS_ALGOS,
  IV_FROM_PREFIX,
  KEY_FORMATS,
  RSA_PADDINGS,
  STEP_OPS,
  SYM_ALGOS,
  cleanRule,
  newStep,
  symNeedsIv,
} from './decryptFormat.js'

const MAX_STEPS = 12

const props = defineProps({
  rule: { type: Object, required: true },
  testFlowId: { type: Number, default: null },
})
const emit = defineEmits(['saved', 'cancel'])
const wb = inject('proxyWorkbench')

const form = reactive(JSON.parse(JSON.stringify(props.rule)))
const isNew = computed(() => !props.rule.id)
const saving = ref(false)
const saveError = ref('')

const opts = (list) => list.map((v) => ({ label: v, value: v }))
const opOptions = computed(() => STEP_OPS.map((op) => ({ label: t(`proxy.decrypt.op.${op}`), value: op })))
const keyFormatOptions = opts(KEY_FORMATS)
const paddingOptions = [{ label: 'PKCS#7', value: 'pkcs7' }, { label: t('proxy.decrypt.noPadding'), value: 'none' }]
const APPLY_TO = ['response', 'request', 'both']
const hasScript = computed(() => form.steps.some((s) => s.op === 'script'))

// ---- match expression: live check + candidate flows for the tester ----
const match = reactive({ checking: false, error: '', matched: 0, total: 0, ids: [] })
let matchTimer = null
let matchToken = 0
async function checkMatch() {
  const source = String(form.match || '').trim()
  const token = ++matchToken
  if (!source) {
    Object.assign(match, { checking: false, error: '', matched: 0, total: 0, ids: [] })
    return
  }
  match.checking = true
  try {
    const r = await api.queryProxyTraffic(source)
    if (token !== matchToken) return
    if (r?.ok) Object.assign(match, { error: '', matched: r.matched || 0, total: r.total || 0, ids: r.ids || [] })
    else Object.assign(match, { error: r?.error || t('toast.error'), matched: 0, ids: [] })
  } catch {
    if (token === matchToken) match.error = t('toast.error')
  } finally {
    if (token === matchToken) match.checking = false
  }
}
watch(() => form.match, () => {
  clearTimeout(matchTimer)
  matchTimer = setTimeout(checkMatch, 350)
}, { immediate: true })
onBeforeUnmount(() => clearTimeout(matchTimer))

const testFlowId = ref(props.testFlowId)
const flowOptions = computed(() => {
  const ids = [...match.ids].reverse().slice(0, 60)
  if (props.testFlowId != null && !ids.includes(props.testFlowId)) ids.unshift(props.testFlowId)
  return ids
    .map((id) => wb.flowsById.value.get(id))
    .filter(Boolean)
    .map((f) => ({ value: f.id, label: `#${f.id} ${f.method} ${f.host}${flowPath(f)}`.slice(0, 96) }))
})
watch(flowOptions, (list) => {
  if (!list.some((o) => o.value === testFlowId.value)) testFlowId.value = list[0]?.value ?? null
}, { immediate: true })

// ---- steps ----
function addStep(op) {
  if (form.steps.length < MAX_STEPS) form.steps.push(newStep(op))
}
function removeStep(idx) {
  form.steps.splice(idx, 1)
}
function moveStep(idx, delta) {
  const to = idx + delta
  if (to < 0 || to >= form.steps.length) return
  const [step] = form.steps.splice(idx, 1)
  form.steps.splice(to, 0, step)
}
function changeOp(idx, op) {
  if (form.steps[idx]?.op !== op) form.steps.splice(idx, 1, newStep(op))
}
function setIvFromPrefix(step, on) {
  step.iv = on ? IV_FROM_PREFIX : ''
}

// ---- test & save ----
const testing = ref(false)
const testResult = ref(null)
async function runTest() {
  if (testFlowId.value == null) return
  testing.value = true
  testResult.value = null
  try {
    const draft = cleanRule({ ...form, name: form.name || 'test' })
    testResult.value = await api.testProxyDecryptRule(draft, testFlowId.value)
  } catch {
    testResult.value = { ok: false, error: t('toast.error') }
  } finally {
    testing.value = false
  }
}
const testParts = computed(() => {
  const items = testResult.value?.items || {}
  return ['request', 'response'].filter((p) => items[p]?.length).map((p) => ({ part: p, data: { items: items[p] } }))
})

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const r = await api.saveProxyDecryptRule(cleanRule(form))
    if (r?.ok) {
      showToast(t('proxy.decrypt.saved'), 'ok')
      emit('saved', r.rule)
    } else {
      saveError.value = r?.error || t('toast.error')
    }
  } catch {
    saveError.value = t('toast.error')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="dr-editor">
    <div class="dr-form">
      <div class="dr-row">
        <label class="dr-field dr-grow">
          <span>{{ t('proxy.decrypt.name') }}</span>
          <input v-model="form.name" type="text" maxlength="80" :placeholder="t('proxy.decrypt.namePlaceholder')" />
        </label>
        <div class="dr-field">
          <span>{{ t('proxy.decrypt.applyTo') }}</span>
          <div class="dr-seg" role="radiogroup" :aria-label="t('proxy.decrypt.applyTo')">
            <button
              v-for="v in APPLY_TO"
              :key="v"
              type="button"
              role="radio"
              :aria-checked="form.apply_to === v"
              :class="{ on: form.apply_to === v }"
              @click="form.apply_to = v"
            >{{ t(`proxy.decrypt.apply.${v}`) }}</button>
          </div>
        </div>
      </div>
      <label class="dr-field">
        <span>{{ t('proxy.decrypt.match') }}</span>
        <input
          v-model="form.match"
          type="text"
          class="mono"
          spellcheck="false"
          autocomplete="off"
          :class="{ invalid: match.error }"
          :placeholder="t('proxy.decrypt.matchPlaceholder')"
        />
        <small v-if="match.error" class="dr-bad">{{ match.error }}</small>
        <small v-else-if="String(form.match || '').trim() && !match.checking">{{ t('proxy.decrypt.matchCount', { n: match.matched, total: match.total }) }}</small>
        <small v-else>{{ t('proxy.decrypt.matchHint') }}</small>
      </label>
      <label class="dr-field">
        <span>{{ t('proxy.decrypt.field') }}</span>
        <input v-model="form.field" type="text" class="mono" spellcheck="false" autocomplete="off" :placeholder="t('proxy.decrypt.fieldPlaceholder')" />
        <small>{{ t('proxy.decrypt.fieldHint') }}</small>
      </label>
      <div class="dr-steps">
        <div class="dr-steps-head">
          <span>{{ t('proxy.decrypt.steps') }}</span>
          <small>{{ t('proxy.decrypt.stepsHint') }}</small>
        </div>
        <ol class="dr-step-list">
          <li v-for="(step, idx) in form.steps" :key="idx" class="dr-step">
            <div class="dr-step-head">
              <span class="dr-step-no mono">{{ idx + 1 }}</span>
              <ListSelect :model-value="step.op" :options="opOptions" min-width="132px" @update:model-value="changeOp(idx, $event)" />
              <span class="dr-step-tools">
                <button type="button" class="ghost tiny" :disabled="idx === 0" :aria-label="t('proxy.decrypt.moveUp')" :title="t('proxy.decrypt.moveUp')" @click="moveStep(idx, -1)">↑</button>
                <button type="button" class="ghost tiny" :disabled="idx === form.steps.length - 1" :aria-label="t('proxy.decrypt.moveDown')" :title="t('proxy.decrypt.moveDown')" @click="moveStep(idx, 1)">↓</button>
                <button type="button" class="ghost tiny dr-remove" :aria-label="t('proxy.decrypt.removeStep')" :title="t('proxy.decrypt.removeStep')" @click="removeStep(idx)">×</button>
              </span>
            </div>
            <div v-if="step.op === 'decode'" class="dr-step-body">
              <label class="dr-mini"><span>{{ t('proxy.decrypt.codec') }}</span><ListSelect v-model="step.codec" :options="opts(DECODE_CODECS)" min-width="120px" /></label>
            </div>
            <div v-else-if="step.op === 'decompress'" class="dr-step-body">
              <label class="dr-mini"><span>{{ t('proxy.decrypt.algo') }}</span><ListSelect v-model="step.algo" :options="opts(DECOMPRESS_ALGOS)" min-width="120px" /></label>
            </div>
            <div v-else-if="step.op === 'slice'" class="dr-step-body">
              <label class="dr-mini"><span>{{ t('proxy.decrypt.sliceStart') }}</span><input v-model.number="step.start" type="number" class="mono" /></label>
              <label class="dr-mini"><span>{{ t('proxy.decrypt.sliceEnd') }}</span><input v-model="step.end" type="number" class="mono" :placeholder="t('proxy.decrypt.sliceEndPlaceholder')" /></label>
            </div>
            <div v-else-if="step.op === 'sym'" class="dr-step-body dr-step-grid">
              <label class="dr-mini"><span>{{ t('proxy.decrypt.algo') }}</span><ListSelect v-model="step.algo" :options="opts(SYM_ALGOS)" min-width="120px" /></label>
              <label v-if="/-(cbc|ecb)$/.test(step.algo)" class="dr-mini"><span>{{ t('proxy.decrypt.padding') }}</span><ListSelect v-model="step.padding" :options="paddingOptions" min-width="110px" /></label>
              <div class="dr-mini dr-wide">
                <span>{{ t('proxy.decrypt.key') }}</span>
                <span class="dr-pair">
                  <input
                    v-model="step.key"
                    type="text"
                    class="mono"
                    spellcheck="false"
                    autocomplete="off"
                    :aria-label="t('proxy.decrypt.key')"
                    :placeholder="t('proxy.decrypt.keyPlaceholder')"
                  />
                  <ListSelect v-model="step.key_format" :options="keyFormatOptions" min-width="104px" />
                </span>
              </div>
              <div v-if="symNeedsIv(step.algo)" class="dr-mini dr-wide">
                <span>{{ step.algo.endsWith('-gcm') ? t('proxy.decrypt.nonce') : 'IV' }}</span>
                <span class="dr-pair">
                  <input
                    v-model="step.iv"
                    type="text"
                    class="mono"
                    spellcheck="false"
                    autocomplete="off"
                    :aria-label="step.algo.endsWith('-gcm') ? t('proxy.decrypt.nonce') : 'IV'"
                    :disabled="step.iv === IV_FROM_PREFIX"
                    :placeholder="t('proxy.decrypt.ivPlaceholder')"
                  />
                  <ListSelect v-model="step.iv_format" :options="keyFormatOptions" min-width="104px" />
                </span>
                <label class="dr-check">
                  <input type="checkbox" :checked="step.iv === IV_FROM_PREFIX" @change="setIvFromPrefix(step, $event.target.checked)" />
                  <small>{{ t('proxy.decrypt.ivFromPrefix') }}</small>
                </label>
              </div>
            </div>
            <div v-else-if="step.op === 'rsa'" class="dr-step-body dr-step-grid">
              <label class="dr-mini dr-wide">
                <span>{{ t('proxy.decrypt.privateKey') }}</span>
                <textarea v-model="step.private_key" rows="4" class="mono" spellcheck="false" placeholder="-----BEGIN PRIVATE KEY-----"></textarea>
              </label>
              <label class="dr-mini"><span>{{ t('proxy.decrypt.padding') }}</span><ListSelect v-model="step.padding" :options="opts(RSA_PADDINGS)" min-width="130px" /></label>
              <label class="dr-mini"><span>{{ t('proxy.decrypt.keyPassword') }}</span><input v-model="step.password" type="password" autocomplete="new-password" /></label>
            </div>
            <div v-else-if="step.op === 'script'" class="dr-step-body">
              <label class="dr-mini dr-wide">
                <span>{{ t('proxy.decrypt.scriptCode') }}</span>
                <textarea v-model="step.code" rows="12" class="mono dr-code" spellcheck="false"></textarea>
                <small>{{ t('proxy.decrypt.scriptHint') }}</small>
              </label>
            </div>
          </li>
        </ol>
        <div class="dr-add">
          <span>{{ t('proxy.decrypt.addStep') }}</span>
          <button v-for="op in STEP_OPS" :key="op" type="button" class="ghost tiny" :disabled="form.steps.length >= MAX_STEPS" @click="addStep(op)">+ {{ t(`proxy.decrypt.op.${op}`) }}</button>
        </div>
      </div>
      <p v-if="hasScript" class="dr-warn">{{ t('proxy.decrypt.scriptWarn') }}</p>
      <p class="dr-secret">{{ t('proxy.decrypt.secretNote') }}</p>
    </div>
    <aside class="dr-test" :aria-label="t('proxy.decrypt.testTitle')">
      <h3>{{ t('proxy.decrypt.testTitle') }}</h3>
      <div class="dr-test-pick">
        <ListSelect v-model="testFlowId" :options="flowOptions" :placeholder="t('proxy.decrypt.testPickFlow')" min-width="0" />
        <button type="button" :disabled="testing || testFlowId == null" @click="runTest">
          {{ testing ? t('proxy.decrypt.testing') : t('proxy.decrypt.test') }}
        </button>
      </div>
      <p v-if="!flowOptions.length" class="dr-muted">{{ t('proxy.decrypt.testNoFlows') }}</p>
      <template v-if="testResult">
        <p v-if="!testResult.ok" class="dr-bad">{{ testResult.error }}</p>
        <template v-else>
          <p class="dr-test-summary" :class="{ bad: !testResult.matched || !testResult.targets }">
            {{ testResult.matched ? t('proxy.decrypt.testMatched', { n: testResult.targets }) : t('proxy.decrypt.testNotMatched') }}
          </p>
          <p v-if="testResult.hint" class="dr-muted">{{ testResult.hint }}</p>
          <section v-for="p in testParts" :key="p.part" class="dr-test-part">
            <h4>{{ t(`proxy.decrypt.apply.${p.part}`) }}</h4>
            <ProxyDecodedPane :part="p.data" :rule-actions="false" @copy="wb.copy($event)" />
          </section>
        </template>
      </template>
      <p v-else class="dr-muted">{{ t('proxy.decrypt.testIntro') }}</p>
    </aside>

    <footer class="dr-foot">
      <button type="button" class="primary" :disabled="saving" @click="save">
        {{ isNew ? t('proxy.decrypt.create') : t('proxy.decrypt.save') }}
      </button>
      <button type="button" class="ghost" @click="emit('cancel')">{{ t('common.cancel') }}</button>
      <span v-if="saveError" class="dr-bad" role="alert">{{ saveError }}</span>
    </footer>
  </div>
</template>

<style scoped>
.dr-editor {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 16px 20px;
  align-items: start;
}
.dr-form { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.dr-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.dr-grow { flex: 1 1 220px; }
.dr-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.dr-field > span,
.dr-steps-head > span {
  color: var(--fg);
  font-size: 12px;
  font-weight: 600;
}
.dr-field > small,
.dr-steps-head > small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.dr-field input.invalid { border-color: color-mix(in srgb, var(--err) 60%, var(--border)); }
.dr-seg {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-sunken);
}
.dr-seg button {
  height: 26px;
  padding: 0 11px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}
.dr-seg button.on { background: var(--card-bg); color: var(--fg-strong); box-shadow: 0 0 0 1px var(--border); }
.dr-steps { display: flex; flex-direction: column; gap: 8px; }
.dr-steps-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.dr-step-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.dr-step {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
}
.dr-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px 7px 10px;
}
.dr-step-no {
  display: inline-grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
}
.dr-step-tools { display: inline-flex; gap: 2px; margin-left: auto; }
.dr-remove:hover:not(:disabled) { color: var(--err); }
.dr-step-body {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  padding: 2px 10px 10px 38px;
}
.dr-mini { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.dr-mini > span:first-child { color: var(--muted); font-size: 11px; }
.dr-mini > small { color: var(--muted-2); font-size: 11px; line-height: 1.5; }
.dr-mini input[type='number'] { width: 110px; }
.dr-wide { flex: 1 1 100%; }
.dr-pair { display: flex; gap: 6px; min-width: 0; }
.dr-pair input { flex: 1 1 auto; min-width: 0; }
.dr-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  cursor: pointer;
}
.dr-mini textarea { width: 100%; resize: vertical; font-size: 12px; }
.dr-code { min-height: 180px; line-height: 1.5; tab-size: 2; }
.dr-add {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  color: var(--muted);
  font-size: 12px;
}
.dr-add span { margin-right: 4px; }
.dr-warn,
.dr-secret {
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 11.5px;
  line-height: 1.55;
}
.dr-warn { background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); }
.dr-secret { background: var(--bg-sunken); color: var(--muted); }
.dr-test {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.dr-test h3,
.dr-test h4 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 600;
}
.dr-test h4 { color: var(--muted); font-size: 11.5px; }
.dr-test-pick { display: flex; gap: 8px; min-width: 0; }
.dr-test-pick > :first-child { flex: 1 1 auto; min-width: 0; }
.dr-test-summary { margin: 0; color: var(--ok); font-size: 12px; font-weight: 600; }
.dr-test-summary.bad { color: var(--warn); }
.dr-test-part :deep(.pd) { padding: 6px 0 0; }
.dr-muted { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
.dr-bad { margin: 0; color: var(--err); font-size: 12px; line-height: 1.5; }
.dr-foot {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
</style>
