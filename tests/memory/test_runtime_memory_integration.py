from __future__ import annotations

from pathlib import Path

from repoterm.agent_reflection import ReflectionEngine
from repoterm.memory import MemoryService, Status


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
        "verified task",
        [
            {"type": "tool_call", "toolName": "pytest"},
            {
                "type": "tool_result",
                "ok": True,
                "success": True,
                "isError": False,
                "content": "1 passed",
            },
            {"type": "assistant", "content": "Verified."},
        ],
    )
    assert verified.success is True
    pending = service.list_pending()
    assert len(pending) == 1
    assert pending[0].status == Status.PENDING

