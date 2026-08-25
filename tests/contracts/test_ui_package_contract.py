"""阶段 E1B+C 的 UI/TUI 包结构和公共出口契约。"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import repoterm.ui as ui_package
import repoterm.ui.commands as commands
import repoterm.ui.history as history
import repoterm.ui.manage as manage
import repoterm.ui.shortcuts as shortcuts
import repoterm.ui.tui as tui
import repoterm.ui.tui.input_parser as input_parser
import repoterm.ui.tui.transcript as transcript
import repoterm.ui.tui.types as tui_types
import repoterm.ui.tty as tty
from repoterm.ui.tui.theme import ColorTheme


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TUI_ALL = (
    "clear_screen",
    "enter_alternate_screen",
    "exit_alternate_screen",
    "hide_cursor",
    "show_cursor",
    "get_permission_prompt_max_scroll_offset",
    "render_banner",
    "render_footer_bar",
    "render_panel",
    "render_permission_prompt",
    "render_slash_menu",
    "render_status_line",
    "render_tool_panel",
    "render_input_prompt",
    "KeyEvent",
    "ParsedInputEvent",
    "ParseResult",
    "TextEvent",
    "WheelEvent",
    "parse_input_chunk",
    "render_markdownish",
    "ColorTheme",
    "theme",
    "format_transcript_text",
    "get_transcript_max_scroll_offset",
    "get_transcript_window_size",
    "render_transcript",
    "TranscriptEntry",
)
EXPECTED_TUI_FILES = {
    "__init__.py",
    "chrome.py",
    "event_flow.py",
    "frame_diff.py",
    "input.py",
    "input_handler.py",
    "input_parser.py",
    "layout.py",
    "markdown.py",
    "navigation.py",
    "renderer.py",
    "runtime_control.py",
    "screen.py",
    "session_flow.py",
    "state.py",
    "theme.py",
    "tool_helpers.py",
    "tool_lifecycle.py",
    "transcript.py",
    "types.py",
    "ui_events.py",
    "ui_hints.py",
    "worker_lifecycle.py",
}


def test_ui_package_layout_and_no_legacy_shims() -> None:
    assert ui_package.__all__ == ()
    ui_init_source = (ROOT / "repoterm/ui/__init__.py").read_text(encoding="utf-8")
    assert "importlib" not in ui_init_source
    assert "sys.modules" not in ui_init_source
    assert "__getattr__" not in ui_init_source

    expected_root_files = {
        "commands.py",
        "tty.py",
        "history.py",
        "shortcuts.py",
        "manage.py",
    }
    assert expected_root_files <= {
        path.name for path in (ROOT / "repoterm/ui").glob("*.py")
    }
    assert {
        path.name for path in (ROOT / "repoterm/ui/tui").glob("*.py")
    } == EXPECTED_TUI_FILES
    assert not (ROOT / "repoterm/tui").exists()
    for old_file in (
        "cli_commands.py",
        "tty_app.py",
        "history.py",
        "local_tool_shortcuts.py",
        "manage_cli.py",
    ):
        assert not (ROOT / "repoterm" / old_file).exists()


def test_ui_public_exports_and_tui_all_are_stable() -> None:
    assert tuple(tui.__all__) == EXPECTED_TUI_ALL
    assert commands.SLASH_COMMANDS.__class__ is list
    assert commands.try_handle_local_command.__module__ == "repoterm.ui.commands"
    assert tty.run_tty_app.__module__ == "repoterm.ui.tty"
    assert history.load_history_entries.__module__ == "repoterm.ui.history"
    assert history.save_history_entries.__module__ == "repoterm.ui.history"
    assert shortcuts.parse_local_tool_shortcut.__module__ == "repoterm.ui.shortcuts"
    assert manage.maybe_handle_management_command.__module__ == "repoterm.ui.manage"

    for symbol in (
        input_parser.KeyEvent,
        input_parser.ParseResult,
        input_parser.TextEvent,
        input_parser.WheelEvent,
        input_parser.parse_input_chunk,
    ):
        assert symbol.__module__ == "repoterm.ui.tui.input_parser"
    assert input_parser.ParsedInputEvent is not None
    assert ColorTheme.__module__ == "repoterm.ui.tui.theme"
    assert transcript.render_transcript.__module__ == "repoterm.ui.tui.transcript"
    assert tui_types.TranscriptEntry.__module__ == "repoterm.ui.tui.types"
    assert tui.render_transcript is transcript.render_transcript
    assert tui.TranscriptEntry is tui_types.TranscriptEntry


def test_run_tty_app_signature_is_unchanged() -> None:
    signature = inspect.signature(tty.run_tty_app)
    parameters = list(signature.parameters.values())
    assert [parameter.name for parameter in parameters] == [
        "runtime",
        "tools",
        "model",
        "messages",
        "cwd",
        "permissions",
        "resume_session",
        "list_sessions_only",
        "list_workspace_sessions_only",
        "memory_manager",
        "context_manager",
        "prompt_bundle",
        "product_snapshot",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters
    )
    assert parameters[0].default is inspect.Parameter.empty
    assert parameters[1].default is inspect.Parameter.empty
    assert parameters[2].default is inspect.Parameter.empty
    assert parameters[3].default is inspect.Parameter.empty
    assert parameters[4].default is inspect.Parameter.empty
    assert parameters[5].default is inspect.Parameter.empty
    assert [parameter.default for parameter in parameters[6:]] == [
        None,
        False,
        False,
        None,
        None,
        None,
        None,
    ]
    return_annotation = str(signature.return_annotation).strip("'\"")
    assert return_annotation == "list[ChatMessage]"


def test_lower_layers_do_not_import_ui() -> None:
    lower_layers = (
        "runtime",
        "tools",
        "safety",
        "context",
        "providers",
        "memory",
        "session",
        "observability",
        "integrations",
    )
    for layer in lower_layers:
        for source_file in (ROOT / "repoterm" / layer).rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            assert "repoterm.ui" not in source, source_file.as_posix()


def _run_import_order(code: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_forward_import_order_does_not_load_legacy_modules() -> None:
    _run_import_order(
        """
import sys
import repoterm.ui.commands
import repoterm.ui.tui
import repoterm.ui.tty
import repoterm.app.interactive
assert 'repoterm.cli_commands' not in sys.modules
assert 'repoterm.tty_app' not in sys.modules
assert 'repoterm.tui' not in sys.modules
"""
    )


def test_reverse_import_order_does_not_load_legacy_modules() -> None:
    _run_import_order(
        """
import sys
import repoterm.app.interactive
import repoterm.ui.tty
import repoterm.ui.tui
import repoterm.ui.commands
assert 'repoterm.cli_commands' not in sys.modules
assert 'repoterm.tty_app' not in sys.modules
assert 'repoterm.tui' not in sys.modules
"""
    )
