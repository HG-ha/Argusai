"""Integration tests for android_objection_run agent tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools.native_reverse import android_objection_run


@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
@patch("argusai.core.tools.native_reverse.run_objection_command")
def test_android_objection_run_attaches_artifact(mock_run: MagicMock, _mock_preflight: MagicMock, tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    job = store.create(title="objection test", target_type="android", platform="android")
    api = type("Api", (), {"reverse_jobs": store})()
    mock_run.return_value = {
        "ok": True,
        "output": "SSL pinning disabled",
        "command": "android sslpinning disable",
        "package": "com.example.app",
    }

    out = android_objection_run(
        api,
        package="com.example.app",
        command="android sslpinning disable",
        job_id=job["id"],
    )
    assert out["ok"] is True
    assert "attached" in out
    assert out["attached"]["artifact"]["kind"] == "trace"
    arts = store.get(job["id"])["artifacts"]
    assert len(arts) == 1
    assert arts[0]["tool"] == "android_objection_run"
