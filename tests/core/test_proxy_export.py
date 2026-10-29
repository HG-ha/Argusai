"""Tests for proxy capture filters and export helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from argusai.core.security.proxy_export import save_flow_body
from argusai.core.security.proxy_filters import (
    host_pattern_matches,
    mime_pattern_matches,
    should_capture_host,
    should_capture_mime,
)
from argusai.core.security.proxy_har import flows_to_har


@dataclass
class _Cfg:
    scope: list[str] = field(default_factory=list)
    deny_hosts: list[str] = field(default_factory=list)
    bypass_hosts: list[str] = field(default_factory=list)
    intercept_mode: str = "all"
    mime_allow: list[str] = field(default_factory=list)


@dataclass
class _Flow:
    id: int = 1
    timestamp: float = 1.0
    method: str = "GET"
    url: str = "https://cdn.example.com/a.m3u8"
    host: str = "cdn.example.com"
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: bytes = b""
    response_status: int = 200
    response_headers: dict[str, str] = field(default_factory=lambda: {"content-type": "application/vnd.apple.mpegurl"})
    response_body: bytes = b"#EXTM3U\n"
    error: str | None = None
    is_https: bool = True
    content_type: str = "application/vnd.apple.mpegurl"
    resource_kind: str = "video"
    filename_hint: str = "a.m3u8"
    raw_request_header_items: list[tuple[str, str]] = field(default_factory=list)
    raw_response_header_items: list[tuple[str, str]] = field(default_factory=list)
    response_encoding: str = ""
    duration_ms: float = 0.0


def test_host_pattern_wildcard_suffix() -> None:
    assert host_pattern_matches("api.example.com", "*.example.com")
    assert host_pattern_matches("example.com", "*.example.com")
    assert not host_pattern_matches("evil-example.com", "*.example.com")


def test_should_bypass_loopback_host() -> None:
    cfg = _Cfg()
    assert not should_capture_host(cfg, "127.0.0.1")
    assert not should_capture_host(cfg, "localhost")


def test_should_bypass_ai_endpoint_host() -> None:
    cfg = _Cfg(bypass_hosts=["api.openai.com"])
    assert not should_capture_host(cfg, "api.openai.com")
    assert should_capture_host(cfg, "cdn.example.com")
    cfg = _Cfg(intercept_mode="allowlist", scope=["api.example.com"])
    assert should_capture_host(cfg, "api.example.com")
    assert not should_capture_host(cfg, "cdn.example.com")


def test_should_capture_denylist_mode() -> None:
    cfg = _Cfg(intercept_mode="denylist", deny_hosts=["*.google.com"])
    assert should_capture_host(cfg, "api.example.com")
    assert not should_capture_host(cfg, "fonts.google.com")


def test_deny_hosts_honoured_in_allowlist_mode() -> None:
    cfg = _Cfg(
        intercept_mode="allowlist",
        scope=["*.example.com"],
        deny_hosts=["analytics.example.com"],
    )
    # In-scope host that is not denied → captured.
    assert should_capture_host(cfg, "api.example.com")
    # In-scope but explicitly denied → not captured (previously a silent loophole).
    assert not should_capture_host(cfg, "analytics.example.com")
    # Out-of-scope → not captured.
    assert not should_capture_host(cfg, "evil.test")


def test_deny_hosts_override_scope_in_all_mode() -> None:
    cfg = _Cfg(scope=["example.com"], deny_hosts=["api.example.com"])
    assert should_capture_host(cfg, "www.example.com")
    assert not should_capture_host(cfg, "api.example.com")


def test_should_capture_mime_allow() -> None:
    cfg = _Cfg(mime_allow=["video/*", "api"])
    assert should_capture_mime(cfg, "video/mp4", "video")
    assert should_capture_mime(cfg, "application/json", "api")
    assert not should_capture_mime(cfg, "text/html", "document")


def test_mime_pattern_resource_kind() -> None:
    assert mime_pattern_matches("text/plain", "api", "api")


def test_flows_to_har_has_entries() -> None:
    flow = _Flow()
    har = flows_to_har([flow])  # type: ignore[list-item]
    assert har["log"]["version"] == "1.2"
    assert len(har["log"]["entries"]) == 1
    assert har["log"]["entries"][0]["request"]["url"] == flow.url


def test_flows_to_har_includes_duration_timings() -> None:
    flow = _Flow()
    flow.duration_ms = 42.5  # type: ignore[attr-defined]
    har = flows_to_har([flow])  # type: ignore[list-item]
    entry = har["log"]["entries"][0]
    assert entry["time"] == 42.5
    assert entry["timings"]["wait"] == 42.5


def test_save_flow_body_writes_file(tmp_path: Path) -> None:
    flow = _Flow(response_body=b"hello")
    result = save_flow_body(flow, tmp_path, part="response")  # type: ignore[arg-type]
    assert result["ok"] is True
    saved = Path(result["path"])
    assert saved.is_file()
    assert saved.read_bytes() == b"hello"
