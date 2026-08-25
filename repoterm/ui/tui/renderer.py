from __future__ import annotations
from typing import Any
from repoterm.tools.background import list_background_tasks
from repoterm.session import format_checkpoint_summary_line
from repoterm.ui.tui.chrome import (
    _cached_terminal_size,
    render_banner,
    render_footer_bar,
    render_composer,
    render_permission_prompt,
    render_slash_menu,
    render_session_feed,
    render_status_line,
    render_tool_panel,
    safe_terminal_width,
    string_display_width,
    SUBTLE,
    RESET,
)
from repoterm.ui.tui.input import render_input_prompt
from repoterm.ui.tui.layout import compute_state_layout, measure_rendered_lines
from repoterm.ui.tui.transcript import format_runtime_summary_line, render_transcript
from repoterm.ui.tui.state import TtyAppArgs, ScreenState
from repoterm.ui.tui.navigation import _get_visible_commands
from repoterm.ui.tui.tool_helpers import _get_session_stats
from repoterm.ui.tui.types import TranscriptEntry
from repoterm.ui.tui.screen import write_frame

# Rendering — cached header & footer
# ---------------------------------------------------------------------------

# Banner cache: the banner rarely changes (only when cwd, model, or stats change).
_banner_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}

_transcript_snapshot_cache: dict[
    str,
    tuple[tuple[int, int, int], list[TranscriptEntry]],
] = {}


def _render_header_panel(args: TtyAppArgs, state: ScreenState) -> str:
    """Render the top banner panel with model info, cwd, and session stats.
    
    The result is cached to avoid re-rendering when stats haven't changed.
    """
    stats = _get_session_stats(args, state)
    cache_key = (
        args.cwd,
        id(args.runtime),
        stats.get("transcriptCount"),
        stats.get("messageCount"),
        stats.get("skillCount"),
        stats.get("mcpCount"),
        _cached_terminal_size(),
    )
    cached = _banner_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_banner(
        args.runtime,
        args.cwd,
        args.permissions.get_summary(),
        stats,
    )
    _banner_cache["key"] = (cache_key, result)
    return result


# Footer cache: only changes with status, tool/skill state, background tasks
_footer_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


def _render_footer_cached(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]],
) -> str:
    """Render the bottom status bar with caching to reduce flicker.
    
    Shows current operation status, tool/skill availability, and background tasks.
    """
    cache_key = (
        status,
        tools_enabled,
        skills_enabled,
        len(background_tasks),
        _cached_terminal_size(),
    )
    cached = _footer_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_footer_bar(status, tools_enabled, skills_enabled, background_tasks)
    _footer_cache["key"] = (cache_key, result)
    return result


def _get_prompt_menu(state: ScreenState) -> str:
    commands = _get_visible_commands(state.input)
    if not commands:
        return ""
    return render_slash_menu(
        commands,
        min(state.selected_slash_index, len(commands) - 1),
    )


def _render_prompt_panel(
    state: ScreenState,
    *,
    include_menu: bool = True,
    menu_rendered: str | None = None,
) -> str:
    input_body = render_input_prompt(state.input, state.cursor_offset, compact=True)
    width, _ = _cached_terminal_size()
    width = safe_terminal_width(width)
    hint = f"{SUBTLE}Enter send · Ctrl+J newline · / commands{RESET}"
    if "\n" not in input_body and string_display_width(input_body + "  " + hint) <= max(1, width):
        prompt_body = f"{input_body}  {hint}"
    else:
        prompt_body = f"{input_body}\n{hint}"

    if include_menu:
        menu = _get_prompt_menu(state) if menu_rendered is None else menu_rendered
        if menu:
            _, rows = _cached_terminal_size()
            base_lines = len(render_composer(prompt_body).splitlines())
            menu = _limit_rendered_lines(menu, max(0, rows - base_lines))
        if menu:
            prompt_body += "\n" + menu
    return render_composer(prompt_body)


def _limit_rendered_lines(rendered: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    return "\n".join(rendered.splitlines()[:maximum])


def _get_transcript_snapshot(state: ScreenState) -> list[TranscriptEntry]:
    cache_key = (id(state.transcript), state.transcript_revision, len(state.transcript))
    cached = _transcript_snapshot_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]

    snapshot = list(state.transcript)
    _transcript_snapshot_cache["key"] = (cache_key, snapshot)
    return snapshot


def _get_session_summary_text(
    transcript_entries: list[TranscriptEntry],
    session: Any | None = None,
) -> str:
    checkpoint_summary_line = format_checkpoint_summary_line(session)
    runtime_summary_line = format_runtime_summary_line(transcript_entries)
    session_metadata = getattr(session, "metadata", None)
    summary_lines = [
        line
        for line in (
            checkpoint_summary_line,
            runtime_summary_line,
            f"readiness-summary: {session_metadata.readiness_summary}"
            if session_metadata and getattr(session_metadata, "readiness_summary", "")
            else "",
            f"instruction-summary: {session_metadata.instruction_summary}"
            if session_metadata and getattr(session_metadata, "instruction_summary", "")
            else "",
            f"hook-summary: {session_metadata.hook_summary}"
            if session_metadata and getattr(session_metadata, "hook_summary", "")
            else "",
            f"delegation-summary: {session_metadata.delegation_summary}"
            if session_metadata and getattr(session_metadata, "delegation_summary", "")
            else "",
            f"extension-summary: {session_metadata.extension_summary}"
            if session_metadata and getattr(session_metadata, "extension_summary", "")
            else "",
        )
        if line
    ]
    if not summary_lines:
        return ""
    summary_block = f"{RESET}\n{SUBTLE}".join(summary_lines)
    return f"{SUBTLE}{summary_block}{RESET}"


def _decorate_session_feed_body(
    transcript_body: str,
    transcript_entries: list[TranscriptEntry],
    session: Any | None = None,
) -> str:
    summary_text = _get_session_summary_text(transcript_entries, session)
    if not summary_text:
        return transcript_body
    if not transcript_body:
        return summary_text
    return f"{summary_text}\n\n{transcript_body}"


def _render_screen(args: TtyAppArgs, state: ScreenState) -> None:
    background_tasks = list_background_tasks()
    header = _render_header_panel(args, state)
    has_skills = len(args.tools.get_skills()) > 0
    footer = _render_footer_cached(state.status, True, has_skills, background_tasks)

    # Snapshot the list to avoid IndexError from concurrent agent-thread appends.
    transcript_snapshot = _get_transcript_snapshot(state)
    summary_text = _get_session_summary_text(transcript_snapshot, state.session)
    columns, rows = _cached_terminal_size()
    measured_width = safe_terminal_width(columns)
    summary_lines = measure_rendered_lines(summary_text, measured_width)
    if summary_text and transcript_snapshot:
        summary_lines += 1  # one intentional blank line between summary and feed

    if state.pending_approval:
        pending = state.pending_approval
        approval_gaps = 2
        header_lines = measure_rendered_lines(header, measured_width)
        footer_lines = measure_rendered_lines(footer, measured_width)
        approval_budget = max(0, rows - header_lines - footer_lines - approval_gaps)
        approval = render_permission_prompt(
            pending.request,
            expanded=pending.details_expanded,
            scroll_offset=pending.details_scroll_offset,
            selected_choice_index=pending.selected_choice_index,
            feedback_mode=pending.feedback_mode,
            feedback_input=pending.feedback_input,
            approval_budget=approval_budget,
        )
        metrics = compute_state_layout(
            args,
            state,
            header_rendered=header,
            prompt_rendered="",
            footer_rendered=footer,
            slash_menu_rendered="",
            approval_rendered=approval,
            transcript_frame_lines=0,
            gap_lines=approval_gaps,
        )
        state.layout_metrics = metrics
        output = "\n\n".join((header, approval, footer))
        write_frame(output)
        return

    prompt_base = _render_prompt_panel(state, include_menu=False)
    full_menu = _get_prompt_menu(state)
    menu_budget = max(
        0,
        rows
        - measure_rendered_lines(header, measured_width)
        - measure_rendered_lines(prompt_base, measured_width)
        - measure_rendered_lines(footer, measured_width)
        - 3
        - 2
        - summary_lines,
    )
    menu = _limit_rendered_lines(full_menu, menu_budget)
    prompt = _render_prompt_panel(state, include_menu=True, menu_rendered=menu)
    metrics = compute_state_layout(
        args,
        state,
        header_rendered=header,
        prompt_rendered=prompt_base,
        footer_rendered=footer,
        slash_menu_rendered=menu,
        contextual_help_rendered=None,
        transcript_summary_lines=summary_lines,
        transcript_frame_lines=2,
        gap_lines=3,
    )
    state.layout_metrics = metrics

    if transcript_snapshot:
        transcript_body = render_transcript(
            transcript_snapshot,
            state.transcript_scroll_offset,
            metrics.transcript_body_lines,
            state.transcript_revision,
        )
        transcript_body = _decorate_session_feed_body(
            transcript_body,
            transcript_snapshot,
            state.session,
        )
    else:
        transcript_body = f"{render_status_line(None)}\n\nType /help for commands."

    feed = render_session_feed(
        transcript_body,
        right_title=f"{len(transcript_snapshot)} events",
        max_body_lines=metrics.feed_body_lines,
    )
    # Build the entire frame into a buffer, then write once.  The three blank
    # separators are part of the same metrics calculation above.
    output = "\n\n".join((header, feed, prompt, footer))
    write_frame(output)


# ---------------------------------------------------------------------------
