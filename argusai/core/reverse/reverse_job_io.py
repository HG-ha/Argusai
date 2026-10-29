"""I/O helpers and normalizers for ReverseJobStore."""

from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time as _time
from pathlib import Path
from typing import Any, Iterable

from argusai.core.reverse.reverse_constants import (
    ARTIFACT_KINDS,
    DEFAULT_ANALYSIS_SUMMARY,
    DEFAULT_RISK_SUMMARY,
    DEFAULT_TOOLCHAIN_STATUS,
    FINDING_KINDS,
    JOB_SCHEMA_VERSION,
    JOB_STATUS_VALUES,
    PLATFORMS,
    TARGET_TYPES,
)

JOB_ID_RE = re.compile(r"^rev-\d{8}-[A-Za-z0-9]{4,8}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def now_iso() -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime())


def new_job_id() -> str:
    date = _time.strftime("%Y%m%d", _time.localtime())
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=4))
    return f"rev-{date}-{rand}"


def new_sub_id(prefix: str) -> str:
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return f"{prefix}-{rand}"


def slug(value: Any) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return s[:64] if s else ""


def slug_tags(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        t = str(raw or "").strip().lower()
        if not t:
            continue
        t = re.sub(r"[^a-z0-9._-]+", "-", t).strip("-")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= 32:
            break
    return out


def str_list(values: Iterable[Any], max_items: int = 32, max_len: int = 400) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if not s:
            continue
        if len(s) > max_len:
            s = s[:max_len]
        out.append(s)
        if len(out) >= max_items:
            break
    return out


def norm_status(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in JOB_STATUS_VALUES else "active"


def norm_artifact_kind(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ARTIFACT_KINDS else "other"


def norm_finding_kind(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in FINDING_KINDS else "note"


def norm_target_type(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in TARGET_TYPES else "unknown"


def norm_platform(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in PLATFORMS else "unknown"


def norm_dict(value: Any, *, max_keys: int = 64) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for idx, (k, v) in enumerate(value.items()):
        if idx >= max_keys:
            break
        key = str(k or "").strip()[:64]
        if not key:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[key] = v
        elif isinstance(v, list):
            out[key] = str_list(v, max_items=16, max_len=400)
        elif isinstance(v, dict):
            out[key] = {str(sk)[:64]: sv for sk, sv in list(v.items())[:16]}
    return out


def default_job_runtime_fields(
    *,
    target_type: str = "unknown",
    platform: str = "unknown",
    target_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import copy

    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "target_type": target_type,
        "platform": platform,
        "target_meta": norm_dict(target_meta or {}),
        "toolchain_status": copy.deepcopy(DEFAULT_TOOLCHAIN_STATUS),
        "analysis_summary": copy.deepcopy(DEFAULT_ANALYSIS_SUMMARY),
        "risk_summary": copy.deepcopy(DEFAULT_RISK_SUMMARY),
        "runtime_sessions": [],
        "evidence_refs": [],
        "replay_refs": [],
    }


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def read_jsonl(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if tail is not None and tail > 0:
        lines = lines[-tail:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for child in p.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total
