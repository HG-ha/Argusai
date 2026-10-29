"""Tests for external HAR / Burp log import helpers."""

from __future__ import annotations

import json
from pathlib import Path

from argusai.core.security.proxy_import import (
    analyze_burp_log_text,
    analyze_har_path,
    parse_burp_proxy_log,
)


def test_parse_burp_proxy_log_extracts_request() -> None:
    text = """================================================================================
GET /api/v1/user HTTP/1.1
Host: api.example.com
User-Agent: Burp

================================================================================
"""
    parsed = parse_burp_proxy_log(text)
    assert parsed["ok"] is True
    assert parsed["request_count"] == 1
    req = parsed["requests"][0]
    assert req["method"] == "GET"
    assert req["url"] == "https://api.example.com/api/v1/user"


def test_analyze_burp_log_text_includes_replay_hint() -> None:
    text = """GET /login HTTP/1.1
Host: app.example.com

username=test
"""
    out = analyze_burp_log_text(text)
    assert out["ok"] is True
    assert out["entry_count"] == 1
    assert out["requests"][0]["method"] == "GET"


def test_analyze_har_path_reads_file(tmp_path: Path) -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://api.example.com/token",
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "postData": {"text": "{\"user\":\"a\"}"},
                    },
                    "response": {"status": 200},
                }
            ]
        }
    }
    path = tmp_path / "capture.har"
    path.write_text(json.dumps(har), encoding="utf-8")
    out = analyze_har_path(path)
    assert out["ok"] is True
    assert out["entry_count"] == 1
    assert out["requests"][0]["method"] == "POST"
