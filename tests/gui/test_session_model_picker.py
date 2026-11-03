"""Exercise actual model-picker handlers with Vue reactivity and a deferred bridge.

Only component services and bridge receipts are fixtures. No Agent process,
model endpoint, user settings, or full component lifecycle is started.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBUI = ROOT / "argusai/webui"
PANEL = WEBUI / "src/components/AssistantPanel.vue"
VUE = WEBUI / "node_modules/vue/index.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    not NODE or not VUE.exists(), reason="local Node and installed Vue are required"
)

HARNESS = r"""
const source = readFileSync(panelFile, 'utf8');
function extract(name, variable = false) {
  const prefix = variable ? '(?:const|let) ' : '(?:async )?function ';
  const suffix = variable ? '\\s*=' : '\\(';
  const match = new RegExp('^' + prefix + name + suffix, 'm').exec(source);
  if (!match) return '';
  const tail = source.slice(match.index + match[0].length);
  const end = /^(?:(?:async )?function |const |let |watch\()/m.exec(tail);
  return source.slice(match.index, end ? match.index + match[0].length + end.index : source.length);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function pump() { for (let i = 0; i < 25; i += 1) await Promise.resolve(); }
const nativeRuntime = () => ({
  runtime_id: 'argusai-native', workspace_mode: 'sandbox', project_dir: '', locked: false,
});
const acpRuntime = () => ({
  runtime_id: 'codex-local', workspace_mode: 'project', project_dir: 'C:/fixture/project', locked: false,
});
function tab(sid, runtime) {
  return reactive({ sessionId: sid, runtime, runtimePending: false, _explicitDraft: true,
    messages: [], nativeModel: 'original-model', name: sid });
}
const first = tab('chat-a', nativeRuntime());
const second = tab('chat-b', acpRuntime());
const activeTab = ref(first);
const sessionRuntime = computed({
  get: () => activeTab.value?.runtime,
  set: value => { activeTab.value.runtime = value; },
});
const nativeRows = [
  { index: 0, name: 'DAWA', model: 'native-alpha', value: 'native-alpha', host: 'fixture.invalid', active: true },
  { index: 0, name: 'DAWA', model: 'native-beta', value: 'native-beta', host: 'fixture.invalid', active: false },
  { index: 1, name: 'DeepSeek', model: 'native-gamma', value: 'native-gamma', host: 'fixture.invalid', active: false },
];
const acpRows = [
  { index: 0, config_id: 'model', name: 'Codex Alpha', model: 'acp-alpha', value: 'acp-alpha', host: 'Model', active: true },
  { index: 1, config_id: 'model', name: 'Codex Beta', model: 'acp-beta', value: 'acp-beta', host: 'Model', active: false },
];
const nativeResult = () => ({ ok: true, kind: 'endpoint', models: nativeRows, active: nativeRows[0] });
const acpResult = () => ({ ok: true, kind: 'acp_model', models: acpRows, active: acpRows[0], runtime: acpRuntime() });
const listCalls = [], switchCalls = [], toasts = [];
const api = {
  listSessionModels(...args) {
    const receipt = deferred(); listCalls.push({ args, receipt }); return receipt.promise;
  },
  switchSessionModel(...args) {
    const receipt = deferred(); switchCalls.push({ args, receipt }); return receipt.promise;
  },
};
const services = {
  ref, computed, reactive, nextTick, activeTab, sessionRuntime, api,
  tabs: reactive([first, second]), plainJson: value => JSON.parse(JSON.stringify(value)),
  runtimePending: computed(() => Boolean(activeTab.value?.runtimePending)),
  t: key => key, showToast: (...args) => toasts.push(args), persistTabs() {},
};
const variableDefaults = {
  showModelModal: 'ref(false)', modelsLoading: 'ref(false)', sessionModels: 'ref([])',
  modelError: "ref('')", collapsedModelGroups: 'ref(new Set())', modelSearch: "ref('')",
  modelPickerKind: "ref('endpoint')", modelPickerOwner: 'ref(null)', modelRequestSeq: '0',
  isAcpSession: "computed(() => Boolean(sessionRuntime.value?.runtime_id) && sessionRuntime.value.runtime_id !== 'argusai-native')",
};
const variables = Object.entries(variableDefaults).map(([name, fallback]) =>
  extract(name, true) || `${name === 'modelRequestSeq' ? 'let' : 'const'} ${name} = ${fallback};`);
for (const name of ['sessionModelGroups', 'modelSearchQuery', 'visibleSessionModelGroups']) {
  const declaration = extract(name, true); if (declaration) variables.push(declaration);
}
const names = ['modelPickerKey', 'invalidateModelPicker', 'isModelPickerRequestCurrent',
  'applySessionRuntimeSnapshot', 'modelGroupKey', 'modelGroupTitle', 'modelDisplayName',
  'modelSearchHaystack', 'modelMatchesQuery', 'resetModelGroupCollapse', 'modelErrorText',
  'refreshNativeModel', 'openModelPicker', 'pickModel'];
const present = names.filter(name => extract(name));
const code = variables.join('\n') + '\n' + present.map(name => extract(name)).join('\n');
const ui = new Function('services', 'with (services) {\n' + code
  + '\nreturn {' + [...Object.keys(variableDefaults), 'sessionModelGroups', ...present].join(',') + '};\n}')(services);
"""

CASES = [
    ("acp_loading_clears_previous_native_rows", r"""
ui.sessionModels.value = nativeRows;
activeTab.value = second;
const pending = ui.openModelPicker(); await pump();
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(ui.modelsLoading.value, true);
assert.equal(ui.isAcpSession.value, true);
assert.equal(listCalls[0].args[2], 'codex-local');
assert.equal(listCalls[0].args[3].project_dir, 'C:/fixture/project');
listCalls[0].receipt.resolve(acpResult()); await pending;
assert.deepEqual(ui.sessionModels.value.map(row => row.value), ['acp-alpha', 'acp-beta']);
assert.ok(ui.sessionModelGroups.value.every(group => !['DAWA', 'DeepSeek'].includes(group.title)));
assert.equal(ui.modelsLoading.value, false);
"""),
    ("older_success_cannot_finish_new_tab_loading", r"""
const pendingA = ui.openModelPicker(); await pump();
activeTab.value = second;
const pendingB = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve(nativeResult()); await pendingA;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(ui.modelsLoading.value, true);
assert.equal(ui.modelError.value, '');
listCalls[1].receipt.resolve(acpResult()); await pendingB;
assert.equal(ui.modelPickerKind.value, 'acp_model');
assert.equal(ui.sessionModels.value[0].value, 'acp-alpha');
"""),
    ("older_exception_cannot_clear_new_tab_rows_or_loading", r"""
const pendingA = ui.openModelPicker(); await pump();
activeTab.value = second;
const pendingB = ui.openModelPicker(); await pump();
listCalls[0].receipt.reject(new Error('Old native bridge failure')); await pendingA;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(ui.modelError.value, '');
assert.equal(ui.modelsLoading.value, true);
listCalls[1].receipt.resolve(acpResult()); await pendingB;
assert.equal(ui.sessionModels.value[0].value, 'acp-alpha');
"""),
    ("older_success_cannot_replace_completed_acp_inventory", r"""
const pendingA = ui.openModelPicker(); await pump();
activeTab.value = second;
const pendingB = ui.openModelPicker(); await pump();
listCalls[1].receipt.resolve(acpResult()); await pendingB;
listCalls[0].receipt.resolve(nativeResult()); await pendingA;
assert.equal(ui.sessionModels.value[0].value, 'acp-alpha');
assert.equal(ui.modelPickerKind.value, 'acp_model');
assert.equal(ui.modelsLoading.value, false);
"""),
    ("older_failure_cannot_clear_completed_acp_inventory", r"""
const pendingA = ui.openModelPicker(); await pump();
activeTab.value = second;
const pendingB = ui.openModelPicker(); await pump();
listCalls[1].receipt.resolve(acpResult()); await pendingB;
listCalls[0].receipt.reject(new Error('Old bridge failed')); await pendingA;
assert.equal(ui.sessionModels.value[0].value, 'acp-alpha');
assert.equal(ui.modelPickerKind.value, 'acp_model');
assert.equal(ui.modelError.value, '');
assert.equal(ui.modelsLoading.value, false);
"""),
    ("closed_picker_ignores_its_pending_receipt", r"""
const pending = ui.openModelPicker(); await pump();
ui.showModelModal.value = false; ui.invalidateModelPicker();
assert.equal(ui.modelsLoading.value, false);
listCalls[0].receipt.resolve(nativeResult()); await pending;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(ui.modelError.value, '');
assert.equal(ui.showModelModal.value, false);
"""),
    ("same_tab_replaced_session_rejects_old_inventory", r"""
const pending = ui.openModelPicker(); await pump();
first.sessionId = 'replacement-session';
listCalls[0].receipt.resolve(nativeResult()); await pending;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(ui.modelError.value, '');
assert.equal(first.sessionId, 'replacement-session');
"""),
    ("same_runtime_changed_workspace_rejects_old_inventory", r"""
activeTab.value = second;
const original = JSON.stringify(second.runtime);
const pending = ui.openModelPicker(); await pump();
second.runtime.project_dir = 'C:/fixture/another-project';
listCalls[0].receipt.resolve(acpResult()); await pending;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(second.runtime.project_dir, 'C:/fixture/another-project');
assert.notEqual(JSON.stringify(second.runtime), original);
"""),
    ("acp_failure_keeps_runtime_and_never_shows_native_rows", r"""
activeTab.value = second; ui.sessionModels.value = nativeRows;
const pending = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve({ ok: false, kind: 'acp_model', code: 'acp_model_probe_failed', error: 'Fixture unavailable' });
await pending;
assert.equal(ui.isAcpSession.value, true);
assert.equal(second.runtime.runtime_id, 'codex-local');
assert.equal(ui.modelPickerKind.value, 'acp_model');
assert.deepEqual(ui.sessionModels.value, []);
assert.ok(ui.modelError.value);
assert.equal(ui.modelsLoading.value, false);
"""),
    ("acp_request_rejects_native_response_kind", r"""
activeTab.value = second;
const pending = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve(nativeResult()); await pending;
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(second.runtime.runtime_id, 'codex-local');
assert.ok(ui.modelError.value);
"""),
    ("native_rows_use_model_label_even_when_another_tab_is_acp", r"""
ui.modelPickerKind.value = 'endpoint'; activeTab.value = second;
assert.equal(ui.modelDisplayName(nativeRows[0]), 'native-alpha');
assert.equal(ui.modelDisplayName(nativeRows[1]), 'native-beta');
assert.equal(ui.modelGroupTitle(nativeRows[0]), 'DAWA');
assert.equal(ui.modelGroupKey(nativeRows[0]), ui.modelGroupKey(nativeRows[1]));
ui.modelPickerKind.value = 'acp_model';
assert.equal(ui.modelDisplayName(acpRows[0]), 'Codex Alpha');
"""),
    ("late_native_switch_cannot_close_or_change_acp_picker", r"""
const inventory = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve(nativeResult()); await inventory;
const selecting = ui.pickModel(nativeRows[1]); await pump();
assert.equal(switchCalls[0].args[0], 'chat-a');
activeTab.value = second;
const nextInventory = ui.openModelPicker(); await pump();
switchCalls[0].receipt.resolve({ ok: true, kind: 'endpoint', model: nativeRows[1] }); await selecting;
assert.equal(ui.showModelModal.value, true);
assert.equal(ui.modelsLoading.value, true);
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(second.nativeModel, 'original-model');
assert.deepEqual(second.runtime.acp_config || {}, {});
listCalls[1].receipt.resolve(acpResult()); await nextInventory;
assert.equal(ui.sessionModels.value[0].value, 'acp-alpha');
"""),
    ("same_tab_changed_runtime_rejects_pending_switch", r"""
const inventory = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve(nativeResult()); await inventory;
const selecting = ui.pickModel(nativeRows[1]); await pump();
first.runtime = acpRuntime();
switchCalls[0].receipt.resolve({ ok: true, kind: 'endpoint', model: nativeRows[1] }); await selecting;
assert.equal(first.nativeModel, 'original-model');
assert.equal(first.runtime.runtime_id, 'codex-local');
assert.deepEqual(first.runtime.acp_config || {}, {});
"""),
    ("late_acp_switch_cannot_close_or_change_native_picker", r"""
activeTab.value = second; second.runtime.locked = true;
const inventory = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve({ ...acpResult(), runtime: { ...acpRuntime(), locked: true } }); await inventory;
const selecting = ui.pickModel(acpRows[1]); await pump();
assert.equal(switchCalls[0].args[0], 'chat-b');
activeTab.value = first;
const nextInventory = ui.openModelPicker(); await pump();
switchCalls[0].receipt.resolve({ ok: true, kind: 'acp_model', model: acpRows[1],
  runtime: { ...acpRuntime(), locked: true, acp_config: { model: 'acp-beta' } } }); await selecting;
assert.equal(ui.showModelModal.value, true);
assert.equal(ui.modelsLoading.value, true);
assert.deepEqual(ui.sessionModels.value, []);
assert.equal(first.nativeModel, 'original-model');
assert.equal(first.runtime.runtime_id, 'argusai-native');
listCalls[1].receipt.resolve(nativeResult()); await nextInventory;
assert.equal(ui.modelDisplayName(ui.sessionModels.value[0]), 'native-alpha');
"""),
    ("native_selection_rejects_acp_switch_response", r"""
const inventory = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve(nativeResult()); await inventory;
const selecting = ui.pickModel(nativeRows[1]); await pump();
switchCalls[0].receipt.resolve({ ok: true, kind: 'acp_model', model: acpRows[1], runtime: acpRuntime() });
await selecting;
assert.equal(first.nativeModel, 'original-model');
assert.equal(first.runtime.runtime_id, 'argusai-native');
assert.ok(toasts.some(row => row[1] === 'error'));
"""),
    ("acp_selection_rejects_native_switch_response", r"""
activeTab.value = second; second.runtime.locked = true;
const inventory = ui.openModelPicker(); await pump();
listCalls[0].receipt.resolve({ ...acpResult(), runtime: { ...acpRuntime(), locked: true } }); await inventory;
const selecting = ui.pickModel(acpRows[1]); await pump();
switchCalls[0].receipt.resolve({ ok: true, kind: 'endpoint', model: nativeRows[1], runtime: nativeRuntime() });
await selecting;
assert.equal(second.runtime.runtime_id, 'codex-local');
assert.notEqual(second.runtime.acp_config?.model, 'acp-beta');
assert.ok(toasts.some(row => row[1] === 'error'));
"""),
    ("unknown_runtime_prevents_native_model_reads", r"""
first.runtimePending = true;
await ui.refreshNativeModel(first);
assert.equal(listCalls.length, 0);
"""),
    ("native_model_read_does_not_mutate_replaced_session", r"""
const pending = ui.refreshNativeModel(first); await pump();
assert.equal(listCalls.length, 1);
first.sessionId = 'replacement-session';
listCalls[0].receipt.resolve(nativeResult()); await pending;
assert.equal(first.nativeModel, 'original-model');
"""),
    ("native_read_does_not_accept_acp_response_kind", r"""
const pending = ui.refreshNativeModel(first); await pump();
listCalls[0].receipt.resolve(acpResult()); await pending;
assert.notEqual(first.nativeModel, 'acp-alpha');
assert.equal(first.runtime.runtime_id, 'argusai-native');
"""),
    ("latest_native_read_wins_over_older_response", r"""
const older = ui.refreshNativeModel(first); await pump();
const newer = ui.refreshNativeModel(first); await pump();
listCalls[1].receipt.resolve({ ...nativeResult(), active: nativeRows[1] }); await newer;
listCalls[0].receipt.resolve(nativeResult()); await older;
assert.equal(first.nativeModel, 'native-beta');
"""),
    ("explicit_acp_draft_survives_default_native_snapshot", r"""
const original = JSON.stringify(second.runtime);
ui.applySessionRuntimeSnapshot(second, nativeRuntime());
assert.equal(JSON.stringify(second.runtime), original);
assert.equal(second.runtimePending, false);
"""),
    ("authoritative_runtime_snapshot_resolves_pending_identity", r"""
first.runtimePending = true; first._explicitDraft = false;
ui.applySessionRuntimeSnapshot(first, { ...acpRuntime(), locked: true });
assert.equal(first.runtime.runtime_id, 'codex-local');
assert.equal(first.runtime.locked, true);
assert.equal(first.runtimePending, false);
"""),
]


def _run_scenario(scenario: str, panel: Path = PANEL) -> subprocess.CompletedProcess:
    script = (
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        f"import {{ ref, computed, reactive, nextTick }} from {json.dumps(VUE.resolve().as_uri())};\n"
        f"const panelFile = new URL({json.dumps(panel.resolve().as_uri())});\n"
        + HARNESS + "\n" + scenario + "\nawait nextTick();\n"
    )
    return subprocess.run(
        [str(NODE), "--input-type=module", "-e", script], cwd=WEBUI,
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )


@pytest.mark.parametrize("scenario", [case[1] for case in CASES], ids=[case[0] for case in CASES])
def test_session_model_picker(scenario: str) -> None:
    result = _run_scenario(scenario)
    assert result.returncode == 0, result.stdout + result.stderr
