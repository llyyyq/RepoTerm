"""Data model for RepoTerm's persistent memory subsystem.

The model deliberately contains only durable, user-meaningful memory data.
Session transcript details, tool output, usage counters, tiers, and related
graphs are intentionally not part of the new persistence model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class Scope(str, Enum):
    """The three durable memory scopes."""

    GLOBAL = "global"
    PROJECT = "project"
    BRANCH = "branch"

    # Short-lived compatibility aliases.  They intentionally map to the new
    # semantics instead of preserving a second USER/LOCAL storage model.
    USER = "global"
    LOCAL = "project"


class Kind(str, Enum):
    """The small, auditable set of memory meanings."""

    PREFERENCE = "preference"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    LESSON = "lesson"


class Status(str, Enum):
    """Memory lifecycle states."""

    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class SourceType(str, Enum):
    """How a memory entered the system."""

    EXPLICIT_USER = "explicit_user"
    VERIFIED_TASK = "verified_task"
    MIGRATION = "migration"


class EvidenceLevel(str, Enum):
    """How strongly a bounded evidence digest supports a memory candidate."""

    OBSERVATION = "observation"
    CHANGE = "change"
    VALIDATION = "validation"
    CONFIRMATION = "confirmation"


class EvidenceKind(str, Enum):
    """The semantic category of a tool result or user confirmation."""

    READ_FILE = "read_file"
    SEARCH = "search"
    LIST_FILES = "list_files"
    SHELL = "shell"
    EDIT_FILE = "edit_file"
    DIFF = "diff"
    CHECKPOINT = "checkpoint"
    TEST = "test"
    BUILD = "build"
    STATIC_CHECK = "static_check"
    SCHEMA = "schema"
    FILE_ASSERT = "file_assert"
    RECOVERY = "recovery"
    USER = "user"


class WriteDecision(str, Enum):
    """The only decisions allowed before a durable candidate is written."""

    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    DELETE = "delete"
    NOOP = "noop"


class MemoryTier(str, Enum):
    """Compatibility-only enum for callers of the removed tiered system."""

    WORKING = "working"
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    ARCHIVAL = "archival"


_SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"(?i)\b(?:api[_ -]?key|auth[_ -]?token|access[_ -]?token|password|passwd|cookie|secret)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b"),
)


def contains_sensitive_text(value: object) -> bool:
    """Return whether a value resembles a credential without logging it."""

    text = str(value or "")
    return any(pattern.search(text) for pattern in _SENSITIVE_TEXT_PATTERNS)


def sanitize_evidence_summary(value: object, *, limit: int = 240) -> str:
    """Produce a bounded evidence digest without retaining secret-looking text."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[: max(0, int(limit))]


def _normalize_evidence_input(value: object) -> str:
    """Build a bounded fingerprint input without storing raw tool input."""

    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    return sanitize_evidence_summary(rendered, limit=512)


def evidence_fingerprint(
    *,
    level: EvidenceLevel | str,
    kind: EvidenceKind | str,
    tool_name: str,
    ok: bool,
    summary: str,
    tool_input: object = None,
) -> str:
    """Hash normalized evidence metadata without persisting raw tool output."""

    payload = "\x1f".join(
        (
            EvidenceLevel(level).value,
            EvidenceKind(kind).value,
            str(tool_name or "").strip().lower(),
            "1" if bool(ok) else "0",
            sanitize_evidence_summary(summary),
            _normalize_evidence_input(tool_input),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """A bounded digest that can support a pending system experience.

    Raw stdout, stderr, diffs, source code and transcript payloads remain in
    Trace/Transcript.  The source ids are the stable references back to those
    authoritative records.
    """

    level: EvidenceLevel
    kind: EvidenceKind
    tool_name: str
    ok: bool
    summary: str
    fingerprint: str = ""
    source_session_id: str | None = None
    source_turn_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        level = self.level if isinstance(self.level, EvidenceLevel) else EvidenceLevel(str(self.level))
        kind = self.kind if isinstance(self.kind, EvidenceKind) else EvidenceKind(str(self.kind))
        tool_name = str(self.tool_name or "").strip().lower() or "unknown"
        summary = sanitize_evidence_summary(self.summary)
        session_id = str(self.source_session_id).strip() if self.source_session_id not in (None, "") else None
        turn_id = str(self.source_turn_id).strip() if self.source_turn_id not in (None, "") else None
        fingerprint = str(self.fingerprint or "").strip() or evidence_fingerprint(
            level=level,
            kind=kind,
            tool_name=tool_name,
            ok=bool(self.ok),
            summary=summary,
        )
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "source_session_id", session_id)
        object.__setattr__(self, "source_turn_id", turn_id)
        object.__setattr__(self, "created_at", float(self.created_at))

    @classmethod
    def create(
        cls,
        *,
        level: EvidenceLevel | str,
        kind: EvidenceKind | str,
        tool_name: str,
        ok: bool,
        summary: str,
        tool_input: object = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        created_at: float | None = None,
    ) -> "VerificationEvidence":
        normalized_level = EvidenceLevel(level)
        normalized_kind = EvidenceKind(kind)
        bounded_summary = sanitize_evidence_summary(summary)
        return cls(
            level=normalized_level,
            kind=normalized_kind,
            tool_name=tool_name,
            ok=ok,
            summary=bounded_summary,
            fingerprint=evidence_fingerprint(
                level=normalized_level,
                kind=normalized_kind,
                tool_name=tool_name,
                ok=ok,
                summary=bounded_summary,
                tool_input=tool_input,
            ),
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            created_at=time.time() if created_at is None else created_at,
        )

    @property
    def supports_experience(self) -> bool:
        return self.ok and self.level in {EvidenceLevel.VALIDATION, EvidenceLevel.CONFIRMATION}

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "kind": self.kind.value,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "summary": self.summary,
            "fingerprint": self.fingerprint,
            "source_session_id": self.source_session_id,
            "source_turn_id": self.source_turn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerificationEvidence":
        return cls(
            level=EvidenceLevel(str(value.get("level", EvidenceLevel.OBSERVATION.value))),
            kind=EvidenceKind(str(value.get("kind", EvidenceKind.SHELL.value))),
            tool_name=str(value.get("tool_name", "unknown")),
            ok=bool(value.get("ok", False)),
            summary=str(value.get("summary", "")),
            fingerprint=str(value.get("fingerprint", "")),
            source_session_id=value.get("source_session_id"),
            source_turn_id=value.get("source_turn_id"),
            created_at=float(value.get("created_at", time.time())),
        )


def normalize_content(content: str) -> str:
    """Return the canonical text used for exact duplicate detection."""

    if not isinstance(content, str):
        content = "" if content is None else str(content)
    normalized = unicodedata.normalize("NFKC", content).strip()
    return re.sub(r"\s+", " ", normalized)


def content_hash(content: str) -> str:
    """Hash canonical content without exposing the content itself."""

    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def project_key_for(workspace: str | Path) -> str:
    """Create the first-version stable key for an absolute workspace path."""

    resolved = Path(workspace).expanduser().resolve(strict=False)
    normalized = os.path.normcase(str(resolved))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def new_memory_id() -> str:
    """Generate the UUID identifier used by new records."""

    return str(uuid.uuid4())


def migration_memory_id(source_path: str, source_id: str, index: int, content: str) -> str:
    """Generate a stable UUID for an imported legacy record."""

    seed = "\x1f".join((source_path, source_id, str(index), normalize_content(content)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


@dataclass(frozen=True)
class MemoryContext:
    """Runtime context used to isolate project and branch memories."""

    project_key: str | None = None
    branch_name: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "MemoryContext":
        if not value:
            return cls()
        project = value.get("project_key", value.get("projectKey"))
        branch = value.get("branch_name", value.get("branchName"))
        return cls(
            project_key=str(project).strip() if project not in (None, "") else None,
            branch_name=str(branch).strip() if branch not in (None, "") else None,
        )


@dataclass
class MemoryEntry:
    """One durable memory record.

    ``category``, ``tags``, ``domains`` and the ``tier`` field are accepted as
    compatibility inputs for the old public import.  They are not persisted by
    the new SQLite schema and do not participate in retrieval.
    """

    id: str = field(default_factory=new_memory_id)
    scope: Scope = Scope.PROJECT
    kind: Kind = Kind.LESSON
    content: str = ""
    key: str | None = None
    status: Status = Status.ACTIVE
    project_key: str | None = None
    branch_name: str | None = None
    source_type: SourceType = SourceType.EXPLICIT_USER
    source_session_id: str | None = None
    source_turn_id: str | None = None
    version: int = 1
    supersedes_id: str | None = None
    content_hash: str = ""
    signal_count: int = 1
    evidence: tuple[VerificationEvidence, ...] = field(default_factory=tuple)
    created_at: float = field(default_factory=lambda: __import__("time").time())
    updated_at: float = field(default_factory=lambda: __import__("time").time())
    expires_at: float | None = None

    # Compatibility surface for the old in-memory tests/callers.
    category: str | None = field(default=None, repr=False, compare=False)
    tags: list[str] = field(default_factory=list, repr=False, compare=False)
    domains: list[str] = field(default_factory=list, repr=False, compare=False)
    tier: MemoryTier = field(default=MemoryTier.SHORT_TERM, repr=False, compare=False)
    usage_count: int = field(default=0, repr=False, compare=False)
    last_accessed: float | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.scope = coerce_scope(self.scope)
        self.status = coerce_status(self.status)
        self.source_type = coerce_source_type(self.source_type)
        if self.category and self.kind == Kind.LESSON:
            self.kind = kind_from_legacy_category(self.category)
        else:
            self.kind = coerce_kind(self.kind)
        if not isinstance(self.content, str):
            self.content = "" if self.content is None else str(self.content)
        self.content = self.content.strip()
        if not self.content:
            raise ValueError("memory content must not be empty")
        if not isinstance(self.id, str) or not self.id.strip():
            self.id = new_memory_id()
        self.id = self.id.strip()
        if self.key is not None:
            self.key = str(self.key).strip() or None
        self.content_hash = self.content_hash or content_hash(self.content)
        self.version = max(1, int(self.version))
        self.signal_count = max(1, int(self.signal_count))
        self.evidence = tuple(
            item if isinstance(item, VerificationEvidence) else VerificationEvidence.from_dict(item)
            for item in tuple(self.evidence or ())
            if isinstance(item, VerificationEvidence) or isinstance(item, Mapping)
        )[:3]
        self.created_at = float(self.created_at)
        self.updated_at = float(self.updated_at)
        self._validate_scope_context()

    def _validate_scope_context(self) -> None:
        if self.scope == Scope.GLOBAL:
            self.project_key = None
            self.branch_name = None
        elif self.scope == Scope.PROJECT:
            if not self.project_key:
                raise ValueError("project memory requires project_key")
            self.branch_name = None
        elif self.scope == Scope.BRANCH:
            if not self.project_key or not self.branch_name:
                raise ValueError("branch memory requires project_key and branch_name")

    @property
    def category_name(self) -> str:
        """Return a legacy category-like display value."""

        return self.category or self.kind.value

    def to_dict(self) -> dict[str, Any]:
        """Serialize only the new core model fields."""

        return {
            "id": self.id,
            "scope": self.scope.value,
            "kind": self.kind.value,
            "key": self.key,
            "content": self.content,
            "status": self.status.value,
            "project_key": self.project_key,
            "branch_name": self.branch_name,
            "source_type": self.source_type.value,
            "source_session_id": self.source_session_id,
            "source_turn_id": self.source_turn_id,
            "version": self.version,
            "supersedes_id": self.supersedes_id,
            "content_hash": self.content_hash,
            "signal_count": self.signal_count,
            "evidence": [item.to_dict() for item in self.evidence],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryEntry":
        """Build a record from the new format or a legacy-shaped dictionary."""

        category = data.get("category")
        kind = data.get("kind", category or Kind.LESSON.value)
        return cls(
            id=str(data.get("id") or new_memory_id()),
            scope=coerce_scope(data.get("scope", Scope.PROJECT.value)),
            kind=kind,
            key=data.get("key"),
            content=data.get("content", ""),
            status=data.get("status", Status.ACTIVE.value),
            project_key=data.get("project_key"),
            branch_name=data.get("branch_name"),
            source_type=data.get("source_type", SourceType.MIGRATION.value),
            source_session_id=data.get("source_session_id"),
            source_turn_id=data.get("source_turn_id"),
            version=data.get("version", 1),
            supersedes_id=data.get("supersedes_id"),
            content_hash=data.get("content_hash", ""),
            signal_count=data.get("signal_count", 1),
            evidence=tuple(data.get("evidence", ()) if isinstance(data.get("evidence", ()), (list, tuple)) else ()),
            created_at=data.get("created_at", __import__("time").time()),
            updated_at=data.get("updated_at", __import__("time").time()),
            expires_at=data.get("expires_at"),
            category=str(category) if category else None,
            tags=list(data.get("tags", [])) if isinstance(data.get("tags", []), list) else [],
            domains=list(data.get("domains", [])) if isinstance(data.get("domains", []), list) else [],
        )


def coerce_scope(value: Scope | str) -> Scope:
    value = value.value if isinstance(value, Scope) else str(value).strip().lower()
    aliases = {"user": Scope.GLOBAL.value, "local": Scope.PROJECT.value}
    return Scope(aliases.get(value, value))


def coerce_kind(value: Kind | str) -> Kind:
    value = value.value if isinstance(value, Kind) else str(value).strip().lower()
    return Kind(value)


def coerce_status(value: Status | str) -> Status:
    value = value.value if isinstance(value, Status) else str(value).strip().lower()
    return Status(value)


def coerce_source_type(value: SourceType | str) -> SourceType:
    value = value.value if isinstance(value, SourceType) else str(value).strip().lower()
    return SourceType(value)


def kind_from_legacy_category(category: str | None) -> Kind:
    """Map old free-form categories into the bounded new kind vocabulary."""

    normalized = str(category or "").strip().lower()
    if normalized in {"preference", "directive", "user_preference", "style"}:
        return Kind.PREFERENCE
    if normalized in {"decision", "architecture", "design", "pattern", "code-pattern"}:
        return Kind.DECISION
    if normalized in {"constraint", "convention", "configuration", "security", "workflow"}:
        return Kind.CONSTRAINT
    return Kind.LESSON


def context_for_workspace(workspace: str | Path | None) -> MemoryContext:
    """Build project context from a workspace without probing Git."""

    if workspace is None:
        return MemoryContext()
    return MemoryContext(project_key=project_key_for(workspace))
