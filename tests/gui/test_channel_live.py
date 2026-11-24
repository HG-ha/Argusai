"""/live mid-turn pushing: what reaches the chat, and exactly once."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

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

    def _run_assistant_chat(self, params, on_delta=None, on_block=None, on_segment=None, on_status=None):
        self.on_status = on_status
        return self._script(params, on_delta, on_block, on_segment)


def _params() -> dict:
    return {
        "session_id": "session-1",
        "message": "hi",
        "message_source": "channel",
        "message_source_ref": "identity-1",
    }


@pytest.mark.parametrize("provider", ["qq", "telegram", "discord", "slack", "feishu", "dingtalk", "wecom", "wechat", "webhook"])
def test_channel_config_can_disable_live_output_even_when_session_live_is_on(provider):
    def script(_params, on_delta, on_block, on_segment):
        assert on_delta is None and on_block is None and on_segment is None
        return {"ok": True, "text": "send this complete answer", "blocks": []}

    api = _Api(script)
    result = api._run_channel_assistant_chat({**_params(), "channel": {"provider": provider, "message_id": "source", "streaming": False}})
    assert result["text"] == "send this complete answer"
    assert not api.gateway.texts and not api.gateway.blocks


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


def test_qq_live_turn_uses_native_replace_stream_and_done() -> None:
    frames = []

    class NativeRuntime:
        def stream_live_update(
            self,
            identity_id,
            *,
            source_message_id,
            text,
            final=False,
        ):
            frames.append({
                "identity_id": identity_id,
                "source_message_id": source_message_id,
                "text": text,
                "final": final,
            })
            return True

    def script(_params, on_delta, _on_block, on_segment):
        on_delta("正在分析")
        on_segment(1)
        on_delta("，已经完成。")
        return {"ok": True, "text": "正在分析，已经完成。", "blocks": []}

    api = _Api(script)
    api._channel_runtime_instance = NativeRuntime()
    params = _params()
    params["channel"] = {
        "provider": "qq",
        "message_id": "qq-message-1",
    }

    result = api._run_channel_assistant_chat(params)

    assert frames[0]["text"] == "正在分析"
    assert frames[-1] == {
        "identity_id": "identity-1",
        "source_message_id": "qq-message-1",
        "text": "正在分析，已经完成。",
        "final": True,
    }
    assert api.gateway.texts == []
    assert result["text"] == ""
    assert result["channel_live_delivered"] is True


def test_progress_before_terminal_reply_can_finish_without_gateway_filtering() -> None:
    frames = []
    class Runtime:
        def stream_capability(self, identity_id):
            return "native"
        def stream_live_update(self, identity_id, **frame):
            frames.append(frame)
            return True
    def script(params, on_delta, on_block, on_segment):
        on_delta("正在检查配置。")
        return {"ok": True, "text": "", "blocks": []}
    api = _Api(script)
    api._channel_runtime_instance = Runtime()
    api._run_channel_assistant_chat({**_params(), "channel": {"provider": "qq", "message_id": "source"}})
    assert frames[-1]["final"] is True
    assert all(frame["text"] == "正在检查配置。" for frame in frames)


@pytest.mark.parametrize("mode", ["native", "edit", "webhook_v1"])
def test_other_provider_modes_use_stream_capability(mode) -> None:
    frames = []
    class Runtime:
        def stream_capability(self, identity_id):
            return mode
        def stream_live_update(self, identity_id, **frame):
            frames.append(frame)
            return True
    def script(params, on_delta, on_block, on_segment):
        on_delta("first")
        on_segment(1)
        on_delta(" second")
        return {"ok": True, "text": "first second", "blocks": []}
    api = _Api(script)
    api._channel_runtime_instance = Runtime()
    params = {**_params(), "channel": {"provider": "slack", "message_id": "source"}}
    result = api._run_channel_assistant_chat(params)
    assert frames[-1]["text"] == "first second"
    assert frames[-1]["final"] is True
    assert api.gateway.texts == []
    assert result["text"] == ""


def test_stream_final_failure_retains_answer_when_outbox_is_offline() -> None:
    class Runtime:
        def stream_capability(self, identity_id):
            return "native"
        def stream_live_update(self, identity_id, **frame):
            return not frame["final"]
    def script(params, on_delta, on_block, on_segment):
        on_delta("complete answer")
        return {"ok": True, "text": "complete answer", "blocks": []}
    api = _Api(script, _Gateway(fail=True))
    api._channel_runtime_instance = Runtime()
    params = {**_params(), "channel": {"provider": "slack", "message_id": "source"}}
    result = api._run_channel_assistant_chat(params)
    assert result["text"] == "complete answer"
    assert result["channel_live_delivered"] is False


def test_feishu_progress_is_complete_before_tool_execution_and_has_separate_bubbles() -> None:
    frames = []

    class Runtime:
        def stream_capability(self, _identity_id):
            return "native"

        def stream_live_update(self, _identity_id, **frame):
            frames.append(frame)
            return True

    def script(_params, on_delta, on_block, on_segment):
        on_delta("先检查")
        on_delta("接口和权限。")
        # A tool may run for minutes before the next model round. Its start
        # must flush the complete Agent message, without forwarding status text.
        api.on_status("正在执行工具：scan")
        assert frames[-1]["text"] == "先检查接口和权限。"
        assert frames[-1]["final"] is True
        on_block(dict(_PLAN))
        on_segment(1)
        on_delta("检查完成，继续验证。")
        assert frames[-1]["text"] == "检查完成，继续验证。"
        assert frames[-1]["final"] is False
        return {"ok": True, "text": "检查完成，继续验证。", "blocks": [dict(_PLAN)]}

    api = _Api(script)
    api._channel_runtime_instance = Runtime()
    result = api._run_channel_assistant_chat({
        **_params(), "channel": {"provider": "feishu", "message_id": "inbound"},
    })

    completed = [frame for frame in frames if frame["final"]]
    assert [frame["text"] for frame in completed] == ["先检查接口和权限。", "检查完成，继续验证。"]
    assert completed[0]["stream_id"] != completed[1]["stream_id"]
    assert api.gateway.texts == []
    assert api.gateway.blocks == [_PLAN]
    assert result["text"] == ""
    assert result["blocks"] == []


@pytest.mark.parametrize("raises", [False, True])
def test_feishu_failed_stream_delivers_progress_before_turn_finishes(raises) -> None:
    attempts = []

    class Runtime:
        def stream_capability(self, _identity_id):
            return "native"

        def stream_live_update(self, _identity_id, **frame):
            attempts.append(frame)
            if raises:
                raise RuntimeError("CardKit unavailable")
            return False

    def script(_params, on_delta, on_block, on_segment):
        on_delta("第一步完成。")
        on_block(dict(_PLAN))
        assert api.gateway.texts == ["第一步完成。"]
        on_segment(1)
        on_delta("正在检查第二步。")
        api.on_status("正在执行工具：inspect")
        assert api.gateway.texts == ["第一步完成。", "正在检查第二步。"]
        return {"ok": True, "text": "正在检查第二步。", "blocks": [dict(_PLAN)]}

    api = _Api(script)
    api._channel_runtime_instance = Runtime()
    result = api._run_channel_assistant_chat({
        **_params(), "channel": {"provider": "feishu", "message_id": "inbound"},
    })

    assert len(attempts) == 1
    assert result["text"] == ""
    assert result["channel_live_delivered"] is True


@pytest.mark.parametrize("cardkit_granted", [False, True])
def test_feishu_progress_reaches_transport_during_autonomous_run(tmp_path, monkeypatch, cardkit_granted):
    import json
    import httpx
    from argusai.core.channels.adapters import StreamSDKAdapter
    from argusai.core.channels.runtime import ChannelRuntimeManager
    from argusai.core.channels.store import ChannelStore

    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/cardkit/v1/cards"):
            if not cardkit_granted:
                return httpx.Response(200, json={
                    "code": 99991672, "msg": "Access denied: cardkit:card:write",
                })
            data = {"card_id": f"card-{len(requests)}"}
        else:
            data = {"message_id": f"om-{len(requests)}"}
        return httpx.Response(200, json={"code": 0, "data": data})

    store = ChannelStore(tmp_path / "channels")
    channel = store.upsert_channel({"id": "feishu-test", "provider": "feishu", "name": "Test"})
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(channel.id, pairing["code"],
                                     external_user_id="user", external_chat_id="chat")
    store.set_identity_permissions(identity.id, ["chat", "send"])
    adapter = StreamSDKAdapter(channel, {})
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "test-token")

    def assert_complete_before_tool(text):
        if cardkit_granted:
            assert requests[-2][0] == "PUT"
            assert requests[-2][2]["content"] == text
            assert json.loads(requests[-1][2]["settings"]) == {"config": {"streaming_mode": False}}
        else:
            assert requests[-1][0] == "PATCH"
            card = json.loads(requests[-1][2]["content"])
            assert any(e.get("content") == text for e in card["body"]["elements"])

    def script(_params, on_delta, on_block, on_segment):
        on_delta("先检查")
        on_delta("接口和权限。")
        api.on_status("正在执行工具")
        assert_complete_before_tool("先检查接口和权限。")
        on_block(dict(_PLAN))
        on_segment(1)
        on_delta("第一步完成，")
        on_delta("继续验证。")
        api.on_status("正在执行下一工具")
        assert_complete_before_tool("第一步完成，继续验证。")
        return {"ok": True, "text": "第一步完成，继续验证。", "blocks": [dict(_PLAN)]}

    api = _Api(script)
    runtime = ChannelRuntimeManager(store, api.gateway)
    runtime._adapters[channel.id] = adapter
    api._channel_runtime_instance = runtime
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(httpx, "request", client.request)
        result = api._run_channel_assistant_chat({
            **_params(), "message_source_ref": identity.id,
            "channel": {"provider": "feishu", "message_id": "inbound"},
        })

    assert len([r for r in requests if r[0] == "POST" and r[1].endswith("/im/v1/messages")]) == 2
    assert all(state["done"] for state in adapter._output_streams.states.values())
    assert result["text"] == ""
    assert api.gateway.texts == []
    assert api.gateway.blocks == [_PLAN]


def test_cancelled_turn_finalizes_remote_stream() -> None:
    frames = []
    class Runtime:
        def stream_capability(self, identity_id):
            return "native"
        def stream_live_update(self, identity_id, **frame):
            frames.append(frame)
            return True
    def script(params, on_delta, on_block, on_segment):
        on_delta("partial")
        raise RuntimeError("cancelled")
    api = _Api(script)
    api._channel_runtime_instance = Runtime()
    with pytest.raises(RuntimeError, match="cancelled"):
        api._run_channel_assistant_chat({**_params(), "channel": {"message_id": "source"}})
    assert frames[-1]["final"] is True


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
