from __future__ import annotations

from types import SimpleNamespace

import pytest

from repoterm.ui.tui import chrome
from repoterm.ui.tui import input as input_module
from repoterm.ui.tui import layout as layout_module
from repoterm.ui.tui import renderer as renderer_module
from repoterm.ui.tui import transcript as transcript_module
from repoterm.ui.tui.chrome import (
    render_banner,
    render_footer_bar,
    render_permission_prompt,
    render_session_feed,
    string_display_width,
    strip_ansi,
)
from repoterm.ui.tui.input import render_input_prompt
from repoterm.ui.tui.renderer import _decorate_session_feed_body, _render_prompt_panel
from repoterm.ui.tui.state import PendingApproval, ScreenState
from repoterm.ui.tui.types import TranscriptEntry


def _set_terminal_size(monkeypatch: pytest.MonkeyPatch, columns: int, rows: int) -> None:
    size = lambda: (columns, rows)
    monkeypatch.setattr(chrome, "_cached_terminal_size", size)
    monkeypatch.setattr(input_module, "_cached_terminal_size", size)
    monkeypatch.setattr(layout_module, "_cached_terminal_size", size)
    monkeypatch.setattr(transcript_module, "_cached_terminal_size", size)
    monkeypatch.setattr(renderer_module, "_cached_terminal_size", size)


def _assert_width_bounded(rendered: str, columns: int) -> None:
    for line in rendered.splitlines():
        assert string_display_width(line) < columns


@pytest.mark.parametrize("columns, rows", [(50, 16), (80, 24), (120, 40), (160, 50)])
def test_header_is_at_most_two_lines_and_width_bounded(
    monkeypatch: pytest.MonkeyPatch,
    columns: int,
    rows: int,
) -> None:
    _set_terminal_size(monkeypatch, columns, rows)

    rendered = render_banner(
        {"model": "deepseek-v4-pro", "baseUrl": "https://api.example.test/v1"},
        r"D:\workspace\calculator_bug",
        ["cwd: secret-is-not-rendered"],
        {"transcriptCount": 4, "messageCount": 3, "skillCount": 2, "mcpCount": 1},
    )

    assert len(rendered.splitlines()) <= 2
    _assert_width_bounded(rendered, columns)
    assert "deepseek-v4-pro" in rendered
    assert "api.example.test" in rendered or columns < 60
    assert "secret-is-not-rendered" not in rendered
    assert "auth_token" not in rendered


@pytest.mark.parametrize("text", ["", "single line", "第一行\n第二行\n第三行"])
def test_composer_preserves_input_and_stays_width_bounded(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    _set_terminal_size(monkeypatch, 50, 16)
    cursor = len(text)

    rendered = render_input_prompt(text, cursor, compact=True)

    assert len(rendered.splitlines()) <= 6
    _assert_width_bounded(rendered, 50)
    if text:
        plain = strip_ansi(rendered)
        assert all(line in plain for line in text.splitlines())


def test_prompt_has_thin_rules_and_slash_menu_has_separate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_terminal_size(monkeypatch, 80, 24)
    state = ScreenState(input="/", cursor_offset=1)

    rendered = _render_prompt_panel(state)
    plain = strip_ansi(rendered)

    assert "commands" in plain
    assert "┌" not in plain
    assert "┐" not in plain
    assert len(rendered.splitlines()) <= 24
    _assert_width_bounded(rendered, 80)


def test_session_feed_has_no_full_rectangle_and_keeps_transcript_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_terminal_size(monkeypatch, 80, 24)
    rendered = render_session_feed(
        "you\n第一行中文\nassistant\npytest passed",
        right_title="4 events",
        max_body_lines=10,
    )
    plain = strip_ansi(rendered)

    assert "session" in plain
    assert "第一行中文" in plain
    assert "┌" not in plain
    assert "┐" not in plain
    assert "└" not in plain
    _assert_width_bounded(rendered, 80)


def test_footer_is_one_line_and_degrades_at_narrow_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_terminal_size(monkeypatch, 50, 16)

    rendered = render_footer_bar(
        "Running tool with a long status",
        tools_enabled=True,
        skills_enabled=True,
        background_tasks=[{"status": "running", "label": "background"}],
    )

    assert len(rendered.splitlines()) == 1
    _assert_width_bounded(rendered, 50)
    assert "tools" in strip_ansi(rendered)


def test_summary_is_visible_and_can_be_budgeted_before_transcript() -> None:
    entries = [TranscriptEntry(id=1, kind="assistant", body="answer")]
    session = SimpleNamespace(
        checkpoints=[],
        metadata=SimpleNamespace(
            readiness_summary="ready",
            instruction_summary="instructions",
            hook_summary="hooks",
            delegation_summary="delegation",
            extension_summary="extensions",
        ),
    )

    rendered = _decorate_session_feed_body("answer", entries, session)

    assert "readiness-summary: ready" in rendered
    assert "instruction-summary: instructions" in rendered
    assert "answer" in rendered


def test_pending_approval_and_diff_remain_high_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_terminal_size(monkeypatch, 80, 24)
    rendered = render_permission_prompt(
        {
            "kind": "edit",
            "summary": "Need approval",
            "details": ["--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-old\n+new"],
            "choices": [{"key": "1", "label": "allow once"}],
        },
        expanded=True,
    )
    plain = strip_ansi(rendered)

    assert "Need approval" in plain
    assert "calculator.py" in plain
    assert "allow once" in plain
    assert any(border in plain for border in ("┌", "╭"))
    _assert_width_bounded(rendered, 80)


def test_collapsed_tool_is_compact_but_keeps_summary() -> None:
    from repoterm.ui.tui.transcript import _render_transcript_entry

    rendered = _render_transcript_entry(
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output is retained",
            toolName="read_file",
            status="success",
            collapsed=True,
            collapsedSummary="calculator.py",
            collapsePhase=3,
        )
    )

    assert "read_file" in strip_ansi(rendered)
    assert "calculator.py" in strip_ansi(rendered)
    assert len(rendered.splitlines()) == 1
