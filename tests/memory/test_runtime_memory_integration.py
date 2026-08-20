from __future__ import annotations

from pathlib import Path

from repoterm.agent_reflection import ReflectionEngine
from repoterm.memory import MemoryService, Status
from repoterm.turn_kernel import TurnRecurrentState, classify_tool_result


def test_injector_is_idempotent_for_one_turn(tmp_path: Path) -> None:
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
    )
    service.remember_explicit("Use pytest for verification.")
    messages = [
        {"role": "system", "content": "System contract."},
        {"role": "user", "content": "Run the test command."},
    ]

    once = service.injector.inject_once(messages, "pytest verification")
    twice = service.injector.inject_once(once, "pytest verification")

    assert twice[0]["content"].count(service.injector.HEADER) == 1
    assert twice == once


def test_reflection_requires_successful_tool_evidence_and_stays_pending(
    tmp_path: Path,
) -> None:
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
    )
    engine = ReflectionEngine(memory_manager=service)

    assistant_only = engine.reflect(
        "assistant-only task",
        [{"type": "assistant", "content": "It is complete."}],
    )
    assert assistant_only.success is False
    assert service.list_pending() == []

    verified = engine.reflect(
        "verified project change",
        [
            {"type": "assistant", "content": "Verified."},
        ],
        verification_state=_verification_state().verification_state,
        stop_reason="done",
    )
    assert verified.success is True
    pending = service.list_pending()
    assert len(pending) == 1
    assert pending[0].status == Status.PENDING
    assert pending[0].evidence[0].kind.value == "test"


def test_failed_validation_needs_a_real_repair_before_reflection_proposes(tmp_path: Path) -> None:
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
    )
    engine = ReflectionEngine(memory_manager=service)
    state = TurnRecurrentState(max_steps=5)
    state.record_verification_evidence(
        classify_tool_result(
            tool_name="pytest",
            tool_input={"cmd": "python -m pytest -q"},
            ok=False,
            result_output="1 failed",
            source_session_id="session-recovery",
            source_turn_id="turn-recovery",
        )
    )
    state.record_verification_evidence(
        classify_tool_result(
            tool_name="edit_file",
            tool_input={"path": "repoterm/example.py"},
            ok=True,
            result_output="updated",
            source_session_id="session-recovery",
            source_turn_id="turn-recovery",
        )
    )
    state.record_verification_evidence(
        classify_tool_result(
            tool_name="pytest",
            tool_input={"cmd": "python -m pytest -q"},
            ok=True,
            result_output="1 passed",
            source_session_id="session-recovery",
            source_turn_id="turn-recovery",
        )
    )

    result = engine.reflect(
        "repair a failing project test",
        [{"type": "assistant", "content": "Repaired and verified."}],
        verification_state=state.verification_state,
        stop_reason="done",
    )

    assert result.success is True
    assert len(service.list_pending()) == 1
    assert "after a failed validation" in service.list_pending()[0].content


def _verification_state() -> TurnRecurrentState:
    state = TurnRecurrentState(max_steps=3)
    state.record_verification_evidence(
        classify_tool_result(
            tool_name="pytest",
            tool_input={"cmd": "python -m pytest -q"},
            ok=True,
            result_output="1 passed",
            source_session_id="session-verified",
            source_turn_id="turn-verified",
        )
    )
    return state
