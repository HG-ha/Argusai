"""Exercise real main-chat composer handlers with a controlled local bridge."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "argusai/webui/src/components/AssistantPanel.vue"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="local Node is required")

HARNESS = r"""
const source = readFileSync(panelFile, 'utf8');
const names = ['send', 'restoreUnsentInput', 'queueUserInput', 'drainQueuedInput',
  'sendQueuedNow', 'clearPendingTools', 'waitForAssistantIdle', 'cancelRun',
  'regenerateFromUser', 'resolvePersistedUserTimestamp', 'startTrackedAssistantTurn', 'submitPendingBatch'];
function extract(name) {
  const match = new RegExp('^(?:async )?function ' + name + '\\(', 'm').exec(source);
  if (!match) return '';
  const tail = source.slice(match.index + match[0].length);
  const end = /^(?:(?:async )?function |const |let )/m.exec(tail);
  return source.slice(match.index, end ? match.index + match[0].length + end.index : source.length);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function pump() { for (let i = 0; i < 25; i++) await Promise.resolve(); }
function tab(sessionId) {
  return { sessionId, messages: [], queuedInputs: [], inputDraft: '', inputPathRefs: {},
    pendingAttachments: [], pendingVoice: null, previewSelectionContext: null,
    runSeq: 0, sending: false, confirmingAction: false, activeStreamMessage: false,
    activeRunId: '', pendingTools: [], pendingTool: null, name: 'Saved conversation',
    thinkingEnabled: true, thinkingEffort: 'high', webSearchAllowed: true, runtime: {},
    renderWindowStart: 0, badge: {}, lastError: '' };
}
const first = tab('chat-a'), second = tab('chat-b');
const activeTab = { value: first };
const calls = [], toasts = [];
const cancelReceipt = deferred();
let backendBusy = false, failStart = false, historyReceipt = null, startReceipt = null;
let serverHistory = [];
let uid = 0;
const api = {
  async cancelAssistantRun(sid) {
    calls.push(['cancel', sid]);
    return await cancelReceipt.promise;
  },
  async listAssistantRunJobs(sid) {
    calls.push(['state', sid]);
    return { ok: true, running: backendBusy, jobs: [] };
  },
  async startAssistantStream(params) {
    calls.push(['start', params]);
    if (startReceipt) return await startReceipt.promise;
    if (backendBusy || failStart) return { ok: false, error: 'assistant session is already running' };
    return { ok: true, run_id: 'next-run', job: {} };
  },
  async getAssistantHistory(sid) {
    calls.push(['history', sid]);
    if (historyReceipt) return await historyReceipt.promise;
    return { ok: true, history: serverHistory };
  },
  async pauseAssistantAfterCurrentResponse() { return { ok: true }; },
};
const services = {
  activeTab, api, props: { page: 'assistant' },
  HISTORY_LOAD_TIMEOUT_MS: 30000,
  voiceState: { value: '' }, voiceInput: { cancel() {} },
  thinkingEnabled: { value: false }, thinkingEffort: { value: 'low' },
  autoFollowScroll: { value: true },
  editingUserMessageUid: { value: null }, editingUserMessageText: { value: '' },
  composeInputForSend: (text, _refs) => String(text),
  normalizeSelectionReference: value => value ? structuredClone(value) : null,
  wireOutboundVoice: value => value ? structuredClone(value) : null,
  plainJson: value => JSON.parse(JSON.stringify(value)),
  buildContextSnapshot: async () => ({}),
  isDefaultTabName: () => false, deriveSessionName: () => '', persistTabs() {},
  resizeAssistantInput() {}, nextTick: async fn => fn?.(),
  scrollBottom() {}, clearActiveTool() {}, recomputeBadge() {},
  refreshRunJobs: async () => [], t: key => key,
  showToast: (...args) => toasts.push(args),
  withTimeout: async promise => await promise,
  sleep: async () => { await Promise.resolve(); },
  cancelEditUserMessage() { calls.push(['end-edit', activeTab.value.sessionId]); },
  addMsg(role, text, _tool, _options, _blocks, _trace, attachments, target, extra = {}) {
    target.messages.push({ _uid: ++uid, role, text, ts: Date.now(), attachments: attachments || [], ...extra });
    return target.messages.length - 1;
  },
  updateMsg(index, patch, target) { target.messages[index] = { ...target.messages[index], ...patch }; },
  async runAssistantStream(target, _runId, _index, options) {
    calls.push(['stream', target.sessionId, options]);
    target.sending = false; target.activeStreamMessage = false; target.activeRunId = '';
  },
};
const ui = new Function('services', 'with (services) {\n'
  + names.map(extract).filter(Boolean).join('\n')
  + '\nreturn {' + names.filter(name => extract(name)).join(',') + '};\n}')(services);
const starts = () => calls.filter(call => call[0] === 'start');
"""

CASES = [
    ("late_rejected_start_after_cancel_restores_unaccepted_payload", r"""
first.inputDraft = 'Never accepted';
first.pendingAttachments = [{ type: 'text', name: 'keep.txt', text: 'A' }];
startReceipt = deferred();
const pendingSend = ui.send(); await pump();
const pendingCancel = ui.cancelRun(first); await pump();
assert.equal(calls.filter(call => call[0] === 'cancel').length, 0);
startReceipt.resolve({ ok: false, error: 'start rejected' });
cancelReceipt.resolve({ ok: true, running: false });
assert.equal(await pendingSend, false);
assert.equal(await pendingCancel, true);
assert.equal(first.inputDraft, 'Never accepted');
assert.equal(first.pendingAttachments[0].name, 'keep.txt');
assert.equal(first.messages.filter(message => message.role === 'user').length, 0);
"""),
    ("cancel_during_confirmation_context_never_starts_a_late_action", r"""
const context = deferred();
services.buildContextSnapshot = async () => await context.promise;
first.pendingTools = [{ id: 'call-a', pending_id: 'approval-a', version: 1 }];
first.pendingDecisions = {};
api.startAssistantActionStream = async params => {
  calls.push(['action-start', params]);
  return { ok: true, run_id: 'action-run' };
};
const confirmation = ui.submitPendingBatch('allow'); await pump();
cancelReceipt.resolve({ ok: true, running: false });
assert.equal(await ui.cancelRun(first), true);
context.resolve({}); await confirmation;
assert.equal(calls.filter(call => call[0] === 'action-start').length, 0);
assert.equal(first.sending, false);
assert.equal(first.confirmingAction, false);
"""),
    ("queued_direct_send_waits_for_pending_start_receipt_before_cancel", r"""
first.inputDraft = 'Initial request'; startReceipt = deferred();
const originalReceipt = startReceipt;
api.cancelAssistantRun = async sid => {
  calls.push(['cancel', sid]); backendBusy = false;
  return { ok: true, running: false };
};
const original = ui.send(); await pump();
first.queuedInputs = [{ text: 'Send now', displayText: 'Send now',
  attachments: [{ type: 'text', name: 'queued.txt', text: 'Q' }] }];
const direct = ui.sendQueuedNow(0); await pump();
assert.equal(calls.filter(call => call[0] === 'cancel').length, 0);
assert.equal(starts().length, 1);
assert.equal(first.queuedInputs.length, 1);
backendBusy = true; startReceipt = null;
originalReceipt.resolve({ ok: true, run_id: 'late-initial-run', job: {} });
await Promise.all([original, direct]);
assert.equal(calls.filter(call => call[0] === 'cancel').length, 1);
assert.equal(starts().length, 2);
assert.equal(starts()[1][1].message, 'Send now');
assert.equal(starts()[1][1].attachments[0].name, 'queued.txt');
assert.equal(first.queuedInputs.length, 0);
assert.equal(toasts.length, 0);
"""),
    ("queued_send_waits_for_ack_and_real_release", r"""
first.sending = true; backendBusy = true;
const item = { text: 'Follow up', displayText: 'Follow up', pathRefs: {}, attachments: [] };
first.queuedInputs = [item];
const pending = ui.sendQueuedNow(0);
await pump();
assert.equal(starts().length, 0);
assert.deepEqual(first.queuedInputs, [item]);
cancelReceipt.resolve({ ok: true, running: true });
await pump();
assert.equal(starts().length, 0);
assert.equal(first.sending, true);
backendBusy = false;
await pending;
assert.equal(starts().length, 1);
assert.equal(starts()[0][1].session_id, 'chat-a');
assert.equal(first.queuedInputs.length, 0);
"""),
    ("queued_send_stays_on_original_tab", r"""
first.sending = true; backendBusy = true;
first.queuedInputs = [{ text: 'Only for A', displayText: 'Only for A' }];
second.inputDraft = 'B draft'; second.pendingAttachments = [{ name: 'b.txt', type: 'text', text: 'B' }];
const pending = ui.sendQueuedNow(0);
await pump(); activeTab.value = second;
cancelReceipt.resolve({ ok: true }); backendBusy = false;
await pending;
assert.equal(starts()[0][1].session_id, 'chat-a');
assert.equal(starts()[0][1].thinking_enabled, true);
assert.equal(second.inputDraft, 'B draft');
assert.equal(second.pendingAttachments[0].name, 'b.txt');
"""),
    ("queued_failure_preserves_item_and_unrelated_composer", r"""
const item = { text: 'Queued', displayText: 'Queued', attachments: [{ type: 'text', name: 'queued.txt', text: 'Q' }],
  voice: { id: 'voice-q', data_base64: 'AAAA' }, selectionReference: { kind: 'message', text: 'quote' } };
first.queuedInputs = [item]; first.inputDraft = 'New draft';
first.pendingAttachments = [{ type: 'text', name: 'draft.txt', text: 'D' }];
failStart = true;
await ui.sendQueuedNow(0);
assert.deepEqual(first.queuedInputs, [item]);
assert.equal(first.inputDraft, 'New draft');
assert.equal(first.pendingAttachments[0].name, 'draft.txt');
assert.equal(starts()[0][1].attachments[0].name, 'queued.txt');
assert.equal(starts()[0][1].voice.id, 'voice-q');
assert.equal(starts()[0][1].selection_reference.text, 'quote');
assert.equal(first.messages.length, 0);
"""),
    ("queue_snapshots_attachments_voice_and_reference", r"""
first.sending = true; first.inputDraft = 'Later';
first.pendingAttachments = [{ type: 'text', name: 'a.txt', text: 'A' }];
first.pendingVoice = { id: 'voice-a', data_base64: 'AAAA' };
first.previewSelectionContext = { kind: 'message', text: 'Evidence' };
ui.queueUserInput();
assert.equal(first.queuedInputs[0].attachments[0].name, 'a.txt');
assert.equal(first.queuedInputs[0].voice.id, 'voice-a');
assert.equal(first.queuedInputs[0].selectionReference.text, 'Evidence');
assert.equal(first.pendingAttachments.length, 0);
assert.equal(first.pendingVoice, null);
assert.equal(first.previewSelectionContext, null);
"""),
    ("automatic_queue_drain_is_bound_to_tab", r"""
first.queuedInputs = [{ text: 'A queued', displayText: 'A queued', attachments: [{ type: 'text', name: 'a.txt', text: 'A' }] }];
ui.drainQueuedInput(); activeTab.value = second;
await new Promise(resolve => setTimeout(resolve, 5));
assert.equal(starts()[0][1].session_id, 'chat-a');
assert.equal(starts()[0][1].attachments[0].name, 'a.txt');
assert.equal(second.messages.length, 0);
"""),
    ("cancel_rejection_keeps_queue_and_confirmation", r"""
first.sending = true; backendBusy = true;
first.pendingTools = [{ id: 'confirm-1' }]; first.queuedInputs = [{ text: 'Keep me' }];
const pending = ui.sendQueuedNow(0); await pump();
cancelReceipt.resolve({ ok: false, error: 'Bridge refused cancellation' });
await pending;
assert.equal(starts().length, 0);
assert.equal(first.pendingTools[0].id, 'confirm-1');
assert.equal(first.queuedInputs[0].text, 'Keep me');
assert.equal(first.sending, true);
"""),
    ("ordinary_rejected_send_restores_entire_payload", r"""
first.inputDraft = 'Original'; first.pendingAttachments = [{ type: 'text', name: 'original.txt', text: 'A' }];
first.pendingVoice = { id: 'voice-a', data_base64: 'AAAA' };
failStart = true;
assert.equal(await ui.send(), false);
assert.equal(first.inputDraft, 'Original');
assert.equal(first.pendingAttachments[0].name, 'original.txt');
assert.equal(first.pendingVoice.id, 'voice-a');
assert.equal(first.messages.length, 0);
"""),
    ("late_rejection_does_not_overwrite_new_draft", r"""
first.inputDraft = 'Original'; startReceipt = deferred();
const pending = ui.send(); await pump();
first.inputDraft = 'New draft';
startReceipt.resolve({ ok: false, error: 'assistant session is already running' });
await pending;
assert.equal(first.inputDraft, 'New draft');
assert.equal(first.queuedInputs[0].text, 'Original');
"""),
    ("edit_resolves_cancelled_optimistic_timestamp", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Original', ts: 100000,
  attachments: [{ id: 'a', type: 'text', name: 'a.txt' }] }, { _uid: 2, role: 'assistant', text: 'Stopped' }];
serverHistory = [{ role: 'user', content: 'Original', timestamp: 101.234567 }];
await ui.regenerateFromUser(first.messages[0], 'Edited');
assert.equal(starts()[0][1].regenerate_user_before, 101.234567);
assert.equal(first.messages[0].text, 'Edited');
assert.equal(first.messages[0].attachments[0].name, 'a.txt');
"""),
    ("duplicate_user_text_uses_original_turn_order", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Same', ts: 100000 },
  { _uid: 2, role: 'assistant', text: 'First' }, { _uid: 3, role: 'user', text: 'Same', ts: 200000 }];
serverHistory = [{ role: 'user', content: 'Same', timestamp: 101 }, { role: 'assistant', content: 'First' },
  { role: 'user', content: 'Same', timestamp: 201 }];
await ui.regenerateFromUser(first.messages[2], 'Edit second');
assert.equal(starts()[0][1].regenerate_user_before, 201);
assert.equal(first.messages[0].text, 'Same');
"""),
    ("unpersisted_user_is_not_replaced_by_unrelated_history", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Not persisted', ts: 100000 }];
serverHistory = [{ role: 'user', content: 'Another turn', timestamp: 101 }];
await ui.regenerateFromUser(first.messages[0], 'Edited');
assert.equal(starts().length, 0);
assert.equal(first.messages[0].text, 'Not persisted');
"""),
    ("late_history_does_not_touch_replaced_session", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Original', ts: 100000 }];
historyReceipt = deferred();
const pending = ui.regenerateFromUser(first.messages[0], 'Edited'); await pump();
first.sessionId = 'replacement'; first.messages = [{ _uid: 9, role: 'user', text: 'Replacement' }];
first.sending = false; first.runSeq += 1;
historyReceipt.resolve({ ok: true, history: [{ role: 'user', content: 'Original', timestamp: 101 }] });
await pending;
assert.equal(starts().length, 0);
assert.equal(first.messages[0].text, 'Replacement');
assert.equal(first.sending, false);
"""),
    ("idle_confirmation_survives_job_storage_and_transient_bridge_errors", r"""
let query = 0;
api.listAssistantRunJobs = async () => {
  if (++query === 1) throw new Error('Temporary bridge error');
  return { ok: false, error: 'Read-only job store', running: false, jobs: [] };
};
assert.equal(await ui.waitForAssistantIdle(first), true);
assert.equal(query, 2);
"""),
    ("repeated_cancel_uses_one_request", r"""
first.sending = true; backendBusy = true;
const firstCancel = ui.cancelRun(first), secondCancel = ui.cancelRun(first);
await pump();
assert.equal(calls.filter(call => call[0] === 'cancel').length, 1);
cancelReceipt.resolve({ ok: true }); backendBusy = false;
assert.equal(await firstCancel, true);
assert.equal(await secondCancel, true);
assert.equal(first.sending, false);
"""),
    ("ambiguous_duplicate_history_does_not_rewrite_another_user_turn", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Unpersisted', ts: 100000 },
  { _uid: 2, role: 'user', text: 'Same', ts: 200000 }];
serverHistory = [{ role: 'user', content: 'Same', timestamp: 101 },
  { role: 'user', content: 'Same', timestamp: 201 }, { role: 'user', content: 'Same', timestamp: 301 }];
await ui.regenerateFromUser(first.messages[1], 'Edited');
assert.equal(starts().length, 0);
assert.equal(first.messages[1].text, 'Same');
"""),
    ("cancel_while_building_context_restores_unaccepted_draft", r"""
const context = deferred();
services.buildContextSnapshot = async () => await context.promise;
first.inputDraft = 'Not accepted yet';
first.pendingAttachments = [{ type: 'text', name: 'keep.txt', text: 'A' }];
const sending = ui.send(); await pump();
cancelReceipt.resolve({ ok: true });
assert.equal(await ui.cancelRun(first), true);
context.resolve({}); await sending;
assert.equal(starts().length, 0);
assert.equal(first.inputDraft, 'Not accepted yet');
assert.equal(first.pendingAttachments[0].name, 'keep.txt');
assert.equal(first.messages.filter(message => message.role === 'user').length, 0);
"""),
    ("idle_query_failure_never_overwrites_a_completed_answer_or_drains_queue", r"""
first.sending = true;
first.messages = [{ _uid: 1, role: 'assistant', text: '' }];
first.queuedInputs = [{ text: 'Keep queued' }];
Object.assign(services, {
  POLL_STALL_HINT_MS: 5000, POLL_INTERVAL_MS: 300,
  waitForAssistantIdle: ui.waitForAssistantIdle,
  finalizeStreamAssistantMessage(index, result) { first.messages[index].text = result.text; return index; },
  applySessionTokenUsage() {}, setPendingFromResponse() {},
  refreshSessions: async () => {}, dispatchTasksChangedIfNeeded() {}, dispatchSandboxFilesChangedIfNeeded() {},
  refreshAgentThreads: async () => {}, isNearBottom: () => false,
  drainQueuedInput() { calls.push(['drained']); },
});
api.getAssistantStreamEvents = async () => ({ ok: true, done: true, next: 1,
  events: [{ kind: 'run_done', result: { ok: true, text: 'Final answer', status: 'done' } }] });
api.listAssistantRunJobs = async () => { throw new Error('Bridge unavailable'); };
const run = new Function('services', 'with (services) {\n' + extract('runAssistantStream')
  + '\nreturn runAssistantStream;}')(services);
await run(first, 'run', 0);
assert.equal(first.messages[0].text, 'Final answer');
assert.equal(first.sending, true);
assert.equal(first.queuedInputs[0].text, 'Keep queued');
assert.equal(calls.filter(call => call[0] === 'drained').length, 0);
"""),
    ("attachment_addendum_does_not_break_precise_or_optimistic_edit_anchor", r"""
for (const localTimestamp of [101234.567, 100000]) {
  calls.length = 0;
  first.messages = [{ _uid: 1, role: 'user', text: 'Original', ts: localTimestamp,
    attachments: [{ id: 'a', type: 'text', name: 'a.txt' }] }];
  serverHistory = [{ role: 'user', content: 'Original\n\nFiles attached: a.txt\nRead the stored file.', timestamp: 101.234567,
    attachments: [{ id: 'a', type: 'text', name: 'a.txt', sandbox_rel: '_attachments/a.txt' }] }];
  await ui.regenerateFromUser(first.messages[0], 'Edited');
  assert.equal(starts()[0][1].regenerate_user_before, 101.234567);
}
"""),
    ("late_rejection_keeps_new_reference_only_draft", r"""
first.inputDraft = 'Original'; startReceipt = deferred();
const pending = ui.send(); await pump();
first.previewSelectionContext = { kind: 'message', text: 'New reference' };
first.inputPathRefs = { latest: { path: 'new.txt' } };
startReceipt.resolve({ ok: false, error: 'assistant session is already running' });
await pending;
assert.equal(first.previewSelectionContext.text, 'New reference');
assert.equal(first.inputPathRefs.latest.path, 'new.txt');
assert.equal(first.queuedInputs[0].text, 'Original');
"""),
    ("acp_edit_startup_rollback_restores_original_dialogue_and_edit_draft", r"""
first.messages = [{ _uid: 1, role: 'user', text: 'Original', ts: 100000 },
  { _uid: 2, role: 'assistant', text: 'Original branch answer', ts: 101000 }];
serverHistory = [{ role: 'user', content: 'Original', timestamp: 100 }];
services.runAssistantStream = async (target, _runId, _index, options) => {
  options.onRegenerateRollback();
  target.sending = false;
};
await ui.regenerateFromUser(first.messages[0], 'Edited draft');
assert.equal(first.messages.length, 2);
assert.equal(first.messages[0].text, 'Original');
assert.equal(first.messages[1].text, 'Original branch answer');
assert.equal(services.editingUserMessageUid.value, 1);
assert.equal(services.editingUserMessageText.value, 'Edited draft');
"""),
]


@pytest.mark.parametrize("scenario", [case[1] for case in CASES], ids=[case[0] for case in CASES])
def test_assistant_composer(scenario: str) -> None:
    script = (
        "import assert from 'node:assert/strict';\nimport { readFileSync } from 'node:fs';\n"
        f"const panelFile = new URL({json.dumps(PANEL.resolve().as_uri())});\n"
        + HARNESS + "\n" + scenario
    )
    result = subprocess.run(
        [str(NODE), "--input-type=module", "-e", script], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
