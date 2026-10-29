from argusai.core.assistant import AssistantSession, ChatMessage


def _approve_plan(session: AssistantSession) -> None:
    session._approve_todo_plan()


def test_set_todos_preserves_child_actions() -> None:
    session = AssistantSession()

    result = session.set_todos([
        {
            "id": "phase.validation",
            "title": "漏洞验证",
            "status": "in_progress",
            "children": [
                {
                    "id": "validate.login-sqli",
                    "title": "验证登录接口 SQLi",
                    "status": "pending",
                    "tool_hint": "sqli_probe",
                    "evidence_refs": ["ev-1"],
                }
            ],
        }
    ])

    assert result["todos"][0]["children"][0]["id"] == "validate.login-sqli"
    assert result["flat"][1]["parent_id"] == "phase.validation"
    assert result["todos"][0]["status"] == "pending"
    assert result["plan_approved"] is False
    assert result["plan_confirmation"]["question"]
    assert session._user_decision_requested is True

    _approve_plan(session)
    assert result["todos"][0]["children"][0]["evidence_refs"] == ["ev-1"]
    current = session.list_todos()["current"]
    assert current["id"] == "phase.validation"
    assert current["status"] == "in_progress"


def test_child_agent_auto_approves_todo_plan_even_on_confirmation_resume() -> None:
    session = AssistantSession()
    session._subagent_depth = 1
    # Confirmation resumes use this source; without the depth check they would
    # re-arm a plan card the child UI cannot answer.
    session._current_turn_source = "confirmation"

    result = session.set_todos([{"id": "step.one", "title": "扫端口"}])

    assert result.get("plan_confirmation") is None
    assert result["plan_approved"] is True
    assert session._todos_plan_approved is True
    assert session._user_decision_requested is False


def test_set_todos_requires_plan_confirmation_before_auto_continue() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "第一步", "status": "pending"}])
    session._todos_touched_this_turn = True

    assert session._should_auto_continue("") is False

    _approve_plan(session)
    session._user_decision_requested = False
    assert session._should_auto_continue("") is True


def test_cron_authored_todos_use_confirmed_schedule_authority() -> None:
    session = AssistantSession()
    session._current_turn_source = "cron"
    session._current_turn_source_ref = "cron-patrol"

    result = session.set_todos([
        {"id": "step.one", "title": "执行已授权巡检", "status": "pending"},
    ])

    assert result["plan_approved"] is True
    assert "plan_confirmation" not in result
    assert result["current"]["status"] == "in_progress"
    assert session._user_decision_requested is False
    assert session.events[-1]["kind"] == "todo_plan_auto_approved"
    assert session.events[-1]["payload"]["cron_job_id"] == "cron-patrol"


def test_cron_decision_cards_choose_recommended_and_audit() -> None:
    session = AssistantSession()
    session._current_turn_source = "cron"
    session._current_turn_source_ref = "cron-choice"

    single = session.stop_for_user_decision(
        "选择执行方式",
        ["保守检查", "完整检查"],
        recommended="完整检查",
    )
    assert single["auto_continued"] is True
    assert single["chosen"] == "完整检查"
    assert single["selection_reason"] == "explicit_recommended"

    batch = session.stop_for_user_decision_batch(
        "选择参数",
        [{
            "id": "scope",
            "question": "范围",
            "options": ["当前", "全部"],
            "recommended": "全部",
        }],
    )
    assert batch["answers"] == {"scope": "全部"}
    assert batch["selection_reasons"] == {"scope": "explicit_recommended"}
    assert session.events[-1]["kind"] == "unattended_decision"
    assert session.events[-1]["payload"]["source_ref"] == "cron-choice"


def test_subagent_authored_todos_do_not_wait_for_parent_user() -> None:
    session = AssistantSession()
    session._current_turn_source = "subagent"
    session._current_turn_source_ref = "agent-reviewer"

    result = session.set_todos([
        {"id": "step.one", "title": "执行子任务", "status": "pending"},
    ])

    assert result["plan_approved"] is True
    assert "plan_confirmation" not in result
    assert result["current"]["status"] == "in_progress"
    assert session._user_decision_requested is False
    assert session.events[-1]["source"] == "subagent"
    assert session.events[-1]["payload"]["source_ref"] == "agent-reviewer"


def test_classify_todo_plan_choice() -> None:
    session = AssistantSession()
    assert session._classify_todo_plan_choice("开始执行") == "approve"
    assert session._classify_todo_plan_choice("修改计划") == "modify"
    assert session._classify_todo_plan_choice("取消任务") == "cancel"
    assert session._classify_todo_plan_choice("删掉第二步，加个 SSRF 验证") == ""


def test_set_todos_after_approval_does_not_rearm_plan() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "克隆仓库"}])
    _approve_plan(session)
    session._user_decision_requested = False

    result = session.set_todos([
        {"id": "step.one", "title": "克隆仓库", "status": "in_progress"},
        {"id": "step.two", "title": "安装依赖", "status": "pending"},
    ])

    assert result.get("plan_confirmation") is None
    assert result["plan_approved"] is True
    assert session._todos_plan_approved is True
    assert session._user_decision_requested is False


def test_set_todos_after_clear_requires_confirmation() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "old", "title": "旧计划"}])
    _approve_plan(session)
    session.clear_todos()

    result = session.set_todos([{"id": "new", "title": "新计划"}])

    assert result["plan_approved"] is False
    assert result["plan_confirmation"]["question"]
    assert session._user_decision_requested is True


def test_modify_choice_revokes_approval_so_set_todos_rearms() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "第一步"}])
    rewritten = session._apply_todo_plan_choice("modify", "修改计划")

    assert "再次展示计划" in rewritten
    assert session._todos_plan_approved is False

    result = session.set_todos([
        {"id": "step.one", "title": "第一步"},
        {"id": "step.two", "title": "补一步"},
    ])
    assert result["plan_confirmation"]["question"]
    assert session._user_decision_requested is True


def test_approve_choice_rewrites_execute_now_prompt() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "第一步"}])
    rewritten = session._apply_todo_plan_choice("approve", "开始执行")

    assert session._todos_plan_approved is True
    assert "不要再次调用 set_todos" in rewritten
    assert rewritten.startswith("开始执行")


def test_update_todo_updates_child_by_id() -> None:
    session = AssistantSession()
    session.set_todos([
        {
            "id": "phase.validation",
            "title": "漏洞验证",
            "children": [{"id": "validate.login-sqli", "title": "验证 SQLi"}],
        }
    ])
    _approve_plan(session)

    result = session.update_todo(
        "validate.login-sqli",
        "completed",
        patch={"finding_refs": ["finding-1"], "blocked_reason": "ignored"},
    )

    child = result["todos"][0]["children"][0]
    assert child["status"] == "completed"
    assert child["finding_refs"] == ["finding-1"]
    assert "blocked_reason" in child


def test_update_todo_blocks_in_progress_before_plan_approval() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "第一步"}])

    result = session.update_todo("step.one", "in_progress")

    assert result["todos"][0]["status"] == "pending"
    assert session._todos_plan_approved is False


def test_add_todos_parent_id_appends_child() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "phase.recon", "title": "资产发现"}])
    _approve_plan(session)

    result = session.add_todos(
        [{"id": "recon.js", "title": "提取 JS 接口", "status": "pending"}],
        parent_id="phase.recon",
        trigger="发现前端 bundle",
    )

    assert result["added"] == ["recon.js"]
    child = result["todos"][0]["children"][0]
    assert child["id"] == "recon.js"
    assert child["trigger"] == "发现前端 bundle"
    assert result.get("plan_confirmation") is None


def test_add_todos_on_empty_list_requires_confirmation() -> None:
    session = AssistantSession()

    result = session.add_todos([{"id": "step.one", "title": "第一步"}])

    assert result["added"] == ["step.one"]
    assert result["plan_approved"] is False
    assert session._user_decision_requested is True


def test_unfinished_todos_include_children() -> None:
    session = AssistantSession()
    session.set_todos([
        {
            "id": "phase.validation",
            "title": "漏洞验证",
            "status": "completed",
            "children": [
                {"id": "validate.one", "title": "已完成", "status": "completed"},
                {"id": "validate.two", "title": "待验证", "status": "pending"},
            ],
        }
    ])
    _approve_plan(session)

    unfinished = session._unfinished_todos()

    assert [item["id"] for item in unfinished] == ["validate.two"]


def test_complete_todo_with_refs_merges_proof_refs() -> None:
    session = AssistantSession()
    session.set_todos([
        {
            "id": "phase.validation",
            "title": "漏洞验证",
            "children": [
                {
                    "id": "validate.login-sqli",
                    "title": "验证登录接口 SQLi",
                    "evidence_refs": ["ev-old"],
                }
            ],
        }
    ])
    _approve_plan(session)

    result = session.complete_todo_with_refs(
        "validate.login-sqli",
        evidence_refs=["ev-old", "ev-new"],
        artifact_refs=["art-1"],
        finding_refs=["finding-1"],
        evidence="复验请求和响应已归档",
    )

    child = result["todos"][0]["children"][0]
    assert child["status"] == "completed"
    assert child["evidence_refs"] == ["ev-old", "ev-new"]
    assert child["artifact_refs"] == ["art-1"]
    assert child["finding_refs"] == ["finding-1"]
    assert child["evidence"] == "复验请求和响应已归档"


def test_create_validation_todo_adds_standard_child() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "phase.validation", "title": "漏洞验证"}])
    _approve_plan(session)

    result = session.create_validation_todo(
        "phase.validation",
        "finding-sqli",
        asset_ref="url:/api/login",
        tool_hint="sqli_probe",
    )

    child = result["todos"][0]["children"][0]
    assert result["validation_todo_id"] == "validate.finding-sqli"
    assert child["kind"] == "validation"
    assert child["phase"] == "validation"
    assert child["target_ref"] == "url:/api/login"
    assert child["tool_hint"] == "sqli_probe"
    assert child["finding_refs"] == ["finding-sqli"]


def test_assessment_plan_snapshot_is_adapter_ready() -> None:
    session = AssistantSession()
    session.set_todos([
        {
            "id": "phase.validation",
            "title": "漏洞验证",
            "children": [{"id": "validate.one", "title": "验证一", "status": "pending"}],
        }
    ])
    _approve_plan(session)

    snapshot = session.assessment_plan_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["type"] == "assessment_plan_snapshot"
    assert snapshot["counts"]["total"] == 2
    assert snapshot["counts"]["unfinished"] == 2
    assert snapshot["current"]["id"] == "phase.validation"


def test_session_roundtrip_preserves_todos_plan_approved() -> None:
    session = AssistantSession()
    session.set_todos([{"id": "step.one", "title": "第一步"}])
    assert session._todos_plan_approved is False

    payload = session.to_dict()
    restored = AssistantSession.from_dict(payload, registry=session.registry)

    assert restored._todos_plan_approved is False
    assert restored.todos[0]["id"] == "step.one"


def test_session_restore_supersedes_all_but_latest_pending_decision() -> None:
    session = AssistantSession()
    session.history.extend([
        ChatMessage(
            role="assistant",
            content="first",
            blocks=[{"type": "user_decision", "question": "旧问题"}],
        ),
        ChatMessage(
            role="assistant",
            content="second",
            blocks=[{"type": "user_decision", "question": "新问题"}],
        ),
    ])

    restored = AssistantSession.from_dict(session.to_dict(), registry=session.registry)
    first = restored.history[0].blocks[0]
    second = restored.history[1].blocks[0]

    assert first["superseded"] is True
    assert first["submitted_response"] == "已由后续决策替代"
    assert not second.get("submitted_response")
