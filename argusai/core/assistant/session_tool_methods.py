"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import re
import time
from typing import Any

from .attachments import *
from .media_auxiliary import (
    auxiliary_endpoint_disable_reason,
    auxiliary_endpoint_enabled,
    vision_describe_image,
)
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

log = logging.getLogger("argusai.assistant")


def _structured_card_signature(block: dict[str, Any]) -> str:
    """Stable content key for a structured_card, ignoring its volatile id.

    Two ``show_structured_card`` calls with the same rendered content produce
    the same signature even though each call gets a fresh block id, so the
    turn-scoped dedup can recognise a repeat.
    """
    try:
        return json.dumps(
            {
                "title": block.get("title") or "",
                "subtitle": block.get("subtitle") or "",
                "sections": block.get("sections") or [],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return ""


class SessionToolMethodsMixin:
    def _register_session_tools(self) -> None:
        self.registry.register(
            "search_memory",
            (
                "Keyword-search this session's history (all user turns are indexed; recent assistant/tool rows too). "
                "Use before repeating a probe when you need URLs, payloads, errors, case_id, or artifact_ref. "
                "For 'what did the user say first' or chronological recall, prefer list_session_messages + read_session_message."
            ),
            {
                "query": {"type": "string", "description": "Search query: tool name, URL, payload, error text, case_id, artifact_ref, or paraphrased user goal."},
                "limit": {"type": "integer", "description": "Maximum number of memory hits", "default": 8},
            },
            lambda query="", limit=8: self.search_memory_tool(str(query or ""), limit=int(limit or 8)),
            required=["query"],
            risk="low",
        )
        self.registry.register(
            "background_job_status",
            (
                "Read status/result of a durable background tool job by job_ref. "
                "Long scanners (sqlmap/nuclei/ffuf) and async run_subagent return a receipt with job_ref; "
                "poll this sparingly — do not busy-loop."
            ),
            {
                "job_ref": {"type": "string", "description": "job://… reference from a background receipt"},
            },
            lambda job_ref="": self.background_job_status_tool(str(job_ref or "")),
            required=["job_ref"],
            risk="low",
            kind="polling",
            dedupe=False,
            concurrency="read_parallel",
        )
        self.registry.register(
            "background_job_list",
            "List recent background jobs for this session (status only).",
            {
                "limit": {"type": "integer", "description": "Max jobs to return", "default": 20},
            },
            lambda limit=20: self.background_job_list_tool(int(limit or 20)),
            risk="low",
            kind="polling",
            dedupe=False,
        )
        self.registry.register(
            "background_job_cancel",
            (
                "Request cancellation of a cancellable background job by job_ref. "
                "Async child Agents support cancellation; jobs without a cancellation hook return an explicit error."
            ),
            {
                "job_ref": {"type": "string", "description": "job://… reference from a background receipt"},
            },
            lambda job_ref="": self.background_job_cancel_tool(str(job_ref or "")),
            required=["job_ref"],
            risk="low",
            kind="control",
            dedupe=False,
            concurrency="serial",
        )
        self.registry.register(
            "wait_for_duration",
            (
                "Pause this Agent turn for a short, deliberate duration, then continue with the next step. "
                "This is a cross-domain timing primitive: use it when the next action genuinely depends on "
                "brief time passage, such as UI/app startup, service warm-up, retry backoff, rate-limit "
                "cooldown, state propagation, or staged re-observation, and no task-specific poll/wait tool exists. "
                "Do not use to wait for user input (call stop_for_user_decision), completed scans "
                "(call wait_for_task/background_job_status), or long schedules (use cron)."
            ),
            {
                "seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 300,
                    "description": "Duration to wait in seconds (0-300).",
                },
                "reason": {
                    "type": "string",
                    "description": "Short operational reason for the delay.",
                    "default": "",
                },
            },
            self.wait_for_duration_tool,
            required=["seconds"],
            risk="low",
            kind="polling",
            dedupe=False,
            concurrency="serial",
            completion="inline",
        )
        self.registry.register(
            "run_subagent",
            (
                "Delegate a focused, self-contained sub-task to an independent readonly sub-agent. "
                "Returns only a distilled conclusion (summary/artifacts/findings) — never the child transcript. "
                "Use when a side investigation would pollute this context. Depth is hard-capped at 1. "
                "mode=sync waits; mode=async returns a job_ref receipt."
            ),
            {
                "role": {"type": "string", "description": "Sub-agent specialty, e.g. reverse-analyst"},
                "objective": {"type": "string", "description": "Single judgeable objective"},
                "context": {"type": "string", "description": "Minimal facts only — do not dump full history", "default": ""},
                "expected_output": {"type": "string", "description": "Desired conclusion shape", "default": ""},
                "permission": {
                    "type": "string",
                    "enum": ["readonly", "scoped"],
                    "description": "readonly default; scoped reserved",
                    "default": "readonly",
                },
                "mode": {
                    "type": "string",
                    "enum": ["sync", "async"],
                    "description": "sync waits for conclusion; async returns job_ref",
                    "default": "sync",
                },
                "scope_ref": {"type": "string", "description": "Server scope ref for scoped permission", "default": ""},
                "requested_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional capability names for scoped mode",
                    "default": [],
                },
            },
            self.run_subagent_tool,
            required=["role", "objective"],
            risk="medium",
            kind="control",
            dedupe=False,
            concurrency="serial",
            completion="inline",
        )
        self.registry.register(
            "spawn_agent",
            (
                "Create a durable independent child-Agent thread and return immediately with an agent_id. "
                "The thread has isolated history, an isolated AI client, persisted checkpoints, and an explicit "
                "capability boundary. Tools in that projection run without a second confirmation card — "
                "spawning the child is the authorization. Use "
                "list_agents/wait_for_agents/send_agent_input/pause_agent/resume_agent/close_agent "
                "for lifecycle control. Prefer this over legacy run_subagent(mode='async')."
            ),
            {
                "role": {"type": "string", "description": "Child Agent specialty"},
                "objective": {"type": "string", "description": "One concrete, judgeable objective"},
                "context": {"type": "string", "description": "Minimal facts required for this objective", "default": ""},
                "expected_output": {"type": "string", "description": "Required result shape", "default": ""},
                "permission": {
                    "type": "string",
                    "enum": ["readonly", "scoped"],
                    "description": "readonly, or an exact scoped capability set. Spawn is the confirmation gate.",
                    "default": "readonly",
                },
                "scope_ref": {"type": "string", "description": "Optional free-text authorization note (not required; scoped mode only needs requested_tools)", "default": ""},
                "requested_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names delegated in scoped mode; those tools run without a second Allow card",
                    "default": [],
                },
                "model": {
                    "type": "string",
                    "description": "Optional configured endpoint name or exact model id",
                    "default": "",
                },
                "thinking_enabled": {
                    "type": "boolean",
                    "description": "Optional child reasoning-mode override; omit to use endpoint behavior",
                },
            },
            self.spawn_agent_tool,
            required=["role", "objective"],
            risk="medium",
            kind="control",
            dedupe=False,
            concurrency="serial",
        )
        self.registry.register(
            "spawn_team",
            (
                "One-off squad inside THIS chat only. Children are grouped in the Agent transcript, "
                "cannot talk to each other, and disappear with the conversation. "
                "Use a preset (web_audit | recon_sweep | reverse_triage) or explicit roles, then "
                "list_agents/send_agent_input/pause_agent/resume_agent/close_agent. "
                "Do NOT use this when the user wants the Team sidebar (小队/团队/建队/让团队做): "
                "that persistent room is team_create / team_add_member / team_message_send."
            ),
            {
                "objective": {"type": "string", "description": "The shared team mission (one concrete goal)"},
                "preset": {
                    "type": "string",
                    "enum": ["", "web_audit", "recon_sweep", "reverse_triage"],
                    "description": "Built-in role set to expand. Omit when passing explicit roles.",
                    "default": "",
                },
                "team": {"type": "string", "description": "Team display name (defaults to the preset label)", "default": ""},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "focus": {"type": "string", "description": "What this role concentrates on"},
                            "objective": {"type": "string", "description": "Optional per-role objective override"},
                            "permission": {"type": "string", "enum": ["readonly", "scoped"]},
                            "scope_ref": {"type": "string"},
                            "requested_tools": {"type": "array", "items": {"type": "string"}},
                            "model": {"type": "string"},
                        },
                    },
                    "description": "Explicit roles; overrides preset when provided.",
                    "default": [],
                },
                "permission": {
                    "type": "string",
                    "enum": ["readonly", "scoped"],
                    "description": "Default permission for roles that do not set their own.",
                    "default": "readonly",
                },
                "scope_ref": {"type": "string", "description": "Shared scope reference for scoped roles", "default": ""},
                "context": {"type": "string", "description": "Shared minimal context handed to every role", "default": ""},
            },
            self.spawn_team_tool,
            required=["objective"],
            risk="medium",
            kind="control",
            dedupe=False,
            concurrency="serial",
        )
        self.registry.register(
            "spawn_external_agent",
            (
                "Create a durable child Agent backed by an explicitly selected local ACP runtime. "
                "This always requires user confirmation and never silently falls back to the native "
                "ArgusAI provider. Use only when the user requested a local coding Agent or the "
                "runtime provides a clearly useful specialization."
            ),
            {
                "runtime_id": {
                    "type": "string",
                    "description": "Enabled ACP runtime id from the configured local Agent catalogue.",
                },
                "role": {"type": "string", "description": "Child Agent specialty"},
                "objective": {"type": "string", "description": "One concrete, judgeable objective"},
                "context": {
                    "type": "string",
                    "description": "Minimal facts required for this objective",
                    "default": "",
                },
                "expected_output": {
                    "type": "string",
                    "description": "Required result shape",
                    "default": "",
                },
                "permission": {
                    "type": "string",
                    "enum": ["readonly", "scoped"],
                    "default": "readonly",
                },
                "scope_ref": {"type": "string", "default": ""},
                "requested_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            self.spawn_external_agent_tool,
            required=["runtime_id", "role", "objective"],
            risk="high",
            kind="control",
            dedupe=False,
            concurrency="serial",
        )
        self.registry.register(
            "send_agent_input",
            (
                "Queue a follow-up instruction for a durable child Agent. If it is running, the instruction is "
                "applied at the next safe response boundary; if it already completed or paused, a new turn starts."
            ),
            {
                "agent_id": {"type": "string", "description": "agent_id from spawn_agent/list_agents"},
                "message": {"type": "string", "description": "Bounded follow-up instruction"},
            },
            self.send_agent_input_tool,
            required=["agent_id", "message"],
            risk="low",
            kind="control",
            dedupe=False,
        )
        self.registry.register(
            "pause_agent",
            "Pause a running child Agent after its current safe response/tool boundary.",
            {"agent_id": {"type": "string", "description": "Child Agent id"}},
            self.pause_agent_tool,
            required=["agent_id"],
            risk="low",
            kind="control",
            dedupe=False,
        )
        self.registry.register(
            "resume_agent",
            "Resume a paused, failed, or completed child Agent with an explicit continuation message.",
            {
                "agent_id": {"type": "string", "description": "Child Agent id"},
                "message": {"type": "string", "description": "Why and how to continue"},
            },
            self.resume_agent_tool,
            required=["agent_id", "message"],
            risk="low",
            kind="control",
            dedupe=False,
        )
        self.registry.register(
            "wait_for_agents",
            (
                "Wait for one or more child Agents to leave queued/running/closing state, up to 300 seconds. "
                "Use once when their results are needed; do not busy-loop."
            ),
            {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more child Agent ids",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 300,
                    "default": 30,
                },
            },
            self.wait_for_agents_tool,
            required=["agent_ids"],
            risk="low",
            kind="polling",
            dedupe=False,
            concurrency="serial",
        )
        self.registry.register(
            "list_agents",
            "List durable child-Agent threads and their current lifecycle state.",
            {
                "include_closed": {
                    "type": "boolean",
                    "description": "Include explicitly closed threads",
                    "default": False,
                },
            },
            self.list_agents_tool,
            risk="low",
            kind="polling",
            dedupe=False,
        )
        self.registry.register(
            "get_agent",
            "Read one child Agent's status, event history, and visible transcript for verification.",
            {"agent_id": {"type": "string", "description": "Child Agent id"}},
            self.get_agent_tool,
            required=["agent_id"],
            risk="low",
            kind="polling",
            dedupe=False,
        )
        self.registry.register(
            "close_agent",
            "Stop a running child Agent and permanently close its thread.",
            {"agent_id": {"type": "string", "description": "Child Agent id"}},
            self.close_agent_tool,
            required=["agent_id"],
            risk="low",
            kind="control",
            dedupe=False,
        )
        self.registry.register(
            "list_session_messages",
            (
                "Browse this session's persisted chat history by stable message_id (history index). "
                "Use proactively when the user asks about earlier turns, the opening goal, or '第一句话'. "
                "Typical flow: list_session_messages(role='user', order='asc', limit=5) → read_session_message(message_id=...)."
            ),
            {
                "role": {
                    "type": "string",
                    "description": "Filter: user | assistant | tool | all",
                    "default": "user",
                },
                "offset": {"type": "integer", "description": "Skip this many matching rows after ordering", "default": 0},
                "limit": {"type": "integer", "description": "Max rows to return (1-50)", "default": 20},
                "order": {
                    "type": "string",
                    "description": "asc = oldest first (use for session start); desc = newest first",
                    "default": "asc",
                },
            },
            lambda role="user", offset=0, limit=20, order="asc": self.list_session_messages_tool(
                str(role or "user"), int(offset or 0), int(limit or 20), str(order or "asc")
            ),
            risk="low",
        )
        self.registry.register(
            "read_session_message",
            (
                "Read one session history row by message_id from list_session_messages. "
                "Use after browsing when you need the full user/assistant text."
            ),
            {
                "message_id": {"type": "integer", "description": "History index from list_session_messages"},
                "max_chars": {"type": "integer", "description": "Content cap (default 12000)", "default": 12000},
            },
            lambda message_id=0, max_chars=12000: self.read_session_message_tool(
                int(message_id), int(max_chars or 12000)
            ),
            required=["message_id"],
            risk="low",
        )
        self.registry.register(
            "vision_inspect_image",
            (
                "Read/understand an image. Pass path= for any image file on disk (project, host, "
                "screenshot, or sandbox) — do not copy it into the session sandbox just to look at it. "
                "Omit path to re-read a conversation image: the user's pasted screenshot, or a shot "
                "from desktop_observe / browser_*. Uses the auxiliary vision endpoint when configured; "
                "native vision models also receive the pixels. Tool/desktop/browser screenshots are "
                "NOT auto-described, so call this to inspect them."
            ),
            {
                "query": {
                    "type": "string",
                    "description": "What to focus on or a question about the image, e.g. '读出报错对话框里的完整文字'. Optional.",
                    "default": "",
                },
                "source": {
                    "type": "string",
                    "enum": ["latest", "user", "tool"],
                    "description": "When path is empty: latest = most recent image of any kind (default); user = the user's pasted image; tool = the most recent tool/desktop/browser screenshot.",
                    "default": "latest",
                },
                "path": {
                    "type": "string",
                    "description": "Optional image file: absolute host path, project/codebase-relative, or sandbox-relative. Takes priority over source=. Empty = use a conversation attachment.",
                    "default": "",
                },
            },
            lambda query="", source="latest", path="": self.vision_inspect_image_tool(
                str(query or ""), str(source or "latest"), str(path or "")
            ),
            risk="low",
        )
        self.registry.register(
            "set_todos",
            "Create or replace the structured todo list for a multi-step assistant task. For security assessments, prefer parent todos for phases and child todos for executable actions.",
            {
                "items": {
                    "type": "array",
                    "description": "Todo items with id/title/content/status and optional children. Broad audits should use phase parent todos with concrete child actions; do not store raw evidence here, store references.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"]},
                            "kind": {"type": "string"},
                            "phase": {"type": "string"},
                            "risk": {"type": "string"},
                            "target_ref": {"type": "string"},
                            "tool_hint": {"type": "string"},
                            "done_when": {"type": "string", "description": "Concrete completion criteria. Required in spirit for broad audit/pentest todos."},
                            "evidence": {"type": "string", "description": "Evidence, artifact_ref, tool result, or reason supporting the current status."},
                            "finding_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            "artifact_refs": {"type": "array", "items": {"type": "string"}},
                            "blocked_reason": {"type": "string"},
                            "children": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                }
            },
            lambda items=None: self.set_todos(items or []),
            required=["items"],
            risk="low",
        )
        self.registry.register(
            "add_todos",
            "Append newly discovered todo items without replacing the current list. Use parent_id to append child actions under an existing parent todo.",
            {
                "items": {
                    "type": "array",
                    "description": "New todo items to append. Use parent_id to connect branch tasks to the todo that discovered them, and trigger to record the condition that caused them.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"]},
                            "kind": {"type": "string"},
                            "phase": {"type": "string"},
                            "risk": {"type": "string"},
                            "target_ref": {"type": "string"},
                            "tool_hint": {"type": "string"},
                            "done_when": {"type": "string", "description": "Concrete completion criteria for this branch task."},
                            "evidence": {"type": "string", "description": "Initial evidence or artifact that justifies creating this task."},
                            "finding_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            "artifact_refs": {"type": "array", "items": {"type": "string"}},
                            "blocked_reason": {"type": "string"},
                            "children": {"type": "array", "items": {"type": "object"}},
                            "parent_id": {"type": "string", "description": "Todo id that discovered or spawned this task."},
                            "trigger": {"type": "string", "description": "Condition/finding that made this task necessary."},
                        },
                    },
                },
                "parent_id": {"type": "string", "description": "Optional default parent_id applied to items that omit it."},
                "trigger": {"type": "string", "description": "Optional default trigger applied to items that omit it."},
            },
            lambda items=None, parent_id="", trigger="": self.add_todos(
                items or [],
                str(parent_id or ""),
                str(trigger or ""),
            ),
            required=["items"],
            risk="low",
        )
        self.registry.register(
            "update_todo",
            "Update one todo item by id, including child todos. Use patch for structured fields and refs.",
            {
                "id": {"type": "string", "description": "Todo id"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"]},
                "content": {"type": "string", "description": "Optional replacement content"},
                "title": {"type": "string", "description": "Optional replacement title"},
                "done_when": {"type": "string", "description": "Optional replacement completion criteria"},
                "evidence": {"type": "string", "description": "Optional evidence or reason for this status update"},
                "patch": {"type": "object", "description": "Optional structured field patch, including finding_refs/evidence_refs/artifact_refs/blocked_reason/tool_hint/target_ref/risk/phase/kind."},
                "parent_id": {"type": "string", "description": "Optional parent todo id, useful when adding/updating branch tasks"},
                "trigger": {"type": "string", "description": "Optional condition/finding that spawned this todo"},
            },
            lambda id="", status="", content="", title="", done_when="", evidence="", patch=None, parent_id="", trigger="": self.update_todo(
                str(id),
                str(status),
                str(content or ""),
                str(title or ""),
                str(done_when or ""),
                str(evidence or ""),
                patch if isinstance(patch, dict) else None,
                str(parent_id or ""),
                str(trigger or ""),
            ),
            required=["id", "status"],
            risk="low",
        )
        self.registry.register(
            "complete_todo_with_refs",
            (
                "Attach finding/evidence/artifact references to a todo and optionally mark it completed. "
                "Use after artifact_add/evidence_add or validation work so the Assessment Plan keeps durable proof links."
            ),
            {
                "id": {"type": "string", "description": "Todo id to update"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"], "default": "completed"},
                "finding_refs": {"type": "array", "items": {"type": "string"}, "description": "Finding ids or route-style refs to attach"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "Evidence ids or route-style refs to attach"},
                "artifact_refs": {"type": "array", "items": {"type": "string"}, "description": "Artifact ids or route-style refs to attach"},
                "evidence": {"type": "string", "description": "Short reason or evidence summary; do not include raw proof bodies"},
                "blocked_reason": {"type": "string", "description": "Reason when status is blocked"},
            },
            lambda id="", status="completed", finding_refs=None, evidence_refs=None, artifact_refs=None, evidence="", blocked_reason="": self.complete_todo_with_refs(
                str(id),
                str(status or "completed"),
                finding_refs or [],
                evidence_refs or [],
                artifact_refs or [],
                str(evidence or ""),
                str(blocked_reason or ""),
            ),
            required=["id"],
            risk="low",
        )
        self.registry.register(
            "create_validation_todo",
            (
                "Create a standardized validation child todo for a finding or suspected issue. "
                "Use before running validation probes so proof can later be attached with complete_todo_with_refs."
            ),
            {
                "parent_id": {"type": "string", "description": "Parent phase todo id, for example phase.validation"},
                "finding_ref": {"type": "string", "description": "Finding id/ref to validate"},
                "asset_ref": {"type": "string", "description": "Asset ref such as url:/api/login, apk:app.apk, exe:sample.exe"},
                "title": {"type": "string", "description": "Optional validation todo title"},
                "tool_hint": {"type": "string", "description": "Suggested validation tool"},
                "risk": {"type": "string", "description": "Validation risk level", "default": "medium"},
                "done_when": {"type": "string", "description": "Concrete validation completion criteria"},
            },
            lambda parent_id="", finding_ref="", asset_ref="", title="", tool_hint="", risk="medium", done_when="": self.create_validation_todo(
                str(parent_id or ""),
                str(finding_ref or ""),
                str(asset_ref or ""),
                str(title or ""),
                str(tool_hint or ""),
                str(risk or "medium"),
                str(done_when or ""),
            ),
            required=["parent_id", "finding_ref"],
            risk="low",
        )
        self.registry.register(
            "assessment_plan_snapshot",
            "Return an adapter-ready snapshot of the current Assessment Plan, including tree, flat rows, current todo and unfinished items.",
            {},
            self.assessment_plan_snapshot,
            risk="low",
        )
        self.registry.register(
            "list_todos",
            "List the current structured todo list.",
            {},
            self.list_todos,
            risk="low",
        )
        self.registry.register(
            "clear_todos",
            "Clear the current structured todo list.",
            {},
            self.clear_todos,
            risk="low",
        )
        self.registry.register(
            "configure_auto_archive_tools",
            (
                "Inspect or adjust this session's automatic tool-result archival policy. "
                "Use add/set when upcoming tools are likely to return large or important evidence "
                "that should survive prompt compaction as case artifacts. This affects only the current assistant session; "
                "use reset to return to the user's global settings."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "remove", "set", "reset"],
                    "description": "list current policy, add/remove tools, set exact session override, or reset to global settings",
                    "default": "list",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names to add/remove/set. Ignored for list/reset.",
                    "default": [],
                },
            },
            lambda action="list", tools=None: self.configure_auto_archive_tools(str(action or "list"), tools or []),
            risk="low",
        )
        self.registry.register(
            "show_data_table",
            (
                "Render a structured, clickable table in the assistant UI. Use this instead of Markdown tables "
                "when presenting search results, candidates, endpoints, findings, files, memory hits, or other "
                "lists where each row may have one or more task-specific action buttons. This is a UI block protocol: provide "
                "data only, never HTML or JavaScript. Each row may have either a single action or an actions array "
                "with up to 4 buttons. Row actions may use href values like '#/knowledge/<id>', '#/task/<id>', "
                "or '#/reverse/<rev-id>' (never put a rev-* id under #/knowledge/), "
                "or a plain value that is sent back as the user's next instruction. Labels and values may be custom, "
                "but keep them short, concrete, and directly actionable. After calling this "
                "tool, do not repeat the same rows as a Markdown table; only write a concise summary and next step. "
                "Use this tool after search/list tools when the user should see structured results."
            ),
            {
                "title": {"type": "string", "description": "Short table title."},
                "subtitle": {"type": "string", "description": "Optional small caption, e.g. query/count/filter.", "default": ""},
                "columns": {
                    "type": "array",
                    "description": "Visible columns. Keep to 2-5 concise columns.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Cell key used by rows[].cells."},
                            "label": {"type": "string", "description": "Column header label."},
                        },
                    },
                },
                "rows": {
                    "type": "array",
                    "description": "Rows. Each row has cells plus optional action {label, href|value} or actions [{label, href|value}].",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "cells": {"type": "object", "description": "Mapping of column key to display value."},
                            "action": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "Concise custom button label, e.g. 查看 / 打开 / 使用 / 对比 / 生成计划."},
                                    "href": {"type": "string", "description": "Optional frontend hash route, e.g. #/knowledge/<id> or #/task/<id>."},
                                    "value": {"type": "string", "description": "Optional action value submitted back to the assistant."},
                                },
                            },
                            "actions": {
                                "type": "array",
                                "description": "Optional multiple row actions. Keep to 1-4 concise buttons.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "Concise custom button label, e.g. 查看 / 使用 / 对比 / 生成计划 / 标为证据."},
                                        "href": {"type": "string", "description": "Optional frontend hash route, e.g. #/knowledge/<id> or #/task/<id>."},
                                        "value": {"type": "string", "description": "Optional action value submitted back to the assistant."},
                                    },
                                },
                            },
                        },
                    },
                },
                "empty_text": {"type": "string", "description": "Text shown when rows is empty.", "default": "暂无数据。"},
            },
            lambda title="", subtitle="", columns=None, rows=None, empty_text="": self.show_data_table(
                str(title or ""),
                str(subtitle or ""),
                list(columns or []),
                list(rows or []),
                str(empty_text or ""),
            ),
            required=["title", "columns", "rows"],
            risk="low",
        )
        self.registry.register(
            "show_structured_card",
            (
                "Render a generic structured UI card using a safe, whitelisted JSON layout. "
                "Use this for overviews, grouped summaries, key/value facts, metrics, steps, timelines, "
                "badges, and action groups when the data is not naturally a table. This is a UI block protocol: "
                "provide data only, never HTML/CSS/JavaScript. Actions may use href values like '#/knowledge/<id>', "
                "'#/task/<id>', or '#/reverse/<rev-id>' (never put a rev-* id under #/knowledge/), "
                "or a plain value submitted back to the assistant."
            ),
            {
                "title": {"type": "string", "description": "Short card title."},
                "subtitle": {"type": "string", "description": "Optional caption.", "default": ""},
                "variant": {
                    "type": "string",
                    "enum": ["neutral", "info", "success", "warning", "danger"],
                    "description": "Visual tone.",
                    "default": "neutral",
                },
                "sections": {
                    "type": "array",
                    "description": (
                        "Whitelisted sections. Supported types: text, kv, metrics, list, steps, badges, actions. "
                        "No nested cards, no HTML."
                    ),
                    "items": {"type": "object"},
                },
            },
            lambda title="", subtitle="", variant="neutral", sections=None: self.show_structured_card(
                str(title or ""),
                str(subtitle or ""),
                str(variant or "neutral"),
                list(sections or []),
            ),
            required=["title", "sections"],
            risk="low",
        )
        self.registry.register(
            "show_file_resource_view",
            (
                "Render a file-resource browser card in the assistant UI. Use this for sandbox artifacts, generated "
                "scripts, PoC files, reports, reverse-engineering outputs, logs, or other files the user may want "
                "to click and preview. Provide metadata only; do not inline full file contents. Paths should be "
                "current-session sandbox relative paths whenever possible. The UI will safely request text previews "
                "on demand."
            ),
            {
                "title": {"type": "string", "description": "Short view title, e.g. 沙盒产物 or 生成的脚本."},
                "subtitle": {"type": "string", "description": "Optional caption or root description.", "default": ""},
                "root_label": {"type": "string", "description": "Display label for the resource root.", "default": "session sandbox"},
                "items": {
                    "type": "array",
                    "description": "Files/directories to show. Keep to 1-80 visible items; include relative_path/path, name, kind, size, mime if known.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "path": {"type": "string"},
                            "relative_path": {"type": "string"},
                            "kind": {"type": "string", "enum": ["file", "dir"]},
                            "mime": {"type": "string"},
                            "size": {"type": "integer"},
                            "previewable": {"type": "boolean"},
                            "description": {"type": "string"},
                            "actions": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                },
                "empty_text": {"type": "string", "description": "Text shown when items is empty.", "default": "暂无文件。"},
            },
            lambda title="", subtitle="", root_label="", items=None, empty_text="": self.show_file_resource_view(
                str(title or ""),
                str(subtitle or ""),
                str(root_label or ""),
                list(items or []),
                str(empty_text or ""),
            ),
            required=["title", "items"],
            risk="low",
        )
        self.registry.register(
            "stop_for_user_decision",
            (
                "HARD STOP: terminate the current auto-running turn immediately and surface a question/decision to the user. "
                "This tool is the ONLY guaranteed way to break the auto-continue loop. "
                "CRITICAL: do NOT batch other tool calls together with this one — any tool calls "
                "in the same batch will be cancelled with tombstone results, and the turn will "
                "end as soon as this tool returns. Call it on its own. "
                "Use this whenever you cannot make further progress without a human choice — "
                "e.g. multiple paths to take, missing credentials, a moral / scope decision, "
                "or the next steps require infrastructure the user must provide. This is the "
                "ONLY reliable way to break out of the auto-continue loop while leaving the "
                "remaining todos as still-in-progress for later."
            ),
            {
                "question": {
                    "type": "string",
                    "description": (
                        "SHORT card title/question only (1-2 sentences, ideally <200 chars). "
                        "Do NOT paste the explanation you already wrote in assistant prose — "
                        "the card is a control, not a second copy of the message."
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete one-click choices for the user. Prefer 2-6 short, action-oriented options whenever you are asking the user to choose a path, scope, priority, report style, or whether to proceed.",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional ONE short line of why we need the choice (≤1-2 sentences). "
                        "Never dump the prior essay / MCP install guide here."
                    ),
                },
                "input_placeholder": {
                    "type": "string",
                    "description": "Optional placeholder for an inline user reply box, e.g. '请输入邮箱和密码，或说明你已在浏览器登录'.",
                },
                "input_required": {
                    "type": "boolean",
                    "description": "Whether the inline reply box should be treated as required before the user submits a free-form answer.",
                },
                "recommended": {
                    "type": "string",
                    "description": "Recommended/default option. For unattended cron this is selected automatically.",
                },
            },
            lambda question="", options=None, context="", input_placeholder="", input_required=False, recommended="": self.stop_for_user_decision(
                str(question or "").strip(),
                list(options or []),
                str(context or "").strip(),
                str(input_placeholder or "").strip(),
                bool(input_required),
                str(recommended or "").strip(),
            ),
            required=["question"],
            risk="low",
        )
        self.registry.register(
            "stop_for_user_decision_batch",
            (
                "HARD STOP: ask the user to answer multiple related decision questions in one card, then end the current turn. "
                "Use this instead of emitting several stop_for_user_decision calls when the choices are independent but should be confirmed together. "
                "CRITICAL: call it on its own; any other tool calls in the same batch will be cancelled with tombstone results. "
                "Each question needs a stable id, a concise question, and 2-6 short options. Keep the batch small."
            ),
            {
                "title": {
                    "type": "string",
                    "description": "Short title for the batch decision card, e.g. '确认执行策略'.",
                },
                "questions": {
                    "type": "array",
                    "description": "2-6 questions the user should answer before the assistant continues.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Stable short id such as 'runtime' or 'scope'."},
                            "question": {"type": "string", "description": "One concise question."},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "2-6 concrete one-click options.",
                            },
                            "required": {"type": "boolean", "description": "Whether this question must be answered.", "default": True},
                            "context": {"type": "string", "description": "Optional per-question context.", "default": ""},
                            "recommended": {
                                "type": "string",
                                "description": "Recommended/default option for unattended execution.",
                                "default": "",
                            },
                        },
                    },
                },
                "context": {
                    "type": "string",
                    "description": "Optional overall context shown above the questions.",
                },
            },
            lambda title="", questions=None, context="": self.stop_for_user_decision_batch(
                str(title or "").strip(),
                list(questions or []),
                str(context or "").strip(),
            ),
            required=["title", "questions"],
            risk="low",
        )


    def _current_app_settings(self) -> Any | None:
        """Best-effort access to the live AppSettings via the GUI API bridge."""
        api = getattr(self.registry, "api", None)
        if api is None:
            return None
        try:
            return api.settings_mgr.get()
        except Exception:
            log.debug("settings_mgr.get() failed", exc_info=True)
            return None

    def _find_recent_image_attachment(self, source: str) -> tuple[dict[str, Any], str] | None:
        """Most recent image attachment in history matching ``source``.

        User-pasted images ride on visible user messages; tool/desktop/browser
        screenshots ride on hidden user messages (see
        ``_append_visual_tool_attachments``). We use the ``hidden`` flag to tell
        them apart for the ``user`` / ``tool`` filters.

        Inline ``data_base64`` is preferred, but a spilled ``sandbox_path`` /
        ``path`` is enough — later turns must still be able to re-read the file.
        """
        for msg in reversed(self.history):
            if getattr(msg, "role", "") != "user":
                continue
            is_tool_shot = bool(getattr(msg, "hidden", False))
            if source == "user" and is_tool_shot:
                continue
            if source == "tool" and not is_tool_shot:
                continue
            for att in getattr(msg, "attachments", None) or []:
                if not isinstance(att, dict) or (att.get("type") or "") != "image":
                    continue
                if att.get("data_base64") or att.get("host_path") or att.get("sandbox_path") or att.get("path"):
                    return att, ("tool" if is_tool_shot else "user")
        return None

    def _image_inspect_search_roots(self) -> list[Any]:
        from pathlib import Path

        roots: list[Any] = []
        api = getattr(self.registry, "api", None)
        session_id = str(getattr(self.registry, "session_id", "") or "")
        kwargs = {"_session_id": session_id, "_session": self}
        if api is not None:
            try:
                from argusai.core.tools.common import _session_project_dir, _session_sandbox_root

                roots.append(Path(_session_sandbox_root(api, kwargs)))
                project = _session_project_dir(api, kwargs)
                if project is not None:
                    roots.append(Path(project))
            except Exception:
                log.debug("image inspect sandbox/project roots failed", exc_info=True)
            try:
                from argusai.core.tools.sandbox_code import _codebase_scope

                state = _codebase_scope(api, kwargs)
                if state.get("root"):
                    roots.append(Path(state["root"]))
            except Exception:
                pass
        try:
            roots.append(Path.cwd())
        except OSError:
            pass
        return roots

    async def vision_inspect_image_tool(
        self,
        query: str = "",
        source: str = "latest",
        path: str = "",
    ) -> dict[str, Any]:
        """Describe or attach an image from a file path or conversation history."""
        from .image_io import (
            assistant_image_attachment,
            load_image_file,
            load_image_from_attachment,
            resolve_inspectable_image,
        )
        from .media_enrichment import _active_chat_endpoint, _endpoint_supports_image

        src = (source or "latest").strip().lower()
        if src not in ("latest", "user", "tool"):
            src = "latest"
        raw_path = str(path or "").strip()
        loaded: dict[str, Any] | None = None
        origin = "file"
        image_name = "image"

        if raw_path:
            target = resolve_inspectable_image(raw_path, search_roots=self._image_inspect_search_roots())
            if target is None:
                return {
                    "ok": False,
                    "error": f"找不到图片文件：{raw_path}",
                    "next_step": (
                        "传入绝对路径，或相对于当前项目 / codebase / 会话沙盒的路径。"
                        "不必先把图片复制进沙盒。"
                    ),
                }
            loaded = load_image_file(target)
            image_name = target.name
            origin = "file"
        else:
            found = self._find_recent_image_attachment(src)
            if not found:
                return {
                    "ok": False,
                    "error": "本轮对话历史中没有找到可读取的图片附件。",
                    "next_step": (
                        "请用户直接粘贴/上传图片，传入 path= 指向磁盘上的图片，"
                        "或先用 desktop_observe / browser_screenshot 等工具产生截图后再调用。"
                    ),
                }
            att, origin = found
            loaded = load_image_from_attachment(att)
            image_name = str(att.get("name") or loaded.get("name") or "image")

        if not loaded or not loaded.get("ok"):
            return {
                "ok": False,
                "error": (loaded or {}).get("error") or "无法读取图片。",
            }

        settings = self._current_app_settings()
        vision_ep = getattr(settings, "ai_auxiliary_vision", None) if settings is not None else None
        native = _endpoint_supports_image(_active_chat_endpoint(settings) if settings is not None else None)
        aux_on = auxiliary_endpoint_enabled(vision_ep)
        attachment = assistant_image_attachment(loaded)
        if not aux_on and not native:
            reason = auxiliary_endpoint_disable_reason(vision_ep, label="视觉辅助端点")
            return {
                "ok": False,
                "error": f"无法读取图片：{reason}。",
                "next_step": (
                    "请在 设置 → AI 配置 → 视觉辅助 中填好接口地址/模型/Key 并打开开关，"
                    "或改用带原生视觉的对话模型；在此之前请让用户用文字描述图片内容，不要假装能看到。"
                ),
            }

        out: dict[str, Any] = {
            "ok": True,
            "image_name": image_name,
            "source": origin,
            "path": loaded.get("path") or raw_path,
            "mime": loaded.get("mime") or "image/png",
            "size": loaded.get("size") or 0,
            "attached_to_model": bool(attachment),
        }
        if attachment:
            out["_assistant_attachments"] = [attachment]
        elif loaded.get("size"):
            out["attachment_skipped_reason"] = (
                f"image too large for model attachment ({loaded.get('size')} bytes)"
            )

        if aux_on:
            result = await vision_describe_image(
                settings,
                data_base64=loaded.get("data_base64") or "",
                mime=loaded.get("mime") or "image/png",
                user_hint=str(query or ""),
            )
            if not result.get("ok"):
                if native and attachment:
                    out["note"] = (
                        f"辅助视觉理解失败：{result.get('error', 'unknown')}。"
                        "图片已附给原生视觉模型，下一轮可直接看图。"
                    )
                    return out
                return {"ok": False, "error": f"视觉理解失败：{result.get('error', 'unknown')}"}
            out["model"] = result.get("model", "")
            out["description"] = result.get("description", "")
            out["note"] = (
                "该描述来自独立视觉辅助端点（非主模型原生视觉）。"
                "如需更多细节可带 query 再次调用。磁盘上的图直接 path= 即可，不必先复制进沙盒。"
            )
            return out

        out["note"] = (
            "图片已附给当前原生视觉模型，下一轮可直接看图。"
            "未启用视觉辅助端点，因此没有单独的文字描述。"
        )
        return out

    def show_data_table(
        self,
        title: str,
        subtitle: str,
        columns: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        empty_text: str = "",
    ) -> dict[str, Any]:
        """Return a sanitized generic table block for model-authored UI."""
        clean_columns: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        for col in columns[:6]:
            if not isinstance(col, dict):
                continue
            key = str(col.get("key") or "").strip()[:64]
            label = str(col.get("label") or key).strip()[:80]
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            clean_columns.append({"key": key, "label": label or key})

        clean_rows: list[dict[str, Any]] = []
        for idx, row in enumerate(rows[:64]):
            if not isinstance(row, dict):
                continue
            raw_cells = row.get("cells") if isinstance(row.get("cells"), dict) else row
            cells: dict[str, str] = {}
            for col in clean_columns:
                value = raw_cells.get(col["key"]) if isinstance(raw_cells, dict) else ""
                if isinstance(value, (list, tuple)):
                    text = ", ".join(str(x) for x in value[:8])
                elif value is None:
                    text = ""
                else:
                    text = str(value)
                cells[col["key"]] = text[:240]

            clean_row: dict[str, Any] = {
                "id": str(row.get("id") or idx)[:80],
                "cells": cells,
            }
            raw_actions = row.get("actions")
            clean_actions: list[dict[str, str]] = []
            if isinstance(raw_actions, list):
                clean_actions = [self._clean_ui_action(action) for action in raw_actions[:4]]
                clean_actions = [action for action in clean_actions if action]
            elif isinstance(row.get("action"), dict):
                clean_action = self._clean_ui_action(row.get("action"))
                if clean_action:
                    clean_actions = [clean_action]
            if clean_actions:
                clean_row["actions"] = clean_actions
                clean_row["action"] = clean_actions[0]
            clean_rows.append(clean_row)

        block = {
            "type": "data_table",
            "source_tool": "show_data_table",
            "render_policy": "model_authored",
            "title": (title or "结果列表").strip()[:120],
            "subtitle": (subtitle or "").strip()[:160],
            "columns": clean_columns,
            "rows": clean_rows,
            "empty_text": (empty_text or "暂无数据。").strip()[:120],
        }
        return {"ok": True, "block": block}


    def _clean_ui_action(self, action: Any) -> dict[str, str]:
        """Sanitize a model-authored UI action."""
        if not isinstance(action, dict):
            return {}
        label = str(action.get("label") or "").strip()[:32]
        href = str(action.get("href") or "").strip()
        value = str(action.get("value") or "").strip()
        if not label:
            label = "打开" if href else ("选择" if value else "")
        clean: dict[str, str] = {}
        if label:
            clean["label"] = label
        if href.startswith("#/"):
            clean["href"] = href[:240]
        elif value:
            clean["value"] = value[:320]
        return clean if len(clean) > 1 else {}


    def show_structured_card(
        self,
        title: str,
        subtitle: str,
        variant: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a sanitized generic structured card block."""
        allowed_variants = {"neutral", "info", "success", "warning", "danger"}
        card_variant = variant if variant in allowed_variants else "neutral"
        clean_sections: list[dict[str, Any]] = []
        for section in sections[:12]:
            if not isinstance(section, dict):
                continue
            stype = str(section.get("type") or "").strip()
            clean: dict[str, Any] = {"type": stype}
            stitle = str(section.get("title") or "").strip()[:100]
            if stitle:
                clean["title"] = stitle

            if stype == "text":
                text = str(section.get("text") or section.get("body") or "").strip()[:1200]
                if not text:
                    continue
                clean["text"] = text
            elif stype in {"kv", "metrics"}:
                items: list[dict[str, str]] = []
                for item in list(section.get("items") or [])[:12]:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("key") or item.get("label") or "").strip()[:80]
                    value = str(item.get("value") or "").strip()[:240]
                    if not label and not value:
                        continue
                    clean_item = {"label": label or "—", "value": value or "—"}
                    note = str(item.get("note") or item.get("trend") or "").strip()[:80]
                    if note:
                        clean_item["note"] = note
                    items.append(clean_item)
                if not items:
                    continue
                clean["items"] = items
            elif stype in {"list", "badges"}:
                items = []
                for item in list(section.get("items") or [])[:24]:
                    if isinstance(item, dict):
                        label = str(item.get("label") or item.get("text") or "").strip()[:140]
                        if not label:
                            continue
                        clean_item = {"label": label}
                        item_variant = str(item.get("variant") or "").strip()
                        if item_variant in allowed_variants:
                            clean_item["variant"] = item_variant
                        items.append(clean_item)
                    else:
                        text = str(item).strip()[:140]
                        if text:
                            items.append({"label": text})
                if not items:
                    continue
                clean["items"] = items
            elif stype == "steps":
                items = []
                for idx, item in enumerate(list(section.get("items") or [])[:12], start=1):
                    if isinstance(item, dict):
                        step_title = str(item.get("title") or item.get("label") or f"Step {idx}").strip()[:120]
                        body = str(item.get("body") or item.get("text") or "").strip()[:360]
                        status = str(item.get("status") or "").strip()[:40]
                        clean_item = {"title": step_title}
                        if body:
                            clean_item["body"] = body
                        if status:
                            clean_item["status"] = status
                        items.append(clean_item)
                    else:
                        text = str(item).strip()[:240]
                        if text:
                            items.append({"title": text})
                if not items:
                    continue
                clean["items"] = items
            elif stype == "actions":
                actions = [self._clean_ui_action(a) for a in list(section.get("actions") or section.get("items") or [])[:8]]
                actions = [a for a in actions if a]
                if not actions:
                    continue
                clean["actions"] = actions
            else:
                continue
            clean_sections.append(clean)

        block = {
            "type": "structured_card",
            "source_tool": "show_structured_card",
            "render_policy": "model_authored",
            "title": (title or "结构化摘要").strip()[:120],
            "subtitle": (subtitle or "").strip()[:180],
            "variant": card_variant,
            "sections": clean_sections,
        }
        # Turn-scoped idempotency: a re-call with identical content this turn is
        # a no-op so the same card can't be persisted (and rendered) twice. We
        # tell the model it already showed it, which also discourages the repeat.
        signature = _structured_card_signature(block)
        seen = getattr(self, "_emitted_card_signatures", None)
        if isinstance(seen, set):
            if signature in seen:
                return {
                    "ok": True,
                    "duplicate": True,
                    "note": "An identical structured card was already shown this turn; not repeating it.",
                }
            seen.add(signature)
        return {"ok": True, "block": block}


    def show_file_resource_view(
        self,
        title: str,
        subtitle: str,
        root_label: str,
        items: list[dict[str, Any]],
        empty_text: str = "",
    ) -> dict[str, Any]:
        """Return a sanitized file-resource browser block."""
        clean_items: list[dict[str, Any]] = []
        for idx, item in enumerate(items[:80]):
            if not isinstance(item, dict):
                continue
            path = str(item.get("relative_path") or item.get("path") or "").strip()
            path = path.strip('"').strip("'").replace("\\", "/")
            name = str(item.get("name") or Path(path).name or f"file-{idx + 1}").strip()[:120]
            if not path and not name:
                continue
            kind = str(item.get("kind") or "file").strip().lower()
            if kind not in {"file", "dir"}:
                kind = "file"
            clean: dict[str, Any] = {
                "id": str(item.get("id") or path or name or idx).strip()[:160],
                "name": name,
                "path": path[:500],
                "relative_path": str(item.get("relative_path") or path).strip().replace("\\", "/")[:500],
                "kind": kind,
                "previewable": bool(item.get("previewable", kind == "file")),
            }
            mime = str(item.get("mime") or "").strip()[:120]
            if mime:
                clean["mime"] = mime
            description = str(item.get("description") or "").strip()[:240]
            if description:
                clean["description"] = description
            try:
                size = int(item.get("size")) if item.get("size") is not None else None
            except (TypeError, ValueError):
                size = None
            if size is not None and size >= 0:
                clean["size"] = size
            actions = [self._clean_ui_action(a) for a in list(item.get("actions") or [])[:4]]
            actions = [a for a in actions if a]
            if actions:
                clean["actions"] = actions
            clean_items.append(clean)

        block = {
            "type": "file_resource_view",
            "source_tool": "show_file_resource_view",
            "render_policy": "model_authored",
            "title": (title or "文件资源").strip()[:120],
            "subtitle": (subtitle or "").strip()[:180],
            "root_label": (root_label or "session sandbox").strip()[:120],
            "items": clean_items,
            "empty_text": (empty_text or "暂无文件。").strip()[:120],
        }
        return {"ok": True, "block": block}


    def set_todos(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        previous_empty = not self.todos
        already_approved = bool(self._todos_plan_approved)
        normalized = self._normalize_todos(items)
        self.todos = self._enforce_single_in_progress(normalized)
        self._todos_touched_this_turn = True
        # Same gate as add_todos: the first plan (or a rewrite before the
        # user confirms) must stop. After 开始执行, set_todos is an in-place
        # refresh — re-arming here is what froze the click, duplicated the
        # "好的，开始…" bubble, and resurrected the plan card.
        plan_confirmation = None
        if previous_empty or not already_approved:
            plan_confirmation = self._arm_todo_plan_confirmation(stop_turn=bool(self.todos))
        result = self.list_todos()
        if plan_confirmation:
            result["plan_confirmation"] = plan_confirmation
        return result


    def add_todos(self, items: list[dict[str, Any]], parent_id: str = "", trigger: str = "") -> dict[str, Any]:
        was_empty = not self.todos
        added: list[str] = []
        updated: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            candidate = dict(raw)
            if parent_id and not candidate.get("parent_id"):
                candidate["parent_id"] = parent_id
            if trigger and not candidate.get("trigger"):
                candidate["trigger"] = trigger
            normalized = self._normalize_todo(candidate)
            todo_id = normalized.get("id") or ""
            if not todo_id:
                continue
            found = self._find_todo_by_id(todo_id)
            existing = found[0] if found else None
            if existing is None:
                target_parent_id = str(normalized.get("parent_id") or parent_id or "").strip()
                if target_parent_id:
                    parent = self._find_todo_by_id(target_parent_id)
                    if parent is not None:
                        normalized.pop("parent_id", None)
                        parent[0].setdefault("children", []).append(normalized)
                    else:
                        self.todos.append(normalized)
                else:
                    self.todos.append(normalized)
                added.append(todo_id)
                continue
            for key in self._TODO_PATCH_FIELDS:
                value = normalized.get(key)
                if value not in (None, "", []):
                    existing[key] = value
            updated.append(todo_id)
        self.todos = self._enforce_single_in_progress(self.todos)
        self._todos_touched_this_turn = True
        plan_confirmation = None
        if was_empty or not self._todos_plan_approved:
            plan_confirmation = self._arm_todo_plan_confirmation(stop_turn=True)
        result = self.list_todos()
        result.update({"added": added, "updated": updated})
        if plan_confirmation:
            result["plan_confirmation"] = plan_confirmation
        return result


    def update_todo(
        self,
        todo_id: str,
        status: str,
        content: str = "",
        title: str = "",
        done_when: str = "",
        evidence: str = "",
        patch: dict[str, Any] | None = None,
        parent_id: str = "",
        trigger: str = "",
    ) -> dict[str, Any]:
        if not todo_id:
            return {"error": "id is required", "todos": self.todos}
        valid = {"pending", "in_progress", "completed", "blocked", "cancelled"}
        clean_patch: dict[str, Any] = dict(patch or {})
        if status:
            clean_patch["status"] = status if status in valid else "pending"
        if not self._todos_plan_approved and clean_patch.get("status") == "in_progress":
            clean_patch["status"] = "pending"
        if content:
            clean_patch["content"] = content
        if title:
            clean_patch["title"] = title
        if done_when:
            clean_patch["done_when"] = done_when
        if evidence:
            clean_patch["evidence"] = evidence
        if parent_id:
            clean_patch["parent_id"] = parent_id
        if trigger:
            clean_patch["trigger"] = trigger

        found = self._find_todo_by_id(todo_id)
        if found is not None:
            todo = found[0]
            normalized_patch = self._normalize_todo({"id": todo_id, **todo, **clean_patch})
            children = todo.get("children") if isinstance(todo.get("children"), list) else []
            todo.clear()
            todo.update(normalized_patch)
            if children and "children" not in clean_patch:
                todo["children"] = children
        else:
            self.todos.append(
                self._normalize_todo(
                    {
                        "id": todo_id,
                        "title": title,
                        "content": content or todo_id,
                        "status": clean_patch.get("status") or "pending",
                        "done_when": done_when,
                        "evidence": evidence,
                        "parent_id": parent_id,
                        "trigger": trigger,
                        **clean_patch,
                    }
                )
            )
        active_id = todo_id if clean_patch.get("status") == "in_progress" else ""
        self.todos = self._enforce_single_in_progress(self.todos, active_id=active_id)
        self._todos_touched_this_turn = True
        return self.list_todos()


    def list_todos(self) -> dict[str, Any]:
        flat = self._flatten_todos(self.todos)
        result: dict[str, Any] = {
            "ok": True,
            "todos": self.todos,
            "flat": flat,
            "current": self._current_action_todo(self.todos) or {},
            "plan_approved": bool(self._todos_plan_approved or not self.todos),
        }
        if self.todos and not self._todos_plan_approved:
            result["plan_confirmation"] = self._build_todo_plan_confirmation_payload()
        return result


    def complete_todo_with_refs(
        self,
        todo_id: str,
        status: str = "completed",
        finding_refs: list[Any] | None = None,
        evidence_refs: list[Any] | None = None,
        artifact_refs: list[Any] | None = None,
        evidence: str = "",
        blocked_reason: str = "",
    ) -> dict[str, Any]:
        """Attach proof references to a todo and update its status."""

        def merge_refs(existing: Any, incoming: list[Any] | None) -> list[str]:
            values: list[str] = []
            seen: set[str] = set()
            for raw in [*(existing if isinstance(existing, list) else []), *(incoming or [])]:
                value = str(raw or "").strip()
                if value and value not in seen and len(values) < 50:
                    values.append(value[:240])
                    seen.add(value)
            return values

        found = self._find_todo_by_id(todo_id)
        current = found[0] if found else {}
        patch: dict[str, Any] = {
            "finding_refs": merge_refs(current.get("finding_refs"), finding_refs),
            "evidence_refs": merge_refs(current.get("evidence_refs"), evidence_refs),
            "artifact_refs": merge_refs(current.get("artifact_refs"), artifact_refs),
        }
        if evidence:
            patch["evidence"] = evidence
        if blocked_reason:
            patch["blocked_reason"] = blocked_reason
        return self.update_todo(
            todo_id,
            status if status in self._TODO_STATUS_VALUES else "completed",
            patch=patch,
        )


    def create_validation_todo(
        self,
        parent_id: str,
        finding_ref: str,
        asset_ref: str = "",
        title: str = "",
        tool_hint: str = "",
        risk: str = "medium",
        done_when: str = "",
    ) -> dict[str, Any]:
        """Create a standard validation child todo under a phase parent."""
        clean_finding = str(finding_ref or "").strip()
        if not clean_finding:
            return {"ok": False, "error": "finding_ref is required", **self.list_todos()}
        parent = str(parent_id or "").strip()
        todo_id = f"validate.{clean_finding}".replace(" ", "-")[:120]
        item = {
            "id": todo_id,
            "title": title or f"验证 {clean_finding}",
            "content": title or f"复验并记录 {clean_finding} 的可复现证据",
            "status": "pending",
            "kind": "validation",
            "phase": "validation",
            "risk": risk or "medium",
            "target_ref": asset_ref,
            "tool_hint": tool_hint,
            "done_when": done_when or "保存可复现证据或明确不可复现原因，并关联到该 validation todo。",
            "finding_refs": [clean_finding],
        }
        result = self.add_todos([item], parent_id=parent, trigger=f"validate finding {clean_finding}")
        result["validation_todo_id"] = todo_id
        return result


    def assessment_plan_snapshot(self) -> dict[str, Any]:
        """Return a stable query shape for UI and external-tool adapters."""
        listed = self.list_todos()
        unfinished = self._unfinished_todos()
        return {
            "ok": True,
            "type": "assessment_plan_snapshot",
            "todos": listed["todos"],
            "flat": listed["flat"],
            "current": listed["current"],
            "unfinished": unfinished,
            "counts": {
                "total": len(listed["flat"]),
                "unfinished": len(unfinished),
                "pending": sum(1 for item in listed["flat"] if (item.get("status") or "pending") == "pending"),
                "in_progress": sum(1 for item in listed["flat"] if item.get("status") == "in_progress"),
                "completed": sum(1 for item in listed["flat"] if item.get("status") == "completed"),
                "blocked": sum(1 for item in listed["flat"] if item.get("status") == "blocked"),
                "cancelled": sum(1 for item in listed["flat"] if item.get("status") == "cancelled"),
            },
        }


    def clear_todos(self) -> dict[str, Any]:
        self.todos = []
        self._todos_plan_approved = True
        self._todos_touched_this_turn = True
        return {"todos": self.todos, "plan_approved": True}


    def _normalize_tool_names(self, names: Any) -> list[str]:
        values = names if isinstance(names, list) else [names]
        known = set(self.registry._tools)
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            name = str(raw or "").strip()
            if not name or name in seen or name not in known:
                continue
            seen.add(name)
            out.append(name)
        return out


    def configure_auto_archive_tools(self, action: str = "list", tools: list[Any] | None = None) -> dict[str, Any]:
        action = (action or "list").strip().lower()
        if action not in {"list", "add", "remove", "set", "reset"}:
            return {
                "ok": False,
                "error": f"unknown action: {action}",
                "allowed_actions": ["list", "add", "remove", "set", "reset"],
            }

        requested = [str(name or "").strip() for name in (tools or []) if str(name or "").strip()]
        valid_requested = self._normalize_tool_names(requested)
        invalid = [name for name in requested if name not in set(self.registry._tools)]

        if action == "reset":
            self.auto_archive_tools_override = None
        elif action == "set":
            self.auto_archive_tools_override = valid_requested
        elif action == "add":
            base = self.auto_archive_tools_override
            if base is None:
                base = sorted(self._settings_auto_archive_tool_names())
            self.auto_archive_tools_override = self._normalize_tool_names([*base, *valid_requested])
        elif action == "remove":
            base = self.auto_archive_tools_override
            if base is None:
                base = sorted(self._settings_auto_archive_tool_names())
            remove = set(valid_requested)
            self.auto_archive_tools_override = [name for name in self._normalize_tool_names(base) if name not in remove]

        effective = sorted(self._auto_archive_tool_names())
        return {
            "ok": True,
            "action": action,
            "source": "session_override" if self.auto_archive_tools_override is not None else "global_settings",
            "auto_archive_tools": effective,
            "count": len(effective),
            "invalid_tools": invalid,
        }


    def _record_unattended_decision(self, payload: dict[str, Any]) -> None:
        """Keep automatic cron/child choices visible in the audit timeline."""
        timestamp = time.time()
        self.events.append({
            "id": f"evt_{int(timestamp * 1000)}_{len(self.events)}",
            "kind": "unattended_decision",
            "source": str(getattr(self, "_current_turn_source", "") or "runtime"),
            "render_policy": "audit",
            "context_policy": "exclude",
            "turn_id": "",
            "timestamp": timestamp,
            "payload": {
                **dict(payload or {}),
                "source_ref": str(
                    getattr(self, "_current_turn_source_ref", "") or ""
                ),
            },
        })
        if len(self.events) > 2000:
            del self.events[:-2000]

    def _register_team_room_pass_tool(self) -> None:
        """Standing-team members can end a turn without posting to the room."""
        if self.registry.get("team_room_pass") is not None:
            return
        self.registry.register(
            "team_room_pass",
            (
                "Stay silent in the Team Room. Call this as your only action when the latest "
                "group message is not your assignment: it does not @ your exact role, it is not "
                "a private message to you, and you have no new evidence, correction, or concrete "
                "handoff. Do not write a chat reply before or after this call. "
                "Private messages and explicit @mentions of your role must be answered instead."
            ),
            {
                "reason": {
                    "type": "string",
                    "description": "One short internal reason. It is not posted to the room.",
                    "default": "",
                },
            },
            lambda reason="": self.team_room_pass(str(reason or "")),
            risk="low",
            kind="control",
            dedupe=False,
            concurrency="serial",
        )

    def team_room_pass(self, reason: str = "") -> dict[str, Any]:
        self._team_room_passed = True
        return {
            "ok": True,
            "silent": True,
            "reason": str(reason or "").strip()[:240],
            "hint": "Turn ends. Do not write a Team Room message.",
        }

    def stop_for_user_decision(
        self,
        question: str,
        options: list[Any] | None = None,
        context: str = "",
        input_placeholder: str = "",
        input_required: bool = False,
        recommended: str = "",
    ) -> dict[str, Any]:
        """Tool callback: model has decided it cannot continue without input.

        Side effects:
        * Sets the session-level flag so ``_should_auto_continue`` returns
          False for the rest of the current user turn, regardless of how the
          todo list looks.
        * Returns a structured payload that ``_blocks_for_tool_trace``
          renders as a prominent "等你回应" card on the frontend.
        """
        clean_question = (question or "").strip()
        # Loom22 §9.6: card question is a short control prompt, not the essay.
        if len(clean_question) > 320:
            clean_question = self._extract_decision_card_question(clean_question, max_chars=320)
        clean_options: list[str] = []
        for opt in options or []:
            text = str(opt or "").strip()
            if text and len(clean_options) < 8:
                clean_options.append(text[:160])
        clean_context = (context or "").strip()[:480]
        clean_input_placeholder = (input_placeholder or "").strip()[:240]
        clean_recommended = (recommended or "").strip()[:160]
        if getattr(self, "_should_agent_self_decide", lambda: False)():
            self._record_unattended_decision({
                "kind": "single",
                "question": clean_question,
                "chosen": "",
                "reason": "agent_self_decide",
                "options": clean_options,
            })
            return {
                "ok": True,
                "stopped": False,
                "auto_continued": True,
                "agent_must_choose": True,
                "question": clean_question,
                "options": clean_options,
                "recommended": clean_recommended,
                "context": clean_context,
                "input_placeholder": clean_input_placeholder,
                "input_required": False,
                "hint": (
                    "自动审批已开启：不要等待用户，也不要再调用 stop_for_user_decision。"
                    "请根据当前证据自行选定一项，在回复里写明选择，然后立刻继续执行。"
                    "不要因为某项排在第一就选它，除非那就是你的判断。"
                ),
            }
        # Cron ticks and child Agents are unattended — never hard-stop for a UI card.
        unattended = getattr(self, "_is_unattended_runtime", lambda: False)()
        if unattended:
            if clean_recommended and clean_recommended in clean_options:
                choice = clean_recommended
                reason = "explicit_recommended"
            elif clean_options:
                # The model authored the option ordering; first is its bounded
                # best-judgement fallback when no explicit default was supplied.
                choice = clean_options[0]
                reason = "agent_ordered_first"
            else:
                choice = "继续"
                reason = "safe_continue_fallback"
            source = getattr(self, "_unattended_source_label", lambda: "Auto-approve")()
            self._record_unattended_decision({
                "kind": "single",
                "question": clean_question,
                "chosen": choice,
                "reason": reason,
                "options": clean_options,
            })
            return {
                "ok": True,
                "stopped": False,
                "auto_continued": True,
                "question": clean_question,
                "options": clean_options,
                "chosen": choice,
                "selection_reason": reason,
                "context": clean_context,
                "input_placeholder": clean_input_placeholder,
                "input_required": False,
                "hint": (
                    f"{source} auto-continued without waiting for a user decision. "
                    f"Assumed choice: {choice}"
                ),
            }
        self._user_decision_requested = True
        return {
            "ok": True,
            "stopped": True,
            "question": clean_question,
            "options": clean_options,
            "context": clean_context,
            "input_placeholder": clean_input_placeholder,
            "input_required": bool(input_required),
                "recommended": clean_recommended,
            "hint": (
                "Card fields must stay short: question ≤ ~2 sentences, options = "
                "concrete choices. Put long explanations only in assistant prose, "
                "not in question/context."
            ),
        }


    def stop_for_user_decision_batch(
        self,
        title: str,
        questions: list[Any] | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        """Tool callback: model needs several user choices before continuing.

        This is intentionally separate from ``stop_for_user_decision`` so old
        session replay and tool schemas remain stable. The frontend renders one
        card with multiple sub-questions and submits all answers in a single
        hidden user message.
        """
        clean_title = (title or "").strip()[:160] or "请确认以下选择"
        clean_context = (context or "").strip()[:1600]
        clean_questions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, raw in enumerate(questions or []):
            if not isinstance(raw, dict):
                continue
            q_text = str(raw.get("question") or "").strip()[:240]
            if not q_text:
                continue
            raw_id = str(raw.get("id") or "").strip().lower()
            qid = re.sub(r"[^a-z0-9_-]+", "_", raw_id)[:48].strip("_") or f"q{idx + 1}"
            if qid in seen_ids:
                suffix = 2
                base = qid[:42] or "q"
                while f"{base}_{suffix}" in seen_ids:
                    suffix += 1
                qid = f"{base}_{suffix}"
            seen_ids.add(qid)
            opts: list[str] = []
            for opt in raw.get("options") or []:
                text = str(opt or "").strip()
                if text and len(opts) < 8:
                    opts.append(text[:160])
            clean_questions.append({
                "id": qid,
                "question": q_text,
                "options": opts[:8],
                "required": bool(raw.get("required", True)),
                "context": str(raw.get("context") or "").strip()[:600],
                "recommended": str(raw.get("recommended") or "").strip()[:160],
            })
            if len(clean_questions) >= 8:
                break
        if not clean_questions:
            # Preserve a useful hard stop even when the model supplied a bad
            # batch payload; the user can still answer in free text.
            clean_questions.append({
                "id": "q1",
                "question": clean_title,
                "options": [],
                "required": True,
                "context": "",
            })
        if getattr(self, "_should_agent_self_decide", lambda: False)():
            self._record_unattended_decision({
                "kind": "batch",
                "title": clean_title,
                "answers": {},
                "reasons": {str(q.get("id") or ""): "agent_self_decide" for q in clean_questions},
            })
            return {
                "ok": True,
                "stopped": False,
                "auto_continued": True,
                "agent_must_choose": True,
                "title": clean_title,
                "questions": clean_questions,
                "answers": {},
                "context": clean_context,
                "hint": (
                    "自动审批已开启：不要等待用户。请根据证据自行回答每一个问题，"
                    "写明选择后立刻继续执行。不要默认选每题第一项。"
                ),
            }
        unattended = getattr(self, "_is_unattended_runtime", lambda: False)()
        if unattended:
            answers: dict[str, str] = {}
            reasons: dict[str, str] = {}
            for q in clean_questions:
                qid = str(q.get("id") or "")
                opts = list(q.get("options") or [])
                recommended = str(q.get("recommended") or "")
                if recommended and recommended in opts:
                    answers[qid] = recommended
                    reasons[qid] = "explicit_recommended"
                elif opts:
                    answers[qid] = str(opts[0])
                    reasons[qid] = "agent_ordered_first"
                else:
                    answers[qid] = "继续"
                    reasons[qid] = "safe_continue_fallback"
            source = getattr(self, "_unattended_source_label", lambda: "Auto-approve")()
            self._record_unattended_decision({
                "kind": "batch",
                "title": clean_title,
                "answers": answers,
                "reasons": reasons,
            })
            return {
                "ok": True,
                "stopped": False,
                "auto_continued": True,
                "title": clean_title,
                "questions": clean_questions,
                "answers": answers,
                "selection_reasons": reasons,
                "context": clean_context,
                "hint": f"{source} auto-continued batch user decision without waiting.",
            }
        self._user_decision_requested = True
        return {
            "ok": True,
            "stopped": True,
            "title": clean_title,
            "questions": clean_questions,
            "context": clean_context,
        }

    def background_job_status_tool(self, job_ref: str = "") -> dict[str, Any]:
        store = getattr(self, "_background_jobs", None)
        if store is None:
            return {"ok": False, "error": "no background jobs in this session", "job_ref": job_ref}
        return store.status(job_ref)

    def background_job_list_tool(self, limit: int = 20) -> dict[str, Any]:
        store = getattr(self, "_background_jobs", None)
        if store is None:
            return {"ok": True, "count": 0, "jobs": []}
        return store.list_jobs(limit=limit)

    def background_job_cancel_tool(self, job_ref: str = "") -> dict[str, Any]:
        store = getattr(self, "_background_jobs", None)
        if store is None:
            return {"ok": False, "error": "no background jobs in this session", "job_ref": job_ref}
        return store.cancel(job_ref)

    async def wait_for_duration_tool(
        self,
        seconds: float = 0,
        reason: str = "",
    ) -> dict[str, Any]:
        """Cancellable short wait; long-lived scheduling belongs to cron."""
        try:
            requested = float(seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "seconds must be a number"}
        if not 0 <= requested <= 300:
            return {"ok": False, "error": "seconds must be between 0 and 300"}
        started = time.monotonic()
        deadline = started + requested
        while True:
            if bool(getattr(self, "_cancel_requested", False)):
                return {
                    "ok": False,
                    "status": "cancelled",
                    "waited_seconds": round(time.monotonic() - started, 3),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.25, remaining))
        return {
            "ok": True,
            "requested_seconds": requested,
            "waited_seconds": round(time.monotonic() - started, 3),
            "reason": str(reason or "")[:200],
        }

    async def run_subagent_tool(
        self,
        role: str = "",
        objective: str = "",
        context: str = "",
        expected_output: str = "",
        permission: str = "readonly",
        mode: str = "sync",
        scope_ref: str = "",
        requested_capabilities: list[Any] | None = None,
    ) -> dict[str, Any]:
        from .subagent import run_subagent

        caps = requested_capabilities if isinstance(requested_capabilities, list) else []
        return await run_subagent(
            self,
            role=str(role or ""),
            objective=str(objective or ""),
            context=str(context or ""),
            expected_output=str(expected_output or ""),
            permission=str(permission or "readonly"),
            mode=str(mode or "sync"),
            scope_ref=str(scope_ref or ""),
            requested_capabilities=[str(c) for c in caps],
        )

    def spawn_agent_tool(
        self,
        role: str = "",
        objective: str = "",
        context: str = "",
        expected_output: str = "",
        permission: str = "readonly",
        scope_ref: str = "",
        requested_tools: list[Any] | None = None,
        model: str = "",
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        tools = requested_tools if isinstance(requested_tools, list) else []
        return get_agent_thread_manager(self).spawn(
            role=str(role or ""),
            objective=str(objective or ""),
            context=str(context or ""),
            expected_output=str(expected_output or ""),
            permission=str(permission or "readonly"),
            scope_ref=str(scope_ref or ""),
            requested_tools=[str(item) for item in tools],
            model=str(model or ""),
            thinking_enabled=thinking_enabled if isinstance(thinking_enabled, bool) else None,
        )

    def spawn_team_tool(
        self,
        objective: str = "",
        preset: str = "",
        team: str = "",
        roles: list[Any] | None = None,
        permission: str = "readonly",
        scope_ref: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        role_list = [r for r in (roles or []) if isinstance(r, dict)]
        return get_agent_thread_manager(self).spawn_team(
            team=str(team or ""),
            preset=str(preset or ""),
            objective=str(objective or ""),
            roles=role_list or None,
            permission=str(permission or "readonly"),
            scope_ref=str(scope_ref or ""),
            context=str(context or ""),
        )

    def spawn_external_agent_tool(
        self,
        runtime_id: str = "",
        role: str = "",
        objective: str = "",
        context: str = "",
        expected_output: str = "",
        permission: str = "readonly",
        scope_ref: str = "",
        requested_tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        tools = requested_tools if isinstance(requested_tools, list) else []
        return get_agent_thread_manager(self).spawn_external(
            runtime_id=str(runtime_id or ""),
            role=str(role or ""),
            objective=str(objective or ""),
            context=str(context or ""),
            expected_output=str(expected_output or ""),
            permission=str(permission or "readonly"),
            scope_ref=str(scope_ref or ""),
            requested_tools=[str(item) for item in tools],
        )

    def send_agent_input_tool(self, agent_id: str = "", message: str = "") -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return get_agent_thread_manager(self).send_input(
            str(agent_id or ""),
            str(message or ""),
        )

    def resume_agent_tool(self, agent_id: str = "", message: str = "") -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return get_agent_thread_manager(self).send_input(
            str(agent_id or ""),
            str(message or ""),
            resume=True,
        )

    def pause_agent_tool(self, agent_id: str = "") -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return get_agent_thread_manager(self).pause(str(agent_id or ""))

    def _retire_agent_notices_seen_in_tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Retire completion notices the parent just read out of a tool result.

        Only for the parent-LLM tool handlers: these projections already carry
        the terminal status and summary, so a queued notice would repeat them
        and buy a pointless wake turn. The GUI's own polling goes through
        ``Api.list_assistant_agents`` and deliberately does not retire anything.
        """
        from .agent_threads import _PARENT_NOTICE_STATES, get_agent_thread_manager

        agents = result.get("agents")
        if not isinstance(agents, list):
            single = result.get("agent")
            agents = [single] if isinstance(single, dict) else []
        seen = [
            str(item.get("agent_id") or "")
            for item in agents
            if isinstance(item, dict) and str(item.get("status") or "") in _PARENT_NOTICE_STATES
        ]
        if seen:
            try:
                get_agent_thread_manager(self).mark_parent_notices_reported(seen)
            except Exception:
                log.debug("retiring child-Agent notices from tool result failed", exc_info=True)
        return result

    def wait_for_agents_tool(
        self,
        agent_ids: list[Any] | None = None,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        ids = agent_ids if isinstance(agent_ids, list) else []

        def _should_interrupt() -> bool:
            # The user typed mid-wait (pause) or cancelled: stop blocking so the
            # parent can respond. Children keep running untouched.
            return bool(
                getattr(self, "_pause_after_current_response", False)
                or getattr(self, "_cancel_requested", False)
            )

        result = self._retire_agent_notices_seen_in_tool_result(
            get_agent_thread_manager(self).wait(
                [str(item) for item in ids],
                timeout_seconds=float(timeout_seconds or 0),
                should_interrupt=_should_interrupt,
            )
        )
        if result.get("interrupted_by_user"):
            result["note"] = (
                "停止等待：用户正在对话。子 Agent 仍在后台运行，未受影响；"
                "先回应用户，之后可再次 wait_for_agents 或 list_agents 查看进度。"
            )
        return result

    def list_agents_tool(self, include_closed: bool = False) -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return self._retire_agent_notices_seen_in_tool_result(
            get_agent_thread_manager(self).list(include_closed=bool(include_closed))
        )

    def get_agent_tool(self, agent_id: str = "") -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return self._retire_agent_notices_seen_in_tool_result(
            get_agent_thread_manager(self).get(str(agent_id or ""), include_detail=True)
        )

    def close_agent_tool(self, agent_id: str = "") -> dict[str, Any]:
        from .agent_threads import get_agent_thread_manager

        return get_agent_thread_manager(self).close(str(agent_id or ""))

