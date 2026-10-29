"""ACP session configOptions helpers (model selectors and friends)."""

from __future__ import annotations

from typing import Any


def _text(value: Any, *, max_length: int = 200) -> str:
    return str(value or "").strip()[:max_length]


def _category(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("category") or value.get("id"), max_length=64).lower()
    return _text(value, max_length=64).lower()


def flatten_select_choices(raw: Any) -> list[dict[str, str]]:
    """Flatten select option lists that may nest groups."""
    rows: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("options"), list) and "value" not in item:
            rows.extend(flatten_select_choices(item.get("options")))
            continue
        value = _text(item.get("value"), max_length=200)
        if not value:
            continue
        rows.append({
            "value": value,
            "name": _text(item.get("name") or value, max_length=160),
        })
    # De-dupe by value while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        if row["value"] in seen:
            continue
        seen.add(row["value"])
        unique.append(row)
    return unique


def normalize_config_options(raw: Any) -> list[dict[str, Any]]:
    """JSON-clean configOptions from session/new|load|set_config_option."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        option_id = _text(item.get("id") or item.get("configId"), max_length=120)
        if not option_id:
            continue
        option_type = _text(item.get("type"), max_length=32).lower() or "select"
        category = _category(item.get("category"))
        name = _text(item.get("name") or option_id, max_length=160)
        if option_type == "boolean":
            current = item.get("currentValue", item.get("current_value"))
            out.append({
                "id": option_id,
                "name": name,
                "type": "boolean",
                "category": category,
                "current_value": bool(current),
            })
            continue
        choices = flatten_select_choices(item.get("options"))
        if not choices:
            continue
        current_value = _text(
            item.get("currentValue", item.get("current_value")),
            max_length=200,
        )
        if current_value and current_value not in {row["value"] for row in choices}:
            choices.insert(0, {"value": current_value, "name": current_value})
        out.append({
            "id": option_id,
            "name": name,
            "type": "select",
            "category": category,
            "current_value": current_value,
            "options": choices,
        })
    return out


def is_model_config_option(option: dict[str, Any]) -> bool:
    """True when an option should appear in channel/GUI model pickers."""
    category = _category(option.get("category"))
    option_id = _text(option.get("id"), max_length=120).lower()
    name = _text(option.get("name"), max_length=160).lower()
    if category in {"model", "models"}:
        return True
    if option_id in {"model", "models"}:
        return True
    if "model" in name and option.get("type") == "select":
        return True
    return False


def model_config_options(options: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Prefer category=model selects; fall back to all select options when none."""
    rows = [dict(item) for item in options if isinstance(item, dict)]
    models = [item for item in rows if item.get("type") == "select" and is_model_config_option(item)]
    if models:
        return models
    return [item for item in rows if item.get("type") == "select"]
