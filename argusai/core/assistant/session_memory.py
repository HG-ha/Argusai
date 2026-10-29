"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import re
import time
from typing import Any

from .attachments import *
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

log = logging.getLogger("argusai.assistant")

# Transient runtime traffic: real facts, but not conversation worth recalling
# months later. Excluded from the derived summary/index projections only.
_MEMORY_EXCLUDED_SOURCES = {"cron_result", "agent_completion_notice"}


class SessionMemoryMixin:
    def _history_fingerprint(self) -> tuple[int, float, int, int, int]:
        """Cheap signal for "did history change since the last rebuild".

        History is append-formed (messages are constructed fully, then
        appended) and truncated wholesale on regenerate, so ``len`` plus a few
        tail attributes detect every mutation that feeds the memory index —
        appends, regenerate truncation, streamed content fills, and the
        tool_calls/tool_trace enrichment that ``_memory_text_for_message``
        folds in on the tail assistant message before the next append.
        """
        if not self.history:
            return (0, 0.0, 0, 0, 0)
        last = self.history[-1]
        return (
            len(self.history),
            float(last.timestamp or 0.0),
            len(last.content or ""),
            len(last.tool_calls or ()),
            len(last.tool_trace or ()),
        )

    def rebuild_memory(self) -> None:
        """Build a compact summary and a lightweight searchable index.

        Raw history remains intact in ``history``. The summary/index are derived
        views used to keep prompt context bounded while still allowing recall.

        The summary deliberately includes (a) every USER message that fell out
        of the recent window — those carry the original goal/intent and losing
        them is what "feels like memory loss" — and (b) the tail of the truncated
        section for short-range context. Tool messages are filtered out of the
        summary since they're already noisy in the index.

        The scan is skipped when history is unchanged since the last rebuild:
        ``to_dict`` (every checkpoint) and ``search_memory`` both call this, so
        a long turn would otherwise re-scan identical history many times.
        """
        fingerprint = self._history_fingerprint()
        if fingerprint == getattr(self, "_memory_fingerprint", None):
            return
        old = self.history[:-self.MAX_HISTORY]
        summary_lines: list[str] = []
        if old:
            summary_lines.append(f"Older conversation contains {len(old)} messages.")
            user_msgs = [m for m in old if m.role == "user" and not m.hidden]
            if user_msgs:
                summary_lines.append("")
                summary_lines.append("### Original user goals (oldest → newest)")
                for m in user_msgs[-12:]:
                    text = self._memory_text_for_message(m, max_len=400)
                    if text:
                        summary_lines.append(f"- {text}")
            assistant_summaries = [
                m for m in old
                if (
                    m.role == "assistant"
                    and m.source != "cron_result"
                    and not m.tool_calls
                    and (m.content or "").strip()
                )
            ]
            if assistant_summaries:
                summary_lines.append("")
                summary_lines.append("### Earlier assistant conclusions")
                for m in assistant_summaries[-10:]:
                    text = self._memory_text_for_message(m, max_len=300)
                    if text:
                        summary_lines.append(f"- {text}")
            summary_lines.append("")
            summary_lines.append("### Recent tail (just before the live window)")
            for m in old[-30:]:
                if m.role == "tool" or m.source in _MEMORY_EXCLUDED_SOURCES:
                    continue
                text = self._memory_text_for_message(m, max_len=220)
                if text:
                    summary_lines.append(f"- {m.role}: {text}")
        self.memory_summary = "\n".join(summary_lines)

        # Full-session user index (small) + recent detail window for all roles.
        by_id: dict[str, dict[str, Any]] = {}
        for idx, m in enumerate(self.history):
            if m.role != "user" or m.hidden or m.ui_hidden:
                continue
            text = self._memory_text_for_message(m, max_len=500)
            if not text:
                continue
            by_id[str(idx)] = {
                "id": str(idx),
                "role": m.role,
                "timestamp": m.timestamp,
                "text": text,
            }
        start = max(0, len(self.history) - self.MEMORY_INDEX_LIMIT)
        for idx, m in enumerate(self.history[start:], start=start):
            if m.source in _MEMORY_EXCLUDED_SOURCES:
                continue
            text = self._memory_text_for_message(m, max_len=1200)
            if not text:
                continue
            eid = str(idx)
            entry = {
                "id": eid,
                "role": m.role,
                "timestamp": m.timestamp,
                "text": text,
            }
            prev = by_id.get(eid)
            if prev is None or len(text) > len(str(prev.get("text") or "")):
                by_id[eid] = entry
        self.memory_index = list(by_id.values())
        self._memory_fingerprint = fingerprint


    def search_memory(self, query: str, limit: int = 8, exclude_last: int = 0) -> list[dict[str, Any]]:
        self.rebuild_memory()
        q = (query or "").strip()
        if not q:
            return []
        terms = self._memory_terms(q)
        max_id = len(self.history) - 1 - max(0, exclude_last)
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in self.memory_index:
            try:
                if int(entry.get("id", "-1")) > max_id:
                    continue
            except ValueError:
                pass
            text = str(entry.get("text") or "")
            hay = text.lower()
            score = 0.0
            for term in terms:
                if term and term in hay:
                    score += 2.0 if len(term) > 3 else 1.0
            if q.lower() in hay:
                score += 5.0
            if score:
                # Prefer newer hits when relevance is similar.
                score += min(float(entry.get("timestamp") or 0) / 10_000_000_000, 0.5)
                scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]


    def search_memory_tool(self, query: str, limit: int = 8) -> dict[str, Any]:
        results = self.search_memory(str(query or ""), limit=int(limit or 8), exclude_last=1)
        enriched: list[dict[str, Any]] = []
        for hit in results:
            item = dict(hit)
            text = str(item.get("text") or "")
            refs = self._extract_memory_refs(text)
            if refs:
                item["refs"] = refs
                if refs.get("case_id") and (refs.get("artifact_ref") or refs.get("artifact_refs")):
                    item["next_step"] = "Call artifact_read(case_id=..., artifact_id=artifact_ref) for the full archived tool result before retrying."
            enriched.append(item)
        return {
            "query": query,
            "results": enriched,
            "memory_summary": self.memory_summary,
            "notes": [
                "Keyword search over indexed session history (all user turns + recent assistant/tool detail).",
                "For chronological recall (e.g. first user message), call list_session_messages(role='user', order='asc', limit=5) then read_session_message(message_id=...).",
                "Tool hits may be excerpts. If case_id + artifact_ref is present, use artifact_read for the full original archived result instead of repeating the tool.",
            ],
        }


    def _extract_memory_refs(self, text: str) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in ("case_id", "artifact_ref", "artifact_refs", "artifact_title", "evidence_ref"):
                    if key in obj:
                        refs[key] = obj[key]
        except Exception:
            pass
        if "case_id" not in refs:
            m = re.search(r"\bcase-\d{8}-[A-Za-z0-9]{4,8}\b", text)
            if m:
                refs["case_id"] = m.group(0)
        if "artifact_ref" not in refs:
            m = re.search(r"\bart-[A-Za-z0-9]{4,12}\b", text)
            if m:
                refs["artifact_ref"] = m.group(0)
        return refs


    def list_session_messages_tool(
        self,
        role: str = "user",
        offset: int = 0,
        limit: int = 20,
        order: str = "asc",
    ) -> dict[str, Any]:
        """Browse session history by index — model-driven recall, not prompt injection."""
        role_filter = (role or "user").strip().lower()
        if role_filter not in ("user", "assistant", "tool", "all"):
            role_filter = "user"
        order_norm = (order or "asc").strip().lower()
        if order_norm not in ("asc", "desc"):
            order_norm = "asc"
        off = max(0, int(offset or 0))
        lim = max(1, min(int(limit or 20), 50))

        rows: list[dict[str, Any]] = []
        for idx, m in enumerate(self.history):
            if m.hidden or m.ui_hidden:
                continue
            if role_filter != "all" and m.role != role_filter:
                continue
            preview = self._memory_text_for_message(m, max_len=320)
            if not preview and not m.attachments:
                continue
            rows.append({
                "message_id": idx,
                "role": m.role,
                "timestamp": m.timestamp,
                "preview": preview,
                "has_tool_calls": bool(m.tool_calls),
                "has_attachments": bool(m.attachments),
            })
        if order_norm == "desc":
            rows.reverse()
        total = len(rows)
        page = rows[off: off + lim]
        return {
            "role": role_filter,
            "order": order_norm,
            "offset": off,
            "limit": lim,
            "total_matching": total,
            "messages": page,
            "notes": [
                "message_id is the stable index into this session's history; pass it to read_session_message.",
                "For the opening user goal, use role=user, order=asc, offset=0, limit=1..5.",
            ],
        }

    def read_session_message_tool(self, message_id: int, max_chars: int = 12000) -> dict[str, Any]:
        """Fetch one history row by index for active recall."""
        try:
            idx = int(message_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "message_id must be an integer history index"}
        if idx < 0 or idx >= len(self.history):
            return {
                "ok": False,
                "error": f"message_id out of range (0..{max(0, len(self.history) - 1)})",
                "history_length": len(self.history),
            }
        m = self.history[idx]
        cap = max(500, min(int(max_chars or 12000), 64_000))
        content = (m.content or "").strip()
        truncated = False
        if len(content) > cap:
            content = content[: cap - 3] + "..."
            truncated = True
        return {
            "ok": True,
            "message_id": idx,
            "role": m.role,
            "timestamp": m.timestamp,
            "content": content,
            "truncated": truncated,
            "has_tool_calls": bool(m.tool_calls),
            "tool_call_names": [
                (tc.get("function") or {}).get("name")
                for tc in (m.tool_calls or [])
                if (tc.get("function") or {}).get("name")
            ],
            "has_attachments": bool(m.attachments),
        }

    def memory_prompt(self, query: str) -> str:
        hits = self.search_memory(query, limit=8, exclude_last=1)
        parts: list[str] = []
        if self.memory_summary:
            parts.append("## Long-term Conversation Summary\n" + self.memory_summary[:6000])
        if hits:
            lines = ["## Relevant Past Context"]
            for h in hits:
                lines.append(f"- [{h.get('role')}] {str(h.get('text') or '')[:900]}")
            parts.append("\n".join(lines))
        kb_hint = self._knowledge_base_hint(query)
        if kb_hint:
            parts.append(kb_hint)
        if not parts:
            return ""
        return "\n\n" + "\n\n".join(parts)

    def _attach_volatile_context(self, messages: list[dict[str, Any]], volatile: str) -> None:
        """Park per-turn volatile context on the live user turn (Loom22 §12.5).

        The current UI snapshot and the long-term memory recall change on every
        turn. Concatenating them onto the system prefix means the provider's
        prompt cache (which keys on the literal prefix) is invalidated each
        turn, so the whole stable system + unchanged history never gets a cache
        hit. Attaching them to the most recent user turn — the volatile tail —
        keeps the prefix stable while putting the context where the model
        attends to it most. Falls back to the system message only if there is
        no user turn to host it.
        """
        if not volatile:
            return
        for i in range(len(messages) - 1, 0, -1):
            m = messages[i]
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                messages[i] = {**m, "content": (m.get("content") or "").rstrip() + volatile}
                return
        if messages:
            m0 = messages[0]
            messages[0] = {**m0, "content": (m0.get("content") or "") + volatile}


    def _build_mcp_servers_prompt(self) -> str:
        """Surface configured MCP servers to the model.

        MCP child tools are deliberately NOT registered as top-level
        ToolRegistry names (Loom22 §5 / MCP plan §"不做的事"), so the
        model has no other way to know what external capabilities the
        user has wired up unless we tell it.

        IMPORTANT: the ``name`` the user typed in settings is just a
        local nickname — the AUTHORITATIVE identity of an MCP server
        comes from the protocol handshake (``serverInfo.name`` +
        ``serverInfo.version``) and its capabilities come from
        ``tools/list``. We expose both layers separately:

        - ``user_label`` / ``note`` — what the user wrote, helpful as
          a hint about what they wanted that server for.
        - ``server_info`` / ``tools`` — pulled from the cache
          populated by previous ``mcp_server_health`` /
          ``mcp_server_list_tools`` calls. When absent we explicitly
          mark the server as "not yet probed" and tell the model to
          probe it.

        We never leak env values or header values — only key names —
        and we only include enabled servers. Returns "" when no MCP
        servers are configured.
        """
        api = getattr(getattr(self, "registry", None), "api", None)
        if api is None:
            return ""
        try:
            from argusai.core.settings import normalize_mcp_servers
            from argusai.core.tools.case_mcp import mcp_discovery_cache
            raw = normalize_mcp_servers(api.settings_mgr.get().mcp_servers)
            cache = mcp_discovery_cache(api)
        except Exception:
            return ""
        servers = [s for s in raw if s.get("enabled", True)]
        if not servers:
            return ""
        lines: list[str] = [
            "## Configured MCP Servers",
            (
                "The user has wired up the following external MCP servers. The `user_label` "
                "and `note` fields below are NICKNAMES the user typed — they hint at intent "
                "but are NOT the authoritative server identity. The real identity comes from "
                "the MCP `initialize` handshake (`serverInfo.name@version`) and the real "
                "tool catalogue comes from `tools/list`."
            ),
            (
                "These child tools are NOT in your top-level tool list. To use them:\n"
                "1. `mcp_server_health(server_id=...)` — fetch authoritative serverInfo + "
                "protocol version (cheap, do this once per server when you first need it).\n"
                "2. `mcp_server_list_tools(server_id=...)` — discover the real tool catalogue "
                "(name, description, JSON schema).\n"
                "3. `mcp_server_call_tool(server_id, tool_name, arguments_json)` — invoke a tool.\n"
                "Before telling the user a capability is unavailable, check whether any of "
                "these servers might provide it via steps 1+2."
            ),
        ]
        for s in servers:
            sid = str(s.get("id") or "").strip()
            transport = s.get("transport") or "stdio"
            policy = s.get("policy") or {}
            bits: list[str] = [f"transport={transport}"]
            if transport == "stdio":
                cmd = (s.get("command") or "").strip()
                if cmd:
                    bits.append(f"command={cmd[:140]}")
            else:
                url = (s.get("url") or "").strip()
                if url:
                    bits.append(f"url={url}")
            risk = policy.get("default_risk") or "high"
            bits.append(f"default_risk={risk}")
            if policy.get("allow_auto_approve"):
                bits.append("auto_approve=allowed")

            user_label = (s.get("name") or "").strip() or sid
            notes = (s.get("notes") or "").strip()
            bits.append(f'user_label="{user_label[:80]}"')
            if notes:
                bits.append(f'note="{notes[:160]}"')

            cached = cache.get(sid) if isinstance(cache, dict) else None
            if isinstance(cached, dict) and cached.get("server_info"):
                info = cached["server_info"] if isinstance(cached["server_info"], dict) else {}
                real_name = str(info.get("name") or "").strip()
                real_ver = str(info.get("version") or "").strip()
                if real_name:
                    ident = real_name + (f"@{real_ver}" if real_ver else "")
                    bits.append(f"server_info={ident}")
                proto = str(cached.get("protocol_version") or "").strip()
                if proto:
                    bits.append(f"protocol={proto}")
            else:
                bits.append("server_info=NOT_YET_PROBED")

            line = f"- `{sid}` — " + ", ".join(bits)
            if isinstance(cached, dict) and isinstance(cached.get("tools"), list) and cached["tools"]:
                # Show up to 16 tool names so the model can reason about
                # capability fit without re-listing every turn. Full
                # schemas still require an explicit list_tools call.
                tnames = []
                for t in cached["tools"][:16]:
                    if not isinstance(t, dict):
                        continue
                    nm = str(t.get("name") or "").strip()
                    if not nm:
                        continue
                    desc = str(t.get("description") or "").strip()
                    if desc:
                        tnames.append(f"{nm}: {desc[:80]}")
                    else:
                        tnames.append(nm)
                if tnames:
                    extra = " ..." if len(cached["tools"]) > 16 else ""
                    line += "\n  tools (cached): " + "; ".join(tnames) + extra
            lines.append(line)
        return "\n\n" + "\n".join(lines)


    def _knowledge_base_hint(self, query: str) -> str:
        """Surface up to ~5 KB entries the model might want to read.

        We only include lightweight summaries (id, title, tags, applies_to,
        confidence). The model can ``kb_read`` for full body when relevant.
        """
        if self.kb is None:
            return ""
        try:
            hits = self.kb.search(query or "", limit=5)
        except Exception:
            return ""
        if not hits:
            # Even with no query match, hint that the KB exists so the model
            # remembers it's available. Show 3 most recent entries.
            try:
                hits = self.kb.list_entries()[:3]
            except Exception:
                hits = []
        if not hits:
            return ""
        lines = [
            "## Knowledge Base — your past notes (use kb_read to load full body)",
        ]
        for h in hits:
            tags = ", ".join((h.get("tags") or [])[:4])
            applies = "; ".join((h.get("applies_to") or [])[:2])
            confidence = h.get("confidence") or "probable"
            line = f"- `{h.get('id')}` [{confidence}] **{h.get('title')}**"
            if tags:
                line += f" — tags: {tags}"
            if applies:
                line += f" — applies: {applies}"
            lines.append(line)
        return "\n".join(lines)


    def _memory_text_for_message(self, m: ChatMessage, max_len: int = 800) -> str:
        text = (m.content or "").strip()
        if m.tool_calls:
            names = []
            for tc in m.tool_calls:
                fn = (tc.get("function") or {}).get("name")
                if fn:
                    names.append(fn)
            if names:
                text = (text + "\n" if text else "") + "tool_calls=" + ",".join(names)
        if m.tool_trace:
            trace_bits = []
            for tr in m.tool_trace[:5]:
                trace_bits.append(f"{tr.get('name')} ok={tr.get('ok')}")
            if trace_bits:
                text = (text + "\n" if text else "") + "tool_trace=" + "; ".join(trace_bits)
        if m.role == "tool" and text.startswith("{"):
            try:
                obj = json.loads(text)
                text = json.dumps(self._compact_tool_result(obj), ensure_ascii=False, default=str)
            except Exception:
                pass
        return text[:max_len]


    def _compact_tool_result(self, obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        keep = {
            "ok", "id", "name", "status", "target_url", "total_findings",
            "severity_counts", "reports", "error", "timed_out", "case_id",
            "artifact_ref", "artifact_refs", "evidence_ref",
        }
        out = {k: obj.get(k) for k in keep if k in obj}
        if "meta" in obj and isinstance(obj["meta"], dict):
            out["meta"] = {k: obj["meta"].get(k) for k in ("id", "name", "target_url", "status", "progress", "total", "severity")}
        if "top_findings" in obj:
            out["top_findings"] = obj.get("top_findings", [])[:5]
        return out or obj


    def _remember_case_from_tool_result(self, fn_name: str, tool_result: Any) -> None:
        """Track the active case whenever the model creates or uses one."""
        if not isinstance(tool_result, dict):
            return
        case_id = ""
        case_obj = tool_result.get("case")
        if isinstance(case_obj, dict):
            case_id = str(case_obj.get("id") or "")
        if not case_id:
            case_id = str(tool_result.get("case_id") or "")
        if case_id.startswith("case-"):
            self.current_case_id = case_id


    def _tool_result_target_hint(self, fn_name: str, args: dict[str, Any], tool_result: dict[str, Any]) -> str:
        for key in ("target", "url", "target_url", "navigate_to"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("url", "target_url"):
            value = tool_result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        tab = tool_result.get("tab")
        if isinstance(tab, dict):
            value = tab.get("url")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fn_name


    def _ensure_current_case_for_tool(self, fn_name: str, args: dict[str, Any], tool_result: dict[str, Any]) -> str:
        """Create one lightweight session case on first critical web result."""
        if self.current_case_id:
            return self.current_case_id
        api = getattr(self.registry, "api", None)
        if api is None:
            return ""
        target_value = self._tool_result_target_hint(fn_name, args, tool_result)
        target_type = "url" if re.match(r"^https?://", target_value, re.I) else "other"
        try:
            from argusai.core.tools import _resolve_case_store

            session_id = getattr(self.registry, "session_id", "") or ""
            case = _resolve_case_store(api).create_case(
                title=f"Assistant web investigation: {target_value[:96]}",
                target={"type": target_type, "value": target_value, "name": "", "scope": []},
                domain="web",
                tags=["assistant-auto-archive"],
                note=f"Automatically created to preserve important tool results for assistant session {session_id}.",
            )
            case_id = str(case.get("id") or "")
            if case_id:
                self.current_case_id = case_id
            return self.current_case_id
        except Exception:
            log.debug("Failed to create auto-archive case for %s", fn_name, exc_info=True)
            return ""


    def _auto_archive_tool_result(self, fn_name: str, args: dict[str, Any], tool_result: Any) -> Any:
        """Attach high-value tool results to the current case and return refs.

        Prompt compaction can later elide the bulky body, but ``case_id`` and
        ``artifact_ref`` survive in the compact summary so the model has a
        stable way to recover the exact request/response instead of retrying.
        """
        recorder = getattr(self, "_record_tool_outcome", None)
        if recorder is not None:
            recorder(fn_name, args, tool_result)
        self._remember_case_from_tool_result(fn_name, tool_result)
        if fn_name not in self._auto_archive_tool_names() or not isinstance(tool_result, dict):
            return tool_result
        if tool_result.get("skipped") or tool_result.get("reason") == "duplicate_tool_call_skipped":
            return tool_result
        if tool_result.get("artifact_ref") and tool_result.get("case_id"):
            return tool_result

        api = getattr(self.registry, "api", None)
        if api is None:
            return tool_result
        case_id = self._ensure_current_case_for_tool(fn_name, args, tool_result)
        if not case_id:
            return tool_result

        try:
            from argusai.core.tools import _resolve_case_store

            target = self._tool_result_target_hint(fn_name, args, tool_result)
            status = tool_result.get("status_code") or tool_result.get("status") or ""
            title = f"{fn_name}: {target[:120]}"
            if status:
                title = f"{title} [{status}]"
            payload = {
                "tool": fn_name,
                "arguments": args,
                "result": tool_result,
                "session_id": getattr(self.registry, "session_id", "") or "",
                "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            }
            artifact = _resolve_case_store(api).add_artifact(
                case_id,
                kind="interaction" if fn_name.startswith("browser_") else "response",
                title=title,
                content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                filename=f"{fn_name}.json",
                domain="web",
                media_type="application/json",
                metadata={
                    "tool": fn_name,
                    "target": target,
                    "status": status,
                    "session_id": getattr(self.registry, "session_id", "") or "",
                },
            )
            enriched = dict(tool_result)
            enriched["case_id"] = case_id
            enriched["artifact_ref"] = artifact.get("id")
            enriched["artifact_refs"] = [artifact.get("id")] if artifact.get("id") else []
            enriched["artifact_title"] = artifact.get("title") or title
            return enriched
        except Exception:
            log.debug("Failed to auto-archive %s result", fn_name, exc_info=True)
            return tool_result


    def _smart_excerpt(self, text: str, budget: int, *, field: str = "text") -> str:
        """Return a valid string excerpt that preserves both start and end."""
        if len(text) <= budget:
            return text
        marker = f"\n...[omitted {len(text) - budget} chars from middle of {field}; full value may be in artifact_ref]...\n"
        if budget <= len(marker) + 32:
            return text[: max(0, budget - len(marker))] + marker
        head_budget = max(1, int(budget * 0.65))
        tail_budget = max(1, budget - head_budget - len(marker))
        return text[:head_budget] + marker + text[-tail_budget:]


    def _shrink_tool_result_for_prompt(self, obj: Any, _depth: int = 0) -> Any:
        if _depth >= self._GENERIC_DICT_DEPTH:
            # Stop walking — represent the deep node as its compact JSON.
            try:
                s = json.dumps(obj, ensure_ascii=False, default=str)
            except Exception:
                s = repr(obj)
            if len(s) > 256:
                s = self._smart_excerpt(s, 256, field="deep")
            return s
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            if len(obj) > self._GENERIC_STRING_CAP:
                return self._smart_excerpt(obj, self._GENERIC_STRING_CAP)
            return obj
        if isinstance(obj, list):
            head = obj[:self._LIST_HEAD]
            shrunk = [self._shrink_tool_result_for_prompt(x, _depth + 1) for x in head]
            extra = len(obj) - len(head)
            if extra > 0:
                shrunk.append(f"...[{extra} more items elided]")
            return shrunk
        if isinstance(obj, tuple):
            return self._shrink_tool_result_for_prompt(list(obj), _depth)
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k == "_assistant_attachments":
                    continue
                if isinstance(v, str) and k in self._FAT_STRING_BUDGETS:
                    budget = self._FAT_STRING_BUDGETS[k]
                    if len(v) > budget:
                        out[k] = self._smart_excerpt(v, budget, field=k)
                        out.setdefault("_truncated_fields", []).append(k)
                    else:
                        out[k] = v
                else:
                    out[k] = self._shrink_tool_result_for_prompt(v, _depth + 1)
            return out
        # Anything else (bytes, datetime, custom class) — let json.dumps
        # handle it via ``default=str``.
        return obj


    def _pop_tool_attachments(self, tool_result: Any) -> list[dict[str, Any]]:
        if not isinstance(tool_result, dict):
            return []
        raw = tool_result.pop("_assistant_attachments", None)
        if not isinstance(raw, list):
            return []
        attachments: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and (item.get("type") or "") == "image" and item.get("data_base64"):
                attachments.append(item)
        return attachments


    def _append_visual_tool_attachments(
        self,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        *,
        source: str,
    ) -> None:
        if not attachments:
            return
        names = ", ".join(str(att.get("name") or "screenshot.png") for att in attachments)
        content = (
            f"Visual attachment(s) captured by {source}: {names}. "
            "Inspect the image(s) directly before making visual/layout/captcha conclusions."
        )
        messages.append({"role": "user", "content": content, "attachments": attachments})
        self.history.append(ChatMessage(
            role="user",
            content=content,
            attachments=attachments,
            hidden=True,
            timestamp=time.time(),
        ))


    def _tool_result_to_prompt(self, fn_name: str, tool_result: Any) -> str:
        """Serialise a tool result for the LLM prompt with per-field
        truncation. ALWAYS use this instead of ``json.dumps(tool_result)``
        when appending to ``messages``.
        """
        # Presentation tools (see ``_PRESENTATION_TOOLS``) return the full UI
        # block so the renderer can draw it. The model does NOT need that payload
        # echoed back — it already authored it — and re-feeding the whole block
        # every subsequent round is pure token waste that also tempts the model
        # to re-emit it. The block still reaches the UI via the tool trace; the
        # model only gets a compact acknowledgement.
        if fn_name in _PRESENTATION_TOOLS and isinstance(tool_result, dict):
            block = tool_result.get("block")
            block = block if isinstance(block, dict) else {}
            ack = {
                "ok": bool(tool_result.get("ok", True)),
                "shown": not tool_result.get("duplicate", False),
                "block_type": block.get("type") or "",
                "title": block.get("title") or "",
                "note": "Displayed to the user. Do not repeat this card; continue or conclude.",
            }
            return json.dumps(ack, ensure_ascii=False)
        if isinstance(tool_result, dict) and tool_result.get("duplicate"):
            return json.dumps(
                {"ok": True, "shown": False, "note": str(tool_result.get("note") or "Already shown this turn.")},
                ensure_ascii=False,
            )
        shrunk = self._shrink_tool_result_for_prompt(tool_result)
        try:
            s = json.dumps(shrunk, ensure_ascii=False, default=str)
        except Exception:
            s = json.dumps({"error": "could not serialise tool result", "tool": fn_name}, ensure_ascii=False)
        if len(s) > self._TOTAL_CAP:
            extra = len(s) - self._TOTAL_CAP
            # Try to keep valid JSON shape: dump a synthetic wrapper.
            wrapper = {
                "_truncated_total": True,
                "_original_bytes": len(s),
                "_kept_bytes": self._TOTAL_CAP,
                "_note": (
                    f"This tool result was {len(s)} bytes; a head+tail excerpt is shown. "
                    "If case_id/artifact_ref is present, read the artifact for the full value; "
                    "otherwise re-run the tool with a narrower query."
                ),
                "preview": self._smart_excerpt(s, self._TOTAL_CAP, field="tool_result_json"),
            }
            s = json.dumps(wrapper, ensure_ascii=False)
        return s


    @staticmethod
    def _provider_tool_use_ids(provider_content: list[dict[str, Any]] | None) -> list[str]:
        ids: list[str] = []
        for block in provider_content or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = str(block.get("id") or "").strip()
            if tid:
                ids.append(tid)
        return ids


    @staticmethod
    def _tool_call_ids_from_message(message: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            tid = str(tc.get("id") or "").strip()
            if tid and tid not in seen:
                ids.append(tid)
                seen.add(tid)
        for tid in SessionMemoryMixin._provider_tool_use_ids(message.get("provider_content") or []):
            if tid and tid not in seen:
                ids.append(tid)
                seen.add(tid)
        return ids


    @staticmethod
    def _missing_tool_result_message(tool_call_id: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(
                {
                    "error": "missing_tool_result",
                    "reason": "previous tool call was interrupted before a result was recorded",
                },
                ensure_ascii=False,
            ),
        }


    def _patch_message_tool_adjacency_in_place(self, messages: list[dict[str, Any]]) -> None:
        """Repair persisted history before any provider sees it.

        Anthropic requires every assistant ``tool_use`` to be answered by
        matching ``tool_result`` blocks in the immediately-following user
        message. We keep canonical history intact, but the transient request
        payload gets tombstone tool results for interrupted calls.
        """
        patched: list[dict[str, Any]] = []
        expected_ids: list[str] = []
        inserted_tombstones = 0
        dropped_orphans = 0
        input_len = len(messages)

        for msg in messages:
            role = msg.get("role")
            if expected_ids:
                if role == "tool":
                    tid = str(msg.get("tool_call_id") or "").strip()
                    if tid in expected_ids:
                        patched.append(msg)
                        expected_ids = [x for x in expected_ids if x != tid]
                        continue
                for tid in expected_ids:
                    patched.append(self._missing_tool_result_message(tid))
                    inserted_tombstones += 1
                expected_ids = []

            if role == "tool":
                dropped_orphans += 1
                continue

            patched.append(msg)
            if role == "assistant":
                expected_ids = self._tool_call_ids_from_message(msg)

        for tid in expected_ids:
            patched.append(self._missing_tool_result_message(tid))
            inserted_tombstones += 1

        if inserted_tombstones or dropped_orphans:
            messages[:] = patched
            log.warning(
                "assistant.tool_adjacency_patch inserted_tombstones=%d dropped_orphan_tool_results=%d input_msgs=%d output_msgs=%d",
                inserted_tombstones,
                dropped_orphans,
                input_len,
                len(messages),
            )


    def _memory_terms(self, text: str) -> list[str]:
        lowered = text.lower()
        terms = re.findall(r"[a-z0-9_\-./:]{2,}", lowered)
        # Add short Chinese fragments so Chinese queries can hit stored text.
        chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        for chunk in chinese:
            terms.extend(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
        return list(dict.fromkeys(terms))[:80]


