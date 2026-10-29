"""Pure planning helpers for admitting and revoking third-party MCP servers.

The functions in this module parse public metadata and build JSON-clean plans.
They never download packages, execute candidate commands, or mutate settings.
Callers must perform each side effect through an explicitly approved executor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen


_EXACT_SEMVER_RE = re.compile(
    r"^(?:v)?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_NPM_INTEGRITY_RE = re.compile(r"^(sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})$")
_SHELL_OPERATORS = ("|", "&&", "||", ";", "\n", "\r", "`", "$(")
_ARG_CONTROL_CHARS = ("\x00", "\n", "\r")
_SHELL_COMMANDS = {
    "bash",
    "cmd",
    "cmd.exe",
    "curl",
    "curl.exe",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "wget",
    "wget.exe",
    "wsl",
    "wsl.exe",
    "zsh",
}
_SECRET_OPTION_RE = re.compile(
    r"(?i)^--?(?:api[-_]?key|authorization|credential|password|secret|token)(?:=|$)"
)


def _text(value: Any, cap: int = 500) -> str:
    return str(value or "").strip()[:cap]


def _safe_public_url(value: Any) -> str:
    """Keep a public HTTP(S) URL while dropping credentials, query and fragment."""
    text = _text(value, 4000)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme.lower(), host, parsed.path, "", "", ""))


def is_exact_version(value: Any) -> bool:
    """Return whether ``value`` is a concrete SemVer, never a tag or range."""
    return bool(_EXACT_SEMVER_RE.fullmatch(_text(value, 200)))


def is_immutable_pin(value: Any) -> bool:
    """Accept exact SemVer releases or full Git commit identifiers."""
    text = _text(value, 200)
    return is_exact_version(text) or bool(_COMMIT_SHA_RE.fullmatch(text))


def normalize_checksums(value: Any) -> dict[str, str]:
    """Normalize supported digest fields without retaining arbitrary metadata."""
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("sha256:"):
            value = {"sha256": text.split(":", 1)[1]}
        elif _SHA256_RE.fullmatch(text):
            value = {"sha256": text}
        else:
            value = {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    sha256 = _text(value.get("sha256") or value.get("sha-256"), 128).lower()
    if _SHA256_RE.fullmatch(sha256):
        out["sha256"] = sha256
    return out


def normalize_npm_integrity(value: Any) -> str:
    """Validate and canonicalize an npm Subresource Integrity value."""
    text = _text(value, 1024)
    match = _NPM_INTEGRITY_RE.fullmatch(text)
    if not match:
        return ""
    try:
        base64.b64decode(match.group(2), validate=True)
    except Exception:
        return ""
    return f"{match.group(1).lower()}-{match.group(2)}"


def parse_github_repository(value: Any) -> dict[str, Any]:
    """Parse a GitHub repository URL, ``owner/name`` string, or API payload."""
    if isinstance(value, dict):
        full_name = _text(value.get("full_name"), 300)
        owner_obj = value.get("owner") if isinstance(value.get("owner"), dict) else {}
        owner = _text(owner_obj.get("login") or value.get("owner"), 150)
        name = _text(value.get("name"), 150)
        if full_name and "/" in full_name:
            owner, name = full_name.split("/", 1)
        if not owner or not name:
            parsed = parse_github_repository(value.get("html_url") or value.get("url") or "")
            owner = owner or parsed.get("owner", "")
            name = name or parsed.get("name", "")
        full_name = f"{owner}/{name}" if owner and name else ""
        return {
            "owner": owner,
            "name": name,
            "full_name": full_name,
            "html_url": _safe_public_url(value.get("html_url"))
            or (f"https://github.com/{full_name}" if full_name else ""),
            "api_url": _safe_public_url(value.get("url"))
            or (f"https://api.github.com/repos/{full_name}" if full_name else ""),
            "default_branch": _text(value.get("default_branch"), 200),
            "visibility": _text(value.get("visibility"), 50),
            "archived": bool(value.get("archived", False)),
            "disabled": bool(value.get("disabled", False)),
            "fork": bool(value.get("fork", False)),
            "pushed_at": _text(value.get("pushed_at"), 100),
            "stars": max(0, int(value.get("stargazers_count") or 0)),
            "topics": [_text(item, 100) for item in (value.get("topics") or []) if _text(item, 100)][:50],
        }

    text = _text(value, 2000).rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    owner = ""
    name = ""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        owner, name = text.split("/", 1)
    else:
        try:
            parsed = urlparse(text)
        except Exception:
            parsed = None
        if parsed and (parsed.hostname or "").lower() in {"github.com", "www.github.com", "api.github.com"}:
            parts = [part for part in parsed.path.split("/") if part]
            if parts and parts[0] == "repos":
                parts = parts[1:]
            if len(parts) >= 2:
                owner, name = parts[0], parts[1]
    if not owner or not name:
        return {}
    full_name = f"{owner}/{name}"
    return {
        "owner": owner,
        "name": name,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "api_url": f"https://api.github.com/repos/{full_name}",
        "default_branch": "",
        "visibility": "",
        "archived": False,
        "disabled": False,
        "fork": False,
        "pushed_at": "",
        "stars": 0,
        "topics": [],
    }


def parse_repository_metadata(value: Any) -> dict[str, Any]:
    """Compatibility name for repository metadata normalization."""
    return parse_github_repository(value)


def _digest_from_asset(value: Any) -> dict[str, str]:
    text = _text(value, 300)
    if ":" not in text:
        return {}
    algorithm, digest = text.split(":", 1)
    return normalize_checksums({algorithm: digest})


def parse_release_metadata(value: Any) -> dict[str, Any]:
    """Normalize one GitHub release without downloading any release asset."""
    if not isinstance(value, dict):
        return {}
    tag = _text(value.get("tag_name") or value.get("tag"), 200)
    version = tag[1:] if tag.startswith("v") and is_exact_version(tag) else tag
    assets: list[dict[str, Any]] = []
    for raw in value.get("assets") or []:
        if not isinstance(raw, dict):
            continue
        checksums = normalize_checksums(raw.get("checksums"))
        if not checksums:
            checksums = _digest_from_asset(raw.get("digest"))
        assets.append({
            "name": _text(raw.get("name"), 300),
            "url": _safe_public_url(raw.get("browser_download_url") or raw.get("url")),
            "size": max(0, int(raw.get("size") or 0)),
            "content_type": _text(raw.get("content_type"), 150),
            "checksums": checksums,
        })
    return {
        "id": _text(value.get("id"), 100),
        "tag": tag,
        "version": version,
        "name": _text(value.get("name"), 300),
        "url": _safe_public_url(value.get("html_url")),
        "draft": bool(value.get("draft", False)),
        "prerelease": bool(value.get("prerelease", False)),
        "published_at": _text(value.get("published_at"), 100),
        "target_commitish": _text(value.get("target_commitish"), 200),
        "assets": assets,
    }


def parse_license_metadata(value: Any) -> dict[str, Any]:
    """Normalize GitHub repository/license endpoint metadata, excluding content."""
    if not isinstance(value, dict):
        return {}
    license_obj = value.get("license") if isinstance(value.get("license"), dict) else value
    spdx_id = _text(license_obj.get("spdx_id"), 100)
    return {
        "spdx_id": spdx_id,
        "name": _text(license_obj.get("name"), 200),
        "key": _text(license_obj.get("key"), 100),
        "url": _safe_public_url(value.get("html_url") or license_obj.get("url")),
        "known": bool(spdx_id and spdx_id not in {"NOASSERTION", "OTHER"}),
    }


def parse_npm_package_metadata(value: Any) -> dict[str, Any]:
    """Normalize an npm package/version payload and its immutable integrity."""
    if not isinstance(value, dict):
        return {}
    dist = value.get("dist") if isinstance(value.get("dist"), dict) else {}
    repository = value.get("repository")
    if isinstance(repository, dict):
        repository_url = repository.get("url")
    else:
        repository_url = repository
    return {
        "name": _text(value.get("name"), 300),
        "version": _text(value.get("version"), 200),
        "integrity": normalize_npm_integrity(value.get("integrity") or dist.get("integrity")),
        "tarball_url": _safe_public_url(dist.get("tarball")),
        "repository_url": _safe_public_url(repository_url),
        "license": _text(value.get("license"), 100),
    }


def normalize_provenance(value: Any) -> dict[str, Any]:
    """Whitelist reproducibility metadata; credentials and arbitrary fields vanish."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except Exception:
            return {}
    if not isinstance(value, dict):
        return {}

    out: dict[str, Any] = {}
    source = _text(value.get("source"), 50).lower()
    if source in {"github", "npm", "manual", "local", "mcp_registry", "modelscope"}:
        out["source"] = source

    repository = parse_repository_metadata(value.get("repository"))
    if repository.get("full_name"):
        out["repository"] = repository
    release = parse_release_metadata(value.get("release"))
    if release:
        out["release"] = release
    license_data = parse_license_metadata(value.get("license"))
    if license_data:
        out["license"] = license_data
    package = parse_npm_package_metadata(value.get("package"))
    if package.get("name"):
        out["package"] = package

    commit_sha = _text(value.get("commit_sha") or value.get("commit"), 128).lower()
    if _COMMIT_SHA_RE.fullmatch(commit_sha):
        out["commit_sha"] = commit_sha
    version = _text(value.get("version"), 200)
    if version:
        out["version"] = version
    tag = _text(value.get("tag"), 200)
    if tag:
        out["tag"] = tag
    checksums = normalize_checksums(value.get("checksums") or value.get("checksum"))
    if checksums:
        out["checksums"] = checksums
    integrity = normalize_npm_integrity(value.get("npm_integrity") or value.get("integrity"))
    if integrity:
        out["npm_integrity"] = integrity
    source_url = _safe_public_url(value.get("source_url"))
    if source_url:
        out["source_url"] = source_url
    verified_at = _text(value.get("verified_at"), 100)
    if verified_at:
        out["verified_at"] = verified_at
    return out


def parse_candidate_metadata(
    repository: Any,
    *,
    release: Any = None,
    license_metadata: Any = None,
    package_metadata: Any = None,
) -> dict[str, Any]:
    """Combine GitHub/npm responses into one reviewable MCP candidate."""
    repo = parse_repository_metadata(repository)
    release_data = parse_release_metadata(release)
    license_data = parse_license_metadata(license_metadata)
    package = parse_npm_package_metadata(package_metadata)
    issues: list[dict[str, str]] = []
    if not repo.get("full_name"):
        issues.append({"code": "repository_invalid", "severity": "blocker"})
    if repo.get("archived") or repo.get("disabled"):
        issues.append({"code": "repository_inactive", "severity": "blocker"})
    if not license_data.get("known"):
        issues.append({"code": "license_review_required", "severity": "warning"})
    if release_data.get("draft"):
        issues.append({"code": "draft_release", "severity": "blocker"})
    if release_data.get("prerelease"):
        issues.append({"code": "prerelease_review_required", "severity": "warning"})

    provenance = normalize_provenance({
        "source": "github",
        "repository": repo,
        "release": release_data,
        "license": license_data,
        "package": package,
        "version": package.get("version") or release_data.get("version"),
        "npm_integrity": package.get("integrity"),
    })
    return {
        "kind": "mcp_server_candidate",
        "repository": repo,
        "release": release_data,
        "license": license_data,
        "package": package,
        "provenance": provenance,
        "issues": issues,
        "eligible": not any(item["severity"] == "blocker" for item in issues),
    }


def _default_github_request(url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    request = Request(url, headers=dict(headers))
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub API returned a non-object payload")
    return payload


def _coerce_response_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    json_method = getattr(value, "json", None)
    if callable(json_method):
        payload = json_method()
        if isinstance(payload, dict):
            return payload
    raise ValueError("GitHub metadata response must be a JSON object")


def fetch_github_candidate(
    repository: str,
    *,
    release_tag: str = "",
    token: str = "",
    requester: Callable[[str, Mapping[str, str]], Any] | None = None,
    package_metadata: Any = None,
) -> dict[str, Any]:
    """Fetch public GitHub metadata only; release assets are never downloaded."""
    repo = parse_github_repository(repository)
    if not repo.get("full_name"):
        return {"ok": False, "error_code": "invalid_repository", "error": "A GitHub owner/name or repository URL is required."}
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ArgusAI-MCP-Provision/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request_json = requester or _default_github_request
    base = f"https://api.github.com/repos/{repo['full_name']}"
    release_path = f"/releases/tags/{quote(release_tag, safe='')}" if release_tag else "/releases/latest"
    try:
        repository_payload = _coerce_response_payload(request_json(base, headers))
        release_payload = _coerce_response_payload(request_json(base + release_path, headers))
        license_payload = _coerce_response_payload(request_json(base + "/license", headers))
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "github_metadata_failed",
            "error": f"{type(exc).__name__}: GitHub metadata request failed",
        }
    return {
        "ok": True,
        "candidate": parse_candidate_metadata(
            repository_payload,
            release=release_payload,
            license_metadata=license_payload,
            package_metadata=package_metadata,
        ),
        "requests": {"count": 3, "assets_downloaded": 0},
    }


def _command_name(command: str) -> str:
    return Path(str(command or "").replace("\\", "/")).name.lower()


def _package_spec_parts(value: str) -> tuple[str, str]:
    text = value.strip()
    if text.startswith("@"):
        marker = text.rfind("@")
        if marker <= 0:
            return text, ""
        return text[:marker], text[marker + 1 :]
    if "@" not in text:
        return text, ""
    return tuple(text.rsplit("@", 1))  # type: ignore[return-value]


def lint_mcp_launch(command: Any, args: Any = None) -> dict[str, Any]:
    """Reject shell indirection and mutable npm launch specifications."""
    command_text = str(command or "").strip()
    argv = [str(item) for item in (args or [])] if isinstance(args, (list, tuple)) else []
    errors: list[dict[str, Any]] = []
    name = _command_name(command_text)

    if not command_text:
        errors.append({"code": "command_required", "message": "A stdio command is required."})
    if name in _SHELL_COMMANDS:
        errors.append({"code": "shell_command_forbidden", "message": "Shell and download-pipe launchers are not allowed."})
    if any(operator in command_text for operator in _SHELL_OPERATORS):
        errors.append({"code": "shell_syntax_forbidden", "message": "Shell control syntax is not allowed in the command."})

    for index, arg in enumerate(argv):
        lowered = arg.strip().lower()
        # Arguments are passed directly to create_subprocess_exec, not through
        # a shell. Punctuation such as ';' or '$(' is therefore literal and
        # valid in Python snippets, regexes and URLs; reject only controls that
        # can corrupt persisted launch configuration.
        if any(character in arg for character in _ARG_CONTROL_CHARS):
            errors.append({
                "code": "argument_control_character",
                "message": "MCP arguments cannot contain NUL or line breaks.",
                "index": index,
            })
        if lowered == "@latest" or lowered.endswith("@latest"):
            errors.append({
                "code": "mutable_version_forbidden",
                "message": "The @latest tag is not an immutable version pin.",
                "index": index,
            })

    if name in {"npx", "npx.cmd", "npx.exe", "pnpx", "pnpx.cmd", "pnpx.exe"}:
        for index, arg in enumerate(argv):
            if arg.strip().lower() in {"-y", "--yes"}:
                errors.append({
                    "code": "npx_auto_install_forbidden",
                    "message": "npx automatic installation is not allowed.",
                    "index": index,
                })
        package_specs: list[tuple[int, str]] = []
        skip_next = False
        for index, arg in enumerate(argv):
            if skip_next:
                package_specs.append((index, arg))
                skip_next = False
                continue
            lowered = arg.lower()
            if lowered in {"--package", "-p"}:
                skip_next = True
                continue
            if lowered.startswith("--package="):
                package_specs.append((index, arg.split("=", 1)[1]))
                continue
            if not arg.startswith("-"):
                package_specs.append((index, arg))
                break
        if not package_specs:
            errors.append({
                "code": "npx_package_required",
                "message": "npx requires an explicitly versioned package.",
            })
        for index, package_spec in package_specs:
            _package, version = _package_spec_parts(package_spec)
            if not is_exact_version(version):
                errors.append({
                    "code": "exact_version_required",
                    "message": "npx packages must use an exact SemVer pin.",
                    "index": index,
                })

    # Keep a stable order while avoiding duplicate code/index reports.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for error in errors:
        key = (error.get("code"), error.get("index"))
        if key not in seen:
            seen.add(key)
            deduped.append(error)
    return {"ok": not deduped, "errors": deduped, "command_type": name or "unknown"}


def redact_launch_args(args: Any) -> list[str]:
    """Return display-safe arguments while retaining non-secret diagnostics."""
    if not isinstance(args, (list, tuple)):
        return []
    out: list[str] = []
    redact_next = False
    for raw in args:
        text = str(raw)
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        if _SECRET_OPTION_RE.match(text):
            if "=" in text:
                out.append(text.split("=", 1)[0] + "=<redacted>")
            else:
                out.append(text)
                redact_next = True
            continue
        out.append(text)
    return out


def _selected_trust_anchor(candidate: dict[str, Any]) -> dict[str, Any]:
    package = candidate.get("package") if isinstance(candidate.get("package"), dict) else {}
    if package.get("name"):
        return {
            "kind": "npm_integrity",
            "value": normalize_npm_integrity(package.get("integrity")),
            "artifact": package.get("name"),
        }
    release = candidate.get("release") if isinstance(candidate.get("release"), dict) else {}
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        checksum = normalize_checksums(asset.get("checksums")).get("sha256", "")
        if checksum:
            return {"kind": "sha256", "value": checksum, "artifact": asset.get("name") or ""}
    provenance = normalize_provenance(candidate.get("provenance"))
    checksums = normalize_checksums(provenance.get("checksums"))
    if checksums.get("sha256"):
        return {"kind": "sha256", "value": checksums["sha256"], "artifact": ""}
    return {"kind": "", "value": "", "artifact": ""}


def build_provision_plan(
    candidate: Any,
    *,
    command: str = "",
    args: list[str] | None = None,
    transport: str = "stdio",
    staging_dir: str = "",
) -> dict[str, Any]:
    """Build a side-effect-free, integrity-gated candidate admission plan."""
    data = candidate if isinstance(candidate, dict) else {}
    if isinstance(data.get("candidate"), dict):
        data = data["candidate"]
    repository = parse_repository_metadata(data.get("repository"))
    release = parse_release_metadata(data.get("release"))
    package = parse_npm_package_metadata(data.get("package"))
    license_data = parse_license_metadata(data.get("license"))
    provenance = normalize_provenance(data.get("provenance") or {
        "source": "github",
        "repository": repository,
        "release": release,
        "package": package,
        "license": license_data,
    })
    launch_lint = lint_mcp_launch(command, args) if transport == "stdio" else {"ok": True, "errors": []}
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not repository.get("full_name"):
        blockers.append({"code": "repository_invalid", "message": "Repository metadata is required."})
    if repository.get("archived") or repository.get("disabled"):
        blockers.append({"code": "repository_inactive", "message": "Archived or disabled repositories cannot be provisioned."})
    if release.get("draft"):
        blockers.append({"code": "draft_release", "message": "Draft releases are not admissible."})
    if release.get("prerelease"):
        warnings.append({"code": "prerelease_review_required", "message": "The selected release is a prerelease."})
    if not license_data.get("known"):
        warnings.append({"code": "license_review_required", "message": "The repository license needs manual review."})
    for error in launch_lint.get("errors") or []:
        blockers.append({"code": str(error.get("code")), "message": str(error.get("message"))})

    version = _text(package.get("version") or release.get("version") or provenance.get("version"), 200)
    commit_sha = _text(provenance.get("commit_sha"), 128)
    if not is_exact_version(version) and not _COMMIT_SHA_RE.fullmatch(commit_sha):
        blockers.append({"code": "exact_version_required", "message": "Use an exact release version or full commit SHA."})

    trust_anchor = _selected_trust_anchor({
        "package": package,
        "release": release,
        "provenance": provenance,
    })
    if not trust_anchor.get("value"):
        code = "npm_integrity_required" if package.get("name") else "checksum_required"
        blockers.append({"code": code, "message": "An immutable package integrity or SHA-256 checksum is required."})

    public_launch = {
        "transport": str(transport or "stdio"),
        "command": _command_name(command) if command else "",
        "args": redact_launch_args(args or []),
        "args_sha256": hashlib.sha256(
            json.dumps([str(item) for item in (args or [])], ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    staging = {
        "required": True,
        "directory": str(staging_dir or ""),
        "isolated_home": True,
        "network_during_health": False,
        "health_checks": [
            "verify pinned version",
            "verify checksum or npm integrity",
            "MCP initialize handshake",
            "tools/list schema inspection",
            "terminate staging process",
        ],
        "promotion_condition": "all health checks pass",
    }
    stages = [
        {"id": "review", "side_effect": False, "requires_confirmation": False},
        {"id": "fetch_to_staging", "side_effect": True, "requires_confirmation": True},
        {"id": "verify_integrity", "side_effect": False, "requires_confirmation": False},
        {"id": "install_to_staging", "side_effect": True, "requires_confirmation": True},
        {"id": "staging_health", "side_effect": True, "requires_confirmation": True},
        {"id": "activate", "side_effect": True, "requires_confirmation": True},
    ]
    plan_material = {
        "repository": repository.get("full_name"),
        "version": version or commit_sha,
        "trust_anchor": trust_anchor,
        "launch": public_launch,
    }
    return {
        "ok": not blockers,
        "ready": not blockers,
        "kind": "mcp_provision_plan",
        "plan_id": hashlib.sha256(
            json.dumps(plan_material, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20],
        "candidate": {
            "repository": repository,
            "release": release,
            "license": license_data,
            "package": package,
        },
        "version_pin": version or commit_sha,
        "trust_anchor": trust_anchor,
        "provenance": provenance,
        "launch": public_launch,
        "launch_lint": launch_lint,
        "staging": staging,
        "stages": stages,
        "blockers": blockers,
        "warnings": warnings,
        "side_effects_performed": [],
        "downloads_performed": 0,
        "commands_executed": 0,
    }


def build_revoke_plan(server: Any, *, managed_root: str = "") -> dict[str, Any]:
    """Plan a local revoke without executing commands or deleting arbitrary paths."""
    data = server if isinstance(server, dict) else {}
    server_id = _text(data.get("id") or data.get("server_id"), 300)
    cleanup_path = _text(data.get("managed_staging_dir"), 4000)
    cleanup_allowed = False
    if cleanup_path and managed_root:
        try:
            root = Path(managed_root).expanduser().resolve()
            target = Path(cleanup_path).expanduser().resolve()
            cleanup_allowed = target != root and root in target.parents
        except Exception:
            cleanup_allowed = False
    steps = [
        {"id": "disable_config", "automatic": True},
        {"id": "stop_tracked_instance", "automatic": False, "requires_confirmation": True},
        {"id": "remove_config_and_secrets", "automatic": True},
        {"id": "clear_discovery_cache", "automatic": True},
    ]
    if cleanup_path:
        steps.append({
            "id": "remove_managed_staging",
            "automatic": False,
            "requires_confirmation": True,
            "allowed": cleanup_allowed,
        })
    return {
        "ok": bool(server_id),
        "kind": "mcp_revoke_plan",
        "server_id": server_id,
        "server_name": _text(data.get("name"), 300),
        "steps": steps,
        "cleanup_allowed": cleanup_allowed,
        "external_accounts_revoked": False,
        "side_effects_performed": [],
    }


def revoke_mcp_server(servers: Any, server_id: str) -> dict[str, Any]:
    """Purely remove one server config and return a secret-free revoke receipt."""
    sid = _text(server_id, 300)
    items = [item for item in (servers or []) if isinstance(item, dict)] if isinstance(servers, list) else []
    matched = next((item for item in items if _text(item.get("id"), 300) == sid), None)
    if not sid:
        return {"ok": False, "error_code": "server_id_required", "servers": items}
    if matched is None:
        return {"ok": False, "error_code": "server_not_found", "server_id": sid, "servers": items}
    remaining = [item for item in items if _text(item.get("id"), 300) != sid]
    return {
        "ok": True,
        "server_id": sid,
        "servers": remaining,
        "receipt": {
            "action": "revoked",
            "server_id": sid,
            "server_name": _text(matched.get("name"), 300),
            "removed_secret_fields": sorted(
                key for key in ("env", "headers") if isinstance(matched.get(key), dict) and matched.get(key)
            ),
            "external_accounts_revoked": False,
        },
    }


# Explicit aliases make the public vocabulary discoverable to callers/tests.
parse_github_repository_metadata = parse_repository_metadata
parse_github_release_metadata = parse_release_metadata
parse_github_license_metadata = parse_license_metadata
fetch_github_mcp_candidate = fetch_github_candidate
build_mcp_provision_plan = build_provision_plan
build_mcp_revoke_plan = build_revoke_plan
