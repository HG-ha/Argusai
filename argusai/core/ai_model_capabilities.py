"""Per-model multimodal input capabilities for chat endpoints.

An endpoint is a connection (URL, protocol, key) that can host many models.
Vision / video / audio belong to the **active model**, not the row. OpenRouter
publishes ``architecture.input_modalities``, LiteLLM uses ``supports_vision``,
Ollama returns ``capabilities: ["vision"]`` — we store the same shape and
resolve in that order:

    user     — toggled in Settings for that model id
    learned  — live chat probe (tiny ``image_url`` / Anthropic image part)
    api      — fields on ``GET /models``
    guess    — name hints (``vl``, ``gpt-4o``, ``claude-*``, …)

Endpoint-level ``image_in`` / ``video_in`` / ``audio_in`` stay in settings as
legacy mirrors of whatever the UI last showed. Runtime ignores them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

SOURCE_USER = "user"
SOURCE_LEARNED = "learned"
SOURCE_API = "api"
SOURCE_GUESS = "guess"

_SOURCE_RANK = {
    SOURCE_GUESS: 0,
    SOURCE_API: 1,
    SOURCE_LEARNED: 2,
    SOURCE_USER: 3,
}

KEYS = ("image_in", "video_in", "audio_in")

# Keep the Vue helper in SettingsPanel.vue in sync when this list changes.
_VISION_MODEL_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "claude-3",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "gemini",
    "vision",
    "-vl",
    "llava",
    "qwen-vl",
    "glm-4v",
    "glm-4.5v",
    "glm-4.6v",
    "omni",
    "internvl",
    "pixtral",
    "deepseek-flash",
    "deepseek-v4-flash",
)

# Official DeepSeek API: current chat IDs are served by V4.1-Flash (native
# vision) except ``deepseek-v4-pro``, which official docs still mark
# text-only until it is rerouted to Flash on 2026-09-14.
_OFFICIAL_DEEPSEEK_HOSTS = frozenset({"api.deepseek.com"})
_OFFICIAL_DEEPSEEK_PROVIDER_IDS = frozenset({"deepseek"})
_DEEPSEEK_PRO_VISION_ON = datetime(2026, 9, 14, tzinfo=timezone.utc)

# Deliberately much narrower than _VISION_MODEL_HINTS: accepting stills says
# nothing about accepting video. gpt-4o and the whole Claude family are vision
# models that reject video blocks outright, so they must NOT appear here.
_VIDEO_MODEL_HINTS = (
    "gemini",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "omni",
    "glm-4v",
    "video",
)

_AUDIO_MODEL_HINTS = (
    "audio",
    "whisper",
    "gpt-4o-realtime",
    "gpt-4o-audio",
)

_IMAGE_TOKENS = frozenset({"image", "images", "vision", "visual", "image_url", "input_image"})
_VIDEO_TOKENS = frozenset({"video", "videos", "video_url"})
_AUDIO_TOKENS = frozenset({"audio", "speech", "input_audio"})


def _guess_image_in(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _VISION_MODEL_HINTS)


def _endpoint_attr(endpoint: Any, key: str) -> str:
    if endpoint is None:
        return ""
    value = getattr(endpoint, key, None)
    if value is None and isinstance(endpoint, dict):
        value = endpoint.get(key)
    return str(value or "").strip()


def official_deepseek_endpoint(endpoint: Any) -> bool:
    """True for the first-party DeepSeek API, not third-party DeepSeek weights."""
    if _endpoint_attr(endpoint, "provider_id") in _OFFICIAL_DEEPSEEK_PROVIDER_IDS:
        return True
    host = (urlparse(_endpoint_attr(endpoint, "base_url")).hostname or "").lower()
    return host in _OFFICIAL_DEEPSEEK_HOSTS


def official_deepseek_image_in(model: str, endpoint: Any = None) -> bool:
    """Vision on the official host: Flash now, Pro after the documented reroute."""
    if not official_deepseek_endpoint(endpoint):
        return False
    name = (model or "").lower()
    if "deepseek-v4-pro" in name:
        return datetime.now(timezone.utc) >= _DEEPSEEK_PRO_VISION_ON
    return bool(name)


def _guess_video_in(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _VIDEO_MODEL_HINTS)


def _guess_audio_in(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _AUDIO_MODEL_HINTS)


def guess_model_capabilities(model: str, endpoint: Any = None) -> dict[str, Any]:
    """Name-only fallback used when the user and the gateway are silent."""
    video = _guess_video_in(model)
    image = video or _guess_image_in(model) or official_deepseek_image_in(model, endpoint)
    audio = _guess_audio_in(model)
    return {
        "image_in": image,
        "video_in": video,
        "audio_in": audio,
        "source": SOURCE_GUESS,
        "sources": {key: SOURCE_GUESS for key in KEYS},
    }


def normalize_capability_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _SOURCE_RANK else SOURCE_API


def normalize_model_capability_entry(raw: Any) -> dict[str, Any]:
    """Return a sanitized modality row or ``{}``."""
    if not isinstance(raw, dict):
        return {}
    entry: dict[str, Any] = {}
    for key in KEYS:
        if key in raw and raw[key] is not None:
            entry[key] = bool(raw[key])
    if not entry:
        return {}
    if entry.get("video_in"):
        entry["image_in"] = True
    entry["source"] = normalize_capability_source(raw.get("source"))
    return entry


def normalize_model_capabilities(raw: Any) -> dict[str, dict[str, Any]]:
    """Sanitize a model-id → modalities mapping from settings or an API payload."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        model = str(key or "").strip()
        if not model:
            continue
        entry = normalize_model_capability_entry(value)
        if entry:
            out[model] = entry
    return out


def _source_rank(source: Any) -> int:
    return _SOURCE_RANK.get(normalize_capability_source(source), 0)


def merge_model_capabilities(
    existing: Any,
    incoming: Any,
    *,
    incoming_source: str = SOURCE_API,
) -> dict[str, dict[str, Any]]:
    """Merge ``incoming`` onto ``existing`` without demoting a stronger source."""
    merged = normalize_model_capabilities(existing)
    source = normalize_capability_source(incoming_source)
    for model, raw in normalize_model_capabilities(incoming).items():
        previous = dict(merged.get(model) or {})
        prev_source = previous.get("source")
        if previous and _source_rank(prev_source) > _source_rank(source):
            for key in KEYS:
                if key not in previous and key in raw:
                    previous[key] = raw[key]
            if previous.get("video_in"):
                previous["image_in"] = True
            entry = normalize_model_capability_entry(previous)
            if entry:
                merged[model] = entry
            continue
        combined = dict(previous)
        for key in KEYS:
            if key in raw:
                combined[key] = raw[key]
        combined["source"] = source
        entry = normalize_model_capability_entry(combined)
        if entry:
            merged[model] = entry
    return merged


def _tokens_from_values(values: Any) -> set[str]:
    tokens: set[str] = set()
    if isinstance(values, str):
        blob = values.lower().replace("->", " ").replace("+", " ")
        for part in blob.replace(",", " ").replace("/", " ").replace("|", " ").split():
            part = part.strip()
            if part and part != "text":
                tokens.add(part)
    elif isinstance(values, (list, tuple, set)):
        for item in values:
            tokens.update(_tokens_from_values(item))
    elif isinstance(values, dict):
        for key, value in values.items():
            name = str(key or "").strip().lower()
            if name in KEYS:
                if value:
                    tokens.update(_tokens_from_values(name[:-3] if name.endswith("_in") else name))
                continue
            if value:
                tokens.update(_tokens_from_values(name))
                tokens.update(_tokens_from_values(value))
    return tokens


def _truthy_flag(record: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        if record.get(key) is True:
            return True
    return False


def extract_model_capabilities_from_record(record: Any) -> dict[str, Any]:
    """Pull image/video/audio input flags out of one ``/models`` item.

    Official OpenAI / Anthropic lists usually have only ``id``. OpenRouter,
    LiteLLM, Ollama and several CN gateways put modalities on the same object
    or one level down (``architecture``, ``model_info``).
    """
    if not isinstance(record, dict):
        return {}

    tokens: set[str] = set()
    found = False

    for key in ("input_modalities", "supported_input_modalities", "modalities", "capabilities"):
        if key in record:
            found = True
            tokens.update(_tokens_from_values(record.get(key)))

    supported = record.get("supported_parameters") or record.get("supported_params")
    if isinstance(supported, (list, tuple, set)):
        found = True
        tokens.update(_tokens_from_values(supported))

    architecture = record.get("architecture")
    if isinstance(architecture, dict):
        if "input_modalities" in architecture:
            found = True
            tokens.update(_tokens_from_values(architecture.get("input_modalities")))
        if architecture.get("modality"):
            found = True
            left, _, _right = str(architecture.get("modality")).partition("->")
            tokens.update(_tokens_from_values(left))

    model_info = record.get("model_info") if isinstance(record.get("model_info"), dict) else {}
    if _truthy_flag(record, "supports_vision", "supports_audio_input") or _truthy_flag(
        model_info, "supports_vision", "supports_audio_input"
    ):
        found = True

    if not found:
        return {}

    image = bool(tokens & _IMAGE_TOKENS) or _truthy_flag(record, "supports_vision")
    video = bool(tokens & _VIDEO_TOKENS)
    audio = bool(tokens & _AUDIO_TOKENS) or _truthy_flag(record, "supports_audio_input")
    if model_info:
        image = image or _truthy_flag(model_info, "supports_vision")
        audio = audio or _truthy_flag(model_info, "supports_audio_input")
    if "vision" in tokens:
        image = True
    if "video" in tokens:
        video = True
    if "audio" in tokens or "speech" in tokens:
        audio = True

    return normalize_model_capability_entry(
        {
            "image_in": image or video,
            "video_in": video,
            "audio_in": audio,
            "source": SOURCE_API,
        }
    )


def extract_model_capabilities_from_models_payload(data: Any) -> dict[str, dict[str, Any]]:
    """Parse an OpenAI-shaped ``{data: [...]}`` or a bare list of model rows."""
    rows: list[Any]
    if isinstance(data, dict):
        raw_rows = data.get("data")
        rows = raw_rows if isinstance(raw_rows, list) else []
        if not rows and data.get("id"):
            rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    capabilities: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("name") or "").strip()
        if not model_id:
            continue
        entry = extract_model_capabilities_from_record(row)
        if entry:
            capabilities[model_id] = entry
    return capabilities


def _endpoint_capability_store(endpoint: Any) -> dict[str, dict[str, Any]]:
    stored = getattr(endpoint, "model_capabilities", None)
    if stored is None and isinstance(endpoint, dict):
        stored = endpoint.get("model_capabilities")
    return normalize_model_capabilities(stored)


def resolve_endpoint_model_capabilities(
    endpoint: Any,
    model: str | None = None,
) -> dict[str, Any]:
    """Return modalities for ``model`` on ``endpoint``.

    Missing per-model keys fall back to the next weaker source. Video always
    implies image so the keyframe fallback never sees "clips yes, stills no".
    """
    chosen = str(model if model is not None else getattr(endpoint, "model", "") or "").strip()
    if not chosen and isinstance(endpoint, dict):
        chosen = str(endpoint.get("model") or "").strip()
    guessed = guess_model_capabilities(chosen, endpoint)
    stored = _endpoint_capability_store(endpoint).get(chosen, {}) if chosen else {}
    result = {key: bool(guessed[key]) for key in KEYS}
    sources = {key: SOURCE_GUESS for key in KEYS}
    stored_source = normalize_capability_source(stored.get("source")) if stored else SOURCE_GUESS
    if stored and stored_source in (SOURCE_USER, SOURCE_LEARNED, SOURCE_API):
        for key in KEYS:
            if key in stored:
                result[key] = bool(stored[key])
                sources[key] = stored_source
    if result["video_in"]:
        result["image_in"] = True
        if sources["image_in"] == SOURCE_GUESS:
            sources["image_in"] = sources["video_in"]
    dominant = sources["image_in"]
    return {
        "image_in": result["image_in"],
        "video_in": result["video_in"],
        "audio_in": result["audio_in"],
        "source": dominant,
        "sources": sources,
    }


# 1×1 PNG. Used only to ask the chat API whether ``image_url`` / image parts
# are accepted — the pixels themselves do not matter.
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

_VISION_REJECT_MARKERS = (
    "does not support image",
    "doesn't support image",
    "do not support image",
    "doesn't support images",
    "does not support images",
    "not support image",
    "images are not supported",
    "image is not supported",
    "does not support vision",
    "not a vision model",
    "without a vision",
    "unexpected item type",
    "unexpected content type",
    "unknown content type",
    "invalid content type",
    "image_url is not supported",
    "image_url is only supported",
    "does not support multimodal",
    "cannot accept image",
    "can't accept image",
    "不支持图片",
    "不支持图像",
    "不支持视觉",
    "不支持 image",
)


def build_image_probe_payload(model: str, *, protocol: str = "openai") -> dict[str, Any]:
    """Minimal chat body that includes one still so the gateway can reject it."""
    name = str(model or "").strip()
    if str(protocol or "openai").lower() == "anthropic":
        return {
            "model": name,
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "ok"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _PROBE_PNG_B64,
                            },
                        },
                    ],
                }
            ],
        }
    return {
        "model": name,
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ok"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG_B64}"},
                    },
                ],
            }
        ],
    }


def interpret_image_probe_response(status: int, body: str = "") -> bool | None:
    """``True`` / ``False`` when the reply is decisive; ``None`` if we cannot tell."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    text = str(body or "").lower()
    if 200 <= code < 300:
        return True
    if code in {400, 422} and any(marker in text for marker in _VISION_REJECT_MARKERS):
        return False
    return None


async def probe_model_image_in(
    client: Any,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    protocol: str = "openai",
) -> bool | None:
    """POST a 1-pixel image to the chat API and see whether the model accepts it."""
    name = str(model or "").strip()
    root = str(base_url or "").rstrip("/")
    if not name or not root:
        return None
    kind = str(protocol or "openai").lower()
    path = "/messages" if kind == "anthropic" else "/chat/completions"
    try:
        resp = await client.post(
            f"{root}{path}",
            headers=headers,
            json=build_image_probe_payload(name, protocol=kind),
        )
    except Exception:
        return None
    body = ""
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    return interpret_image_probe_response(getattr(resp, "status_code", 0), body)


# Tiny ISO-BMFF stub. Gateways that accept ``video_url`` usually reply 200 or
# "invalid media"; text/still-only models reject the part type instead.
_PROBE_MP4_B64 = "AAAAHGZ0eXBpc29tAAACAG1wNDFpc29tAAAAAGlzb20="

_VIDEO_REJECT_MARKERS = (
    "does not support video",
    "doesn't support video",
    "do not support video",
    "not support video",
    "videos are not supported",
    "video is not supported",
    "video_url is not supported",
    "video_url is only supported",
    "cannot accept video",
    "can't accept video",
    "unexpected item type",
    "unexpected content type",
    "unknown content type",
    "invalid content type",
    "不支持视频",
    "不支持 video",
)

_VIDEO_ACCEPTED_BUT_BAD_MEDIA = (
    "invalid video",
    "invalid media",
    "failed to decode",
    "cannot decode",
    "unable to process video",
    "unable to parse video",
    "corrupt",
    "无法解析视频",
    "视频解码",
)


def build_video_probe_payload(model: str, *, protocol: str = "openai") -> dict[str, Any] | None:
    """Anthropic has no video part; only OpenAI-compatible rails are probed."""
    if str(protocol or "openai").lower() == "anthropic":
        return None
    return {
        "model": str(model or "").strip(),
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ok"},
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{_PROBE_MP4_B64}"},
                    },
                ],
            }
        ],
    }


def interpret_video_probe_response(status: int, body: str = "") -> bool | None:
    """``True`` if the part type was accepted; ``False`` if the model rejects video."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    text = str(body or "").lower()
    if 200 <= code < 300:
        return True
    if code not in {400, 422}:
        return None
    if any(marker in text for marker in _VIDEO_ACCEPTED_BUT_BAD_MEDIA):
        return True
    if any(marker in text for marker in _VIDEO_REJECT_MARKERS):
        return False
    return None


async def probe_model_video_in(
    client: Any,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    protocol: str = "openai",
) -> bool | None:
    """POST a stub clip and see whether ``video_url`` is a legal content type."""
    name = str(model or "").strip()
    root = str(base_url or "").rstrip("/")
    kind = str(protocol or "openai").lower()
    payload = build_video_probe_payload(name, protocol=kind)
    if not name or not root or payload is None:
        return False if kind == "anthropic" and name else None
    try:
        resp = await client.post(
            f"{root}/chat/completions",
            headers=headers,
            json=payload,
        )
    except Exception:
        return None
    body = ""
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    return interpret_video_probe_response(getattr(resp, "status_code", 0), body)


async def probe_model_modalities(
    client: Any,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    protocol: str = "openai",
) -> dict[str, bool]:
    """Live-test image, then video (skipped when stills were already rejected)."""
    image = await probe_model_image_in(
        client,
        base_url=base_url,
        headers=headers,
        model=model,
        protocol=protocol,
    )
    if image is False:
        return {"image_in": False, "video_in": False}
    if str(protocol or "openai").lower() == "anthropic":
        learned = {"video_in": False}
        if image is not None:
            learned["image_in"] = image
        return learned
    video = await probe_model_video_in(
        client,
        base_url=base_url,
        headers=headers,
        model=model,
        protocol=protocol,
    )
    learned: dict[str, bool] = {}
    if image is not None:
        learned["image_in"] = image
    if video is not None:
        learned["video_in"] = video
        if video:
            learned["image_in"] = True
    return learned
