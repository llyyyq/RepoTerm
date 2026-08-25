from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from repoterm.ui.tui import screen
from repoterm.ui.tui import runtime_control
from repoterm.ui.tui.input_parser import KeyEvent, WheelEvent, parse_input_chunk
from repoterm.ui.tui.state import ScreenState, TtyAppArgs


def _force_ansi_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")


def test_enter_alternate_screen_does_not_leave_sync_output_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)

    screen.enter_alternate_screen()
    output = capsys.readouterr().out

    assert screen.DISABLE_SYNC_OUTPUT in output
    assert screen.ENABLE_SYNC_OUTPUT not in output
    assert screen.ENABLE_MOUSE_TRACKING in output
    assert "\u001b[?1000h" in output
    assert "\u001b[?1006h" in output
    assert "\u001b[?1002h" not in output
    assert "\u001b[?1003h" not in output
    assert screen.ENABLE_BRACKETED_PASTE in output
    assert screen.ENABLE_FOCUS_TRACKING in output


def test_exit_alternate_screen_disables_every_mode_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)

    screen.exit_alternate_screen()
    screen.exit_alternate_screen()
    output = capsys.readouterr().out

    assert output.count(screen.DISABLE_SYNC_OUTPUT) == 2
    assert output.count(screen.DISABLE_MOUSE_TRACKING) == 2
    assert output.count(screen.DISABLE_BRACKETED_PASTE) == 2
    assert output.count(screen.DISABLE_FOCUS_TRACKING) == 2
    assert output.count(screen.EXIT_ALT_SCREEN) == 2
    assert screen.ENABLE_SYNC_OUTPUT not in output
    for sequence in ("\u001b[?1006l", "\u001b[?1003l", "\u001b[?1002l", "\u001b[?1000l"):
        assert output.count(sequence) == 2


@pytest.mark.parametrize(
    ("sequence", "direction"),
    [
        ("\u001b[<64;10;5M", "up"),
        ("\u001b[<65;10;5M", "down"),
    ],
)
def test_sgr_wheel_events_remain_available(sequence: str, direction: str) -> None:
    result = parse_input_chunk(sequence, incoming_chunk=sequence)

    assert len(result.events) == 1
    assert isinstance(result.events[0], WheelEvent)
    assert result.events[0].direction == direction


def test_write_frame_wraps_one_complete_frame_in_sync_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)

    screen.write_frame("FRAME")
    output = capsys.readouterr().out

    assert output == (
        screen.ENABLE_SYNC_OUTPUT
        + "\u001b[H\u001b[2JFRAME\u001b[J"
        + screen.DISABLE_SYNC_OUTPUT
    )


def test_identical_frame_is_not_written_twice(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)
    monkeypatch.setattr(screen, "_cached_terminal_size", lambda: (80, 24))
    screen.invalidate_frame_cache()

    screen.write_frame("header\ncomposer")
    first = capsys.readouterr().out
    screen.write_frame("header\ncomposer")
    second = capsys.readouterr().out

    assert first
    assert second == ""


def test_safe_content_change_uses_incremental_update_and_one_sync_pair(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)
    monkeypatch.setattr(screen, "_cached_terminal_size", lambda: (80, 24))
    screen.invalidate_frame_cache()

    screen.write_frame("header\ncomposer a\nfooter")
    full = capsys.readouterr().out
    screen.write_frame("header\ncomposer b\nfooter")
    changed = capsys.readouterr().out

    assert "\u001b[2;1H" in changed
    assert "\u001b[2K" in changed
    assert "\u001b[H" not in changed
    assert "\u001b[2J" not in changed
    assert changed.count(screen.ENABLE_SYNC_OUTPUT) == 1
    assert changed.count(screen.DISABLE_SYNC_OUTPUT) == 1
    assert len(changed) < len(full)


def test_resize_forces_full_frame_and_cache_invalidation_forces_full(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_ansi_terminal(monkeypatch)
    size = [(80, 24)]
    monkeypatch.setattr(screen, "_cached_terminal_size", lambda: size[0])
    screen.invalidate_frame_cache()

    screen.write_frame("one\ntwo")
    capsys.readouterr()
    size[0] = (100, 30)
    screen.write_frame("one\nchanged")
    resized = capsys.readouterr().out
    screen.invalidate_frame_cache()
    screen.write_frame("one\nchanged")
    invalidated = capsys.readouterr().out

    assert "\u001b[H" in resized and "\u001b[J" in resized
    assert "\u001b[H" in invalidated and "\u001b[J" in invalidated


def test_runtime_cleanup_still_exits_screen_when_cursor_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited: list[str] = []

    def fail_show_cursor() -> None:
        raise RuntimeError("cursor restore failed")

    monkeypatch.setattr(runtime_control, "show_cursor", fail_show_cursor)
    monkeypatch.setattr(runtime_control, "exit_alternate_screen", lambda: exited.append("screen"))

    with pytest.raises(RuntimeError, match="cursor restore failed"):
        runtime_control.exit_tty_runtime(None)

    assert exited == ["screen"]


def test_tty_render_exception_runs_terminal_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    import repoterm.ui.tty as tty

    calls: list[str] = []
    app_args = TtyAppArgs(None, None, None, [], ".", None)
    state = ScreenState()

    monkeypatch.setattr(tty, "handle_session_listing", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tty, "load_or_create_session", lambda *_args: object())
    monkeypatch.setattr(tty, "build_tty_runtime_state", lambda *_args, **_kwargs: (app_args, state))
    monkeypatch.setattr(
        tty,
        "install_permission_prompt",
        lambda *_args, **_kwargs: (threading.Event(), {}, None),
    )
    monkeypatch.setattr(tty, "enter_tty_runtime", lambda: calls.append("enter"))
    monkeypatch.setattr(tty, "install_sigwinch_rerender", lambda *_args: None)

    def fail_render(*_args, **_kwargs) -> None:
        raise RuntimeError("render failed")

    monkeypatch.setattr(tty, "_render_screen", fail_render)
    monkeypatch.setattr(tty, "exit_tty_runtime", lambda *_args: calls.append("exit"))
    monkeypatch.setattr(tty, "finalize_tty_session", lambda *_args: calls.append("finalize"))

    with pytest.raises(RuntimeError, match="render failed"):
        tty.run_tty_app(runtime=None, tools=None, model=None, messages=[], cwd=".", permissions=None)

    assert calls == ["enter", "exit", "finalize"]


def _patch_tty_loop_bootstrap(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Install a deterministic TTY loop harness without entering raw mode."""

    import repoterm.ui.tty as tty

    app_args = TtyAppArgs(None, None, None, [], ".", None)
    state = ScreenState()

    monkeypatch.setattr(tty, "handle_session_listing", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tty, "load_or_create_session", lambda *_args: object())
    monkeypatch.setattr(tty, "build_tty_runtime_state", lambda *_args, **_kwargs: (app_args, state))
    monkeypatch.setattr(
        tty,
        "install_permission_prompt",
        lambda *_args, **_kwargs: (threading.Event(), {}, None),
    )
    monkeypatch.setattr(tty, "enter_tty_runtime", lambda: calls.append("enter"))
    monkeypatch.setattr(tty, "install_sigwinch_rerender", lambda *_args: None)
    monkeypatch.setattr(tty, "_render_screen", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tty, "exit_tty_runtime", lambda *_args: calls.append("exit"))
    monkeypatch.setattr(tty, "finalize_tty_session", lambda *_args: calls.append("finalize"))
    monkeypatch.setattr(tty, "_RawModeContext", _FakeRawModeContext)

    # Use the Windows input branch so the test can provide input without a real
    # stdin file descriptor or a platform-specific select implementation.
    monkeypatch.setattr(tty.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(kbhit=lambda: True))


class _FakeRawModeContext:
    def __enter__(self) -> "_FakeRawModeContext":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_tty_input_parser_exception_runs_terminal_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    import repoterm.ui.tty as tty

    calls: list[str] = []
    _patch_tty_loop_bootstrap(monkeypatch, calls)
    monkeypatch.setattr(tty, "_win_read_one_key", iter(["input", ""]).__next__)

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("parser failed")

    monkeypatch.setattr(tty, "parse_input_chunk", fail_parse)

    with pytest.raises(RuntimeError, match="parser failed"):
        tty.run_tty_app(runtime=None, tools=None, model=None, messages=[], cwd=".", permissions=None)

    assert calls == ["enter", "exit", "finalize"]


def test_tty_ctrl_c_stops_loop_before_following_events(monkeypatch: pytest.MonkeyPatch) -> None:
    import repoterm.ui.tty as tty

    calls: list[str] = []
    handled_events: list[KeyEvent] = []
    read_chunks = iter(["input", ""])
    _patch_tty_loop_bootstrap(monkeypatch, calls)
    monkeypatch.setattr(tty, "_win_read_one_key", read_chunks.__next__)
    monkeypatch.setattr(
        tty,
        "parse_input_chunk",
        lambda *_args, **_kwargs: SimpleNamespace(
            events=[
                KeyEvent(name="c", ctrl=True, meta=False),
                KeyEvent(name="return", ctrl=False, meta=False),
            ],
            rest="",
        ),
    )
    monkeypatch.setattr(tty, "_handle_tty_event", lambda *_args: handled_events.append(_args[2]))

    tty.run_tty_app(runtime=None, tools=None, model=None, messages=[], cwd=".", permissions=None)

    assert calls == ["enter", "exit", "finalize"]
    assert [event.name for event in handled_events] == ["c"]
