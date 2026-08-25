from __future__ import annotations

from typing import Any

from repoterm.ui.commands import SLASH_COMMANDS, find_matching_slash_commands
from repoterm.ui.tui.chrome import _cached_terminal_size
from repoterm.ui.tui.state import ScreenState, TtyAppArgs
from repoterm.ui.tui.chrome import get_permission_prompt_max_scroll_offset
from repoterm.ui.tui.layout import LayoutMetrics, compute_state_layout
from repoterm.ui.tui.transcript import get_transcript_max_scroll_offset


def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    cached_metrics = state.layout_metrics
    if isinstance(cached_metrics, LayoutMetrics):
        columns, rows = _cached_terminal_size()
        if (columns, rows) == (cached_metrics.terminal_columns, cached_metrics.terminal_rows):
            return cached_metrics.transcript_body_lines

    return compute_state_layout(args, state).transcript_body_lines


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(
        state.transcript,
        _get_transcript_body_lines(args, state),
        state.transcript_revision,
    )


def _scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    max_offset = _get_max_transcript_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    next_offset = _get_max_transcript_scroll_offset(args, state) if target == "top" else 0
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _scroll_pending_approval_by(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or not pending.details_expanded:
        return False
    max_offset = get_permission_prompt_max_scroll_offset(pending.request, expanded=True)
    next_offset = max(0, min(max_offset, pending.details_scroll_offset + delta))
    if next_offset == pending.details_scroll_offset:
        return False
    pending.details_scroll_offset = next_offset
    return True


def _toggle_pending_approval_expand(state: ScreenState) -> bool:
    pending = state.pending_approval
    if not pending or pending.request.get("kind") != "edit":
        return False
    pending.details_expanded = not pending.details_expanded
    pending.details_scroll_offset = 0
    return True


def _move_pending_approval_selection(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or pending.feedback_mode:
        return False
    total = len(pending.request.get("choices", []))
    if total <= 0:
        return False
    pending.selected_choice_index = (pending.selected_choice_index + delta + total) % total
    return True


def _history_up(state: ScreenState) -> bool:
    if not state.history or state.history_index <= 0:
        return False
    if state.history_index == len(state.history):
        state.history_draft = state.input
    state.history_index -= 1
    state.input = state.history[state.history_index] if state.history_index < len(state.history) else ""
    state.cursor_offset = len(state.input)
    return True


def _history_down(state: ScreenState) -> bool:
    if state.history_index >= len(state.history):
        return False
    state.history_index += 1
    state.input = (
        state.history_draft
        if state.history_index == len(state.history)
        else (state.history[state.history_index] if state.history_index < len(state.history) else "")
    )
    state.cursor_offset = len(state.input)
    return True


def _get_visible_commands(input_text: str) -> list[Any]:
    if not input_text.startswith("/"):
        return []
    if input_text == "/":
        return SLASH_COMMANDS
    matches = find_matching_slash_commands(input_text)
    visible = [
        cmd for cmd in SLASH_COMMANDS if getattr(cmd, "usage", str(cmd)) in matches
    ]
    # Put the shortest matching command first. This makes a prefix such as
    # ``/rea`` complete to the actionable ``/read`` command by default while
    # keeping longer matches available through menu navigation.
    return sorted(
        visible,
        key=lambda command: (
            len(getattr(command, "name", "")),
            getattr(command, "usage", str(command)),
        ),
    )


def _slash_command_completion(command: Any) -> str:
    """Return only a command name plus a space when arguments are supported."""
    name = getattr(command, "name", str(command))
    usage = getattr(command, "usage", name)
    return name + (" " if usage.strip() != name else "")


def _is_exact_slash_command(input_text: str) -> bool:
    """Return whether the first token names a registered slash command.

    Arguments remain part of the submitted input, so ``/read file.txt`` and
    ``/cmd python --version`` resolve through their exact command entries.
    """
    if not input_text.startswith("/"):
        return False
    command_name = input_text.split(maxsplit=1)[0]
    return any(
        getattr(command, "name", "") == command_name
        for command in SLASH_COMMANDS
    )
