"""Tests for mobile Frida recipe library."""

from __future__ import annotations

from pathlib import Path

from argusai.core.reverse.mobile_frida_recipes import get_recipe, list_recipes


def test_list_recipes_all() -> None:
    recipes = list_recipes()
    assert len(recipes) >= 4
    ids = {r["id"] for r in recipes}
    assert "android_ssl_pinning_bypass" in ids
    assert "ios_nsurlsession_trace" in ids


def test_list_recipes_platform_filter() -> None:
    android = list_recipes(platform="android")
    ios = list_recipes(platform="ios")
    assert all(r["platform"] == "android" for r in android)
    assert all(r["platform"] == "ios" for r in ios)
    assert len(android) >= 3
    assert len(ios) >= 1


def test_get_recipe_returns_script() -> None:
    rec = get_recipe("android_crypto_trace")
    assert rec is not None
    assert rec["id"] == "android_crypto_trace"
    assert "Cipher" in rec["script"]
    assert get_recipe("missing-recipe") is None


def test_mobile_frida_recipe_apply_writes_artifact(tmp_path: Path) -> None:
    from argusai.core.reverse.reverse_jobs import ReverseJobStore
    from argusai.core.tools.native_reverse import mobile_frida_recipe_apply

    store = ReverseJobStore(tmp_path / "reverse_jobs")
    job = store.create(title="mobile test", target_type="android", platform="android")
    api = type("Api", (), {"reverse_jobs": store})()

    out = mobile_frida_recipe_apply(api, job_id=job["id"], recipe_id="android_crypto_trace")
    assert out["ok"] is True
    assert out["artifact"]["kind"] == "frida_script"
    assert out["next_step"]["tool"] == "frida_spawn"
    assert out["next_step"]["requires_confirmation"] is True

    arts = store.get(job["id"])["artifacts"]
    assert len(arts) == 1
    payload = store.read_artifact(job["id"], arts[0]["id"], max_bytes=65536)
    assert payload is not None
    assert "Cipher" in payload["content"]
