"""Auxiliary vision / ASR providers (separate from chat endpoints)."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from argusai.core.ai import AIConfig, AuxiliaryAIEndpoint, OpenAICompatibleProvider

log = logging.getLogger("argusai.media_auxiliary")

VISION_SYSTEM = (
    "You describe images for a security assessment and reverse-engineering assistant. Be factual and concise. "
    "Mention UI text, window titles, visible apps, and security-relevant technical details. "
    "Do not invent content that is not visible."
)

ASR_SYSTEM = (
    "You transcribe user speech for a security assessment and reverse-engineering assistant. "
    "Return only the transcript in the user's language, no commentary."
)


def auxiliary_endpoint_configured(ep: AuxiliaryAIEndpoint | None) -> bool:
    """True when base_url, model, and api_key are all present."""
    if not ep:
        return False
    return bool(
        str(ep.base_url or "").strip()
        and str(ep.model or "").strip()
        and str(ep.api_key or "").strip()
    )


def auxiliary_endpoint_enabled(ep: AuxiliaryAIEndpoint | None) -> bool:
    """Active iff the user explicitly toggled it on AND credentials are complete.

    Filling the URL/model/key does NOT auto-enable the endpoint — the switch
    in the Settings UI is the single source of truth.
    """
    if ep is None:
        return False
    if not bool(getattr(ep, "enabled", False)):
        return False
    return auxiliary_endpoint_configured(ep)


def auxiliary_endpoint_disable_reason(ep: AuxiliaryAIEndpoint | None, *, label: str) -> str:
    if auxiliary_endpoint_enabled(ep):
        return ""
    if not ep:
        return f"{label}未配置"
    if not ep.enabled:
        return f"{label}未启用（请在设置中打开右侧开关）"
    if not str(ep.base_url or "").strip():
        return f"{label}缺少接口地址（base_url）"
    if not str(ep.model or "").strip():
        return f"{label}缺少模型名（model）"
    if not str(ep.api_key or "").strip():
        return f"{label}缺少 API Key"
    return f"{label}未就绪"


def create_auxiliary_provider(ep: AuxiliaryAIEndpoint | None) -> OpenAICompatibleProvider | None:
    """Build a direct OpenAI-compatible provider for the auxiliary endpoint.

    Auxiliary endpoints (vision/ASR) are single-endpoint and must NEVER go
    through :class:`FallbackProvider` — that would let a failing vision call
    silently spill over into the chat endpoints. We instantiate
    :class:`OpenAICompatibleProvider` directly so the call site sees the real
    error (auth / model-not-found / format-rejected) instead of being told
    "未配置或未启用".
    """
    if not auxiliary_endpoint_enabled(ep):
        return None
    assert ep is not None
    config = AIConfig(
        base_url=ep.base_url,
        api_key=ep.api_key,
        model=ep.model,
        max_tokens=ep.max_tokens,
        context_window=ep.context_window,
        temperature=ep.temperature,
        send_temperature=ep.send_temperature,
        timeout=ep.timeout,
        protocol=ep.protocol or "openai",
        extra_headers=dict(ep.extra_headers or {}),
        extra_body=dict(ep.extra_body or {}),
        # This is a dedicated vision/ASR endpoint: always carry the image/audio
        # block. Without this the provider's modality gate would strip the
        # image_url (the model name may not match the vision heuristic, and the
        # auxiliary AIConfig has image_in/audio_in unset), so the vision model
        # would receive a text-only request and answer "no image provided".
        force_multimodal=True,
    )
    return OpenAICompatibleProvider(config)


async def vision_describe_image(
    settings: Any,
    *,
    data_base64: str,
    mime: str = "image/png",
    user_hint: str = "",
) -> dict[str, Any]:
    """Internal vision bridge — uses configured auxiliary endpoint only."""
    ep = getattr(settings, "ai_auxiliary_vision", None)
    provider = create_auxiliary_provider(ep)
    if provider is None:
        return {"ok": False, "error": "视觉辅助端点未配置或未启用"}
    prompt = user_hint.strip() or "请描述这张图片中的可见内容，便于安全分析。"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": VISION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data_base64}"},
                },
            ],
        },
    ]
    try:
        result = await provider.chat(messages, max_tokens=min(int(getattr(ep, "max_tokens", 4096) or 4096), 8192))
        text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        await provider.close()
        return {"ok": True, "description": str(text).strip(), "model": getattr(ep, "model", "")}
    except Exception as exc:
        log.warning("vision_describe_image failed: %s", exc)
        try:
            await provider.close()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


async def asr_transcribe_audio(
    settings: Any,
    *,
    data_base64: str | None = None,
    file_path: str | Path | None = None,
    mime: str = "audio/wav",
    language_hint: str = "",
) -> dict[str, Any]:
    """Internal ASR bridge — OpenAI-compatible audio input or text fallback."""
    ep = getattr(settings, "ai_auxiliary_asr", None)
    provider = create_auxiliary_provider(ep)
    if provider is None:
        return {"ok": False, "error": "ASR 辅助端点未配置或未启用"}

    raw_b64 = data_base64 or ""
    if not raw_b64 and file_path:
        try:
            raw_b64 = base64.b64encode(Path(file_path).read_bytes()).decode("ascii")
        except OSError as exc:
            return {"ok": False, "error": f"读取音频失败: {exc}"}

    if not raw_b64:
        return {"ok": False, "error": "无音频数据"}

    # OpenAI-compatible audio input.
    #
    # DashScope qwen3-asr-flash (compatible mode) is strict:
    #   - `input_audio.data` MUST be a `data:audio/...;base64,...` URL
    #   - NO `format` field (that's GPT-4o syntax — DashScope errors on it)
    #   - NO system prompt, NO text part, NO extra messages
    #   - Officially documented MIME: audio/wav, audio/mpeg; other models may
    #     accept more (webm/ogg/opus/m4a), so we always send the real MIME and
    #     let the upstream decide.
    fmt = _audio_format_from_mime(mime)
    audio_mime = mime if mime and mime.startswith("audio/") else f"audio/{fmt}"
    audio_data = raw_b64
    if raw_b64 and not raw_b64.startswith("data:"):
        audio_data = f"data:{audio_mime};base64,{raw_b64}"
    is_qwen_asr = "asr" in str(getattr(ep, "model", "") or "").lower()
    user_content: list[dict[str, Any]] = []
    if not is_qwen_asr:
        user_content.append({
            "type": "text",
            "text": (language_hint.strip() or "请将以下音频转写为文字，只输出转写正文。"),
        })
    audio_block: dict[str, Any] = {"data": audio_data}
    if not is_qwen_asr:
        audio_block["format"] = fmt
    user_content.append({"type": "input_audio", "input_audio": audio_block})
    messages: list[dict[str, Any]] = []
    if not is_qwen_asr:
        messages.append({"role": "system", "content": ASR_SYSTEM})
    messages.append({"role": "user", "content": user_content})
    log.info(
        "asr.call model=%s base_url=%s fmt=%s mime=%s payload_bytes=%d",
        getattr(ep, "model", ""),
        getattr(ep, "base_url", ""),
        fmt,
        audio_mime,
        len(raw_b64),
    )
    try:
        result = await provider.chat(messages, max_tokens=min(int(getattr(ep, "max_tokens", 4096) or 4096), 8192))
        text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        await provider.close()
        transcript = str(text).strip()
        log.info("asr.ok model=%s transcript_chars=%d", getattr(ep, "model", ""), len(transcript))
        return {"ok": True, "transcript": transcript, "model": getattr(ep, "model", "")}
    except Exception as exc:
        log.warning("asr.fail model=%s err=%s", getattr(ep, "model", ""), exc)
        try:
            await provider.close()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


def _audio_format_from_mime(mime: str) -> str:
    m = (mime or "").lower().split(";")[0].strip()
    if "wav" in m:
        return "wav"
    if "mp3" in m or "mpeg" in m:
        return "mp3"
    if "ogg" in m:
        return "ogg"
    if "m4a" in m or "mp4" in m:
        return "m4a"
    if "webm" in m:
        return "webm"
    return "wav"
