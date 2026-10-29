<script setup>
// Active-rewrite rules card on the proxy settings tab. Rules modify live
// traffic, so each is saved individually into user settings.
import { inject, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import { useConfirm } from '../../composables/useConfirm.js'
import ProxyRewriteRuleEditor from './ProxyRewriteRuleEditor.vue'
import { actionsSummary, newRule } from './rewriteFormat.js'

const wb = inject('proxyWorkbench')
const confirmDialog = useConfirm()

const rootEl = ref(null)
const rules = ref([])
const errors = ref({})
const active = ref(false)
const loaded = ref(false)
const busyId = ref('')
const editing = ref(null)
const editorKey = ref(0)
const testFlowId = ref(null)

async function load() {
  try {
    const r = await api.getProxyRewriteRules()
    if (r?.ok) {
      rules.value = Array.isArray(r.rules) ? r.rules : []
      errors.value = r.errors || {}
      active.value = Boolean(r.active)
    }
  } catch {
  } finally {
    loaded.value = true
  }
}

function openEditor(rule, flowId = null) {
  editing.value = JSON.parse(JSON.stringify(rule))
  testFlowId.value = flowId ?? wb.selectedId.value ?? null
  editorKey.value += 1
  nextTick(() => rootEl.value?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
}

function onSaved() {
  editing.value = null
  load()
}

async function toggle(rule) {
  busyId.value = rule.id
  try {
    const r = await api.saveProxyRewriteRule({ ...rule, enabled: !rule.enabled })
    if (!r?.ok) showToast(r?.error || t('toast.error'), 'error')
    await load()
  } catch {
    showToast(t('toast.error'), 'error')
  } finally {
    busyId.value = ''
  }
}

async function remove(rule) {
  const ok = await confirmDialog({
    title: t('proxy.rewrite.deleteTitle'),
    content: t('proxy.rewrite.deleteConfirm', { name: rule.name }),
    positiveText: t('proxy.rewrite.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  busyId.value = rule.id
  try {
    await api.deleteProxyRewriteRule(rule.id)
    if (editing.value?.id === rule.id) editing.value = null
    await load()
  } catch {
    showToast(t('toast.error'), 'error')
  } finally {
    busyId.value = ''
  }
}

function applyDraft(draft) {
  if (!draft) return
  wb.rewriteDraft.value = null
  const { editId, testFlowId: flowId, ...rule } = draft
  if (editId) {
    const existing = rules.value.find((r) => r.id === editId)
    if (existing) openEditor(existing, flowId)
    return
  }
  openEditor(rule, flowId)
}

watch([() => wb.rewriteDraft.value, loaded], ([draft, ready]) => {
  if (draft && ready) applyDraft(draft)
}, { immediate: true })
onMounted(load)
</script>

<template>
  <section ref="rootEl" class="proxy-card proxy-card-wide rw-card">
    <header class="proxy-card-head">
      <h2>
        {{ t('proxy.rewrite.title') }}
        <span v-if="rules.length" class="rw-count mono">{{ rules.length }}</span>
        <span v-if="active" class="rw-live">{{ t('proxy.rewrite.liveBadge') }}</span>
      </h2>
      <button v-if="!editing" type="button" class="primary" @click="openEditor(newRule())">{{ t('proxy.rewrite.newRule') }}</button>
    </header>
    <p class="proxy-card-lead">{{ t('proxy.rewrite.lead') }}</p>

    <ProxyRewriteRuleEditor
      v-if="editing"
      :key="editorKey"
      :rule="editing"
      :test-flow-id="testFlowId"
      @saved="onSaved"
      @cancel="editing = null"
    />
    <template v-else>
      <div v-if="loaded && !rules.length" class="rw-empty">
        <p>{{ t('proxy.rewrite.empty') }}</p>
        <p class="rw-empty-tip">{{ t('proxy.rewrite.emptyTip') }}</p>
      </div>
      <ul v-else class="rw-list">
        <li v-for="rule in rules" :key="rule.id" class="rw-rule" :class="{ off: !rule.enabled, broken: errors[rule.id] }">
          <label class="rw-switch" :title="rule.enabled ? t('proxy.rewrite.disable') : t('proxy.rewrite.enable')">
            <input type="checkbox" :checked="rule.enabled" :disabled="busyId === rule.id" @change="toggle(rule)" />
          </label>
          <div class="rw-rule-main">
            <div class="rw-rule-line">
              <strong>{{ rule.name }}</strong>
              <span class="rw-tag">{{ t(`proxy.rewrite.apply.${rule.apply_to || 'request'}`) }}</span>
            </div>
            <code class="rw-rule-match mono">{{ rule.match }}</code>
            <span class="rw-rule-acts mono">{{ actionsSummary(rule.actions) }}</span>
            <span v-if="errors[rule.id]" class="rw-rule-error">{{ errors[rule.id] }}</span>
          </div>
          <div class="rw-rule-actions">
            <button type="button" class="ghost tiny" @click="openEditor(rule)">{{ t('proxy.rewrite.edit') }}</button>
            <button type="button" class="ghost tiny rw-del" :disabled="busyId === rule.id" @click="remove(rule)">{{ t('proxy.rewrite.delete') }}</button>
          </div>
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.rw-card .proxy-card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.rw-card .proxy-card-head h2 { display: inline-flex; align-items: center; gap: 8px; margin: 0; color: var(--fg-strong); font-size: 14px; font-weight: 600; }
.rw-card .proxy-card-lead { margin: 0 0 12px; color: var(--muted); font-size: 12px; line-height: 1.6; }
.rw-count { padding: 0 7px; border-radius: 999px; background: var(--panel-2); color: var(--muted); font-size: 11px; font-weight: 500; }
.rw-live { padding: 1px 8px; border-radius: 999px; background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); font-size: 10.5px; font-weight: 600; }
.rw-empty { padding: 18px 16px; border: 1px dashed var(--border); border-radius: 10px; color: var(--muted); font-size: 12px; line-height: 1.6; }
.rw-empty p { margin: 0; }
.rw-empty-tip { margin-top: 6px !important; color: var(--muted-2); }
.rw-list { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 0; list-style: none; }
.rw-rule { display: flex; align-items: flex-start; gap: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-sunken); }
.rw-rule.off { opacity: 0.6; }
.rw-rule.broken { border-color: color-mix(in srgb, var(--err) 45%, var(--border)); }
.rw-switch { padding-top: 2px; cursor: pointer; }
.rw-rule-main { flex: 1 1 auto; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rw-rule-line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.rw-rule-line strong { color: var(--fg-strong); font-size: 13px; }
.rw-tag { padding: 1px 7px; border-radius: 6px; background: var(--panel-2); color: var(--muted); font-size: 11px; }
.rw-rule-match, .rw-rule-acts { overflow: hidden; font-size: 11.5px; text-overflow: ellipsis; white-space: nowrap; }
.rw-rule-match { color: var(--fg); }
.rw-rule-acts { color: var(--muted); }
.rw-rule-error { color: var(--err); font-size: 11.5px; }
.rw-rule-actions { display: inline-flex; gap: 4px; flex: 0 0 auto; }
.rw-del:hover:not(:disabled) { color: var(--err); }
</style>
