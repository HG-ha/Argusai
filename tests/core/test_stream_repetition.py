"""In-stream repetition watchdog must not flag structured numeric tables."""

from __future__ import annotations

from argusai.core.assistant.streaming import _StreamRepetitionGuard, _repeat_key


def test_repeat_key_keeps_digits() -> None:
    assert "1" in _repeat_key("1×1=1")
    a = _repeat_key("1×5=5  2×5=10  3×5=15  4×5=20  5×5=25")
    b = _repeat_key("1×6=6  2×6=12  3×6=18  4×6=24  5×6=30  6×6=36")
    assert a != b
    assert "5" in a and "6" in b


def test_times_table_does_not_trip_stream_guard() -> None:
    table = """输入框已聚焦。现在输入九九乘法表内容。

九九乘法表内容（9 行）：

```
1×1=1
1×2=2  2×2=4
1×3=3  2×3=6  3×3=9
1×4=4  2×4=8  3×4=12  4×4=16
1×5=5  2×5=10  3×5=15  4×5=20  5×5=25
1×6=6  2×6=12  3×6=18  4×6=24  5×6=30  6×6=36
1×7=7  2×7=14  3×7=21  4×7=28  5×7=35  6×7=42  7×7=49
1×8=8  2×8=16  3×8=24  4×8=32  5×8=40  6×8=48  7×8=56  8×8=64
1×9=9  2×9=18  3×9=27  4×9=36  5×9=45  6×9=54  7×9=63  8×9=72  9×9=81
```
"""
    guard = _StreamRepetitionGuard()
    for i in range(0, len(table), 20):
        verdict = guard.feed(table[i : i + 20])
        assert verdict["state"] not in {"truncate", "stop"}, verdict
    assert guard.state() in {"ok", "warn"}


def test_verbatim_paragraph_loop_still_stops() -> None:
    loop = "让我再检查一下当前桌面状态。\n" * 16
    guard = _StreamRepetitionGuard()
    saw_stop_or_truncate = False
    for i in range(0, len(loop), 12):
        verdict = guard.feed(loop[i : i + 12])
        if verdict["state"] in {"truncate", "stop"}:
            saw_stop_or_truncate = True
            break
    assert saw_stop_or_truncate


def test_soft_first_thresholds_follow_loom22_spirit() -> None:
    """warn < truncate ≈ 2×warn style; truncate must be > 4 (old false-positive)."""
    guard = _StreamRepetitionGuard()
    assert guard._warn_repeats == 3
    assert guard._truncate_repeats >= 6
    assert guard._hard_stop_repeats >= guard._truncate_repeats
