"""Asset reference helpers shared by assessment planning and evidence graph."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


ASSET_TYPES = {
    "url", "api", "js", "host", "cidr", "service", "repository",
    "apk", "aab", "ipa", "exe", "dll", "elf", "firmware",
    "container", "image", "k8s", "cloud", "ad", "identity",
    "pcap", "process", "dump", "file", "cluster", "namespace",
    "cloud_account", "iac_repo", "container_image", "ad_domain", "dc",
    "sbom", "wifi_interface", "wifi_pcap", "rootfs", "board",
    "uart_capture", "flash_dump", "other",
}

_ASSET_REF_RE = re.compile(r"^(?P<type>[a-zA-Z][a-zA-Z0-9_-]{0,31}):(?P<value>.+)$")


@dataclass
class AssetRef:
    """Stable reference to an auditable asset."""

    type: str
    value: str
    label: str = ""
    scope: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = data["type"] if data["type"] in ASSET_TYPES else "other"
        return data

    def to_ref(self) -> str:
        kind = self.type if self.type in ASSET_TYPES else "other"
        return f"{kind}:{self.value}"


def normalize_asset_ref(value: Any, *, default_type: str = "other") -> AssetRef:
    """Normalize a user/model supplied asset reference.

    Accepted input forms:
    - ``"url:https://example.test"``
    - ``{"type": "apk", "value": "app.apk", "label": "Android app"}``
    - bare strings, which are wrapped in ``default_type``.
    """
    if isinstance(value, dict):
        kind = str(value.get("type") or default_type or "other").strip().lower()
        raw_value = str(value.get("value") or value.get("path") or value.get("url") or "").strip()
        label = str(value.get("label") or value.get("name") or "").strip()
        scope_raw = value.get("scope")
        scope = [str(x).strip() for x in scope_raw] if isinstance(scope_raw, list) else []
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        return AssetRef(type=kind if kind in ASSET_TYPES else "other", value=raw_value, label=label, scope=scope, metadata=metadata)

    text = str(value or "").strip()
    match = _ASSET_REF_RE.match(text)
    if match:
        kind = match.group("type").lower()
        raw_value = match.group("value").strip()
        return AssetRef(type=kind if kind in ASSET_TYPES else "other", value=raw_value)
    kind = str(default_type or "other").strip().lower()
    return AssetRef(type=kind if kind in ASSET_TYPES else "other", value=text)

