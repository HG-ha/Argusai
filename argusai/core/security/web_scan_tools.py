"""Controlled bridges for external web scanners (nuclei, ffuf).

Tools must be installed on the host PATH; ArgusAI does not bundle them.
Scope checks reuse ``CommandRunner`` policy from ``argusai.core.infra.command``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from argusai.core.infra.command import (
    BLOCKED_TOKENS,
    CommandRequest,
    CommandRunner,
    in_scope,
    normalize_scope,
)

log = logging.getLogger("argusai.web_scan")

_DEFAULT_NUCLEI_TIMEOUT = 180
_DEFAULT_FFUF_TIMEOUT = 120
_MAX_RATE_LIMIT = 150
_MAX_FFUF_WORDS = 5000

_NUCLEI_BLOCKED_OPTIONS = frozenset({
    "-update", "-ut", "-update-templates", "-ud",
    "-headless", "-hps", "-sign", "-auth", "-profile",
})
_FFUF_BLOCKED_OPTIONS = frozenset({
    "-recursion", "-recursion-depth", "-recursion-strategy",
    "-request", "-request-proto", "-replay-proxy",
})


def _bundled_wordlist(name: str = "common_paths.txt") -> Path:
    return Path(__file__).resolve().parent / "wordlists" / name


def _split_extra_args(extra_args: list[str] | str | None) -> list[str]:
    if extra_args is None:
        return []
    if isinstance(extra_args, str):
        return [part for part in extra_args.split() if part]
    return [str(part) for part in extra_args if str(part).strip()]


def _option_name(arg: str) -> str:
    return arg.split("=", 1)[0].strip().lower()


def _validate_extra_args(args: list[str], *, blocked: frozenset[str]) -> dict[str, Any]:
    blocked_found: list[str] = []
    for arg in args:
        name = _option_name(arg)
        if name in blocked:
            blocked_found.append(name)
        if any(token in arg for token in BLOCKED_TOKENS):
            blocked_found.append("shell_metacharacter")
    return {"ok": not blocked_found, "blocked": blocked_found}


def web_scan_tool_inventory() -> dict[str, Any]:
    nuclei = shutil.which("nuclei")
    ffuf = shutil.which("ffuf")
    wordlist = _bundled_wordlist()
    return {
        "ok": True,
        "nuclei": {"installed": bool(nuclei), "path": nuclei or ""},
        "ffuf": {"installed": bool(ffuf), "path": ffuf or ""},
        "bundled_wordlists": {
            "common_paths": str(wordlist) if wordlist.is_file() else "",
            "entry_count": len(wordlist.read_text(encoding="utf-8").splitlines()) if wordlist.is_file() else 0,
        },
    }


def nuclei_status() -> dict[str, Any]:
    inv = web_scan_tool_inventory()
    return {"ok": True, **inv["nuclei"], "inventory": inv}


def ffuf_status() -> dict[str, Any]:
    inv = web_scan_tool_inventory()
    return {"ok": True, **inv["ffuf"], "inventory": inv}


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return f"https://{text}"
    return text


def _ensure_fuzz_url(url: str) -> str:
    text = _normalize_url(url)
    if "FUZZ" in text:
        return text
    if text.endswith("/"):
        return f"{text}FUZZ"
    return f"{text}/FUZZ"


def nuclei_plan_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    tags: str = "",
    severity: str = "medium,high,critical",
    rate_limit: int = 50,
    extra_args: list[str] | str | None = None,
) -> dict[str, Any]:
    target = _normalize_url(url)
    scope_list = normalize_scope(scope)
    if not target:
        return {"ok": False, "error": "url is required"}
    if scope_list and not in_scope(target, scope_list):
        return {"ok": False, "error": f"target out of authorized scope: {target}", "url": target}

    status = nuclei_status()
    if not status.get("installed"):
        return {
            "ok": False,
            "error": "nuclei not installed on PATH",
            "install_hint": "Install ProjectDiscovery nuclei and ensure it is on PATH.",
        }

    rate = max(1, min(int(rate_limit or 50), _MAX_RATE_LIMIT))
    args = [
        "-u", target,
        "-jsonl",
        "-silent",
        "-no-interactsh",
        "-rate-limit", str(rate),
        "-severity", str(severity or "medium,high,critical"),
    ]
    if tags.strip():
        args.extend(["-tags", tags.strip()])
    extra = _split_extra_args(extra_args)
    check = _validate_extra_args(extra, blocked=_NUCLEI_BLOCKED_OPTIONS)
    if not check["ok"]:
        return {"ok": False, "error": "blocked nuclei options", "blocked": check["blocked"]}
    args.extend(extra)

    return {
        "ok": True,
        "tool": "nuclei",
        "target": target,
        "scope": scope_list,
        "command": "nuclei",
        "args": args,
        "risk": "medium-high",
        "notes": [
            "Single-target nuclei scan with -no-interactsh and capped rate-limit.",
            "Template update / headless / auth profile switches are blocked.",
        ],
    }


def parse_nuclei_jsonl(text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        findings.append({
            "template_id": row.get("template-id") or row.get("templateID") or "",
            "name": info.get("name") or row.get("matcher-name") or "",
            "severity": str(info.get("severity") or "unknown"),
            "host": row.get("host") or "",
            "matched_at": row.get("matched-at") or row.get("matched") or "",
            "type": row.get("type") or "",
        })
    severities = sorted({f.get("severity") for f in findings if f.get("severity")})
    return {
        "finding_count": len(findings),
        "findings": findings[:100],
        "severities": severities,
        "has_findings": bool(findings),
    }


async def nuclei_run_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    tags: str = "",
    severity: str = "medium,high,critical",
    rate_limit: int = 50,
    extra_args: list[str] | str | None = None,
    timeout_seconds: int = _DEFAULT_NUCLEI_TIMEOUT,
) -> dict[str, Any]:
    plan = nuclei_plan_scan(
        url,
        scope=scope,
        tags=tags,
        severity=severity,
        rate_limit=rate_limit,
        extra_args=extra_args,
    )
    if not plan.get("ok"):
        return plan

    timeout = max(30, min(int(timeout_seconds or _DEFAULT_NUCLEI_TIMEOUT), 900))
    result = await CommandRunner().run(
        CommandRequest(
            plan["command"],
            plan["args"],
            plan["target"],
            plan.get("scope") or [],
            plan.get("risk", "medium-high"),
            float(timeout),
        )
    )
    parsed = parse_nuclei_jsonl(str(result.get("stdout_excerpt") or ""))
    result["parsed"] = parsed
    result["summary"] = (
        f"nuclei · {parsed['finding_count']} matches · {plan['target']}"
        if parsed.get("has_findings")
        else f"nuclei · no matches · {plan['target']}"
    )
    result["next_step"] = "Review parsed.findings and confirm with web_request_replay or manual verification."
    return result


def ffuf_plan_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    wordlist: str = "",
    match_codes: str = "200,204,301,302,307,401,403,405",
    rate: int = 50,
    extra_args: list[str] | str | None = None,
) -> dict[str, Any]:
    target = _ensure_fuzz_url(url)
    scope_list = normalize_scope(scope)
    if not target or "FUZZ" not in target:
        return {"ok": False, "error": "url must contain FUZZ or be a base URL for directory discovery"}
    base = target.replace("/FUZZ", "").replace("FUZZ", "")
    if scope_list and not in_scope(base, scope_list):
        return {"ok": False, "error": f"target out of authorized scope: {target}", "url": target}

    status = ffuf_status()
    if not status.get("installed"):
        return {
            "ok": False,
            "error": "ffuf not installed on PATH",
            "install_hint": "Install ffuf and ensure it is on PATH.",
        }

    wl_text = str(wordlist or "").strip()
    wl_path = Path(wl_text).expanduser() if wl_text else _bundled_wordlist()
    if not wl_path.is_file():
        return {"ok": False, "error": f"wordlist not found: {wl_path}"}
    line_count = len([ln for ln in wl_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()])
    if line_count > _MAX_FFUF_WORDS:
        return {
            "ok": False,
            "error": f"wordlist too large ({line_count} > {_MAX_FFUF_WORDS}); use a smaller list",
        }

    rate_val = max(1, min(int(rate or 50), _MAX_RATE_LIMIT))
    args = [
        "-u", target,
        "-w", str(wl_path.resolve()),
        "-json",
        "-noninteractive",
        "-ac",
        "-rate", str(rate_val),
        "-mc", str(match_codes or "200,204,301,302,307,401,403,405"),
    ]
    extra = _split_extra_args(extra_args)
    check = _validate_extra_args(extra, blocked=_FFUF_BLOCKED_OPTIONS)
    if not check["ok"]:
        return {"ok": False, "error": "blocked ffuf options", "blocked": check["blocked"]}
    args.extend(extra)

    return {
        "ok": True,
        "tool": "ffuf",
        "target": target,
        "scope": scope_list,
        "command": "ffuf",
        "args": args,
        "wordlist": str(wl_path.resolve()),
        "wordlist_entries": line_count,
        "risk": "medium-high",
        "notes": [
            "Content discovery with bundled/common wordlist unless wordlist= is provided.",
            "Recursion and raw request replay switches are blocked.",
        ],
    }


def parse_ffuf_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {"result_count": 0, "results": [], "has_results": False}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"result_count": 0, "results": [], "has_results": False, "parse_error": "invalid json"}
    rows = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    results = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        results.append({
            "url": row.get("url") or "",
            "status": row.get("status"),
            "length": row.get("length"),
            "words": row.get("words"),
            "input": (row.get("input") or {}).get("FUZZ") if isinstance(row.get("input"), dict) else "",
        })
    return {
        "result_count": len(rows),
        "results": results,
        "has_results": bool(rows),
    }


async def ffuf_run_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    wordlist: str = "",
    match_codes: str = "200,204,301,302,307,401,403,405",
    rate: int = 50,
    extra_args: list[str] | str | None = None,
    timeout_seconds: int = _DEFAULT_FFUF_TIMEOUT,
) -> dict[str, Any]:
    plan = ffuf_plan_scan(
        url,
        scope=scope,
        wordlist=wordlist,
        match_codes=match_codes,
        rate=rate,
        extra_args=extra_args,
    )
    if not plan.get("ok"):
        return plan

    timeout = max(30, min(int(timeout_seconds or _DEFAULT_FFUF_TIMEOUT), 900))
    result = await CommandRunner().run(
        CommandRequest(
            plan["command"],
            plan["args"],
            plan["target"],
            plan.get("scope") or [],
            plan.get("risk", "medium-high"),
            float(timeout),
        )
    )
    parsed = parse_ffuf_json(str(result.get("stdout_excerpt") or ""))
    result["parsed"] = parsed
    result["summary"] = (
        f"ffuf · {parsed['result_count']} hits · {plan['target']}"
        if parsed.get("has_results")
        else f"ffuf · no hits · {plan['target']}"
    )
    result["next_step"] = "Review parsed.results and manually verify interesting paths."
    return result
