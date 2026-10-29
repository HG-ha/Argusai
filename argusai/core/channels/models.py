"""Typed communication-channel configuration and normalized events."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterator, Mapping
from typing import Any, Literal

ChannelProvider = Literal[
    "telegram",
    "discord",
    "slack",
    "feishu",
    "dingtalk",
    "wecom",
    "wechat",
    "webhook",
]

CHANNEL_PROVIDERS = frozenset({
    "telegram",
    "discord",
    "slack",
    "feishu",
    "dingtalk",
    "wecom",
    "wechat",
    "webhook",
})

ChatKind = Literal["direct", "group"]

_GROUP_CHAT_HINTS = frozenset({
    "channel",
    "channels",
    "chatroom",
    "group",
    "groups",
    "mpim",
    "private_channel",
    "public_channel",
    "supergroup",
    "topic",
})
_DIRECT_CHAT_HINTS = frozenset({
    "bot_p2p",
    "direct",
    "dm",
    "im",
    "p2p",
    "private",
    "single",
})


def infer_chat_kind(
    provider: str,
    *,
    chat_id: str = "",
    raw: Mapping[str, Any] | None = None,
    chat_kind: str = "",
) -> ChatKind:
    """Classify a channel event as a 1:1 chat or a group/room.

    ArgusAI only accepts direct messages. ``chat_kind="group"`` is an explicit
    override; the dataclass default ``"direct"`` is not, so provider payloads
    can still mark a group when the field was left unset.
    """
    payload = raw if isinstance(raw, Mapping) else {}
    explicit = str(chat_kind or "").strip().lower()
    if explicit in _GROUP_CHAT_HINTS:
        return "group"
    if payload.get("is_group") is True:
        return "group"

    def classify(value: Any) -> ChatKind | None:
        token = str(value or "").strip().lower()
        if token in _GROUP_CHAT_HINTS or token == "2":
            return "group"
        if token in _DIRECT_CHAT_HINTS or token == "1":
            return "direct"
        return None

    named = classify(payload.get("chat_kind") or payload.get("chat_type"))
    if named is not None:
        return named

    kind = str(provider or "").strip().lower()
    chat = str(chat_id or "").strip()

    if kind == "telegram":
        room = payload.get("chat") if isinstance(payload.get("chat"), Mapping) else {}
        if not room:
            message = payload.get("message") if isinstance(payload.get("message"), Mapping) else {}
            room = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
        return classify(room.get("type") if isinstance(room, Mapping) else "") or "direct"

    if kind == "discord":
        return "group" if payload.get("guild_id") else "direct"

    if kind == "slack":
        typed = classify(payload.get("channel_type"))
        if typed is not None:
            return typed
        channel = chat
        raw_channel = payload.get("channel")
        if not channel and isinstance(raw_channel, str):
            channel = raw_channel
        elif not channel and isinstance(raw_channel, Mapping):
            channel = str(raw_channel.get("id") or "")
        if channel.startswith("D"):
            return "direct"
        if channel.startswith(("C", "G")):
            return "group"
        return "direct"

    if kind == "feishu":
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else {}
        return classify(
            payload.get("chat_type")
            or (message.get("chat_type") if isinstance(message, Mapping) else "")
        ) or "direct"

    if kind == "dingtalk":
        typed = classify(
            payload.get("conversationType") or payload.get("conversation_type")
        )
        if typed is not None:
            return typed
        conversation = chat or str(payload.get("conversationId") or "")
        return "group" if conversation.startswith("cid") else "direct"

    if kind == "wecom":
        if payload.get("ChatId") or payload.get("chatid"):
            return "group"
        return "direct"

    if kind == "wechat":
        return "group" if payload.get("group_id") else "direct"

    return classify(explicit) or "direct"

ChannelAttachmentKind = Literal["text", "image", "audio", "video", "file"]


class ChannelError(ValueError):
    """Store/gateway failure whose message is shown to a remote channel user.

    ``code`` keys into the channel message catalog so the gateway can render the
    failure in the host's UI language; ``str(exc)`` stays English for logs and
    for callers that predate the catalog.
    """

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


class ChannelDeliveryError(RuntimeError):
    """Transport failure that states whether retrying could ever succeed.

    Missing OAuth scopes, revoked tokens and deleted chats fail identically on
    every attempt, so the outbox marks them failed at once instead of burning
    the retry budget and re-posting the same message three times.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class ChannelAttachment(Mapping[str, Any]):
    """Provider-neutral attachment contract.

    ``ChannelEvent.attachments`` intentionally remains a tuple of dictionaries
    because existing assistant and gateway callers mutate/copy those values.
    Outbound callers may use this typed contract or the same legacy dictionary
    shape; adapters normalize both before touching a provider API.
    """

    kind: ChannelAttachmentKind = "file"
    name: str = ""
    mime: str = "application/octet-stream"
    size: int = 0
    sha256: str = ""
    data_base64: str = ""
    text: str = ""
    path: str = ""
    caption: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> ChannelAttachmentKind:
        """Legacy alias used by assistant attachment dictionaries."""

        return self.kind

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChannelAttachment":
        raw_kind = str(value.get("kind") or value.get("type") or "file").lower()
        kind: ChannelAttachmentKind = (
            raw_kind
            if raw_kind in {"text", "image", "audio", "video", "file"}
            else "file"
        )  # type: ignore[assignment]
        data = value.get("data_base64")
        if data is None:
            data = value.get("data")
        return cls(
            kind=kind,
            name=str(value.get("name") or value.get("filename") or ""),
            mime=str(value.get("mime") or value.get("content_type") or ""),
            size=max(0, int(value.get("size") or 0)),
            sha256=str(value.get("sha256") or "").lower(),
            data_base64=data if isinstance(data, str) else "",
            text=str(value.get("text") or ""),
            path=str(
                value.get("path")
                or value.get("sandbox_path")
                or value.get("file_path")
                or ""
            ),
            caption=str(value.get("caption") or ""),
            metadata=(
                dict(value.get("metadata") or {})
                if isinstance(value.get("metadata"), Mapping)
                else {}
            ),
        )

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "type": self.kind,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.caption:
            result["caption"] = self.caption
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        if include_content:
            if self.text:
                result["text"] = self.text
            elif self.data_base64:
                result["data_base64"] = self.data_base64
            elif self.path:
                result["path"] = self.path
        return result

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    provider: ChannelProvider
    name: str
    enabled: bool = False
    transport: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    secret_names: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "enabled": self.enabled,
            "transport": self.transport,
            "settings": dict(self.settings),
            "secret_names": list(self.secret_names),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result["configured_secrets"] = {
            name: True for name in self.secret_names
        }
        result.pop("secret_names", None)
        return result


@dataclass(frozen=True)
class ChannelIdentity:
    id: str
    channel_id: str
    provider: ChannelProvider
    external_user_id: str
    external_chat_id: str = ""
    external_thread_id: str = ""
    display_name: str = ""
    bound_session_id: str = ""
    allowed_session_ids: tuple[str, ...] = ()
    permissions: tuple[str, ...] = (
        "cancel",
        "chat",
        "configure",
        "confirm",
        "send",
        "status",
    )
    locale: str = ""
    paired_at: float = 0.0
    last_seen_at: float = 0.0

    def accessible_session_ids(self) -> tuple[str, ...]:
        """Sessions this pairing may list, switch to, or read from a channel."""
        seen: list[str] = []
        for item in (*self.allowed_session_ids, self.bound_session_id):
            session_id = str(item or "").strip()
            if session_id and session_id not in seen:
                seen.append(session_id)
        return tuple(seen)

    def can_access_session(self, session_id: str) -> bool:
        return str(session_id or "").strip() in self.accessible_session_ids()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "provider": self.provider,
            "external_user_id": self.external_user_id,
            "external_chat_id": self.external_chat_id,
            "external_thread_id": self.external_thread_id,
            "display_name": self.display_name,
            "bound_session_id": self.bound_session_id,
            "allowed_session_ids": list(self.accessible_session_ids()),
            "permissions": list(self.permissions),
            "locale": self.locale,
            "paired_at": self.paired_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(frozen=True)
class ChannelEvent:
    channel_id: str
    provider: ChannelProvider
    external_user_id: str
    external_chat_id: str
    message_id: str
    text: str
    external_thread_id: str = ""
    display_name: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    timestamp: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    event_kind: Literal["message", "action"] = "message"
    action_token: str = ""
    action_value: str = ""
    chat_kind: ChatKind = "direct"


@dataclass(frozen=True)
class ChannelReply:
    text: str
    external_chat_id: str
    external_thread_id: str = ""
    reply_to_message_id: str = ""
    blocks: tuple[dict[str, Any], ...] = ()
    attachments: tuple[ChannelAttachment | dict[str, Any], ...] = ()
    locale: str = "en"
