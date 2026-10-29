from __future__ import annotations

from pathlib import Path

from argusai.core.security.proxy import ProxyServer
from argusai.core.security.proxy_ca import (
    CA_COMMON_NAME,
    ca_info,
    fingerprint_sha256,
    install_ca_to_user_store,
    invalidate_trust_cache,
    migrate_legacy_ca_dir,
    uninstall_ca_from_user_store,
)
from argusai.core.security.proxy_media import classify_flow


def test_classify_flow_video_m3u8() -> None:
    meta = classify_flow(url="https://cdn.example.com/live/stream.m3u8", response_headers={"content-type": "application/vnd.apple.mpegurl"})
    assert meta["resource_kind"] == "video"


def test_classify_flow_api_json() -> None:
    meta = classify_flow(
        url="https://api.example.com/v1/user",
        response_headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )
    assert meta["resource_kind"] == "api"


def test_ca_fingerprint_stable(tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text(
        "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    fp1 = fingerprint_sha256(cert)
    fp2 = fingerprint_sha256(cert)
    assert fp1 == fp2
    assert ":" in fp1


def test_migrate_legacy_ca_dir(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "ca.pem").write_text("legacy-cert", encoding="utf-8")
    (legacy / "ca.key").write_text("legacy-key", encoding="utf-8")
    target = tmp_path / "proxy"
    migrate_legacy_ca_dir(target, legacy_dirs=[legacy])
    assert (target / "ca.pem").read_text(encoding="utf-8") == "legacy-cert"


def test_ca_info_reports_exists(tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    info = ca_info(tmp_path)
    assert info["ca_exists"] is True
    assert info["ca_cert_path"].endswith("ca.pem")


def test_ca_trust_probe_is_cached_across_status_polls(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class _Proc:
            returncode = 0
            stdout = CA_COMMON_NAME
            stderr = ""
        return _Proc()

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Windows")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    invalidate_trust_cache()

    assert ca_info(tmp_path)["ca_trusted"] is True
    for _ in range(5):
        assert ca_info(tmp_path)["ca_trusted"] is True
    assert len(calls) == 1

    uninstall_ca_from_user_store(cert)
    calls.clear()
    assert ca_info(tmp_path)["ca_trusted"] is True
    assert len(calls) == 1


def test_ca_trust_probe_suppresses_console_window(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Proc()

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Windows")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    monkeypatch.setattr(
        "argusai.core.security.proxy_ca.quiet_subprocess_kwargs",
        lambda: {"creationflags": 0x08000000},
    )
    invalidate_trust_cache()

    ca_info(tmp_path)
    assert seen and seen[0].get("creationflags") == 0x08000000


def test_uninstall_ca_windows_success(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class _Proc:
            returncode = 0
            stdout = "Certificate deleted"
            stderr = ""
        return _Proc()

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Windows")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    result = uninstall_ca_from_user_store(cert)
    assert result["ok"] is True
    assert any(CA_COMMON_NAME in " ".join(c) for c in calls)


def test_uninstall_ca_windows_not_found_is_ok(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "CertUtil: -delstore command FAILED: 0x80092004 (-2146885628 CRYPT_E_NOT_FOUND)"
        return _Proc()

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Windows")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    result = uninstall_ca_from_user_store(cert)
    assert result["ok"] is True


def test_uninstall_ca_unsupported_os(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Linux")
    result = uninstall_ca_from_user_store(cert)
    assert result["ok"] is False
    assert "not supported" in result["error"].lower()


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_macos_install_ca_uses_user_trust_not_admin_store(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    keychain = tmp_path / "login.keychain-db"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["security", "verify-cert"]:
            return _Proc(1)
        return _Proc(0)

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Darwin")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    monkeypatch.setattr("argusai.core.security.proxy_ca.macos_login_keychain", lambda: keychain)
    invalidate_trust_cache()
    result = install_ca_to_user_store(cert)
    assert result["ok"] is True
    add = next(c for c in calls if c[:2] == ["security", "add-trusted-cert"])
    assert "-d" not in add
    assert "-r" in add and "trustRoot" in add
    assert str(keychain) in add


def test_macos_install_ca_skips_when_already_trusted(monkeypatch, tmp_path: Path) -> None:
    cert = tmp_path / "ca.pem"
    cert.write_text("pem", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc(0)

    monkeypatch.setattr("argusai.core.security.proxy_ca.platform.system", lambda: "Darwin")
    monkeypatch.setattr("argusai.core.security.proxy_ca.subprocess.run", fake_run)
    invalidate_trust_cache()
    result = install_ca_to_user_store(cert)
    assert result["ok"] is True
    assert result.get("already_trusted") is True
    assert all(c[:2] != ["security", "add-trusted-cert"] for c in calls)


def test_build_origin_request_preserves_accept_encoding() -> None:
    payload = ProxyServer._build_origin_request(
        "GET",
        "/",
        {"accept-encoding": "gzip, br", "user-agent": "test"},
        b"",
        host="example.com",
        port=443,
    )
    text = payload.decode("latin-1")
    assert "accept-encoding: gzip, br" in text
    assert "Connection: close" in text


def test_relay_response_header_items_preserve_set_cookie() -> None:
    import httpx

    headers = httpx.Headers([("Content-Type", "text/html"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
    items = ProxyServer._relay_response_header_items(headers, 11)
    cookies = [v for k, v in items if k.lower() == "set-cookie"]
    assert cookies == ["a=1", "b=2"]
    assert ("Content-Length", "11") in items


def test_split_target_url() -> None:
    host, port, path, use_ssl = ProxyServer._split_target_url("https://api2.wer.plus/docs?q=1")
    assert host == "api2.wer.plus"
    assert port == 443
    assert path == "/docs?q=1"
    assert use_ssl is True
