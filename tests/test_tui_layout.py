from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from repoterm.safety.permissions import PermissionManager
from repoterm.tools.registry import ToolRegistry
from repoterm.ui.tui import chrome, input as input_module, layout as layout_module
from repoterm.ui.tui import renderer as renderer_module
from repoterm.ui.tui import transcript as transcript_module
from repoterm.ui.tui.layout import (
    compute_layout_metrics,
    measure_rendered_lines,
)
from repoterm.ui.tui.state import PendingApproval, ScreenState, TtyAppArgs
from repoterm.ui.tui.types import TranscriptEntry


TERMINAL_SIZES = [(50, 16), (80, 24), (120, 40), (160, 50)]
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@pytest.mark.parametrize("columns, rows", TERMINAL_SIZES)
def test_layout_never_allocates_more_lines_than_terminal(columns: int, rows: int) -> None:
    metrics = compute_layout_metrics(
        columns,
        rows,
        header_lines=2,
        prompt_lines=3,
        footer_lines=1,
        contextual_help_lines=1,
        gap_lines=3,
        transcript_frame_lines=2,
    )

    assert metrics.total_lines <= rows
    assert metrics.terminal_columns == columns
    assert metrics.terminal_rows == rows
    assert metrics.transcript_body_lines >= 4 or metrics.transcript_body_lines == 0


@pytest.mark.parametrize("text", ["", "hello", "第一行\n第二行\n第三行", "calculator_bug.py 🧮"])
def test_measure_rendered_lines_handles_empty_multiline_and_wide_text(text: str) -> None:
    lines = measure_rendered_lines(text, width=20)

    expected_minimum = 0 if text == "" else text.count("\n") + 1
    assert lines >= expected_minimum
    assert measure_rendered_lines(text, width=20) == lines


def test_multiline_prompt_and_slash_menu_reduce_transcript_budget() -> None:
    base = compute_layout_metrics(
        80,
        24,
        header_lines=1,
        prompt_lines=2,
        footer_lines=1,
        gap_lines=3,
        transcript_frame_lines=2,
    )
    expanded = compute_layout_metrics(
        80,
        24,
        header_lines=1,
        prompt_lines=4,
        footer_lines=1,
        slash_menu_lines=5,
        gap_lines=3,
        transcript_frame_lines=2,
    )

    assert expanded.transcript_body_lines < base.transcript_body_lines
    assert expanded.slash_menu_lines == 5
    assert expanded.total_lines <= 24


def test_session_summary_is_reserved_before_transcript() -> None:
    without_summary = compute_layout_metrics(
        120,
        40,
        header_lines=1,
        prompt_lines=2,
        footer_lines=1,
        gap_lines=3,
        transcript_frame_lines=2,
    )
    with_summary = compute_layout_metrics(
        120,
        40,
        header_lines=1,
        prompt_lines=2,
        footer_lines=1,
        gap_lines=3,
        transcript_summary_lines=3,
        transcript_frame_lines=2,
    )

    assert with_summary.transcript_summary_lines == 3
    assert with_summary.transcript_body_lines == without_summary.transcript_body_lines - 3
    assert with_summary.feed_body_lines <= without_summary.feed_body_lines
    assert with_summary.total_lines <= 40


def test_pending_approval_uses_its_own_budget_without_overflow() -> None:
    metrics = compute_layout_metrics(
        50,
        16,
        header_lines=1,
        footer_lines=1,
        approval_lines=8,
        gap_lines=2,
    )

    assert metrics.total_lines <= 16
    assert metrics.approval_lines == 8
    assert metrics.transcript_body_lines >= 4 or metrics.transcript_body_lines == 0


def test_tiny_terminal_does_not_fake_a_twenty_four_row_terminal() -> None:
    metrics = compute_layout_metrics(
        50,
        16,
        header_lines=1,
        prompt_lines=3,
        footer_lines=1,
        gap_lines=3,
        transcript_frame_lines=2,
    )

    assert metrics.terminal_rows == 16
    assert metrics.total_lines <= 16
    assert metrics.terminal_rows != 24


def _patch_render_terminal_size(monkeypatch: pytest.MonkeyPatch, columns: int, rows: int) -> None:
    size = lambda: (columns, rows)
    monkeypatch.setattr(chrome, "_cached_terminal_size", size)
    monkeypatch.setattr(input_module, "_cached_terminal_size", size)
    monkeypatch.setattr(layout_module, "_cached_terminal_size", size)
    monkeypatch.setattr(transcript_module, "_cached_terminal_size", size)
    monkeypatch.setattr(renderer_module, "_cached_terminal_size", size)


def _render_frame_for_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    columns: int,
    rows: int,
    state: ScreenState,
) -> tuple[str, object]:
    _patch_render_terminal_size(monkeypatch, columns, rows)
    args = TtyAppArgs(
        runtime={"model": "deepseek-v4-pro", "baseUrl": "https://api.example.test/v1"},
        tools=ToolRegistry([]),
        model=object(),
        messages=[],
        cwd=str(tmp_path / "calculator_bug"),
        permissions=PermissionManager(str(tmp_path)),
    )
    captured: list[str] = []
    monkeypatch.setattr(renderer_module, "write_frame", captured.append)
    monkeypatch.setattr(renderer_module, "list_background_tasks", lambda: [])
    renderer_module._last_render_hash = -1
    renderer_module._last_render_time = 0.0

    renderer_module._render_screen(args, state)

    assert captured
    return captured[0], state.layout_metrics


@pytest.mark.parametrize("columns, rows", TERMINAL_SIZES)
@pytest.mark.parametrize(
    "state_factory",
    [
        lambda: ScreenState(),
        lambda: ScreenState(input="single line", cursor_offset=11),
        lambda: ScreenState(input="第一行中文\n第二行中文", cursor_offset=9),
        lambda: ScreenState(input="/", cursor_offset=1),
    ],
)
def test_rendered_frame_fits_all_required_terminal_sizes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    columns: int,
    rows: int,
    state_factory,
) -> None:
    frame, metrics = _render_frame_for_state(monkeypatch, tmp_path, columns, rows, state_factory())
    visible = _ANSI_RE.sub("", frame)

    assert len(visible.splitlines()) <= rows
    assert metrics.total_lines <= rows
    for line in visible.splitlines():
        assert chrome.string_display_width(line) <= columns


@pytest.mark.parametrize("columns, rows", TERMINAL_SIZES)
@pytest.mark.parametrize(
    "state_factory",
    [
        lambda: ScreenState(),
        lambda: ScreenState(
            transcript=[TranscriptEntry(id=1, kind="assistant", body="short answer")]
        ),
        lambda: ScreenState(
            transcript=[
                TranscriptEntry(id=1, kind="user", body="question"),
                TranscriptEntry(id=2, kind="assistant", body="answer"),
                TranscriptEntry(id=3, kind="tool", body="done", toolName="read_file"),
            ]
        ),
        lambda: ScreenState(
            transcript=[TranscriptEntry(id=1, kind="assistant", body="answer")],
            session=SimpleNamespace(
                checkpoints=[],
                metadata=SimpleNamespace(
                    readiness_summary="ready",
                    instruction_summary="instructions",
                    hook_summary="hooks",
                    delegation_summary="delegation",
                    extension_summary="extensions",
                ),
            ),
        ),
    ],
)
def test_normal_frame_uses_exact_terminal_height(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    columns: int,
    rows: int,
    state_factory,
) -> None:
    frame, metrics = _render_frame_for_state(monkeypatch, tmp_path, columns, rows, state_factory())
    visible = _ANSI_RE.sub("", frame)

    assert len(visible.splitlines()) == metrics.total_lines
    assert metrics.total_lines == rows


def test_frame_budget_includes_session_summary(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    state = ScreenState(
        transcript=[TranscriptEntry(id=1, kind="assistant", body="answer")],
        session=SimpleNamespace(
            checkpoints=[],
            metadata=SimpleNamespace(
                readiness_summary="ready",
                instruction_summary="instructions",
                hook_summary="hooks",
                delegation_summary="delegation",
                extension_summary="extensions",
            ),
        ),
    )

    frame, metrics = _render_frame_for_state(monkeypatch, tmp_path, 80, 24, state)
    visible = _ANSI_RE.sub("", frame)

    assert "readiness-summary: ready" in visible
    assert metrics.transcript_summary_lines > 0
    assert len(visible.splitlines()) == metrics.total_lines == 24


def test_pending_approval_frame_fits_small_terminal_and_keeps_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    choices = [
        {"key": str(index), "label": f"approval choice {index}"}
        for index in range(1, 8)
    ]
    state = ScreenState(
        pending_approval=PendingApproval(
            request={
                "kind": "edit",
                "summary": "Need approval",
                "details": [f"diff line {index}" for index in range(100)],
                "choices": choices,
            },
            resolve=lambda _result: None,
            details_expanded=True,
        )
    )

    frame, metrics = _render_frame_for_state(monkeypatch, tmp_path, 50, 16, state)
    visible = _ANSI_RE.sub("", frame)

    assert "Need approval" in visible
    for choice in choices:
        assert choice["label"] in visible
    assert "Diff space insufficient" in visible or "diff line" in visible or "scroll" in visible
    assert metrics.total_lines <= 16
    assert len(visible.splitlines()) <= 16
