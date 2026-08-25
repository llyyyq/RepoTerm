"""RepoTerm 持久化记忆的数据模型。

本模块只描述可以跨回合保存、且用户能够理解和审计的数据。Session
Transcript、原始工具输出、使用计数、旧版 tier 和关系图都不属于新的
持久化模型；这些内容应留在运行时 Trace 或兼容层中。
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
    """三种实际持久化作用域；`user`/`local` 只是兼容别名。"""

    GLOBAL = "global"
    PROJECT = "project"
    BRANCH = "branch"

    # Short-lived compatibility aliases.  They intentionally map to the new
    # semantics instead of preserving a second USER/LOCAL storage model.
    USER = "global"
    LOCAL = "project"


class Kind(str, Enum):
    """受限且可审计的记忆含义集合。"""

    PREFERENCE = "preference"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    LESSON = "lesson"


class Status(str, Enum):
    """记忆候选从产生到归档、拒绝或被替代的生命周期状态。"""

    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class SourceType(str, Enum):
    """记录进入系统的来源；来源决定默认写入门禁。"""

    EXPLICIT_USER = "explicit_user"
    VERIFIED_TASK = "verified_task"
    MIGRATION = "migration"


class EvidenceLevel(str, Enum):
    """有界证据摘要的强度；只有验证/确认可以支撑经验。"""

    OBSERVATION = "observation"
    CHANGE = "change"
    VALIDATION = "validation"
    CONFIRMATION = "confirmation"


class EvidenceKind(str, Enum):
    """工具结果或用户确认的语义类别。"""

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
    """写入前允许对外报告的有限决策集合。"""

    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    DELETE = "delete"
    NOOP = "noop"


class MemoryTier(str, Enum):
    """已删除的分层系统留下的兼容枚举，不参与新存储和检索。"""

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
    """判断文本是否像凭据，但绝不记录或回显原文。"""

    text = str(value or "")
    return any(pattern.search(text) for pattern in _SENSITIVE_TEXT_PATTERNS)


def sanitize_evidence_summary(value: object, *, limit: int = 240) -> str:
    """把证据摘要压缩、脱敏并限制长度，避免长期保存密钥或大段输出。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[: max(0, int(limit))]


def _normalize_evidence_input(value: object) -> str:
    """构造指纹输入；只用于哈希，不把完整工具输入写入数据库。"""

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
    """对规范化证据元数据计算哈希，不持久化原始工具输出。"""

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
    """可支持 pending 经验的有界证据摘要。

    Raw stdout, stderr, diffs, source code and transcript payloads remain in
    Trace/Transcript.  The source ids are the stable references back to those
    authoritative records.

    原始 stdout、stderr、Diff、源码和 Transcript 仍保留在 Trace/Transcript；
    本对象只保存清洗后的摘要和回溯来源所需的 session/turn 标识。
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
        """统一枚举、工具名、摘要和来源字段，保证对象可安全序列化。"""
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
        """从一次工具/用户结果创建证据，并在创建时完成摘要脱敏和指纹计算。"""
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
        """返回该证据是否能打开经验写入门禁。"""
        return self.ok and self.level in {EvidenceLevel.VALIDATION, EvidenceLevel.CONFIRMATION}

    def to_dict(self) -> dict[str, object]:
        """转换为可写入 `evidence_json` 的稳定字典。"""
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
        """从 SQLite/旧数据中的字典恢复证据对象并重新执行规范化。"""
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
    """生成用于精确去重的规范正文：Unicode 归一化并折叠空白。"""

    if not isinstance(content, str):
        content = "" if content is None else str(content)
    normalized = unicodedata.normalize("NFKC", content).strip()
    return re.sub(r"\s+", " ", normalized)


def content_hash(content: str) -> str:
    """对规范正文计算哈希；哈希值用于去重，不暴露正文。"""

    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def project_key_for(workspace: str | Path) -> str:
    """把绝对 workspace 路径转换为稳定项目 key，用于项目级隔离。"""

    resolved = Path(workspace).expanduser().resolve(strict=False)
    normalized = os.path.normcase(str(resolved))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def new_memory_id() -> str:
    """生成新记忆记录使用的 UUID。"""

    return str(uuid.uuid4())


def migration_memory_id(source_path: str, source_id: str, index: int, content: str) -> str:
    """根据旧来源和正文生成可重复的迁移 UUID，保证迁移幂等。"""

    seed = "\x1f".join((source_path, source_id, str(index), normalize_content(content)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


@dataclass(frozen=True)
class MemoryContext:
    """运行时用于隔离项目记忆和分支记忆的最小上下文。"""

    project_key: str | None = None
    branch_name: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "MemoryContext":
        """兼容 snake_case/camelCase 输入并丢弃空上下文值。"""
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
    """一条可持久化的记忆记录。

    ``category``, ``tags``, ``domains`` and the ``tier`` field are accepted as
    compatibility inputs for the old public import.  They are not persisted by
    the new SQLite schema and do not participate in retrieval.

    旧接口中的 ``category``、``tags``、``domains`` 和 ``tier`` 只为兼容旧
    调用者保留，不写入新 schema，也不参与确定性检索。
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
        """规范化字段并校验 scope 与 project/branch 上下文是否匹配。"""
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
        """执行作用域约束：global 无项目，branch 必须同时有项目和分支。"""
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
        """返回旧 CLI 需要的类别展示值，不改变新模型的 kind。"""

        return self.category or self.kind.value

    def to_dict(self) -> dict[str, Any]:
        """只序列化新核心字段，供迁移、测试和审计展示使用。"""

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
        """从新格式或旧形状字典构造记录，并交给构造器统一校验。"""

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
    """把作用域字符串和兼容别名转换成正式 `Scope`。"""
    value = value.value if isinstance(value, Scope) else str(value).strip().lower()
    aliases = {"user": Scope.GLOBAL.value, "local": Scope.PROJECT.value}
    return Scope(aliases.get(value, value))


def coerce_kind(value: Kind | str) -> Kind:
    """把 kind 输入转换成受限枚举，非法值交给上层处理。"""
    value = value.value if isinstance(value, Kind) else str(value).strip().lower()
    return Kind(value)


def coerce_status(value: Status | str) -> Status:
    """把状态输入转换成正式 `Status`。"""
    value = value.value if isinstance(value, Status) else str(value).strip().lower()
    return Status(value)


def coerce_source_type(value: SourceType | str) -> SourceType:
    """把来源输入转换成正式 `SourceType`。"""
    value = value.value if isinstance(value, SourceType) else str(value).strip().lower()
    return SourceType(value)


def kind_from_legacy_category(category: str | None) -> Kind:
    """把旧版自由类别映射到新的有限 kind 词汇。"""

    normalized = str(category or "").strip().lower()
    if normalized in {"preference", "directive", "user_preference", "style"}:
        return Kind.PREFERENCE
    if normalized in {"decision", "architecture", "design", "pattern", "code-pattern"}:
        return Kind.DECISION
    if normalized in {"constraint", "convention", "configuration", "security", "workflow"}:
        return Kind.CONSTRAINT
    return Kind.LESSON


def context_for_workspace(workspace: str | Path | None) -> MemoryContext:
    """只根据 workspace 构造项目上下文；Git 分支由 Service 按需查询。"""

    if workspace is None:
        return MemoryContext()
    return MemoryContext(project_key=project_key_for(workspace))
