"""Contracts for the Agent Loop package boundary introduced by D4."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import subprocess
import sys

import repoterm.runtime as runtime_package
import repoterm.runtime.loop as loop
from repoterm.runtime.loop import (
    STABLE_TASK_STATE_MARKER,
    _record_implicit_preference_signal,
    run_agent_turn,
)


def test_runtime_loop_has_one_explicit_path_and_lightweight_runtime_init() -> None:
    """The Agent Loop has only its new path and the package init stays inert."""

    repo_root = Path(__file__).resolve().parents[2]
    new_path = repo_root / "repoterm/runtime/loop.py"
    old_path = repo_root / "repoterm/agent_loop.py"
    assert new_path.exists()
    assert not old_path.exists()
    assert loop.__file__ is not None
    assert Path(loop.__file__).resolve() == new_path.resolve()
    assert run_agent_turn.__module__ == loop.__name__
    assert _record_implicit_preference_signal.__module__ == loop.__name__
    assert isinstance(STABLE_TASK_STATE_MARKER, str)

    init_source = Path(runtime_package.__file__).read_text(encoding="utf-8")
    assert runtime_package.__all__ == ()
    assert "import *" not in init_source
    assert "__getattr__" not in init_source
    assert "importlib" not in init_source
    assert "sys.modules" not in init_source


def test_run_agent_turn_signature_is_unchanged() -> None:
    """The moved public entry point keeps its exact keyword-only contract."""

    signature = inspect.signature(run_agent_turn)
    expected_names = (
        "model",
        "tools",
        "messages",
        "cwd",
        "permissions",
        "session",
        "store",
        "max_steps",
        "on_tool_start",
        "on_tool_result",
        "on_assistant_message",
        "on_progress_message",
        "on_runtime_event",
        "on_assistant_stream_chunk",
        "on_thinking_chunk",
        "context_manager",
        "memory_manager",
        "runtime",
        "metrics_collector",
        "system_prompt",
        "project_context",
        "enable_work_chain",
    )
    assert tuple(signature.parameters) == expected_names
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    required = {"model", "tools", "messages", "cwd"}
    for name, parameter in signature.parameters.items():
        if name in required:
            assert parameter.default is inspect.Parameter.empty
    assert signature.parameters["max_steps"].default == 50
    assert signature.parameters["system_prompt"].default == ""
    assert signature.parameters["project_context"].default == ""
    assert signature.parameters["enable_work_chain"].default is True
    nullable_names = expected_names[4:7] + expected_names[8:19]
    for name in nullable_names:
        assert signature.parameters[name].default is None
    assert str(signature.return_annotation).strip("'\"") == "list[ChatMessage]"


def test_runtime_loop_import_orders_keep_one_module_identity() -> None:
    """Loop/tool and UI-facing import orders must not split the Agent Loop module."""

    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    snippets = (
        "import sys; import repoterm.runtime.loop; import repoterm.tools.task; "
        "import repoterm.app.interactive; import repoterm.app.headless; "
        "assert sys.modules['repoterm.runtime.loop'].__name__ == 'repoterm.runtime.loop'; "
        "assert 'repoterm.agent_loop' not in sys.modules",
        "import sys; import repoterm.tools.task; import repoterm.runtime.loop; "
        "assert sys.modules['repoterm.runtime.loop'].__name__ == 'repoterm.runtime.loop'; "
        "assert 'repoterm.agent_loop' not in sys.modules",
    )
    for snippet in snippets:
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_lower_runtime_layers_do_not_import_the_runtime_loop() -> None:
    """Planning, Control, Context and services remain below the Agent Loop."""

    repo_root = Path(__file__).resolve().parents[2]
    lower_layers = (
        repo_root / "repoterm/runtime/planning",
        repo_root / "repoterm/runtime/control",
        repo_root / "repoterm/context",
        repo_root / "repoterm/providers",
        repo_root / "repoterm/safety",
        repo_root / "repoterm/memory",
        repo_root / "repoterm/session",
        repo_root / "repoterm/observability",
    )
    for directory in lower_layers:
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "repoterm.runtime.loop" not in source
