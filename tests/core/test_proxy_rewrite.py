"""Active rewrite: request/response edits and short-circuit through the proxy."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx

from argusai.core.events import EventBus
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_flow import ProxyConfig
from argusai.core.security.proxy_server import ProxyServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _start_origin(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _start_proxy(tmp_path: Path, rules):
    port = _free_port()
    bus = EventBus()
    config = ProxyConfig(listen_host="127.0.0.1", listen_port=port, ca_dir=tmp_path, allow_loopback=True)
    proxy = ProxyServer(config, PassiveAnalyzer(bus), bus)
    proxy._load_ca_material()
    proxy.set_rewrite_rules(rules)
    await proxy.start()
    return proxy, port


async def _echo_origin(reader, writer):
    """Echo back the request line, a chosen header, and the body as JSON-ish text."""
    request = await reader.readuntil(b"\r\n\r\n")
    head = request.decode("latin-1")
    length = 0
    injected = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip() or "0")
        if line.lower().startswith("x-injected:"):
            injected = line.split(":", 1)[1].strip()
    body = await reader.readexactly(length) if length else b""
    first = head.split("\r\n", 1)[0]
    payload = f"method={first.split()[0]} injected={injected} body={body.decode('latin-1')}".encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )
    await writer.drain()
    writer.close()


async def _run(tmp_path, rules, method, path, **kw):
    origin, origin_port = await _start_origin(_echo_origin)
    proxy, proxy_port = await _start_proxy(tmp_path, rules)
    try:
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", trust_env=False, timeout=10) as client:
            resp = await client.request(method, f"http://127.0.0.1:{origin_port}{path}", **kw)
        return resp, proxy
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_request_header_injection_reaches_upstream(tmp_path: Path) -> None:
    rules = [{
        "name": "inject", "match": "host:127.0.0.1", "apply_to": "request",
        "actions": [{"op": "set_header", "name": "X-Injected", "value": "yes"}],
    }]
    resp, proxy = asyncio.run(_run(tmp_path, rules, "GET", "/x"))
    assert resp.status_code == 200
    assert "injected=yes" in resp.text
    assert proxy.flows[-1].rewritten == "request"


def test_request_body_rewrite_updates_content_length(tmp_path: Path) -> None:
    rules = [{
        "name": "body", "match": "method:POST", "apply_to": "request",
        "actions": [{"op": "replace_body", "find": "secret", "replace": "REDACTED"}],
    }]
    resp, _ = asyncio.run(_run(tmp_path, rules, "POST", "/p", content=b"secret-value"))
    assert "body=REDACTED-value" in resp.text


def test_short_circuit_blocks_upstream(tmp_path: Path) -> None:
    rules = [{
        "name": "block", "match": "path:/blocked", "apply_to": "request",
        "actions": [{"op": "short_circuit", "status": 403, "headers": {"Content-Type": "text/plain"}, "body": "denied"}],
    }]
    resp, proxy = asyncio.run(_run(tmp_path, rules, "GET", "/blocked"))
    assert resp.status_code == 403
    assert resp.text == "denied"
    assert proxy.flows[-1].rewritten == "short_circuit"
    # The echo origin was never reached: no "method=" marker in the body.
    assert "method=" not in resp.text


def test_response_status_and_body_rewrite(tmp_path: Path) -> None:
    rules = [{
        "name": "resp", "match": "host:127.0.0.1", "apply_to": "response",
        "actions": [
            {"op": "set_status", "status": 418},
            {"op": "set_header", "name": "X-Tea", "value": "pot"},
            {"op": "replace_body", "find": "method=GET", "replace": "method=TEAPOT"},
        ],
    }]
    resp, proxy = asyncio.run(_run(tmp_path, rules, "GET", "/y"))
    assert resp.status_code == 418
    assert resp.headers.get("X-Tea") == "pot"
    assert "method=TEAPOT" in resp.text
    assert proxy.flows[-1].rewritten == "response"


def test_manager_test_rewrite_rule_dry_runs_without_sending(tmp_path: Path) -> None:
    import time

    from argusai.core.security.proxy_flow import TrafficFlow
    from argusai.core.security.proxy_manager import ProxyManager

    mgr = ProxyManager(ca_dir=tmp_path / "ca")
    server = ProxyServer(ProxyConfig(max_history=50), PassiveAnalyzer(mgr.bus), mgr.bus)
    mgr._server = server
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url="https://app.example.test/v2/profile",
        host="app.example.test",
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body=b'{"role":"user"}',
        raw_response_header_items=[("Content-Type", "application/json")],
    )
    server._append_flow(flow)

    rule = {
        "name": "role", "match": "host:app.example.test", "apply_to": "response",
        "actions": [{"op": "replace_body", "find": '"role":"user"', "replace": '"role":"admin"'}],
    }
    result = mgr.test_rewrite_rule(rule, flow.id)
    assert result["ok"] and result["matched"] is True
    assert '"role":"admin"' in result["response"]["body_preview"]
    # Bad rule: a response rule using a request-only action is rejected.
    bad = mgr.test_rewrite_rule(
        {"name": "x", "match": "host:a", "apply_to": "response", "actions": [{"op": "set_method", "method": "POST"}]},
        flow.id,
    )
    assert bad["ok"] is False


def test_settings_round_trip_keeps_rewrite_rules(tmp_path: Path) -> None:
    from argusai.core.settings import AppSettings, _parse

    settings = AppSettings(proxy_rewrite_rules=[{"id": "r1", "name": "n", "match": "host:a", "apply_to": "request",
                                                 "actions": [{"op": "set_header", "name": "X", "value": "1"}]}])
    restored = _parse(settings.to_dict())
    assert restored.proxy_rewrite_rules == settings.proxy_rewrite_rules


def test_disabled_and_nonmatching_rules_leave_traffic_untouched(tmp_path: Path) -> None:
    rules = [
        {"name": "off", "enabled": False, "match": "host:127.0.0.1", "apply_to": "request",
         "actions": [{"op": "set_header", "name": "X-Injected", "value": "no"}]},
        {"name": "other", "match": "host:other.example", "apply_to": "request",
         "actions": [{"op": "set_header", "name": "X-Injected", "value": "no"}]},
    ]
    resp, proxy = asyncio.run(_run(tmp_path, rules, "GET", "/z"))
    assert "injected=" in resp.text and "injected=no" not in resp.text
    assert proxy.flows[-1].rewritten == ""
