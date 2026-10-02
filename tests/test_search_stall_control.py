"""Progress governance also runs when optional work_chain is disabled."""

from types import SimpleNamespace

from repoterm.runtime.loop import run_agent_turn
from repoterm.tools.registry import ToolDefinition, ToolRegistry, ToolResult
from repoterm.contracts.types import AgentStep


class RepeatingSearchModel:
    def __init__(self) -> None:
        self.calls = 0

    def next(self, messages, on_stream_chunk=None, store=None):
        self.calls += 1
        if self.calls <= 6:
            return AgentStep(
                type="tool_calls",
                calls=[{
                    "id": f"grep-{self.calls}",
                    "toolName": "grep_files",
                    "input": {"pattern": f"query-{self.calls}"},
                }],
            )
        return AgentStep(type="assistant", content="Search is blocked; more evidence is needed.")


class NeverStoppingSearchModel:
    def __init__(self) -> None:
        self.calls = 0

    def next(self, messages, on_stream_chunk=None, store=None):
        self.calls += 1
        return AgentStep(type="tool_calls", calls=[{
            "id": f"grep-{self.calls}", "toolName": "grep_files",
            "input": {"pattern": f"query-{self.calls}"},
        }])


def test_repeated_successful_searches_receive_governance_feedback(tmp_path) -> None:
    actual_calls = []

    def run_grep(input_data, context):
        actual_calls.append(input_data["pattern"])
        return ToolResult(ok=True, output="src/module.py:5: same location")

    tool = ToolDefinition(
        name="grep_files",
        description="Test search",
        input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
        validator=lambda data: data,
        run=run_grep,
    )
    messages = run_agent_turn(
        model=RepeatingSearchModel(),
        tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "Find the implementation"}],
        cwd=str(tmp_path),
        max_steps=7,
        enable_work_chain=False,
    )

    results = [message for message in messages if message["role"] == "tool_result"]
    assert len(results) == 6
    assert len(actual_calls) == 6
    assert all(result["isError"] is False for result in results)
    assert any("Recent actions did not add task evidence" in result["content"] for result in results)


def test_unproductive_action_sequence_stops_after_recovery_window(tmp_path) -> None:
    actual_calls = []
    tool = ToolDefinition(
        name="grep_files", description="Test search",
        input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
        validator=lambda data: data,
        run=lambda data, context: (actual_calls.append(data["pattern"]) or ToolResult(ok=True, output="src/module.py:5: same location")),
    )
    model = NeverStoppingSearchModel()
    session = SimpleNamespace(progress_governance={}, messages=[])
    messages = run_agent_turn(
        model=model, tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "Find the implementation"}],
        cwd=str(tmp_path), max_steps=40, enable_work_chain=False, session=session,
    )
    assert model.calls == 9
    assert len(actual_calls) == 9
    assert messages[-1]["role"] == "assistant"
    assert "任务未被证明完成" in messages[-1]["content"]
    assert session.progress_governance["message_count"] == len(messages)
    assert session.progress_governance["state"]["stage"] == "stop_stuck"


class MapThenReadModel:
    def __init__(self) -> None:
        self.calls = 0

    def next(self, messages, on_stream_chunk=None, store=None):
        self.calls += 1
        if self.calls <= 6 or self.calls in {8, 10}:
            return AgentStep(type="tool_calls", calls=[{
                "id": f"grep-{self.calls}", "toolName": "grep_files",
                "input": {"pattern": f"query-{self.calls}"},
            }])
        if self.calls == 7:
            return AgentStep(type="tool_calls", calls=[{
                "id": "map-1", "toolName": "repository_map",
                "input": {"query": "failing test"},
            }])
        if self.calls == 9:
            return AgentStep(type="tool_calls", calls=[{
                "id": "read-1", "toolName": "read_file",
                "input": {"path": "src/module.py", "start_line": 20},
            }])
        return AgentStep(type="assistant", content="Need to fix the candidate next.")


def test_map_then_targeted_read_resumes_search_without_forcing_completion(tmp_path) -> None:
    actual_greps = []
    registry = ToolRegistry([
        ToolDefinition(
            name="grep_files", description="Test search", input_schema={"type": "object"},
            validator=lambda data: data,
            run=lambda data, context: (actual_greps.append(data["pattern"]) or ToolResult(ok=True, output="src/module.py:5: same location")),
        ),
        ToolDefinition(
            name="repository_map", description="Test map", input_schema={"type": "object"},
            validator=lambda data: data,
            run=lambda data, context: ToolResult(ok=True, output="src/module.py:20-22 candidate"),
        ),
        ToolDefinition(
            name="read_file", description="Test read", input_schema={"type": "object"},
            validator=lambda data: data,
            run=lambda data, context: ToolResult(ok=True, output="FILE: src/module.py\nSTART_LINE: 20\nEND_LINE: 22\n20: def candidate():"),
        ),
    ])
    messages = run_agent_turn(
        model=MapThenReadModel(), tools=registry,
        messages=[{"role": "user", "content": "Find the implementation"}],
        cwd=str(tmp_path), max_steps=15, enable_work_chain=False,
    )
    assert len(actual_greps) == 8
    assert messages[-1]["role"] == "assistant"
    assert "Need to fix" in messages[-1]["content"]
