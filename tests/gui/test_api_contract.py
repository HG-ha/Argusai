"""Bridge contract: every api.js call() must map to a public Api method."""

from __future__ import annotations

import re
from pathlib import Path

from argusai.gui import Api


def test_api_js_call_names_match_python_methods() -> None:
    api_js = Path("argusai/webui/src/api.js").read_text(encoding="utf-8")
    calls = sorted(set(re.findall(r'call\(\s*"([a-z0-9_]+)"', api_js, re.MULTILINE)))
    py = {name for name in dir(Api) if not name.startswith("_")}
    missing = [c for c in calls if c not in py]
    assert not missing, f"api.js calls Python methods that are not on Api: {missing}"


def test_required_attrs_on_api() -> None:
    assert hasattr(Api, "list_tasks")
    assert hasattr(Api, "get_settings")
    assert hasattr(Api, "uninstall_proxy_ca")
    assert hasattr(Api, "save_proxy_flow_body_as")
