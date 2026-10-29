"""runtime_install accepts managed WABT (not reverse_toolchain_install)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from argusai.core.tools.sandbox_code import runtime_install


def test_runtime_install_accepts_wabt() -> None:
    api = MagicMock()
    api.settings_mgr.get.return_value = MagicMock(
        data_root="C:/ArgusAI/data",
        runtime_mirrors={},
    )
    with patch(
        "argusai.core.managed_runtimes.install_runtime",
        return_value={"ok": True, "runtime": "wabt"},
    ) as mock_install:
        out = asyncio.run(runtime_install(api, runtime="wabt"))
    assert out["ok"] is True
    assert mock_install.call_args.args[0] == "wabt"


def test_runtime_install_rejects_unknown() -> None:
    api = MagicMock()
    out = asyncio.run(runtime_install(api, runtime="jadx"))
    assert out["ok"] is False
    assert out["error_code"] == "invalid_runtime"
