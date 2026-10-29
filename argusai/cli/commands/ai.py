"""``argusai ai`` — manage chat AI endpoints in settings.json."""

from __future__ import annotations

import json
from typing import Any, Optional

import typer
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload, parse_kv_pairs
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="ai",
    help="Manage chat AI endpoints (settings.json).",
    rich_markup_mode="rich",
)


def _print_endpoints(rows: list[dict[str, Any]], *, ai_enabled: bool) -> None:
    table = Table(
        title="AI Endpoints",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("#")
    table.add_column("On")
    table.add_column("Active")
    table.add_column("Name")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Priority")
    table.add_column("Key")
    table.add_column("Base URL")
    for row in rows:
        table.add_row(
            str(row.get("index")),
            "yes" if row.get("enabled") else "no",
            "*" if row.get("active") else "",
            str(row.get("name") or "")[:24],
            str(row.get("provider_id") or "")[:18],
            str(row.get("model") or "")[:28],
            str(row.get("priority") or 0),
            "set" if row.get("api_key_set") else "-",
            str(row.get("base_url") or "")[:40],
        )
    get_console().print(table)
    if not ai_enabled:
        get_console().print("[warn]ai.enabled is false in settings.json[/]")
    get_console().print(
        "[hud.dim]Add: argusai ai add --provider ali-bailian --api-key sk-… · "
        "Switch: argusai ai activate <name|#>[/]"
    )


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured chat AI endpoints."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_ai_endpoints()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("endpoints") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload(
            {"ai_enabled": result.get("ai_enabled", True), "endpoints": rows},
            json_out=True,
        )
        return
    if not rows:
        get_console().print("[hud.dim]No AI endpoints configured.[/]")
        get_console().print(
            "[hud.dim]Try: argusai ai providers · "
            "argusai ai add --provider ali-bailian --api-key sk-…[/]"
        )
        return
    _print_endpoints(rows, ai_enabled=bool(result.get("ai_enabled", True)))


@app.command("providers")
def providers_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List built-in provider presets usable with ``ai add --provider``."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_ai_provider_presets()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    presets = [p for p in list(result.get("presets") or []) if isinstance(p, dict)]
    if json_out:
        ok_payload({"presets": presets, "categories": result.get("categories") or []}, json_out=True)
        return
    table = Table(
        title="AI Provider Presets",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Protocol")
    table.add_column("Default model")
    table.add_column("Base URL")
    for p in presets:
        table.add_row(
            str(p.get("id") or ""),
            str(p.get("name") or "")[:22],
            str(p.get("protocol") or ""),
            str(p.get("default_model") or "")[:28],
            str(p.get("base_url") or "")[:42],
        )
    get_console().print(table)
    get_console().print(
        "[hud.dim]Example: argusai ai add --provider ali-bailian --api-key sk-…[/]"
    )


@app.command("add")
def add_cmd(
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Display name."),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Preset id/name/alias (see: argusai ai providers).",
    ),
    base_url: Optional[str] = typer.Option(None, "--base-url", "-u", help="API base URL."),
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k", help="API key / token."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Active model id."),
    protocol: Optional[str] = typer.Option(
        None, "--protocol", help="openai or anthropic (default from provider)."
    ),
    priority: Optional[int] = typer.Option(None, "--priority", help="Failover priority (higher first)."),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="Max output tokens."),
    context_window: Optional[int] = typer.Option(None, "--context-window", help="Context window tokens."),
    index: Optional[int] = typer.Option(None, "--index", help="Update existing endpoint by index."),
    extra_body: Optional[str] = typer.Option(
        None,
        "--extra-body",
        help='JSON object merged into extra_body, e.g. \'{"explicit_cache":true}\'.',
    ),
    header: Optional[list[str]] = typer.Option(
        None, "--header", "-H", help="Extra header KEY=VALUE (repeatable)."
    ),
    enable: bool = typer.Option(True, "--enable/--disable", help="Enabled flag."),
    activate: bool = typer.Option(
        False, "--activate", help="After save, raise priority so this endpoint is active."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Add or update a chat AI endpoint in settings.json."""
    set_json_mode(json_out)
    if not provider and not base_url and index is None and not name:
        fail(
            "need --provider and/or --base-url (or --index/--name to update)",
            json_out=json_out,
        )

    payload: dict[str, Any] = {"enabled": bool(enable)}
    if name:
        payload["name"] = name.strip()
    if provider:
        payload["provider_id"] = provider.strip()
    if base_url:
        payload["base_url"] = base_url.strip()
    if api_key is not None:
        payload["api_key"] = api_key
    if model:
        payload["model"] = model.strip()
    if protocol:
        proto = protocol.strip().lower()
        if proto not in {"openai", "anthropic"}:
            fail("--protocol must be openai or anthropic", json_out=json_out)
        payload["protocol"] = proto
    if priority is not None:
        payload["priority"] = int(priority)
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    if context_window is not None:
        payload["context_window"] = int(context_window)
    if index is not None:
        payload["index"] = int(index)

    if extra_body:
        try:
            parsed = json.loads(extra_body)
        except json.JSONDecodeError as exc:
            fail(f"--extra-body is not valid JSON: {exc}", json_out=json_out)
        if not isinstance(parsed, dict):
            fail("--extra-body must be a JSON object", json_out=json_out)
        payload["extra_body"] = parsed

    try:
        headers = parse_kv_pairs(header, flag="--header")
    except ValueError as exc:
        fail(str(exc), json_out=json_out)
    if headers:
        payload["extra_headers"] = headers

    api = headless_api()
    try:
        result = api.save_ai_endpoint(payload)
        if isinstance(result, dict) and result.get("ok") and activate:
            sel = result.get("index")
            activated = api.activate_ai_endpoint(sel)
            if isinstance(activated, dict) and activated.get("ok"):
                result = activated
                result["activated"] = True
    finally:
        api.shutdown_background_services()

    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "save failed"), json_out=json_out)
    endpoint = dict(result.get("endpoint") or {})
    idx = result.get("index")
    if json_out:
        ok_payload({"index": idx, "endpoint": endpoint, "activated": bool(result.get("activated"))}, json_out=True)
        return
    get_console().print(
        f"[ok]saved[/]  #{idx}  [accent]{endpoint.get('name') or ''}[/]  "
        f"model={endpoint.get('model')}  provider={endpoint.get('provider_id')}"
    )
    get_console().print(
        f"[hud.dim]List: argusai ai list · Test: argusai ai test {idx} · "
        f"Activate: argusai ai activate {idx}[/]"
    )


@app.command("enable")
def enable_cmd(
    selector: str = typer.Argument(..., help="Endpoint index or name."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Enable an endpoint without deleting it."""
    _set_enabled(selector, True, json_out=json_out)


@app.command("disable")
def disable_cmd(
    selector: str = typer.Argument(..., help="Endpoint index or name."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Disable an endpoint without deleting it."""
    _set_enabled(selector, False, json_out=json_out)


def _set_enabled(selector: str, enabled: bool, *, json_out: bool) -> None:
    set_json_mode(json_out)
    api = headless_api()
    try:
        listed = api.list_ai_endpoints()
        if not isinstance(listed, dict) or not listed.get("ok"):
            fail(str((listed or {}).get("error") or "list failed"), json_out=json_out)
        rows = [r for r in list(listed.get("endpoints") or []) if isinstance(r, dict)]
        match = _match_row(rows, selector)
        if match is None:
            fail(f"endpoint not found: {selector!r}", json_out=json_out)
        result = api.save_ai_endpoint({"index": int(match["index"]), "enabled": bool(enabled)})
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "update failed"), json_out=json_out)
    verb = "enabled" if enabled else "disabled"
    ok_payload(
        {"endpoint": result.get("endpoint")},
        json_out=json_out,
        human=f"[ok]{verb}[/]  {selector}",
    )


@app.command("remove")
def remove_cmd(
    selector: str = typer.Argument(..., help="Endpoint index or name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Delete an AI endpoint from settings.json."""
    set_json_mode(json_out)
    if not yes and not json_out:
        if not typer.confirm(f"Delete AI endpoint {selector!r}?", default=False):
            raise typer.Abort()
    api = headless_api()
    try:
        result = api.delete_ai_endpoint(selector)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    ok_payload(
        {"index": result.get("index"), "name": result.get("name")},
        json_out=json_out,
        human=f"[ok]removed[/]  #{result.get('index')}  {result.get('name') or selector}",
    )


@app.command("activate")
def activate_cmd(
    selector: str = typer.Argument(..., help="Endpoint index or name."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Also switch active model."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Raise an endpoint's priority so new turns use it first."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.activate_ai_endpoint(selector, model or "")
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "activate failed"), json_out=json_out)
    endpoint = dict(result.get("endpoint") or {})
    ok_payload(
        {"endpoint": endpoint},
        json_out=json_out,
        human=(
            f"[ok]active[/]  #{endpoint.get('index')}  {endpoint.get('name')}  "
            f"model={endpoint.get('model')}"
        ),
    )


@app.command("test")
def test_cmd(
    selector: str = typer.Argument(..., help="Endpoint index or name."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Send a lightweight ping prompt to one endpoint."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        listed = api.list_ai_endpoints()
        if not isinstance(listed, dict) or not listed.get("ok"):
            fail(str((listed or {}).get("error") or "list failed"), json_out=json_out)
        rows = [r for r in list(listed.get("endpoints") or []) if isinstance(r, dict)]
        match = _match_row(rows, selector)
        if match is None:
            fail(f"endpoint not found: {selector!r}", json_out=json_out)
        # Need the real key for the probe — reload from settings via save path.
        settings = api.settings_mgr.get()
        endpoints = list(getattr(settings, "ai_endpoints", None) or [])
        index = int(match["index"])
        if index < 0 or index >= len(endpoints):
            fail(f"endpoint not found: {selector!r}", json_out=json_out)
        payload = endpoints[index].to_dict()
        result = api.test_ai_endpoint(payload)
    finally:
        api.shutdown_background_services()

    if json_out:
        from argusai.cli.ui import emit_result

        emit_result({"ok": bool(result.get("ok")), **{k: v for k, v in result.items() if k != "ok"}})
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if result.get("ok"):
        get_console().print(
            f"[ok]test ok[/]  #{match['index']}  {match.get('name')}  "
            f"response={str(result.get('response') or '')[:80]}"
        )
        return
    fail(str(result.get("error") or "test failed"), json_out=False)


def _match_row(rows: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    text = str(selector or "").strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        return next((r for r in rows if int(r.get("index", -1)) == index), None)
    folded = text.casefold()
    return next((r for r in rows if str(r.get("name") or "").casefold() == folded), None) or next(
        (r for r in rows if folded in str(r.get("name") or "").casefold()),
        None,
    )
