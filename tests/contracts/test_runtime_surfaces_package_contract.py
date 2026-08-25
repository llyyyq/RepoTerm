"""Contracts for the Runtime Product Surfaces package boundary introduced by E1A."""

from __future__ import annotations

from dataclasses import is_dataclass
from pathlib import Path

import repoterm.runtime.surfaces as surfaces
from repoterm.runtime.surfaces import (
    DelegationStatus,
    ExtensionManifest,
    HookStatus,
    InstructionLayer,
    PromptBundle,
    ReadinessReport,
    build_product_snapshot,
    build_readiness_report,
    collect_instruction_layers,
)


def test_runtime_surfaces_has_only_the_new_path_and_no_ui_dependency() -> None:
    """Product Surfaces lives below UI and has no reverse UI dependency."""

    repo_root = Path(__file__).resolve().parents[2]
    new_path = repo_root / "repoterm/runtime/surfaces.py"
    old_path = repo_root / "repoterm/product_surfaces.py"
    assert new_path.exists()
    assert not old_path.exists()
    assert surfaces.__file__ is not None
    assert Path(surfaces.__file__).resolve() == new_path.resolve()

    source = new_path.read_text(encoding="utf-8")
    assert "repoterm.ui" not in source
    assert "repoterm.tui" not in source
    assert "import *" not in source
    assert "importlib" not in source
    assert "__getattr__" not in source
    assert "sys.modules" not in source


def test_runtime_surfaces_core_objects_remain_available_from_new_module() -> None:
    """All Product Surfaces dataclasses and builders keep their public identity."""

    dataclasses = (
        InstructionLayer,
        ExtensionManifest,
        HookStatus,
        DelegationStatus,
        ReadinessReport,
        PromptBundle,
    )
    assert all(is_dataclass(item) for item in dataclasses)
    assert all(item.__module__ == surfaces.__name__ for item in dataclasses)
    assert collect_instruction_layers.__module__ == surfaces.__name__
    assert build_readiness_report.__module__ == surfaces.__name__
    assert build_product_snapshot.__module__ == surfaces.__name__


def test_runtime_surfaces_builders_are_deterministic_for_same_input(tmp_path) -> None:
    """Moving the module preserves deterministic Product Surface structures."""

    runtime = {"model": "mock"}
    assert collect_instruction_layers(tmp_path) == collect_instruction_layers(tmp_path)
    assert build_readiness_report(tmp_path, runtime=runtime) == build_readiness_report(
        tmp_path,
        runtime=runtime,
    )
    assert build_product_snapshot(tmp_path, runtime=runtime) == build_product_snapshot(
        tmp_path,
        runtime=runtime,
    )
