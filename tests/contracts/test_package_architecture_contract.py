"""阶段 F3 的最终包结构、旧 Import 与依赖方向契约。"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_PACKAGES = (
    "repoterm/app",
    "repoterm/contracts",
    "repoterm/runtime",
    "repoterm/runtime/planning",
    "repoterm/runtime/control",
    "repoterm/providers",
    "repoterm/context",
    "repoterm/safety",
    "repoterm/session",
    "repoterm/memory",
    "repoterm/observability",
    "repoterm/tools",
    "repoterm/integrations",
    "repoterm/ui",
    "repoterm/ui/tui",
    "benchmarks/evaluation",
)

_KEY_PATHS = (
    "repoterm/app/interactive.py",
    "repoterm/app/headless.py",
    "repoterm/app/readiness.py",
    "repoterm/app/structure_check.py",
    "repoterm/runtime/loop.py",
    "repoterm/runtime/turn_kernel.py",
    "repoterm/runtime/runtime_profiles.py",
    "repoterm/runtime/surfaces.py",
    "repoterm/runtime/planning/prompt.py",
    "repoterm/runtime/planning/router.py",
    "repoterm/runtime/control/verification_controller.py",
    "repoterm/runtime/control/progress_controller.py",
    "repoterm/contracts/types.py",
    "repoterm/contracts/state.py",
    "repoterm/providers/registry.py",
    "repoterm/providers/anthropic.py",
    "repoterm/providers/openai.py",
    "repoterm/providers/fallback_simulation.py",
    "repoterm/context/manager.py",
    "repoterm/context/compactor.py",
    "repoterm/context/micro_compact.py",
    "repoterm/safety/permissions.py",
    "repoterm/safety/file_review.py",
    "repoterm/session/service.py",
    "repoterm/tools/registry.py",
    "repoterm/tools/background.py",
    "repoterm/observability/logging.py",
    "repoterm/observability/metrics.py",
    "repoterm/observability/cost.py",
    "repoterm/integrations/mcp.py",
    "repoterm/integrations/skills.py",
    "repoterm/ui/commands.py",
    "repoterm/ui/tty.py",
    "repoterm/ui/tui/input_handler.py",
    "benchmarks/evaluation/llm_e2e.py",
    "benchmarks/evaluation/runtime_profile.py",
    "benchmarks/llm_e2e_eval.py",
    "benchmarks/runtime_profile_eval.py",
    "benchmarks/runtime_regression_eval.py",
)

_OLD_PATHS = (
    "repoterm/main.py",
    "repoterm/headless.py",
    "repoterm/readiness.py",
    "repoterm/structure_check.py",
    "repoterm/engineering_structure.py",
    "repoterm/install.py",
    "repoterm/agent_loop.py",
    "repoterm/turn_kernel.py",
    "repoterm/runtime_profiles.py",
    "repoterm/product_surfaces.py",
    "repoterm/tooling.py",
    "repoterm/background_tasks.py",
    "repoterm/permissions.py",
    "repoterm/file_review.py",
    "repoterm/evidence_safety.py",
    "repoterm/workspace.py",
    "repoterm/session.py",
    "repoterm/types.py",
    "repoterm/state.py",
    "repoterm/model_registry.py",
    "repoterm/anthropic_adapter.py",
    "repoterm/openai_adapter.py",
    "repoterm/mock_model.py",
    "repoterm/api_retry.py",
    "repoterm/model_switcher.py",
    "repoterm/fallback_simulation.py",
    "repoterm/context_manager.py",
    "repoterm/context_compactor.py",
    "repoterm/micro_compact.py",
    "repoterm/layered_context.py",
    "repoterm/working_memory.py",
    "repoterm/circuit_breaker.py",
    "repoterm/logging_config.py",
    "repoterm/agent_metrics.py",
    "repoterm/cost_tracker.py",
    "repoterm/decision_audit.py",
    "repoterm/mcp.py",
    "repoterm/skills.py",
    "repoterm/cli_commands.py",
    "repoterm/tty_app.py",
    "repoterm/history.py",
    "repoterm/local_tool_shortcuts.py",
    "repoterm/manage_cli.py",
    "repoterm/tui",
    "repoterm/llm_e2e_eval.py",
    "repoterm/runtime_profile_eval.py",
)

_OLD_IMPORTS = tuple(
    f"repoterm.{name}"
    for name in (
        "agent_loop",
        "turn_kernel",
        "runtime_profiles",
        "product_surfaces",
        "tooling",
        "background_tasks",
        "permissions",
        "file_review",
        "evidence_safety",
        "workspace",
        "model_registry",
        "anthropic_adapter",
        "openai_adapter",
        "mock_model",
        "api_retry",
        "model_switcher",
        "fallback_simulation",
        "context_manager",
        "context_compactor",
        "micro_compact",
        "layered_context",
        "working_memory",
        "circuit_breaker",
        "logging_config",
        "agent_metrics",
        "cost_tracker",
        "decision_audit",
        "mcp",
        "skills",
        "cli_commands",
        "tty_app",
        "history",
        "local_tool_shortcuts",
        "manage_cli",
        "tui",
        "main",
        "headless",
        "readiness",
        "structure_check",
        "engineering_structure",
        "install",
        "types",
        "state",
        "llm_e2e_eval",
        "runtime_profile_eval",
    )
)


def _python_files(*roots: str) -> list[Path]:
    return [path for root in roots for path in (ROOT / root).rglob("*.py")]


def _import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def _assert_no_imports(paths: list[Path], forbidden: tuple[str, ...]) -> None:
    violations = [
        (path.relative_to(ROOT).as_posix(), module)
        for path in paths
        for module in _import_modules(path)
        if module == module.removeprefix(".")
        and any(module == item or module.startswith(f"{item}.") for item in forbidden)
    ]
    assert not violations, violations


def test_final_package_layout_and_console_scripts() -> None:
    """根目录、目标包、关键实现和四个入口必须稳定存在。"""

    root_python_files = {path.name for path in (ROOT / "repoterm").glob("*.py")}
    assert root_python_files == {"__init__.py", "config.py"}

    for package in _REQUIRED_PACKAGES:
        package_path = ROOT / package
        assert package_path.is_dir(), package
        assert (package_path / "__init__.py").is_file(), package

    assert all((ROOT / path).is_file() for path in _KEY_PATHS)

    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["scripts"] == {
        "repoterm": "repoterm.app.interactive:main",
        "repoterm-headless": "repoterm.app.headless:main",
        "repoterm-readiness": "repoterm.app.readiness:main",
        "repoterm-structure-check": "repoterm.app.structure_check:main",
    }


def test_old_implementation_paths_and_executable_imports_are_absent() -> None:
    """旧文件不应形成双份实现，可执行 Python Import 也必须清零。"""

    assert all(not (ROOT / path).exists() for path in _OLD_PATHS)

    executable_roots = _python_files("repoterm", "tests", "benchmarks")
    violations = [
        (path.relative_to(ROOT).as_posix(), module)
        for path in executable_roots
        for module in _import_modules(path)
        if module in _OLD_IMPORTS
        or any(module.startswith(f"{old}.") for old in _OLD_IMPORTS)
    ]
    assert not violations, violations


def test_dependency_direction_is_acyclic_at_package_boundaries() -> None:
    """契约层只能用标准库，底层产品层不能反向依赖 UI 或评测。"""

    contracts = _python_files("repoterm/contracts")
    _assert_no_imports(contracts, ("repoterm", "benchmarks"))

    lower_layers = _python_files(
        "repoterm/runtime",
        "repoterm/providers",
        "repoterm/context",
        "repoterm/safety",
    )
    _assert_no_imports(
        lower_layers,
        ("repoterm.app", "repoterm.ui", "benchmarks.evaluation"),
    )

    providers = _python_files("repoterm/providers")
    _assert_no_imports(
        providers,
        (
            "repoterm.runtime.loop",
            "repoterm.ui",
            "repoterm.app",
            "benchmarks",
        ),
    )

    product_code = _python_files("repoterm")
    _assert_no_imports(product_code, ("benchmarks.evaluation",))


def test_representative_forward_and_reverse_imports_do_not_cycle() -> None:
    """关键入口和底层包按两个方向导入时都不得产生循环依赖。"""

    snippets = (
        "import repoterm.runtime.loop; import repoterm.app.interactive; "
        "import repoterm.ui.tty; import repoterm.providers.registry",
        "import repoterm.providers.registry; import repoterm.ui.tty; "
        "import repoterm.app.interactive; import repoterm.runtime.loop",
    )
    environment = {**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for snippet in snippets:
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
