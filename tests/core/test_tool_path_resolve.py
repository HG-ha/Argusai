"""Tests for shared tool path resolvers."""

from __future__ import annotations

from unittest.mock import MagicMock

from argusai.core.tools.common import _resolve_session_output_path


def test_expandvars_userprofile_as_absolute(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "home"
    profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))

    api = MagicMock()
    api.settings_mgr.get.return_value = MagicMock(data_root=str(tmp_path / "data"))
    kwargs = {"_session_id": "sess-1"}

    target, kind, _root = _resolve_session_output_path(
        api,
        kwargs,
        r"%USERPROFILE%\Downloads\lanzou-downloader",
    )
    assert kind == "absolute_user_path"
    assert target == (profile / "Downloads" / "lanzou-downloader").resolve()


def test_unexpanded_env_var_stays_in_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("USERPROFILE", raising=False)
    api = MagicMock()
    data_root = tmp_path / "data"
    api.settings_mgr.get.return_value = MagicMock(data_root=str(data_root))
    kwargs = {"_session_id": "sess-1"}

    target, kind, root = _resolve_session_output_path(
        api,
        kwargs,
        r"%USERPROFILE%\Downloads\out",
    )
    assert kind == "session_sandbox"
    assert root.endswith("sess-1.sandbox") or root.endswith("sess-1.sandbox".replace("/", "\\"))
    assert target.name == "out"
    assert target.parent.name == "Downloads"
    assert target.parent.parent.name == "%USERPROFILE%"
