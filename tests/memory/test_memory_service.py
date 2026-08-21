from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from repoterm.memory import (
    BranchScopeError,
    EvidenceKind,
    EvidenceLevel,
    Kind,
    MemoryConfirmationRequired,
    MemoryConflictError,
    MemoryContext,
    MemoryEvidenceRequired,
    MemoryService,
    Scope,
    SensitiveMemoryError,
    Status,
    VerificationEvidence,
    WriteDecision,
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


def experience_content(condition: str = "a verified project change") -> str:
    return (
        f"Applicable condition: {condition}.\n"
        "Effective action: run the targeted pytest command.\n"
        "Verification result: pytest completed successfully."
    )


def verification_evidence(
    *,
    session_id: str = "session-1",
    turn_id: str = "turn-1",
    summary: str = "pytest: 1 passed",
) -> VerificationEvidence:
    return VerificationEvidence.create(
        level=EvidenceLevel.VALIDATION,
        kind=EvidenceKind.TEST,
        tool_name="pytest",
        ok=True,
        summary=summary,
        source_session_id=session_id,
        source_turn_id=turn_id,
    )


def propose_experience(
    service: MemoryService,
    *,
    session_id: str = "session-1",
    turn_id: str = "turn-1",
    content: str | None = None,
    key: str | None = "test_command",
    evidence: tuple[VerificationEvidence, ...] | None = None,
):
    evidence = evidence or (verification_evidence(session_id=session_id, turn_id=turn_id),)
    return service.propose_experience(
        content or experience_content(),
        key=key,
        evidence=evidence,
    )


def test_pending_is_not_searchable_or_injected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    candidate = propose_experience(service)

    assert candidate.status == Status.PENDING
    assert service.search("pytest") == []
    assert service.build_prompt_context("pytest") == ""


def test_approve_makes_candidate_searchable_and_injectable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    candidate = propose_experience(service)
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


def test_migration_rejects_a_sensitive_source_without_marker_and_allows_retry(tmp_path: Path) -> None:
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
    raw = source.read_bytes()
    marker = _marker_key(source, __import__("hashlib").sha256(raw).hexdigest())

    assert report.imported == 0
    assert report.skipped == 1
    assert report.markers_written == 0
    assert service.store.get_metadata(marker) is None
    assert service.search("pytest") == []
    assert service.search("sk-test-secret-value") == []
    assert service.stats()[Scope.PROJECT.value][Status.ACTIVE.value] == 0

    source.write_text(
        json.dumps({"scope": "project", "entries": [{"id": "valid", "content": "Use pytest for tests"}]}),
        encoding="utf-8",
    )
    retried = service.migrate_legacy(workspace=workspace, home=home)
    assert retried.imported == 1
    assert retried.markers_written == 1
    assert service.search("pytest")


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


def test_propose_experience_rejects_boolean_or_observation_only_evidence(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(MemoryEvidenceRequired):
        service.propose(experience_content(), verified=True)

    observation = VerificationEvidence.create(
        level=EvidenceLevel.OBSERVATION,
        kind=EvidenceKind.READ_FILE,
        tool_name="read_file",
        ok=True,
        summary="read a file",
        source_session_id="session-1",
        source_turn_id="turn-1",
    )
    with pytest.raises(MemoryEvidenceRequired):
        propose_experience(service, evidence=(observation,))
    assert service.list_pending() == []


def test_experience_evidence_is_bounded_redacted_and_survives_restart(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    evidence = (
        VerificationEvidence.create(
            level=EvidenceLevel.OBSERVATION,
            kind=EvidenceKind.SEARCH,
            tool_name="rg",
            ok=True,
            summary="found the target test",
            source_session_id="session-2",
            source_turn_id="turn-2",
        ),
        verification_evidence(
            session_id="session-2",
            turn_id="turn-2",
            summary="pytest: 1 passed api_key=sk-1234567890abcdefghi",
        ),
        VerificationEvidence.create(
            level=EvidenceLevel.CONFIRMATION,
            kind=EvidenceKind.USER,
            tool_name="user",
            ok=True,
            summary="user confirmed the workflow",
            source_session_id="session-2",
            source_turn_id="turn-2",
        ),
    )

    candidate = propose_experience(
        service,
        session_id="session-2",
        turn_id="turn-2",
        evidence=evidence,
    )
    restored = MemoryService(db_path=service.db_path, workspace=tmp_path / "workspace").store.get(candidate.id)

    assert restored is not None
    assert len(restored.evidence) == 3
    assert all(len(item.summary) <= 240 for item in restored.evidence)
    assert "sk-1234567890abcdefghi" not in restored.evidence[1].summary
    assert "[redacted]" in restored.evidence[1].summary
    assert restored.evidence[1].fingerprint == evidence[1].fingerprint


def test_one_experience_candidate_per_turn_and_changed_body_clears_old_evidence(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    candidate = propose_experience(service, session_id="session-3", turn_id="turn-3")
    duplicate_turn = propose_experience(
        service,
        session_id="session-3",
        turn_id="turn-3",
        content=experience_content("a different but same-turn conclusion"),
    )

    assert duplicate_turn.id == candidate.id
    assert service.last_write_decision is WriteDecision.NOOP
    assert len(service.list_pending()) == 1

    active = service.approve(candidate.id)
    updated = service.update(active.id, "User-confirmed replacement lesson")
    assert updated.evidence == ()
    assert updated.source_session_id is None
    assert updated.source_turn_id is None


def test_duplicate_is_noop_and_accumulates_signal_count(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.remember_explicit("Use pytest -q", key="test-command")
    duplicate = service.remember_explicit(" Use   pytest -q ", key="ignored")

    assert duplicate.id == first.id
    assert duplicate.signal_count == 2
    assert service.last_write_decision is WriteDecision.NOOP


def test_implicit_preference_requires_two_distinct_signals_and_stays_pending(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.observe_implicit_preference(
        "Prefer concise Chinese responses",
        source_session_id="session-a",
        source_turn_id="turn-a",
    )
    same_turn = service.observe_implicit_preference(
        "Prefer concise Chinese responses",
        source_session_id="session-a",
        source_turn_id="turn-a",
    )
    second = service.observe_implicit_preference(
        "Prefer concise Chinese responses",
        source_session_id="session-b",
        source_turn_id="turn-b",
    )
    third = service.observe_implicit_preference(
        "Prefer concise Chinese responses",
        source_session_id="session-c",
        source_turn_id="turn-c",
    )

    assert first is None and same_turn is None
    assert second is not None and second.status is Status.PENDING
    assert third is not None and third.id == second.id
    assert third.signal_count == 3
    assert len(service.list_pending()) == 1


def test_implicit_preference_conflict_keeps_one_latest_pending_candidate(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    common = {
        "scope": Scope.GLOBAL,
        "key": "preferences.verbosity",
    }

    assert service.observe_implicit_preference(
        "preferences.verbosity = concise",
        source_session_id="session-concise-a",
        source_turn_id="turn-concise-a",
        **common,
    ) is None
    concise = service.observe_implicit_preference(
        "preferences.verbosity = concise",
        source_session_id="session-concise-b",
        source_turn_id="turn-concise-b",
        **common,
    )
    assert concise is not None and concise.status is Status.PENDING

    assert service.observe_implicit_preference(
        "preferences.verbosity = detailed",
        source_session_id="session-detailed-a",
        source_turn_id="turn-detailed-a",
        **common,
    ) is None
    detailed = service.observe_implicit_preference(
        "preferences.verbosity = detailed",
        source_session_id="session-detailed-b",
        source_turn_id="turn-detailed-b",
        **common,
    )

    assert detailed is not None and detailed.status is Status.PENDING
    assert detailed.content.endswith("detailed")
    assert detailed.signal_count == 2
    assert service.store.get(concise.id).status is Status.SUPERSEDED
    pending = service.list_pending()
    assert len(pending) == 1
    assert pending[0].id == detailed.id


def test_implicit_preference_pending_does_not_replace_active_value(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    active = service.remember_explicit(
        "preferences.verbosity = normal",
        scope=Scope.GLOBAL,
        kind=Kind.PREFERENCE,
        key="preferences.verbosity",
    )

    for session_id, turn_id in (
        ("session-a", "turn-a"),
        ("session-b", "turn-b"),
    ):
        service.observe_implicit_preference(
            "preferences.verbosity = concise",
            scope=Scope.GLOBAL,
            key="preferences.verbosity",
            source_session_id=session_id,
            source_turn_id=turn_id,
        )

    assert service.store.get(active.id).status is Status.ACTIVE
    pending = service.list_pending()
    assert len(pending) == 1
    assert pending[0].content.endswith("concise")


def test_memory_manager_with_workspace_uses_production_database(monkeypatch, tmp_path: Path) -> None:
    import repoterm.memory as memory_module
    from repoterm.memory import MemoryManager

    production_dir = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(memory_module, "REPOTERM_DIR", production_dir)

    manager = MemoryManager(project_root=workspace)

    assert manager.db_path == production_dir / "memory.sqlite3"
    assert not (workspace / ".repoterm-memory" / "memory.sqlite3").exists()


def test_implicit_preference_signal_parser_is_conservative() -> None:
    from repoterm.memory import extract_implicit_preference_signal

    assert extract_implicit_preference_signal("I prefer concise responses") == (
        "preferences.verbosity",
        "preferences.verbosity = concise",
    )
    assert extract_implicit_preference_signal("以后请使用中文回答") == (
        "preferences.language",
        "preferences.language = Chinese",
    )
    assert extract_implicit_preference_signal("Please remember to use Chinese") is None
    assert extract_implicit_preference_signal("Please fix the failing test") is None


def test_v1_database_is_upgraded_to_v2_atomically(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES('schema_version', '1');
            CREATE TABLE memory_entries (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                kind TEXT NOT NULL,
                key TEXT,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                project_key TEXT,
                branch_name TEXT,
                source_type TEXT NOT NULL,
                source_session_id TEXT,
                source_turn_id TEXT,
                version INTEGER NOT NULL,
                supersedes_id TEXT,
                content_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO memory_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-row", "project", "lesson", None, "legacy pytest rule", "active",
                project_key_for(tmp_path / "workspace"), None, "migration", None, None,
                1, None, "legacy-hash", 1.0, 1.0, None,
            ),
        )

    store = MemoryStore(db_path)
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_entries)")}

    upgraded = store.get("legacy-row")
    assert {"signal_count", "evidence_json"} <= columns
    assert store.get_metadata("schema_version") == "2"
    assert upgraded is not None and upgraded.signal_count == 1 and upgraded.evidence == ()
