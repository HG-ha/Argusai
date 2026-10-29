"""Safe, offline-only TShark bridges for packet-capture analysis.

The helpers in this module accept local capture files, build fixed argv lists,
and execute through :class:`CommandRunner`.  They never start a live capture
and never invoke a shell.
"""

from __future__ import annotations

import csv
import io
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from argusai.core.infra.command import BLOCKED_TOKENS, CommandRequest, CommandRunner

_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 1800
_DEFAULT_PACKET_LIMIT = 5000
_MAX_PACKET_LIMIT = 100_000
_MAX_PARSE_CHARS = 2_000_000
_MAX_RESULT_ROWS = 200
_MAX_FIELD_CHARS = 2048

_PACKET_FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "_ws.col.Protocol",
    "ip.src",
    "ipv6.src",
    "tcp.srcport",
    "udp.srcport",
    "ip.dst",
    "ipv6.dst",
    "tcp.dstport",
    "udp.dstport",
)
_HTTP_FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "ip.src",
    "ipv6.src",
    "tcp.srcport",
    "ip.dst",
    "ipv6.dst",
    "tcp.dstport",
    "http.request.method",
    "http.host",
    "http.request.uri",
    "http.request.full_uri",
    "http.response.code",
    "http.content_type",
    "http.user_agent",
)


def _missing_tool(tool: str = "tshark") -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "do_not_retry": True,
        "can_install": False,
        "error_code": "tool_unavailable",
        "tool": tool,
        "error": f"{tool} is not installed or is not available on PATH",
        "install_hint": (
            "Install Wireshark/TShark manually for this platform and ensure "
            "tshark is on PATH. ArgusAI does not download CLI tools."
        ),
    }


def pcap_tool_inventory() -> dict[str, Any]:
    """Report host TShark availability without running or installing it."""
    path = shutil.which("tshark")
    return {
        "ok": True,
        "offline_only": True,
        "live_capture_supported": False,
        "tshark": {
            "installed": bool(path),
            "path": path or "",
            "can_install": False,
        },
    }


def tshark_status() -> dict[str, Any]:
    inventory = pcap_tool_inventory()
    row = dict(inventory["tshark"])
    if not row["installed"]:
        row.update({k: v for k, v in _missing_tool().items() if k != "ok"})
    return {"ok": True, **row, "inventory": inventory}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_capture_path(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
) -> Path:
    text = str(path or "").strip()
    if not text:
        raise ValueError("pcap path is required")
    candidate = Path(text).expanduser().resolve()
    if allowed_root is not None:
        root = Path(allowed_root).expanduser().resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("pcap path escapes allowed root")
    if not candidate.is_file():
        raise FileNotFoundError(f"pcap file not found: {candidate}")
    return candidate


def _safe_display_filter(value: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError("display_filter is required")
    if len(text) > 4096:
        raise ValueError("display_filter is too long")
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError("display_filter must be a single line")
    # CommandRunner intentionally treats shell-style &&/|| as a policy tripwire.
    # TShark accepts the equivalent textual operators.
    text = text.replace("&&", " and ").replace("||", " or ")
    if any(token in text for token in BLOCKED_TOKENS):
        raise ValueError("display_filter contains a blocked token")
    return " ".join(text.split())


def _fields_args(fields: tuple[str, ...]) -> list[str]:
    args = ["-T", "fields", "-E", "header=y", "-E", "separator=/t", "-E", "quote=d"]
    for field in fields:
        args.extend(["-e", field])
    return args


def _base_plan(
    path: str | Path,
    *,
    fields: tuple[str, ...],
    display_filter: str = "",
    filter_required: bool = False,
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    try:
        capture = _resolve_capture_path(path, allowed_root=allowed_root)
        packet_limit = _bounded_int(max_packets, _DEFAULT_PACKET_LIMIT, 1, _MAX_PACKET_LIMIT)
        safe_filter = _safe_display_filter(display_filter, required=filter_required)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_local_input",
            "error": str(exc),
        }

    status = tshark_status()
    if not status.get("installed"):
        return {**_missing_tool(), "input": str(capture)}

    args = ["-n", "-r", str(capture), "-c", str(packet_limit)]
    if safe_filter:
        args.extend(["-Y", safe_filter])
    args.extend(_fields_args(fields))
    return {
        "ok": True,
        "tool": "tshark",
        "command": "tshark",
        "args": args,
        "input": str(capture),
        "display_filter": safe_filter,
        "max_packets": packet_limit,
        "risk": "low",
        "offline": True,
        "live_capture": False,
    }


def pcap_summary_plan(
    path: str | Path,
    *,
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan bounded packet metadata extraction for an offline capture."""
    plan = _base_plan(
        path,
        fields=_PACKET_FIELDS,
        max_packets=max_packets,
        allowed_root=allowed_root,
    )
    if plan.get("ok"):
        plan["operation"] = "summary"
    return plan


def pcap_filter_plan(
    path: str | Path,
    display_filter: str,
    *,
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan a read-only TShark display-filter query."""
    plan = _base_plan(
        path,
        fields=_PACKET_FIELDS,
        display_filter=display_filter,
        filter_required=True,
        max_packets=max_packets,
        allowed_root=allowed_root,
    )
    if plan.get("ok"):
        plan["operation"] = "filter"
    return plan


def pcap_http_extract_plan(
    path: str | Path,
    *,
    display_filter: str = "",
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan extraction of clear-text HTTP request/response metadata."""
    user_filter = str(display_filter or "").strip()
    http_filter = (
        f"(http.request or http.response) and ({user_filter})"
        if user_filter
        else "http.request or http.response"
    )
    plan = _base_plan(
        path,
        fields=_HTTP_FIELDS,
        display_filter=http_filter,
        max_packets=max_packets,
        allowed_root=allowed_root,
    )
    if plan.get("ok"):
        plan["operation"] = "http_extract"
        plan["notes"] = [
            "Extracts decoded clear-text HTTP metadata only.",
            "TLS/QUIC payloads remain opaque unless TShark is separately configured with valid keys.",
        ]
    return plan


def _trim(value: Any) -> str:
    return str(value or "")[:_MAX_FIELD_CHARS]


def _read_tsv(text: str) -> tuple[list[dict[str, str]], bool]:
    raw = str(text or "")
    input_truncated = len(raw) > _MAX_PARSE_CHARS
    raw = raw[:_MAX_PARSE_CHARS]
    reader = csv.reader(io.StringIO(raw), delimiter="\t", quotechar='"')
    try:
        header = next(reader)
    except StopIteration:
        return [], input_truncated
    names = [str(name or "").strip() for name in header]
    if not names:
        return [], input_truncated
    rows: list[dict[str, str]] = []
    for values in reader:
        if not any(values):
            continue
        padded = [*values, *([""] * max(0, len(names) - len(values)))]
        rows.append({name: _trim(padded[index]) for index, name in enumerate(names) if name})
    return rows, input_truncated


def _first(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value.split(",", 1)[0]
    return ""


def _packet_from_row(row: dict[str, str]) -> dict[str, Any]:
    length_text = _first(row, "frame.len")
    try:
        length: int | None = int(length_text)
    except ValueError:
        length = None
    return {
        "number": _first(row, "frame.number"),
        "time_epoch": _first(row, "frame.time_epoch"),
        "length": length,
        "protocol": _first(row, "_ws.col.Protocol"),
        "source": _first(row, "ip.src", "ipv6.src"),
        "source_port": _first(row, "tcp.srcport", "udp.srcport"),
        "destination": _first(row, "ip.dst", "ipv6.dst"),
        "destination_port": _first(row, "tcp.dstport", "udp.dstport"),
    }


def parse_tshark_packets(text: str, *, max_rows: int = _MAX_RESULT_ROWS) -> dict[str, Any]:
    """Parse the fixed packet-field TSV emitted by this module."""
    rows, input_truncated = _read_tsv(text)
    packets = [_packet_from_row(row) for row in rows]
    cap = _bounded_int(max_rows, _MAX_RESULT_ROWS, 1, 1000)
    protocols = Counter(packet["protocol"] for packet in packets if packet["protocol"])
    endpoints = Counter()
    total_bytes = 0
    times: list[float] = []
    for packet in packets:
        if packet["source"]:
            endpoints[packet["source"]] += 1
        if packet["destination"]:
            endpoints[packet["destination"]] += 1
        if isinstance(packet["length"], int):
            total_bytes += packet["length"]
        try:
            times.append(float(packet["time_epoch"]))
        except (TypeError, ValueError):
            pass
    return {
        "ok": True,
        "packet_count": len(packets),
        "packets": packets[:cap],
        "packets_truncated": len(packets) > cap or input_truncated,
        "protocols": [
            {"name": name, "packets": count}
            for name, count in protocols.most_common(50)
        ],
        "top_endpoints": [
            {"address": address, "packets": count}
            for address, count in endpoints.most_common(50)
        ],
        "total_bytes": total_bytes,
        "first_time_epoch": min(times) if times else None,
        "last_time_epoch": max(times) if times else None,
        "input_truncated": input_truncated,
    }


def parse_tshark_summary(text: str, *, max_rows: int = _MAX_RESULT_ROWS) -> dict[str, Any]:
    """Alias with a summary-oriented name for callers and tests."""
    return parse_tshark_packets(text, max_rows=max_rows)


def parse_tshark_http(text: str, *, max_rows: int = _MAX_RESULT_ROWS) -> dict[str, Any]:
    """Parse bounded clear-text HTTP metadata emitted by TShark."""
    rows, input_truncated = _read_tsv(text)
    cap = _bounded_int(max_rows, _MAX_RESULT_ROWS, 1, 1000)
    transactions: list[dict[str, Any]] = []
    methods: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    hosts: Counter[str] = Counter()
    for row in rows:
        method = _first(row, "http.request.method")
        host = _first(row, "http.host")
        uri = _first(row, "http.request.uri")
        full_uri = _first(row, "http.request.full_uri")
        status = _first(row, "http.response.code")
        if method:
            methods[method] += 1
        if status:
            statuses[status] += 1
        if host:
            hosts[host] += 1
        transactions.append({
            "frame": _first(row, "frame.number"),
            "time_epoch": _first(row, "frame.time_epoch"),
            "source": _first(row, "ip.src", "ipv6.src"),
            "source_port": _first(row, "tcp.srcport"),
            "destination": _first(row, "ip.dst", "ipv6.dst"),
            "destination_port": _first(row, "tcp.dstport"),
            "kind": "request" if method else ("response" if status else "http"),
            "method": method,
            "host": host,
            "uri": uri,
            "url": full_uri or (f"http://{host}{uri}" if host and uri else ""),
            "status": status,
            "content_type": _first(row, "http.content_type"),
            "user_agent": _first(row, "http.user_agent"),
        })
    request_count = sum(1 for row in transactions if row["kind"] == "request")
    response_count = sum(1 for row in transactions if row["kind"] == "response")
    return {
        "ok": True,
        "http_row_count": len(transactions),
        "request_count": request_count,
        "response_count": response_count,
        "transactions": transactions[:cap],
        "transactions_truncated": len(transactions) > cap or input_truncated,
        "methods": dict(methods.most_common(20)),
        "statuses": dict(statuses.most_common(20)),
        "top_hosts": [
            {"host": host, "requests": count}
            for host, count in hosts.most_common(50)
        ],
        "input_truncated": input_truncated,
        "encrypted_payloads_included": False,
    }


async def _run_plan(plan: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    if not plan.get("ok"):
        return plan
    timeout = _bounded_int(timeout_seconds, _DEFAULT_TIMEOUT, 10, _MAX_TIMEOUT)
    result = await CommandRunner().run(
        CommandRequest(
            str(plan["command"]),
            list(plan["args"]),
            "local.offline",
            [],
            str(plan.get("risk") or "low"),
            float(timeout),
        )
    )
    result["input"] = plan["input"]
    result["operation"] = plan.get("operation")
    result["offline"] = True
    if not result.get("ok") and "tool not found" in str(result.get("error") or "").lower():
        return {**result, **_missing_tool()}
    return result


async def pcap_summary(
    path: str | Path,
    *,
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = pcap_summary_plan(path, max_packets=max_packets, allowed_root=allowed_root)
    result = await _run_plan(plan, timeout_seconds=timeout_seconds)
    if plan.get("ok"):
        result["parsed"] = parse_tshark_summary(str(result.get("stdout_excerpt") or ""))
    return result


async def pcap_filter(
    path: str | Path,
    display_filter: str,
    *,
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = pcap_filter_plan(
        path,
        display_filter,
        max_packets=max_packets,
        allowed_root=allowed_root,
    )
    result = await _run_plan(plan, timeout_seconds=timeout_seconds)
    if plan.get("ok"):
        result["parsed"] = parse_tshark_packets(str(result.get("stdout_excerpt") or ""))
    return result


async def pcap_http_extract(
    path: str | Path,
    *,
    display_filter: str = "",
    max_packets: int = _DEFAULT_PACKET_LIMIT,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = pcap_http_extract_plan(
        path,
        display_filter=display_filter,
        max_packets=max_packets,
        allowed_root=allowed_root,
    )
    result = await _run_plan(plan, timeout_seconds=timeout_seconds)
    if plan.get("ok"):
        result["parsed"] = parse_tshark_http(str(result.get("stdout_excerpt") or ""))
    return result
