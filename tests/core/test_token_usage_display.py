from argusai.core.token_usage_display import (
    context_usage_parts,
    context_usage_status_params,
    token_usage_status_rows,
)


def test_context_usage_parts_from_prompt_fields() -> None:
    parts = context_usage_parts(
        {
            "prompt_tokens": 123_400,
            "prompt_bytes": 493_600,
            "context_window": 256_000,
        }
    )
    assert parts is not None
    assert parts["used"] == 123_400
    assert parts["window"] == 256_000
    assert round(parts["percent"]) == 48


def test_context_usage_parts_missing_window() -> None:
    assert context_usage_parts({"prompt_tokens": 100}) is None


def test_token_usage_status_rows_prepend_context() -> None:
    rows = token_usage_status_rows(
        {
            "prompt_tokens": 123_400,
            "context_window": 256_000,
            "token_usage": {
                "input_tokens": 120_000,
                "output_tokens": 4_200,
                "cache_hit_tokens": 90_000,
                "cache_miss_tokens": 30_000,
                "calls": 8,
            },
            "cache_hit_rate": 75.0,
        },
        lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
    )
    assert rows[0].startswith("status_row_context:")
    assert "48%" in rows[0]
    assert any(row.startswith("status_row_tokens:") for row in rows)


def test_context_usage_status_params_format() -> None:
    params = context_usage_status_params(
        {"last_prompt_tokens_est": 1_500, "context_window": 128_000}
    )
    assert params == {"percent": "1%", "used": "1.5k", "window": "128.0k"}
