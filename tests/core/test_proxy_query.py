"""Traffic filter expressions shared by the GUI filter box and the agent tool."""

from __future__ import annotations

import time

import pytest

from argusai.core.security import proxy_query
from argusai.core.security.proxy_flow import TrafficFlow
from argusai.core.security.proxy_query import QueryError, compile_query, run_query


def _flow(
    fid: int,
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    kind: str = "api",
    req_body: bytes = b"",
    resp_body: bytes = b"",
    req_headers: list[tuple[str, str]] | None = None,
    resp_headers: list[tuple[str, str]] | None = None,
    duration: float = 50.0,
    error: str | None = None,
    decoded: dict | None = None,
) -> TrafficFlow:
    host = url.split("/")[2].split(":")[0]
    return TrafficFlow(
        id=fid,
        timestamp=time.time(),
        method=method,
        url=url,
        host=host,
        response_status=status,
        resource_kind=kind,
        request_body=req_body,
        response_body=resp_body,
        raw_request_header_items=req_headers or [],
        raw_response_header_items=resp_headers or [("Content-Type", "application/json")],
        response_full_size=len(resp_body),
        duration_ms=duration,
        is_https=url.startswith("https"),
        error=error,
        decoded=decoded,
    )


FLOWS = [
    _flow(1, "https://api.example.com/v1/login", method="POST", req_body=b"user=demo&password=s3cret",
          resp_body=b'{"code":0,"token":"abc"}', req_headers=[("Authorization", "Bearer xyz")]),
    _flow(2, "https://api.example.com/v1/profile?id=42", resp_body=b'{"code":-1,"msg":"denied"}', status=403),
    _flow(3, "https://cdn.example.com/app.js", kind="script", resp_body=b"x" * 300_000, duration=2500),
    _flow(4, "http://www.google.com/gen_204", status=204, kind="other"),
    _flow(5, "https://pay.example.cn/api/order", method="POST", status=500, error=None,
          resp_body=b"U2FsdGVkX19", decoded={"response": {"texts": ['{"orderId":"9007199254740993","amount":100}']}}),
    _flow(6, "https://api.example.com/v2/ping", status=0, error="connect timeout"),
]
FINDINGS = {2: [{"id": "proxy.cors-wildcard", "title": "CORS wildcard allows all origins"}]}


def ids(query: str) -> list[int]:
    return run_query(compile_query(query), FLOWS, FINDINGS).ids


def test_bare_words_and_regex_match_url() -> None:
    assert ids("login") == [1]
    assert ids("/\\/v\\d\\//") == [1, 2, 6]
    assert ids("https://api.example.com/v1/profile") == [2]
    assert ids("") == [1, 2, 3, 4, 5, 6]


def test_paths_starting_with_slash_are_values_unless_slash_terminated() -> None:
    assert ids("path:/login") == [1]
    assert ids("path:/api/order") == [5]
    assert ids("path:/v\\d/ method:GET") == [2, 6]


def test_text_fields_lists_and_globs() -> None:
    assert ids("host:cdn") == [3]
    assert ids("host:*.example.cn") == [5]
    assert ids("method:POST,PUT") == [1, 5]
    assert ids("type:js") == [3]
    assert ids("path:/v1/") == [1, 2]
    assert ids("host:/^API\\./i") == [1, 2, 6]


def test_numeric_comparisons_and_units() -> None:
    assert ids("status:4xx") == [2]
    assert ids("status:>=500") == [5]
    assert ids("status:200-299") == [1, 3, 4]
    assert ids("status:failed") == [6]
    assert ids("size:>100k") == [3]
    assert ids("duration:>2s") == [3]
    assert ids("id:2,4") == [2, 4]


def test_boolean_operators_negation_and_grouping() -> None:
    assert ids("host:api.example.com -status:403") == [1, 6]
    assert ids("(path:/login OR path:/order) method:POST") == [1, 5]
    assert ids("status:403 | status:204") == [2, 4]
    assert ids("NOT host:example") == [4]
    assert ids("!(host:example.com)") == [4, 5]


def test_headers_bodies_findings_and_flags() -> None:
    assert ids("req.header:authorization") == [1]
    assert ids("header:authorization=/^Bearer /") == [1]
    assert ids("resp.header:content-type=json") == [1, 2, 3, 4, 5, 6]
    assert ids('resp.body:/"code":\\s*-1/') == [2]
    assert ids("req.body:password") == [1]
    assert ids("has:finding") == [2]
    assert ids("finding:cors") == [2]
    assert ids("has:auth") == [1]
    assert ids("tls:no") == [4]


def test_decrypted_text_is_searchable() -> None:
    assert ids("dec:orderId") == [5]
    assert ids("resp.body:9007199254740993") == [5]  # raw body OR decrypted text
    assert ids("has:decrypted") == [5]
    assert ids("dec:/\\p{Han}/") == []  # str-only regex feature still compiles


@pytest.mark.parametrize(
    "query, fragment",
    [
        ("hots:api", "未知字段"),
        ("status:abc", "需要数字"),
        ("host:/(unclosed/", "正则表达式无效"),
        ("(host:a", "括号没有闭合"),
        ("host:a)", "多余的 )"),
        ("has:everything", "has:"),
        ("status:/4../", "不支持正则"),
    ],
)
def test_errors_carry_messages(query: str, fragment: str) -> None:
    with pytest.raises(QueryError) as exc:
        compile_query(query)
    assert fragment in str(exc.value)


def test_catastrophic_regex_times_out_instead_of_hanging(monkeypatch) -> None:
    monkeypatch.setattr(proxy_query, "REGEX_TIMEOUT_SECONDS", 0.05)
    evil = _flow(9, "https://x.test/", resp_body=b"a" * 40 + b"!")
    started = time.monotonic()
    result = run_query(compile_query("resp.body:/(a|aa)+$/"), [evil])
    assert time.monotonic() - started < 2
    assert result.ids == []
    assert result.regex_timeouts == 1
