from __future__ import annotations

from pathlib import Path

from Main.RepoTermFrontline.Src.Application.Query.RuntimeCapabilityInventory import (
    build_runtime_capability_inventory,
)


def test_runtime_capability_inventory_covers_core_current_app_files() -> None:
    inventory = build_runtime_capability_inventory(Path.cwd())

    assert inventory["logicalProductApp"] == "product/app/repoterm_frontline"
    assert inventory["currentImplementationRoot"] == "repoterm"
    assert inventory["missingEvidence"] == []
    assert inventory["sliceCount"] >= 8
    assert {
        item["currentPath"] for item in inventory["slices"]
    } >= {
        "repoterm/app/interactive.py",
        "repoterm/app/headless.py",
        "repoterm/app/readiness.py",
        "repoterm/ui/commands.py",
        "repoterm/session/service.py",
        "repoterm/config.py",
        "repoterm/runtime/surfaces.py",
    }


def test_runtime_capability_inventory_names_next_migration_candidates() -> None:
    inventory = build_runtime_capability_inventory(Path.cwd())
    candidates = inventory["nextMigrationCandidates"]

    assert [item["currentPath"] for item in candidates] == [
        "repoterm/app/interactive.py",
        "repoterm/app/headless.py",
        "repoterm/app/readiness.py",
    ]
    assert candidates[0]["migrationCandidate"] == "Main/RepoTermFrontline/Src/Boot"
    assert "entry" in inventory["capabilityKindCounts"]
