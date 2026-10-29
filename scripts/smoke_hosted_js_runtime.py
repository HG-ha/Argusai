"""Smoke test the hosted JS runtime AI-tool path.

Run from the project root:

    py -3 scripts/smoke_hosted_js_runtime.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argusai.core import _tools
from argusai.core.reverse.js_runtime import runtime_stop
from argusai.core.reverse.reverse_jobs import ReverseJobStore


class _Settings:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root


class _SettingsMgr:
    def __init__(self, data_root: Path) -> None:
        self._settings = _Settings(data_root)

    def get(self) -> _Settings:
        return self._settings


def _assert_ok(label: str, result: dict) -> dict:
    if not result.get("ok"):
        raise AssertionError(f"{label} failed: {result}")
    print(f"ok: {label}")
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="argusai-hosted-smoke-") as td:
        root = Path(td)
        api = SimpleNamespace(
            settings_mgr=_SettingsMgr(root),
            reverse_jobs=ReverseJobStore(root / "reverse_jobs"),
        )
        session = "hosted-smoke"
        try:
            job = _assert_ok(
                "reverse_job_create",
                _tools.reverse_job_create(api, title="hosted runtime smoke", target_url="https://example.test/", scope="example.test"),
            )["job"]
            _assert_ok("js_runtime_create", _tools.js_runtime_create(api, session_id=session, env="browser"))
            _assert_ok(
                "js_runtime_load",
                _tools.js_runtime_load(
                    api,
                    session_id=session,
                    source="function add(a,b){ return a+b; } globalThis.add=add;",
                    filename="smoke.js",
                ),
            )
            _assert_ok("js_runtime_sources", _tools.js_runtime_sources(api, session_id=session))
            _assert_ok("js_runtime_instrument", _tools.js_runtime_instrument(api, session_id=session, target="add", hook_id="add"))
            _assert_ok("js_runtime_call", _tools.js_runtime_call(api, session_id=session, target="add", args_json="[2,3]"))
            drained = _assert_ok("js_runtime_drain", _tools.js_runtime_drain(api, session_id=session, limit=10))
            _assert_ok(
                "reverse_oracle_add",
                _tools.reverse_oracle_add(
                    api,
                    job_id=job["id"],
                    records_json=json.dumps(drained["records"]),
                    stage="add",
                    runtime="hosted-js",
                ),
            )
            _assert_ok("reverse_oracle_read", _tools.reverse_oracle_read(api, job_id=job["id"], stage="add"))
            _assert_ok(
                "js_runtime_patch",
                _tools.js_runtime_patch(
                    api,
                    session_id=session,
                    mode="string",
                    search="return a+b;",
                    replacement="return a*b;",
                ),
            )
            patched = _assert_ok("js_runtime_call patched", _tools.js_runtime_call(api, session_id=session, target="add", args_json="[2,3]"))
            if patched.get("result") != "6":
                raise AssertionError(f"patched call returned unexpected result: {patched}")
            _assert_ok("js_runtime_patches", _tools.js_runtime_patches(api, session_id=session))
            _assert_ok("js_runtime_hooks", _tools.js_runtime_hooks(api, session_id=session))
        finally:
            _tools.js_runtime_close(api, session_id=session)
            runtime_stop()
    print("DONE")


if __name__ == "__main__":
    main()
