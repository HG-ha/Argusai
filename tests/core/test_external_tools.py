from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.core.external_tools import (
    Architecture,
    EXTERNAL_TOOL_SPECS,
    ExternalToolRegistry,
    ExternalToolSpec,
    ExternalToolUnavailable,
    HostPlatform,
    InstallArtifact,
    InstallKind,
    InstallPlan,
    InstallPolicy,
    LicenseMetadata,
    ManagedPath,
    OperatingSystem,
    PlatformMetadata,
    Probe,
    ProbeSource,
    UnsafeArgvError,
    build_install_plan,
    build_safe_argv,
    download_install_artifact,
    install_managed_runtime,
)
from argusai.core.external_tools import install as install_module
from argusai.core.external_tools import registry as registry_module
from argusai.core.managed_runtimes import ArchiveSpec

WINDOWS_X64 = HostPlatform(OperatingSystem.WINDOWS, Architecture.X64)
LINUX_X64 = HostPlatform(OperatingSystem.LINUX, Architecture.X64)
PINNED_SHA256 = "a" * 64


def _license() -> LicenseMetadata:
    return LicenseMetadata(
        spdx_id="Apache-2.0",
        name="Apache License 2.0",
        url="https://spdx.org/licenses/Apache-2.0.html",
        redistributable=True,
    )


def _manual_spec(
    *,
    managed_paths: tuple[ManagedPath, ...] = (),
    version_args: tuple[str, ...] = (),
    platforms: PlatformMetadata | None = None,
) -> ExternalToolSpec:
    return ExternalToolSpec(
        tool_id="demo",
        display_name="Demo CLI",
        description="Fixture tool",
        probe=Probe(
            ("demo",),
            version_args=version_args,
            managed_paths=managed_paths,
        ),
        platforms=platforms or PlatformMetadata(),
        license=_license(),
        install=InstallPolicy.manual(docs_url="https://example.test/demo/docs"),
    )


def _archive_spec() -> ExternalToolSpec:
    artifact = InstallArtifact(
        platform_key="win-x64",
        filename="demo-1.2.3-win-x64.zip",
        sha256=PINNED_SHA256,
        sources=(
            "https://downloads.example.test/demo/1.2.3/demo-1.2.3-win-x64.zip",
        ),
    )
    return ExternalToolSpec(
        tool_id="demo",
        display_name="Demo CLI",
        probe=Probe(("demo",), version_args=()),
        platforms=PlatformMetadata(platform_keys=("win-x64",)),
        license=_license(),
        install=InstallPolicy.verified_archive("1.2.3", (artifact,)),
    )


def test_default_catalogue_covers_declared_cross_domain_clis() -> None:
    names = {spec.tool_id for spec in EXTERNAL_TOOL_SPECS}
    assert {
        "tshark",
        "trivy",
        "syft",
        "osv-scanner",
        "checkov",
        "prowler",
        "kubectl",
        "appium",
        "certipy",
        "binwalk",
        "aircrack-ng",
        "chipsec",
        "openocd",
        "sigrok-cli",
    } <= names
    assert all(spec.install.kind is InstallKind.MANUAL for spec in EXTERNAL_TOOL_SPECS)


def test_spec_declares_platform_license_probe_and_install_metadata() -> None:
    spec = _archive_spec()

    data = spec.as_dict()

    assert data["platforms"]["platform_keys"] == ["win-x64"]
    assert data["license"]["spdx_id"] == "Apache-2.0"
    assert data["probe"]["commands"] == ["demo"]
    assert data["install"]["kind"] == "verified_archive"
    assert data["install"]["version"] == "1.2.3"


def test_declarations_reject_unpinned_or_floating_install_sources() -> None:
    with pytest.raises(ValueError, match="64 hexadecimal"):
        InstallArtifact(
            "win-x64",
            "demo.zip",
            "not-a-checksum",
            ("https://downloads.example.test/demo/1.2.3/demo.zip",),
        )
    with pytest.raises(ValueError, match="floating 'latest'"):
        InstallArtifact(
            "win-x64",
            "demo.zip",
            PINNED_SHA256,
            ("https://downloads.example.test/demo/latest/demo.zip",),
        )
    with pytest.raises(ValueError, match="exact pinned version"):
        InstallPolicy.verified_archive(
            "latest",
            (
                InstallArtifact(
                    "win-x64",
                    "demo.zip",
                    PINNED_SHA256,
                    ("https://downloads.example.test/demo/1.2.3/demo.zip",),
                ),
            ),
        )
    with pytest.raises(ValueError, match="relative paths"):
        ManagedPath("../escape/demo.exe")


def test_registry_rejects_duplicate_registration() -> None:
    registry = ExternalToolRegistry([_manual_spec()], host_platform=WINDOWS_X64)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_manual_spec())


def test_registry_prefers_override_then_managed_then_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "override" / "demo.exe"
    managed = tmp_path / "managed" / "demo" / "bin" / "demo.exe"
    on_path = tmp_path / "path" / "demo.exe"
    for executable in (override, managed, on_path):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"fixture")
    spec = _manual_spec(
        managed_paths=(ManagedPath("demo/bin/demo.exe", ("win-x64",)),),
    )
    monkeypatch.setattr(
        registry_module.shutil,
        "which",
        lambda _command, **_kwargs: str(on_path),
    )

    with_override = ExternalToolRegistry(
        [spec],
        overrides={"demo": override},
        managed_roots=(tmp_path / "managed",),
        host_platform=WINDOWS_X64,
    ).detect("demo")
    with_managed = ExternalToolRegistry(
        [spec],
        managed_roots=(tmp_path / "managed",),
        host_platform=WINDOWS_X64,
    ).detect("demo")
    with_path = ExternalToolRegistry([spec], host_platform=WINDOWS_X64).detect("demo")

    assert with_override.source is ProbeSource.OVERRIDE
    assert with_override.path == str(override.resolve())
    assert with_managed.source is ProbeSource.MANAGED
    assert with_managed.path == str(managed.resolve())
    assert with_path.source is ProbeSource.PATH
    assert with_path.path == str(on_path.resolve())


def test_invalid_override_falls_back_and_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "demo.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(
        registry_module.shutil,
        "which",
        lambda _command, **_kwargs: str(executable),
    )
    registry = ExternalToolRegistry(
        [_manual_spec()],
        overrides={"demo": tmp_path / "missing.exe"},
        host_platform=WINDOWS_X64,
    )

    result = registry.detect("demo")

    assert result.available is True
    assert result.source is ProbeSource.PATH
    assert result.warnings and "configured override not found" in result.warnings[0]


def test_fixed_version_probe_uses_declared_argv_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "demo.exe"
    executable.write_bytes(b"fixture")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"demo 1.2.3\n", stderr=b"")

    monkeypatch.setattr(registry_module.subprocess, "run", fake_run)
    registry = ExternalToolRegistry(
        [_manual_spec(version_args=("version", "--short"))],
        overrides={"demo": executable},
        host_platform=WINDOWS_X64,
    )

    result = registry.detect("demo")

    assert result.version == "demo 1.2.3"
    assert calls == [[str(executable.resolve()), "version", "--short"]]


def test_inventory_and_structured_blocked_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module.shutil, "which", lambda *_args, **_kwargs: None)
    registry = ExternalToolRegistry([_archive_spec()], host_platform=WINDOWS_X64)

    inventory = registry.inventory()
    blocked = registry.blocked_result("demo")
    unknown = registry.blocked_result("missing")

    assert inventory["missing"] == ["demo"]
    assert inventory["tools"]["demo"]["probe_result"]["error_code"] == "external_tool_not_found"
    assert blocked["ok"] is False
    assert blocked["blocked"] is True
    assert blocked["do_not_retry"] is True
    assert blocked["can_install"] is True
    assert blocked["install_plan"]["version"] == "1.2.3"
    assert blocked["install_plan"]["sha256"] == PINNED_SHA256
    assert unknown["error_code"] == "external_tool_unknown"
    assert unknown["can_install"] is False


def test_unsupported_platform_is_distinct_from_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module.shutil, "which", lambda *_args, **_kwargs: None)
    linux_only = _manual_spec(
        platforms=PlatformMetadata(
            operating_systems=(OperatingSystem.LINUX,),
            architectures=(Architecture.X64,),
        )
    )
    registry = ExternalToolRegistry([linux_only], host_platform=WINDOWS_X64)

    inventory = registry.inventory()
    blocked = registry.blocked_result("demo")

    assert inventory["unsupported"] == ["demo"]
    assert inventory["missing"] == []
    assert blocked["error_code"] == "external_tool_unsupported_platform"


def test_install_plan_is_pinned_and_has_no_command_field() -> None:
    plan = build_install_plan(_archive_spec(), host_platform=WINDOWS_X64)

    assert plan.kind is InstallKind.VERIFIED_ARCHIVE
    assert plan.version == "1.2.3"
    assert plan.sha256 == PINNED_SHA256
    assert "command" not in InstallPlan.__dataclass_fields__
    parameters = inspect.signature(download_install_artifact).parameters
    assert "url" not in parameters
    assert "checksum" not in parameters
    assert "command" not in parameters
    assert "downloader" not in parameters


def test_download_install_artifact_reuses_verified_downloader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_install_plan(_archive_spec(), host_platform=WINDOWS_X64)
    calls: list[tuple[tuple[str, ...], Path, str]] = []

    def fake_download(
        urls: tuple[str, ...],
        destination: Path,
        expected_sha256: str,
    ) -> dict[str, object]:
        calls.append((urls, destination, expected_sha256))
        destination.write_bytes(b"offline fixture")
        return {"source": urls[0], "sha256": expected_sha256, "attempts": []}

    monkeypatch.setattr(install_module, "download_verified", fake_download)
    destination = tmp_path / plan.filename

    result = download_install_artifact(plan, destination)

    assert result["sha256"] == PINNED_SHA256
    assert calls == [(plan.sources, destination, PINNED_SHA256)]


def test_managed_runtime_plan_reuses_existing_manifest_and_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = ArchiveSpec(
        runtime="wabt",
        version="1.0.36",
        platform="win-x64",
        filename="wabt-1.0.36-windows.tar.gz",
        sha256="b" * 64,
        approx_download_bytes=123,
    )
    runtime_calls: list[tuple[str, Path | None, bool]] = []
    monkeypatch.setattr(install_module, "archive_spec", lambda *_args, **_kwargs: managed)
    monkeypatch.setattr(
        install_module,
        "archive_sources",
        lambda _spec: ["https://downloads.example.test/wabt/1.0.36/wabt.tar.gz"],
    )

    def fake_install(
        runtime: str,
        *,
        tools_dir: Path | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        runtime_calls.append((runtime, tools_dir, force))
        return {"ok": True, "runtime": runtime}

    monkeypatch.setattr(install_module, "install_runtime", fake_install)
    spec = ExternalToolSpec(
        tool_id="wasm2wat",
        display_name="WABT wasm2wat",
        probe=Probe(("wasm2wat",), version_args=()),
        platforms=PlatformMetadata(platform_keys=("win-x64",)),
        license=_license(),
        install=InstallPolicy.managed_runtime("wabt"),
    )

    plan = build_install_plan(spec, host_platform=WINDOWS_X64)
    result = install_managed_runtime(plan, tools_dir=tmp_path, force=True)

    assert plan.version == managed.version
    assert plan.sha256 == managed.sha256
    assert result["ok"] is True
    assert runtime_calls == [("wabt", tmp_path, True)]


def test_safe_argv_helper_never_executes_and_preserves_argument_boundaries(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "demo.exe"
    executable.write_bytes(b"fixture")
    registry = ExternalToolRegistry(
        [_manual_spec()],
        overrides={"demo": executable},
        host_platform=WINDOWS_X64,
    )

    argv = build_safe_argv(registry, "demo", ("--value", "a;b && c", ""))

    assert argv == (str(executable.resolve()), "--value", "a;b && c", "")


def test_safe_argv_rejects_controls_and_returns_blocked_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module.shutil, "which", lambda *_args, **_kwargs: None)
    registry = ExternalToolRegistry([_manual_spec()], host_platform=WINDOWS_X64)

    with pytest.raises(ExternalToolUnavailable) as exc_info:
        build_safe_argv(registry, "demo", ())
    assert exc_info.value.blocked_result["blocked"] is True
    assert exc_info.value.blocked_result["do_not_retry"] is True

    available = registry_module.ProbeResult(
        tool_id="demo",
        available=True,
        platform=LINUX_X64,
        path="/opt/demo",
    )
    with pytest.raises(UnsafeArgvError, match="control character"):
        from argusai.core.external_tools import argv_from_probe

        argv_from_probe(available, ("bad\nargument",))
