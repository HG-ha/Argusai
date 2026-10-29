<script setup>
// Decryption rules card on the proxy settings tab. Rules are saved one by
// one into user settings (they carry keys), independent of the form footer.
import { inject, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { t } from '../../i18n.js'
import { showToast } from '../../state.js'
import { useConfirm } from '../../composables/useConfirm.js'
import ProxyDecryptRuleEditor from './ProxyDecryptRuleEditor.vue'
import { newRule, stepsSummary } from './decryptFormat.js'

const wb = inject('proxyWorkbench')
const confirmDialog = useConfirm()

const rootEl = ref(null)
const rules = ref([])
const errors = ref({})
const loaded = ref(false)
const busyId = ref('')
const editing = ref(null)
const editorKey = ref(0)
const testFlowId = ref(null)

async function load() {
  try {
    const r = await api.getProxyDecryptRules()
    if (r?.ok) {
      rules.value = Array.isArray(r.rules) ? r.rules : []
      errors.value = r.errors || {}
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
    const r = await api.saveProxyDecryptRule({ ...rule, enabled: !rule.enabled })
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
    title: t('proxy.decrypt.deleteTitle'),
    content: t('proxy.decrypt.deleteConfirm', { name: rule.name }),
    positiveText: t('proxy.decrypt.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  busyId.value = rule.id
  try {
    await api.deleteProxyDecryptRule(rule.id)
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
  wb.ruleDraft.value = null
  const { editId, testFlowId: flowId, ...rule } = draft
  if (editId) {
    const existing = rules.value.find((r) => r.id === editId)
    if (existing) openEditor(existing, flowId)
    return
  }
  openEditor(rule, flowId)
}

// Drafts may arrive before the list loads (the tab mounts on demand).
watch([() => wb.ruleDraft.value, loaded], ([draft, ready]) => {
  if (draft && ready) applyDraft(draft)
}, { immediate: true })
onMounted(load)
</script>

<template>
  <section ref="rootEl" class="proxy-card proxy-card-wide dr-card">
    <header class="proxy-card-head">
      <h2>{{ t('proxy.decrypt.title') }} <span v-if="rules.length" class="dr-count mono">{{ rules.length }}</span></h2>
      <button v-if="!editing" type="button" class="primary" @click="openEditor(newRule())">{{ t('proxy.decrypt.newRule') }}</button>
    </header>
    <p class="proxy-card-lead">{{ t('proxy.decrypt.lead') }}</p>

    <ProxyDecryptRuleEditor
      v-if="editing"
      :key="editorKey"
      :rule="editing"
      :test-flow-id="testFlowId"
      @saved="onSaved"
      @cancel="editing = null"
    />
    <template v-else>
      <div v-if="loaded && !rules.length" class="dr-empty">
        <p>{{ t('proxy.decrypt.empty') }}</p>
        <p class="dr-empty-tip">{{ t('proxy.decrypt.emptyTip') }}</p>
      </div>
      <ul v-else class="dr-list">
        <li v-for="rule in rules" :key="rule.id" class="dr-rule" :class="{ off: !rule.enabled, broken: errors[rule.id] }">
          <label class="dr-switch" :title="rule.enabled ? t('proxy.decrypt.disable') : t('proxy.decrypt.enable')">
            <input
              type="checkbox"
              :checked="rule.enabled"
              :disabled="busyId === rule.id"
              :aria-label="rule.enabled ? t('proxy.decrypt.disable') : t('proxy.decrypt.enable')"
              @change="toggle(rule)"
            />
          </label>
          <div class="dr-rule-main">
            <div class="dr-rule-line">
              <strong>{{ rule.name }}</strong>
              <span class="dr-tag">{{ t(`proxy.decrypt.apply.${rule.apply_to || 'response'}`) }}</span>
              <code v-if="rule.field" class="dr-tag mono">{{ rule.field }}</code>
            </div>
            <code class="dr-rule-match mono">{{ rule.match }}</code>
            <span class="dr-rule-steps mono">{{ stepsSummary(rule.steps) }}</span>
            <span v-if="errors[rule.id]" class="dr-rule-error">{{ errors[rule.id] }}</span>
          </div>
          <div class="dr-rule-actions">
            <button type="button" class="ghost tiny" @click="openEditor(rule)">{{ t('proxy.decrypt.edit') }}</button>
            <button type="button" class="ghost tiny dr-del" :disabled="busyId === rule.id" @click="remove(rule)">{{ t('proxy.decrypt.delete') }}</button>
          </div>
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.dr-card .proxy-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}
.dr-card .proxy-card-head h2 {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: var(--fg-strong);
  font-size: 14px;
  font-weight: 600;
}
.dr-card .proxy-card-lead {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.dr-count {
  padding: 0 7px;
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
  font-weight: 500;
}
.dr-empty {
  padding: 18px 16px;
  border: 1px dashed var(--border);
  border-radius: 10px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.dr-empty p { margin: 0; }
.dr-empty-tip { margin-top: 6px !important; color: var(--muted-2); }
.dr-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.dr-rule {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
}
.dr-rule.off { opacity: 0.6; }
.dr-rule.broken { border-color: color-mix(in srgb, var(--err) 45%, var(--border)); }
.dr-switch { padding-top: 2px; cursor: pointer; }
.dr-rule-main {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.dr-rule-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.dr-rule-line strong { color: var(--fg-strong); font-size: 13px; }
.dr-tag {
  padding: 1px 7px;
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
}
.dr-rule-match,
.dr-rule-steps {
  overflow: hidden;
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dr-rule-match { color: var(--fg); }
.dr-rule-steps { color: var(--muted); }
.dr-rule-error { color: var(--err); font-size: 11.5px; }
.dr-rule-actions { display: inline-flex; gap: 4px; flex: 0 0 auto; }
.dr-del:hover:not(:disabled) { color: var(--err); }
</style>
