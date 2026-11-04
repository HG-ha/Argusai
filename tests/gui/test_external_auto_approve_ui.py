"""Keep external confirmation receipts bound to their exact ticket and chat.

Use real Vue reactivity and actual component handlers. Only confirmation/UI
services and asynchronous bridge responses are fixtures; no Agent is started.
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
function extract(name) {
  const match = new RegExp('^(?:async )?function ' + name + '\\(', 'm').exec(source);
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
function ticket(pendingId = 'pending-old', version = 1, callId = 'call-reused') {
  return { id: callId, name: 'run_host_shell', arguments: {}, external_confirmation: true,
    risk: 'high', pending_id: pendingId, version };
}
function block(tool) {
  return { type: 'tool_confirmation', confirm_id: tool.id, name: tool.name,
    external_confirmation: true, pending_id: tool.pending_id, pending_version: tool.version,
    confirm_value: 'allow', cancel_value: 'deny' };
}
function receipt(tool = ticket(), verdict = 'allow') {
  return { pending_id: tool.pending_id, version: tool.version, call_id: tool.id, verdict };
}
function tab(sid) {
  const tool = ticket();
  return reactive({ sessionId: sid, autoApproveMode: false, autoAllowTools: [], sending: true,
    confirmingAction: false, runSeq: 7, pendingTools: [tool], pendingTool: tool,
    pendingDecisions: { [tool.id]: 'allow' }, pendingAutoAllowNames: [], pendingAutoAllowPermissions: [],
    runtime: { runtime_id: 'codex-local', locked: true },
    messages: [{ role: 'assistant', blocks: [block(tool)] }], lastError: '', liveJob: null });
}
const first = tab('chat-a'), second = tab('chat-b');
const activeTab = ref(first);
const autoApproveBusy = ref(false);
const confirmCalls = [], modeCalls = [], actionCalls = [], streamCalls = [], toasts = [];
let deferActions = false;
const api = {
  setAssistantAutoApproveMode(...args) {
    const pending = deferred(); modeCalls.push({ args, pending }); return pending.promise;
  },
  startAssistantActionStream(params) {
    const pending = deferred(); actionCalls.push({ params, pending });
    return deferActions ? pending.promise : Promise.resolve({ ok: false, error: 'Unexpected extra approval' });
  },
  startAssistantStream(params) {
    streamCalls.push(params); return Promise.resolve({ ok: false, error: 'Unexpected new Agent stream' });
  },
};
const services = {
  ref, computed, reactive, nextTick, activeTab, autoApproveBusy, api,
  tabs: reactive([first, second]),
  confirmDialog(options) {
    const pending = deferred(); confirmCalls.push({ options, pending }); return pending.promise;
  },
  t: (key, params = {}) => params.msg ? `${key}: ${params.msg}` : key,
  showToast: (...args) => toasts.push(args),
  applySessionThinking() {}, clearActiveTool() {}, recomputeBadge() {}, persistTabs() {},
  mcpPermissionKey: () => '', isPendingUserDecisionBlock: () => false,
  handleCardAction: (...args) => streamCalls.push(args),
  buildContextSnapshot: async () => ({}), props: { page: 'assistant' },
  autoFollowScroll: ref(false),
  addMsg: () => { assert.fail('Resolving an external ticket must not add an Agent turn'); },
  appendMsgBlock(index, value, target) { target.messages[index].blocks.push(value); },
  updateMsg() {}, runAssistantStream: (...args) => streamCalls.push(args),
};
const names = ['plainJson', 'clearPendingTools', 'setPendingFromResponse',
  'applySessionAssistantPolicy', 'findPendingUserDecisionBlock', 'autoPickDecisionResponse',
  'drainPendingAfterAutoApprove', 'toggleAutoApproveMode', 'isExternalPendingTool',
  'canResolvePendingWhileSending', 'markExternalConfirmationResolved',
  'applyExternalConfirmationReceipt', 'hydrateExternalPendingBlock', 'appendStreamBlock',
  'startTrackedAssistantTurn', 'submitPendingBatch'];
const present = names.filter(name => extract(name));
const ui = new Function('services', 'with (services) {\n'
  + present.map(name => extract(name)).join('\n')
  + '\nreturn {' + present.join(',') + '};\n}')(services);
function replacePending(target, next) {
  target.pendingTools = [next]; target.pendingTool = next;
  target.pendingDecisions = { [next.id]: 'deny' };
  target.messages[0].blocks.push(block(next));
}
async function approveToggleDialog() {
  confirmCalls[0].pending.resolve(true); await pump();
  assert.equal(modeCalls.length, 1);
}
"""

CASES = [
    ("backend_auto_approve_receipt_does_not_resubmit_old_ticket", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
await approveToggleDialog();
modeCalls[0].pending.resolve({ ok: true, auto_approve_mode: true, external_confirmation: receipt() });
await pending;
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
assert.deepEqual(first.pendingTools, []);
assert.equal(first.messages[0].blocks[0].decision, 'allow');
assert.equal(first.autoApproveMode, true);
"""),
    ("new_external_ticket_arriving_during_enable_stays_pending", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
await approveToggleDialog();
const newer = ticket('pending-new', 2);
replacePending(first, newer);
modeCalls[0].pending.resolve({ ok: true, auto_approve_mode: true, external_confirmation: receipt() });
await pending;
assert.equal(first.pendingTools.length, 1);
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
assert.equal(first.pendingTool.pending_id, 'pending-new');
assert.equal(first.pendingDecisions[newer.id], 'deny');
assert.equal(first.messages[0].blocks[0].decision, 'allow');
assert.equal(first.messages[0].blocks[1].decision, undefined);
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("receipt_removes_only_old_ticket_when_new_one_is_also_present", r"""
const older = first.pendingTools[0];
const newer = ticket('pending-new', 2, 'call-new');
first.pendingTools.push(newer); first.messages[0].blocks.push(block(newer));
first.pendingDecisions[newer.id] = 'deny';
ui.applyExternalConfirmationReceipt(first, receipt(older));
assert.deepEqual(first.pendingTools.map(tool => tool.id), ['call-new']);
assert.equal(first.pendingTool.id, 'call-new');
assert.equal(first.pendingDecisions['call-new'], 'deny');
assert.equal(first.messages[0].blocks[0].decision, 'allow');
assert.equal(first.messages[0].blocks[1].decision, undefined);
assert.equal(second.pendingTools.length, 1);
assert.equal(second.messages[0].blocks[0].decision, undefined);
"""),
    ("receipt_does_not_change_different_version_pending_id_or_call_id", r"""
const visibleState = () => JSON.stringify({
  pendingTools: first.pendingTools, pendingTool: first.pendingTool,
  pendingDecisions: first.pendingDecisions, messages: first.messages,
  autoApproveMode: first.autoApproveMode,
});
for (const change of [{ version: 2 }, { pending_id: 'different-ticket' }, { call_id: 'different-call' }]) {
  const before = visibleState();
  ui.applyExternalConfirmationReceipt(first, { ...receipt(), ...change });
  assert.equal(visibleState(), before);
}
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("receipt_is_idempotent_and_preserves_later_same_call_id", r"""
ui.applyExternalConfirmationReceipt(first, receipt());
const newer = ticket('pending-new', 2);
replacePending(first, newer);
ui.applyExternalConfirmationReceipt(first, receipt());
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
assert.equal(first.messages[0].blocks[1].decision, undefined);
"""),
    ("receipt_before_stream_card_does_not_resurrect_old_ticket", r"""
ui.clearPendingTools(first); first.messages[0].blocks = [];
ui.applyExternalConfirmationReceipt(first, receipt());
const delayedOldCard = block(ticket());
ui.appendStreamBlock(0, delayedOldCard, first);
assert.equal(delayedOldCard.decision, 'allow');
assert.deepEqual(first.pendingTools, []);
const newer = ticket('pending-new', 2);
ui.appendStreamBlock(0, block(newer), first);
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
const repeatedOldCard = block(ticket());
ui.appendStreamBlock(0, repeatedOldCard, first);
assert.equal(repeatedOldCard.decision, 'allow');
assert.equal(first.pendingTools.length, 1);
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("late_stream_card_receipt_history_is_scoped_to_original_chat", r"""
ui.applyExternalConfirmationReceipt(first, receipt());
const otherChatCard = block(ticket());
ui.appendStreamBlock(0, otherChatCard, second);
assert.equal(otherChatCard.decision, undefined);
assert.equal(second.pendingTools.length, 1);
assert.equal(second.pendingTools[0].pending_id, 'pending-old');
assert.equal(first.messages[0].blocks[0].decision, 'allow');
"""),
    ("external_pending_is_not_auto_submitted_by_frontend_drain", r"""
first.autoApproveMode = true;
await ui.drainPendingAfterAutoApprove(first);
assert.equal(first.pendingTools.length, 1);
assert.equal(first.messages[0].blocks[0].decision, undefined);
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("cancelled_mode_dialog_does_not_change_policy_or_confirmation", r"""
const before = JSON.stringify(first);
const pending = ui.toggleAutoApproveMode(); await pump();
confirmCalls[0].pending.resolve(false); await pending;
assert.equal(modeCalls.length, 0);
assert.equal(JSON.stringify(first), before);
assert.equal(actionCalls.length, 0);
"""),
    ("replaced_session_during_dialog_cannot_enable_another_chat", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
first.sessionId = 'replacement-chat'; first.autoApproveMode = false;
confirmCalls[0].pending.resolve(true); await pending;
assert.equal(modeCalls.length, 0);
assert.equal(first.autoApproveMode, false);
assert.equal(actionCalls.length, 0);
"""),
    ("late_mode_response_cannot_change_replaced_session", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
await approveToggleDialog();
first.sessionId = 'replacement-chat'; first.autoApproveMode = false;
const newer = ticket('replacement-ticket', 3);
replacePending(first, newer);
modeCalls[0].pending.resolve({ ok: true, auto_approve_mode: true, external_confirmation: receipt() });
await pending;
assert.equal(modeCalls[0].args[0], 'chat-a');
assert.equal(first.autoApproveMode, false);
assert.equal(first.pendingTools[0].pending_id, 'replacement-ticket');
assert.ok(first.messages[0].blocks.every(card => !card.decision));
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("late_mode_failure_cannot_rollback_replaced_session_policy", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
await approveToggleDialog();
first.sessionId = 'replacement-chat'; first.autoApproveMode = true;
replacePending(first, ticket('replacement-ticket', 3));
modeCalls[0].pending.reject(new Error('Old enable request failed'));
await pending;
assert.equal(first.autoApproveMode, true);
assert.equal(first.pendingTools[0].pending_id, 'replacement-ticket');
assert.equal(toasts.length, 0);
assert.equal(actionCalls.length, 0);
"""),
    ("enable_receipt_updates_original_chat_without_touching_active_chat", r"""
const pending = ui.toggleAutoApproveMode(); await pump();
await approveToggleDialog();
activeTab.value = second;
modeCalls[0].pending.resolve({ ok: true, auto_approve_mode: true, external_confirmation: receipt() });
await pending;
assert.equal(first.autoApproveMode, true);
assert.deepEqual(first.pendingTools, []);
assert.equal(first.messages[0].blocks[0].decision, 'allow');
assert.equal(second.autoApproveMode, false);
assert.equal(second.pendingTools.length, 1);
assert.equal(second.messages[0].blocks[0].decision, undefined);
assert.equal(actionCalls.length, 0);
assert.equal(streamCalls.length, 0);
"""),
    ("external_submit_preserves_new_ticket_received_before_ack", r"""
deferActions = true;
const pending = ui.submitPendingBatch('allow'); await pump();
assert.equal(actionCalls.length, 1);
assert.equal(actionCalls[0].params.action.pending_id, 'pending-old');
assert.equal(actionCalls[0].params.action.call_id, 'call-reused');
const newer = ticket('pending-new', 2);
replacePending(first, newer);
actionCalls[0].pending.resolve({ ok: true, resolved_external: true, pending_id: 'pending-old', verdict: 'allow' });
await pending;
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
assert.equal(first.pendingTool.pending_id, 'pending-new');
assert.equal(first.pendingDecisions[newer.id], 'deny');
assert.equal(first.messages[0].blocks[0].decision, 'allow');
assert.equal(first.messages[0].blocks[1].decision, undefined);
assert.equal(first.sending, true);
assert.equal(first.confirmingAction, false);
assert.equal(streamCalls.length, 0);
"""),
    ("external_deny_ack_marks_only_the_captured_old_ticket", r"""
deferActions = true;
const pending = ui.submitPendingBatch('deny'); await pump();
assert.equal(actionCalls[0].params.action.verdict, 'deny');
const newer = ticket('pending-new', 2);
replacePending(first, newer);
actionCalls[0].pending.resolve({ ok: true, resolved_external: true, pending_id: 'pending-old', verdict: 'deny' });
await pending;
assert.equal(first.pendingTools[0].pending_id, 'pending-new');
assert.equal(first.messages[0].blocks[0].decision, 'deny');
assert.equal(first.messages[0].blocks[1].decision, undefined);
assert.equal(first.pendingDecisions[newer.id], 'deny');
assert.equal(streamCalls.length, 0);
"""),
    ("external_submit_cannot_apply_old_ack_to_replaced_session", r"""
deferActions = true;
const pending = ui.submitPendingBatch('allow'); await pump();
first.sessionId = 'replacement-chat';
replacePending(first, ticket('replacement-ticket', 3));
actionCalls[0].pending.resolve({ ok: true, resolved_external: true, pending_id: 'pending-old', verdict: 'allow' });
await pending;
assert.equal(first.pendingTools[0].pending_id, 'replacement-ticket');
assert.ok(first.messages[0].blocks.every(card => !card.decision));
assert.equal(second.pendingTools.length, 1);
assert.equal(streamCalls.length, 0);
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
def test_external_auto_approve_ui(scenario: str) -> None:
    result = _run_scenario(scenario)
    assert result.returncode == 0, result.stdout + result.stderr


def test_greenlight_toolbar_uses_negotiated_capability_and_can_disable():
    source = PANEL.read_text(encoding="utf-8")
    start = source.index("const greenlightAvailable = computed(")
    finish = source.index("const engagementScopeSummary", start)
    declaration = source[start:finish]
    script = f"""
import {{computed, ref}} from {json.dumps(VUE.as_uri())};
import assert from 'node:assert/strict';
const activeTab = ref({{runtimePending:false}});
const isAcpSession = ref(true);
const engagementActive = ref(false);
const sessionRuntime = ref({{acp_prompt_capabilities:{{}}}});
{declaration}
assert.equal(greenlightAvailable.value,false);
sessionRuntime.value.acp_prompt_capabilities.system_prompt=true;
assert.equal(greenlightAvailable.value,true);
activeTab.value.runtimePending=true;
assert.equal(greenlightAvailable.value,false);
activeTab.value.runtimePending=false;
sessionRuntime.value.acp_prompt_capabilities.system_prompt=false;
engagementActive.value=true;
assert.equal(greenlightAvailable.value,true);
engagementActive.value=false;
isAcpSession.value=false;
assert.equal(greenlightAvailable.value,true);
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=WEBUI, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
