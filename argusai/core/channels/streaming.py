"""Provider output streams, independent of agent-authored channel_reply bubbles.

Each stream is scoped to one inbound event, thread, and optional delivery. Transient
previews never enter the durable outbox; callers keep their final reply if finalization fails.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .models import ChannelDeliveryError, ChannelReply


def _utf16_chunks(text: str, limit: int) -> list[str]:
    """Respect provider character budgets without splitting emoji surrogates."""
    chunks: list[str] = []
    start = units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(text[start:index])
            start, units = index, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def stream_mode(config: Any) -> str:
    provider = config.provider
    if config.settings.get("streaming") is False:
        return "staged"
    if provider in {"qq", "telegram", "slack", "feishu"}:
        return "native"
    if provider == "discord":
        return "edit"
    if provider == "dingtalk" and config.settings.get("card_template_id"):
        return "native"
    if provider == "webhook" and config.settings.get("stream_protocol") == "v1":
        return "webhook_v1"
    return "staged"


class OutputStreams:
    """Serialized, bounded stream state owned by a single channel adapter."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.states: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.peer_users: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        from .adapters import check_api_response

        kwargs.setdefault("timeout", 20)
        response = httpx.request(method, url, **kwargs)
        payload = check_api_response(response, "Channel stream failed")
        if payload.get("code") not in (None, 0, "0", ""):
            raise ChannelDeliveryError(f"Channel stream failed: {payload.get('code')}: {payload.get('message', '')}")
        return payload

    def is_complete(self, *, chat_id: str, source_message_id: str, text: str,
                    thread_id: str = "", stream_id: str = "") -> bool:
        """Recognize a retry of one completed delivery without replaying prefixes."""
        key = (chat_id, thread_id, source_message_id, stream_id)
        with self.lock:
            state = self.states.get(key)
            return bool(
                state and state["done"] and not state.get("failed")
                and state["last"] == text
                and time.monotonic() - state["touched"] <= 86400
            )

    def update(self, *, chat_id: str, source_message_id: str, text: str,
               final: bool = False, thread_id: str = "", stream_id: str = "") -> bool:
        if stream_mode(self.adapter.config) == "staged":
            return False
        key = (chat_id, thread_id, source_message_id, stream_id)
        with self.lock:
            now = time.monotonic()
            # Expiry is only housekeeping. Active streams are refreshed each call.
            for old_key, old in list(self.states.items()):
                if now - old["touched"] > 86400:
                    self.states.pop(old_key, None)
            state = self.states.get(key)
            if state is None:
                if len(self.states) >= 256:
                    completed = next((k for k, v in self.states.items()
                                      if v["done"] or v.get("failed")), None)
                    if completed is None:
                        return False  # Never evict an active remote stream.
                    self.states.pop(completed)
                state = {"id": uuid.uuid4().hex, "last": "", "touched": now,
                         "pushed": 0.0, "sequence": 0, "done": False}
                self.states[key] = state
            state["touched"] = now
            if state.get("failed"):
                return False
            if state["done"]:
                return text == state["last"]
            # Coalesce frames without sleeping on the model callback thread.
            if not final and now - state["pushed"] < 1.0:
                return True
            if not final and text == state["last"]:
                return True
            try:
                handler = getattr(self, "_" + self.adapter.config.provider)
                handler(state, chat_id, source_message_id, thread_id, text, final)
                state.update(last=text, pushed=now, done=final)
                return True
            except Exception:
                # A final failure must reach the caller so the normal outbox
                # fallback can preserve the complete answer.
                state["failed"] = True
                self._close_failed(state, chat_id)
                raise

    def _close_failed(self, state: dict, chat: str) -> None:
        """Best-effort close after an update failed; never mask that failure."""
        try:
            provider = self.adapter.config.provider
            if provider == "slack" and state.get("ts"):
                self.request("POST", f"{self.adapter.API_BASE}/chat.stopStream",
                             headers=self.adapter._bot_headers(),
                             json={"channel": chat, "ts": state["ts"]})
            elif provider == "feishu" and state.get("card_id"):
                self._feishu_close(state)
            elif provider == "dingtalk" and state.get("created"):
                self._ding_frame(state, state["last"], True, failed=True)
        except Exception:
            pass

    def _telegram(self, state: dict, chat: str, source: str, thread: str,
                  text: str, final: bool) -> None:
        adapter = self.adapter
        if final:
            adapter.send(ChannelReply(text=text, external_chat_id=chat,
                                      external_thread_id=thread,
                                      reply_to_message_id=source))
            return
        payload: dict[str, Any] = {
            "chat_id": chat, "draft_id": int(state["id"][:7], 16) or 1,
            # Raw partial text avoids malformed half-open Markdown entities.
            "text": _utf16_chunks(text, 4000)[-1],
        }
        if thread:
            payload["message_thread_id"] = int(thread)
        self.request("POST", f"{adapter._base}/sendMessageDraft", json=payload,
                     **adapter._httpx_kwargs(timeout=20))

    def _slack(self, state: dict, chat: str, source: str, thread: str,
               text: str, final: bool) -> None:
        adapter = self.adapter
        headers = adapter._bot_headers()
        base = adapter.API_BASE
        # Streams expire at ten minutes. Roll over before that, and split only
        # for the transport's hard length limit (never for human bubble style).
        if state.get("ts") and time.monotonic() - state["started"] >= 540:
            old_ts = state.pop("ts")
            if time.monotonic() - state["started"] < 600:
                self.request("POST", f"{base}/chat.stopStream", headers=headers,
                             json={"channel": chat, "ts": old_ts,
                                   "session_status": "processing"})
            if final and text == state["last"]:
                return  # The old message already contains the entire answer.
        previous = state["last"]
        if not text.startswith(previous):
            raise ValueError("Slack stream requires append-only text")
        pending = text[len(previous):]
        while pending or not state.get("ts"):
            used = int(state.get("used", 0)) if state.get("ts") else 0
            part, pending = pending[:12000 - used], pending[12000 - used:]
            if not state.get("ts"):
                payload = self.request("POST", f"{base}/chat.startStream", headers=headers,
                                       json={"channel": chat, "thread_ts": thread or source,
                                             "markdown_text": part})
                state["ts"] = payload["ts"]
                state["started"] = time.monotonic()
            elif part:
                self.request("POST", f"{base}/chat.appendStream", headers=headers,
                             json={"channel": chat, "ts": state["ts"], "markdown_text": part})
            state["used"] = used + len(part)
            if pending:
                self.request("POST", f"{base}/chat.stopStream", headers=headers,
                             json={"channel": chat, "ts": state.pop("ts"),
                                   "session_status": "processing"})
            else:
                break
        if final:
            self.request("POST", f"{base}/chat.stopStream", headers=headers,
                         json={"channel": chat, "ts": state["ts"]})

    def _discord(self, state: dict, chat: str, source: str, thread: str,
                 text: str, final: bool) -> None:
        base = f"{self.adapter.API_BASE}/channels/{quote(thread or chat, safe='')}/messages"
        ids = state.setdefault("message_ids", [])
        # Discord caps content at 2000 chars. Keep stable message IDs across
        # updates, including when a response crosses several message lengths.
        previous_chunks = _utf16_chunks(state["last"], 2000)
        for index, content in enumerate(_utf16_chunks(text, 2000)):
            if index < len(ids) and index < len(previous_chunks) and previous_chunks[index] == content:
                continue
            payload = {"content": content, "allowed_mentions": {"parse": []}}
            if index == len(ids):
                result = self.request("POST", base, headers=self.adapter._headers(), json=payload)
                ids.append(result["id"])
            else:
                self.request("PATCH", f"{base}/{quote(ids[index], safe='')}",
                             headers=self.adapter._headers(), json=payload)

    def _feishu_token(self) -> str:
        getter = getattr(self.adapter, "_get_feishu_token", None)
        if callable(getter):
            return getter()
        data = self.request("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                            json={"app_id": self.adapter.credentials.get("app_id", ""),
                                  "app_secret": self.adapter.credentials.get("app_secret", "")})
        return str(data["tenant_access_token"])

    def _feishu(self, state: dict, chat: str, source: str, thread: str,
                text: str, final: bool) -> None:
        base = "https://open.feishu.cn/open-apis"
        headers = {"Authorization": f"Bearer {self._feishu_token()}"}
        if not state.get("card_id"):
            card = {"schema": "2.0", "config": {"streaming_mode": True, "update_multi": True},
                    "body": {"elements": [{"tag": "markdown", "element_id": "answer", "content": text}]}}
            payload = self.request("POST", f"{base}/cardkit/v1/cards", headers=headers,
                                   json={"type": "card_json", "data": json.dumps(card, ensure_ascii=False)})
            state["card_id"] = payload["data"]["card_id"]
            message = {"msg_type": "interactive", "content": json.dumps(
                {"type": "card", "data": {"card_id": state["card_id"]}})}
            if thread:
                self.request("POST", f"{base}/im/v1/messages/{quote(source, safe='')}/reply",
                             headers=headers, json={**message, "reply_in_thread": True})
            else:
                self.request("POST", f"{base}/im/v1/messages", headers=headers,
                             params={"receive_id_type": "chat_id"},
                             json={**message, "receive_id": chat})
        else:
            state["sequence"] += 1
            self.request("PUT", f"{base}/cardkit/v1/cards/{quote(state['card_id'], safe='')}/elements/answer/content",
                         headers=headers, json={"content": text, "sequence": state["sequence"],
                                                "uuid": uuid.uuid4().hex})
        if final:
            self._feishu_close(state)

    def _feishu_close(self, state: dict) -> None:
        state["sequence"] += 1
        self.request("PATCH", f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{quote(state['card_id'], safe='')}/settings",
                     headers={"Authorization": f"Bearer {self._feishu_token()}"},
                     json={"settings": json.dumps({"streaming_mode": False}),
                           "sequence": state["sequence"], "uuid": uuid.uuid4().hex})

    def _dingtalk(self, state: dict, chat: str, source: str, thread: str,
                  text: str, final: bool) -> None:
        adapter = self.adapter
        settings = adapter.config.settings
        staff_id = self.peer_users.get(chat, "")
        if not staff_id:
            raise ValueError("DingTalk card delivery requires the inbound sender's staff ID")
        headers = {"x-acs-dingtalk-access-token": adapter._get_dingtalk_token()}
        content_key = str(settings.get("card_content_key") or "content")
        if not state.get("created"):
            # Mirrors the official dingtalk-stream SDK CardReplier contract.
            self.request("POST", "https://api.dingtalk.com/v1.0/card/instances", headers=headers,
                         json={"cardTemplateId": settings["card_template_id"], "outTrackId": state["id"],
                               "cardData": {"cardParamMap": {content_key: ""}}, "callbackType": "STREAM",
                               "imRobotOpenSpaceModel": {"supportForward": False}})
            self.request("POST", "https://api.dingtalk.com/v1.0/card/instances/deliver", headers=headers,
                         json={"outTrackId": state["id"], "userIdType": 1,
                               "openSpaceId": f"dtv1.card//IM_ROBOT.{staff_id}",
                               "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"}})
            state["created"] = True
        self._ding_frame(state, text, final)

    def _ding_frame(self, state: dict, text: str, final: bool, failed: bool = False) -> None:
        self.request("PUT", "https://api.dingtalk.com/v1.0/card/streaming",
                     headers={"x-acs-dingtalk-access-token": self.adapter._get_dingtalk_token()},
                     json={"outTrackId": state["id"], "guid": uuid.uuid4().hex,
                           "key": str(self.adapter.config.settings.get("card_content_key") or "content"),
                           "content": text, "isFull": True, "isFinalize": final, "isError": failed})

    def _webhook(self, state: dict, chat: str, source: str, thread: str,
                 text: str, final: bool) -> None:
        url = (self.adapter._reply_urls.get(chat)
               or str(self.adapter.config.settings.get("outbound_url") or ""))
        if not url:
            raise ValueError("Webhook stream requires outbound_url")
        state["sequence"] += 1
        event = "stream_end" if final else "stream_delta" if state.get("started") else "stream_start"
        self.request("POST", url, json={"type": event, "protocol": "argusai.stream.v1",
                     "stream_id": state["id"], "sequence": state["sequence"],
                     "chat_id": chat, "thread_id": thread, "reply_to_message_id": source,
                     "text": text, "mode": "replace", "final": final})
        state["started"] = True
