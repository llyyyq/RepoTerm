"""Agent self-reflection system.

Provides post-task reflection to improve future performance:
- Success/failure analysis
- Strategy effectiveness review
- Error pattern recognition
- Memory recording for future reference
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from repoterm.observability.logging import get_logger
from repoterm.memory import Kind, MemoryService, Scope, VerificationEvidence
from repoterm.runtime.turn_kernel import TurnVerificationState

logger = get_logger("agent_reflection")


@dataclass
class ReflectionResult:
    """Result of a reflection cycle."""

    task_summary: str
    success: bool
    key_decisions: list[str]
    errors_encountered: list[str]
    lessons_learned: list[str]
    suggested_improvements: list[str]
    confidence: float
    timestamp: float = field(default_factory=time.time)
    # Structured task context for domain-aware memory retrieval
    task_context: dict[str, Any] = field(default_factory=dict)

    def to_memory_entry(self) -> dict[str, Any]:
        """Convert to a memory entry for persistence."""
        context = self.task_context
        # Build domain list from context files
        domains: list[str] = []
        if context.get("files"):
            try:
                from repoterm.runtime.planning.domain_classifier import get_active_domain_values
                domains = get_active_domain_values(
                    current_files=context.get("files", []),
                    intent_text=self.task_summary,
                )
            except Exception:
                pass

        return {
            "content": self._format_content(),
            "category": "task_context" if context else "reflection",
            "tags": self._build_tags(),
            "domains": domains,
            "metadata": {
                "confidence": self.confidence,
                "key_decisions": self.key_decisions,
                "errors": self.errors_encountered,
                "improvements": self.suggested_improvements,
                "task_context": context,
            },
        }

    def _build_tags(self) -> list[str]:
        tags = ["self-reflection"]
        if self.success:
            tags.append("success")
        else:
            tags.append("failure")
        ctx = self.task_context
        if ctx.get("libraries"):
            tags.extend(ctx["libraries"])
        if ctx.get("tools"):
            tags.extend(ctx["tools"])
        return tags

    def _format_content(self) -> str:
        parts = [
            f"Task Context: {self.task_summary}",
        ]
        ctx = self.task_context
        if ctx.get("files"):
            parts.append(f"Files: {', '.join(ctx['files'][:8])}")
        if ctx.get("libraries"):
            parts.append(f"Libraries/Tools: {', '.join(ctx['libraries'])}")
        if ctx.get("project_state"):
            parts.append(f"State: {ctx['project_state']}")
        parts.extend(["", "Key Decisions:"])
        for d in self.key_decisions:
            parts.append(f"  - {d}")

        if self.errors_encountered:
            parts.extend(["", "Errors Encountered:"])
            for e in self.errors_encountered:
                parts.append(f"  - {e}")

        parts.extend(["", "Lessons Learned:"])
        for lesson in self.lessons_learned:
            parts.append(f"  - {lesson}")

        return "\n".join(parts)


class ReflectionEngine:
    """负责把经过验证的反思转换为 pending 经验候选。"""

    # 反思只持有运行时传入的 MemoryService，不创建自己的存储或注入路径。
    def __init__(
        self,
        memory_manager: MemoryService | None = None,
        min_confidence_threshold: float = 0.5,
    ):
        self.memory = memory_manager
        self.min_confidence = min_confidence_threshold

    # 反思可以生成诊断，但只有共享验证状态满足门禁时才提交 pending 经验。
    def reflect(
        self,
        task_description: str,
        execution_trace: list[dict[str, Any]],
        metrics: Any | None = None,
        *,
        verification_state: TurnVerificationState | None = None,
        stop_reason: str | None = None,
    ) -> ReflectionResult:
        """Generate reflection using the turn's structured evidence source.

        ``execution_trace`` remains useful for a user-visible diagnostic, but
        it is never reinterpreted as verification proof.  The runtime-owned
        :class:`TurnVerificationState` is the sole authority for a durable
        experience candidate.
        """
        tool_calls = [s for s in execution_trace if s.get("type") == "tool_call"]
        errors = [s for s in execution_trace if s.get("type") == "error"]
        assistant_msgs = [s for s in execution_trace if s.get("type") == "assistant"]

        evidence = (
            verification_state.evidence_for_experience()
            if verification_state is not None
            else ()
        )
        success = self._experience_is_eligible(
            task_description=task_description,
            verification_state=verification_state,
            stop_reason=stop_reason,
        )

        key_decisions = self._extract_decisions(assistant_msgs)
        error_list = [e.get("content", "Unknown error") for e in errors]
        lessons = self._generate_lessons(
            verification_state=verification_state,
            task_description=task_description,
            eligible=success,
        )
        improvements = self._generate_improvements(tool_calls, errors, metrics)
        confidence = self._calculate_confidence(success, len(errors), len(tool_calls))

        # Extract structured task context from execution trace
        task_context = self._extract_task_context(tool_calls, assistant_msgs)

        reflection = ReflectionResult(
            task_summary=task_description[:200],
            success=success,
            key_decisions=key_decisions,
            errors_encountered=error_list,
            lessons_learned=lessons,
            suggested_improvements=improvements,
            confidence=confidence,
            task_context=task_context,
        )

        if self.memory and success and evidence and confidence >= self.min_confidence:
            content = self._experience_content(verification_state, task_description)
            if content:
                self._persist_reflection(reflection, content=content, evidence=evidence)

        return reflection

    # 任务必须正常结束、拥有成功验证，并满足失败恢复顺序，才能进入经验候选。
    @staticmethod
    def _experience_is_eligible(
        *,
        task_description: str,
        verification_state: TurnVerificationState | None,
        stop_reason: str | None,
    ) -> bool:
        """Apply the terminal and recovery gates before any memory proposal."""

        if not str(task_description or "").strip() or verification_state is None:
            return False
        if stop_reason != "done":
            return False
        successful = verification_state.evidence_for_experience()
        if not successful:
            return False
        source_sessions = {item.source_session_id for item in successful}
        source_turns = {item.source_turn_id for item in successful}
        if None in source_sessions or None in source_turns or len(source_sessions) != 1 or len(source_turns) != 1:
            return False
        items = verification_state.evidence_items
        failed_indices = [
            index
            for index, item in enumerate(items)
            if item.level.value == "validation" and not item.ok
        ]
        if not failed_indices:
            return True
        # A failed validation can only teach a recovery when an actual change
        # follows it and a later validation then succeeds.
        latest_failure = failed_indices[-1]
        changed_after_failure = any(
            item.level.value == "change" for item in items[latest_failure + 1 :]
        )
        validated_after_failure = any(
            item.supports_experience for item in items[latest_failure + 1 :]
        )
        return changed_after_failure and validated_after_failure

    # 只生成条件/动作/验证结果模板，不把完整任务 Transcript 写入记忆。
    @staticmethod
    def _experience_content(
        verification_state: TurnVerificationState | None,
        task_description: str,
    ) -> str | None:
        if verification_state is None:
            return None
        evidence = verification_state.evidence_for_experience()
        if not evidence:
            return None
        strongest = evidence[-1]
        has_failed_validation = any(
            item.level.value == "validation" and not item.ok
            for item in verification_state.evidence_items
        )
        condition = (
            f"when a repository change needs {strongest.tool_name} {strongest.kind.value} before delivery"
        )
        action = (
            f"after a failed validation, apply a scoped repair and rerun {strongest.tool_name}"
            if has_failed_validation
            else f"run the targeted {strongest.tool_name} validation after the scoped change"
        )
        # Do not store the task transcript.  The evidence digest remains the
        # traceable, bounded result reference.
        del task_description
        return (
            f"Applicable condition: {condition}.\n"
            f"Effective action: {action}.\n"
            f"Verification result: {strongest.summary}."
        )

    def _extract_task_context(
        self, tool_calls: list[dict[str, Any]], assistant_msgs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Extract structured task context from execution trace.

        Produces:  files, libraries, tools, project_state
        These become the task_context field on ReflectionResult, which is
        persisted as a domain-tagged 'task_context' memory entry.
        """
        context: dict[str, Any] = {}

        # Extract file paths from tool calls (read_file, write_file, edit_file)
        files: set[str] = set()
        libraries: set[str] = set()
        tool_names: set[str] = set()

        for call in tool_calls:
            name = call.get("name", call.get("toolName", ""))
            if name:
                tool_names.add(name)
            # Try to detect file paths
            for key in ("path", "filePath", "file_path", "input"):
                val = call.get(key, "")
                if isinstance(val, str) and ("." in val or "/" in val):
                    files.add(val)
                elif isinstance(val, dict):
                    for v in val.values():
                        if isinstance(v, str) and ("." in v or "/" in v):
                            files.add(v)

        # Detect libraries from tool calls (npm, pip, cargo, etc.)
        known_libs = {
            "react", "vue", "angular", "svelte", "next", "nuxt",
            "express", "fastapi", "flask", "django", "spring", "gin",
            "prisma", "typeorm", "sequelize", "drizzle",
            "zustand", "redux", "mobx", "jotai",
            "tailwind", "bootstrap", "material-ui", "chakra",
            "jest", "vitest", "pytest", "mocha",
            "docker", "kubernetes", "terraform",
        }
        for msg in assistant_msgs:
            content = msg.get("content", "").lower()
            for lib in known_libs:
                if lib in content:
                    libraries.add(lib)

        if files:
            context["files"] = sorted(files)[:10]
        if libraries:
            context["libraries"] = sorted(libraries)[:10]
        if tool_names:
            context["tools"] = sorted(tool_names)[:10]

        return context

    def _extract_decisions(self, assistant_msgs: list[dict[str, Any]]) -> list[str]:
        """Extract key decisions from assistant messages."""
        decisions = []
        keywords = ["decide", "choose", "select", "use ", "will ", "plan to", "start by"]
        for msg in assistant_msgs:
            content = msg.get("content", "")
            if any(kw in content.lower() for kw in keywords):
                first_sentence = content.split(".")[0].strip()
                if len(first_sentence) > 10:
                    decisions.append(first_sentence[:200])
        return decisions[:5]

    def _generate_lessons(
        self,
        *,
        verification_state: TurnVerificationState | None,
        task_description: str,
        eligible: bool,
    ) -> list[str]:
        """Return at most one reusable, evidence-backed lesson preview."""

        if not eligible:
            return []
        content = self._experience_content(verification_state, task_description)
        return [content] if content else []

    def _generate_improvements(
        self,
        tool_calls: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        metrics: Any | None,
    ) -> list[str]:
        """Generate improvement suggestions."""
        improvements = []

        if len(errors) > 2:
            improvements.append("High error rate detected. Consider breaking task into smaller steps.")

        if len(tool_calls) > 10:
            improvements.append("Many tool calls used. Consider more efficient approaches or better planning.")

        if metrics and hasattr(metrics, "get_summary"):
            try:
                stats = metrics.get_summary()
                if stats.get("overall_success_rate", 1.0) < 0.7:
                    improvements.append(
                        "Low success rate. Review tool usage patterns and error recovery strategies."
                    )
            except Exception:
                pass

        return improvements

    def _calculate_confidence(
        self,
        success: bool,
        error_count: int,
        tool_count: int,
    ) -> float:
        """Calculate reflection confidence score."""
        base = 0.8 if success else 0.4
        error_penalty = min(error_count * 0.1, 0.3)
        tool_bonus = min(tool_count * 0.02, 0.1)
        return max(0.0, min(1.0, base - error_penalty + tool_bonus))

    # 通过 propose_experience 保存 pending；绝不能使用旧 add_entry 直接 active。
    def _persist_reflection(
        self,
        reflection: ReflectionResult,
        *,
        content: str,
        evidence: tuple[VerificationEvidence, ...],
    ) -> None:
        """Save reflection to long-term memory."""
        if self.memory is None:
            return

        try:
            propose = getattr(self.memory, "propose_experience", None)
            if not callable(propose):
                logger.warning("Reflection memory service does not support propose_experience()")
                return
            propose(
                content=content,
                scope=Scope.PROJECT,
                kind=Kind.LESSON,
                context=self.memory.context(),
                evidence=evidence,
            )
            logger.info("Reflection proposed as pending (confidence: %.2f)", reflection.confidence)
        except Exception as e:
            logger.warning("Failed to persist reflection: %s", e)
