"""Tests for safe offline TShark planning and parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from argusai.core.security.pcap_tools import (
    parse_tshark_http,
    parse_tshark_summary,
    pcap_filter_plan,
    pcap_http_extract_plan,
    pcap_summary,
    pcap_summary_plan,
    pcap_tool_inventory,
)


def _capture(tmp_path: Path, name: str = "sample.pcap") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xd4\xc3\xb2\xa1")
    return path


def test_inventory_reports_manual_only_when_tshark_missing(monkeypatch) -> None:
    monkeypatch.setattr("argusai.core.security.pcap_tools.shutil.which", lambda _name: None)
    inventory = pcap_tool_inventory()
    assert inventory["offline_only"] is True
    assert inventory["live_capture_supported"] is False
    assert inventory["tshark"]["installed"] is False
    assert inventory["tshark"]["can_install"] is False


def test_summary_plan_missing_cli_is_non_retryable(monkeypatch, tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": False},
    )
    plan = pcap_summary_plan(capture)
    assert plan["ok"] is False
    assert plan["blocked"] is True
    assert plan["do_not_retry"] is True
    assert plan["can_install"] is False


def test_summary_plan_builds_offline_argv(monkeypatch, tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": True},
    )
    plan = pcap_summary_plan(capture, max_packets=7000)
    assert plan["ok"] is True
    assert plan["args"][:3] == ["-n", "-r", str(capture.resolve())]
    assert "-i" not in plan["args"]
    assert "-c" in plan["args"]
    assert "7000" in plan["args"]
    assert plan["offline"] is True
    assert plan["live_capture"] is False


def test_plan_rejects_path_outside_allowed_root(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    capture = _capture(tmp_path, "outside.pcap")
    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": True},
    )
    plan = pcap_summary_plan(capture, allowed_root=root)
    assert plan["ok"] is False
    assert plan["blocked"] is True
    assert "escapes" in plan["error"]


def test_filter_plan_normalizes_tshark_boolean_operators(monkeypatch, tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": True},
    )
    plan = pcap_filter_plan(capture, "tcp.port == 443 && ip.addr == 192.0.2.1")
    assert plan["ok"] is True
    display_filter = plan["args"][plan["args"].index("-Y") + 1]
    assert " and " in display_filter
    assert "&&" not in display_filter


def test_http_plan_is_decode_only(monkeypatch, tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": True},
    )
    plan = pcap_http_extract_plan(capture)
    assert plan["ok"] is True
    display_filter = plan["args"][plan["args"].index("-Y") + 1]
    assert display_filter == "http.request or http.response"
    assert any("TLS/QUIC" in note for note in plan["notes"])


def test_parse_tshark_summary_aggregates_bounded_metadata() -> None:
    text = (
        "frame.number\tframe.time_epoch\tframe.len\t_ws.col.Protocol\tip.src\t"
        "ipv6.src\ttcp.srcport\tudp.srcport\tip.dst\tipv6.dst\ttcp.dstport\tudp.dstport\n"
        "1\t1700000000.0\t60\tTCP\t192.0.2.1\t\t12345\t\t198.51.100.2\t\t443\t\n"
        "2\t1700000001.5\t90\tDNS\t198.51.100.2\t\t\t53\t192.0.2.1\t\t\t53000\n"
    )
    parsed = parse_tshark_summary(text)
    assert parsed["packet_count"] == 2
    assert parsed["total_bytes"] == 150
    assert parsed["protocols"][0]["name"] in {"TCP", "DNS"}
    assert parsed["first_time_epoch"] == 1700000000.0
    assert parsed["last_time_epoch"] == 1700000001.5


def test_parse_tshark_http_extracts_requests_and_responses() -> None:
    text = (
        "frame.number\tframe.time_epoch\tip.src\tipv6.src\ttcp.srcport\tip.dst\t"
        "ipv6.dst\ttcp.dstport\thttp.request.method\thttp.host\thttp.request.uri\t"
        "http.request.full_uri\thttp.response.code\thttp.content_type\thttp.user_agent\n"
        "4\t1.0\t192.0.2.10\t\t50000\t198.51.100.3\t\t80\tGET\texample.test\t/a\t"
        "http://example.test/a\t\t\tAgent/1\n"
        "5\t1.1\t198.51.100.3\t\t80\t192.0.2.10\t\t50000\t\t\t\t\t200\ttext/plain\t\n"
    )
    parsed = parse_tshark_http(text)
    assert parsed["request_count"] == 1
    assert parsed["response_count"] == 1
    assert parsed["transactions"][0]["url"] == "http://example.test/a"
    assert parsed["statuses"] == {"200": 1}
    assert parsed["encrypted_payloads_included"] is False


def test_summary_run_uses_command_runner_without_shell(
    monkeypatch, tmp_path: Path
) -> None:
    capture = _capture(tmp_path)
    seen = {}

    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.tshark_status",
        lambda: {"ok": True, "installed": True},
    )

    async def fake_run(_self, request):
        seen["request"] = request
        return {
            "ok": True,
            "stdout_excerpt": "frame.number\tframe.time_epoch\n1\t1.0\n",
            "stderr_excerpt": "",
        }

    monkeypatch.setattr(
        "argusai.core.security.pcap_tools.CommandRunner.run",
        fake_run,
    )
    result = asyncio.run(pcap_summary(capture))
    assert result["ok"] is True
    assert result["parsed"]["packet_count"] == 1
    assert seen["request"].command == "tshark"
    assert seen["request"].target == "local.offline"
