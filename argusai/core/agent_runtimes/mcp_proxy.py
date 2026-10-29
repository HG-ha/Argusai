"""Minimal stdio MCP server forwarding calls to the loopback tool broker."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"


def _broker_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = str(os.environ.get("ARGUSAI_TOOL_BROKER_URL") or "").rstrip("/")
    token = str(os.environ.get("ARGUSAI_TOOL_BROKER_TOKEN") or "")
    if not base or not token:
        raise RuntimeError("ArgusAI tool broker environment is missing")
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=660) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": str(exc)}
        raise RuntimeError(str(detail.get("error") or exc)) from exc


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "argusai-tools", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        result = _broker_post("/v1/tools/list", {})
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "tool listing failed"))
        return {"tools": list(result.get("tools") or [])}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        result = _broker_post("/v1/tools/call", {
            "name": name,
            "arguments": arguments if isinstance(arguments, dict) else {},
            "call_id": str(params.get("_meta", {}).get("progressToken") or ""),
        })
        payload = result.get("result") if result.get("ok") else result
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload if isinstance(payload, dict) else {"value": payload},
            "isError": not bool(result.get("ok")),
        }
    raise KeyError(method)


def main() -> None:
    for raw_line in sys.stdin.buffer:
        try:
            message = json.loads(raw_line.decode("utf-8"))
            request_id = message.get("id")
            method = str(message.get("method") or "")
            if request_id is None:
                continue
            try:
                result = _handle(
                    method,
                    message.get("params") if isinstance(message.get("params"), dict) else {},
                )
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except KeyError:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            sys.stdout.buffer.write(
                json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            sys.stdout.buffer.flush()
        except Exception:
            continue


if __name__ == "__main__":
    main()
