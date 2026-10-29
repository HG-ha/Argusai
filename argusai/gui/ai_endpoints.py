"""Settings-panel helpers that exercise an AI endpoint without going through assistant flow.

All three methods are blocking: we run the async work via a dedicated loop in a
worker thread so the caller (pywebview bridge) gets a synchronous return.

``list_ai_provider_presets`` is read-only and returns prefill metadata from
:mod:`argusai.core.ai_provider_catalog`; it never exposes API keys.
"""

from __future__ import annotations

from typing import Any


def _run_blocking(coro_factory: Any) -> Any:
    """Synchronously run a no-arg coroutine factory regardless of the host loop state.

    pywebview bridge methods are blocking; some hosts already have an
    asyncio loop on the calling thread (frozen builds, certain WebViews),
    so we can't just ``asyncio.run(...)`` unconditionally.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
        nested = True
    except RuntimeError:
        # Keep asyncio.run() outside this except suite so nested task errors
        # are not chained as "During handling of the above exception".
        nested = False
    if nested:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro_factory()).result()
    return asyncio.run(coro_factory())


class AIEndpointsMixin:
    """Direct probes against a single AI endpoint (test ping + list models)."""

    def list_ai_provider_presets(self) -> dict[str, Any]:
        """Return the read-only provider preset catalog for the settings UI.

        Grouped by category in display order. Each entry carries prefill
        metadata (name, region, protocol, base_url, auth scheme, console link,
        default/suggested models, context/max tokens, modality hints) but no
        API keys or promotional parameters.
        """
        from argusai.core.ai_provider_catalog import (
            CATEGORY_LABELS,
            CATEGORY_ORDER,
            CUSTOM_PROVIDER_ID,
            list_provider_presets,
        )

        presets = []
        for p in list_provider_presets():
            presets.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "category": p.category,
                    "region": p.region,
                    "protocol": p.protocol,
                    "base_url": p.base_url,
                    "auth_scheme": p.auth.scheme,
                    "console_url": p.console_url,
                    "website_url": p.website_url,
                    "api_key_url": p.api_key_url,
                    "models_supported": p.models_supported,
                    "model_list_strategy": p.model_list_strategy,
                    "default_model": p.default_model,
                    "suggested_models": list(p.suggested_models),
                    "context_window": p.context_window,
                    "max_tokens": p.max_tokens,
                    "modality_hints": dict(p.modality_hints),
                    "aliases": list(p.aliases),
                    "featured": p.featured,
                    "requires_base_url_edit": p.requires_base_url_edit,
                    "note": p.note,
                }
            )
        return {
            "ok": True,
            "categories": [{"id": c, "label": CATEGORY_LABELS.get(c, c)} for c in CATEGORY_ORDER],
            "custom_id": CUSTOM_PROVIDER_ID,
            "presets": presets,
        }

    def test_ai_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        """Send a lightweight test prompt to a single endpoint."""
        from argusai.core.ai import AIEndpoint, create_provider

        ep = AIEndpoint.from_dict(endpoint)
        provider = create_provider(override=ep.to_ai_config())
        if not provider:
            return {"ok": False, "error": "Invalid endpoint configuration"}

        async def _test() -> dict[str, Any]:
            try:
                result = await provider.analyze("Reply with exactly the word: pong", "ping")
                return {"ok": True, "response": result[:200]}
            except Exception as e:
                return {"ok": False, "error": str(e)[:300]}
            finally:
                await provider.close()

        return _run_blocking(_test)

    def list_ai_models(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        """Fetch available models from an endpoint's ``/models`` API.

        Auth is chosen from the endpoint's provider preset when known
        (``x-api-key`` for Anthropic, ``Bearer`` otherwise). The probe runs
        even for presets the catalogue does not mark ``api``: most of them are
        OpenAI-compatible relays that do serve the path, and the ones that do
        not simply fall back to the preset's suggested models, so a 404/403 is
        never mistaken for an unusable endpoint.
        """
        import httpx

        from argusai.core.ai import (
            AIEndpoint,
            default_ai_client_headers,
            merge_ai_headers,
        )
        from argusai.core.ai_model_capabilities import (
            SOURCE_LEARNED,
            extract_model_capabilities_from_models_payload,
            merge_model_capabilities,
            probe_model_modalities,
        )
        from argusai.core.ai_model_limits import extract_model_limits_from_models_payload
        from argusai.core.ai_provider_catalog import get_provider_preset

        ep = AIEndpoint.from_dict(endpoint)
        base_url = ep.base_url.rstrip("/")
        api_key = ep.api_key or ""
        timeout = ep.timeout or 30.0
        extra_headers = ep.extra_headers or {}

        preset = get_provider_preset(ep.provider_id)
        suggested = list(preset.suggested_models) if preset is not None else []

        def _curated(**extra: Any) -> dict[str, Any]:
            """Offer the vetted list when the endpoint will not enumerate itself.

            ``manual`` tells the UI to keep hand-entry in front of the user; it
            is not a verdict on the endpoint, which may well be answering chat
            requests fine without publishing a catalogue.
            """
            return {"ok": True, "models": suggested, "manual": True, "source": "preset", **extra}

        if not base_url:
            return _curated()
        if preset is not None and preset.model_list_strategy == "manual":
            # A local-deploy template serves whatever the operator loaded, so
            # there is nothing worth probing until they edit the base URL.
            return _curated()

        def _auth_headers() -> dict[str, str]:
            headers: dict[str, str] = {}
            if api_key:
                if preset is not None and preset.auth.scheme == "x-api-key":
                    headers[preset.auth.header] = api_key
                else:
                    headers["Authorization"] = f"Bearer {api_key}"
            if str(ep.protocol or "") == "anthropic":
                # Anthropic rejects every request that omits this, /models included.
                headers.setdefault("anthropic-version", "2023-06-01")
            extra = (
                {str(k): str(v) for k, v in extra_headers.items()}
                if isinstance(extra_headers, dict)
                else {}
            )
            return merge_ai_headers(headers, default_ai_client_headers(), extra)

        async def _probe_current(client: Any, caps: dict[str, Any]) -> dict[str, Any]:
            model = str(ep.model or "").strip()
            if not model:
                return caps
            learned = await probe_model_modalities(
                client,
                base_url=base_url,
                headers=_auth_headers(),
                model=model,
                protocol=str(ep.protocol or "openai"),
            )
            if not learned:
                return caps
            return merge_model_capabilities(
                caps,
                {model: {**learned, "source": SOURCE_LEARNED}},
                incoming_source=SOURCE_LEARNED,
            )

        async def _fetch() -> dict[str, Any]:
            from argusai.core.infra.httpx_client import direct_async_client

            async with direct_async_client(timeout=timeout, verify=False) as client:
                resp = await client.get(f"{base_url}/models", headers=_auth_headers())
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("data") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    rows = []
                models = [
                    str(m.get("id") or "")
                    for m in rows
                    if isinstance(m, dict) and m.get("id")
                ]
                caps = extract_model_capabilities_from_models_payload(data)
                caps = await _probe_current(client, caps)
                payload = {
                    "ok": True,
                    "models": sorted(models),
                    "model_limits": extract_model_limits_from_models_payload(data),
                    "model_capabilities": caps,
                    "source": "api",
                }
                return payload if payload["models"] else {**_curated(source="empty"), "model_capabilities": caps}

        async def _curated_with_probe(**extra: Any) -> dict[str, Any]:
            from argusai.core.infra.httpx_client import direct_async_client

            payload = _curated(**extra)
            try:
                async with direct_async_client(timeout=min(float(timeout or 30), 20.0), verify=False) as client:
                    payload["model_capabilities"] = await _probe_current(client, {})
            except Exception:
                payload.setdefault("model_capabilities", {})
            return payload

        try:
            fetched = _run_blocking(_fetch)
        except httpx.HTTPStatusError as e:
            # No catalogue is not a broken endpoint: hand back the vetted list
            # rather than an error the user cannot act on.
            return _run_blocking(lambda: _curated_with_probe(source="error", status=e.response.status_code))
        except Exception as e:
            if suggested:
                return _run_blocking(lambda: _curated_with_probe(source="error", error=str(e)[:300]))
            return {"ok": False, "error": str(e)[:300]}
        return fetched

    def list_ai_endpoints(self) -> dict[str, Any]:
        """Return configured chat AI endpoints (API keys redacted)."""
        settings = self.settings_mgr.get()
        endpoints = list(getattr(settings, "ai_endpoints", None) or [])
        ranked = sorted(
            enumerate(endpoints),
            key=lambda item: int(getattr(item[1], "priority", 0) or 0),
            reverse=True,
        )
        active_index = ranked[0][0] if ranked and getattr(ranked[0][1], "enabled", True) else None
        # Prefer the highest-priority *enabled* endpoint as active.
        for index, ep in ranked:
            if getattr(ep, "enabled", True):
                active_index = index
                break
        rows = [
            _public_endpoint_row(ep, index=index, active=(index == active_index))
            for index, ep in enumerate(endpoints)
        ]
        return {
            "ok": True,
            "ai_enabled": bool(getattr(settings, "ai_enabled", True)),
            "endpoints": rows,
        }

    def save_ai_endpoint(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add or update one chat AI endpoint in ``settings.json``.

        Match existing rows by ``index`` (preferred) or case-insensitive ``name``.
        Partial updates merge onto the previous row; omitted ``api_key`` keeps
        the stored secret. ``provider_id`` / ``--provider`` prefills base URL,
        protocol, and default model when those fields are blank.
        """
        from argusai.core.ai import AIEndpoint

        data = dict(payload or {})
        result: dict[str, Any] = {}

        def _update(settings):
            endpoints = list(settings.ai_endpoints or [])
            index = _resolve_endpoint_index(endpoints, data)
            previous = endpoints[index].to_dict() if index is not None else {}
            merged = _merge_endpoint_payload(previous, data)
            if not str(merged.get("base_url") or "").strip():
                raise ValueError("base_url is required (or pass a known --provider)")
            if not str(merged.get("model") or "").strip():
                raise ValueError("model is required")
            # Allow enable/disable without forcing a key, but new rows need one.
            if index is None and not str(merged.get("api_key") or "").strip():
                raise ValueError("api_key is required for new endpoints")
            endpoint = AIEndpoint.from_dict(merged)
            if index is None:
                endpoints.append(endpoint)
                index = len(endpoints) - 1
            else:
                endpoints[index] = endpoint
            settings.ai_endpoints = endpoints
            if "ai_enabled" in data:
                settings.ai_enabled = bool(data.get("ai_enabled"))
            result["index"] = index
            result["endpoint"] = endpoint
            return settings

        try:
            self.settings_mgr.update(_update)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        endpoint = result["endpoint"]
        index = int(result["index"])
        return {
            "ok": True,
            "index": index,
            "endpoint": _public_endpoint_row(endpoint, index=index, active=False),
        }

    def delete_ai_endpoint(self, selector: str | int) -> dict[str, Any]:
        """Delete one endpoint by 0-based index or display name."""
        result: dict[str, Any] = {}

        def _update(settings):
            endpoints = list(settings.ai_endpoints or [])
            index = _resolve_endpoint_index(endpoints, {"index": selector, "name": selector})
            if index is None:
                raise ValueError(f"endpoint not found: {selector!r}")
            removed = endpoints.pop(index)
            settings.ai_endpoints = endpoints
            result["index"] = index
            result["name"] = str(getattr(removed, "name", "") or "")
            return settings

        try:
            self.settings_mgr.update(_update)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "index": result["index"], "name": result["name"]}

    def activate_ai_endpoint(self, selector: str | int, model: str = "") -> dict[str, Any]:
        """Make an endpoint the failover head (raise priority) and optionally set model."""
        chosen_model = str(model or "").strip()
        result: dict[str, Any] = {}

        def _update(settings):
            endpoints = list(settings.ai_endpoints or [])
            index = _resolve_endpoint_index(endpoints, {"index": selector, "name": selector})
            if index is None:
                raise ValueError(f"endpoint not found: {selector!r}")
            chosen = endpoints[index]
            if chosen_model:
                catalog = chosen.model_catalog() if hasattr(chosen, "model_catalog") else []
                if catalog and chosen_model not in catalog:
                    # Allow setting a new active model; keep it in the catalog.
                    models = list(getattr(chosen, "models", None) or [])
                    if chosen_model not in models and chosen_model != chosen.model:
                        models.append(chosen_model)
                        chosen.models = models
                chosen.model = chosen_model
            top = max((int(getattr(item, "priority", 0) or 0) for item in endpoints), default=0)
            chosen.priority = top + 10
            chosen.enabled = True
            settings.ai_endpoints = endpoints
            result["index"] = index
            result["endpoint"] = chosen
            return settings

        try:
            self.settings_mgr.update(_update)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        endpoint = result["endpoint"]
        index = int(result["index"])
        return {
            "ok": True,
            "index": index,
            "endpoint": _public_endpoint_row(endpoint, index=index, active=True),
        }


def _redact_api_key(api_key: str) -> str:
    key = str(api_key or "")
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return f"{'*' * 8}{key[-4:]}"


def _public_endpoint_row(endpoint: Any, *, index: int, active: bool) -> dict[str, Any]:
    raw = endpoint.to_dict() if hasattr(endpoint, "to_dict") else dict(endpoint or {})
    key = str(raw.get("api_key") or "")
    return {
        "index": index,
        "active": bool(active),
        "enabled": bool(raw.get("enabled", True)),
        "name": str(raw.get("name") or ""),
        "provider_id": str(raw.get("provider_id") or ""),
        "base_url": str(raw.get("base_url") or ""),
        "model": str(raw.get("model") or ""),
        "models": list(raw.get("models") or []),
        "protocol": str(raw.get("protocol") or "openai"),
        "priority": int(raw.get("priority") or 0),
        "max_tokens": int(raw.get("max_tokens") or 0),
        "context_window": int(raw.get("context_window") or 0),
        "model_limits": dict(raw.get("model_limits") or {})
        if isinstance(raw.get("model_limits"), dict)
        else {},
        "model_capabilities": dict(raw.get("model_capabilities") or {})
        if isinstance(raw.get("model_capabilities"), dict)
        else {},
        "api_key_set": bool(key.strip()),
        "api_key": _redact_api_key(key),
        "image_in": bool(raw.get("image_in")),
        "video_in": bool(raw.get("video_in")),
        "audio_in": bool(raw.get("audio_in")),
        "extra_body": dict(raw.get("extra_body") or {}) if isinstance(raw.get("extra_body"), dict) else {},
    }


def _resolve_endpoint_index(endpoints: list[Any], data: dict[str, Any]) -> int | None:
    if "index" in data and data.get("index") is not None and str(data.get("index")) != "":
        raw = data.get("index")
        try:
            index = int(raw)
        except (TypeError, ValueError):
            index = None
        else:
            if 0 <= index < len(endpoints):
                return index
            raise ValueError(f"endpoint index out of range: {index}")
    name = str(data.get("name") or "").strip()
    if not name:
        # Allow ``selector`` style payloads from delete/activate.
        name = str(data.get("selector") or "").strip()
    if not name:
        return None
    if name.isdigit():
        index = int(name)
        if 0 <= index < len(endpoints):
            return index
        raise ValueError(f"endpoint index out of range: {index}")
    folded = name.casefold()
    for i, ep in enumerate(endpoints):
        if str(getattr(ep, "name", "") or "").casefold() == folded:
            return i
    return None


def _merge_endpoint_payload(previous: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    from argusai.core.ai_provider_catalog import (
        CUSTOM_PROVIDER_ID,
        get_provider_preset,
        resolve_provider_id_for,
    )

    merged = dict(previous or {})
    # Don't let a blank api_key from partial updates wipe the stored secret.
    incoming_key = data.get("api_key")
    for key, value in data.items():
        if key in {"index", "ai_enabled", "selector"}:
            continue
        if key == "api_key" and (value is None or str(value) == ""):
            continue
        merged[key] = value

    provider_token = str(data.get("provider_id") or data.get("provider") or "").strip()
    preset = get_provider_preset(provider_token) if provider_token else None
    if preset is None and provider_token:
        preset = _find_provider_preset(provider_token)
    if preset is not None:
        merged["provider_id"] = preset.id
        if not str(data.get("base_url") or "").strip():
            if not str(merged.get("base_url") or "").strip() or not previous:
                merged["base_url"] = preset.base_url
        if not str(data.get("protocol") or "").strip():
            if not previous or not str(previous.get("protocol") or "").strip():
                merged["protocol"] = preset.protocol
        if not str(data.get("model") or "").strip() and not str(merged.get("model") or "").strip():
            merged["model"] = preset.default_model
        if "max_tokens" not in data and (not previous or not previous.get("max_tokens")):
            merged["max_tokens"] = preset.max_tokens
        if "context_window" not in data and (not previous or not previous.get("context_window")):
            merged["context_window"] = preset.context_window
        if "image_in" not in data and not previous:
            merged["image_in"] = bool(preset.modality_hints.get("image_in"))
        if "video_in" not in data and not previous:
            merged["video_in"] = bool(preset.modality_hints.get("video_in"))
        if "audio_in" not in data and not previous:
            merged["audio_in"] = bool(preset.modality_hints.get("audio_in"))
        if not str(merged.get("name") or "").strip():
            merged["name"] = preset.name
    else:
        merged["provider_id"] = resolve_provider_id_for(merged) or CUSTOM_PROVIDER_ID

    if incoming_key is not None and str(incoming_key) != "":
        merged["api_key"] = str(incoming_key)
    if not str(merged.get("name") or "").strip():
        merged["name"] = str(merged.get("model") or merged.get("provider_id") or "endpoint")
    return merged


def _find_provider_preset(token: str):
    from argusai.core.ai_provider_catalog import get_provider_preset, list_provider_presets

    key = str(token or "").strip()
    if not key:
        return None
    direct = get_provider_preset(key)
    if direct is not None:
        return direct
    folded = key.casefold()
    for preset in list_provider_presets():
        if preset.id.casefold() == folded or preset.name.casefold() == folded:
            return preset
        if any(str(alias).casefold() == folded for alias in (preset.aliases or ())):
            return preset
    return None
