"""Contracts for the Runtime Planning package boundary introduced by D1."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import repoterm.runtime.planning as planning
import repoterm.runtime.planning.capability_registry as capability_registry
import repoterm.runtime.planning.domain_classifier as domain_classifier
import repoterm.runtime.planning.intelligence as intelligence
import repoterm.runtime.planning.intent_parser as intent_parser
import repoterm.runtime.planning.prompt as prompt
import repoterm.runtime.planning.prompt_pipeline as prompt_pipeline
import repoterm.runtime.planning.router as router
import repoterm.runtime.planning.smart_router as smart_router
import repoterm.runtime.planning.task_graph as task_graph
import repoterm.runtime.planning.task_object as task_object
import repoterm.runtime.planning.task_tracker as task_tracker
from repoterm.runtime.planning.intent_parser import ParsedIntent
from repoterm.runtime.planning.prompt import (
    build_system_prompt,
    build_system_prompt_bundle,
)
from repoterm.runtime.planning.task_object import TaskObject


_PLANNING_MODULES = (
    intelligence,
    router,
    smart_router,
    domain_classifier,
    intent_parser,
    task_graph,
    task_object,
    task_tracker,
    capability_registry,
    prompt,
    prompt_pipeline,
)

_OLD_PATHS = (
    "repoterm/agent_intelligence.py",
    "repoterm/agent_router.py",
    "repoterm/smart_router.py",
    "repoterm/domain_classifier.py",
    "repoterm/intent_parser.py",
    "repoterm/task_graph.py",
    "repoterm/task_object.py",
    "repoterm/task_tracker.py",
    "repoterm/capability_registry.py",
    "repoterm/prompt.py",
    "repoterm/prompt_pipeline.py",
)


def test_runtime_planning_modules_have_explicit_paths_and_lightweight_exports() -> None:
    """Every Planning implementation lives at its new path without bulk exports."""

    expected_directory = Path("repoterm/runtime/planning").resolve()
    for module in _PLANNING_MODULES:
        module_path = Path(module.__file__).resolve()
        assert module_path.parent == expected_directory
        source = module_path.read_text(encoding="utf-8")
        assert "repoterm.ui" not in source
        assert "repoterm.tui" not in source
        assert "repoterm.tty_app" not in source
        assert "repoterm.runtime.loop" not in source

    assert all(not Path(path).exists() for path in _OLD_PATHS)
    init_source = Path(planning.__file__).read_text(encoding="utf-8")
    assert planning.__all__ == ()
    assert not hasattr(planning, "TaskObject")
    assert not hasattr(planning, "ParsedIntent")
    assert not hasattr(planning, "build_system_prompt")
    assert "import *" not in init_source
    assert "__getattr__" not in init_source
    assert "importlib" not in init_source
    assert "sys.modules" not in init_source


def test_runtime_planning_public_objects_are_available_from_new_modules() -> None:
    """Core Planning objects remain directly importable from explicit modules."""

    assert callable(build_system_prompt)
    assert callable(build_system_prompt_bundle)
    assert TaskObject.__module__ == task_object.__name__
    assert ParsedIntent.__module__ == intent_parser.__name__


def test_runtime_planning_import_orders_work_in_isolated_processes() -> None:
    """The two documented import orders must not create a package cycle."""

    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    snippets = (
        "import repoterm.runtime.planning.prompt; "
        "import repoterm.runtime.planning.task_object; "
        "import repoterm.runtime.planning.intent_parser",
        "import repoterm.runtime.planning.intent_parser; "
        "import repoterm.runtime.planning.task_object; "
        "import repoterm.runtime.planning.prompt",
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
