"""RepoTerm 记忆系统的公共导出和迁移期兼容门面。

新代码应依赖 MemoryService、MemoryStore 和这里导出的模型；MemoryManager、
MemoryScope、_tokenize 等符号只为旧调用方过渡，不能重新引入第二套真值源。

New code should depend on :class:`MemoryService`, :class:`MemoryStore`, and
the model enums exported here.  ``MemoryManager`` and ``MemoryScope`` remain
short-lived compatibility names only; they delegate to the same service and
do not reintroduce JSON/Markdown storage or a second retrieval path.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from repoterm.config import REPOTERM_DIR

from .injector import MemoryInjector, PromptMemory, default_token_estimator
from .models import (
    Kind,
    MemoryContext,
    MemoryEntry,
    MemoryTier,
    Scope,
    SourceType,
    Status,
    EvidenceKind,
    EvidenceLevel,
    VerificationEvidence,
    WriteDecision,
    content_hash,
    project_key_for,
)
from .service import (
    BranchScopeError,
    MemoryConfirmationRequired,
    MemoryConflictError,
    MemoryEvidenceRequired,
    MemoryNotFoundError,
    MemoryService,
    MemoryServiceError,
    SensitiveMemoryError,
    extract_implicit_preference_signal,
    looks_like_sensitive_content,
)
from .store import MemoryStore, SCHEMA_VERSION

logger = logging.getLogger(__name__)

MemoryScope = Scope

# These two symbols are intentionally kept as compatibility exports for the
# legacy evaluation suite.  Retrieval in the new service is deterministic and
# SQLite-backed; the tokenizer is only a pure helper for callers that still
# import it directly.
_WORD_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")
_CJK_BIGRAM_RE = re.compile(r"[\u4e00-\u9fff]{2}")
_CODE_TERM_EXPANSIONS: dict[str, list[str]] = {
    "function": ["func", "method"],
    "func": ["function", "method"],
    "method": ["function", "func"],
    "class": ["type"],
    "type": ["class"],
    "variable": ["var"],
    "var": ["variable"],
    "parameter": ["param", "argument", "arg"],
    "param": ["parameter", "argument", "arg"],
    "argument": ["parameter", "param", "arg"],
    "attribute": ["attr", "property", "prop"],
    "attr": ["attribute", "property", "prop"],
    "property": ["attribute", "attr", "prop"],
    "interface": ["module"],
    "module": ["interface"],
}


def _auto_classify_content(content: str) -> tuple[str, list[str]]:
    """为已删除 JSON 系统的旧调用者提供不持久化的兼容分类。"""

    text = str(content or "").lower()
    rules = (
        ("architecture", ("architecture", "design", "架构"), ["design-pattern"]),
        ("code-pattern", ("function", "func", "method", "函数", "代码"), ["function"]),
        ("testing", ("pytest", "test", "testing", "测试"), ["test"]),
        ("configuration", ("config", "configuration", "配置"), ["config"]),
        ("workflow", ("git", "workflow", "工作流"), ["git"]),
        ("security", ("security", "credential", "安全"), ["security"]),
        ("performance", ("performance", "optimization", "性能", "优化"), ["optimization"]),
        ("convention", ("convention", "style", "规范", "风格"), ["style"]),
    )
    matches = [(category, tags) for category, keywords, tags in rules if any(keyword in text for keyword in keywords)]
    if not matches:
        return "general", []
    category, tags = matches[0]
    return category, tags


@lru_cache(maxsize=1024)
def _tokenize(text: str) -> list[str]:
    """提供旧评测依赖的无依赖分词；新检索由 Service 使用确定性规则完成。"""

    value = str(text or "")
    tokens = [part.lower() for part in _WORD_RE.findall(value)]
    tokens.extend(part.lower() for part in _CJK_BIGRAM_RE.findall(value))
    return tokens


def create_memory_service(
    workspace: str | Path | None = None,
    *,
    runtime: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    max_entries: int | None = None,
    token_budget: int | None = None,
    token_estimator=None,
) -> MemoryService:
    """根据显式数据库路径和运行时配置创建唯一 MemoryService 组合根。"""

    runtime = runtime or {}
    if db_path is None:
        configured = runtime.get("memoryDbPath") or runtime.get("memory_db_path")
        if configured:
            db_path = configured
        else:
            from repoterm.config import REPOTERM_DIR

            db_path = REPOTERM_DIR / "memory.sqlite3"
    entries = max_entries if max_entries is not None else runtime.get("memoryMaxEntries", 5)
    budget = token_budget if token_budget is not None else runtime.get("memoryTokenBudget", 800)
    return MemoryService(
        db_path=db_path,
        workspace=workspace,
        max_entries=int(entries),
        token_budget=int(budget),
        token_estimator=token_estimator,
    )


class MemoryManager(MemoryService):
    """Thin compatibility facade over :class:`MemoryService`.

    ``project_root``/``workspace`` supplies scope context only.  Unless a
    caller explicitly passes ``db_path`` (as isolated tests may), this facade
    uses the same single production database as the runtime composition root.
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        db_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        # 兼容旧 project_root 参数，但仍使用同一个生产 SQLite 数据库。
        target_workspace = workspace or project_root
        if db_path is None:
            db_path = REPOTERM_DIR / "memory.sqlite3"
        super().__init__(db_path=db_path, workspace=target_workspace, **kwargs)


def inject_memory_into_prompt(
    system_prompt: str,
    memory_manager: MemoryService,
    max_tokens: int = 800,
) -> str:
    """兼容旧调用，委托唯一确定性 Injector 追加一个 advisory 记忆块。"""

    block = memory_manager.build_prompt_context("", budget=max_tokens)
    return f"{system_prompt}\n\n{block}" if block else system_prompt


def format_memory_list(
    memory_manager: MemoryService | None = None,
    scope: Scope | None = None,
    category: str | None = None,
) -> str:
    """为旧 CLI 输出有界正文预览；不创建新的存储或检索实现。"""

    if memory_manager is None:
        return "No MemoryService available."
    entries = memory_manager.store.list_entries(scopes=(scope,) if scope else None)
    if category:
        entries = [entry for entry in entries if entry.kind.value == category or entry.category == category]
    if not entries:
        return "No memories found."
    lines = ["=" * 60]
    for entry in entries[:20]:
        lines.append(f"[{entry.scope.value}] [{entry.kind.value}] {entry.content[:100].replace(chr(10), ' ')}")
    lines.extend(["=" * 60, f"Total: {len(entries)} entries"])
    return "\n".join(lines)


__all__ = [
    "BranchScopeError",
    "Kind",
    "MemoryConfirmationRequired",
    "MemoryConflictError",
    "MemoryEvidenceRequired",
    "MemoryContext",
    "MemoryEntry",
    "MemoryInjector",
    "MemoryManager",
    "MemoryScope",
    "MemoryService",
    "MemoryServiceError",
    "MemoryStore",
    "MemoryTier",
    "EvidenceKind",
    "EvidenceLevel",
    "PromptMemory",
    "SCHEMA_VERSION",
    "Scope",
    "SensitiveMemoryError",
    "SourceType",
    "Status",
    "VerificationEvidence",
    "WriteDecision",
    "content_hash",
    "create_memory_service",
    "default_token_estimator",
    "extract_implicit_preference_signal",
    "format_memory_list",
    "inject_memory_into_prompt",
    "looks_like_sensitive_content",
    "project_key_for",
]
