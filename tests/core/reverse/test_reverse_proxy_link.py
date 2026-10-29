"""Tests for reverse_job_link_proxy_flow."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from argusai.core.reverse.reverse_artifacts import ReverseArtifactStore
from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools import EXECUTORS
from argusai.core.tools.native_reverse import reverse_job_link_proxy_flow


def test_executor_registered() -> None:
    assert "reverse_job_link_proxy_flow" in EXECUTORS


def test_link_proxy_flow_attaches_har(tmp_path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    ReverseArtifactStore(store)
    job = store.create(title="proxy-link", target_type="android", platform="android")

    har = {"log": {"version": "1.2", "entries": [{"request": {"url": "https://api.example.com"}}]}}
    api = MagicMock()
    api.reverse_jobs = store
    api.get_proxy_flow_har.return_value = {
        "ok": True,
        "har": har,
        "flow_summary": {"id": 42, "url": "https://api.example.com", "method": "GET", "status": 200},
    }

    out = reverse_job_link_proxy_flow(api, job_id=job["id"], flow_id=42, note="login api")

    assert out["ok"] is True
    assert out["evidence_ref"]["type"] == "proxy_flow"
    assert out["evidence_ref"]["ref_id"] == "42"
    assert out["attached"]["artifact_ref"].startswith("rev://")
    api.get_proxy_flow_har.assert_called_once_with(42)

    job_data = store.get(job["id"])
    assert len(job_data.get("evidence_refs") or []) == 1
    arts = job_data.get("artifacts") or []
    assert any(a.get("tool") == "reverse_job_link_proxy_flow" for a in arts)
    rel = next(a["path"] for a in arts if a.get("tool") == "reverse_job_link_proxy_flow")
    payload = json.loads((store.root / job["id"] / rel).read_text(encoding="utf-8"))
    assert payload["log"]["entries"][0]["request"]["url"] == "https://api.example.com"
