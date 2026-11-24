from types import SimpleNamespace

import pytest

from argusai.gui.webview_permissions import _origin_tuple, _trusted_microphone_request


def origin(host="127.0.0.1", port=4173, scheme="http"):
    return SimpleNamespace(host=lambda: host, port=lambda: port, protocol=lambda: scheme)


@pytest.mark.parametrize("url", ["https://example.com", "file:///tmp/app.html", "http://127.0.0.1:bad", "http://127.0.0.1.evil.test:4173"])
def test_only_local_http_app_origins_can_inherit_microphone_permission(url):
    assert _origin_tuple(url) is None


@pytest.mark.parametrize("host,port,main,frame_host,allowed", [
    ("127.0.0.1", 4173, True, "127.0.0.1", True),
    ("127.0.0.1", 4174, True, "127.0.0.1", False),
    ("example.com", 4173, True, "example.com", False),
    ("127.0.0.1", 4173, False, "127.0.0.1", False),
    ("127.0.0.1", 4173, True, "example.com", False),
])
def test_microphone_consent_cannot_transfer_to_remote_pages_or_frames(host, port, main, frame_host, allowed):
    expected = _origin_tuple("http://127.0.0.1:4173/")
    frame = SimpleNamespace(isMainFrame=lambda: main, securityOrigin=lambda: origin(frame_host))
    assert _trusted_microphone_request(expected, origin(host, port), frame) is allowed
