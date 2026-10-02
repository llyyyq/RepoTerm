from repoterm.runtime.planning.task_object import TaskState
from repoterm.memory import EvidenceKind, EvidenceLevel
from repoterm.runtime.turn_kernel import (
    TurnBudgetSignals,
    TurnRecurrentState,
    TurnVerificationState,
    build_stable_task_pack,
    classify_tool_result,
    decide_assistant_turn,
    decide_tool_turn,
    derive_turn_step_policy,
)


class DummyTask:
    title = "Repair reader"
    goal = "Keep durable state stable"
    description = "Refactor the turn kernel"


class DummySlotState:
    value = "running"


class DummySlot:
    state = DummySlotState()


class DummyTaskGraph:
    slots = {"turn:task-1": DummySlot()}

    def get_progress_percentage(self) -> float:
        return 35.0


def test_turn_recurrent_state_maps_await_user_to_paused() -> None:
    turn_state = TurnRecurrentState(max_steps=5)
    turn_state.set_stop_reason("await_user")

    assert turn_state.final_task_state() is TaskState.PAUSED


def test_successful_test_steers_toward_final_without_auto_completing() -> None:
    state = TurnRecurrentState(max_steps=30)
    state.begin_step()
    passed = classify_tool_result(
        tool_name="test_runner",
        tool_input={},
        ok=True,
        result_output="2 passed in 0.12s",
        source_session_id=None,
        source_turn_id=None,
    )
    assert passed is not None
    assert "final answer" in state.post_tool_guidance(passed)
    assert state.stop_reason is None

    state.begin_step()
    assert state.post_tool_guidance(None) == ""
    state.begin_step()
    assert "already passed" in state.post_tool_guidance(None)

    changed = classify_tool_result(
        tool_name="edit_file",
        tool_input={"path": "sample.py"},
        ok=True,
        result_output="updated",
        source_session_id=None,
        source_turn_id=None,
    )
    assert changed is not None
    assert state.post_tool_guidance(changed) == ""
    assert state.successful_test_step is None


def test_validation_before_latest_edit_is_not_completion_evidence() -> None:
    state = TurnRecurrentState(max_steps=5)
    passed = classify_tool_result(
        tool_name="test_runner", tool_input={}, ok=True,
        result_output="1 passed", source_session_id=None, source_turn_id=None,
    )
    changed = classify_tool_result(
        tool_name="edit_file", tool_input={"path": "sample.py"}, ok=True,
        result_output="updated", source_session_id=None, source_turn_id=None,
    )
    assert passed is not None and changed is not None
    state.record_verification_evidence(passed)
    assert state.has_verification_evidence()
    state.record_verification_evidence(changed)
    assert not state.has_verification_evidence()
    assert state.verification_state.evidence_summary == ""
    state.verification_state.requires_evidence = True
    decision = decide_assistant_turn(
        turn_state=state,
        step_content="The tests passed, so the fix is complete.",
        step_kind=None, stop_reason=None, block_types=None,
        ignored_block_types=None, is_empty=False, treat_as_progress=False,
        is_recoverable_thinking_stop=False, format_diagnostics=lambda *_: "",
        nudge_continue="continue", nudge_after_tool_result="after tool",
        resume_after_pause="resume pause", resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=type("Policy", (), {"phase": "verify"})(),
    )
    assert decision.kind == "progress"
    assert "no successful validation remains" in (decision.assistant_content or "")
    state.record_verification_evidence(passed)
    assert state.has_verification_evidence()


def test_default_profile_blocks_final_after_code_change_without_validation() -> None:
    state = TurnRecurrentState(max_steps=50)
    changed = classify_tool_result(
        tool_name="edit_file", tool_input={"path": "sample.py"}, ok=True,
        result_output="updated", source_session_id=None, source_turn_id=None,
    )
    assert changed is not None
    state.record_verification_evidence(changed, requires_validation=True)

    decision = decide_assistant_turn(
        turn_state=state,
        step_content="The implementation is fixed.",
        step_kind=None, stop_reason=None, block_types=None,
        ignored_block_types=None, is_empty=False, treat_as_progress=False,
        is_recoverable_thinking_stop=False, format_diagnostics=lambda *_: "",
        nudge_continue="continue", nudge_after_tool_result="after tool",
        resume_after_pause="resume pause", resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=type("Policy", (), {"phase": "execute"})(),
    )

    assert decision.kind == "progress"
    assert "no successful validation remains" in (decision.assistant_content or "")


def test_document_change_can_finish_without_code_validation_gate() -> None:
    state = TurnRecurrentState(max_steps=50)
    changed = classify_tool_result(
        tool_name="edit_file", tool_input={"path": "README.md"}, ok=True,
        result_output="updated", source_session_id=None, source_turn_id=None,
    )
    assert changed is not None
    state.record_verification_evidence(changed, requires_validation=False)

    decision = decide_assistant_turn(
        turn_state=state,
        step_content="Updated the documentation.",
        step_kind=None, stop_reason=None, block_types=None,
        ignored_block_types=None, is_empty=False, treat_as_progress=False,
        is_recoverable_thinking_stop=False, format_diagnostics=lambda *_: "",
        nudge_continue="continue", nudge_after_tool_result="after tool",
        resume_after_pause="resume pause", resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=type("Policy", (), {"phase": "execute"})(),
    )

    assert decision.kind == "final"


def test_verification_state_round_trip_preserves_unverified_code_change() -> None:
    state = TurnVerificationState(strict=False)
    changed = classify_tool_result(
        tool_name="edit_file", tool_input={"path": "sample.py"}, ok=True,
        result_output="updated", source_session_id="session", source_turn_id="turn",
    )
    assert changed is not None
    state.record_evidence(changed, requires_validation=True)

    restored = TurnVerificationState.from_state(state.to_state(), strict=False)

    assert restored.latest_change_index == 0
    assert restored.latest_change_requires_validation is True
    assert restored.supporting_evidence() == ()


def test_failed_test_feedback_reports_delta_without_inventing_success() -> None:
    state = TurnRecurrentState(max_steps=10)
    failed = classify_tool_result(
        tool_name="test_runner", tool_input={}, ok=False,
        result_output="FAILED tests/test_a.py::test_first - AssertionError\n1 failed, 1 passed",
        source_session_id=None, source_turn_id=None,
    )
    assert failed is not None
    guidance = state.post_tool_guidance(failed, result_output="FAILED tests/test_a.py::test_first - AssertionError")
    assert "New: tests/test_a.py::test_first" in guidance
    guidance = state.post_tool_guidance(failed, result_output="FAILED tests/test_a.py::test_first - AssertionError")
    assert "No newly failing test IDs" in guidance

    unittest_failure = "FAIL: test_works_in_mono_process_only_environment (tests.test_black.BlackTestCase)\nAssertionError: 1 != 0"
    guidance = state.post_tool_guidance(failed, result_output=unittest_failure)
    assert "test_works_in_mono_process_only_environment" in guidance
    assert "failure-mechanism hypothesis" in guidance


def test_build_stable_task_pack_includes_graph_and_protected_context() -> None:
    pack = build_stable_task_pack(
        task=DummyTask(),
        task_metadata={"intent_type": "code", "action_type": "update"},
        protected_context=["user asked for durable state retention"],
        task_graph=DummyTaskGraph(),
        task_slot_key="turn:task-1",
        latest_tool_result_summary="read_file: loaded turn kernel",
        progress_state={"summary": "patched the assistant decision path"},
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
            last_verification_note="need explicit final answer",
        ),
        budget_signals=TurnBudgetSignals(
            remaining_steps=7,
            tool_error_count=1,
            saw_tool_result=True,
        ),
    )

    assert pack is not None
    text = pack.to_protected_text()
    assert "Task graph: progress=35%" in text
    assert "slot=running" in text
    assert "Latest tool result: read_file: loaded turn kernel" in text
    assert "Protected context:" in text


def test_derive_turn_step_policy_becomes_verification_heavy_late_in_single_deep() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        widen_after_step=6,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 6
    turn_state.saw_tool_result = True
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.phase == "verify"
    assert turn_state.verification_state.requires_explicit_final is True
    assert "phase=verify" in turn_state.verification_state.last_verification_note


def test_derive_turn_step_policy_allows_widening_after_stall_threshold() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state.tool_error_count = 2
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is True
    assert "widening=ready" in turn_state.verification_state.last_verification_note


def test_derive_turn_step_policy_requires_explicit_signal_before_widening() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is False
    assert policy.widening_reason == ""


def test_derive_turn_step_policy_records_model_stall_as_widening_reason() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        empty_response_retry_limit=3,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state.empty_response_retry_count = 3
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is True
    assert "stalled repeatedly" in policy.widening_reason
    assert "assistant returned repeated empty responses" in policy.widening_evidence_summary


def test_turn_recurrent_state_widening_transition_extends_budget_once() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        widen_after_step=4,
    )
    turn_state.step = 6
    turn_state._refresh_budget_signals()

    first = turn_state.activate_widening(extra_steps=5)
    second = turn_state.activate_widening(extra_steps=5)

    assert first is True
    assert second is False
    assert turn_state.widening_active is True
    assert turn_state.widening_transition_count == 1
    assert turn_state.max_steps == 13


def test_decide_assistant_turn_returns_verification_failed_in_late_verify_mode() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
        ),
    )
    turn_state.step = 3
    turn_state._refresh_budget_signals()
    turn_state.saw_tool_result = True
    turn_state.empty_response_retry_count = turn_state.empty_response_retry_limit

    decision = decide_assistant_turn(
        turn_state=turn_state,
        step_content="",
        step_kind=None,
        stop_reason=None,
        block_types=None,
        ignored_block_types=None,
        is_empty=True,
        treat_as_progress=False,
        is_recoverable_thinking_stop=False,
        format_diagnostics=lambda *_: "",
        nudge_continue="continue",
        nudge_after_tool_result="after tool",
        resume_after_pause="resume pause",
        resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=derive_turn_step_policy(turn_state),
    )

    assert decision.kind == "fallback"
    assert decision.stop_reason == "verification_failed"
    assert "verification failure" in (decision.assistant_content or "").lower()


def test_decide_assistant_turn_rejects_unsupported_final_in_verify_mode() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
        ),
    )
    turn_state.step = 4
    turn_state.record_tool_result(True, summary="pytest: 5 passed")
    turn_state.record_verification_evidence(
        classify_tool_result(
            tool_name="pytest",
            tool_input={"cmd": "python -m pytest -q"},
            ok=True,
            result_output="5 passed",
            source_session_id="session-1",
            source_turn_id="turn-1",
        )
    )

    decision = decide_assistant_turn(
        turn_state=turn_state,
        step_content="Done, the fix is complete.",
        step_kind=None,
        stop_reason=None,
        block_types=None,
        ignored_block_types=None,
        is_empty=False,
        treat_as_progress=False,
        is_recoverable_thinking_stop=False,
        format_diagnostics=lambda *_: "",
        nudge_continue="continue",
        nudge_after_tool_result="after tool",
        resume_after_pause="resume pause",
        resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=derive_turn_step_policy(turn_state),
    )

    assert decision.kind == "progress"
    assert "verification guard" in (decision.assistant_content or "").lower()
    assert "5 passed" in (decision.user_content or "")


def test_only_semantic_validation_commands_open_the_evidence_gate() -> None:
    turn_state = TurnRecurrentState(max_steps=5)
    read = classify_tool_result(
        tool_name="read_file",
        tool_input={"path": "README.md"},
        ok=True,
        result_output="contents omitted",
        source_session_id="session-1",
        source_turn_id="turn-1",
    )
    shell = classify_tool_result(
        tool_name="run_command",
        tool_input={"cmd": "python --version"},
        ok=True,
        result_output="Python 3.12",
        source_session_id="session-1",
        source_turn_id="turn-1",
    )
    pytest_result = classify_tool_result(
        tool_name="run_command",
        tool_input={"cmd": "python -m pytest -q"},
        ok=True,
        result_output="2 passed",
        source_session_id="session-1",
        source_turn_id="turn-1",
    )

    turn_state.record_tool_result(True, summary="read_file: observed")
    assert read is None
    turn_state.record_tool_result(True, summary="run_command: observed")
    assert shell is None
    assert turn_state.has_verification_evidence() is False

    turn_state.record_tool_result(True, summary="pytest: 2 passed")
    turn_state.record_verification_evidence(pytest_result)
    assert pytest_result.level is EvidenceLevel.VALIDATION
    assert pytest_result.kind is EvidenceKind.TEST
    assert turn_state.has_verification_evidence() is True


def test_observation_text_does_not_open_the_evidence_gate() -> None:
    cases = (
        ("read_file", {"path": "pytest.ini"}),
        ("grep_files", {"query": "pytest"}),
        ("run_command", {"cmd": "echo pytest"}),
    )

    for tool_name, tool_input in cases:
        evidence = classify_tool_result(
            tool_name=tool_name,
            tool_input=tool_input,
            ok=True,
            result_output="pytest text was observed",
            source_session_id="session-1",
            source_turn_id="turn-1",
        )
        assert evidence is None


def test_decide_tool_turn_keeps_await_user_typed() -> None:
    decision = decide_tool_turn(
        tool_name="ask_user",
        result_output="Need approval",
        await_user=True,
    )

    assert decision.kind == "await_user"
    assert decision.stop_reason == "await_user"
