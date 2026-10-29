"""Safety checks for wireless, firmware, and hardware helpers."""

from __future__ import annotations

import pytest

from argusai.core.security.firmware_tools import resolve_firmware_path, rootfs_grep
from argusai.core.security.hardware_tools import (
    build_sigrok_offline_plan,
    hardware_action_policy,
    parse_uart_log,
)
from argusai.core.security.wireless_tools import (
    parse_nmcli_wifi,
    wireless_action_policy,
)


def test_wireless_policy_blocks_active_and_credential_actions() -> None:
    for action in ("deauth", "packet injection", "credential cracking", "evil twin"):
        verdict = wireless_action_policy(action)
        assert verdict["blocked"] is True
        assert verdict["allowed"] is False
    assert wireless_action_policy("inventory")["allowed"] is True


def test_nmcli_parser_handles_escaped_bssid_delimiters() -> None:
    parsed = parse_nmcli_wifi(
        "*:Lab:AA\\:BB\\:CC\\:DD\\:EE\\:FF:Infra:36:5180:866 Mbit/s:80:****:WPA2\n"
    )
    assert parsed["network_count"] == 1
    assert parsed["networks"][0]["bssid"] == "AA:BB:CC:DD:EE:FF"
    assert parsed["networks"][0]["channel"] == 36


def test_firmware_paths_cannot_escape_workspace(tmp_path) -> None:
    root = tmp_path / "firmware"
    root.mkdir()
    inside = root / "image.bin"
    inside.write_bytes(b"firmware")
    assert resolve_firmware_path(root, "image.bin") == inside.resolve()
    with pytest.raises(ValueError, match="escapes firmware workspace"):
        resolve_firmware_path(root, "../outside.bin", must_exist=False)


def test_rootfs_grep_is_bounded_and_does_not_follow_symlinks(tmp_path) -> None:
    root = tmp_path / "rootfs"
    config = root / "etc" / "app.conf"
    config.parent.mkdir(parents=True)
    config.write_text("debug=true\nlisten=0.0.0.0\n", encoding="utf-8")
    result = rootfs_grep(root, "listen", max_matches=1)
    assert result["ok"] is True
    assert result["match_count"] == 1
    assert result["matches"][0]["path"] == "etc/app.conf"


def test_hardware_policy_and_sigrok_plan_remain_read_only(tmp_path, monkeypatch) -> None:
    for action in ("flash", "program", "reset", "write memory"):
        assert hardware_action_policy("openocd", action)["blocked"] is True
    assert hardware_action_policy("openocd", "version")["allowed"] is True

    capture = tmp_path / "uart.sr"
    capture.write_bytes(b"capture")
    monkeypatch.setattr(
        "argusai.core.security.hardware_tools.shutil.which",
        lambda name: "/tools/sigrok-cli" if name == "sigrok-cli" else None,
    )
    plan = build_sigrok_offline_plan(capture, decoder="uart", channels="D0,D1")
    assert plan["ok"] is True
    assert "--input-file" in plan["command"]
    assert "--output-file" not in plan["command"]
    assert plan["execution"] == "not_performed"


def test_uart_parser_marks_signals_as_unverified_leads() -> None:
    parsed = parse_uart_log("[1.0] RX: U-Boot\n[2.0] RX: login:\n")
    assert parsed["stages"] == ["bootloader", "login"]
    assert parsed["security_signals"]
    assert "not proof" in parsed["confidence_note"]
