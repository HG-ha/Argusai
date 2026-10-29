"""Registry and deterministic discovery for declared external CLIs."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from argusai.core.external_tools.models import (
    ExternalToolSpec,
    HostPlatform,
    InstallKind,
    Probe,
    ProbeSource,
    detect_host_platform,
)
from argusai.core.infra.win_stdio import quiet_subprocess_kwargs


@dataclass(frozen=True)
class ProbeResult:
    tool_id: str
    available: bool
    platform: HostPlatform
    path: str = ""
    source: ProbeSource | None = None
    version: str = ""
    version_error: str = ""
    error_code: str = ""
    error: str = ""
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "available": self.available,
            "platform": self.platform.as_dict(),
            "path": self.path,
            "source": self.source.value if self.source else "",
            "version": self.version,
            "version_error": self.version_error,
            "error_code": self.error_code,
            "error": self.error,
            "warnings": list(self.warnings),
        }


def _windows_commands(commands: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for command in commands:
        suffix = Path(command).suffix.lower()
        candidates = (
            (command,)
            if suffix in {".exe", ".cmd", ".bat", ".com"}
            else (f"{command}.exe", f"{command}.cmd", f"{command}.bat", command)
        )
        for candidate in candidates:
            if candidate not in expanded:
                expanded.append(candidate)
    return tuple(expanded)


def _probe_version(path: str, probe: Probe) -> tuple[str, str]:
    if not probe.version_args:
        return "", ""
    try:
        process = subprocess.run(
            [path, *probe.version_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=probe.timeout_seconds,
            check=False,
            text=False,
            env=os.environ.copy(),
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    output = ((process.stdout or b"") + (process.stderr or b"")).decode(
        "utf-8",
        errors="replace",
    ).strip()
    first_line = output.splitlines()[0][:300] if output else ""
    if process.returncode != 0:
        return first_line, f"version probe exited {process.returncode}: {output[:500]}"
    return first_line, ""


class ExternalToolRegistry:
    """Application-owned external tool declarations and host discovery.

    Resolution order is explicit override, trusted managed roots, then PATH.
    Invalid overrides are reported as warnings and do not hide a healthy
    managed or PATH installation.
    """

    def __init__(
        self,
        specs: Iterable[ExternalToolSpec] = (),
        *,
        overrides: Mapping[str, str | os.PathLike[str]] | None = None,
        managed_roots: Sequence[str | os.PathLike[str]] = (),
        host_platform: HostPlatform | None = None,
        path_env: str | None = None,
    ) -> None:
        self._specs: dict[str, ExternalToolSpec] = {}
        self._overrides: dict[str, str] = {}
        self._managed_roots = tuple(Path(root).expanduser().resolve() for root in managed_roots)
        self.host_platform = host_platform or detect_host_platform()
        self.path_env = path_env
        self.set_overrides(overrides)
        for spec in specs:
            self.register(spec)

    def register(self, spec: ExternalToolSpec, *, replace: bool = False) -> ExternalToolSpec:
        if not isinstance(spec, ExternalToolSpec):
            raise TypeError("registry entries must be ExternalToolSpec instances")
        if spec.tool_id in self._specs and not replace:
            raise ValueError(f"external tool is already registered: {spec.tool_id}")
        self._specs[spec.tool_id] = spec
        return spec

    def get(self, tool_id: str) -> ExternalToolSpec | None:
        return self._specs.get(str(tool_id or "").strip().lower())

    def set_overrides(
        self,
        overrides: Mapping[str, str | os.PathLike[str]] | None,
    ) -> None:
        cleaned: dict[str, str] = {}
        for raw_name, raw_path in (overrides or {}).items():
            name = str(raw_name or "").strip().lower()
            path = os.fspath(raw_path).strip() if raw_path is not None else ""
            if name and path:
                cleaned[name] = path
        self._overrides = cleaned

    def _commands(self, probe: Probe) -> tuple[str, ...]:
        if self.host_platform.operating_system.value == "windows":
            return _windows_commands(probe.commands)
        return probe.commands

    def _directory_candidate(self, directory: Path, probe: Probe) -> Path | None:
        for command in self._commands(probe):
            for parent in (directory, directory / "bin"):
                candidate = parent / command
                if candidate.is_file():
                    return candidate.resolve()
        return None

    def _override_candidate(self, tool_id: str, probe: Probe) -> tuple[Path | None, str]:
        raw = self._overrides.get(tool_id, "").strip()
        if not raw:
            return None, ""
        path = Path(raw).expanduser()
        if path.is_file():
            return path.resolve(), ""
        if path.is_dir():
            candidate = self._directory_candidate(path, probe)
            if candidate is not None:
                return candidate, ""
        return None, f"configured override not found: {raw}"

    def _managed_candidate(self, probe: Probe) -> Path | None:
        for root in self._managed_roots:
            for managed_path in probe.managed_paths:
                if not managed_path.supports(self.host_platform):
                    continue
                candidate = (root / Path(managed_path.relative_path)).resolve()
                if candidate != root and root not in candidate.parents:
                    continue
                if candidate.is_file():
                    return candidate
                if candidate.is_dir():
                    nested = self._directory_candidate(candidate, probe)
                    if nested is not None:
                        return nested
        return None

    def _path_candidate(self, probe: Probe) -> Path | None:
        for command in self._commands(probe):
            if self.path_env is None:
                found = shutil.which(command)
            else:
                found = shutil.which(command, path=self.path_env)
            if found:
                return Path(found).expanduser().resolve()
        return None

    def detect(self, tool_id: str) -> ProbeResult:
        key = str(tool_id or "").strip().lower()
        spec = self.get(key)
        if spec is None:
            return ProbeResult(
                tool_id=key,
                available=False,
                platform=self.host_platform,
                error_code="external_tool_unknown",
                error=f"unknown external tool: {key or '(empty)'}",
            )
        if not spec.platforms.supports(self.host_platform):
            return ProbeResult(
                tool_id=key,
                available=False,
                platform=self.host_platform,
                error_code="external_tool_unsupported_platform",
                error=f"{key} does not support {self.host_platform.key}",
            )

        warnings: list[str] = []
        candidate, override_warning = self._override_candidate(key, spec.probe)
        source: ProbeSource | None = None
        if candidate is not None:
            source = ProbeSource.OVERRIDE
        else:
            if override_warning:
                warnings.append(override_warning)
            candidate = self._managed_candidate(spec.probe)
            if candidate is not None:
                source = ProbeSource.MANAGED
            else:
                candidate = self._path_candidate(spec.probe)
                if candidate is not None:
                    source = ProbeSource.PATH

        if candidate is None:
            return ProbeResult(
                tool_id=key,
                available=False,
                platform=self.host_platform,
                error_code="external_tool_not_found",
                error=f"{key} was not found in overrides, managed roots, or PATH",
                warnings=tuple(warnings),
            )

        version, version_error = _probe_version(str(candidate), spec.probe)
        return ProbeResult(
            tool_id=key,
            available=True,
            platform=self.host_platform,
            path=str(candidate),
            source=source,
            version=version,
            version_error=version_error,
            warnings=tuple(warnings),
        )

    def inventory(self, *, names: Iterable[str] | None = None) -> dict[str, Any]:
        if names is None:
            selected = tuple(sorted(self._specs))
        else:
            wanted = {str(name or "").strip().lower() for name in names}
            selected = tuple(name for name in sorted(self._specs) if name in wanted)
        tools: dict[str, dict[str, Any]] = {}
        available: list[str] = []
        missing: list[str] = []
        unsupported: list[str] = []
        for name in selected:
            spec = self._specs[name]
            result = self.detect(name)
            tools[name] = {**spec.as_dict(), "probe_result": result.as_dict()}
            if result.available:
                available.append(name)
            elif result.error_code == "external_tool_unsupported_platform":
                unsupported.append(name)
            else:
                missing.append(name)
        return {
            "ok": True,
            "platform": self.host_platform.as_dict(),
            "available": available,
            "missing": missing,
            "unsupported": unsupported,
            "tools": tools,
        }

    def install_plan(self, tool_id: str) -> Any:
        spec = self.get(tool_id)
        if spec is None:
            raise KeyError(f"unknown external tool: {tool_id}")
        from argusai.core.external_tools.install import build_install_plan

        return build_install_plan(spec, host_platform=self.host_platform)

    def blocked_result(
        self,
        tool_id: str,
        *,
        probe_result: ProbeResult | None = None,
    ) -> dict[str, Any]:
        result = probe_result or self.detect(tool_id)
        if result.available:
            return {
                "ok": True,
                "blocked": False,
                "external_tool": result.as_dict(),
            }
        spec = self.get(tool_id)
        install = spec.install.as_dict() if spec else {}
        can_install = False
        plan: dict[str, Any] | None = None
        install_error = ""
        if spec and spec.install.kind is not InstallKind.MANUAL:
            try:
                built_plan = self.install_plan(spec.tool_id)
                plan = built_plan.as_dict()
                can_install = True
            except (RuntimeError, ValueError) as exc:
                install_error = str(exc)
        docs_url = str(install.get("docs_url") or "")
        if can_install:
            suggested_fix = (
                f"Create and execute the declared install plan for {spec.tool_id}, "
                "then probe it again."
            )
            next_step = "install_external_tool"
        else:
            suggested_fix = (
                f"Install {result.tool_id} from its official documentation"
                f"{f' ({docs_url})' if docs_url else ''}, then configure an override or PATH."
            )
            next_step = "configure_external_tool"
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": result.error_code or "external_tool_unavailable",
            "external_tool_required": result.tool_id,
            "error": result.error or f"{result.tool_id} is unavailable",
            "suggested_fix": suggested_fix,
            "next_step": next_step,
            "can_install": can_install,
            "install_error": install_error,
            "install": install,
            "install_plan": plan,
            "external_tool": result.as_dict(),
        }
