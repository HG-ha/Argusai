<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'
import { t } from '../i18n.js'
import { showToast } from '../state.js'
import { useConfirm } from '../composables/useConfirm.js'
import WorkbenchModal from './workbench/WorkbenchModal.vue'
import PageEmpty from './list/PageEmpty.vue'

const confirmDialog = useConfirm()
const runtimes = ref([])
const loading = ref(false)
const testing = reactive({})
const testResults = reactive({})
const selectedId = ref('')
const query = ref('')
const showEditor = ref(false)
const saving = ref(false)
const choosingExecutable = ref(false)
const editingId = ref('')
const form = reactive(emptyForm())

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return runtimes.value
  return runtimes.value.filter(item =>
    `${item.name || ''} ${item.id || ''} ${item.command || ''}`.toLowerCase().includes(q),
  )
})
const selected = computed(() =>
  runtimes.value.find(item => item.id === selectedId.value) || filtered.value[0] || null,
)
const availableCount = computed(() => runtimes.value.filter(item => item.probe?.available).length)
const presetRuntimes = computed(() =>
  runtimes.value.filter(item => item.backend === 'acp' && item.preset),
)

function emptyForm() {
  return {
    name: '',
    command: '',
    args_text: '',
    cwd: '',
    notes: '',
    enabled: true,
  }
}

function selectRuntime(item) {
  selectedId.value = item.id
}

function runtimeSubtitle(item) {
  if (item.backend === 'native') return t('localAgents.nativeRuntime')
  const command = String(item.probe?.resolved_command || item.command || '')
  const executable = command.split(/[\\/]/).filter(Boolean).pop()
  return executable ? `ACP · ${executable}` : 'ACP'
}

async function loadRuntimes() {
  loading.value = true
  try {
    const result = await api.listAgentRuntimes()
    if (!result?.ok) throw new Error(result?.error || t('localAgents.loadFailed'))
    runtimes.value = Array.isArray(result.runtimes) ? result.runtimes : []
    if (!selectedId.value || !runtimes.value.some(item => item.id === selectedId.value)) {
      selectedId.value = runtimes.value[0]?.id || ''
    }
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    loading.value = false
  }
}

async function testRuntime(item) {
  testing[item.id] = true
  try {
    const result = await api.testAgentRuntime(item.id)
    testResults[item.id] = result || {}
    showToast(
      result?.ok ? t('localAgents.testPassed') : `${t('localAgents.testFailed')}: ${result?.error || ''}`,
      result?.ok ? 'ok' : 'error',
    )
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    testing[item.id] = false
  }
}

async function toggleRuntime(item) {
  try {
    const result = await api.saveAgentRuntime({ ...item, enabled: item.enabled === false })
    if (!result?.ok) throw new Error(result?.error || t('localAgents.saveFailed'))
    await loadRuntimes()
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  }
}

function openNew() {
  editingId.value = ''
  Object.assign(form, emptyForm())
  showEditor.value = true
}

function openPresetRuntime(item) {
  showEditor.value = false
  selectedId.value = item.id
}

async function chooseRuntimeExecutable() {
  choosingExecutable.value = true
  try {
    const result = await api.chooseAgentRuntimeExecutable(form.command)
    if (result?.cancelled) return
    if (!result?.ok) throw new Error(result?.error || t('localAgents.chooseExecutableFailed'))
    form.command = result.path || ''
    if (!form.name.trim()) form.name = result.suggested_name || ''
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    choosingExecutable.value = false
  }
}

function openEdit(item) {
  editingId.value = item.id || ''
  Object.assign(form, {
    name: item.name || '',
    command: item.command || '',
    args_text: Array.isArray(item.args) ? item.args.join('\n') : '',
    cwd: item.cwd || '',
    notes: item.notes || '',
    enabled: item.enabled !== false,
  })
  showEditor.value = true
}

async function saveRuntime() {
  saving.value = true
  try {
    const result = await api.saveAgentRuntime({
      id: editingId.value,
      name: form.name.trim(),
      backend: 'acp',
      command: form.command.trim(),
      args: form.args_text.split(/\r?\n/).map(item => item.trim()).filter(Boolean),
      cwd: form.cwd.trim(),
      notes: form.notes.trim(),
      enabled: !!form.enabled,
    })
    if (!result?.ok) throw new Error(result?.error || t('localAgents.saveFailed'))
    showEditor.value = false
    selectedId.value = result.runtime?.id || editingId.value
    await loadRuntimes()
    showToast(t('localAgents.saved'), 'ok')
  } catch (error) {
    showToast(String(error?.message || error), 'error')
  } finally {
    saving.value = false
  }
}

async function deleteRuntime(item) {
  const ok = await confirmDialog({
    title: t('localAgents.deleteTitle'),
    content: t('localAgents.deleteContent', { name: item.name || item.id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    type: 'warning',
  })
  if (!ok) return
  const result = await api.deleteAgentRuntime(item.id)
  if (!result?.ok) {
    showToast(result?.error || t('localAgents.deleteFailed'), 'error')
    return
  }
  await loadRuntimes()
}

onMounted(loadRuntimes)
</script>

<template>
  <section class="integration-page">
    <header class="integration-head">
      <div>
        <p class="integration-kicker">{{ t('localAgents.kicker') }}</p>
        <h1>{{ t('localAgents.title') }}</h1>
        <p>{{ t('localAgents.hint') }}</p>
      </div>
      <div class="integration-head-actions">
        <span class="inventory-chip">{{ availableCount }} / {{ runtimes.length }} {{ t('localAgents.available') }}</span>
        <button class="btn ghost" type="button" :disabled="loading" @click="loadRuntimes">
          {{ loading ? t('common.loading') : t('common.refresh') }}
        </button>
        <button class="btn primary" type="button" @click="openNew">{{ t('localAgents.add') }}</button>
      </div>
    </header>

    <div class="integration-shell">
      <aside class="integration-master">
        <label class="integration-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
          <input v-model="query" :placeholder="t('localAgents.search')" />
        </label>
        <div class="integration-list">
          <button
            v-for="item in filtered"
            :key="item.id"
            type="button"
            class="integration-row"
            :class="{ active: selected?.id === item.id }"
            @click="selectRuntime(item)"
          >
            <span class="runtime-mark" :class="{ native: item.backend === 'native' }">
              {{ item.backend === 'native' ? 'A' : (item.name || '?').slice(0, 1).toUpperCase() }}
            </span>
            <span class="runtime-copy">
              <strong>{{ item.name }}</strong>
              <small>{{ runtimeSubtitle(item) }}</small>
            </span>
            <span class="status-dot" :class="{ live: item.probe?.available && item.enabled !== false }"></span>
          </button>
          <PageEmpty
            v-if="!loading && !filtered.length"
            :title="t('localAgents.empty')"
            :hint="t('localAgents.emptyHint')"
          />
        </div>
      </aside>

      <article v-if="selected" class="integration-detail">
        <div class="runtime-detail-head">
          <div class="runtime-title">
            <span class="runtime-mark large" :class="{ native: selected.backend === 'native' }">
              {{ selected.backend === 'native' ? 'A' : (selected.name || '?').slice(0, 1).toUpperCase() }}
            </span>
            <div>
              <h2>{{ selected.name }}</h2>
              <p>{{ selected.notes || t('localAgents.noNotes') }}</p>
            </div>
          </div>
          <div class="detail-actions">
            <button class="btn ghost" type="button" :disabled="testing[selected.id]" @click="testRuntime(selected)">
              {{ testing[selected.id] ? t('localAgents.testing') : t('localAgents.test') }}
            </button>
            <button v-if="selected.backend !== 'native'" class="btn ghost" type="button" @click="openEdit(selected)">
              {{ t('common.edit') }}
            </button>
          </div>
        </div>

        <div class="route-line" aria-label="Agent connection route">
          <span class="route-node">{{ t('localAgents.argusClient') }}</span>
          <span class="route-arrow">→</span>
          <span class="route-node accent">ACP</span>
          <span class="route-arrow">→</span>
          <span class="route-node">{{ selected.command || t('localAgents.inProcess') }}</span>
        </div>

        <div class="detail-section">
          <div class="detail-section-head">
            <div>
              <h3>{{ t('localAgents.connection') }}</h3>
              <p>{{ t('localAgents.connectionHint') }}</p>
            </div>
            <button
              v-if="selected.backend !== 'native'"
              type="button"
              class="switch"
              :class="{ on: selected.enabled !== false }"
              :aria-label="t('localAgents.toggle')"
              @click="toggleRuntime(selected)"
            ><span></span></button>
          </div>
          <dl class="fact-rows">
            <div><dt>{{ t('localAgents.status') }}</dt><dd><span class="status-label" :class="{ live: selected.probe?.available }">{{ selected.probe?.available ? t('localAgents.detected') : t('localAgents.notDetected') }}</span></dd></div>
            <div><dt>{{ t('localAgents.command') }}</dt><dd><code>{{ selected.probe?.resolved_command || selected.command || '—' }}</code></dd></div>
            <div><dt>{{ t('localAgents.arguments') }}</dt><dd><code>{{ selected.args?.length ? selected.args.join(' ') : '—' }}</code></dd></div>
            <div><dt>{{ t('localAgents.workingDirectory') }}</dt><dd><code>{{ selected.cwd || t('localAgents.sessionWorkspace') }}</code></dd></div>
          </dl>
        </div>

        <div class="detail-section">
          <div class="detail-section-head">
            <div>
              <h3>{{ t('localAgents.capabilities') }}</h3>
              <p>{{ t('localAgents.capabilitiesHint') }}</p>
            </div>
          </div>
          <div class="capability-grid">
            <div><span>session/new</span><strong>{{ selected.backend === 'native' || testResults[selected.id]?.ok ? '✓' : '—' }}</strong></div>
            <div><span>session/load</span><strong>{{ testResults[selected.id]?.agent_capabilities?.loadSession || selected.backend === 'native' ? '✓' : '—' }}</strong></div>
            <div><span>MCP stdio</span><strong>{{ selected.backend === 'acp' ? '✓' : '—' }}</strong></div>
            <div><span>{{ t('localAgents.argusTools') }}</span><strong>✓</strong></div>
          </div>
          <p v-if="testResults[selected.id]?.error" class="detail-error">{{ testResults[selected.id].error }}</p>
        </div>

        <div v-if="selected.backend !== 'native'" class="danger-strip">
          <div><strong>{{ t('localAgents.removeTitle') }}</strong><span>{{ t('localAgents.removeHint') }}</span></div>
          <button v-if="!selected.preset" class="btn danger" type="button" @click="deleteRuntime(selected)">{{ t('common.delete') }}</button>
        </div>
      </article>
    </div>

    <WorkbenchModal
      :open="showEditor"
      size="lg"
      :title="editingId ? t('localAgents.editTitle') : t('localAgents.addTitle')"
      :busy="saving"
      :close-label="t('common.close')"
      @close="showEditor = false"
    >
      <form class="runtime-form" @submit.prevent="saveRuntime">
        <section v-if="!editingId" class="preset-assist">
          <div class="preset-assist-head">
            <div>
              <strong>{{ t('localAgents.presetsReadyTitle') }}</strong>
              <p>{{ t('localAgents.presetsReadyHint') }}</p>
            </div>
          </div>
          <div class="preset-shortcuts">
            <button v-for="item in presetRuntimes" :key="item.id" type="button" @click="openPresetRuntime(item)">
              <span class="runtime-mark">{{ (item.name || '?').slice(0, 1).toUpperCase() }}</span>
              <span><strong>{{ item.name }}</strong><small>{{ item.probe?.available ? t('localAgents.detected') : t('localAgents.notDetected') }}</small></span>
              <span class="preset-open">{{ t('localAgents.openPreset') }}</span>
            </button>
          </div>
        </section>

        <section v-if="!editingId" class="custom-runtime-guide">
          <strong>{{ t('localAgents.customGuideTitle') }}</strong>
          <ol>
            <li><span>1</span><p>{{ t('localAgents.customGuideStep1') }}</p></li>
            <li><span>2</span><p>{{ t('localAgents.customGuideStep2') }}</p></li>
            <li><span>3</span><p>{{ t('localAgents.customGuideStep3') }}</p></li>
          </ol>
        </section>

        <label>
          <span>{{ t('localAgents.name') }}</span>
          <input v-model="form.name" required :placeholder="t('localAgents.namePlaceholder')" />
        </label>
        <label>
          <span>{{ t('localAgents.command') }}</span>
          <div class="command-picker">
            <input v-model="form.command" required :placeholder="t('localAgents.commandPlaceholder')" />
            <button class="btn ghost" type="button" :disabled="choosingExecutable" @click="chooseRuntimeExecutable">
              {{ choosingExecutable ? t('common.loading') : t('localAgents.chooseExecutable') }}
            </button>
          </div>
          <small class="field-hint">{{ t('localAgents.commandHint') }}</small>
        </label>
        <label>
          <span>{{ t('localAgents.argumentsOnePerLine') }}</span>
          <textarea v-model="form.args_text" rows="4" :placeholder="t('localAgents.argumentsPlaceholder')"></textarea>
          <small class="field-hint">{{ t('localAgents.argumentsHint') }}</small>
        </label>
        <label>
          <span>{{ t('localAgents.workingDirectory') }}</span>
          <input v-model="form.cwd" :placeholder="t('localAgents.optional')" />
          <small class="field-hint">{{ t('localAgents.workingDirectoryHint') }}</small>
        </label>
        <label><span>{{ t('localAgents.notes') }}</span><textarea v-model="form.notes" rows="3"></textarea></label>
        <label class="check-row"><input v-model="form.enabled" type="checkbox" /><span>{{ t('localAgents.enableAfterSave') }}</span></label>
      </form>
      <template #footer>
        <button class="btn ghost" type="button" @click="showEditor = false">{{ t('common.cancel') }}</button>
        <button class="btn primary" type="button" :disabled="saving" @click="saveRuntime">{{ saving ? t('localAgents.saving') : t('common.save') }}</button>
      </template>
    </WorkbenchModal>
  </section>
</template>

<style scoped>
.integration-page{height:100%;min-height:0;display:flex;flex-direction:column;padding:18px 20px;gap:14px;overflow:hidden;background:transparent}
.integration-head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex:none}.integration-kicker{margin:0 0 7px;font:700 11px/1.2 var(--font-mono);letter-spacing:.14em;color:var(--accent);text-transform:uppercase}.integration-head h1{margin:0;font-size:20px;line-height:1.2;letter-spacing:-.015em}.integration-head p:not(.integration-kicker){margin:5px 0 0;color:var(--text-secondary);font-size:12px}.integration-head-actions{display:flex;align-items:center;gap:8px}.inventory-chip{padding:7px 10px;border:1px solid var(--border);border-radius:8px;color:var(--text-secondary);font-size:12px;background:var(--surface)}
.integration-shell{display:grid;grid-template-columns:minmax(230px,290px) minmax(0,1fr);min-height:0;flex:1;border:1px solid var(--border);border-radius:14px;overflow:hidden;background:var(--surface);box-shadow:none}
.integration-master{min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--border);background:color-mix(in srgb,var(--surface) 95%,var(--page-bg))}.integration-search{margin:14px;display:flex;align-items:center;gap:8px;padding:0 10px;height:36px;border:1px solid var(--border);border-radius:8px;background:var(--input-bg)}.integration-search:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}.integration-search svg{width:15px;fill:none;stroke:var(--text-tertiary);stroke-width:2}.integration-search input{min-width:0;flex:1;border:0;outline:0;background:none;color:var(--text-primary)}
.integration-list{min-height:0;overflow:auto;padding:0 8px 12px}.integration-row{width:100%;display:grid;grid-template-columns:34px minmax(0,1fr) 10px;align-items:center;gap:10px;padding:10px;border:0;border-radius:9px;background:transparent;color:inherit;text-align:left;cursor:pointer}.integration-row:hover{background:var(--surface-hover)}.integration-row.active{background:color-mix(in srgb,var(--accent) 10%,var(--surface))}
.runtime-mark{width:32px;height:32px;flex:0 0 32px;flex-shrink:0;display:grid;place-items:center;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-hover);color:var(--text-secondary);font:750 13px/1 var(--font-mono);box-sizing:border-box}.integration-row.active .runtime-mark,.runtime-mark.large{border-color:color-mix(in srgb,var(--accent) 38%,var(--border));background:color-mix(in srgb,var(--accent) 13%,var(--surface));color:var(--accent)}.runtime-mark.native{border-color:color-mix(in srgb,var(--accent) 45%,var(--border));background:color-mix(in srgb,var(--accent) 16%,var(--surface));color:var(--accent)}.runtime-mark.large{width:44px;height:44px;flex:0 0 44px;flex-shrink:0;border-radius:11px;font-size:16px}.runtime-copy{min-width:0;display:flex;flex-direction:column;gap:3px}.runtime-copy strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}.runtime-copy small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-tertiary);font:11px/1.2 var(--font-mono)}.status-dot{width:7px;height:7px;flex-shrink:0;border-radius:50%;background:var(--text-tertiary);opacity:.45}.status-dot.live{background:#2ba36a;opacity:1;box-shadow:0 0 0 3px color-mix(in srgb,#2ba36a 14%,transparent)}
.integration-detail{min-width:0;min-height:0;overflow:auto;padding:22px 24px}.runtime-detail-head{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px}.runtime-title{min-width:0;flex:1 1 220px;display:flex;align-items:center;gap:12px}.runtime-title>div{min-width:0}.runtime-title h2{margin:0;font-size:19px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.runtime-title p{margin:5px 0 0;color:var(--text-secondary);font-size:12px;overflow-wrap:anywhere}.detail-actions{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:8px}
.route-line{margin:20px 0;display:flex;align-items:center;gap:8px;overflow:auto;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--page-bg)}.route-node{flex:none;padding:6px 9px;border:1px solid var(--border);border-radius:7px;background:var(--surface);font:11px/1.2 var(--font-mono)}.route-node.accent{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));color:var(--accent)}.route-arrow{color:var(--text-tertiary)}
.detail-section{margin-top:14px;border:1px solid var(--border);border-radius:11px;overflow:hidden}.detail-section-head{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 16px;border-bottom:1px solid var(--border)}.detail-section h3{margin:0;font-size:13px}.detail-section-head p{margin:4px 0 0;color:var(--text-tertiary);font-size:11px}.fact-rows{margin:0}.fact-rows>div{display:grid;grid-template-columns:150px minmax(0,1fr);gap:14px;padding:10px 16px;border-top:1px solid var(--border-subtle)}.fact-rows>div:first-child{border-top:0}.fact-rows dt{color:var(--text-secondary);font-size:12px}.fact-rows dd{margin:0;min-width:0;font-size:12px}.fact-rows code{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-primary);font:11px/1.4 var(--font-mono)}.status-label{color:var(--text-tertiary)}.status-label.live{color:#21885a}
.capability-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:14px}.capability-grid div{display:flex;justify-content:space-between;align-items:center;padding:10px;border-radius:8px;background:var(--page-bg);font:11px/1.2 var(--font-mono)}.capability-grid strong{color:#21885a}.detail-error{margin:0;padding:0 14px 14px;color:var(--danger);font-size:12px}.danger-strip{margin-top:14px;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 15px;border:1px solid color-mix(in srgb,var(--danger) 28%,var(--border));border-radius:10px}.danger-strip div{display:flex;flex-direction:column;gap:3px}.danger-strip strong{font-size:12px}.danger-strip span{color:var(--text-tertiary);font-size:11px}
.switch{width:38px;height:22px;padding:2px;border:0;border-radius:99px;background:var(--border-strong);cursor:pointer}.switch span{display:block;width:18px;height:18px;border-radius:50%;background:white;transition:transform .16s ease}.switch.on{background:var(--accent)}.switch.on span{transform:translateX(16px)}
.runtime-form{display:flex;flex-direction:column;gap:14px}.runtime-form label{display:flex;flex-direction:column;gap:6px;color:var(--text-secondary);font-size:12px}.runtime-form input,.runtime-form textarea{width:100%;box-sizing:border-box;border:1px solid var(--border);border-radius:8px;background:var(--input-bg);color:var(--text-primary);padding:9px 10px;outline:0;font:12px/1.45 var(--font-mono)}.runtime-form input:focus,.runtime-form textarea:focus{border-color:var(--accent)}.runtime-form .check-row{flex-direction:row;align-items:center}.runtime-form .check-row input{width:15px}.field-hint{color:var(--text-tertiary);font-size:10px;line-height:1.45}.command-picker{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.command-picker .btn{white-space:nowrap}.preset-assist{padding:13px;border:1px solid color-mix(in srgb,var(--accent) 24%,var(--border));border-radius:10px;background:color-mix(in srgb,var(--accent) 4%,var(--surface))}.preset-assist-head strong{font-size:12px}.preset-assist-head p{margin:4px 0 11px;color:var(--text-secondary);font-size:11px;line-height:1.5}.preset-shortcuts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.preset-shortcuts button{min-width:0;display:grid;grid-template-columns:28px minmax(0,1fr) auto;align-items:center;gap:8px;padding:7px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:inherit;text-align:left;cursor:pointer}.preset-shortcuts button:hover{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));background:var(--surface-hover)}.preset-shortcuts .runtime-mark{width:27px;height:27px;border-radius:7px;font-size:10px}.preset-shortcuts button>span:nth-child(2){min-width:0;display:flex;flex-direction:column;gap:2px}.preset-shortcuts button strong,.preset-shortcuts button small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.preset-shortcuts button strong{font-size:11px}.preset-shortcuts button small{color:var(--text-tertiary);font-size:9px}.preset-open{color:var(--accent);font-size:10px}.custom-runtime-guide{padding:12px 13px;border:1px solid var(--border);border-radius:10px;background:var(--surface)}.custom-runtime-guide>strong{font-size:12px}.custom-runtime-guide ol{margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:7px;list-style:none}.custom-runtime-guide li{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;align-items:start}.custom-runtime-guide li>span{width:19px;height:19px;display:grid;place-items:center;border-radius:50%;background:var(--surface-hover);color:var(--text-secondary);font:700 9px/1 var(--font-mono)}.custom-runtime-guide p{margin:1px 0 0;color:var(--text-secondary);font-size:10px;line-height:1.5}.btn.danger{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 35%,var(--border))}
@media(max-width:820px){.integration-page{padding:18px}.integration-head{align-items:flex-start;flex-direction:column}.integration-head-actions{width:100%;flex-wrap:wrap}.integration-shell{grid-template-columns:1fr;overflow:auto}.integration-master{max-height:260px;border-right:0;border-bottom:1px solid var(--border)}.integration-detail{overflow:visible}.fact-rows>div{grid-template-columns:1fr;gap:4px}.capability-grid{grid-template-columns:1fr}.preset-shortcuts{grid-template-columns:1fr}.command-picker{grid-template-columns:1fr}}
</style>
