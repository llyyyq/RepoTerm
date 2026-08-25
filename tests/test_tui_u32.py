from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace

from repoterm.safety.permissions import PermissionManager
from repoterm.tools.registry import ToolRegistry
from repoterm.ui.tui.event_flow import _handle_event
from repoterm.ui.tui.input_handler import (
    UiEventDispatcher,
    _execute_tool_shortcut,
    _handle_input,
    drain_ui_events,
)
import repoterm.ui.tui.input_handler as input_handler_module
from repoterm.ui.tui.input_parser import KeyEvent
from repoterm.ui.tui.state import ScreenState, TtyAppArgs
from repoterm.ui.tui.ui_events import ApprovalTicketManager, UiEventQueue, UiEventType
from repoterm.ui.tui.worker_lifecycle import UiLifecycleState, WorkerLifecycle


def _args(tmp_path) -> TtyAppArgs:
    return TtyAppArgs(
        runtime={"model": "test"},
        tools=SimpleNamespace(),
        model=object(),
        messages=[{"role": "system", "content": "sys"}],
        cwd=str(tmp_path),
        permissions=SimpleNamespace(),
    )


def test_ui_event_queue_is_fifo_immutable_and_uses_bounded_drain() -> None:
    queue = UiEventQueue()
    payload = {"content": "hello", "nested": {"value": 1}}
    assert queue.publish(UiEventType.ASSISTANT_MESSAGE, "turn-1", payload)
    payload["content"] = "changed"
    payload["nested"]["value"] = 2
    queue.publish(UiEventType.ASSISTANT_MESSAGE, "turn-1", {"content": "second"})
    queue.publish(UiEventType.PROGRESS_MESSAGE, "turn-1", {"content": "third"})

    events = queue.drain(max_events=2)
    assert len(events) == 2
    assert events[0].sequence == 1
    assert events[0].payload["content"] == "hello"
    assert events[0].payload["nested"]["value"] == 1
    assert events[1].sequence == 2
    remaining = queue.drain()
    assert [event.sequence for event in remaining] == [3]
    assert remaining[0].payload["content"] == "third"


def test_worker_event_does_not_change_screen_until_main_thread_drains(tmp_path) -> None:
    state = ScreenState()
    state.ui_event_queue = UiEventQueue()
    args = _args(tmp_path)
    rerenders: list[str] = []
    dispatcher = UiEventDispatcher(args, state, lambda: rerenders.append("render"), "turn-1")
    state.ui_event_handler = dispatcher

    state.ui_event_queue.publish(
        UiEventType.ASSISTANT_MESSAGE,
        "turn-1",
        {"content": "from worker"},
    )
    assert state.transcript == []

    assert drain_ui_events(args, state, lambda: rerenders.append("render")) == 1
    assert [entry.body for entry in state.transcript] == ["from worker"]
    assert rerenders


def test_non_owner_thread_cannot_drain_or_consume_ui_events(tmp_path) -> None:
    state = ScreenState()
    args = _args(tmp_path)
    dispatcher = UiEventDispatcher(args, state, lambda: None, "turn-1")
    state.ui_event_handler = dispatcher
    state.ui_event_queue.publish(
        UiEventType.ASSISTANT_MESSAGE,
        "turn-1",
        {"content": "owner only"},
    )
    errors: list[str] = []

    def wrong_thread_drain() -> None:
        try:
            drain_ui_events(args, state, lambda: None)
        except RuntimeError as error:
            errors.append(str(error))

    worker = Thread(target=wrong_thread_drain)
    worker.start()
    worker.join(timeout=1)
    assert errors and "owner thread" in errors[0]
    assert state.transcript == []
    assert state.ui_event_queue.pending == 1

    drain_ui_events(args, state, lambda: None)
    assert [entry.body for entry in state.transcript] == ["owner only"]


def test_agent_callback_only_publishes_until_main_thread_drains(tmp_path, monkeypatch) -> None:
    callback_seen = Event()
    release = Event()

    def fake_run_agent_turn(**kwargs):
        kwargs["on_assistant_message"]("worker output")
        callback_seen.set()
        release.wait(2)
        return [*kwargs["messages"], {"role": "assistant", "content": "done"}]

    monkeypatch.setattr(input_handler_module, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(input_handler_module, "save_history_entries", lambda _history: None)
    args = _args(tmp_path)
    args.tools = ToolRegistry([])
    args.permissions = PermissionManager(str(tmp_path))
    state = ScreenState(input="hello")

    assert input_handler_module._handle_input(args, state, lambda: None) is False
    assert callback_seen.wait(1)
    assert [entry.body for entry in state.transcript] == ["hello"]

    release.set()
    joiner = Thread(target=lambda: state.agent_thread.join(timeout=3))
    joiner.start()
    joiner.join(timeout=3)
    assert not joiner.is_alive()
    assert not any(entry.body == "worker output" for entry in state.transcript)
    assert state.ui_event_queue.pending >= 1
    drain_ui_events(args, state, lambda: None)
    assert any(entry.body == "worker output" for entry in state.transcript)


def test_agent_failure_is_redacted_once_and_ignores_late_events(tmp_path, monkeypatch) -> None:
    def failing_run_agent_turn(**_kwargs):
        raise RuntimeError("apiKey=sk-1234567890abcdef")

    monkeypatch.setattr(input_handler_module, "run_agent_turn", failing_run_agent_turn)
    monkeypatch.setattr(input_handler_module, "save_history_entries", lambda _history: None)
    args = _args(tmp_path)
    args.tools = ToolRegistry([])
    args.permissions = PermissionManager(str(tmp_path))
    state = ScreenState(input="fail safely")

    assert _handle_input(args, state, lambda: None) is False
    state.agent_thread.join(timeout=3)
    assert state.is_busy is True
    assert "done" not in state.agent_result

    drain_ui_events(args, state, lambda: None)
    errors = [entry for entry in state.transcript if entry.runtimeKind == "error"]
    assert len(errors) == 1
    assert "sk-1234567890abcdef" not in errors[0].body
    assert state.is_busy is False
    assert state.active_tool is None
    assert state.status is None

    transcript_after_failure = list(state.transcript)
    messages_after_failure = list(args.messages)
    turn_id = state.active_turn_id
    assert turn_id is not None
    state.ui_event_queue.publish(
        UiEventType.ASSISTANT_MESSAGE,
        turn_id,
        {"content": "late assistant output"},
    )
    state.ui_event_queue.publish(
        UiEventType.AGENT_COMPLETED,
        turn_id,
        {"messages": [*args.messages, {"role": "assistant", "content": "late"}]},
    )

    assert drain_ui_events(args, state, lambda: None) == 0
    assert state.transcript == transcript_after_failure
    assert args.messages == messages_after_failure


def test_exit_command_remains_available_while_worker_is_busy(tmp_path) -> None:
    args = _args(tmp_path)
    state = ScreenState(input="/exit", is_busy=True)
    assert _handle_input(args, state, lambda: None) is True


def test_event_order_and_stream_merge_do_not_cross_runtime_or_tool_boundaries(tmp_path) -> None:
    state = ScreenState()
    state.ui_event_queue = UiEventQueue()
    args = _args(tmp_path)
    dispatcher = UiEventDispatcher(args, state, lambda: None, "turn-1")
    state.ui_event_handler = dispatcher

    state.ui_event_queue.publish(UiEventType.RUNTIME_EVENT, "turn-1", {
        "category": "phase", "message": "phase", "step": 1,
    })
    state.ui_event_queue.publish(UiEventType.TOOL_START, "turn-1", {
        "tool_name": "read_file", "tool_input": {"path": "a.txt"},
    })
    state.ui_event_queue.publish(UiEventType.TOOL_RESULT, "turn-1", {
        "tool_name": "read_file", "output": "done", "is_error": False,
    })
    state.ui_event_queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "turn-1", {"content": "a"})
    state.ui_event_queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "turn-1", {"content": "b"})
    state.ui_event_queue.publish(UiEventType.RUNTIME_EVENT, "turn-1", {
        "category": "stop", "message": "done", "step": 2, "stop_reason": "done",
    })
    state.ui_event_queue.publish(UiEventType.AGENT_COMPLETED, "turn-1", {
        "messages": [{"role": "assistant", "content": "done"}],
    })

    queued = state.ui_event_queue.drain()
    assert len(queued) == 6
    assert queued[3].payload["content"] == "ab"
    for event in queued:
        from repoterm.ui.tui.input_handler import apply_ui_event
        apply_ui_event(args, state, event, lambda: None)
    assert [entry.kind for entry in state.transcript] == [
        "progress", "tool", "progress"
    ]
    assert state.transcript[2].runtimeKind == "stop"
    assert state.agent_result is None


def test_shortcut_worker_does_not_block_main_thread_and_presents_once(tmp_path) -> None:
    started = Event()
    release = Event()
    calls: list[str] = []

    def execute(_name, _input, *, context):
        started.set()
        release.wait(2)
        calls.append("execute")
        return SimpleNamespace(ok=True, output="shortcut output")

    state = ScreenState()
    args = _args(tmp_path)
    args.tools = SimpleNamespace(execute=execute)
    renders: list[str] = []
    _execute_tool_shortcut(args, state, "read_file", {"path": "a.txt"}, lambda: renders.append("render"))
    assert started.wait(1)
    assert state.is_busy is True
    # The main thread remains able to update independent UI state while the
    # controlled tool is blocked.
    state.transcript_scroll_offset = 3
    assert state.transcript_scroll_offset == 3
    assert calls == []

    release.set()
    state.shortcut_thread.join(2)
    assert calls == ["execute"]
    assert state.is_busy is True
    assert state.transcript[-1].collapsed is False
    drain_ui_events(args, state, lambda: None)
    assert len([entry for entry in state.transcript if entry.kind == "tool"]) == 1
    assert state.transcript[-1].collapsed is True
    assert state.is_busy is False


def test_shortcut_failure_is_redacted_and_restores_busy_state(tmp_path) -> None:
    started = Event()

    def execute(_name, _input, *, context):
        started.set()
        raise RuntimeError("token=sk-1234567890abcdef")

    state = ScreenState()
    args = _args(tmp_path)
    args.tools = SimpleNamespace(execute=execute)
    _execute_tool_shortcut(args, state, "read_file", {"path": "a.txt"}, lambda: None)

    assert started.wait(1)
    state.shortcut_thread.join(timeout=3)
    assert state.is_busy is True
    assert state.status == "Running read_file..."

    drain_ui_events(args, state, lambda: None)
    assert state.is_busy is False
    assert state.active_tool is None
    assert state.status is None
    assert [tool for tool in state.recent_tools if tool["status"] == "error"] == [
        {"name": "read_file", "status": "error"}
    ]
    assert len([entry for entry in state.transcript if entry.kind == "tool"]) == 1
    assert "sk-1234567890abcdef" not in state.transcript[-1].body


def test_permission_tickets_are_independent_and_resolve_once(tmp_path) -> None:
    queue = UiEventQueue()
    manager = ApprovalTicketManager(queue)
    first = manager.create({"summary": "first", "choices": [{"key": "y", "decision": "allow_once"}]}, "turn")
    second = manager.create({"summary": "second", "choices": [{"key": "n", "decision": "deny_once"}]}, "turn")
    state = ScreenState()
    state.ui_event_queue = queue
    state.approval_manager = manager
    args = _args(tmp_path)
    args.permissions = SimpleNamespace()

    events = queue.drain()
    for event in events:
        from repoterm.ui.tui.input_handler import apply_ui_event
        apply_ui_event(args, state, event, lambda: None)

    assert state.pending_approval is not None
    assert state.pending_approval.request_id == first.request_id
    _handle_event(
        args,
        state,
        KeyEvent(name="y", ctrl=False, meta=False),
        lambda: None,
        manager,
        {},
        lambda *_args: False,
    )
    assert first.wait()["decision"] == "allow_once"
    assert state.pending_approval is not None
    assert state.pending_approval.request_id == second.request_id
    assert not manager.resolve(first.request_id, {"decision": "deny_once"})

    _handle_event(
        args,
        state,
        KeyEvent(name="n", ctrl=False, meta=False),
        lambda: None,
        manager,
        {},
        lambda *_args: False,
    )
    assert second.wait()["decision"] == "deny_once"


def test_closing_wakes_permission_waiter_and_rejects_late_events() -> None:
    queue = UiEventQueue()
    manager = ApprovalTicketManager(queue)
    lifecycle = WorkerLifecycle(queue)
    ticket = manager.create({"summary": "wait"}, "turn")
    result: list[dict] = []
    waiter = Thread(target=lambda: result.append(ticket.wait()), daemon=True)
    waiter.start()

    lifecycle.close(manager)
    lifecycle.join_all(timeout=0.1)
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert result == [{"decision": "deny_once", "reason": "ui_closed"}]
    assert lifecycle.state is UiLifecycleState.CLOSED
    assert not queue.publish(UiEventType.ASSISTANT_MESSAGE, "late", {"content": "ignored"})
    late_ticket = manager.create({"summary": "after close"}, "permission")
    assert late_ticket.resolved is True
    assert late_ticket.wait() == {"decision": "deny_once", "reason": "ui_closed"}


def test_worker_lifecycle_close_and_join_are_idempotent() -> None:
    queue = UiEventQueue()
    lifecycle = WorkerLifecycle(queue)
    started = Event()
    release = Event()

    def worker_body() -> None:
        started.set()
        release.wait(2)

    worker = lifecycle.start(worker_body, name="u32-idempotence")
    assert started.wait(1)
    lifecycle.close()
    lifecycle.close()
    assert lifecycle.join_all(timeout=0.01) is False
    assert lifecycle.state is UiLifecycleState.CLOSED
    assert not queue.publish(UiEventType.PROGRESS_MESSAGE, "late", {"content": "ignored"})
    release.set()
    worker.join(timeout=1)
    assert lifecycle.join_all(timeout=0.01) is True
