"""Terminal layout measurement and height allocation for the TUI.

This module deliberately contains no rendering side effects.  Renderers measure
the strings they are about to write, then use one :class:`LayoutMetrics`
instance to allocate the feed and the surrounding UI components.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .chrome import _cached_terminal_size, char_display_width, safe_terminal_width


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True, slots=True)
class LayoutMetrics:
    """Measured line budget for one terminal frame.

    ``prompt_lines`` and ``slash_menu_lines`` are kept separate so the menu
    can be removed from the budget when it is closed.  ``transcript_body_lines``
    is the remaining window available to transcript content after the optional
    session summary has been reserved.
    """

    terminal_columns: int
    terminal_rows: int
    header_lines: int = 0
    transcript_body_lines: int = 0
    prompt_lines: int = 0
    footer_lines: int = 0
    contextual_help_lines: int = 0
    slash_menu_lines: int = 0
    approval_lines: int = 0
    gap_lines: int = 0
    transcript_summary_lines: int = 0
    transcript_frame_lines: int = 0
    minimum_transcript_lines: int = 0

    @property
    def feed_body_lines(self) -> int:
        """Return the total feed body budget, including its summary."""

        return self.transcript_summary_lines + self.transcript_body_lines

    @property
    def reserved_lines(self) -> int:
        """Return all non-transcript lines reserved by the layout."""

        return (
            self.header_lines
            + self.prompt_lines
            + self.footer_lines
            + self.contextual_help_lines
            + self.slash_menu_lines
            + self.approval_lines
            + self.gap_lines
            + self.transcript_frame_lines
            + self.transcript_summary_lines
        )

    @property
    def total_lines(self) -> int:
        """Return the number of lines represented by this allocation."""

        return self.reserved_lines + self.transcript_body_lines


def _clean_count(value: int | None) -> int:
    return max(0, int(value or 0))


def _visual_line_count(line: str, width: int | None) -> int:
    if width is None or width <= 0:
        return 1

    plain = _ANSI_RE.sub("", line)
    if not plain:
        return 1

    rows = 1
    used = 0
    for char in plain:
        char_width = char_display_width(char)
        if used and used + char_width > width:
            rows += 1
            used = 0
        if char_width > width:
            rows += 1
            used = 0
        else:
            used += char_width
    return rows


def measure_rendered_lines(rendered: str | None, width: int | None = None) -> int:
    """Count logical or display-wrapped lines in an already-rendered string."""

    if not rendered:
        return 0
    return sum(_visual_line_count(line, width) for line in rendered.split("\n"))


def compute_layout_metrics(
    terminal_columns: int,
    terminal_rows: int,
    *,
    header_lines: int = 0,
    prompt_lines: int = 0,
    footer_lines: int = 1,
    contextual_help_lines: int = 0,
    slash_menu_lines: int = 0,
    approval_lines: int = 0,
    gap_lines: int = 0,
    transcript_summary_lines: int = 0,
    transcript_frame_lines: int = 0,
    normal_minimum: int = 8,
    tiny_minimum: int = 4,
    tiny_terminal_rows: int = 24,
) -> LayoutMetrics:
    """Compute a bounded layout allocation from measured component heights.

    The formula is intentionally direct:

    ``terminal rows - header - prompt - footer - menu - help - approval``
    ``- gaps - feed frame - summary = transcript rows``.

    No minimum is allowed to make the allocation exceed the terminal.  A
    terminal below ``tiny_terminal_rows`` therefore uses the smaller minimum
    only as a preference; the actual result always remains bounded.
    """

    columns = max(1, int(terminal_columns))
    rows = max(1, int(terminal_rows))
    header = _clean_count(header_lines)
    prompt = _clean_count(prompt_lines)
    footer = _clean_count(footer_lines)
    help_lines = _clean_count(contextual_help_lines)
    menu = _clean_count(slash_menu_lines)
    approval = _clean_count(approval_lines)
    gaps = _clean_count(gap_lines)
    frame = _clean_count(transcript_frame_lines)
    requested_summary = _clean_count(transcript_summary_lines)

    fixed = header + prompt + footer + help_lines + menu + approval + gaps + frame
    feed_available = max(0, rows - fixed)
    summary = min(requested_summary, feed_available)
    transcript_available = max(0, feed_available - summary)

    preferred_minimum = (
        _clean_count(normal_minimum)
        if rows >= tiny_terminal_rows
        else _clean_count(tiny_minimum)
    )
    actual_minimum = min(preferred_minimum, transcript_available)

    return LayoutMetrics(
        terminal_columns=columns,
        terminal_rows=rows,
        header_lines=header,
        transcript_body_lines=transcript_available,
        prompt_lines=prompt,
        footer_lines=footer,
        contextual_help_lines=help_lines,
        slash_menu_lines=menu,
        approval_lines=approval,
        gap_lines=gaps,
        transcript_summary_lines=summary,
        transcript_frame_lines=frame,
        minimum_transcript_lines=actual_minimum,
    )


def compute_state_layout(
    args: Any,
    state: Any,
    *,
    header_rendered: str | None = None,
    prompt_rendered: str | None = None,
    footer_rendered: str | None = None,
    contextual_help_rendered: str | None = None,
    slash_menu_rendered: str | None = None,
    approval_rendered: str | None = None,
    transcript_summary_lines: int = 0,
    transcript_frame_lines: int = 2,
    gap_lines: int = 3,
) -> LayoutMetrics:
    """Compute a layout for a TUI state using measured component strings.

    The optional rendered strings let the main renderer pass the exact strings
    it will write.  Navigation can call this function without them as a
    fallback before the first frame has been rendered.
    """

    columns, rows = _cached_terminal_size()
    measured_width = safe_terminal_width(columns)

    if header_rendered is None:
        from .chrome import render_banner
        from .tool_helpers import _get_session_stats

        header_rendered = render_banner(
            args.runtime,
            args.cwd,
            args.permissions.get_summary(),
            _get_session_stats(args, state),
        )

    if prompt_rendered is None:
        from .chrome import render_composer
        from .input import render_input_prompt

        prompt_rendered = render_composer(
            render_input_prompt(state.input, state.cursor_offset, compact=True)
        )

    if slash_menu_rendered is None:
        from .chrome import render_slash_menu
        from .navigation import _get_visible_commands

        commands = _get_visible_commands(state.input)
        slash_menu_rendered = (
            render_slash_menu(commands, min(state.selected_slash_index, len(commands) - 1))
            if commands
            else ""
        )

    if footer_rendered is None:
        from repoterm.tools.background import list_background_tasks
        from .chrome import render_footer_bar

        footer_rendered = render_footer_bar(
            state.status,
            True,
            bool(args.tools.get_skills()),
            list_background_tasks(),
        )

    if approval_rendered is None and state.pending_approval is not None:
        from .chrome import render_permission_prompt

        pending = state.pending_approval
        approval_rendered = render_permission_prompt(
            pending.request,
            expanded=pending.details_expanded,
            scroll_offset=pending.details_scroll_offset,
            selected_choice_index=pending.selected_choice_index,
            feedback_mode=pending.feedback_mode,
            feedback_input=pending.feedback_input,
        )

    return compute_layout_metrics(
        columns,
        rows,
        header_lines=measure_rendered_lines(header_rendered, measured_width),
        prompt_lines=measure_rendered_lines(prompt_rendered, measured_width),
        footer_lines=measure_rendered_lines(footer_rendered, measured_width),
        contextual_help_lines=measure_rendered_lines(contextual_help_rendered, measured_width),
        slash_menu_lines=measure_rendered_lines(slash_menu_rendered, measured_width),
        approval_lines=measure_rendered_lines(approval_rendered, measured_width),
        gap_lines=gap_lines,
        transcript_summary_lines=transcript_summary_lines,
        transcript_frame_lines=transcript_frame_lines,
    )
