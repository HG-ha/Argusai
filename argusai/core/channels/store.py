"""Durable channel configuration, encrypted secrets, and paired identities."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .i18n import SUPPORTED_LOCALES, normalize_locale
from .models import (
    CHANNEL_PROVIDERS,
    ChannelConfig,
    ChannelError,
    ChannelEvent,
    ChannelIdentity,
)
from .secret_store import ChannelSecretStore

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PAIR_CODE_RE = re.compile(r"^\d{6}$")
_IDENTITY_PERMISSIONS = frozenset({
    "chat",
    "status",
    "cancel",
    "confirm",
    "configure",
    "send",
})
# Fresh pairings get the full capability set. Operators can still revoke via
# GUI / ``argusai channels grant``. Least-privilege remains available — we just
# stop shipping dead "Allow" buttons that then refuse with confirm_not_allowed.
_DEFAULT_IDENTITY_PERMISSIONS = tuple(sorted(_IDENTITY_PERMISSIONS))


def _normalize_session_ids(*groups: Any) -> tuple[str, ...]:
    seen: list[str] = []
    for group in groups:
        if group is None:
            continue
        items = group if isinstance(group, (list, tuple, set)) else (group,)
        for item in items:
            session_id = str(item or "").strip()
            if session_id and session_id not in seen:
                seen.append(session_id)
    return tuple(seen)

_DEFAULT_TRANSPORTS = {
    "telegram": "long_polling",
    "discord": "gateway",
    "slack": "socket_mode",
    "feishu": "websocket",
    "dingtalk": "stream",
    "wecom": "webhook",
    "wechat": "ilink_long_polling",
    "webhook": "http",
}


class ChannelStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / "channels.json"
        self.pairings_path = self.root / "pairings.json"
        self.events_path = self.root / "event_keys.json"
        self.actions_path = self.root / "channel_actions.json"
        self.outbox_path = self.root / "channel_outbox.json"
        self.outbox_lock_path = self.root / "channel_outbox.lock"
        self.secrets = ChannelSecretStore(self.root)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "channels": [], "identities": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": 1, "channels": [], "identities": []}
        return payload if isinstance(payload, dict) else {
            "version": 1,
            "channels": [],
            "identities": [],
        }

    def _write(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def _read_pairings(self) -> dict[str, dict[str, Any]]:
        """Load pending pair codes from disk (shared across CLI / serve processes)."""
        if not self.pairings_path.exists():
            return {}
        try:
            payload = json.loads(self.pairings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        rows = payload.get("pairings") if isinstance(payload, dict) else None
        if not isinstance(rows, dict):
            return {}
        now = time.time()
        alive: dict[str, dict[str, Any]] = {}
        for pairing_id, raw in rows.items():
            if not isinstance(raw, dict):
                continue
            try:
                expires_at = float(raw.get("expires_at") or 0)
            except (TypeError, ValueError):
                continue
            if expires_at <= now:
                continue
            salt_hex = str(raw.get("salt_hex") or "")
            digest_hex = str(raw.get("digest_hex") or "")
            try:
                salt = bytes.fromhex(salt_hex)
                digest = bytes.fromhex(digest_hex)
            except ValueError:
                continue
            if len(salt) < 16 or len(digest) != 32:
                continue
            alive[str(pairing_id)] = {
                "channel_id": str(raw.get("channel_id") or ""),
                "provider": str(raw.get("provider") or ""),
                "salt": salt,
                "digest": digest,
                "bound_session_id": str(raw.get("bound_session_id") or ""),
                "expires_at": expires_at,
            }
        return alive

    def _write_pairings(self, pairings: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        serializable = {
            pairing_id: {
                "channel_id": str(row.get("channel_id") or ""),
                "provider": str(row.get("provider") or ""),
                "salt_hex": bytes(row["salt"]).hex(),
                "digest_hex": bytes(row["digest"]).hex(),
                "bound_session_id": str(row.get("bound_session_id") or ""),
                "expires_at": float(row.get("expires_at") or 0),
            }
            for pairing_id, row in pairings.items()
            if isinstance(row, dict) and isinstance(row.get("salt"), (bytes, bytearray))
            and isinstance(row.get("digest"), (bytes, bytearray))
        }
        payload = {"version": 1, "pairings": serializable}
        tmp = self.pairings_path.with_suffix(
            f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.pairings_path)

    @staticmethod
    def _config(raw: dict[str, Any]) -> ChannelConfig:
        provider = str(raw.get("provider") or "").strip().lower()
        if provider not in CHANNEL_PROVIDERS:
            raise ValueError(f"unsupported channel provider: {provider}")
        channel_id = str(raw.get("id") or "").strip().lower()
        if not _ID_RE.fullmatch(channel_id):
            raise ValueError("invalid channel id")
        return ChannelConfig(
            id=channel_id,
            provider=provider,  # type: ignore[arg-type]
            name=str(raw.get("name") or provider.title()).strip()[:120],
            enabled=bool(raw.get("enabled", False)),
            transport=str(
                raw.get("transport")
                or _DEFAULT_TRANSPORTS.get(provider)
                or ""
            ).strip(),
            settings=dict(raw.get("settings") or {}),
            secret_names=tuple(sorted({
                str(item).strip()
                for item in raw.get("secret_names") or []
                if str(item).strip()
            })),
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
        )

    @staticmethod
    def _identity(raw: dict[str, Any]) -> ChannelIdentity:
        provider = str(raw.get("provider") or "").strip().lower()
        if provider not in CHANNEL_PROVIDERS:
            raise ValueError("invalid identity provider")
        bound_session_id = str(raw.get("bound_session_id") or "")
        if "allowed_session_ids" in raw:
            allowed_session_ids = _normalize_session_ids(
                raw.get("allowed_session_ids"),
                (bound_session_id,),
            )
        else:
            # Legacy identities only keep the conversation they are bound to.
            allowed_session_ids = _normalize_session_ids((bound_session_id,))
        return ChannelIdentity(
            id=str(raw.get("id") or ""),
            channel_id=str(raw.get("channel_id") or ""),
            provider=provider,  # type: ignore[arg-type]
            external_user_id=str(raw.get("external_user_id") or ""),
            external_chat_id=str(raw.get("external_chat_id") or ""),
            external_thread_id=str(raw.get("external_thread_id") or ""),
            display_name=str(raw.get("display_name") or "")[:200],
            bound_session_id=bound_session_id,
            allowed_session_ids=allowed_session_ids,
            permissions=tuple(
                str(item)
                for item in raw.get("permissions") or ("chat", "status")
                if str(item) in _IDENTITY_PERMISSIONS
            ),
            locale=(
                str(raw.get("locale") or "").strip().lower()
                if str(raw.get("locale") or "").strip().lower() in SUPPORTED_LOCALES
                else ""
            ),
            paired_at=float(raw.get("paired_at") or 0),
            last_seen_at=float(raw.get("last_seen_at") or 0),
        )

    def list_channels(self, *, public: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for raw in self._read().get("channels") or []:
                try:
                    config = self._config(raw)
                except ValueError:
                    continue
                rows.append(config.to_public_dict() if public else config.to_dict())
            rows.sort(key=lambda item: (str(item.get("provider")), str(item.get("name"))))
            return rows

    def get_channel(self, channel_id: str) -> ChannelConfig:
        safe_id = str(channel_id or "").strip().lower()
        with self._lock:
            for raw in self._read().get("channels") or []:
                if str(raw.get("id") or "") == safe_id:
                    return self._config(raw)
        raise ValueError("channel not found")

    def upsert_channel(
        self,
        value: dict[str, Any],
        *,
        secret_values: dict[str, Any] | None = None,
    ) -> ChannelConfig:
        provider = str(value.get("provider") or "").strip().lower()
        if provider not in CHANNEL_PROVIDERS:
            raise ValueError(f"unsupported channel provider: {provider}")
        requested_id = str(value.get("id") or "").strip().lower()
        channel_id = requested_id or f"{provider}-{uuid.uuid4().hex[:10]}"
        if not _ID_RE.fullmatch(channel_id):
            raise ValueError("invalid channel id")
        now = time.time()
        with self._lock:
            payload = self._read()
            rows = list(payload.get("channels") or [])
            existing = next(
                (dict(row) for row in rows if str(row.get("id") or "") == channel_id),
                None,
            )
            secret_names = set(existing.get("secret_names") or []) if existing else set()
            if secret_values is not None:
                secret_names = set(self.secrets.set_many(channel_id, secret_values))
            config = self._config({
                "id": channel_id,
                "provider": provider,
                "name": value.get("name") or (existing or {}).get("name") or provider.title(),
                "enabled": value.get("enabled", (existing or {}).get("enabled", False)),
                "transport": value.get("transport") or (existing or {}).get("transport"),
                "settings": value.get("settings", (existing or {}).get("settings") or {}),
                "secret_names": sorted(secret_names),
                "created_at": (existing or {}).get("created_at") or now,
                "updated_at": now,
            })
            rows = [row for row in rows if str(row.get("id") or "") != channel_id]
            rows.append(config.to_dict())
            payload["version"] = 1
            payload["channels"] = rows
            payload.setdefault("identities", [])
            self._write(payload)
            return config

    def set_enabled(self, channel_id: str, enabled: bool) -> ChannelConfig:
        config = self.get_channel(channel_id)
        return self.upsert_channel({
            **config.to_dict(),
            "enabled": bool(enabled),
        })

    def delete_channel(self, channel_id: str) -> None:
        safe_id = str(channel_id or "").strip().lower()
        with self._lock:
            payload = self._read()
            before = len(payload.get("channels") or [])
            payload["channels"] = [
                row for row in payload.get("channels") or []
                if str(row.get("id") or "") != safe_id
            ]
            if len(payload["channels"]) == before:
                raise ValueError("channel not found")
            payload["identities"] = [
                row for row in payload.get("identities") or []
                if str(row.get("channel_id") or "") != safe_id
            ]
            self._write(payload)
            self.secrets.delete_namespace(safe_id)

    def credentials(self, channel_id: str) -> dict[str, str]:
        self.get_channel(channel_id)
        return self.secrets.get_many(channel_id)

    def claim_event(
        self,
        event: ChannelEvent,
        *,
        ttl_seconds: float = 7 * 24 * 60 * 60,
        max_entries: int = 5000,
    ) -> bool:
        """Atomically claim one provider event so retries cannot repeat a turn."""
        parts = (
            event.channel_id,
            event.provider,
            event.external_user_id,
            event.external_chat_id,
            event.external_thread_id,
            event.message_id,
        )
        event_key = hashlib.sha256(
            "\x1f".join(str(item or "") for item in parts).encode("utf-8")
        ).hexdigest()
        now = time.time()
        cutoff = now - max(60.0, float(ttl_seconds))
        with self._lock:
            try:
                payload = json.loads(self.events_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                payload = {"version": 1, "events": []}
            rows = [
                row for row in list(payload.get("events") or [])
                if isinstance(row, dict) and float(row.get("timestamp") or 0) >= cutoff
            ]
            if any(hmac.compare_digest(str(row.get("key") or ""), event_key) for row in rows):
                return False
            rows.append({"key": event_key, "timestamp": now})
            rows = rows[-max(100, min(int(max_entries), 50_000)):]
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.events_path.with_suffix(
                f".{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(
                json.dumps({"version": 1, "events": rows}, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.events_path)
            return True

    def _read_action_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.actions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"version": 1, "actions": [], "groups": []}
        if not isinstance(payload, dict):
            payload = {"version": 1, "actions": [], "groups": []}
        payload.setdefault("actions", [])
        payload.setdefault("groups", [])
        return payload

    def _write_action_state(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.actions_path.with_suffix(
            f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.actions_path)

    def _read_outbox(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"version": 1, "jobs": []}
        if not isinstance(payload, dict):
            payload = {"version": 1, "jobs": []}
        payload.setdefault("jobs", [])
        return payload

    @contextmanager
    def _outbox_process_lock(self) -> Iterator[None]:
        """Serialize outbox read-modify-write cycles across GUI/daemon hosts."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self.outbox_lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_outbox(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.outbox_path.with_suffix(
            f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.outbox_path)

    def enqueue_outbound(
        self,
        *,
        identity_id: str,
        session_id: str,
        text: str = "",
        attachments: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        allow_cross_session: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one proactive delivery for whichever process hosts channels."""
        identity = self.get_identity(identity_id)
        if "send" not in identity.permissions:
            raise ValueError("channel identity does not allow outbound delivery")
        safe_session = str(session_id or "").strip()
        bound_session = str(identity.bound_session_id or "").strip()
        if not safe_session or (
            bound_session != safe_session and not bool(allow_cross_session)
        ):
            raise ValueError("channel identity is bound to a different conversation")
        safe_attachments = [
            json.loads(json.dumps(item, ensure_ascii=False, default=str))
            for item in list(attachments)[:8]
            if isinstance(item, dict)
        ]
        safe_blocks = [
            json.loads(json.dumps(item, ensure_ascii=False, default=str))
            for item in list(blocks)[:24]
            if isinstance(item, dict)
        ]
        if not str(text or "").strip() and not safe_attachments and not safe_blocks:
            raise ValueError("outbound delivery requires text, a card, or an attachment")
        safe_meta: dict[str, Any] = {}
        if isinstance(meta, dict):
            try:
                safe_meta = json.loads(json.dumps(meta, ensure_ascii=False, default=str))
            except Exception:
                safe_meta = {}
        now = time.time()
        job = {
            "id": f"delivery-{uuid.uuid4().hex}",
            "identity_id": identity.id,
            "channel_id": identity.channel_id,
            "session_id": safe_session,
            # Inbound routing remains attached to this conversation even when
            # a separately confirmed desktop turn sends to the same recipient.
            "recipient_session_id": bound_session,
            "text": str(text or ""),
            "attachments": safe_attachments,
            "blocks": safe_blocks,
            # Structured causal provenance frozen at send time (origin session,
            # the instruction that triggered it, etc.), mirrored into recipient
            # history so the receiving Agent never has to guess why it arrived.
            "meta": safe_meta,
            "state": "pending",
            "attempts": 0,
            # Parts already handed to the provider. Retries resume from here so a
            # failed attachment never re-posts the text that already arrived.
            "delivered_parts": 0,
            "created_at": now,
            "updated_at": now,
            "next_attempt_at": now,
            "lease_until": 0.0,
            "last_error": "",
        }
        with self._lock:
            with self._outbox_process_lock():
                state = self._read_outbox()
                jobs = [
                    row for row in list(state.get("jobs") or [])
                    if isinstance(row, dict)
                ]
                jobs.append(job)
                state["jobs"] = jobs[-2000:]
                self._write_outbox(state)
        return dict(job)

    def claim_outbound(
        self,
        *,
        limit: int = 8,
        lease_seconds: float = 90.0,
    ) -> list[dict[str, Any]]:
        """Lease due deliveries so retries survive process restarts."""
        now = time.time()
        claimed: list[dict[str, Any]] = []
        with self._lock:
            with self._outbox_process_lock():
                state = self._read_outbox()
                jobs = [
                    row for row in list(state.get("jobs") or [])
                    if isinstance(row, dict)
                ]
                for job in jobs:
                    current_state = str(job.get("state") or "pending")
                    due = float(job.get("next_attempt_at") or 0) <= now
                    lease_expired = float(job.get("lease_until") or 0) <= now
                    if current_state == "processing" and lease_expired:
                        current_state = "pending"
                        job["state"] = "pending"
                    if current_state != "pending" or not due:
                        continue
                    job["state"] = "processing"
                    job["attempts"] = int(job.get("attempts") or 0) + 1
                    job["lease_until"] = now + max(15.0, float(lease_seconds))
                    job["updated_at"] = now
                    claimed.append(json.loads(json.dumps(job, ensure_ascii=False)))
                    if len(claimed) >= max(1, min(int(limit), 50)):
                        break
                state["jobs"] = jobs
                self._write_outbox(state)
        return claimed

    def record_outbound_progress(self, job_id: str, delivered_parts: int) -> None:
        """Persist how much of a delivery already reached the provider."""
        with self._lock:
            with self._outbox_process_lock():
                state = self._read_outbox()
                jobs = [
                    row for row in list(state.get("jobs") or [])
                    if isinstance(row, dict)
                ]
                for job in jobs:
                    if str(job.get("id") or "") != str(job_id or ""):
                        continue
                    job["delivered_parts"] = max(
                        int(job.get("delivered_parts") or 0),
                        int(delivered_parts),
                    )
                    job["updated_at"] = time.time()
                    break
                state["jobs"] = jobs
                self._write_outbox(state)

    def finish_outbound(
        self,
        job_id: str,
        *,
        error: str = "",
        permanent: bool = False,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Mark a delivery sent or schedule a bounded exponential retry."""
        now = time.time()
        result: dict[str, Any] | None = None
        with self._lock:
            with self._outbox_process_lock():
                state = self._read_outbox()
                jobs = [
                    row for row in list(state.get("jobs") or [])
                    if isinstance(row, dict)
                ]
                for job in jobs:
                    if str(job.get("id") or "") != str(job_id or ""):
                        continue
                    attempts = int(job.get("attempts") or 0)
                    clean_error = str(error or "")[:1000]
                    if not clean_error:
                        job["state"] = "sent"
                        job["sent_at"] = now
                        job["last_error"] = ""
                    elif permanent or attempts >= max(1, int(max_attempts)):
                        job["state"] = "failed"
                        job["last_error"] = clean_error
                    else:
                        job["state"] = "pending"
                        job["last_error"] = clean_error
                        job["next_attempt_at"] = now + min(60.0, 2.0 ** attempts)
                    job["lease_until"] = 0.0
                    job["updated_at"] = now
                    result = dict(job)
                    break
                if result is None:
                    raise ValueError("outbound delivery not found")
                terminal = [
                    row for row in jobs
                    if str(row.get("state") or "") in {"sent", "failed"}
                ][-500:]
                active = [
                    row for row in jobs
                    if str(row.get("state") or "") not in {"sent", "failed"}
                ]
                state["jobs"] = active + terminal
                self._write_outbox(state)
        return result

    def list_outbound(
        self,
        *,
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            with self._outbox_process_lock():
                jobs = [
                    dict(row) for row in self._read_outbox().get("jobs") or []
                    if isinstance(row, dict)
                    and (
                        not session_id
                        or str(row.get("session_id") or "") == str(session_id)
                    )
                ]
        jobs.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
        return jobs[:max(1, min(int(limit), 500))]

    @staticmethod
    def _prune_action_state(payload: dict[str, Any], now: float) -> None:
        payload["actions"] = [
            row for row in list(payload.get("actions") or [])
            if isinstance(row, dict) and float(row.get("expires_at") or 0) > now
        ][-5000:]
        payload["groups"] = [
            row for row in list(payload.get("groups") or [])
            if isinstance(row, dict) and float(row.get("expires_at") or 0) > now
        ][-500:]

    def issue_action(
        self,
        *,
        channel_id: str,
        identity_id: str,
        session_id: str,
        payload: dict[str, Any],
        ttl_seconds: float = 900,
    ) -> str:
        """Create a short opaque callback token; only its digest is persisted."""
        token = "ca_" + secrets.token_urlsafe(18)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = time.time()
        expires_at = now + max(30.0, min(float(ttl_seconds), 3600.0))
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, now)
            state["actions"].append({
                "digest": digest,
                "channel_id": str(channel_id or ""),
                "identity_id": str(identity_id or ""),
                "session_id": str(session_id or ""),
                "payload": json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
                "created_at": now,
                "expires_at": expires_at,
            })
            self._write_action_state(state)
        return token

    def consume_action(
        self,
        token: str,
        *,
        channel_id: str,
        identity_id: str,
    ) -> dict[str, Any]:
        """Atomically consume one callback token and enforce its identity binding."""
        raw_token = str(token or "").strip()
        if not raw_token.startswith("ca_") or len(raw_token) > 64:
            raise ChannelError("action_invalid", "channel action is invalid or expired")
        digest = hashlib.sha256(raw_token.encode("ascii", errors="ignore")).hexdigest()
        now = time.time()
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, now)
            match_index = next(
                (
                    index for index, row in enumerate(state["actions"])
                    if hmac.compare_digest(str(row.get("digest") or ""), digest)
                ),
                -1,
            )
            if match_index < 0:
                self._write_action_state(state)
                raise ChannelError("action_invalid", "channel action is invalid or expired")
            match = state["actions"][match_index]
            if (
                str(match.get("channel_id") or "") != str(channel_id or "")
                or str(match.get("identity_id") or "") != str(identity_id or "")
            ):
                self._write_action_state(state)
                raise ChannelError(
                    "action_other_identity",
                    "channel action belongs to another identity",
                )
            state["actions"].pop(match_index)
            self._write_action_state(state)
        result = dict(match.get("payload") or {})
        result["session_id"] = str(match.get("session_id") or "")
        return result

    def create_action_group(
        self,
        *,
        channel_id: str,
        identity_id: str,
        session_id: str,
        title: str,
        questions: list[dict[str, Any]],
        ttl_seconds: float = 900,
    ) -> str:
        group_id = "cg_" + uuid.uuid4().hex
        now = time.time()
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, now)
            state["groups"].append({
                "id": group_id,
                "channel_id": str(channel_id or ""),
                "identity_id": str(identity_id or ""),
                "session_id": str(session_id or ""),
                "title": str(title or "")[:160],
                "questions": json.loads(json.dumps(questions, ensure_ascii=False, default=str)),
                "answers": {},
                "expires_at": now + max(30.0, min(float(ttl_seconds), 3600.0)),
            })
            self._write_action_state(state)
        return group_id

    def set_action_group_answer(
        self,
        group_id: str,
        *,
        channel_id: str,
        identity_id: str,
        question_id: str,
        value: str,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, time.time())
            group = next(
                (row for row in state["groups"] if str(row.get("id") or "") == group_id),
                None,
            )
            self._validate_action_group(group, channel_id, identity_id)
            valid = {
                str(item.get("id") or ""): {
                    str(option) for option in item.get("options") or []
                }
                for item in group.get("questions") or []
                if isinstance(item, dict)
            }
            if question_id not in valid or value not in valid[question_id]:
                raise ChannelError("batch_invalid_option", "invalid batch decision option")
            answers = dict(group.get("answers") or {})
            answers[question_id] = value
            group["answers"] = answers
            self._write_action_state(state)
            return dict(group)

    def get_action_group(
        self,
        group_id: str,
        *,
        channel_id: str,
        identity_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, time.time())
            group = next(
                (row for row in state["groups"] if str(row.get("id") or "") == group_id),
                None,
            )
            self._validate_action_group(group, channel_id, identity_id)
            self._write_action_state(state)
            return dict(group)

    def consume_action_group(
        self,
        group_id: str,
        *,
        channel_id: str,
        identity_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_action_state()
            self._prune_action_state(state, time.time())
            group = next(
                (row for row in state["groups"] if str(row.get("id") or "") == group_id),
                None,
            )
            self._validate_action_group(group, channel_id, identity_id)
            answers = dict(group.get("answers") or {})
            missing = [
                str(item.get("id") or "")
                for item in group.get("questions") or []
                if isinstance(item, dict)
                and bool(item.get("required", True))
                and str(item.get("id") or "") not in answers
            ]
            if missing:
                raise ChannelError(
                    "batch_incomplete",
                    "batch decision is incomplete: " + ", ".join(missing),
                    missing=", ".join(missing),
                )
            state["groups"] = [
                row for row in state["groups"]
                if str(row.get("id") or "") != group_id
            ]
            self._write_action_state(state)
            return dict(group)

    @staticmethod
    def _validate_action_group(
        group: dict[str, Any] | None,
        channel_id: str,
        identity_id: str,
    ) -> None:
        if group is None:
            raise ChannelError("group_invalid", "channel action group is invalid or expired")
        if (
            str(group.get("channel_id") or "") != str(channel_id or "")
            or str(group.get("identity_id") or "") != str(identity_id or "")
        ):
            raise ChannelError(
                "group_other_identity",
                "channel action group belongs to another identity",
            )

    def create_pairing(
        self,
        channel_id: str,
        *,
        bound_session_id: str = "",
        ttl_seconds: float = 300,
    ) -> dict[str, Any]:
        channel = self.get_channel(channel_id)
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(24)
        digest = hashlib.sha256(salt + code.encode("ascii")).digest()
        pairing_id = f"pair-{uuid.uuid4().hex}"
        expires_at = time.time() + max(30.0, min(float(ttl_seconds), 1800.0))
        with self._lock:
            pairings = self._read_pairings()
            pairings[pairing_id] = {
                "channel_id": channel.id,
                "provider": channel.provider,
                "salt": salt,
                "digest": digest,
                "bound_session_id": str(bound_session_id or ""),
                "expires_at": expires_at,
            }
            self._write_pairings(pairings)
        return {
            "pairing_id": pairing_id,
            "channel_id": channel.id,
            "code": code,
            "expires_at": expires_at,
        }

    def consume_pairing(
        self,
        channel_id: str,
        code: str,
        *,
        external_user_id: str,
        external_chat_id: str = "",
        external_thread_id: str = "",
        display_name: str = "",
    ) -> ChannelIdentity:
        safe_code = str(code or "").strip()
        user_id = str(external_user_id or "").strip()
        if not _PAIR_CODE_RE.fullmatch(safe_code) or not user_id:
            raise ChannelError(
                "pairing_invalid_input",
                "invalid pairing code or external user id",
            )
        now = time.time()
        with self._lock:
            pairings = self._read_pairings()
            match_id = ""
            match: dict[str, Any] | None = None
            for pairing_id, pairing in list(pairings.items()):
                if float(pairing.get("expires_at") or 0) <= now:
                    pairings.pop(pairing_id, None)
                    continue
                digest = hashlib.sha256(
                    pairing["salt"] + safe_code.encode("ascii")
                ).digest()
                if (
                    pairing.get("channel_id") == channel_id
                    and hmac.compare_digest(digest, pairing["digest"])
                ):
                    match_id = pairing_id
                    match = pairing
                    break
            if match is None:
                self._write_pairings(pairings)
                raise ChannelError("pairing_invalid", "pairing code is invalid or expired")
            pairings.pop(match_id, None)
            self._write_pairings(pairings)
            payload = self._read()
            bound_session_id = str(match.get("bound_session_id") or "")
            identity = ChannelIdentity(
                id=f"identity-{uuid.uuid4().hex}",
                channel_id=str(match["channel_id"]),
                provider=str(match["provider"]),  # type: ignore[arg-type]
                external_user_id=user_id,
                external_chat_id=str(external_chat_id or ""),
                external_thread_id=str(external_thread_id or ""),
                display_name=str(display_name or "")[:200],
                bound_session_id=bound_session_id,
                allowed_session_ids=_normalize_session_ids((bound_session_id,)),
                permissions=_DEFAULT_IDENTITY_PERMISSIONS,
                paired_at=now,
                last_seen_at=now,
            )
            identities = [
                row for row in payload.get("identities") or []
                if not (
                    str(row.get("channel_id") or "") == identity.channel_id
                    and str(row.get("external_user_id") or "") == user_id
                    and str(row.get("external_chat_id") or "") == identity.external_chat_id
                    and str(row.get("external_thread_id") or "") == identity.external_thread_id
                )
            ]
            identities.append(identity.to_dict())
            payload["identities"] = identities
            self._write(payload)
            return identity

    def list_identities(self, channel_id: str = "") -> list[ChannelIdentity]:
        with self._lock:
            identities = []
            for raw in self._read().get("identities") or []:
                try:
                    identity = self._identity(raw)
                except ValueError:
                    continue
                if channel_id and identity.channel_id != channel_id:
                    continue
                identities.append(identity)
            return sorted(identities, key=lambda item: item.paired_at, reverse=True)

    def get_identity(self, identity_id: str) -> ChannelIdentity:
        safe_id = str(identity_id or "").strip()
        for identity in self.list_identities():
            if identity.id == safe_id:
                return identity
        raise ValueError("identity not found")

    def touch_identity(self, identity_id: str) -> ChannelIdentity:
        """Persist recent source activity used for safe default delivery routing."""
        with self._lock:
            payload = self._read()
            updated: ChannelIdentity | None = None
            rows = []
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                if identity.id == str(identity_id or ""):
                    identity = replace(identity, last_seen_at=time.time())
                    updated = identity
                rows.append(identity.to_dict())
            if updated is None:
                raise ValueError("identity not found")
            payload["identities"] = rows
            self._write(payload)
            return updated

    def list_identities_for_session(self, session_id: str) -> list[ChannelIdentity]:
        safe_session = str(session_id or "").strip()
        if not safe_session:
            return []
        return [
            identity for identity in self.list_identities()
            if identity.bound_session_id == safe_session
        ]

    def find_identity(
        self,
        channel_id: str,
        external_user_id: str,
        external_chat_id: str = "",
        external_thread_id: str = "",
    ) -> ChannelIdentity | None:
        for identity in self.list_identities(channel_id):
            if (
                identity.external_user_id == str(external_user_id or "")
                and identity.external_chat_id == str(external_chat_id or "")
                and identity.external_thread_id == str(external_thread_id or "")
            ):
                return identity
        return None

    def bind_identity(self, identity_id: str, session_id: str) -> ChannelIdentity:
        with self._lock:
            payload = self._read()
            updated: ChannelIdentity | None = None
            rows = []
            bound = str(session_id or "")
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                if identity.id == identity_id:
                    identity = replace(
                        identity,
                        bound_session_id=bound,
                        allowed_session_ids=_normalize_session_ids(
                            identity.allowed_session_ids,
                            (identity.bound_session_id, bound),
                        ),
                    )
                    updated = identity
                rows.append(identity.to_dict())
            if updated is None:
                raise ValueError("identity not found")
            payload["identities"] = rows
            self._write(payload)
            return updated

    def grant_identity_session(self, identity_id: str, session_id: str) -> ChannelIdentity:
        """Admit a conversation to this pairing without changing the current bind."""
        granted = str(session_id or "").strip()
        if not granted:
            raise ValueError("session_id is required")
        with self._lock:
            payload = self._read()
            updated: ChannelIdentity | None = None
            rows = []
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                if identity.id == identity_id:
                    identity = replace(
                        identity,
                        allowed_session_ids=_normalize_session_ids(
                            identity.allowed_session_ids,
                            (identity.bound_session_id, granted),
                        ),
                    )
                    updated = identity
                rows.append(identity.to_dict())
            if updated is None:
                raise ValueError("identity not found")
            payload["identities"] = rows
            self._write(payload)
            return updated

    def unbind_session(self, session_id: str) -> int:
        safe_session = str(session_id or "")
        if not safe_session:
            return 0
        with self._lock:
            payload = self._read()
            rows = []
            count = 0
            changed = False
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                allowed = tuple(
                    item for item in identity.allowed_session_ids
                    if item != safe_session
                )
                if identity.bound_session_id == safe_session:
                    identity = replace(
                        identity,
                        bound_session_id="",
                        allowed_session_ids=allowed,
                    )
                    count += 1
                    changed = True
                elif allowed != identity.allowed_session_ids:
                    identity = replace(identity, allowed_session_ids=allowed)
                    changed = True
                rows.append(identity.to_dict())
            if changed:
                payload["identities"] = rows
                self._write(payload)
            return count

    def set_identity_locale(self, identity_id: str, locale: str) -> ChannelIdentity:
        """Persist one paired user's preferred reply language."""
        normalized = normalize_locale(locale)
        with self._lock:
            payload = self._read()
            updated: ChannelIdentity | None = None
            rows = []
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                if identity.id == str(identity_id or ""):
                    identity = replace(identity, locale=normalized)
                    updated = identity
                rows.append(identity.to_dict())
            if updated is None:
                raise ValueError("identity not found")
            payload["identities"] = rows
            self._write(payload)
            return updated

    def set_identity_permissions(
        self,
        identity_id: str,
        permissions: list[str] | tuple[str, ...],
    ) -> ChannelIdentity:
        normalized = tuple(
            name
            for name in ("chat", "status", "cancel", "confirm", "configure", "send")
            if name in {str(item) for item in permissions}
        )
        with self._lock:
            payload = self._read()
            updated: ChannelIdentity | None = None
            rows = []
            for raw in payload.get("identities") or []:
                identity = self._identity(raw)
                if identity.id == identity_id:
                    identity = replace(identity, permissions=normalized)
                    updated = identity
                rows.append(identity.to_dict())
            if updated is None:
                raise ValueError("identity not found")
            payload["identities"] = rows
            self._write(payload)
            return updated

    def delete_identity(self, identity_id: str) -> None:
        with self._lock:
            payload = self._read()
            before = len(payload.get("identities") or [])
            payload["identities"] = [
                row for row in payload.get("identities") or []
                if str(row.get("id") or "") != str(identity_id or "")
            ]
            if len(payload["identities"]) == before:
                raise ValueError("identity not found")
            self._write(payload)
