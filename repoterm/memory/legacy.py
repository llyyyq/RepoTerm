"""Non-destructive, idempotent migration from RepoTerm's old files."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Kind,
    MemoryEntry,
    Scope,
    SourceType,
    Status,
    coerce_scope,
    kind_from_legacy_category,
    migration_memory_id,
    project_key_for,
)
from .service import looks_like_sensitive_content


@dataclass
class MigrationReport:
    """Counts and safe diagnostics for one migration run."""

    imported: int = 0
    deduplicated: int = 0
    skipped: int = 0
    legacy_local: int = 0
    markers_written: int = 0
    errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "deduplicated": self.deduplicated,
            "skipped": self.skipped,
            "legacy_local": self.legacy_local,
            "markers_written": self.markers_written,
            "errors": list(self.errors),
            "sources": list(self.sources),
        }

    def __str__(self) -> str:
        return (
            "MigrationReport("
            f"imported={self.imported}, deduplicated={self.deduplicated}, "
            f"skipped={self.skipped}, errors={len(self.errors)})"
        )


def migrate_legacy(
    service,
    *,
    workspace: str | Path | None = None,
    home: str | Path | None = None,
) -> MigrationReport:
    """Import listed legacy sources without changing any source file."""

    workspace_path = Path(workspace).expanduser().resolve(strict=False) if workspace else None
    home_path = Path(home).expanduser().resolve(strict=False) if home else Path.home()
    report = MigrationReport()

    sources: list[tuple[Path, str, Path | None]] = [
        (home_path / ".repoterm" / "memory" / "memory.json", "global", None),
        (home_path / ".repoterm" / "USER.md", "user-md", None),
    ]
    if workspace_path is not None:
        sources.extend(
            [
                (workspace_path / ".repoterm-memory" / "memory.json", "project", workspace_path),
                (workspace_path / ".repoterm-memory-local" / "memory.json", "local", workspace_path),
                (workspace_path / ".repoterm" / "USER.md", "user-md", workspace_path),
            ]
        )

    for path, source_kind, source_workspace in sources:
        if not path.exists() or not path.is_file():
            continue
        report.sources.append(str(path))
        raw = _read_bytes(path, report)
        if raw is None:
            continue
        # A legacy source is an indivisible migration unit.  If it contains a
        # credential-looking value anywhere, import none of its records and do
        # not write its marker so the user can remediate and retry later.
        if looks_like_sensitive_content(raw.decode("utf-8", errors="replace")):
            report.skipped += 1
            report.errors.append(f"{path}: sensitive source was not imported")
            continue
        source_hash = hashlib.sha256(raw).hexdigest()
        marker = _marker_key(path, source_hash)
        if service.store.get_metadata(marker) is not None:
            continue

        if source_kind == "user-md":
            entries = _parse_user_md(raw.decode("utf-8", errors="replace"), path, report)
        else:
            entries = _parse_json(raw, path, source_kind, source_workspace, report)

        # Parsing can construct records from sources with mixed encodings or
        # legacy shapes.  Keep a second structured check as a defence in
        # depth; one sensitive record still rejects the whole source.
        if any(looks_like_sensitive_content(entry.content) for entry, _ in entries):
            report.skipped += max(1, len(entries))
            report.errors.append(f"{path}: sensitive source was not imported")
            continue
        safe_entries = entries

        try:
            results = service._import_migrated_batch(
                (entry for entry, _ in safe_entries),
                marker=marker,
                source_hash=source_hash,
            )
        except Exception:
            # Never include content in diagnostics; the source remains
            # untouched and a later run can retry this source.  Since the
            # marker is part of the same transaction, no partial import is
            # considered complete.
            report.errors.append(f"{path}: failed to import source transaction")
            continue

        for (entry, is_local), (inserted, result) in zip(safe_entries, results):
            if inserted:
                report.imported += 1
            elif result == "duplicate":
                report.deduplicated += 1
            if is_local:
                report.legacy_local += 1
        report.markers_written += 1

    return report


def _read_bytes(path: Path, report: MigrationReport) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        report.errors.append(f"{path}: source could not be read")
        return None


def _marker_key(path: Path, source_hash: str) -> str:
    return f"migration:{path.resolve(strict=False)}:{source_hash}"


def _parse_json(
    raw: bytes,
    path: Path,
    source_kind: str,
    source_workspace: Path | None,
    report: MigrationReport,
) -> list[tuple[MemoryEntry, bool]]:
    try:
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        report.errors.append(f"{path}: invalid JSON source")
        return []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("entries"), list):
        report.errors.append(f"{path}: entries list is missing")
        return []

    result: list[tuple[MemoryEntry, bool]] = []
    for index, raw_entry in enumerate(parsed["entries"]):
        entry, is_local, error = _convert_legacy_entry(
            raw_entry,
            index=index,
            path=path,
            source_kind=source_kind,
            source_workspace=source_workspace,
        )
        if error:
            report.skipped += 1
            report.errors.append(f"{path}: entry {index} skipped ({error})")
        elif entry is not None:
            result.append((entry, is_local))
    return result


def _convert_legacy_entry(
    raw_entry: Any,
    *,
    index: int,
    path: Path,
    source_kind: str,
    source_workspace: Path | None,
) -> tuple[MemoryEntry | None, bool, str | None]:
    if not isinstance(raw_entry, dict):
        return None, False, "record is not an object"
    content = raw_entry.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, source_kind == "local", "content is empty or invalid"
    scope_value = raw_entry.get("scope", source_kind)
    try:
        scope = coerce_scope(scope_value)
    except ValueError:
        return None, False, "scope is invalid"
    if source_kind == "global":
        scope = Scope.GLOBAL
    if scope in {Scope.PROJECT, Scope.BRANCH} and source_workspace is None:
        return None, False, "project context is unavailable"
    if scope == Scope.BRANCH:
        # Legacy LOCAL/PROJECT files do not contain trustworthy branch identity.
        return None, False, "legacy source cannot establish branch identity"
    project_key = project_key_for(source_workspace) if scope == Scope.PROJECT and source_workspace else None
    category = raw_entry.get("kind", raw_entry.get("category", "lesson"))
    try:
        kind = kind_from_legacy_category(str(category))
    except (TypeError, ValueError):
        kind = Kind.LESSON
    old_id = str(raw_entry.get("id") or "")
    try:
        status = Status(str(raw_entry.get("status", Status.ACTIVE.value)).lower())
    except ValueError:
        status = Status.ACTIVE
    try:
        created_at = float(raw_entry.get("created_at", 0) or 0)
    except (TypeError, ValueError):
        created_at = 0.0
    try:
        updated_at = float(raw_entry.get("updated_at", created_at) or created_at)
    except (TypeError, ValueError):
        updated_at = created_at
    now = __import__("time").time()
    created_at = created_at if created_at > 0 else now
    updated_at = updated_at if updated_at > 0 else created_at
    try:
        entry = MemoryEntry(
            id=migration_memory_id(str(path), old_id, index, content),
            scope=scope,
            kind=kind,
            key=raw_entry.get("key") if isinstance(raw_entry.get("key"), str) else None,
            content=content,
            status=status,
            project_key=project_key,
            source_type=SourceType.MIGRATION,
            version=1,
            created_at=created_at,
            updated_at=updated_at,
        )
    except (TypeError, ValueError):
        return None, source_kind == "local", "record failed validation"
    return entry, source_kind == "local", None


def _parse_user_md(
    content: str,
    path: Path,
    report: MigrationReport,
) -> list[tuple[MemoryEntry, bool]]:
    """Import only explicit key/value preference bullets from USER.md."""

    result: list[tuple[MemoryEntry, bool]] = []
    heading = ""
    for index, line in enumerate(content.splitlines()):
        heading_match = re.match(r"^##\s+(.+?)\s*$", line)
        if heading_match:
            heading = heading_match.group(1).strip().lower()
            continue
        match = re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+?)\s*$", line)
        if not match or not match.group(2).strip():
            continue
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip()
        entry = MemoryEntry(
            id=migration_memory_id(str(path), key, index, value),
            scope=Scope.GLOBAL,
            kind=Kind.PREFERENCE,
            key=f"preference.{key}",
            content=f"{key}: {value}",
            status=Status.ACTIVE,
            source_type=SourceType.MIGRATION,
        )
        result.append((entry, False))
    return result
