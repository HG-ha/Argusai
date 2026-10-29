"""ToolRegistry dispatch behaviour."""

from __future__ import annotations

import asyncio

from argusai.core.assistant.tools import ToolRegistry, unknown_tool_result


def test_dispatch_unknown_tool_returns_structured_error() -> None:
    registry = ToolRegistry()
    registry.register("extract_strings", "Extract strings from a file.", {}, lambda **kwargs: {"ok": True})
    registry.register(
        "reverse_artifact_copy_to",
        "Copy reverse artifact.",
        {},
        lambda **kwargs: {"ok": True},
    )

    out = asyncio.run(registry.dispatch("reverse_artifact_extract_strings", {}))

    assert out["ok"] is False
    assert out.get("do_not_retry") is True
    assert "Unknown tool" in out["error"]
    assert out["unknown_tool"] == "reverse_artifact_extract_strings"
    assert "extract_strings" in out.get("suggestions") or "reverse_artifact_copy_to" in out.get("suggestions")
    assert out["next_step"] in out.get("suggestions") or out["next_step"] == "tool_search"


def test_unknown_tool_result_shape() -> None:
    registry = ToolRegistry()
    registry.register("android_apktool_decode", "Decode APK.", {}, lambda **kwargs: {"ok": True})

    out = unknown_tool_result(registry, "not_a_real_tool_xyz")

    assert out["ok"] is False
    assert out["unknown_tool"] == "not_a_real_tool_xyz"
    assert out["next_step"] in out.get("suggestions") or out["next_step"] == "tool_search"
    assert "invoke_catalogue_tool" in out["suggested_fix"]


def test_dispatch_rejects_missing_required_arguments() -> None:
    called = False

    def run(**kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        "run_shell_snippet",
        "Run shell.",
        {"script": {"type": "string"}},
        run,
        required=["script"],
    )

    out = asyncio.run(registry.dispatch("run_shell_snippet", {}))

    assert out["ok"] is False
    assert out["error_code"] == "invalid_arguments"
    assert out["missing_arguments"] == ["script"]
    assert called is False


def test_dispatch_rejects_blank_snippet_body() -> None:
    registry = ToolRegistry()
    registry.register(
        "run_python_snippet",
        "Run Python.",
        {"code": {"type": "string"}},
        lambda **kwargs: {"ok": True},
        required=["code"],
    )

    out = asyncio.run(registry.dispatch("run_python_snippet", {"code": " \n\t"}))

    assert out["ok"] is False
    assert out["error_code"] == "invalid_arguments"
    assert out["empty_arguments"] == ["code"]
