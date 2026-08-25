"""Pure ANSI frame-update calculations for the terminal renderer.

The module deliberately knows nothing about stdout, terminal state, or RepoTerm
business objects.  Content changes use a full refresh because a logical frame
row is not necessarily one physical terminal row after wrapping.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Literal


FrameUpdateMode = Literal["full", "incremental", "noop"]

# A large diff is more expensive and less reliable than redrawing the frame.
# Keep this internal and fixed so callers cannot accidentally create a second
# user-facing rendering policy.
FULL_REFRESH_THRESHOLD = 0.60

_CURSOR_HOME = "\x1b[H"
_CLEAR_SCREEN = "\x1b[2J"
_CLEAR_TO_END = "\x1b[J"
_CURSOR_ROW_START = "\x1b[{row};1H"
_CLEAR_LINE = "\x1b[2K"


@dataclass(frozen=True, slots=True)
class FrameUpdate:
    """The pure result of comparing one logical frame with its predecessor."""

    mode: FrameUpdateMode
    payload: str
    changed_rows: int
    total_rows: int


def _frame_rows(frame: str) -> list[str]:
    """Split a frame without discarding empty or trailing logical rows."""

    return frame.split("\n")


def _resolve_size(
    terminal_size: tuple[int, int] | None,
    columns: int | None,
    rows: int | None,
) -> tuple[int, int] | None:
    if terminal_size is not None:
        return (int(terminal_size[0]), int(terminal_size[1]))
    if columns is None or rows is None:
        return None
    return (int(columns), int(rows))


_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;:]*m")


def _full_payload(frame: str) -> str:
    """Build a full refresh that cannot leave characters from the old frame."""

    # Clearing only from the final cursor position leaves stale suffixes on
    # earlier, shorter rows.  Full fallback is allowed to clear the screen;
    # incremental updates remain responsible for avoiding this global erase.
    return f"{_CURSOR_HOME}{_CLEAR_SCREEN}{frame}{_CLEAR_TO_END}"


def _changed_row_indexes(previous: list[str], current: list[str]) -> list[int]:
    """Return the union of changed and removed row positions."""

    return [
        index
        for index in range(max(len(previous), len(current)))
        if (previous[index] if index < len(previous) else None)
        != (current[index] if index < len(current) else None)
    ]


def _incremental_payload(current: list[str], changed_indexes: list[int]) -> str:
    """Clear and rewrite only the changed logical rows."""

    parts: list[str] = []
    for index in changed_indexes:
        row = current[index] if index < len(current) else ""
        parts.append(_CURSOR_ROW_START.format(row=index + 1))
        parts.append(_CLEAR_LINE)
        parts.append(row)
    return "".join(parts)


def _is_emoji_base(character: str) -> bool:
    """Return whether a character normally occupies an emoji cell."""

    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x1FC00 <= codepoint <= 0x1FFFD
        or 0x2600 <= codepoint <= 0x27BF
    )


def _is_emoji_modifier(character: str) -> bool:
    """Return whether a character modifies the preceding emoji glyph."""

    return 0x1F3FB <= ord(character) <= 0x1F3FF


_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_TEXT_EMOJI_VARIATION_BASES = {"©", "®", "™"}


def _is_variation_capable(character: str) -> bool:
    """Return whether a variation selector can change this glyph's width."""

    return _is_emoji_base(character) or character in _TEXT_EMOJI_VARIATION_BASES


def _terminal_display_width(text: str) -> int:
    """Calculate conservative terminal cells for ANSI-coloured Unicode text.

    This is intentionally small and dependency-free.  It handles the cases
    that affect frame safety: ANSI SGR, East Asian wide characters, combining
    marks, variation selectors, emoji modifiers, regional-indicator flags and
    common zero-width-joiner emoji sequences.
    """

    visible = _ANSI_SGR_RE.sub("", text)
    width = 0
    index = 0
    while index < len(visible):
        character = visible[index]
        codepoint = ord(character)

        if (
            index + 1 < len(visible)
            and visible[index + 1] in _VARIATION_SELECTORS
            and _is_variation_capable(character)
        ):
            # Emoji presentation of text-default symbols such as ©️ is two
            # cells in the terminals supported by RepoTerm.
            width += 2
            index += 2
            continue

        if character == "\u200d":
            # The following emoji is part of the same grapheme cluster.
            index += 1
            if index < len(visible) and _is_emoji_base(visible[index]):
                index += 1
            continue
        if character in _VARIATION_SELECTORS or _is_emoji_modifier(character):
            index += 1
            continue
        if unicodedata.combining(character):
            index += 1
            continue

        # A regional-indicator pair is one flag glyph, not two emoji cells.
        if (
            0x1F1E6 <= codepoint <= 0x1F1FF
            and index + 1 < len(visible)
            and 0x1F1E6 <= ord(visible[index + 1]) <= 0x1F1FF
        ):
            width += 2
            index += 2
            continue

        # Keycap sequences are rendered as one wide emoji glyph by modern
        # terminals even though their base character is ASCII.
        if (
            character in "0123456789#*"
            and index + 2 < len(visible)
            and visible[index + 1] in {"\ufe0e", "\ufe0f"}
            and visible[index + 2] == "\u20e3"
        ):
            width += 2
            index += 3
            continue

        if _is_emoji_base(character):
            width += 2
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            width += 2
        else:
            width += 1
        index += 1
    return width


def _safe_line_width(line: str) -> int | None:
    """Return a line width, or ``None`` when terminal rewriting is uncertain."""

    visible = _ANSI_SGR_RE.sub("", line)
    if "\x1b" in visible:
        # Only SGR is understood.  Cursor movement, erasing and OSC payloads
        # can change terminal state outside the logical line being replaced.
        return None
    for index, character in enumerate(visible):
        category = unicodedata.category(character)
        if category == "Cc" or ord(character) == 0x7F:
            # Tabs, carriage returns and other controls do not have a stable
            # one-line cell width for this conservative diff algorithm.
            return None
        if character in _VARIATION_SELECTORS:
            if index == 0 or not _is_variation_capable(visible[index - 1]):
                # An unknown variation-selector sequence is not safe to
                # rewrite row-by-row without a grapheme-width implementation.
                return None
            if character == "\ufe0e":
                # Text presentation is terminal-dependent; use a full frame.
                return None
    return _terminal_display_width(line)


def _frame_has_unreset_sgr(frame: str) -> bool:
    """Return whether SGR state can flow from one logical row to the next."""

    style_active = False
    for line in _frame_rows(frame):
        for match in _ANSI_SGR_RE.finditer(line):
            parameters = match.group(0)[2:-1]
            for parameter in parameters.split(";") if parameters else ["0"]:
                if parameter in {"", "0"}:
                    style_active = False
                else:
                    style_active = True
        if style_active:
            return True
    return False


def _rows_are_safe_for_incremental(
    previous: list[str],
    current: list[str],
    *,
    columns: int,
    rows: int,
) -> bool:
    """Prove that every logical row fits without terminal auto-wrap."""

    if columns <= 0 or rows <= 0 or len(previous) > rows or len(current) > rows:
        return False
    if _frame_has_unreset_sgr("\n".join(previous)) or _frame_has_unreset_sgr(
        "\n".join(current)
    ):
        # A changed row may inherit style from a preceding row.  Rewriting it
        # alone would silently change the rendered appearance.
        return False
    # A row exactly as wide as the terminal may leave the cursor in a pending
    # wrap state.  Strictly less than the width is the safe proof boundary.
    return all(
        (width := _safe_line_width(line)) is not None and width < columns
        for line in [*previous, *current]
    )


def compute_frame_update(
    previous: str | None,
    current: str,
    *,
    terminal_size: tuple[int, int] | None = None,
    previous_terminal_size: tuple[int, int] | None = None,
    columns: int | None = None,
    rows: int | None = None,
    previous_columns: int | None = None,
    previous_rows: int | None = None,
) -> FrameUpdate:
    """Compare two frames and choose a full or noop update.

    ``terminal_size``/``previous_terminal_size`` are the preferred dimension
    arguments.  The explicit column/row spellings are accepted as a small,
    dependency-free convenience for callers that already hold scalar values.
    Incremental output requires both explicit, equal terminal dimensions; the
    screen layer supplies those dimensions whenever it has a cached
    predecessor.
    """

    current_frame_rows = _frame_rows(current)
    total_rows = len(current_frame_rows)
    if previous is None:
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)
    previous_frame_rows = _frame_rows(previous)

    current_size = _resolve_size(terminal_size, columns, rows)
    previous_size = _resolve_size(
        previous_terminal_size,
        previous_columns,
        previous_rows,
    )
    if current_size is not None and previous_size is not None and current_size != previous_size:
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)

    if previous == current:
        return FrameUpdate("noop", "", 0, total_rows)

    changed_indexes = _changed_row_indexes(previous_frame_rows, current_frame_rows)
    if len(previous_frame_rows) != len(current_frame_rows):
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)

    if current_size is None or previous_size is None:
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)

    columns, rows = current_size
    if not _rows_are_safe_for_incremental(
        previous_frame_rows,
        current_frame_rows,
        columns=columns,
        rows=rows,
    ):
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)

    changed_rows = len(changed_indexes)
    if changed_rows / total_rows >= FULL_REFRESH_THRESHOLD:
        return FrameUpdate("full", _full_payload(current), total_rows, total_rows)

    return FrameUpdate(
        "incremental",
        _incremental_payload(current_frame_rows, changed_indexes),
        changed_rows,
        total_rows,
    )


def diff_frames(
    previous: str | None,
    current: str,
    **kwargs: object,
) -> FrameUpdate:
    """Compatibility-named wrapper around :func:`compute_frame_update`."""

    return compute_frame_update(previous, current, **kwargs)  # type: ignore[arg-type]


__all__ = ["FULL_REFRESH_THRESHOLD", "FrameUpdate", "compute_frame_update", "diff_frames"]
