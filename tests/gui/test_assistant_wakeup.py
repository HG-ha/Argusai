"""Run the real wake-up timeline projection without a browser or model."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WEBUI = REPO / "argusai" / "webui"
HELPER = WEBUI / "src" / "components" / "assistant" / "panelUtils.js"
COMPONENT = WEBUI / "src" / "components" / "AssistantPanel.vue"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="local Node is required")

HARNESS = r"""
const messages = [
  { _uid: 1, role: 'user', text: 'Review the sample', ts: 100000 },
  { _uid: 2, role: 'assistant', text: 'First result', ts: 110000 },
  { _uid: 3, role: 'assistant', text: 'Scheduled result', ts: 120000 },
];
const wake = (id = 'wake-1', payload = {}, timestamp = 115) => ({
  id, kind: 'cron_wakeup', timestamp,
  payload: { job_id: 'cron-patrol', job_name: 'Sample patrol',
    trigger_kind: 'interval', history_offset: 2, run_id: 'run-1', ...payload },
});
const notices = items => items.filter(item => item.runtimeNotice);
const order = items => items.map(item => item.runtimeNotice ? 'wake' : item.text);
"""

CASES = [
    (
        "places_wakeup_before_its_reply_even_if_timestamp_is_later",
        r"""
const result = mergeScheduledWakeups(messages, [wake('wake-1', {}, 999)]);
assert.deepEqual(order(result), ['Review the sample', 'First result', 'wake', 'Scheduled result']);
const notice = notices(result)[0];
assert.equal(notice._uid, 'cron-wakeup:run-1');
assert.equal(notice.role, 'system');
assert.equal(notice.text, '');
assert.equal(notice.runtimeNotice.jobId, 'cron-patrol');
assert.equal(notice.runtimeNotice.jobName, 'Sample patrol');
assert.equal(notice.runtimeNotice.triggerKind, 'interval');
assert.equal(notice.runtimeNotice.runId, 'run-1');
assert.equal(notice.ts, 999000);
""",
    ),
    (
        "first_message_and_empty_history_wakeup",
        r"""
assert.deepEqual(order(mergeScheduledWakeups(messages, [wake('first', { history_offset: 0 })])),
  ['wake', 'Review the sample', 'First result', 'Scheduled result']);
const result = mergeScheduledWakeups([], [wake('empty', { history_offset: 0 })]);
assert.equal(result.length, 1);
assert.equal(result[0].runtimeNotice.jobId, 'cron-patrol');
""",
    ),
    (
        "ignores_other_session_events",
        r"""
const result = mergeScheduledWakeups(messages, [
  { ...wake(), kind: 'cron_result' },
  { ...wake(), kind: 'user_decision' },
  { ...wake(), kind: 'tool_confirmation' },
]);
assert.deepEqual(result, messages);
""",
    ),
    (
        "deduplicates_event_id_and_run_id",
        r"""
const result = mergeScheduledWakeups(messages, [
  wake(), wake(), wake('same-run-different-event'),
  wake('second-run', { run_id: 'run-2' }, 116),
]);
assert.equal(notices(result).length, 2);
assert.deepEqual(notices(result).map(item => item.runtimeNotice.runId), ['run-1', 'run-2']);
const sameEvent = mergeScheduledWakeups(messages, [
  wake('same-event', { run_id: 'first-run' }),
  wake('same-event', { run_id: 'changed-run' }),
]);
assert.equal(notices(sameEvent).length, 1);
""",
    ),
    (
        "same_offset_keeps_chronological_wakeup_order",
        r"""
const result = mergeScheduledWakeups(messages, [
  wake('later', { run_id: 'run-2' }, 117),
  wake('earlier', { run_id: 'run-1' }, 115),
]);
assert.deepEqual(notices(result).map(item => item.runtimeNotice.runId), ['run-1', 'run-2']);
assert.deepEqual(order(result), ['Review the sample', 'First result', 'wake', 'wake', 'Scheduled result']);
""",
    ),
    (
        "legacy_event_without_offset_uses_timestamp",
        r"""
const event = { id: 'legacy', kind: 'cron_wakeup', timestamp: 115, payload: {} };
const result = mergeScheduledWakeups(messages, [event]);
assert.deepEqual(order(result), ['Review the sample', 'First result', 'wake', 'Scheduled result']);
assert.equal(notices(result)[0].role, 'system');
assert.equal(notices(result)[0]._uid, 'cron-wakeup:legacy');
""",
    ),
    (
        "invalid_offset_uses_timestamp_and_stale_offset_is_discarded",
        r"""
for (const history_offset of [-1, 'invalid', '', '   ']) {
  assert.deepEqual(order(mergeScheduledWakeups(messages, [wake('fallback', { history_offset })])),
    ['Review the sample', 'First result', 'wake', 'Scheduled result']);
}
assert.deepEqual(mergeScheduledWakeups(messages, [wake('stale', { history_offset: 100 })]), messages);
""",
    ),
    (
        "projects_all_trigger_types_and_legacy_run_reference",
        r"""
for (const trigger_kind of ['interval', 'cron', 'webhook', 'file_change']) {
  const result = mergeScheduledWakeups(messages, [wake(trigger_kind, { trigger_kind })]);
  assert.equal(notices(result)[0].runtimeNotice.triggerKind, trigger_kind);
}
const legacyRun = wake('legacy-run', { run_id: '', assistant_run_job_id: 'job-run-1' });
assert.equal(notices(mergeScheduledWakeups(messages, [legacyRun]))[0].runtimeNotice.runId, 'job-run-1');
""",
    ),
    (
        "does_not_mutate_inputs_or_expose_task_prompt_and_context",
        r"""
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
const events = [wake('private', {
  prompt: 'SECRET_PLAYBOOK', context: { token: 'SECRET_CONTEXT' },
  unrelated: 'SECRET_EXTRA',
})];
const original = JSON.stringify({ messages, events });
freeze(messages); freeze(events);
const result = mergeScheduledWakeups(messages, events);
assert.equal(JSON.stringify({ messages, events }), original);
assert.doesNotMatch(JSON.stringify(notices(result)), /SECRET_/);
assert.deepEqual(Object.keys(notices(result)[0].runtimeNotice).sort(),
  ['jobId', 'jobName', 'runId', 'triggerKind']);
""",
    ),
    (
        "live_and_reloaded_history_put_notice_at_the_same_boundary",
        r"""
const event = wake();
const live = mergeScheduledWakeups(messages.slice(0, 2), [event]);
live.push({ ...messages[2] });
const reloaded = mergeScheduledWakeups(JSON.parse(JSON.stringify(messages)), [JSON.parse(JSON.stringify(event))]);
assert.deepEqual(live, reloaded);
""",
    ),
    (
        "regenerate_never_crosses_a_scheduled_wakeup_to_an_old_user",
        r"""
const source = readFileSync(componentFile, 'utf8');
const start = source.indexOf('function previousUserMessage(');
const end = source.indexOf('\nfunction lastAssistantIndex(', start);
assert.ok(start >= 0 && end > start);
const state = { value: structuredClone(messages) };
const previousUser = new Function('messages',
  source.slice(start, end) + '\nreturn previousUserMessage;')(state);
assert.equal(previousUser(2), state.value[0]);
state.value = mergeScheduledWakeups(structuredClone(messages), [wake()]);
assert.equal(previousUser(1), state.value[0]);
assert.equal(previousUser(3), null);
const newUser = { _uid: 4, role: 'user', text: 'Explain the scheduled result', ts: 130000 };
state.value.push(newUser, { _uid: 5, role: 'assistant', text: 'Explanation', ts: 140000 });
assert.equal(previousUser(5), newUser);
""",
    ),
    (
        "notice_prevents_real_component_merging_distinct_turns",
        r"""
const source = readFileSync(componentFile, 'utf8');
const start = source.indexOf('function mergeToolOnlyAssistantMessages(');
const end = source.indexOf('\nfunction scrollToBottomNow(', start);
assert.ok(start >= 0 && end > start);
const mergeTools = new Function('isToolOnlyAssistantMessage',
  source.slice(start, end) + '\nreturn mergeToolOnlyAssistantMessages;')(
    message => Boolean(!message.text && message.toolCalls?.length));
const prior = { _uid: 1, role: 'assistant', text: 'Prior turn result', ts: 100000 };
const tool = { _uid: 2, role: 'assistant', text: '', ts: 120000,
  toolCalls: [{ name: 'read_sample' }], blocks: [], toolTrace: [] };
assert.equal(mergeTools(structuredClone([prior, tool])).length, 1);
const projected = mergeScheduledWakeups([prior, tool], [wake('boundary', { history_offset: 1 })]);
const result = mergeTools(projected);
assert.equal(result.length, 3);
assert.equal(result[1].role, 'system');
assert.deepEqual(result[2].toolCalls, [{ name: 'read_sample' }]);
""",
    ),
]


@pytest.mark.parametrize("scenario", [case[1] for case in CASES], ids=[case[0] for case in CASES])
def test_scheduled_wakeup_projection(scenario: str) -> None:
    script = (
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        f"import {{ mergeScheduledWakeups }} from {json.dumps(HELPER.resolve().as_uri())};\n"
        f"const componentFile = new URL({json.dumps(COMPONENT.resolve().as_uri())});\n"
        + HARNESS + "\n" + scenario
    )
    result = subprocess.run(
        [str(NODE), "--input-type=module", "-e", script], cwd=WEBUI,
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
