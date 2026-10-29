"""HookManager — bridges Python ``ReverseJobStore`` state with the
in-page hook runtime running in a Playwright-controlled browser.

Responsibilities:

* Maintain a per-job *plan* (list of currently-active hook specs).
* Push that plan to the browser context's ``add_init_script`` so any
  freshly loaded page already has the hooks. Existing pages are
  patched via direct ``page.evaluate`` so the user doesn't have to
  reload.
* Drain hits from every page and forward them to
  ``ReverseJobStore.append_hook_hits``.
* Stay sane across browser restarts, attach/detach cycles, and page
  closures. A hook is conceptually a *definition* persisted in the
  job; the runtime state is recoverable.

This module is browser-pool aware but stays loosely coupled — it
takes a hub at install/drain time rather than holding one as state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from argusai.core.reverse.hook_runtime import HOOK_RUNTIME_JS, runtime_bootstrap_script
from argusai.core.reverse.reverse_jobs import ReverseJobStore

log = logging.getLogger("argusai.hook_manager")


def _spec_from_hook(record: dict[str, Any]) -> dict[str, Any]:
    """Build the in-page install spec from a ReverseJobStore hook record."""
    spec: dict[str, Any] = {
        "id": record["id"],
        "kind": record.get("kind") or "function",
        "target": record.get("target") or "",
        "captureStack": bool(record.get("capture_stack", True)),
    }
    if record.get("filter_src"):
        spec["filterSrc"] = record["filter_src"]
    if record.get("replace_return_src"):
        spec["replaceReturnSrc"] = record["replace_return_src"]
    if record.get("replace_get_src"):
        spec["replaceGetSrc"] = record["replace_get_src"]
    if record.get("side_effect_src"):
        spec["sideEffectSrc"] = record["side_effect_src"]
    if record.get("tag"):
        spec["tag"] = record["tag"]
    return spec


class HookManager:
    """Coordinates hook installation against a live browser hub.

    The manager itself is stateless beyond per-job plan caches. The
    authoritative state lives in:

    * the ``ReverseJobStore`` (definitions)
    * the browser pages (live ring buffers)
    """

    def __init__(self, store: ReverseJobStore) -> None:
        self.store = store
        # job_id → list[spec]; rebuilt on demand from the store.
        self._plans: dict[str, list[dict[str, Any]]] = {}
        # job_id → init-script-handle (Playwright returns a handle in newer versions).
        self._init_handles: dict[str, Any] = {}
        # global lock to serialise installs/drains against the same job.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, job_id: str) -> asyncio.Lock:
        lk = self._locks.get(job_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[job_id] = lk
        return lk

    def _build_plan(self, job_id: str) -> list[dict[str, Any]]:
        job = self.store.get(job_id)
        if job is None:
            return []
        return [_spec_from_hook(h) for h in (job.get("hooks") or [])]

    # ---- Browser-side patch helpers (called inside hub loop) ----

    async def _refresh_init_script(self, context: Any, job_id: str) -> None:
        """(Re)register the add_init_script for this context.

        Playwright doesn't support removing an init script directly, so
        each refresh just adds the new bundle on top. Old hooks become
        no-ops because the runtime is idempotent on hook id.
        """
        plan = self._plans.get(job_id) or []
        script = runtime_bootstrap_script(plan)
        try:
            await context.add_init_script(script)
        except Exception as e:
            log.warning("add_init_script failed for job %s: %s", job_id, e)

    async def _ensure_runtime_on_page(self, page: Any) -> bool:
        """Ensure ``__argusai_hook_bridge`` exists on the live page.

        Re-evaluates the runtime IIFE; it's idempotent.
        """
        try:
            await page.evaluate(HOOK_RUNTIME_JS)
            return True
        except Exception as e:
            log.debug("Inject runtime to live page failed: %s", e)
            return False

    async def _install_on_live_page(self, page: Any, spec: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_runtime_on_page(page)
        encoded = json.dumps(spec, ensure_ascii=False)
        try:
            return await page.evaluate(
                f"(function(){{return window.__argusai_hook_bridge.install({encoded});}})()"
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _uninstall_on_live_page(self, page: Any, hook_id: str) -> dict[str, Any]:
        try:
            return await page.evaluate(
                "(function(id){return window.__argusai_hook_bridge && window.__argusai_hook_bridge.uninstall(id);})("
                + json.dumps(hook_id, ensure_ascii=False) + ")"
            )
        except Exception:
            return {"ok": True}

    async def _drain_page(self, page: Any, hook_id: str | None = None) -> dict[str, Any]:
        """Pull buffered hits for one hook (or all) from one page.

        Returns a normalised ``{ok, hits, dropped}`` dict. If the page
        doesn't have the runtime (no hooks ever installed), returns an
        empty hits list rather than failing.
        """
        if hook_id:
            expr = (
                "(function(id){return window.__argusai_hook_bridge && window.__argusai_hook_bridge.drain(id, 0);})("
                + json.dumps(hook_id, ensure_ascii=False) + ")"
            )
        else:
            expr = "(function(){return window.__argusai_hook_bridge && window.__argusai_hook_bridge.drainAll();})()"
        try:
            res = await page.evaluate(expr)
            if not isinstance(res, dict):
                return {"ok": True, "hits": [], "dropped": 0}
            return res
        except Exception as e:
            return {"ok": False, "error": str(e), "hits": [], "dropped": 0}

    # ---- Public async API (called from probes / tools) ----

    async def install_hook(
        self,
        hub: Any,
        job_id: str,
        *,
        target: str,
        kind: str = "function",
        replace_return_src: str = "",
        replace_get_src: str = "",
        filter_src: str = "",
        side_effect_src: str = "",
        tag: str = "",
        recipe: str = "",
        capture_stack: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        """Persist a hook definition and inject it into the live browser."""
        # 1. Persist definition. The store assigns the id.
        record = self.store.add_hook(
            job_id,
            target=target,
            kind=kind,
            expression=replace_return_src or replace_get_src or "",
            recipe=recipe,
            persisted=True,
            note=note,
        )
        # Stash optional behaviour fields on the record. The store ignores
        # unknown fields on update; we save them back via update_hook below.
        if replace_return_src or replace_get_src or filter_src or side_effect_src or tag is not None:
            self._enrich_hook(job_id, record["id"], {
                "replace_return_src": replace_return_src,
                "replace_get_src": replace_get_src,
                "filter_src": filter_src,
                "side_effect_src": side_effect_src,
                "tag": tag,
                "capture_stack": bool(capture_stack),
            })

        # 2. Rebuild plan and refresh init-script + live pages.
        async with self._lock_for(job_id):
            self._plans[job_id] = self._build_plan(job_id)
            spec = _spec_from_hook({**record, "capture_stack": capture_stack,
                                     "replace_return_src": replace_return_src,
                                     "replace_get_src": replace_get_src,
                                     "filter_src": filter_src,
                                     "side_effect_src": side_effect_src,
                                     "tag": tag})
            live_status = await self._apply_to_browser(hub, job_id, install_spec=spec)
        return {"ok": True, "hook": record, "live": live_status}

    def _enrich_hook(self, job_id: str, hook_id: str, fields: dict[str, Any]) -> None:
        """Persist extended fields (replace_*_src, filter_src, tag, capture_stack)
        directly into the job.json. Bypasses the store's typed setters because
        these are hook-runtime details not yet exposed on the public API.
        """
        try:
            job = self.store._load_or_raise(job_id)  # noqa: SLF001
            job_dir, data = job
            for h in data.get("hooks") or []:
                if h.get("id") == hook_id:
                    for k, v in fields.items():
                        if v != "":
                            h[k] = v
                    break
            self.store._write_job(job_dir, data)  # noqa: SLF001
        except Exception as e:
            log.warning("hook enrich failed: %s", e)

    async def remove_hook(self, hub: Any, job_id: str, hook_id: str) -> dict[str, Any]:
        ok = self.store.remove_hook(job_id, hook_id)
        async with self._lock_for(job_id):
            self._plans[job_id] = self._build_plan(job_id)
            live = await self._apply_to_browser(hub, job_id, uninstall_id=hook_id)
        return {"ok": ok, "live": live}

    async def list_hooks(self, hub: Any, job_id: str) -> dict[str, Any]:
        """Return persisted hooks plus live-status from each open page."""
        job = self.store.get(job_id)
        if job is None:
            return {"ok": False, "error": "job not found", "hooks": []}
        live_by_id: dict[str, dict[str, Any]] = {}
        if hub is not None:
            try:
                async def _scan(page: Any) -> dict[str, Any]:
                    try:
                        res = await page.evaluate(
                            "(function(){return window.__argusai_hook_bridge && window.__argusai_hook_bridge.list();})()"
                        )
                        return res or {"hooks": []}
                    except Exception:
                        return {"hooks": []}
                # Walk every page. Some pages may not have the runtime.
                for entry in await self._walk_pages(hub, _scan):
                    for live in entry.get("hooks", []) or []:
                        live_by_id.setdefault(live["id"], live)
            except Exception as e:
                log.debug("live list_hooks failed: %s", e)
        out = []
        for h in job.get("hooks") or []:
            merged = dict(h)
            live = live_by_id.get(h["id"])
            if live:
                merged["live"] = live
            else:
                merged["live"] = None
            out.append(merged)
        return {"ok": True, "hooks": out}

    async def drain(self, hub: Any, job_id: str, hook_id: str = "") -> dict[str, Any]:
        """Drain hits from every live page and append to the job log."""
        if hub is None:
            return {"ok": True, "drained": 0, "dropped": 0}
        total_added = 0
        total_dropped = 0
        per_hook: dict[str, dict[str, Any]] = {}
        async def _do(page: Any) -> dict[str, Any]:
            if hook_id:
                return await self._drain_page(page, hook_id)
            return await self._drain_page(page, None)
        try:
            results = await self._walk_pages(hub, _do)
        except Exception as e:
            return {"ok": False, "error": str(e), "drained": 0, "dropped": 0}
        # Merge results across pages.
        for r in results:
            if not isinstance(r, dict):
                continue
            if "batches" in r:
                for hid, batch in (r.get("batches") or {}).items():
                    rec = per_hook.setdefault(hid, {"hits": [], "dropped": 0})
                    for hit in (batch.get("hits") or []):
                        rec["hits"].append(hit)
                    rec["dropped"] += int(batch.get("dropped") or 0)
            else:
                # Single-hook drain.
                hid = hook_id
                if hid:
                    rec = per_hook.setdefault(hid, {"hits": [], "dropped": 0})
                    for hit in (r.get("hits") or []):
                        rec["hits"].append(hit)
                    rec["dropped"] += int(r.get("dropped") or 0)
        # Persist into the job.
        for hid, batch in per_hook.items():
            try:
                self.store.append_hook_hits(
                    job_id, hid,
                    [self._normalise_hit(h) for h in batch["hits"]],
                    dropped=batch["dropped"],
                )
                total_added += len(batch["hits"])
                total_dropped += batch["dropped"]
            except FileNotFoundError:
                # Hook was removed mid-drain; ignore.
                continue
            except Exception as e:
                log.warning("append_hook_hits failed for %s: %s", hid, e)
        return {"ok": True, "drained": total_added, "dropped": total_dropped, "per_hook": {hid: {"added": len(p["hits"]), "dropped": p["dropped"]} for hid, p in per_hook.items()}}

    def _normalise_hit(self, hit: dict[str, Any]) -> dict[str, Any]:
        """Convert in-page timestamps (epoch ms) to ISO and drop noise."""
        out = dict(hit)
        ts = hit.get("t")
        if isinstance(ts, (int, float)):
            try:
                out["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts / 1000.0))
            except (OSError, ValueError):
                pass
            out["epoch_ms"] = int(ts)
        out.pop("t", None)
        return out

    # ---- Internal: browser walking ----

    async def _walk_pages(self, hub: Any, async_fn: Any) -> list[Any]:
        """Run ``async_fn(page)`` on every open page in the hub. Returns the
        list of per-page results."""
        # Use the hub's own machinery: a special helper that gives us all
        # pages. If unavailable, fall back to a single fresh page.
        try:
            if hasattr(hub, "acall_with_each_page"):
                return await hub.acall_with_each_page(async_fn)
        except Exception as e:
            log.debug("acall_with_each_page failed: %s", e)
        try:
            return [await hub.acall_with_page(async_fn)]
        except Exception as e:
            log.debug("acall_with_page fallback failed: %s", e)
            return []

    async def _apply_to_browser(
        self,
        hub: Any,
        job_id: str,
        *,
        install_spec: dict[str, Any] | None = None,
        uninstall_id: str = "",
    ) -> dict[str, Any]:
        """Sync the plan to the browser:

        1. Refresh ``add_init_script`` so new pages are auto-hooked.
        2. Apply the same change to every currently-open page.
        """
        if hub is None:
            return {"ok": False, "error": "no browser hub attached"}

        # Step 1: refresh init script via hub's context.
        try:
            async def _refresh(ctx_holder_page: Any) -> dict[str, Any]:
                ctx = ctx_holder_page.context
                await self._refresh_init_script(ctx, job_id)
                return {"ok": True}
            await hub.acall_with_page(_refresh)
        except Exception as e:
            log.warning("refresh init script failed: %s", e)

        # Step 2: apply install/uninstall on every live page.
        async def _apply(page: Any) -> dict[str, Any]:
            if install_spec:
                return await self._install_on_live_page(page, install_spec)
            if uninstall_id:
                return await self._uninstall_on_live_page(page, uninstall_id)
            return {"ok": True}

        try:
            results = await self._walk_pages(hub, _apply)
            return {"ok": True, "page_results": results}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def reattach_all(self, hub: Any, job_id: str) -> dict[str, Any]:
        """Re-inject every hook for a job. Called after a fresh browser
        attach so a user-facing "re-attach" button works.
        """
        async with self._lock_for(job_id):
            self._plans[job_id] = self._build_plan(job_id)
        # Apply each hook in the plan to live pages.
        for spec in self._plans[job_id]:
            try:
                await self._apply_to_browser(hub, job_id, install_spec=spec)
            except Exception as e:
                log.warning("reattach failed for %s: %s", spec.get("id"), e)
        return {"ok": True, "reattached": len(self._plans[job_id])}
