"""HTTP client helpers that bypass OS / env proxy settings.

ArgusAI's passive MITM proxy may enable the system proxy. Internal traffic
(AI endpoints, proxy upstream forwarding, scan runtime) must not loop through
the local MITM or SSL verification will fail on self-signed certificates.
"""

from __future__ import annotations

from typing import Any

import httpx


def direct_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` with ``trust_env=False`` (ignore system proxy)."""
    return httpx.AsyncClient(trust_env=False, **kwargs)
