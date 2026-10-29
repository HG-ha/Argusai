"""Communication channel contracts and security gateway."""

from __future__ import annotations

from typing import Any

from .i18n import BILINGUAL, SUPPORTED_LOCALES, channel_text, normalize_locale
from .models import (
    CHANNEL_PROVIDERS,
    ChannelAttachment,
    ChannelAttachmentKind,
    ChannelConfig,
    ChannelError,
    ChannelEvent,
    ChannelIdentity,
    ChannelProvider,
    ChannelReply,
    infer_chat_kind,
)

__all__ = [
    "BILINGUAL",
    "CHANNEL_PROVIDERS",
    "SUPPORTED_LOCALES",
    "ChannelAttachment",
    "ChannelAttachmentKind",
    "ChannelConfig",
    "ChannelAdapter",
    "ChannelError",
    "ChannelEvent",
    "ChannelGateway",
    "ChannelIdentity",
    "ChannelProvider",
    "ChannelReply",
    "ChannelRuntimeManager",
    "ChannelSecretStore",
    "ChannelStore",
    "channel_text",
    "create_channel_adapter",
    "infer_chat_kind",
    "normalize_locale",
]


def __getattr__(name: str) -> Any:
    """Lazy-load heavy channel runtime pieces (avoid import cycles / optional deps)."""
    if name == "ChannelGateway":
        from .gateway import ChannelGateway

        return ChannelGateway
    if name == "ChannelAdapter":
        from .adapters import ChannelAdapter

        return ChannelAdapter
    if name == "create_channel_adapter":
        from .adapters import create_channel_adapter

        return create_channel_adapter
    if name == "ChannelSecretStore":
        from .secret_store import ChannelSecretStore

        return ChannelSecretStore
    if name == "ChannelRuntimeManager":
        from .runtime import ChannelRuntimeManager

        return ChannelRuntimeManager
    if name == "ChannelStore":
        from .store import ChannelStore

        return ChannelStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
