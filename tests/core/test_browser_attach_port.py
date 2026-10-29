"""CDP attach helpers — port parsing and attach entrypoint imports."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from argusai.core.tools.common import _extract_port


def test_extract_port_defaults() -> None:
    assert _extract_port("http://127.0.0.1:9222") == 9222
    assert _extract_port("http://127.0.0.1:9223/") == 9223
    assert _extract_port("") == 9222


@patch("argusai.core.browser.browser_pool.get_attached_hub", return_value=None)
@patch(
    "argusai.core.browser.browser_attach.cdp_endpoint_alive",
    return_value={"ok": False, "browser": ""},
)
def test_browser_attach_existing_uses_extract_port(
    _mock_alive: MagicMock,
    _mock_hub: MagicMock,
) -> None:
    """Regression: browser_attach_existing must not raise NameError."""
    import asyncio

    from argusai.core.tools.browser_attach import browser_attach_existing

    out = asyncio.run(browser_attach_existing(None, endpoint="http://127.0.0.1:9222"))
    assert out["ok"] is False
    _mock_alive.assert_called_once_with(9222)
