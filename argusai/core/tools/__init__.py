"""Aggregated AI assistant tool executors."""

from __future__ import annotations

from typing import Any

from .common import *
from . import kb_tasks as _kb_tasks
from .kb_tasks import *
from . import browser_reverse as _browser_reverse
from .browser_reverse import *
from . import browser_attach as _browser_attach
from .browser_attach import *
from . import web_static as _web_static
from .web_static import *
from . import sandbox_code as _sandbox_code
from .sandbox_code import *
from . import case_mcp as _case_mcp
from .case_mcp import *
from . import skills as _skills
from .skills import *
from . import user_todos as _user_todos
from .user_todos import *
from . import desktop_automation as _desktop_automation
from .desktop_automation import *
from . import user_profile_tools as _user_profile_tools
from .user_profile_tools import *
from . import native_reverse as _native_reverse
from .native_reverse import *
from . import proxy_import_tools as _proxy_import_tools
from .proxy_import_tools import *
from . import network_pcap as _network_pcap
from .network_pcap import *
from . import supply_chain as _supply_chain
from .supply_chain import *
from . import wireless_static as _wireless_static
from .wireless_static import *
from . import iot_static as _iot_static
from .iot_static import *
from . import hardware_static as _hardware_static
from .hardware_static import *
from . import cloud_static as _cloud_static
from .cloud_static import *
from . import mobile_dast as _mobile_dast
from .mobile_dast import *
from . import directory_static as _directory_static
from .directory_static import *
from . import assistant_cron_tools as _assistant_cron_tools
from .assistant_cron_tools import *
from . import local_surface_tools as _local_surface_tools
from .local_surface_tools import *
from . import channel_tools as _channel_tools
from .channel_tools import *
from . import document_parse as _document_parse
from .document_parse import *

_MODULES = [
    _kb_tasks,
    _browser_reverse,
    _browser_attach,
    _web_static,
    _sandbox_code,
    _case_mcp,
    _skills,
    _user_todos,
    _desktop_automation,
    _user_profile_tools,
    _native_reverse,
    _proxy_import_tools,
    _network_pcap,
    _supply_chain,
    _wireless_static,
    _iot_static,
    _hardware_static,
    _cloud_static,
    _mobile_dast,
    _directory_static,
    _assistant_cron_tools,
    _local_surface_tools,
    _channel_tools,
    _document_parse,
]

# Registry of all executors keyed by stable tool name. Keep this contract
# identical to the former argusai.core._tools.EXECUTORS globals() scan.
EXECUTORS: dict[str, Any] = {}
for _module in _MODULES:
    for _name, _obj in vars(_module).items():
        if callable(_obj) and not _name.startswith("_") and _name not in {"Any", "Path"}:
            EXECUTORS[_name] = _obj

# Re-export private helpers used by the assistant runtime and a few legacy
# call sites. This intentionally mirrors the old single-module namespace.
for _module in _MODULES:
    for _name, _obj in vars(_module).items():
        if _name.startswith("__"):
            continue
        globals().setdefault(_name, _obj)

__all__ = sorted({*EXECUTORS.keys(), "EXECUTORS"} | {name for name in globals() if name.startswith("_") and not name.startswith("__")})
