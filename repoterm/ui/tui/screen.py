from __future__ import annotations

import os
import sys

from .chrome import _cached_terminal_size
from .frame_diff import compute_frame_update

ENTER_ALT_SCREEN = "\u001b[?1049h"
EXIT_ALT_SCREEN = "\u001b[?1049l"
# Home first, then erase from the cursor to the end.  Frame updates use the
# same non-global clear semantics; the alternate-screen entry is the only
# place where this initial reset is emitted.
ERASE_SCREEN_AND_HOME = "\u001b[H\u001b[J"
# Mouse tracking sequence breakdown:
#   ?1000h  — basic X10 mouse reporting (button press/release)
#   ?1002h  — button-event tracking (only reports while button pressed, can interfere)
#   ?1003h  — any-event tracking (reports all mouse events including scroll without button)
#   ?1006h  — SGR extended encoding (supports coordinates > 223, required for modern terminals)
# Strategy: use ?1000h (basic) + ?1006h (SGR format).  Wheel events remain
# available through SGR encoding without enabling continuous any-event motion.
ENABLE_MOUSE_TRACKING = "\u001b[?1000h\u001b[?1006h"
DISABLE_MOUSE_TRACKING = "\u001b[?1006l\u001b[?1003l\u001b[?1002l\u001b[?1000l"

ENABLE_BRACKETED_PASTE  = "[?2004h"
DISABLE_BRACKETED_PASTE = "[?2004l"
ENABLE_FOCUS_TRACKING  = "[?1004h"
ENABLE_SYNC_OUTPUT  = "[?2026h"
DISABLE_SYNC_OUTPUT = "[?2026l"
DISABLE_FOCUS_TRACKING = "[?1004l"
# Terminal types that do not support alternate screen or mouse tracking.
# NOTE: the empty string is intentionally NOT included. On Windows the TERM
# environment variable is unset by default, so treating "" as dumb would skip
# the alternate screen buffer and cause every redraw frame to accumulate in the
# terminal scrollback (garbled "stacked frame" output when scrolling up —
# GitHub issue #7). Pipes / non-interactive output are handled separately via
# the isatty() guard in _is_dumb_terminal().
_DUMB_TERMS = frozenset({"dumb", "linux"})

# The last logical frame is kept here, at the terminal boundary.  Renderer
# code remains responsible only for producing a complete logical frame.
_last_frame: str | None = None
_last_frame_size: tuple[int, int] | None = None
_last_frame_was_dumb: bool | None = None


# ---------------------------------------------------------------------------
# Windows VT processing
# ---------------------------------------------------------------------------

_vt_enabled = False


def _enable_windows_vt_processing() -> None:
    """Enable ANSI / VT escape sequence processing on Windows 10+.

    Without this call the console ignores escape codes for colours,
    alternate-screen, cursor visibility, mouse tracking, etc.
    The function is a no-op on non-Windows platforms or when the
    underlying API call is unavailable.
    """
    global _vt_enabled
    if _vt_enabled:
        return

    if sys.platform != "win32":
        _vt_enabled = True
        return

    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        STD_OUTPUT_HANDLE = -11
        STD_ERROR_HANDLE = -12
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001

        for handle_id in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            handle = kernel32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
                kernel32.SetConsoleMode(handle, new_mode)

        # Also enable VT input processing so the console sends ANSI
        # escape sequences for special keys instead of Windows-native
        # key events (useful for ConPTY / Windows Terminal).
        STD_INPUT_HANDLE = -10
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode_in = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_in, ctypes.byref(mode_in)):
            kernel32.SetConsoleMode(h_in, mode_in.value | ENABLE_VIRTUAL_TERMINAL_INPUT)

        _vt_enabled = True
    except Exception:
        # If ctypes is unavailable or the call fails (e.g. old Windows),
        # fall through silently — ANSI codes will simply not render.
        _vt_enabled = True


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def hide_cursor() -> None:
    _enable_windows_vt_processing()
    sys.stdout.write("\u001b[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\u001b[?25h")
    sys.stdout.flush()


def invalidate_frame_cache() -> None:
    """Forget the physical frame currently believed to be on screen."""

    global _last_frame, _last_frame_size, _last_frame_was_dumb
    _last_frame = None
    _last_frame_size = None
    _last_frame_was_dumb = None


def _is_dumb_terminal() -> bool:
    """Return True if the terminal likely doesn't support escape sequences.

    Windows has no ``TERM`` variable by default, but modern Windows consoles
    support the alternate-screen buffer and mouse tracking once VT processing is
    enabled (done in :func:`_enable_windows_vt_processing`). We therefore guard
    against non-interactive (piped) output with ``isatty()`` rather than treating
    an empty ``TERM`` as dumb — otherwise Windows would skip the alternate screen
    and redraw frames would pile up in the scrollback (issue #7).
    """
    if not sys.stdout.isatty():
        return True
    return os.environ.get("TERM", "") in _DUMB_TERMS


def enter_alternate_screen() -> None:
    _enable_windows_vt_processing()
    invalidate_frame_cache()
    if _is_dumb_terminal():
        # Dumb terminals (e.g. 'linux' console, 'dumb', piped output)
        # don't support alternate screen or mouse tracking.
        return
    sys.stdout.write(
        DISABLE_SYNC_OUTPUT
        + DISABLE_MOUSE_TRACKING
        + ENTER_ALT_SCREEN
        + ERASE_SCREEN_AND_HOME
        + ENABLE_MOUSE_TRACKING
        + ENABLE_BRACKETED_PASTE
        + ENABLE_FOCUS_TRACKING
    )
    sys.stdout.flush()


def exit_alternate_screen() -> None:
    invalidate_frame_cache()
    if _is_dumb_terminal():
        return
    sys.stdout.write(
        DISABLE_SYNC_OUTPUT
        + DISABLE_MOUSE_TRACKING
        + DISABLE_BRACKETED_PASTE
        + DISABLE_FOCUS_TRACKING
        + EXIT_ALT_SCREEN
    )
    sys.stdout.flush()


def write_frame(frame: str) -> None:
    """Write a complete or incremental frame in one terminal transaction."""

    global _last_frame, _last_frame_size, _last_frame_was_dumb

    current_size = _cached_terminal_size()
    is_dumb = _is_dumb_terminal()
    same_physical_context = (
        _last_frame is not None
        and _last_frame_size == current_size
        and _last_frame_was_dumb == is_dumb
    )

    if is_dumb:
        if same_physical_context and _last_frame == frame:
            return
        payload = frame
    else:
        update = compute_frame_update(
            _last_frame if same_physical_context else None,
            frame,
            terminal_size=current_size,
            previous_terminal_size=_last_frame_size if same_physical_context else None,
        )
        if update.mode == "noop":
            return
        payload = ENABLE_SYNC_OUTPUT + update.payload + DISABLE_SYNC_OUTPUT

    # Exactly one write and one flush per update.  The cache is committed only
    # after both operations succeed so a failed write can be retried safely.
    sys.stdout.write(payload)
    sys.stdout.flush()
    _last_frame = frame
    _last_frame_size = current_size
    _last_frame_was_dumb = is_dumb


def clear_screen() -> None:
    invalidate_frame_cache()
    sys.stdout.write("\u001b[H\u001b[J")
    sys.stdout.flush()
