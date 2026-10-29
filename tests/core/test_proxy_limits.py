"""Bounded decoding, history memory budget and transport-sized flow details."""

from __future__ import annotations

import gzip
import time
import zlib

import pytest

from argusai.core.events import EventBus
from argusai.core.security import proxy_server
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_constants import decode_body_prefix
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow
from argusai.core.security.proxy_server import ProxyServer


def test_gzip_bomb_decodes_only_up_to_limit() -> None:
    bomb = gzip.compress(b"\0" * (32 * 1024 * 1024))
    out, complete = decode_body_prefix("gzip", bomb, 4096)
    assert len(out) == 4096
    assert complete is False


def test_truncated_gzip_prefix_still_yields_text() -> None:
    body = b"line\n" * 20000
    raw = gzip.compress(body)
    out, complete = decode_body_prefix("gzip", raw[: len(raw) // 2], 10 * len(body))
    assert out and body.startswith(out)
    assert complete is True  # decoded everything it was given


def test_raw_deflate_falls_back_and_identity_is_capped() -> None:
    body = b'{"ok": true}' * 100
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw_deflate = compressor.compress(body) + compressor.flush()
    assert decode_body_prefix("deflate", raw_deflate, 1 << 20) == (body, True)
    assert decode_body_prefix("", body, 10) == (body[:10], False)


def test_zstd_is_bounded() -> None:
    zstandard = pytest.importorskip("zstandard")
    raw = zstandard.ZstdCompressor().compress(b"x" * (8 * 1024 * 1024))
    out, complete = decode_body_prefix("zstd", raw, 1000)
    assert out == b"x" * 1000
    assert complete is False


def _flow(server: ProxyServer, size: int) -> TrafficFlow:
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url=f"https://cdn.example.test/{server.last_flow_id}",
        host="cdn.example.test",
        response_status=200,
        response_headers={"content-type": "application/octet-stream"},
        response_body=b"z" * size,
    )
    server._append_flow(flow, analyze=False)
    return flow


def test_history_budget_releases_oldest_bodies(monkeypatch) -> None:
    monkeypatch.setattr(proxy_server, "_HISTORY_BODY_BUDGET", 2500)
    bus = EventBus()
    server = ProxyServer(ProxyConfig(max_history=50), PassiveAnalyzer(bus), bus)
    flows = [_flow(server, 1000) for _ in range(5)]
    assert server._body_bytes <= 2500
    assert [f.body_evicted for f in flows] == [True, True, True, False, False]
    assert flows[0].to_dict()["body_evicted"] is True
    assert flows[-1].response_body == b"z" * 1000

    server.clear_flows()
    assert server._body_bytes == 0


def test_body_classification_distrusts_control_bytes_but_keeps_gbk() -> None:
    classify = TrafficFlow._classify_body
    assert classify(b"\0" * 4096, "text/plain") == "binary"
    assert classify("你好，世界".encode("gbk") * 200, "text/html") == "html"
    # A UTF-8 character split by the 1 KiB probe is still text.
    assert classify(("a" * 1023 + "你好" * 400).encode("utf-8"), "") == "text"


def test_detail_payload_respects_text_limit_and_media_flag() -> None:
    flow = TrafficFlow(
        id=1,
        timestamp=time.time(),
        method="GET",
        url="https://api.example.test/big.json",
        host="api.example.test",
        response_status=200,
        content_type="application/json",
        response_headers={"content-type": "application/json"},
        response_body=b'{"k": "' + b"v" * 300_000 + b'"}',
        response_full_size=300_009,
    )
    detail = flow.to_dict(include_body_preview=True, text_limit=1000)
    body = detail["response_body"]
    assert body["text_limited"] is True
    assert len(body["text"]) == 1000
    assert "pretty" not in body
    assert len(detail["response_body_preview"]) < 9000

    image = TrafficFlow(
        id=2,
        timestamp=time.time(),
        method="GET",
        url="https://api.example.test/a.png",
        host="api.example.test",
        content_type="image/png",
        response_body=b"\x89PNG" + b"\0" * 100,
    )
    assert "base64" not in image.to_dict(include_body_preview=True, inline_media=False)["response_body"]
    assert "base64" in image.to_dict(include_body_preview=True)["response_body"]
