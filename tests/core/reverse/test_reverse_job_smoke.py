"""Pytest wrapper for reverse job integration smoke flow."""

from __future__ import annotations

import runpy
from pathlib import Path


def test_smoke_reverse_jobs_script_runs() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "smoke_reverse_jobs.py"
    runpy.run_path(str(script), run_name="__smoke__")
