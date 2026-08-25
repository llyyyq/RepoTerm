from __future__ import annotations

from .chrome import (
    _cached_terminal_size,
    BRIGHT_GREEN,
    BOLD,
    HIGHLIGHT_BG,
    RESET,
    char_display_width,
    safe_terminal_width,
    string_display_width,
)
from .theme import theme


def _build_wrapped_input_lines(current_input: str, cursor_offset: int) -> list[str]:
    """Wrap input by display columns while retaining the real cursor offset."""

    t = theme()
    columns, _ = _cached_terminal_size()
    columns = safe_terminal_width(columns)
    cursor = max(0, min(cursor_offset, len(current_input)))
    first_prefix = f"{t.input}{BOLD}›{RESET} "
    continuation_prefix = "  "

    logical_lines = current_input.split("\n")
    segments: list[tuple[str, int, int, bool, str]] = []
    global_offset = 0

    for logical_index, logical_line in enumerate(logical_lines):
        prefix = first_prefix if logical_index == 0 else continuation_prefix
        available = max(1, columns - string_display_width(prefix))
        if not logical_line:
            segments.append(("", global_offset, global_offset, True, prefix))
        else:
            local_start = 0
            while local_start < len(logical_line):
                local_end = local_start
                used = 0
                while local_end < len(logical_line):
                    char_width = char_display_width(logical_line[local_end])
                    if local_end > local_start and used + char_width > available:
                        break
                    used += char_width
                    local_end += 1
                if local_end == local_start:
                    local_end += 1
                segments.append(
                    (
                        logical_line[local_start:local_end],
                        global_offset + local_start,
                        global_offset + local_end,
                        local_end == len(logical_line),
                        prefix,
                    )
                )
                local_start = local_end
        global_offset += len(logical_line) + 1

    cursor_index = 0
    for index, (_text, start, end, logical_end, _prefix) in enumerate(segments):
        if start <= cursor < end or (start == end == cursor):
            cursor_index = index
            break
        if cursor == end and logical_end:
            cursor_index = index

    max_visible_lines = 6
    if len(segments) > max_visible_lines:
        first_visible = max(0, min(cursor_index - 2, len(segments) - max_visible_lines))
        segments = segments[first_visible:first_visible + max_visible_lines]

    rendered: list[str] = []
    for text, start, end, logical_end, prefix in segments:
        if start <= cursor < end:
            local_cursor = cursor - start
        elif start == end == cursor or (cursor == end and logical_end):
            local_cursor = len(text)
        else:
            local_cursor = -1

        if not current_input:
            content = (
                f"{HIGHLIGHT_BG}{BRIGHT_GREEN} {RESET}"
                f"{t.subtle}在这里输入内容…{RESET}"
            )
        elif local_cursor >= 0:
            before = text[:local_cursor]
            current = text[local_cursor] if local_cursor < len(text) else " "
            after = text[local_cursor + 1:]
            content = f"{before}{HIGHLIGHT_BG}{BRIGHT_GREEN}{current}{RESET}{after}"
        else:
            content = text
        rendered.append(f"{prefix}{content}")
    return rendered or [f"{first_prefix}{HIGHLIGHT_BG}{BRIGHT_GREEN} {RESET}"]


def render_input_prompt(current_input: str, cursor_offset: int, compact: bool = False) -> str:
    """Render a bounded composer view without mutating the input value.

    The visible window is capped at six rows, but ``current_input`` and the
    caller's code-point cursor offset remain untouched.  Wide characters are
    measured by terminal display width rather than Python string length.
    """

    lines = _build_wrapped_input_lines(current_input, cursor_offset)
    input_view = "\n".join(lines)
    if compact:
        return input_view

    t = theme()
    hint = f"{t.subtle}Enter send · Ctrl+J newline · / commands{RESET}"
    return f"{input_view}\n{hint}"
