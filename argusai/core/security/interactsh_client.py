"""Pure-Python client for ProjectDiscovery's Interactsh out-of-band service.

This module talks to the public Interactsh servers (``oast.fun``, ``oast.live``,
``oast.pro``, ...) so the assistant can request real callback subdomains and
receive DNS / HTTP / SMTP events without the user having to install or run any
external binary.

Protocol summary (HTTP API, all JSON, TLS):

* ``POST /register`` with ``{"public-key": <b64 PEM>, "secret-key": <uuid>,
  "correlation-id": <20 lowercase alnum>}`` registers a fresh tenant.
* ``GET /poll?id=<cid>&secret=<secret>`` returns ``{"data": [<b64 enc events>],
  "aes_key": <b64 RSA-OAEP encrypted AES key>}``. The AES key is encrypted with
  the public key we shipped at registration; each event is base64 of
  ``IV(16) || ciphertext`` and ciphertext is AES-CFB with the recovered key.
* ``POST /deregister`` cleans up. Polls beyond ~6h with no event activity get
  garbage collected by the server, so long-lived sessions must keep polling.

Subdomains under ``<cid>.<server>`` will resolve and any DNS lookup / HTTP
request to them gets recorded as an event the next poll will deliver.

Reference implementation: github.com/projectdiscovery/interactsh.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("argusai.interactsh")


# Project Discovery's rotating fleet of public Interactsh servers. We default
# to the first one and fall back through the list on registration failure.
DEFAULT_SERVERS: list[str] = [
    "oast.fun",
    "oast.live",
    "oast.pro",
    "oast.site",
    "oast.me",
    "oast.online",
]

_USER_AGENT = "argusai-oob/1.0"


def _new_correlation_id(length: int = 20) -> str:
    alpha = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alpha) for _ in range(length))


def _new_subdomain_label(length: int = 8) -> str:
    alpha = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alpha) for _ in range(length))


@dataclass
class InteractEvent:
    """One decrypted interaction event as returned by the server."""

    raw: dict[str, Any]
    received_at: float = field(default_factory=time.time)


class InteractshClient:
    """Single-tenant client for one Interactsh correlation id.

    Lifecycle:

    1. Construct with the desired ``server`` and poll interval.
    2. Call :meth:`register` (HTTP, ~1s round-trip).
    3. Call :meth:`start_polling` to spawn the background polling thread.
    4. Hand out subdomains via :meth:`new_subdomain` and read decrypted events
       via :meth:`events`.
    5. Call :meth:`shutdown` when finished to deregister and stop polling.

    The class is thread-safe for concurrent ``events()`` readers / one writer.
    """

    def __init__(
        self,
        server: str = "",
        poll_interval: float = 5.0,
        request_timeout: float = 15.0,
        allow_fallback: bool = True,
    ) -> None:
        self.server: str = (server or DEFAULT_SERVERS[0]).strip()
        self.poll_interval: float = max(1.0, float(poll_interval or 5.0))
        self.request_timeout: float = max(3.0, float(request_timeout or 15.0))
        self.allow_fallback: bool = bool(allow_fallback)
        self.correlation_id: str = ""
        self.secret_key: str = ""
        self.public_key_pem: str = ""
        self._private_key: rsa.RSAPrivateKey | None = None
        self._events: list[InteractEvent] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._registered = False
        self.last_error: str = ""
        self.registered_at: float = 0.0
        self.last_polled_at: float = 0.0
        self._consecutive_poll_errors: int = 0

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def is_running(self) -> bool:
        return bool(self._poll_thread and self._poll_thread.is_alive())

    @property
    def is_unhealthy(self) -> bool:
        return self._registered and self._consecutive_poll_errors >= 3

    @property
    def domain(self) -> str:
        if not self._registered:
            return ""
        return f"{self.correlation_id}.{self.server}"

    def status(self) -> dict[str, Any]:
        return {
            "registered": self._registered,
            "running": self.is_running,
            "server": self.server,
            "domain": self.domain,
            "correlation_id": self.correlation_id,
            "event_count": len(self._events),
            "last_error": self.last_error,
            "consecutive_poll_errors": self._consecutive_poll_errors,
            "registered_at": self.registered_at,
            "last_polled_at": self.last_polled_at,
            "poll_interval": self.poll_interval,
        }

    def new_subdomain(self, label: str = "") -> str:
        """Return a fresh random subdomain under our correlation id.

        The label is purely cosmetic — the server will deliver any event under
        the entire correlation id, regardless of which label was hit. Using a
        unique label per probe lets us correlate which payload triggered which
        callback later.
        """

        if not self._registered:
            raise RuntimeError("Interactsh client is not registered yet")
        lbl = (label or "").strip().lower() or _new_subdomain_label()
        # Strip anything that would not be a valid DNS label
        lbl = "".join(c for c in lbl if c.isalnum() or c == "-")[:32] or _new_subdomain_label()
        return f"{lbl}.{self.domain}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(self) -> dict[str, Any]:
        """Generate fresh keys and register with the Interactsh server.

        Tries the default server list in order so a transient outage or a
        middlebox returning bogus 200 responses on one Project Discovery host
        does not break the audit session.
        """

        if self._registered:
            return {"server": self.server, "domain": self.domain, "reused": True}

        # Fresh RSA-2048 keypair (Interactsh server uses our public key to wrap
        # the AES symmetric key it ships back on every poll).
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_bytes = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.public_key_pem = public_bytes.decode("ascii")
        public_b64 = base64.b64encode(public_bytes).decode("ascii")

        candidates = [self.server]
        if self.allow_fallback:
            candidates.extend(s for s in DEFAULT_SERVERS if s != self.server)
        last_exc: Exception | None = None
        for srv in candidates:
            correlation_id = _new_correlation_id()
            secret_key = str(uuid.uuid4())
            body = {
                "public-key": public_b64,
                "secret-key": secret_key,
                "correlation-id": correlation_id,
            }
            try:
                r = httpx.post(
                    f"https://{srv}/register",
                    json=body,
                    timeout=self.request_timeout,
                    headers={"User-Agent": _USER_AGENT},
                )
                r.raise_for_status()
                self._validate_register_response(srv, r)
                self._validate_poll_response(srv, correlation_id, secret_key)
            except Exception as exc:
                last_exc = exc
                log.info("interactsh register on %s failed: %s", srv, exc)
                continue
            self.server = srv
            self.correlation_id = correlation_id
            self.secret_key = secret_key
            self._registered = True
            self.registered_at = time.time()
            self.last_error = ""
            self._consecutive_poll_errors = 0
            log.info("interactsh registered: %s", self.domain)
            return {"server": self.server, "domain": self.domain, "reused": False}

        self.last_error = f"register failed on all servers: {last_exc}"
        raise RuntimeError(self.last_error)

    def _validate_register_response(self, server: str, response: httpx.Response) -> None:
        """Reject obvious non-Interactsh responses that still use HTTP 200."""

        text = (response.text or "").strip()
        content_type = response.headers.get("content-type", "").lower()
        lowered = text.lower()
        if "already exists" in lowered:
            raise RuntimeError("register API returned 'already exists' for a fresh correlation id")
        if text and "application/json" in content_type:
            try:
                payload = response.json()
            except Exception as exc:
                raise RuntimeError(f"register API returned invalid JSON: {exc}") from exc
            if isinstance(payload, dict):
                msg = json.dumps(payload, ensure_ascii=False, default=str).lower()
                if "already exists" in msg or payload.get("error"):
                    raise RuntimeError(f"register API rejected session: {payload}")
            return
        if text and text[:1] in "[{":
            try:
                payload = response.json()
            except Exception as exc:
                raise RuntimeError(f"register API returned malformed JSON-looking body: {exc}") from exc
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(f"register API rejected session: {payload}")
            return
        # The public server commonly returns a tiny text success response, so
        # don't require JSON here. The poll probe below verifies the API shape.
        if len(text) > 512 or "<html" in lowered:
            raise RuntimeError(f"register API on {server} returned a non-API response")

    def _validate_poll_response(self, server: str, correlation_id: str, secret_key: str) -> None:
        """Make one cheap poll before accepting a server.

        A healthy empty Interactsh poll is JSON and usually contains empty
        ``data`` / ``aes_key`` fields. Captive portals, WAF error pages, and
        poisoned mirrors often return HTTP 200 with unrelated text; accepting
        those makes OOB look configured while every later poll is useless.
        """

        r = httpx.get(
            f"https://{server}/poll",
            params={"id": correlation_id, "secret": secret_key},
            timeout=self.request_timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        try:
            payload = r.json() or {}
        except Exception as exc:
            raise RuntimeError(f"poll API returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"poll API returned unexpected payload type: {type(payload).__name__}")
        if "data" not in payload and "aes_key" not in payload:
            raise RuntimeError(f"poll API returned unexpected schema: {payload}")

    def start_polling(self) -> None:
        if not self._registered:
            raise RuntimeError("Cannot poll: not registered")
        if self.is_running:
            return
        self._stop_event.clear()
        t = threading.Thread(
            target=self._poll_loop,
            name="interactsh-poll",
            daemon=True,
        )
        self._poll_thread = t
        t.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5.0)
        self._poll_thread = None
        if self._registered:
            try:
                httpx.post(
                    f"https://{self.server}/deregister",
                    json={
                        "correlation-id": self.correlation_id,
                        "secret-key": self.secret_key,
                    },
                    timeout=5.0,
                    headers={"User-Agent": _USER_AGENT},
                )
            except Exception as exc:
                log.debug("interactsh deregister error (ignored): %s", exc)
        self._registered = False

    # ------------------------------------------------------------------
    # Internal poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                events = self._poll_once()
                self._consecutive_poll_errors = 0
                if events:
                    now = time.time()
                    with self._lock:
                        for ev in events:
                            self._events.append(InteractEvent(raw=ev, received_at=now))
            except Exception as exc:
                self._consecutive_poll_errors += 1
                self.last_error = f"poll failed: {exc}"
                log.debug("interactsh poll error: %s", exc)
            if self._stop_event.wait(self.poll_interval):
                break

    def _poll_once(self) -> list[dict[str, Any]]:
        if not self._registered:
            return []
        r = httpx.get(
            f"https://{self.server}/poll",
            params={"id": self.correlation_id, "secret": self.secret_key},
            timeout=self.request_timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        self.last_polled_at = time.time()
        if r.status_code in (401, 404):
            # Server forgot us (cold rotation or our session expired). Try to
            # re-register so the next poll comes back alive.
            log.info("interactsh session lost (%s), re-registering", r.status_code)
            self._registered = False
            self.register()
            return []
        r.raise_for_status()
        payload = r.json() or {}
        encrypted_aes_b64 = payload.get("aes_key") or ""
        encrypted_events = payload.get("data") or []
        if not encrypted_aes_b64 or not encrypted_events:
            return []
        try:
            encrypted_aes = base64.b64decode(encrypted_aes_b64)
        except Exception as exc:
            raise RuntimeError(f"bad aes_key base64: {exc}") from exc
        assert self._private_key is not None
        aes_key = self._private_key.decrypt(
            encrypted_aes,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        events: list[dict[str, Any]] = []
        for enc_event_b64 in encrypted_events:
            try:
                blob = base64.b64decode(enc_event_b64)
                if len(blob) < 16:
                    continue
                iv, ciphertext = blob[:16], blob[16:]
                cipher = Cipher(algorithms.AES(aes_key), modes.CFB(iv))
                dec = cipher.decryptor()
                plaintext = dec.update(ciphertext) + dec.finalize()
                events.append(json.loads(plaintext.decode("utf-8", errors="replace")))
            except Exception as exc:
                log.warning("failed to decrypt interactsh event: %s", exc)
        return events

    # ------------------------------------------------------------------
    # Public event readers
    # ------------------------------------------------------------------

    def events(self, token: str = "", since: float = 0.0, limit: int = 200) -> list[dict[str, Any]]:
        """Return a snapshot of received events.

        Args:
            token: If non-empty, only events whose serialized JSON contains
                this substring are returned. Use the unique random label you
                injected into a payload to filter for a specific probe.
            since: Unix timestamp; only events received at or after this time
                are returned. Use 0 for "all events".
            limit: Hard cap on number of events returned.
        """

        with self._lock:
            snapshot = list(self._events)
        out: list[dict[str, Any]] = []
        for ev in snapshot:
            if since and ev.received_at < since:
                continue
            if token:
                blob = json.dumps(ev.raw, default=str, ensure_ascii=False)
                if token not in blob:
                    continue
            row = dict(ev.raw)
            row["_received_at"] = ev.received_at
            out.append(row)
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    def wait_for_event(
        self,
        token: str = "",
        timeout: float = 30.0,
        since: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Block until at least one matching event arrives or timeout hits.

        Useful for synchronous "fire payload, wait briefly, read callback"
        workflows that the agent typically performs.
        """

        deadline = time.time() + max(1.0, float(timeout))
        baseline = since or time.time()
        while time.time() < deadline:
            hits = self.events(token=token, since=baseline)
            if hits:
                return hits
            time.sleep(min(1.0, self.poll_interval / 2.0))
        return []
