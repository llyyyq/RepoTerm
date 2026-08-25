"""阶段 E2 的 Application Entry Package 结构契约。"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import repoterm.app as app_package
import repoterm.app.engineering_structure as engineering_structure
import repoterm.app.headless as headless
import repoterm.app.install as install
import repoterm.app.interactive as interactive
import repoterm.app.readiness as readiness
import repoterm.app.structure_check as structure_check


ROOT = Path(__file__).resolve().parents[2]
OLD_MODULES = (
    "repoterm.main",
    "repoterm.headless",
    "repoterm.readiness",
    "repoterm.structure_check",
    "repoterm.engineering_structure",
    "repoterm.install",
)
OLD_FILES = tuple(module.replace(".", "/") + ".py" for module in OLD_MODULES)
NEW_FILES = (
    "repoterm/app/interactive.py",
    "repoterm/app/headless.py",
    "repoterm/app/readiness.py",
    "repoterm/app/structure_check.py",
    "repoterm/app/engineering_structure.py",
    "repoterm/app/install.py",
)


def test_app_layout_and_lightweight_package_boundary() -> None:
    assert app_package.__all__ == ()
    init_source = (ROOT / "repoterm/app/__init__.py").read_text(encoding="utf-8")
    assert "import *" not in init_source
    assert "importlib" not in init_source
    assert "__getattr__" not in init_source
    assert "sys.modules" not in init_source

    assert all((ROOT / path).exists() for path in NEW_FILES)
    assert all(not (ROOT / path).exists() for path in OLD_FILES)


def test_app_public_functions_and_signatures_are_stable() -> None:
    assert interactive.main.__module__ == "repoterm.app.interactive"
    assert headless.main.__module__ == "repoterm.app.headless"
    assert headless.run_headless.__module__ == "repoterm.app.headless"
    assert readiness.main.__module__ == "repoterm.app.readiness"
    assert structure_check.main.__module__ == "repoterm.app.structure_check"
    assert (
        structure_check.check_material_inventory.__module__
        == "repoterm.app.structure_check"
    )
    assert install.main.__module__ == "repoterm.app.install"

    assert list(inspect.signature(interactive.main).parameters) == []
    assert list(inspect.signature(install.main).parameters) == []

    headless_signature = inspect.signature(headless.run_headless)
    assert list(headless_signature.parameters) == ["prompt", "allow_edits"]
    assert headless_signature.parameters["prompt"].default is None
    assert headless_signature.parameters["allow_edits"].default is False

    for function in (
        headless.main,
        readiness.main,
        structure_check.main,
    ):
        signature = inspect.signature(function)
        assert list(signature.parameters) == ["argv"]
        assert signature.parameters["argv"].default is None

    inventory_signature = inspect.signature(structure_check.check_material_inventory)
    assert list(inventory_signature.parameters) == ["root", "inventory_path"]
    assert inventory_signature.parameters["inventory_path"].default is None
    assert (
        inventory_signature.parameters["inventory_path"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )


def test_install_launcher_uses_the_new_interactive_module() -> None:
    source = (ROOT / "repoterm/app/install.py").read_text(encoding="utf-8")
    assert "python -m repoterm.main" not in source
    assert "python3 -m repoterm.main" not in source
    assert "repoterm.app.interactive" in source


def test_pyproject_scripts_target_the_application_package() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'repoterm = "repoterm.app.interactive:main"' in pyproject
    assert 'repoterm-headless = "repoterm.app.headless:main"' in pyproject
    assert 'repoterm-readiness = "repoterm.app.readiness:main"' in pyproject
    assert (
        'repoterm-structure-check = "repoterm.app.structure_check:main"'
        in pyproject
    )


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


def test_forward_import_order_keeps_legacy_modules_unloaded() -> None:
    _run_import_order(
        """
import sys
import repoterm.app.interactive
import repoterm.app.headless
import repoterm.app.readiness
import repoterm.app.structure_check
import repoterm.app.engineering_structure
import repoterm.app.install
assert all(module not in sys.modules for module in (
    'repoterm.main', 'repoterm.headless', 'repoterm.readiness',
    'repoterm.structure_check', 'repoterm.engineering_structure',
    'repoterm.install',
))
"""
    )


def test_reverse_import_order_keeps_legacy_modules_unloaded() -> None:
    _run_import_order(
        """
import sys
import repoterm.app.install
import repoterm.app.engineering_structure
import repoterm.app.structure_check
import repoterm.app.readiness
import repoterm.app.headless
import repoterm.app.interactive
assert all(module not in sys.modules for module in (
    'repoterm.main', 'repoterm.headless', 'repoterm.readiness',
    'repoterm.structure_check', 'repoterm.engineering_structure',
    'repoterm.install',
))
"""
    )


def test_lower_layers_do_not_import_app_package() -> None:
    lower_layers = (
        "runtime",
        "providers",
        "context",
        "safety",
        "memory",
        "session",
        "observability",
        "tools",
        "integrations",
        "contracts",
    )
    for layer in lower_layers:
        for source_file in (ROOT / "repoterm" / layer).rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            assert "repoterm.app" not in source, source_file.as_posix()
