"""Replay synthetic contract trajectories against the production governor.

The fixtures are design cases, not evidence of real-model task performance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoterm.memory.models import EvidenceKind, EvidenceLevel, VerificationEvidence
from repoterm.runtime.progress_governor import ProgressGovernor
from repoterm.runtime.loop import run_agent_turn
from repoterm.runtime.turn_kernel import TurnVerificationState
from repoterm.tools.registry import ToolDefinition, ToolRegistry, ToolResult
from repoterm.contracts.types import AgentStep


TRACE_PATH = Path(__file__).parent / "fixtures" / "progress_governance_traces.json"
TRACE_DOCUMENT = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
CASES = TRACE_DOCUMENT["cases"]


def replay(case: dict[str, object]) -> str:
    governor = ProgressGovernor()
    verification = TurnVerificationState()
    last_intervention = "allow"

    for index, event in enumerate(case["events"], 1):
        action = event["action"]
        if action == "finish":
            if verification.supporting_evidence():
                return "allow"
            return (
                "require_verification"
                if case.get("remaining_steps", 1)
                else "stop_unverified"
            )

        evidence = None
        if "workspace_version" in event:
            evidence = VerificationEvidence.create(
                level=EvidenceLevel.CHANGE,
                kind=EvidenceKind.EDIT_FILE,
                tool_name="edit_file",
                ok=True,
                summary="changed",
                tool_input={"version": event["workspace_version"]},
            )
        elif "validation_version" in event:
            evidence = VerificationEvidence.create(
                level=EvidenceLevel.VALIDATION,
                kind=EvidenceKind.TEST,
                tool_name="test_runner",
                ok=event["observation"] == "pass",
                summary=event["observation"],
                tool_input={"version": event["validation_version"]},
            )
        if evidence is not None:
            verification.record_evidence(evidence)

        decision = governor.observe(
            event_id=str(index),
            tool_name=action.split(":", 1)[0],
            tool_input={"action": action},
            output=event["observation"],
            ok=True,
            evidence=evidence,
            poll_remaining=event.get("poll_remaining", 0),
        )
        if decision.action != "allow":
            last_intervention = decision.action
        if decision.reason in {"new_observation", "bounded_poll"}:
            last_intervention = "allow"
    return last_intervention


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_production_governor_replays_contract(case: dict[str, object]) -> None:
    assert replay(case) == case["expected"]


def test_trace_fixture_is_unique_and_tool_success_is_not_progress() -> None:
    assert TRACE_DOCUMENT["schema_version"] == 1
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))
    assert len(CASES) >= 10
    for case in CASES:
        assert case["events"]
        for event in case["events"]:
            assert isinstance(event["action"], str) and event["action"]
            assert isinstance(event["observation"], str) and event["observation"]
            assert "ok" not in event


def test_progress_governor_state_round_trips_without_raw_outputs() -> None:
    governor = ProgressGovernor(nudge_after=2, replan_after=4)
    governor.observe(
        event_id="one", tool_name="grep_files",
        tool_input={"path": "src", "pattern": "handler"},
        output="src/app.py:10: handler", ok=True,
    )
    governor.observe(
        event_id="two", tool_name="grep_files",
        tool_input={"path": "src", "pattern": "handler"},
        output="src/app.py:10: handler", ok=True,
    )
    state = governor.to_state()
    restored = ProgressGovernor.from_state(state)

    assert restored.to_state() == state
    serialized = json.dumps(state, sort_keys=True)
    assert "src/app.py" not in serialized
    assert "handler" not in serialized


def test_progress_governor_rejects_unknown_checkpoint_version() -> None:
    restored = ProgressGovernor.from_state({"version": 999, "stage": "stop_stuck"})
    assert restored.stage == "allow"
    assert restored.stagnant_actions == 0


def test_weak_new_observation_does_not_erase_repeated_action_chain() -> None:
    governor = ProgressGovernor()
    for index in range(3):
        governor.observe(
            event_id=str(index),
            tool_name="read_file",
            tool_input={"path": "a.py"},
            output="same",
            ok=True,
        )
    assert governor.stagnant_actions == 2
    decision = governor.observe(
        event_id="new",
        tool_name="read_file",
        tool_input={"path": "a.py"},
        output="different source evidence",
        ok=True,
    )
    assert decision.action == "allow"
    assert governor.stagnant_actions == 2


def test_recovery_outcome_distinguishes_hint_from_actual_progress() -> None:
    governor = ProgressGovernor(nudge_after=2, replan_after=4)
    governor.observe(event_id="one", tool_name="lookup", tool_input={}, output="same", ok=True)
    governor.observe(event_id="two", tool_name="lookup", tool_input={}, output="same", ok=True)
    hint = governor.observe(event_id="hint", tool_name="lookup", tool_input={}, output="same", ok=True)
    assert hint.action == "nudge"
    assert hint.intervention_event_id == "hint"

    weak = governor.observe(
        event_id="weak", tool_name="lookup", tool_input={}, output="new location", ok=True,
    )
    assert weak.recovery_outcome == "weak_evidence"
    assert governor.stage == "nudge"

    not_recovered = governor.observe(
        event_id="repeat", tool_name="lookup", tool_input={}, output="same", ok=True,
    )
    assert not_recovered.recovery_outcome == "not_recovered"
    assert not_recovered.intervention_event_id == "hint"

    change = VerificationEvidence.create(
        level=EvidenceLevel.CHANGE, kind=EvidenceKind.EDIT_FILE,
        tool_name="edit_file", ok=True, summary="changed", tool_input={"path": "a.py"},
    )
    recovered = governor.observe(
        event_id="edit", tool_name="edit_file", tool_input={"path": "a.py"},
        output="changed", ok=True, evidence=change,
    )
    assert recovered.recovery_outcome == "recovered"
    assert recovered.intervention_event_id == "hint"
    assert governor.stage == "allow"


def test_tool_error_does_not_consume_recovery_followup() -> None:
    governor = ProgressGovernor(nudge_after=1)
    hint = governor.observe_progress_message(event_id="hint", content="I will inspect.")
    assert hint.action == "nudge"
    error = governor.observe(
        event_id="error", tool_name="lookup", tool_input={}, output="invalid arguments", ok=False,
    )
    assert error.recovery_outcome == ""
    followup = governor.observe_progress_message(event_id="followup", content="I will try again.")
    assert followup.recovery_outcome == ""


def test_unique_search_hits_require_replan_without_premature_stop() -> None:
    governor = ProgressGovernor()
    decisions = [
        governor.observe(
            event_id=str(index),
            tool_name="code_search",
            tool_input={"path": "src", "query": f"candidate-{index}"},
            output=f"src/module_{index}.py:10: candidate",
            ok=True,
        )
        for index in range(16)
    ]
    assert any(item.action == "nudge" for item in decisions)
    assert any(item.action == "require_replan" for item in decisions)
    assert not any(item.action == "stop_stuck" for item in decisions)
    assert governor.stage == "require_replan"


def test_long_exploration_with_interspersed_repeats_does_not_hard_stop() -> None:
    """Exploration can be slow without being a consecutive action loop."""

    governor = ProgressGovernor()
    decisions = []
    for index in range(26):
        location = index if index % 4 else max(0, index - 1)
        decisions.append(governor.observe(
            event_id=str(index), tool_name="read_file",
            tool_input={"path": f"src/module_{location}.py"},
            output=f"src/module_{location}.py:10: candidate",
            ok=True,
        ))
    assert any(item.action == "require_replan" for item in decisions)
    assert all(item.action != "stop_stuck" for item in decisions)


def test_exact_loop_still_stops_after_recovery_window() -> None:
    governor = ProgressGovernor()
    decisions = [governor.observe(
        event_id=str(index), tool_name="read_file",
        tool_input={"path": "src/module.py"},
        output="src/module.py:10: same candidate", ok=True,
    ) for index in range(9)]
    assert any(item.action == "stop_stuck" for item in decisions)


def test_recovery_defers_same_question_despite_rotating_search_hits() -> None:
    governor = ProgressGovernor()
    for index in range(10):
        governor.observe(
            event_id=str(index), tool_name="code_search",
            tool_input={"path": "src", "query": f"handler exception flow candidate variant{index}"},
            output=f"src/module_{index}.py:10: candidate",
            ok=True,
        )
    assert governor.stage == "require_replan"
    held = governor.before_action(
        event_id="next", tool_name="code_search",
        tool_input={"path": "src", "query": "handler exception flow candidate next"},
        read_only=True,
    )
    assert held.action == "defer"
    assert governor.before_action(
        event_id="other-method", tool_name="read_file",
        tool_input={"path": "src/module_0.py"}, read_only=True,
    ).action == "allow"


def test_task_gap_tracks_unverified_change_without_trusting_model_prose() -> None:
    governor = ProgressGovernor(nudge_after=1)
    governor.note_task_state(changed=True, verified=False)
    decision = governor.observe_progress_message(
        event_id="progress", content="The fix is complete."
    )
    assert decision.task_gap == "verify_change"
    assert "independent, focused check" in decision.guidance
    governor.note_task_state(changed=True, verified=True)
    assert governor.task_gap == "review_result"


def test_identical_validation_cannot_refill_recovery_budget() -> None:
    governor = ProgressGovernor()
    evidence = VerificationEvidence.create(
        level=EvidenceLevel.VALIDATION,
        kind=EvidenceKind.TEST,
        tool_name="test_runner",
        ok=True,
        summary="passed",
        tool_input={"target": "tests/test_a.py"},
    )
    first = governor.observe(
        event_id="first", tool_name="test_runner",
        tool_input={"target": "tests/test_a.py"},
        output="1 passed", ok=True, evidence=evidence,
    )
    assert first.reason == "new_validation"
    second = governor.observe(
        event_id="second", tool_name="test_runner",
        tool_input={"target": "tests/test_a.py"},
        output="1 passed", ok=True, evidence=evidence,
    )
    assert second.reason != "new_validation"
    assert governor.stagnant_actions == 1


def test_tool_error_does_not_count_as_action_progress_or_stall() -> None:
    governor = ProgressGovernor()
    decision = governor.observe(
        event_id="error",
        tool_name="any_tool",
        tool_input={},
        output="invalid arguments",
        ok=False,
    )
    assert decision.reason == "tool_error_out_of_scope"
    assert governor.stagnant_actions == 0


def test_repeated_progress_messages_cannot_continue_forever() -> None:
    governor = ProgressGovernor()
    decisions = [
        governor.observe_progress_message(
            event_id=str(index), content="I will inspect the repository next."
        )
        for index in range(8)
    ]
    assert any(item.action == "nudge" for item in decisions)
    assert any(item.action == "require_replan" for item in decisions)
    assert decisions[-1].action == "stop_stuck"


def test_loop_stops_repeated_model_progress_without_tools(tmp_path: Path) -> None:
    class RepeatingProgressModel:
        calls = 0

        def next(self, messages, on_stream_chunk=None, store=None):
            self.calls += 1
            return AgentStep(
                type="assistant",
                content="I will inspect the repository next.",
                kind="progress",
            )

    model = RepeatingProgressModel()
    events = []
    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "Inspect the code"}],
        cwd=str(tmp_path),
        max_steps=20,
        enable_work_chain=False,
        on_runtime_event=events.append,
    )
    assert model.calls == 8
    assert messages[-1]["role"] == "assistant"
    assert "任务未被证明完成" in messages[-1]["content"]
    assert any(
        "progress-governance:recovery-outcome outcome=not_recovered" in event.message
        for event in events
    )


def test_black_trajectory_requires_replan_then_defers_equivalent_action() -> None:
    """A compact replay of the observed Black loop, without source contents."""

    governor = ProgressGovernor()
    decisions = []
    for index in range(8):
        decisions.append(governor.observe(
            event_id=f"black-{index}",
            tool_name="grep_files",
            tool_input={"path": "black.py", "pattern": f"ProcessPoolExecutor Manager reformat_many ThreadPoolExecutor variant{index}"},
            output="black.py:612: same candidate\nblack.py:670: same candidate",
            ok=True,
        ).action)
    assert "nudge" in decisions
    assert "require_replan" in decisions
    assert governor.before_action(
        event_id="next-search", tool_name="grep_files",
        tool_input={"path": "black.py", "pattern": "ProcessPoolExecutor Manager reformat_many ThreadPoolExecutor another"},
        read_only=True,
    ).action == "defer"
    # A different information source remains available; no tool class is banned.
    assert governor.before_action(
        event_id="targeted-read", tool_name="read_file",
        tool_input={"path": "black.py", "start_line": 612}, read_only=True,
    ).action == "allow"


def test_scope_gate_is_generic_and_does_not_block_new_scope_or_writes() -> None:
    governor = ProgressGovernor()
    for index in range(7):
        governor.observe(
            event_id=str(index), tool_name="catalog_lookup",
            tool_input={"path": "src/a.py", "query": f"candidate symbol location module q{index}"},
            output="src/a.py:7: same result", ok=True,
        )
    assert governor.stage == "require_replan"
    assert governor.before_action(
        event_id="same", tool_name="catalog_lookup",
        tool_input={"path": "src/a.py", "query": "candidate symbol location module next"}, read_only=True,
    ).action == "defer"
    assert governor.before_action(
        event_id="new-scope", tool_name="catalog_lookup",
        tool_input={"path": "src/b.py", "query": "candidate symbol location module next"}, read_only=True,
    ).action == "allow"
    assert governor.before_action(
        event_id="new-question", tool_name="catalog_lookup",
        tool_input={"path": "src/a.py", "query": "unrelated exception handler"}, read_only=True,
    ).action == "allow"
    assert governor.before_action(
        event_id="write", tool_name="edit_file",
        tool_input={"path": "src/a.py"}, read_only=False,
    ).action == "allow"
    assert governor.before_action(
        event_id="poll", tool_name="catalog_lookup",
        tool_input={"path": "src/a.py"}, read_only=True, poll_remaining=2,
    ).action == "allow"
    assert governor.before_action(
        event_id="same-again", tool_name="catalog_lookup",
        tool_input={"path": "src/a.py", "query": "candidate symbol location module repeat"},
        read_only=True,
    ).action == "stop_stuck"


def test_loop_defers_once_without_counting_a_tool_failure(tmp_path: Path) -> None:
    class RepeatingLookupModel:
        calls = 0

        def next(self, messages, on_stream_chunk=None, store=None):
            self.calls += 1
            if self.calls <= 8:
                return AgentStep(type="tool_calls", calls=[{
                    "id": f"lookup-{self.calls}", "toolName": "catalog_lookup",
                    "input": {"path": "src/a.py", "query": f"candidate symbol location module q{self.calls}"},
                }])
            return AgentStep(type="assistant", content="No further lookup is needed.")

    executed: list[int] = []
    tool = ToolDefinition(
        name="catalog_lookup", description="Read a catalog",
        input_schema={"type": "object"}, validator=lambda value: value,
        run=lambda value, context: (
            executed.append(len(executed)) or ToolResult(ok=True, output="src/a.py:7: same result")
        ),
    )
    # The generic tool must advertise a read-only capability to be eligible.
    from repoterm.tools.registry import ToolCapability, ToolMetadata
    tool.metadata = ToolMetadata(
        name=tool.name, description=tool.description,
        capabilities={ToolCapability.READ_ONLY},
    )
    events = []
    messages = run_agent_turn(
        model=RepeatingLookupModel(), tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "Inspect src/a.py"}],
        cwd=str(tmp_path), max_steps=9, enable_work_chain=False,
        on_runtime_event=events.append,
    )
    assert len(executed) == 7
    held = [item for item in messages if item["role"] == "tool_result" and "not executed" in item["content"]]
    assert len(held) == 1
    assert any("progress-governance:defer" in event.message for event in events)
    assert messages[-1]["role"] == "assistant"


def test_deferred_batch_preserves_results_for_other_calls(tmp_path: Path) -> None:
    from repoterm.tools.registry import ToolCapability, ToolMetadata

    class BatchModel:
        calls = 0

        def next(self, messages, on_stream_chunk=None, store=None):
            self.calls += 1
            if self.calls <= 7:
                calls = [{
                    "id": f"lookup-{self.calls}", "toolName": "catalog_lookup",
                    "input": {"path": "src/a.py", "query": f"candidate symbol location module q{self.calls}"},
                }]
            elif self.calls == 8:
                calls = [
                    {"id": "held", "toolName": "catalog_lookup",
                     "input": {"path": "src/a.py", "query": "candidate symbol location module again"}},
                    {"id": "other", "toolName": "catalog_lookup",
                     "input": {"path": "src/b.py", "query": "new-scope"}},
                ]
            else:
                return AgentStep(type="assistant", content="Evidence collected.")
            return AgentStep(type="tool_calls", calls=calls)

    actual_paths: list[str] = []

    def run_lookup(value, context):
        actual_paths.append(value["path"])
        return ToolResult(ok=True, output="src/a.py:7: same result")

    tool = ToolDefinition(
        name="catalog_lookup", description="Read a catalog",
        input_schema={"type": "object"}, validator=lambda value: value,
        run=run_lookup,
        metadata=ToolMetadata(
            name="catalog_lookup", description="Read a catalog",
            capabilities={ToolCapability.READ_ONLY},
        ),
    )
    messages = run_agent_turn(
        model=BatchModel(), tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "Inspect source"}],
        cwd=str(tmp_path), max_steps=10, enable_work_chain=False,
    )
    assert actual_paths.count("src/a.py") == 7
    assert actual_paths.count("src/b.py") == 1
    results = {item["toolUseId"]: item for item in messages if item["role"] == "tool_result"}
    assert results["held"]["isError"] is True
    assert "not executed" in results["held"]["content"]
    assert results["other"]["isError"] is False


def test_change_and_verification_are_not_held_after_stall() -> None:
    governor = ProgressGovernor()
    for index in range(7):
        governor.observe(
            event_id=str(index), tool_name="code_search",
            tool_input={"path": "src/a.py", "query": f"candidate symbol location module q{index}"},
            output="src/a.py:7: known location", ok=True,
        )
    assert governor.before_action(
        event_id="edit", tool_name="edit_file",
        tool_input={"path": "src/a.py"}, read_only=False,
    ).action == "allow"
    assert governor.before_action(
        event_id="test", tool_name="test_runner",
        tool_input={"path": "tests/test_a.py"}, read_only=False,
    ).action == "allow"
    governor.observe(
        event_id="edit-done", tool_name="edit_file", tool_input={"path": "src/a.py"},
        output="edited", ok=True,
        evidence=VerificationEvidence.create(
            level=EvidenceLevel.CHANGE, kind=EvidenceKind.EDIT_FILE,
            tool_name="edit_file", ok=True, summary="edited",
            tool_input={"path": "src/a.py"},
        ),
    )
    assert governor.stage == "allow"
    assert governor.before_action(
        event_id="search-again", tool_name="code_search",
        tool_input={"path": "src/a.py"}, read_only=True,
    ).action == "allow"


def test_exhausted_lookup_in_batch_does_not_cancel_a_write(tmp_path: Path) -> None:
    from repoterm.tools.registry import ToolCapability, ToolMetadata

    class BatchRecoveryModel:
        calls = 0

        def next(self, messages, on_stream_chunk=None, store=None):
            self.calls += 1
            if self.calls <= 8:
                calls = [{
                    "id": f"lookup-{self.calls}", "toolName": "catalog_lookup",
                    "input": {"path": "src/a.py", "query": f"candidate symbol location module q{self.calls}"},
                }]
            elif self.calls == 9:
                calls = [
                    {"id": "repeat", "toolName": "catalog_lookup",
                     "input": {"path": "src/a.py", "query": "candidate symbol location module still"}},
                    {"id": "repair", "toolName": "repair_code",
                     "input": {"path": "src/a.py"}},
                ]
            else:
                return AgentStep(type="assistant", content="The repair was attempted.")
            return AgentStep(type="tool_calls", calls=calls)

    executed: list[str] = []
    lookup = ToolDefinition(
        name="catalog_lookup", description="Read a catalog",
        input_schema={"type": "object"}, validator=lambda value: value,
        run=lambda value, context: (
            executed.append("lookup") or ToolResult(ok=True, output="src/a.py:7: same")
        ),
        metadata=ToolMetadata(
            name="catalog_lookup", description="Read a catalog",
            capabilities={ToolCapability.READ_ONLY},
        ),
    )
    repair = ToolDefinition(
        name="repair_code", description="Change code",
        input_schema={"type": "object"}, validator=lambda value: value,
        run=lambda value, context: (
            executed.append("repair") or ToolResult(ok=True, output="Changed src/a.py")
        ),
    )
    messages = run_agent_turn(
        model=BatchRecoveryModel(), tools=ToolRegistry([lookup, repair]),
        messages=[{"role": "user", "content": "Repair the code"}],
        cwd=str(tmp_path), max_steps=10, enable_work_chain=False,
    )
    assert executed.count("lookup") == 7
    assert executed.count("repair") == 1
    results = {item["toolUseId"]: item for item in messages if item["role"] == "tool_result"}
    assert "not executed" in results["repeat"]["content"]
    assert results["repair"]["isError"] is False


def test_new_evidence_reopens_a_previously_deferred_scope() -> None:
    governor = ProgressGovernor()
    for index in range(7):
        governor.observe(
            event_id=str(index), tool_name="catalog_lookup",
            tool_input={"path": "src/a.py", "query": f"candidate symbol location module q{index}"},
            output="src/a.py:7: known", ok=True,
        )
    same = {"path": "src/a.py", "query": "candidate symbol location module next"}
    assert governor.before_action(
        event_id="held", tool_name="catalog_lookup",
        tool_input=same, read_only=True,
    ).action == "defer"
    governor.observe(
        event_id="new-location", tool_name="catalog_lookup",
        tool_input={"path": "src/a.py", "query": "distinct exception path"},
        output="src/a.py:50: newly located exception handler", ok=True,
    )
    assert governor.before_action(
        event_id="retry-after-evidence", tool_name="catalog_lookup",
        tool_input=same, read_only=True,
    ).action == "allow"
