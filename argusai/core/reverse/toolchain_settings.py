"""Shared toolchain path keys for settings UI and PATH scan autofill."""

from __future__ import annotations

# Keep in sync with SettingsPanel toolchain path fields (webui).
SETTINGS_TOOLCHAIN_KEYS: tuple[str, ...] = (
    "jadx",
    "apktool",
    "frida",
    "adb",
    "zipalign",
    "apksigner",
    "jarsigner",
    "capa",
    "yara",
    "objection",
    "class-dump",
    "ghidra",
    "ida",
    "innoextract",
    # Used by assistant video attachments (keyframe fallback), not by reverse
    # jobs; it lives here because this is the one place users set binary paths.
    # ffprobe is derived from it, so it gets no field of its own.
    "ffmpeg",
)

# When probing PATH for settings autofill, try these detect_tool names in order.
SETTINGS_PROBE_ALIASES: dict[str, tuple[str, ...]] = {}
