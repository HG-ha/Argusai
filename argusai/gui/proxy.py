"""Thin pass-through to :class:`argusai.core.security.proxy.ProxyManager`."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def _collect_bypass_hosts(settings: Any) -> list[str]:
    hosts: set[str] = set()
    for ep in getattr(settings, "ai_endpoints", None) or []:
        url = getattr(ep, "base_url", "") or ""
        host = urlparse(url).hostname
        if host:
            hosts.add(host.lower())
    for attr in ("ai_auxiliary_vision", "ai_auxiliary_asr"):
        aux = getattr(settings, attr, None)
        url = getattr(aux, "base_url", "") or ""
        host = urlparse(url).hostname
        if host:
            hosts.add(host.lower())
    return sorted(hosts)


class ProxyMixin:
    """Bridge methods for the local passive MITM proxy."""

    def start_proxy(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.security.proxy import ProxyConfig

        params = params or {}
        settings = self.settings_mgr.get()
        bypass = sorted({
            *(str(h).strip().lower() for h in (params.get("bypass_hosts") or []) if str(h).strip()),
            *_collect_bypass_hosts(settings),
        })
        config = ProxyConfig(
            listen_host=params.get("host", "127.0.0.1"),
            listen_port=params.get("port", 8866),
            scope=params.get("scope") or [],
            deny_hosts=params.get("deny_hosts") or [],
            bypass_hosts=bypass,
            intercept_mode=str(params.get("intercept_mode") or "all"),
            mime_allow=params.get("mime_allow") or [],
            max_history=int(params.get("max_history") or 500),
        )
        return self.proxy.start_proxy(
            config,
            use_system_proxy=bool(params.get("use_system_proxy")),
        )

    def stop_proxy(self) -> dict[str, Any]:
        return self.proxy.stop_proxy()

    def get_proxy_status(self) -> dict[str, Any]:
        return self.proxy.get_status()

    def get_proxy_ca_info(self) -> dict[str, Any]:
        return self.proxy.get_ca_info()

    def install_proxy_ca(self) -> dict[str, Any]:
        return self.proxy.install_proxy_ca()

    def uninstall_proxy_ca(self) -> dict[str, Any]:
        return self.proxy.uninstall_proxy_ca()

    def open_proxy_ca_dir(self) -> dict[str, Any]:
        return self.proxy.open_proxy_ca_dir()

    def get_proxy_traffic(
        self,
        since: int = 0,
        limit: int = 100,
        resource_kind: str = "",
        host: str = "",
    ) -> list[dict[str, Any]]:
        return self.proxy.get_traffic(
            since=since,
            limit=limit,
            resource_kind=resource_kind,
            host=host,
        )

    def get_proxy_traffic_after(
        self,
        after_id: int = 0,
        limit: int = 500,
        generation: int | None = None,
        query: str = "",
        decode_seq: int | None = None,
    ) -> dict[str, Any]:
        return self.proxy.get_traffic_after(
            after_id=int(after_id or 0),
            limit=int(limit or 500),
            generation=None if generation is None else int(generation),
            query=str(query or ""),
            decode_seq=None if decode_seq is None else int(decode_seq),
        )

    def query_proxy_traffic(self, query: str = "") -> dict[str, Any]:
        return self.proxy.query_traffic(str(query or ""))

    def get_proxy_flow_decoded(self, flow_id: int) -> dict[str, Any]:
        return self.proxy.get_flow_decoded(int(flow_id or 0))

    # -- decryption rules (stored in user settings: they carry keys) --------

    def get_proxy_decrypt_rules(self) -> dict[str, Any]:
        rules = list(getattr(self.settings_mgr.get(), "proxy_decrypt_rules", []) or [])
        return {"ok": True, "rules": rules, "errors": self.proxy.decryptor.rule_errors}

    def save_proxy_decrypt_rule(self, rule: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.security.proxy_decrypt import MAX_RULES, DecryptRule, RuleError, validate_rule

        try:
            parsed = DecryptRule.from_dict(rule or {})
            validate_rule(parsed)
        except RuleError as exc:
            return {"ok": False, "error": str(exc)}
        parsed.updated_at = __import__("time").time()
        saved = parsed.to_dict()

        def _upsert(settings: Any) -> None:
            rules = [r for r in (settings.proxy_decrypt_rules or []) if r.get("id") != saved["id"]]
            existing = next((i for i, r in enumerate(settings.proxy_decrypt_rules or []) if r.get("id") == saved["id"]), None)
            if existing is None and len(rules) >= MAX_RULES:
                raise RuleError(f"解密规则已达上限 {MAX_RULES} 条")
            rules.insert(existing if existing is not None else len(rules), saved)
            settings.proxy_decrypt_rules = rules

        try:
            settings = self.settings_mgr.update(_upsert)
        except RuleError as exc:
            return {"ok": False, "error": str(exc)}
        self.proxy.set_decrypt_rules(list(settings.proxy_decrypt_rules))
        return {"ok": True, "rule": saved}

    def delete_proxy_decrypt_rule(self, rule_id: str = "") -> dict[str, Any]:
        target = str(rule_id or "")
        removed = False

        def _remove(settings: Any) -> None:
            nonlocal removed
            before = list(settings.proxy_decrypt_rules or [])
            settings.proxy_decrypt_rules = [r for r in before if r.get("id") != target]
            removed = len(settings.proxy_decrypt_rules) != len(before)

        settings = self.settings_mgr.update(_remove)
        self.proxy.set_decrypt_rules(list(settings.proxy_decrypt_rules))
        return {"ok": True, "removed": removed}

    def test_proxy_decrypt_rule(self, rule: dict[str, Any] | None = None, flow_id: int = 0) -> dict[str, Any]:
        return self.proxy.test_decrypt_rule(rule or {}, int(flow_id or 0))

    # -- active rewrite rules (stored in user settings; they modify live traffic) --

    def get_proxy_rewrite_rules(self) -> dict[str, Any]:
        rules = list(getattr(self.settings_mgr.get(), "proxy_rewrite_rules", []) or [])
        status = self.proxy.get_rewrite_rules()
        return {"ok": True, "rules": rules, "errors": status.get("errors") or {}, "active": status.get("active", False)}

    # ---- interactive breakpoints ------------------------------------

    def get_proxy_breakpoint_config(self) -> dict[str, Any]:
        return self.proxy.get_breakpoint_config()

    def set_proxy_breakpoint_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.proxy.set_breakpoint_config(config or {})

    def list_proxy_breakpoints(self) -> dict[str, Any]:
        return self.proxy.breakpoint_list_paused()

    def resolve_proxy_breakpoint(
        self, pause_id: str = "", action: str = "forward", edits: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.proxy.breakpoint_resolve(str(pause_id or ""), str(action or "forward"), edits or {})

    def save_proxy_rewrite_rule(self, rule: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.security.proxy_rewrite import MAX_RULES, RewriteError, RewriteRule, validate_rule

        try:
            parsed = RewriteRule.from_dict(rule or {})
            validate_rule(parsed)
        except RewriteError as exc:
            return {"ok": False, "error": str(exc)}
        parsed.updated_at = __import__("time").time()
        saved = parsed.to_dict()

        def _upsert(settings: Any) -> None:
            rules = [r for r in (settings.proxy_rewrite_rules or []) if r.get("id") != saved["id"]]
            existing = next((i for i, r in enumerate(settings.proxy_rewrite_rules or []) if r.get("id") == saved["id"]), None)
            if existing is None and len(rules) >= MAX_RULES:
                raise RewriteError(f"改写规则已达上限 {MAX_RULES} 条")
            rules.insert(existing if existing is not None else len(rules), saved)
            settings.proxy_rewrite_rules = rules

        try:
            settings = self.settings_mgr.update(_upsert)
        except RewriteError as exc:
            return {"ok": False, "error": str(exc)}
        self.proxy.set_rewrite_rules(list(settings.proxy_rewrite_rules))
        return {"ok": True, "rule": saved}

    def delete_proxy_rewrite_rule(self, rule_id: str = "") -> dict[str, Any]:
        target = str(rule_id or "")
        removed = False

        def _remove(settings: Any) -> None:
            nonlocal removed
            before = list(settings.proxy_rewrite_rules or [])
            settings.proxy_rewrite_rules = [r for r in before if r.get("id") != target]
            removed = len(settings.proxy_rewrite_rules) != len(before)

        settings = self.settings_mgr.update(_remove)
        self.proxy.set_rewrite_rules(list(settings.proxy_rewrite_rules))
        return {"ok": True, "removed": removed}

    def test_proxy_rewrite_rule(self, rule: dict[str, Any] | None = None, flow_id: int = 0) -> dict[str, Any]:
        return self.proxy.test_rewrite_rule(rule or {}, int(flow_id or 0))

    def clear_proxy_traffic(self) -> dict[str, Any]:
        return self.proxy.clear_traffic()

    def get_proxy_flow_detail(
        self,
        flow_id: int,
        text_limit: int | None = None,
        inline_media: bool = True,
    ) -> dict[str, Any]:
        """``text_limit`` (chars, 0/None = everything captured) keeps big text
        bodies from crossing the bridge in one piece."""
        limit = int(text_limit or 0)
        return self.proxy.get_flow_detail(
            int(flow_id or 0),
            text_limit=limit if limit > 0 else None,
            inline_media=bool(inline_media),
        )

    def get_proxy_flow_har(self, flow_id: int) -> dict[str, Any]:
        return self.proxy.flow_har_json(int(flow_id or 0))

    def export_proxy_har(
        self,
        since: int = 0,
        limit: int = 500,
        path: str = "",
    ) -> dict[str, Any]:
        return self.proxy.export_proxy_har(since=since, limit=limit, path=path)

    def save_proxy_flow_body(self, flow_id: int, part: str = "response") -> dict[str, Any]:
        return self.proxy.save_proxy_flow_body(int(flow_id or 0), part=part)

    def save_proxy_flow_body_as(
        self, flow_id: int, part: str = "response"
    ) -> dict[str, Any]:
        """Pop the OS save dialog and write the chosen body part to disk."""
        from pathlib import Path

        payload = self.proxy.get_flow_body_for_save(int(flow_id or 0), part=part)
        if not payload.get("ok"):
            return payload
        data: bytes = payload.pop("bytes")
        suggested = str(payload.get("suggested_filename") or "flow.bin")
        picker = getattr(self, "_pick_save_file", None)
        if not callable(picker):
            return {"ok": False, "error": "save dialog is unavailable"}
        picked = picker(save_filename=suggested)
        if not picked.get("ok"):
            return picked
        try:
            target = Path(str(picked["path"])).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return {
                "ok": True,
                "path": str(target.resolve()),
                "size_bytes": len(data),
                "truncated": payload.get("truncated", False),
                "mime": payload.get("mime", ""),
            }
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def open_proxy_exports_dir(self) -> dict[str, Any]:
        return self.proxy.open_proxy_exports_dir()

    def import_proxy_har(self, path: str = "") -> dict[str, Any]:
        picked_path = str(path or "").strip()
        if not picked_path:
            picker = getattr(self, "_pick_open_file", None)
            if not callable(picker):
                return {"ok": False, "error": "open dialog is unavailable"}
            picked = picker(file_types=("HAR files (*.har;*.json)", "All files (*.*)"))
            if not picked.get("ok"):
                return picked
            picked_path = str(picked["path"])
        return self.proxy.import_proxy_har(picked_path)

    def import_proxy_burp_log(self, path: str = "") -> dict[str, Any]:
        picked_path = str(path or "").strip()
        if not picked_path:
            picker = getattr(self, "_pick_open_file", None)
            if not callable(picker):
                return {"ok": False, "error": "open dialog is unavailable"}
            picked = picker(file_types=("Text files (*.txt;*.log)", "All files (*.*)"))
            if not picked.get("ok"):
                return picked
            picked_path = str(picked["path"])
        return self.proxy.import_proxy_burp_log(picked_path)

    def open_proxy_captures_dir(self) -> dict[str, Any]:
        return self.proxy.open_proxy_captures_dir()

    def get_proxy_findings(self) -> list[dict[str, Any]]:
        return self.proxy.get_findings()
