"""pywebview bridge for communication-channel management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from argusai.core.channels import (
    BILINGUAL,
    CHANNEL_PROVIDERS,
    ChannelGateway,
    ChannelRuntimeManager,
    ChannelStore,
)
from argusai.core.channels.adapters import (
    ILINK_API_BASE,
    ilink_qrcode_status,
    ilink_request_qrcode,
)
from argusai.core.paths import PathManager

from ._helpers import debug_log


def _selected_index(target: dict[str, Any]) -> int:
    """Read a row index that may arrive as an int or as card-action text."""
    raw = target.get("index")
    try:
        return int(str(raw if raw is not None else "").strip())
    except ValueError:
        return -1


def _live_block_key(block: dict[str, Any]) -> str:
    """Identify a UI block across the live push and the final reply.

    Never key on auto-assigned ``id``: live emit happens before
    ``normalize_blocks`` stamps a random id onto the final reply copy, so
    id-based matching would let every card through twice. Prefer stable
    decision/confirm tokens; otherwise hash content with volatile protocol
    fields stripped.
    """
    kind = str(block.get("type") or "")
    for field in ("confirm_id", "pending_id", "batch_id"):
        value = str(block.get(field) or "").strip()
        if value:
            return f"{kind}:{field}:{value}"
    fingerprint = {
        key: value
        for key, value in dict(block).items()
        if key not in {"id", "version", "source"}
    }
    digest = hashlib.sha1(
        json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{kind}:sha1:{digest}"


_CHANNEL_PROVIDER_META = {
    "qq": {
        "name": "QQ",
        "transport": "websocket",
        "secrets": ["app_id", "client_secret"],
        "card_mode": "native",
    },
    "telegram": {
        "name": "Telegram",
        "transport": "long_polling",
        "secrets": ["bot_token"],
        "card_mode": "native",
    },
    "discord": {
        "name": "Discord",
        "transport": "gateway",
        "secrets": ["bot_token"],
        "card_mode": "native",
    },
    "slack": {
        "name": "Slack",
        "transport": "socket_mode",
        "secrets": ["bot_token", "app_token"],
        "card_mode": "native",
    },
    "feishu": {
        "name": "飞书 / Feishu",
        "transport": "websocket",
        "secrets": ["app_id", "app_secret", "verification_token"],
        "card_mode": "native",
    },
    "dingtalk": {
        "name": "钉钉 / DingTalk",
        "transport": "stream",
        "secrets": ["client_id", "client_secret"],
        "card_mode": "command_fallback",
    },
    "wecom": {
        "name": "企业微信 / WeCom",
        "transport": "webhook",
        "secrets": ["corp_id", "corp_secret", "agent_id", "token", "aes_key"],
        "card_mode": "native",
    },
    "wechat": {
        "name": "微信 / WeChat (iLink)",
        "transport": "ilink_long_polling",
        "secrets": ["bot_token"],
        "card_mode": "plain_text",
    },
    "webhook": {
        "name": "Generic Webhook",
        "transport": "http",
        "secrets": ["signing_secret"],
        "card_mode": "structured_payload",
    },
}


class ChannelsMixin:
    def _channels_store(self) -> ChannelStore:
        store = getattr(self, "_channel_store_instance", None)
        root = PathManager(data_root=self.settings_mgr.get().data_root).channels_dir
        if store is None or store.root != root:
            store = ChannelStore(root)
            self._channel_store_instance = store
        return store

    def _channels_gateway(self) -> ChannelGateway:
        gateway = getattr(self, "_channel_gateway_instance", None)
        store = self._channels_store()
        if gateway is None or gateway.store is not store:
            gateway = ChannelGateway(
                store,
                turn_handler=self._channel_turn,
                session_handler=self.get_assistant_session,
                cancel_handler=self.cancel_assistant_run,
                new_session_handler=lambda _identity: uuid.uuid4().hex,
                confirmation_handler=self._confirm_channel_action,
                session_option_handler=self._channel_session_option,
                session_list_handler=self.list_assistant_sessions,
                session_file_handler=self._channel_session_file,
                side_question_handler=self.assistant_side_question,
                endpoint_handler=self._channel_ai_endpoint,
                agent_runtime_handler=self._channel_agent_runtime,
                # Remote users may not share the desktop UI's language, so channels
                # answer bilingually until someone picks a side with /lang.
                default_locale=BILINGUAL,
                on_outbound=self._wake_channel_outbox,
            )
            self._channel_gateway_instance = gateway
        return gateway

    def _wake_channel_outbox(self) -> None:
        """Nudge the runtime outbox loop after gateway-side enqueue."""
        runtime = getattr(self, "_channel_runtime_instance", None)
        wake = getattr(runtime, "_outbox_wake", None) if runtime is not None else None
        if wake is not None:
            wake.set()

    def _channel_interjections(self) -> dict[str, list[dict[str, Any]]]:
        queue = getattr(self, "_channel_interjection_map", None)
        if queue is None:
            queue = {}
            self._channel_interjection_map = queue
        return queue

    def _channel_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one channel-authored turn, interrupting any turn already running.

        Channels share the desktop's busy flag, so one conversation can never be
        advanced by two turns at once. A message that arrives mid-turn does not
        wait for the task to finish: it cancels the running turn outright and is
        queued to run next, so chatting over a channel feels like interrupting.
        The cancelled turn clears the busy marker in its ``finally`` and then
        drains this queued message.
        """
        session_id = str(params.get("session_id") or "")
        if not session_id:
            return self._run_assistant_chat(params)
        with self._assistant_run_lock:
            busy = session_id in self._assistant_running_sessions
            if busy:
                pending = self._channel_interjections().setdefault(session_id, [])
                pending.append(dict(params))
                position = len(pending)
            else:
                self._assistant_running_sessions.add(session_id)
        if busy:
            try:
                self.cancel_assistant_run(session_id)
            except Exception as exc:
                debug_log(f"channel_interrupt_cancel_failed session_id={session_id} error={exc}")
            return {
                "ok": True,
                "queued": True,
                "interrupted": True,
                "position": position,
                "session_id": session_id,
            }
        try:
            return self._run_channel_assistant_chat(params)
        finally:
            with self._assistant_run_lock:
                self._assistant_running_sessions.discard(session_id)
            self._drain_channel_interjections(session_id)

    _WAIT_CARD_TYPES = frozenset({
        "user_decision",
        "user_decision_batch",
        "confirm",
        "command_plan",
    })
    _AWAITING_STATUSES = frozenset({
        "awaiting_user_decision",
        "awaiting_confirmation",
        "pending_user_decision",
        "pending_confirmation",
    })

    def _push_channel_wait_cards(
        self,
        session_id: str,
        result: dict[str, Any] | None = None,
        *,
        skip_identity_id: str = "",
    ) -> int:
        """Fan-out unresolved decision/confirm cards to bound channel identities."""
        safe_session = str(session_id or "").strip()
        if not safe_session:
            return 0
        payload = result if isinstance(result, dict) else {}
        status_name = str(payload.get("status") or "").strip()
        awaits = status_name in self._AWAITING_STATUSES
        allowed = self._WAIT_CARD_TYPES | self._WAIT_CONTEXT_TYPES
        blocks = [
            dict(block)
            for block in list(payload.get("blocks") or [])
            if isinstance(block, dict)
            and str(block.get("type") or "") in allowed
        ]
        if not awaits and not any(
            str(block.get("type") or "") in self._WAIT_CARD_TYPES for block in blocks
        ):
            return 0
        try:
            return int(self._channels_gateway().push_session_interaction(
                safe_session,
                blocks,
                skip_identity_id=str(skip_identity_id or ""),
            ) or 0)
        except Exception as exc:
            debug_log(
                f"channel_wait_card_push_failed session_id={safe_session} error={exc}"
            )
            return 0

    # Blocks that must travel with a wait card so channel order stays natural
    # (e.g. todo plan above "以上是多步任务计划…请确认").
    _WAIT_CONTEXT_TYPES = frozenset({"todo_list"})

    def _bundle_wait_delivery_blocks(
        self,
        result_blocks: list[Any],
        interactive: list[dict[str, Any]],
        *,
        already_pushed_keys: set[str],
    ) -> list[dict[str, Any]]:
        """Keep todo/plan context ahead of the decision in one outbound payload."""
        wait_keys = {_live_block_key(block) for block in interactive}
        bundled: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in result_blocks:
            if not isinstance(raw, dict):
                continue
            key = _live_block_key(raw)
            if key in already_pushed_keys or key in seen:
                continue
            kind = str(raw.get("type") or "")
            if kind in self._WAIT_CARD_TYPES and key in wait_keys:
                bundled.append(dict(raw))
                seen.add(key)
            elif kind in self._WAIT_CONTEXT_TYPES:
                bundled.append(dict(raw))
                seen.add(key)
        for block in interactive:
            key = _live_block_key(block)
            if key in already_pushed_keys or key in seen:
                continue
            bundled.append(dict(block))
            seen.add(key)
        return bundled

    def _deliver_channel_wait_cards(
        self,
        identity_id: str,
        session_id: str,
        result: dict[str, Any],
        *,
        already_pushed_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """Push stop/confirm cards onto the channel outbox for this turn.

        ``stop_for_user_decision`` is final-only in the assistant runtime (so the
        GUI does not double-render), which means channel ``/live`` never sees the
        card mid-turn. Delivering it here — on the outbox — is the authoritative
        channel path; the sync reply then keeps prose/preview blocks only.

        Todo/plan blocks that belong with the wait card are bundled in the same
        outbound message (plan first, confirm second) so Feishu does not show
        "以上是多步任务计划" before the plan itself.
        """
        if not isinstance(result, dict) or not result.get("ok"):
            return result
        out = dict(result)
        status_name = str(out.get("status") or "").strip()
        awaits = status_name in self._AWAITING_STATUSES
        already = set(already_pushed_keys or ())
        result_blocks = list(out.get("blocks") or [])
        interactive = [
            dict(block)
            for block in result_blocks
            if isinstance(block, dict)
            and str(block.get("type") or "") in self._WAIT_CARD_TYPES
            and _live_block_key(block) not in already
        ]
        if awaits and not interactive:
            try:
                status = self.get_assistant_session(session_id)
            except Exception:
                status = {}
            if isinstance(status, dict) and status.get("ok"):
                from argusai.core.channels.gateway import ChannelGateway

                interactive = [
                    block
                    for block in ChannelGateway.pending_interaction_blocks(status)
                    if _live_block_key(block) not in already
                ]
        if not interactive:
            # Live already pushed the wait card(s); mark delivered so sync reply
            # does not recover and re-send them.
            if awaits and already:
                out["channel_wait_cards_delivered"] = True
            return out

        delivery_blocks = self._bundle_wait_delivery_blocks(
            result_blocks,
            interactive,
            already_pushed_keys=already,
        )
        gateway = self._channels_gateway()
        author_id = str(identity_id or "").strip()
        author_pushed = False
        if author_id:
            try:
                author_pushed = bool(
                    gateway.queue_live_update(author_id, raw_blocks=delivery_blocks)
                )
            except Exception as exc:
                debug_log(
                    f"channel_wait_card_author_push_failed "
                    f"identity_id={author_id} error={exc}"
                )
                author_pushed = False
        self._push_channel_wait_cards(
            session_id,
            {
                **out,
                "blocks": delivery_blocks,
                "status": status_name or "awaiting_user_decision",
            },
            skip_identity_id=author_id if author_pushed else "",
        )
        if not author_pushed:
            # Keep cards on the sync reply so ``_result_reply`` can still send them.
            return out

        pushed_keys = {_live_block_key(block) for block in delivery_blocks}
        out["blocks"] = [
            block
            for block in result_blocks
            if not (isinstance(block, dict) and _live_block_key(block) in pushed_keys)
        ]
        out["channel_wait_cards_delivered"] = True
        out["channel_live_delivered"] = True
        return out

    def _run_channel_assistant_chat(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run a channel turn, optionally pushing live segments/cards mid-turn."""
        session_id = str(params.get("session_id") or "")
        identity_id = str(params.get("message_source_ref") or "")

        live = False
        if session_id and identity_id:
            try:
                status = self.get_assistant_session(session_id)
                live = bool(status.get("ok") and status.get("channel_live"))
            except Exception:
                live = False
        if not live:
            result = self._run_assistant_chat(params)
            if isinstance(result, dict) and result.get("ok"):
                return self._deliver_channel_wait_cards(identity_id, session_id, result)
            return result

        gateway = self._channels_gateway()
        buffer = {"text": "", "delivered": False}
        channel = params.get("channel") if isinstance(params.get("channel"), dict) else {}
        runtime = getattr(self, "_channel_runtime_instance", None)
        capability = getattr(runtime, "stream_capability", None)
        mode = "native" if str(channel.get("provider") or "").lower() == "qq" else "staged"
        if callable(capability):
            try:
                mode = capability(identity_id)
            except Exception:
                mode = "staged"
        native_stream = {
            "enabled": (
                mode != "staged"
                and bool(str(channel.get("message_id") or "").strip())
                and bool(identity_id)
            ),
            "text": "",
            "last_push": 0.0,
            "attempted": False,
        }
        pushed_keys: set[str] = set()
        _LIVE_CARD_TYPES = {
            "confirm",
            "command_plan",
            "user_decision",
            "user_decision_batch",
            "todo_list",
            "structured_card",
            "data_table",
            "file_resource_view",
        }

        def flush_text() -> None:
            text = str(buffer["text"] or "").strip()
            if not text:
                return
            try:
                delivered = bool(gateway.queue_live_update(identity_id, text=text))
            except Exception as exc:
                # Keep the text buffered: a later flush retries it, and whatever
                # is still unsent at the end rides out on the final reply rather
                # than vanishing.
                debug_log(f"channel_live_text_failed identity_id={identity_id} error={exc}")
                return
            if not delivered:
                return
            buffer["text"] = ""
            buffer["delivered"] = True

        def push_native_stream(*, final: bool = False) -> bool:
            """Update one provider stream; keep outbox fallback intact."""
            if not native_stream["enabled"]:
                return False
            text = str(native_stream["text"] or "")
            if not text.strip():
                return False
            runtime = getattr(self, "_channel_runtime_instance", None)
            sender = getattr(runtime, "stream_live_update", None)
            if not callable(sender):
                return False
            try:
                sent = bool(sender(
                    identity_id,
                    source_message_id=str(channel.get("message_id") or ""),
                    text=text,
                    final=final,
                ))
            except Exception as exc:
                debug_log(
                    f"channel_native_stream_failed identity_id={identity_id} "
                    f"final={final} error={exc}"
                )
                return False
            if sent:
                native_stream["attempted"] = True
                native_stream["last_push"] = time.monotonic()
            return sent

        def on_delta(text: str) -> None:
            if text:
                buffer["text"] = str(buffer["text"] or "") + str(text)
                native_stream["text"] = str(native_stream["text"] or "") + str(text)
                # Adapters additionally coalesce frames to their rate budget.
                if (
                    native_stream["enabled"]
                    and time.monotonic() - float(native_stream["last_push"] or 0) >= 0.45
                ):
                    push_native_stream()

        def on_segment(_round: int) -> None:
            if not native_stream["enabled"]:
                flush_text()
            elif not native_stream["attempted"]:
                push_native_stream()

        def on_block(block: dict[str, Any]) -> None:
            if not isinstance(block, dict):
                return
            if str(block.get("type") or "") not in _LIVE_CARD_TYPES:
                return
            # Decision cards are final-only in the assistant runtime; they are
            # force-delivered after the turn via ``_deliver_channel_wait_cards``.
            if str(block.get("type") or "") in {"user_decision", "user_decision_batch"}:
                return
            key = _live_block_key(block)
            if key in pushed_keys:
                return
            try:
                delivered = bool(gateway.queue_live_update(identity_id, raw_blocks=[block]))
            except Exception as exc:
                debug_log(f"channel_live_card_failed identity_id={identity_id} error={exc}")
                return
            if delivered:
                pushed_keys.add(key)

        try:
            result = self._run_assistant_chat(
                params,
                on_delta=on_delta,
                on_block=on_block,
                on_segment=on_segment,
            )
        except BaseException:
            # Cancellation/errors must close remote typing/streaming state too.
            push_native_stream(final=True)
            raise
        native_complete = push_native_stream(final=True)
        if native_complete:
            buffer["text"] = ""
            buffer["delivered"] = True
        else:
            flush_text()
        if not isinstance(result, dict) or not result.get("ok"):
            return result
        out = dict(result)
        # Avoid re-sending the last bubble that was already flushed live, but
        # only when the live push actually got through.
        if buffer["delivered"] and not str(buffer["text"] or "").strip():
            out["text"] = ""
        # Lets the gateway drop a trailing hollow "完成。" — but only for a turn
        # whose real content the user has already seen.
        out["channel_live_delivered"] = bool(
            result.get("channel_live_delivered") or buffer["delivered"] or pushed_keys
        )
        if pushed_keys:
            out["blocks"] = [
                block for block in list(result.get("blocks") or [])
                if not (isinstance(block, dict) and _live_block_key(block) in pushed_keys)
            ]
        return self._deliver_channel_wait_cards(
            identity_id,
            session_id,
            out,
            already_pushed_keys=pushed_keys,
        )

    def _drain_channel_interjections(self, session_id: str) -> None:
        """Run the messages that arrived while this conversation was busy.

        Their senders were told the turn was queued, so the answers travel back
        over the outbox instead of as a reply to an inbound request.
        """
        # A cross-session delivery may have landed while this turn held the
        # slot; write it before the next queued turn runs so causal order holds.
        self._flush_pending_channel_history(session_id)
        while True:
            with self._assistant_run_lock:
                pending = self._channel_interjections().get(session_id) or []
                if not pending:
                    self._channel_interjections().pop(session_id, None)
                    return
                if session_id in self._assistant_running_sessions:
                    # Someone else owns the session; they drain when they finish.
                    return
                params = pending.pop(0)
                self._assistant_running_sessions.add(session_id)
            try:
                result = self._run_channel_assistant_chat(params)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            finally:
                with self._assistant_run_lock:
                    self._assistant_running_sessions.discard(session_id)
            identity_id = str(params.get("message_source_ref") or "")
            if not identity_id:
                continue
            try:
                self._channels_gateway().queue_result_delivery(identity_id, result)
            except Exception as exc:
                debug_log(
                    f"channel_interjection_delivery_failed session_id={session_id} "
                    f"identity_id={identity_id} error={exc}"
                )

    def _drain_channel_interjections_async(self, session_id: str) -> None:
        """Drain off the caller's thread, so a finishing run is not held up."""
        has_interjections = bool(self._channel_interjections().get(session_id) or [])
        has_pending_history = bool(
            self._pending_channel_history().get(session_id) or []
        )
        if not has_interjections and not has_pending_history:
            return
        threading.Thread(
            target=self._drain_channel_interjections,
            args=(session_id,),
            daemon=True,
        ).start()

    def _channel_session_option(
        self,
        session_id: str,
        option: str,
        action: str,
    ) -> dict[str, Any]:
        if option == "tools":
            if action not in {"allow_all", "allow_standard", "disable_all"}:
                return {"ok": False, "error": "unsupported tool authorization action"}
            enabled = action != "disable_all"
            # allow_standard leaves NO_AUTO_APPROVE_TOOLS prompting, which is what
            # a one-tap card may grant; allow_all is the explicit /allow command.
            high_risk = action == "allow_all"
            result = self.set_assistant_auto_allow_all(
                session_id,
                enabled,
                include_high_risk=high_risk,
                include_reverse_dynamic=high_risk,
            )
            if not result.get("ok"):
                return result
            if not enabled:
                message = "Session-wide tool authorization is off."
            elif high_risk:
                message = "All tools are allowed for this conversation. Send /allow off to stop."
            else:
                message = (
                    "Ordinary tools are allowed for this conversation; high-risk tools "
                    "still ask. Send /allow off to stop."
                )
            return {**result, "message": message}
        if option == "greenlight":
            if action not in {"on", "off", "status"}:
                return {"ok": False, "error": "unsupported greenlight action"}
            if action == "status":
                result = self.get_assistant_session(session_id)
                declaration = result.get("engagement_declaration")
                enabled = isinstance(declaration, dict) and bool(declaration.get("active"))
                return {"ok": True, "enabled": enabled}
            declaration = (
                {
                    "active": True,
                    "mode": "greenlight",
                    "auth_type": "other",
                    "scope": "*",
                }
                if action == "on"
                else {"active": False}
            )
            result = self.set_assistant_engagement(session_id, declaration)
            if not result.get("ok"):
                return result
            enabled = action == "on"
            return {**result, "enabled": enabled}
        if option == "live":
            if action not in {"on", "off", "status"}:
                return {"ok": False, "error": "unsupported live action"}
            if action == "status":
                result = self.get_assistant_session(session_id)
                enabled = bool(result.get("channel_live"))
                return {"ok": True, "enabled": enabled}
            enabled = action == "on"
            result = self.set_assistant_channel_live(session_id, enabled)
            if not result.get("ok"):
                return result
            return {**result, "enabled": enabled}
        if option == "think":
            from argusai.core.channels.slash import THINK_EFFORT_ACTIONS

            allowed = {"on", "off", "status"} | set(THINK_EFFORT_ACTIONS)
            if action not in allowed:
                return {"ok": False, "error": "unsupported think action"}
            if action == "status":
                result = self.get_assistant_session(session_id)
                enabled = bool(result.get("thinking_enabled"))
                return {
                    "ok": True,
                    "enabled": enabled,
                    "thinking_effort": str(result.get("thinking_effort") or "high"),
                }
            if action in THINK_EFFORT_ACTIONS:
                result = self.set_assistant_thinking(session_id, True, effort=action)
            else:
                result = self.set_assistant_thinking(session_id, action == "on")
            if not result.get("ok"):
                return result
            return {
                **result,
                "enabled": bool(result.get("thinking_enabled")),
            }
        if option != "search":
            return {"ok": False, "error": "unsupported channel setting"}
        if action == "status":
            result = self.get_assistant_session(session_id)
            enabled = bool(result.get("allow_web_search"))
        else:
            enabled = action == "on"
            result = self.set_assistant_web_search(session_id, enabled)
            if not result.get("ok"):
                return result
        return {
            "ok": True,
            "enabled": enabled,
            "message": f"Web search is {'on' if enabled else 'off'}.",
        }

    @staticmethod
    def _endpoint_view(
        index: int,
        ordinal: int,
        endpoint: Any,
        active: bool,
        model: str = "",
    ) -> dict[str, Any]:
        """Project one endpoint/model pair for remote display, without credentials."""
        base_url = str(getattr(endpoint, "base_url", "") or "")
        host = base_url.split("//")[-1].split("/")[0] if base_url else ""
        chosen = str(model or getattr(endpoint, "model", "") or "")
        return {
            "index": index,
            "ordinal": ordinal,
            "name": str(getattr(endpoint, "name", "") or "") or host or f"endpoint-{ordinal}",
            "model": chosen,
            # Mirrors the ACP rows so the gateway can carry both through one
            # action payload.
            "value": chosen,
            "host": host,
            "active": bool(active),
        }

    def _endpoint_rows(self, settings: Any, session: Any = None) -> list[dict[str, Any]]:
        """One row per switchable (endpoint, model) pair, best route first."""
        enabled = [
            (index, endpoint)
            for index, endpoint in enumerate(settings.ai_endpoints or [])
            if getattr(endpoint, "enabled", True)
        ]
        enabled.sort(key=lambda item: int(getattr(item[1], "priority", 0) or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        for position, (index, endpoint) in enumerate(enabled):
            catalog = endpoint.model_catalog() if hasattr(endpoint, "model_catalog") else [
                str(getattr(endpoint, "model", "") or "")
            ]
            for model in catalog or [""]:
                active = position == 0 and model == str(getattr(endpoint, "model", "") or "")
                if session is not None:
                    selection = session.native_model_selection
                    active = (session.native_endpoint_key(endpoint) == selection.get("endpoint_key")
                              and model == selection.get("model"))
                rows.append(self._endpoint_view(
                    index,
                    len(rows) + 1,
                    endpoint,
                    # Only the top-priority endpoint running its selected model
                    # is what a new turn would actually use.
                    active,
                    model,
                ))
        return rows

    def _channel_ai_endpoint(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        """List or switch models: native endpoints, or ACP configOptions for ACP sessions.

        Only the main chat failover chain is exposed for native sessions. The
        vision and ASR auxiliary endpoints are configured separately and are
        never touched from a channel.
        """
        session_id = str(target.get("session_id") or "").strip()
        if session_id:
            acp = self._channel_acp_model(action, target)
            if not acp.get("fallback_native"):
                # Failures carry no kind of their own, but callers still need to
                # know which world the error came from.
                return {"kind": "acp_model", **acp}

        settings = self.settings_mgr.get()
        session = self._ensure_session(session_id) if session_id else None
        if session is not None:
            previous_selection = dict(session.native_model_selection)
            try:
                session.native_model_endpoints(settings.ai_endpoints)
            except ValueError:
                # Keep the model catalog usable so a removed/disabled route can
                # be replaced explicitly, without silently selecting another.
                pass
            if previous_selection != session.native_model_selection:
                self._save_session(self._session_path(session_id), session, "")
        rows = self._endpoint_rows(settings, session)
        if action == "list":
            return {"ok": True, "kind": "endpoint", "endpoints": rows}

        selector = str(target.get("selector") or "").strip()
        if selector:
            match = None
            if selector.isdigit():
                ordinal = int(selector)
                match = next((row for row in rows if row["ordinal"] == ordinal), None)
            if match is None:
                folded = selector.casefold()
                # A bare model name is the most specific thing a user can type,
                # so it outranks an endpoint name that merely contains it.
                match = next(
                    (row for row in rows if row["model"].casefold() == folded),
                    None,
                ) or next(
                    (row for row in rows if row["name"].casefold() == folded),
                    None,
                ) or next(
                    (
                        row for row in rows
                        if folded in row["model"].casefold()
                        or folded in row["name"].casefold()
                    ),
                    None,
                )
        else:
            index = _selected_index(target)
            model = str(target.get("value") or "")
            match = next(
                (
                    row for row in rows
                    if row["index"] == index and (not model or row["model"] == model)
                ),
                None,
            )
            # The list is keyed by array position, so a rename, a deletion or a
            # model-catalog edit between listing and confirming must not switch
            # the wrong one.
            if match is not None and str(target.get("name") or "") != match["name"]:
                return {"ok": False, "code": "endpoint_changed"}
        if match is None:
            return {"ok": False, "code": "endpoint_not_found"}
        if action == "resolve":
            return {"ok": True, "kind": "endpoint", "endpoint": match}
        if action != "activate":
            return {"ok": False, "code": "endpoint_failed"}

        if session is not None:
            previous_selection = dict(session.native_model_selection)
            try:
                session.select_native_model(settings.ai_endpoints[match["index"]], match["model"])
                self._save_session(self._session_path(session_id), session, "")
            except (ValueError, OSError):
                session.native_model_selection = previous_selection
                return {"ok": False, "code": "endpoint_changed"}
            return {"ok": True, "kind": "endpoint", "endpoint": {**match, "active": True}}

        def _promote(current: Any) -> None:
            endpoints = list(current.ai_endpoints or [])
            if match["index"] >= len(endpoints):
                raise ValueError("endpoint_changed")
            chosen = endpoints[match["index"]]
            if match["model"] and match["model"] not in chosen.model_catalog():
                raise ValueError("endpoint_changed")
            top = max(int(getattr(item, "priority", 0) or 0) for item in endpoints)
            chosen.priority = top + 10
            if match["model"]:
                chosen.model = match["model"]

        try:
            self.settings_mgr.update(_promote)
        except ValueError:
            return {"ok": False, "code": "endpoint_changed"}
        return {"ok": True, "kind": "endpoint", "endpoint": {**match, "ordinal": 1, "active": True}}

    def _cached_acp_model_binding(
        self,
        runtime_id: str,
        *,
        exclude_session_id: str = "",
    ):
        """Find a recent successful model inventory for one ACP runtime."""
        from argusai.core.agent_runtimes import normalize_session_runtime_binding

        safe_runtime = str(runtime_id or "").strip()
        excluded = str(exclude_session_id or "").strip()
        if not safe_runtime:
            return None
        loaded = getattr(self, "_assistant_sessions", None) or {}
        for session_id, session in reversed(list(loaded.items())):
            if str(session_id or "") == excluded:
                continue
            binding = getattr(session, "runtime_binding", None)
            if (
                binding is not None
                and binding.runtime_id == safe_runtime
                and binding.acp_config_options
            ):
                return binding

        root = Path(getattr(self, "_sessions_dir", "") or "")
        if not root.is_dir():
            root = PathManager(
                data_root=self.settings_mgr.get().data_root,
            ).sessions_dir
        try:
            paths = sorted(
                root.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:200]
        except OSError:
            paths = []
        for path in paths:
            if excluded and path.stem == excluded:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                binding = normalize_session_runtime_binding(payload.get("runtime"))
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                json.JSONDecodeError,
            ):
                continue
            if binding.runtime_id == safe_runtime and binding.acp_config_options:
                return binding
        return None

    def _channel_acp_model(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        """List or persist an ACP session model preference for the bound conversation."""
        from argusai.core.agent_runtimes import (
            NATIVE_RUNTIME_ID,
            fetch_acp_config_options,
            normalize_session_runtime_binding,
            runtime_configs,
        )
        from argusai.core.agent_runtimes.acp_config import model_config_options
        from argusai.core.paths import PathManager

        session_id = str(target.get("session_id") or "").strip()
        if not session_id:
            return {"ok": False, "fallback_native": True, "code": "endpoint_failed"}
        try:
            status = self.get_assistant_session(session_id)
        except Exception:
            status = {}
        binding = (
            normalize_session_runtime_binding(status.get("runtime") or {})
            if status.get("ok")
            else None
        )
        if binding is None or binding.runtime_id == NATIVE_RUNTIME_ID:
            # A desktop conversation that has not run a turn yet has no stored
            # binding, so the caller passes the complete runtime snapshot from
            # the new-conversation dialog. Keeping only runtime_id loses project
            # cwd and model preferences, which makes many agents fail discovery.
            snapshot = (
                dict(target.get("runtime") or {})
                if isinstance(target.get("runtime"), dict)
                else {}
            )
            requested = str(
                target.get("runtime_id") or snapshot.get("runtime_id") or ""
            ).strip()
            if not requested or requested == NATIVE_RUNTIME_ID:
                return {"ok": False, "fallback_native": True, "code": "endpoint_failed"}
            candidate = binding.to_dict() if binding is not None else {}
            if str(snapshot.get("runtime_id") or "").strip() in {"", requested}:
                candidate.update(snapshot)
            candidate["runtime_id"] = requested
            binding = normalize_session_runtime_binding(candidate)

        known = {
            item.id: item
            for item in runtime_configs(self.settings_mgr.get().agent_runtimes)
        }
        runtime = known.get(binding.runtime_id)
        if runtime is None or runtime.backend != "acp" or not runtime.enabled:
            return {"ok": False, "code": "acp_model_unavailable"}

        options = model_config_options(list(binding.acp_config_options))
        model_source = "session"
        stale = False
        probe_error = ""
        force_refresh = (
            str(target.get("refresh") or "").lower() in {"1", "true", "yes"}
        )
        if not options and not force_refresh:
            cached = self._cached_acp_model_binding(
                runtime.id,
                exclude_session_id=session_id,
            )
            if cached is not None:
                session = self._ensure_session(session_id)
                session.runtime_binding = normalize_session_runtime_binding({
                    **binding.to_dict(),
                    "acp_config": dict(binding.acp_config),
                    "acp_config_options": list(cached.acp_config_options),
                })
                binding = session.runtime_binding
                options = model_config_options(list(binding.acp_config_options))
                model_source = "runtime_cache"
                stale = True
                try:
                    self._save_session(self._session_path(session_id), session, "")
                except Exception:
                    pass
        refresh = action == "list" and (not options or force_refresh)
        if refresh or (action in {"resolve", "activate"} and not options):
            paths = PathManager(data_root=self.settings_mgr.get().data_root)
            cwd = (
                binding.project_dir
                if binding.workspace_mode == "project" and binding.project_dir
                else str(paths.assistant_sandbox(session_id))
            )
            from .ai_endpoints import _run_blocking

            # A channel worker is blocked for the whole probe, which spawns and
            # tears down a real agent process, so this gets a far shorter budget
            # than a turn would.
            probe = _run_blocking(lambda: fetch_acp_config_options(
                runtime,
                cwd=cwd,
                preferred=dict(binding.acp_config or {}),
                timeout_seconds=15.0,
            ))
            if probe.get("ok"):
                options = model_config_options(list(probe.get("config_options") or []))
                session = self._ensure_session(session_id)
                session.runtime_binding = normalize_session_runtime_binding({
                    **binding.to_dict(),
                    "acp_config_options": list(probe.get("config_options") or []),
                    "acp_prompt_capabilities": dict(
                        probe.get("prompt_capabilities") or {}
                    ),
                })
                binding = session.runtime_binding
                model_source = "probe"
                stale = False
                try:
                    self._save_session(self._session_path(session_id), session, "")
                except Exception:
                    pass
            elif not options:
                cached = self._cached_acp_model_binding(
                    runtime.id,
                    exclude_session_id=session_id,
                )
                if cached is None:
                    return {
                        "ok": False,
                        "code": "acp_model_probe_failed",
                        "probe_code": str(probe.get("error_code") or ""),
                        "error": str(probe.get("error") or ""),
                    }
                session = self._ensure_session(session_id)
                session.runtime_binding = normalize_session_runtime_binding({
                    **binding.to_dict(),
                    "acp_config": dict(binding.acp_config),
                    "acp_config_options": list(cached.acp_config_options),
                })
                binding = session.runtime_binding
                options = model_config_options(list(binding.acp_config_options))
                model_source = "runtime_cache"
                stale = True
                probe_error = str(probe.get("error") or "")
                try:
                    self._save_session(self._session_path(session_id), session, "")
                except Exception:
                    pass

        if not options:
            return {"ok": False, "code": "acp_model_none"}

        # Flatten all model-like select options into one numbered list.
        rows: list[dict[str, Any]] = []
        preferred = dict(binding.acp_config or {})
        for option in options:
            config_id = str(option.get("id") or "")
            current = preferred.get(config_id, option.get("current_value"))
            for choice in list(option.get("options") or []):
                if not isinstance(choice, dict):
                    continue
                value = str(choice.get("value") or "")
                if not value:
                    continue
                active = str(current) == value
                rows.append({
                    "index": len(rows),
                    "ordinal": len(rows) + 1,
                    "name": str(choice.get("name") or value)[:60],
                    "model": value[:80],
                    "host": str(option.get("name") or config_id)[:80],
                    "active": active,
                    "config_id": config_id,
                    "value": value,
                })
        if not rows:
            return {"ok": False, "code": "acp_model_none"}

        # If nothing is marked active yet, treat the agent's currentValue as active.
        if not any(row["active"] for row in rows):
            for row in rows:
                option = next(
                    (item for item in options if item.get("id") == row["config_id"]),
                    None,
                )
                if option and str(option.get("current_value") or "") == row["value"]:
                    row["active"] = True
                    break

        if action == "list":
            return {
                "ok": True,
                "kind": "acp_model",
                "endpoints": rows,
                "runtime": binding.to_dict(),
                "model_source": model_source,
                "stale": stale,
                "warning": probe_error,
            }

        selector = str(target.get("selector") or "").strip()
        match = None
        if selector:
            if selector.isdigit():
                match = next((row for row in rows if row["ordinal"] == int(selector)), None)
            if match is None:
                folded = selector.casefold()
                match = next(
                    (
                        row for row in rows
                        if row["name"].casefold() == folded
                        or row["model"].casefold() == folded
                        or row["value"].casefold() == folded
                    ),
                    None,
                ) or next(
                    (row for row in rows if folded in row["name"].casefold()),
                    None,
                )
        else:
            index = _selected_index(target)
            match = next((row for row in rows if row["index"] == index), None)
            if match is not None:
                expected_name = str(target.get("name") or "")
                expected_value = str(target.get("value") or "")
                if expected_name and expected_name != match["name"]:
                    return {"ok": False, "code": "acp_model_changed"}
                if expected_value and expected_value != match["value"]:
                    return {"ok": False, "code": "acp_model_changed"}
        if match is None:
            return {"ok": False, "code": "acp_model_not_found"}
        if action == "resolve":
            return {
                "ok": True,
                "kind": "acp_model",
                "endpoint": match,
                "model_source": model_source,
                "stale": stale,
            }
        if action != "activate":
            return {"ok": False, "code": "acp_model_failed"}

        session = self._ensure_session(session_id)
        live = normalize_session_runtime_binding(
            session.runtime_binding.to_dict()
            if getattr(session, "runtime_binding", None) is not None
            else binding.to_dict()
        )
        if live.runtime_id == NATIVE_RUNTIME_ID:
            live = binding
        next_config = dict(live.acp_config or {})
        next_config[str(match["config_id"])] = str(match["value"])
        # Keep option snapshot current_value in sync for the next /model list.
        next_options = []
        for option in list(live.acp_config_options or binding.acp_config_options):
            item = dict(option)
            if item.get("id") == match["config_id"]:
                item["current_value"] = match["value"]
            next_options.append(item)
        session.runtime_binding = normalize_session_runtime_binding({
            **live.to_dict(),
            "acp_config": next_config,
            "acp_config_options": next_options or list(binding.acp_config_options),
        })
        try:
            self._save_session(self._session_path(session_id), session, "")
        except Exception:
            return {"ok": False, "code": "acp_model_failed"}
        return {
            "ok": True,
            "kind": "acp_model",
            "endpoint": {**match, "active": True},
            "runtime": session.runtime_binding.to_dict(),
        }

    def _channel_agent_runtime(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        """List local agents and open conversations bound to one of them.

        Remote callers never get to choose a working directory: a channel-made
        conversation is pinned to its own sandbox so an agent reached from a
        phone cannot be pointed at a local project tree.
        """
        from argusai.core.agent_runtimes import (
            NATIVE_RUNTIME_ID,
            discover_agent_runtimes,
            normalize_session_runtime_binding,
        )

        inventory = discover_agent_runtimes(self.settings_mgr.get().agent_runtimes)
        rows: list[dict[str, Any]] = []
        for item in inventory.get("runtimes") or []:
            if not item.get("enabled"):
                continue
            backend = str(item.get("backend") or "")
            rows.append({
                "index": len(rows),
                "ordinal": len(rows) + 1,
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "kind": "native" if backend == "native" else "acp",
                "available": (
                    True if backend == "native"
                    else bool(dict(item.get("probe") or {}).get("available"))
                ),
            })
        if action == "list":
            return {"ok": True, "agents": rows}
        if action != "create":
            return {"ok": False, "code": "agent_create_failed"}

        wanted = str(target.get("id") or "").strip().lower()
        selector = str(target.get("selector") or "").strip()
        if wanted:
            match = next((row for row in rows if row["id"] == wanted), None)
        elif selector.isdigit():
            match = next((row for row in rows if row["ordinal"] == int(selector)), None)
        else:
            folded = selector.casefold()
            match = next(
                (
                    row for row in rows
                    if row["id"] == folded or row["name"].casefold() == folded
                ),
                None,
            ) or next(
                (row for row in rows if folded and folded in row["name"].casefold()),
                None,
            )
        if match is None:
            return {"ok": False, "code": "agent_not_found"}
        if not match["available"]:
            return {"ok": False, "code": "agent_not_detected"}

        session_id = uuid.uuid4().hex
        session = self._ensure_session(session_id)
        session.runtime_binding = normalize_session_runtime_binding({
            "runtime_id": match["id"],
            "workspace_mode": "sandbox",
            # Locked up front: channel turns carry no runtime of their own, so
            # an unlocked binding would silently fall back to the native agent.
            "locked": match["id"] != NATIVE_RUNTIME_ID,
        })
        try:
            self._save_session(self._session_path(session_id), session, "")
        except Exception:
            self._assistant_sessions.pop(session_id, None)
            return {"ok": False, "code": "agent_create_failed"}
        return {"ok": True, "session_id": session_id, "agent": match}

    def _channel_session_file(self, session_id: str, selector: str) -> dict[str, Any]:
        """List or resolve one regular file inside a conversation sandbox."""
        try:
            safe_session = self._validate_session_id(session_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        root = PathManager(
            data_root=self.settings_mgr.get().data_root,
        ).assistant_sandbox(safe_session).resolve()
        root.mkdir(parents=True, exist_ok=True)

        def rows() -> list[dict[str, Any]]:
            files: list[tuple[float, dict[str, Any]]] = []
            for path in root.rglob("*"):
                try:
                    if (
                        not path.is_file()
                        or any(part in {".venv", "_venv", "node_modules"} for part in path.parts)
                    ):
                        continue
                    stat = path.stat()
                    relative = path.relative_to(root).as_posix()
                    files.append((stat.st_mtime, {
                        "name": path.name,
                        "relative_path": relative,
                        "size": stat.st_size,
                        "modified_at": stat.st_mtime,
                    }))
                except (OSError, ValueError):
                    continue
            files.sort(key=lambda item: (-item[0], item[1]["relative_path"]))
            # The index is assigned before any search filter and travels with
            # the row, so `/file <n>` means the same file whether the user is
            # looking at the full list or at search hits.
            return [
                {**item, "index": position}
                for position, (_, item) in enumerate(files[:500], 1)
            ]

        clean_selector = str(selector or "list").strip()
        available = rows()
        low = clean_selector.lower()
        if not clean_selector or low == "list" or low.startswith("search "):
            query = ""
            if low.startswith("search "):
                query = clean_selector[7:].strip()[:120].casefold()
            if query:
                available = [
                    item for item in available
                    if query in str(item.get("relative_path") or "").casefold()
                    or query in str(item.get("name") or "").casefold()
                ]
            return {
                "ok": True,
                "files": available,
                "query": clean_selector[7:].strip()[:120] if low.startswith("search ") else "",
            }
        target: Path | None = None
        if clean_selector.isdigit():
            wanted = int(clean_selector)
            match = next(
                (item for item in available if int(item.get("index") or 0) == wanted),
                None,
            )
            if match is not None:
                target = (root / match["relative_path"]).resolve()
        else:
            raw = Path(clean_selector.replace("\\", "/"))
            if raw.is_absolute() or clean_selector.startswith(("/", "\\")):
                return {"ok": False, "error": "Only sandbox-relative file paths are allowed."}
            candidate = (root / raw).resolve()
            if candidate != root and root in candidate.parents:
                target = candidate
        if target is None or not target.is_file():
            return {"ok": False, "error": "Conversation file not found. Send /file list to refresh."}
        try:
            size = target.stat().st_size
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if size > 32 * 1024 * 1024:
            return {"ok": False, "error": "File exceeds the 32 MiB channel transfer limit."}
        digest = hashlib.sha256()
        try:
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return {
            "ok": True,
            "message": f"Sending {target.name}.",
            "attachment": {
                "type": "file",
                "name": target.name,
                "mime": mime,
                "size": size,
                "sha256": digest.hexdigest(),
                "path": str(target),
                "relative_path": target.relative_to(root).as_posix(),
            },
        }

    def _pending_channel_history(self) -> dict[str, list[dict[str, Any]]]:
        queue = getattr(self, "_pending_channel_history_map", None)
        if queue is None:
            queue = {}
            self._pending_channel_history_map = queue
        return queue

    def _build_channel_outbound_record(
        self, job: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Turn a delivered outbox job into a mirror-worthy history record.

        Returns ``None`` when the delivery went to the same conversation that
        authored it (nothing to mirror) or lacks the identifiers needed to
        dedupe. The record is fully frozen here so it can be applied later on a
        different thread without touching the outbox job again.
        """
        origin_session_id = str(job.get("session_id") or "").strip()
        recipient_session_id = str(
            job.get("recipient_session_id") or origin_session_id
        ).strip()
        delivery_id = str(job.get("id") or "").strip()
        if (
            not recipient_session_id
            or recipient_session_id == origin_session_id
            or not delivery_id
        ):
            return None
        try:
            identity = self._channels_store().get_identity(
                str(job.get("identity_id") or "")
            )
            provider = str(identity.provider or "channel")
        except Exception:
            provider = "channel"
        text = str(job.get("text") or "").strip()
        attachment_names = [
            str(item.get("name") or "attachment")
            for item in list(job.get("attachments") or [])
            if isinstance(item, dict)
        ]
        meta = job.get("meta") if isinstance(job.get("meta"), dict) else {}
        reason = str(meta.get("reason") or "").strip()
        source_meta: dict[str, Any] = {
            "origin_session_id": origin_session_id,
            "delivery_id": delivery_id,
            "provider": provider,
        }
        if reason:
            source_meta["reason"] = reason[:600]
        trigger_ref = str(meta.get("trigger_ref") or "").strip()
        if trigger_ref:
            source_meta["trigger_ref"] = trigger_ref
        if attachment_names:
            source_meta["attachments"] = attachment_names[:8]
        lines = [
            f"[Delivered proactively to this {provider} chat from another ArgusAI conversation]",
            f"Origin conversation: {origin_session_id}",
        ]
        if reason:
            lines.append(f"Reason (from that conversation): {reason[:600]}")
        if text:
            lines.extend(["", text[:20_000]])
        if attachment_names:
            lines.extend(["", "Attachments: " + ", ".join(attachment_names[:8])])
        return {
            "recipient_session_id": recipient_session_id,
            "delivery_id": delivery_id,
            "content": "\n".join(lines),
            "source_meta": source_meta,
        }

    def _apply_channel_outbound_record(self, record: dict[str, Any]) -> None:
        """Append one frozen mirror record. Caller must hold the session slot."""
        from argusai.core.assistant.messages import ChatMessage

        recipient_session_id = str(record.get("recipient_session_id") or "")
        delivery_id = str(record.get("delivery_id") or "")
        session = self._ensure_session(recipient_session_id)
        if any(
            item.source == "channel_outbound" and item.source_ref == delivery_id
            for item in session.history
        ):
            return
        session.history.append(ChatMessage(
            role="assistant",
            content=str(record.get("content") or ""),
            source="channel_outbound",
            source_ref=delivery_id,
            source_meta=dict(record.get("source_meta") or {}),
            timestamp=time.time(),
        ))
        self._save_session(
            self._session_path(recipient_session_id),
            session,
            "",
        )
        self._notify_assistant_session_updated(
            recipient_session_id,
            reason="channel_outbound",
            source_ref=delivery_id,
        )

    def _record_channel_outbound_delivery(self, job: dict[str, Any]) -> None:
        """Mirror a delivered cross-session message into recipient history.

        Runs on the background outbox thread. The recipient conversation may be
        mid-turn, so we never write its history concurrently: if a turn owns the
        session we queue the record and let that turn flush it on completion;
        otherwise we reserve the session slot (the same gate real turns use) for
        the brief read-modify-write so no turn can start underneath us.
        """
        record = self._build_channel_outbound_record(job)
        if record is None:
            return
        recipient_session_id = record["recipient_session_id"]
        with self._assistant_run_lock:
            if recipient_session_id in self._assistant_running_sessions:
                self._pending_channel_history().setdefault(
                    recipient_session_id, []
                ).append(record)
                return
            self._assistant_running_sessions.add(recipient_session_id)
        try:
            self._apply_channel_outbound_record(record)
        finally:
            with self._assistant_run_lock:
                self._assistant_running_sessions.discard(recipient_session_id)
        self._flush_pending_channel_history(recipient_session_id)

    def _flush_pending_channel_history(self, session_id: str) -> None:
        """Apply records that queued while a turn owned this conversation."""
        while True:
            with self._assistant_run_lock:
                pending = self._pending_channel_history().get(session_id) or []
                if not pending:
                    self._pending_channel_history().pop(session_id, None)
                    return
                if session_id in self._assistant_running_sessions:
                    # A turn owns the slot; it flushes when it finishes.
                    return
                record = pending.pop(0)
                self._assistant_running_sessions.add(session_id)
            try:
                self._apply_channel_outbound_record(record)
            except Exception as exc:
                debug_log(
                    f"channel_outbound_history_flush_failed session_id={session_id} error={exc}"
                )
            finally:
                with self._assistant_run_lock:
                    self._assistant_running_sessions.discard(session_id)

    def _channels_runtime(self) -> ChannelRuntimeManager:
        runtime = getattr(self, "_channel_runtime_instance", None)
        gateway = self._channels_gateway()
        if runtime is None or runtime.gateway is not gateway:
            if runtime is not None:
                runtime.shutdown()
            runtime = ChannelRuntimeManager(
                self._channels_store(),
                gateway,
                outbound_delivered_handler=self._record_channel_outbound_delivery,
            )
            self._channel_runtime_instance = runtime
        return runtime

    def _confirm_channel_action(
        self,
        session_id: str,
        action: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        try:
            safe_id = self._validate_session_id(session_id)
            session = self._ensure_session(safe_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        broker = self._assistant_confirmation_broker
        external = broker.resolve_external(
            safe_id,
            session,
            action,
            source=source,
        )
        if external is not None:
            return external
        prepared = broker.prepare(session, action, source=source)
        if not prepared.get("ok"):
            return prepared
        return self._run_assistant_chat({
            "session_id": safe_id,
            "message": "",
            "hide_user_message": True,
            "confirmation_action": True,
            "confirmation": prepared["decision"],
            "message_source": "confirmation",
            "message_source_ref": source,
            "_typed_action_rpc": True,
        })

    def list_channel_providers(self) -> dict[str, Any]:
        return {
            "ok": True,
            "providers": [
                {"id": provider, **_CHANNEL_PROVIDER_META[provider]}
                for provider in sorted(CHANNEL_PROVIDERS)
            ],
        }

    def list_channels(self) -> dict[str, Any]:
        store = self._channels_store()
        runtime = self._channels_runtime()
        try:
            runtime.refresh()
        except Exception as exc:
            debug_log(f"channel runtime refresh failed error={exc}")
        statuses = {
            item["channel_id"]: item
            for item in runtime.statuses()
        }
        channels = []
        for row in store.list_channels():
            channels.append({
                **row,
                "status": statuses.get(row["id"], {
                    "connected": False,
                    "last_error": "",
                }),
                "identity_count": len(store.list_identities(row["id"])),
            })
        return {"ok": True, "channels": channels}

    def save_channel(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(data or {})
        secret_values = payload.pop("secrets", None)
        try:
            config = self._channels_store().upsert_channel(
                payload,
                secret_values=secret_values if isinstance(secret_values, dict) else None,
            )
            self._channels_runtime().refresh()
            return {"ok": True, "channel": config.to_public_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> dict[str, Any]:
        try:
            config = self._channels_store().set_enabled(channel_id, bool(enabled))
            self._channels_runtime().refresh()
            return {"ok": True, "channel": config.to_public_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_channel(self, channel_id: str) -> dict[str, Any]:
        try:
            self._channels_store().delete_channel(channel_id)
            self._channels_runtime().refresh()
            return {"ok": True, "channel_id": channel_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def test_channel(self, channel_id: str) -> dict[str, Any]:
        try:
            return self._channels_runtime().test_channel(channel_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _qq_login_runtime(self) -> tuple[threading.RLock, dict[str, dict[str, Any]]]:
        """Return the process-local QQ Onboard registry.

        ``start_onboard`` deliberately keeps the decrypted AppSecret inside the
        Python process.  The web view only sees a QR URL/image plus coarse
        progress, while successful credentials go straight into ChannelStore's
        encrypted secret store.
        """
        lock = getattr(self, "_qq_login_state_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._qq_login_state_lock = lock
        flows = getattr(self, "_qq_login_flows", None)
        if not isinstance(flows, dict):
            flows = {}
            self._qq_login_flows = flows
        return lock, flows

    @staticmethod
    def _qq_login_public(flow: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "login_id",
            "channel_id",
            "status",
            "qr_url",
            "image",
            "error",
            "channel",
            "created_at",
            "updated_at",
        }
        return {key: value for key, value in flow.items() if key in allowed}

    def _update_qq_login(self, login_id: str, **updates: Any) -> dict[str, Any] | None:
        lock, flows = self._qq_login_runtime()
        with lock:
            flow = flows.get(login_id)
            if flow is None:
                return None
            flow.update(updates)
            flow["updated_at"] = time.time()
            return dict(flow)

    def start_qq_login(self, channel_id: str) -> dict[str, Any]:
        """Start official QQ Bot QR Onboard without exposing its AppSecret."""
        target = str(channel_id or "").strip().lower()
        if not target:
            return {"ok": False, "error": "save the QQ channel before scanning"}
        try:
            existing = self._channels_store().get_channel(target)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if existing.provider != "qq":
            return {"ok": False, "error": "channel is not a QQ channel"}

        login_id = uuid.uuid4().hex
        now = time.time()
        lock, flows = self._qq_login_runtime()
        flow: dict[str, Any] = {
            "login_id": login_id,
            "channel_id": target,
            "status": "starting",
            "qr_url": "",
            "image": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "loop": None,
            "task": None,
        }
        with lock:
            flows[login_id] = flow

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def onboard() -> None:
                from qqbot_agent_sdk import start_onboard

                def on_qr_ready(url: str) -> None:
                    link = str(url or "").strip()
                    image = ""
                    if link:
                        try:
                            import segno

                            image = segno.make(link, error="m").svg_data_uri(scale=8, border=2)
                        except Exception:
                            image = ""
                    self._update_qq_login(
                        login_id,
                        status="wait",
                        qr_url=link,
                        image=image,
                    )

                result = await start_onboard(
                    on_qr_ready=on_qr_ready,
                    poll_interval=2.0,
                    poll_timeout=300.0,
                    source="argusai",
                )
                lock_inner, flows_inner = self._qq_login_runtime()
                with lock_inner:
                    current = flows_inner.get(login_id)
                    if current is None or current.get("status") == "canceled":
                        return
                channel = self._channels_store().get_channel(target)
                if channel.provider != "qq":
                    raise ValueError("channel is not a QQ channel")
                saved = self._channels_store().upsert_channel(
                    {
                        **channel.to_dict(),
                        # A successful official Onboard is an explicit request
                        # to connect this bot; leaving it disabled would make a
                        # completed scan look broken.
                        "enabled": True,
                    },
                    secret_values={
                        "app_id": str(result.app_id or ""),
                        "client_secret": str(result.client_secret or ""),
                    },
                )
                self._channels_runtime().refresh()
                self._update_qq_login(
                    login_id,
                    status="confirmed",
                    channel=saved.to_public_dict(),
                )

            task = loop.create_task(onboard())
            with lock:
                current = flows.get(login_id)
                if current is None or current.get("status") == "canceled":
                    task.cancel()
                else:
                    current["loop"] = loop
                    current["task"] = task
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                self._update_qq_login(login_id, status="canceled")
            except TimeoutError as exc:
                self._update_qq_login(login_id, status="expired", error=str(exc))
            except Exception as exc:
                status = "expired" if type(exc).__name__ == "OnboardExpiredError" else "error"
                self._update_qq_login(
                    login_id,
                    status=status,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                with lock:
                    current = flows.get(login_id)
                    if current is not None:
                        current["loop"] = None
                        current["task"] = None
                loop.close()

        thread = threading.Thread(
            target=worker,
            daemon=True,
            name=f"argusai-qq-onboard-{target}",
        )
        flow["thread"] = thread
        response = {"ok": True, **self._qq_login_public(dict(flow))}
        thread.start()
        return response

    def poll_qq_login(self, login_id: str, channel_id: str = "") -> dict[str, Any]:
        safe_id = str(login_id or "").strip()
        target = str(channel_id or "").strip().lower()
        lock, flows = self._qq_login_runtime()
        with lock:
            flow = flows.get(safe_id)
            if flow is None:
                return {"ok": False, "error": "QQ login session not found"}
            if target and flow.get("channel_id") != target:
                return {"ok": False, "error": "QQ login session belongs to another channel"}
            return {"ok": True, **self._qq_login_public(dict(flow))}

    def cancel_qq_login(self, login_id: str) -> dict[str, Any]:
        safe_id = str(login_id or "").strip()
        lock, flows = self._qq_login_runtime()
        with lock:
            flow = flows.get(safe_id)
            if flow is None:
                return {"ok": True, "status": "missing"}
            if flow.get("status") in {"confirmed", "expired", "error", "canceled"}:
                return {"ok": True, "status": str(flow.get("status") or "")}
            flow["status"] = "canceled"
            flow["updated_at"] = time.time()
            loop = flow.get("loop")
            task = flow.get("task")
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        return {"ok": True, "status": "canceled"}

    def _cancel_all_qq_logins(self) -> None:
        lock, flows = self._qq_login_runtime()
        with lock:
            pending = [
                login_id
                for login_id, flow in flows.items()
                if flow.get("status") in {"starting", "wait"}
            ]
        for login_id in pending:
            self.cancel_qq_login(login_id)

    def start_wechat_login(self) -> dict[str, Any]:
        """Request a WeChat iLink sign-in QR code for the user to scan."""
        try:
            return {"ok": True, **ilink_request_qrcode()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def poll_wechat_login(self, qrcode: str, channel_id: str = "") -> dict[str, Any]:
        """Advance one step of the QR handshake, saving the token once granted.

        The bot token is written straight into the channel's secret store rather
        than handed back to the web view, so it never crosses the bridge.
        """
        try:
            payload = ilink_qrcode_status(str(qrcode or ""))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        status = str(payload.get("status") or "wait")
        if status != "confirmed":
            return {"ok": True, "status": status}
        token = str(payload.get("bot_token") or "")
        if not token:
            return {"ok": False, "error": "WeChat confirmed the scan but returned no bot_token"}
        identity = {
            "bot_id": str(payload.get("ilink_bot_id") or ""),
            "user_id": str(payload.get("ilink_user_id") or ""),
            "base_url": str(payload.get("baseurl") or ILINK_API_BASE),
        }
        target = str(channel_id or "").strip()
        if not target:
            return {"ok": False, "error": "save the channel before signing in"}
        try:
            store = self._channels_store()
            existing = store.get_channel(target)
            if existing.provider != "wechat":
                return {"ok": False, "error": "channel is not a WeChat iLink channel"}
            config = store.upsert_channel(
                {
                    "id": target,
                    "provider": "wechat",
                    "name": existing.name,
                    # Signing in is the only way this channel can ever come
                    # online, and until the adapter polls, WeChat shows the bot
                    # as unreachable and /pair goes nowhere. Leaving it disabled
                    # here would make a successful scan look like a failure.
                    "enabled": True,
                    "settings": {**dict(existing.settings), **identity},
                },
                secret_values={"bot_token": token},
            )
            self._channels_runtime().refresh()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "status": "confirmed",
            "identity": identity,
            "channel": config.to_public_dict(),
        }

    def create_channel_pairing(
        self,
        channel_id: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        try:
            safe_session = self._validate_session_id(session_id) if session_id else ""
            return {
                "ok": True,
                **self._channels_store().create_pairing(
                    channel_id,
                    bound_session_id=safe_session,
                ),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_channel_identities(self, channel_id: str = "") -> dict[str, Any]:
        identities = self._channels_store().list_identities(channel_id)
        return {
            "ok": True,
            "identities": [item.to_dict() for item in identities],
        }

    def bind_channel_identity(self, identity_id: str, session_id: str) -> dict[str, Any]:
        try:
            safe_session = self._validate_session_id(session_id) if session_id else ""
            identity = self._channels_store().bind_identity(identity_id, safe_session)
            return {"ok": True, "identity": identity.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_channel_identity_permissions(
        self,
        identity_id: str,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            identity = self._channels_store().set_identity_permissions(
                identity_id,
                list(permissions or []),
            )
            return {"ok": True, "identity": identity.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_channel_identity(self, identity_id: str) -> dict[str, Any]:
        try:
            self._channels_store().delete_identity(identity_id)
            return {"ok": True, "identity_id": identity_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
