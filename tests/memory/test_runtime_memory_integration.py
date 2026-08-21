from __future__ import annotations

from pathlib import Path

from repoterm.agent_loop import _record_implicit_preference_signal, run_agent_turn
from repoterm.agent_reflection import ReflectionEngine
from repoterm.memory import MemoryService, Status
from repoterm.tooling import ToolRegistry
from repoterm.turn_kernel import TurnRecurrentState, classify_tool_result
from repoterm.types import AgentStep, ModelAdapter


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


def test_agent_loop_preference_signal_enters_global_pending_after_two_turns(
    tmp_path: Path,
) -> None:
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
    )

    first = _record_implicit_preference_signal(
        service,
        "I prefer concise responses",
        source_session_id="session-a",
        source_turn_id="turn-a",
    )
    second = _record_implicit_preference_signal(
        service,
        "I prefer concise responses",
        source_session_id="session-b",
        source_turn_id="turn-b",
    )

    assert first is None
    assert second is not None
    assert second.scope.value == "global"
    assert second.kind.value == "preference"
    assert second.status is Status.PENDING
    assert len(service.list_pending()) == 1


def test_run_agent_turn_observes_implicit_preference_without_work_chain(
    tmp_path: Path,
) -> None:
    class OneShotModel(ModelAdapter):
        def next(self, messages, on_stream_chunk=None):
            return AgentStep(type="assistant", content="acknowledged")

    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
    )

    for _ in range(2):
        run_agent_turn(
            model=OneShotModel(),
            tools=ToolRegistry([]),
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "I prefer concise responses"},
            ],
            cwd=str(tmp_path),
            memory_manager=service,
            runtime={"model": "mock"},
            enable_work_chain=False,
        )

    pending = service.list_pending()
    assert len(pending) == 1
    assert pending[0].scope.value == "global"
    assert pending[0].kind.value == "preference"
    assert pending[0].status is Status.PENDING


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
