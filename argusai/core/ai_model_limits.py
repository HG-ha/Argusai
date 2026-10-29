"""Per-model context / output limits for chat endpoints.

Endpoint-level ``context_window`` / ``max_tokens`` stay the fallback. When the
same gateway serves several models, each id can carry its own pair so the
assistant budget follows the model that will actually run — not the vendor
preset written when the row was created.

Sources, highest rank wins:

- ``user`` — typed in Settings for that model
- ``learned`` — parsed from a provider 400 (output cap)
- ``api`` — fields on ``GET /models``
- ``preset`` — provider-catalogue default copied onto one model id
"""

from __future__ import annotations

from typing import Any

SOURCE_USER = "user"
SOURCE_LEARNED = "learned"
SOURCE_API = "api"
SOURCE_PRESET = "preset"

_SOURCE_RANK = {
    SOURCE_PRESET: 0,
    SOURCE_API: 1,
    SOURCE_LEARNED: 2,
    SOURCE_USER: 3,
}

_CONTEXT_KEYS = (
    "context_length",
    "context_window",
    "max_model_len",
    "max_context_length",
    "max_input_tokens",
    "n_ctx",
    "n_ctx_train",
)
_OUTPUT_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "max_output",
    "completion_tokens",
)
_NESTED_OBJECTS = ("top_provider", "meta", "architecture")

_MIN_CONTEXT = 1024
_MIN_OUTPUT = 256
_MAX_TOKENS = 10_000_000


def _positive_int(value: Any, *, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number < minimum or number > _MAX_TOKENS:
        return 0
    return number


def normalize_limit_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _SOURCE_RANK else SOURCE_API


def normalize_model_limit_entry(raw: Any) -> dict[str, Any]:
    """Return a sanitized ``{context_window, max_tokens, source}`` or ``{}``."""
    if not isinstance(raw, dict):
        return {}
    context_window = _positive_int(raw.get("context_window"), minimum=_MIN_CONTEXT)
    max_tokens = _positive_int(raw.get("max_tokens"), minimum=_MIN_OUTPUT)
    if not context_window and not max_tokens:
        return {}
    entry: dict[str, Any] = {"source": normalize_limit_source(raw.get("source"))}
    if context_window:
        entry["context_window"] = context_window
    if max_tokens:
        entry["max_tokens"] = max_tokens
    return entry


def normalize_model_limits(raw: Any) -> dict[str, dict[str, Any]]:
    """Sanitize a model-id → limits mapping from settings or an API payload."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        model = str(key or "").strip()
        if not model:
            continue
        entry = normalize_model_limit_entry(value)
        if entry:
            out[model] = entry
    return out


def _source_rank(source: Any) -> int:
    return _SOURCE_RANK.get(normalize_limit_source(source), 0)


def merge_model_limits(
    existing: Any,
    incoming: Any,
    *,
    incoming_source: str = SOURCE_API,
) -> dict[str, dict[str, Any]]:
    """Merge ``incoming`` onto ``existing`` without demoting a stronger source."""
    merged = normalize_model_limits(existing)
    source = normalize_limit_source(incoming_source)
    for model, raw in normalize_model_limits(incoming).items():
        candidate = dict(raw)
        candidate["source"] = source
        previous = merged.get(model)
        if previous is not None and _source_rank(previous.get("source")) > _source_rank(source):
            continue
        combined = dict(previous or {})
        if candidate.get("context_window"):
            combined["context_window"] = candidate["context_window"]
        if candidate.get("max_tokens"):
            combined["max_tokens"] = candidate["max_tokens"]
        combined["source"] = source
        entry = normalize_model_limit_entry(combined)
        if entry:
            merged[model] = entry
    return merged


def _scan_limit_fields(record: dict[str, Any]) -> tuple[int, int]:
    context_window = 0
    max_tokens = 0
    for key in _CONTEXT_KEYS:
        if not context_window:
            context_window = _positive_int(record.get(key), minimum=_MIN_CONTEXT)
    for key in _OUTPUT_KEYS:
        if not max_tokens:
            max_tokens = _positive_int(record.get(key), minimum=_MIN_OUTPUT)
    return context_window, max_tokens


def extract_model_limits_from_record(record: Any) -> dict[str, Any]:
    """Pull context / output caps out of one ``/models`` item.

    Official OpenAI / Anthropic lists usually have only ``id``. OpenRouter,
    vLLM, Together, Groq and many CN gateways put the window on the same
    object or one level down (``top_provider``, ``meta``).
    """
    if not isinstance(record, dict):
        return {}
    context_window, max_tokens = _scan_limit_fields(record)
    for nested_key in _NESTED_OBJECTS:
        nested = record.get(nested_key)
        if not isinstance(nested, dict):
            continue
        nested_context, nested_output = _scan_limit_fields(nested)
        context_window = context_window or nested_context
        max_tokens = max_tokens or nested_output
    model_info = record.get("model_info")
    if isinstance(model_info, dict) and not context_window:
        for key, value in model_info.items():
            if "context_length" in str(key).lower() or str(key).lower().endswith(".context_length"):
                context_window = _positive_int(value, minimum=_MIN_CONTEXT)
                if context_window:
                    break
    return normalize_model_limit_entry(
        {
            "context_window": context_window,
            "max_tokens": max_tokens,
            "source": SOURCE_API,
        }
    )


def extract_model_limits_from_models_payload(data: Any) -> dict[str, dict[str, Any]]:
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
    limits: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("name") or "").strip()
        if not model_id:
            continue
        entry = extract_model_limits_from_record(row)
        if entry:
            limits[model_id] = entry
    return limits


def resolve_endpoint_model_limits(endpoint: Any, model: str | None = None) -> tuple[int, int]:
    """Return ``(context_window, max_tokens)`` for ``model`` on ``endpoint``.

    Missing per-model fields fall back to the endpoint defaults, then to the
    same numbers ``AIEndpoint`` / ``AIConfig`` use when a row is blank.
    """
    chosen = str(model if model is not None else getattr(endpoint, "model", "") or "").strip()
    stored = getattr(endpoint, "model_limits", None)
    if stored is None and isinstance(endpoint, dict):
        stored = endpoint.get("model_limits")
    entry = normalize_model_limits(stored).get(chosen, {}) if chosen else {}
    context_window = int(entry.get("context_window") or 0)
    max_tokens = int(entry.get("max_tokens") or 0)
    if context_window <= 0:
        context_window = int(getattr(endpoint, "context_window", 0) or 0)
        if context_window <= 0 and isinstance(endpoint, dict):
            context_window = int(endpoint.get("context_window") or 0)
    if max_tokens <= 0:
        max_tokens = int(getattr(endpoint, "max_tokens", 0) or 0)
        if max_tokens <= 0 and isinstance(endpoint, dict):
            max_tokens = int(endpoint.get("max_tokens") or 0)
    if context_window <= 0:
        context_window = 131072
    if max_tokens <= 0:
        max_tokens = 16384
    return context_window, max_tokens
