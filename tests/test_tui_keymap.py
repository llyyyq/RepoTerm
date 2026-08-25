from __future__ import annotations

import threading

import pytest

from repoterm.ui.tui.event_flow import (
    _handle_event,
    _handle_normal_mode_key,
    _handle_normal_mode_navigation,
    _handle_normal_mode_return,
    _handle_normal_mode_tab,
)
from repoterm.ui.tui.input_parser import KeyEvent, TextEvent, parse_input_chunk
from repoterm.ui.tui.input_handler import _WIN_SCANCODE_TO_ANSI
from repoterm.ui.tui import navigation as navigation_module
from repoterm.ui.tui.layout import LayoutMetrics
from repoterm.ui.tui.navigation import _get_visible_commands
from repoterm.ui.tui.types import TranscriptEntry
from repoterm.ui.commands import find_matching_slash_commands
from repoterm.ui.tui.state import PendingApproval, ScreenState, TtyAppArgs


def _args() -> TtyAppArgs:
    return TtyAppArgs(
        runtime=None,
        tools=None,
        model=None,
        messages=[],
        cwd=".",
        permissions=None,
    )


def test_control_bytes_are_key_events_not_text_events() -> None:
    events = parse_input_chunk("\x01\x05\x10\x0e\x15\x03\x17\x0b", incoming_chunk="\x01\x05\x10\x0e\x15\x03\x17\x0b").events

    assert all(isinstance(event, KeyEvent) for event in events)
    assert [event.name for event in events] == ["a", "e", "p", "n", "u", "c", "w", "k"]
    assert all(event.ctrl is True and event.meta is False for event in events)
    assert not any(isinstance(event, TextEvent) for event in events)


def test_alt_character_is_a_key_event() -> None:
    result = parse_input_chunk("\x1ba", incoming_chunk="\x1ba")

    assert len(result.events) == 1
    assert result.events[0] == KeyEvent(name="a", ctrl=False, meta=True)


@pytest.mark.parametrize(
    "submitted_text",
    ["/help", "/tools", "/session", "/read README.md", "/cmd python --version", "inspect the README"],
)
def test_exact_commands_and_natural_language_submit_without_usage_insertion(
    submitted_text: str,
) -> None:
    submitted: list[str] = []
    state = ScreenState(input=submitted_text, cursor_offset=len(submitted_text))

    _handle_normal_mode_return(
        _args(),
        state,
        _get_visible_commands(state.input),
        lambda: None,
        lambda _args, _state, _rerender, value: submitted.append(value) or False,
    )

    assert submitted == [submitted_text]
    assert state.input == ""


def test_unique_partial_slash_completion_uses_name_only() -> None:
    state = ScreenState(input="/rea", cursor_offset=4)

    _handle_normal_mode_tab(state, _get_visible_commands(state.input), lambda: None)

    assert state.input == "/read "
    assert "<" not in state.input and "[" not in state.input


def test_unknown_slash_command_does_not_offer_every_command() -> None:
    assert find_matching_slash_commands("/definitely-unknown") == []
    state = ScreenState(input="/definitely-unknown", cursor_offset=19)
    submitted: list[str] = []

    _handle_normal_mode_return(
        _args(),
        state,
        _get_visible_commands(state.input),
        lambda: None,
        lambda _args, _state, _rerender, value: submitted.append(value) or False,
    )

    assert submitted == ["/definitely-unknown"]


def test_function_key_sequences_are_key_events() -> None:
    result = parse_input_chunk(
        "\x1bOP\x1bOQ\x1bOR\x1bOS\x1b[15~\x1b[24~\x1b[1;5P",
        incoming_chunk="\x1bOP\x1bOQ\x1bOR\x1bOS\x1b[15~\x1b[24~\x1b[1;5P",
    )

    assert result.events == [
        KeyEvent(name="f1", ctrl=False, meta=False),
        KeyEvent(name="f2", ctrl=False, meta=False),
        KeyEvent(name="f3", ctrl=False, meta=False),
        KeyEvent(name="f4", ctrl=False, meta=False),
        KeyEvent(name="f5", ctrl=False, meta=False),
        KeyEvent(name="f12", ctrl=False, meta=False),
        KeyEvent(name="f1", ctrl=True, meta=False),
    ]
    assert all(isinstance(event, KeyEvent) for event in result.events)


def test_windows_function_scan_codes_are_not_swallowed() -> None:
    assert _WIN_SCANCODE_TO_ANSI[59] == "\x1bOP"
    assert _WIN_SCANCODE_TO_ANSI[68] == "\x1b[21~"
    assert _WIN_SCANCODE_TO_ANSI[134] == "\x1b[24~"


def test_ctrl_left_and_right_reach_word_navigation_before_plain_navigation() -> None:
    events = parse_input_chunk("\x1b[1;5D\x1b[1;5C", incoming_chunk="\x1b[1;5D\x1b[1;5C").events
    assert events == [
        KeyEvent(name="left", ctrl=True, meta=False),
        KeyEvent(name="right", ctrl=True, meta=False),
    ]

    state = ScreenState(input="one two", cursor_offset=7)
    assert _handle_normal_mode_navigation(state, events[0], lambda: None) is True
    assert state.cursor_offset == 4

    assert _handle_normal_mode_navigation(state, events[1], lambda: None) is True
    assert state.cursor_offset == 7


def test_ctrl_home_end_move_transcript_without_moving_input_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(navigation_module, "_cached_terminal_size", lambda: (80, 24))
    state = ScreenState(
        input="draft",
        cursor_offset=2,
        transcript=[
            # Enough wrapped content to create a non-zero transcript offset.
            TranscriptEntry(
                id=1,
                kind="assistant",
                body="long line " * 80,
            )
        ],
        layout_metrics=LayoutMetrics(
            terminal_columns=80,
            terminal_rows=24,
            transcript_body_lines=3,
        ),
    )

    assert _handle_normal_mode_key(
        _args(),
        state,
        KeyEvent(name="home", ctrl=True, meta=False),
        [],
        lambda: None,
        lambda *_args: False,
    ) is True
    top_offset = state.transcript_scroll_offset
    assert top_offset > 0
    assert state.input == "draft"
    assert state.cursor_offset == 2

    assert _handle_normal_mode_key(
        _args(),
        state,
        KeyEvent(name="end", ctrl=True, meta=False),
        [],
        lambda: None,
        lambda *_args: False,
    ) is True
    assert state.transcript_scroll_offset == 0
    assert state.input == "draft"
    assert state.cursor_offset == 2


def test_ctrl_u_is_handled_by_key_event_path() -> None:
    event = parse_input_chunk("\x15", incoming_chunk="\x15").events[0]
    state = ScreenState(input="draft", cursor_offset=5)

    assert isinstance(event, KeyEvent)
    assert _handle_normal_mode_key(_args(), state, event, [], lambda: None, lambda *_args: False) is True
    assert state.input == ""
    assert state.cursor_offset == 0


def test_ctrl_c_raises_system_exit_as_a_key_event() -> None:
    with pytest.raises(SystemExit):
        _handle_event(
            _args(),
            ScreenState(),
            KeyEvent(name="c", ctrl=True, meta=False),
            lambda: None,
            threading.Event(),
            {},
            lambda *_args: False,
        )


def test_pending_approval_key_uses_key_name() -> None:
    state = ScreenState(
        pending_approval=PendingApproval(
            request={"choices": [{"key": "y", "decision": "allow_once"}]},
            resolve=lambda _result: None,
        )
    )
    approval_event = threading.Event()
    approval_result: dict[str, object] = {}

    _handle_event(
        _args(),
        state,
        KeyEvent(name="y", ctrl=False, meta=False),
        lambda: None,
        approval_event,
        approval_result,
        lambda *_args: False,
    )

    assert approval_event.is_set()
    assert approval_result["decision"] == "allow_once"
