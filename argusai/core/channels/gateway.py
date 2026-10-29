"""Allowlisted command gateway from channel events to assistant turns."""

from __future__ import annotations

import re
import threading
from typing import Any, Callable

from argusai.core.token_usage_display import token_usage_status_rows

from .cards import batch_response_text, project_batch_group_card, project_channel_cards
from .i18n import BILINGUAL, DEFAULT_LOCALE, Frag, channel_text, normalize_locale
from .models import ChannelEvent, ChannelIdentity, ChannelReply, infer_chat_kind
from .slash import INVISIBLE_SPACE, THINK_ACTIONS, match_slash_names, think_action, toggle_action
from .store import ChannelStore

TurnHandler = Callable[[dict[str, Any]], dict[str, Any]]
SessionHandler = Callable[[str], dict[str, Any]]
CancelHandler = Callable[[str], dict[str, Any]]
NewSessionHandler = Callable[[ChannelIdentity], str]
ConfirmationHandler = Callable[[str, dict[str, Any], str], dict[str, Any]]
SessionOptionHandler = Callable[[str, str, str], dict[str, Any]]
SessionListHandler = Callable[[], Any]
SessionFileHandler = Callable[[str, str], dict[str, Any]]
SideQuestionHandler = Callable[[str, str], dict[str, Any]]
EndpointHandler = Callable[[str, dict[str, Any]], dict[str, Any]]
AgentRuntimeHandler = Callable[[str, dict[str, Any]], dict[str, Any]]
LocaleProvider = Callable[[], str]
OutboundWake = Callable[[], None]

_PAIR_RE = re.compile(r"^/pair(?:\s+)(\d{6})$", re.IGNORECASE)
_DECISION_RE = re.compile(r"^/(allow|deny)(?:\s+([A-Za-z0-9._:-]+))?$", re.IGNORECASE)
_ACTION_RE = re.compile(r"^/act(?:\s+)(ca_[A-Za-z0-9_-]{10,60})$", re.IGNORECASE)
_USE_RE = re.compile(r"^/use(?:\s+)([A-Za-z0-9._:-]+)$", re.IGNORECASE)
_FILE_RE = re.compile(r"^/file(?:\s+(.+))?$", re.IGNORECASE)
_LANG_RE = re.compile(r"^/lang(?:\s+([A-Za-z+-]{2,8}))?$", re.IGNORECASE)
_MODEL_RE = re.compile(r"^/(?:model|endpoint)(?:\s+(.+))?$", re.IGNORECASE)
_AGENT_RE = re.compile(r"^/agents?$", re.IGNORECASE)
_NEW_RE = re.compile(r"^/new(?:\s+(.+))?$", re.IGNORECASE)
_BTW_RE = re.compile(r"^/(?:btw|side)(?:\s+([\s\S]+))?$", re.IGNORECASE)
_SESSION_PAGE_SIZE = 5
_FILE_PAGE_SIZE = 5
# Feishu/Lark rewrite @mentions to ``@_user_1`` placeholders in text content.
_MENTION_TOKEN_RE = re.compile(r"@_user_\d+")


def _normalize_inbound_text(text: str) -> str:
    """Strip provider mention placeholders so ``@Bot /status`` stays a command."""
    cleaned = _MENTION_TOKEN_RE.sub(" ", str(text or ""))
    cleaned = cleaned.translate(INVISIBLE_SPACE)
    return re.sub(r"\s+", " ", cleaned).strip()

#: ``/help`` rows, grouped. The command column is deliberately not translated:
#: users type these tokens as-is whatever language they read.
_HELP_LAYOUT: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("help_group_conversation", (
        ("/btw <question>", "help_btw"),
        ("/new [agent]", "help_new"),
        ("/agent", "help_agent"),
        ("/sessions [page|search kw]", "help_sessions"),
        ("/use <number|id>", "help_use"),
        ("/status", "help_status"),
    )),
    ("help_group_run", (
        ("/tools", "help_tools"),
        ("/allow [call-id|pending|all|off]", "help_allow"),
        ("/deny [call-id|all]", "help_deny"),
        ("/act <token>", "help_act"),
        ("/cancel", "help_cancel"),
        ("/greenlight [on|off|status]", "help_greenlight"),
        ("/live [on|off|status]", "help_live"),
    )),
    ("help_group_config", (
        ("/model [number|name|refresh]", "help_model"),
        ("/search [on|off|status]", "help_search"),
        ("/think [on|off|status|effort]", "help_think"),
        ("/lang [zh|en|both]", "help_lang"),
        ("/file [list|search kw|number|path]", "help_file"),
    )),
)


class ChannelGateway:
    """Security boundary shared by every messaging transport."""

    def __init__(
        self,
        store: ChannelStore,
        *,
        turn_handler: TurnHandler,
        session_handler: SessionHandler,
        cancel_handler: CancelHandler,
        new_session_handler: NewSessionHandler,
        confirmation_handler: ConfirmationHandler,
        session_option_handler: SessionOptionHandler | None = None,
        session_list_handler: SessionListHandler | None = None,
        session_file_handler: SessionFileHandler | None = None,
        side_question_handler: SideQuestionHandler | None = None,
        endpoint_handler: EndpointHandler | None = None,
        agent_runtime_handler: AgentRuntimeHandler | None = None,
        locale_provider: LocaleProvider | None = None,
        default_locale: str = DEFAULT_LOCALE,
        on_outbound: OutboundWake | None = None,
    ) -> None:
        self.store = store
        self.turn_handler = turn_handler
        self.session_handler = session_handler
        self.cancel_handler = cancel_handler
        self.new_session_handler = new_session_handler
        self.confirmation_handler = confirmation_handler
        self.session_option_handler = session_option_handler
        self.session_list_handler = session_list_handler
        self.session_file_handler = session_file_handler
        self.side_question_handler = side_question_handler
        self.endpoint_handler = endpoint_handler
        self.agent_runtime_handler = agent_runtime_handler
        self.locale_provider = locale_provider
        self.default_locale = normalize_locale(default_locale)
        self.on_outbound = on_outbound
        self._active = threading.local()

    def _enqueue_outbound(self, **kwargs: Any) -> dict[str, Any]:
        """Persist one outbox job and wake the runtime worker when present."""
        job = self.store.enqueue_outbound(**kwargs)
        if self.on_outbound is not None:
            try:
                self.on_outbound()
            except Exception:
                pass
        return job

    def locale(self) -> str:
        """Return the language this reply should be written in.

        A paired user's own ``/lang`` choice wins; otherwise the host decides,
        and channels fall back to the configured default.
        """
        chosen = str(getattr(self._active, "locale", "") or "")
        if chosen:
            return normalize_locale(chosen)
        if self.locale_provider is not None:
            try:
                return normalize_locale(self.locale_provider())
            except Exception:
                return self.default_locale
        return self.default_locale

    def _use_locale(self, identity: ChannelIdentity | None) -> None:
        self._active.locale = str(getattr(identity, "locale", "") or "")

    def _t(self, key: str, **params: Any) -> str:
        return channel_text(key, self.locale(), **params)

    def _card_title(self, key: str, **params: Any) -> str:
        """Card titles become provider headers, which must stay one line."""
        return channel_text(key, self.locale(), inline=True, **params)

    def _error_text(self, exc: Exception) -> str:
        code = str(getattr(exc, "code", "") or "")
        if code:
            try:
                return self._t(code, **dict(getattr(exc, "params", {}) or {}))
            except KeyError:
                pass
        return str(exc)

    def _reply(
        self,
        event: ChannelEvent,
        text: str,
        *,
        blocks: tuple[dict[str, Any], ...] = (),
        attachments: tuple[dict[str, Any], ...] = (),
    ) -> ChannelReply:
        return ChannelReply(
            text=text,
            external_chat_id=event.external_chat_id,
            external_thread_id=event.external_thread_id,
            reply_to_message_id=event.message_id,
            blocks=blocks,
            attachments=attachments,
            locale=self.locale(),
        )

    @staticmethod
    def starts_turn(event: ChannelEvent) -> bool:
        """True when handling this event may occupy the session for minutes.

        Transports serialize these per chat so two messages cannot race into the
        same conversation, and deliberately do *not* serialize anything else:
        ``/cancel`` and ``/btw`` exist to be used while a turn is still running.
        """
        if str(getattr(event, "event_kind", "") or "") == "action":
            return False
        return not str(event.text or "").strip().startswith("/")

    @staticmethod
    def _allowed(identity: ChannelIdentity, permission: str) -> bool:
        return permission in set(identity.permissions)

    _INTERACTIVE_BLOCK_TYPES = frozenset({
        "user_decision",
        "user_decision_batch",
        "confirm",
        "command_plan",
    })

    @staticmethod
    def _block_awaits_user(block: dict[str, Any]) -> bool:
        if not isinstance(block, dict):
            return False
        kind = str(block.get("type") or "")
        if kind not in ChannelGateway._INTERACTIVE_BLOCK_TYPES:
            return False
        if kind in {"user_decision", "user_decision_batch"}:
            return not bool(
                block.get("submitted_response")
                or block.get("superseded")
            )
        return True

    @staticmethod
    def _has_pending_user_decision(status: dict[str, Any]) -> bool:
        for message in reversed(list(status.get("messages") or status.get("history") or [])):
            if not isinstance(message, dict):
                continue
            for block in reversed(list(message.get("blocks") or [])):
                if (
                    isinstance(block, dict)
                    and block.get("type") in {"user_decision", "user_decision_batch"}
                    and ChannelGateway._block_awaits_user(block)
                ):
                    return True
        return False

    @classmethod
    def pending_interaction_blocks(cls, status: dict[str, Any]) -> list[dict[str, Any]]:
        """Return unresolved decision / confirm blocks the channel can act on."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add(block: dict[str, Any]) -> None:
            if not cls._block_awaits_user(block):
                return
            kind = str(block.get("type") or "")
            key = "|".join((
                kind,
                str(block.get("confirm_id") or block.get("pending_id") or ""),
                str(block.get("question") or block.get("title") or "")[:160],
                ",".join(
                    str(opt)[:80]
                    for opt in list(block.get("options") or [])[:8]
                ),
            ))
            if key in seen:
                return
            seen.add(key)
            found.append(dict(block))

        for message in reversed(list(status.get("messages") or status.get("history") or [])):
            if not isinstance(message, dict):
                continue
            open_decisions = [
                block
                for block in list(message.get("blocks") or [])
                if isinstance(block, dict)
                and str(block.get("type") or "") in {"user_decision", "user_decision_batch"}
                and cls._block_awaits_user(block)
            ]
            if not open_decisions:
                continue
            for block in open_decisions:
                _add(block)
            break

        pending_blocks = [
            block
            for block in list(status.get("pending_blocks") or [])
            if isinstance(block, dict)
        ]
        if pending_blocks:
            for block in pending_blocks:
                _add(block)
        else:
            for pending in status.get("pending_tools") or []:
                if not isinstance(pending, dict):
                    continue
                call_id = str(pending.get("id") or "").strip()
                pending_id = str(pending.get("pending_id") or "").strip()
                if not call_id or not pending_id:
                    continue
                _add({
                    "type": "confirm",
                    "title": str(pending.get("name") or "tool"),
                    "name": pending.get("name") or "",
                    "confirm_id": call_id,
                    "pending_id": pending_id,
                    "pending_version": pending.get("version") or 1,
                    "arguments": pending.get("arguments") or {},
                    "risk": pending.get("risk") or "",
                })
        return found

    @staticmethod
    def _session_title(value: Any, fallback: str, *, limit: int = 42) -> str:
        title = str(value or fallback).replace("`", " ")
        title = re.sub(r"<[^>]*>", " ", title)
        title = re.sub(r"^\s*[#>*_-]+\s*", "", title)
        title = re.sub(r"\s+", " ", title).strip(" -:#|/\\")
        if not title:
            title = fallback
        return title if len(title) <= limit else title[:max(1, limit - 1)].rstrip() + "…"

    def _session_rows(
        self,
        identity: ChannelIdentity,
        query: str = "",
    ) -> list[dict[str, Any]]:
        if self.session_list_handler is None:
            return []
        allowed = set(identity.accessible_session_ids())
        if not allowed:
            return []
        try:
            raw = self.session_list_handler()
        except Exception:
            return []
        if isinstance(raw, dict):
            if raw.get("ok") is False:
                return []
            raw = raw.get("sessions") or []
        if not isinstance(raw, list):
            return []
        rows = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("id") or item.get("session_id") or "").strip()
            if not session_id or session_id not in allowed:
                continue
            try:
                # GUI list uses msg_count; detail/status APIs use message_count.
                message_count = int(
                    item.get("message_count")
                    if item.get("message_count") is not None
                    else item.get("msg_count")
                    or 0
                )
            except (TypeError, ValueError):
                message_count = 0
            search_name = self._session_title(
                item.get("name"),
                session_id,
                limit=240,
            )
            rows.append({
                "id": session_id,
                "name": self._session_title(search_name, session_id),
                "search_name": search_name,
                "message_count": message_count,
                "ordinal": len(rows) + 1,
            })
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip().lower()
        if clean_query:
            rows = [
                row for row in rows
                if clean_query in f"{row['search_name']} {row['id']}".lower()
            ]
        return rows[:500]

    def _sessions_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        *,
        page: int = 1,
        query: str = "",
    ) -> ChannelReply:
        rows = self._session_rows(identity, query)
        if not rows:
            return self._reply(
                event,
                (
                    self._t("no_sessions_match", query=query)
                    if query
                    else self._t("no_sessions")
                ),
            )
        page_count = max(1, (len(rows) + _SESSION_PAGE_SIZE - 1) // _SESSION_PAGE_SIZE)
        safe_page = max(1, min(int(page or 1), page_count))
        start = (safe_page - 1) * _SESSION_PAGE_SIZE
        visible = rows[start:start + _SESSION_PAGE_SIZE]
        actions = []
        lines = [
            self._card_title(
                "sessions_title_query",
                page=safe_page,
                page_count=page_count,
                query=query,
            )
            if query
            else self._card_title(
                "sessions_title",
                page=safe_page,
                page_count=page_count,
            )
        ]
        for row in visible:
            current = row["id"] == identity.bound_session_id
            lines.append(self._t(
                "sessions_row",
                ordinal=row["ordinal"],
                name=row["name"],
                count=row["message_count"],
                marker=Frag("sessions_current_marker") if current else "",
            ))
            if not current:
                token = self.store.issue_action(
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    session_id=identity.bound_session_id,
                    payload={
                        "kind": "switch_session",
                        "target_session_id": row["id"],
                    },
                )
                # Ordinal-only labels: the body already lists full titles, and
                # long button captions stack into huge duplicate rows on Telegram
                # / Feishu.
                actions.append({
                    "label": self._t("list_pick_label", ordinal=row["ordinal"]),
                    "token": token,
                    "style": "default",
                })
        for label, target_page in (
            (self._t("sessions_previous"), safe_page - 1),
            (self._t("sessions_next"), safe_page + 1),
        ):
            if 1 <= target_page <= page_count:
                token = self.store.issue_action(
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    session_id=identity.bound_session_id,
                    payload={
                        "kind": "sessions_page",
                        "page": target_page,
                        "query": query,
                    },
                )
                actions.append({
                    "label": label,
                    "token": token,
                    "style": "primary",
                })
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": lines[0],
            "sections": [{"title": "", "rows": lines[1:]}],
            "footer": self._t("sessions_hint"),
            "actions": actions,
        },))

    def _status_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
    ) -> ChannelReply:
        """Render the bound conversation as a compact overview card."""
        status = self.session_handler(identity.bound_session_id)
        if not status.get("ok"):
            return self._reply(
                event,
                str(status.get("error") or self._t("conversation_unavailable")),
            )
        session_id = str(
            status.get("session_id") or identity.bound_session_id or ""
        ).strip()
        name = self._session_title(
            status.get("name"),
            session_id or self._t("status_overview_title"),
            limit=60,
        )
        pending = [
            item for item in list(status.get("pending_tools") or [])
            if isinstance(item, dict)
        ]
        pending_names = ", ".join(
            str(item.get("name") or "tool")[:40] for item in pending[:4]
        )
        if len(pending) > 4:
            pending_names = f"{pending_names}, …"
        todos = list(status.get("todos") or [])
        auto_allow = list(status.get("auto_allow_tools") or [])
        try:
            message_count = int(status.get("message_count") or 0)
        except (TypeError, ValueError):
            message_count = 0
        try:
            memory_count = int(status.get("memory_count") or 0)
        except (TypeError, ValueError):
            memory_count = 0
        runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
        runtime_id = str(runtime.get("runtime_id") or "argusai-native").strip()
        agent_name = (
            "ArgusAI"
            if runtime_id in {"", "argusai-native", "native"}
            else runtime_id
        )
        declaration = status.get("engagement_declaration")
        greenlight = isinstance(declaration, dict) and bool(declaration.get("active"))
        scope = "*"
        if greenlight:
            raw_scope = declaration.get("scope") if isinstance(declaration, dict) else ""
            if isinstance(raw_scope, (list, tuple)):
                scope = ", ".join(str(item) for item in raw_scope[:4] if str(item).strip()) or "*"
            else:
                scope = str(raw_scope or "*").strip() or "*"
            scope = scope if len(scope) <= 48 else scope[:47].rstrip() + "…"
        note = str(status.get("note") or "").strip()
        rows = [
            self._t("status_row_name", name=name),
            self._t("status_row_id", session_id=session_id),
            self._t("status_row_messages", count=message_count),
            self._t("status_row_agent", name=agent_name[:60]),
            self._t(
                "status_row_running"
                if status.get("active_stream")
                else "status_row_idle"
            ),
            self._t("status_row_greenlight_on", scope=scope)
            if greenlight
            else self._t("status_row_greenlight_off"),
            self._t(
                "status_row_search_on"
                if bool(status.get("allow_web_search", True))
                else "status_row_search_off"
            ),
            self._t(
                "status_row_think_effort",
                effort=str(status.get("thinking_effort") or "high"),
            )
            if bool(status.get("thinking_enabled"))
            else self._t("status_row_think_off"),
            self._t(
                "status_row_live_on"
                if bool(status.get("channel_live"))
                else "status_row_live_off"
            ),
            self._t("status_row_auto_allow", count=len(auto_allow)),
            self._t("status_row_pending", names=pending_names)
            if pending
            else self._t("status_row_pending_none"),
            self._t("status_row_todos", count=len(todos)),
            self._t("status_row_memory", count=memory_count),
        ]
        rows.extend(token_usage_status_rows(status, self._t))
        if note:
            rows.append(self._t(
                "status_row_note",
                note=self._session_title(note, note, limit=80),
            ))
        if self.session_file_handler is not None:
            try:
                files = self.session_file_handler(session_id, "list")
            except Exception:
                files = {}
            if isinstance(files, dict) and files.get("ok"):
                file_rows = list(files.get("files") or [])
                rows.append(
                    self._t("status_row_sandbox", count=len(file_rows))
                    if file_rows
                    else self._t("status_row_sandbox_empty")
                )
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": self._card_title("status_overview_title"),
            "sections": [{"title": "", "rows": rows}],
            "footer": self._t("status_overview_hint"),
            "actions": [],
        },))

    def _switch_session(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        selector: str,
    ) -> ChannelReply:
        rows = self._session_rows(identity)
        selected: dict[str, Any] | None = None
        clean_selector = str(selector or "").strip()
        if clean_selector.isdigit():
            index = int(clean_selector) - 1
            if 0 <= index < len(rows):
                selected = rows[index]
        if selected is None:
            selected = next(
                (row for row in rows if row["id"].lower() == clean_selector.lower()),
                None,
            )
        if selected is None:
            return self._reply(event, self._t("session_not_found"))
        updated = self.store.bind_identity(identity.id, selected["id"])
        return self._reply(
            event,
            self._t(
                "session_switched",
                name=selected["name"],
                session_id=updated.bound_session_id,
            ),
        )

    def _files_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        *,
        page: int = 1,
        query: str = "",
    ) -> ChannelReply:
        """List sandbox files with search, pagination, and download buttons."""
        if self.session_file_handler is None:
            return self._reply(event, self._t("file_unavailable"))
        selector = f"search {query}" if query else "list"
        try:
            result = self.session_file_handler(identity.bound_session_id, selector)
        except Exception as exc:
            return self._reply(event, str(exc) or self._t("file_failed"))
        if not result.get("ok"):
            return self._reply(
                event,
                str(result.get("error") or self._t("file_failed")),
            )
        rows = [
            row for row in list(result.get("files") or [])
            if isinstance(row, dict)
        ]
        if not rows:
            return self._reply(
                event,
                (
                    self._t("file_none_match", query=query)
                    if query
                    else self._t("file_none")
                ),
            )
        page_count = max(1, (len(rows) + _FILE_PAGE_SIZE - 1) // _FILE_PAGE_SIZE)
        safe_page = max(1, min(int(page or 1), page_count))
        start = (safe_page - 1) * _FILE_PAGE_SIZE
        visible = rows[start:start + _FILE_PAGE_SIZE]
        actions: list[dict[str, Any]] = []
        title = (
            self._card_title(
                "file_list_title_query",
                page=safe_page,
                page_count=page_count,
                query=query,
            )
            if query
            else self._card_title(
                "file_list_title_paged",
                page=safe_page,
                page_count=page_count,
            )
        )
        listing = []
        for offset, row in enumerate(visible):
            # The handler numbers rows over the unfiltered listing, so a search
            # hit keeps the number that `/file <n>` will resolve.
            ordinal = int(row.get("index") or (start + offset + 1))
            path = str(row.get("relative_path") or row.get("name") or "file")
            listing.append(self._t(
                "file_list_row",
                index=ordinal,
                path=path,
                size=int(row.get("size") or 0),
            ))
            token = self.store.issue_action(
                channel_id=event.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                payload={
                    "kind": "download_file",
                    "relative_path": path,
                },
            )
            actions.append({
                "label": self._t("list_pick_label", ordinal=ordinal),
                "token": token,
                "style": "default",
            })
        for label, target_page in (
            (self._t("files_previous"), safe_page - 1),
            (self._t("files_next"), safe_page + 1),
        ):
            if 1 <= target_page <= page_count:
                token = self.store.issue_action(
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    session_id=identity.bound_session_id,
                    payload={
                        "kind": "files_page",
                        "page": target_page,
                        "query": query,
                    },
                )
                actions.append({
                    "label": label,
                    "token": token,
                    "style": "primary",
                })
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": title,
            "sections": [{"title": "", "rows": listing}],
            "footer": self._t("file_list_hint"),
            "actions": actions,
        },))

    def handle(self, event: ChannelEvent) -> ChannelReply:
        text = _normalize_inbound_text(event.text)
        self._use_locale(None)
        if infer_chat_kind(
            event.provider,
            chat_id=event.external_chat_id,
            raw=event.raw,
            chat_kind=event.chat_kind,
        ) == "group":
            # Direct messages only: pairing, commands, and cards stay 1:1.
            return self._reply(event, "")
        # Mention-only pings / a lone "/" from Feishu's slash palette should not
        # produce a spooky "unknown command" when the user typed nothing useful.
        if (
            event.event_kind != "action"
            and not event.attachments
            and (not text or text == "/")
        ):
            return self._reply(event, "")
        pair_match = _PAIR_RE.fullmatch(text)
        action_match = _ACTION_RE.fullmatch(text)
        use_match = _USE_RE.fullmatch(text)
        identity = self.store.find_identity(
            event.channel_id,
            event.external_user_id,
            event.external_chat_id,
            event.external_thread_id,
        )
        if identity is None and (event.event_kind == "action" or action_match is not None):
            candidates = [
                item for item in self.store.list_identities(event.channel_id)
                if item.external_user_id == event.external_user_id
                and item.external_chat_id == event.external_chat_id
            ]
            if len(candidates) == 1:
                identity = candidates[0]
        if identity is None:
            if pair_match is None:
                return self._reply(event, self._t("not_paired"))
            try:
                paired = self.store.consume_pairing(
                    event.channel_id,
                    pair_match.group(1),
                    external_user_id=event.external_user_id,
                    external_chat_id=event.external_chat_id,
                    external_thread_id=event.external_thread_id,
                    display_name=event.display_name,
                )
            except ValueError as exc:
                return self._reply(event, self._error_text(exc))
            return self._reply(
                event,
                self._t(
                    "pairing_complete_bound",
                    session_id=paired.bound_session_id,
                )
                if paired.bound_session_id
                else self._t("pairing_complete_unbound"),
            )

        try:
            identity = self.store.touch_identity(identity.id)
        except ValueError:
            pass
        self._use_locale(identity)
        lang_match = _LANG_RE.fullmatch(text)
        if lang_match is not None:
            return self._set_language(event, identity, lang_match.group(1) or "")
        if event.event_kind == "action" or action_match is not None:
            return self._handle_action(
                event,
                identity,
                event.action_token or (action_match.group(1) if action_match else ""),
            )
        if text.lower() == "/help":
            return self._reply(event, "", blocks=(self._help_card(),))
        new_match = _NEW_RE.fullmatch(text)
        if new_match is not None:
            if not self._allowed(identity, "chat"):
                return self._reply(event, self._t("create_not_allowed"))
            agent = str(new_match.group(1) or "").strip()[:80]
            if agent:
                return self._create_agent_session(event, identity, {"selector": agent})
            session_id = self.new_session_handler(identity)
            updated = self.store.bind_identity(identity.id, session_id)
            return self._session_created_reply(
                event,
                updated,
                created_text=self._t(
                    "session_created",
                    session_id=updated.bound_session_id,
                ),
            )
        if _AGENT_RE.fullmatch(text) is not None:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("agent_not_allowed"))
            return self._agents_reply(event, identity)
        if text.lower() == "/sessions" or text.lower().startswith("/sessions "):
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("switch_not_allowed"))
            argument = text[len("/sessions"):].strip()
            page = 1
            query = ""
            if argument:
                if argument.isdigit():
                    page = max(1, int(argument))
                elif argument.lower().startswith("search "):
                    query = argument[7:].strip()[:120]
                    if not query:
                        return self._reply(event, self._t("usage_sessions_search"))
                else:
                    return self._reply(event, self._t("usage_sessions"))
            return self._sessions_reply(event, identity, page=page, query=query)
        if use_match is not None:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("switch_not_allowed"))
            return self._switch_session(event, identity, use_match.group(1))
        model_match = _MODEL_RE.fullmatch(text)
        if model_match is not None:
            # Native endpoints are global; ACP models are per bound conversation.
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("endpoint_not_allowed"))
            if self.endpoint_handler is None:
                return self._reply(event, self._t("endpoint_unavailable"))
            selector = str(model_match.group(1) or "").strip()[:120]
            target: dict[str, Any] = {}
            if identity.bound_session_id:
                target["session_id"] = identity.bound_session_id
            if not selector or selector.lower() == "list":
                return self._endpoints_reply(event, identity, target)
            if selector.lower() in {"refresh", "reload"}:
                target["refresh"] = "1"
                return self._endpoints_reply(event, identity, target)
            target["selector"] = selector
            return self._endpoint_prompt(event, identity, target)
        if not identity.bound_session_id:
            return self._reply(event, self._t("no_conversation_bound"))
        btw_match = _BTW_RE.fullmatch(text)
        if btw_match is not None:
            return self._side_question(event, identity, btw_match.group(1) or "")
        file_match = _FILE_RE.fullmatch(text)
        if file_match is not None:
            if not self._allowed(identity, "send"):
                return self._reply(event, self._t("file_not_allowed"))
            if self.session_file_handler is None:
                return self._reply(event, self._t("file_unavailable"))
            selector = str(file_match.group(1) or "").strip()
            low = selector.lower()
            if (
                not selector
                or low == "list"
                or low.startswith("list ")
                or low.startswith("search ")
            ):
                page = 1
                query = ""
                if low.startswith("search "):
                    query = selector[7:].strip()[:120]
                    if not query:
                        return self._reply(event, self._t("usage_file_search"))
                elif low.startswith("list "):
                    rest = selector[5:].strip()
                    if rest.isdigit():
                        page = max(1, int(rest))
                    elif rest.lower().startswith("search "):
                        query = rest[7:].strip()[:120]
                        if not query:
                            return self._reply(event, self._t("usage_file_search"))
                    elif rest:
                        return self._reply(event, self._t("usage_file"))
                return self._files_reply(
                    event,
                    identity,
                    page=page,
                    query=query,
                )
            result = self.session_file_handler(identity.bound_session_id, selector)
            if not result.get("ok"):
                return self._reply(
                    event,
                    str(result.get("error") or self._t("file_failed")),
                )
            attachment = result.get("attachment")
            if isinstance(attachment, dict):
                return self._reply(
                    event,
                    str(result.get("message") or self._t(
                        "file_sending",
                        name=attachment.get("name") or "file",
                    )),
                    attachments=(attachment,),
                )
            return self._reply(event, self._t("file_failed"))
        if text.lower() == "/status":
            if not self._allowed(identity, "status"):
                return self._reply(event, self._t("status_not_allowed"))
            return self._status_reply(event, identity)
        if text.lower() == "/tools":
            if not self._allowed(identity, "status"):
                return self._reply(event, self._t("tools_not_allowed"))
            status = self.session_handler(identity.bound_session_id)
            if not status.get("ok"):
                return self._reply(
                    event,
                    str(status.get("error") or self._t("conversation_unavailable")),
                )
            pending = list(status.get("pending_tools") or [])
            tools = list(status.get("tools") or [])
            detail = (
                Frag(
                    "tools_pending",
                    names=", ".join(
                        str(item.get("name") or "tool") for item in pending[:6]
                    ),
                )
                if pending
                else Frag("tools_no_pending")
            )
            return self._reply(
                event,
                self._t("tools_line", count=len(tools), suffix=detail),
            )
        green_match = match_slash_names(text, ("/greenlight",), glue_toggle=True)
        if green_match is not None:
            resolved = toggle_action(green_match[1])
            if resolved is None:
                return self._reply(event, self._t("usage_greenlight"))
            action, _ = resolved
            if action in {"on", "off"}:
                if not self._allowed(identity, "configure"):
                    return self._reply(event, self._t("greenlight_not_allowed"))
                if self.session_option_handler is None:
                    return self._reply(event, self._t("greenlight_unavailable"))
                result = self.session_option_handler(
                    identity.bound_session_id,
                    "greenlight",
                    action,
                )
                if not result.get("ok"):
                    return self._reply(
                        event,
                        str(result.get("error") or self._t("greenlight_failed")),
                    )
                return self._reply(
                    event,
                    self._t(
                        "greenlight_active"
                        if result.get("enabled")
                        else "greenlight_off"
                    ),
                )
            if not self._allowed(identity, "status"):
                return self._reply(event, self._t("greenlight_not_allowed"))
            status = self.session_handler(identity.bound_session_id)
            declaration = status.get("engagement_declaration")
            active = isinstance(declaration, dict) and bool(declaration.get("active"))
            return self._reply(
                event,
                self._t("greenlight_active" if active else "greenlight_off"),
            )
        live_match = match_slash_names(text, ("/live",), glue_toggle=True)
        if live_match is not None:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("live_not_allowed"))
            if self.session_option_handler is None:
                return self._reply(event, self._t("live_unavailable"))
            resolved = toggle_action(live_match[1])
            if resolved is None:
                return self._reply(event, self._t("usage_live"))
            action, _ = resolved
            result = self.session_option_handler(
                identity.bound_session_id,
                "live",
                action,
            )
            if not result.get("ok"):
                return self._reply(
                    event,
                    str(result.get("error") or self._t("live_failed")),
                )
            return self._reply(
                event,
                self._t("live_on" if result.get("enabled") else "live_off"),
            )
        search_match = match_slash_names(text, ("/search",), glue_toggle=True)
        if search_match is not None:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("search_not_allowed"))
            if self.session_option_handler is None:
                return self._reply(event, self._t("search_unavailable"))
            resolved = toggle_action(search_match[1])
            if resolved is None:
                return self._reply(event, self._t("usage_search"))
            action, _ = resolved
            result = self.session_option_handler(
                identity.bound_session_id,
                "search",
                action,
            )
            return self._reply(
                event,
                str(
                    result.get("message")
                    or result.get("error")
                    or self._t("search_updated")
                ),
            )
        think_match = match_slash_names(
            text,
            ("/think", "/thinking"),
            glue_toggle=True,
            glue_values=THINK_ACTIONS,
        )
        if think_match is not None:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("think_not_allowed"))
            if self.session_option_handler is None:
                return self._reply(event, self._t("think_unavailable"))
            action = think_action(think_match[1])
            if action is None:
                return self._reply(event, self._t("usage_think"))
            result = self.session_option_handler(
                identity.bound_session_id,
                "think",
                action,
            )
            if not result.get("ok"):
                return self._reply(
                    event,
                    str(result.get("error") or self._t("think_unavailable")),
                )
            if result.get("enabled"):
                return self._reply(
                    event,
                    self._t(
                        "think_on_effort",
                        effort=str(result.get("thinking_effort") or "high"),
                    ),
                )
            return self._reply(event, self._t("think_off"))
        if text.lower() == "/cancel":
            if not self._allowed(identity, "cancel"):
                return self._reply(event, self._t("cancel_not_allowed"))
            result = self.cancel_handler(identity.bound_session_id)
            return self._reply(
                event,
                self._t("cancel_requested")
                if result.get("ok")
                else str(result.get("error") or self._t("cancel_failed")),
            )
        decision = _DECISION_RE.fullmatch(text)
        if decision is not None:
            if not self._allowed(identity, "confirm"):
                return self._reply(event, self._t("confirm_not_allowed"))
            requested_call = str(decision.group(2) or "").lower()
            verdict = "allow" if decision.group(1).lower() == "allow" else "deny"
            if verdict == "allow" and requested_call == "all":
                if not self._allowed(identity, "configure"):
                    return self._reply(event, self._t("auto_allow_requires_configure"))
                return self._session_auto_allow_prompt(event, identity)
            if verdict == "allow" and requested_call == "off":
                if not self._allowed(identity, "configure"):
                    return self._reply(event, self._t("session_tools_not_allowed"))
                if self.session_option_handler is None:
                    return self._reply(event, self._t("session_tools_unavailable"))
                result = self.session_option_handler(
                    identity.bound_session_id,
                    "tools",
                    "disable_all",
                )
                return self._reply(
                    event,
                    str(
                        result.get("message")
                        or result.get("error")
                        or self._t("session_tools_disabled")
                    ),
                )
            status = self.session_handler(identity.bound_session_id)
            pending = list(status.get("pending_tools") or [])
            if not pending:
                return self._reply(event, self._t("no_pending_confirmation"))
            selected = pending[0]
            if requested_call == "all" or (verdict == "allow" and requested_call == "pending"):
                action = {
                    "type": "batch_confirmation_decision",
                    "pending_id": str(selected.get("pending_id") or ""),
                    "version": int(selected.get("version") or 0),
                    "decisions": {
                        str(item.get("id") or ""): verdict
                        for item in pending
                        if str(item.get("id") or "")
                    },
                }
            else:
                if requested_call:
                    selected = next(
                        (
                            item for item in pending
                            if str(item.get("id") or "").lower() == requested_call
                        ),
                        selected,
                    )
                action = {
                    "type": "confirmation_decision",
                    "pending_id": str(selected.get("pending_id") or ""),
                    "version": int(selected.get("version") or 0),
                    "call_id": str(selected.get("id") or ""),
                    "verdict": verdict,
                }
            result = self.confirmation_handler(
                identity.bound_session_id,
                action,
                event.provider,
            )
            return self._reply(
                event,
                self._t("decision_forwarded")
                if result.get("ok")
                else str(result.get("error") or self._t("confirmation_failed")),
            )
        if text.startswith("/"):
            command = text.split(None, 1)[0][:40]
            return self._reply(event, self._t("unknown_command", command=command))
        if not self._allowed(identity, "chat"):
            return self._reply(event, self._t("chat_not_allowed"))
        session_status = self.session_handler(identity.bound_session_id)
        # ``queued`` below: the session was mid-turn, so the host asked it to
        # wrap up the current step and will run this message next.
        result = self.turn_handler({
            "session_id": identity.bound_session_id,
            "message": text,
            "attachments": list(event.attachments),
            "hide_user_message": self._has_pending_user_decision(session_status),
            "message_source": "channel",
            "message_source_ref": identity.id,
            "channel": {
                "channel_id": event.channel_id,
                "provider": event.provider,
                "external_user_id": event.external_user_id,
                "message_id": event.message_id,
            },
        })
        if not result.get("ok"):
            return self._reply(
                event,
                str(result.get("error") or self._t("assistant_turn_failed")),
            )
        if result.get("queued"):
            return self._reply(
                event,
                self._t("interrupted" if result.get("interrupted") else "interjected"),
            )
        return self._result_reply(event, identity, result)

    def queue_result_delivery(self, identity_id: str, result: dict[str, Any]) -> None:
        """Send a turn's answer to a channel that is no longer waiting on it.

        An interjection already got its acknowledgement, so when the queued turn
        finally runs there is no inbound request left to answer: the reply has to
        travel out on the outbox instead.
        """
        identity = self.store.get_identity(identity_id)
        self._use_locale(identity)
        event = ChannelEvent(
            channel_id=identity.channel_id,
            provider=identity.provider,
            external_user_id=identity.external_user_id,
            external_chat_id=identity.external_chat_id,
            external_thread_id=identity.external_thread_id,
            message_id="",
            text="",
        )
        reply = (
            self._result_reply(event, identity, result)
            if result.get("ok")
            else self._reply(
                event,
                str(result.get("error") or self._t("assistant_turn_failed")),
            )
        )
        if not str(reply.text or "").strip() and not reply.blocks and not reply.attachments:
            return
        self._enqueue_outbound(
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            text=reply.text,
            blocks=list(reply.blocks),
            attachments=[item for item in reply.attachments if isinstance(item, dict)],
        )

    def queue_live_update(
        self,
        identity_id: str,
        *,
        text: str = "",
        raw_blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    ) -> bool:
        """Push one mid-turn segment or card while ``/live`` is on.

        Returns True only when an outbox job was actually enqueued. Callers must
        treat False as "not delivered" and keep sync-reply fallbacks intact.
        """
        clean_text = str(text or "").strip()
        blocks_in = [
            dict(block)
            for block in list(raw_blocks or [])
            if isinstance(block, dict)
        ]
        if not clean_text and not blocks_in:
            return False
        identity = self.store.get_identity(identity_id)
        # Outbox delivery requires ``send``; chat-only identities still receive
        # sync replies to inbound events, but cannot be pushed proactively.
        if "send" not in identity.permissions:
            return False
        self._use_locale(identity)

        def issue(payload: dict[str, Any]) -> str:
            return self.store.issue_action(
                channel_id=identity.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                payload=payload,
            )

        def create_group(title: str, questions: list[dict[str, Any]]) -> str:
            return self.store.create_action_group(
                channel_id=identity.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                title=title,
                questions=questions,
            )

        cards = project_channel_cards(
            blocks_in,
            issue_action=issue,
            create_batch_group=create_group,
            locale=self.locale(),
        ) if blocks_in else ()
        if not clean_text and not cards:
            return False
        self._enqueue_outbound(
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            text=clean_text,
            blocks=list(cards),
            attachments=[],
        )
        return True

    def _set_language(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        requested: str,
    ) -> ChannelReply:
        wanted = str(requested or "").strip().lower()
        if wanted not in {"zh", "en", BILINGUAL}:
            return self._reply(
                event,
                self._t("lang_usage", current=self.locale()),
            )
        try:
            updated = self.store.set_identity_locale(identity.id, wanted)
        except ValueError as exc:
            return self._reply(event, self._error_text(exc))
        self._use_locale(updated)
        return self._reply(event, self._t("lang_set", current=self.locale()))

    def _help_card(self) -> dict[str, Any]:
        """Render the command reference as a card with one row per command."""
        locale = self.locale()
        return {
            "type": "info",
            "title": channel_text("help_title", locale, inline=True),
            "sections": [
                {
                    "title": channel_text(group, locale, inline=True),
                    "rows": [
                        f"{command} — {channel_text(key, locale, inline=True)}"
                        for command, key in rows
                    ],
                }
                for group, rows in _HELP_LAYOUT
            ],
            "footer": channel_text("help_plain", locale, inline=True),
            "actions": [],
        }

    def _agents_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
    ) -> ChannelReply:
        if self.agent_runtime_handler is None:
            return self._reply(event, self._t("agent_unavailable"))
        result = self.agent_runtime_handler("list", {})
        if not result.get("ok"):
            return self._reply(
                event,
                self._handler_error_text(result, fallback="agent_none"),
            )
        rows = [row for row in list(result.get("agents") or []) if isinstance(row, dict)]
        if not rows:
            return self._reply(event, self._t("agent_none"))
        lines: list[str] = []
        actions: list[dict[str, Any]] = []
        for row in rows:
            lines.append(self._t(
                "agent_row",
                ordinal=row.get("ordinal") or 0,
                name=str(row.get("name") or "")[:60],
                kind=str(row.get("kind") or "")[:20],
                marker=Frag("agent_unavailable_marker") if not row.get("available") else "",
            ))
            if not row.get("available"):
                continue
            actions.append({
                "label": self._t(
                    "list_pick_label",
                    ordinal=row.get("ordinal") or 0,
                ),
                "token": self.store.issue_action(
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    session_id=identity.bound_session_id,
                    payload={
                        "kind": "new_agent_session",
                        "agent_id": str(row.get("id") or "")[:80],
                    },
                ),
                "style": "default",
            })
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": self._card_title("agent_list_title"),
            "sections": [{"title": "", "rows": lines}],
            "footer": self._t("agent_list_hint"),
            "actions": actions,
        },))

    def _create_agent_session(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        target: dict[str, Any],
    ) -> ChannelReply:
        """Start a conversation bound to one local agent, sandboxed by default."""
        if not self._allowed(identity, "configure"):
            return self._reply(event, self._t("agent_not_allowed"))
        if self.agent_runtime_handler is None:
            return self._reply(event, self._t("agent_unavailable"))
        created = self.agent_runtime_handler("create", target)
        if not created.get("ok"):
            return self._reply(
                event,
                self._handler_error_text(created, fallback="agent_create_failed"),
            )
        session_id = str(created.get("session_id") or "")
        if not session_id:
            return self._reply(event, self._t("agent_create_failed"))
        updated = self.store.bind_identity(identity.id, session_id)
        return self._session_created_reply(
            event,
            updated,
            created_text=self._t(
                "agent_session_created",
                name=str(dict(created.get("agent") or {}).get("name") or "")[:60],
                session_id=updated.bound_session_id,
            ),
        )

    def _session_created_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        *,
        created_text: str,
    ) -> ChannelReply:
        """Acknowledge a new conversation and offer one-tap ready-mode toggles."""
        if not (
            self._allowed(identity, "configure")
            and self.session_option_handler is not None
        ):
            return self._reply(event, created_text)
        enable_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload={"kind": "session_ready_mode", "enabled": True},
        )
        skip_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload={"kind": "dismiss"},
        )
        return self._reply(
            event,
            created_text,
            blocks=({
                "type": "info",
                "title": self._card_title("ready_mode_title"),
                "body": self._t("ready_mode_body"),
                "footer": self._t("ready_mode_footer"),
                "actions": [
                    {
                        "label": self._t("ready_mode_enable"),
                        "token": enable_token,
                        "style": "danger",
                    },
                    {
                        "label": self._t("ready_mode_skip"),
                        "token": skip_token,
                        "style": "default",
                    },
                ],
            },),
        )

    def _handler_error_text(
        self,
        result: dict[str, Any],
        *,
        fallback: str = "endpoint_failed",
    ) -> str:
        code = str(result.get("code") or "")
        if code:
            try:
                return self._t(code)
            except KeyError:
                pass
        return str(result.get("error") or self._t(fallback))

    def _endpoints_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        target: dict[str, Any] | None = None,
    ) -> ChannelReply:
        payload = dict(target or {})
        if identity.bound_session_id and "session_id" not in payload:
            payload["session_id"] = identity.bound_session_id
        result = self.endpoint_handler("list", payload)
        if not result.get("ok"):
            return self._reply(event, self._handler_error_text(result))
        kind = str(result.get("kind") or "endpoint")
        rows = [row for row in list(result.get("endpoints") or []) if isinstance(row, dict)]
        if not rows:
            return self._reply(
                event,
                self._t("acp_model_none" if kind == "acp_model" else "endpoint_none"),
            )
        lines: list[str] = []
        actions: list[dict[str, Any]] = []
        for row in rows:
            lines.append(self._t(
                "endpoint_row",
                ordinal=row.get("ordinal") or 0,
                name=str(row.get("name") or "")[:60],
                model=str(row.get("model") or "")[:60],
                marker=Frag("endpoint_current_marker") if row.get("active") else "",
            ))
            if row.get("active"):
                continue
            action_payload: dict[str, Any] = {
                "kind": "endpoint_prompt",
                "index": int(row.get("index") or 0),
                "name": str(row.get("name") or "")[:60],
                # For ACP this is the configOption value; for native endpoints
                # it is the model, since one endpoint now lists several rows.
                "value": str(row.get("value") or "")[:200],
            }
            if kind == "acp_model":
                action_payload["config_id"] = str(row.get("config_id") or "")[:120]
                if identity.bound_session_id:
                    action_payload["session_id"] = identity.bound_session_id
            actions.append({
                "label": self._t(
                    "list_pick_label",
                    ordinal=row.get("ordinal") or 0,
                ),
                "token": self.store.issue_action(
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    session_id=identity.bound_session_id,
                    payload=action_payload,
                ),
                "style": "default",
            })
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": self._card_title(
                "acp_model_list_title" if kind == "acp_model" else "endpoint_list_title"
            ),
            "sections": [{"title": "", "rows": lines}],
            "footer": self._t(
                "acp_model_list_hint" if kind == "acp_model" else "endpoint_list_hint"
            ),
            "actions": actions,
        },))

    def _endpoint_prompt(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        target: dict[str, Any],
    ) -> ChannelReply:
        """Resolve one endpoint/model and ask for a second, explicit confirmation."""
        payload = dict(target)
        if identity.bound_session_id and "session_id" not in payload:
            payload["session_id"] = identity.bound_session_id
        resolved = self.endpoint_handler("resolve", payload)
        if not resolved.get("ok"):
            return self._reply(event, self._handler_error_text(resolved))
        kind = str(resolved.get("kind") or "endpoint")
        endpoint = dict(resolved.get("endpoint") or {})
        if endpoint.get("active"):
            return self._reply(
                event,
                self._t(
                    "acp_model_already_active" if kind == "acp_model" else "endpoint_already_active",
                    name=endpoint.get("name") or "",
                ),
            )
        switch_payload: dict[str, Any] = {
            "kind": "switch_endpoint",
            "index": int(endpoint.get("index") or 0),
            "name": str(endpoint.get("name") or "")[:60],
            "value": str(endpoint.get("value") or "")[:200],
        }
        if kind == "acp_model":
            switch_payload["config_id"] = str(endpoint.get("config_id") or "")[:120]
            if identity.bound_session_id:
                switch_payload["session_id"] = identity.bound_session_id
        switch_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload=switch_payload,
        )
        cancel_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload={"kind": "dismiss"},
        )
        return self._reply(
            event,
            self._t("endpoint_switch_prompt"),
            blocks=({
                "type": "user_decision",
                "title": self._t(
                    "acp_model_switch_title" if kind == "acp_model" else "endpoint_switch_title"
                ),
                "body": self._t(
                    "acp_model_switch_body" if kind == "acp_model" else "endpoint_switch_body",
                    name=str(endpoint.get("name") or "")[:60],
                    model=str(endpoint.get("model") or "")[:60],
                    host=str(endpoint.get("host") or "")[:80],
                ),
                "actions": [
                    {
                        "label": self._t("endpoint_switch_label"),
                        "token": switch_token,
                        "style": "primary",
                    },
                    {
                        "label": self._t("cancel_label"),
                        "token": cancel_token,
                        "style": "default",
                    },
                ],
            },),
        )

    def _session_auto_allow_prompt(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
    ) -> ChannelReply:
        allow_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload={"kind": "session_auto_allow", "enabled": True},
        )
        cancel_token = self.store.issue_action(
            channel_id=event.channel_id,
            identity_id=identity.id,
            session_id=identity.bound_session_id,
            payload={"kind": "dismiss"},
        )
        return self._reply(
            event,
            self._t("auto_allow_prompt"),
            blocks=({
                "type": "user_decision",
                "title": self._t("auto_allow_title"),
                "body": self._t("auto_allow_body"),
                "actions": [
                    {
                        "label": self._t("auto_allow_label"),
                        "token": allow_token,
                        "style": "danger",
                    },
                    {
                        "label": self._t("cancel_label"),
                        "token": cancel_token,
                        "style": "default",
                    },
                ],
            },),
        )

    def _side_question(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        question: str,
    ) -> ChannelReply:
        """Answer one question about the conversation without joining it.

        Runs off the serialization path on purpose: the whole point is to ask
        while the main turn is still working. This path is read-only: it must
        not attach decision/confirm buttons (those belong on the main turn's
        channel delivery).
        """
        if not self._allowed(identity, "chat"):
            return self._reply(event, self._t("chat_not_allowed"))
        clean = str(question or "").strip()[:1000]
        if not clean:
            return self._reply(event, self._t("usage_btw"))
        if self.side_question_handler is None:
            return self._reply(event, self._t("side_unavailable"))
        try:
            result = self.side_question_handler(identity.bound_session_id, clean)
        except Exception as exc:
            return self._reply(event, self._error_text(exc))
        if not result.get("ok"):
            return self._reply(event, str(result.get("error") or self._t("side_failed")))
        answer = str(result.get("text") or "").strip()
        if not answer:
            return self._reply(event, self._t("side_empty"))
        return self._reply(event, "", blocks=({
            "type": "info",
            "title": self._card_title("side_title"),
            "body": answer,
            "footer": self._t("side_footer"),
            "actions": [],
        },))

    def _project_interaction_cards(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        raw_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        can_confirm = self._allowed(identity, "confirm")
        can_decide = self._allowed(identity, "chat")
        filtered: list[dict[str, Any]] = []
        denied_notices: list[dict[str, Any]] = []
        for raw in raw_blocks:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "")
            if kind in {"confirm", "command_plan"} and not can_confirm:
                # Notify without dead Allow/Deny buttons that later refuse.
                name = str(raw.get("name") or raw.get("title") or "tool")[:80]
                denied_notices.append({
                    "type": "info",
                    "title": self._card_title("waiting_confirmation_title"),
                    "sections": [{
                        "title": "",
                        "rows": [self._t("confirm_not_allowed_hint", name=name)],
                    }],
                    "footer": "",
                    "actions": [],
                })
                continue
            if kind in {"user_decision", "user_decision_batch"} and not can_decide:
                continue
            filtered.append(dict(raw))

        def issue(payload: dict[str, Any]) -> str:
            return self.store.issue_action(
                channel_id=event.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                payload=payload,
            )

        def create_group(title: str, questions: list[dict[str, Any]]) -> str:
            return self.store.create_action_group(
                channel_id=event.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                title=title,
                questions=questions,
            )

        cards = list(project_channel_cards(
            filtered,
            issue_action=issue,
            create_batch_group=create_group,
            locale=self.locale(),
        ))
        cards.extend(denied_notices)
        return cards

    def push_session_interaction(
        self,
        session_id: str,
        raw_blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        *,
        skip_identity_id: str = "",
        text: str = "",
    ) -> int:
        """Push unresolved decision/confirm cards to identities bound to a session.

        Desktop-originated turns never go through ``_result_reply``, so without
        this fan-out a Feishu/Telegram user only learns about the wait via
        ``/btw`` prose and never receives clickable options.
        """
        safe_session = str(session_id or "").strip()
        if not safe_session:
            return 0
        # Keep caller order: todo/plan context may precede the wait card.
        context_types = {"todo_list"}
        blocks_in = [
            dict(block)
            for block in list(raw_blocks or [])
            if isinstance(block, dict)
            and (
                self._block_awaits_user(block)
                or str(block.get("type") or "") in context_types
            )
        ]
        if not any(self._block_awaits_user(block) for block in blocks_in):
            try:
                status = self.session_handler(safe_session)
            except Exception:
                status = {}
            if isinstance(status, dict) and status.get("ok"):
                blocks_in = self.pending_interaction_blocks(status)
        if not any(self._block_awaits_user(block) for block in blocks_in):
            return 0
        skip = str(skip_identity_id or "").strip()
        delivered = 0
        for identity in self.store.list_identities_for_session(safe_session):
            if skip and identity.id == skip:
                continue
            if "send" not in identity.permissions:
                continue
            self._use_locale(identity)
            event = ChannelEvent(
                channel_id=identity.channel_id,
                provider=identity.provider,
                external_user_id=identity.external_user_id,
                external_chat_id=identity.external_chat_id,
                external_thread_id=identity.external_thread_id,
                message_id="",
                text="",
            )
            cards = self._project_interaction_cards(event, identity, blocks_in)
            if not cards:
                continue
            try:
                self._enqueue_outbound(
                    identity_id=identity.id,
                    session_id=safe_session,
                    text=str(text or "").strip(),
                    blocks=list(cards),
                    attachments=[],
                )
            except ValueError:
                continue
            delivered += 1
        return delivered

    def _result_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        result: dict[str, Any],
    ) -> ChannelReply:
        response_text = str(result.get("text") or "").strip()
        if not response_text and result.get("pending_tools"):
            response_text = self._t("waiting_confirmation")
        raw_blocks = [
            dict(block)
            for block in list(result.get("blocks") or [])
            if isinstance(block, dict)
        ]
        wait_cards_delivered = bool(result.get("channel_wait_cards_delivered"))
        if not wait_cards_delivered and not any(
            str(block.get("type") or "") in {"confirm", "command_plan"}
            for block in raw_blocks
        ):
            for pending in result.get("pending_tools") or []:
                if not isinstance(pending, dict):
                    continue
                raw_blocks.append({
                    "type": "confirm",
                    "title": self._t(
                        "card_run_tool",
                        name=pending.get("name") or "tool",
                    ),
                    "name": pending.get("name") or "",
                    "confirm_id": pending.get("id") or "",
                    "pending_id": pending.get("pending_id") or "",
                    "pending_version": pending.get("version") or 1,
                    "arguments": pending.get("arguments") or {},
                    "risk": pending.get("risk") or "",
                })

        status_name = str(result.get("status") or "").strip()
        awaits_user = status_name in {
            "awaiting_user_decision",
            "awaiting_confirmation",
            "pending_user_decision",
            "pending_confirmation",
        }
        if (
            awaits_user
            and not wait_cards_delivered
            and not any(self._block_awaits_user(block) for block in raw_blocks)
        ):
            # Live filtering / rebuild quirks can drop the wait card from the
            # turn payload; recover it from the session so buttons still ship.
            try:
                status = self.session_handler(identity.bound_session_id)
            except Exception:
                status = {}
            if isinstance(status, dict):
                raw_blocks.extend(self.pending_interaction_blocks(status))

        # Live/outbox already delivered clickable wait cards — keep prose only
        # so Feishu/Telegram do not get a second identical Allow/Deny card.
        project_blocks = (
            [
                block
                for block in raw_blocks
                if not self._block_awaits_user(block)
            ]
            if wait_cards_delivered
            else raw_blocks
        )
        cards = tuple(self._project_interaction_cards(event, identity, project_blocks))
        text = str(response_text or "").strip()
        for marker in ("[Waiting for your decision]", "[等待用户决定]"):
            if text.startswith(marker):
                text = text.removeprefix(marker).strip()
        # Models (and old card_text fallbacks) often emit a hollow "完成。" / "Done."
        # after /live already pushed the real content — treat that as silence.
        # Without a live push the same words may be the entire answer, and
        # dropping them would leave the chat with no reply at all.
        if (
            text
            and not cards
            and result.get("channel_live_delivered")
            and self._is_hollow_done(text)
        ):
            text = ""
        if not text and not cards:
            # Live mid-turn delivery already pushed the useful content, or the
            # turn produced nothing channel-visible — do not invent "完成。".
            return self._reply(event, "")
        if (
            not wait_cards_delivered
            and (
                awaits_user
                or any(self._block_awaits_user(block) for block in raw_blocks)
            )
        ):
            # Sibling identities (and the author, when the sync reply itself has
            # no interactive card) still need the clickable wait card.
            has_interactive = any(
                str(card.get("type") or "") in {
                    "user_decision",
                    "user_decision_batch",
                    "tool_confirm",
                }
                for card in cards
            )
            try:
                self.push_session_interaction(
                    identity.bound_session_id,
                    [block for block in raw_blocks if self._block_awaits_user(block)],
                    skip_identity_id=identity.id if has_interactive else "",
                )
            except Exception:
                pass
        return self._reply(event, text, blocks=cards)

    @staticmethod
    def _is_hollow_done(text: str) -> bool:
        clean = re.sub(r"\s+", "", str(text or "").strip())
        # "OK" is deliberately absent: it is a plausible complete answer on its
        # own, unlike a bare completion announcement.
        return clean in {
            "完成",
            "完成。",
            "完成!",
            "完成！",
            "已完成",
            "已完成。",
            "Done",
            "Done.",
            "Done!",
        }

    def _handle_action(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        token: str,
    ) -> ChannelReply:
        try:
            action = self.store.consume_action(
                token,
                channel_id=event.channel_id,
                identity_id=identity.id,
            )
        except ValueError as exc:
            return self._reply(event, self._error_text(exc))
        session_id = str(action.get("session_id") or "")
        kind = str(action.get("kind") or "")
        if kind == "switch_session":
            if session_id != identity.bound_session_id:
                return self._reply(event, self._t("action_other_conversation"))
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("switch_not_allowed"))
            return self._switch_session(
                event,
                identity,
                str(action.get("target_session_id") or ""),
            )
        if kind == "sessions_page":
            if session_id != identity.bound_session_id:
                return self._reply(event, self._t("action_other_conversation"))
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("switch_not_allowed"))
            return self._sessions_reply(
                event,
                identity,
                page=int(action.get("page") or 1),
                query=str(action.get("query") or "")[:120],
            )
        if kind == "files_page":
            if session_id != identity.bound_session_id:
                return self._reply(event, self._t("action_other_conversation"))
            if not self._allowed(identity, "send"):
                return self._reply(event, self._t("file_not_allowed"))
            return self._files_reply(
                event,
                identity,
                page=int(action.get("page") or 1),
                query=str(action.get("query") or "")[:120],
            )
        if kind == "download_file":
            if session_id != identity.bound_session_id:
                return self._reply(event, self._t("action_other_conversation"))
            if not self._allowed(identity, "send"):
                return self._reply(event, self._t("file_not_allowed"))
            if self.session_file_handler is None:
                return self._reply(event, self._t("file_unavailable"))
            relative_path = str(action.get("relative_path") or "").strip()
            if not relative_path:
                return self._reply(event, self._t("file_failed"))
            result = self.session_file_handler(identity.bound_session_id, relative_path)
            if not result.get("ok"):
                return self._reply(
                    event,
                    str(result.get("error") or self._t("file_failed")),
                )
            attachment = result.get("attachment")
            if not isinstance(attachment, dict):
                return self._reply(event, self._t("file_failed"))
            return self._reply(
                event,
                str(result.get("message") or self._t(
                    "file_sending",
                    name=attachment.get("name") or "file",
                )),
                attachments=(attachment,),
            )
        if kind == "new_agent_session":
            return self._create_agent_session(
                event,
                identity,
                {"id": str(action.get("agent_id") or "")},
            )
        if kind in {"endpoint_prompt", "switch_endpoint"}:
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("endpoint_not_allowed"))
            if self.endpoint_handler is None:
                return self._reply(event, self._t("endpoint_unavailable"))
            target = {
                "index": int(action.get("index") or 0),
                "name": str(action.get("name") or ""),
            }
            if action.get("session_id"):
                target["session_id"] = str(action.get("session_id") or "")
            elif identity.bound_session_id:
                target["session_id"] = identity.bound_session_id
            if action.get("value"):
                target["value"] = str(action.get("value") or "")
            if action.get("config_id"):
                target["config_id"] = str(action.get("config_id") or "")
            if kind == "endpoint_prompt":
                return self._endpoint_prompt(event, identity, target)
            applied = self.endpoint_handler("activate", target)
            if not applied.get("ok"):
                return self._reply(event, self._handler_error_text(applied))
            endpoint = dict(applied.get("endpoint") or {})
            reply_key = (
                "acp_model_switched"
                if str(applied.get("kind") or "") == "acp_model"
                else "endpoint_switched"
            )
            return self._reply(
                event,
                self._t(
                    reply_key,
                    name=str(endpoint.get("name") or "")[:60],
                    model=str(endpoint.get("model") or "")[:60],
                ),
            )
        if not session_id or session_id != identity.bound_session_id:
            return self._reply(event, self._t("action_other_conversation"))
        if kind == "dismiss":
            return self._reply(event, self._t("action_cancelled"))
        if kind == "session_ready_mode":
            if not self._allowed(identity, "configure"):
                return self._reply(event, self._t("ready_mode_not_allowed"))
            if self.session_option_handler is None:
                return self._reply(event, self._t("ready_mode_unavailable"))
            session_id = identity.bound_session_id
            errors: list[str] = []
            # High-risk tool auto-allow remains behind its own explicit command.
            # Greenlight is included because the ready-mode card names it
            # explicitly and still requires a user click.
            for option, action in (
                ("tools", "allow_standard"),
                ("live", "on"),
                ("search", "on"),
                ("greenlight", "on"),
            ):
                try:
                    configured = self.session_option_handler(session_id, option, action)
                except Exception as exc:
                    errors.append(f"{option}: {exc}")
                    continue
                if not configured.get("ok"):
                    errors.append(
                        f"{option}: {configured.get('error') or 'failed'}"
                    )
            if errors:
                return self._reply(
                    event,
                    self._t(
                        "ready_mode_partial",
                        detail="; ".join(errors)[:400],
                    ),
                )
            return self._reply(event, self._t("ready_mode_enabled"))
        if kind == "session_auto_allow":
            if not (
                self._allowed(identity, "confirm")
                and self._allowed(identity, "configure")
            ):
                return self._reply(event, self._t("auto_allow_not_allowed"))
            if self.session_option_handler is None:
                return self._reply(event, self._t("session_tools_unavailable"))
            configured = self.session_option_handler(
                session_id,
                "tools",
                "allow_all",
            )
            if not configured.get("ok"):
                return self._reply(
                    event,
                    str(configured.get("error") or self._t("auto_allow_failed")),
                )
            status = self.session_handler(session_id)
            pending = [
                item for item in list(status.get("pending_tools") or [])
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
            if pending:
                selected = pending[0]
                forwarded = self.confirmation_handler(
                    session_id,
                    {
                        "type": "batch_confirmation_decision",
                        "pending_id": str(selected.get("pending_id") or ""),
                        "version": int(selected.get("version") or 0),
                        "decisions": {
                            str(item.get("id") or ""): "allow"
                            for item in pending
                        },
                    },
                    event.provider,
                )
                if not forwarded.get("ok"):
                    return self._reply(
                        event,
                        self._t(
                            "auto_allow_batch_partial",
                            error=forwarded.get("error") or Frag("confirmation_failed"),
                        ),
                    )
            return self._reply(
                event,
                str(configured.get("message") or self._t("auto_allow_enabled")),
            )
        if kind == "tool_confirmation":
            if not self._allowed(identity, "confirm"):
                return self._reply(event, self._t("confirm_not_allowed"))
            rpc = {
                "type": "confirmation_decision",
                "pending_id": str(action.get("pending_id") or ""),
                "version": int(action.get("version") or 0),
                "call_id": str(action.get("call_id") or ""),
                "verdict": str(action.get("verdict") or "deny"),
            }
            result = self.confirmation_handler(session_id, rpc, event.provider)
            return self._reply(
                event,
                self._t("decision_forwarded")
                if result.get("ok")
                else str(result.get("error") or self._t("confirmation_failed")),
            )
        if kind == "user_decision":
            if not self._allowed(identity, "chat"):
                return self._reply(event, self._t("chat_not_allowed"))
            return self._forward_hidden_decision(
                event,
                identity,
                str(action.get("response") or ""),
            )
        if kind == "batch_select":
            if not self._allowed(identity, "chat"):
                return self._reply(event, self._t("chat_not_allowed"))
            try:
                group = self.store.set_action_group_answer(
                    str(action.get("group_id") or ""),
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                    question_id=str(action.get("question_id") or ""),
                    value=str(action.get("value") or ""),
                )
            except ValueError as exc:
                return self._reply(event, self._error_text(exc))
            return self._batch_group_reply(event, identity, group)
        if kind == "batch_submit":
            if not self._allowed(identity, "chat"):
                return self._reply(event, self._t("chat_not_allowed"))
            group_id = str(action.get("group_id") or "")
            try:
                group = self.store.consume_action_group(
                    group_id,
                    channel_id=event.channel_id,
                    identity_id=identity.id,
                )
            except ValueError as exc:
                try:
                    group = self.store.get_action_group(
                        group_id,
                        channel_id=event.channel_id,
                        identity_id=identity.id,
                    )
                except ValueError:
                    return self._reply(event, self._error_text(exc))
                reply = self._batch_group_reply(event, identity, group)
                return ChannelReply(
                    text=self._error_text(exc),
                    external_chat_id=reply.external_chat_id,
                    external_thread_id=reply.external_thread_id,
                    reply_to_message_id=reply.reply_to_message_id,
                    blocks=reply.blocks,
                    locale=reply.locale,
                )
            return self._forward_hidden_decision(
                event,
                identity,
                batch_response_text(group),
            )
        return self._reply(event, self._t("action_unsupported"))

    def _batch_group_reply(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        group: dict[str, Any],
    ) -> ChannelReply:
        def issue(payload: dict[str, Any]) -> str:
            return self.store.issue_action(
                channel_id=event.channel_id,
                identity_id=identity.id,
                session_id=identity.bound_session_id,
                payload=payload,
            )

        card = project_batch_group_card(group, issue_action=issue, locale=self.locale())
        return self._reply(
            event,
            self._t("selection_updated"),
            blocks=(card,),
        )

    def _forward_hidden_decision(
        self,
        event: ChannelEvent,
        identity: ChannelIdentity,
        response: str,
    ) -> ChannelReply:
        result = self.turn_handler({
            "session_id": identity.bound_session_id,
            "message": response,
            "attachments": [],
            "hide_user_message": True,
            "message_source": "channel_decision",
            "message_source_ref": identity.id,
            "channel": {
                "channel_id": event.channel_id,
                "provider": event.provider,
                "external_user_id": event.external_user_id,
                "message_id": event.message_id,
            },
        })
        if not result.get("ok"):
            return self._reply(
                event,
                str(result.get("error") or self._t("assistant_turn_failed")),
            )
        if result.get("queued"):
            return self._reply(event, self._t("interjected"))
        return self._result_reply(event, identity, result)
