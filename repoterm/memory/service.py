"""Public lifecycle and retrieval service for persistent memory."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .injector import MemoryInjector, TokenEstimator
from .models import (
    Kind,
    MemoryContext,
    MemoryEntry,
    Scope,
    SourceType,
    Status,
    VerificationEvidence,
    WriteDecision,
    contains_sensitive_text,
    coerce_kind,
    coerce_scope,
    content_hash,
    context_for_workspace,
    kind_from_legacy_category,
    new_memory_id,
    project_key_for,
)
from .store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryServiceError(ValueError):
    """Base class for expected memory operation errors."""


class SensitiveMemoryError(MemoryServiceError):
    """Raised when content appears to contain a credential or secret."""


class BranchScopeError(MemoryServiceError):
    """Raised when a branch memory has no trustworthy Git context."""


class MemoryConflictError(MemoryServiceError):
    """Raised when a requested lifecycle transition would overwrite a key."""


class MemoryNotFoundError(MemoryServiceError):
    """Raised when an operation references an unknown record."""


class MemoryConfirmationRequired(MemoryServiceError):
    """Raised when purge is attempted without explicit confirmation."""


class MemoryEvidenceRequired(MemoryServiceError):
    """Raised when a system experience lacks successful verification evidence."""


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_EXPERIENCE_TEMPLATE_FIELDS = (
    ("Applicable condition:", "有效条件：", "适用条件："),
    ("Effective action:", "有效动作："),
    ("Verification result:", "验证结果："),
)
_GENERIC_EXPERIENCE_PATTERNS = (
    "task completed successfully",
    "used n tools",
    "consider alternative approaches",
    "本次任务已完成",
)

_IMPLICIT_PREFERENCE_LANGUAGE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:i\s+)?(?:prefer|like)\s+(?:to\s+)?(?:respond|reply|answer|write)\s+(?:in\s+)?"
    r"|\b(?:please\s+)?(?:always\s+)?(?:use|respond|reply|answer|write)\s+(?:in\s+)?"
    r"|(?:从现在开始|以后|请始终|请一直)\s*(?:请\s*)?(?:使用|用)?\s*"
    r"|(?:回答请(?:使用|用)?|我(?:更)?偏好|我喜欢)\s*(?:使用|用)?\s*"
    r")(?:\b(?P<value>chinese|english)\b|(?P<cjk_value>中文|英文))"
    r"(?:\s+(?:responses?|answers?|replies?)|回答|回复|响应)?(?=$|[.!?。！？])",
    re.IGNORECASE,
)
_IMPLICIT_PREFERENCE_STYLE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:i\s+)?(?:prefer|like)\s+"
    r"|\b(?:please\s+)?(?:be|keep\s+(?:responses?|answers?|replies?)\s+)\s*"
    r"|(?:我(?:更)?偏好|我喜欢|请保持|回答请)\s*"
    r")(?:\b(?P<value>concise|brief|short|detailed|verbose)\b|"
    r"(?P<cjk_value>简洁|简短|详细|精炼))"
    r"(?:\s+(?:responses?|answers?|replies?)|回答|回复|响应)?(?=$|[.!?。！？])",
    re.IGNORECASE,
)
_IMPLICIT_PREFERENCE_EXPLICIT_MARKER = re.compile(
    r"(?i)\b(?:remember|save|store|persist|memorize)\b|记住|记忆|保存|存储"
)


def looks_like_sensitive_content(content: str) -> bool:
    """Return whether content resembles a credential without logging it."""

    return contains_sensitive_text(content)


def extract_implicit_preference_signal(text: str) -> tuple[str, str] | None:
    """Extract one conservative, normalized preference signal from a Turn.

    This is intentionally an allow-list for communication preferences only.
    It does not infer project facts, tool choices, or lessons from arbitrary
    prose.  Explicit "remember/save" language is left to the explicit memory
    command path and therefore cannot create a second implicit signal.
    """

    normalized = " ".join(str(text or "").split())
    if not normalized or len(normalized) > 240:
        return None
    if _IMPLICIT_PREFERENCE_EXPLICIT_MARKER.search(normalized):
        return None

    language_match = _IMPLICIT_PREFERENCE_LANGUAGE_PATTERN.search(normalized)
    if language_match:
        raw_value = language_match.group("value") or language_match.group("cjk_value")
        canonical = {
            "chinese": "Chinese",
            "english": "English",
            "中文": "Chinese",
            "英文": "English",
        }[raw_value.lower()]
        return "preferences.language", f"preferences.language = {canonical}"

    style_match = _IMPLICIT_PREFERENCE_STYLE_PATTERN.search(normalized)
    if style_match:
        raw_value = style_match.group("value") or style_match.group("cjk_value")
        canonical = {
            "concise": "concise",
            "brief": "concise",
            "short": "concise",
            "detailed": "detailed",
            "verbose": "detailed",
            "简洁": "concise",
            "简短": "concise",
            "详细": "detailed",
            "精炼": "concise",
        }[raw_value.lower()]
        return "preferences.verbosity", f"preferences.verbosity = {canonical}"

    return None


def _is_reusable_experience(content: str) -> bool:
    """Reject generic reflections that cannot guide a future turn."""

    normalized = " ".join(str(content or "").split())
    lowered = normalized.lower()
    return (
        all(any(marker in normalized for marker in alternatives) for alternatives in _EXPERIENCE_TEMPLATE_FIELDS)
        and not any(pattern in lowered for pattern in _GENERIC_EXPERIENCE_PATTERNS)
    )


def _legacy_auto_category(content: str) -> str:
    """Map the old ``category='auto'`` call form to the new kind vocabulary."""

    text = str(content or "").lower()
    if any(term in text for term in ("pytest", "test", "testing", "测试")):
        return "testing"
    if any(term in text for term in ("function", "func", "method", "函数", "代码")):
        return "code-pattern"
    if any(term in text for term in ("security", "credential", "安全")):
        return "security"
    return "general"


def _git_branch(workspace: Path) -> str | None:
    """Return the current branch, or ``None`` outside a Git worktree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


def _as_context(
    value: MemoryContext | Mapping[str, Any] | None,
    *,
    fallback: MemoryContext,
) -> MemoryContext:
    if value is None:
        return fallback
    if isinstance(value, MemoryContext):
        return value
    return MemoryContext.from_mapping(value)


def _scope_priority(scope: Scope) -> int:
    return {Scope.BRANCH: 2, Scope.PROJECT: 1, Scope.GLOBAL: 0}[scope]


class MemoryService:
    """The only business-facing entry point for persistent memory."""

    def __init__(
        self,
        *,
        store: MemoryStore | None = None,
        db_path: str | Path | None = None,
        workspace: str | Path | None = None,
        max_entries: int = 5,
        token_budget: int = 800,
        token_estimator: TokenEstimator | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if store is not None and db_path is not None:
            raise ValueError("pass store or db_path, not both")
        if store is None:
            if db_path is None:
                raise ValueError("MemoryService requires an explicit db_path or store")
            store = MemoryStore(db_path)
        self.store = store
        self.workspace = Path(workspace).expanduser().resolve(strict=False) if workspace else None
        self._base_context = context_for_workspace(self.workspace)
        self._clock = clock or time.time
        self.max_entries = max(0, int(max_entries))
        self.token_budget = max(0, int(token_budget))
        self.injector = MemoryInjector(
            self,
            max_entries=self.max_entries,
            token_budget=self.token_budget,
            token_estimator=token_estimator,
        )
        self._last_write_decision = WriteDecision.NOOP

    @property
    def db_path(self) -> Path:
        return self.store.db_path

    @property
    def last_write_decision(self) -> WriteDecision:
        """Expose the deterministic decision made by the latest write call."""

        return self._last_write_decision

    @property
    def project_key(self) -> str | None:
        return self._base_context.project_key

    @property
    def branch_name(self) -> str | None:
        if self.workspace is None:
            return None
        return _git_branch(self.workspace)

    def context(self) -> MemoryContext:
        """Return the current workspace and branch context."""

        return MemoryContext(project_key=self.project_key, branch_name=self.branch_name)

    # ------------------------------------------------------------------
    # Explicit and candidate write paths
    # ------------------------------------------------------------------

    def remember_explicit(
        self,
        content: str,
        *,
        scope: Scope | str = Scope.PROJECT,
        kind: Kind | str = Kind.LESSON,
        key: str | None = None,
        context: MemoryContext | Mapping[str, Any] | None = None,
        project_key: str | None = None,
        branch_name: str | None = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        expires_at: float | None = None,
    ) -> MemoryEntry:
        """Create a user-confirmed active record, unless a key conflicts."""

        return self._create(
            content,
            scope=scope,
            kind=kind,
            key=key,
            status=Status.ACTIVE,
            source_type=SourceType.EXPLICIT_USER,
            context=context,
            project_key=project_key,
            branch_name=branch_name,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            expires_at=expires_at,
        )

    def propose(
        self,
        content: str,
        *,
        scope: Scope | str = Scope.PROJECT,
        kind: Kind | str = Kind.LESSON,
        key: str | None = None,
        context: MemoryContext | Mapping[str, Any] | None = None,
        project_key: str | None = None,
        branch_name: str | None = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        expires_at: float | None = None,
        evidence: Sequence[VerificationEvidence] = (),
        verified: bool | None = None,
    ) -> MemoryEntry:
        """Compatibility name for :meth:`propose_experience`.

        The former ``verified=True`` flag is deliberately non-authoritative:
        callers must provide actual structured evidence.  Keeping the keyword
        avoids a misleading silent compatibility break while removing its
        ability to bypass the service gate.
        """

        del verified
        return self.propose_experience(
            content,
            scope=scope,
            kind=kind,
            key=key,
            context=context,
            project_key=project_key,
            branch_name=branch_name,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            expires_at=expires_at,
            evidence=evidence,
        )

    def propose_experience(
        self,
        content: str,
        *,
        evidence: Sequence[VerificationEvidence],
        scope: Scope | str = Scope.PROJECT,
        kind: Kind | str = Kind.LESSON,
        key: str | None = None,
        context: MemoryContext | Mapping[str, Any] | None = None,
        project_key: str | None = None,
        branch_name: str | None = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        expires_at: float | None = None,
    ) -> MemoryEntry:
        """Persist one evidence-backed, reviewable task experience.

        System-generated experiences can never become active directly.  The
        evidence and source identifiers are mandatory so approval remains
        auditable after restart.
        """

        normalized_evidence = tuple(evidence)
        if not normalized_evidence:
            raise MemoryEvidenceRequired("experience requires validation or confirmation evidence")
        if len(normalized_evidence) > 3:
            raise MemoryEvidenceRequired("an experience may retain at most three evidence digests")
        if not all(isinstance(item, VerificationEvidence) for item in normalized_evidence):
            raise MemoryEvidenceRequired("experience evidence must be VerificationEvidence")
        if not any(item.supports_experience for item in normalized_evidence):
            raise MemoryEvidenceRequired("experience requires successful validation or confirmation evidence")
        if not _is_reusable_experience(content):
            raise MemoryEvidenceRequired("experience content must use the reusable condition/action/result template")

        evidence_sessions = {item.source_session_id for item in normalized_evidence}
        evidence_turns = {item.source_turn_id for item in normalized_evidence}
        if None in evidence_sessions or None in evidence_turns or len(evidence_sessions) != 1 or len(evidence_turns) != 1:
            raise MemoryEvidenceRequired("experience evidence requires one source session and turn")
        evidence_session_id = next(iter(evidence_sessions))
        evidence_turn_id = next(iter(evidence_turns))
        if source_session_id not in (None, evidence_session_id):
            raise MemoryEvidenceRequired("evidence session does not match candidate session")
        if source_turn_id not in (None, evidence_turn_id):
            raise MemoryEvidenceRequired("evidence turn does not match candidate turn")

        return self._create(
            content,
            scope=scope,
            kind=kind,
            key=key,
            status=Status.PENDING,
            source_type=SourceType.VERIFIED_TASK,
            context=context,
            project_key=project_key,
            branch_name=branch_name,
            source_session_id=evidence_session_id,
            source_turn_id=evidence_turn_id,
            expires_at=expires_at,
            evidence=normalized_evidence,
        )

    def observe_implicit_preference(
        self,
        content: str,
        *,
        scope: Scope | str = Scope.PROJECT,
        key: str | None = None,
        context: MemoryContext | Mapping[str, Any] | None = None,
        project_key: str | None = None,
        branch_name: str | None = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> MemoryEntry | None:
        """Accumulate user preference signals without silently activating one.

        A single incidental preference is deliberately not durable memory.
        Only a second distinct turn/session creates one pending candidate.
        """

        if looks_like_sensitive_content(content):
            raise SensitiveMemoryError("memory content resembles a credential and was not stored")
        if not source_session_id and not source_turn_id:
            raise MemoryServiceError("implicit preference requires a source session or turn")
        normalized_scope = coerce_scope(scope)
        resolved_context = self._resolve_context(
            normalized_scope,
            context=context,
            project_key=project_key,
            branch_name=branch_name,
        )
        normalized_key = str(key).strip() if key not in (None, "") else None
        marker_input = "\x1f".join(
            (
                normalized_scope.value,
                resolved_context.project_key or "",
                resolved_context.branch_name if normalized_scope == Scope.BRANCH else "",
                normalized_key or "",
                content_hash(content),
            )
        )
        marker = "implicit-preference:" + hashlib.sha256(marker_input.encode("utf-8")).hexdigest()
        signal = json.dumps([source_session_id or "", source_turn_id or ""], ensure_ascii=False)

        def record_signal(connection):
            raw_state = self.store.get_metadata(marker, connection=connection)
            try:
                state = json.loads(raw_state) if raw_state else {"signals": []}
            except (TypeError, ValueError, json.JSONDecodeError):
                state = {"signals": []}
            signals = state.get("signals") if isinstance(state, dict) else []
            signals = [str(item) for item in signals] if isinstance(signals, list) else []
            if signal in signals:
                return len(signals), False
            signals.append(signal)
            self.store.set_metadata(
                marker,
                json.dumps({"signals": signals}, ensure_ascii=False, separators=(",", ":")),
                connection=connection,
            )
            return len(signals), True

        signal_count, is_new_signal = self.store.run_in_transaction(record_signal)
        if not is_new_signal or signal_count < 2:
            self._last_write_decision = WriteDecision.NOOP
            return None
        return self._create(
            content,
            scope=normalized_scope,
            kind=Kind.PREFERENCE,
            key=normalized_key,
            status=Status.PENDING,
            source_type=SourceType.EXPLICIT_USER,
            context=resolved_context,
            project_key=None,
            branch_name=None,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            expires_at=None,
            signal_count=signal_count,
        )

    def _create(
        self,
        content: str,
        *,
        scope: Scope | str,
        kind: Kind | str,
        key: str | None,
        status: Status,
        source_type: SourceType,
        context: MemoryContext | Mapping[str, Any] | None,
        project_key: str | None,
        branch_name: str | None,
        source_session_id: str | None,
        source_turn_id: str | None,
        expires_at: float | None,
        evidence: Sequence[VerificationEvidence] = (),
        signal_count: int = 1,
    ) -> MemoryEntry:
        if looks_like_sensitive_content(content):
            raise SensitiveMemoryError("memory content resembles a credential and was not stored")
        normalized_scope = coerce_scope(scope)
        normalized_kind = self._coerce_kind(kind)
        resolved_context = self._resolve_context(
            normalized_scope,
            context=context,
            project_key=project_key,
            branch_name=branch_name,
        )
        now = self._clock()
        candidate = MemoryEntry(
            id=new_memory_id(),
            scope=normalized_scope,
            kind=normalized_kind,
            key=str(key).strip() if key not in (None, "") else None,
            content=content,
            status=status,
            project_key=resolved_context.project_key,
            branch_name=resolved_context.branch_name if normalized_scope == Scope.BRANCH else None,
            source_type=source_type,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            signal_count=signal_count,
            evidence=tuple(evidence),
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )

        def write(connection):
            if source_type == SourceType.VERIFIED_TASK:
                if not source_session_id or not source_turn_id:
                    raise MemoryEvidenceRequired("system experience requires source session and turn")
                existing_turn = self.store.find_by_source(
                    source_type=source_type.value,
                    source_session_id=source_session_id,
                    source_turn_id=source_turn_id,
                    connection=connection,
                )
                if existing_turn is not None:
                    return existing_turn, WriteDecision.NOOP
            duplicate = self.store.find_by_hash(
                scope=candidate.scope,
                content_hash=candidate.content_hash,
                project_key=candidate.project_key,
                branch_name=candidate.branch_name,
                connection=connection,
            )
            if duplicate is not None:
                return self.store.increment_signal_count(
                    duplicate.id,
                    updated_at=now,
                    connection=connection,
                ), WriteDecision.NOOP
            decision = WriteDecision.CREATE
            if candidate.key:
                pending_conflict = self.store.find_pending_by_key(
                    scope=candidate.scope,
                    key=candidate.key,
                    project_key=candidate.project_key,
                    branch_name=candidate.branch_name,
                    connection=connection,
                )
                if pending_conflict is not None:
                    # Keep exactly one pending value for a business key.  A
                    # newer conflicting candidate supersedes the older review
                    # item, while its signal/evidence history remains auditable.
                    self.store.update_version_link(
                        pending_conflict.id,
                        status=Status.SUPERSEDED,
                        updated_at=now,
                        connection=connection,
                    )
                    candidate.supersedes_id = pending_conflict.id
                    candidate.version = pending_conflict.version + 1
                    decision = WriteDecision.UPDATE
                conflict = self.store.find_active_by_key(
                    scope=candidate.scope,
                    key=candidate.key,
                    project_key=candidate.project_key,
                    branch_name=candidate.branch_name,
                    connection=connection,
                )
                if conflict is not None:
                    # A conflicting value is a reviewable update candidate;
                    # no active fact is silently overwritten.
                    candidate.status = Status.PENDING
                    decision = WriteDecision.UPDATE
            self.store.insert(candidate, connection=connection)
            return candidate, decision

        result, decision = self.store.run_in_transaction(write)
        self._last_write_decision = decision
        return self._legacy_fields(result)

    def approve(self, entry_id: str) -> MemoryEntry:
        """Activate a pending record and resolve an explicitly approved key."""

        now = self._clock()

        def transition(connection):
            entry = self.store.get(entry_id, connection=connection)
            if entry is None:
                raise MemoryNotFoundError(f"memory not found: {entry_id}")
            if entry.status != Status.PENDING:
                raise MemoryServiceError("only pending memories can be approved")
            if entry.expires_at is not None and entry.expires_at <= now:
                raise MemoryServiceError("expired memory cannot be approved")
            if entry.key:
                conflict = self.store.find_active_by_key(
                    scope=entry.scope,
                    key=entry.key,
                    project_key=entry.project_key,
                    branch_name=entry.branch_name,
                    connection=connection,
                )
                if conflict is not None and conflict.id != entry.id:
                    self.store.update_version_link(
                        conflict.id,
                        status=Status.SUPERSEDED,
                        updated_at=now,
                        connection=connection,
                    )
                    entry.supersedes_id = conflict.id
                    entry.version = conflict.version + 1
            entry.status = Status.ACTIVE
            entry.updated_at = now
            self.store.replace(entry, connection=connection)
            return entry

        result = self._legacy_fields(self.store.run_in_transaction(transition))
        self._last_write_decision = WriteDecision.UPDATE
        return result

    def reject(self, entry_id: str) -> MemoryEntry:
        return self._transition(entry_id, expected=Status.PENDING, target=Status.REJECTED)

    def update(self, entry_id: str, content: str) -> MemoryEntry:
        """Create a new version and supersede the previous record atomically."""

        if looks_like_sensitive_content(content):
            raise SensitiveMemoryError("memory content resembles a credential and was not stored")
        now = self._clock()

        def transition(connection):
            old = self.store.get(entry_id, connection=connection)
            if old is None:
                raise MemoryNotFoundError(f"memory not found: {entry_id}")
            if old.status not in {Status.ACTIVE, Status.ARCHIVED}:
                raise MemoryServiceError("only active or archived memories can be updated")
            new_hash = content_hash(content)
            duplicate = self.store.find_by_hash(
                scope=old.scope,
                content_hash=new_hash,
                project_key=old.project_key,
                branch_name=old.branch_name,
                connection=connection,
            )
            if duplicate is not None and duplicate.id != old.id:
                return duplicate, WriteDecision.NOOP
            if old.key:
                conflict = self.store.find_active_by_key(
                    scope=old.scope,
                    key=old.key,
                    project_key=old.project_key,
                    branch_name=old.branch_name,
                    connection=connection,
                )
                if conflict is not None and conflict.id != old.id:
                    raise MemoryConflictError("another active memory already owns this key")
            new_entry = MemoryEntry(
                id=new_memory_id(),
                scope=old.scope,
                kind=old.kind,
                key=old.key,
                content=content,
                status=Status.ACTIVE,
                project_key=old.project_key,
                branch_name=old.branch_name,
                # A user-directed body change is a new explicit assertion;
                # prior task evidence must never prove different content.
                source_type=SourceType.EXPLICIT_USER,
                source_session_id=None,
                source_turn_id=None,
                version=old.version + 1,
                supersedes_id=old.id,
                evidence=(),
                signal_count=1,
                created_at=now,
                updated_at=now,
                expires_at=old.expires_at,
            )
            self.store.update_version_link(
                old.id,
                status=Status.SUPERSEDED,
                updated_at=now,
                connection=connection,
            )
            self.store.insert(new_entry, connection=connection)
            return new_entry, WriteDecision.UPDATE

        result, decision = self.store.run_in_transaction(transition)
        self._last_write_decision = decision
        return self._legacy_fields(result)

    def archive(self, entry_id: str) -> MemoryEntry:
        return self._transition(entry_id, expected=None, target=Status.ARCHIVED)

    def restore(self, entry_id: str) -> MemoryEntry:
        now = self._clock()

        def transition(connection):
            entry = self.store.get(entry_id, connection=connection)
            if entry is None:
                raise MemoryNotFoundError(f"memory not found: {entry_id}")
            if entry.status != Status.ARCHIVED:
                raise MemoryServiceError("only archived memories can be restored")
            if entry.expires_at is not None and entry.expires_at <= now:
                raise MemoryServiceError("expired memory cannot be restored")
            if entry.key:
                conflict = self.store.find_active_by_key(
                    scope=entry.scope,
                    key=entry.key,
                    project_key=entry.project_key,
                    branch_name=entry.branch_name,
                    connection=connection,
                )
                if conflict is not None and conflict.id != entry.id:
                    raise MemoryConflictError("cannot restore: active memory already owns this key")
            entry.status = Status.ACTIVE
            entry.updated_at = now
            self.store.replace(entry, connection=connection)
            return entry

        result = self._legacy_fields(self.store.run_in_transaction(transition))
        self._last_write_decision = WriteDecision.UPDATE
        return result

    def purge(self, entry_id: str, *, confirmed: bool = False) -> None:
        if not confirmed:
            raise MemoryConfirmationRequired("purge requires confirmed=True")

        def delete(connection):
            if not self.store.delete(entry_id, connection=connection):
                raise MemoryNotFoundError(f"memory not found: {entry_id}")

        self.store.run_in_transaction(delete)
        self._last_write_decision = WriteDecision.DELETE

    def _transition(
        self,
        entry_id: str,
        *,
        expected: Status | None,
        target: Status,
    ) -> MemoryEntry:
        now = self._clock()

        def transition(connection):
            entry = self.store.get(entry_id, connection=connection)
            if entry is None:
                raise MemoryNotFoundError(f"memory not found: {entry_id}")
            if expected is not None and entry.status != expected:
                raise MemoryServiceError(f"memory must be {expected.value}")
            if target == Status.ARCHIVED and entry.status in {Status.SUPERSEDED, Status.REJECTED}:
                raise MemoryServiceError("superseded or rejected memories cannot be archived")
            entry.status = target
            entry.updated_at = now
            self.store.replace(entry, connection=connection)
            return entry

        result = self._legacy_fields(self.store.run_in_transaction(transition))
        self._last_write_decision = (
            WriteDecision.ARCHIVE if target == Status.ARCHIVED else WriteDecision.UPDATE
        )
        return result

    # ------------------------------------------------------------------
    # Deterministic search and injection
    # ------------------------------------------------------------------

    def search(
        self,
        query: str = "",
        *,
        context: MemoryContext | Mapping[str, Any] | None = None,
        scope: Scope | str | None = None,
        limit: int = 50,
        min_relevance: float | None = None,
        **_: Any,
    ) -> list[MemoryEntry]:
        """Search active, in-scope, non-expired records deterministically."""

        if limit <= 0:
            return []
        normalized_scope = coerce_scope(scope) if scope is not None else None
        resolved_context = _as_context(context, fallback=self.context())
        active = self.store.list_entries(statuses=(Status.ACTIVE,))
        now = self._clock()
        terms = [term.lower() for term in _TOKEN_RE.findall(str(query or ""))]
        phrase = " ".join(str(query or "").strip().lower().split())
        ranked: list[tuple[tuple[float, int, float, str], MemoryEntry]] = []

        for entry in active:
            if entry.expires_at is not None and entry.expires_at <= now:
                continue
            if normalized_scope is not None and entry.scope != normalized_scope:
                continue
            if not self._visible(entry, resolved_context):
                continue
            score, relevance = self._relevance(entry, terms, phrase)
            if terms and relevance <= 0:
                continue
            if min_relevance is not None and relevance < float(min_relevance):
                continue
            sort_key = (-score, -_scope_priority(entry.scope), -entry.updated_at, entry.id)
            ranked.append((sort_key, entry))

        ranked.sort(key=lambda item: item[0])
        return [self._legacy_fields(entry) for _, entry in ranked[:limit]]

    @staticmethod
    def _relevance(entry: MemoryEntry, terms: list[str], phrase: str) -> tuple[float, float]:
        if not terms:
            return 0.0, 1.0
        key_text = (entry.key or "").lower()
        content_text = entry.content.lower()
        combined_tokens = set(_TOKEN_RE.findall(f"{key_text} {content_text}"))
        matched = sum(1 for term in set(terms) if term in combined_tokens)
        relevance = matched / max(1, len(set(terms)))
        score = float(matched)
        if key_text and all(term in key_text for term in set(terms)):
            score += 3.0
        if phrase and phrase in content_text:
            score += 2.0
        return score, relevance

    @staticmethod
    def _visible(entry: MemoryEntry, context: MemoryContext) -> bool:
        if entry.scope == Scope.GLOBAL:
            return True
        if entry.scope == Scope.PROJECT:
            return bool(context.project_key and entry.project_key == context.project_key)
        return bool(
            context.project_key
            and context.branch_name
            and entry.project_key == context.project_key
            and entry.branch_name == context.branch_name
        )

    def build_prompt_context(
        self,
        task: str,
        budget: int | None = None,
        *,
        context: MemoryContext | Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> str:
        return self.injector.build_prompt_context(
            task,
            context=context,
            budget=self.token_budget if budget is None else budget,
            limit=self.max_entries if limit is None else limit,
        )

    def detect_conflicts(
        self,
        content: str,
        *,
        scope: Scope | str | None = None,
        threshold: float = 0.6,
    ) -> list[tuple[MemoryEntry, float]]:
        """Return deterministic lexical overlaps for compatibility callers.

        This is an inspection aid for explicit updates; it does not merge or
        write records and therefore cannot bypass the pending/approval flow.
        """

        candidate_tokens = set(_TOKEN_RE.findall(str(content or "").lower()))
        if not candidate_tokens:
            return []
        normalized_scope = coerce_scope(scope) if scope is not None else None
        context = self.context()
        conflicts: list[tuple[MemoryEntry, float]] = []
        for entry in self.store.list_entries(statuses=(Status.ACTIVE,)):
            if normalized_scope is not None and entry.scope != normalized_scope:
                continue
            if not self._visible(entry, context):
                continue
            entry_tokens = set(_TOKEN_RE.findall(entry.content.lower()))
            union = candidate_tokens | entry_tokens
            similarity = len(candidate_tokens & entry_tokens) / len(union) if union else 0.0
            if similarity >= float(threshold):
                conflicts.append((self._legacy_fields(entry), similarity))
        conflicts.sort(key=lambda item: (-item[1], item[0].id))
        return conflicts

    # ------------------------------------------------------------------
    # Queries, migration, and safe compatibility surface
    # ------------------------------------------------------------------

    def list_pending(
        self,
        *,
        context: MemoryContext | Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        resolved_context = _as_context(context, fallback=self.context())
        entries = [
            entry
            for entry in self.store.list_entries(statuses=(Status.PENDING,))
            if self._visible(entry, resolved_context)
        ]
        return [self._legacy_fields(entry) for entry in entries[:limit] if limit is not None] if limit is not None else [self._legacy_fields(entry) for entry in entries]

    def stats(self) -> dict[str, dict[str, int]]:
        return self.store.count_by_scope_and_status()

    def migrate_legacy(
        self,
        *,
        workspace: str | Path | None = None,
        home: str | Path | None = None,
    ) -> Any:
        from .legacy import migrate_legacy

        return migrate_legacy(self, workspace=workspace or self.workspace, home=home)

    def _import_migrated(self, entry: MemoryEntry) -> tuple[bool, str]:
        """Insert one validated migration record transactionally.

        Kept as a narrow compatibility seam for callers that already import
        one record at a time.  The migration reader uses
        :meth:`_import_migrated_batch` so a source and its marker share one
        transaction.
        """

        return self._import_migrated_batch((entry,))[0]

    def _import_migrated_batch(
        self,
        entries: Iterable[MemoryEntry],
        *,
        marker: str | None = None,
        source_hash: str | None = None,
    ) -> list[tuple[bool, str]]:
        """Import one legacy source and optionally its marker atomically.

        The callback deliberately does not catch database errors.  Any real
        import or marker failure therefore rolls back every insert from the
        source and leaves the marker absent, allowing a later run to retry.
        Invalid or sensitive records are filtered by ``legacy.py`` before
        this method is called, so valid records in the same source remain
        importable without weakening the security check.
        """

        records = tuple(entries)
        if any(looks_like_sensitive_content(entry.content) for entry in records):
            raise SensitiveMemoryError("migration contains content resembling a credential")

        def write(connection):
            results: list[tuple[bool, str]] = []
            for entry in records:
                duplicate = self.store.find_by_hash(
                    scope=entry.scope,
                    content_hash=entry.content_hash,
                    project_key=entry.project_key,
                    branch_name=entry.branch_name,
                    connection=connection,
                )
                if duplicate is not None:
                    results.append((False, "duplicate"))
                    continue
                self.store.insert(entry, connection=connection)
                results.append((True, "imported"))
            if marker is not None:
                if source_hash is None:
                    raise ValueError("source_hash is required when marker is supplied")
                self.store.set_metadata(marker, source_hash, connection=connection)
            return results

        return self.store.run_in_transaction(write)

    def _resolve_context(
        self,
        scope: Scope,
        *,
        context: MemoryContext | Mapping[str, Any] | None,
        project_key: str | None,
        branch_name: str | None,
    ) -> MemoryContext:
        fallback = self.context()
        resolved = _as_context(context, fallback=fallback)
        if project_key is not None:
            resolved = MemoryContext(project_key=str(project_key), branch_name=resolved.branch_name)
        if branch_name is not None:
            resolved = MemoryContext(project_key=resolved.project_key, branch_name=str(branch_name))
        if scope == Scope.PROJECT and not resolved.project_key:
            raise MemoryServiceError("project memory requires a workspace project_key")
        if scope == Scope.BRANCH:
            if not resolved.project_key:
                raise BranchScopeError("branch memory requires a workspace project_key")
            if not resolved.branch_name:
                branch = self.branch_name
                if branch:
                    resolved = MemoryContext(project_key=resolved.project_key, branch_name=branch)
                else:
                    raise BranchScopeError("branch memory requires a current Git branch")
        return resolved

    @staticmethod
    def _coerce_kind(kind: Kind | str) -> Kind:
        try:
            return coerce_kind(kind)
        except ValueError:
            return kind_from_legacy_category(str(kind))

    @staticmethod
    def _legacy_fields(entry: MemoryEntry) -> MemoryEntry:
        if not entry.category:
            entry.category = entry.kind.value
        return entry

    # -- Explicit command compatibility.  These methods delegate to the new
    # service and do not maintain a JSON/Markdown second truth source. -------

    def handle_user_memory_input(self, user_input: str) -> str | None:
        raw = str(user_input or "").strip()
        if not raw:
            return None
        content: str | None = None
        scope: Scope | str = Scope.PROJECT
        local_alias = False
        if raw.startswith("#"):
            content = raw[1:].strip()
            if content.lower().startswith("remember "):
                content = content[9:].strip()
        elif raw.lower().startswith("/memory add "):
            content = raw[len("/memory add "):].strip()
        elif raw.lower().startswith("/memory remember "):
            content = raw[len("/memory remember "):].strip()
        if content is None:
            return None
        match = re.match(r"^(global|user|project|local|branch)\s*:\s*(.+)$", content, re.I)
        if match:
            raw_scope, content = match.group(1).lower(), match.group(2).strip()
            scope = {"user": "global", "local": "project"}.get(raw_scope, raw_scope)
            local_alias = raw_scope == "local"
        if not content:
            return "Usage: # <memory> or /memory add [global|project|branch:] <memory>"
        try:
            entry = self.remember_explicit(
                content,
                scope=scope,
                kind=Kind.PREFERENCE,
            )
        except SensitiveMemoryError:
            return "Memory rejected: content resembles a credential."
        except MemoryServiceError as exc:
            return f"Memory rejected: {exc}"
        suffix = " (local: alias for project; deprecated)" if local_alias else ""
        return f"Saved memory ({entry.scope.value}, {entry.status.value}){suffix}: {entry.content}"

    def get_relevant_context(self, query: str = "", *, max_entries: int = 5, max_tokens: int = 800) -> str:
        return self.build_prompt_context(query, budget=max_tokens, limit=max_entries)

    def add_entry(
        self,
        scope: Scope | str,
        category: str,
        content: str,
        tags: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> MemoryEntry:
        del tags
        if str(category).lower() == "auto":
            category = _legacy_auto_category(content)
        entry = self.remember_explicit(
            content,
            scope=scope,
            kind=self._coerce_kind(category),
            key=kwargs.get("key"),
        )
        entry.category = category
        return entry

    def update_entry(self, scope: Scope | str, entry_id: str, content: str) -> bool:
        del scope
        self.update(entry_id, content)
        return True

    def delete_entry(self, scope: Scope | str, entry_id: str) -> bool:
        del scope
        entry = self.store.get(entry_id)
        if entry is None:
            return False
        if entry.status not in {Status.SUPERSEDED, Status.REJECTED}:
            self.archive(entry_id)
        # Legacy lifecycle traces address the original ID after update().
        # Archive its active successor as part of the same user-visible delete
        # boundary, while retaining both records for audit history.
        active_entries = self.store.list_entries(statuses=(Status.ACTIVE,))
        for successor in active_entries:
            if successor.supersedes_id == entry_id:
                self.archive(successor.id)
        return True

    def clear_scope(self, scope: Scope | str) -> None:
        normalized = coerce_scope(scope)
        for entry in self.store.list_entries(scopes=(normalized,)):
            if entry.status not in {Status.ARCHIVED, Status.SUPERSEDED, Status.REJECTED}:
                self.archive(entry.id)

    def format_stats(self) -> str:
        stats = self.stats()
        lines = ["Memory System Status", "=" * 40]
        total = 0
        for scope in (Scope.GLOBAL, Scope.PROJECT, Scope.BRANCH):
            counts = stats.get(scope.value, {})
            count = sum(counts.values())
            total += count
            lines.append(f"{scope.value}: {count} records")
        lines.append(f"Total: {total} records")
        lines.append(f"Database: {self.db_path}")
        return "\n".join(lines)

    @property
    def memories(self) -> dict[Scope, "_MemoryScopeView"]:
        return {scope: _MemoryScopeView(self, scope) for scope in (Scope.GLOBAL, Scope.PROJECT, Scope.BRANCH)}

    def search_by_tag(self, scope: Scope | str, tag: str) -> list[MemoryEntry]:
        del scope, tag
        return []

    def get_all_tags(self, scope: Scope | str) -> set[str]:
        del scope
        return set()

    def get_tags_by_category(self, scope: Scope | str) -> dict[str, list[str]]:
        del scope
        return {}

    def check_integrity(self, scope: Scope | str) -> dict[str, Any]:
        normalized = coerce_scope(scope)
        entries = self.store.list_entries(scopes=(normalized,))
        ids = [entry.id for entry in entries]
        issues = [] if len(ids) == len(set(ids)) else ["duplicate IDs"]
        return {"is_valid": not issues, "issues": issues}

    def compress_scope(self, scope: Scope | str, **_: Any) -> dict[str, int]:
        """Compatibility no-op: semantic similarity is never merged automatically."""

        normalized = coerce_scope(scope)
        entries = self.store.list_entries(scopes=(normalized,))
        return {"merged_count": 0, "removed_count": 0, "remaining_count": len(entries)}


class _MemoryScopeView:
    """Read-only compatibility view over one SQLite scope."""

    max_entries = 200
    max_size_bytes = 25 * 1024

    def __init__(self, service: MemoryService, scope: Scope) -> None:
        self.service = service
        self.scope = scope

    @property
    def entries(self) -> list[MemoryEntry]:
        context = self.service.context()
        return [
            service_entry
            for service_entry in self.service.store.list_entries(scopes=(self.scope,))
            if self.service._visible(service_entry, context)
        ]

    @property
    def size_bytes(self) -> int:
        return sum(len(entry.content.encode("utf-8")) for entry in self.entries)

    def get_entries_by_category(self, category: str) -> list[MemoryEntry]:
        return [entry for entry in self.entries if entry.category == category or entry.kind.value == category]

    def search(self, query: str, **kwargs: Any) -> list[MemoryEntry]:
        return self.service.search(query, scope=self.scope, **kwargs)
