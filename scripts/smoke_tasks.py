"""Smoke test: spawn 2 tasks concurrently via the TaskManager.

Run me from the project root::

    uv run python scripts/smoke_tasks.py
"""

from __future__ import annotations

import time
from pathlib import Path

import argusai.checks  # noqa: F401 — register
from argusai.core import Runner, ContextSettings
from argusai.core.tasks import TaskManager


def factory(task):
    settings = ContextSettings(headless=True)
    return Runner(bus=task.bus, work_dir=task.root, settings=settings)


def main() -> None:
    mgr = TaskManager(root=Path("tasks"))

    # Start two tasks in parallel
    t1 = mgr.create(
        {
            "url": "https://store.jade.run/",
            "name": "jade-smoke-1",
            "checks": ["recon", "headers", "tls"],
        },
        runner_factory=factory,
    )
    t2 = mgr.create(
        {
            "url": "https://store.jade.run/",
            "name": "jade-smoke-2",
            "checks": ["headers", "tls"],
        },
        runner_factory=factory,
    )

    print(f"spawned {t1.id} and {t2.id}")

    while t1.is_running() or t2.is_running():
        print(
            f"  t1={t1.status} (findings={len(t1.findings)})  "
            f"t2={t2.status} (findings={len(t2.findings)})"
        )
        time.sleep(1.0)

    print("DONE")
    for t in (t1, t2):
        print(
            f"  {t.id}: status={t.status}, "
            f"findings={len(t.findings)}, "
            f"severity={t.meta.severity}, "
            f"elapsed={(t.meta.finished_at or 0) - (t.meta.started_at or 0):.1f}s"
        )


if __name__ == "__main__":
    main()
