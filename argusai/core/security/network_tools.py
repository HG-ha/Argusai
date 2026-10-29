"""Structured wrappers for common network tools."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from argusai.core.infra.command import CommandRequest, CommandRunner, discover_network_tools, normalize_scope


def tool_inventory() -> dict[str, Any]:
    return discover_network_tools()


async def ping_host(target: str, scope: str | list[str] | None = None, count: int = 4) -> dict[str, Any]:
    inv = discover_network_tools()
    cmd = "ping"
    count = max(1, min(int(count or 4), 10))
    args = ["-n", str(count), target] if inv["platform"].startswith("win") else ["-c", str(count), target]
    return await CommandRunner().run(CommandRequest(cmd, args, target, normalize_scope(scope), "low", 20))


async def dns_lookup(target: str, scope: str | list[str] | None = None, record_type: str = "A") -> dict[str, Any]:
    record_type = (record_type or "A").upper()
    inv = discover_network_tools()
    if inv["tools"].get("dig"):
        return await CommandRunner().run(
            CommandRequest("dig", [target, record_type, "+short"], target, normalize_scope(scope), "low", 20)
        )
    return await CommandRunner().run(
        CommandRequest("nslookup", ["-type=" + record_type, target], target, normalize_scope(scope), "low", 20)
    )


async def trace_route(target: str, scope: str | list[str] | None = None, max_hops: int = 20) -> dict[str, Any]:
    inv = discover_network_tools()
    max_hops = max(1, min(int(max_hops or 20), 30))
    if inv["tools"].get("mtr"):
        return await CommandRunner().run(
            CommandRequest("mtr", ["-r", "-c", "5", "-m", str(max_hops), target], target, normalize_scope(scope), "medium", 45)
        )
    if inv["platform"].startswith("win"):
        return await CommandRunner().run(
            CommandRequest("tracert", ["-h", str(max_hops), target], target, normalize_scope(scope), "medium", 45)
        )
    return await CommandRunner().run(
        CommandRequest("traceroute", ["-m", str(max_hops), target], target, normalize_scope(scope), "medium", 45)
    )


async def http_curl(
    target: str,
    scope: str | list[str] | None = None,
    method: str = "GET",
    data: str = "",
    headers: dict[str, str] | list[str] | str | None = None,
    cookie: str = "",
    follow_redirects: bool = True,
    insecure: bool = False,
    timeout_seconds: int = 30,
    user_agent: str = "",
) -> dict[str, Any]:
    """Scoped curl with optional body / headers / cookies.

    ``data`` is sent as raw request body (``--data-binary``). Header tokens
    that contain shell metacharacters are rejected upstream by ``CommandRunner``.
    """
    method = (method or "GET").upper()
    timeout_seconds = max(1, min(int(timeout_seconds or 30), 120))
    args: list[str] = ["-sS", "-i", "--max-time", str(timeout_seconds), "-X", method]
    if follow_redirects:
        args.append("-L")
    if insecure:
        args.append("-k")
    if user_agent:
        args.extend(["-A", user_agent])
    if cookie:
        args.extend(["-b", cookie])
    header_list: list[str] = []
    if isinstance(headers, dict):
        header_list = [f"{k}: {v}" for k, v in headers.items() if k]
    elif isinstance(headers, list):
        header_list = [str(h) for h in headers if h]
    elif isinstance(headers, str) and headers.strip():
        try:
            import json

            parsed = json.loads(headers)
            if isinstance(parsed, dict):
                header_list = [f"{k}: {v}" for k, v in parsed.items() if k]
            elif isinstance(parsed, list):
                header_list = [str(h) for h in parsed if h]
        except Exception:
            header_list = [line.strip() for line in headers.splitlines() if line.strip() and ":" in line]
    for h in header_list:
        args.extend(["-H", h])
    if data:
        args.extend(["--data-binary", data])
    args.append(target)
    return await CommandRunner().run(
        CommandRequest("curl", args, target, normalize_scope(scope), "medium", float(timeout_seconds))
    )


async def whois_lookup(target: str, scope: str | list[str] | None = None) -> dict[str, Any]:
    return await CommandRunner().run(CommandRequest("whois", [target], target, normalize_scope(scope), "low", 30))


async def nmap_scan(
    target: str,
    scope: str | list[str] | None = None,
    ports: str = "top-100",
    service_detection: bool = False,
    aggressive: bool = False,
) -> dict[str, Any]:
    risk = "high" if aggressive or ports in ("-", "all", "1-65535") else ("medium-high" if service_detection else "medium")
    inv = discover_network_tools()
    if not inv.get("tools", {}).get("nmap"):
        return await _nmap_fallback_scan(
            target,
            scope=scope,
            ports=ports,
            service_detection=service_detection,
            aggressive=aggressive,
        )
    if ports == "top-100":
        port_args = ["--top-ports", "100"]
    elif ports in ("-", "all"):
        port_args = ["-p", "1-65535"]
    else:
        port_args = ["-p", ports]
    args = ["-oX", "-", *port_args]
    if service_detection:
        args.append("-sV")
    if aggressive:
        args.append("-A")
    args.append(target)
    result = await CommandRunner().run(CommandRequest("nmap", args, target, normalize_scope(scope), risk, 120))
    if result.get("ok"):
        result["parsed"] = parse_nmap_xml(result.get("stdout_excerpt", ""))
    return result


async def _nmap_fallback_scan(
    target: str,
    *,
    scope: str | list[str] | None,
    ports: str,
    service_detection: bool,
    aggressive: bool,
) -> dict[str, Any]:
    """Use the built-in TCP scanner when external nmap is unavailable."""
    if ports in ("-", "all", "1-65535"):
        return {
            "ok": False,
            "command": "nmap",
            "args": ["-p", "1-65535", target],
            "target": target,
            "error": (
                "nmap is not installed and the built-in fallback refuses full 1-65535 scans. "
                "Install nmap or use a narrower port list."
            ),
            "fallback_available": True,
            "fallback_tool": "network_scan_ports",
        }
    try:
        from argusai.core.security.network_recon import scan_ports

        fallback_ports = "common" if ports == "top-100" else ports
        fallback_scope = scope if normalize_scope(scope) else target
        result = await scan_ports(
            target,
            fallback_scope,
            ports=fallback_ports,
            timeout_seconds=3.0,
            concurrency=64,
            max_ports=256,
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "nmap",
            "target": target,
            "error": f"nmap is not installed and built-in fallback failed: {type(exc).__name__}: {exc}",
            "fallback_available": True,
            "fallback_tool": "network_scan_ports",
        }
    if not result.get("ok"):
        result.setdefault("command", "nmap")
        result["fallback_reason"] = "nmap is not installed"
        result["fallback_tool"] = "network_scan_ports"
        return result
    profile = result.get("profile") or {}
    parsed_hosts = []
    ports_out = []
    for item in profile.get("ports") or []:
        fp = item.get("fingerprint") or {}
        ports_out.append({
            "protocol": "tcp",
            "port": int(item.get("port") or 0),
            "state": {"state": item.get("state") or "open", "reason": item.get("reason") or "connect"},
            "service": {
                "name": fp.get("service") or "",
                "product": fp.get("product") or "",
                "version": fp.get("version") or "",
                "extrainfo": fp.get("banner") or "",
                "cpe": fp.get("cpe") or [],
            },
            "scripts": [],
        })
    if ports_out:
        parsed_hosts.append({
            "status": {"state": "up", "reason": "connect-fallback"},
            "addresses": [{"addr": profile.get("host") or target, "addrtype": "hostname"}],
            "hostnames": [],
            "ports": ports_out,
            "os": [],
        })
    result.update({
        "command": "nmap",
        "args": ["(fallback)", "network_scan_ports", str(fallback_ports)],
        "risk": "medium",
        "target": target,
        "fallback": True,
        "fallback_reason": "nmap is not installed",
        "fallback_tool": "network_scan_ports",
        "warning": (
            "External nmap is not installed. Used the built-in TCP connect scanner instead; "
            "aggressive OS detection and NSE scripts are unavailable."
        ),
        "parsed": {"format": "nmap_fallback", "hosts": parsed_hosts},
    })
    if aggressive or service_detection:
        result["capability_gap"] = "Fallback can do lightweight service fingerprints, not nmap -sV/-A/NSE detail."
    return result


def parse_nmap_xml(xml_text: str) -> dict[str, Any]:
    """Parse nmap XML into host/service/OS structures safe for model use."""
    if not xml_text.strip():
        return {"format": "nmap_xml", "hosts": [], "error": "empty nmap output"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"format": "nmap_xml", "hosts": [], "error": f"xml parse failed: {exc}"}
    hosts: list[dict[str, Any]] = []
    for host_el in root.findall("host"):
        host: dict[str, Any] = {
            "status": (host_el.find("status").attrib if host_el.find("status") is not None else {}),
            "addresses": [],
            "hostnames": [],
            "ports": [],
            "os": [],
        }
        for addr in host_el.findall("address"):
            host["addresses"].append({
                "addr": addr.attrib.get("addr", ""),
                "addrtype": addr.attrib.get("addrtype", ""),
                "vendor": addr.attrib.get("vendor", ""),
            })
        for hn in host_el.findall("hostnames/hostname"):
            host["hostnames"].append({
                "name": hn.attrib.get("name", ""),
                "type": hn.attrib.get("type", ""),
            })
        for port_el in host_el.findall("ports/port"):
            state_el = port_el.find("state")
            service_el = port_el.find("service")
            scripts = []
            for script in port_el.findall("script"):
                scripts.append({
                    "id": script.attrib.get("id", ""),
                    "output": script.attrib.get("output", ""),
                })
            cpes = [cpe.text or "" for cpe in port_el.findall("service/cpe") if (cpe.text or "").strip()]
            host["ports"].append({
                "protocol": port_el.attrib.get("protocol", ""),
                "port": int(port_el.attrib.get("portid", "0") or 0),
                "state": state_el.attrib if state_el is not None else {},
                "service": {
                    "name": service_el.attrib.get("name", "") if service_el is not None else "",
                    "product": service_el.attrib.get("product", "") if service_el is not None else "",
                    "version": service_el.attrib.get("version", "") if service_el is not None else "",
                    "extrainfo": service_el.attrib.get("extrainfo", "") if service_el is not None else "",
                    "ostype": service_el.attrib.get("ostype", "") if service_el is not None else "",
                    "method": service_el.attrib.get("method", "") if service_el is not None else "",
                    "conf": service_el.attrib.get("conf", "") if service_el is not None else "",
                    "cpe": cpes,
                },
                "scripts": scripts,
            })
        for osmatch in host_el.findall("os/osmatch"):
            classes = []
            for cls in osmatch.findall("osclass"):
                classes.append({
                    "type": cls.attrib.get("type", ""),
                    "vendor": cls.attrib.get("vendor", ""),
                    "osfamily": cls.attrib.get("osfamily", ""),
                    "osgen": cls.attrib.get("osgen", ""),
                    "accuracy": int(cls.attrib.get("accuracy", "0") or 0),
                    "cpe": [cpe.text or "" for cpe in cls.findall("cpe") if (cpe.text or "").strip()],
                })
            host["os"].append({
                "name": osmatch.attrib.get("name", ""),
                "accuracy": int(osmatch.attrib.get("accuracy", "0") or 0),
                "classes": classes,
            })
        hosts.append(host)
    return {
        "format": "nmap_xml",
        "scanner": root.attrib.get("scanner", "nmap"),
        "version": root.attrib.get("version", ""),
        "args": root.attrib.get("args", ""),
        "hosts": hosts,
    }
