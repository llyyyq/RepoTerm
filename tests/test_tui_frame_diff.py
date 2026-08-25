from __future__ import annotations

import io

from repoterm.ui.tui.frame_diff import (
    FULL_REFRESH_THRESHOLD,
    FrameUpdate,
    compute_frame_update,
)


def test_first_frame_is_full_and_clears_previous_screen() -> None:
    update = compute_frame_update(None, "header\nbody", columns=80, rows=24)

    assert isinstance(update, FrameUpdate)
    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H")
    assert "\x1b[2J" in update.payload
    assert update.changed_rows == update.total_rows == 2


def test_identical_frame_is_noop_with_empty_payload() -> None:
    update = compute_frame_update(
        "header\nbody",
        "header\nbody",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update == FrameUpdate("noop", "", 0, 2)


def test_short_ascii_change_uses_safe_incremental_refresh() -> None:
    update = compute_frame_update(
        "header\nold\nfooter",
        "header\nnew\nfooter",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "incremental"
    assert update.changed_rows == 1
    assert update.total_rows == 3
    assert "\x1b[2;1H" in update.payload
    assert "\x1b[2K" in update.payload
    assert "new" in update.payload
    assert "\x1b[H" not in update.payload


def test_shorter_changed_line_is_cleared_before_incremental_rewrite() -> None:
    update = compute_frame_update(
        "header\nlong content\nfooter",
        "header\nshort\nfooter",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "incremental"
    assert "\x1b[2Kshort" in update.payload
    assert "long content" not in update.payload


def test_shorter_frame_uses_full_refresh() -> None:
    update = compute_frame_update("one\ntwo\nthree", "one\ntwo")

    assert update.mode == "full"
    assert update.changed_rows == update.total_rows == 2


def test_full_refresh_clears_stale_suffixes_from_previous_frame() -> None:
    update = compute_frame_update(
        "previous frame with a stale suffix\nold second row",
        "new\nnewer",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H\x1b[2J")


def test_resize_forces_full_refresh() -> None:
    update = compute_frame_update(
        "one\ntwo",
        "one\nchanged",
        terminal_size=(120, 40),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H")


def test_majority_difference_falls_back_to_full_refresh() -> None:
    previous = "\n".join(f"old-{index}" for index in range(10))
    current = "\n".join(f"new-{index}" for index in range(10))

    update = compute_frame_update(
        previous,
        current,
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "full"
    assert update.changed_rows == update.total_rows == 10
    assert FULL_REFRESH_THRESHOLD == 0.60


def test_unicode_ansi_and_empty_rows_use_incremental_refresh_when_safe() -> None:
    previous = "\x1b[31m中文\x1b[0m\n\nunchanged\nkeep\nkeep"
    current = "\x1b[32m中文\x1b[0m\n\nchanged\nkeep\nkeep"

    update = compute_frame_update(
        previous,
        current,
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "incremental"
    assert update.changed_rows == 2
    assert update.total_rows == 5
    assert "\x1b[1;1H" in update.payload
    assert "\x1b[3;1H" in update.payload
    assert "changed" in update.payload


def test_wrapping_boundary_change_never_uses_logical_row_diff() -> None:
    previous = "short\n" + ("x" * 80) + "\nfooter"
    current = "shorter\n" + ("x" * 80) + "\nfooter"

    update = compute_frame_update(previous, current, columns=80, rows=24)

    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H")
    assert "\x1b[2J" in update.payload


def test_emoji_and_combining_text_use_terminal_cell_width() -> None:
    update = compute_frame_update(
        "title\nCafe\u0301 🙂\nfooter",
        "title\nCafé 🙂\nfooter",
        terminal_size=(20, 24),
        previous_terminal_size=(20, 24),
    )

    assert update.mode == "incremental"
    assert update.changed_rows == 1
    assert "\x1b[2;1H" in update.payload


def test_text_emoji_variation_selector_width_forces_full_refresh() -> None:
    update = compute_frame_update(
        "a\n©️",
        "a\n®️",
        terminal_size=(2, 24),
        previous_terminal_size=(2, 24),
    )

    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H")


def test_cross_line_sgr_state_forces_full_refresh() -> None:
    previous = "\x1b[31mheader\nold\nfooter\x1b[0m"
    current = "\x1b[31mheader\nnew\nfooter\x1b[0m"

    update = compute_frame_update(
        previous,
        current,
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "full"
    assert update.payload.startswith("\x1b[H")
    assert "new" in update.payload


def test_exact_width_and_over_width_rows_force_full_refresh() -> None:
    exact = compute_frame_update(
        "title\n" + ("x" * 20) + "\nfooter",
        "title\n" + ("y" * 20) + "\nfooter",
        terminal_size=(20, 24),
        previous_terminal_size=(20, 24),
    )
    over = compute_frame_update(
        "title\n" + ("x" * 21) + "\nfooter",
        "title\n" + ("y" * 21) + "\nfooter",
        terminal_size=(20, 24),
        previous_terminal_size=(20, 24),
    )

    assert exact.mode == "full"
    assert over.mode == "full"


def test_unknown_cursor_control_forces_full_refresh() -> None:
    update = compute_frame_update(
        "title\nold\nfooter",
        "title\n\x1b[2Knew\nfooter",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )

    assert update.mode == "full"


def test_diff_engine_does_not_write_stdout(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)

    compute_frame_update("one", "two")

    assert stream.getvalue() == ""
