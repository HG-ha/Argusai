"""Whitespace-tolerant slash-command tokenization."""

from __future__ import annotations

from argusai.core.channels.slash import (
    THINK_ACTIONS,
    match_slash_names,
    split_slash_command,
    think_action,
    toggle_action,
)


def test_split_slash_collapses_gaps_and_help_brackets() -> None:
    assert split_slash_command("/think  on") == ("/think", ("on",))
    assert split_slash_command("/think [on]") == ("/think", ("on",))
    assert split_slash_command("/think@Bot on") == ("/think", ("on",))
    assert split_slash_command("/think\u3000开") == ("/think", ("开",))
    assert split_slash_command(" /thinkon") == ("/thinkon", ())


def test_match_think_glues_ime_eaten_space() -> None:
    names = ("/think", "/thinking")
    assert match_slash_names("/thinkon", names, glue_toggle=True) == (
        "/think",
        ("on",),
    )
    assert match_slash_names("/thinking off", names, glue_toggle=True) == (
        "/thinking",
        ("off",),
    )
    assert match_slash_names("/search on", names, glue_toggle=True) is None


def test_toggle_action_maps_chinese_and_rejects_junk() -> None:
    assert toggle_action(()) == ("status", "")
    assert toggle_action(("开",)) == ("on", "")
    assert toggle_action(("on", "extra")) is None
    assert toggle_action(("on", "fix it"), allow_extra=True) == ("on", "fix it")


def test_think_action_accepts_effort_levels() -> None:
    assert think_action(()) == "status"
    assert think_action(("高",)) == "high"
    assert think_action(("max",)) == "max"
    assert think_action(("maybe",)) is None
    assert think_action(("high", "extra")) is None
    names = ("/think", "/thinking")
    assert match_slash_names(
        "/thinkhigh",
        names,
        glue_toggle=True,
        glue_values=THINK_ACTIONS,
    ) == ("/think", ("high",))
    assert match_slash_names(
        "/think最大",
        names,
        glue_toggle=True,
        glue_values=THINK_ACTIONS,
    ) == ("/think", ("max",))
