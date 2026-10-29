"""Isolated official-SDK runners for persistent Feishu and DingTalk streams."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any, Callable

_EVENT_PREFIX = "ARGUSAI_CHANNEL_EVENT "
_STATUS_PREFIX = "ARGUSAI_CHANNEL_STATUS "


def _write(prefix: str, payload: dict[str, Any]) -> None:
    raw = (
        prefix
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n"
    ).encode("utf-8")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _event(payload: dict[str, Any]) -> None:
    _write(_EVENT_PREFIX, payload)


def _status(*, connected: bool, error: str = "") -> None:
    _write(_STATUS_PREFIX, {"connected": connected, "error": error})


def _dict_to_ns(value: Any) -> Any:
    """Turn nested JSON dicts into attribute-accessible objects (SDK-shaped)."""
    if isinstance(value, dict):
        return types.SimpleNamespace(
            **{str(key): _dict_to_ns(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_dict_to_ns(item) for item in value]
    return value


def _field_map(value: Any) -> dict[str, Any]:
    """Read a dict or SimpleNamespace as a plain mapping."""
    if isinstance(value, dict):
        return value
    if isinstance(value, types.SimpleNamespace):
        return dict(vars(value))
    return {}


def _field_get(value: Any, *names: str) -> Any:
    mapping = _field_map(value)
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
        attr = getattr(value, name, None)
        if attr not in (None, ""):
            return attr
    return ""


class _FeishuStreamHandler:
    """Minimal stand-in for ``EventDispatcherHandler`` used by ``lark_oapi.ws.Client``.

    The real dispatcher imports the entire generated ``lark_oapi.api`` surface
    (~40 MB). Sidecar only needs ``do_without_validation`` for two event types.
    """

    def __init__(self) -> None:
        self._processors: dict[str, Callable[[Any], Any]] = {}

    def register(self, event_key: str, callback: Callable[[Any], Any]) -> None:
        self._processors[event_key] = callback

    def do_without_validation(self, payload: bytes) -> Any:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            return None
        schema = str(raw.get("schema") or "")
        if schema:
            header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
            event_type = str(header.get("event_type") or "")
            event_key = f"p2.{event_type}"
        elif raw.get("uuid"):
            event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
            event_key = f"p1.{event.get('type') or ''}"
        else:
            return None
        callback = self._processors.get(event_key)
        if callback is None:
            return None
        return callback(_dict_to_ns(raw))


def _patch_feishu_ws_card_frames(Client: Any) -> None:
    """Route websocket CARD frames through the event handler.

    Upstream ``lark_oapi.ws.Client._handle_data_frame`` returns early on
    ``MessageType.CARD``, so ``card.action.trigger`` never reaches the
    registered callback (larksuite/oapi-sdk-python#126). CARD payloads use the
    same ``do_without_validation`` contract as EVENT, so rewrite the frame
    type and let the stock handler finish the ACK.
    """
    if getattr(Client, "_argusai_card_frames_patched", False):
        return
    from lark_oapi.ws.client import _get_by_key
    from lark_oapi.ws.const import HEADER_TYPE
    from lark_oapi.ws.enum import MessageType

    original = Client._handle_data_frame

    async def _handle_data_frame(self: Any, frame: Any) -> None:
        try:
            headers = frame.headers
            if MessageType(_get_by_key(headers, HEADER_TYPE)) == MessageType.CARD:
                for header in headers:
                    if header.key == HEADER_TYPE:
                        header.value = MessageType.EVENT.value
                        break
        except Exception:
            pass
        await original(self, frame)

    Client._handle_data_frame = _handle_data_frame
    Client._argusai_card_frames_patched = True


def _emit_feishu_card_action(data: Any) -> dict[str, Any]:
    """Normalize a card.action.trigger payload and emit it to the parent."""
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    value = _field_map(getattr(action, "value", None))
    token = str(value.get("argusai_action") or value.get("action_token") or "")
    operator = getattr(event, "operator", None)
    operator_id = getattr(operator, "operator_id", None)
    context = getattr(event, "context", None)
    user_id = str(
        _field_get(operator_id, "open_id", "user_id", "union_id")
        or _field_get(operator, "open_id", "user_id", "union_id")
        or ""
    )
    chat_id = str(
        _field_get(context, "open_chat_id", "chat_id")
        or _field_get(event, "open_chat_id", "chat_id")
        or ""
    )
    message_id = str(
        _field_get(context, "open_message_id", "message_id")
        or ""
    )
    if token and user_id and chat_id:
        _event({
            "provider": "feishu",
            "external_user_id": user_id,
            "external_chat_id": chat_id,
            "message_id": message_id or f"card-{token[-12:]}",
            "text": "",
            "timestamp": 0,
            "event_kind": "action",
            "action_token": token,
            "action_value": str(getattr(action, "option", "") or ""),
            "raw": {"value": value},
        })
    # Feishu waits for a callback body on CARD frames; an empty toast closes it.
    return {"toast": {"type": "info", "content": "ok"}}


def _load_feishu_ws() -> tuple[Any, Any]:
    """Import Feishu WS client without executing ``lark_oapi.__init__``.

    The package ``__init__`` does ``from .api import *`` and pulls ~40 MB of
    generated stubs. We only need ``lark_oapi.ws.client`` + ``LogLevel``.
    """
    if "lark_oapi.ws.client" in sys.modules:
        Client = sys.modules["lark_oapi.ws.client"].Client
        LogLevel = importlib.import_module("lark_oapi.core.enum").LogLevel
        _patch_feishu_ws_card_frames(Client)
        return Client, LogLevel

    spec = importlib.util.find_spec("lark_oapi")
    if spec is None:
        raise ImportError("lark_oapi is not installed")
    # A frozen build compiles the package in, so there is no directory to point
    # the stubs at. Its loader resolves submodules by dotted name and ignores
    # ``__path__``, which only has to exist for the import machinery to descend.
    locations = list(spec.submodule_search_locations or ())
    root = Path(locations[0]) if locations else None

    def ensure_pkg(name: str, relative: str) -> None:
        existing = sys.modules.get(name)
        if existing is not None:
            return
        mod = types.ModuleType(name)
        path = root / relative if root is not None else None
        mod.__path__ = [str(path)] if path is not None else []
        if path is not None:
            mod.__file__ = str(path / "__init__.py")
        sys.modules[name] = mod

    # Skip lark_oapi/__init__.py (eager api import) and the generated dispatcher.
    ensure_pkg("lark_oapi", "")
    ensure_pkg("lark_oapi.event", "event")
    dispatcher = types.ModuleType("lark_oapi.event.dispatcher_handler")

    class EventDispatcherHandler:  # noqa: N801 - match SDK name for type annotations
        pass

    dispatcher.EventDispatcherHandler = EventDispatcherHandler
    sys.modules["lark_oapi.event.dispatcher_handler"] = dispatcher

    Client = importlib.import_module("lark_oapi.ws.client").Client
    LogLevel = importlib.import_module("lark_oapi.core.enum").LogLevel
    _patch_feishu_ws_card_frames(Client)
    return Client, LogLevel


def _feishu_post_root(content: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the locale object Feishu nests inside a ``post`` payload."""
    for locale_key in ("zh_cn", "en_us", "ja_jp"):
        wrapped = content.get(locale_key)
        if isinstance(wrapped, dict) and (wrapped.get("content") or wrapped.get("title")):
            return wrapped
    return content


def _feishu_walk_post_nodes(content: dict[str, Any]):
    body = _feishu_post_root(content).get("content")
    if not isinstance(body, list):
        return
    for paragraph in body:
        if not isinstance(paragraph, list):
            continue
        for node in paragraph:
            if isinstance(node, dict):
                yield node


def _feishu_post_plain(content: dict[str, Any]) -> str:
    """Flatten a Feishu ``post`` (rich-text) payload into readable text."""
    post = _feishu_post_root(content)
    lines: list[str] = []
    title = str(post.get("title") or "").strip()
    if title:
        lines.append(title)
    body = post.get("content")
    if not isinstance(body, list):
        return "\n".join(lines).strip()
    for paragraph in body:
        if not isinstance(paragraph, list):
            continue
        parts: list[str] = []
        for node in paragraph:
            if not isinstance(node, dict):
                continue
            tag = str(node.get("tag") or "")
            if tag in {"text", "a"}:
                parts.append(str(node.get("text") or ""))
            elif tag == "at":
                name = str(node.get("user_name") or node.get("text") or "").strip()
                parts.append(f"@{name}" if name else "@user")
            elif tag == "emotion":
                parts.append(str(node.get("emoji_type") or ""))
            elif tag == "img":
                parts.append("[image]")
            elif tag in {"media", "emotion_img"}:
                parts.append("[media]")
            elif tag == "code_block":
                parts.append(str(node.get("text") or ""))
            elif tag == "hr":
                parts.append("---")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def feishu_inbound_text(message_type: str, content: Any) -> str:
    """Best-effort plain text for every Feishu IM message type we accept."""
    if not isinstance(content, dict):
        return ""
    kind = str(message_type or "")
    if kind == "text":
        return str(content.get("text") or "")
    if kind == "post":
        return _feishu_post_plain(content)
    if kind == "sticker":
        return str(content.get("emoji_type") or "")
    if kind == "share_chat":
        return str(content.get("chat_name") or content.get("name") or "[shared chat]")
    if kind == "share_user":
        return str(content.get("user_name") or "[shared contact]")
    if kind == "merge_forward":
        return str(content.get("title") or "[forwarded messages]")
    if kind == "location":
        name = str(content.get("name") or content.get("address") or "").strip()
        return name or "[location]"
    return str(content.get("text") or "")


def feishu_media_refs(message_type: str, content: Any) -> list[dict[str, str]]:
    """Downloadable Feishu resources (image / file / sticker / post embeds)."""
    if not isinstance(content, dict):
        return []
    kind = str(message_type or "")
    if kind in {"image", "sticker"}:
        key = str(content.get("image_key") or content.get("file_key") or "")
        if not key:
            return []
        name, mime = (
            ("feishu-sticker.png", "image/png")
            if kind == "sticker"
            else ("feishu-image.jpg", "image/jpeg")
        )
        return [{
            "file_key": key,
            "resource_type": "image",
            "name": str(content.get("file_name") or name),
            "mime": mime,
        }]
    if kind in {"file", "audio", "media"}:
        key = str(content.get("file_key") or "")
        if not key:
            return []
        name, mime = {
            "audio": ("feishu-audio.ogg", "audio/ogg"),
            "media": ("feishu-video.mp4", "video/mp4"),
            "file": ("feishu-file", "application/octet-stream"),
        }[kind]
        return [{
            "file_key": key,
            "resource_type": "file",
            "name": str(content.get("file_name") or name),
            "mime": mime,
        }]
    if kind != "post":
        return []
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in _feishu_walk_post_nodes(content):
        tag = str(node.get("tag") or "")
        if tag == "img":
            key = str(node.get("image_key") or "")
            resource_type, name, mime = "image", "feishu-image.jpg", "image/jpeg"
        elif tag == "media":
            key = str(node.get("file_key") or "")
            resource_type, name, mime = "file", "feishu-video.mp4", "video/mp4"
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append({
            "file_key": key,
            "resource_type": resource_type,
            "name": str(node.get("file_name") or name),
            "mime": mime,
        })
    return refs


_FEISHU_FILE_TYPES = frozenset({"image", "file", "audio", "media", "sticker"})


def _feishu_message_event_payload(data: Any) -> dict[str, Any] | None:
    """Normalize one ``im.message.receive_v1`` frame, or ``None`` to drop it."""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    if message is None or sender_id is None:
        return None
    # Feishu can echo the bot's own outbound traffic. Replying to those
    # creates ghost "unknown command" / chat loops that look unsolicited.
    sender_type = str(getattr(sender, "sender_type", "") or "").lower()
    if sender_type in {"app", "bot"}:
        return None
    content_raw = str(getattr(message, "content", "") or "")
    try:
        content = json.loads(content_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    message_type = str(getattr(message, "message_type", "") or "")
    text = feishu_inbound_text(message_type, content)
    if not text and message_type not in _FEISHU_FILE_TYPES:
        return None
    return {
        "provider": "feishu",
        "external_user_id": str(
            getattr(sender_id, "open_id", "")
            or getattr(sender_id, "user_id", "")
            or getattr(sender_id, "union_id", "")
            or ""
        ),
        "external_chat_id": str(getattr(message, "chat_id", "") or ""),
        "external_thread_id": str(getattr(message, "thread_id", "") or ""),
        "message_id": str(getattr(message, "message_id", "") or ""),
        "text": text,
        "timestamp": float(getattr(message, "create_time", 0) or 0) / 1000,
        "raw": {
            "message_type": message_type,
            "chat_type": str(getattr(message, "chat_type", "") or ""),
            "thread_id": str(getattr(message, "thread_id", "") or ""),
            "sender_type": sender_type,
            "content": content if isinstance(content, dict) else {},
        },
    }


def _run_feishu(credentials: dict[str, str]) -> None:
    Client, LogLevel = _load_feishu_ws()

    app_id = str(credentials.get("app_id") or "")
    app_secret = str(credentials.get("app_secret") or "")
    if not app_id or not app_secret:
        raise ValueError("app_id and app_secret are required")

    def receive(data: Any) -> None:
        payload = _feishu_message_event_payload(data)
        if payload is not None:
            _event(payload)

    handler = _FeishuStreamHandler()
    handler.register("p2.im.message.receive_v1", receive)
    handler.register("p2.card.action.trigger", _emit_feishu_card_action)
    client = Client(
        app_id,
        app_secret,
        log_level=LogLevel.WARNING,
        event_handler=handler,
    )
    _status(connected=True)
    client.start()


def _run_dingtalk(credentials: dict[str, str]) -> None:
    import dingtalk_stream

    client_id = str(credentials.get("client_id") or "")
    client_secret = str(credentials.get("client_secret") or "")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")

    class Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback: Any) -> tuple[str, str]:
            message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            raw = message.to_dict()
            text = "\n".join(
                str(item or "").strip()
                for item in (message.get_text_list() or [])
                if str(item or "").strip()
            )
            download_codes = list(message.get_image_list() or [])
            for getter_name in (
                "get_file_list",
                "get_audio_list",
                "get_video_list",
            ):
                getter = getattr(message, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    media_items = list(getter() or [])
                except Exception:
                    media_items = []
                for media_item in media_items:
                    if isinstance(media_item, dict):
                        code = (
                            media_item.get("downloadCode")
                            or media_item.get("download_code")
                            or media_item.get("mediaId")
                        )
                    else:
                        code = media_item
                    if code:
                        download_codes.append(str(code))
            content = raw.get("content")
            if isinstance(content, dict) and content.get("downloadCode"):
                download_codes.append(str(content["downloadCode"]))
            if not text and isinstance(content, dict):
                text = str(
                    content.get("content")
                    or content.get("text")
                    or ""
                ).strip()
            if text or download_codes:
                raw["download_codes"] = list(dict.fromkeys(download_codes))
                _event({
                    "provider": "dingtalk",
                    "external_user_id": str(
                        getattr(message, "sender_staff_id", "")
                        or getattr(message, "sender_id", "")
                        or ""
                    ),
                    "external_chat_id": str(
                        getattr(message, "conversation_id", "")
                        or getattr(message, "sender_id", "")
                        or ""
                    ),
                    "message_id": str(getattr(message, "message_id", "") or ""),
                    "text": text,
                    "display_name": str(getattr(message, "sender_nick", "") or ""),
                    "timestamp": float(getattr(message, "create_at", 0) or 0) / 1000,
                    "raw": raw,
                })
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(client_id, client_secret)
    )
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
    _status(connected=True)
    client.start_forever()


def main() -> None:
    try:
        raw = sys.stdin.buffer.readline()
        config = json.loads(raw.decode("utf-8")) if raw else {}
        provider = str(config.get("provider") or "")
        credentials = {
            str(key): str(value)
            for key, value in dict(config.get("credentials") or {}).items()
        }
        if provider == "feishu":
            _run_feishu(credentials)
        elif provider == "dingtalk":
            _run_dingtalk(credentials)
        else:
            raise ValueError(f"unsupported stream provider: {provider}")
    except KeyboardInterrupt:
        return
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _status(connected=False, error=message)
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:
            pass
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
