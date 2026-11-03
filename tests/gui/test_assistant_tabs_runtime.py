"""Run the real tab store with Vue reactivity and a deferred session-list bridge.

Only the bridge and localStorage are fixtures; no user configuration, Agent,
model endpoint or browser profile is loaded.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WEBUI = REPO / "argusai" / "webui"
STORE = WEBUI / "src" / "composables" / "useAssistantTabs.js"
VUE = WEBUI / "node_modules" / "vue" / "index.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not NODE or not VUE.exists(), reason="local Node and installed Vue are required"
)

HARNESS = r"""
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
let moduleSequence = 0;
const storageKey = 'argusai.assistant.tabs.v1';
const acpRuntime = {
  runtime_id: 'codex-local', workspace_mode: 'project', project_dir: 'C:/fixture/project', locked: false,
};
const savedNative = {
  id: 'saved-native', name: 'Previous conversation', runtime_id: 'argusai-native',
  workspace_mode: 'sandbox', project_dir: '',
};
async function loadStore(storage = new Map()) {
  const receipt = deferred();
  let nextList = () => receipt.promise;
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
  };
  globalThis.__tabsApi = { listAssistantSessions: () => nextList() };
  const source = readFileSync(storeFile, 'utf8')
    .replace("from 'vue'", `from ${JSON.stringify(vueUrl)}`)
    .replace("import { api } from '../api.js'", 'const api = globalThis.__tabsApi');
  const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}#${++moduleSequence}`);
  const store = module.useAssistantTabs();
  async function finish(rows) {
    receipt.resolve(rows);
    await store.refreshSessions(true);
    await nextTick();
  }
  return { store, storage, finish, nextList: fn => { nextList = fn; } };
}
"""

CASES = [
    (
        "known_saved_runtime_is_seeded_before_history",
        r"""
const fixture = await loadStore();
const codex = { id: 'saved-codex', name: 'Codex', ...acpRuntime };
const cursor = { id: 'saved-cursor', name: 'Cursor', runtime_id: 'cursor-local', workspace_mode: 'sandbox', project_dir: '' };
await fixture.finish([savedNative, codex, cursor]);
const tab = fixture.store.openTab({ sessionId: codex.id });
assert.equal(tab.runtime.runtime_id, 'codex-local');
assert.equal(tab.runtime.workspace_mode, 'project');
assert.equal(tab.runtime.project_dir, 'C:/fixture/project');
assert.equal(tab.runtime.locked, true);
assert.equal(tab.runtimePending, false);
const replaced = fixture.store.replaceActiveTabSession(cursor.id);
assert.equal(replaced, tab);
assert.equal(tab.runtime.runtime_id, 'cursor-local');
assert.equal(tab.runtime.project_dir, '');
assert.equal(tab.runtimePending, false);
""",
    ),
    (
        "late_restore_does_not_replace_explicit_acp_draft",
        r"""
const fixture = await loadStore();
const draft = fixture.store.openTab({ runtime: acpRuntime });
await fixture.finish([savedNative]);
assert.equal(fixture.store.activeTab.value, draft);
assert.equal(draft.runtime.runtime_id, 'codex-local');
assert.equal(draft.runtimePending, false);
assert.equal(fixture.store.preferLastConversation(), false);
""",
    ),
    (
        "explicit_native_draft_is_protected_but_automatic_tab_is_not",
        r"""
const fixture = await loadStore();
const draft = fixture.store.openTab({ name: '新会话' });
const draftId = draft.sessionId;
await fixture.finish([savedNative]);
assert.equal(fixture.store.activeTab.value, draft);
assert.equal(draft.sessionId, draftId);
fixture.store.closeTab(draft.sessionId);
assert.equal(fixture.store.preferLastConversation(), true);
assert.equal(fixture.store.activeTab.value.sessionId, savedNative.id);
""",
    ),
    (
        "late_native_record_does_not_overwrite_unlocked_acp_draft",
        r"""
const fixture = await loadStore();
const draft = fixture.store.openTab({ runtime: acpRuntime });
await fixture.finish([{ ...savedNative, id: draft.sessionId }]);
assert.equal(draft.runtime.runtime_id, 'codex-local');
assert.equal(draft.runtime.workspace_mode, 'project');
assert.equal(draft.runtime.locked, false);
const matching = { id: draft.sessionId, name: 'Saved Codex', ...acpRuntime };
fixture.nextList(async () => [matching]);
await fixture.store.refreshSessions(true);
assert.equal(draft.runtime.runtime_id, 'codex-local');
assert.equal(draft.runtime.locked, true);
assert.equal(draft.runtimePending, false);
""",
    ),
    (
        "legacy_unlocked_workspace_draft_survives_refresh",
        r"""
const persisted = {
  tabIds: ['legacy-project'], activeTabId: 'legacy-project', names: {},
  runtimes: { 'legacy-project': { runtime_id: 'argusai-native', workspace_mode: 'project', project_dir: 'C:/fixture/project', locked: false } },
};
const fixture = await loadStore(new Map([[storageKey, JSON.stringify(persisted)]]));
const draft = fixture.store.activeTab.value;
await fixture.finish([{ ...savedNative, id: draft.sessionId }]);
assert.equal(draft.runtime.workspace_mode, 'project');
assert.equal(draft.runtime.project_dir, 'C:/fixture/project');
assert.equal(draft.runtime.locked, false);
""",
    ),
    (
        "unknown_saved_runtime_stays_pending_until_metadata_arrives",
        r"""
const fixture = await loadStore();
const tab = fixture.store.openTab({ sessionId: 'unknown-saved' });
assert.equal(tab.runtimePending, true);
const row = { id: tab.sessionId, name: 'Saved Codex', ...acpRuntime };
await fixture.finish([row]);
assert.equal(tab.runtime.runtime_id, 'codex-local');
assert.equal(tab.runtimePending, false);
const replaced = fixture.store.replaceActiveTabSession('another-unknown');
assert.equal(replaced.runtimePending, true);
""",
    ),
    (
        "explicit_draft_survives_persisted_restore",
        r"""
const original = await loadStore();
await original.finish([]);
const draft = original.store.openTab({ runtime: acpRuntime });
const id = draft.sessionId;
const restored = await loadStore(original.storage);
assert.equal(restored.store.activeTab.value.sessionId, id);
assert.equal(restored.store.activeTab.value.runtimePending, false);
await restored.finish([savedNative]);
assert.equal(restored.store.activeTab.value.sessionId, id);
assert.equal(restored.store.activeTab.value.runtime.runtime_id, 'codex-local');
""",
    ),
    (
        "automatic_restore_still_prefers_desktop_history_and_filters_channels",
        r"""
const fixture = await loadStore();
await fixture.finish([{ ...savedNative, id: 'channel-owned', origin: 'channel' }, savedNative]);
assert.equal(fixture.store.activeTab.value.sessionId, savedNative.id);
assert.deepEqual(fixture.store.sessions.value.map(row => row.id), [savedNative.id]);
assert.equal(fixture.store.tabs.some(tab => tab.sessionId === 'channel-owned'), false);
assert.equal(fixture.store.activeTab.value.runtimePending, false);
""",
    ),
]


@pytest.mark.parametrize("scenario", [case[1] for case in CASES], ids=[case[0] for case in CASES])
def test_assistant_tabs_runtime(scenario: str) -> None:
    script = (
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        f"import {{ nextTick }} from {json.dumps(VUE.resolve().as_uri())};\n"
        f"const vueUrl = {json.dumps(VUE.resolve().as_uri())};\n"
        f"const storeFile = new URL({json.dumps(STORE.resolve().as_uri())});\n"
        + HARNESS
        + "\n"
        + scenario
    )
    result = subprocess.run(
        [str(NODE), "--input-type=module", "-e", script],
        cwd=WEBUI,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
