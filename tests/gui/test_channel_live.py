"""/live mid-turn pushing: what reaches the chat, and exactly once."""

from __future__ import annotations

import threading

from argusai.gui.channels import ChannelsMixin


class _Gateway:
    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.blocks: list[dict] = []
        self.fanout: list[dict] = []
        self._fail = fail

    def queue_live_update(self, _identity_id, text="", raw_blocks=()):
        if self._fail:
            raise RuntimeError("channel is offline")
        if text:
            self.texts.append(text)
        for block in raw_blocks or ():
            self.blocks.append(block)
        return True

    def push_session_interaction(self, session_id, raw_blocks=None, *, skip_identity_id="", text=""):
        self.fanout.append({
            "session_id": session_id,
            "blocks": list(raw_blocks or []),
            "skip_identity_id": skip_identity_id,
            "text": text,
        })
        return 0


class _Api(ChannelsMixin):
    """Just enough host to drive the live wrapper."""

    def __init__(self, script, gateway=None) -> None:
        self._assistant_run_lock = threading.Lock()
        self._assistant_running_sessions: set[str] = set()
        self.gateway = gateway or _Gateway()
        self._script = script

    def get_assistant_session(self, _session_id):
        return {"ok": True, "channel_live": True}

    def _channels_gateway(self):
        return self.gateway

    def _run_assistant_chat(self, params, on_delta=None, on_block=None, on_segment=None):
        return self._script(params, on_delta, on_block, on_segment)


def _params() -> dict:
    return {
        "session_id": "session-1",
        "message": "hi",
        "message_source": "channel",
        "message_source_ref": "identity-1",
    }


_PLAN = {
    "type": "todo_list",
    "mode": "assessment_plan",
    "todos": [{"title": "recon", "status": "pending"}],
}


def test_a_card_pushed_live_is_not_repeated_in_the_final_reply() -> None:
    # Assistant UI blocks carry no id, so id-based dedup let every plan and
    # decision card arrive twice, with two live action tokens for one decision.
    def script(_params, on_delta, on_block, on_segment):
        on_block(dict(_PLAN))
        on_delta("working on it")
        on_segment(1)
        return {"ok": True, "text": "", "blocks": [dict(_PLAN)]}

    api = _Api(script)
    result = api._run_channel_assistant_chat(_params())

    assert api.gateway.blocks == [_PLAN]
    assert api.gateway.texts == ["working on it"]
    assert result["blocks"] == []


def test_normalized_final_block_with_new_id_is_still_deduped() -> None:
    """ChatResponse.normalize_blocks stamps a fresh id after the live emit."""
    card = {
        "type": "structured_card",
        "title": "核心能力一览",
        "subtitle": "按你常用的技术方向分组",
        "sections": [{"type": "list", "items": [{"label": "JS/WASM 逆向"}]}],
    }

    def script(_params, on_delta, on_block, on_segment):
        on_block(dict(card))
        return {
            "ok": True,
            "text": "卡片已重新发出。",
            "blocks": [{
                **card,
                "id": "blk_after_normalize",
                "version": 2,
                "source": "model",
            }],
        }

    api = _Api(script)
    result = api._run_channel_assistant_chat(_params())

    assert api.gateway.blocks == [card]
    assert result["blocks"] == []
    assert result["text"] == "卡片已重新发出。"
    assert result["channel_live_delivered"] is True


def test_an_edited_plan_is_pushed_again() -> None:
    """Progress on the same plan is new content, not a duplicate."""
    done = {**_PLAN, "todos": [{"title": "recon", "status": "completed"}]}

    def script(_params, on_delta, on_block, on_segment):
        on_block(dict(_PLAN))
        on_block(dict(_PLAN))
        on_block(dict(done))
        return {"ok": True, "text": "", "blocks": []}

    api = _Api(script)
    api._run_channel_assistant_chat(_params())

    assert api.gateway.blocks == [_PLAN, done]


def test_text_survives_a_failed_live_push() -> None:
    def script(_params, on_delta, _on_block, on_segment):
        on_delta("the actual answer")
        on_segment(1)
        return {"ok": True, "text": "the actual answer", "blocks": []}

    api = _Api(script, gateway=_Gateway(fail=True))
    result = api._run_channel_assistant_chat(_params())

    # Blanking the reply here would leave the remote user with nothing at all.
    assert result["text"] == "the actual answer"
    assert result["channel_live_delivered"] is False


def test_a_delivered_turn_is_marked_so_trailing_filler_can_be_dropped() -> None:
    def script(_params, on_delta, _on_block, on_segment):
        on_delta("step one done")
        on_segment(1)
        return {"ok": True, "text": "完成。", "blocks": []}

    api = _Api(script)
    result = api._run_channel_assistant_chat(_params())

    assert api.gateway.texts == ["step one done"]
    assert result["channel_live_delivered"] is True


def test_wait_decision_card_is_force_pushed_to_the_channel() -> None:
    """stop_for_user_decision is final-only in the runtime; channels must push it."""
    decision = {
        "type": "user_decision",
        "question": "选择部署方式？",
        "options": ["开始部署", "取消"],
    }

    def script(_params, on_delta, on_block, on_segment):
        # Runtime never streams user_decision through on_block.
        on_delta("评估完成，请选择下一步。")
        on_segment(1)
        return {
            "ok": True,
            "text": "[等待用户决定] 评估完成，请选择下一步。",
            "status": "awaiting_user_decision",
            "blocks": [dict(decision)],
        }

    api = _Api(script)
    result = api._run_channel_assistant_chat(_params())

    assert api.gateway.blocks == [decision]
    assert result.get("channel_wait_cards_delivered") is True
    assert not any(
        isinstance(block, dict) and block.get("type") == "user_decision"
        for block in result.get("blocks") or []
    )


def test_todo_plan_is_bundled_before_decision_in_one_push() -> None:
    """Avoid Feishu showing '以上是多步任务计划' before the plan itself."""
    plan = {
        "type": "todo_list",
        "flat": [
            {"content": "SSH 连通性", "status": "pending"},
            {"content": "磁盘快照", "status": "pending"},
        ],
    }
    decision = {
        "type": "user_decision",
        "question": "以上是多步任务计划（2 项待处理）。请确认是否按此计划开始执行？",
        "options": ["开始执行", "修改计划", "取消任务"],
        "decision_kind": "todo_plan",
    }

    def script(_params, on_delta=None, on_block=None, on_segment=None):
        return {
            "ok": True,
            "text": "",
            "status": "awaiting_user_decision",
            "blocks": [dict(plan), dict(decision)],
        }

    class _OfflineLiveApi(_Api):
        def get_assistant_session(self, _session_id):
            return {"ok": True, "channel_live": False}

    api = _OfflineLiveApi(script)
    result = api._run_channel_assistant_chat(_params())

    assert [block["type"] for block in api.gateway.blocks] == [
        "todo_list",
        "user_decision",
    ]
    assert result.get("channel_wait_cards_delivered") is True
    assert result.get("blocks") == []


def test_failed_live_push_keeps_wait_card_for_sync_reply() -> None:
    """No ``send`` / failed outbox must not strip cards from the turn result."""
    decision = {
        "type": "user_decision",
        "question": "继续？",
        "options": ["是", "否"],
    }

    def script(_params, on_delta, on_block, on_segment):
        return {
            "ok": True,
            "text": "[等待用户决定]",
            "status": "awaiting_user_decision",
            "blocks": [dict(decision)],
        }

    class _SilentGateway(_Gateway):
        def queue_live_update(self, _identity_id, text="", raw_blocks=()):
            return False

    api = _Api(script, gateway=_SilentGateway())
    result = api._run_channel_assistant_chat(_params())

    assert result.get("channel_wait_cards_delivered") is not True
    assert any(
        isinstance(block, dict) and block.get("type") == "user_decision"
        for block in result.get("blocks") or []
    )


def test_wait_decision_card_is_pushed_when_live_is_off() -> None:
    decision = {
        "type": "user_decision",
        "question": "继续？",
        "options": ["是", "否"],
    }

    class _OfflineLiveApi(_Api):
        def get_assistant_session(self, _session_id):
            return {"ok": True, "channel_live": False}

    def script(_params, on_delta=None, on_block=None, on_segment=None):
        return {
            "ok": True,
            "text": "[等待用户决定]",
            "status": "awaiting_user_decision",
            "blocks": [dict(decision)],
        }

    api = _OfflineLiveApi(script)
    result = api._run_channel_assistant_chat(_params())

    assert api.gateway.blocks == [decision]
    assert result.get("channel_wait_cards_delivered") is True
