"""/file listing and numbered selection inside a conversation sandbox."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

from argusai.core.paths import PathManager
from argusai.gui.channels import ChannelsMixin


class _Api(ChannelsMixin):
    def __init__(self, data_root) -> None:
        self.settings_mgr = SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(data_root)),
        )

    def _validate_session_id(self, session_id):
        return str(session_id)


def _sandbox(tmp_path, names):
    api = _Api(tmp_path / "data")
    root = PathManager(data_root=str(tmp_path / "data")).assistant_sandbox("session-1")
    root.mkdir(parents=True, exist_ok=True)
    # Newest first is the listing order, so age them apart deterministically.
    now = time.time()
    for age, name in enumerate(names):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
        os.utime(path, (now - age, now - age))
    return api


def test_listing_numbers_rows_from_one(tmp_path) -> None:
    api = _sandbox(tmp_path, ["alpha.txt", "notes/report.md", "beta.txt"])

    listed = api._channel_session_file("session-1", "list")

    assert [row["index"] for row in listed["files"]] == [1, 2, 3]
    assert [row["relative_path"] for row in listed["files"]] == [
        "alpha.txt",
        "notes/report.md",
        "beta.txt",
    ]


def test_a_search_hit_keeps_the_number_that_downloads_it(tmp_path) -> None:
    # Renumbering the filtered rows made `/file 1` after a search download the
    # first file of the whole sandbox instead of the hit the user was reading.
    api = _sandbox(tmp_path, ["alpha.txt", "notes/report.md", "beta.txt"])

    found = api._channel_session_file("session-1", "search report")
    assert [row["relative_path"] for row in found["files"]] == ["notes/report.md"]
    assert found["files"][0]["index"] == 2

    sent = api._channel_session_file("session-1", "2")
    assert sent["ok"] is True
    assert sent["attachment"]["relative_path"] == "notes/report.md"


def test_an_unknown_number_is_refused(tmp_path) -> None:
    api = _sandbox(tmp_path, ["alpha.txt"])

    assert api._channel_session_file("session-1", "7")["ok"] is False
