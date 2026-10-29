"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *

def _data_root_from_api(api: Any) -> str:
    try:
        return str(api.settings_mgr.get().data_root)
    except Exception:
        from argusai.core.settings import default_data_root

        return default_data_root()


def _mcp_runtime_context(api: Any, kwargs: dict[str, Any] | None = None) -> dict[str, str]:
    from argusai.core.paths import PathManager

    tools_dir = PathManager(data_root=_data_root_from_api(api)).tools_dir
    runtime_cwd = ""
    if kwargs and kwargs.get("_session_id"):
        try:
            runtime_cwd = str(_session_sandbox_root(api, kwargs))
        except Exception:
            runtime_cwd = ""
    return {"runtime_tools_dir": str(tools_dir), "runtime_cwd": runtime_cwd}


def _mcp_server_spec(api: Any, server: dict[str, Any], kwargs: dict[str, Any] | None = None):
    from argusai.core.infra.mcp_client import spec_from_server_dict

    return spec_from_server_dict(server, **_mcp_runtime_context(api, kwargs))


def _resolve_case_store(api: Any):
    from pathlib import Path
    from argusai.core.workspace.cases import CaseStore

    return CaseStore(Path(_data_root_from_api(api)) / "cases")


def _resolve_session_vault(api: Any):
    from pathlib import Path
    from argusai.core.workspace.session_vault import SessionVault

    return SessionVault(Path(_data_root_from_api(api)) / "session_vault")


def case_create(
    api: Any,
    title: str = "",
    target_type: str = "url",
    target_value: str = "",
    target_name: str = "",
    scope: list[str] | str | None = None,
    domain: str = "",
    tags: list[str] | None = None,
    note: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a cross-domain analysis case for Web, network, mobile or binary work."""
    from argusai.core.workspace.cases import CaseTarget
    from argusai.core.infra.command import normalize_scope

    target = CaseTarget(
        type=str(target_type or "other"),
        value=str(target_value or ""),
        name=str(target_name or ""),
        scope=normalize_scope(scope),
    )
    return {"ok": True, "case": _resolve_case_store(api).create_case(title=title, target=target, domain=domain, tags=tags or [], note=note)}


def case_list(api: Any, limit: int = 50, **kwargs: Any) -> dict[str, Any]:
    """List recent analysis cases."""
    items = _resolve_case_store(api).list_cases(limit=_safe_int(limit, 50, 1, 200))
    return {"ok": True, "count": len(items), "cases": items}


def case_get(api: Any, case_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Read one analysis case summary with artifact/evidence references."""
    try:
        return {"ok": True, "case": _resolve_case_store(api).get_case(str(case_id or ""))}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def case_events(api: Any, case_id: str = "", tail: int = 200, **kwargs: Any) -> dict[str, Any]:
    """Read the timeline for one analysis case."""
    try:
        events = _resolve_case_store(api).events(str(case_id or ""), tail=_safe_int(tail, 200, 1, 5000))
        return {"ok": True, "count": len(events), "events": events}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def case_link_add(
    api: Any,
    case_id: str = "",
    source_ref: str = "",
    relation: str = "",
    target_ref: str = "",
    note: str = "",
    metadata_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Link two case resources for attack-chain or evidence-graph reasoning."""
    import json

    try:
        metadata = json.loads(metadata_json or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except Exception:
        metadata = {}
    try:
        link = _resolve_case_store(api).add_link(
            str(case_id or ""),
            source_ref=source_ref,
            relation=relation,
            target_ref=target_ref,
            note=note,
            metadata=metadata,
        )
        return {"ok": True, "case_id": case_id, "link": link, "link_ref": link["id"]}
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def case_graph(api: Any, case_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a compact graph of artifacts, evidence, findings and explicit case links."""
    try:
        graph = _resolve_case_store(api).graph(str(case_id or ""))
        return {
            "ok": True,
            **graph,
            "node_count": len(graph.get("nodes") or []),
            "edge_count": len(graph.get("edges") or []),
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def artifact_add(
    api: Any,
    case_id: str = "",
    kind: str = "other",
    title: str = "",
    content: str = "",
    filename: str = "",
    domain: str = "",
    media_type: str = "",
    metadata_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach text content as a stable artifact under a case."""
    import json

    try:
        metadata = json.loads(metadata_json or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except Exception:
        metadata = {}
    try:
        artifact = _resolve_case_store(api).add_artifact(
            str(case_id or ""),
            kind=kind,
            title=title,
            content=str(content or ""),
            filename=filename or "",
            domain=domain,
            media_type=media_type,
            metadata=metadata,
        )
        return {"ok": True, "case_id": case_id, "artifact": artifact, "artifact_ref": artifact["id"]}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def artifact_read(api: Any, case_id: str = "", artifact_id: str = "", max_chars: int = 32768, offset: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Read a case artifact by stable reference."""
    try:
        data = _resolve_case_store(api).read_artifact(
            str(case_id or ""),
            str(artifact_id or ""),
            max_chars=_safe_int(max_chars, 32768, 256, 200000),
            offset=_safe_int(offset, 0, 0, 100000000),
        )
        return {"ok": True, "case_id": case_id, **data}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def evidence_add(
    api: Any,
    case_id: str = "",
    kind: str = "other",
    title: str = "",
    confidence: str = "suspected",
    artifact_refs: list[str] | str | None = None,
    interaction_refs: list[str] | str | None = None,
    finding_uid: str = "",
    summary: str = "",
    metadata_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach structured evidence to a case timeline."""
    import json

    def _list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(x) for x in value if str(x)]
        return [x.strip() for x in str(value or "").split(",") if x.strip()]

    try:
        metadata = json.loads(metadata_json or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except Exception:
        metadata = {}
    try:
        evidence = _resolve_case_store(api).add_evidence(
            str(case_id or ""),
            kind=kind,
            title=title,
            confidence=confidence,
            artifact_refs=_list(artifact_refs),
            interaction_refs=_list(interaction_refs),
            finding_uid=finding_uid,
            summary=summary,
            metadata=metadata,
        )
        return {"ok": True, "case_id": case_id, "evidence": evidence, "evidence_ref": evidence["id"]}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def domain_inventory(api: Any, domain: str = "", **kwargs: Any) -> dict[str, Any]:
    """List current and planned platform domains and their artifact/tool boundaries."""
    from argusai.core.workspace.domain_registry import domain_inventory as _inventory

    return _inventory(domain=domain)


def asset_ref_normalize(api: Any, asset_ref: str = "", default_type: str = "other", **kwargs: Any) -> dict[str, Any]:
    """Normalize an asset reference for todo target_ref, case links and evidence graph."""
    from argusai.core.workspace.assets import normalize_asset_ref

    asset = normalize_asset_ref(asset_ref, default_type=default_type)
    return {"ok": True, "asset": asset.to_dict(), "asset_ref": asset.to_ref()}


def tool_adapter_inventory(api: Any, domain: str = "", asset_type: str = "", **kwargs: Any) -> dict[str, Any]:
    """List planned/existing external tool adapters by domain or asset type."""
    from argusai.core.workspace.tool_adapters import adapter_inventory

    return adapter_inventory(domain=domain, asset_type=asset_type)


def external_tool_inventory(api: Any, names: str = "", **kwargs: Any) -> dict[str, Any]:
    """Discover declared external CLIs through overrides, managed roots and PATH."""
    from argusai.core.external_tools import create_default_registry

    selected = [item.strip() for item in str(names or "").split(",") if item.strip()]
    return create_default_registry().inventory(names=selected or None)


def external_tool_install_plan(api: Any, tool_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Return a pinned install plan, or a stable manual-install blocker."""
    from argusai.core.external_tools import create_default_registry

    registry = create_default_registry()
    spec = registry.get(tool_id)
    if spec is None:
        return registry.blocked_result(tool_id)
    probe = registry.detect(tool_id)
    if probe.available:
        return {
            "ok": True,
            "already_available": True,
            "external_tool": probe.as_dict(),
            "install_plan": None,
        }
    return registry.blocked_result(tool_id, probe_result=probe)


def _mcp_public_server(server: dict[str, Any]) -> dict[str, Any]:
    from argusai.core.infra.mcp_client import public_server_view

    return public_server_view(server)


def _find_mcp_server(api: Any, server_id: str) -> dict[str, Any] | None:
    from argusai.core.settings import normalize_mcp_servers

    sid = str(server_id or "").strip()
    if not sid:
        return None
    for item in normalize_mcp_servers(api.settings_mgr.get().mcp_servers):
        if item.get("id") == sid:
            return item
    return None


def mcp_discovery_cache(api: Any) -> dict[str, dict[str, Any]]:
    """Lazy in-memory cache of MCP `initialize` / `tools/list` results.

    Keyed by ``server_id``. Populated opportunistically by
    ``mcp_server_health`` (writes ``server_info`` / ``protocol_version``)
    and ``mcp_server_list_tools`` (writes ``tools`` — a short list of
    ``{name, description}``). Read by ``_build_mcp_servers_prompt`` so
    the system prompt can show the AUTHORITATIVE server identity from
    the protocol handshake instead of treating the user-supplied
    nickname as the source of truth.

    The cache lives on ``api`` for the lifetime of the process. When a
    user edits an MCP server entry, the next probe simply overwrites
    the cache entry — slight staleness between edit and re-probe is
    cosmetic.
    """
    cache = getattr(api, "_mcp_discovery_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(api, "_mcp_discovery_cache", cache)
        except Exception:
            pass
    return cache


def _update_mcp_cache(api: Any, server_id: str, patch: dict[str, Any]) -> None:
    sid = str(server_id or "").strip()
    if not sid or not isinstance(patch, dict) or not patch:
        return
    cache = mcp_discovery_cache(api)
    entry = cache.get(sid)
    if not isinstance(entry, dict):
        entry = {}
    entry.update(patch)
    entry["ts"] = time.time()
    cache[sid] = entry


def _mark_mcp_last_probe(
    api: Any,
    server_id: str,
    *,
    status: str,
    elapsed_ms: int | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    """Write a compact ``last_probe`` envelope into the discovery cache.

    Used by all probe entry points (manual UI test, AI ``mcp_server_health``
    tool call, and the background startup probe) so the UI badge and the
    system prompt always reflect the most recent verdict.
    """
    payload: dict[str, Any] = {"status": status, "ts": time.time()}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = int(elapsed_ms)
    if error_code:
        payload["error_code"] = error_code
    if error:
        payload["error"] = str(error)[:400]
    _update_mcp_cache(api, server_id, {"last_probe": payload})


async def _probe_one_mcp_server(api: Any, server: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """Best-effort probe for a single server. Writes results into cache.

    Returns a small status dict, but the side effect (cache update) is
    the real product — the caller does not need to inspect the return
    value. ``timeout`` bounds the full handshake so a hung remote does
    not stall the rest of the startup batch.
    """
    import asyncio
    from argusai.core.infra.mcp_client import classify_exception, make_mcp_client

    sid = str(server.get("id") or "").strip()
    if not sid:
        return {"ok": False, "error": "missing id"}
    _mark_mcp_last_probe(api, sid, status="pending")
    spec = _mcp_server_spec(api, server)
    started = time.perf_counter()

    async def _do() -> dict[str, Any]:
        async with make_mcp_client(spec) as client:
            return client.health()

    try:
        health = await asyncio.wait_for(_do(), timeout=timeout)
        info = health.get("server_info") if isinstance(health.get("server_info"), dict) else {}
        _update_mcp_cache(api, sid, {
            "server_info": info,
            "protocol_version": health.get("protocol_version") or "",
            "capabilities": health.get("capabilities") if isinstance(health.get("capabilities"), dict) else {},
            "transport": spec.transport,
        })
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _mark_mcp_last_probe(api, sid, status="alive", elapsed_ms=elapsed_ms)
        return {"ok": True, "server_id": sid, "status": "alive", "elapsed_ms": elapsed_ms}
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _mark_mcp_last_probe(
            api,
            sid,
            status="dead",
            elapsed_ms=elapsed_ms,
            error_code="timeout",
            error=f"probe exceeded {timeout:.1f}s",
        )
        return {"ok": False, "server_id": sid, "status": "dead", "error_code": "timeout"}
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        code, _retryable, _fix = classify_exception(exc)
        _mark_mcp_last_probe(
            api,
            sid,
            status="dead",
            elapsed_ms=elapsed_ms,
            error_code=code,
            error=str(exc),
        )
        return {"ok": False, "server_id": sid, "status": "dead", "error_code": code}


async def probe_all_mcp_servers(
    api: Any,
    *,
    concurrency: int = 4,
    stdio_timeout: float = 8.0,
    remote_timeout: float = 6.0,
) -> dict[str, Any]:
    """Probe every enabled MCP server in parallel, bounded by ``concurrency``.

    Results land in ``api._mcp_discovery_cache``; this function only
    returns a summary so the GUI can show a "probed N of M" toast
    when the manual refresh button is used.

    stdio probes get a longer timeout because spawning Node + handshake
    can be slow; remote probes get a shorter one because hung TCP is
    expensive.
    """
    import asyncio
    from argusai.core.settings import normalize_mcp_servers

    try:
        all_servers = normalize_mcp_servers(api.settings_mgr.get().mcp_servers)
    except Exception:
        all_servers = []
    enabled = [s for s in all_servers if s.get("enabled", True)]
    if not enabled:
        return {"ok": True, "probed": 0, "alive": 0, "dead": 0, "servers": []}

    for s in enabled:
        sid = str(s.get("id") or "").strip()
        if sid:
            _mark_mcp_last_probe(api, sid, status="pending")

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _bounded(server: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            transport = str(server.get("transport") or "stdio")
            t = stdio_timeout if transport == "stdio" else remote_timeout
            return await _probe_one_mcp_server(api, server, timeout=t)

    results = await asyncio.gather(*(_bounded(s) for s in enabled), return_exceptions=True)
    summary = {"ok": True, "probed": 0, "alive": 0, "dead": 0, "servers": []}
    for r in results:
        if isinstance(r, dict):
            summary["servers"].append(r)
            summary["probed"] += 1
            if r.get("status") == "alive":
                summary["alive"] += 1
            elif r.get("status") == "dead":
                summary["dead"] += 1
    return summary


def mcp_server_list(api: Any, include_disabled: bool = True, **kwargs: Any) -> dict[str, Any]:
    """List configured external MCP servers without exposing env / header values."""
    from argusai.core.settings import normalize_mcp_servers

    servers = normalize_mcp_servers(api.settings_mgr.get().mcp_servers)
    if not include_disabled:
        servers = [server for server in servers if server.get("enabled", True)]
    return {"ok": True, "count": len(servers), "servers": [_mcp_public_server(server) for server in servers]}


def _mcp_marketplace_public_entry(
    value: dict[str, Any],
    *,
    include_config: bool = False,
) -> dict[str, Any]:
    """Return bounded catalogue metadata without secret-bearing values."""
    row = {
        "source": str(value.get("catalog_source") or ""),
        "server_id": str(value.get("name") or ""),
        "title": str(value.get("title") or ""),
        "english_title": str(value.get("english_title") or ""),
        "description": str(value.get("description") or "")[:1200],
        "version": str(value.get("version") or ""),
        "status": str(value.get("status") or ""),
        "categories": [str(item) for item in (value.get("categories") or [])[:10]],
        "tags": [str(item) for item in (value.get("tags") or [])[:15]],
        "view_count": _safe_int(value.get("view_count"), 0, 0, 2_000_000_000),
        "publisher": str(value.get("publisher") or ""),
        "is_hosted": bool(value.get("is_hosted")),
        "is_verified": bool(value.get("is_verified")),
        "website_url": str(value.get("website_url") or ""),
        "repository_url": str(value.get("repository_url") or ""),
        "importable": isinstance(value.get("config"), dict),
        "import_error": str(value.get("import_error") or ""),
    }
    config = value.get("config")
    if include_config and isinstance(config, dict):
        row["config_preview"] = {
            "id": str(config.get("id") or ""),
            "name": str(config.get("name") or ""),
            "enabled": False,
            "transport": str(config.get("transport") or ""),
            "command": str(config.get("command") or ""),
            "args": [str(item) for item in (config.get("args") or [])[:100]],
            "url": str(config.get("url") or ""),
            "env_keys": sorted(str(key) for key in (config.get("env") or {}).keys()),
            "header_keys": sorted(
                str(key) for key in (config.get("headers") or {}).keys()
            ),
            "timeout_seconds": _safe_int(
                config.get("timeout_seconds"), 20, 1, 120
            ),
            "notes": str(config.get("notes") or "")[:1200],
            "policy": dict(config.get("policy") or {}),
            "provenance": dict(config.get("provenance") or {}),
        }
    return row


def mcp_marketplace_search(
    api: Any,
    query: str = "",
    source: str = "modelscope",
    cursor: str = "",
    limit: int = 10,
    category: str = "",
    hosted: str = "all",
    **kwargs: Any,
) -> dict[str, Any]:
    """Search the public MCP marketplace without installing or enabling anything."""
    from argusai.core.infra.mcp_registry import search_mcp_marketplace

    hosted_value = str(hosted or "all").strip().lower()
    hosted_filter = (
        True
        if hosted_value == "hosted"
        else False
        if hosted_value == "local"
        else None
    )
    try:
        result = search_mcp_marketplace(
            source,
            query,
            cursor,
            _safe_int(limit, 10, 1, 20),
            category,
            hosted_filter,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "marketplace_request_failed",
            "error": str(exc),
        }
    return {
        "ok": True,
        "source": result.get("source"),
        "source_label": result.get("source_label"),
        "count": result.get("count", 0),
        "total": result.get("total", 0),
        "next_cursor": result.get("next_cursor", ""),
        "servers": [
            _mcp_marketplace_public_entry(row)
            for row in result.get("servers") or []
            if isinstance(row, dict)
        ],
        "read_only": True,
    }


def mcp_marketplace_get(
    api: Any,
    server_id: str = "",
    source: str = "modelscope",
    **kwargs: Any,
) -> dict[str, Any]:
    """Read one MCP marketplace entry and its redacted import preview."""
    from argusai.core.infra.mcp_registry import get_mcp_marketplace_entry

    try:
        result = get_mcp_marketplace_entry(source, server_id)
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "marketplace_request_failed",
            "error": str(exc),
        }
    server = result.get("server")
    if not isinstance(server, dict):
        return {
            "ok": False,
            "error_code": "invalid_marketplace_entry",
            "error": "marketplace returned an invalid server entry",
        }
    return {
        "ok": True,
        "source": result.get("source"),
        "server": _mcp_marketplace_public_entry(server, include_config=True),
        "read_only": True,
    }


def mcp_github_candidate(
    api: Any,
    repository: str = "",
    release_tag: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch public GitHub metadata for review without downloading release assets."""
    from argusai.core.security.mcp_provision import fetch_github_candidate

    return fetch_github_candidate(repository, release_tag=release_tag)


def mcp_provision_plan(
    api: Any,
    candidate_json: str = "{}",
    command: str = "",
    args_json: str = "[]",
    transport: str = "stdio",
    staging_dir: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an integrity-gated MCP admission plan without side effects."""
    import json
    from argusai.core.security.mcp_provision import build_provision_plan

    try:
        candidate = json.loads(candidate_json or "{}")
        args = json.loads(args_json or "[]")
    except json.JSONDecodeError as exc:
        return {"ok": False, "error_code": "invalid_json", "error": str(exc)}
    if not isinstance(candidate, dict):
        return {"ok": False, "error_code": "invalid_candidate", "error": "candidate_json must be an object"}
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return {"ok": False, "error_code": "invalid_args", "error": "args_json must be an array of strings"}
    return build_provision_plan(
        candidate,
        command=command,
        args=args,
        transport=transport,
        staging_dir=staging_dir,
    )


def mcp_revoke_plan(
    api: Any,
    server_id: str = "",
    managed_root: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan revocation of a configured MCP server without mutating settings."""
    from argusai.core.security.mcp_provision import build_revoke_plan

    server = _find_mcp_server(api, server_id)
    if not server:
        return {"ok": False, "error_code": "server_not_found", "error": f"MCP server not found: {server_id}"}
    return build_revoke_plan(server, managed_root=managed_root)


def mcp_server_add(
    api: Any,
    name: str = "",
    command: str = "",
    args: list[str] | None = None,
    server_id: str = "",
    enabled: bool = True,
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    notes: str = "",
    transport: str = "stdio",
    url: str = "",
    headers_json: str = "{}",
    policy_json: str = "{}",
    provenance_json: str = "{}",
    provenance: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Add or update one MCP server after launch lint and provenance normalization."""
    import json
    from argusai.core.settings import normalize_mcp_servers
    from argusai.core.infra.mcp_client import lint_mcp_stdio_args
    from argusai.core.security.mcp_provision import normalize_provenance

    try:
        env_obj = json.loads(env_json or "{}")
        env = {str(k): str(v) for k, v in env_obj.items()} if isinstance(env_obj, dict) else {}
    except Exception:
        return {"ok": False, "error": "env_json must be a JSON object"}
    try:
        headers_obj = json.loads(headers_json or "{}")
        headers = {str(k): str(v) for k, v in headers_obj.items()} if isinstance(headers_obj, dict) else {}
    except Exception:
        return {"ok": False, "error": "headers_json must be a JSON object"}
    try:
        policy_obj = json.loads(policy_json or "{}")
        policy = policy_obj if isinstance(policy_obj, dict) else {}
    except Exception:
        return {"ok": False, "error": "policy_json must be a JSON object"}
    provenance_obj: dict[str, Any] = provenance if isinstance(provenance, dict) else {}
    if provenance_json and provenance_json.strip() not in {"", "{}"}:
        try:
            parsed_provenance = json.loads(provenance_json)
            if not isinstance(parsed_provenance, dict):
                return {"ok": False, "error": "provenance_json must be a JSON object"}
            provenance_obj = parsed_provenance
        except Exception:
            return {"ok": False, "error": "provenance_json must be a JSON object"}
    clean_provenance = normalize_provenance(provenance_obj)

    transport_norm = str(transport or "stdio").strip().lower()
    if transport_norm == "stdio":
        launch_lint = lint_mcp_stdio_args(command, args or [])
        if not launch_lint.get("ok"):
            codes = sorted({
                str(item.get("code") or "unsafe_argument")
                for item in launch_lint.get("errors") or []
                if isinstance(item, dict)
            })
            return {
                "ok": False,
                "error_code": "unsafe_mcp_launch",
                "error": f"MCP stdio launch rejected: {', '.join(codes) or 'unsafe_argument'}",
                "launch_lint": launch_lint,
            }

    candidate = normalize_mcp_servers([{
        "id": server_id,
        "name": name,
        "enabled": enabled,
        "transport": transport,
        "command": command,
        "args": args or [],
        "env": env,
        "cwd": cwd,
        "url": url,
        "headers": headers,
        "timeout_seconds": timeout_seconds,
        "notes": notes,
        "policy": policy,
        "provenance": clean_provenance,
    }])
    if not candidate:
        return {"ok": False, "error": "command (stdio) or url (http/sse) is required"}
    server = candidate[0]
    result: dict[str, Any] = {}

    def _update(settings):
        servers = normalize_mcp_servers(settings.mcp_servers)
        replaced = False
        for idx, item in enumerate(servers):
            if item["id"] == server["id"]:
                servers[idx] = server
                replaced = True
                break
        if not replaced:
            servers.append(server)
        settings.mcp_servers = servers
        result.update({"replaced": replaced, "servers": servers})
        return settings

    api.settings_mgr.update(_update)
    servers = result["servers"]
    return {
        "ok": True,
        "action": "updated" if result["replaced"] else "created",
        "server": _mcp_public_server(server),
        "count": len(servers),
    }


def mcp_server_delete(api: Any, server_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Revoke one configured MCP server without exposing its secrets."""
    from argusai.core.settings import normalize_mcp_servers
    from argusai.core.security.mcp_provision import revoke_mcp_server

    sid = str(server_id or "").strip()
    if not sid:
        return {"ok": False, "error": "server_id is required"}
    result: dict[str, Any] = {}

    def _update(settings):
        servers = normalize_mcp_servers(settings.mcp_servers)
        revoked = revoke_mcp_server(servers, sid)
        if not revoked.get("ok"):
            raise ValueError(f"MCP server not found: {sid}")
        settings.mcp_servers = revoked["servers"]
        result.update(revoked)
        return settings

    try:
        api.settings_mgr.update(_update)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    next_servers = result["servers"]
    try:
        mcp_discovery_cache(api).pop(sid, None)
    except Exception:
        pass
    return {
        "ok": True,
        "deleted": sid,
        "revoked": sid,
        "count": len(next_servers),
        "receipt": result.get("receipt") or {},
    }


async def mcp_server_health(api: Any, server_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Check a configured external MCP server by id (any transport)."""
    import time
    from argusai.core.infra.mcp_client import (
        classify_exception,
        make_mcp_client,
        mcp_error_payload,
        mcp_runtime_for_stdio,
        spec_from_server_dict,
    )

    server = _find_mcp_server(api, server_id)
    sid = str(server_id or "").strip()
    if not server:
        return {"ok": False, "status": "missing", "error": f"MCP server not found: {sid}"}
    if not server.get("enabled", True):
        return {"ok": False, "status": "disabled", "server_id": sid, "server": server.get("name", sid)}
    spec = _mcp_server_spec(api, server, kwargs)
    started = time.perf_counter()
    try:
        async with make_mcp_client(spec) as client:
            health = client.health()
        runtime_info = mcp_runtime_for_stdio(spec) if spec.transport == "stdio" else {"transport": spec.transport}
        server_info = health.get("server_info") if isinstance(health.get("server_info"), dict) else {}
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _update_mcp_cache(api, sid, {
            "server_info": server_info,
            "protocol_version": health.get("protocol_version") or "",
            "capabilities": health.get("capabilities") if isinstance(health.get("capabilities"), dict) else {},
            "transport": spec.transport,
        })
        _mark_mcp_last_probe(api, sid, status="alive", elapsed_ms=elapsed_ms)
        return {
            "ok": True,
            "status": "alive",
            "server_id": sid,
            "server": spec.safe_name,
            "elapsed_ms": elapsed_ms,
            "runtime": health.get("runtime") or runtime_info,
            **{k: v for k, v in health.items() if k != "runtime"},
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _mark_mcp_last_probe(api, sid, status="dead", elapsed_ms=elapsed_ms, error_code=code, error=str(exc))
        return {
            **mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix),
            "status": "dead",
            "server_id": sid,
            "server": spec.safe_name,
            "elapsed_ms": elapsed_ms,
        }


def _policy_decision_for_tool(spec: Any, tool_name: str) -> dict[str, Any]:
    """Resolve allow/deny + effective risk for ``tool_name`` on ``spec``.

    Returns a dict the caller can attach to the structured response.
    """
    policy = spec.policy or {}
    allow_tools = list(policy.get("allow_tools") or [])
    deny_tools = list(policy.get("deny_tools") or [])
    tool_risks = dict(policy.get("tool_risks") or {})
    default_risk = str(policy.get("default_risk") or "high")
    name = (tool_name or "").strip()
    decision: dict[str, Any] = {
        "tool_name": name,
        "default_risk": default_risk,
        "effective_risk": tool_risks.get(name) or default_risk,
        "allow_auto_approve": bool(policy.get("allow_auto_approve")),
        "require_confirmation_for_all": bool(policy.get("require_confirmation_for_all", True)),
    }
    if name in set(deny_tools):
        decision["allowed"] = False
        decision["reason"] = "deny_listed"
        return decision
    if allow_tools and name not in set(allow_tools):
        decision["allowed"] = False
        decision["reason"] = "not_in_allow_list"
        return decision
    decision["allowed"] = True
    return decision


async def mcp_server_list_tools(
    api: Any,
    server_id: str = "",
    query: str = "",
    limit: int = 50,
    include_schema: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """List tools from a configured MCP server with bounded, searchable output.

    Large MCP servers can expose hundreds of tools. Returning every JSON
    schema in one response is exactly the kind of context explosion Loom22
    warns against, so this endpoint supports:

    * ``query``: case-insensitive match against tool name + description.
    * ``limit``: clamped page size (1..100), default 50.
    * ``include_schema``: when false, return only lightweight metadata.

    The full raw catalogue is still fetched from the server so the in-memory
    prompt cache can keep a compact name/description summary.
    """
    import time
    from argusai.core.infra.mcp_client import (
        classify_exception,
        make_mcp_client,
        mcp_error_payload,
        mcp_tool_to_registry_item,
        spec_from_server_dict,
    )

    server = _find_mcp_server(api, server_id)
    if not server:
        return {"ok": False, "error_code": "missing_server", "error": f"MCP server not found: {server_id}"}
    if not server.get("enabled", True):
        return {"ok": False, "error_code": "disabled", "error": "MCP server is disabled in settings."}
    spec = _mcp_server_spec(api, server, kwargs)
    q = str(query or "").strip().lower()
    try:
        max_items = int(limit)
    except Exception:
        max_items = 50
    max_items = max(1, min(100, max_items))
    want_schema = bool(include_schema)
    started = time.perf_counter()
    try:
        async with make_mcp_client(spec) as client:
            listing = await client.list_tools()
        policy = spec.policy or {}
        raw_tools = [tool for tool in (listing.get("tools") or []) if isinstance(tool, dict)]
        filtered_tools = []
        for tool in raw_tools:
            name = str(tool.get("name") or "").strip()
            desc = str(tool.get("description") or "").strip()
            haystack = f"{name}\n{desc}".lower()
            if q and q not in haystack:
                continue
            filtered_tools.append(tool)
        page_tools = filtered_tools[:max_items]
        tools_out = [
            mcp_tool_to_registry_item(
                spec.safe_name,
                tool,
                server_id=spec.server_id,
                transport=spec.transport,
                default_risk=str(policy.get("default_risk") or "high"),
                tool_risks=dict(policy.get("tool_risks") or {}),
                allow_tools=list(policy.get("allow_tools") or []),
                deny_tools=list(policy.get("deny_tools") or []),
            )
            for tool in page_tools
        ]
        if not want_schema:
            for tool in tools_out:
                tool.pop("parameters", None)
                tool.pop("required", None)
                tool.pop("schema", None)
        cached_tools: list[dict[str, str]] = []
        for raw_tool in raw_tools:
            name = str(raw_tool.get("name") or "").strip()
            if not name:
                continue
            desc = str(raw_tool.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 200:
                desc = desc[:197] + "..."
            cached_tools.append({"name": name, "description": desc})
        _update_mcp_cache(api, spec.server_id, {
            "transport": spec.transport,
            "tools": cached_tools,
        })
        return {
            "ok": True,
            "server_id": spec.server_id,
            "server": spec.safe_name,
            "transport": spec.transport,
            "count": len(tools_out),
            "total_count": len(raw_tools),
            "filtered_count": len(filtered_tools),
            "limit": max_items,
            "query": q,
            "include_schema": want_schema,
            "has_more": len(filtered_tools) > max_items,
            "tools": tools_out,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "policy_summary": {
                "default_risk": policy.get("default_risk"),
                "allow_auto_approve": bool(policy.get("allow_auto_approve")),
                "require_confirmation_for_all": bool(policy.get("require_confirmation_for_all", True)),
                "deny_tools": list(policy.get("deny_tools") or []),
                "allow_tools": list(policy.get("allow_tools") or []),
            },
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix)


async def mcp_server_call_tool(
    api: Any,
    server_id: str = "",
    tool_name: str = "",
    arguments_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Call one MCP server tool via its policy gate and structured error surface."""
    import json
    import time
    from argusai.core.infra.mcp_client import (
        classify_exception,
        make_mcp_client,
        maybe_truncate_call_result,
        mcp_error_payload,
        sanitize_mcp_arguments,
        spec_from_server_dict,
    )

    server = _find_mcp_server(api, server_id)
    if not server:
        return {"ok": False, "error_code": "missing_server", "error": f"MCP server not found: {server_id}"}
    if not server.get("enabled", True):
        return {"ok": False, "error_code": "disabled", "error": "MCP server is disabled in settings."}
    spec = _mcp_server_spec(api, server, kwargs)

    clean_tool = str(tool_name or "").strip()
    if not clean_tool:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="tool_name is required", retryable=False)
    try:
        parsed_args = json.loads(arguments_json or "{}")
        if not isinstance(parsed_args, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception as exc:
        return mcp_error_payload(spec, error_code="invalid_arguments", error=f"arguments_json must be a JSON object: {exc}", retryable=False, tool=clean_tool)
    parsed_args = sanitize_mcp_arguments(parsed_args)

    decision = _policy_decision_for_tool(spec, clean_tool)
    if not decision.get("allowed"):
        return mcp_error_payload(
            spec,
            error_code="tool_denied",
            error=f"Tool {clean_tool!r} is blocked by the server policy ({decision.get('reason')}).",
            retryable=False,
            suggested_fix="Update the server's allow_tools / deny_tools policy in the MCP panel.",
            tool=clean_tool,
            extra={"policy_decision": decision},
        )

    started = time.perf_counter()
    try:
        async with make_mcp_client(spec) as client:
            result = await client.call_tool(clean_tool, parsed_args)
        max_bytes = int((spec.policy or {}).get("max_result_bytes") or 0)
        result = maybe_truncate_call_result(spec, tool_name=clean_tool, result=result, max_bytes=max_bytes)
        return {
            "ok": not result.get("is_error"),
            "server_id": spec.server_id,
            "server": spec.safe_name,
            "tool": clean_tool,
            "arguments": parsed_args,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "policy_decision": decision,
            **{k: v for k, v in result.items() if k != "source"},
            "source": spec.source_block(tool=clean_tool),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix, tool=clean_tool, extra={"policy_decision": decision})


async def mcp_server_list_resources(api: Any, server_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """List MCP server resources by id (does not read their contents)."""
    import time
    from argusai.core.infra.mcp_client import classify_exception, make_mcp_client, mcp_error_payload, spec_from_server_dict

    server = _find_mcp_server(api, server_id)
    if not server:
        return {"ok": False, "error_code": "missing_server", "error": f"MCP server not found: {server_id}"}
    if not server.get("enabled", True):
        return {"ok": False, "error_code": "disabled", "error": "MCP server is disabled in settings."}
    spec = _mcp_server_spec(api, server, kwargs)
    started = time.perf_counter()
    try:
        async with make_mcp_client(spec) as client:
            listing = await client.list_resources()
        return {
            "ok": True,
            "server_id": spec.server_id,
            "server": spec.safe_name,
            "transport": spec.transport,
            "count": len(listing.get("resources") or []),
            **listing,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix)


async def mcp_server_read_resource(
    api: Any,
    server_id: str = "",
    uri: str = "",
    offset: int = 0,
    limit: int = 20000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read one MCP resource with size enforcement (returns at most ``limit`` chars)."""
    import time
    from argusai.core.infra.mcp_client import (
        classify_exception,
        make_mcp_client,
        maybe_truncate_call_result,
        mcp_error_payload,
        spec_from_server_dict,
    )

    server = _find_mcp_server(api, server_id)
    if not server:
        return {"ok": False, "error_code": "missing_server", "error": f"MCP server not found: {server_id}"}
    if not server.get("enabled", True):
        return {"ok": False, "error_code": "disabled", "error": "MCP server is disabled in settings."}
    spec = _mcp_server_spec(api, server, kwargs)
    clean_uri = str(uri or "").strip()
    if not clean_uri:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="uri is required", retryable=False)

    clamp_limit = max(256, min(int(limit or 20000), int((spec.policy or {}).get("max_result_bytes") or 200_000)))
    offset_int = max(0, int(offset or 0))

    started = time.perf_counter()
    try:
        async with make_mcp_client(spec) as client:
            result = await client.read_resource(clean_uri)
        summary = str(result.get("summary") or "")
        if offset_int or len(summary) > clamp_limit:
            sliced = summary[offset_int:offset_int + clamp_limit]
            result = {
                **result,
                "summary": sliced,
                "summary_offset": offset_int,
                "summary_limit": clamp_limit,
                "summary_truncated": len(summary) > offset_int + clamp_limit,
            }
        result = maybe_truncate_call_result(spec, tool_name=f"read_resource:{clean_uri}", result=result, max_bytes=clamp_limit * 4)
        return {
            "ok": True,
            "server_id": spec.server_id,
            "server": spec.safe_name,
            "uri": clean_uri,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **{k: v for k, v in result.items() if k != "source"},
            "source": spec.source_block(uri=clean_uri),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix, uri=clean_uri)


async def mcp_stdio_health(
    api: Any,
    command: str = "",
    args: list[str] | None = None,
    server_name: str = "external",
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Check whether an external stdio MCP server can start and complete initialize.

    Legacy ad-hoc entrypoint kept for backward compatibility. Prefer
    ``mcp_server_health`` after registering the server in settings.
    """
    import time
    from argusai.core.infra.mcp_client import (
        MCPStdioClient,
        classify_exception,
        mcp_error_payload,
        parse_server_spec,
    )

    spec = parse_server_spec(
        server_name=server_name,
        command=command,
        args=args or [],
        env_json=env_json,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        transport="stdio",
        **_mcp_runtime_context(api, kwargs),
    )
    if not spec.command:
        return {"ok": False, "status": "unconfigured", "error": "command is required"}
    started = time.perf_counter()
    try:
        async with MCPStdioClient(spec) as client:
            health = client.health()
        return {
            "ok": True,
            "status": "alive",
            "server": spec.safe_name,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **health,
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return {
            **mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix),
            "status": "dead",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


async def mcp_stdio_list_tools(
    api: Any,
    command: str = "",
    args: list[str] | None = None,
    server_name: str = "external",
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Discover tools from an external stdio MCP server and map them to ArgusAI tool metadata.

    Legacy ad-hoc entrypoint. Prefer ``mcp_server_list_tools`` after registering the server.
    """
    from argusai.core.infra.mcp_client import (
        MCPStdioClient,
        classify_exception,
        mcp_error_payload,
        mcp_tool_to_registry_item,
        parse_server_spec,
    )

    spec = parse_server_spec(
        server_name=server_name,
        command=command,
        args=args or [],
        env_json=env_json,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        transport="stdio",
        **_mcp_runtime_context(api, kwargs),
    )
    if not spec.command:
        return {"ok": False, "error_code": "invalid_arguments", "error": "command is required"}
    try:
        async with MCPStdioClient(spec) as client:
            result = await client.list_tools()
        tools = [
            mcp_tool_to_registry_item(spec.safe_name, tool, transport="stdio")
            for tool in result.get("tools") or []
            if isinstance(tool, dict)
        ]
        return {
            "ok": True,
            "server": spec.safe_name,
            "count": len(tools),
            "tools": tools,
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix)


async def mcp_stdio_call_tool(
    api: Any,
    command: str = "",
    tool_name: str = "",
    arguments_json: str = "{}",
    args: list[str] | None = None,
    server_name: str = "external",
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call one tool on an external stdio MCP server (ad-hoc, no saved policy)."""
    import json
    from argusai.core.infra.mcp_client import (
        MCPStdioClient,
        classify_exception,
        maybe_truncate_call_result,
        mcp_error_payload,
        parse_server_spec,
        sanitize_mcp_arguments,
    )

    spec = parse_server_spec(
        server_name=server_name,
        command=command,
        args=args or [],
        env_json=env_json,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        transport="stdio",
        **_mcp_runtime_context(api, kwargs),
    )
    if not spec.command:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="command is required", retryable=False)
    clean_tool = str(tool_name or "").strip()
    if not clean_tool:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="tool_name is required", retryable=False)
    try:
        parsed_args = json.loads(arguments_json or "{}")
        if not isinstance(parsed_args, dict):
            return mcp_error_payload(spec, error_code="invalid_arguments", error="arguments_json must be a JSON object", retryable=False, tool=clean_tool)
    except Exception as exc:
        return mcp_error_payload(spec, error_code="invalid_arguments", error=f"arguments_json must be a JSON object: {exc}", retryable=False, tool=clean_tool)
    parsed_args = sanitize_mcp_arguments(parsed_args)
    try:
        async with MCPStdioClient(spec) as client:
            result = await client.call_tool(clean_tool, parsed_args)
        result = maybe_truncate_call_result(spec, tool_name=clean_tool, result=result, max_bytes=int((spec.policy or {}).get("max_result_bytes") or 0))
        return {
            "ok": not result.get("is_error"),
            "server": spec.safe_name,
            "tool": clean_tool,
            "arguments": parsed_args,
            **{k: v for k, v in result.items() if k != "source"},
            "source": spec.source_block(tool=clean_tool),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix, tool=clean_tool)


async def mcp_stdio_list_resources(
    api: Any,
    command: str = "",
    args: list[str] | None = None,
    server_name: str = "external",
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """List resources from an external stdio MCP server without reading contents (legacy)."""
    from argusai.core.infra.mcp_client import (
        MCPStdioClient,
        classify_exception,
        mcp_error_payload,
        parse_server_spec,
    )

    spec = parse_server_spec(
        server_name=server_name,
        command=command,
        args=args or [],
        env_json=env_json,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        transport="stdio",
        **_mcp_runtime_context(api, kwargs),
    )
    if not spec.command:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="command is required", retryable=False)
    try:
        async with MCPStdioClient(spec) as client:
            result = await client.list_resources()
        return {
            "ok": True,
            "server": spec.safe_name,
            "count": len(result.get("resources") or []),
            **result,
            "source": spec.source_block(),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix)


async def mcp_stdio_read_resource(
    api: Any,
    command: str = "",
    uri: str = "",
    args: list[str] | None = None,
    server_name: str = "external",
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read one resource from an external stdio MCP server and normalize its content (legacy)."""
    from argusai.core.infra.mcp_client import (
        MCPStdioClient,
        classify_exception,
        mcp_error_payload,
        parse_server_spec,
    )

    spec = parse_server_spec(
        server_name=server_name,
        command=command,
        args=args or [],
        env_json=env_json,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        transport="stdio",
        **_mcp_runtime_context(api, kwargs),
    )
    clean_uri = str(uri or "").strip()
    if not spec.command:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="command is required", retryable=False)
    if not clean_uri:
        return mcp_error_payload(spec, error_code="invalid_arguments", error="uri is required", retryable=False)
    try:
        async with MCPStdioClient(spec) as client:
            result = await client.read_resource(clean_uri)
        return {
            "ok": True,
            "server": spec.safe_name,
            "uri": clean_uri,
            **{k: v for k, v in result.items() if k != "source"},
            "source": spec.source_block(uri=clean_uri),
        }
    except Exception as exc:
        code, retryable, fix = classify_exception(exc)
        return mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix, uri=clean_uri)


def session_vault_save(
    api: Any,
    name: str = "",
    headers_json: str = "{}",
    cookies_json: str = "{}",
    scope: list[str] | str | None = None,
    role: str = "",
    source: str = "",
    notes: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Save a named session for multi-role authorization workflows."""
    import json
    from argusai.core.infra.command import normalize_scope

    try:
        headers = json.loads(headers_json or "{}")
        cookies = json.loads(cookies_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid headers_json/cookies_json: {e}"}
    try:
        session = _resolve_session_vault(api).save(
            str(name or ""),
            headers=headers if isinstance(headers, dict) else {},
            cookies=cookies if isinstance(cookies, dict) else {},
            scope=normalize_scope(scope),
            role=role,
            source=source,
            notes=notes,
        )
        return {"ok": True, "session": session}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def session_vault_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List named sessions available to AuthMatrix tools."""
    sessions = _resolve_session_vault(api).list()
    return {"ok": True, "count": len(sessions), "sessions": sessions}


def session_vault_get(api: Any, name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Read one named session, including headers/cookies for replay tools."""
    try:
        session = _resolve_session_vault(api).get(str(name or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if session is None:
        return {"ok": False, "error": "session not found", "name": name}
    return {"ok": True, "session": session}


async def auth_matrix_from_vault(
    api: Any,
    urls: list[str] | str = "",
    scope: str = "",
    session_names: list[str] | str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run role_matrix_probe using named sessions from the session vault."""
    import json
    from argusai.core.security.auth_flow import role_matrix_probe as _probe
    from argusai.core.workspace.session_vault import headers_from_session

    names = session_names if isinstance(session_names, list) else [x.strip() for x in str(session_names or "").split(",") if x.strip()]
    vault = _resolve_session_vault(api)
    roles: dict[str, dict[str, str]] = {}
    missing = []
    for name in names:
        session = vault.get(name)
        if not session:
            missing.append(name)
            continue
        roles[name] = headers_from_session(session)
    if missing:
        return {"ok": False, "error": "missing sessions", "missing": missing}
    return await _probe(urls, scope=scope, roles_json=json.dumps(roles, ensure_ascii=False))


def web_request_parse(api: Any, url: str = "", method: str = "GET", headers_json: str = "{}", body: str = "", raw: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse explicit or raw HTTP request data into a canonical Web interaction."""
    import json
    from argusai.core.security.web_replay import parse_request

    headers: dict[str, str] = {}
    if headers_json:
        try:
            parsed = json.loads(headers_json)
            headers = parsed if isinstance(parsed, dict) else {}
        except Exception:
            headers = {}
    return parse_request(url=url, method=method, headers=headers, body=body, raw=raw)


def web_request_mutate(api: Any, request_json: str = "{}", location: str = "query", name: str = "", value: str = "", **kwargs: Any) -> dict[str, Any]:
    """Mutate one location in a canonical Web request."""
    import json
    from argusai.core.security.web_replay import mutate_request

    try:
        request = json.loads(request_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid request_json: {e}"}
    return mutate_request(request, location=location, name=name, value=value)


async def web_request_replay(api: Any, request_json: str = "{}", scope: str = "", timeout_seconds: int = 20, **kwargs: Any) -> dict[str, Any]:
    """Replay a canonical Web request with scope enforcement."""
    import json
    from argusai.core.security.web_replay import replay_request

    try:
        request = json.loads(request_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid request_json: {e}"}
    return await replay_request(request, scope=scope, timeout=_safe_int(timeout_seconds, 20, 1, 120))


def web_response_diff(api: Any, response_a_json: str = "{}", response_b_json: str = "{}", **kwargs: Any) -> dict[str, Any]:
    """Diff two Web replay responses."""
    import json
    from argusai.core.security.web_replay import diff_responses

    try:
        a = json.loads(response_a_json or "{}")
        b = json.loads(response_b_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid response JSON: {e}"}
    return diff_responses(a, b)


def web_request_to_curl(api: Any, request_json: str = "{}", **kwargs: Any) -> dict[str, Any]:
    """Convert a canonical Web request to a curl reproduction command."""
    import json
    from argusai.core.security.web_replay import request_to_curl

    try:
        request = json.loads(request_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid request_json: {e}"}
    return request_to_curl(request)


def api_inventory_build(api: Any, items_json: str = "", source: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a normalized API inventory from browser traces, proxy flows, HAR or endpoint lists."""
    from argusai.core.security.web_protocol_tools import api_inventory_build as _tool

    return _tool(items_json=items_json, source=source)


def business_surface_extract(
    api: Any,
    items_json: str = "",
    html: str = "",
    text: str = "",
    source: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Extract evidence-backed business candidates; use after browser/proxy/HAR/JS discovery, not as a final classifier."""
    from argusai.core.security.business_surface import business_surface_extract as _tool

    return _tool(items_json=items_json, html=html, text=text, source=source)


def business_test_plan(
    api: Any,
    surface_json: str = "{}",
    depth: str = "standard",
    roles_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Summarize coverage requirements and evidence gaps so the Agent can create concrete todos."""
    from argusai.core.security.business_surface import business_test_plan as _tool

    return _tool(surface_json=surface_json, depth=depth, roles_json=roles_json)


def business_risk_select(
    api: Any,
    surface_json: str = "{}",
    asset: str = "",
    risk_type: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Filter candidate business signals for Agent review before targeted validation."""
    from argusai.core.security.business_surface import business_risk_select as _tool

    return _tool(surface_json=surface_json, asset=asset, risk_type=risk_type)


def api_inventory_diff(api: Any, old_json: str = "{}", new_json: str = "{}", **kwargs: Any) -> dict[str, Any]:
    """Diff two API inventories and list added/removed endpoints."""
    from argusai.core.security.web_protocol_tools import api_inventory_diff as _tool

    return _tool(old_json=old_json, new_json=new_json)


def api_spec_import(api: Any, spec: str = "", base_url: str = "", source: str = "", **kwargs: Any) -> dict[str, Any]:
    """Import an OpenAPI (v2/v3, JSON or YAML) or Postman collection into an API inventory.

    Each endpoint carries a replayable request_template and a parameters list, so
    the Agent can feed it straight into api_request_select / web_request_replay /
    the fuzzer. Pass base_url to override or supply the server when the spec omits
    an absolute one.
    """
    from argusai.core.security.web_protocol_tools import api_spec_import as _tool

    return _tool(spec=spec, base_url=base_url, source=source)


def api_request_select(
    api: Any,
    inventory_json: str = "{}",
    protocol: str = "",
    tag: str = "",
    method: str = "",
    contains: str = "",
    limit: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Select endpoints from an API inventory before replay or protocol-specific probing."""
    from argusai.core.security.web_protocol_tools import api_request_select as _tool

    return _tool(
        inventory_json=inventory_json,
        protocol=protocol,
        tag=tag,
        method=method,
        contains=contains,
        limit=_safe_int(limit, 20, 1, 100),
    )


async def graphql_probe(
    api: Any,
    endpoint: str = "",
    scope: str = "",
    headers_json: str = "{}",
    introspect: bool = True,
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run safe read-only GraphQL probes (__typename and optional introspection)."""
    import json
    from argusai.core.security.web_protocol_tools import graphql_probe as _tool

    try:
        headers = json.loads(headers_json or "{}")
        if not isinstance(headers, dict):
            headers = {}
    except Exception:
        headers = {}
    return await _tool(
        endpoint,
        scope=scope,
        headers={str(k): str(v) for k, v in headers.items()},
        introspect=bool(introspect),
        timeout=_safe_int(timeout_seconds, 20, 1, 60),
    )


async def graphql_batch_probe(
    api: Any,
    endpoint: str = "",
    scope: str = "",
    headers_json: str = "{}",
    timeout_seconds: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Check whether a GraphQL endpoint accepts JSON-array query batching."""
    import json
    from argusai.core.security.web_protocol_tools import graphql_batch_probe as _tool

    try:
        headers = json.loads(headers_json or "{}")
        if not isinstance(headers, dict):
            headers = {}
    except Exception:
        headers = {}
    return await _tool(
        endpoint,
        scope=scope,
        headers={str(k): str(v) for k, v in headers.items()},
        timeout=_safe_int(timeout_seconds, 20, 1, 60),
    )


def websocket_handshake_probe(
    api: Any,
    url: str = "",
    scope: str = "",
    headers_json: str = "{}",
    timeout_seconds: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform a scoped WebSocket Upgrade handshake without sending frames."""
    import json
    from argusai.core.security.web_protocol_tools import websocket_handshake_probe as _tool

    try:
        headers = json.loads(headers_json or "{}")
        if not isinstance(headers, dict):
            headers = {}
    except Exception:
        headers = {}
    return _tool(
        url,
        scope=scope,
        headers={str(k): str(v) for k, v in headers.items()},
        timeout=_safe_int(timeout_seconds, 10, 1, 30),
    )


def websocket_connect_probe(
    api: Any,
    url: str = "",
    scope: str = "",
    headers_json: str = "{}",
    origins_json: str = "[]",
    timeout_seconds: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Probe WebSocket connection and Origin behavior without sending frames."""
    import json
    from argusai.core.security.web_protocol_tools import websocket_connect_probe as _tool

    try:
        headers = json.loads(headers_json or "{}")
        if not isinstance(headers, dict):
            headers = {}
    except Exception:
        headers = {}
    try:
        origins = json.loads(origins_json or "[]")
        if isinstance(origins, str):
            origins = [origins]
        if not isinstance(origins, list):
            origins = []
    except Exception:
        origins = []
    return _tool(
        url,
        scope=scope,
        headers={str(k): str(v) for k, v in headers.items()},
        origins=[str(x) for x in origins],
        timeout=_safe_int(timeout_seconds, 10, 1, 30),
    )


def websocket_replay_plan(api: Any, messages_json: str = "[]", url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a safe WebSocket replay/mutation plan from captured frame messages."""
    from argusai.core.security.web_protocol_tools import websocket_replay_plan as _tool

    return _tool(messages_json=messages_json, url=url)


def websocket_frame_analyze(api: Any, messages_json: str = "[]", **kwargs: Any) -> dict[str, Any]:
    """Analyze captured WebSocket frames from browser hooks, HAR, or proxy output."""
    from argusai.core.security.web_protocol_tools import websocket_frame_analyze as _tool

    return _tool(messages_json=messages_json)


def websocket_send_receive(
    api: Any,
    url: str = "",
    scope: str = "",
    headers_json: str = "{}",
    messages_json: str = "[]",
    timeout_seconds: int = 10,
    receive_count: int = 3,
    delay_seconds: int = 1,
    authorization_phrase: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send a tiny number of authorized WebSocket messages and capture replies."""
    import json
    from argusai.core.security.web_protocol_tools import websocket_send_receive as _tool

    try:
        headers = json.loads(headers_json or "{}")
        if not isinstance(headers, dict):
            headers = {}
    except Exception:
        headers = {}
    try:
        messages = json.loads(messages_json or "[]")
        if not isinstance(messages, list):
            messages = [messages]
    except Exception as e:
        return {"ok": False, "error": f"invalid messages_json: {e}"}
    return _tool(
        url,
        scope=scope,
        headers={str(k): str(v) for k, v in headers.items()},
        messages=messages,
        timeout=_safe_int(timeout_seconds, 10, 1, 30),
        receive_count=_safe_int(receive_count, 3, 0, 10),
        delay_seconds=float(_safe_int(delay_seconds, 1, 1, 10)),
        authorization_phrase=authorization_phrase,
    )


def websocket_authz_probe(
    api: Any,
    url: str = "",
    scope: str = "",
    baseline_headers_json: str = "{}",
    comparison_headers_json: str = "{}",
    message_json: str = "",
    timeout_seconds: int = 10,
    authorization_phrase: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Compare one WebSocket message under two authorization contexts."""
    import json
    from argusai.core.security.web_protocol_tools import websocket_authz_probe as _tool

    def _headers(text: str) -> dict[str, str]:
        try:
            raw = json.loads(text or "{}")
            return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    try:
        message = json.loads(message_json) if message_json else ""
    except Exception:
        message = message_json
    return _tool(
        url,
        scope=scope,
        baseline_headers=_headers(baseline_headers_json),
        comparison_headers=_headers(comparison_headers_json),
        message=message,
        timeout=_safe_int(timeout_seconds, 10, 1, 30),
        authorization_phrase=authorization_phrase,
    )


def grpc_web_analyze(api: Any, headers_json: str = "{}", body: str = "", body_format: str = "text", **kwargs: Any) -> dict[str, Any]:
    """Analyze gRPC-Web headers/body and preview protobuf wire fields."""
    from argusai.core.security.web_protocol_tools import grpc_web_analyze as _tool

    return _tool(headers=headers_json, body=body, body_format=body_format)


def protobuf_field_preview(api: Any, data: str = "", input_format: str = "base64", **kwargs: Any) -> dict[str, Any]:
    """Preview protobuf wire fields without requiring a .proto schema."""
    from argusai.core.security.web_protocol_tools import protobuf_field_preview as _tool

    return _tool(data=data, input_format=input_format)


def js_signature_oracle_plan(
    api: Any,
    job_id: str = "",
    request_artifact_ref: str = "",
    signature_fields: list[str] | str = "",
    runtime: str = "browser",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan the ReverseJob oracle records needed for signed Web request replay."""
    fields = signature_fields if isinstance(signature_fields, list) else [x.strip() for x in str(signature_fields or "").split(",") if x.strip()]
    return {
        "ok": True,
        "job_id": job_id,
        "request_artifact_ref": request_artifact_ref,
        "runtime": runtime,
        "signature_fields": fields,
        "workflow": [
            "Apply fetch/xhr/crypto hook recipes to capture unsigned inputs and signed outputs.",
            "Drain hook hits and persist them with reverse_oracle_add(stage='signature').",
            "Use js_runtime_* or browser hooks as an oracle to regenerate signatures after web_request_mutate.",
            "Replay mutated requests with web_request_replay and attach response evidence to the same case.",
        ],
        "recommended_tools": [
            "reverse_recipe_apply",
            "browser_hook_drain",
            "reverse_oracle_add",
            "js_runtime_call",
            "web_request_mutate",
            "web_request_replay",
        ],
    }


