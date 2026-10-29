"""Export captured proxy flows as HAR 1.2."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argusai.core.security.proxy import TrafficFlow


def _iso_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _header_list(headers: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": str(k), "value": str(v)} for k, v in headers.items()]


def _body_payload(data: bytes, mime_type: str = "") -> dict[str, Any]:
    if not data:
        return {"size": 0, "mimeType": mime_type}
    try:
        text = data.decode("utf-8")
        return {"size": len(data), "mimeType": mime_type, "text": text}
    except UnicodeDecodeError:
        return {
            "size": len(data),
            "mimeType": mime_type,
            "encoding": "base64",
            "text": base64.b64encode(data).decode("ascii"),
        }


def flow_to_har_entry(flow: TrafficFlow) -> dict[str, Any]:
    req_mime = flow.request_headers.get("content-type", "")
    resp_mime = flow.content_type or flow.response_headers.get("content-type", "")

    request_header_items = (
        flow.raw_request_header_items
        if getattr(flow, "raw_request_header_items", None)
        else list(flow.request_headers.items())
    )
    response_header_items = (
        flow.raw_response_header_items
        if getattr(flow, "raw_response_header_items", None)
        else list(flow.response_headers.items())
    )

    request: dict[str, Any] = {
        "method": flow.method,
        "url": flow.url,
        "httpVersion": "HTTP/1.1",
        "headers": [{"name": str(k), "value": str(v)} for k, v in request_header_items],
        "queryString": [],
        "cookies": [],
        "headersSize": -1,
        "bodySize": len(flow.request_body),
    }
    if flow.request_body:
        request["postData"] = _body_payload(flow.request_body, req_mime)

    duration_ms = float(getattr(flow, "duration_ms", 0.0) or 0.0)
    # HAR timings: we only measure end-to-end wall time; bucket it into "wait"
    # so a viewer's waterfall still renders something meaningful.
    timings = {"send": 0, "wait": round(duration_ms, 2), "receive": 0}

    return {
        "startedDateTime": _iso_timestamp(flow.timestamp),
        "time": round(duration_ms, 2),
        "request": request,
        "response": {
            "status": flow.response_status or 0,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": str(k), "value": str(v)} for k, v in response_header_items],
            "cookies": [],
            "content": _body_payload(flow.response_body, resp_mime),
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": len(flow.response_body),
        },
        "cache": {},
        "timings": timings,
        "comment": (
            f"resource_kind={flow.resource_kind}; is_https={flow.is_https}; "
            f"encoding={getattr(flow, 'response_encoding', '') or '-'}"
        ),
    }


def flows_to_har(flows: list[TrafficFlow], *, creator: str = "ArgusAI") -> dict[str, Any]:
    entries = [flow_to_har_entry(flow) for flow in flows]
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": creator, "version": "1.0"},
            "entries": entries,
        }
    }


def write_har_file(flows: list[TrafficFlow], path: Path, *, creator: str = "ArgusAI") -> dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = flows_to_har(flows, creator=creator)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "entry_count": len(flows),
        "size_bytes": path.stat().st_size,
    }
