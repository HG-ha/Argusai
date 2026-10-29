# -*- coding: utf-8 -*-
"""Tests for the v2 stream-event classifier in :mod:`argusai.gui.assistant_stream`.

The chat loop still emits Chinese status strings (``"\u6b63\u5728\u6267\u884c\u5de5\u5177\uff1a..."``).
The classifier upgrades those into a typed ``StreamEvent`` envelope so the
frontend can stop parsing localized text.
"""

from __future__ import annotations

import threading
import time

from argusai.gui.assistant_stream import AssistantStreamMixin, _classify_status_to_v2


def test_classify_tool_start_chinese_colon() -> None:
    # "\u6b63\u5728\u6267\u884c\u5de5\u5177\uff1afoo" = "Now running tool: foo"
    text = "\u6b63\u5728\u6267\u884c\u5de5\u5177\uff1afoo"

    kind, data = _classify_status_to_v2(text)

    assert kind == "tool_activity"
    assert data["tool"] == "foo"
    assert data["status"] == "start"


def test_classify_tool_start_with_timeout() -> None:
    kind, data = _classify_status_to_v2("正在执行工具：android_apktool_decode（上限 600s）")
    assert kind == "tool_activity"
    assert data["tool"] == "android_apktool_decode"
    assert data["timeout_seconds"] == 600


def test_classify_tool_start_ascii_colon() -> None:
    # Accept both ":" and "\uff1a".
    text = "\u6b63\u5728\u6267\u884c\u5de5\u5177:bar"

    kind, data = _classify_status_to_v2(text)

    assert kind == "tool_activity"
    assert data["tool"] == "bar"


def test_classify_tool_end() -> None:
    text = "\u5de5\u5177\u6267\u884c\u5b8c\u6210\uff0c\u6b63\u5728\u6574\u7406\u7ed3\u679c..."

    kind, data = _classify_status_to_v2(text)

    assert kind == "tool_activity"
    assert data["status"] == "end"
    assert "summary" in data


def test_classify_generic_text_falls_back_to_stage() -> None:
    text = "\u6b63\u5728\u601d\u8003..."

    kind, data = _classify_status_to_v2(text)

    assert kind == "stage"
    assert data["label"] == text


def test_classify_empty_returns_stage() -> None:
    kind, data = _classify_status_to_v2("")

    assert kind == "stage"
    assert data["label"] == ""


def test_completed_detached_stream_schedules_gc() -> None:
    class _Api(AssistantStreamMixin):
        _ASSISTANT_STREAM_GC_DELAY = 0.01

    api = _Api()
    api._assistant_streams = {
        "run-detached": {
            "lock": threading.Lock(),
            "events": [],
            "done": True,
        },
    }

    api._schedule_assistant_stream_gc("run-detached")
    time.sleep(0.05)
    assert "run-detached" not in api._assistant_streams
