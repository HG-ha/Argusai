"""Background job_ref completion (Loom22 §16.7)."""

from __future__ import annotations

import asyncio
import threading
import time

from argusai.core.assistant.background_jobs import BackgroundJobStore
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


def test_background_job_store_completes() -> None:
    store = BackgroundJobStore()

    def work() -> dict:
        time.sleep(0.05)
        return {"ok": True, "value": 42}

    receipt = store.start(name="demo", arguments={"x": 1}, runner=work, turn_id="t1")
    assert receipt["ok"] is True
    assert receipt["status"] == "running"
    job_ref = receipt["job_ref"]
    deadline = time.time() + 2
    while time.time() < deadline:
        st = store.status(job_ref)
        if st.get("status") in {"done", "failed"}:
            break
        time.sleep(0.02)
    st = store.status(job_ref)
    assert st["status"] == "done"
    assert st["result"]["value"] == 42


def test_background_job_store_cancels_supported_job() -> None:
    store = BackgroundJobStore()
    started = threading.Event()
    release = threading.Event()

    def work() -> dict:
        started.set()
        release.wait(timeout=2)
        return {"ok": False, "status": "cancelled"}

    receipt = store.start(
        name="run_subagent",
        arguments={},
        runner=work,
        cancel=release.set,
    )
    assert started.wait(timeout=1)
    cancelled = store.cancel(receipt["job_ref"])
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelling"

    deadline = time.time() + 2
    while time.time() < deadline:
        status = store.status(receipt["job_ref"])
        if status.get("status") == "cancelled":
            break
        time.sleep(0.02)
    assert store.status(receipt["job_ref"])["status"] == "cancelled"


def test_background_job_store_shutdown_cancels_all_supported_jobs() -> None:
    store = BackgroundJobStore()
    started = threading.Event()
    release = threading.Event()

    receipt = store.start(
        name="run_subagent",
        arguments={},
        runner=lambda: (started.set(), release.wait(timeout=2), {"ok": False})[-1],
        cancel=release.set,
    )
    assert started.wait(timeout=1)

    store.shutdown()

    assert release.is_set()
    deadline = time.time() + 2
    while time.time() < deadline and store.status(receipt["job_ref"]).get("status") != "cancelled":
        time.sleep(0.02)
    assert store.status(receipt["job_ref"])["status"] == "cancelled"


def test_dispatch_parks_background_tool() -> None:
    reg = ToolRegistry()

    async def slow(**kwargs):
        await asyncio.sleep(0.2)
        return {"ok": True, "scanned": True}

    reg.register(
        "nuclei_run_scan",
        "scan",
        {},
        slow,
        completion="background",
        concurrency="serial",
    )
    session = AssistantSession(registry=reg, ai_provider=None)

    async def _run():
        receipt = await session._dispatch_tool("nuclei_run_scan", {"url": "https://example.com"})
        assert receipt.get("job_ref")
        assert receipt.get("status") == "running"
        # Original pairing is complete — must not still be blocking on the scan.
        return receipt

    receipt = asyncio.run(_run())
    job_ref = receipt["job_ref"]
    deadline = time.time() + 3
    while time.time() < deadline:
        st = session.background_job_status_tool(job_ref)
        if st.get("status") == "done":
            break
        time.sleep(0.05)
    st = session.background_job_status_tool(job_ref)
    assert st["status"] == "done"
    assert st["result"]["scanned"] is True


def test_wait_inline_bypasses_park() -> None:
    reg = ToolRegistry()

    async def quick(**kwargs):
        return {"ok": True, "inline": True}

    reg.register("ffuf_run_scan", "scan", {}, quick, completion="background")
    session = AssistantSession(registry=reg, ai_provider=None)

    async def _run():
        return await session._dispatch_tool("ffuf_run_scan", {"wait": True})

    out = asyncio.run(_run())
    assert out.get("inline") is True
    assert "job_ref" not in out
