"""macOS desktop helpers (mocked; safe on CI)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argusai.core.workspace import desktop_control as dc
from argusai.core.workspace import darwin_input as di


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_darwin_list_monitors_parses_osascript_json() -> None:
    payload = json.dumps([
        {
            "index": 0,
            "primary": True,
            "left": 0,
            "top": 0,
            "width": 1440,
            "height": 900,
            "right": 1440,
            "bottom": 900,
            "name": "display-1",
        },
    ])
    with patch.object(dc, "_darwin_osascript", return_value=payload):
        mons = dc._darwin_list_monitors()
    assert len(mons) == 1
    assert mons[0]["width"] == 1440
    assert mons[0].get("coordinate_system") == "macos_cocoa_bottom_left"


def test_cocoa_point_to_cg_flips_y() -> None:
    monitors = [{"bottom": 1200}]
    assert di.cocoa_point_to_cg(100, 200, monitors) == (100.0, 1000.0)


def test_darwin_screencapture_invokes_cli(tmp_path: Path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    proc = MagicMock()
    proc.returncode = 0
    with patch("subprocess.run", return_value=proc) as run:
        ok, err = dc._darwin_screencapture(png, monitor=1)
    assert ok is True
    assert err == ""
    args = run.call_args[0][0]
    assert args[0] == "screencapture"
    assert "-D2" in args
