"""User habits / preferences profile (distinct from knowledge base)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserHabitsProfile:
    speaking_style: str = ""
    task_focus: str = ""
    audit_domains: list[str] = field(default_factory=list)
    risk_preference: str = ""
    output_format: str = ""
    avoid: str = ""
    notes: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaking_style": str(self.speaking_style or ""),
            "task_focus": str(self.task_focus or ""),
            "audit_domains": [str(x).strip() for x in (self.audit_domains or []) if str(x).strip()][:20],
            "risk_preference": str(self.risk_preference or ""),
            "output_format": str(self.output_format or ""),
            "avoid": str(self.avoid or ""),
            "notes": str(self.notes or ""),
            "updated_at": float(self.updated_at or 0),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> UserHabitsProfile:
        if not isinstance(raw, dict):
            raw = {}
        domains = raw.get("audit_domains") or []
        if isinstance(domains, str):
            domains = [x.strip() for x in domains.replace(",", "\n").splitlines() if x.strip()]
        elif not isinstance(domains, list):
            domains = []
        return cls(
            speaking_style=str(raw.get("speaking_style") or "")[:2000],
            task_focus=str(raw.get("task_focus") or "")[:2000],
            audit_domains=[str(x).strip()[:120] for x in domains if str(x).strip()][:20],
            risk_preference=str(raw.get("risk_preference") or "")[:500],
            output_format=str(raw.get("output_format") or "")[:2000],
            avoid=str(raw.get("avoid") or "")[:2000],
            notes=str(raw.get("notes") or "")[:4000],
            updated_at=float(raw.get("updated_at") or 0),
        )


def profile_from_settings(settings: Any) -> UserHabitsProfile:
    if settings is None:
        return UserHabitsProfile()
    if hasattr(settings, "user_habits"):
        raw = getattr(settings, "user_habits")
        if isinstance(raw, UserHabitsProfile):
            return raw
        return UserHabitsProfile.from_dict(raw)
    if isinstance(settings, dict):
        return UserHabitsProfile.from_dict(settings.get("user_habits") or settings.get("user_profile"))
    return UserHabitsProfile()


def profile_prompt_summary(settings: Any, *, max_chars: int = 1200) -> str:
    profile = profile_from_settings(settings)
    parts: list[str] = []
    if profile.speaking_style.strip():
        parts.append(f"- 说话风格：{profile.speaking_style.strip()}")
    if profile.task_focus.strip():
        parts.append(f"- 任务方向：{profile.task_focus.strip()}")
    if profile.audit_domains:
        parts.append(f"- 常用审计方向：{', '.join(profile.audit_domains)}")
    if profile.risk_preference.strip():
        parts.append(f"- 风险偏好：{profile.risk_preference.strip()}")
    if profile.output_format.strip():
        parts.append(f"- 输出偏好：{profile.output_format.strip()}")
    if profile.avoid.strip():
        parts.append(f"- 避免事项：{profile.avoid.strip()}")
    if profile.notes.strip():
        parts.append(f"- 备注：{profile.notes.strip()[:400]}")
    if not parts:
        return ""
    text = "## User habits profile\n" + "\n".join(parts)
    return text[:max_chars]
