"""Exercise the real TeamPanel send handlers against a deferred GUI bridge.

Vue's refs and computed values are real; only the bridge and browser/lifecycle
services are replaced. No model, network request, or persistent team is used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WEBUI = REPO / "argusai" / "webui"
COMPONENT = WEBUI / "src" / "components" / "TeamPanel.vue"
VUE = WEBUI / "node_modules" / "vue" / "index.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not NODE or not VUE.exists(), reason="local Node and installed Vue are required"
)

HARNESS = r"""
const source = readFileSync(componentFile, 'utf8');
const setup = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import[^\n]*\n/gm, '');
const calls = [];
const toasts = [];
const bridgeOverrides = new Map();
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
async function until(predicate) {
  for (let tick = 0; tick < 30; tick += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.fail('Expected bridge operation did not start');
}
let resolveSend;
let rejectSend;
const sendReceipt = new Promise((resolve, reject) => {
  resolveSend = resolve;
  rejectSend = reject;
});
const roster = [
  { agent_id: 'member-a', role: 'analyst', status: 'paused' },
  { agent_id: 'member-b', role: 'reporter', status: 'paused' },
];
const api = {
  async call(method, ...args) {
    calls.push({ method, args });
    if (method === 'send_team_message') return sendReceipt;
    if (bridgeOverrides.has(method)) return bridgeOverrides.get(method)(...args);
    if (method === 'get_team') {
      return { ok: true, team: { team_id: args[0], members: roster } };
    }
    if (method === 'get_team_chat') return { ok: true, messages: [] };
    if (method === 'get_team_member') {
      return { ok: true, role: 'analyst', history: [] };
    }
    throw new Error(`Unexpected bridge call: ${method}`);
  },
};
const services = {
  ref,
  computed,
  nextTick,
  onMounted: () => {},
  onUnmounted: () => {},
  api,
  t: (key, params = {}) => params.msg ? `${key}: ${params.msg}` : key,
  showToast: (...args) => toasts.push(args),
  useConfirm: () => async () => true,
  useLiveChannel: () => {},
  useSandboxBrowser: () => ({}),
  requestAnimationFrame: callback => callback(),
  setTimeout: callback => callback(),
};
const createSetup = new Function(...Object.keys(services), setup + `
return {
  sendSteer, onComposerKeydown, steerDraft, pendingAttachments, sendingSteer,
  activeTeamId, activeMemberId, teamDetail, memberDetail, teamChat,
  chatTargetId, mentionOpen, teamWebSearch,
};
`);
const ui = createSetup(...Object.values(services));
ui.activeTeamId.value = 'team-a';
ui.teamDetail.value = { team_id: 'team-a', members: roster };
const sendCalls = () => calls.filter(call => call.method === 'send_team_message');
const refreshCalls = () => calls.filter(call => call.method !== 'send_team_message');
const accepted = { ok: true, message: { id: 'message-1' }, delivered: ['member-a'] };
"""

CASES = [
    (
        "clears_text_and_attachments_before_bridge_receipt",
        r"""
ui.steerDraft.value = '  inspect login  ';
ui.pendingAttachments.value = [{ id: 'scope', name: 'scope.txt' }];
ui.mentionOpen.value = true;
const pending = ui.sendSteer();
assert.equal(ui.steerDraft.value, '');
assert.equal(ui.pendingAttachments.value.length, 0);
assert.equal(ui.mentionOpen.value, false);
assert.equal(ui.sendingSteer.value, true);
assert.equal(sendCalls().length, 1);
assert.equal(sendCalls()[0].args[1], 'inspect login');
resolveSend(accepted);
await pending;
assert.equal(ui.sendingSteer.value, false);
assert.equal(toasts.length, 0);
""",
    ),
    (
        "late_success_preserves_next_draft_and_attachments",
        r"""
ui.steerDraft.value = 'first instruction';
ui.pendingAttachments.value = [{ id: 'first', name: 'first.txt' }];
const pending = ui.sendSteer();
ui.steerDraft.value = 'next instruction';
ui.pendingAttachments.value = [{ id: 'next', name: 'next.txt' }];
resolveSend(accepted);
await pending;
assert.equal(ui.steerDraft.value, 'next instruction');
assert.deepEqual(ui.pendingAttachments.value.map(row => row.id), ['next']);
assert.deepEqual(sendCalls()[0].args[4].map(row => row.id), ['first']);
""",
    ),
    (
        "persisted_message_with_failed_delivery_is_not_restored",
        r"""
ui.steerDraft.value = 'already in the room';
ui.pendingAttachments.value = [{ id: 'scope', name: 'scope.txt' }];
const pending = ui.sendSteer();
resolveSend({
  ok: false,
  message: { id: 'persisted-message' },
  delivered: [],
  errors: [{ agent_id: 'member-a', error: 'confirmation required' }],
});
await pending;
assert.equal(ui.steerDraft.value, '');
assert.equal(ui.pendingAttachments.value.length, 0);
assert.equal(ui.sendingSteer.value, false);
assert.ok(toasts.some(row => row[1] === 'error'));
assert.ok(toasts.some(row => String(row[0]).includes('member-a: confirmation required')));
assert.ok(refreshCalls().some(call => call.method === 'get_team_chat'));
""",
    ),
    (
        "unaccepted_rejection_restores_original_text_and_attachments",
        r"""
ui.steerDraft.value = '  preserve this original text\n';
ui.pendingAttachments.value = [{ id: 'scope', name: 'scope.txt' }];
const pending = ui.sendSteer();
resolveSend({ ok: false, error: 'no matching team members' });
await pending;
assert.equal(ui.steerDraft.value, '  preserve this original text\n');
assert.deepEqual(ui.pendingAttachments.value.map(row => row.id), ['scope']);
assert.equal(ui.sendingSteer.value, false);
assert.ok(toasts.some(row => String(row[0]).includes('no matching team members')));
""",
    ),
    (
        "bridge_exception_restores_unaccepted_draft",
        r"""
ui.steerDraft.value = 'retry after bridge recovery';
const pending = ui.sendSteer();
rejectSend(new Error('bridge disconnected'));
await pending;
assert.equal(ui.steerDraft.value, 'retry after bridge recovery');
assert.equal(ui.sendingSteer.value, false);
assert.ok(toasts.some(row => String(row[0]).includes('bridge disconnected')));
""",
    ),
    (
        "failed_send_does_not_overwrite_new_text",
        r"""
ui.steerDraft.value = 'old instruction';
ui.pendingAttachments.value = [{ id: 'old', name: 'old.txt' }];
const pending = ui.sendSteer();
ui.steerDraft.value = 'new instruction';
resolveSend({ ok: false, error: 'rejected' });
await pending;
assert.equal(ui.steerDraft.value, 'new instruction');
assert.equal(ui.pendingAttachments.value.length, 0);
""",
    ),
    (
        "failed_send_does_not_overwrite_new_attachment",
        r"""
ui.steerDraft.value = 'old instruction';
ui.pendingAttachments.value = [{ id: 'old', name: 'old.txt' }];
const pending = ui.sendSteer();
ui.pendingAttachments.value = [{ id: 'new', name: 'new.txt' }];
rejectSend(new Error('bridge disconnected'));
await pending;
assert.equal(ui.steerDraft.value, '');
assert.deepEqual(ui.pendingAttachments.value.map(row => row.id), ['new']);
""",
    ),
    (
        "switching_team_does_not_refresh_or_clear_the_new_team",
        r"""
ui.steerDraft.value = 'team A instruction';
const pending = ui.sendSteer();
ui.activeTeamId.value = 'team-b';
ui.teamDetail.value = { team_id: 'team-b', members: [] };
ui.steerDraft.value = 'team B draft';
resolveSend(accepted);
await pending;
assert.equal(sendCalls()[0].args[0], 'team-a');
assert.equal(ui.steerDraft.value, 'team B draft');
assert.equal(ui.teamDetail.value.team_id, 'team-b');
assert.equal(refreshCalls().length, 0);
""",
    ),
    (
        "switching_private_member_does_not_restore_or_refresh_old_context",
        r"""
ui.activeMemberId.value = 'member-a';
ui.steerDraft.value = 'private to A';
const pending = ui.sendSteer();
ui.activeMemberId.value = 'member-b';
ui.memberDetail.value = { role: 'reporter' };
resolveSend({ ok: false, error: 'rejected' });
await pending;
assert.equal(ui.steerDraft.value, '');
assert.equal(ui.memberDetail.value.role, 'reporter');
assert.equal(refreshCalls().length, 0);
""",
    ),
    (
        "switching_recipient_does_not_restore_old_draft",
        r"""
ui.chatTargetId.value = 'member-a';
ui.steerDraft.value = 'addressed to A';
const pending = ui.sendSteer();
ui.chatTargetId.value = 'member-b';
resolveSend({ ok: false, error: 'rejected' });
await pending;
assert.equal(ui.steerDraft.value, '');
assert.equal(ui.chatTargetId.value, 'member-b');
""",
    ),
    (
        "late_team_refresh_does_not_replace_another_teams_state",
        r"""
const teamResult = deferred();
const chatResult = deferred();
bridgeOverrides.set('get_team', () => teamResult.promise);
bridgeOverrides.set('get_team_chat', () => chatResult.promise);
ui.steerDraft.value = 'team A instruction';
const pending = ui.sendSteer();
resolveSend(accepted);
await until(() => refreshCalls().some(call => call.method === 'get_team_chat'));
ui.activeTeamId.value = 'team-b';
ui.teamDetail.value = { team_id: 'team-b', members: [] };
ui.teamChat.value = [{ id: 'team-b-message', content: 'B conversation' }];
teamResult.resolve({ ok: true, team: { team_id: 'team-a', members: roster } });
chatResult.resolve({ ok: true, messages: [{ id: 'team-a-message' }] });
await pending;
assert.equal(ui.teamDetail.value.team_id, 'team-b');
assert.deepEqual(ui.teamChat.value.map(row => row.id), ['team-b-message']);
""",
    ),
    (
        "late_private_member_refresh_does_not_replace_new_member",
        r"""
const memberResult = deferred();
bridgeOverrides.set('get_team_member', () => memberResult.promise);
ui.activeMemberId.value = 'member-a';
ui.steerDraft.value = 'private to A';
const pending = ui.sendSteer();
resolveSend(accepted);
await until(() => refreshCalls().some(call => call.method === 'get_team_member'));
ui.activeMemberId.value = 'member-b';
ui.memberDetail.value = { role: 'reporter', history: [{ content: 'B history' }] };
memberResult.resolve({ ok: true, role: 'analyst', history: [{ content: 'A history' }] });
await pending;
assert.equal(ui.memberDetail.value.role, 'reporter');
assert.equal(ui.memberDetail.value.history[0].content, 'B history');
""",
    ),
    (
        "duplicate_submit_keeps_the_next_draft_and_sends_once",
        r"""
ui.steerDraft.value = 'first instruction';
const pending = ui.sendSteer();
ui.steerDraft.value = 'next instruction';
await ui.sendSteer();
assert.equal(sendCalls().length, 1);
assert.equal(ui.steerDraft.value, 'next instruction');
resolveSend(accepted);
await pending;
assert.equal(ui.steerDraft.value, 'next instruction');
""",
    ),
    (
        "attachment_only_send_uses_a_stable_snapshot",
        r"""
const selected = [{ id: 'first', name: 'first.txt' }];
ui.pendingAttachments.value = selected;
const pending = ui.sendSteer();
selected.push({ id: 'later', name: 'later.txt' });
assert.deepEqual(sendCalls()[0].args[4].map(row => row.id), ['first']);
assert.equal(ui.pendingAttachments.value.length, 0);
resolveSend(accepted);
await pending;
""",
    ),
    (
        "group_mentions_and_private_routing_keep_their_scope",
        r"""
ui.steerDraft.value = '@analyst inspect this';
ui.teamWebSearch.value = false;
const pending = ui.sendSteer();
assert.deepEqual(sendCalls()[0].args[2], ['member-a']);
assert.equal(sendCalls()[0].args[3], 'group');
assert.equal(sendCalls()[0].args[5].allow_web_search, false);
resolveSend(accepted);
await pending;

ui.activeMemberId.value = 'member-b';
ui.chatTargetId.value = 'all';
ui.steerDraft.value = '@all this is still private';
await ui.sendSteer();
assert.deepEqual(sendCalls()[1].args[2], ['member-b']);
assert.equal(sendCalls()[1].args[3], 'private');
assert.ok(refreshCalls().some(call =>
  call.method === 'get_team_member' && call.args[1] === 'member-b'));
""",
    ),
    (
        "ime_enter_does_not_send",
        r"""
ui.steerDraft.value = '正在输入中文';
ui.onComposerKeydown({ key: 'Enter', isComposing: true, keyCode: 13, preventDefault() {} });
ui.onComposerKeydown({ key: 'Enter', isComposing: false, keyCode: 229, preventDefault() {} });
assert.equal(sendCalls().length, 0);
assert.equal(ui.steerDraft.value, '正在输入中文');
""",
    ),
]


@pytest.mark.parametrize("scenario", [case[1] for case in CASES], ids=[case[0] for case in CASES])
def test_team_composer(scenario: str) -> None:
    script = (
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        f"import {{ ref, computed, nextTick }} from {json.dumps(VUE.resolve().as_uri())};\n"
        f"const componentFile = new URL({json.dumps(COMPONENT.resolve().as_uri())});\n"
        + HARNESS
        + "\n"
        + scenario
        + "\nawait nextTick();\n"
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
