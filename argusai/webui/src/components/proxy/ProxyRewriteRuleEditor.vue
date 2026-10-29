<script setup>
// Editor and live tester for one active-rewrite rule: which flows (match),
// which part (apply_to) and an ordered list of actions that change the
// request before it goes upstream and/or the response before it returns.
import { computed, inject, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import ListSelect from '../list/ListSelect.vue'
import { flowPath } from './proxyFormat.js'
import { APPLY_TO, actionLabel, cleanRule, newAction, newRule, opsFor } from './rewriteFormat.js'

const MAX_ACTIONS = 16

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

const opOptions = computed(() => opsFor(form.apply_to).map((op) => ({ label: t(`proxy.rewrite.op.${op}`), value: op })))

// ---- match: live check + candidate flows for the tester ----
const match = reactive({ error: '', matched: 0, total: 0, ids: [] })
let matchTimer = null
let matchToken = 0
async function checkMatch() {
  const source = String(form.match || '').trim()
  const token = ++matchToken
  if (!source) {
    Object.assign(match, { error: '', matched: 0, total: 0, ids: [] })
    return
  }
  try {
    const r = await api.queryProxyTraffic(source)
    if (token !== matchToken) return
    if (r?.ok) Object.assign(match, { error: '', matched: r.matched || 0, total: r.total || 0, ids: r.ids || [] })
    else Object.assign(match, { error: r?.error || t('toast.error'), matched: 0, ids: [] })
  } catch {
    if (token === matchToken) match.error = t('toast.error')
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

// Keep actions legal when apply_to narrows (e.g. both -> response drops set_method).
watch(() => form.apply_to, () => {
  const allowed = new Set(opsFor(form.apply_to))
  for (const action of form.actions) {
    if (!allowed.has(action.op)) Object.assign(action, newAction([...allowed][0]))
  }
})

function addAction(op) {
  if (form.actions.length < MAX_ACTIONS) form.actions.push(newAction(op))
}
function removeAction(idx) {
  form.actions.splice(idx, 1)
}
function moveAction(idx, delta) {
  const to = idx + delta
  if (to < 0 || to >= form.actions.length) return
  const [a] = form.actions.splice(idx, 1)
  form.actions.splice(to, 0, a)
}
function changeOp(idx, op) {
  if (form.actions[idx]?.op !== op) form.actions.splice(idx, 1, newAction(op))
}
function scHeadersText(action) {
  return Object.entries(action.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n')
}
function setScHeaders(action, text) {
  const headers = {}
  for (const line of String(text || '').split('\n')) {
    const idx = line.indexOf(':')
    if (idx > 0) headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
  }
  action.headers = headers
}

// ---- test & save ----
const testing = ref(false)
const testResult = ref(null)
async function runTest() {
  if (testFlowId.value == null) return
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await api.testProxyRewriteRule(cleanRule({ ...form, name: form.name || 'test' }), testFlowId.value)
  } catch {
    testResult.value = { ok: false, error: t('toast.error') }
  } finally {
    testing.value = false
  }
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const r = await api.saveProxyRewriteRule(cleanRule(form))
    if (r?.ok) {
      showToast(t('proxy.rewrite.saved'), 'ok')
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
  <div class="rw-editor">
    <div class="rw-form">
      <div class="rw-row">
        <label class="rw-field rw-grow">
          <span>{{ t('proxy.rewrite.name') }}</span>
          <input v-model="form.name" type="text" maxlength="80" :placeholder="t('proxy.rewrite.namePlaceholder')" />
        </label>
        <div class="rw-field">
          <span>{{ t('proxy.rewrite.applyTo') }}</span>
          <div class="rw-seg" role="radiogroup" :aria-label="t('proxy.rewrite.applyTo')">
            <button
              v-for="v in APPLY_TO"
              :key="v"
              type="button"
              role="radio"
              :aria-checked="form.apply_to === v"
              :class="{ on: form.apply_to === v }"
              @click="form.apply_to = v"
            >{{ t(`proxy.rewrite.apply.${v}`) }}</button>
          </div>
        </div>
      </div>
      <label class="rw-field">
        <span>{{ t('proxy.rewrite.match') }}</span>
        <input
          v-model="form.match"
          type="text"
          class="mono"
          spellcheck="false"
          autocomplete="off"
          :class="{ invalid: match.error }"
          :placeholder="t('proxy.rewrite.matchPlaceholder')"
        />
        <small v-if="match.error" class="rw-bad">{{ match.error }}</small>
        <small v-else-if="String(form.match || '').trim()">{{ t('proxy.rewrite.matchCount', { n: match.matched, total: match.total }) }}</small>
        <small v-else>{{ t('proxy.rewrite.matchHint') }}</small>
      </label>

      <div class="rw-actions">
        <div class="rw-actions-head">
          <span>{{ t('proxy.rewrite.actions') }}</span>
          <small>{{ t('proxy.rewrite.actionsHint') }}</small>
        </div>
        <ol class="rw-action-list">
          <li v-for="(action, idx) in form.actions" :key="idx" class="rw-action">
            <div class="rw-action-head">
              <span class="rw-no mono">{{ idx + 1 }}</span>
              <ListSelect :model-value="action.op" :options="opOptions" min-width="150px" @update:model-value="changeOp(idx, $event)" />
              <span class="rw-action-tools">
                <button type="button" class="ghost tiny" :disabled="idx === 0" :title="t('proxy.rewrite.moveUp')" @click="moveAction(idx, -1)">↑</button>
                <button type="button" class="ghost tiny" :disabled="idx === form.actions.length - 1" :title="t('proxy.rewrite.moveDown')" @click="moveAction(idx, 1)">↓</button>
                <button type="button" class="ghost tiny rw-del" :title="t('proxy.rewrite.removeAction')" @click="removeAction(idx)">×</button>
              </span>
            </div>
            <div class="rw-action-body">
              <template v-if="action.op === 'set_header' || action.op === 'set_query'">
                <input v-model="action.name" class="mono" :placeholder="t('proxy.rewrite.fieldName')" :aria-label="t('proxy.rewrite.fieldName')" />
                <input v-model="action.value" class="mono rw-grow" :placeholder="t('proxy.rewrite.fieldValue')" :aria-label="t('proxy.rewrite.fieldValue')" />
              </template>
              <template v-else-if="action.op === 'remove_header' || action.op === 'remove_query'">
                <input v-model="action.name" class="mono rw-grow" :placeholder="t('proxy.rewrite.fieldName')" :aria-label="t('proxy.rewrite.fieldName')" />
              </template>
              <template v-else-if="action.op === 'set_method'">
                <input v-model="action.method" class="mono" placeholder="GET" :aria-label="t('proxy.rewrite.op.set_method')" />
              </template>
              <template v-else-if="action.op === 'set_status'">
                <input v-model.number="action.status" type="number" min="100" max="599" class="mono" :aria-label="t('proxy.rewrite.op.set_status')" />
              </template>
              <template v-else-if="action.op === 'replace_body'">
                <input v-model="action.find" class="mono rw-grow" :placeholder="t('proxy.rewrite.find')" :aria-label="t('proxy.rewrite.find')" />
                <input v-model="action.replace" class="mono rw-grow" :placeholder="t('proxy.rewrite.replace')" :aria-label="t('proxy.rewrite.replace')" />
                <label class="rw-check"><input v-model="action.regex" type="checkbox" /><small>{{ t('proxy.rewrite.regex') }}</small></label>
              </template>
              <template v-else-if="action.op === 'short_circuit'">
                <label class="rw-mini"><span>{{ t('proxy.rewrite.op.set_status') }}</span><input v-model.number="action.status" type="number" min="100" max="599" class="mono" /></label>
                <label class="rw-mini rw-grow"><span>{{ t('proxy.rewrite.scHeaders') }}</span><textarea :value="scHeadersText(action)" rows="2" class="mono" spellcheck="false" placeholder="Content-Type: application/json" @input="setScHeaders(action, $event.target.value)"></textarea></label>
                <label class="rw-mini rw-grow"><span>{{ t('proxy.rewrite.scBody') }}</span><textarea v-model="action.body" rows="2" class="mono" spellcheck="false"></textarea></label>
              </template>
            </div>
          </li>
        </ol>
        <div class="rw-add">
          <span>{{ t('proxy.rewrite.addAction') }}</span>
          <button v-for="op in opsFor(form.apply_to)" :key="op" type="button" class="ghost tiny" :disabled="form.actions.length >= MAX_ACTIONS" @click="addAction(op)">+ {{ t(`proxy.rewrite.op.${op}`) }}</button>
        </div>
      </div>
      <p class="rw-warn">{{ t('proxy.rewrite.liveWarn') }}</p>
    </div>

    <aside class="rw-test" :aria-label="t('proxy.rewrite.testTitle')">
      <h3>{{ t('proxy.rewrite.testTitle') }}</h3>
      <div class="rw-test-pick">
        <ListSelect v-model="testFlowId" :options="flowOptions" :placeholder="t('proxy.rewrite.testPickFlow')" min-width="0" />
        <button type="button" :disabled="testing || testFlowId == null" @click="runTest">{{ testing ? t('proxy.rewrite.testing') : t('proxy.rewrite.test') }}</button>
      </div>
      <p v-if="!flowOptions.length" class="rw-muted">{{ t('proxy.rewrite.testNoFlows') }}</p>
      <template v-if="testResult">
        <p v-if="!testResult.ok" class="rw-bad">{{ testResult.error }}</p>
        <template v-else>
          <p class="rw-test-summary" :class="{ bad: !testResult.matched }">
            {{ testResult.matched ? t('proxy.rewrite.testMatched') : t('proxy.rewrite.testNotMatched') }}
          </p>
          <section v-for="part in ['request', 'response']" v-show="testResult[part]" :key="part" class="rw-test-part">
            <h4>{{ t(`proxy.rewrite.apply.${part}`) }}</h4>
            <p v-if="testResult[part]" class="rw-trace mono">{{ (testResult[part].trace || []).join(' · ') || t('proxy.rewrite.noChange') }}</p>
            <p v-if="testResult[part] && testResult[part].short_circuit" class="rw-sc">{{ t('proxy.rewrite.willBlock') }}</p>
            <pre v-if="testResult[part] && testResult[part].body_preview" class="rw-preview mono selectable">{{ testResult[part].body_preview }}</pre>
          </section>
        </template>
      </template>
      <p v-else class="rw-muted">{{ t('proxy.rewrite.testIntro') }}</p>
    </aside>

    <footer class="rw-foot">
      <button type="button" class="primary" :disabled="saving" @click="save">{{ isNew ? t('proxy.rewrite.create') : t('proxy.rewrite.save') }}</button>
      <button type="button" class="ghost" @click="emit('cancel')">{{ t('common.cancel') }}</button>
      <span v-if="saveError" class="rw-bad" role="alert">{{ saveError }}</span>
    </footer>
  </div>
</template>

<style scoped>
.rw-editor {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 16px 20px;
  align-items: start;
}
.rw-form { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.rw-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.rw-grow { flex: 1 1 160px; }
.rw-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rw-field > span, .rw-actions-head > span { color: var(--fg); font-size: 12px; font-weight: 600; }
.rw-field > small, .rw-actions-head > small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.rw-field input.invalid { border-color: color-mix(in srgb, var(--err) 60%, var(--border)); }
.rw-seg { display: inline-flex; padding: 2px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-sunken); }
.rw-seg button { height: 26px; padding: 0 11px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: 12px; }
.rw-seg button.on { background: var(--card-bg); color: var(--fg-strong); box-shadow: 0 0 0 1px var(--border); }
.rw-actions { display: flex; flex-direction: column; gap: 8px; }
.rw-actions-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.rw-action-list { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 0; list-style: none; }
.rw-action { border: 1px solid var(--border); border-radius: 10px; background: var(--bg-sunken); }
.rw-action-head { display: flex; align-items: center; gap: 8px; padding: 7px 8px 7px 10px; }
.rw-no { display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; background: var(--panel-2); color: var(--muted); font-size: 11px; }
.rw-action-tools { display: inline-flex; gap: 2px; margin-left: auto; }
.rw-del:hover:not(:disabled) { color: var(--err); }
.rw-action-body { display: flex; flex-wrap: wrap; gap: 8px; padding: 2px 10px 10px 38px; }
.rw-action-body input[type='text'], .rw-action-body input:not([type]), .rw-action-body input.mono { min-width: 0; }
.rw-action-body input[type='number'] { width: 90px; }
.rw-mini { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rw-mini > span { color: var(--muted); font-size: 11px; }
.rw-mini textarea { width: 100%; resize: vertical; font-size: 12px; }
.rw-check { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
.rw-add { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; color: var(--muted); font-size: 12px; }
.rw-add span { margin-right: 4px; }
.rw-warn { margin: 0; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); font-size: 11.5px; line-height: 1.55; }
.rw-test { display: flex; flex-direction: column; gap: 10px; min-width: 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; }
.rw-test h3 { margin: 0; color: var(--fg-strong); font-size: 13px; font-weight: 600; }
.rw-test h4 { margin: 0; color: var(--muted); font-size: 11.5px; }
.rw-test-pick { display: flex; gap: 8px; min-width: 0; }
.rw-test-pick > :first-child { flex: 1 1 auto; min-width: 0; }
.rw-test-summary { margin: 0; color: var(--ok); font-size: 12px; font-weight: 600; }
.rw-test-summary.bad { color: var(--warn); }
.rw-test-part { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
.rw-trace { color: var(--muted); font-size: 11.5px; }
.rw-sc { margin: 0; color: var(--warn); font-size: 12px; }
.rw-preview { max-height: 160px; overflow: auto; margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-sunken); color: var(--fg); font-size: 11.5px; white-space: pre-wrap; word-break: break-all; }
.rw-muted { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
.rw-bad { margin: 0; color: var(--err); font-size: 12px; line-height: 1.5; }
.rw-foot { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-top: 12px; border-top: 1px solid var(--border); }
.selectable { user-select: text; -webkit-user-select: text; cursor: text; }
</style>
