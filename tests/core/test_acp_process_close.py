"""ACP process shutdown should not race the SDK message queue."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from argusai.core.agent_runtimes.acp_runtime import ACPAgentProcess
from argusai.core.agent_runtimes.models import AgentRuntimeConfig


def test_acp_process_close_stops_subprocess_before_connection() -> None:
    order: list[str] = []

    process = ACPAgentProcess(
        AgentRuntimeConfig(id="x", name="x", backend="acp", command="x"),
    )

    class FakeProc:
        returncode = None
        stdin = SimpleNamespace(close=lambda: order.append("stdin_close"))
        pid = 1

        async def wait(self):
            order.append("process_wait")
            self.returncode = 0

    connection = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("connection_close")))
    process.process = FakeProc()
    process.connection = connection
    process._stderr_task = None

    asyncio.run(process.close())

    assert order.index("process_wait") < order.index("connection_close")
    assert process.process is None
    assert process.connection is None
