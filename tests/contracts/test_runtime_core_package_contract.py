"""Contracts for the Runtime Core package boundary introduced by D3."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import repoterm.runtime as runtime
import repoterm.runtime.auto_mode as auto_mode
import repoterm.runtime.hooks as hooks
import repoterm.runtime.reflection as reflection
import repoterm.runtime.runtime_profiles as runtime_profiles
import repoterm.runtime.turn_kernel as turn_kernel
from repoterm.runtime.auto_mode import AutoModeChecker, PermissionMode
from repoterm.runtime.hooks import HookEvent, fire_hook_sync
from repoterm.runtime.reflection import ReflectionEngine
from repoterm.runtime.runtime_profiles import RuntimeProfile, resolve_runtime_profile
from repoterm.runtime.turn_kernel import TurnRecurrentState, TurnVerificationState


_RUNTIME_MODULES = (
    turn_kernel,
    runtime_profiles,
    auto_mode,
    hooks,
    reflection,
)

_OLD_PATHS = (
    "repoterm/turn_kernel.py",
    "repoterm/runtime_profiles.py",
    "repoterm/auto_mode.py",
    "repoterm/hooks.py",
    "repoterm/agent_reflection.py",
)


def test_runtime_core_modules_have_explicit_paths_and_lightweight_exports() -> None:
    """Every Runtime Core implementation has an explicit path and inert package init."""

    expected_directory = Path("repoterm/runtime").resolve()
    forbidden_dependencies = (
        "repoterm.ui",
        "repoterm.tui",
        "repoterm.tty_app",
        "repoterm.main",
        "repoterm.headless",
        "repoterm.benchmarks",
        "repoterm.agent_loop",
        "repoterm.runtime.loop",
    )
    for module in _RUNTIME_MODULES:
        module_path = Path(module.__file__).resolve()
        assert module_path.parent == expected_directory
        source = module_path.read_text(encoding="utf-8")
        assert all(dependency not in source for dependency in forbidden_dependencies)
        assert "importlib" not in source
        assert "__import__(" not in source
        assert "import_module(" not in source

    repo_root = Path(__file__).resolve().parents[2]
    assert all(not (repo_root / path).exists() for path in _OLD_PATHS)

    init_source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert runtime.__all__ == ()
    assert "import *" not in init_source
    assert "__getattr__" not in init_source
    assert "importlib" not in init_source
    assert "sys.modules" not in init_source


def test_runtime_core_objects_remain_available_from_explicit_modules() -> None:
    """Core Runtime objects remain directly available from their moved modules."""

    assert TurnRecurrentState.__module__ == turn_kernel.__name__
    assert TurnVerificationState.__module__ == turn_kernel.__name__
    assert RuntimeProfile.__module__ == runtime_profiles.__name__
    assert resolve_runtime_profile.__module__ == runtime_profiles.__name__
    assert AutoModeChecker.__module__ == auto_mode.__name__
    assert PermissionMode.__module__ == auto_mode.__name__
    assert HookEvent.__module__ == hooks.__name__
    assert fire_hook_sync.__module__ == hooks.__name__
    assert ReflectionEngine.__module__ == reflection.__name__


def test_runtime_core_import_orders_work_in_isolated_processes() -> None:
    """Forward and reverse imports of all Runtime Core modules must not cycle."""

    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    snippets = (
        "import repoterm.runtime.turn_kernel; "
        "import repoterm.runtime.runtime_profiles; "
        "import repoterm.runtime.auto_mode; "
        "import repoterm.runtime.hooks; "
        "import repoterm.runtime.reflection",
        "import repoterm.runtime.reflection; "
        "import repoterm.runtime.hooks; "
        "import repoterm.runtime.auto_mode; "
        "import repoterm.runtime.runtime_profiles; "
        "import repoterm.runtime.turn_kernel",
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
