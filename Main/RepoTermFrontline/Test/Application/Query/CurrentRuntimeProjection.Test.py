from __future__ import annotations

from pathlib import Path

from Main.RepoTermFrontline.Src.Application.Query.CurrentRuntimeProjection import (
    build_current_runtime_projection,
)


def test_current_runtime_projection_reports_all_entry_evidence() -> None:
    projection = build_current_runtime_projection(Path.cwd())

    assert projection["logicalProductApp"] == "product/app/repoterm_frontline"
    assert projection["currentImplementationRoot"] == "repoterm"
    assert projection["entryCount"] == 5
    assert projection["missingEvidence"] == []
    assert {
        entry["evidencePath"] for entry in projection["entries"]
    } == {
        "repoterm/app/interactive.py",
        "repoterm/app/headless.py",
        "repoterm/app/readiness.py",
        "repoterm/ui/commands.py",
        "repoterm/runtime/surfaces.py",
    }
