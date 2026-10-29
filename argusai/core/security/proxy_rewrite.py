"""Rule-driven active rewriting of proxied HTTP traffic.

This turns the otherwise-passive MITM proxy into an active one on demand: a
rule selects flows with a ``proxy_query`` expression and applies an ordered
list of actions to the request before it goes upstream and/or to the response
before it returns to the client.

Everything here is pure (no sockets): the server feeds in the parsed
method/url/headers/body and writes back whatever these functions return. Rules
are user-authored and saved to user settings; saving one is a high-risk,
always-confirmed Agent operation because it modifies live traffic.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from argusai.core.security.proxy_query import QueryError, compile_query

MAX_RULES = 64
MAX_ACTIONS = 16
# Body rewrites only run on an already-buffered, uncompressed body within this
# size; larger or content-encoded bodies keep streaming and skip body actions.
MAX_REWRITE_BODY_BYTES = 2 * 1024 * 1024
APPLY_TO = ("request", "response", "both")

REQUEST_OPS = ("set_header", "remove_header", "set_query", "remove_query", "set_method", "replace_body", "short_circuit")
RESPONSE_OPS = ("set_header", "remove_header", "set_status", "replace_body")
ALL_OPS = tuple(sorted(set(REQUEST_OPS) | set(RESPONSE_OPS)))

# Header values can carry tokens worth masking when a rule is shown to the AI.
_SECRET_HEADER_HINT = ("authorization", "cookie", "token", "secret", "key", "password")


class RewriteError(ValueError):
    pass


@dataclass
class RewriteRule:
    id: str = dc_field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    enabled: bool = True
    match: str = ""
    apply_to: str = "request"
    actions: list[dict[str, Any]] = dc_field(default_factory=list)
    note: str = ""
    updated_at: float = dc_field(default_factory=time.time)

    @property
    def targets(self) -> tuple[str, ...]:
        return ("request", "response") if self.apply_to == "both" else (self.apply_to,)

    @property
    def has_body_action(self) -> bool:
        return any(str(a.get("op")) == "replace_body" for a in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "match": self.match,
            "apply_to": self.apply_to,
            "actions": [dict(a) for a in self.actions],
            "note": self.note,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RewriteRule":
        if not isinstance(raw, dict):
            raise RewriteError("规则必须是对象")
        apply_to = str(raw.get("apply_to") or "request").lower()
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:12])[:32],
            name=str(raw.get("name") or "").strip()[:80],
            enabled=bool(raw.get("enabled", True)),
            match=str(raw.get("match") or "").strip(),
            apply_to=apply_to if apply_to in APPLY_TO else "request",
            actions=[_normalize_action(a) for a in (raw.get("actions") or []) if isinstance(a, dict)],
            note=str(raw.get("note") or "")[:2000],
            updated_at=float(raw.get("updated_at") or time.time()),
        )


def _normalize_action(raw: dict[str, Any]) -> dict[str, Any]:
    action = {k: v for k, v in raw.items() if v is not None}
    action["op"] = str(action.get("op") or "").strip().lower()
    for key in ("name", "method"):
        if key in action:
            action[key] = str(action[key]).strip()
    return action


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]} ({len(value)} chars)"


def redact_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Rule dict with likely-secret header/body values masked for the AI bridge."""
    out = dict(rule)
    actions = []
    for action in rule.get("actions") or []:
        masked = dict(action)
        name = str(masked.get("name") or "").lower()
        if masked.get("op") == "set_header" and any(h in name for h in _SECRET_HEADER_HINT):
            if isinstance(masked.get("value"), str) and masked["value"]:
                masked["value"] = _mask(masked["value"])
        actions.append(masked)
    out["actions"] = actions
    return out


def validate_rule(rule: RewriteRule) -> None:
    if not rule.name:
        raise RewriteError("请填写规则名称")
    if not rule.match:
        raise RewriteError("请填写匹配表达式，例如 host:api.example.com path:/v2/")
    try:
        compile_query(rule.match)
    except QueryError as exc:
        raise RewriteError(f"匹配表达式有误：{exc}") from exc
    if rule.apply_to not in APPLY_TO:
        raise RewriteError(f"apply_to 需为 {', '.join(APPLY_TO)}")
    if not rule.actions:
        raise RewriteError("规则至少需要一个动作")
    if len(rule.actions) > MAX_ACTIONS:
        raise RewriteError(f"动作过多（上限 {MAX_ACTIONS}）")
    for idx, action in enumerate(rule.actions, 1):
        _validate_action(idx, action, rule)


def _validate_action(idx: int, action: dict[str, Any], rule: RewriteRule) -> None:
    op = action.get("op")
    where = f"第 {idx} 个动作"
    if op not in ALL_OPS:
        raise RewriteError(f"{where}：未知操作 {op!r}，可用 {', '.join(ALL_OPS)}")
    # Actions must be legal for every part the rule applies to.
    for target in rule.targets:
        allowed = REQUEST_OPS if target == "request" else RESPONSE_OPS
        if op not in allowed:
            raise RewriteError(f"{where}：{op} 不能用于{('请求' if target == 'request' else '响应')}（apply_to={rule.apply_to}）")
    if op in ("set_header", "remove_header", "set_query", "remove_query") and not str(action.get("name") or "").strip():
        raise RewriteError(f"{where}：{op} 需要 name")
    if op == "set_header" and "value" not in action:
        raise RewriteError(f"{where}：set_header 需要 value")
    if op == "set_method" and not str(action.get("method") or "").strip():
        raise RewriteError(f"{where}：set_method 需要 method")
    if op == "set_status":
        try:
            code = int(action.get("status"))
        except (TypeError, ValueError) as exc:
            raise RewriteError(f"{where}：set_status 需要整数状态码") from exc
        if not 100 <= code <= 599:
            raise RewriteError(f"{where}：状态码需在 100-599")
    if op == "replace_body":
        if "find" not in action:
            raise RewriteError(f"{where}：replace_body 需要 find")
        if action.get("regex"):
            try:
                re.compile(str(action.get("find") or ""))
            except re.error as exc:
                raise RewriteError(f"{where}：find 正则无效：{exc}") from exc
    if op == "short_circuit":
        try:
            code = int(action.get("status", 200))
        except (TypeError, ValueError) as exc:
            raise RewriteError(f"{where}：short_circuit 的 status 需为整数") from exc
        if not 100 <= code <= 599:
            raise RewriteError(f"{where}：short_circuit 状态码需在 100-599")


@dataclass
class RequestRewrite:
    method: str
    url: str
    header_items: list[tuple[str, str]]
    body: bytes
    short_circuit: dict[str, Any] | None = None  # {status, reason, headers, body}
    trace: list[str] = dc_field(default_factory=list)


@dataclass
class ResponseRewrite:
    status: int
    reason: str
    header_items: list[tuple[str, str]]
    body: bytes
    trace: list[str] = dc_field(default_factory=list)


def _set_header(items: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    low = name.lower()
    out = [(k, v) for k, v in items if k.lower() != low]
    out.append((name, value))
    return out


def _remove_header(items: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    low = name.lower()
    return [(k, v) for k, v in items if k.lower() != low]


def _rewrite_query(url: str, changes: list[tuple[str, str, bool]]) -> str:
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    for name, value, remove in changes:
        params = [(k, v) for k, v in params if k != name]
        if not remove:
            params.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(params)))


def _replace_body_bytes(body: bytes, action: dict[str, Any]) -> bytes:
    if not body or len(body) > MAX_REWRITE_BODY_BYTES:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body  # only text bodies are rewritten
    find = str(action.get("find") or "")
    replace = str(action.get("replace") or "")
    count = int(action.get("count") or 0)
    if action.get("regex"):
        new_text = re.sub(find, replace, text, count=count if count > 0 else 0)
    else:
        new_text = text.replace(find, replace, count if count > 0 else -1)
    return new_text.encode("utf-8")


def _content_length_fixed(items: list[tuple[str, str]], body: bytes) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in items if k.lower() != "content-length"]
    out.append(("Content-Length", str(len(body))))
    return out


def apply_request_actions(
    actions: list[dict[str, Any]],
    *,
    method: str,
    url: str,
    header_items: list[tuple[str, str]],
    body: bytes | None,
    rule_name: str,
) -> RequestRewrite:
    """Apply one rule's request actions. ``body`` is None when not buffered."""
    items = list(header_items)
    out_body = body
    body_touched = False
    trace: list[str] = []
    for action in actions:
        op = str(action.get("op"))
        if op == "set_header":
            items = _set_header(items, str(action["name"]), str(action.get("value") or ""))
            trace.append(f"set_header {action['name']}")
        elif op == "remove_header":
            items = _remove_header(items, str(action["name"]))
            trace.append(f"remove_header {action['name']}")
        elif op == "set_method":
            method = str(action["method"]).upper()
            trace.append(f"set_method {method}")
        elif op == "set_query":
            url = _rewrite_query(url, [(str(action["name"]), str(action.get("value") or ""), False)])
            trace.append(f"set_query {action['name']}")
        elif op == "remove_query":
            url = _rewrite_query(url, [(str(action["name"]), "", True)])
            trace.append(f"remove_query {action['name']}")
        elif op == "replace_body":
            if out_body is not None:
                new_body = _replace_body_bytes(out_body, action)
                if new_body != out_body:
                    out_body = new_body
                    body_touched = True
                    trace.append("replace_body")
        elif op == "short_circuit":
            sc_body = str(action.get("body") or "").encode("utf-8")
            sc_headers = [(str(k), str(v)) for k, v in (action.get("headers") or {}).items()]
            sc_headers = _content_length_fixed(sc_headers, sc_body)
            return RequestRewrite(
                method=method,
                url=url,
                header_items=items,
                body=out_body if out_body is not None else b"",
                short_circuit={
                    "status": int(action.get("status", 200)),
                    "reason": str(action.get("reason") or ""),
                    "headers": sc_headers,
                    "body": sc_body,
                },
                trace=trace + [f"short_circuit {action.get('status', 200)}"],
            )
    if body_touched and out_body is not None:
        items = _content_length_fixed(items, out_body)
    return RequestRewrite(method=method, url=url, header_items=items, body=out_body if out_body is not None else b"", trace=trace)


def apply_response_actions(
    actions: list[dict[str, Any]],
    *,
    status: int,
    reason: str,
    header_items: list[tuple[str, str]],
    body: bytes | None,
    rule_name: str,
) -> ResponseRewrite:
    items = list(header_items)
    out_body = body
    body_touched = False
    trace: list[str] = []
    for action in actions:
        op = str(action.get("op"))
        if op == "set_header":
            items = _set_header(items, str(action["name"]), str(action.get("value") or ""))
            trace.append(f"set_header {action['name']}")
        elif op == "remove_header":
            items = _remove_header(items, str(action["name"]))
            trace.append(f"remove_header {action['name']}")
        elif op == "set_status":
            status = int(action["status"])
            trace.append(f"set_status {status}")
        elif op == "replace_body":
            if out_body is not None:
                new_body = _replace_body_bytes(out_body, action)
                if new_body != out_body:
                    out_body = new_body
                    body_touched = True
                    trace.append("replace_body")
    if body_touched and out_body is not None:
        items = _content_length_fixed(items, out_body)
    return ResponseRewrite(status=status, reason=reason, header_items=items, body=out_body if out_body is not None else b"", trace=trace)


@dataclass
class _CompiledRule:
    rule: RewriteRule
    query: Any


class ProxyRewriteEngine:
    """Holds enabled rewrite rules and applies the matching ones in order."""

    def __init__(self) -> None:
        self._request_rules: list[_CompiledRule] = []
        self._response_rules: list[_CompiledRule] = []
        self._errors: dict[str, str] = {}

    @property
    def active(self) -> bool:
        return bool(self._request_rules or self._response_rules)

    @property
    def has_request_rules(self) -> bool:
        return bool(self._request_rules)

    @property
    def has_response_rules(self) -> bool:
        return bool(self._response_rules)

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def set_rules(self, raw_rules: Any) -> None:
        request_rules: list[_CompiledRule] = []
        response_rules: list[_CompiledRule] = []
        errors: dict[str, str] = {}
        for raw in list(raw_rules or [])[:MAX_RULES]:
            try:
                rule = RewriteRule.from_dict(raw)
                validate_rule(rule)
            except RewriteError as exc:
                errors[str((raw or {}).get("id") or "?")] = str(exc)
                continue
            if not rule.enabled:
                continue
            compiled = _CompiledRule(rule, compile_query(rule.match))
            if "request" in rule.targets:
                request_rules.append(compiled)
            if "response" in rule.targets:
                response_rules.append(compiled)
        self._request_rules = request_rules
        self._response_rules = response_rules
        self._errors = errors

    def _matches(self, compiled: _CompiledRule, flow: Any) -> bool:
        from argusai.core.security.proxy_query import FlowView, _Budget

        budget = _Budget(deadline=time.monotonic() + 1.0)
        try:
            return bool(compiled.query.predicate(FlowView(flow), budget))
        except Exception:
            return False

    def request_rules_for(self, flow: Any) -> list[RewriteRule]:
        return [c.rule for c in self._request_rules if self._matches(c, flow)]

    def response_rules_for(self, flow: Any) -> list[RewriteRule]:
        return [c.rule for c in self._response_rules if self._matches(c, flow)]
