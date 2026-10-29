"""Read-only clients for public MCP catalogues.

Catalogue metadata is untrusted input.  This module deliberately exposes only
the small, JSON-clean subset needed by ArgusAI's marketplace UI and never
downloads or executes a listed package.  Imported launch configurations are
disabled and keep the existing high-risk/confirm-every-tool policy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


REGISTRY_BASE_URL = "https://registry.modelcontextprotocol.io"
MODELSCOPE_BASE_URL = "https://modelscope.cn/openapi/v1"
_MAX_RESPONSE_BYTES = 2_000_000
_ALLOWED_REMOTE_TRANSPORTS = {
    "streamable-http": "streamable_http",
    "streamable_http": "streamable_http",
    "sse": "sse",
}


def _text(value: Any, cap: int = 500) -> str:
    return str(value or "").strip()[:cap]


def _public_url(value: Any, *, https_only: bool = False) -> str:
    """Return a credential-free public URL, dropping query and fragment."""
    raw = _text(value, 4000)
    if not raw or "{" in raw or "}" in raw:
        return ""
    try:
        parsed = urlparse(raw)
    except (TypeError, ValueError):
        return ""
    allowed = {"https"} if https_only else {"http", "https"}
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in allowed or not hostname:
        return ""
    host = hostname.lower()
    if port:
        host = f"{host}:{port}"
    return urlunparse((parsed.scheme.lower(), host, parsed.path or "/", "", "", ""))


def _input_names(values: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return out
    for value in values[:50]:
        if not isinstance(value, dict):
            continue
        name = _text(value.get("name"), 150)
        if not name:
            continue
        out.append(
            {
                "name": name,
                "required": bool(value.get("isRequired", False)),
                "secret": bool(value.get("isSecret", False)),
                "description": _text(value.get("description"), 400),
            }
        )
    return out


def _string_list(value: Any, *, limit: int = 30, cap: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        text = _text(item, cap)
        if text and text not in out:
            out.append(text)
    return out


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _remote_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for raw in value[:20]:
        if not isinstance(raw, dict):
            continue
        transport = _ALLOWED_REMOTE_TRANSPORTS.get(
            _text(raw.get("type"), 50).lower()
        )
        url = _public_url(raw.get("url"))
        if not transport or not url:
            continue
        rows.append(
            {
                "transport": transport,
                "url": url,
                "secure": url.startswith("https://"),
                "headers": _input_names(raw.get("headers")),
                "variables": _input_names(raw.get("variables")),
            }
        )
    return rows


def _package_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for raw in value[:20]:
        if not isinstance(raw, dict):
            continue
        transport_obj = (
            raw.get("transport") if isinstance(raw.get("transport"), dict) else {}
        )
        registry_type = _text(raw.get("registryType"), 50).lower()
        identifier = _text(raw.get("identifier"), 500)
        version = _text(raw.get("version"), 100)
        if not registry_type or not identifier:
            continue
        rows.append(
            {
                "registry_type": registry_type,
                "identifier": identifier,
                "version": version,
                "runtime_hint": _text(raw.get("runtimeHint"), 100),
                "transport": _text(transport_obj.get("type"), 50).lower(),
                "environment": _input_names(raw.get("environmentVariables")),
                "arguments": _input_names(raw.get("packageArguments")),
                "checksum": _text(raw.get("fileSha256"), 128).lower(),
            }
        )
    return rows


def _safe_id(name: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name)
    return value.strip("-._")[:120] or "registry-mcp"


def _import_config(
    server: dict[str, Any],
    remotes: list[dict[str, Any]],
    packages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build a reviewable disabled config; never install or start anything."""
    title = _text(server.get("title") or server.get("name"), 200)
    name = _text(server.get("name"), 300)
    version = _text(server.get("version"), 100)
    repository = (
        server.get("repository")
        if isinstance(server.get("repository"), dict)
        else {}
    )
    provenance = {
        "source": "mcp_registry",
        "source_url": (
            f"{REGISTRY_BASE_URL}/v0.1/servers/"
            f"{quote(name, safe='')}/versions/{quote(version or 'latest', safe='')}"
        ),
        "repository": repository,
        "version": version,
    }
    notes = _text(server.get("description"), 1000)
    policy = {
        "default_risk": "high",
        "allow_auto_approve": False,
        "require_confirmation_for_all": True,
        "allow_insecure_http": False,
        "max_timeout_seconds": 60,
        "max_result_bytes": 200_000,
    }
    secure_remote = next((row for row in remotes if row["secure"]), None)
    if secure_remote:
        required = [
            row["name"]
            for row in secure_remote["headers"] + secure_remote["variables"]
            if row["required"]
        ]
        suffix = f" Required inputs: {', '.join(required)}." if required else ""
        return {
            "id": _safe_id(name),
            "name": title,
            "enabled": False,
            "transport": secure_remote["transport"],
            "url": secure_remote["url"],
            "headers": {},
            "timeout_seconds": 20,
            "notes": f"{notes}{suffix}".strip(),
            "policy": policy,
            "provenance": provenance,
        }

    # Exact-version npm entries can be reviewed as a disabled stdio config.
    # Saving it still performs no package download; enable/test remains a
    # separate high-risk user action.
    npm = next(
        (
            row
            for row in packages
            if row["registry_type"] == "npm"
            and row["transport"] == "stdio"
            and row["version"]
            and row["version"].lower() != "latest"
        ),
        None,
    )
    if npm:
        required_env = [
            row["name"] for row in npm["environment"] if row["required"]
        ]
        required_args = [
            row["name"] for row in npm["arguments"] if row["required"]
        ]
        required = required_env + required_args
        suffix = f" Required inputs: {', '.join(required)}." if required else ""
        return {
            "id": _safe_id(name),
            "name": title,
            "enabled": False,
            "transport": "stdio",
            "command": "npx",
            "args": [f"{npm['identifier']}@{npm['version']}"],
            "env": {},
            "timeout_seconds": 20,
            "notes": f"{notes}{suffix}".strip(),
            "policy": policy,
            "provenance": provenance,
        }
    return None


def normalize_registry_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    server = value.get("server") if isinstance(value.get("server"), dict) else value
    name = _text(server.get("name"), 300)
    version = _text(server.get("version"), 100)
    description = _text(server.get("description"), 1000)
    if not name or not version or not description:
        return None
    meta_root = value.get("_meta") if isinstance(value.get("_meta"), dict) else {}
    official = (
        meta_root.get("io.modelcontextprotocol.registry/official")
        if isinstance(
            meta_root.get("io.modelcontextprotocol.registry/official"), dict
        )
        else {}
    )
    remotes = _remote_rows(server.get("remotes"))
    packages = _package_rows(server.get("packages"))
    repository = (
        server.get("repository")
        if isinstance(server.get("repository"), dict)
        else {}
    )
    repo_url = _public_url(repository.get("url"))
    row = {
        "catalog_source": "official",
        "name": name,
        "title": _text(server.get("title") or name, 200),
        "description": description,
        "version": version,
        "status": _text(official.get("status"), 50) or "unknown",
        "updated_at": _text(official.get("updatedAt"), 100),
        "repository_url": repo_url,
        "website_url": _public_url(server.get("websiteUrl")),
        "remotes": remotes,
        "packages": packages,
        "categories": [],
        "tags": [],
        "view_count": 0,
        "is_hosted": bool(remotes),
        "is_verified": official.get("status") == "active",
    }
    row["config"] = _import_config(server, remotes, packages)
    return row


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "ArgusAI-MCP-Marketplace/1",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except Exception as primary_exc:
        # Some Windows Python/OpenSSL builds reject the Registry's otherwise
        # valid chain while Windows Schannel accepts it. Use the OS curl as a
        # strict-TLS fallback; never weaken verification with ``verify=False``.
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise primary_exc
        kwargs: dict[str, Any] = {}
        try:
            from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

            kwargs.update(quiet_subprocess_kwargs())
        except Exception:
            pass
        args = [
            curl,
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "12",
            "--max-filesize",
            str(_MAX_RESPONSE_BYTES),
            "--proto",
            "=https",
            "-H",
            "Accept: application/json",
            "-H",
            "User-Agent: ArgusAI-MCP-Marketplace/1",
        ]
        if method != "GET":
            args.extend(["-X", method])
        if body is not None:
            args.extend(
                [
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    body.decode("utf-8"),
                ]
            )
        args.append(url)
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            **kwargs,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or str(primary_exc)) from primary_exc
        raw = completed.stdout
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("registry response is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("registry returned a non-object response")
    return payload


def _default_request(url: str) -> dict[str, Any]:
    return _request_json(url)


def search_mcp_registry(
    query: str = "",
    cursor: str = "",
    limit: int = 20,
    *,
    requester: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search latest entries from the official read-only MCP Registry."""
    clean_query = _text(query, 120)
    clean_cursor = _text(cursor, 600)
    clean_limit = max(1, min(int(limit or 20), 50))
    params = {"limit": clean_limit, "version": "latest"}
    if clean_query:
        params["search"] = clean_query
    if clean_cursor:
        params["cursor"] = clean_cursor
    url = f"{REGISTRY_BASE_URL}/v0.1/servers?{urlencode(params)}"
    fetch = requester or _default_request
    payload = fetch(url)
    rows: list[dict[str, Any]] = []
    for raw in payload.get("servers") or []:
        row = normalize_registry_entry(raw)
        if row is not None and row["status"] != "deleted":
            rows.append(row)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "ok": True,
        "source": "official_mcp_registry",
        "preview": True,
        "servers": rows,
        "next_cursor": _text(metadata.get("nextCursor"), 600),
        "count": len(rows),
    }


_MODELSCOPE_RUNNERS = {
    "bunx",
    "deno",
    "docker",
    "node",
    "npx",
    "python",
    "python3",
    "uvx",
}
_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$",
    re.IGNORECASE,
)


def _modelscope_locale(server: dict[str, Any], language: str = "zh") -> dict[str, Any]:
    locales = server.get("locales") if isinstance(server.get("locales"), dict) else {}
    locale = locales.get(language) if isinstance(locales.get(language), dict) else {}
    return locale


def _modelscope_provenance(server: dict[str, Any]) -> dict[str, Any]:
    server_id = _text(server.get("id"), 300)
    website = (
        f"https://modelscope.cn/mcp/servers/{quote(server_id, safe='')}"
        if server_id
        else "https://modelscope.cn/mcp"
    )
    source_url = _public_url(server.get("source_url"))
    provenance: dict[str, Any] = {
        "source": "modelscope",
        "source_url": website,
    }
    if source_url:
        provenance["repository"] = {"url": source_url}
    return provenance


def _modelscope_import_config(server: dict[str, Any]) -> dict[str, Any] | None:
    """Build a disabled config from a detail response, without executing it."""
    server_id = _text(server.get("id"), 300)
    title = _text(
        _modelscope_locale(server).get("name")
        or server.get("chinese_name")
        or server.get("name")
        or server_id,
        200,
    )
    description = _text(
        _modelscope_locale(server).get("description") or server.get("description"),
        1000,
    )
    policy = {
        "default_risk": "high",
        "allow_auto_approve": False,
        "require_confirmation_for_all": True,
        "allow_insecure_http": False,
        "max_timeout_seconds": 60,
        "max_result_bytes": 200_000,
    }
    provenance = _modelscope_provenance(server)
    required_env = _string_list(
        (
            server.get("env_schema", {}).get("required")
            if isinstance(server.get("env_schema"), dict)
            else []
        ),
        limit=50,
        cap=150,
    )
    suffix = f" 必填环境变量：{', '.join(required_env)}。" if required_env else ""
    notes = f"{description}{suffix}".strip()

    operational_urls = (
        server.get("operational_urls")
        if isinstance(server.get("operational_urls"), list)
        else []
    )
    for remote in operational_urls[:20]:
        if not isinstance(remote, dict) or bool(remote.get("auth_required")):
            continue
        url = _public_url(remote.get("url"), https_only=True)
        transport = _ALLOWED_REMOTE_TRANSPORTS.get(
            _text(remote.get("transport_type"), 50).lower()
        )
        if url and transport:
            return {
                "id": _safe_id(server_id),
                "name": title,
                "enabled": False,
                "transport": transport,
                "url": url,
                "headers": {},
                "timeout_seconds": 20,
                "notes": notes,
                "policy": policy,
                "provenance": provenance,
            }

    configs = server.get("server_config")
    if not isinstance(configs, list):
        return None
    for wrapper in configs[:20]:
        roots = wrapper.get("mcpServers") if isinstance(wrapper, dict) else None
        if not isinstance(roots, dict):
            continue
        for raw in list(roots.values())[:20]:
            if not isinstance(raw, dict):
                continue
            command = _text(raw.get("command"), 100).lower()
            args = _string_list(raw.get("args"), limit=100, cap=1000)
            if (
                command not in _MODELSCOPE_RUNNERS
                or any(any(ch in arg for ch in ("\x00", "\n", "\r")) for arg in args)
            ):
                continue
            return {
                "id": _safe_id(server_id),
                "name": title,
                "enabled": False,
                "transport": "stdio",
                "command": command,
                "args": args,
                "env": {},
                "timeout_seconds": 20,
                "notes": notes,
                "policy": policy,
                "provenance": provenance,
            }
    return None


def _split_npm_package_spec(value: str) -> tuple[str, str]:
    text = value.strip()
    if text.startswith("@"):
        marker = text.rfind("@")
        return (text, "") if marker <= 0 else (text[:marker], text[marker + 1 :])
    if "@" not in text:
        return text, ""
    return tuple(text.rsplit("@", 1))  # type: ignore[return-value]


def prepare_marketplace_config(
    value: dict[str, Any],
    *,
    requester: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Make a catalogue config satisfy ArgusAI's immutable-launch policy.

    ModelScope publishes configurations for general-purpose clients and often
    uses ``npx -y package``. ArgusAI removes unattended-install flags and pins
    the package to the exact version currently returned by the npm Registry.
    No package is downloaded or executed here.
    """
    config = dict(value or {})
    provenance = (
        dict(config.get("provenance"))
        if isinstance(config.get("provenance"), dict)
        else {}
    )
    if _text(provenance.get("source"), 50).lower() != "modelscope":
        return config

    command = _text(config.get("command"), 100).lower()
    args = [
        _text(arg, 1000)
        for arg in (config.get("args") if isinstance(config.get("args"), list) else [])
        if _text(arg, 1000)
    ]
    if command in {"npx", "npx.cmd", "npx.exe"}:
        args = [arg for arg in args if arg.strip().lower() not in {"-y", "--yes"}]
        package_index = next(
            (index for index, arg in enumerate(args) if not arg.startswith("-")),
            None,
        )
        if package_index is None:
            raise ValueError("魔搭配置缺少 npm 包名，无法安全导入")
        package_name, version = _split_npm_package_spec(args[package_index])
        if not _NPM_PACKAGE_RE.fullmatch(package_name):
            raise ValueError("魔搭配置包含无效 npm 包名，无法安全导入")

        from argusai.core.security.mcp_provision import is_exact_version

        package_metadata: dict[str, Any] = {}
        if not is_exact_version(version):
            url = (
                "https://registry.npmjs.org/"
                f"{quote(package_name, safe='')}/latest"
            )
            package_metadata = (
                requester(url) if requester is not None else _request_json(url)
            )
            resolved_name = _text(package_metadata.get("name"), 300)
            version = _text(package_metadata.get("version"), 200)
            if resolved_name.lower() != package_name.lower() or not is_exact_version(version):
                raise ValueError("npm Registry 未返回可信的精确版本，无法安全导入")
        args[package_index] = f"{package_name}@{version}"
        provenance["version"] = version
        if package_metadata:
            provenance["package"] = {
                "name": package_name,
                "version": version,
                "dist": (
                    package_metadata.get("dist")
                    if isinstance(package_metadata.get("dist"), dict)
                    else {}
                ),
                "repository": package_metadata.get("repository"),
                "license": package_metadata.get("license"),
            }

    from argusai.core.security.mcp_provision import lint_mcp_launch

    launch_lint = lint_mcp_launch(command, args)
    if not launch_lint.get("ok"):
        codes = ", ".join(
            _text(error.get("code"), 100)
            for error in launch_lint.get("errors") or []
            if isinstance(error, dict)
        )
        raise ValueError(f"市场配置不符合安全启动规则：{codes or 'unsafe_config'}")
    config["command"] = command
    config["args"] = args
    config["provenance"] = provenance
    return config


def normalize_modelscope_entry(value: Any) -> dict[str, Any] | None:
    """Normalize a ModelScope summary or detail using Chinese-first metadata."""
    if not isinstance(value, dict):
        return None
    server_id = _text(value.get("id"), 300)
    locale_zh = _modelscope_locale(value)
    title = _text(
        locale_zh.get("name")
        or value.get("chinese_name")
        or value.get("name")
        or server_id,
        200,
    )
    description = _text(
        locale_zh.get("description") or value.get("description"),
        1000,
    )
    if not server_id or not title:
        return None
    source_url = _public_url(value.get("source_url"))
    website_url = f"https://modelscope.cn/mcp/servers/{quote(server_id, safe='')}"
    row = {
        "catalog_source": "modelscope",
        "name": server_id,
        "title": title,
        "english_title": _text(_modelscope_locale(value, "en").get("name"), 200),
        "description": description or "魔搭暂未提供服务简介。",
        "version": "",
        "status": "verified" if bool(value.get("is_verified")) else "listed",
        "updated_at": "",
        "repository_url": source_url,
        "website_url": website_url,
        "remotes": [],
        "packages": [],
        "categories": _string_list(value.get("categories"), limit=20),
        "tags": _string_list(value.get("tags"), limit=30),
        "view_count": _nonnegative_int(value.get("view_count")),
        "publisher": _text(value.get("publisher") or value.get("author"), 200),
        "is_hosted": bool(value.get("is_hosted")),
        "is_verified": bool(value.get("is_verified")),
        "config": _modelscope_import_config(value),
    }
    return row


_MODELSCOPE_BROWSE_CATEGORIES: tuple[str, ...] = (
    "developer-tools",
    "search",
    "browser-automation",
    "file-systems",
    "knowledge-and-memory",
    "research-and-data",
    "databases",
    "finance",
    "location-services",
    "communication",
)


def search_modelscope_marketplace(
    query: str = "",
    cursor: str = "",
    limit: int = 20,
    category: str = "",
    hosted: bool | None = None,
    *,
    requester: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Search ModelScope's Chinese MCP catalogue without fetching details."""
    clean_limit = max(1, min(int(limit or 20), 20))
    per_bucket_pages = max(1, 100 // clean_limit)
    try:
        requested_page = max(1, int(cursor or 1))
    except (TypeError, ValueError):
        requested_page = 1

    clean_query = _text(query, 120)
    clean_category = _text(category, 100)

    # ModelScope's public OpenAPI enforces page_number * page_size <= 100 per
    # query. When browsing the unfiltered catalogue (no search term and no
    # category filter), pages beyond the first 5 buckets are seamlessly
    # served from each category slice so users can paginate through 55 pages
    # (~1,100 extensions) instead of hitting a 5-page wall.
    unfiltered_browse = not clean_query and not clean_category
    max_browse_pages = per_bucket_pages * (1 + len(_MODELSCOPE_BROWSE_CATEGORIES)) if unfiltered_browse else per_bucket_pages
    page = min(requested_page, max_browse_pages)

    effective_category = clean_category
    api_page = page
    if unfiltered_browse and page > per_bucket_pages:
        offset_page = page - per_bucket_pages - 1
        cat_idx = min(offset_page // per_bucket_pages, len(_MODELSCOPE_BROWSE_CATEGORIES) - 1)
        effective_category = _MODELSCOPE_BROWSE_CATEGORIES[cat_idx]
        api_page = (offset_page % per_bucket_pages) + 1

    body: dict[str, Any] = {
        "page_number": api_page,
        "page_size": clean_limit,
    }
    if clean_query:
        body["search"] = clean_query
    filters: dict[str, Any] = {}
    if effective_category:
        filters["category"] = effective_category
    if isinstance(hosted, bool):
        filters["is_hosted"] = hosted
    if filters:
        body["filter"] = filters

    url = f"{MODELSCOPE_BASE_URL}/mcp/servers"
    payload = (
        requester(url, body)
        if requester is not None
        else _request_json(url, method="PUT", payload=body)
    )
    if payload.get("success") is False:
        raise RuntimeError(_text(payload.get("message"), 500) or "ModelScope request failed")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_rows = data.get("mcp_server_list")
    rows: list[dict[str, Any]] = []
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            row = normalize_modelscope_entry(raw)
            if row is not None:
                rows.append(row)
    total = _nonnegative_int(data.get("total_count"))
    if unfiltered_browse:
        max_pages = max_browse_pages if total > 100 else max(1, (total + clean_limit - 1) // clean_limit)
        has_more = len(rows) >= clean_limit and page < max_pages
    else:
        capped_total = min(total, 100)
        max_pages = max(1, (capped_total + clean_limit - 1) // clean_limit) if capped_total else 1
        has_more = len(rows) >= clean_limit and api_page * clean_limit < capped_total
    return {
        "ok": True,
        "source": "modelscope",
        "source_label": "魔搭 ModelScope",
        "preview": False,
        "servers": rows,
        "next_cursor": str(page + 1) if has_more else "",
        "count": len(rows),
        "total": total,
        "page": page,
        "max_pages": max_pages,
    }


def get_modelscope_marketplace_entry(
    server_id: str,
    *,
    requester: Callable[[str], dict[str, Any]] | None = None,
    npm_requester: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch one ModelScope detail only when the user asks to import it."""
    clean_id = _text(server_id, 300)
    if not clean_id:
        raise ValueError("server id required")
    url = f"{MODELSCOPE_BASE_URL}/mcp/servers/{quote(clean_id, safe='')}"
    payload = requester(url) if requester is not None else _request_json(url)
    if payload.get("success") is False:
        raise RuntimeError(_text(payload.get("message"), 500) or "ModelScope request failed")
    row = normalize_modelscope_entry(payload.get("data"))
    if row is None:
        raise ValueError("ModelScope returned an invalid MCP entry")
    if isinstance(row.get("config"), dict):
        try:
            row["config"] = prepare_marketplace_config(
                row["config"],
                requester=npm_requester,
            )
        except ValueError as exc:
            row["config"] = None
            row["import_error"] = str(exc)
    return {"ok": True, "source": "modelscope", "server": row}


def get_registry_marketplace_entry(
    server_id: str,
    *,
    requester: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve one exact official Registry entry through its search API."""
    clean_id = _text(server_id, 300)
    if not clean_id:
        raise ValueError("server id required")
    result = search_mcp_registry(
        clean_id,
        "",
        20,
        requester=requester,
    )
    row = next(
        (
            item
            for item in result.get("servers") or []
            if _text(item.get("name"), 300).lower() == clean_id.lower()
        ),
        None,
    )
    if row is None:
        raise ValueError("official MCP Registry entry not found")
    return {"ok": True, "source": "official", "server": row}


def search_mcp_marketplace(
    source: str = "modelscope",
    query: str = "",
    cursor: str = "",
    limit: int = 20,
    category: str = "",
    hosted: bool | None = None,
) -> dict[str, Any]:
    """Dispatch a read-only search to a supported MCP catalogue."""
    clean_source = _text(source, 50).lower()
    if clean_source == "modelscope":
        return search_modelscope_marketplace(
            query,
            cursor,
            limit,
            category,
            hosted,
        )
    if clean_source == "official":
        result = search_mcp_registry(query, cursor, limit)
        result["source"] = "official"
        result["source_label"] = "官方 MCP Registry"
        # The official Registry exposes page count, not catalogue total.
        result["total"] = 0
        return result
    raise ValueError("unsupported MCP marketplace source")


def get_mcp_marketplace_entry(source: str, server_id: str) -> dict[str, Any]:
    clean_source = _text(source, 50).lower()
    if clean_source == "modelscope":
        return get_modelscope_marketplace_entry(server_id)
    if clean_source == "official":
        return get_registry_marketplace_entry(server_id)
    raise ValueError("unsupported MCP marketplace source")


__all__ = [
    "MODELSCOPE_BASE_URL",
    "REGISTRY_BASE_URL",
    "get_mcp_marketplace_entry",
    "get_modelscope_marketplace_entry",
    "get_registry_marketplace_entry",
    "normalize_modelscope_entry",
    "normalize_registry_entry",
    "prepare_marketplace_config",
    "search_mcp_marketplace",
    "search_modelscope_marketplace",
    "search_mcp_registry",
]
