"""Built-in communication transport adapters."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import websockets
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .cards import card_text, feishu_card_has_body, render_channel_reply
from .markdown import PLAIN, render_markdown
from .stream_sidecar import feishu_inbound_text, feishu_media_refs
from .media import (
    FORWARD_MEDIA_MAX_BYTES,
    FORWARD_MEDIA_MAX_COUNT,
    MAX_ATTACHMENTS,
    TEXT_LIMIT,
    ChannelMediaError,
    attachment_bytes,
    attachment_from_bytes,
    attachment_limit,
    detect_kind,
    download_attachment,
    download_attachment_async,
    normalize_attachment,
    normalize_attachments,
    pack_forwarded_media,
    prepare_forwarded_text,
    safe_filename,
    to_assistant_dict,
)
from .models import (
    ChannelAttachment,
    ChannelConfig,
    ChannelDeliveryError,
    ChannelEvent,
    ChannelReply,
    infer_chat_kind,
)

log = logging.getLogger(__name__)
EventCallback = Callable[[ChannelEvent], None]

# Substrings every provider uses for "this app may not do that", which no
# amount of retrying will change.
_PERMANENT_MARKERS = (
    "access denied",
    "forbidden",
    "insufficient",
    "invalid credentials",
    "invalid token",
    "missing access",
    "not authorized",
    "permission",
    "scope",
    "unauthorized",
)


def _api_payload(response: httpx.Response) -> tuple[str, dict[str, Any]]:
    try:
        parsed = response.json()
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False, default=str)[:600], parsed
    return str(getattr(response, "text", "") or "")[:600], {}


def _api_message(payload: dict[str, Any], snippet: str) -> str:
    for key in ("msg", "errmsg", "description", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return snippet


def delivery_error(action: str, detail: str, status: int = 0) -> ChannelDeliveryError:
    """Build a delivery error, classifying scope/auth failures as permanent."""

    lowered = str(detail or "").lower()
    permanent = status in {401, 403} or any(
        marker in lowered for marker in _PERMANENT_MARKERS
    )
    return ChannelDeliveryError(f"{action}: {detail}"[:900], permanent=permanent)


def check_api_response(response: httpx.Response, action: str) -> dict[str, Any]:
    """Return the decoded body, or raise an error that names the real cause.

    ``raise_for_status`` throws away the response body, which is exactly where
    every provider puts the reason (missing scope, bad chat id, oversized file).
    """

    snippet, payload = _api_payload(response)
    try:
        code = int(payload.get("code", payload.get("errcode", 0)) or 0)
    except (TypeError, ValueError):
        code = 0
    status = int(getattr(response, "status_code", 200) or 200)
    http_failed = status >= 400
    if not http_failed and code == 0 and payload.get("ok") is not False:
        return payload
    message = _api_message(payload, snippet)
    detail = f"[{code}] {message}" if code else message
    if http_failed:
        detail = f"HTTP {status} {detail}".strip()
    raise delivery_error(action, detail, status)


def _attachment_kind(mime: str, name: str = "") -> str:
    return detect_kind(mime, name)


def _telegram_inbound_text(message: dict[str, Any]) -> str:
    """Plain text Telegram still understands when the bubble is not ``text``."""
    text = str(message.get("text") or message.get("caption") or "").strip()
    if text:
        return text
    sticker = message.get("sticker")
    if isinstance(sticker, dict):
        emoji = str(sticker.get("emoji") or "").strip()
        if emoji:
            return emoji
    location = message.get("location") if isinstance(message.get("location"), dict) else {}
    if location and location.get("latitude") is not None:
        venue = message.get("venue") if isinstance(message.get("venue"), dict) else {}
        title = str(venue.get("title") or venue.get("address") or "").strip()
        if title:
            return title
        return f"{location.get('latitude')}, {location.get('longitude')}"
    contact = message.get("contact") if isinstance(message.get("contact"), dict) else {}
    if contact:
        name = " ".join(
            str(contact.get(key) or "")
            for key in ("first_name", "last_name")
        ).strip()
        phone = str(contact.get("phone_number") or "").strip()
        return " ".join(part for part in (name, phone) if part)
    poll = message.get("poll") if isinstance(message.get("poll"), dict) else {}
    question = str(poll.get("question") or "").strip()
    if question:
        return question
    dice = message.get("dice") if isinstance(message.get("dice"), dict) else {}
    emoji = str(dice.get("emoji") or "").strip()
    if emoji:
        value = dice.get("value")
        return f"{emoji} {value}".strip() if value is not None else emoji
    return ""


_SLACK_SKIP_SUBTYPES = frozenset({
    "bot_message",
    "channel_archive",
    "channel_join",
    "channel_leave",
    "channel_name",
    "channel_purpose",
    "channel_topic",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "message_changed",
    "message_deleted",
    "message_replied",
    "pinned_item",
    "unpinned_item",
})


def _slack_blocks_text(blocks: Any) -> str:
    lines: list[str] = []
    if not isinstance(blocks, list):
        return ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            value = str(text_obj.get("text") or "").strip()
            if value:
                lines.append(value)
        for field in list(block.get("fields") or []):
            if isinstance(field, dict):
                value = str(field.get("text") or "").strip()
                if value:
                    lines.append(value)
        for element in list(block.get("elements") or []):
            if isinstance(element, dict) and element.get("type") == "text":
                value = str(element.get("text") or "").strip()
                if value:
                    lines.append(value)
    return "\n".join(lines)


def _discord_inbound_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    content = str(message.get("content") or "").strip()
    if content:
        parts.append(content)
    for embed in list(message.get("embeds") or []):
        if not isinstance(embed, dict):
            continue
        for key in ("title", "description"):
            value = str(embed.get(key) or "").strip()
            if value:
                parts.append(value)
        url = str(embed.get("url") or "").strip()
        if url:
            parts.append(url)
    names: list[str] = []
    for item in list(message.get("sticker_items") or message.get("stickers") or []):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    if names:
        parts.append(" ".join(f":{name}:" for name in names))
    return "\n".join(parts)


def _wecom_inbound_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("Content") or payload.get("content") or "").strip()
    if text:
        return text
    message_type = str(payload.get("MsgType") or payload.get("msg_type") or "").lower()
    if message_type == "location":
        label = str(payload.get("Label") or payload.get("label") or "").strip()
        if label:
            return label
        latitude = payload.get("Location_X") or payload.get("location_x")
        longitude = payload.get("Location_Y") or payload.get("location_y")
        if latitude is not None and longitude is not None:
            return f"{latitude}, {longitude}"
        return "[location]"
    if message_type == "link":
        return "\n".join(
            part
            for part in (
                str(payload.get("Title") or payload.get("title") or "").strip(),
                str(payload.get("Description") or payload.get("description") or "").strip(),
                str(payload.get("Url") or payload.get("url") or "").strip(),
            )
            if part
        )
    return ""


def _attachment_limit(kind: str) -> int:
    return attachment_limit(kind)


def _attachment_from_bytes(
    data: bytes,
    *,
    mime: str,
    name: str,
) -> dict[str, Any]:
    try:
        return to_assistant_dict(attachment_from_bytes(data, mime=mime, name=name))
    except ChannelMediaError as exc:
        return _attachment_error(name, str(exc))


def _attachment_error(name: str, reason: str) -> dict[str, Any]:
    """Represent a rejected provider file as visible, inert assistant text."""
    display_name = safe_filename(name, "attachment")
    message = f"Attachment '{display_name}' was not received: {reason}"
    return to_assistant_dict(attachment_from_bytes(
        message.encode("utf-8"),
        mime="text/plain",
        name=f"{display_name}.error.txt",
    ))


def _download_attachment(
    client: httpx.Client,
    url: str,
    *,
    mime: str,
    name: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        return to_assistant_dict(download_attachment(
            client,
            url,
            mime=mime,
            name=name,
            headers=headers,
        ))
    except ChannelMediaError as exc:
        return _attachment_error(name, str(exc))
    except Exception as exc:
        return _attachment_error(name, f"provider download failed ({type(exc).__name__})")


async def _download_attachment_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    mime: str,
    name: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        return to_assistant_dict(await download_attachment_async(
            client,
            url,
            mime=mime,
            name=name,
            headers=headers,
        ))
    except ChannelMediaError as exc:
        return _attachment_error(name, str(exc))
    except Exception as exc:
        return _attachment_error(name, f"provider download failed ({type(exc).__name__})")


def _prepared_attachments(reply: ChannelReply) -> list[tuple[ChannelAttachment, bytes]]:
    normalized = normalize_attachments(reply.attachments)
    return [(attachment, attachment_bytes(attachment)) for attachment in normalized]


class ChannelAdapter(ABC):
    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        self.config = config
        self.credentials = dict(credentials)
        self._on_event: EventCallback | None = None
        self._stop = threading.Event()
        self._last_error = ""
        self._connected = False

        from .streaming import OutputStreams
        self._output_streams = OutputStreams(self)

    def status(self) -> dict[str, Any]:
        return {
            "channel_id": self.config.id,
            "provider": self.config.provider,
            "connected": self._connected,
            "last_error": self._last_error,
            "stream_mode": self.stream_mode(),
        }

    def stream_mode(self) -> str:
        from .streaming import stream_mode
        return stream_mode(self.config)

    def stream_update(self, *, chat_id: str, source_message_id: str,
                      text: str, final: bool = False, thread_id: str = "",
                      stream_id: str = "") -> bool:
        return self._output_streams.update(
            chat_id=chat_id, source_message_id=source_message_id,
            text=text, final=final, thread_id=thread_id, stream_id=stream_id,
        )

    @staticmethod
    def _completed_reply_prefixes(text: str, *, max_frames: int = 5) -> list[str]:
        """Build a bounded cumulative frame set for one completed Agent bubble."""
        content = str(text or "")
        if not content:
            return []
        frame_count = min(max_frames, max(2, (len(content) + 11) // 12))
        if len(content) <= 2:
            frame_count = 1
        cuts = {
            max(
                1,
                min(
                    len(content),
                    (len(content) * index + frame_count - 1) // frame_count,
                ),
            )
            for index in range(1, frame_count + 1)
        }
        return [content[:cut] for cut in sorted(cuts)]

    def send_streamed_reply(
        self,
        reply: ChannelReply,
        *,
        source_message_id: str,
        delivery_id: str = "",
        frame_delay: float = 1.05,
    ) -> bool:
        """Replay one accepted tool-call bubble through the provider stream.

        Tool arguments are only valid once their JSON has closed, so they do
        not reach the normal assistant token callback.  Cumulative frames keep
        the provider-native live experience while preserving the exact bubble
        boundary selected by the Agent. Unsupported/disabled streams return
        False and the durable outbox falls back to ``send``.
        """
        if reply.blocks or reply.attachments or self.stream_mode() == "staged":
            return False
        prefixes = self._completed_reply_prefixes(reply.text)
        if not prefixes:
            return False
        extra: dict[str, str] = {}
        if self.config.provider != "qq":
            # One inbound message may produce several distinct bubbles. Keep
            # their state separate while retaining the real platform reply ID.
            extra = {
                "thread_id": reply.external_thread_id,
                "stream_id": delivery_id or uuid.uuid4().hex,
            }
            if self._output_streams.is_complete(
                chat_id=reply.external_chat_id,
                source_message_id=source_message_id,
                text=reply.text,
                **extra,
            ):
                return True
        for prefix in prefixes[:-1]:
            if not self.stream_update(
                chat_id=reply.external_chat_id,
                source_message_id=source_message_id,
                text=prefix,
                final=False,
                **extra,
            ):
                return False
            if frame_delay > 0 and self._stop.wait(frame_delay):
                raise RuntimeError(
                    f"{self.config.provider} channel stopped during streamed reply"
                )
        return self.stream_update(
            chat_id=reply.external_chat_id,
            source_message_id=source_message_id,
            text=prefixes[-1],
            final=True,
            **extra,
        )

    def worker_alive(self) -> bool:
        """False when this adapter's poll/server thread has died."""
        thread = getattr(self, "_thread", None)
        if isinstance(thread, threading.Thread):
            return thread.is_alive()
        return True

    @abstractmethod
    def start(self, on_event: EventCallback) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send(self, reply: ChannelReply) -> None:
        raise NotImplementedError

    @abstractmethod
    def test(self) -> dict[str, Any]:
        raise NotImplementedError

    def _emit(self, event: ChannelEvent) -> None:
        kind = infer_chat_kind(
            event.provider,
            chat_id=event.external_chat_id,
            raw=event.raw,
            chat_kind=event.chat_kind,
        )
        if kind == "group":
            return
        if kind != event.chat_kind:
            event = replace(event, chat_kind=kind)
        if event.provider == "dingtalk":
            self._output_streams.peer_users[event.external_chat_id] = event.external_user_id
        if self._on_event is not None:
            self._on_event(event)


class TelegramAdapter(ChannelAdapter):
    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None
        self._offset = 0

    def _setting(self, key: str, default: str = "") -> str:
        settings = self.config.settings if isinstance(self.config.settings, dict) else {}
        return str(settings.get(key) or default).strip()

    @property
    def _proxy(self) -> str | None:
        """Optional HTTP(S)/SOCKS proxy for api.telegram.org (e.g. http://127.0.0.1:7890)."""
        value = self._setting("proxy")
        return value or None

    @property
    def _api_root(self) -> str:
        """Bot API host root; override for a local Bot API server."""
        return (self._setting("api_base") or "https://api.telegram.org").rstrip("/")

    @property
    def _base(self) -> str:
        token = self.credentials.get("bot_token", "")
        return f"{self._api_root}/bot{token}"

    def _httpx_kwargs(self, **extra: Any) -> dict[str, Any]:
        kwargs = dict(extra)
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return kwargs

    @staticmethod
    def _transport_hint(exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        if any(
            token in lowered
            for token in (
                "unexpected_eof",
                "ssl",
                "connecterror",
                "timed out",
                "connection refused",
                "name or service not known",
                "getaddrinfo",
            )
        ):
            return (
                f"{text}. If api.telegram.org is unreachable, set channel settings "
                "`proxy` (e.g. http://127.0.0.1:7890) or `api_base` to a local Bot API server."
            )
        return text

    def test(self) -> dict[str, Any]:
        if not self.credentials.get("bot_token"):
            return {"ok": False, "error": "bot_token is required"}
        try:
            response = httpx.get(
                f"{self._base}/getMe",
                **self._httpx_kwargs(timeout=15),
            )
            payload = response.json()
            return {
                "ok": bool(payload.get("ok")),
                "identity": payload.get("result") or {},
                "error": "" if payload.get("ok") else str(payload.get("description") or ""),
            }
        except Exception as exc:
            return {"ok": False, "error": self._transport_hint(exc)}

    def start(self, on_event: EventCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"argusai-telegram-{self.config.id}",
        )
        self._thread.start()

    def _loop(self) -> None:
        self._connected = True
        with httpx.Client(
            **self._httpx_kwargs(timeout=httpx.Timeout(35, connect=10)),
        ) as client:
            while not self._stop.is_set():
                try:
                    response = client.get(
                        f"{self._base}/getUpdates",
                        params={
                            "offset": self._offset,
                            "timeout": 25,
                            "allowed_updates": json.dumps([
                                "message",
                                "edited_message",
                                "callback_query",
                            ]),
                        },
                    )
                    payload = response.json()
                    if not payload.get("ok"):
                        raise RuntimeError(str(payload.get("description") or "Telegram polling failed"))
                    for update in payload.get("result") or []:
                        self._offset = max(self._offset, int(update.get("update_id") or 0) + 1)
                        callback = update.get("callback_query") or {}
                        if callback:
                            callback_id = str(callback.get("id") or "")
                            if callback_id:
                                client.post(
                                    f"{self._base}/answerCallbackQuery",
                                    json={"callback_query_id": callback_id},
                                )
                            data = str(callback.get("data") or "")
                            message = callback.get("message") or {}
                            sender = callback.get("from") or {}
                            chat = message.get("chat") or {}
                            if data.startswith("act:"):
                                # Dedup on the callback id, never the chat message
                                # id: several buttons share one message. Prefix so
                                # send() does not treat it as reply_to_message_id
                                # (callback ids are numeric and Telegram 400s them).
                                self._emit(ChannelEvent(
                                    channel_id=self.config.id,
                                    provider="telegram",
                                    external_user_id=str(sender.get("id") or ""),
                                    external_chat_id=str(chat.get("id") or ""),
                                    message_id=(
                                        f"cb:{callback_id}"
                                        if callback_id
                                        else uuid.uuid4().hex
                                    ),
                                    text="",
                                    external_thread_id=str(
                                        message.get("message_thread_id") or ""
                                    ),
                                    display_name=" ".join(
                                        str(sender.get(key) or "")
                                        for key in ("first_name", "last_name")
                                    ).strip(),
                                    timestamp=time.time(),
                                    raw=callback,
                                    event_kind="action",
                                    action_token=data.removeprefix("act:"),
                                ))
                            continue
                        message = (
                            update.get("message")
                            or update.get("edited_message")
                            or {}
                        )
                        sender = message.get("from") or {}
                        chat = message.get("chat") or {}
                        text = _telegram_inbound_text(message)
                        attachments = self._message_attachments(client, message)
                        if not text and not attachments:
                            continue
                        self._emit(ChannelEvent(
                            channel_id=self.config.id,
                            provider="telegram",
                            external_user_id=str(sender.get("id") or ""),
                            external_chat_id=str(chat.get("id") or ""),
                            message_id=str(message.get("message_id") or ""),
                            text=text,
                            external_thread_id=str(message.get("message_thread_id") or ""),
                            attachments=tuple(attachments),
                            display_name=" ".join(
                                str(sender.get(key) or "")
                                for key in ("first_name", "last_name")
                            ).strip(),
                            timestamp=float(message.get("date") or time.time()),
                            raw=message,
                        ))
                    self._last_error = ""
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stop.wait(3)
        self._connected = False

    def _message_attachments(
        self,
        client: httpx.Client,
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[dict[str, Any], str, str]] = []
        photos = list(message.get("photo") or [])
        if photos:
            candidates.append((dict(photos[-1]), "image/jpeg", "telegram-photo.jpg"))
        sticker = message.get("sticker")
        if isinstance(sticker, dict):
            if sticker.get("is_video"):
                candidates.append((sticker, "video/webm", "telegram-sticker.webm"))
            elif sticker.get("is_animated"):
                candidates.append((
                    sticker,
                    "application/x-tgsticker",
                    "telegram-sticker.tgs",
                ))
            else:
                candidates.append((sticker, "image/webp", "telegram-sticker.webp"))
        for field, default_mime, default_name in (
            ("video", "video/mp4", "telegram-video.mp4"),
            ("video_note", "video/mp4", "telegram-video-note.mp4"),
            ("animation", "video/mp4", "telegram-animation.mp4"),
            ("audio", "audio/mpeg", "telegram-audio.mp3"),
            ("voice", "audio/ogg", "telegram-voice.ogg"),
            ("document", "application/octet-stream", "telegram-document"),
        ):
            value = message.get(field)
            if isinstance(value, dict):
                candidates.append((
                    value,
                    str(value.get("mime_type") or default_mime),
                    str(value.get("file_name") or default_name),
                ))
        attachments: list[dict[str, Any]] = []
        deadline = time.monotonic() + 60
        total_bytes = 0
        for value, mime, name in candidates[:FORWARD_MEDIA_MAX_COUNT]:
            if time.monotonic() >= deadline:
                attachments.append(_attachment_error(name, "60 second attachment download budget exhausted"))
                break
            kind = _attachment_kind(mime, name)
            if not kind:
                continue
            if int(value.get("file_size") or 0) > _attachment_limit(kind):
                attachments.append(_attachment_error(
                    name,
                    f"file exceeds the {_attachment_limit(kind)} byte limit",
                ))
                continue
            try:
                file_id = str(value.get("file_id") or "")
                if not file_id:
                    raise ChannelMediaError("Telegram event did not include a file_id")
                response = client.get(
                    f"{self._base}/getFile",
                    params={"file_id": file_id},
                    timeout=max(0.1, deadline - time.monotonic()),
                )
                payload = response.json()
                file_path = str(dict(payload.get("result") or {}).get("file_path") or "")
                if not payload.get("ok") or not file_path:
                    raise ChannelMediaError("Telegram getFile did not return a downloadable file path")
                attachment = self._download_with_deadline(
                    client,
                    f"{self._base.replace('/bot', '/file/bot')}/{file_path}",
                    mime=mime,
                    name=name,
                    deadline=deadline,
                )
                if attachment is None:
                    raise ChannelMediaError("provider did not return attachment bytes")
                size = len(attachment_bytes(attachment))
                if total_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                    raise ChannelMediaError("message attachment byte budget exceeded")
                total_bytes += size
                attachments.append(attachment)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ChannelMediaError) else f"provider download failed ({type(exc).__name__})"
                attachments.append(_attachment_error(name, reason))
                log.debug("Telegram attachment download failed", exc_info=True)
        if len(attachments) > MAX_ATTACHMENTS:
            return [pack_forwarded_media(
                attachments, provider="telegram", message_id=str(message.get("message_id") or ""),
            )]
        return attachments

    @staticmethod
    def _download_with_deadline(
        client: httpx.Client, url: str, *, mime: str, name: str, deadline: float,
    ) -> dict[str, Any]:
        """Keep the synchronous stream inside this message's download budget."""
        class DeadlineResponse:
            def __init__(self, response):
                self.response = response
                self.headers = response.headers

            def raise_for_status(self):
                return self.response.raise_for_status()

            def iter_bytes(self):
                for chunk in self.response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise ChannelMediaError("60 second attachment download budget exhausted")
                    yield chunk

        class DeadlineClient:
            @contextlib.contextmanager
            def stream(self, *args, **kwargs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ChannelMediaError("60 second attachment download budget exhausted")
                with client.stream(*args, **kwargs, timeout=max(0.1, remaining)) as response:
                    yield DeadlineResponse(response)

        return _download_attachment(DeadlineClient(), url, mime=mime, name=name)

    def _post_message(self, payload: dict[str, Any]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self._base}/sendMessage",
                    json=payload,
                    **self._httpx_kwargs(timeout=20),
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= 2:
                    break
                self._stop.wait(0.5 * (2 ** attempt))
                continue
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt >= 2:
                return response
            delay = 0.5 * (2 ** attempt)
            if response.status_code == 429:
                try:
                    delay = max(
                        delay,
                        min(
                            float(
                                dict(response.json().get("parameters") or {}).get(
                                    "retry_after"
                                )
                                or 0
                            ),
                            30.0,
                        ),
                    )
                except (TypeError, ValueError):
                    pass
            self._stop.wait(delay)
        if last_error is not None:
            raise RuntimeError(
                "Telegram send transport failed after 3 attempts: "
                f"{self._transport_hint(last_error)}"
            ) from last_error
        raise RuntimeError("Telegram send failed after 3 attempts")

    def send(self, reply: ChannelReply) -> None:
        prepared = _prepared_attachments(reply)
        rendered = render_channel_reply("telegram", reply)
        chunks = [
            chunk for chunk in
            (rendered.get("text_chunks") or [str(rendered.get("text") or "")])
            if str(chunk).strip()
        ]
        if prepared and not reply.text and not reply.blocks:
            chunks = []
        for index, text in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": reply.external_chat_id,
                "text": text,
            }
            if rendered.get("parse_mode"):
                payload["parse_mode"] = rendered["parse_mode"]
            if index == 0 and reply.reply_to_message_id.isdigit():
                payload["reply_to_message_id"] = int(reply.reply_to_message_id)
            if reply.external_thread_id.isdigit():
                payload["message_thread_id"] = int(reply.external_thread_id)
            if index == 0 and rendered.get("reply_markup"):
                payload["reply_markup"] = rendered["reply_markup"]
            response = self._post_message(payload)
            # Stale or non-message ids (e.g. a numeric callback_query id) make
            # Telegram reject the whole send. Drop reply_to and retry once so
            # the keyboard still arrives.
            if (
                not response.is_success
                and response.status_code == 400
                and payload.pop("reply_to_message_id", None) is not None
            ):
                response = self._post_message(payload)
            if response.is_success:
                continue
            try:
                error = str(response.json().get("description") or response.text)
            except (TypeError, ValueError):
                error = response.text
            if response.status_code == 400:
                # Telegram rejects the whole message when one entity is malformed.
                # Resend as marker-free text, and with /act tokens so the user can
                # still act on a card whose keyboard was refused.
                fallback = render_markdown(
                    card_text(reply.text, reply.blocks, include_tokens=True),
                    PLAIN,
                )
                fallback_ok = True
                for fallback_start in range(0, len(fallback) or 1, 4000):
                    retry = self._post_message({
                        "chat_id": reply.external_chat_id,
                        "text": fallback[fallback_start:fallback_start + 4000],
                    })
                    if retry.is_success:
                        continue
                    fallback_ok = False
                    try:
                        error = str(retry.json().get("description") or retry.text)
                    except (TypeError, ValueError):
                        error = retry.text
                    break
                if fallback_ok:
                    break
            raise delivery_error(
                "Telegram send failed",
                str(error or response.status_code),
                response.status_code,
            )
        for index, (attachment, data) in enumerate(prepared):
            method, field = {
                "image": ("sendPhoto", "photo"),
                "video": ("sendVideo", "video"),
                "audio": ("sendAudio", "audio"),
            }.get(attachment.kind, ("sendDocument", "document"))
            form: dict[str, Any] = {"chat_id": reply.external_chat_id}
            if attachment.caption:
                form["caption"] = attachment.caption[:1024]
            if reply.external_thread_id.isdigit():
                form["message_thread_id"] = reply.external_thread_id
            if index == 0 and reply.reply_to_message_id.isdigit():
                form["reply_to_message_id"] = reply.reply_to_message_id
            response = httpx.post(
                f"{self._base}/{method}",
                data=form,
                files={field: (attachment.name, data, attachment.mime)},
                **self._httpx_kwargs(timeout=30),
            )
            if response.is_success:
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = {"ok": True}
                if payload.get("ok", True):
                    continue
            try:
                error = str(response.json().get("description") or response.text)
            except (TypeError, ValueError):
                error = response.text
            raise delivery_error(
                f"Telegram {attachment.kind} upload failed",
                str(error or response.status_code),
                response.status_code,
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None


class DiscordAdapter(ChannelAdapter):
    GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
    API_BASE = "https://discord.com/api/v10"

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.credentials.get('bot_token', '')}"}

    def test(self) -> dict[str, Any]:
        if not self.credentials.get("bot_token"):
            return {"ok": False, "error": "bot_token is required"}
        try:
            response = httpx.get(f"{self.API_BASE}/users/@me", headers=self._headers(), timeout=15)
            payload = response.json()
            return {
                "ok": response.is_success,
                "identity": payload if response.is_success else {},
                "error": "" if response.is_success else str(payload.get("message") or response.status_code),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start(self, on_event: EventCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run_forever()),
            daemon=True,
            name=f"argusai-discord-{self.config.id}",
        )
        self._thread.start()

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._gateway_once()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(3)
        self._connected = False

    async def _gateway_once(self) -> None:
        async with websockets.connect(self.GATEWAY_URL, open_timeout=20) as socket:
            hello = json.loads(await socket.recv())
            interval = float(hello["d"]["heartbeat_interval"]) / 1000
            token = self.credentials.get("bot_token", "")
            await socket.send(json.dumps({
                "op": 2,
                "d": {
                    "token": token,
                    "intents": (1 << 9) | (1 << 12) | (1 << 15),
                    "properties": {"os": "windows", "browser": "argusai", "device": "argusai"},
                },
            }))
            sequence: int | None = None
            self._connected = True
            self._last_error = ""

            async def heartbeat() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(interval)
                    await socket.send(json.dumps({"op": 1, "d": sequence}))

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                async for raw in socket:
                    payload = json.loads(raw)
                    if payload.get("s") is not None:
                        sequence = int(payload["s"])
                    if payload.get("op") == 7:
                        return
                    if payload.get("t") == "INTERACTION_CREATE":
                        interaction = payload.get("d") or {}
                        data = interaction.get("data") or {}
                        custom_id = str(data.get("custom_id") or "")
                        if interaction.get("type") == 3 and custom_id.startswith("act:"):
                            async with httpx.AsyncClient(timeout=10) as client:
                                await client.post(
                                    f"{self.API_BASE}/interactions/{interaction.get('id')}/{interaction.get('token')}/callback",
                                    json={"type": 6},
                                )
                            member = interaction.get("member") or {}
                            user = member.get("user") or interaction.get("user") or {}
                            message = interaction.get("message") or {}
                            self._emit(ChannelEvent(
                                channel_id=self.config.id,
                                provider="discord",
                                external_user_id=str(user.get("id") or ""),
                                external_chat_id=str(interaction.get("channel_id") or ""),
                                message_id=str(interaction.get("id") or uuid.uuid4().hex),
                                text="",
                                display_name=str(user.get("global_name") or user.get("username") or ""),
                                timestamp=time.time(),
                                raw=interaction,
                                event_kind="action",
                                action_token=custom_id.removeprefix("act:"),
                                action_value=str(data.get("values") or ""),
                            ))
                        continue
                    if payload.get("t") != "MESSAGE_CREATE":
                        continue
                    message = payload.get("d") or {}
                    author = message.get("author") or {}
                    if author.get("bot"):
                        continue
                    text, records = self._message_content(message)
                    attachments = await self._message_attachments(message)
                    attachments = records + attachments
                    if not text and not attachments:
                        continue
                    self._emit(ChannelEvent(
                        channel_id=self.config.id,
                        provider="discord",
                        external_user_id=str(author.get("id") or ""),
                        external_chat_id=str(message.get("channel_id") or ""),
                        message_id=str(message.get("id") or ""),
                        text=text,
                        attachments=tuple(attachments),
                        display_name=str(author.get("global_name") or author.get("username") or ""),
                        timestamp=time.time(),
                        raw=message,
                    ))
            finally:
                heartbeat_task.cancel()

    @staticmethod
    def _message_parts(message: dict[str, Any]) -> tuple[list[tuple[dict[str, Any], str]], list[str]]:
        """Read only the snapshots included in this gateway event."""
        parts: list[tuple[dict[str, Any], str]] = []
        warnings: list[str] = []
        pending = [(message, 0, str(message.get("id") or "message"))]
        while pending and len(parts) < FORWARD_MEDIA_MAX_COUNT:
            node, depth, source_id = pending.pop()
            if depth > 4:
                warnings.append("【Discord 转发嵌套超过 4 层，未完整展开。】")
                continue
            parts.append((node, source_id))
            snapshots = node.get("message_snapshots") or []
            reference = node.get("message_reference")
            reference = reference if isinstance(reference, dict) else {}
            if isinstance(reference, dict) and reference.get("type") == 1 and not snapshots:
                warnings.append("【Discord 转发事件未携带消息快照，无法读取原文或附件。】")
            for index, snapshot in reversed(list(enumerate(snapshots))):
                child = snapshot.get("message") if isinstance(snapshot, dict) else None
                if isinstance(child, dict):
                    child_id = str(reference.get("message_id") or f"{source_id}/snapshot-{index + 1}")
                    pending.append((child, depth + 1, child_id))
        if pending:
            warnings.append("【Discord 转发消息超过展开数量限制，未完整读取。】")
        return parts, list(dict.fromkeys(warnings))

    def _message_content(self, message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        parts, warnings = self._message_parts(message)
        lines = [_discord_inbound_text(message)]
        for node, source_id in parts[1:]:
            text = _discord_inbound_text(node)
            lines.extend([
                f"【Discord 转发快照 {source_id}；以下为用户转发内容】",
                text or "【该快照无文字；媒体附件见归档】",
                "【转发快照结束】",
            ])
        lines.extend(warnings)
        text = "\n".join(line for line in lines if line)
        if len(parts) > 1:
            return prepare_forwarded_text(text, provider="discord", message_id=str(message.get("id") or ""))
        return text, []

    async def _message_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        parts, _warnings = self._message_parts(message)
        candidates: list[tuple[dict[str, Any], str]] = []
        for node, source_id in parts:
            candidates.extend((item, source_id) for item in node.get("attachments") or [] if isinstance(item, dict))
            for item in node.get("sticker_items") or node.get("stickers") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                format_type = int(item.get("format_type") or 1)
                if format_type == 3:
                    continue
                extension, mime = ("gif", "image/gif") if format_type == 4 else ("png", "image/png")
                candidates.append(({
                    "filename": f"{safe_filename(str(item.get('name') or 'discord-sticker'))}.{extension}",
                    "content_type": mime,
                    "url": f"https://cdn.discordapp.com/stickers/{item['id']}.{extension}",
                    "_sticker": True,
                }, source_id))
        attachments: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_bytes = 0
        deadline = time.monotonic() + 60
        if len(candidates) > FORWARD_MEDIA_MAX_COUNT:
            warnings.append(f"{len(candidates) - FORWARD_MEDIA_MAX_COUNT} attachments exceed the 64 file limit")
        async with httpx.AsyncClient(timeout=30) as client:
            for index, (item, source_id) in enumerate(candidates[:FORWARD_MEDIA_MAX_COUNT]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    warnings.append(f"60 second download budget exhausted; {min(len(candidates), FORWARD_MEDIA_MAX_COUNT) - index} attachments not downloaded")
                    break
                mime = str(item.get("content_type") or "")
                name = str(item.get("filename") or "discord-attachment")
                try:
                    limit = min(_attachment_limit(_attachment_kind(mime, name)), FORWARD_MEDIA_MAX_BYTES - total_bytes)
                    if int(item.get("size") or 0) > limit:
                        raise ChannelMediaError("file exceeds the remaining attachment byte limit")
                    url = str(item.get("url") or "")
                    if not url:
                        raise ChannelMediaError("Discord event did not include a download URL")
                    attachment = await asyncio.wait_for(_download_attachment_async(
                        client, url, mime=mime, name=name,
                        headers=self._headers() if item.get("_sticker") else None,
                    ), timeout=remaining)
                    if attachment is None:
                        raise ChannelMediaError("provider did not return attachment bytes")
                    size = len(attachment_bytes(attachment))
                    if total_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                        raise ChannelMediaError("message attachment byte budget exceeded")
                    total_bytes += size
                    attachment["source_message_id"] = source_id
                    attachments.append(attachment)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ChannelMediaError) else f"provider download failed ({type(exc).__name__})"
                    warnings.append(f"{safe_filename(name)}: {reason}")
        force_archive = len(parts) > 1
        if attachments and (force_archive or len(attachments) + bool(warnings) > MAX_ATTACHMENTS):
            try:
                attachments = [pack_forwarded_media(
                    attachments, provider="discord", message_id=str(message.get("id") or ""),
                )]
            except ChannelMediaError as exc:
                attachments = [_attachment_error("discord-media", f"archive unavailable: {exc}; downloaded files could not be attached")]
        if warnings:
            attachments.append(_attachment_error("discord-media", "; ".join(warnings)))
        return attachments

    def send(self, reply: ChannelReply) -> None:
        prepared = _prepared_attachments(reply)
        rendered = render_channel_reply("discord", reply)
        chunks = list(rendered.get("content_chunks") or [str(rendered.get("content") or "")])
        for start, content in enumerate(chunks or [""]):
            payload: dict[str, Any] = {
                "content": content,
                "message_reference": (
                    {"message_id": reply.reply_to_message_id}
                    if reply.reply_to_message_id
                    else None
                ),
                "components": rendered.get("components") if start == 0 else [],
            }
            request: dict[str, Any] = {
                "headers": self._headers(),
                "timeout": 30 if start == 0 and prepared else 20,
            }
            if start == 0 and prepared:
                payload["attachments"] = [
                    {
                        "id": index,
                        "filename": attachment.name,
                        **(
                            {"description": attachment.caption[:1024]}
                            if attachment.caption
                            else {}
                        ),
                    }
                    for index, (attachment, _data) in enumerate(prepared)
                ]
                request["data"] = {
                    "payload_json": json.dumps(payload, ensure_ascii=False)
                }
                request["files"] = [
                    (
                        f"files[{index}]",
                        (attachment.name, data, attachment.mime),
                    )
                    for index, (attachment, data) in enumerate(prepared)
                ]
            else:
                request["json"] = payload
            response = httpx.post(
                f"{self.API_BASE}/channels/{reply.external_chat_id}/messages",
                **request,
            )
            check_api_response(response, "Discord send failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4)
        self._thread = None


class SlackAdapter(ChannelAdapter):
    API_BASE = "https://slack.com/api"

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None

    def _bot_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.get('bot_token', '')}"}

    def test(self) -> dict[str, Any]:
        if not self.credentials.get("bot_token") or not self.credentials.get("app_token"):
            return {"ok": False, "error": "bot_token and app_token are required"}
        try:
            response = httpx.post(
                f"{self.API_BASE}/auth.test",
                headers=self._bot_headers(),
                timeout=15,
            )
            payload = response.json()
            return {
                "ok": bool(payload.get("ok")),
                "identity": payload if payload.get("ok") else {},
                "error": "" if payload.get("ok") else str(payload.get("error") or ""),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start(self, on_event: EventCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run_forever()),
            daemon=True,
            name=f"argusai-slack-{self.config.id}",
        )
        self._thread.start()

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                response = httpx.post(
                    f"{self.API_BASE}/apps.connections.open",
                    headers={"Authorization": f"Bearer {self.credentials.get('app_token', '')}"},
                    timeout=15,
                )
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError(str(payload.get("error") or "Socket Mode connection failed"))
                async with websockets.connect(str(payload["url"]), open_timeout=20) as socket:
                    self._connected = True
                    self._last_error = ""
                    async for raw in socket:
                        envelope = json.loads(raw)
                        envelope_id = str(envelope.get("envelope_id") or "")
                        if envelope_id:
                            await socket.send(json.dumps({"envelope_id": envelope_id}))
                        socket_payload = envelope.get("payload") or {}
                        if socket_payload.get("type") == "interactive":
                            interaction = socket_payload.get("payload") or {}
                            actions = list(interaction.get("actions") or [])
                            action = actions[0] if actions else {}
                            token = str(action.get("value") or "")
                            user = interaction.get("user") or {}
                            channel = interaction.get("channel") or {}
                            container = interaction.get("container") or {}
                            message = interaction.get("message") or {}
                            if token:
                                self._emit(ChannelEvent(
                                    channel_id=self.config.id,
                                    provider="slack",
                                    external_user_id=str(user.get("id") or ""),
                                    external_chat_id=str(channel.get("id") or ""),
                                    message_id=envelope_id or str(container.get("message_ts") or uuid.uuid4().hex),
                                    text="",
                                    external_thread_id=str(
                                        container.get("thread_ts")
                                        or message.get("thread_ts")
                                        or ""
                                    ),
                                    display_name=str(user.get("username") or user.get("name") or ""),
                                    timestamp=time.time(),
                                    raw=interaction,
                                    event_kind="action",
                                    action_token=token,
                                    action_value=str(action.get("selected_option") or ""),
                                ))
                            continue
                        event = socket_payload.get("event") or {}
                        if event.get("type") not in {"message", "app_mention"} or event.get("bot_id"):
                            continue
                        if event.get("hidden") or str(event.get("subtype") or "") in _SLACK_SKIP_SUBTYPES:
                            continue
                        text, records = self._message_content(event)
                        attachments = await self._message_attachments(event)
                        attachments = records + attachments
                        if not text and not attachments:
                            continue
                        self._emit(ChannelEvent(
                            channel_id=self.config.id,
                            provider="slack",
                            external_user_id=str(event.get("user") or ""),
                            external_chat_id=str(event.get("channel") or ""),
                            message_id=str(event.get("ts") or uuid.uuid4().hex),
                            text=text,
                            external_thread_id=str(event.get("thread_ts") or ""),
                            attachments=tuple(attachments),
                            timestamp=time.time(),
                            raw=event,
                        ))
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(3)
        self._connected = False

    @staticmethod
    def _message_parts(event: dict[str, Any]) -> tuple[list[tuple[dict[str, Any], str]], list[str]]:
        """Inspect shared content already present in this event, without link fetching."""
        parts: list[tuple[dict[str, Any], str]] = []
        warnings: list[str] = []
        pending = [(event, 0, str(event.get("ts") or "message"))]
        while pending and len(parts) < FORWARD_MEDIA_MAX_COUNT:
            node, depth, source_id = pending.pop()
            if depth > 4:
                warnings.append("【Slack 共享消息嵌套超过 4 层，未完整展开。】")
                continue
            parts.append((node, source_id))
            children = list(node.get("attachments") or [])
            if isinstance(node.get("message"), dict):
                children.insert(0, node["message"])
            for index, child in reversed(list(enumerate(children))):
                if isinstance(child, dict):
                    child_id = str(child.get("ts") or child.get("message_ts") or f"{source_id}/shared-{index + 1}")
                    pending.append((child, depth + 1, child_id))
        if pending:
            warnings.append("【Slack 共享消息超过展开数量限制，未完整读取。】")
        return parts, list(dict.fromkeys(warnings))

    def _message_content(self, event: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        parts, warnings = self._message_parts(event)
        lines: list[str] = []
        for index, (node, source_id) in enumerate(parts):
            text = str(node.get("text") or node.get("fallback") or "").strip() or _slack_blocks_text(node.get("blocks"))
            if index == 0:
                if text:
                    lines.append(text)
                continue
            if not text and isinstance(node.get("message"), dict) and not node.get("files"):
                # The nested message is emitted as its own part below.
                continue
            lines.extend([
                f"【Slack 共享消息 {source_id}；以下为用户转发内容】",
                text or (
                    "【正文未包含在事件中；附件见媒体归档】" if node.get("files")
                    else "【事件仅提供共享引用，未携带可读取的正文或文件；未自动访问分享链接。】"
                ),
                "【共享内容结束】",
            ])
        lines.extend(warnings)
        text = "\n".join(lines)
        if len(parts) > 1:
            return prepare_forwarded_text(text, provider="slack", message_id=str(event.get("ts") or ""))
        return text, []

    async def _message_attachments(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        parts, _warnings = self._message_parts(event)
        candidates: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for node, source_id in parts:
            for item in node.get("files") or []:
                if not isinstance(item, dict):
                    continue
                file_id = str(item.get("id") or "")
                if file_id and file_id in seen:
                    continue
                if file_id:
                    seen.add(file_id)
                candidates.append((item, source_id))
        attachments: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_bytes = 0
        deadline = time.monotonic() + 60
        if len(candidates) > FORWARD_MEDIA_MAX_COUNT:
            warnings.append(f"{len(candidates) - FORWARD_MEDIA_MAX_COUNT} attachments exceed the 64 file limit")
        async with httpx.AsyncClient(timeout=30) as client:
            for index, (item, source_id) in enumerate(candidates[:FORWARD_MEDIA_MAX_COUNT]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    warnings.append(f"60 second download budget exhausted; {min(len(candidates), FORWARD_MEDIA_MAX_COUNT) - index} attachments not downloaded")
                    break
                mime = str(item.get("mimetype") or "")
                name = str(item.get("name") or "slack-attachment")
                try:
                    limit = min(_attachment_limit(_attachment_kind(mime, name)), FORWARD_MEDIA_MAX_BYTES - total_bytes)
                    if int(item.get("size") or 0) > limit:
                        raise ChannelMediaError("file exceeds the remaining attachment byte limit")
                    url = str(item.get("url_private_download") or item.get("url_private") or "")
                    if not url:
                        raise ChannelMediaError("Slack event did not include a downloadable file URL")
                    attachment = await asyncio.wait_for(_download_attachment_async(
                        client, url, mime=mime, name=name, headers=self._bot_headers(),
                    ), timeout=remaining)
                    if attachment is None:
                        raise ChannelMediaError("provider did not return attachment bytes")
                    size = len(attachment_bytes(attachment))
                    if total_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                        raise ChannelMediaError("message attachment byte budget exceeded")
                    total_bytes += size
                    attachment["source_message_id"] = source_id
                    attachments.append(attachment)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ChannelMediaError) else f"provider download failed ({type(exc).__name__})"
                    warnings.append(f"{safe_filename(name)}: {reason}")
        if attachments and (len(parts) > 1 or len(attachments) + bool(warnings) > MAX_ATTACHMENTS):
            try:
                attachments = [pack_forwarded_media(
                    attachments, provider="slack", message_id=str(event.get("ts") or ""),
                )]
            except ChannelMediaError as exc:
                attachments = [_attachment_error("slack-media", f"archive unavailable: {exc}; downloaded files could not be attached")]
        if warnings:
            attachments.append(_attachment_error("slack-media", "; ".join(warnings)))
        return attachments

    def send(self, reply: ChannelReply) -> None:
        prepared = _prepared_attachments(reply)
        rendered = render_channel_reply("slack", reply)
        if reply.text or reply.blocks:
            response = httpx.post(
                f"{self.API_BASE}/chat.postMessage",
                headers=self._bot_headers(),
                json={
                    "channel": reply.external_chat_id,
                    "text": rendered.get("text") or reply.text,
                    "blocks": rendered.get("blocks") or [],
                    "thread_ts": reply.external_thread_id or reply.reply_to_message_id or None,
                },
                timeout=20,
            )
            check_api_response(response, "Slack send failed")
        uploaded: list[dict[str, str]] = []
        captions: list[str] = []
        for attachment, data in prepared:
            response = httpx.post(
                f"{self.API_BASE}/files.getUploadURLExternal",
                headers=self._bot_headers(),
                data={"filename": attachment.name, "length": str(len(data))},
                timeout=20,
            )
            payload = check_api_response(response, "Slack upload URL request failed")
            upload_url = str(payload.get("upload_url") or "")
            file_id = str(payload.get("file_id") or "")
            if not upload_url or not file_id:
                raise ChannelDeliveryError(
                    "Slack upload URL request returned no upload target: "
                    f"{json.dumps(payload, ensure_ascii=False)[:300]}"
                )
            upload_response = httpx.put(
                upload_url,
                content=data,
                headers={"Content-Type": attachment.mime},
                timeout=30,
            )
            check_api_response(upload_response, "Slack file upload failed")
            uploaded.append({"id": file_id, "title": attachment.name})
            if attachment.caption:
                captions.append(attachment.caption)
        if uploaded:
            complete_payload: dict[str, Any] = {
                "files": uploaded,
                "channel_id": reply.external_chat_id,
            }
            thread_ts = reply.external_thread_id or reply.reply_to_message_id
            if thread_ts:
                complete_payload["thread_ts"] = thread_ts
            if captions:
                complete_payload["initial_comment"] = "\n".join(captions)[:3000]
            response = httpx.post(
                f"{self.API_BASE}/files.completeUploadExternal",
                headers=self._bot_headers(),
                json=complete_payload,
                timeout=20,
            )
            check_api_response(response, "Slack upload completion failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4)
        self._thread = None


class StreamSDKAdapter(ChannelAdapter):
    """Run persistent official SDKs in a killable child process."""

    _EVENT_PREFIX = b"ARGUSAI_CHANNEL_EVENT "
    _STATUS_PREFIX = b"ARGUSAI_CHANNEL_STATUS "

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.RLock()
        self._reply_urls: dict[str, str] = {}
        self._dingtalk_robot_codes: dict[str, str] = {}
        self._feishu_token = ""
        self._feishu_token_expires_at = 0.0
        self._dingtalk_token = ""
        self._dingtalk_token_expires_at = 0.0

    def test(self) -> dict[str, Any]:
        if self.config.provider == "feishu":
            if not self.credentials.get("app_id") or not self.credentials.get("app_secret"):
                return {"ok": False, "error": "app_id and app_secret are required"}
            try:
                response = httpx.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.credentials["app_id"],
                        "app_secret": self.credentials["app_secret"],
                    },
                    timeout=15,
                )
                payload = response.json()
                token = str(payload.get("tenant_access_token") or "")
                return {
                    "ok": bool(token),
                    "identity": {"app_id": self.credentials["app_id"]} if token else {},
                    "error": "" if token else str(payload.get("msg") or "Feishu authentication failed"),
                }
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if not self.credentials.get("client_id") or not self.credentials.get("client_secret"):
            return {"ok": False, "error": "client_id and client_secret are required"}
        try:
            response = httpx.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={
                    "appKey": self.credentials["client_id"],
                    "appSecret": self.credentials["client_secret"],
                },
                timeout=15,
            )
            payload = response.json()
            token = str(payload.get("accessToken") or "")
            return {
                "ok": bool(token),
                "identity": {"client_id": self.credentials["client_id"]} if token else {},
                "error": "" if token else str(payload.get("message") or payload.get("code") or "DingTalk authentication failed"),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start(self, on_event: EventCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"argusai-{self.config.provider}-stream-{self.config.id}",
        )
        self._thread.start()

    @staticmethod
    def _sidecar_command() -> list[str]:
        from argusai.core.infra.runtime_exec import self_relaunch_command

        return self_relaunch_command("--channel-stream-sidecar")

    def _loop(self) -> None:
        while not self._stop.is_set():
            error_lines: list[str] = []
            creationflags = (
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0
            )
            try:
                from argusai.core.infra.runtime_exec import self_relaunch_env

                command = self._sidecar_command()
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self_relaunch_env(),
                    creationflags=creationflags,
                )
                with self._process_lock:
                    self._process = process
                config = json.dumps({
                    "provider": self.config.provider,
                    "credentials": self.credentials,
                }, ensure_ascii=False).encode("utf-8") + b"\n"
                assert process.stdin is not None
                process.stdin.write(config)
                process.stdin.close()

                def consume_stderr() -> None:
                    assert process.stderr is not None
                    for raw in iter(process.stderr.readline, b""):
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line:
                            error_lines.append(line)
                            del error_lines[:-8]

                stderr_thread = threading.Thread(
                    target=consume_stderr,
                    daemon=True,
                    name=f"argusai-{self.config.provider}-stream-log",
                )
                stderr_thread.start()
                assert process.stdout is not None
                for raw in iter(process.stdout.readline, b""):
                    self._handle_sidecar_line(raw.rstrip(b"\r\n"))
                    if self._stop.is_set():
                        break
                reap_timeout = 0.2 if self._stop.is_set() else 3
                return_code = process.wait(timeout=reap_timeout)
                stderr_thread.join(timeout=0.2 if self._stop.is_set() else 1)
                self._connected = False
                if not self._stop.is_set():
                    prior = str(self._last_error or "").strip()
                    if error_lines:
                        detail = " | ".join(error_lines[-3:])
                    elif prior:
                        detail = prior
                    else:
                        detail = f"exit code {return_code}"
                    if (
                        return_code not in (0, None)
                        and f"exit {return_code}" not in detail
                        and f"exit code {return_code}" not in detail
                    ):
                        detail = f"{detail} (exit {return_code})"
                    self._last_error = f"stream sidecar stopped: {detail}"
            except Exception as exc:
                self._connected = False
                try:
                    command = self._sidecar_command()
                    detail = f"{type(exc).__name__}: {exc} (cmd={command!r})"
                except Exception:
                    detail = f"{type(exc).__name__}: {exc}"
                self._last_error = detail
            finally:
                with self._process_lock:
                    self._process = None
            if not self._stop.is_set():
                self._stop.wait(3)

    @staticmethod
    def _force_kill_sidecar(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                from argusai.core.infra.win_stdio import background_subprocess_kwargs

                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    **background_subprocess_kwargs(),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    process.kill()
        else:
            with contextlib.suppress(Exception):
                process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)

    def _handle_sidecar_line(self, raw: bytes) -> None:
        try:
            if raw.startswith(self._STATUS_PREFIX):
                payload = json.loads(raw[len(self._STATUS_PREFIX):].decode("utf-8"))
                self._connected = bool(payload.get("connected"))
                self._last_error = str(payload.get("error") or "")
                return
            if not raw.startswith(self._EVENT_PREFIX):
                return
            payload = json.loads(raw[len(self._EVENT_PREFIX):].decode("utf-8"))
            raw_event = dict(payload.get("raw") or {})
            media_warnings: list[str] = []
            if self.config.provider == "feishu":
                if str(raw_event.get("message_type") or "") == "merge_forward":
                    attachments = []
                    event_text = self._feishu_merge_forward_text(
                        payload, raw_event, attachments=attachments,
                    )
                    incomplete = "内容尚不完整】" in event_text
                    event_text, forward_attachments = prepare_forwarded_text(
                        event_text,
                        provider="feishu",
                        message_id=str(payload.get("message_id") or ""),
                    )
                    if incomplete and forward_attachments:
                        event_text += "\n【部分转发内容未能展开；附件中注明了读取缺失，不能视为完整记录。】"
                    if attachments:
                        media_archive = pack_forwarded_media(
                            attachments, provider="feishu",
                            message_id=str(payload.get("message_id") or ""),
                        )
                        event_text += (
                            f"\n【转发媒体文件已打包为 {media_archive['name']}，内含 manifest.json；"
                            "可在会话沙盒按需解压，读取图片或文件。】"
                        )
                        attachments = [media_archive]
                    attachments = forward_attachments + attachments
                else:
                    attachments = self._feishu_attachments(payload, raw_event, warnings=media_warnings)
                    event_text = str(payload.get("text") or "")
            elif self.config.provider == "dingtalk":
                attachments = self._dingtalk_attachments(raw_event, warnings=media_warnings)
                event_text = str(payload.get("text") or "")
            else:
                attachments = []
                event_text = str(payload.get("text") or "")
            if len(attachments) > MAX_ATTACHMENTS:
                media_archive = pack_forwarded_media(
                    attachments, provider=self.config.provider,
                    message_id=str(payload.get("message_id") or ""),
                )
                event_text += (
                    f"\n【本条消息的 {len(attachments)} 个媒体文件已打包为 {media_archive['name']}，"
                    "内含 manifest.json；可在会话沙盒按需解压读取。】"
                )
                attachments = [media_archive]
            if media_warnings:
                event_text += "\n" + "\n".join(f"【{warning}】" for warning in dict.fromkeys(media_warnings))
            if self.config.provider == "dingtalk":
                callback_url = str(raw_event.get("sessionWebhook") or "")
                chat_id = str(payload.get("external_chat_id") or "")
                if callback_url and chat_id:
                    self._reply_urls[chat_id] = callback_url
                robot_code = str(raw_event.get("robotCode") or "")
                if robot_code and chat_id:
                    self._dingtalk_robot_codes[chat_id] = robot_code
            event = ChannelEvent(
                channel_id=self.config.id,
                provider=self.config.provider,
                external_user_id=str(payload.get("external_user_id") or ""),
                external_chat_id=str(payload.get("external_chat_id") or ""),
                message_id=str(payload.get("message_id") or uuid.uuid4().hex),
                text=event_text,
                external_thread_id=str(payload.get("external_thread_id") or ""),
                attachments=tuple(attachments),
                display_name=str(payload.get("display_name") or ""),
                timestamp=float(payload.get("timestamp") or time.time()),
                raw=raw_event,
                event_kind=(
                    "action"
                    if str(payload.get("event_kind") or "") == "action"
                    else "message"
                ),
                action_token=str(payload.get("action_token") or ""),
                action_value=str(payload.get("action_value") or ""),
                chat_kind=infer_chat_kind(
                    self.config.provider,
                    chat_id=str(payload.get("external_chat_id") or ""),
                    raw=raw_event,
                    chat_kind=str(payload.get("chat_kind") or ""),
                ),
            )
            if (
                event.text
                or event.attachments
                or (event.event_kind == "action" and event.action_token)
            ) and event.external_user_id and event.external_chat_id:
                self._emit(event)
        except Exception as exc:
            self._last_error = f"invalid stream event: {type(exc).__name__}: {exc}"

    def _get_feishu_token(self) -> str:
        if self._feishu_token and time.time() < self._feishu_token_expires_at:
            return self._feishu_token
        token_response = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.credentials.get("app_id", ""),
                "app_secret": self.credentials.get("app_secret", ""),
            },
            timeout=20,
        )
        token_payload = check_api_response(token_response, "Feishu authentication failed")
        token = str(token_payload.get("tenant_access_token") or "")
        if not token:
            raise delivery_error(
                "Feishu authentication failed",
                "the tenant access token response carried no token",
            )
        expires_in = int(token_payload.get("expire") or 7200)
        self._feishu_token = token
        self._feishu_token_expires_at = time.time() + max(60, expires_in - 300)
        return token

    def _feishu_merge_forward_text(
        self,
        payload: dict[str, Any],
        raw_event: dict[str, Any],
        *,
        attachments: list[dict[str, Any]] | None = None,
        media_limit: int = FORWARD_MEDIA_MAX_COUNT,
    ) -> str:
        """Read this exact merge and its descendants, never the chat's history."""
        original = str(payload.get("text") or "").strip()
        if str(raw_event.get("message_type") or "") != "merge_forward":
            return original
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return original or "【飞书合并转发消息：事件未携带可展开标识】"
        try:
            token = self._get_feishu_token()
            # The get-message API returns one merge + N children in items;
            # unlike list(chat), it has no pagination or 50-message window.
            # Fetch a nested merge separately only when its children were not
            # included. Bound network work and traversal, reporting partial
            # results explicitly instead of silently dropping records.
            items_by_id: dict[str, dict[str, Any]] = {}
            fetched: set[str] = set()
            pending = [message_id]
            selected: list[dict[str, Any]] = []
            warnings: list[str] = []
            deadline = time.monotonic() + 30
            while pending:
                if len(fetched) >= 16 or time.monotonic() >= deadline:
                    warnings.append("部分嵌套转发达到本次展开限额，内容尚不完整")
                    break
                current_id = pending.pop(0)
                fetched.add(current_id)
                try:
                    response = httpx.get(
                        "https://open.feishu.cn/open-apis/im/v1/messages/"
                        + quote(current_id, safe=""),
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=max(1, min(20, deadline - time.monotonic())),
                    )
                    body = check_api_response(response, "Feishu merge-forward expansion failed")
                    data = body.get("data") if isinstance(body.get("data"), dict) else {}
                    returned = data.get("items") if isinstance(data.get("items"), list) else []
                    for item in returned:
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("message_id") or "")
                        if not item_id or item_id in items_by_id:
                            continue
                        if len(items_by_id) >= 10_000:
                            warnings.append("转发记录超过本次 10000 个节点的展开限额，内容尚不完整")
                            break
                        items_by_id[item_id] = item
                except Exception:
                    if current_id == message_id:
                        raise
                    log.debug("Nested Feishu merge-forward expansion failed", exc_info=True)
                    warnings.append("部分嵌套转发读取失败，内容尚不完整")

                children: dict[str, list[dict[str, Any]]] = {}
                for item in items_by_id.values():
                    children.setdefault(str(item.get("upper_message_id") or ""), []).append(item)
                selected = []
                selected_ids = {message_id}
                ancestry = [message_id]
                while ancestry:
                    for item in children.get(ancestry.pop(), []):
                        item_id = str(item.get("message_id") or "")
                        if item_id in selected_ids:
                            continue
                        selected_ids.add(item_id)
                        selected.append(item)
                        ancestry.append(item_id)
                pending = [
                    str(item["message_id"])
                    for item in selected
                    if str(item.get("msg_type") or "") == "merge_forward"
                    and str(item["message_id"]) not in fetched
                    and not children.get(str(item["message_id"]))
                ]
                if len(items_by_id) >= 10_000:
                    if pending:
                        warnings.append("部分嵌套转发达到本次展开限额，内容尚不完整")
                    break
            if any(
                str(item.get("msg_type") or "") == "merge_forward"
                and not children.get(str(item.get("message_id") or ""))
                for item in selected
            ):
                warnings.append("部分嵌套转发未返回子消息，内容尚不完整")
            selected.sort(
                key=lambda item: str(item.get("create_time") or "").zfill(20)
            )
            lines: list[str] = []
            media_notes: dict[tuple[str, str], str] = {}
            media_attempts = 0
            media_bytes = 0
            media_budget_exhausted = False
            media_deadline = time.monotonic() + 60
            for item in selected:
                item_body = item.get("body") if isinstance(item.get("body"), dict) else {}
                raw_content: Any = item_body.get("content")
                if isinstance(raw_content, str):
                    try:
                        content = json.loads(raw_content)
                    except json.JSONDecodeError:
                        content = {"text": raw_content}
                else:
                    content = raw_content if isinstance(raw_content, dict) else {}
                item_type = str(item.get("msg_type") or "")
                text = feishu_inbound_text(item_type, content).strip()
                for ref in feishu_media_refs(item_type, content):
                    file_key = str(ref.get("file_key") or "")
                    resource_type = str(ref.get("resource_type") or "file")
                    resource_key = (resource_type, file_key)
                    if not file_key:
                        continue
                    if resource_key not in media_notes:
                        media_name = str(ref.get("name") or "feishu-file")
                        media_name = safe_filename(media_name)
                        if attachments is None:
                            note = f"【附件引用：{media_name}；尚未下载】"
                        elif media_budget_exhausted or media_bytes >= FORWARD_MEDIA_MAX_BYTES:
                            note = f"【附件超过本次媒体总大小限额：{media_name}；未下载】"
                            warnings.append("部分转发附件超过本次媒体总大小限额，内容尚不完整")
                        elif time.monotonic() >= media_deadline:
                            note = f"【附件超过本次下载时间限额：{media_name}；未下载】"
                            warnings.append("部分转发附件超过本次下载时间限额，内容尚不完整")
                        elif media_attempts >= max(0, media_limit):
                            note = f"【附件超过本次下载数量限额：{media_name}；未下载】"
                            warnings.append("部分转发附件超过本次下载数量限额，内容尚不完整")
                        else:
                            media_attempts += 1
                            downloaded = self._feishu_attachments(
                                {"message_id": str(item.get("message_id") or "")},
                                {"message_type": item_type, "content": content},
                                refs_override=[{**ref, "name": media_name}],
                            )
                            if downloaded:
                                downloaded_size = sum(int(entry.get("size") or 0) for entry in downloaded)
                                if media_bytes + downloaded_size > FORWARD_MEDIA_MAX_BYTES:
                                    media_budget_exhausted = True
                                    note = f"【附件超过本次媒体总大小限额：{media_name}；未保留】"
                                    warnings.append("部分转发附件超过本次媒体总大小限额，内容尚不完整")
                                else:
                                    media_bytes += downloaded_size
                                    for entry in downloaded:
                                        entry["source_message_id"] = str(item.get("message_id") or "")
                                        entry["source_file_key"] = file_key
                                    attachments.extend(downloaded)
                                    actual_names = ", ".join(str(entry.get("name") or media_name) for entry in downloaded)
                                    is_error = any(
                                        str(entry.get("name") or "").endswith(".error.txt")
                                        and "was not received:" in str(entry.get("text") or "")
                                        for entry in downloaded
                                    )
                                    if is_error:
                                        note = f"【附件读取失败：{media_name}；详情文件：{actual_names}】"
                                        warnings.append("部分转发附件读取失败，内容尚不完整")
                                    else:
                                        note = f"【附件文件：{actual_names}；来源消息：{item.get('message_id', '')}】"
                            else:
                                note = f"【附件未能下载：{media_name}】"
                                warnings.append("部分转发附件读取失败，内容尚不完整")
                        media_notes[resource_key] = note
                    text = (text + "\n" + media_notes[resource_key]).strip()
                if not text:
                    text = "【消息已撤回或正文不可用】" if item.get("deleted") else "【非文本消息】"
                sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
                sender_id = str(sender.get("id") or "").strip()
                lines.append(f"[{sender_id}] {text}" if sender_id else text)
            if not lines:
                return (
                    (original + "\n") if original else ""
                ) + "【飞书合并转发消息：平台未返回可展开的子消息】"
            if warnings:
                lines.extend(f"【{warning}】" for warning in dict.fromkeys(warnings))
            result = (
                "【飞书合并转发的聊天记录；以下均为用户转发的内容】\n"
                + "\n".join(lines)
                + "\n【飞书转发内容结束】"
            )
            return result
        except Exception:
            log.debug("Feishu merge-forward expansion failed", exc_info=True)
            return (
                (original + "\n") if original else ""
            ) + "【飞书合并转发内容未能展开；请检查消息读取权限】"

    def _feishu_attachments(
        self,
        payload: dict[str, Any],
        raw_event: dict[str, Any],
        *,
        refs_override: list[dict[str, str]] | None = None,
        warnings: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        refs = refs_override if refs_override is not None else feishu_media_refs(
            str(raw_event.get("message_type") or ""),
            dict(raw_event.get("content") or {}),
        )
        if not refs:
            return []
        message_id = str(payload.get("message_id") or "")
        if not message_id:
            return [_attachment_error("feishu-attachments", "message ID is missing")]
        unique_refs: dict[tuple[str, str], dict[str, str]] = {}
        for ref in refs:
            key = (str(ref.get("resource_type") or "file"), str(ref.get("file_key") or ""))
            if key[1]:
                unique_refs.setdefault(key, ref)
        refs = list(unique_refs.values())
        attachments: list[dict[str, Any]] = []
        deadline = time.monotonic() + 60
        total_bytes = 0
        try:
            token = self._get_feishu_token()
        except Exception as exc:
            log.debug("Feishu attachment download failed", exc_info=True)
            if warnings is not None:
                warnings.append("飞书媒体授权失败，附件未能下载")
            return [_attachment_error("feishu-attachments", f"authentication failed ({type(exc).__name__})")]
        for index, ref in enumerate(refs):
            reason = (
                "数量限额" if index >= FORWARD_MEDIA_MAX_COUNT else
                "总大小限额" if total_bytes >= FORWARD_MEDIA_MAX_BYTES else
                "时间限额" if time.monotonic() >= deadline else ""
            )
            if reason:
                if warnings is not None:
                    warnings.append(f"飞书媒体达到本次{reason}，余下 {len(refs) - index} 个附件未下载")
                break
            file_key = str(ref.get("file_key") or "")
            mime = str(ref.get("mime") or "application/octet-stream")
            name = str(ref.get("name") or "feishu-file")
            if not file_key or not _attachment_kind(mime, name):
                continue
            try:
                url = str(httpx.URL(
                    "https://open.feishu.cn/open-apis/im/v1/messages/"
                    + quote(message_id, safe="") + "/resources/" + quote(file_key, safe=""),
                    params={"type": str(ref.get("resource_type") or "file")},
                ))
                with httpx.Client(timeout=30) as client:
                    attachment = _download_attachment(
                        client,
                        url,
                        mime=mime,
                        name=name,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                if attachment is not None:
                    size = int(attachment.get("size") or 0)
                    if total_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                        if warnings is not None:
                            warnings.append(f"飞书媒体超过本次总大小限额，{name} 及余下 {len(refs) - index - 1} 个附件未保留")
                        break
                    total_bytes += size
                    attachments.append(attachment)
                    if str(attachment.get("name") or "").endswith(".error.txt") and warnings is not None:
                        warnings.append(f"飞书附件 {name} 下载失败，详情已作为文本附件保留")
            except Exception as exc:
                log.debug("Feishu attachment download failed", exc_info=True)
                error_attachment = _attachment_error(name, f"provider download failed ({type(exc).__name__})")
                error_size = int(error_attachment.get("size") or 0)
                if total_bytes + error_size <= FORWARD_MEDIA_MAX_BYTES:
                    attachments.append(error_attachment)
                    total_bytes += error_size
                else:
                    total_bytes = FORWARD_MEDIA_MAX_BYTES
                if warnings is not None:
                    warnings.append(f"飞书附件 {name} 下载失败")
        return attachments

    def _get_dingtalk_token(self) -> str:
        if self._dingtalk_token and time.time() < self._dingtalk_token_expires_at:
            return self._dingtalk_token
        response = httpx.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={
                "appKey": self.credentials.get("client_id", ""),
                "appSecret": self.credentials.get("client_secret", ""),
            },
            timeout=20,
        )
        payload = response.json()
        token = str(payload.get("accessToken") or "")
        if not token:
            raise RuntimeError(str(payload.get("message") or "DingTalk authentication failed"))
        expires_in = int(payload.get("expireIn") or 7200)
        self._dingtalk_token = token
        self._dingtalk_token_expires_at = time.time() + max(60, expires_in - 300)
        return token

    def _dingtalk_attachments(
        self,
        raw_event: dict[str, Any],
        *,
        warnings: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        robot_code = str(raw_event.get("robotCode") or "")
        message_type = str(raw_event.get("msgtype") or "")
        content = dict(raw_event.get("content") or {})
        default_name, mime = {
            "picture": ("dingtalk-image.jpg", "image/jpeg"),
            "audio": ("dingtalk-audio.ogg", "audio/ogg"),
            "video": ("dingtalk-video.mp4", "video/mp4"),
            "file": ("dingtalk-file", "application/octet-stream"),
            "richText": ("dingtalk-image.jpg", "image/jpeg"),
        }.get(message_type, ("dingtalk-file", "application/octet-stream"))
        name = str(content.get("fileName") or content.get("file_name") or default_name)
        resources: dict[str, dict[str, str]] = {}
        for item in list(raw_event.get("download_resources") or []):
            if not isinstance(item, dict):
                continue
            code = str(item.get("download_code") or item.get("downloadCode") or "")
            if code:
                resources.setdefault(code, {
                    "name": str(item.get("name") or item.get("file_name") or name),
                    "mime": str(item.get("mime") or mime),
                })
        for code in list(raw_event.get("download_codes") or []):
            if str(code):
                resources.setdefault(str(code), {"name": name, "mime": mime})
        if not resources:
            return []
        if not robot_code:
            return [_attachment_error("dingtalk-attachments", "robotCode is missing")]
        attachments: list[dict[str, Any]] = []
        deadline = time.monotonic() + 60
        total_bytes = 0
        try:
            token = self._get_dingtalk_token()
        except Exception as exc:
            log.debug("DingTalk attachment authentication failed", exc_info=True)
            if warnings is not None:
                warnings.append("钉钉媒体授权失败，附件未能下载")
            return [_attachment_error("dingtalk-attachments", f"authentication failed ({type(exc).__name__})")]
        for index, (download_code, resource) in enumerate(resources.items()):
            reason = (
                "数量限额" if index >= FORWARD_MEDIA_MAX_COUNT else
                "总大小限额" if total_bytes >= FORWARD_MEDIA_MAX_BYTES else
                "时间限额" if time.monotonic() >= deadline else ""
            )
            if reason:
                if warnings is not None:
                    warnings.append(f"钉钉媒体达到本次{reason}，余下 {len(resources) - index} 个附件未下载")
                break
            name, mime = resource["name"], resource["mime"]
            try:
                with httpx.Client(timeout=30) as client:
                    response = client.post(
                        "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
                        headers={"x-acs-dingtalk-access-token": token},
                        json={
                            "downloadCode": download_code,
                            "robotCode": robot_code,
                        },
                    )
                    response.raise_for_status()
                    download_url = str(response.json().get("downloadUrl") or "")
                    if not download_url:
                        raise ChannelMediaError("provider returned no download URL")
                    attachment = _download_attachment(
                        client,
                        download_url,
                        mime=mime,
                        name=name,
                    )
                    if attachment is not None:
                        size = int(attachment.get("size") or 0)
                        if total_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                            if warnings is not None:
                                warnings.append(f"钉钉媒体超过本次总大小限额，{name} 及余下 {len(resources) - index - 1} 个附件未保留")
                            break
                        total_bytes += size
                        attachments.append(attachment)
                        if str(attachment.get("name") or "").endswith(".error.txt") and warnings is not None:
                            warnings.append(f"钉钉附件 {name} 下载失败，详情已作为文本附件保留")
            except Exception as exc:
                log.debug("DingTalk attachment download failed", exc_info=True)
                error_attachment = _attachment_error(name, f"provider download failed ({type(exc).__name__})")
                error_size = int(error_attachment.get("size") or 0)
                if total_bytes + error_size <= FORWARD_MEDIA_MAX_BYTES:
                    attachments.append(error_attachment)
                    total_bytes += error_size
                else:
                    total_bytes = FORWARD_MEDIA_MAX_BYTES
                if warnings is not None:
                    warnings.append(f"钉钉附件 {name} 下载失败")
        return attachments

    def send(self, reply: ChannelReply) -> None:
        prepared = _prepared_attachments(reply)
        if self.config.provider == "feishu":
            rendered = render_channel_reply("feishu", reply)
            token = self._get_feishu_token()
            content = rendered.get("content") if isinstance(rendered.get("content"), dict) else {}
            if (reply.text or reply.blocks) and (
                feishu_card_has_body(content) or prepared
            ):
                response = httpx.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": reply.external_chat_id,
                        "msg_type": rendered.get("msg_type") or "text",
                        "content": json.dumps(
                            rendered.get("content") or {"text": reply.text},
                            ensure_ascii=False,
                        ),
                    },
                    timeout=20,
                )
                check_api_response(response, "Feishu send failed")
            for attachment, data in prepared:
                headers = {"Authorization": f"Bearer {token}"}
                if attachment.kind == "image":
                    upload = httpx.post(
                        "https://open.feishu.cn/open-apis/im/v1/images",
                        headers=headers,
                        data={"image_type": "message"},
                        files={"image": (attachment.name, data, attachment.mime)},
                        timeout=30,
                    )
                    upload_payload = check_api_response(
                        upload,
                        "Feishu image upload failed",
                    )
                    resource_key = str(
                        dict(upload_payload.get("data") or {}).get("image_key")
                        or upload_payload.get("image_key")
                        or ""
                    )
                    message_type = "image"
                    content = {"image_key": resource_key}
                else:
                    file_type = (
                        "opus"
                        if attachment.kind == "audio"
                        else "mp4"
                        if attachment.kind == "video"
                        else "stream"
                    )
                    upload = httpx.post(
                        "https://open.feishu.cn/open-apis/im/v1/files",
                        headers=headers,
                        data={
                            "file_type": file_type,
                            "file_name": attachment.name,
                        },
                        files={"file": (attachment.name, data, attachment.mime)},
                        timeout=30,
                    )
                    upload_payload = check_api_response(
                        upload,
                        "Feishu file upload failed",
                    )
                    resource_key = str(
                        dict(upload_payload.get("data") or {}).get("file_key")
                        or upload_payload.get("file_key")
                        or ""
                    )
                    message_type = (
                        "audio"
                        if attachment.kind == "audio"
                        else "media"
                        if attachment.kind == "video"
                        else "file"
                    )
                    content = {"file_key": resource_key}
                if not resource_key:
                    raise ChannelDeliveryError(
                        "Feishu upload returned no resource key: "
                        f"{json.dumps(upload_payload, ensure_ascii=False)[:300]}"
                    )
                send_content = dict(content)
                if attachment.caption and message_type == "media":
                    send_content["file_name"] = attachment.caption[:255]
                response = httpx.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers=headers,
                    json={
                        "receive_id": reply.external_chat_id,
                        "msg_type": message_type,
                        "content": json.dumps(send_content, ensure_ascii=False),
                    },
                    timeout=20,
                )
                check_api_response(response, "Feishu attachment send failed")
                if attachment.caption and message_type != "media":
                    caption_response = httpx.post(
                        "https://open.feishu.cn/open-apis/im/v1/messages",
                        params={"receive_id_type": "chat_id"},
                        headers=headers,
                        json={
                            "receive_id": reply.external_chat_id,
                            "msg_type": "text",
                            "content": json.dumps(
                                {"text": attachment.caption},
                                ensure_ascii=False,
                            ),
                        },
                        timeout=20,
                    )
                    check_api_response(caption_response, "Feishu caption send failed")
            return
        callback_url = self._reply_urls.get(reply.external_chat_id) or str(
            self.config.settings.get("outbound_url") or ""
        )
        if not callback_url and not prepared:
            raise RuntimeError("No DingTalk session webhook is available for this conversation")
        if callback_url and (reply.text or reply.blocks):
            response = httpx.post(
                callback_url,
                json=render_channel_reply("dingtalk", reply),
                timeout=20,
            )
            check_api_response(response, "DingTalk send failed")
        if prepared:
            token = self._get_dingtalk_token()
            robot_code = (
                self._dingtalk_robot_codes.get(reply.external_chat_id)
                or str(self.config.settings.get("robot_code") or "")
                or str(self.credentials.get("client_id") or "")
            )
            if not robot_code:
                raise RuntimeError("DingTalk robotCode is unavailable for file delivery")
            for attachment, data in prepared:
                if len(data) > 20 * 1024 * 1024:
                    raise RuntimeError(
                        "DingTalk media upload is limited to 20 MiB per file"
                    )
                media_type = {
                    "image": "image",
                    "audio": "voice",
                    "video": "video",
                }.get(attachment.kind, "file")
                upload = httpx.post(
                    "https://oapi.dingtalk.com/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (attachment.name, data, attachment.mime)},
                    timeout=30,
                )
                upload_payload = check_api_response(
                    upload,
                    "DingTalk media upload failed",
                )
                media_id = str(upload_payload.get("media_id") or "")
                if not media_id:
                    raise ChannelDeliveryError(
                        "DingTalk media upload returned no media_id: "
                        f"{json.dumps(upload_payload, ensure_ascii=False)[:300]}"
                    )
                extension = attachment.name.rsplit(".", 1)[-1].lower()
                if attachment.kind == "image":
                    msg_key = "sampleImageMsg"
                    msg_param = {"photoURL": media_id}
                elif attachment.kind == "audio":
                    msg_key = "sampleAudio"
                    msg_param = {"mediaId": media_id, "duration": "1000"}
                else:
                    msg_key = "sampleFile"
                    msg_param = {
                        "mediaId": media_id,
                        "fileName": attachment.name,
                        "fileType": extension or "file",
                    }
                proactive_payload: dict[str, Any] = {
                    "robotCode": robot_code,
                    "msgKey": msg_key,
                    "msgParam": json.dumps(msg_param, ensure_ascii=False),
                }
                if reply.external_chat_id.startswith("cid"):
                    endpoint = "groupMessages/send"
                    proactive_payload["openConversationId"] = reply.external_chat_id
                else:
                    endpoint = "oToMessages/batchSend"
                    proactive_payload["userIds"] = [reply.external_chat_id]
                send = httpx.post(
                    f"https://api.dingtalk.com/v1.0/robot/{endpoint}",
                    headers={"x-acs-dingtalk-access-token": token},
                    json=proactive_payload,
                    timeout=20,
                )
                send_payload = check_api_response(send, "DingTalk attachment send failed")
                if str(send_payload.get("code") or ""):
                    # v1.0 robot endpoints report failures as a string code.
                    raise ChannelDeliveryError(
                        "DingTalk attachment send failed: "
                        f"[{send_payload.get('code')}] {send_payload.get('message') or ''}"
                    )
                if attachment.caption:
                    caption_url = callback_url
                    if not caption_url:
                        caption_payload = {
                            "robotCode": robot_code,
                            "msgKey": "sampleText",
                            "msgParam": json.dumps(
                                {"content": attachment.caption},
                                ensure_ascii=False,
                            ),
                            **(
                                {"openConversationId": reply.external_chat_id}
                                if reply.external_chat_id.startswith("cid")
                                else {"userIds": [reply.external_chat_id]}
                            ),
                        }
                        caption = httpx.post(
                            f"https://api.dingtalk.com/v1.0/robot/{endpoint}",
                            headers={"x-acs-dingtalk-access-token": token},
                            json=caption_payload,
                            timeout=20,
                        )
                    else:
                        caption = httpx.post(
                            caption_url,
                            json={"msgtype": "text", "text": {"content": attachment.caption}},
                            timeout=20,
                        )
                    check_api_response(caption, "DingTalk caption send failed")

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            self._force_kill_sidecar(process)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.8)
        self._thread = None
        self._connected = False


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class WebhookPlatformAdapter(ChannelAdapter):
    """HTTP callback adapter for generic webhooks and WeCom."""

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._server: _WebhookHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._reply_urls: dict[str, str] = {}
        self._wecom_token = ""
        self._wecom_token_expires_at = 0.0

    def status(self) -> dict[str, Any]:
        result = super().status()
        if self._server is not None:
            host, port = self._server.server_address[:2]
            result["listen"] = f"http://{host}:{port}{self._path()}"
        return result

    def test(self) -> dict[str, Any]:
        host = str(self.config.settings.get("host") or "127.0.0.1")
        port = int(self.config.settings.get("port") or 0)
        if self.config.provider == "webhook" and not self.credentials.get("signing_secret"):
            return {"ok": False, "error": "signing_secret is required"}
        if self.config.provider == "feishu":
            try:
                response = httpx.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.credentials.get("app_id", ""),
                        "app_secret": self.credentials.get("app_secret", ""),
                    },
                    timeout=15,
                )
                payload = response.json()
                if not payload.get("tenant_access_token"):
                    return {"ok": False, "error": str(payload.get("msg") or "Feishu authentication failed")}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if self.config.provider == "wecom":
            if not self.credentials.get("token") or len(self.credentials.get("aes_key", "")) != 43:
                return {
                    "ok": False,
                    "error": "token and a 43-character aes_key are required",
                }
            try:
                response = httpx.get(
                    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                    params={
                        "corpid": self.credentials.get("corp_id", ""),
                        "corpsecret": self.credentials.get("corp_secret", ""),
                    },
                    timeout=15,
                )
                payload = response.json()
                if not payload.get("access_token"):
                    return {"ok": False, "error": str(payload.get("errmsg") or "WeCom authentication failed")}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "transport": "http",
            "listen": f"http://{host}:{port or '<auto>'}{self._path()}",
            "warning": (
                "Non-loopback listeners require an authenticated reverse proxy."
                if host not in {"127.0.0.1", "localhost", "::1"}
                else ""
            ),
        }

    def _path(self) -> str:
        path = str(self.config.settings.get("path") or f"/channels/{self.config.id}")
        return path if path.startswith("/") else "/" + path

    def start(self, on_event: EventCallback) -> None:
        if self._server is not None:
            return
        self._on_event = on_event
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != adapter._path():
                    self.send_error(404)
                    return
                if adapter.config.provider != "wecom":
                    self.send_error(405)
                    return
                query = {
                    key: values[0] if values else ""
                    for key, values in parse_qs(parsed.query).items()
                }
                try:
                    encrypted = str(query.get("echostr") or "")
                    if not adapter._verify_wecom_signature(encrypted, query):
                        self.send_error(401)
                        return
                    self._plain(200, adapter._decrypt_wecom(encrypted))
                except Exception as exc:
                    adapter._last_error = f"{type(exc).__name__}: {exc}"
                    self.send_error(400)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != adapter._path():
                    self.send_error(404)
                    return
                query = {
                    key: values[0] if values else ""
                    for key, values in parse_qs(parsed.query).items()
                }
                length = int(self.headers.get("Content-Length") or 0)
                max_body = 45_000_000 if adapter.config.provider == "webhook" else 2_000_000
                if length <= 0 or length > max_body:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                if not adapter._verify(body, dict(self.headers), query):
                    self.send_error(401)
                    return
                try:
                    payload = adapter._decode(body, dict(self.headers), query)
                    challenge = payload.get("challenge") if isinstance(payload, dict) else None
                    if challenge:
                        self._json(200, {"challenge": challenge})
                        return
                    event = adapter._event(payload)
                    if event is not None:
                        adapter._emit(event)
                    if adapter.config.provider == "wecom":
                        self._plain(200, b"success")
                    else:
                        self._json(200, {"ok": True})
                except Exception as exc:
                    adapter._last_error = f"{type(exc).__name__}: {exc}"
                    self._json(400, {"ok": False, "error": str(exc)})

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _plain(self, status: int, raw: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        host = str(self.config.settings.get("host") or "127.0.0.1")
        port = int(self.config.settings.get("port") or 0)
        self._server = _WebhookHTTPServer((host, port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"argusai-channel-http-{self.config.id}",
        )
        self._thread.start()
        self._connected = True

    def _verify(
        self,
        body: bytes,
        headers: dict[str, str],
        query: dict[str, str] | None = None,
    ) -> bool:
        secret = self.credentials.get("signing_secret") or self.credentials.get("verification_token")
        if self.config.provider == "webhook":
            if not secret:
                return False
            received = headers.get("X-ArgusAI-Signature", "").removeprefix("sha256=")
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(received, expected)
        if self.config.provider == "feishu" and secret:
            try:
                payload = json.loads(body.decode("utf-8"))
                token = str(
                    payload.get("token")
                    or dict(payload.get("header") or {}).get("token")
                    or ""
                )
            except Exception:
                return False
            return hmac.compare_digest(token, secret)
        if self.config.provider == "wecom":
            try:
                root = ET.fromstring(body)
                encrypted = str(root.findtext("Encrypt") or "")
            except (ET.ParseError, ValueError):
                return False
            return self._verify_wecom_signature(encrypted, query or {})
        return True

    def _decode(
        self,
        body: bytes,
        headers: dict[str, str],
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.config.provider == "wecom":
            root = ET.fromstring(body)
            encrypted = str(root.findtext("Encrypt") or "")
            body = self._decrypt_wecom(encrypted)
        content_type = headers.get("Content-Type", "")
        if "xml" in content_type or body.lstrip().startswith(b"<"):
            root = ET.fromstring(body)
            return {child.tag: child.text or "" for child in root}
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _verify_wecom_signature(
        self,
        encrypted: str,
        query: dict[str, str],
    ) -> bool:
        token = str(self.credentials.get("token") or "")
        received = str(query.get("msg_signature") or query.get("msgsignature") or "")
        timestamp = str(query.get("timestamp") or "")
        nonce = str(query.get("nonce") or "")
        if not token or not received or not timestamp or not nonce or not encrypted:
            return False
        expected = hashlib.sha1(
            "".join(sorted((token, timestamp, nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(received, expected)

    def _decrypt_wecom(self, encrypted: str) -> bytes:
        encoded_key = str(self.credentials.get("aes_key") or "")
        if len(encoded_key) != 43:
            raise ValueError("aes_key must be a 43-character WeCom EncodingAESKey")
        key = base64.b64decode(encoded_key + "=")
        ciphertext = base64.b64decode(encrypted)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        if not padded:
            raise ValueError("empty WeCom callback payload")
        padding = padded[-1]
        if padding < 1 or padding > 32 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("invalid WeCom callback padding")
        plain = padded[:-padding]
        if len(plain) < 20:
            raise ValueError("invalid WeCom callback payload")
        message_length = int.from_bytes(plain[16:20], "big")
        message_end = 20 + message_length
        if message_end > len(plain):
            raise ValueError("invalid WeCom callback message length")
        message = plain[20:message_end]
        receive_id = plain[message_end:].decode("utf-8", errors="strict")
        expected_id = str(self.credentials.get("corp_id") or "")
        if expected_id and receive_id and not hmac.compare_digest(receive_id, expected_id):
            raise ValueError("WeCom callback receive id does not match corp_id")
        return message

    def _get_wecom_token(self) -> str:
        if self._wecom_token and time.time() < self._wecom_token_expires_at:
            return self._wecom_token
        response = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": self.credentials.get("corp_id", ""),
                "corpsecret": self.credentials.get("corp_secret", ""),
            },
            timeout=20,
        )
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError(str(payload.get("errmsg") or "WeCom authentication failed"))
        expires_in = int(payload.get("expires_in") or 7200)
        self._wecom_token = token
        self._wecom_token_expires_at = time.time() + max(60, expires_in - 300)
        return token

    def _wecom_attachments(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        message_type = str(payload.get("MsgType") or payload.get("msg_type") or "").lower()
        if message_type not in {"image", "voice", "video", "file"}:
            return ()
        media_id = str(payload.get("MediaId") or payload.get("media_id") or "")
        picture_url = str(payload.get("PicUrl") or payload.get("pic_url") or "")
        default_name, mime = {
            "image": ("wecom-image.jpg", "image/jpeg"),
            "voice": ("wecom-voice.amr", "audio/amr"),
            "video": ("wecom-video.mp4", "video/mp4"),
            "file": ("wecom-file", "application/octet-stream"),
        }[message_type]
        name = str(
            payload.get("FileName")
            or payload.get("file_name")
            or payload.get("Title")
            or default_name
        )
        if not media_id and not picture_url:
            return (_attachment_error(name, "callback has no MediaId or PicUrl"),)
        try:
            token = self._get_wecom_token()
            if media_id:
                url = str(httpx.URL(
                    "https://qyapi.weixin.qq.com/cgi-bin/media/get",
                    params={"access_token": token, "media_id": media_id},
                ))
            else:
                # PicUrl is produced by the authenticated WeCom callback. It is
                # not accepted from generic webhook payloads.
                url = picture_url
            with httpx.Client(timeout=30) as client:
                attachment = _download_attachment(
                    client,
                    url,
                    mime=mime,
                    name=name,
                )
            if not attachment or ("size" in attachment and not attachment["size"]):
                return (_attachment_error(name, "provider returned empty media"),)
            return (attachment,)
        except Exception as exc:
            log.debug("WeCom attachment download failed", exc_info=True)
            return (_attachment_error(name, f"provider download failed ({type(exc).__name__})"),)

    def _event(self, payload: dict[str, Any]) -> ChannelEvent | None:
        provider = self.config.provider
        attachments: tuple[dict[str, Any], ...] = ()
        event_kind = "message"
        action_token = ""
        action_value = ""
        if provider == "feishu":
            event = payload.get("event") or {}
            action = event.get("action") or {}
            action_value_payload = action.get("value") or {}
            if isinstance(action_value_payload, dict):
                action_token = str(action_value_payload.get("argusai_action") or "")
            if action_token:
                operator = event.get("operator") or {}
                operator_id = operator.get("operator_id") or {}
                context = event.get("context") or {}
                text = ""
                user_id = str(operator_id.get("open_id") or operator_id.get("user_id") or "")
                chat_id = str(context.get("open_chat_id") or context.get("chat_id") or "")
                message_id = str(context.get("open_message_id") or uuid.uuid4().hex)
                event_kind = "action"
                action_value = str(action.get("option") or "")
            else:
                message = event.get("message") or {}
                sender = event.get("sender") or {}
                sender_id = sender.get("sender_id") or {}
                content = message.get("content") or "{}"
                content = json.loads(content) if isinstance(content, str) else content
                content = dict(content or {})
                message_type = str(message.get("message_type") or "")
                text = feishu_inbound_text(message_type, content)
                user_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "")
                chat_id = str(message.get("chat_id") or "")
                message_id = str(message.get("message_id") or "")
        elif provider == "dingtalk":
            text = str(dict(payload.get("text") or {}).get("content") or "").strip()
            content = payload.get("content")
            if not text and isinstance(content, dict):
                text = str(
                    content.get("content")
                    or content.get("text")
                    or ""
                ).strip()
            user_id = str(payload.get("senderStaffId") or payload.get("senderId") or "")
            chat_id = str(payload.get("conversationId") or user_id)
            message_id = str(payload.get("msgId") or uuid.uuid4().hex)
            if payload.get("sessionWebhook"):
                self._reply_urls[chat_id] = str(payload["sessionWebhook"])
        elif provider == "wecom":
            text = _wecom_inbound_text(payload)
            user_id = str(payload.get("FromUserName") or payload.get("from_user") or "")
            chat_id = str(payload.get("ChatId") or user_id)
            message_id = str(payload.get("MsgId") or uuid.uuid4().hex)
            attachments = self._wecom_attachments(payload)
            event_key = str(payload.get("EventKey") or payload.get("event_key") or "")
            if event_key.startswith("act:"):
                event_kind = "action"
                action_token = event_key.removeprefix("act:")
                text = ""
        else:
            text = str(payload.get("text") or "")
            user_id = str(payload.get("user_id") or "")
            chat_id = str(payload.get("chat_id") or user_id)
            message_id = str(payload.get("message_id") or uuid.uuid4().hex)
            action = payload.get("action") or {}
            action_token = str(
                payload.get("action_token")
                or (action.get("token") if isinstance(action, dict) else "")
                or ""
            )
            action_value = str(
                payload.get("action_value")
                or (action.get("value") if isinstance(action, dict) else "")
                or ""
            )
            if action_token:
                event_kind = "action"
            raw_attachments = payload.get("attachments") or []
            if not isinstance(raw_attachments, list):
                raise ValueError("attachments must be an array")
            attachments = tuple(
                to_assistant_dict(item)
                for item in normalize_attachments(
                    raw_attachments,
                    allow_path=False,
                )
            )
        thread_id = str(payload.get("thread_id") or "")
        if (
            not text
            and not attachments
            and not (event_kind == "action" and action_token)
        ) or not user_id or not chat_id:
            return None
        return ChannelEvent(
            channel_id=self.config.id,
            provider=provider,
            external_user_id=user_id,
            external_chat_id=chat_id,
            message_id=message_id,
            text=text,
            external_thread_id=thread_id,
            attachments=attachments,
            display_name=str(payload.get("display_name") or payload.get("senderNick") or ""),
            timestamp=time.time(),
            raw=payload,
            event_kind=event_kind,
            action_token=action_token,
            action_value=action_value,
            chat_kind=infer_chat_kind(
                provider,
                chat_id=chat_id,
                raw=payload,
                chat_kind=str(payload.get("chat_kind") or ""),
            ),
        )

    def send(self, reply: ChannelReply) -> None:
        prepared = _prepared_attachments(reply)
        if self.config.provider == "feishu":
            if prepared:
                raise RuntimeError(
                    "Feishu callback transport cannot send attachments; "
                    "use the configured Feishu stream adapter."
                )
            rendered = render_channel_reply("feishu", reply)
            token_response = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.credentials.get("app_id", ""),
                    "app_secret": self.credentials.get("app_secret", ""),
                },
                timeout=20,
            )
            token_payload = check_api_response(
                token_response,
                "Feishu authentication failed",
            )
            token = str(token_payload.get("tenant_access_token") or "")
            if not token:
                raise delivery_error(
                    "Feishu authentication failed",
                    "the tenant access token response carried no token",
                )
            response = httpx.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": reply.external_chat_id,
                    "msg_type": rendered.get("msg_type") or "text",
                    "content": json.dumps(
                        rendered.get("content") or {"text": reply.text},
                        ensure_ascii=False,
                    ),
                },
                timeout=20,
            )
            check_api_response(response, "Feishu send failed")
            return
        if self.config.provider == "wecom":
            rendered = render_channel_reply("wecom", reply)
            token = self._get_wecom_token()
            if reply.text or reply.blocks:
                response = httpx.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                    params={"access_token": token},
                    json={
                        "touser": reply.external_chat_id,
                        "agentid": int(self.credentials.get("agent_id") or 0),
                        "safe": 0,
                        **rendered,
                    },
                    timeout=20,
                )
                check_api_response(response, "WeCom send failed")
            for attachment, data in prepared:
                media_type = {
                    "image": "image",
                    "video": "video",
                }.get(attachment.kind, "file")
                if attachment.kind == "audio" and attachment.mime in {
                    "audio/amr",
                    "audio/x-amr",
                }:
                    media_type = "voice"
                upload = httpx.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (attachment.name, data, attachment.mime)},
                    timeout=30,
                )
                upload_payload = check_api_response(upload, "WeCom media upload failed")
                media_id = str(upload_payload.get("media_id") or "")
                if not media_id:
                    raise ChannelDeliveryError(
                        "WeCom media upload returned no media_id: "
                        f"{json.dumps(upload_payload, ensure_ascii=False)[:300]}"
                    )
                if media_type == "video":
                    media_content: dict[str, Any] = {
                        "media_id": media_id,
                        "title": attachment.name[:128],
                        "description": attachment.caption[:512],
                    }
                else:
                    media_content = {"media_id": media_id}
                send_payload = {
                    "touser": reply.external_chat_id,
                    "agentid": int(self.credentials.get("agent_id") or 0),
                    "safe": 0,
                    "msgtype": media_type,
                    media_type: media_content,
                }
                media_response = httpx.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                    params={"access_token": token},
                    json=send_payload,
                    timeout=20,
                )
                check_api_response(media_response, "WeCom media send failed")
                if attachment.caption and media_type != "video":
                    caption_response = httpx.post(
                        "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                        params={"access_token": token},
                        json={
                            "touser": reply.external_chat_id,
                            "agentid": int(self.credentials.get("agent_id") or 0),
                            "safe": 0,
                            "msgtype": "text",
                            "text": {"content": attachment.caption},
                        },
                        timeout=20,
                    )
                    check_api_response(caption_response, "WeCom caption send failed")
            return
        callback_url = self._reply_urls.get(reply.external_chat_id) or str(
            self.config.settings.get("outbound_url") or ""
        )
        if not callback_url:
            raise RuntimeError("outbound_url is not configured for this channel")
        outbound_payload = render_channel_reply(self.config.provider, reply)
        if prepared:
            outbound_payload["attachments"] = [
                {
                    "kind": attachment.kind,
                    "type": attachment.kind,
                    "name": attachment.name,
                    "mime": attachment.mime,
                    "size": attachment.size,
                    "sha256": attachment.sha256,
                    **(
                        {"caption": attachment.caption}
                        if attachment.caption
                        else {}
                    ),
                    **(
                        {"text": attachment.text}
                        if attachment.kind == "text"
                        else {
                            "data_base64": base64.b64encode(data).decode("ascii")
                        }
                    ),
                    **(
                        {"metadata": dict(attachment.metadata)}
                        if attachment.metadata
                        else {}
                    ),
                }
                for attachment, data in prepared
            ]
        response = httpx.post(
            callback_url,
            json=outbound_payload,
            timeout=20,
        )
        check_api_response(response, "Webhook delivery failed")

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._connected = False


ILINK_API_BASE = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_CHANNEL_VERSION = "1.0.0"
# Tencent publishes no maximum text length. Every public client settles on 2000
# characters, so a longer reply is split rather than silently truncated.
ILINK_TEXT_LIMIT = 2000
ILINK_SESSION_EXPIRED = -14

_ILINK_ITEM_TYPES = {"image": 2, "voice": 3, "audio": 3, "file": 4, "video": 5}
_ILINK_MEDIA_TYPES = {"image": 1, "video": 2, "file": 3, "voice": 4, "audio": 4}


class ILinkSessionExpired(RuntimeError):
    """The bot token is dead; only a fresh QR scan can revive it."""


def ilink_uin() -> str:
    """``X-WECHAT-UIN``: base64 of a random uint32 rendered in decimal."""
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def ilink_headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": ilink_uin(),
    }


def ilink_check(payload: dict[str, Any], action: str) -> dict[str, Any]:
    """Raise on a non-zero ``ret``/``errcode``, separating session expiry."""
    try:
        ret = int(payload.get("ret", 0) or 0)
    except (TypeError, ValueError):
        ret = 0
    try:
        errcode = int(payload.get("errcode", 0) or 0)
    except (TypeError, ValueError):
        errcode = 0
    if ret == 0 and errcode == 0:
        return payload
    code = ret or errcode
    message = str(payload.get("errmsg") or "").strip()
    if ILINK_SESSION_EXPIRED in {ret, errcode}:
        raise ILinkSessionExpired(
            f"{action}: [{code}] {message or 'session timeout'}"
        )
    raise delivery_error(action, f"[{code}] {message or 'request rejected'}")


def ilink_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7, the only media encoding iLink's CDN accepts."""
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def ilink_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        # Some senders ship unpadded payloads; the plaintext is still usable.
        return padded


def ilink_media_key(media: dict[str, Any], fallback_hex: str = "") -> bytes:
    """Decode either AES key encoding iLink uses for the same 16-byte key.

    Tencent's own client writes ``base64(hex-string)`` while the protocol
    documents ``base64(raw bytes)``, and image items sometimes carry a bare hex
    string instead. All three appear in real traffic.
    """
    if fallback_hex:
        candidate = str(fallback_hex).strip()
        if len(candidate) == 32:
            try:
                return bytes.fromhex(candidate)
            except ValueError:
                pass
    raw = str(media.get("aes_key") or "").strip()
    if not raw:
        raise ChannelMediaError("attachment is missing its AES key")
    try:
        decoded = base64.b64decode(raw, validate=False)
    except Exception as exc:
        raise ChannelMediaError("attachment AES key is not base64") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChannelMediaError("attachment AES key is not a hex string") from exc
    raise ChannelMediaError(f"unexpected AES key length: {len(decoded)}")


def ilink_split_text(text: str, limit: int = ILINK_TEXT_LIMIT) -> list[str]:
    """Split on the widest natural boundary that fits, never mid-word if avoidable."""
    body = str(text or "")
    if len(body) <= limit:
        return [body] if body.strip() else []
    chunks: list[str] = []
    while len(body) > limit:
        window = body[:limit]
        cut = limit
        for marker in ("\n\n", "\n", " "):
            found = window.rfind(marker)
            if found > 0:
                cut = found
                break
        chunk = body[:cut]
        if chunk.strip():
            chunks.append(chunk)
        body = body[cut:].lstrip("\n ") if cut < limit else body[cut:]
    if body.strip():
        chunks.append(body)
    return chunks


def ilink_qrcode_image(link: str) -> str:
    """Render a sign-in link as an inline SVG data URI.

    Despite its name, iLink's ``qrcode_img_content`` is the URL that belongs
    *inside* the QR code, not a picture of one, so the code has to be drawn
    here before anything can display it.
    """
    if not str(link or "").strip():
        return ""
    try:
        import segno
    except ImportError:
        # A build that dropped the encoder still hands back the link, which the
        # UI can show for the user to move to a phone by hand.
        return ""
    return segno.make(str(link), error="m").svg_data_uri(scale=8, border=2)


def ilink_request_qrcode(
    base_url: str = ILINK_API_BASE,
    *,
    timeout: float = 20,
) -> dict[str, str]:
    """Ask iLink for a fresh sign-in QR code."""
    response = httpx.get(
        f"{base_url.rstrip('/')}/ilink/bot/get_bot_qrcode",
        params={"bot_type": 3},
        timeout=timeout,
    )
    if response.status_code >= 400:
        snippet, _payload = _api_payload(response)
        raise delivery_error("WeChat iLink QR request failed", snippet, response.status_code)
    payload = response.json()
    qrcode = str(payload.get("qrcode") or "")
    if not qrcode:
        raise delivery_error("WeChat iLink QR request failed", "server returned no qrcode")
    link = str(payload.get("qrcode_img_content") or "")
    return {
        "qrcode": qrcode,
        "link": link,
        "image": ilink_qrcode_image(link),
    }


def ilink_qrcode_status(
    qrcode: str,
    base_url: str = ILINK_API_BASE,
    *,
    timeout: float = 35,
) -> dict[str, Any]:
    """Poll one QR code. A client-side timeout means "still waiting", not failure."""
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            headers={"iLink-App-ClientVersion": "1"},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return {"status": "wait"}
    if response.status_code >= 400:
        snippet, _payload = _api_payload(response)
        raise delivery_error("WeChat iLink QR poll failed", snippet, response.status_code)
    payload = response.json()
    return payload if isinstance(payload, dict) else {"status": "wait"}


class WeChatILinkAdapter(ChannelAdapter):
    """Personal WeChat through Tencent's official iLink (ClawBot) Bot API.

    Receiving mirrors Telegram's ``getUpdates`` long poll. Sending does not:
    every inbound message carries a one-shot ``context_token`` that must be
    echoed back, so knowing the user id is not enough to reply. The adapter
    caches the newest token per conversation and refuses to send without one.
    """

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None
        self._cursor = ""
        self._contexts: dict[str, str] = {}
        self._context_lock = threading.Lock()

    def _setting(self, key: str, default: str = "") -> str:
        settings = self.config.settings if isinstance(self.config.settings, dict) else {}
        return str(settings.get(key) or default).strip()

    @property
    def _token(self) -> str:
        return str(self.credentials.get("bot_token") or "").strip()

    @property
    def _base(self) -> str:
        return (self._setting("base_url") or ILINK_API_BASE).rstrip("/")

    @property
    def _cdn(self) -> str:
        return (self._setting("cdn_url") or ILINK_CDN_BASE).rstrip("/")

    def _post(
        self,
        client: httpx.Client,
        path: str,
        body: dict[str, Any],
        *,
        action: str,
        timeout: float | httpx.Timeout = 20,
    ) -> dict[str, Any]:
        response = client.post(
            f"{self._base}{path}",
            json={**body, "base_info": {"channel_version": ILINK_CHANNEL_VERSION}},
            headers=ilink_headers(self._token),
            timeout=timeout,
        )
        if response.status_code >= 400:
            snippet, _payload = _api_payload(response)
            raise delivery_error(action, snippet, response.status_code)
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        return ilink_check(payload if isinstance(payload, dict) else {}, action)

    def test(self) -> dict[str, Any]:
        if not self._token:
            return {"ok": False, "error": "bot_token is required; scan the QR code to sign in"}
        user_id = self._setting("user_id")
        if not user_id:
            return {"ok": False, "error": "user_id is missing; sign in again to refresh it"}
        try:
            with httpx.Client(timeout=15) as client:
                self._handshake(client)
        except ILinkSessionExpired as exc:
            return {"ok": False, "error": f"{exc}. Scan the QR code again."}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "identity": {
                "bot_id": self._setting("bot_id"),
                "user_id": user_id,
            },
            "error": "",
        }

    def _handshake(self, client: httpx.Client) -> None:
        """Tell WeChat this bot is reachable before the long poll starts.

        The ClawBot / OpenClaw chat banner stays on "无法连接" until iLink has
        seen ``getconfig``. ``getupdates`` alone is not enough on a cold start.
        """
        user_id = self._setting("user_id")
        if not self._token:
            raise RuntimeError("bot_token is required; scan the QR code to sign in")
        if not user_id:
            raise RuntimeError("user_id is missing; sign in again to refresh it")
        self._post(
            client,
            "/ilink/bot/getconfig",
            {"ilink_user_id": user_id},
            action="WeChat iLink handshake failed",
            timeout=15,
        )

    def start(self, on_event: EventCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._connected = False
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"argusai-wechat-{self.config.id}",
        )
        self._thread.start()

    def _loop(self) -> None:
        failures = 0
        announced = False
        with httpx.Client(timeout=httpx.Timeout(40, connect=10)) as client:
            while not self._stop.is_set():
                try:
                    if not announced:
                        self._handshake(client)
                        announced = True
                        self._connected = True
                        self._last_error = ""
                    payload = self._post(
                        client,
                        "/ilink/bot/getupdates",
                        {"get_updates_buf": self._cursor},
                        action="WeChat iLink polling failed",
                        timeout=httpx.Timeout(40, connect=10),
                    )
                except ILinkSessionExpired as exc:
                    # Hammering an expired session earns nothing; wait for the
                    # user to re-scan instead of burning the token's rate limit.
                    announced = False
                    self._last_error = f"{exc}. Scan the QR code again to reconnect."
                    self._connected = False
                    self._stop.wait(300)
                    continue
                except Exception as exc:
                    failures += 1
                    announced = False
                    self._connected = False
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stop.wait(30 if failures >= 3 else 2)
                    continue
                failures = 0
                self._last_error = ""
                self._connected = True
                cursor = str(payload.get("get_updates_buf") or "")
                if cursor:
                    self._cursor = cursor
                for message in payload.get("msgs") or []:
                    if isinstance(message, dict):
                        self._emit_message(client, message)
        self._connected = False

    def _emit_message(self, client: httpx.Client, message: dict[str, Any]) -> None:
        # ``message_type`` 2 is the bot's own traffic echoed back.
        if int(message.get("message_type") or 1) != 1:
            return
        sender = str(message.get("from_user_id") or "")
        chat = str(message.get("group_id") or "") or sender
        if not chat:
            return
        context_token = str(message.get("context_token") or "")
        if context_token:
            with self._context_lock:
                self._contexts[chat] = context_token
        text_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        media_deadline = time.monotonic() + 60
        media_attempts = media_bytes = omitted = failed = 0

        def receive_media(item: dict[str, Any], kind: str, name: str) -> None:
            nonlocal media_attempts, media_bytes, omitted, failed
            remaining = media_deadline - time.monotonic()
            if (
                media_attempts >= FORWARD_MEDIA_MAX_COUNT
                or media_bytes >= FORWARD_MEDIA_MAX_BYTES
                or remaining <= 0
            ):
                omitted += 1
                return
            media_attempts += 1
            received = self._download_media(
                client, item, kind, name,
                timeout=remaining, max_bytes=FORWARD_MEDIA_MAX_BYTES - media_bytes,
            )
            if str(received.get("name") or "").endswith(".error.txt"):
                failed += 1
            size = int(received.get("size") or 0)
            if media_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                omitted += 1
                return
            media_bytes += size
            attachments.append(received)

        for item in message.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            kind = int(item.get("type") or 0)
            if kind == 1:
                text_parts.append(str((item.get("text_item") or {}).get("text") or ""))
            elif kind == 3:
                voice = item.get("voice_item") or {}
                # The server already transcribes voice; keep the text even when
                # the audio itself cannot be fetched.
                transcript = str(voice.get("text") or "").strip()
                if transcript:
                    text_parts.append(transcript)
                receive_media(voice, "audio", "wechat-voice.silk")
            elif kind in {2, 4, 5}:
                field, kind_name, default_name = {
                    2: ("image_item", "image", "wechat-image.jpg"),
                    4: ("file_item", "file", "wechat-file"),
                    5: ("video_item", "video", "wechat-video.mp4"),
                }[kind]
                item_body = item.get(field) or {}
                receive_media(
                    item_body,
                    kind_name,
                    str(item_body.get("file_name") or default_name),
                )
        if omitted:
            text_parts.append(
                f"【另有 {omitted} 个媒体附件因数量、总大小或下载时限未留存，附件不完整。】"
            )
        if failed:
            text_parts.append(f"【{failed} 个媒体附件下载失败，未留存原文件。】")
        if len(attachments) > MAX_ATTACHMENTS:
            try:
                archive = pack_forwarded_media(
                    attachments, provider="wechat",
                    message_id=str(message.get("message_id") or message.get("client_id") or ""),
                )
            except ChannelMediaError as exc:
                attachments = [_attachment_error("wechat-media", str(exc))]
                text_parts.append("【媒体归档失败，本条消息的媒体附件未留存。】")
            else:
                attachments = [archive]
                text_parts.append(
                    f"【媒体文件已打包为 {archive['name']}，内含 manifest.json；"
                    "可在会话沙盒按需解压，读取图片或文件。】"
                )
        text = "\n".join(part for part in text_parts if part.strip())
        if not text and not attachments:
            return
        self._emit(ChannelEvent(
            channel_id=self.config.id,
            provider="wechat",
            external_user_id=sender,
            external_chat_id=chat,
            message_id=str(message.get("message_id") or message.get("client_id") or ""),
            text=text,
            attachments=tuple(attachments),
            timestamp=float(message.get("create_time_ms") or 0) / 1000 or time.time(),
            raw=message,
        ))

    def _download_media(
        self,
        client: httpx.Client,
        item: dict[str, Any],
        kind: str,
        name: str,
        *,
        timeout: float = 60,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        param = str(media.get("encrypt_query_param") or "")
        if not param:
            return _attachment_error(name, "attachment has no CDN reference")
        try:
            key = ilink_media_key(media, str(item.get("aeskey") or ""))
            limit = _attachment_limit(kind)
            if max_bytes is not None:
                limit = min(limit, max_bytes)
            deadline = time.monotonic() + timeout
            encrypted = bytearray()
            with client.stream(
                "GET", f"{self._cdn}/download",
                params={"encrypted_query_param": param},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    return _attachment_error(name, f"CDN download returned HTTP {response.status_code}")
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        return _attachment_error(name, "media download time limit exceeded")
                    # AES padding adds at most one 16-byte block.
                    if len(encrypted) + len(chunk) > limit + 16:
                        return _attachment_error(name, f"file exceeds the {limit} byte limit")
                    encrypted.extend(chunk)
            content = ilink_decrypt(bytes(encrypted), key)
            if not content:
                return _attachment_error(name, "provider returned empty media")
            if len(content) > limit:
                return _attachment_error(name, f"file exceeds the {limit} byte limit")
            return _attachment_from_bytes(
                content,
                mime="",
                name=name,
            )
        except ChannelMediaError as exc:
            return _attachment_error(name, str(exc))
        except Exception as exc:
            return _attachment_error(name, f"CDN download failed ({type(exc).__name__})")

    def _context_token(self, chat_id: str) -> str:
        with self._context_lock:
            return self._contexts.get(chat_id, "")

    def send(self, reply: ChannelReply) -> None:
        chat = str(reply.external_chat_id or "")
        context_token = self._context_token(chat)
        if not context_token:
            # WeChat has no "open a conversation" API: without a token from a
            # recent inbound message there is nowhere to deliver this.
            raise ChannelDeliveryError(
                "WeChat iLink send failed: no context token for this conversation. "
                "WeChat only accepts a reply after the user messages the bot again.",
                permanent=True,
            )
        prepared = _prepared_attachments(reply)
        rendered = render_channel_reply("wechat", reply)
        body = str(rendered.get("text") or "")
        chunks = ilink_split_text(body)
        if prepared and not reply.text and not reply.blocks:
            chunks = []
        with httpx.Client(timeout=20) as client:
            for chunk in chunks:
                self._send_items(
                    client,
                    chat,
                    context_token,
                    [{"type": 1, "text_item": {"text": chunk}}],
                )
            for attachment, data in prepared:
                if attachment.kind == "text":
                    for chunk in ilink_split_text(attachment.text):
                        self._send_items(
                            client,
                            chat,
                            context_token,
                            [{"type": 1, "text_item": {"text": chunk}}],
                        )
                    continue
                self._send_items(
                    client,
                    chat,
                    context_token,
                    [self._upload_media(client, chat, attachment, data)],
                )

    def _send_items(
        self,
        client: httpx.Client,
        chat_id: str,
        context_token: str,
        items: list[dict[str, Any]],
    ) -> None:
        self._post(
            client,
            "/ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    # A reused client_id makes WeChat drop the later messages
                    # as duplicate updates of the first one.
                    "client_id": f"argusai:{uuid.uuid4().hex}",
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": items,
                },
            },
            action="WeChat iLink send failed",
        )

    def _upload_media(
        self,
        client: httpx.Client,
        chat_id: str,
        attachment: ChannelAttachment,
        data: bytes,
    ) -> dict[str, Any]:
        kind = attachment.kind
        media_type = _ILINK_MEDIA_TYPES.get(kind, 3)
        item_type = _ILINK_ITEM_TYPES.get(kind, 4)
        key = os.urandom(16)
        key_hex = key.hex()
        filekey = os.urandom(16).hex()
        ciphertext = ilink_encrypt(data, key)
        upload = self._post(
            client,
            "/ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": chat_id,
                "rawsize": len(data),
                "rawfilemd5": hashlib.md5(data).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": key_hex,
            },
            action=f"WeChat iLink {kind} upload setup failed",
        )
        upload_param = str(upload.get("upload_param") or "")
        if not upload_param:
            raise delivery_error(
                f"WeChat iLink {kind} upload setup failed",
                "server returned no upload_param",
            )
        response = client.post(
            f"{self._cdn}/upload",
            params={"encrypted_query_param": upload_param, "filekey": filekey},
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        if response.status_code >= 400:
            raise delivery_error(
                f"WeChat iLink {kind} upload failed",
                str(response.headers.get("x-error-message") or response.text)[:300],
                response.status_code,
            )
        encrypt_param = str(response.headers.get("x-encrypted-param") or "")
        if not encrypt_param:
            raise delivery_error(
                f"WeChat iLink {kind} upload failed",
                "CDN returned no x-encrypted-param header",
            )
        media = {
            "encrypt_query_param": encrypt_param,
            # Tencent's own client base64s the hex text rather than the bytes.
            "aes_key": base64.b64encode(key_hex.encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        if item_type == 2:
            return {"type": 2, "image_item": {"media": media, "mid_size": len(ciphertext)}}
        if item_type == 5:
            return {
                "type": 5,
                "video_item": {"media": media, "video_size": len(ciphertext)},
            }
        if item_type == 3:
            return {"type": 3, "voice_item": {"media": media}}
        return {
            "type": 4,
            "file_item": {
                "media": media,
                "file_name": attachment.name or "attachment",
                "len": str(len(data)),
            },
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._connected = False


_QQ_COMPOSITE_MESSAGE_TYPES = frozenset({101, 102})
_QQ_FORWARD_MAX_NODES = 10_000
_QQ_FORWARD_MAX_DEPTH = 32


def _qq_composite_parts(
    raw: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[str], int]:
    """Extract bounded nested text/media from QQ composite/forward payloads."""
    lines: list[str] = []
    attachments: list[dict[str, Any]] = []
    warnings: list[str] = []
    pending: list[tuple[Any, int]] = [
        (item, 0) for item in reversed(list(raw.get("msg_elements") or []))
    ]
    visited = 0
    text_bytes = 0
    while pending and visited < _QQ_FORWARD_MAX_NODES:
        item, depth = pending.pop()
        if not isinstance(item, dict):
            continue
        if depth > _QQ_FORWARD_MAX_DEPTH:
            warning = "【转发嵌套超过读取深度，部分子记录未展开；当前记录不完整。】"
            if warning not in warnings:
                warnings.append(warning)
            continue
        visited += 1
        content = str(item.get("content") or "").strip()
        if content:
            encoded = content.encode("utf-8", errors="replace")
            remaining = max(0, TEXT_LIMIT - text_bytes)
            lines.append(encoded[:remaining].decode("utf-8", errors="ignore"))
            text_bytes += len(encoded) + 1
            if text_bytes > TEXT_LIMIT:
                warnings.append("【转发正文超过 4 MiB，后续内容未展开；请分批转发，当前记录不完整。】")
                break
        for attachment in list(item.get("attachments") or []):
            if isinstance(attachment, dict):
                attachments.append(dict(attachment))
        nested = item.get("msg_elements") or item.get("elements") or []
        if isinstance(nested, list):
            pending.extend((child, depth + 1) for child in reversed(nested))
    if pending and visited >= _QQ_FORWARD_MAX_NODES:
        warnings.append("【转发超过 10,000 个消息节点，后续节点未展开；当前记录不完整。】")
    return lines, attachments, warnings, visited


def _qq_inbound_text(
    parsed: Any, raw: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Preserve QQ merged-forward records instead of dropping msg_elements."""
    text = str(getattr(parsed, "content", "") or "").strip()
    try:
        message_type = int(getattr(parsed, "message_type", 0) or 0)
    except (TypeError, ValueError):
        message_type = 0
    if message_type not in _QQ_COMPOSITE_MESSAGE_TYPES:
        return text, [], []
    lines, attachments, warnings, count = _qq_composite_parts(raw)
    if not lines and not warnings:
        lines = [
            str(getattr(element, "content", "") or "").strip()
            for element in list(getattr(parsed, "msg_elements", None) or [])[:_QQ_FORWARD_MAX_NODES]
            if str(getattr(element, "content", "") or "").strip()
        ]
    heading = (
        "【QQ 合并转发的聊天记录；以下均为用户转发的内容】"
        if message_type == 102
        else "【QQ 组合消息；以下均为用户转发的内容】"
    )
    body_parts: list[str] = []
    if text and text not in lines:
        body_parts.append(text)
    body_parts.extend(lines)
    body = "\n".join(body_parts).strip()
    if not body:
        body = "【平台事件未携带可展开的转发正文】"
    record = f"{heading}\n{body}\n【QQ 转发内容结束】"
    if warnings:
        record += "\n" + "\n".join(warnings)
    event_text, records = prepare_forwarded_text(
        record,
        provider="qq",
        message_id=str(getattr(parsed, "message_id", "") or raw.get("id") or ""),
        force_archive=count > 64,
    )
    if records and warnings:
        event_text += "\n" + "\n".join(warnings)
    return event_text, attachments, records


class QQAdapter(ChannelAdapter):
    """Official QQ Bot C2C adapter backed by ``qqbot-agent-sdk``.

    The SDK owns a WebSocket thread; this adapter owns a small asyncio control
    loop for inbound parsing, attachment downloads, and outbound API calls.
    Group/guild events are rejected at the boundary so pairing cannot silently
    widen an Agent conversation to a room full of people.
    """

    def __init__(self, config: ChannelConfig, credentials: dict[str, str]) -> None:
        super().__init__(config, credentials)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api: Any = None
        self._ws: Any = None
        self._http_client: httpx.AsyncClient | None = None
        self._session_lock = threading.Lock()
        self._session_id: str | None = None
        self._session_seq: int | None = None
        # QQ's native C2C stream endpoint is not exposed by the current Python
        # SDK, so the adapter keeps the small amount of sequencing state the
        # official protocol requires (stable msg_seq, increasing index, and
        # the stream_msg_id returned by the first frame).
        self._stream_states: dict[str, dict[str, Any]] = {}
        self._stream_failures: set[str] = set()

    def _credentials(self) -> tuple[str, str]:
        return (
            str(self.credentials.get("app_id") or "").strip(),
            str(self.credentials.get("client_secret") or "").strip(),
        )

    def test(self) -> dict[str, Any]:
        app_id, secret = self._credentials()
        if not app_id or not secret:
            return {"ok": False, "error": "app_id and client_secret are required"}
        try:
            from qqbot_agent_sdk import QQApiClient

            client = QQApiClient(app_id, secret, log_tag=f"ArgusAI:{self.config.id}")
            client.ensure_token_sync()
            gateway = client.get_gateway_url_sync()
            return {
                "ok": True,
                "identity": {"app_id": app_id},
                "gateway": gateway,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start(self, on_event: EventCallback) -> None:
        app_id, secret = self._credentials()
        if not app_id or not secret:
            raise ValueError("QQ app_id and client_secret are required")
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_event = on_event
        self._stop.clear()
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
            name=f"argusai-qq-{self.config.id}",
        )
        self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._connected = False
            log.exception("QQ channel worker failed channel_id=%s", self.config.id)
        finally:
            self._loop = None
            self._api = None
            self._ws = None
            self._http_client = None

    async def _run(self) -> None:
        from qqbot_agent_sdk import QQApiClient, QQWebSocket, WSCallbacks

        app_id, secret = self._credentials()
        self._loop = asyncio.get_running_loop()
        timeout = httpx.Timeout(60, connect=15)
        async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
            api = QQApiClient(app_id, secret, log_tag=f"ArgusAI:{self.config.id}")
            api.setup(client)
            self._api = api
            self._http_client = client

            callbacks = WSCallbacks(
                on_message_event=self._on_message_event,
                on_interaction_event=self._on_interaction_event,
                on_connected=self._on_connected,
                on_disconnected=self._on_disconnected,
                on_fatal_error=self._on_fatal_error,
                get_token=api.ensure_token_sync,
                get_session=self._get_session,
                set_session=self._set_session,
                set_heartbeat_interval=lambda _seconds: None,
                clear_token=lambda: self._clear_token(api),
                fail_pending=lambda reason: self._set_error(reason),
                get_gateway_url=api.get_gateway_url_sync,
            )
            gateway_url = await api.get_gateway_url()
            ws = QQWebSocket(callbacks, log_tag=f"ArgusAI:{self.config.id}")
            self._ws = ws
            ws.start(gateway_url, self._loop)
            try:
                while not self._stop.is_set():
                    await asyncio.sleep(0.2)
            finally:
                await ws.async_stop()
                self._connected = False

    def _on_connected(self) -> None:
        self._connected = True
        self._last_error = ""

    def _on_disconnected(self) -> None:
        self._connected = False

    def _on_fatal_error(self, code: str, message: str) -> None:
        self._last_error = f"{code}: {message}".strip(": ")
        self._connected = False
        self._stop.set()

    def _set_error(self, reason: str) -> None:
        if reason:
            self._last_error = str(reason)[:900]

    @staticmethod
    def _clear_token(api: Any) -> None:
        # The official SDK exposes token invalidation only through this callback
        # contract; keep the mutation local to its documented cache fields.
        api._access_token = None
        api._token_expires_at = 0.0

    def _get_session(self) -> tuple[str | None, int | None]:
        with self._session_lock:
            return self._session_id, self._session_seq

    def _set_session(self, session_id: str | None, seq: int | None) -> None:
        with self._session_lock:
            self._session_id = str(session_id) if session_id else None
            self._session_seq = int(seq) if seq is not None else None

    async def _on_message_event(self, event_type: str, raw: dict[str, Any]) -> None:
        from qqbot_agent_sdk import EventParser

        parsed = EventParser.parse(event_type, raw)
        if parsed is None or str(parsed.chat_scope or "").lower() != "c2c":
            return
        text, nested_attachments, records = _qq_inbound_text(parsed, raw)
        attachments: list[dict[str, Any]] = list(records)
        seen: set[str] = set()
        candidates = list(parsed.attachments or [])
        for element in list(parsed.msg_elements or []):
            candidates.extend(list(getattr(element, "attachments", None) or []))
        candidates.extend(nested_attachments)
        client = self._http_client
        is_forward = str(getattr(parsed, "message_type", "")) in {"101", "102"}
        forwarded_media: list[dict[str, Any]] = []
        media_bytes = 0
        download_started = time.monotonic()
        if client is not None:
            omitted = 0
            failed = 0
            for item in candidates:
                if isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    if url.startswith("//"):
                        url = f"https:{url}"
                    mime = str(item.get("content_type") or item.get("mime") or "")
                    name = str(item.get("filename") or item.get("name") or "qq-attachment")
                else:
                    url = str(getattr(item, "resolved_url", "") or "").strip()
                    mime = str(getattr(item, "content_type", "") or "")
                    name = str(getattr(item, "filename", "") or "qq-attachment")
                dedup_key = url or f"missing-url:{mime}:{name}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                if (
                    len(seen) > FORWARD_MEDIA_MAX_COUNT
                    or time.monotonic() - download_started > 60
                    or (is_forward and media_bytes >= FORWARD_MEDIA_MAX_BYTES)
                    or (not is_forward and len(attachments) >= MAX_ATTACHMENTS)
                ):
                    omitted += 1
                    continue
                received = (
                    await _download_attachment_async(client, url, mime=mime, name=name)
                    if url else _attachment_error(name, "platform did not provide a download URL")
                )
                if str(received.get("name") or "").endswith(".error.txt"):
                    failed += 1
                if is_forward:
                    size = int(received.get("size") or 0)
                    if media_bytes + size > FORWARD_MEDIA_MAX_BYTES:
                        omitted += 1
                        continue
                    media_bytes += size
                    forwarded_media.append(received)
                else:
                    attachments.append(received)
            if omitted:
                text += f"\n【另有 {omitted} 个媒体附件因数量、总大小或下载时限未留存，附件不完整。】"
            if failed:
                text += f"\n【{failed} 个媒体附件下载失败，已保留错误说明。】"
        elif candidates:
            text += "\n【媒体下载连接不可用，图片或文件未留存。】"
        if forwarded_media:
            archive = pack_forwarded_media(
                forwarded_media, provider="qq", message_id=str(parsed.message_id or ""),
            )
            attachments.append(archive)
            text += (
                f"\n【转发媒体文件已打包为 {archive['name']}，内含 manifest.json；"
                "可在会话沙盒中按需解压读取图片或文件。】"
            )
        raw_payload = dict(raw or {})
        raw_payload["chat_scope"] = "c2c"
        raw_payload["chat_type"] = "c2c"
        author = raw_payload.get("author")
        display_name = str(parsed.user_name or "").strip()
        if not display_name and isinstance(author, dict):
            display_name = str(author.get("username") or "").strip()
        self._emit(ChannelEvent(
            channel_id=self.config.id,
            provider="qq",
            external_user_id=str(parsed.user_id or parsed.chat_id),
            external_chat_id=str(parsed.chat_id),
            external_thread_id="",
            message_id=str(parsed.message_id or ""),
            text=text,
            display_name=display_name,
            timestamp=time.time(),
            attachments=tuple(attachments),
            chat_kind="direct",
            raw=raw_payload,
        ))

    async def _on_interaction_event(
        self,
        _event_type: str,
        raw: dict[str, Any],
    ) -> None:
        """ACK an official QQ keyboard callback and forward ArgusAI actions."""
        from qqbot_agent_sdk import parse_interaction_event

        api = self._api
        if api is None:
            return
        try:
            interaction = parse_interaction_event(raw)
            interaction_id = str(getattr(interaction, "id", "") or "")
            if interaction_id:
                # QQ requires interaction ACKs promptly; acknowledge before the
                # action enters the potentially busy Agent conversation.
                await api.acknowledge_interaction(interaction_id)
        except Exception:
            log.warning("QQ interaction could not be parsed or acknowledged", exc_info=True)
            return

        resolved = getattr(getattr(interaction, "data", None), "resolved", None)
        button_data = str(getattr(resolved, "button_data", "") or "").strip()
        if not bool(getattr(interaction, "is_c2c", False)):
            return
        if not button_data.startswith("act:"):
            return
        token = button_data.removeprefix("act:").strip()
        if not token.startswith("ca_") or len(token) > 64:
            return
        chat_id = str(getattr(interaction, "chat_id", "") or "").strip()
        user_id = str(getattr(interaction, "user_openid", "") or chat_id).strip()
        if not chat_id or not user_id:
            return
        raw_payload = dict(raw or {})
        raw_payload["chat_scope"] = "c2c"
        # QQ interaction chat_type=2 means C2C, whereas several other
        # providers use 2 for groups. Normalize before the shared boundary.
        raw_payload["chat_type"] = "c2c"
        self._emit(ChannelEvent(
            channel_id=self.config.id,
            provider="qq",
            external_user_id=user_id,
            external_chat_id=chat_id,
            message_id=f"interaction:{interaction_id}",
            text="",
            timestamp=time.time(),
            chat_kind="direct",
            raw=raw_payload,
            event_kind="action",
            action_token=token,
        ))

    def send(self, reply: ChannelReply) -> None:
        loop = self._loop
        api = self._api
        if loop is None or api is None or loop.is_closed():
            raise RuntimeError("QQ channel is not connected")
        future = asyncio.run_coroutine_threadsafe(self._send_async(reply), loop)
        try:
            future.result(timeout=180)
        except Exception as exc:
            raise delivery_error("QQ send failed", f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _qq_plain_fallback_allowed(exc: Exception) -> bool:
        """Only downgrade capability errors; retry ambiguous network failures."""
        message = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in message for marker in (
            "[400]",
            "[403]",
            "keyboard",
            "markdown",
            "msg_type",
            "not support",
            "not allowed",
            "permission",
            "unsupported",
        ))

    @staticmethod
    def _qq_keyboard(payload: dict[str, Any]) -> Any:
        spec = payload.get("keyboard")
        if not isinstance(spec, dict):
            return None
        from qqbot_agent_sdk.dto import (
            InlineKeyboard,
            KeyboardButton,
            KeyboardButtonAction,
            KeyboardButtonPermission,
            KeyboardButtonRenderData,
            KeyboardContent,
            KeyboardRow,
        )

        rows: list[Any] = []
        for raw_row in list(spec.get("rows") or [])[:5]:
            if not isinstance(raw_row, list):
                continue
            buttons: list[Any] = []
            for raw_button in raw_row[:5]:
                if not isinstance(raw_button, dict):
                    continue
                data = str(raw_button.get("data") or "")[:128]
                label = str(raw_button.get("label") or "")[:30]
                if not data or not label:
                    continue
                buttons.append(KeyboardButton(
                    id=str(raw_button.get("id") or uuid.uuid4().hex[:12])[:64],
                    render_data=KeyboardButtonRenderData(
                        label=label,
                        visited_label=str(
                            raw_button.get("visited_label") or label
                        )[:30],
                        style=int(raw_button.get("style") or 0),
                    ),
                    action=KeyboardButtonAction(
                        type=1,
                        data=data,
                        permission=KeyboardButtonPermission(type=2),
                        click_limit=1,
                    ),
                    group_id=str(raw_button.get("group_id") or "argusai")[:64],
                ))
            if buttons:
                rows.append(KeyboardRow(buttons=buttons))
        return InlineKeyboard(content=KeyboardContent(rows=rows)) if rows else None

    async def _send_qq_markdown(
        self,
        *,
        api: Any,
        chat_id: str,
        reply: ChannelReply,
        rendered: dict[str, Any],
    ) -> None:
        from qqbot_agent_sdk import MessageToCreate, QQMessageType
        from qqbot_agent_sdk.dto import MarkdownContent

        chunks = [
            str(item).strip()
            for item in list(rendered.get("text_chunks") or [])
            if str(item).strip()
        ]
        keyboard = self._qq_keyboard(rendered)
        try:
            for index, chunk in enumerate(chunks):
                await api.post_c2c_message(
                    chat_id,
                    MessageToCreate(
                        msg_type=QQMessageType.MARKDOWN,
                        msg_id=(
                            str(reply.reply_to_message_id or "")
                            if index == 0
                            else ""
                        ),
                        msg_seq=api.next_msg_seq(chat_id),
                        markdown=MarkdownContent(content=chunk),
                    ),
                    keyboard=(keyboard if index == 0 else None),
                )
        except Exception as exc:
            if not self._qq_plain_fallback_allowed(exc):
                raise
            # Markdown and inline keyboards are reviewed QQ capabilities. A bot
            # without either permission still gets a complete actionable reply.
            fallback_chunks = [
                str(item).strip()
                for item in list(rendered.get("plain_fallback_chunks") or [])
                if str(item).strip()
            ]
            for index, chunk in enumerate(fallback_chunks):
                await api.send_text(
                    "c2c",
                    chat_id,
                    chunk,
                    reply_to=(
                        reply.reply_to_message_id if index == 0 else None
                    ),
                    markdown=False,
                    max_length=4000,
                )

    def stream_update(
        self,
        *,
        chat_id: str,
        source_message_id: str,
        text: str,
        final: bool = False,
    ) -> bool:
        """Send one full-text QQ C2C stream frame from any runtime thread."""
        loop = self._loop
        api = self._api
        if loop is None or api is None or loop.is_closed():
            return False
        target = str(chat_id or "").strip()
        source = str(source_message_id or "").strip()
        content = str(text or "")
        if not target or not source or not content.strip():
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._stream_update_async(
                chat_id=target,
                source_message_id=source,
                text=content,
                final=final,
            ),
            loop,
        )
        if final:
            future.result(timeout=60)
        else:
            def record_failure(done: Any) -> None:
                try:
                    done.result()
                except Exception as exc:
                    self._set_error(f"QQ native stream failed: {exc}")
                    log.warning(
                        "QQ native stream frame failed channel_id=%s",
                        self.config.id,
                        exc_info=True,
                    )

            future.add_done_callback(record_failure)
        return True

    async def _stream_update_async(
        self,
        *,
        chat_id: str,
        source_message_id: str,
        text: str,
        final: bool,
    ) -> None:
        api = self._api
        if api is None:
            raise RuntimeError("QQ channel is not connected")
        if source_message_id in self._stream_failures:
            raise RuntimeError("QQ native stream was already rejected for this message")
        state = self._stream_states.get(source_message_id)
        if state is None:
            state = {
                "chat_id": chat_id,
                "msg_seq": api.next_msg_seq(source_message_id),
                "index": 0,
                "stream_msg_id": "",
                "last_text": "",
                "lock": asyncio.Lock(),
            }
            self._stream_states[source_message_id] = state
        lock = state["lock"]
        async with lock:
            if text == state.get("last_text") and not final:
                return
            index = int(state.get("index") or 0)
            state["index"] = index + 1
            body: dict[str, Any] = {
                "input_mode": "replace",
                "input_state": 10 if final else 1,
                "content_type": "markdown",
                "content_raw": text,
                "event_id": source_message_id,
                "msg_id": source_message_id,
                "msg_seq": int(state["msg_seq"]),
                "index": index,
            }
            if state.get("stream_msg_id"):
                body["stream_msg_id"] = str(state["stream_msg_id"])
            try:
                response = await api.request(
                    "POST",
                    f"/v2/users/{chat_id}/stream_messages",
                    body,
                )
            except Exception:
                self._stream_states.pop(source_message_id, None)
                if len(self._stream_failures) >= 256:
                    self._stream_failures.clear()
                self._stream_failures.add(source_message_id)
                raise
            stream_id = str(
                (response or {}).get("id")
                or (response or {}).get("stream_msg_id")
                or ""
            )
            if stream_id and not state.get("stream_msg_id"):
                state["stream_msg_id"] = stream_id
            state["last_text"] = text
            if final:
                self._stream_states.pop(source_message_id, None)

    async def _send_async(self, reply: ChannelReply) -> None:
        from qqbot_agent_sdk import (
            MEDIA_TYPE_FILE,
            MEDIA_TYPE_IMAGE,
            MEDIA_TYPE_VIDEO,
            MEDIA_TYPE_VOICE,
            MediaInfo,
            MediaUploader,
            MessageToCreate,
            QQMessageType,
        )

        api = self._api
        client = self._http_client
        if api is None or client is None:
            raise RuntimeError("QQ channel is not connected")
        chat_id = str(reply.external_chat_id or "").strip()
        if not chat_id:
            raise ValueError("QQ reply requires a C2C chat id")
        rendered = render_channel_reply("qq", reply)
        await self._send_qq_markdown(
            api=api,
            chat_id=chat_id,
            reply=reply,
            rendered=rendered,
        )

        media_types = {
            "image": MEDIA_TYPE_IMAGE,
            "video": MEDIA_TYPE_VIDEO,
            "audio": MEDIA_TYPE_VOICE,
            "text": MEDIA_TYPE_FILE,
            "file": MEDIA_TYPE_FILE,
        }
        uploader = MediaUploader(api, client, log_tag=f"ArgusAI:{self.config.id}")
        for attachment, data in _prepared_attachments(reply):
            suffix = os.path.splitext(attachment.name or "")[1][:20]
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="argusai-qq-",
                    suffix=suffix,
                    delete=False,
                ) as handle:
                    handle.write(data)
                    tmp_path = handle.name
                file_info = await uploader.upload(
                    "c2c",
                    chat_id,
                    tmp_path,
                    media_types.get(attachment.kind, MEDIA_TYPE_FILE),
                    file_name=attachment.name or None,
                )
                await api.post_c2c_message(chat_id, MessageToCreate(
                    msg_type=QQMessageType.RICH_MEDIA,
                    msg_id=str(reply.reply_to_message_id or ""),
                    msg_seq=api.next_msg_seq(chat_id),
                    media=MediaInfo(file_info=file_info),
                ))
            finally:
                if tmp_path:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)

    def stop(self) -> None:
        self._stop.set()
        self._stream_states.clear()
        self._stream_failures.clear()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._connected = False


def create_channel_adapter(
    config: ChannelConfig,
    credentials: dict[str, str],
) -> ChannelAdapter:
    if config.provider == "qq":
        return QQAdapter(config, credentials)
    if config.provider == "telegram":
        return TelegramAdapter(config, credentials)
    if config.provider == "discord":
        return DiscordAdapter(config, credentials)
    if config.provider == "slack":
        return SlackAdapter(config, credentials)
    if config.provider == "wechat":
        return WeChatILinkAdapter(config, credentials)
    if config.provider in {"feishu", "dingtalk"}:
        return StreamSDKAdapter(config, credentials)
    return WebhookPlatformAdapter(config, credentials)
