"""Contracts for the Runtime Control package boundary introduced by D2."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import repoterm.runtime.control as control
import repoterm.runtime.control.adaptive_pid_tuner as adaptive_pid_tuner
import repoterm.runtime.control.context_cybernetics as context_cybernetics
import repoterm.runtime.control.cost_control as cost_control
import repoterm.runtime.control.cybernetic_ablation as cybernetic_ablation
import repoterm.runtime.control.cybernetic_orchestrator as cybernetic_orchestrator
import repoterm.runtime.control.cybernetic_supervisor as cybernetic_supervisor
import repoterm.runtime.control.decoupling_controller as decoupling_controller
import repoterm.runtime.control.feedback_controller as feedback_controller
import repoterm.runtime.control.feedforward_controller as feedforward_controller
import repoterm.runtime.control.pipeline_engine as pipeline_engine
import repoterm.runtime.control.predictive_controller as predictive_controller
import repoterm.runtime.control.progress_controller as progress_controller
import repoterm.runtime.control.self_healing_engine as self_healing_engine
import repoterm.runtime.control.stability_monitor as stability_monitor
import repoterm.runtime.control.state_observer as state_observer
import repoterm.runtime.control.verification_controller as verification_controller
from repoterm.runtime.control.cybernetic_orchestrator import CyberneticOrchestrator
from repoterm.runtime.control.progress_controller import ProgressController
from repoterm.runtime.control.verification_controller import (
    VerificationController,
    VerificationSignal,
)


_CONTROL_MODULES = (
    adaptive_pid_tuner,
    context_cybernetics,
    cost_control,
    cybernetic_ablation,
    cybernetic_orchestrator,
    cybernetic_supervisor,
    decoupling_controller,
    feedback_controller,
    feedforward_controller,
    pipeline_engine,
    predictive_controller,
    progress_controller,
    self_healing_engine,
    stability_monitor,
    state_observer,
    verification_controller,
)

_OLD_PATHS = tuple(
    f"repoterm/{module.__name__.rsplit('.', 1)[-1]}.py"
    for module in _CONTROL_MODULES
)


def test_runtime_control_modules_have_explicit_paths_and_lightweight_exports() -> None:
    """Every Control implementation is explicit and the package stays inert."""

    expected_directory = Path("repoterm/runtime/control").resolve()
    for module in _CONTROL_MODULES:
        module_path = Path(module.__file__).resolve()
        assert module_path.parent == expected_directory
        source = module_path.read_text(encoding="utf-8")
        assert "repoterm.ui" not in source
        assert "repoterm.tui" not in source
        assert "repoterm.tty_app" not in source
        assert "repoterm.main" not in source
        assert "repoterm.headless" not in source
        assert "repoterm.benchmarks" not in source
        assert "repoterm.agent_loop" not in source
        assert "repoterm.runtime.loop" not in source

    repo_root = Path(__file__).resolve().parents[2]
    assert all(not (repo_root / path).exists() for path in _OLD_PATHS)

    init_source = Path(control.__file__).read_text(encoding="utf-8")
    assert control.__all__ == ()
    assert "import *" not in init_source
    assert "__getattr__" not in init_source
    assert "importlib" not in init_source
    assert "sys.modules" not in init_source


def test_runtime_control_core_types_remain_available_from_explicit_modules() -> None:
    """Core Control objects are directly available from their moved modules."""

    assert VerificationController.__module__ == verification_controller.__name__
    assert VerificationSignal.__module__ == verification_controller.__name__
    assert ProgressController.__module__ == progress_controller.__name__
    assert CyberneticOrchestrator.__module__ == cybernetic_orchestrator.__name__


def test_runtime_control_import_orders_work_in_isolated_processes() -> None:
    """The documented forward and reverse imports must not create a cycle."""

    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    snippets = (
        "import repoterm.runtime.control.verification_controller; "
        "import repoterm.runtime.control.cybernetic_orchestrator; "
        "import repoterm.runtime.control.feedback_controller",
        "import repoterm.runtime.control.feedback_controller; "
        "import repoterm.runtime.control.cybernetic_orchestrator; "
        "import repoterm.runtime.control.verification_controller",
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
