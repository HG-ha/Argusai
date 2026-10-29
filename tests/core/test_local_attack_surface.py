"""local_attack_surface_snapshot collection (mocked subprocess)."""

from __future__ import annotations

from unittest import mock

from argusai.core.security.local_surface import (
    _parse_posix_netstat_output,
    _parse_ss_output,
    collect_local_attack_surface,
)
from argusai.core.tools.local_surface_tools import local_attack_surface_snapshot


def test_collect_local_attack_surface_windows_mock() -> None:
    netstat = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4\n"
        "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       1234\n"
        "  TCP    [::]:3389               [::]:0                 LISTENING       88\n"
        "  UDP    [::]:5353               *:*                                    222\n"
    )

    def fake_run(cmd, timeout=20):  # noqa: ANN001
        joined = " ".join(cmd)
        if "netstat" in joined:
            return True, netstat
        if "Get-NetFirewallProfile" in joined or "powershell" in joined.lower():
            return True, '[{"Name":"Domain","Enabled":true}]'
        return False, ""

    with mock.patch("argusai.core.security.local_surface.platform.system", return_value="Windows"):
        with mock.patch("argusai.core.security.local_surface._run", side_effect=fake_run):
            result = collect_local_attack_surface(include_firewall=True)

    assert result["ok"] is True
    assert 445 in result["baseline"]["port_set"]
    assert 8080 in result["baseline"]["port_set"]
    assert 3389 in result["baseline"]["port_set"]
    assert 5353 in result["baseline"]["port_set"]
    assert result["baseline"]["fingerprint"]
    assert any(r["port"] == 445 for r in result["high_interest_listeners"])
    assert any(r["proto"] == "UDP" and r["address"] == "::" for r in result["listeners"])
    assert result["firewall"]["ok"] is True


def test_posix_ss_parser_handles_tcp_udp_ipv4_ipv6() -> None:
    output = (
        "Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "tcp   LISTEN 0      128          [::]:22           [::]:*\n"
        "udp   UNCONN 0      0         0.0.0.0:5353      0.0.0.0:*\n"
        "tcp   ESTAB  0      0       127.0.0.1:443      127.0.0.1:50000\n"
    )
    rows = _parse_ss_output(output)
    assert {(row["proto"], row["address"], row["port"]) for row in rows} == {
        ("TCP", "::", 22),
        ("UDP", "0.0.0.0", 5353),
    }


def test_posix_netstat_parser_uses_its_own_column_layout() -> None:
    output = (
        "Proto Recv-Q Send-Q Local Address Foreign Address State\n"
        "tcp6       0      0 :::8443      :::*        LISTEN\n"
        "udp        0      0 127.0.0.53:53 0.0.0.0:*\n"
        "tcp        0      0 127.0.0.1:80  127.0.0.1:50000 ESTABLISHED\n"
    )
    rows = _parse_posix_netstat_output(output)
    assert {(row["proto"], row["address"], row["port"]) for row in rows} == {
        ("TCP", "::", 8443),
        ("UDP", "127.0.0.53", 53),
    }


def test_firewall_command_error_is_not_reported_as_success() -> None:
    def fake_run(cmd, timeout=20):  # noqa: ANN001
        if "netstat" in " ".join(cmd):
            return True, ""
        return True, "Access is denied"

    with mock.patch("argusai.core.security.local_surface.platform.system", return_value="Windows"):
        with mock.patch("argusai.core.security.local_surface._run", side_effect=fake_run):
            result = collect_local_attack_surface(include_firewall=True)

    assert result["ok"] is True
    assert result["firewall"]["ok"] is False
    assert result["firewall"]["error"]


def test_tool_wrapper_passes_through() -> None:
    fake = {"ok": True, "baseline": {"port_set": [22], "fingerprint": "abc"}}
    with mock.patch(
        "argusai.core.security.local_surface.collect_local_attack_surface",
        return_value=fake,
    ):
        out = local_attack_surface_snapshot(api=None, include_firewall=False)
    assert out == fake
