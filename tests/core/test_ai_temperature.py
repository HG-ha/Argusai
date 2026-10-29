"""Tests for optional temperature on AI requests."""

from __future__ import annotations

from argusai.core.ai import AIConfig, _apply_temperature


def test_apply_temperature_includes_when_enabled() -> None:
    body: dict = {}
    cfg = AIConfig(temperature=0.5, send_temperature=True)
    _apply_temperature(body, cfg, None)
    assert body["temperature"] == 0.5


def test_apply_temperature_omits_when_disabled() -> None:
    body: dict = {}
    cfg = AIConfig(temperature=0.5, send_temperature=False)
    _apply_temperature(body, cfg, 0.25)
    assert "temperature" not in body


def test_apply_temperature_call_override_when_enabled() -> None:
    body: dict = {}
    cfg = AIConfig(temperature=0.5, send_temperature=True)
    _apply_temperature(body, cfg, 0.2)
    assert body["temperature"] == 0.2
