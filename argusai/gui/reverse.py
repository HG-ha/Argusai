"""Bridge methods for reverse-engineering jobs.

Five resource families live here, each following the same
``add → list/read → remove`` shape but persisted by
:class:`argusai.core.reverse.reverse_jobs.ReverseJobStore`:

* **Job** itself (status, scope, stack, intent, linked task).
* **Artifact** (a JS bundle, decompiled WASM, deobfuscated snippet, etc.).
* **Finding** (analyst note attached to a job).
* **Hook** (browser hook spec, optionally backed by a recipe).
* **Reproduce** (Python/JS script reproducing a finding).

Plus filesystem-bridging helpers that let the user (or the AI) jump
from a job/artifact straight into the OS file manager, copy a single
artifact out, or export the whole job to a zip.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


class ReverseMixin:
    """Bridge methods for the reverse-engineering subsystem."""

    # ---- Jobs ----------------------------------------------------------

    def reverse_job_list(
        self,
        scope: str = "",
        status: str = "",
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        try:
            jobs = self.reverse_jobs.list(
                scope=str(scope or ""),
                status=str(status or ""),
                include_trashed=bool(include_trashed),
            )
            return {"ok": True, "jobs": jobs, "root": str(self.reverse_jobs.root)}
        except Exception as e:
            return {"ok": False, "error": str(e), "jobs": []}

    def reverse_job_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            job = self.reverse_jobs.create(
                title=str(data.get("title") or "").strip(),
                target_url=str(data.get("target_url") or "").strip(),
                scope=str(data.get("scope") or "").strip(),
                tags=list(data.get("tags") or []),
                stack=dict(data.get("stack") or {}),
                intent=str(data.get("intent") or "").strip(),
                linked_task_id=str(data.get("linked_task_id") or "").strip(),
                target_type=str(data.get("target_type") or "").strip(),
                platform=str(data.get("platform") or "").strip(),
                target_meta=dict(data.get("target_meta") or {}),
            )
            return {"ok": True, "job": job}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_get(self, job_id: str, include_trashed: bool = False) -> dict[str, Any]:
        try:
            job = self.reverse_jobs.get(str(job_id or ""), include_trashed=bool(include_trashed))
            if job is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "job": job}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_update(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            update_kwargs: dict[str, Any] = {}
            for key in ("title", "scope", "status", "intent", "linked_task_id", "target_type", "platform"):
                if key in data:
                    update_kwargs[key] = data[key]
            if "tags" in data:
                update_kwargs["tags"] = list(data["tags"] or [])
            if "stack" in data:
                update_kwargs["stack"] = dict(data["stack"] or {})
            if "linked_kb_ids" in data:
                update_kwargs["linked_kb_ids"] = list(data["linked_kb_ids"] or [])
            if "target_meta" in data:
                update_kwargs["target_meta"] = dict(data["target_meta"] or {})
            if "toolchain_status" in data:
                update_kwargs["toolchain_status"] = dict(data["toolchain_status"] or {})
            if "analysis_summary" in data:
                update_kwargs["analysis_summary"] = dict(data["analysis_summary"] or {})
            if "risk_summary" in data:
                update_kwargs["risk_summary"] = dict(data["risk_summary"] or {})
            job = self.reverse_jobs.update(str(job_id or ""), **update_kwargs)
            return {"ok": True, "job": job}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_trash(self, job_id: str) -> dict[str, Any]:
        try:
            jid = str(job_id or "").strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            if not self.reverse_jobs.trash(jid):
                return {"ok": False, "error": f"job not found: {jid}"}
            return {"ok": True, "id": jid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_restore(self, job_id: str) -> dict[str, Any]:
        try:
            jid = str(job_id or "").strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            if not self.reverse_jobs.restore(jid):
                return {"ok": False, "error": f"job not found in trash: {jid}"}
            return {"ok": True, "id": jid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_hard_delete(self, job_id: str) -> dict[str, Any]:
        try:
            jid = str(job_id or "").strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            if not self.reverse_jobs.hard_delete(jid):
                return {"ok": False, "error": f"job not found: {jid}"}
            return {"ok": True, "id": jid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_job_events(self, job_id: str, tail: int = 200) -> dict[str, Any]:
        try:
            events = self.reverse_jobs.list_events(str(job_id or ""), tail=int(tail or 200))
            return {"ok": True, "events": events}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e), "events": []}
        except Exception as e:
            return {"ok": False, "error": str(e), "events": []}

    def reverse_job_size(self, job_id: str) -> dict[str, Any]:
        try:
            size = self.reverse_jobs.job_size_bytes(str(job_id or ""))
            return {"ok": True, "size_bytes": size}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_toolchain_scan(
        self,
        job_id: str = "",
        tools: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            from argusai.core.reverse.toolchain import clear_toolchain_cache, scan_toolchain

            clear_toolchain_cache()
            scan = scan_toolchain(names=list(tools) if tools else None)
            if job_id:
                status = self.reverse_jobs.record_toolchain_scan(str(job_id or ""), scan)
                scan["job_id"] = job_id
                scan["toolchain_status"] = status
            return scan
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def frida_session_list(self) -> dict[str, Any]:
        try:
            from argusai.core.reverse.frida_runtime import get_frida_manager

            sessions = get_frida_manager().list_sessions()
            return {"ok": True, "count": len(sessions), "sessions": sessions}
        except Exception as e:
            return {"ok": False, "error": str(e), "sessions": []}

    def frida_session_stop(self, session_id: str = "", job_id: str = "") -> dict[str, Any]:
        try:
            from argusai.core.reverse.frida_runtime import get_frida_manager

            result = get_frida_manager().stop(str(session_id or ""))
            if job_id and result.get("ok"):
                job = self.reverse_jobs.get(str(job_id))
                if job:
                    for sess in job.get("runtime_sessions") or []:
                        if isinstance(sess, dict) and (sess.get("meta") or {}).get("session_id") == session_id:
                            self.reverse_jobs.stop_runtime_session(str(job_id), str(sess.get("id") or ""), state="stopped")
                            break
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Artifacts -----------------------------------------------------

    def reverse_artifact_add(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            artifact = self.reverse_jobs.add_artifact(
                str(job_id or ""),
                content=data.get("content") or "",
                kind=str(data.get("kind") or "other"),
                filename=str(data.get("filename") or ""),
                url=str(data.get("url") or ""),
                from_artifact=str(data.get("from") or data.get("from_artifact") or ""),
                tool=str(data.get("tool") or ""),
                note=str(data.get("note") or ""),
            )
            return {"ok": True, "artifact": artifact}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_artifact_read(
        self, job_id: str, artifact_id: str, max_bytes: int = 2_000_000
    ) -> dict[str, Any]:
        try:
            data = self.reverse_jobs.read_artifact(
                str(job_id or ""),
                str(artifact_id or ""),
                max_bytes=int(max_bytes or 2_000_000),
            )
            if data is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "artifact": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_artifact_remove(self, job_id: str, artifact_id: str) -> dict[str, Any]:
        try:
            return {"ok": bool(self.reverse_jobs.remove_artifact(str(job_id or ""), str(artifact_id or "")))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Filesystem bridging ------------------------------------------
    #
    # Three flavours of "let me get at the files":
    #
    # 1. ``reverse_job_reveal_dir`` — open the whole job folder in the
    #    OS file manager. The detail-header button in the reverse panel
    #    calls this, and the AI also exposes it as a tool so the user
    #    can ask "show me the folder".
    # 2. ``reverse_artifact_reveal`` — same idea but selects a single
    #    artifact file. Used by the per-row icon in the Artifacts tab.
    # 3. ``reverse_artifact_copy_to`` — copy a single artifact out of
    #    the job into either an absolute path or the current assistant
    #    session sandbox (so subsequent ``export_workspace_bundle``
    #    picks it up).
    # 4. ``reverse_job_export`` — zip or copy the entire job into a
    #    user-chosen folder; ``decompiled/`` is opt-in.

    def reverse_job_reveal_dir(self, job_id: str) -> dict[str, Any]:
        """Open the job's directory in the OS file manager."""
        from argusai.core.infra.explorer import reveal_path

        try:
            jid = str(job_id or "").strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            job = self.reverse_jobs.get(jid, include_trashed=True)
            if job is None:
                return {"ok": False, "error": f"job not found: {jid}"}
            job_dir = Path(str(job.get("_dir") or "")).resolve()
            if not job_dir.is_dir():
                return {"ok": False, "error": f"job directory missing: {job_dir}"}
            result = reveal_path(job_dir)
            result.setdefault("job_id", jid)
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_artifact_reveal(self, job_id: str, artifact_id: str) -> dict[str, Any]:
        """Reveal a single artifact file in the OS file manager."""
        from argusai.core.infra.explorer import reveal_path
        from argusai.core.reverse.reverse_artifacts import resolve_artifact_path

        try:
            target = resolve_artifact_path(
                self.reverse_jobs, str(job_id or ""), str(artifact_id or "")
            )
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        result = reveal_path(target)
        result.setdefault("job_id", str(job_id or ""))
        result.setdefault("artifact_id", str(artifact_id or ""))
        return result

    def reverse_artifact_copy_to(
        self,
        job_id: str,
        artifact_id: str,
        dest_path: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Copy a single artifact to a destination on disk.

        ``dest_path``:
          * empty → write to the current assistant session sandbox
            (``<sandbox>/imports/<artifact-filename>``) when a
            ``session_id`` is provided; otherwise to
            ``<store.root>/_exports/<job-id>/`` for direct UI use.
          * non-empty → treated as an absolute path. If it points to an
            existing directory we append the artifact's filename;
            otherwise the path is used verbatim.
        """
        from argusai.core.paths import PathManager
        from argusai.core.reverse.reverse_artifacts import resolve_artifact_path

        try:
            jid = str(job_id or "").strip()
            aid = str(artifact_id or "").strip()
            if not jid or not aid:
                return {"ok": False, "error": "job_id and artifact_id are required"}
            src = resolve_artifact_path(self.reverse_jobs, jid, aid)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        dest_raw = str(dest_path or "").strip()
        try:
            if dest_raw:
                dest = Path(dest_raw).expanduser().resolve()
                if dest.is_dir():
                    target = dest / src.name
                else:
                    target = dest
            elif session_id:
                sid = self._validate_session_id(session_id)
                sandbox = PathManager(
                    data_root=self.settings_mgr.get().data_root
                ).assistant_sandbox(sid)
                target = (sandbox / "imports" / src.name).resolve()
            else:
                target = (Path(self.reverse_jobs.root) / "_exports" / jid / src.name).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            return {
                "ok": True,
                "job_id": jid,
                "artifact_id": aid,
                "source": str(src),
                "path": str(target),
                "size": target.stat().st_size,
            }
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def reverse_job_export(
        self,
        job_id: str,
        dest_dir: str = "",
        include_decompiled: bool = False,
        as_zip: bool = True,
    ) -> dict[str, Any]:
        """Export job artifacts/findings/reproduce/hooks into a folder or zip.

        ``decompiled/`` (potentially hundreds of MB) is opt-in. Default
        destination is ``<store.root>/_exports/``.
        """
        from argusai.core.infra.explorer import reveal_path
        from argusai.core.reverse.reverse_job_export import export_reverse_job

        try:
            jid = str(job_id or "").strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            result = export_reverse_job(
                self.reverse_jobs,
                jid,
                dest_dir=(dest_dir or None),
                include_decompiled=bool(include_decompiled),
                as_zip=bool(as_zip),
            )
            try:
                reveal_path(Path(result["path"]))
            except Exception:
                pass
            return result
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except OSError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Findings ------------------------------------------------------

    def reverse_finding_add(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            finding = self.reverse_jobs.add_finding(
                str(job_id or ""),
                title=str(data.get("title") or "").strip(),
                body=str(data.get("body") or ""),
                kind=str(data.get("kind") or "note"),
                confidence=str(data.get("confidence") or "probable"),
                tags=list(data.get("tags") or []),
                coverage_id=str(data.get("coverage_id") or ""),
            )
            return {"ok": True, "finding": finding}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_finding_read(self, job_id: str, finding_id: str) -> dict[str, Any]:
        try:
            data = self.reverse_jobs.read_finding(str(job_id or ""), str(finding_id or ""))
            if data is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "finding": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_finding_remove(self, job_id: str, finding_id: str) -> dict[str, Any]:
        try:
            return {"ok": bool(self.reverse_jobs.remove_finding(str(job_id or ""), str(finding_id or "")))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Hooks ---------------------------------------------------------

    def reverse_hook_add(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            hook = self.reverse_jobs.add_hook(
                str(job_id or ""),
                target=str(data.get("target") or "").strip(),
                kind=str(data.get("kind") or "function"),
                expression=str(data.get("expression") or ""),
                scope=str(data.get("scope") or "page"),
                persisted=bool(data.get("persisted", True)),
                recipe=str(data.get("recipe") or ""),
                note=str(data.get("note") or ""),
            )
            return {"ok": True, "hook": hook}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_hook_remove(self, job_id: str, hook_id: str) -> dict[str, Any]:
        try:
            return {"ok": bool(self.reverse_jobs.remove_hook(str(job_id or ""), str(hook_id or "")))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_hook_hits(self, job_id: str, hook_id: str, tail: int = 200) -> dict[str, Any]:
        try:
            hits = self.reverse_jobs.read_hook_hits(
                str(job_id or ""), str(hook_id or ""), tail=int(tail or 200),
            )
            return {"ok": True, "hits": hits}
        except Exception as e:
            return {"ok": False, "error": str(e), "hits": []}

    # ---- Reproduce -----------------------------------------------------

    def reverse_reproduce_add(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = payload or {}
            rec = self.reverse_jobs.add_reproduce(
                str(job_id or ""),
                name=str(data.get("name") or "").strip(),
                content=str(data.get("content") or ""),
                lang=str(data.get("lang") or "python"),
                from_finding=str(data.get("from_finding") or ""),
                verified=bool(data.get("verified", False)),
            )
            return {"ok": True, "reproduce": rec}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_reproduce_read(self, job_id: str, rep_id: str) -> dict[str, Any]:
        try:
            data = self.reverse_jobs.read_reproduce(str(job_id or ""), str(rep_id or ""))
            if data is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "reproduce": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Recipes -------------------------------------------------------

    def reverse_recipe_list(self) -> dict[str, Any]:
        """Catalogue of pre-baked hook recipes the UI can offer."""
        try:
            from argusai.core.reverse.hook_recipes import list_recipes
            return {"ok": True, "recipes": list_recipes()}
        except Exception as e:
            return {"ok": False, "error": str(e), "recipes": []}

    def reverse_recipe_apply(self, job_id: str, recipe_id: str) -> dict[str, Any]:
        """Materialise every hook from a recipe into the ReverseJob (persisted
        only — actual browser injection happens when the user clicks Drain or
        opens the target URL with the hooks loaded).

        Returns the list of installed hook records so the UI can show them.
        """
        try:
            from argusai.core.reverse.hook_recipes import get_recipe

            recipe = get_recipe(str(recipe_id or ""))
            if recipe is None:
                return {"ok": False, "error": "unknown recipe"}
            installed = []
            jid = str(job_id or "")
            for spec in recipe.get("hooks") or []:
                rec = self.reverse_jobs.add_hook(
                    jid,
                    target=str(spec.get("target") or ""),
                    kind=str(spec.get("kind") or "function"),
                    expression=str(spec.get("replace_return_src") or spec.get("replace_get_src") or ""),
                    recipe=str(recipe_id or ""),
                    persisted=True,
                    note=str(spec.get("note") or ""),
                )
                # Persist the extended behaviour fields directly into job.json.
                # ``add_hook`` only exposes a fixed schema, so the recipe-specific
                # bits (replace_*_src, filter_src, tag) are merged in manually.
                extras = {
                    k: spec.get(k)
                    for k in ("replace_return_src", "replace_get_src", "filter_src", "tag")
                    if spec.get(k)
                }
                if extras:
                    try:
                        job_dir, data = self.reverse_jobs._load_or_raise(jid)  # noqa: SLF001
                        for h in data.get("hooks") or []:
                            if h.get("id") == rec["id"]:
                                h.update(extras)
                                break
                        self.reverse_jobs._write_job(job_dir, data)  # noqa: SLF001
                    except Exception:
                        pass
                installed.append(rec)
            return {"ok": True, "installed": installed, "recipe": recipe_id}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- Live browser probes ------------------------------------------

    def reverse_hook_drain_run(self, job_id: str, hook_id: str = "") -> dict[str, Any]:
        """Pull buffered hook hits from the live browser into the job's hits log.

        Synchronous wrapper around ``browser_hook_drain``: spins a private
        event loop so we don't depend on the caller's loop state. Returns
        ``{ok, drained, dropped, per_hook}``.
        """
        try:
            import asyncio
            from argusai.core.browser.browser_pool import get_browser_hub  # noqa: F401 — kept for parity
            from argusai.core.security.probes import browser_hook_drain

            coro = browser_hook_drain(
                store=self.reverse_jobs,
                job_id=str(job_id or ""),
                hook_id=str(hook_id or ""),
            )
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reverse_hook_reattach_run(self, job_id: str) -> dict[str, Any]:
        try:
            import asyncio
            from argusai.core.security.probes import browser_hook_reattach

            coro = browser_hook_reattach(
                store=self.reverse_jobs,
                job_id=str(job_id or ""),
            )
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
