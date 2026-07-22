from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = (
    ROOT / "Docs" / "Documentation" / "engineering" / "material-inventory.json"
)


def _load_inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _load_repo_json(path_text: str) -> dict:
    return json.loads((ROOT / path_text).read_text(encoding="utf-8"))


def _assert_repo_path_exists(path_text: str) -> None:
    path = ROOT / path_text
    assert path.exists(), f"expected repo path to exist: {path_text}"


def _assert_repo_or_optional_material_path(path_text: str) -> None:
    path = ROOT / path_text
    if path.exists():
        return
    optional_roots = {
        material["path"].rstrip("/")
        for material in _load_inventory()["materials"]
        if material.get("presencePolicy") == "optional-workspace-material"
    }
    assert any(
        path_text == root or path_text.startswith(f"{root}/")
        for root in optional_roots
    ), f"expected repo or optional material path: {path_text}"


def test_material_inventory_tracks_current_product_app_entries() -> None:
    inventory = _load_inventory()

    assert inventory["schemaVersion"] == 2

    app = inventory["currentProductApp"]
    assert app["logicalBoundary"] == "product/app/repoterm_frontline"
    assert app["currentSourceRoot"] == "repoterm"
    assert app["status"] == "active"

    entries = {entry["name"]: entry for entry in app["entrySurfaces"]}
    assert entries["interactive-cli"]["path"] == "repoterm/main.py"
    assert entries["headless-runner"]["path"] == "repoterm/headless.py"
    assert entries["local-command-surface"]["path"] == "repoterm/cli_commands.py"
    assert entries["product-surfaces"]["path"] == "repoterm/product_surfaces.py"
    assert entries["readiness-gate"]["path"] == "repoterm/readiness.py"
    assert entries["readiness-gate"]["script"] == "repoterm-readiness"
    assert "release-readiness" not in entries

    for entry in app["entrySurfaces"]:
        _assert_repo_path_exists(entry["path"])

    for evidence in app["coverageEvidence"]:
        assert evidence["reason"]
        _assert_repo_path_exists(evidence["path"])


def test_material_inventory_covers_known_material_roots() -> None:
    inventory = _load_inventory()

    materials = {item["path"]: item for item in inventory["materials"]}
    assert {
        "ts-src/py-src",
        "ts-src",
        "RepoTerm-fork",
        "RepoTerm-main-work",
        "claude-code-src",
        "superpowers-zh",
        ".dead-modules-backup",
        "experiments",
        "outputs",
    }.issubset(materials)

    assert "py-src" in materials["ts-src/py-src"]["historicalAliases"]
    assert "paper_experiments" in materials["experiments"]["historicalAliases"]
    assert materials["ts-src"]["burndownManifest"] == (
        "Docs/Documentation/engineering/material-burndown/ts-src.json"
    )
    assert materials["RepoTerm-fork"]["burndownManifest"] == (
        "Docs/Documentation/engineering/material-burndown/repoterm-fork.json"
    )
    assert materials["RepoTerm-main-work"]["burndownManifest"] == (
        "Docs/Documentation/engineering/material-burndown/repoterm-main-work.json"
    )


def test_material_inventory_materials_are_observed_and_evidenced() -> None:
    inventory = _load_inventory()

    for material in inventory["materials"]:
        assert material["identity"]
        assert material["status"]
        assert material["callerSummary"]
        assert material["replacementTarget"]
        assert material["retirementCondition"]
        optional_workspace_material = (
            material.get("presencePolicy") == "optional-workspace-material"
        )
        if not optional_workspace_material:
            _assert_repo_path_exists(material["path"])

        assert material["observedEntries"], f"{material['path']} is missing observedEntries"
        for entry in material["observedEntries"]:
            assert entry["name"]
            assert entry["result"]
            if not optional_workspace_material:
                _assert_repo_path_exists(entry["path"])

        assert material["coverageEvidence"], f"{material['path']} is missing coverageEvidence"
        for evidence in material["coverageEvidence"]:
            assert evidence["reason"]
            _assert_repo_path_exists(evidence["path"])

        for caller in material["currentCallers"]:
            assert caller["reason"]
            _assert_repo_path_exists(caller["path"])

        for reference in material.get("historicalReferences", []):
            assert reference["reason"]
            _assert_repo_path_exists(reference["path"])

        if "burndownManifest" in material:
            _assert_repo_path_exists(material["burndownManifest"])


def test_archive_approved_materials_have_no_current_callers() -> None:
    inventory = _load_inventory()
    materials = {item["path"]: item for item in inventory["materials"]}

    for path in ("ts-src", "RepoTerm-fork", "RepoTerm-main-work"):
        material = materials[path]
        assert material["status"].startswith("archive-approved-")
        assert material["currentCallers"] == []

    assert materials["ts-src"]["historicalReferences"][0]["path"] == "Docs/Documentation/CODE_WIKI.md"
    assert materials["RepoTerm-fork"]["historicalReferences"][0]["path"] == (
        "Docs/Documentation/CODE_WIKI.md"
    )


def test_material_inventory_focused_gates_remain_portable() -> None:
    inventory = _load_inventory()

    gates = {gate["name"]: gate for gate in inventory["focusedGates"]}
    assert set(gates) == {
        "compileall",
        "product-entry-gates",
        "structure-compliance",
        "readiness-gate",
    }
    assert "benchmarks" in gates["compileall"]["command"]
    assert "Main" in gates["compileall"]["command"]
    assert "Package" in gates["compileall"]["command"]
    assert "AppProjection.Test.py" in gates["product-entry-gates"]["command"]
    assert "RepoTermFrontline.Test.py" in gates["product-entry-gates"]["command"]
    assert "LocalCommandSurface.Test.py" in gates["product-entry-gates"]["command"]
    assert "RuntimeLifecycleSurface.Test.py" in gates["product-entry-gates"]["command"]
    assert "CurrentRuntimeProjection.Test.py" in gates["product-entry-gates"]["command"]
    assert "RuntimeCapabilityInventory.Test.py" in gates["product-entry-gates"]["command"]
    assert "ProductRootProjection.Test.py" in gates["product-entry-gates"]["command"]
    assert "StructureCompliance.Test.py" in gates["product-entry-gates"]["command"]
    assert "--import-mode=importlib" in gates["product-entry-gates"]["command"]
    assert "tests/test_engineering_structure.py" in gates["product-entry-gates"]["command"]
    assert (
        gates["structure-compliance"]["command"]
        == "python -m repoterm.structure_check --root . --hotspots 5 --max-dependency-upstream 4 --check-material-inventory --report .temp/structure-compliance.json"
    )
    assert (
        gates["readiness-gate"]["command"]
        == "python -m repoterm.readiness --json --fail-on blocked"
    )
    for gate in gates.values():
        assert gate["command"].startswith("python -m ")
        assert gate["portableFallback"].startswith("python3 -m ")


def test_material_inventory_contains_no_distribution_release_gates() -> None:
    inventory = _load_inventory()
    serialized = json.dumps(inventory, ensure_ascii=False)

    assert "repoterm.release_readiness" not in serialized
    assert "test_packaging.py" not in serialized


def test_ts_src_py_src_burndown_manifest_tracks_legacy_only_modules() -> None:
    manifest = _load_repo_json("Docs/Documentation/engineering/material-burndown/ts-src-py-src.json")

    assert manifest["materialRoot"] == "ts-src/py-src"
    assert manifest["summary"]["legacyOnlyRelativePathCount"] == 11
    assert manifest["summary"]["sharedLegacyTestFileCount"] == 16

    entries = {entry["legacyRelativePath"]: entry for entry in manifest["entries"]}
    assert len(entries) == 11

    assert entries["async_context.py"]["status"] == "legacy-only-no-current-caller"
    assert manifest["summary"]["currentNameResidueCount"] == 0
    assert manifest["summary"]["retiredLegacyOnlyModuleCount"] == 11
    assert manifest["dispositionPolicy"].startswith("Legacy-only modules are retired")
    assert entries["tools/multi_edit.py"]["status"] == "legacy-only-no-current-caller"
    assert entries["tools/run_with_debug.py"]["status"] == "legacy-only-no-current-caller"
    assert not entries["tools/multi_edit.py"]["currentReferences"]
    assert not entries["tools/run_with_debug.py"]["currentReferences"]
    assert entries["tools/multi_edit.py"]["disposition"] == "retired"
    assert entries["tools/multi_edit.py"]["replacementEvidence"][0]["path"] == (
        "repoterm/tools/patch_file.py"
    )
    assert entries["sub_agents.py"]["replacementEvidence"][0]["path"] == (
        "repoterm/tools/task.py"
    )

    for entry in manifest["entries"]:
        _assert_repo_or_optional_material_path(entry["legacyPath"])
        assert entry["disposition"] == "retired"
        for current in entry["currentReferences"]:
            assert current["reason"]
            _assert_repo_path_exists(current["path"])
        for evidence in entry["replacementEvidence"]:
            assert evidence["reason"]
            _assert_repo_path_exists(evidence["path"])


def test_legacy_only_tool_names_are_not_live_current_code_heuristics() -> None:
    stale_tool_names = {
        "api_tester",
        "db_explorer",
        "docker_helper",
        "multi_edit",
        "run_with_debug",
    }
    current_sources = [
        ROOT / "repoterm" / "tooling.py",
        ROOT / "repoterm" / "context_manager.py",
    ]

    for source_path in current_sources:
        source = source_path.read_text(encoding="utf-8")
        for tool_name in stale_tool_names:
            assert tool_name not in source, f"stale legacy tool name in {source_path}"


def test_ts_src_burndown_manifest_tracks_reference_boundary() -> None:
    manifest = _load_repo_json("Docs/Documentation/engineering/material-burndown/ts-src.json")

    assert manifest["materialRoot"] == "ts-src"
    assert manifest["summary"]["activeProductCallerCount"] == 0
    assert manifest["summary"]["typescriptSourceFileCount"] == 45
    assert manifest["summary"]["delegatedNestedMaterialCount"] == 1
    assert manifest["summary"]["docsReferenceCallerCount"] == 1
    assert manifest["archiveApproval"]["approvedAction"] == (
        "archival deletion allowed after inventory gates pass"
    )
    assert manifest["archiveApproval"]["retainedInPlace"] is False
    assert manifest["dispositionPolicy"].startswith(
        "Archive-approved reference material"
    )
    assert "current product-facing docs no longer link into ts-src" in (
        manifest["dispositionPolicy"]
    )

    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["ts-src/package.json"]["status"] == (
        "legacy-node-package-no-product-caller"
    )
    assert entries["ts-src/src/index.ts"]["replacementEvidence"][0]["path"] == (
        "repoterm/main.py"
    )
    assert entries["ts-src/py-src"]["disposition"] == "archived-deleted"
    assert entries["ts-src/py-src"]["currentReferences"][0]["path"] == (
        "Docs/Documentation/engineering/material-burndown/ts-src-py-src.json"
    )
    assert not entries["ts-src/ARCHITECTURE_ZH.md"]["currentReferences"]

    usage_guide = (
        ROOT / "Docs" / "Documentation" / "USAGE_GUIDE.md"
    ).read_text(encoding="utf-8")
    assert "../ts-src/" not in usage_guide
    code_wiki = (
        ROOT / "Docs" / "Documentation" / "CODE_WIKI.md"
    ).read_text(encoding="utf-8")
    assert "engineering/material-inventory.json" in code_wiki
    assert "engineering/material-burndown/" in code_wiki

    for entry in manifest["entries"]:
        _assert_repo_or_optional_material_path(entry["path"])
        assert entry["disposition"] == "archived-deleted"
        for current in entry["currentReferences"]:
            assert current["reason"]
            _assert_repo_or_optional_material_path(current["path"])
        for evidence in entry["replacementEvidence"]:
            assert evidence["reason"]
            _assert_repo_path_exists(evidence["path"])


def test_repoterm_fork_burndown_manifest_tracks_comparison_boundary() -> None:
    manifest = _load_repo_json("Docs/Documentation/engineering/material-burndown/repoterm-fork.json")

    assert manifest["materialRoot"] == "RepoTerm-fork"
    assert manifest["summary"]["activeProductCallerCount"] == 0
    assert manifest["summary"]["typescriptSourceFileCount"] == 45
    assert manifest["summary"]["externalFileCountExcludingGit"] == 127
    assert manifest["archiveApproval"]["retainedInPlace"] is True
    assert manifest["dispositionPolicy"].startswith("Archive-approved")

    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["RepoTerm-fork/package.json"]["status"] == (
        "comparison-node-package-no-product-caller"
    )
    assert entries["RepoTerm-fork/src/index.ts"]["replacementEvidence"][0]["path"] == (
        "repoterm/main.py"
    )
    assert entries["RepoTerm-fork/external/RepoTerm"]["status"] == (
        "nested-external-reference"
    )

    for entry in manifest["entries"]:
        _assert_repo_or_optional_material_path(entry["path"])
        assert entry["disposition"] == "retained-reference"
        for current in entry["currentReferences"]:
            assert current["reason"]
            _assert_repo_or_optional_material_path(current["path"])
        for evidence in entry["replacementEvidence"]:
            assert evidence["reason"]
            _assert_repo_path_exists(evidence["path"])


def test_repoterm_main_work_burndown_manifest_tracks_parity_source_boundary() -> None:
    manifest = _load_repo_json("Docs/Documentation/engineering/material-burndown/repoterm-main-work.json")

    assert manifest["materialRoot"] == "RepoTerm-main-work"
    assert manifest["summary"]["activeProductCallerCount"] == 0
    assert manifest["summary"]["activeParityCallerCount"] == 0
    assert manifest["summary"]["migratedParityProvenanceCount"] == 1
    assert manifest["summary"]["testSourceFileCount"] == 21
    assert manifest["summary"]["externalFileCountExcludingGit"] == 1029
    assert manifest["archiveApproval"]["retainedInPlace"] is True
    assert manifest["dispositionPolicy"].startswith("Archive-approved")

    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["RepoTerm-main-work/package.json"]["status"] == (
        "comparison-node-package-no-product-caller"
    )
    parity_entry = entries["RepoTerm-main-work/test/input-parser.test.ts"]
    assert parity_entry["status"] == "parity-source-provenance-migrated"
    assert parity_entry["disposition"] == "retained-reference"
    assert not parity_entry["currentReferences"]
    assert {
        evidence["path"] for evidence in parity_entry["replacementEvidence"]
    } == {
        "tests/test_ts_ported.py",
        "Docs/Documentation/engineering/ts-parity-provenance.json",
    }

    provenance = _load_repo_json("Docs/Documentation/engineering/ts-parity-provenance.json")
    assert provenance["pythonTestPath"] == "tests/test_ts_ported.py"
    assert len(provenance["portedScenarios"]) == 5
    ts_ported = (ROOT / "tests" / "test_ts_ported.py").read_text(encoding="utf-8")
    assert "RepoTerm-main-work" not in ts_ported

    for entry in manifest["entries"]:
        _assert_repo_or_optional_material_path(entry["path"])
        assert entry["disposition"] == "retained-reference"
        for current in entry["currentReferences"]:
            assert current["reason"]
            _assert_repo_or_optional_material_path(current["path"])
        for evidence in entry["replacementEvidence"]:
            assert evidence["reason"]
            _assert_repo_path_exists(evidence["path"])
