"""Field-style filter expressions for captured proxy traffic.

    host:/^api\\./ method:POST,PUT status:4xx resp.body:/"code":\\s*-1/ -host:google.com
    (path:/login OR path:/oauth) has:finding size:>100k

Whitespace means AND; ``OR`` / ``|`` alternates; ``-`` / ``!`` / ``NOT``
negates; parentheses group. A bare word or ``/regex/`` matches the URL.
Shared by the GUI filter box and the agent's ``proxy_search_traffic`` tool,
so the grammar documented in ``skills/proxy-traffic-query`` lives here.
"""

from __future__ import annotations

import fnmatch
import re as _stdlib_re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import regex

from argusai.core.security.proxy_flow import TrafficFlow

# Per regex search; a catastrophic pattern gives up instead of hanging the UI.
REGEX_TIMEOUT_SECONDS = 0.25
# Whole evaluation budget; the caller gets a partial result past this.
QUERY_DEADLINE_SECONDS = 4.0
MAX_QUERY_CHARS = 4096

_KIND_ALIASES = {
    "js": "script", "javascript": "script", "css": "stylesheet", "style": "stylesheet",
    "img": "image", "pic": "image", "doc": "document", "html": "document",
    "media": "video", "json": "api", "xhr": "api", "fetch": "api",
}

TEXT_FIELDS = {"url", "host", "path", "method", "type", "ct", "error", "finding"}
NUMERIC_FIELDS = {"status", "size", "reqsize", "duration", "id"}
BODY_FIELDS = {"body", "req.body", "resp.body", "dec", "dec.req", "dec.resp"}
HEADER_FIELDS = {"header", "req.header", "resp.header"}
FLAG_FIELDS = {"has", "tls"}
_FIELD_ALIASES = {
    "kind": "type", "mime": "ct", "content-type": "ct", "time": "duration",
    "reqbody": "req.body", "respbody": "resp.body", "resbody": "resp.body",
    "request.body": "req.body", "response.body": "resp.body",
    "decrypted": "dec", "dec.request": "dec.req", "dec.response": "dec.resp",
    "reqheader": "req.header", "respheader": "resp.header",
    "request.header": "req.header", "response.header": "resp.header",
    "scheme": "tls", "https": "tls", "code": "status",
}
ALL_FIELDS = sorted(TEXT_FIELDS | NUMERIC_FIELDS | BODY_FIELDS | HEADER_FIELDS | FLAG_FIELDS)
HAS_VALUES = ("finding", "error", "body", "reqbody", "decrypted", "cookie", "auth")


class QueryError(ValueError):
    def __init__(self, message: str, position: int = 0) -> None:
        super().__init__(message)
        self.position = position


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    kind: str  # term | lparen | rparen | or | and | not
    pos: int
    negate: bool = False
    field: str = ""
    raw: str = ""
    regex: tuple[str, str] | None = None  # (pattern, flags)
    quoted: bool = False


_OPERATOR_WORDS = {"or": "or", "and": "and", "not": "not"}


def _read_regex(src: str, i: int) -> tuple[str, str, int]:
    """``src[i] == '/'``; returns (pattern, flags, next index)."""
    start = i
    i += 1
    out: list[str] = []
    while i < len(src):
        ch = src[i]
        if ch == "\\" and i + 1 < len(src):
            nxt = src[i + 1]
            # ``\/`` is an escaped delimiter; every other escape belongs to the regex.
            out.append("/" if nxt == "/" else ch + nxt)
            i += 2
            continue
        if ch == "/":
            i += 1
            flags_start = i
            while i < len(src) and src[i] in "imsx":
                i += 1
            return "".join(out), src[flags_start:i], i
        out.append(ch)
        i += 1
    raise QueryError("正则表达式缺少结尾的 /", start)


def _regex_literal_at(src: str, i: int) -> tuple[str, str, int] | None:
    """``/re/flags`` only when the closing slash ends the term.

    Paths start with ``/`` too: ``path:/login`` and ``path:/api/v1/user`` are
    plain values, ``path:/v\\d+/`` is a regex.
    """
    if i >= len(src) or src[i] != "/":
        return None
    try:
        pattern, flags, end = _read_regex(src, i)
    except QueryError:
        return None
    if end < len(src) and not src[end].isspace() and src[end] != ")":
        return None
    return pattern, flags, end


def _read_quoted(src: str, i: int) -> tuple[str, int]:
    start = i
    quote = src[i]
    i += 1
    out: list[str] = []
    while i < len(src):
        ch = src[i]
        if ch == "\\" and i + 1 < len(src):
            out.append(src[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise QueryError("引号没有闭合", start)


def _tokenize(src: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(_Token("lparen", i))
            i += 1
            continue
        if ch == ")":
            tokens.append(_Token("rparen", i))
            i += 1
            continue
        if ch == "|":
            tokens.append(_Token("or", i))
            i += 2 if src.startswith("||", i) else 1
            continue
        if ch == "&":
            tokens.append(_Token("and", i))
            i += 2 if src.startswith("&&", i) else 1
            continue
        start = i
        negate = False
        if ch in "-!" and i + 1 < n and not src[i + 1].isspace():
            negate = True
            i += 1
            if src[i] == "(":
                tokens.append(_Token("not", start))
                continue
        # Field name?
        name_end = i
        while name_end < n and (src[name_end].isalnum() or src[name_end] in "._-"):
            name_end += 1
        field_name = ""
        if name_end < n and src[name_end] == ":" and name_end > i:
            candidate = src[i:name_end].lower()
            # A pasted URL (``https://``) or ``host:port`` is a bare value, not a field.
            looks_like_value = src.startswith("//", name_end + 1) or (
                "." in candidate and _FIELD_ALIASES.get(candidate, candidate) not in ALL_FIELDS
            )
            if not looks_like_value:
                field_name = candidate
                i = name_end + 1
        token = _Token("term", start, negate=negate, field=field_name)
        literal = _regex_literal_at(src, i)
        if literal is not None:
            pattern, flags, i = literal
            token.regex = (pattern, flags)
        elif i < n and src[i] in "\"'":
            token.raw, i = _read_quoted(src, i)
            token.quoted = True
        else:
            value_start = i
            while i < n and not src[i].isspace() and src[i] != ")":
                # ``name=/re/`` inside a header term.
                if src[i] == "=" and (literal := _regex_literal_at(src, i + 1)) is not None:
                    token.raw = src[value_start:i + 1]
                    pattern, flags, i = literal
                    token.regex = (pattern, flags)
                    break
                if src[i] in "\"'" and i > value_start and src[i - 1] == "=":
                    head = src[value_start:i]
                    quoted, i = _read_quoted(src, i)
                    token.raw = head + quoted
                    token.quoted = True
                    break
                i += 1
            else:
                token.raw = src[value_start:i]
            if not token.regex and not token.quoted:
                token.raw = src[value_start:i]
        if not field_name and not negate and not token.regex and not token.quoted:
            word = token.raw.lower()
            if word in _OPERATOR_WORDS:
                tokens.append(_Token(_OPERATOR_WORDS[word], start))
                continue
        if not field_name and not token.raw and not token.regex and not token.quoted:
            raise QueryError("缺少筛选值", start)
        tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------


def _compile(pattern: str, flags: str, pos: int, *, as_bytes: bool = False) -> Any:
    bits = regex.V0
    if "i" in flags:
        bits |= regex.IGNORECASE
    if "m" in flags:
        bits |= regex.MULTILINE
    if "s" in flags:
        bits |= regex.DOTALL
    if "x" in flags:
        bits |= regex.VERBOSE
    try:
        return regex.compile(pattern.encode("utf-8") if as_bytes else pattern, bits)
    except regex.error as exc:
        raise QueryError(f"正则表达式无效：{exc}", pos) from exc


@dataclass
class _Budget:
    deadline: float
    regex_timeouts: int = 0

    def search(self, compiled: Any, subject: Any) -> bool:
        try:
            return compiled.search(subject, timeout=REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError:
            self.regex_timeouts += 1
            return False


@dataclass
class _TextMatcher:
    """Substring (case-insensitive), list, glob or regex match on text/bytes."""

    pos: int
    regex_src: tuple[str, str] | None = None
    needles: list[str] = field(default_factory=list)
    glob: bool = False
    _str: Any = None
    _bytes: Any = None
    # str-only regex features (``\p{Han}``) cannot compile as bytes; decode instead.
    _bytes_as_text: bool = False

    @classmethod
    def build(cls, token: _Token, *, allow_list: bool = True) -> "_TextMatcher":
        if token.regex:
            matcher = cls(token.pos, regex_src=token.regex)
            matcher._str = _compile(*token.regex, token.pos)
            return matcher
        raw = token.raw
        if not raw:
            raise QueryError("缺少筛选值", token.pos)
        parts = [raw] if token.quoted or not allow_list else [p for p in raw.split(",") if p]
        return cls(token.pos, needles=[p.lower() for p in parts], glob=not token.quoted and "*" in raw)

    def match(self, text: str, budget: _Budget) -> bool:
        if self._str is not None:
            return budget.search(self._str, text)
        lowered = text.lower()
        if self.glob:
            return any(fnmatch.fnmatchcase(lowered, needle) for needle in self.needles)
        return any(needle in lowered for needle in self.needles)

    def prepare_bytes(self) -> "_TextMatcher":
        """Compile the bytes form up front so body fields fail at parse time."""
        if self.regex_src is not None:
            try:
                self._bytes = _compile(*self.regex_src, self.pos, as_bytes=True)
            except QueryError:
                self._bytes_as_text = True
        else:
            alternatives = b"|".join(re_escape_bytes(n.encode("utf-8")) for n in self.needles)
            self._bytes = regex.compile(alternatives, regex.IGNORECASE)
        return self

    def match_bytes(self, data: bytes, budget: _Budget) -> bool:
        if not data:
            return False
        if self._bytes_as_text:
            return self.match(data.decode("utf-8", errors="replace"), budget)
        return budget.search(self._bytes, data)


def re_escape_bytes(value: bytes) -> bytes:
    return _stdlib_re.escape(value)


_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1 << 20, "mb": 1 << 20, "g": 1 << 30, "gb": 1 << 30}
_TIME_UNITS = {"": 1.0, "ms": 1.0, "s": 1000.0, "m": 60_000.0, "min": 60_000.0}
_NUMBER = regex.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$", regex.IGNORECASE)


def _parse_number(text: str, field_name: str, pos: int) -> float:
    match = _NUMBER.match(text.strip())
    if not match:
        raise QueryError(f"{field_name} 需要数字，收到 {text!r}", pos)
    value, unit = float(match.group(1)), match.group(2).lower()
    units = _TIME_UNITS if field_name == "duration" else _UNITS
    if field_name in ("status", "id"):
        units = {"": 1}
    if unit not in units:
        raise QueryError(f"{field_name} 不支持单位 {unit!r}", pos)
    return value * units[unit]


def _numeric_predicate(field_name: str, raw: str, pos: int) -> Callable[[float], bool]:
    text = raw.strip().lower()
    if not text:
        raise QueryError(f"{field_name} 缺少数值", pos)
    if "," in text:
        preds = [_numeric_predicate(field_name, part, pos) for part in text.split(",") if part]
        return lambda v: any(p(v) for p in preds)
    if field_name == "status" and len(text) == 3 and text[0].isdigit() and text[1:] == "xx":
        low = int(text[0]) * 100
        return lambda v: low <= v < low + 100
    for op in (">=", "<=", ">", "<", "="):
        if text.startswith(op):
            target = _parse_number(text[len(op):], field_name, pos)
            return {
                ">=": lambda v: v >= target,
                "<=": lambda v: v <= target,
                ">": lambda v: v > target,
                "<": lambda v: v < target,
                "=": lambda v: v == target,
            }[op]
    if "-" in text[1:]:
        lo_text, _, hi_text = text.partition("-")
        lo = _parse_number(lo_text, field_name, pos)
        hi = _parse_number(hi_text, field_name, pos)
        return lambda v: lo <= v <= hi
    target = _parse_number(text, field_name, pos)
    return lambda v: v == target


# ---------------------------------------------------------------------------
# Flow view (lazy accessors)
# ---------------------------------------------------------------------------


class FlowView:
    """What a query sees of one flow; bodies and decrypted text are lazy."""

    __slots__ = ("flow", "findings", "decoded", "_path")

    def __init__(self, flow: TrafficFlow, findings: list[dict] | None = None) -> None:
        self.flow = flow
        self.findings = findings or []
        self.decoded = getattr(flow, "decoded", None) or {}
        self._path: str | None = None

    @property
    def path(self) -> str:
        if self._path is None:
            parts = urlsplit(self.flow.url or "")
            self._path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return self._path

    def decrypted_texts(self, part: str) -> list[str]:
        entry = self.decoded.get(part) if isinstance(self.decoded, dict) else None
        if not isinstance(entry, dict):
            return []
        return [str(t) for t in entry.get("texts") or [] if t]

    def header_items(self, part: str) -> list[tuple[str, str]]:
        if part == "request":
            items = self.flow.raw_request_header_items or list(self.flow.request_headers.items())
        else:
            items = self.flow.raw_response_header_items or list(self.flow.response_headers.items())
        return [(str(k), str(v)) for k, v in items]


# ---------------------------------------------------------------------------
# Parser → predicate tree
# ---------------------------------------------------------------------------

Predicate = Callable[[FlowView, _Budget], bool]


def _canonical_field(name: str, pos: int) -> str:
    name = _FIELD_ALIASES.get(name, name)
    if name and name not in ALL_FIELDS:
        raise QueryError(f"未知字段 {name}:，可用字段：{', '.join(ALL_FIELDS)}", pos)
    return name or "url"


def _term_predicate(token: _Token) -> Predicate:
    name = _canonical_field(token.field, token.pos)
    pred = _field_predicate(name, token)
    if token.negate:
        return lambda view, budget: not pred(view, budget)
    return pred


def _field_predicate(name: str, token: _Token) -> Predicate:
    if name in NUMERIC_FIELDS:
        if token.regex:
            raise QueryError(f"{name} 不支持正则，请用比较：{name}:>=400、{name}:200-299", token.pos)
        raw = token.raw.lower()
        if name == "status" and raw in ("failed", "error", "0"):
            return lambda view, _b: bool(view.flow.error) or not view.flow.response_status
        check = _numeric_predicate(name, raw, token.pos)
        getters: dict[str, Callable[[TrafficFlow], float]] = {
            "status": lambda f: float(f.response_status or 0),
            "size": lambda f: float(f.response_size),
            "reqsize": lambda f: float(f.request_full_size or len(f.request_body)),
            "duration": lambda f: float(f.duration_ms or 0),
            "id": lambda f: float(f.id),
        }
        get = getters[name]
        return lambda view, _b: check(get(view.flow))

    if name == "has":
        what = token.raw.lower()
        checks: dict[str, Callable[[FlowView], bool]] = {
            "finding": lambda v: bool(v.findings),
            "findings": lambda v: bool(v.findings),
            "error": lambda v: bool(v.flow.error),
            "body": lambda v: bool(v.flow.response_body),
            "reqbody": lambda v: bool(v.flow.request_body),
            "decrypted": lambda v: bool(v.decrypted_texts("request") or v.decrypted_texts("response")),
            "cookie": lambda v: any(k.lower() == "cookie" for k, _ in v.header_items("request")),
            "auth": lambda v: any(k.lower() == "authorization" for k, _ in v.header_items("request")),
        }
        if what not in checks:
            raise QueryError(f"has: 只支持 {', '.join(HAS_VALUES)}", token.pos)
        check = checks[what]
        return lambda view, _b: check(view)

    if name == "tls":
        truthy = token.raw.lower() in ("yes", "true", "1", "on", "https")
        if token.raw.lower() not in ("yes", "true", "1", "on", "https", "no", "false", "0", "off", "http"):
            raise QueryError("tls: 只接受 yes / no", token.pos)
        return lambda view, _b: bool(view.flow.is_https) == truthy

    if name in HEADER_FIELDS:
        return _header_predicate(name, token)

    if name in BODY_FIELDS:
        matcher = _TextMatcher.build(token, allow_list=False).prepare_bytes()
        parts = {"body": ("request", "response"), "req.body": ("request",), "resp.body": ("response",),
                 "dec": ("request", "response"), "dec.req": ("request",), "dec.resp": ("response",)}[name]
        include_raw = not name.startswith("dec")

        def body_pred(view: FlowView, budget: _Budget) -> bool:
            for part in parts:
                if include_raw:
                    raw = view.flow.request_body if part == "request" else view.flow.response_body
                    if matcher.match_bytes(raw, budget):
                        return True
                for text in view.decrypted_texts(part):
                    if matcher.match(text, budget):
                        return True
            return False

        return body_pred

    if name == "type":
        if token.regex:
            matcher = _TextMatcher.build(token)
            return lambda view, budget: matcher.match(view.flow.resource_kind or "other", budget)
        wanted = {_KIND_ALIASES.get(p.lower(), p.lower()) for p in token.raw.split(",") if p}
        return lambda view, _b: (view.flow.resource_kind or "other") in wanted

    if name == "method":
        if token.regex:
            matcher = _TextMatcher.build(token)
            return lambda view, budget: matcher.match(view.flow.method or "", budget)
        methods = {p.upper() for p in token.raw.split(",") if p}
        return lambda view, _b: (view.flow.method or "").upper() in methods

    if name == "finding":
        if token.raw == "*" and not token.regex:
            return lambda view, _b: bool(view.findings)
        matcher = _TextMatcher.build(token)

        def finding_pred(view: FlowView, budget: _Budget) -> bool:
            for finding in view.findings:
                subject = f"{finding.get('id', '')} {finding.get('title', '')}"
                if matcher.match(subject, budget):
                    return True
            return False

        return finding_pred

    matcher = _TextMatcher.build(token)
    getters_text: dict[str, Callable[[FlowView], str]] = {
        "url": lambda v: v.flow.url or "",
        "host": lambda v: v.flow.host or "",
        "path": lambda v: v.path,
        "ct": lambda v: v.flow.content_type or "",
        "error": lambda v: v.flow.error or "",
    }
    get_text = getters_text[name]
    return lambda view, budget: matcher.match(get_text(view), budget)


def _header_predicate(name: str, token: _Token) -> Predicate:
    parts = {"header": ("request", "response"), "req.header": ("request",), "resp.header": ("response",)}[name]
    raw = token.raw
    header_name = ""
    value_matcher: _TextMatcher | None = None
    line_matcher: _TextMatcher | None = None
    if token.regex and not raw:
        line_matcher = _TextMatcher.build(token, allow_list=False)
    elif "=" in raw:
        header_name, _, value = raw.partition("=")
        header_name = header_name.strip().lower()
        if token.regex:
            value_matcher = _TextMatcher(token.pos, regex_src=token.regex)
            value_matcher._str = _compile(*token.regex, token.pos)
        elif value:
            value_matcher = _TextMatcher(token.pos, needles=[value.lower()])
    else:
        header_name = raw.strip().lower()
    if not header_name and line_matcher is None:
        raise QueryError("header: 需要头名称，如 header:authorization 或 header:cookie=/sid=/", token.pos)

    def header_pred(view: FlowView, budget: _Budget) -> bool:
        for part in parts:
            for key, value in view.header_items(part):
                if line_matcher is not None:
                    if line_matcher.match(f"{key}: {value}", budget):
                        return True
                    continue
                if key.lower() != header_name:
                    continue
                if value_matcher is None or value_matcher.match(value, budget):
                    return True
        return False

    return header_pred


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def parse(self) -> Predicate:
        if not self.tokens:
            return lambda _v, _b: True
        pred = self.parse_or()
        extra = self.peek()
        if extra is not None:
            raise QueryError("多余的 )" if extra.kind == "rparen" else "无法解析的内容", extra.pos)
        return pred

    def parse_or(self) -> Predicate:
        branches = [self.parse_and()]
        while (tok := self.peek()) is not None and tok.kind == "or":
            self.take()
            branches.append(self.parse_and())
        if len(branches) == 1:
            return branches[0]
        return lambda view, budget: any(b(view, budget) for b in branches)

    def parse_and(self) -> Predicate:
        items = [self.parse_unary()]
        while (tok := self.peek()) is not None and tok.kind not in ("or", "rparen"):
            if tok.kind == "and":
                self.take()
            items.append(self.parse_unary())
        if len(items) == 1:
            return items[0]
        return lambda view, budget: all(p(view, budget) for p in items)

    def parse_unary(self) -> Predicate:
        tok = self.peek()
        if tok is None:
            raise QueryError("表达式不完整", self.tokens[-1].pos if self.tokens else 0)
        if tok.kind == "not":
            self.take()
            inner = self.parse_unary()
            return lambda view, budget: not inner(view, budget)
        if tok.kind == "lparen":
            self.take()
            inner = self.parse_or()
            closing = self.peek()
            if closing is None or closing.kind != "rparen":
                raise QueryError("括号没有闭合", tok.pos)
            self.take()
            return inner
        if tok.kind == "term":
            self.take()
            return _term_predicate(tok)
        raise QueryError(f"这里不能出现 {tok.kind.upper()}", tok.pos)


@dataclass
class CompiledQuery:
    source: str
    predicate: Predicate

    @property
    def empty(self) -> bool:
        return not self.source.strip()


def compile_query(source: str) -> CompiledQuery:
    """Parse ``source``; raises :class:`QueryError` with a character position."""
    text = str(source or "")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryError(f"表达式过长（上限 {MAX_QUERY_CHARS} 字符）", MAX_QUERY_CHARS)
    return CompiledQuery(text, _Parser(_tokenize(text)).parse())


@dataclass
class QueryResult:
    ids: list[int]
    scanned: int
    total: int
    timed_out: bool = False
    regex_timeouts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "ids": self.ids,
            "matched": len(self.ids),
            "scanned": self.scanned,
            "total": self.total,
            "timed_out": self.timed_out,
            "regex_timeouts": self.regex_timeouts,
        }


def run_query(
    query: CompiledQuery,
    flows: Iterable[TrafficFlow],
    findings_by_flow: dict[int, list[dict]] | None = None,
    *,
    deadline_seconds: float = QUERY_DEADLINE_SECONDS,
) -> QueryResult:
    flows = list(flows)
    budget = _Budget(deadline=time.monotonic() + deadline_seconds)
    findings_by_flow = findings_by_flow or {}
    ids: list[int] = []
    scanned = 0
    timed_out = False
    for flow in flows:
        if time.monotonic() > budget.deadline:
            timed_out = True
            break
        scanned += 1
        if query.predicate(FlowView(flow, findings_by_flow.get(flow.id)), budget):
            ids.append(flow.id)
    return QueryResult(ids, scanned, len(flows), timed_out, budget.regex_timeouts)


def query_error_payload(exc: QueryError, source: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": str(exc),
        "position": exc.position,
        "query": source,
        "hint": "语法见 skill proxy-traffic-query；示例：host:/^api\\./ method:POST status:4xx resp.body:/token/",
    }
