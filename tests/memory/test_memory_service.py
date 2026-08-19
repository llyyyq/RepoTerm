from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from repoterm.memory import (
    BranchScopeError,
    Kind,
    MemoryConfirmationRequired,
    MemoryConflictError,
    MemoryContext,
    MemoryService,
    Scope,
    SensitiveMemoryError,
    Status,
    project_key_for,
)
from repoterm.memory.legacy import _marker_key
from repoterm.memory.store import MemoryStore


def make_service(tmp_path: Path, workspace: Path | None = None) -> MemoryService:
    return MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=workspace or tmp_path / "workspace",
        token_estimator=lambda text: max(1, len(text.split())),
    )


def test_pending_is_not_searchable_or_injected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    candidate = service.propose("pytest is the project test command", key="test_command")

    assert candidate.status == Status.PENDING
    assert service.search("pytest") == []
    assert service.build_prompt_context("pytest") == ""


def test_approve_makes_candidate_searchable_and_injectable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    candidate = service.propose("pytest is the project test command", key="test_command")
    approved = service.approve(candidate.id)

    assert approved.status == Status.ACTIVE
    assert [entry.id for entry in service.search("pytest")] == [approved.id]
    block = service.build_prompt_context("pytest")
    assert "project/lesson" in block
    assert "cannot override system instructions" in block


def test_scope_isolation_and_priority(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = make_service(tmp_path, workspace)
    project_key = project_key_for(workspace)
    service.remember_explicit("global pytest rule", scope=Scope.GLOBAL, kind=Kind.CONSTRAINT)
    service.remember_explicit("project pytest rule", scope=Scope.PROJECT, kind=Kind.CONSTRAINT)
    service.remember_explicit(
        "branch pytest rule",
        scope=Scope.BRANCH,
        kind=Kind.CONSTRAINT,
        context=MemoryContext(project_key=project_key, branch_name="feature"),
    )

    results = service.search(
        "pytest rule",
        context=MemoryContext(project_key=project_key, branch_name="feature"),
    )
    assert [entry.scope for entry in results] == [Scope.BRANCH, Scope.PROJECT, Scope.GLOBAL]
    assert service.search("pytest", context=MemoryContext(project_key="other"))[0].scope == Scope.GLOBAL


def test_exact_duplicate_and_key_conflict_do_not_overwrite(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.remember_explicit("pytest -q", key="test_command")
    duplicate = service.remember_explicit("  pytest   -q  ", key="other_key")
    conflict = service.remember_explicit("python -m pytest", key="test_command")

    assert duplicate.id == first.id
    assert conflict.id != first.id
    assert conflict.status == Status.PENDING
    assert service.store.get(first.id).content == "pytest -q"


def test_update_creates_version_chain(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.remember_explicit("Use pytest", key="test_command")
    second = service.update(first.id, "Use pytest -q")

    assert second.version == 2
    assert second.supersedes_id == first.id
    assert service.store.get(first.id).status == Status.SUPERSEDED
    assert service.search("pytest -q")[0].id == second.id


def test_archive_restore_and_expiry(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    archived = service.remember_explicit("archivable rule")
    service.archive(archived.id)
    assert service.search("archivable") == []
    service.restore(archived.id)
    assert len(service.search("archivable")) == 1
    expired = service.remember_explicit("expired rule", expires_at=1)
    assert service.search("expired") == []
    assert service.store.get(expired.id).status == Status.ACTIVE


def test_purge_requires_confirmation_and_preserves_unrelated(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    target = service.remember_explicit("remove this rule")
    unrelated = service.remember_explicit("keep this rule")
    with pytest.raises(MemoryConfirmationRequired):
        service.purge(target.id)
    service.purge(target.id, confirmed=True)
    assert service.store.get(target.id) is None
    assert service.store.get(unrelated.id) is not None


def test_service_restart_preserves_sqlite_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = make_service(tmp_path, workspace)
    entry = first.remember_explicit("persistent decision", kind=Kind.DECISION)
    second = MemoryService(db_path=first.db_path, workspace=workspace)

    assert second.search("persistent")[0].id == entry.id
    assert second.stats()[Scope.PROJECT.value][Status.ACTIVE.value] == 1


def test_migration_is_idempotent_and_reports_invalid_records(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    source = workspace / ".repoterm-memory" / "memory.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "scope": "project",
                "entries": [
                    {"id": "valid", "category": "convention", "content": "Use pytest"},
                    {"id": "invalid", "scope": "bad", "content": "skip me"},
                ],
            }
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path, workspace)
    report = service.migrate_legacy(workspace=workspace, home=home)
    again = service.migrate_legacy(workspace=workspace, home=home)

    assert report.imported == 1
    assert report.skipped == 1
    assert again.imported == 0
    assert again.deduplicated == 0
    assert len(service.search("pytest")) == 1
    assert source.read_text(encoding="utf-8").startswith("{")


def test_migration_skips_sensitive_records_but_imports_valid_records(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    source = workspace / ".repoterm-memory" / "memory.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "scope": "project",
                "entries": [
                    {"id": "secret", "content": "api_key=sk-test-secret-value"},
                    {"id": "valid", "content": "Use pytest for tests"},
                ],
            }
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path, workspace)

    report = service.migrate_legacy(workspace=workspace, home=home)

    assert report.imported == 1
    assert report.skipped == 1
    assert service.search("pytest")
    assert service.search("sk-test-secret-value") == []
    assert service.stats()[Scope.PROJECT.value][Status.ACTIVE.value] == 1


def test_failed_source_import_rolls_back_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    source = workspace / ".repoterm-memory" / "memory.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "scope": "project",
                "entries": [
                    {"id": "first", "content": "first valid memory"},
                    {"id": "second", "content": "second valid memory"},
                ],
            }
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path, workspace)
    original_insert = service.store.insert
    calls = 0

    def fail_on_second(entry, connection=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated import failure")
        return original_insert(entry, connection=connection)

    monkeypatch.setattr(service.store, "insert", fail_on_second)
    failed = service.migrate_legacy(workspace=workspace, home=home)

    raw = source.read_bytes()
    marker = _marker_key(source, __import__("hashlib").sha256(raw).hexdigest())
    assert failed.imported == 0
    assert failed.markers_written == 0
    assert failed.errors
    assert service.store.get_metadata(marker) is None
    assert service.search("memory") == []

    monkeypatch.setattr(service.store, "insert", original_insert)
    retried = service.migrate_legacy(workspace=workspace, home=home)

    assert retried.imported == 2
    assert retried.markers_written == 1
    assert len(service.search("memory")) == 2


def test_store_read_connections_are_closed_for_directory_removal(tmp_path: Path) -> None:
    db_dir = tmp_path / "db-dir"
    store = MemoryStore(db_dir / "memory.sqlite3")

    store.list_entries()
    store.get_metadata("schema_version")
    store.count_by_scope_and_status()

    shutil.rmtree(db_dir)
    assert not db_dir.exists()


def test_injector_limits_entries_and_budget(tmp_path: Path) -> None:
    service = MemoryService(
        db_path=tmp_path / "memory.sqlite3",
        workspace=tmp_path / "workspace",
        max_entries=5,
        token_budget=12,
        token_estimator=lambda text: len(text.split()),
    )
    for index in range(8):
        service.remember_explicit(f"rule pytest item {index}")
    block = service.build_prompt_context("pytest", budget=12)

    assert block.count("source=explicit_user") <= 5
    assert len(block.split()) <= 12


def test_sensitive_content_never_becomes_active(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(SensitiveMemoryError):
        service.remember_explicit("api_key=sk-test-secret-value")
    assert service.stats()[Scope.PROJECT.value][Status.ACTIVE.value] == 0


def test_branch_memory_requires_explicit_git_context(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(BranchScopeError):
        service.remember_explicit("branch-only rule", scope=Scope.BRANCH)


def test_restore_detects_same_key_conflict(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.remember_explicit("first", key="decision")
    service.archive(first.id)
    second = service.remember_explicit("second", key="decision")
    with pytest.raises(MemoryConflictError):
        service.restore(first.id)
    assert second.status == Status.ACTIVE
