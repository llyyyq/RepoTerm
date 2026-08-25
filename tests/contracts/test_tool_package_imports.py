"""Contract tests for the C1 Tool package import boundary."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("label", "imports"),
    [
        (
            "safety-first",
            (
                "import repoterm.safety.file_review",
                "import repoterm.tools.registry",
                "import repoterm.integrations.mcp",
            ),
        ),
        (
            "integrations-first",
            (
                "import repoterm.integrations.mcp",
                "import repoterm.tools.registry",
                "import repoterm.safety.workspace",
            ),
        ),
    ],
)
def test_tool_package_import_order_is_cycle_free(
    tmp_path: Path, label: str, imports: tuple[str, ...]
) -> None:
    profile_home = tmp_path / label
    profile_home.mkdir()
    environment = os.environ.copy()
    for variable in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        environment[variable] = str(profile_home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", "\n".join(imports)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{label} import order failed:\n{result.stderr}"
