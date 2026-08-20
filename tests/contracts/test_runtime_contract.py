"""Deterministic user-observable contracts for the refactor safety line.

Run this focused baseline with::

    python -m pytest -q tests/contracts

The scenarios deliberately use scripted adapters and temporary directories;
they never contact a real model or mutate the repository under test.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import repoterm.permissions as permissions_module
import repoterm.session as session_module
from repoterm.agent_loop import run_agent_turn
from repoterm.context_manager import ContextManager
from repoterm.permissions import PermissionManager
from repoterm.session import (
    create_file_checkpoint,
    create_new_session,
    load_session,
    save_session,
)
from repoterm.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from repoterm.tools.write_file import write_file_tool
from repoterm.types import AgentStep, ChatMessage, RuntimeEvent


class DeterministicModel:
    """A finite script that makes AgentOps scenarios replayable offline."""

    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = steps
        self.calls = 0
        self.received_messages: list[list[ChatMessage]] = []

    def next(self, messages, on_stream_chunk=None, store=None) -> AgentStep:
        self.received_messages.append(deepcopy(messages))
        step = self._steps[self.calls]
        self.calls += 1
        return step


@pytest.fixture
def isolated_runtime_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Keep permission/session persistence out of the user's real profile."""

    runtime_root = tmp_path / "runtime-state"
    monkeypatch.setattr(session_module, "REPOTERM_DIR", runtime_root)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(
        permissions_module,
        "REPOTERM_PERMISSIONS_PATH",
        runtime_root / "permissions.json",
    )
    yield runtime_root


def test_agent_phases_and_verification_guard_are_observable(tmp_path: Path) -> None:
    """A final claim is rejected until it cites evidence gathered this turn."""

    workspace = tmp_path / "repo"
    workspace.mkdir()
    probe = ToolDefinition(
        name="verification_probe",
        description="Return deterministic verification evidence.",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda _value, _context: ToolResult(
            ok=True,
            output="contract_probe: 1 passed",
        ),
    )
    model = DeterministicModel(
        [
            AgentStep(type="assistant", content="Inspecting inputs.", kind="progress"),
            AgentStep(type="assistant", content="Choosing an approach.", kind="progress"),
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "contract-verification",
                        "toolName": "verification_probe",
                        "input": {},
                    }
                ],
            ),
            AgentStep(type="assistant", content="Done; the change is correct."),
            AgentStep(
                type="assistant",
                content="Verified by contract_probe: 1 passed; the change is correct.",
            ),
        ]
    )
    events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([probe]),
        messages=[
            {"role": "system", "content": "Deterministic contract runtime"},
            {"role": "user", "content": "Repair and verify the runtime."},
        ],
        cwd=str(workspace),
        runtime={"runtimeProfile": "single-deep"},
        on_runtime_event=events.append,
        enable_work_chain=False,
    )

    phases = [event.phase for event in events if event.category == "phase"]
    assert phases.index("explore") < phases.index("execute") < phases.index("verify")
    assert any(event.category == "guard" for event in events)
    assert any(
        message["role"] == "user"
        and "strict verification mode" in message["content"]
        for message in messages
    )
    assert messages[-1] == {
        "role": "assistant",
        "content": "Verified by contract_probe: 1 passed; the change is correct.",
    }


def test_tool_registry_rejects_invalid_arguments_as_a_tool_result(tmp_path: Path) -> None:
    """Invalid input never reaches the runner and is normalized for the agent."""

    runner_inputs: list[dict] = []

    def validate(value):
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            raise ValueError("path must be a string")
        return value

    tool = ToolDefinition(
        name="contract_reader",
        description="Read a contract fixture.",
        input_schema={"type": "object"},
        validator=validate,
        run=lambda value, _context: (
            runner_inputs.append(value) or ToolResult(ok=True, output="unreachable")
        ),
    )

    result = ToolRegistry([tool]).execute(
        "contract_reader",
        {"path": 42},
        ToolContext(cwd=str(tmp_path)),
    )

    assert isinstance(result, ToolResult)
    assert result.ok is False
    assert "Input validation error" in result.output
    assert "path must be a string" in result.output
    assert runner_inputs == []


def test_context_compaction_preserves_user_constraints_and_key_results() -> None:
    """Compaction may discard narration, but not intent, edits, or failures."""

    manager = ContextManager(context_window=420)
    messages = [
        {"role": "system", "content": "SYSTEM CONTRACT: preserve safety policy."},
        {
            "role": "user",
            "content": "Keep the public API stable while repairing src/critical.py.",
        },
        {
            "role": "assistant_tool_call",
            "toolName": "write_file",
            "toolUseId": "edit-1",
            "input": {"path": "src/critical.py", "content": "fixed"},
        },
        {
            "role": "tool_result",
            "toolName": "write_file",
            "toolUseId": "edit-1",
            "content": "Updated src/critical.py after review.",
            "isError": False,
        },
        {
            "role": "tool_result",
            "toolName": "test_runner",
            "toolUseId": "test-1",
            "content": "contract_parser failed: invariant broken",
            "isError": True,
        },
    ]
    messages.extend(
        {
            "role": "assistant_progress",
            "content": f"temporary narration {index}: " + ("noise " * 80),
        }
        for index in range(12)
    )
    messages.append(
        {"role": "user", "content": "Final constraint: do not change the public API."}
    )
    for message in messages:
        manager.add_message(message)

    assert manager.should_auto_compact() is True
    compacted = manager.compact_messages()
    compacted_text = "\n".join(str(message.get("content", "")) for message in compacted)

    assert compacted[0] == messages[0]
    assert "Keep the public API stable" in compacted_text
    assert "src/critical.py" in compacted_text
    assert "contract_parser failed" in compacted_text
    assert "Final constraint: do not change the public API" in compacted_text
    assert len(compacted) < len(messages)


def test_permission_denial_leaves_workspace_and_checkpoints_unchanged(
    tmp_path: Path,
    isolated_runtime_storage: Path,
) -> None:
    """A denied edit cannot partially write data or create a checkpoint."""

    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "protected.txt"
    target.write_text("original\n", encoding="utf-8")
    session = create_new_session(str(workspace))
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda _request: {
            "decision": "deny_with_feedback",
            "feedback": "Keep protected.txt unchanged.",
        },
    )

    result = ToolRegistry([write_file_tool]).execute(
        "write_file",
        {"path": "protected.txt", "content": "changed\n"},
        ToolContext(
            cwd=str(workspace),
            permissions=permissions,
            session=session,
        ),
    )

    assert result.ok is False
    assert "Keep protected.txt unchanged." in result.output
    assert target.read_text(encoding="utf-8") == "original\n"
    assert session.checkpoints == []
    assert [path.name for path in workspace.iterdir()] == ["protected.txt"]


def test_checkpointed_session_resume_is_idempotent(
    tmp_path: Path,
    isolated_runtime_storage: Path,
) -> None:
    """Repeated resume reconstructs one logical state without duplicate deltas."""

    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    session = create_new_session(str(workspace))
    session.messages = [{"role": "user", "content": "Update state.txt safely."}]
    save_session(session, force_full=True)
    checkpoint = create_file_checkpoint(
        session,
        file_path=str(target),
        existed=True,
        previous_content="before\n",
    )
    assert checkpoint is not None
    target.write_text("after\n", encoding="utf-8")
    session.messages.append(
        {"role": "assistant", "content": "Updated state.txt with a checkpoint."}
    )
    save_session(session, force_full=False)
    before_load = {
        path.relative_to(isolated_runtime_storage).as_posix(): path.read_text(
            encoding="utf-8"
        )
        for path in isolated_runtime_storage.rglob("*.json")
    }

    first = load_session(session.session_id)
    second = load_session(session.session_id)
    after_load = {
        path.relative_to(isolated_runtime_storage).as_posix(): path.read_text(
            encoding="utf-8"
        )
        for path in isolated_runtime_storage.rglob("*.json")
    }

    assert first is not None and second is not None
    assert first.messages == second.messages == session.messages
    assert len(first.checkpoints) == len(second.checkpoints) == 1
    assert first.checkpoints[0].checkpoint_id == checkpoint.checkpoint_id
    assert second.checkpoints[0].previous_content == "before\n"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert before_load == after_load


def test_memory_pending_requires_approval_and_rejection_is_not_retrievable(
    tmp_path: Path,
) -> None:
    """Model candidates stay out of retrieval until a human approves them."""

    from repoterm.memory import (
        EvidenceKind,
        EvidenceLevel,
        MemoryService,
        VerificationEvidence,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=workspace,
    )

    def evidence(turn_id: str) -> tuple[VerificationEvidence, ...]:
        return (
            VerificationEvidence.create(
                level=EvidenceLevel.VALIDATION,
                kind=EvidenceKind.TEST,
                tool_name="pytest",
                ok=True,
                summary="contract test passed",
                source_session_id="contract-session",
                source_turn_id=turn_id,
            ),
        )

    approved = service.propose_experience(
        "Applicable condition: contract validation is required.\n"
        "Effective action: use the contract test command for validation.\n"
        "Verification result: pytest passed.",
        evidence=evidence("turn-approved"),
    )
    rejected = service.propose_experience(
        "Applicable condition: an unverified shortcut is proposed.\n"
        "Effective action: do not use the unverified shortcut.\n"
        "Verification result: pytest passed.",
        evidence=evidence("turn-rejected"),
    )

    assert approved.status.value == "pending"
    assert service.search("contract test command") == []
    service.approve(approved.id)
    service.reject(rejected.id)

    assert [entry.id for entry in service.search("contract test command")] == [
        approved.id
    ]
    assert service.search("unverified shortcut") == []
