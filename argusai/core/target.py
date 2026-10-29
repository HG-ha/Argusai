"""Target and authentication configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class AuthConfig:
    """Describe how to obtain an authenticated session.

    Only ``mode`` is required. The other fields are interpreted by the
    corresponding auth strategy:

    - ``mode="none"``: no auth.
    - ``mode="static"``: apply ``headers`` (e.g. ``{"Authorization": "Bearer ..."}``)
      and ``cookies`` directly to every HTTP request.
    - ``mode="login_form"``: Playwright navigates to ``login_url``, fills
      ``username`` / ``password`` into ``selectors`` and submits.
    - ``mode="script"``: import ``script`` (``module:callable``) which receives
      the shared :class:`~argusai.core.context.Context` and returns a dict of
      headers/cookies to apply.
    """

    mode: str = "none"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    login_url: str | None = None
    username: str | None = None
    password: str | None = None
    selectors: dict[str, str] = field(default_factory=dict)
    script: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Target:
    """What to audit.

    ``scope`` is an allow-list of hosts that checks may touch. If empty, only
    the base URL's host is in scope. This is a safety guard — the recon check
    will not follow links outside scope.
    """

    url: str
    type: str = "url"
    name: str = ""
    scope: list[str] = field(default_factory=list)
    auth: AuthConfig | None = None
    headers: dict[str, str] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)
    artifact: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = urlparse(self.url).netloc or self.url
        if not self.scope:
            host = urlparse(self.url).netloc
            if host:
                self.scope = [host]

    @property
    def origin(self) -> str:
        u = urlparse(self.url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    def in_scope(self, url: str) -> bool:
        """Check if ``url`` is within this target's scope."""
        try:
            host = urlparse(url).netloc
        except ValueError:
            return False
        if not host:
            return True
        return any(host == s or host.endswith("." + s) for s in self.scope)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Target":
        d = dict(d)
        auth_raw = d.pop("auth", None)
        auth = AuthConfig(**auth_raw) if isinstance(auth_raw, dict) else None
        return cls(auth=auth, **d)
