"""End-to-end checks for the SQLite-backed memory contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoterm.memory import (
    EvidenceKind,
    EvidenceLevel,
    MemoryService,
    Scope,
    Status,
    VerificationEvidence,
)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


class TestCrossSessionMemoryContinuity:
    """Memory survives service/session boundaries through one SQLite file."""

    def test_memory_survives_session_close_and_reopen(self, tmp_workspace: Path) -> None:
        db_path = tmp_workspace / "memory.sqlite3"
        first = MemoryService(db_path=db_path, workspace=tmp_workspace)
        first.remember_explicit(
            "Backend uses FastAPI with async handlers",
            scope=Scope.PROJECT,
            key="backend_framework",
        )
        first.remember_explicit(
            "Use Redis for the caching layer",
            scope=Scope.PROJECT,
            key="cache_backend",
        )

        del first

        second = MemoryService(db_path=db_path, workspace=tmp_workspace)
        project_entries = second.store.list_entries(scopes=(Scope.PROJECT,))
        assert db_path.exists()
        assert len(project_entries) == 2
        assert all(entry.status is Status.ACTIVE for entry in project_entries)
        assert any("FastAPI" in entry.content for entry in project_entries)
        assert any("Redis" in entry.content for entry in project_entries)

    def test_verified_experience_is_pending_until_approval(self, tmp_workspace: Path) -> None:
        service = MemoryService(
            db_path=tmp_workspace / "memory.sqlite3",
            workspace=tmp_workspace,
        )
        evidence = VerificationEvidence.create(
            level=EvidenceLevel.VALIDATION,
            kind=EvidenceKind.TEST,
            tool_name="pytest",
            ok=True,
            summary="1 passed",
            source_session_id="session-e2e",
            source_turn_id="turn-e2e",
        )
        candidate = service.propose_experience(
            "Applicable condition: a repository change needs regression coverage.\n"
            "Effective action: run the focused pytest command.\n"
            "Verification result: the focused test passed.",
            evidence=(evidence,),
            source_session_id="session-e2e",
            source_turn_id="turn-e2e",
        )

        assert candidate.status is Status.PENDING
        assert service.search("focused pytest") == []
        approved = service.approve(candidate.id)
        assert approved.status is Status.ACTIVE
        assert service.search("focused pytest")
