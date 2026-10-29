"""Event sources for assistant cron: signed webhooks and file mtime polling."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from argusai.core.channels.secret_store import ChannelSecretStore

from .cron import CronStore, _watch_state_path, validate_cron_job_id

log = logging.getLogger("argusai.assistant.cron.triggers")

MAX_WEBHOOK_BODY_BYTES = 24_000
WEBHOOK_REPLAY_WINDOW_SECONDS = 300
MAX_WATCH_FILES = 10_000
MAX_CHANGE_ROWS = 100


def _secret_store(root: Path) -> ChannelSecretStore:
    return ChannelSecretStore(root / "webhook_secrets")


def ensure_webhook_secret(root: Path, job_id: str) -> tuple[str, bool]:
    jid = validate_cron_job_id(job_id)
    store = _secret_store(root)
    current = store.get_many(jid).get("signing_secret", "")
    if current:
        return current, False
    secret = secrets.token_urlsafe(32)
    store.set_many(jid, {"signing_secret": secret})
    return secret, True


def webhook_secret_configured(root: Path, job_id: str) -> bool:
    jid = validate_cron_job_id(job_id)
    return bool(_secret_store(root).get_many(jid).get("signing_secret"))


def rotate_webhook_secret(root: Path, job_id: str) -> str:
    jid = validate_cron_job_id(job_id)
    secret = secrets.token_urlsafe(32)
    _secret_store(root).set_many(jid, {"signing_secret": secret})
    return secret


def delete_webhook_secret(root: Path, job_id: str) -> None:
    jid = validate_cron_job_id(job_id)
    _secret_store(root).delete_namespace(jid)


def webhook_url(host: str, port: int, job_id: str) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{display_host}:{port}/api/cron/webhook/{job_id}"


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    signed = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


class _CronWebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CronWebhookServer:
    """One signed listener for all webhook-triggered jobs in one data root."""

    def __init__(self, store: CronStore, *, host: str, port: int) -> None:
        self.store = store
        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self._server: _CronWebhookHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def listen_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    def status(self) -> dict[str, Any]:
        return {
            "running": self._server is not None,
            "host": self.host,
            "port": self.listen_port,
            "loopback": self.host in {"127.0.0.1", "localhost", "::1"},
        }

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArgusAICronWebhook/1"

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("cron.webhook " + fmt, *args)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlsplit(self.path)
                prefix = "/api/cron/webhook/"
                if not parsed.path.startswith(prefix):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                job_id = unquote(parsed.path[len(prefix):]).strip("/")
                try:
                    job_id = validate_cron_job_id(job_id)
                except ValueError:
                    self._json(404, {"ok": False, "error": "job not found"})
                    return

                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length < 0 or length > MAX_WEBHOOK_BODY_BYTES:
                    self._json(413, {"ok": False, "error": "payload too large"})
                    return
                body = self.rfile.read(length)
                timestamp = str(self.headers.get("X-ArgusAI-Timestamp") or "").strip()
                received = str(self.headers.get("X-ArgusAI-Signature") or "").strip()
                received = received.removeprefix("sha256=")
                try:
                    request_time = float(timestamp)
                except ValueError:
                    request_time = 0
                if (
                    not request_time
                    or abs(time.time() - request_time) > WEBHOOK_REPLAY_WINDOW_SECONDS
                ):
                    self._json(401, {"ok": False, "error": "stale or missing timestamp"})
                    return
                secret, _created = ensure_webhook_secret(owner.store.root, job_id)
                expected = webhook_signature(secret, timestamp, body)
                if not received or not hmac.compare_digest(received, expected):
                    self._json(401, {"ok": False, "error": "invalid signature"})
                    return

                try:
                    decoded = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = {"text": body.decode("utf-8", errors="replace")}
                if not isinstance(decoded, dict):
                    decoded = {"value": decoded}
                event_id = str(
                    self.headers.get("X-ArgusAI-Event-Id")
                    or decoded.get("event_id")
                    or ""
                ).strip()
                try:
                    job = owner.store.get_job(job_id)
                    debounce = int(
                        ((job or {}).get("schedule") or {}).get("debounce_seconds")
                        or 0
                    )
                    result = owner.store.enqueue_event(
                        job_id,
                        source="webhook",
                        payload={
                            "body": decoded,
                            "received_at": time.time(),
                        },
                        event_id=event_id,
                        ready_at=time.time() + max(0, debounce),
                        coalesce=debounce > 0,
                    )
                except OverflowError:
                    self._json(429, {"ok": False, "error": "trigger queue is full"})
                    return
                except ValueError as exc:
                    status = 409 if "disabled" in str(exc) else 404
                    self._json(status, {"ok": False, "error": str(exc)})
                    return
                self._json(
                    200 if result.get("duplicate") else 202,
                    {
                        "ok": True,
                        "duplicate": bool(result.get("duplicate")),
                        "event_id": result.get("event_id"),
                        "queued": result.get("queued"),
                    },
                )

        self._server = _CronWebhookHTTPServer((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="argusai-cron-webhook",
            daemon=True,
        )
        self._thread.start()
        log.info("cron.webhook_listening host=%s port=%s", self.host, self.port)

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)


class FileChangePoller:
    """Cross-platform file/directory watcher based on durable mtime snapshots."""

    def __init__(self, store: CronStore) -> None:
        self.store = store
        self._last_poll: dict[str, float] = {}

    @staticmethod
    def _snapshot(schedule: dict[str, Any]) -> dict[str, list[int]]:
        target = Path(str(schedule.get("path") or "")).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"watch path not found: {target}")
        files: list[Path]
        if target.is_file():
            files = [target]
        else:
            pattern = str(schedule.get("glob") or "*")
            iterator = (
                target.rglob(pattern)
                if bool(schedule.get("recursive", True))
                else target.glob(pattern)
            )
            files = []
            for item in iterator:
                if not item.is_file():
                    continue
                files.append(item)
                if len(files) > MAX_WATCH_FILES:
                    raise ValueError(
                        f"file watch exceeds {MAX_WATCH_FILES} matching files"
                    )
        snapshot: dict[str, list[int]] = {}
        for item in files:
            try:
                stat = item.stat()
            except OSError:
                continue
            snapshot[os.path.normcase(str(item.resolve()))] = [
                int(stat.st_mtime_ns),
                int(stat.st_size),
            ]
        return snapshot

    def _load_baseline(self, job_id: str) -> dict[str, list[int]] | None:
        path = _watch_state_path(self.store.root, job_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        baseline = raw.get("baseline") if isinstance(raw, dict) else None
        if not isinstance(baseline, dict):
            return None
        return {
            str(key): [int(value[0]), int(value[1])]
            for key, value in baseline.items()
            if isinstance(value, list) and len(value) == 2
        }

    def _save_baseline(
        self,
        job_id: str,
        baseline: dict[str, list[int]],
    ) -> None:
        path = _watch_state_path(self.store.root, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store._atomic_write_json(path, {
            "updated_at": time.time(),
            "baseline": baseline,
        })

    def initialize_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Capture the save-time baseline so creation itself never fires."""
        jid = validate_cron_job_id(str(job.get("id") or ""))
        schedule = (
            dict(job.get("schedule") or {})
            if isinstance(job.get("schedule"), dict)
            else {}
        )
        if schedule.get("kind") != "file_change":
            return {"ok": False, "error": "job is not a file-change trigger"}
        try:
            baseline = self._snapshot(schedule)
            self._save_baseline(jid, baseline)
        except Exception as exc:
            summary = f"File watch failed: {exc}"[:2000]
            self.store.update_job_fields(
                jid,
                last_status="watch_error",
                last_summary=summary,
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "files": len(baseline)}

    @staticmethod
    def _changes(
        before: dict[str, list[int]],
        after: dict[str, list[int]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path)
            new = after.get(path)
            if old == new:
                continue
            kind = "created" if old is None else "deleted" if new is None else "modified"
            rows.append({
                "path": path,
                "kind": kind,
                "mtime_ns": int((new or old or [0])[0]),
                "size": int((new or old or [0, 0])[1]),
            })
            if len(rows) >= MAX_CHANGE_ROWS:
                break
        return rows

    def poll_once(self) -> list[dict[str, Any]]:
        now = time.time()
        queued: list[dict[str, Any]] = []
        for job in self.store.list_jobs():
            if not job.get("enabled"):
                continue
            schedule = (
                dict(job.get("schedule") or {})
                if isinstance(job.get("schedule"), dict)
                else {}
            )
            if schedule.get("kind") != "file_change":
                continue
            jid = str(job.get("id") or "")
            poll_seconds = max(1, int(schedule.get("poll_seconds") or 2))
            if now - self._last_poll.get(jid, 0) < poll_seconds:
                continue
            self._last_poll[jid] = now
            try:
                current = self._snapshot(schedule)
                baseline = self._load_baseline(jid)
                if baseline is None:
                    self._save_baseline(jid, current)
                    continue
                changes = self._changes(baseline, current)
                if not changes:
                    continue
                debounce = max(0, int(schedule.get("debounce_seconds") or 0))
                result = self.store.enqueue_event(
                    jid,
                    source="file_change",
                    payload={
                        "watch_path": schedule.get("path"),
                        "glob": schedule.get("glob"),
                        "changes": changes,
                    },
                    ready_at=now + debounce,
                    coalesce=True,
                )
                self._save_baseline(jid, current)
                queued.append(result)
                self.store.update_job_fields(
                    jid,
                    last_status="queued",
                    last_summary=f"{len(changes)} file change(s) queued",
                )
            except Exception as exc:
                summary = f"File watch failed: {exc}"[:2000]
                current_job = self.store.get_job(jid) or {}
                if current_job.get("last_summary") != summary:
                    self.store.update_job_fields(
                        jid,
                        last_status="watch_error",
                        last_summary=summary,
                    )
        return queued
