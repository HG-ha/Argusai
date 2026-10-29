"""Enrich user attachments via auxiliary vision/ASR before the chat model runs."""

from __future__ import annotations

import logging
from typing import Any

from argusai.core.ai import AIEndpoint
from argusai.core.ai_model_capabilities import (
    SOURCE_API,
    SOURCE_USER,
    resolve_endpoint_model_capabilities,
)

from .media_auxiliary import (
    asr_transcribe_audio,
    auxiliary_endpoint_disable_reason,
    auxiliary_endpoint_enabled,
    vision_describe_image,
)
from .video_frames import extract_keyframes, ffmpeg_available

log = logging.getLogger("argusai.media_enrichment")

# Keyframes handed straight to a vision model are cheap; keyframes routed
# through the auxiliary endpoint cost one round-trip each, so sample fewer.
_VIDEO_FRAMES_NATIVE = 4
_VIDEO_FRAMES_AUXILIARY = 3


def _active_chat_endpoint(settings: Any) -> AIEndpoint | None:
    eps = [ep for ep in getattr(settings, "ai_endpoints", []) or [] if getattr(ep, "enabled", True)]
    if not eps:
        return None
    eps.sort(key=lambda e: int(getattr(e, "priority", 0) or 0), reverse=True)
    return eps[0]


def _resolved_caps(ep: AIEndpoint | None) -> dict[str, Any]:
    if not ep:
        return {
            "image_in": False,
            "video_in": False,
            "audio_in": False,
            "source": "guess",
            "sources": {},
        }
    return resolve_endpoint_model_capabilities(ep)


def _endpoint_supports_image(ep: AIEndpoint | None) -> bool:
    """True only when the active model actually receives image_url blocks."""
    return bool(_resolved_caps(ep).get("image_in"))


def _endpoint_supports_video(ep: AIEndpoint | None) -> bool:
    """Mirrors :func:`argusai.core.ai._effective_native_video_in`.

    Vision models that only take stills (gpt-4o, claude, …) must land in the
    keyframe fallback, not in the native rail — those gateways 4xx on a
    ``video_url`` block.
    """
    return bool(_resolved_caps(ep).get("video_in"))


def _endpoint_supports_audio(ep: AIEndpoint | None) -> bool:
    return bool(_resolved_caps(ep).get("audio_in"))


def _explicit_no_image(ep: AIEndpoint | None) -> bool:
    """True when Settings or ``/models`` said this model does not take stills."""
    caps = _resolved_caps(ep)
    if caps.get("image_in"):
        return False
    source = str((caps.get("sources") or {}).get("image_in") or "")
    return source in (SOURCE_USER, SOURCE_API)


def vision_capability_note(settings: Any) -> str:
    """One-line system-prompt note telling the agent how (and whether) it can see images.

    The agent must never hallucinate "I cannot see the image" when an
    auxiliary vision endpoint is wired up, nor pretend to see images when no
    vision path exists at all. This is computed per turn from the *active
    model*, not from an endpoint-wide toggle.
    """
    chat_ep = _active_chat_endpoint(settings)
    native = _endpoint_supports_image(chat_ep)
    aux_on = auxiliary_endpoint_enabled(getattr(settings, "ai_auxiliary_vision", None))
    if native:
        line = (
            "- Image understanding: **NATIVE** — the active chat model accepts images directly, "
            "so user-attached images and tool/desktop/browser screenshots are visible to you. "
            "You are not limited to the session sandbox: inspect any image file with the "
            "always-loaded `vision_inspect_image(path=...)`. "
            "`codebase_read` / `read_text_file` / `sandbox_read_file` also attach raster images. "
            "Do not copy a file into the sandbox just to look at it."
        )
        if aux_on:
            line += " You may also call `vision_inspect_image` to re-read any image on demand."
        return line + "\n" + _video_capability_line(chat_ep, native_image=True, aux_on=aux_on)
    if aux_on:
        model = str(getattr(getattr(settings, "ai_auxiliary_vision", None), "model", "") or "")
        return (
            "- Image understanding: **AVAILABLE via auxiliary vision endpoint**"
            + (f" (`{model}`)" if model else "")
            + ". You DO have image-understanding capability even though the main chat model is text-only. "
            "User-attached images are auto-transcribed into the message as **[视觉理解 · …]** text before you run. "
            "Tool/desktop/browser screenshots are NOT auto-transcribed — call `vision_inspect_image` to read them. "
            "Disk images (project, host, or sandbox) use `vision_inspect_image(path=...)` or a read tool; "
            "do not copy them into the sandbox just to look. "
            "Never tell the user you cannot see an image; call `vision_inspect_image` instead."
        ) + "\n" + _video_capability_line(chat_ep, native_image=False, aux_on=True)
    if _explicit_no_image(chat_ep):
        return (
            "- Image understanding: **UNAVAILABLE** — the active chat model is recorded as "
            "text-only and no auxiliary vision endpoint is enabled. You genuinely cannot see "
            "images. Ask the user to describe the image in text; do not pretend to see it. "
            "(The user can enable one in 设置 → AI 配置 → 视觉辅助, or turn on 原生视觉输入 "
            "for this model.)"
        ) + "\n" + _video_capability_line(chat_ep, native_image=False, aux_on=False)
    return (
        "- Image understanding: **UNCONFIRMED** — this model name is not known to accept "
        "images and no auxiliary vision endpoint is enabled. Do not invent pixel details. "
        "If the user attached an image, ask them to enable 原生视觉输入 for the current "
        "model or 设置 → AI 配置 → 视觉辅助; do not claim you can see it."
    ) + "\n" + _video_capability_line(chat_ep, native_image=False, aux_on=False, unconfirmed=True)


def _video_capability_line(
    ep: AIEndpoint | None,
    *,
    native_image: bool,
    aux_on: bool,
    unconfirmed: bool = False,
) -> str:
    """Video is gated separately from stills — say which of the three paths is live."""
    if _endpoint_supports_video(ep):
        return (
            "- Video understanding: **NATIVE** — the active chat model accepts video clips directly."
        )
    if unconfirmed and not (native_image or aux_on):
        return (
            "- Video understanding: **UNCONFIRMED** — this model is not known to accept video. "
            "Attached clips may only reach you as a text note."
        )
    if not (native_image or aux_on):
        return (
            "- Video understanding: **UNAVAILABLE** — no image path exists either, so attached "
            "clips reach you as a text note only."
        )
    if not ffmpeg_available():
        return (
            "- Video understanding: **UNAVAILABLE** — the model takes stills but not video, and "
            "ffmpeg is not installed locally, so keyframes cannot be extracted. Tell the user to "
            "install ffmpeg (or set its path in 设置 → 逆向 Toolchain 路径) or switch to a "
            "video-capable endpoint; never pretend to have watched the clip."
        )
    return (
        "- Video understanding: **KEYFRAMES ONLY** — attached clips are sampled with ffmpeg into a "
        "few evenly spaced stills before you run. You see snapshots, not motion: do not claim to "
        "have watched the clip, and say so if a question needs the frames in between."
    )


def _video_label(att: dict[str, Any]) -> str:
    name = str(att.get("name") or "").strip() or "video"
    duration_ms = att.get("duration_ms")
    try:
        sec = int(int(duration_ms) / 1000)
    except (TypeError, ValueError):
        sec = 0
    return f"{name} · {sec // 60:02d}:{sec % 60:02d}" if sec > 0 else name


async def _degrade_video_to_frames(
    settings: Any,
    att: dict[str, Any],
    *,
    native_image: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn one video into stills the active setup can actually read.

    Returns ``(derived_attachments, notes)``. Derived entries are ordinary
    ``image`` attachments tagged ``derived="video_frame"`` so the caller can
    splice them into history; notes are prose added to the user turn.
    """
    label = _video_label(att)
    aux_ep = getattr(settings, "ai_auxiliary_vision", None)
    aux_on = auxiliary_endpoint_enabled(aux_ep)

    if not native_image and not aux_on:
        reason = auxiliary_endpoint_disable_reason(aux_ep, label="视觉辅助端点")
        return [], [
            f"- 用户附带了视频 **{label}**，但当前对话模型既不支持视频也不支持图片，且{reason}。"
            "请让用户改用具备视觉能力的端点，或改为文字描述。"
        ]

    if not ffmpeg_available():
        return [], [
            f"- 用户附带了视频 **{label}**，但当前对话模型不支持原生视频输入，"
            "且本机未检测到 ffmpeg（用于抽取关键帧）。视频内容未能送达。"
            "请提示用户：安装 ffmpeg 并加入 PATH（或在 设置 → 逆向 Toolchain 路径 中指定 ffmpeg），"
            "或改用支持视频输入的端点。"
        ]

    count = _VIDEO_FRAMES_NATIVE if native_image else _VIDEO_FRAMES_AUXILIARY
    result = await extract_keyframes(
        path=str(att.get("host_path") or att.get("sandbox_path") or att.get("path") or ""),
        data_base64=str(att.get("data_base64") or ""),
        mime=str(att.get("mime") or "video/mp4"),
        count=count,
    )
    if not result.get("ok"):
        if result.get("reason") == "no_ffmpeg":
            return [], [
                f"- 用户附带了视频 **{label}**，但本机未检测到 ffmpeg，无法抽取关键帧。"
            ]
        return [], [
            f"- 视频 **{label}** 的关键帧抽取失败：{result.get('error', 'unknown')}。"
        ]

    frames = result.get("frames") or []
    stamps = "、".join(str(f.get("label") or "") for f in frames if f.get("label"))

    if native_image:
        derived = [
            {
                "type": "image",
                "mime": frame.get("mime") or "image/jpeg",
                "data_base64": frame.get("data_base64") or "",
                "name": f"{att.get('name') or 'video'} @ {frame.get('label') or i + 1}",
                "derived": "video_frame",
            }
            for i, frame in enumerate(frames)
        ]
        return derived, [
            f"- 用户附带了视频 **{label}**。当前对话模型不支持原生视频，已用 ffmpeg 抽取 "
            f"{len(frames)} 帧关键帧（{stamps or '均匀采样'}）并作为图片一并提供；"
            "这是静态采样，帧与帧之间的动作可能缺失。"
        ]

    notes: list[str] = []
    described = 0
    for frame in frames:
        described += 1
        outcome = await vision_describe_image(
            settings,
            data_base64=frame.get("data_base64") or "",
            mime=frame.get("mime") or "image/jpeg",
        )
        stamp = frame.get("label") or f"#{described}"
        if outcome.get("ok"):
            notes.append(
                f"- **[视频关键帧 · {label} @ {stamp}]**"
                f"（辅助端点 `{outcome.get('model', '')}`）\n\n{outcome.get('description', '')}"
            )
        else:
            notes.append(f"- 视频关键帧 {stamp} 理解失败：{outcome.get('error', 'unknown')}")
    return [], notes


def _voice_seconds_label(att: dict[str, Any]) -> str:
    sec = max(1, int(int(att.get("duration_ms") or 1000) / 1000))
    return f"{sec}秒"


def _apply_voice_transcript(message: str, transcript: str) -> str:
    text = str(transcript or "").strip()
    if not text:
        return message
    base = (message or "").rstrip()
    if not base:
        return text
    return f"{base}\n\n{text}"


def _voice_placeholder(att: dict[str, Any], detail: str) -> str:
    return f"[语音消息 · {_voice_seconds_label(att)}；{detail}]"


async def enrich_user_attachments(
    settings: Any,
    message: str,
    attachments: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Run internal vision/ASR tools when the active chat model lacks modalities."""
    if not attachments:
        return message, attachments

    chat_ep = _active_chat_endpoint(settings)
    notes: list[str] = []
    out: list[dict[str, Any]] = []

    for att in attachments:
        kind = (att.get("type") or "").lower()
        is_voice = att.get("origin") == "voice"

        if kind == "image" and not _endpoint_supports_image(chat_ep):
            vision_ep = getattr(settings, "ai_auxiliary_vision", None)
            if not auxiliary_endpoint_enabled(vision_ep):
                reason = auxiliary_endpoint_disable_reason(vision_ep, label="视觉辅助端点")
                notes.append(f"- 用户附带了图片，但当前对话模型不支持视觉且{reason}。")
                continue
            data = att.get("data_base64") or ""
            mime = att.get("mime") or "image/png"
            if not data:
                continue
            result = await vision_describe_image(settings, data_base64=data, mime=mime)
            if result.get("ok"):
                name = att.get("name") or "image"
                notes.append(f"- **[视觉理解 · {name}]**（辅助端点 `{result.get('model', '')}`）\n\n{result.get('description', '')}")
            else:
                notes.append(f"- 视觉理解失败：{result.get('error', 'unknown')}")
            continue

        if kind == "video" and not _endpoint_supports_video(chat_ep):
            derived, video_notes = await _degrade_video_to_frames(
                settings,
                att,
                native_image=_endpoint_supports_image(chat_ep),
            )
            out.extend(derived)
            notes.extend(video_notes)
            continue

        if kind == "audio" and (is_voice or not _endpoint_supports_audio(chat_ep)):
            asr_ep = getattr(settings, "ai_auxiliary_asr", None)
            log.info(
                "asr_endpoint_probe is_voice=%s enabled=%s has_url=%s has_model=%s has_key=%s ep_enabled_flag=%s",
                is_voice,
                auxiliary_endpoint_enabled(asr_ep),
                bool(str(getattr(asr_ep, "base_url", "") or "").strip()),
                bool(str(getattr(asr_ep, "model", "") or "").strip()),
                bool(str(getattr(asr_ep, "api_key", "") or "").strip()),
                bool(getattr(asr_ep, "enabled", False)),
            )
            if not auxiliary_endpoint_enabled(asr_ep):
                reason = auxiliary_endpoint_disable_reason(asr_ep, label="ASR 辅助端点")
                if is_voice and not (message or "").strip():
                    message = _voice_placeholder(att, reason)
                else:
                    label = "语音消息" if is_voice else "语音"
                    notes.append(f"- 用户发送了{label}，但当前对话模型不支持音频且{reason}。")
                continue
            result = await asr_transcribe_audio(
                settings,
                data_base64=att.get("data_base64") or "",
                file_path=att.get("host_path") or att.get("sandbox_path") or att.get("path") or "",
                mime=att.get("mime") or "audio/wav",
            )
            if result.get("ok"):
                transcript = str(result.get("transcript") or "").strip()
                if is_voice:
                    message = _apply_voice_transcript(message, transcript)
                    if not transcript and not (message or "").strip():
                        message = _voice_placeholder(att, "转写结果为空")
                else:
                    name = att.get("name") or "audio"
                    notes.append(
                        f"- **[语音转写 · {name}]**（辅助端点 `{result.get('model', '')}`）\n\n{transcript}"
                    )
            elif is_voice and not (message or "").strip():
                message = _voice_placeholder(att, f"转写失败：{result.get('error', 'unknown')}")
            else:
                notes.append(f"- 语音转写失败：{result.get('error', 'unknown')}")
            continue

        if kind == "image" and _endpoint_supports_image(chat_ep):
            out.append(att)
        elif kind == "video" and _endpoint_supports_video(chat_ep):
            out.append(att)
        elif kind == "audio" and _endpoint_supports_audio(chat_ep):
            out.append(att)
        elif kind not in ("image", "video", "audio"):
            out.append(att)

    addendum = ""
    if notes:
        addendum = "\n\n---\n**多模态辅助理解**（内部视觉/ASR 端点，非模型原生能力）\n\n" + "\n\n".join(notes)

    return (message or "").rstrip() + addendum, out
