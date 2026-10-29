"""Proxy lifecycle manager (thread + asyncio loop)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from argusai.core.events import Event, EventBus
from argusai.core.finding import Finding
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_constants import _DEFAULT_MAX_HISTORY
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow
from argusai.core.security.proxy_server import ProxyServer
from argusai.core.security.proxy_system import (
    _clear_system_proxy_snapshot,
    _remember_system_proxy_snapshot,
)

log = logging.getLogger("argusai.proxy")

# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op. Lets the proxy workbench reflect new flows /
# findings promptly instead of relying solely on its 1.5s poll.
_UI_NOTIFY: Callable[[str, dict[str, Any]], None] | None = None


def set_ui_notify(callback: "Callable[[str, dict[str, Any]], None] | None") -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


def _notify_proxy_ui(reason: str = "") -> None:
    cb = _UI_NOTIFY
    if cb is None:
        return
    try:
        cb("proxy", {"reason": reason})
    except Exception:
        pass


def _notify_breakpoint_ui() -> None:
    # Dedicated channel: fires only when a request parks / resolves, so the
    # breakpoint panel never rides the high-frequency "proxy" flow pushes.
    cb = _UI_NOTIFY
    if cb is None:
        return
    try:
        cb("proxy_breakpoint", {})
    except Exception:
        pass


class ProxyManager:
    """Manages proxy lifecycle, similar to TaskManager for scans."""

    def __init__(self, bus: EventBus | None = None, ca_dir: Path | None = None) -> None:
        from argusai.core.security.proxy_ca import migrate_legacy_ca_dir

        self.bus = bus or EventBus()
        self._ca_dir = Path(ca_dir) if ca_dir else Path("proxy_ca")
        self._ca_dir.mkdir(parents=True, exist_ok=True)
        migrate_legacy_ca_dir(self._ca_dir)
        self._server: ProxyServer | None = None
        self._analyzer: PassiveAnalyzer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._findings: list[Finding] = []
        self._finding_keys: set[str] = set()
        self._config: ProxyConfig | None = None
        self._system_proxy_snapshot: Any = None
        self._ready_event: threading.Event | None = None
        self._startup_error: str | None = None
        # History survives stop/restart so a stopped proxy can still be browsed
        # and "save and restart" does not wipe the capture.
        self._retained_flows: list[TrafficFlow] = []
        self._last_flow_id = 0
        # Bumped on clear so incremental readers know to drop their buffer.
        self._generation = 0
        # Active-rewrite rules live in the server's hot path, so they are kept
        # here (raw) and re-pushed to each freshly created server on start.
        self._rewrite_rules: list[dict[str, Any]] = []
        # Interactive breakpoint config (runtime-only; not persisted). Re-pushed
        # to each freshly created server on start.
        self._breakpoint_config: dict[str, Any] = {}
        self._control: Any = None

        from argusai.core.security.proxy_decrypt import ProxyDecryptService

        self.decryptor = ProxyDecryptService()
        decrypt_analyzer = PassiveAnalyzer(self.bus)
        self.decryptor.analyze = lambda flow: decrypt_analyzer.analyze(flow, source="decrypted")
        self.decryptor.on_decoded = self._account_decoded

        self._unsub = self.bus.subscribe(self._on_event)

    def _account_decoded(self, flow: TrafficFlow, delta: int) -> None:
        server = self._server
        if server is not None and any(f is flow for f in reversed(server.flows[-50:])):
            server.note_decoded_bytes(delta)

    def _on_event(self, evt: Event) -> None:
        if evt.type in ("proxy.flow", "finding"):
            _notify_proxy_ui(evt.type)
        if evt.type == "proxy.flow":
            server = self._server
            flow_id = evt.payload.get("flow_id")
            if server is not None and flow_id is not None:
                for flow in reversed(server.flows):
                    if flow.id == flow_id:
                        self.decryptor.submit(flow)
                        break
            return
        if evt.type == "finding":
            raw = evt.payload.get("finding")
            if raw:
                try:
                    finding = Finding.from_dict(raw)
                except Exception:
                    return
                # A restart brings a fresh analyzer whose dedup cache is empty.
                key = f"{finding.id}|{finding.url or ''}"
                if key in self._finding_keys:
                    return
                self._finding_keys.add(key)
                self._findings.append(finding)

    def _history(self) -> list[TrafficFlow]:
        server = self._server
        return list(server.flows) if server is not None else list(self._retained_flows)

    def _retain_history(self, server: ProxyServer | None) -> None:
        if server is None:
            return
        self._retained_flows = list(server.flows)
        self._last_flow_id = max(self._last_flow_id, server.last_flow_id)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    def needs_shutdown(self) -> bool:
        if self._system_proxy_snapshot is not None:
            return True
        if self._thread is not None and self._thread.is_alive():
            return True
        return self.is_running

    def _restore_system_proxy(self) -> None:
        snapshot = self._system_proxy_snapshot
        if snapshot is None:
            return
        self._system_proxy_snapshot = None
        _clear_system_proxy_snapshot(snapshot)
        try:
            from argusai.core.security.system_proxy import restore_system_proxy

            restore_system_proxy(snapshot)
        except Exception as exc:
            log.warning("Failed to restore system proxy: %s", exc)

    @property
    def config(self) -> ProxyConfig | None:
        return self._config

    def start_proxy(
        self,
        config: ProxyConfig | None = None,
        *,
        use_system_proxy: bool = False,
        startup_timeout: float = 5.0,
    ) -> dict[str, Any]:
        if self.is_running:
            return {"ok": False, "error": "proxy already running"}

        config = config or ProxyConfig()
        config.ca_dir = self._ca_dir
        self._ensure_ca_material()
        self._config = config
        self._analyzer = PassiveAnalyzer(self.bus)
        self._server = ProxyServer(config, self._analyzer, self.bus)
        self._server.adopt_history(self._retained_flows, self._last_flow_id)
        self._server.set_rewrite_rules(self._rewrite_rules)
        self._server.set_breakpoint_config(self._breakpoint_config)
        self._server.set_breakpoint_on_change(_notify_breakpoint_ui)

        if use_system_proxy:
            from argusai.core.security.system_proxy import enable_system_proxy

            snapshot, sys_result = enable_system_proxy(config.listen_host, config.listen_port)
            if not sys_result.get("ok"):
                self._server = None
                self._analyzer = None
                self._config = None
                return {"ok": False, "error": sys_result.get("error") or "failed to set system proxy"}
            self._system_proxy_snapshot = snapshot
            _remember_system_proxy_snapshot(snapshot)

        self._ready_event = threading.Event()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="proxy-thread"
        )
        self._thread.start()

        if not self._ready_event.wait(timeout=startup_timeout):
            err = self._startup_error or "proxy startup timed out"
            self.stop_proxy(force=True)
            return {"ok": False, "error": err}
        if self._startup_error:
            err = self._startup_error
            self.stop_proxy(force=True)
            return {"ok": False, "error": err}

        control: dict[str, Any] | None = None
        try:
            from argusai.core.security.proxy_control import ProxyControlServer

            self._control = ProxyControlServer(self, self._ca_dir)
            control = self._control.start()
        except Exception:
            log.warning("proxy breakpoint control failed to start", exc_info=True)
            self._control = None

        return {
            "ok": True,
            "host": config.listen_host,
            "port": config.listen_port,
            "scope": config.scope,
            "system_proxy": bool(use_system_proxy),
            "ca": self.get_ca_info(),
            "control": control,
        }

    def stop_proxy(self, *, force: bool = False) -> dict[str, Any]:
        control = self._control
        self._control = None
        if control is not None:
            try:
                control.stop()
            except Exception:
                log.debug("proxy control stop failed", exc_info=True)
        if not self.needs_shutdown():
            return {"ok": False, "error": "proxy not running"}

        server = self._server
        loop = self._loop
        thread = self._thread
        wait_timeout = 1.5 if force else 5.0
        join_timeout = 1.0 if force else 5.0

        if (
            server is not None
            and server.is_running
            and loop is not None
            and not loop.is_closed()
        ):
            server._running = False
            stop_coro = server.stop()
            try:
                future = asyncio.run_coroutine_threadsafe(stop_coro, loop)
            except RuntimeError as e:
                # Loop closed between our check and the dispatch — close the
                # coroutine ourselves so it doesn't trigger "never awaited".
                stop_coro.close()
                log.debug("proxy stop dispatch skipped: %s", e)
            except Exception as e:
                stop_coro.close()
                log.warning("proxy stop dispatch failed: %s", e)
            else:
                try:
                    future.result(timeout=wait_timeout)
                except Exception as e:
                    log.warning("proxy stop timed out or failed: %s", e)

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive() and force:
                log.debug("proxy thread still alive after forced shutdown")

        self._retain_history(server)
        self._server = None
        self._thread = None
        self._loop = None
        self._analyzer = None
        self._config = None
        self._ready_event = None
        self._startup_error = None
        self._restore_system_proxy()
        return {"ok": True}

    def _ensure_ca_material(self) -> None:
        cert = self._ca_dir / "ca.pem"
        key = self._ca_dir / "ca.key"
        if cert.is_file() and key.is_file():
            return
        bootstrap = ProxyServer(
            ProxyConfig(ca_dir=self._ca_dir),
            PassiveAnalyzer(self.bus),
            self.bus,
        )
        bootstrap._load_ca_material()

    def get_ca_info(self) -> dict[str, Any]:
        from argusai.core.security.proxy_ca import ca_info

        self._ensure_ca_material()
        return ca_info(self._ca_dir)

    def install_proxy_ca(self) -> dict[str, Any]:
        from argusai.core.security.proxy_ca import ca_cert_path, install_ca_to_user_store

        return install_ca_to_user_store(ca_cert_path(self._ca_dir))

    def uninstall_proxy_ca(self) -> dict[str, Any]:
        from argusai.core.security.proxy_ca import ca_cert_path, uninstall_ca_from_user_store

        return uninstall_ca_from_user_store(ca_cert_path(self._ca_dir))

    def open_proxy_ca_dir(self) -> dict[str, Any]:
        from argusai.core.security.proxy_ca import reveal_ca_dir

        return reveal_ca_dir(self._ca_dir)

    def get_flow_detail(
        self,
        flow_id: int,
        *,
        text_limit: int | None = None,
        inline_media: bool = True,
    ) -> dict[str, Any]:
        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        return {
            "ok": True,
            "flow": flow.to_dict(
                include_body_preview=True,
                text_limit=text_limit,
                inline_media=inline_media,
            ),
        }

    def flow_har_json(self, flow_id: int) -> dict[str, Any]:
        """Build a single-entry HAR 1.2 document for one captured flow."""
        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        from argusai.core.security.proxy_har import flows_to_har

        har = flows_to_har([flow])
        return {
            "ok": True,
            "har": har,
            "flow_summary": {
                "id": flow.id,
                "method": flow.method,
                "url": flow.url,
                "host": flow.host,
                "status": flow.response_status,
                "resource_kind": flow.resource_kind,
            },
        }

    def _find_flow(self, flow_id: int) -> TrafficFlow | None:
        needle = int(flow_id)
        for flow in self._history():
            if flow.id == needle:
                return flow
        return None

    def _flow_missing(self) -> dict[str, Any]:
        if self._server is None and not self._retained_flows:
            return {"ok": False, "error": "proxy not running"}
        return {"ok": False, "error": "flow not found"}

    def export_proxy_har(
        self,
        *,
        since: int = 0,
        limit: int = 500,
        path: str = "",
    ) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_exports_dir
        from argusai.core.security.proxy_har import write_har_file

        history = self._history()
        if self._server is None and not history:
            return {"ok": False, "error": "proxy not running"}
        flows = history[max(0, int(since or 0)):][: max(1, min(int(limit or 500), 5000))]
        if not flows:
            return {"ok": False, "error": "no flows to export"}

        exports = proxy_exports_dir(self._ca_dir)
        exports.mkdir(parents=True, exist_ok=True)
        if path.strip():
            out = Path(path.strip())
            if not out.is_absolute():
                out = exports / out.name
        else:
            out = exports / f"proxy_{int(time.time())}.har"

        result = write_har_file(flows, out)
        result["exports_dir"] = str(exports)
        return result

    def save_proxy_flow_body(
        self,
        flow_id: int,
        *,
        part: str = "response",
    ) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_captures_dir, save_flow_body

        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        chosen: str = "request" if str(part).lower() == "request" else "response"
        result = save_flow_body(flow, self._ca_dir, part=chosen)  # type: ignore[arg-type]
        result["captures_dir"] = str(proxy_captures_dir(self._ca_dir))
        return result

    def get_flow_body_for_save(
        self,
        flow_id: int,
        *,
        part: str = "response",
    ) -> dict[str, Any]:
        """Return raw bytes + suggested filename for the GUI save dialog.

        Bytes never cross the pywebview bridge directly; the caller in
        :class:`argusai.gui.proxy.ProxyMixin` immediately persists them to the
        path the user picks.
        """
        from argusai.core.security.proxy_export import _guess_extension, _safe_filename

        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        chosen = "request" if str(part).lower() == "request" else "response"
        data = flow.request_body if chosen == "request" else flow.response_body
        if not data:
            return {"ok": False, "error": f"empty {chosen} body"}
        content_type = (
            flow.content_type
            if chosen == "response"
            else flow._first_request_content_type()
        )
        ext = _guess_extension(content_type if chosen == "response" else "", flow.filename_hint)
        base = _safe_filename(flow.filename_hint or flow.host or "flow")
        suggested = f"flow{flow.id}_{chosen}_{base}{ext}"
        truncated = bool(
            (chosen == "response" and flow.response_full_size > len(flow.response_body))
            or (chosen == "request" and flow.request_full_size > len(flow.request_body))
        )
        return {
            "ok": True,
            "bytes": data,
            "suggested_filename": suggested,
            "mime": content_type,
            "size": len(data),
            "truncated": truncated,
        }

    def open_proxy_exports_dir(self) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_exports_dir, reveal_dir

        return reveal_dir(proxy_exports_dir(self._ca_dir))

    def import_proxy_har(self, path: str) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_exports_dir
        from argusai.core.security.proxy_import import analyze_har_path, store_import_copy

        text = str(path or "").strip()
        if not text:
            return {"ok": False, "error": "path is required"}
        src = Path(text).expanduser().resolve()
        result = analyze_har_path(src)
        if not result.get("ok"):
            return result
        stored = store_import_copy(src, proxy_exports_dir(self._ca_dir), kind="har")
        result.update(stored)
        return result

    def import_proxy_burp_log(self, path: str) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_exports_dir
        from argusai.core.security.proxy_import import analyze_burp_log_path, store_import_copy

        text = str(path or "").strip()
        if not text:
            return {"ok": False, "error": "path is required"}
        src = Path(text).expanduser().resolve()
        result = analyze_burp_log_path(src)
        if not result.get("ok"):
            return result
        stored = store_import_copy(src, proxy_exports_dir(self._ca_dir), kind="burp")
        result.update(stored)
        return result

    def open_proxy_captures_dir(self) -> dict[str, Any]:
        from argusai.core.security.proxy_export import proxy_captures_dir, reveal_dir

        return reveal_dir(proxy_captures_dir(self._ca_dir))

    def get_traffic(
        self,
        since: int = 0,
        limit: int = 100,
        *,
        resource_kind: str = "",
        host: str = "",
    ) -> list[dict]:
        flows = self._history()[max(0, int(since or 0)):]
        if resource_kind:
            flows = [f for f in flows if f.resource_kind == resource_kind]
        if host:
            needle = host.strip().lower()
            flows = [f for f in flows if needle in (f.host or "").lower()]
        # Newest ``limit`` flows (oldest first); callers want what just happened.
        return [f.to_dict() for f in flows[-max(1, int(limit or 100)):]]

    def get_traffic_after(
        self,
        after_id: int = 0,
        limit: int = 500,
        generation: int | None = None,
        query: str = "",
        decode_seq: int | None = None,
    ) -> dict[str, Any]:
        """Flows newer than ``after_id`` for an incremental reader.

        ``reset`` tells the reader its buffer is stale (history was cleared, or
        it never synced) and ``flows`` is a full snapshot instead of a delta.
        With ``query`` the delta is also filtered (``query_matches``), so a live
        filter costs one pass over new flows only. When decryption progressed
        since ``decode_seq`` the reader gets every decrypted id; older flows
        whose plaintext changed are re-evaluated too and listed in
        ``query_rechecked`` (drop them from the match set, then add
        ``query_matches``).
        """
        flows = self._history()
        reset = generation is None or int(generation) != self._generation
        after = 0 if reset else int(after_id or 0)
        newer = [f for f in flows if f.id > after]
        cap = max(1, min(int(limit or 500), 5000))
        sent = newer[-cap:]
        payload: dict[str, Any] = {
            "ok": True,
            "generation": self._generation,
            "reset": reset,
            "flows": [f.to_dict() for f in sent],
            "first_id": flows[0].id if flows else 0,
            "last_id": flows[-1].id if flows else 0,
            "total": len(flows),
            "cap": self._config.max_history if self._config else _DEFAULT_MAX_HISTORY,
            "running": self.is_running,
            "finding_count": len(self._findings),
            "decode_seq": self.decryptor.seq,
        }
        seq = self.decryptor.seq
        since = None if decode_seq is None else int(decode_seq)
        if since is not None and since > seq:
            since = None  # the service restarted; the reader's cursor is meaningless
        recheck: list[TrafficFlow] = []
        if not reset and since is not None and since != seq:
            recheck = [f for f in flows if f.decode_seq > since and f.id <= after]
        if query.strip():
            delta = self.query_traffic(query, flows=recheck + sent)
            payload["query_matches"] = delta.get("ids", [])
            payload["query_rechecked"] = [f.id for f in recheck]
            if not delta.get("ok"):
                payload["query_error"] = delta.get("error", "")
        if since is None or since != seq:
            payload["decrypted_ids"] = [f.id for f in flows if f.decrypted]
        return payload

    def _findings_by_flow(self) -> dict[int, list[dict[str, Any]]]:
        by_flow: dict[int, list[dict[str, Any]]] = {}
        for finding in self._findings:
            flow_id = (finding.metadata or {}).get("flow_id")
            if flow_id is not None:
                by_flow.setdefault(int(flow_id), []).append({"id": finding.id, "title": finding.title})
        return by_flow

    def query_traffic(
        self,
        query: str,
        *,
        flows: list[TrafficFlow] | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Evaluate a filter expression (``proxy_query`` syntax) over history."""
        from argusai.core.security.proxy_query import QueryError, compile_query, query_error_payload, run_query

        try:
            compiled = compile_query(query)
        except QueryError as exc:
            return query_error_payload(exc, query)
        pool = self._history() if flows is None else flows
        result = run_query(compiled, pool, self._findings_by_flow()).to_dict()
        if limit and result["ids"]:
            wanted = set(result["ids"][-int(limit):])
            result["flows"] = [f.to_dict() for f in pool if f.id in wanted]
        result["query"] = query
        return result

    def set_decrypt_rules(self, rules: list[dict[str, Any]]) -> None:
        """Install rules and re-run decoding over the whole history."""
        self.decryptor.set_rules(rules, self._history())

    def set_rewrite_rules(self, rules: list[dict[str, Any]]) -> dict[str, Any]:
        """Install active rewrite rules on the live server (and future ones)."""
        self._rewrite_rules = [dict(r) for r in (rules or [])]
        server = self._server
        if server is not None:
            server.set_rewrite_rules(self._rewrite_rules)
            return {"ok": True, "active": server._rewrite.active, "errors": server.rewrite_errors}
        from argusai.core.security.proxy_rewrite import ProxyRewriteEngine

        probe = ProxyRewriteEngine()
        probe.set_rules(self._rewrite_rules)
        return {"ok": True, "active": probe.active, "errors": probe.errors}

    def test_rewrite_rule(self, rule: dict[str, Any], flow_id: int) -> dict[str, Any]:
        """Dry-run one rewrite rule against a captured flow, without sending."""
        from argusai.core.security.proxy_rewrite import (
            RewriteError,
            RewriteRule,
            apply_request_actions,
            apply_response_actions,
            validate_rule,
        )

        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        try:
            parsed = RewriteRule.from_dict(rule or {})
            validate_rule(parsed)
        except RewriteError as exc:
            return {"ok": False, "error": str(exc)}
        from argusai.core.security.proxy_query import FlowView, _Budget, compile_query

        budget = _Budget(deadline=time.monotonic() + 1.0)
        matched = bool(compile_query(parsed.match).predicate(FlowView(flow), budget))
        out: dict[str, Any] = {"ok": True, "matched": matched, "apply_to": parsed.apply_to}
        if "request" in parsed.targets:
            rr = apply_request_actions(
                parsed.actions,
                method=flow.method,
                url=flow.url,
                header_items=list(flow.raw_request_header_items) or [(k, v) for k, v in flow.request_headers.items()],
                body=flow.request_body,
                rule_name=parsed.name,
            )
            out["request"] = {
                "method": rr.method,
                "url": rr.url,
                "headers": rr.header_items,
                "body_preview": rr.body[:512].decode("utf-8", "replace"),
                "short_circuit": rr.short_circuit is not None,
                "trace": rr.trace,
            }
        if "response" in parsed.targets:
            rr2 = apply_response_actions(
                parsed.actions,
                status=flow.response_status or 0,
                reason="",
                header_items=list(flow.raw_response_header_items) or [(k, v) for k, v in flow.response_headers.items()],
                body=flow.response_body,
                rule_name=parsed.name,
            )
            out["response"] = {
                "status": rr2.status,
                "headers": rr2.header_items,
                "body_preview": rr2.body[:512].decode("utf-8", "replace"),
                "trace": rr2.trace,
            }
        return out

    def get_rewrite_rules(self) -> dict[str, Any]:
        from argusai.core.security.proxy_rewrite import ProxyRewriteEngine

        probe = ProxyRewriteEngine()
        probe.set_rules(self._rewrite_rules)
        return {"ok": True, "rules": list(self._rewrite_rules), "errors": probe.errors, "active": probe.active}

    # ---- interactive breakpoints ------------------------------------

    def set_breakpoint_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        self._breakpoint_config = dict(config or {})
        server = self._server
        if server is not None:
            applied = server.set_breakpoint_config(self._breakpoint_config)
        else:
            from argusai.core.security.proxy_breakpoint import BreakpointEngine

            applied = BreakpointEngine()
            applied.set_config(self._breakpoint_config)
            applied = applied.get_config()
        return {"ok": True, "config": applied, "running": server is not None}

    def get_breakpoint_config(self) -> dict[str, Any]:
        server = self._server
        config = server.get_breakpoint_config() if server is not None else dict(self._breakpoint_config)
        return {"ok": True, "config": config, "running": server is not None}

    def breakpoint_list_paused(self) -> dict[str, Any]:
        server = self._server
        paused = server.breakpoint_list_paused() if server is not None else []
        return {"ok": True, "paused": paused, "running": server is not None}

    def breakpoint_resolve(self, pause_id: str, action: str = "forward", edits: dict[str, Any] | None = None) -> dict[str, Any]:
        server = self._server
        if server is None:
            return {"ok": False, "error": "proxy is not running"}
        return server.breakpoint_resolve(str(pause_id or ""), str(action or "forward"), edits or {})

    def get_flow_decoded(self, flow_id: int) -> dict[str, Any]:
        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        decoded = flow.decoded
        if decoded is None and not flow.body_evicted:
            # Not processed yet (still queued, or captured before the service ran).
            decoded = self.decryptor.decode_flow(flow)
        return {
            "ok": True,
            "flow_id": flow.id,
            "decoded": decoded,
            "decode_seq": flow.decode_seq,
            "evicted": flow.body_evicted,
        }

    def test_decrypt_rule(self, rule: dict[str, Any], flow_id: int) -> dict[str, Any]:
        flow = self._find_flow(flow_id)
        if flow is None:
            return self._flow_missing()
        if flow.body_evicted:
            return {"ok": False, "error": "这条流量的正文已被释放，换一条较新的流量测试"}
        return self.decryptor.test_rule(rule, flow)

    def clear_traffic(self) -> dict[str, Any]:
        server = self._server
        if server is not None:
            server.clear_flows()
        self._retained_flows = []
        self._findings = []
        self._finding_keys = set()
        analyzer = self._analyzer
        if analyzer is not None:
            analyzer.reset_dedup()
        self._generation += 1
        return {"ok": True, "generation": self._generation}

    def get_findings(self) -> list[dict]:
        return [f.to_dict() for f in self._findings]

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "config": self._config.to_dict() if self._config else None,
            "flow_count": len(self._history()),
            "flow_cap": self._config.max_history if self._config else _DEFAULT_MAX_HISTORY,
            "finding_count": len(self._findings),
            "generation": self._generation,
            "ca": self.get_ca_info(),
            "system_proxy_active": self._system_proxy_snapshot is not None,
        }

    def _run_loop(self) -> None:
        """Body of the proxy thread — own asyncio loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            try:
                loop.run_until_complete(self._server.start())
            except Exception as exc:
                self._startup_error = f"proxy bind failed: {exc}"
                log.error("proxy startup failed: %s", exc)
                self.bus.emit("proxy.error", error=str(exc))
                return
            finally:
                evt = self._ready_event
                if evt is not None:
                    evt.set()
            loop.run_forever()
        except Exception as e:
            log.error("proxy thread error: %s", e)
            self.bus.emit("proxy.error", error=str(e))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception as exc:
                log.debug("proxy loop drain error: %s", exc)
            try:
                loop.close()
            except Exception:
                pass
