---
name: race-condition-workflow
description: Use for authorized race-condition and business timing tests such as duplicate coupon redemption, double spending, limit bypass, repeated state transitions, and TOCTOU flaws. Uses one bounded Python script inside the session sandbox to issue a synchronized request burst.
trigger-keywords:
  - "\\b(race condition|race window|double spend|toctou|concurrent request|limit overrun)\\b"
  - "竞态|竞争条件|并发.*(重复|绕过|超限|兑换|下单|扣款)|重复.*(优惠券|退款|领取)"
---

## Race Condition Workflow

Use only on an explicitly authorized workflow and preferably with disposable test accounts/data. A race finding requires a violated business invariant or unauthorized state change; response timing or different response bodies alone are not proof.

### Choose a safe candidate

Good candidates are state transitions that should succeed at most once:

- redeeming one coupon or invite code;
- claiming one reward or limited item;
- submitting one payment, refund, transfer, withdrawal, vote, or booking;
- consuming a one-time token;
- changing the same object from two sessions;
- check-then-act flows such as quota, balance, stock, role, or file replacement.

Do not race destructive production actions, real payments, real inventory, account deletion, password reset, email/SMS delivery, or irreversible external integrations. If the only available action has real-world impact, stop and propose a test-account/staging plan.

### Establish controls first

1. Create or reuse a case and record the expected invariant: for example, “one coupon can reduce the total exactly once.”
2. Capture one representative request with browser/proxy tools, normalize it with `web_request_parse`, and attach it with `artifact_add`.
3. Replay it once with `web_request_replay` to establish the baseline response and state transition.
4. Repeat sequentially two or three times. If the action already succeeds repeatedly without concurrency, report a normal business-logic/replay flaw instead of a race.
5. Record the authoritative state before the burst: balance, stock, redemption count, order count, object version, or another server-side value.

### Run one bounded sandbox script

Do not emit many `http_curl` tool calls: tool-level deduplication and serialization do not provide a synchronized burst. Generate one target-specific script and execute the inner loop in one `run_python_snippet` or `run_python_script` call.

- Default to 5 workers; use 2-10 for normal testing and never exceed 20.
- Hard-code an exact allowed hostname in the script and reject every other hostname before opening a connection.
- Use one request template, the same authenticated test session, and the same body for every worker unless the hypothesis specifically compares two roles.
- Use an `asyncio.Event` as a start gate and `asyncio.gather` for the burst.
- Keep redirects disabled so a target cannot redirect the script out of scope.
- Use short timeouts and cap captured response bodies.
- Prefer `httpx`; if it is absent from the session venv, install it with `sandbox_pip_install`, not host Python.

The generated script must:

1. Parse and validate the exact scheme and hostname before creating the client.
2. Reject worker counts outside 2-20.
3. Create one bounded asynchronous client with redirects disabled.
4. Create all worker tasks behind one `asyncio.Event`, then release the gate once and await them with `asyncio.gather`.
5. Return only per-worker status, elapsed time, response length, a bounded preview, and a hash for comparison.
6. Catch and report individual worker errors without retrying or increasing concurrency automatically.

Do not print full cookies, authorization headers, tokens, or large response bodies. Redact secrets before attaching script output as evidence.

### Verify the invariant after the burst

Immediately query the authoritative state again using a normal read-only request or UI:

- Count committed operations, not just HTTP 2xx responses.
- Check whether duplicate order/refund/ledger rows exist.
- Compare balance, quota, inventory, coupon state, and object version before vs. after.
- Distinguish multiple accepted requests from multiple committed side effects; asynchronous queues may return success while later deduplicating correctly.
- Repeat once with a fresh disposable object/token if the first run is ambiguous. Do not keep increasing concurrency until something breaks.

Confirmed evidence should include the invariant, baseline and sequential-control results, worker count, redacted request template, per-worker status/timing/hash summary, and authoritative before/after state. Attach these with `artifact_add`, then use `evidence_add(kind='http')`.

### Precision limit

The template produces an ordinary synchronized burst suitable for millisecond-scale business race windows. It is not an HTTP/2 single-packet attack and cannot guarantee that final request bytes arrive in one network packet. If only a sub-millisecond window is suspected, report the ordinary burst as inconclusive and recommend a controlled HTTP/2 single-packet test; do not claim the target is safe from a negative result.
