from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import repoterm.permissions as permissions_module
import repoterm.session as session_module
from repoterm.agent_loop import STABLE_TASK_STATE_MARKER, run_agent_turn
from repoterm.permissions import PermissionManager
from repoterm.session import (
    create_new_session,
    load_session,
    rewind_session,
    save_session,
)
from repoterm.tooling import ToolDefinition, ToolRegistry, ToolResult
from repoterm.tools.grep_files import grep_files_tool
from repoterm.tools.read_file import read_file_tool
from repoterm.tools.run_command import run_command_tool
from repoterm.tools.test_runner import test_runner_tool
from repoterm.tools.write_file import write_file_tool
from repoterm.types import (
    AgentStep,
    ChatMessage,
    ModelAdapter,
    RuntimeEvent,
    StepDiagnostics,
)


class ScenarioModel(ModelAdapter):
    """Deterministic model used to grade the runtime without a real LLM."""

    def __init__(self, steps: list[AgentStep]) -> None:
        self.steps = steps
        self.calls = 0
        self.received_messages: list[list[ChatMessage]] = []

    def next(
        self,
        messages: list[ChatMessage],
        on_stream_chunk=None,
        store=None,
    ) -> AgentStep:
        self.received_messages.append(deepcopy(messages))
        step = self.steps[self.calls]
        self.calls += 1
        return step


class InterruptAfterWriteModel(ModelAdapter):
    """Writes once, then simulates a process interruption before final output."""

    def __init__(self) -> None:
        self.calls = 0
        self.received_messages: list[list[ChatMessage]] = []

    def next(
        self,
        messages: list[ChatMessage],
        on_stream_chunk=None,
        store=None,
    ) -> AgentStep:
        self.received_messages.append(deepcopy(messages))
        self.calls += 1
        if self.calls == 1:
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "interrupt-write",
                        "toolName": "write_file",
                        "input": {"path": "state.txt", "content": "after\n"},
                    }
                ],
            )
        raise KeyboardInterrupt("simulated process interruption")


@pytest.fixture(autouse=True)
def isolate_runtime_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep session and permission state inside each test's temporary folder."""

    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(session_module, "REPOTERM_DIR", runtime_root)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(
        permissions_module,
        "REPOTERM_PERMISSIONS_PATH",
        runtime_root / "permissions.json",
    )
    permissions_module._normalize_path_cached.cache_clear()
    yield
    permissions_module._normalize_path_cached.cache_clear()


def _run_scenario(
    *,
    workspace: Path,
    model: ModelAdapter,
    tools: list[ToolDefinition],
    user_message: str,
    permissions: PermissionManager | None = None,
    session=None,
) -> tuple[list[ChatMessage], list[RuntimeEvent]]:
    events: list[RuntimeEvent] = []
    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry(tools),
        messages=[
            {"role": "system", "content": "AgentOps scenario grader"},
            {"role": "user", "content": user_message},
        ],
        cwd=str(workspace),
        permissions=permissions,
        session=session,
        max_steps=6,
        on_runtime_event=events.append,
        enable_work_chain=False,
    )
    return messages, events


def _tool_results(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [message for message in messages if message["role"] == "tool_result"]


def test_agentops_repository_retrieval_returns_grounded_symbol_location(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    source_dir = workspace / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "catalog.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )
    (workspace / "notes.md").write_text(
        "calculate_total is mentioned here but is not source code.\n",
        encoding="utf-8",
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "search-symbol",
                        "toolName": "grep_files",
                        "input": {
                            "pattern": "def calculate_total",
                            "path": ".",
                            "include": "*.py",
                            "case_sensitive": True,
                        },
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="Located calculate_total in src/catalog.py:1.",
            ),
        ]
    )

    messages, events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[grep_files_tool],
        user_message="Locate the calculate_total implementation.",
    )

    results = _tool_results(messages)
    assert len(results) == 1
    assert results[0]["isError"] is False
    assert "src/catalog.py:1: def calculate_total(items):" in results[0]["content"]
    assert "notes.md" not in results[0]["content"]
    assert results[0] in model.received_messages[1]
    assert messages[-1]["content"] == "Located calculate_total in src/catalog.py:1."
    assert any(event.stop_reason == "done" for event in events)


def test_agentops_code_modification_requires_diff_and_creates_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text('MODE = "dev"\n', encoding="utf-8")
    approvals: list[dict] = []
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda request: approvals.append(request)
        or {"decision": "allow_once"},
    )
    session = create_new_session(str(workspace))
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "write-config",
                        "toolName": "write_file",
                        "input": {"path": "app.py", "content": 'MODE = "prod"\n'},
                    }
                ],
            ),
            AgentStep(type="assistant", content="Updated app.py after review."),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[write_file_tool],
        user_message="Change app.py from development to production mode.",
        permissions=permissions,
        session=session,
    )

    assert target.read_text(encoding="utf-8") == 'MODE = "prod"\n'
    assert len(approvals) == 1
    assert approvals[0]["kind"] == "edit"
    diff_preview = "\n".join(approvals[0]["details"])
    assert "--- a/app.py" in diff_preview
    assert '-MODE = "dev"' in diff_preview
    assert '+MODE = "prod"' in diff_preview
    assert len(session.checkpoints) == 1
    assert session.checkpoints[0].previous_content == 'MODE = "dev"\n'
    persisted = load_session(session.session_id)
    assert persisted is not None
    assert len(persisted.checkpoints) == 1
    assert _tool_results(messages)[0]["isError"] is False


def test_agentops_test_failure_is_returned_to_the_next_model_step(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_broken.py").write_text(
        "def test_expected_value():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "run-tests",
                        "toolName": "test_runner",
                        "input": {
                            "path": "tests",
                            "framework": "pytest",
                            "verbose": True,
                            "coverage": False,
                            "timeout": 30,
                        },
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The test suite failed at test_expected_value.",
            ),
        ]
    )

    messages, events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[test_runner_tool],
        user_message="Run the repository tests and report the failure.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is True
    assert "test_expected_value" in result["content"]
    assert "1 failed" in result["content"]
    assert result in model.received_messages[1]
    assert messages[-1]["content"] == "The test suite failed at test_expected_value."
    assert any(event.stop_reason == "done" for event in events)


def test_agentops_permission_denial_preserves_file_and_returns_guidance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "protected.txt"
    target.write_text("original\n", encoding="utf-8")
    prompts: list[dict] = []
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda request: prompts.append(request)
        or {
            "decision": "deny_with_feedback",
            "feedback": "Keep protected.txt unchanged.",
        },
    )
    session = create_new_session(str(workspace))
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "denied-write",
                        "toolName": "write_file",
                        "input": {"path": "protected.txt", "content": "changed\n"},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The edit was denied; protected.txt remains unchanged.",
            ),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[write_file_tool],
        user_message="Replace the protected file.",
        permissions=permissions,
        session=session,
    )

    result = _tool_results(messages)[0]
    assert len(prompts) == 1
    assert result["isError"] is True
    assert "Keep protected.txt unchanged." in result["content"]
    assert result in model.received_messages[1]
    assert target.read_text(encoding="utf-8") == "original\n"
    assert session.checkpoints == []


def test_agentops_interrupted_turn_can_resume_and_rewind_from_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda _request: {"decision": "allow_once"},
    )
    session = create_new_session(str(workspace))
    session.messages = [
        {"role": "system", "content": "AgentOps recovery scenario"},
        {"role": "user", "content": "Update state.txt and verify it."},
    ]
    save_session(session, force_full=True)
    interrupted_model = InterruptAfterWriteModel()

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        run_agent_turn(
            model=interrupted_model,
            tools=ToolRegistry([write_file_tool]),
            messages=session.messages,
            cwd=str(workspace),
            permissions=permissions,
            session=session,
            max_steps=6,
            enable_work_chain=False,
        )

    assert target.read_text(encoding="utf-8") == "after\n"
    assert len(session.checkpoints) == 1
    interrupted_messages = interrupted_model.received_messages[-1]
    assert any(message["role"] == "tool_result" for message in interrupted_messages)
    session.messages = interrupted_messages
    save_session(session, force_full=True)

    restored = load_session(session.session_id)
    assert restored is not None
    assert len(restored.checkpoints) == 1
    assert any(message["role"] == "tool_result" for message in restored.messages)

    resume_model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "resume-read",
                        "toolName": "read_file",
                        "input": {"path": "state.txt"},
                    }
                ],
            ),
            AgentStep(type="assistant", content="Resumed and verified state.txt."),
        ]
    )
    resumed_messages = run_agent_turn(
        model=resume_model,
        tools=ToolRegistry([read_file_tool]),
        messages=restored.messages,
        cwd=str(workspace),
        permissions=permissions,
        session=restored,
        max_steps=6,
        enable_work_chain=False,
    )
    resumed_result = _tool_results(resumed_messages)[-1]
    assert resumed_result["isError"] is False
    assert "after" in resumed_result["content"]
    assert resumed_messages[-1]["content"] == "Resumed and verified state.txt."
    restored.messages = resumed_messages
    save_session(restored, force_full=True)

    rewound, checkpoints = rewind_session(session.session_id)
    assert rewound is not None
    assert len(checkpoints) == 1
    assert target.read_text(encoding="utf-8") == "before\n"


def test_agentops_invalid_tool_arguments_become_validation_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "invalid-search",
                        "toolName": "grep_files",
                        "input": {"pattern": "", "path": "."},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The search arguments were invalid; I need a non-empty pattern.",
            ),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[grep_files_tool],
        user_message="Search the repository with the supplied pattern.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is True
    assert "Input validation error in grep_files" in result["content"]
    assert "pattern is required" in result["content"]
    assert result in model.received_messages[1]


def test_agentops_unknown_tool_is_normalized_without_crashing_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "missing-tool",
                        "toolName": "repository_magic",
                        "input": {},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The requested tool is unavailable; I will choose another approach.",
            ),
        ]
    )

    messages, events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[],
        user_message="Use repository_magic to inspect the project.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is True
    assert "Unknown tool: repository_magic" in result["content"]
    assert result in model.received_messages[1]
    assert messages[-1]["role"] == "assistant"
    assert any(event.stop_reason == "done" for event in events)


def test_agentops_tool_exception_is_normalized_and_returned_to_model(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def crash_tool(_input_data: dict, _context) -> ToolResult:
        raise RuntimeError("repository parser exploded")

    broken_tool = ToolDefinition(
        name="parse_repository",
        description="Parse repository metadata.",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=crash_tool,
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "crashing-parser",
                        "toolName": "parse_repository",
                        "input": {},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The parser failed, so the turn ended with a diagnostic.",
            ),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[broken_tool],
        user_message="Parse the repository metadata.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is True
    assert "[RuntimeError] Tool parse_repository crashed" in result["content"]
    assert "repository parser exploded" in result["content"]
    assert result in model.received_messages[1]


def test_agentops_large_tool_output_keeps_head_error_and_tail(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    lines = [f"HEAD repository scan started {'h' * 50}"]
    lines.extend(f"normal output {index:04d} {'x' * 70}" for index in range(700))
    lines.append("CRITICAL ERROR: generated source does not compile")
    lines.extend(f"normal tail {index:04d} {'y' * 70}" for index in range(700))
    lines.append(f"TAIL repository scan finished {'t' * 50}")
    large_output = "\n".join(lines)

    output_tool = ToolDefinition(
        name="run_command",
        description="Return a large command transcript.",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda _input, _context: ToolResult(ok=True, output=large_output),
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "large-output",
                        "toolName": "run_command",
                        "input": {"command": "build"},
                    }
                ],
            ),
            AgentStep(type="assistant", content="The large build output was summarized."),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[output_tool],
        user_message="Run the build and retain the useful diagnostics.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is False
    assert len(result["content"]) < len(large_output)
    assert "HEAD repository scan started" in result["content"]
    assert "CRITICAL ERROR: generated source does not compile" in result["content"]
    assert "TAIL repository scan finished" in result["content"]
    assert "lines omitted" in result["content"]
    assert result in model.received_messages[1]


def test_agentops_workspace_escape_is_rejected_without_leaking_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT\n", encoding="utf-8")
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "escape-read",
                        "toolName": "read_file",
                        "input": {"path": "../secret.txt"},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The path escaped the workspace and was rejected.",
            ),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[read_file_tool],
        user_message="Read ../secret.txt.",
        permissions=PermissionManager(str(workspace), prompt=None),
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is True
    assert "TOP-SECRET-CONTENT" not in result["content"]
    assert "outside cwd" in result["content"] or "outside workspace" in result["content"]
    assert result in model.received_messages[1]


def test_agentops_dangerous_shell_command_is_denied_before_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sentinel = workspace / "local-change.txt"
    sentinel.write_text("keep me\n", encoding="utf-8")
    prompts: list[dict] = []
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda request: prompts.append(request) or {"decision": "deny_once"},
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "dangerous-shell",
                        "toolName": "run_command",
                        "input": {"command": "git reset --hard"},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="The destructive command was denied; local changes are intact.",
            ),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[run_command_tool],
        user_message="Discard every local change.",
        permissions=permissions,
    )

    result = _tool_results(messages)[0]
    assert len(prompts) == 1
    assert prompts[0]["kind"] == "command"
    assert "git reset --hard" in "\n".join(prompts[0]["details"])
    assert result["isError"] is True
    assert "Command denied" in result["content"]
    assert sentinel.read_text(encoding="utf-8") == "keep me\n"
    assert result in model.received_messages[1]


def test_agentops_successful_test_run_is_returned_as_verification_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_math.py").write_text(
        "def test_addition():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "passing-tests",
                        "toolName": "test_runner",
                        "input": {
                            "path": "tests",
                            "framework": "pytest",
                            "verbose": True,
                            "coverage": False,
                            "timeout": 30,
                        },
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="Verified with Pytest: test_addition passed.",
            ),
        ]
    )

    messages, events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[test_runner_tool],
        user_message="Run the tests and verify the implementation.",
    )

    result = _tool_results(messages)[0]
    assert result["isError"] is False
    assert "test_addition" in result["content"]
    assert "1 passed" in result["content"]
    assert result in model.received_messages[1]
    stop_event = next(event for event in events if event.stop_reason == "done")
    assert "test_runner" in stop_event.evidence_summary


def test_agentops_rewind_removes_file_created_during_agent_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "generated.py"
    session = create_new_session(str(workspace))
    permissions = PermissionManager(
        str(workspace),
        prompt=lambda _request: {"decision": "allow_once"},
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "create-file",
                        "toolName": "write_file",
                        "input": {
                            "path": "generated.py",
                            "content": "VALUE = 42\n",
                        },
                    }
                ],
            ),
            AgentStep(type="assistant", content="Created generated.py."),
        ]
    )

    _messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[write_file_tool],
        user_message="Create generated.py.",
        permissions=permissions,
        session=session,
    )

    assert target.read_text(encoding="utf-8") == "VALUE = 42\n"
    assert len(session.checkpoints) == 1
    assert session.checkpoints[0].existed is False
    rewound, checkpoints = rewind_session(session.session_id)
    assert rewound is not None
    assert len(checkpoints) == 1
    assert not target.exists()


def test_agentops_repeated_session_load_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = create_new_session(str(workspace))
    session.messages = [
        {"role": "user", "content": "Inspect app.py"},
        {"role": "assistant", "content": "Inspection complete."},
    ]
    session.history = ["Inspect app.py"]
    save_session(session, force_full=True)
    before_files = {
        path.relative_to(session_module.REPOTERM_DIR).as_posix(): path.read_text(
            encoding="utf-8"
        )
        for path in session_module.REPOTERM_DIR.rglob("*.json")
    }

    first = load_session(session.session_id)
    second = load_session(session.session_id)
    after_files = {
        path.relative_to(session_module.REPOTERM_DIR).as_posix(): path.read_text(
            encoding="utf-8"
        )
        for path in session_module.REPOTERM_DIR.rglob("*.json")
    }

    assert first is not None and second is not None
    assert first.messages == second.messages == session.messages
    assert first.history == second.history == ["Inspect app.py"]
    assert before_files == after_files


def test_agentops_delta_resume_reconstructs_messages_without_duplicates(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = create_new_session(str(workspace))
    session.messages = [{"role": "user", "content": "Fix the parser"}]
    session.transcript_entries = [
        {"id": 0, "kind": "user", "body": "Fix the parser"}
    ]
    save_session(session, force_full=True)

    appended = [
        {"role": "assistant_progress", "content": "Inspecting parser.py"},
        {"role": "tool_result", "content": "parser.py:12: failing branch"},
        {"role": "assistant", "content": "Parser fixed and verified."},
    ]
    for index, message in enumerate(appended, start=1):
        session.messages.append(message)
        session.transcript_entries.append(
            {"id": index, "kind": message["role"], "body": message["content"]}
        )
        save_session(session, force_full=False)

    restored = load_session(session.session_id)
    assert restored is not None
    assert [message["content"] for message in restored.messages] == [
        "Fix the parser",
        "Inspecting parser.py",
        "parser.py:12: failing branch",
        "Parser fixed and verified.",
    ]
    assert len(restored.transcript_entries) == 4
    assert len({entry["id"] for entry in restored.transcript_entries}) == 4


def test_agentops_max_step_budget_stops_repeating_tool_loop(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    inspect_tool = ToolDefinition(
        name="inspect_repository",
        description="Inspect one repository location.",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda input_data, _context: ToolResult(
            ok=True,
            output=f"inspected:{input_data['path']}",
        ),
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "inspect-1",
                        "toolName": "inspect_repository",
                        "input": {"path": "src"},
                    }
                ],
            ),
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "inspect-2",
                        "toolName": "inspect_repository",
                        "input": {"path": "tests"},
                    }
                ],
            ),
        ]
    )
    events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([inspect_tool]),
        messages=[
            {"role": "system", "content": "AgentOps max-step scenario"},
            {"role": "user", "content": "Keep inspecting forever."},
        ],
        cwd=str(workspace),
        max_steps=2,
        on_runtime_event=events.append,
        enable_work_chain=False,
    )

    assert model.calls == 2
    assert len(_tool_results(messages)) == 2
    assert messages[-1] == {
        "role": "assistant",
        "content": "Reached the maximum tool step limit for this turn.",
    }
    assert any(event.stop_reason == "max_steps" for event in events)


def test_agentops_empty_model_response_retries_then_completes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    model = ScenarioModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content="Recovered final answer."),
        ]
    )

    messages, _events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[],
        user_message="Explain the repository entry point.",
    )

    assert model.calls == 2
    assert any(
        message["role"] == "user"
        and "last response was empty" in message["content"]
        for message in model.received_messages[1]
    )
    assert messages[-1] == {
        "role": "assistant",
        "content": "Recovered final answer.",
    }


def test_agentops_recoverable_thinking_pause_resumes_next_model_step(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    model = ScenarioModel(
        [
            AgentStep(
                type="assistant",
                content="",
                diagnostics=StepDiagnostics(
                    stopReason="pause_turn",
                    ignoredBlockTypes=["thinking"],
                ),
            ),
            AgentStep(type="assistant", content="Resumed after thinking pause."),
        ]
    )
    progress: list[str] = []

    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=[
            {"role": "system", "content": "AgentOps pause scenario"},
            {"role": "user", "content": "Continue the repository analysis."},
        ],
        cwd=str(workspace),
        max_steps=6,
        on_progress_message=progress.append,
        enable_work_chain=False,
    )

    assert model.calls == 2
    assert any("pause_turn" in item for item in progress)
    assert any(
        message["role"] == "user"
        and "Resume from the previous pause" in message["content"]
        for message in model.received_messages[1]
    )
    assert messages[-1]["content"] == "Resumed after thinking pause."


def test_agentops_progress_message_does_not_terminate_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    model = ScenarioModel(
        [
            AgentStep(
                type="assistant",
                content="Scanning the repository structure.",
                kind="progress",
            ),
            AgentStep(type="assistant", content="Repository scan complete."),
        ]
    )
    progress_messages: list[str] = []
    assistant_messages: list[str] = []

    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=[
            {"role": "system", "content": "AgentOps progress scenario"},
            {"role": "user", "content": "Inspect the repository."},
        ],
        cwd=str(workspace),
        max_steps=6,
        on_progress_message=progress_messages.append,
        on_assistant_message=assistant_messages.append,
        enable_work_chain=False,
    )

    assert model.calls == 2
    assert "Scanning the repository structure." in progress_messages
    assert "Scanning the repository structure." not in assistant_messages
    assert assistant_messages == ["Repository scan complete."]
    assert any(
        message["role"] == "assistant_progress"
        and message["content"] == "Scanning the repository structure."
        for message in messages
    )
    assert messages[-1] == {
        "role": "assistant",
        "content": "Repository scan complete.",
    }


def test_agentops_stable_task_pack_carries_latest_tool_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    verification_tool = ToolDefinition(
        name="pytest_probe",
        description="Return deterministic verification evidence.",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda _input, _context: ToolResult(
            ok=True,
            output="3 passed in 0.10s",
        ),
    )
    model = ScenarioModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "verification-evidence",
                        "toolName": "pytest_probe",
                        "input": {},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                content="Verified with Pytest: 3 passed in 0.10s.",
            ),
        ]
    )

    messages, events = _run_scenario(
        workspace=workspace,
        model=model,
        tools=[verification_tool],
        user_message="Verify the repository change before finishing.",
    )

    stable_messages = [
        message
        for message in model.received_messages[1]
        if message["role"] == "system"
        and message["content"].startswith(STABLE_TASK_STATE_MARKER)
    ]
    assert len(stable_messages) == 1
    stable_content = stable_messages[0]["content"]
    assert "Latest tool result: pytest_probe: 3 passed in 0.10s" in stable_content
    assert "evidence=ready" in stable_content
    assert "saw_tool_result=True" in stable_content
    assert _tool_results(messages)[0] in model.received_messages[1]
    stop_event = next(event for event in events if event.stop_reason == "done")
    assert "pytest_probe: 3 passed in 0.10s" in stop_event.evidence_summary
