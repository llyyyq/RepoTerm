"""阶段 F1 的 Evaluation 与 Provider Fallback 结构契约。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import benchmarks.evaluation as evaluation_package
import benchmarks.evaluation.llm_e2e as llm_e2e
import benchmarks.evaluation.runtime_profile as runtime_profile
import repoterm.app.readiness as readiness
import repoterm.providers.fallback_simulation as fallback_simulation


ROOT = Path(__file__).resolve().parents[2]


def test_evaluation_layout_and_lightweight_package_boundary() -> None:
    assert evaluation_package.__all__ == ()
    init_source = (ROOT / "benchmarks/evaluation/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "import *" not in init_source
    assert "importlib" not in init_source
    assert "__getattr__" not in init_source
    assert "sys.modules" not in init_source

    assert (ROOT / "benchmarks/evaluation/llm_e2e.py").exists()
    assert (ROOT / "benchmarks/evaluation/runtime_profile.py").exists()
    assert not (ROOT / "repoterm/llm_e2e_eval.py").exists()
    assert not (ROOT / "repoterm/runtime_profile_eval.py").exists()
    assert not (ROOT / "repoterm/fallback_simulation.py").exists()
    assert (ROOT / "repoterm/providers/fallback_simulation.py").exists()
    assert not (ROOT / "benchmarks/evaluation/fallback_simulation.py").exists()


def test_wrappers_and_readiness_point_to_the_new_implementations() -> None:
    llm_wrapper = (ROOT / "benchmarks/llm_e2e_eval.py").read_text(encoding="utf-8")
    runtime_wrapper = (ROOT / "benchmarks/runtime_profile_eval.py").read_text(
        encoding="utf-8"
    )
    readiness_source = (ROOT / "repoterm/app/readiness.py").read_text(
        encoding="utf-8"
    )
    assert "from benchmarks.evaluation.llm_e2e import main" in llm_wrapper
    assert "from benchmarks.evaluation.runtime_profile import" in runtime_wrapper
    assert "from repoterm.providers.fallback_simulation import" in readiness_source
    assert "repoterm.fallback_simulation" not in readiness_source

    provider_init = (ROOT / "repoterm/providers/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "fallback_simulation" not in provider_init


def test_llm_e2e_root_and_default_output_paths_are_unchanged() -> None:
    assert llm_e2e.REPO_ROOT == ROOT.resolve()
    assert llm_e2e.DEFAULT_ARTIFACTS_DIR == ROOT / ".temp" / "llm_e2e"
    assert llm_e2e.DEFAULT_JSON_REPORT == ROOT / "benchmarks" / "llm_e2e_results.json"
    assert llm_e2e.DEFAULT_MARKDOWN_REPORT == (
        ROOT / "benchmarks" / "llm_e2e_results.md"
    )
    assert llm_e2e.DEFAULT_DRY_RUN_REPORT == (
        ROOT / "benchmarks" / "llm_e2e_dry_run.md"
    )


def test_evaluation_public_functions_are_importable() -> None:
    assert llm_e2e.main.__module__ == "benchmarks.evaluation.llm_e2e"
    assert llm_e2e.run_fixture_preflight.__module__ == "benchmarks.evaluation.llm_e2e"
    assert runtime_profile.evaluate_runtime_profiles.__module__ == (
        "benchmarks.evaluation.runtime_profile"
    )
    assert runtime_profile.runtime_profile_eval_as_dict.__module__ == (
        "benchmarks.evaluation.runtime_profile"
    )
    assert fallback_simulation.select_fallback_preview.__module__ == (
        "repoterm.providers.fallback_simulation"
    )
    assert readiness.main.__module__ == "repoterm.app.readiness"


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


def test_forward_import_order_does_not_load_old_modules() -> None:
    _run_import_order(
        """
import sys
import benchmarks.evaluation.llm_e2e
import benchmarks.evaluation.runtime_profile
import repoterm.providers.fallback_simulation
import repoterm.app.readiness
assert 'repoterm.llm_e2e_eval' not in sys.modules
assert 'repoterm.runtime_profile_eval' not in sys.modules
assert 'repoterm.fallback_simulation' not in sys.modules
"""
    )


def test_reverse_import_order_does_not_load_old_modules() -> None:
    _run_import_order(
        """
import sys
import repoterm.app.readiness
import repoterm.providers.fallback_simulation
import benchmarks.evaluation.runtime_profile
import benchmarks.evaluation.llm_e2e
assert 'repoterm.llm_e2e_eval' not in sys.modules
assert 'repoterm.runtime_profile_eval' not in sys.modules
assert 'repoterm.fallback_simulation' not in sys.modules
"""
    )


def test_product_layers_do_not_import_evaluation_implementations() -> None:
    product_layers = (
        "repoterm/app",
        "repoterm/runtime",
        "repoterm/providers",
        "repoterm/context",
        "repoterm/safety",
        "repoterm/session",
        "repoterm/memory",
        "repoterm/observability",
        "repoterm/tools",
        "repoterm/integrations",
    )
    for layer in product_layers:
        for source_file in (ROOT / layer).rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            assert "benchmarks.evaluation" not in source, source_file.as_posix()
