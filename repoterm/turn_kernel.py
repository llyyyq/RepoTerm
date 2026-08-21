from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
import re
from typing import Any, Callable, Literal

from repoterm.layered_context import ContextBuilder, LayeredContext
from repoterm.memory.models import EvidenceKind, EvidenceLevel, VerificationEvidence
from repoterm.task_object import TaskState
from repoterm.types import RuntimeEventCategory

TurnStopReason = Literal[
    "done",
    "max_steps",
    "await_user",
    "blocked",
    "verification_failed",
    "widen_needed",
]

TurnStepPhase = Literal["explore", "execute", "verify"]

_TEST_COMMAND_RE = re.compile(
    r"^\s*(?:pytest|py\.test|python(?:3(?:\.\d+)?)?\s+-m\s+pytest|"
    r"npm\s+(?:test|run\s+test)|yarn\s+test|pnpm\s+test|cargo\s+test|"
    r"go\s+test|mvn\s+test|gradle\s+test|tox|nox|make\s+test)(?:\s|$)",
    re.IGNORECASE,
)
_BUILD_COMMAND_RE = re.compile(
    r"^\s*(?:python(?:3(?:\.\d+)?)?\s+-m\s+build|npm\s+run\s+build|"
    r"yarn\s+build|pnpm\s+build|cargo\s+build|go\s+build|mvn\s+(?:package|verify)|"
    r"gradle\s+(?:build|assemble)|make(?:\s+build)?)(?:\s|$)",
    re.IGNORECASE,
)
_STATIC_CHECK_COMMAND_RE = re.compile(
    r"^\s*(?:ruff(?:\s+check)?|flake8|mypy|pyright|pylint|eslint|"
    r"black\s+--check|isort\s+--check|python(?:3(?:\.\d+)?)?\s+-m\s+(?:ruff|mypy))(?:\s|$)",
    re.IGNORECASE,
)


def _tool_input_text(tool_input: object) -> str:
    """Extract command semantics without retaining the original input."""

    if isinstance(tool_input, dict):
        for key in ("cmd", "command", "script", "args"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple)):
                return " ".join(str(item) for item in value)
        return ""
    return str(tool_input or "")


def _validation_category(tool_name: str, tool_input: object) -> tuple[EvidenceLevel, EvidenceKind]:
    """Classify a tool semantically; command exit status alone is insufficient."""

    normalized_name = str(tool_name or "").strip().lower()
    command = _tool_input_text(tool_input)
    test_tool_names = {
        "pytest",
        "pytest_probe",
        "test_runner",
        "run_tests",
        "test",
        "verification",
    }
    if (
        normalized_name in test_tool_names
        or normalized_name.endswith("_verification")
        or _TEST_COMMAND_RE.search(command)
    ):
        return EvidenceLevel.VALIDATION, EvidenceKind.TEST
    if "build" in normalized_name or _BUILD_COMMAND_RE.search(command):
        return EvidenceLevel.VALIDATION, EvidenceKind.BUILD
    if (
        any(token in normalized_name for token in ("lint", "typecheck", "type_check", "static"))
        or _STATIC_CHECK_COMMAND_RE.search(command)
    ):
        return EvidenceLevel.VALIDATION, EvidenceKind.STATIC_CHECK
    if "schema" in normalized_name or "migration" in normalized_name:
        return EvidenceLevel.VALIDATION, EvidenceKind.SCHEMA
    if any(token in normalized_name for token in ("file_assert", "assert_file", "check_file")):
        return EvidenceLevel.VALIDATION, EvidenceKind.FILE_ASSERT
    if "recovery" in normalized_name:
        return EvidenceLevel.VALIDATION, EvidenceKind.RECOVERY
    if any(token in normalized_name for token in ("read", "grep", "search", "list", "glob")):
        if "search" in normalized_name or "grep" in normalized_name:
            return EvidenceLevel.OBSERVATION, EvidenceKind.SEARCH
        if "list" in normalized_name or "glob" in normalized_name:
            return EvidenceLevel.OBSERVATION, EvidenceKind.LIST_FILES
        return EvidenceLevel.OBSERVATION, EvidenceKind.READ_FILE
    if any(token in normalized_name for token in ("edit", "write", "patch")):
        return EvidenceLevel.CHANGE, EvidenceKind.EDIT_FILE
    if "diff" in normalized_name:
        return EvidenceLevel.CHANGE, EvidenceKind.DIFF
    if "checkpoint" in normalized_name:
        return EvidenceLevel.CHANGE, EvidenceKind.CHECKPOINT
    return EvidenceLevel.OBSERVATION, EvidenceKind.SHELL


def _safe_evidence_summary(
    *,
    level: EvidenceLevel,
    kind: EvidenceKind,
    tool_name: str,
    ok: bool,
    result_output: str,
) -> str:
    """Produce a digest, not a raw stdout/stderr/diff/code payload."""

    label = str(tool_name or "tool").strip().lower() or "tool"
    if level == EvidenceLevel.VALIDATION:
        outcome = "succeeded" if ok else "failed"
        normalized = " ".join(str(result_output or "").split())
        count = re.search(
            r"\b(\d+)\s+(passed|failed|error(?:s)?)\b",
            normalized,
            re.I,
        )
        if count:
            timing = re.search(r"\bin\s+(\d+(?:\.\d+)?)s\b", normalized, re.I)
            detail = f"{count.group(1)} {count.group(2).lower()}"
            if timing:
                detail += f" in {timing.group(1)}s"
            return f"{label}: {detail}"
        return f"{label}: {kind.value} {outcome}"
    return f"{label}: {kind.value} {'succeeded' if ok else 'failed'}"


# 将工具结果转换为 CHANGE/VALIDATION 证据；read/search/list 等普通观察返回 None。
def classify_tool_result(
    *,
    tool_name: str,
    tool_input: object,
    ok: bool,
    result_output: str,
    source_session_id: str | None,
    source_turn_id: str | None,
) -> VerificationEvidence | None:
    """构造 CHANGE/VALIDATION 证据；普通观察只留在 Trace。

    ``read_file``, search and listing results are useful runtime observations,
    but they are not evidence objects.  The caller records their bounded
    summary separately, so an observation cannot accidentally participate in
    the verification gate or an experience candidate.
    """

    level, kind = _validation_category(tool_name, tool_input)
    if level == EvidenceLevel.OBSERVATION:
        return None
    return VerificationEvidence.create(
        level=level,
        kind=kind,
        tool_name=tool_name,
        ok=ok,
        summary=_safe_evidence_summary(
            level=level,
            kind=kind,
            tool_name=tool_name,
            ok=ok,
            result_output=result_output,
        ),
        tool_input=tool_input,
        source_session_id=source_session_id,
        source_turn_id=source_turn_id,
    )


@dataclass(slots=True)
class TurnBudgetSignals:
    remaining_steps: int | None = None
    hit_max_steps: bool = False
    tool_error_count: int = 0
    saw_tool_result: bool = False


@dataclass(slots=True)
class TurnVerificationState:
    strict: bool = False
    requires_explicit_final: bool = False
    requires_evidence: bool = False
    evidence_ready: bool = False
    evidence_summary: str = ""
    last_verification_note: str = ""
    evidence_items: list[VerificationEvidence] = field(default_factory=list)

    def record_evidence(self, evidence: VerificationEvidence) -> None:
        """记录分类后的证据；只有支持经验的等级才能打开当前 Turn 门禁。"""

        self.evidence_items.append(evidence)
        # Keep per-turn state bounded while retaining the most recent outcome
        # history for recovery-aware reflection.  Experience persistence has a
        # stricter three-digest cap below.
        if len(self.evidence_items) > 24:
            del self.evidence_items[:-24]
        supporting = self.supporting_evidence()
        self.evidence_ready = bool(supporting)
        if supporting:
            self.evidence_summary = supporting[-1].summary

    def supporting_evidence(self) -> tuple[VerificationEvidence, ...]:
        """返回当前 Turn 中所有成功的 VALIDATION/CONFIRMATION 证据。"""
        return tuple(item for item in self.evidence_items if item.supports_experience)

    def evidence_for_experience(self) -> tuple[VerificationEvidence, ...]:
        """返回最多三条可写入经验、且带稳定来源引用的成功证据摘要。"""

        return self.supporting_evidence()[-3:]


@dataclass(slots=True)
class TurnStepPolicy:
    phase: TurnStepPhase = "explore"
    phase_index: int = 0
    remaining_steps: int | None = None
    guidance: str = ""
    verification_focus: str = "light"
    allow_widening: bool = False
    widening_active: bool = False
    widening_reason: str = ""
    widening_evidence_summary: str = ""
    should_compact_aggressively: bool = False

    def terminal_summary(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.guidance:
            parts.append(self.guidance)
        if self.allow_widening:
            if self.widening_reason:
                parts.append(
                    f"widening is now allowed because {self.widening_reason}"
                )
            else:
                parts.append("widening is now allowed if depth stalls")
        if self.widening_active:
            parts.append("widened mode is active")
        if self.should_compact_aggressively:
            parts.append("favor compact evidence over long narration")
        return " | ".join(parts)


@dataclass(slots=True)
class StableTaskPack:
    task_title: str = ""
    task_goal: str = ""
    task_description: str = ""
    intent_type: str = ""
    action_type: str = ""
    task_graph_summary: str = ""
    protected_context: list[str] = field(default_factory=list)
    latest_tool_result_summary: str = ""
    progress_summary: str = ""
    verification_summary: str = ""
    budget_summary: str = ""

    def to_protected_text(self) -> str:
        lines: list[str] = []
        if self.task_title:
            lines.append(f"Task: {self.task_title}")
        if self.task_goal:
            lines.append(f"Goal: {self.task_goal}")
        if self.task_description:
            lines.append(f"Description: {self.task_description}")
        if self.intent_type or self.action_type:
            lines.append(
                f"Intent: {self.intent_type or 'unknown'} / {self.action_type or 'unknown'}"
            )
        if self.task_graph_summary:
            lines.append(f"Task graph: {self.task_graph_summary}")
        if self.progress_summary:
            lines.append(f"Progress: {self.progress_summary}")
        if self.latest_tool_result_summary:
            lines.append(f"Latest tool result: {self.latest_tool_result_summary}")
        if self.verification_summary:
            lines.append(f"Verification: {self.verification_summary}")
        if self.budget_summary:
            lines.append(f"Budget: {self.budget_summary}")
        if self.protected_context:
            lines.append("Protected context:")
            for item in self.protected_context[:5]:
                lines.append(f"- {item[:240]}")
        return "\n".join(lines)


@dataclass(slots=True)
class TurnPreludeState:
    """Prelude artifacts prepared once before the recurrent tool loop."""

    task: Any | None = None
    task_metadata: dict[str, Any] = field(default_factory=dict)
    layered_context: LayeredContext | None = None
    context_builder: ContextBuilder | None = None
    auditor: Any | None = None
    task_graph: Any | None = None
    task_graph_id: str | None = None
    task_slot_key: str | None = None


@dataclass(slots=True)
class TurnRecurrentState:
    """Mutable loop state for a single agent turn."""

    max_steps: int | None
    profile_name: str = "single"
    widen_after_step: int | None = None
    empty_response_retry_limit: int = 2
    recoverable_thinking_retry_limit: int = 3
    saw_tool_result: bool = False
    empty_response_retry_count: int = 0
    recoverable_thinking_retry_count: int = 0
    tool_error_count: int = 0
    tool_observation_count: int = 0
    successful_tool_observation_count: int = 0
    step: int = 0
    widening_active: bool = False
    widening_transition_count: int = 0
    widening_trigger_reason: str = ""
    widening_trigger_evidence: str = ""
    latest_tool_result_summary: str = ""
    progress_state: dict[str, Any] = field(default_factory=dict)
    verification_state: TurnVerificationState = field(default_factory=TurnVerificationState)
    budget_signals: TurnBudgetSignals = field(default_factory=TurnBudgetSignals)
    stop_reason: TurnStopReason | None = None
    stable_task_pack: StableTaskPack | None = None
    step_policy: TurnStepPolicy = field(default_factory=TurnStepPolicy)

    def has_remaining_steps(self) -> bool:
        return self.max_steps is None or self.step < self.max_steps

    def begin_step(self) -> int:
        self.step += 1
        self._refresh_budget_signals()
        return self.step

    def can_retry_empty_response(self) -> bool:
        return self.empty_response_retry_count < self.empty_response_retry_limit

    def record_empty_response_retry(self) -> None:
        self.empty_response_retry_count += 1

    def can_retry_recoverable_thinking(self) -> bool:
        return (
            self.recoverable_thinking_retry_count
            < self.recoverable_thinking_retry_limit
        )

    def record_recoverable_thinking_retry(self) -> None:
        self.recoverable_thinking_retry_count += 1

    def record_tool_result(self, ok: bool, summary: str | None = None) -> None:
        """Record tool observations and errors only.

        This method must not decide that a tool result is validation evidence:
        semantic classification happens in :func:`classify_tool_result`, then
        :meth:`record_verification_evidence` receives the explicit result.
        """

        self.saw_tool_result = True
        self.tool_observation_count += 1
        if ok:
            self.successful_tool_observation_count += 1
        if not ok:
            self.tool_error_count += 1
        if summary:
            normalized = " ".join(summary.split())
            self.latest_tool_result_summary = normalized[:280]
        self._refresh_budget_signals()

    def record_verification_evidence(self, evidence: VerificationEvidence) -> None:
        self.verification_state.record_evidence(evidence)

    def set_progress_summary(self, summary: str) -> None:
        self.progress_state["summary"] = summary[:280]

    def set_stop_reason(self, reason: TurnStopReason) -> None:
        self.stop_reason = reason
        self._refresh_budget_signals()

    def has_verification_evidence(self) -> bool:
        return bool(self.verification_state.supporting_evidence())

    def activate_widening(self, *, extra_steps: int = 0) -> bool:
        if self.widening_active:
            return False
        self.widening_active = True
        self.widening_transition_count += 1
        self.empty_response_retry_count = 0
        self.recoverable_thinking_retry_count = 0
        if extra_steps > 0 and self.max_steps is not None:
            self.max_steps += extra_steps
        self._refresh_budget_signals()
        return True

    def final_task_state(self) -> TaskState:
        if self.stop_reason == "done":
            return (
                TaskState.COMPLETED
                if self.tool_error_count == 0
                else TaskState.FAILED
            )
        if self.stop_reason == "await_user":
            return TaskState.PAUSED
        if self.stop_reason in {
            "max_steps",
            "blocked",
            "verification_failed",
            "widen_needed",
        }:
            return TaskState.FAILED
        return TaskState.COMPLETED if self.tool_error_count == 0 else TaskState.FAILED

    def _refresh_budget_signals(self) -> None:
        remaining_steps = None
        hit_max_steps = False
        if self.max_steps is not None:
            remaining_steps = max(self.max_steps - self.step, 0)
            hit_max_steps = self.step >= self.max_steps
        self.budget_signals = TurnBudgetSignals(
            remaining_steps=remaining_steps,
            hit_max_steps=hit_max_steps,
            tool_error_count=self.tool_error_count,
            saw_tool_result=self.saw_tool_result,
        )


@dataclass(slots=True)
class AssistantTurnDecision:
    """Structured outcome for one assistant response inside the recurrent loop."""

    kind: Literal["progress", "retry", "fallback", "final"]
    assistant_content: str | None = None
    user_content: str | None = None
    protect_final_answer: bool = False
    stop_reason: TurnStopReason | None = None
    runtime_event_category: RuntimeEventCategory | None = None


@dataclass(slots=True)
class ToolTurnDecision:
    kind: Literal["continue", "await_user"]
    assistant_content: str | None = None
    stop_reason: TurnStopReason | None = None
    progress_summary: str = ""


@dataclass(slots=True)
class TurnCodaSummary:
    step: int
    tool_error_count: int
    success: bool
    result_summary: str
    error_rate: float
    avg_latency: float
    context_usage: float
    task_state: TaskState
    stop_reason: TurnStopReason | None


def _summarize_task_graph(task_graph: Any | None, task_slot_key: str | None) -> str:
    if task_graph is None:
        return ""
    progress = 0.0
    try:
        progress = float(task_graph.get_progress_percentage())
    except Exception:
        progress = 0.0
    slot_state = ""
    if task_slot_key:
        try:
            slot = task_graph.slots.get(task_slot_key)
            if slot is not None and getattr(slot, "state", None) is not None:
                slot_state = getattr(slot.state, "value", str(slot.state))
        except Exception:
            slot_state = ""
    parts = [f"progress={progress:.0f}%"]
    if slot_state:
        parts.append(f"slot={slot_state}")
    return ", ".join(parts)


def _derive_widening_signal(
    turn_state: TurnRecurrentState,
    *,
    step: int,
) -> tuple[bool, str, str]:
    if turn_state.widening_active:
        return False, "", ""
    if turn_state.widen_after_step is None or step < turn_state.widen_after_step:
        return False, "", ""

    if turn_state.tool_error_count > 0:
        evidence = (
            turn_state.latest_tool_result_summary
            or f"{turn_state.tool_error_count} tool error(s) observed in this run"
        )
        return True, "tool failures already made the narrow path unstable", evidence[:200]

    if turn_state.has_verification_evidence() and (
        turn_state.empty_response_retry_count > 0
        or turn_state.recoverable_thinking_retry_count > 0
    ):
        evidence = (
            turn_state.latest_tool_result_summary
            or "the narrow path produced evidence but the next step still stalled"
        )
        return True, "the narrow path already produced evidence and then stalled", evidence[:200]

    if (
        not turn_state.saw_tool_result
        and turn_state.empty_response_retry_count >= turn_state.empty_response_retry_limit
    ):
        return (
            True,
            "the model stalled repeatedly before producing new evidence",
            (
                "assistant returned repeated empty responses while the turn stayed on "
                "the same narrow path"
            ),
        )

    if (
        not turn_state.saw_tool_result
        and turn_state.recoverable_thinking_retry_count
        >= turn_state.recoverable_thinking_retry_limit
    ):
        return (
            True,
            "recoverable pauses kept repeating on the same narrow path",
            (
                "the model kept hitting recoverable pause/max-token retries without "
                "producing fresh external evidence"
            ),
        )

    return False, "", ""


def derive_turn_step_policy(turn_state: TurnRecurrentState) -> TurnStepPolicy:
    """Derive the current per-step policy from budget, profile, and progress."""

    step = max(turn_state.step, 1)
    max_steps = turn_state.max_steps or 0
    remaining_steps = turn_state.budget_signals.remaining_steps
    evidence_ready = turn_state.has_verification_evidence()

    verify_after = max(3, ceil(max_steps * 0.7)) if max_steps else 6
    if turn_state.verification_state.strict:
        verify_after = min(verify_after, 4 if max_steps else 4)
    execute_after = 2 if turn_state.profile_name == "single-deep" else 1

    if turn_state.widening_active and not (
        remaining_steps is not None and remaining_steps <= 1
    ):
        phase: TurnStepPhase = "execute"
    elif step <= execute_after:
        phase: TurnStepPhase = "explore"
    elif (
        (max_steps and step >= verify_after)
        or (remaining_steps is not None and remaining_steps <= 2)
        or (
            turn_state.verification_state.strict
            and turn_state.saw_tool_result
            and step >= execute_after + 1
        )
    ):
        phase = "verify"
    else:
        phase = "execute"

    allow_widening, widening_reason, widening_evidence_summary = (
        _derive_widening_signal(turn_state, step=step)
    )

    if turn_state.widening_active:
        guidance = (
            "compare alternative approaches, reuse the evidence you already have, "
            "and avoid repeating the same narrow line of attack"
        )
        verification_focus = "normal"
    elif phase == "explore":
        guidance = "inspect, decompose, and anchor the task before committing"
        verification_focus = "light"
    elif phase == "execute":
        guidance = "prefer concrete tool use and incremental edits"
        verification_focus = "normal"
    else:
        guidance = "verify changes, test evidence, and finalize only with support"
        verification_focus = "strict" if turn_state.verification_state.strict else "normal"

    policy = TurnStepPolicy(
        phase=phase,
        phase_index=step,
        remaining_steps=remaining_steps,
        guidance=guidance,
        verification_focus=verification_focus,
        allow_widening=allow_widening,
        widening_active=turn_state.widening_active,
        widening_reason=widening_reason,
        widening_evidence_summary=widening_evidence_summary,
        should_compact_aggressively=(
            phase == "verify" or allow_widening or turn_state.widening_active
        ),
    )
    turn_state.step_policy = policy
    turn_state.widening_trigger_reason = widening_reason
    turn_state.widening_trigger_evidence = widening_evidence_summary
    turn_state.verification_state.requires_explicit_final = phase == "verify"
    turn_state.verification_state.requires_evidence = (
        phase == "verify"
        and turn_state.verification_state.strict
        and turn_state.saw_tool_result
    )
    turn_state.verification_state.evidence_ready = evidence_ready
    if evidence_ready and not turn_state.verification_state.evidence_summary:
        turn_state.verification_state.evidence_summary = turn_state.latest_tool_result_summary[:200]
    turn_state.verification_state.last_verification_note = (
        f"phase={phase}, verification={verification_focus}, "
        f"widening={'active' if turn_state.widening_active else ('ready' if allow_widening else 'hold')}, "
        f"evidence={'ready' if turn_state.verification_state.evidence_ready else 'missing'}"
    )
    if widening_reason:
        turn_state.verification_state.last_verification_note += (
            f", widening_reason={widening_reason}"
        )
    return policy


def render_turn_policy_message(
    *,
    previous_policy: TurnStepPolicy | None,
    current_policy: TurnStepPolicy,
) -> str | None:
    """Return a compact terminal-visible policy update when the phase meaningfully changes."""

    if previous_policy is not None:
        if (
            previous_policy.phase_index > 0
            and previous_policy.phase == current_policy.phase
            and previous_policy.allow_widening == current_policy.allow_widening
            and previous_policy.widening_active == current_policy.widening_active
        ):
            return None
    message = (
        f"Runtime phase: {current_policy.phase}. {current_policy.guidance} "
        f"(verification={current_policy.verification_focus}, "
        f"remaining_steps="
        f"{'open' if current_policy.remaining_steps is None else current_policy.remaining_steps})."
    )
    if current_policy.allow_widening:
        if current_policy.widening_reason:
            message += (
                " Widening is now available because "
                f"{current_policy.widening_reason}."
            )
        else:
            message += " Widening is now available if the current path keeps stalling."
    if current_policy.widening_active:
        message += " Widened mode is active."
    return message


def _step_aware_followup_nudge(
    *,
    step_policy: TurnStepPolicy | None,
    saw_tool_result: bool,
    nudge_continue: str,
    nudge_after_tool_result: str,
) -> str:
    if step_policy is None:
        return nudge_after_tool_result if saw_tool_result else nudge_continue
    if step_policy.phase == "verify":
        return (
            "You are in verification mode. Use the current evidence to run the most "
            "relevant validation step, summarize the result, and only then finalize "
            "or explain the remaining blocker."
        )
    if step_policy.phase == "explore" and not saw_tool_result:
        return (
            "You are still in exploration mode. Inspect the most relevant files, "
            "tests, or symbols first so the next step is grounded in evidence."
        )
    return nudge_after_tool_result if saw_tool_result else nudge_continue


def _content_mentions_evidence(content: str, evidence_summary: str) -> bool:
    normalized_content = " ".join(content.lower().split())
    if not normalized_content:
        return False
    evidence_markers = (
        "verified",
        "verification",
        "validated",
        "test",
        "tests",
        "checked",
        "inspected",
        "confirmed",
        "according to",
        "based on",
        "tool output",
        "output shows",
        "log shows",
        "diff shows",
        "I ran",
        "I checked",
    )
    if any(marker in normalized_content for marker in evidence_markers):
        return True
    evidence_tokens = [
        token.strip(".,:;()[]{}'\"")
        for token in evidence_summary.lower().split()
        if len(token.strip(".,:;()[]{}'\"")) >= 4
    ]
    overlap = sum(1 for token in set(evidence_tokens[:8]) if token and token in normalized_content)
    return overlap >= 2


def build_verification_evidence_nudge(evidence_summary: str) -> str:
    evidence_fragment = evidence_summary[:180].strip()
    if evidence_fragment:
        return (
            "You are in strict verification mode. Before finalizing, cite the strongest "
            f"evidence from this run, for example: {evidence_fragment}. If that evidence "
            "is insufficient, run one more validation step or state the exact blocker."
        )
    return (
        "You are in strict verification mode. Before finalizing, summarize the concrete "
        "evidence from this run or state the exact blocker. Do not end with an unsupported conclusion."
    )


def build_widening_transition_nudge(
    latest_tool_result_summary: str,
    *,
    widening_reason: str = "",
    widening_evidence_summary: str = "",
) -> str:
    evidence_fragment = (
        widening_evidence_summary[:180].strip()
        or latest_tool_result_summary[:180].strip()
    )
    reason_fragment = widening_reason.strip()
    if evidence_fragment:
        lead = (
            "Switch to widened mode because "
            f"{reason_fragment}. "
            if reason_fragment
            else "Switch to widened mode. "
        )
        return (
            lead
            + "Do not keep pushing the same narrow path. Compare at least two "
            "alternative approaches, reuse the strongest evidence already gathered, "
            f"and choose the next step grounded in this run: {evidence_fragment}"
        )
    if reason_fragment:
        return (
            "Switch to widened mode because "
            f"{reason_fragment}. Do not keep pushing the same narrow path. Compare "
            "at least two alternative approaches, inspect a different source of "
            "evidence, and then choose the most promising next step."
        )
    return (
        "Switch to widened mode. Do not keep pushing the same narrow path. Compare at least "
        "two alternative approaches, inspect a different source of evidence, and then choose "
        "the most promising next step."
    )


def build_stable_task_pack(
    *,
    task: Any | None,
    task_metadata: dict[str, Any] | None,
    protected_context: list[str] | None,
    task_graph: Any | None,
    task_slot_key: str | None,
    latest_tool_result_summary: str,
    progress_state: dict[str, Any] | None,
    verification_state: TurnVerificationState | None,
    budget_signals: TurnBudgetSignals | None,
) -> StableTaskPack | None:
    if task is None and not protected_context and not latest_tool_result_summary:
        return None

    metadata = task_metadata or {}
    progress_summary = ""
    if progress_state:
        progress_summary = str(progress_state.get("summary", ""))

    verification_summary = ""
    if verification_state:
        verification_parts = []
        if verification_state.strict:
            verification_parts.append("strict")
        if verification_state.requires_explicit_final:
            verification_parts.append("explicit-final")
        if verification_state.requires_evidence:
            verification_parts.append("evidence-required")
        if verification_state.evidence_ready:
            verification_parts.append("evidence-ready")
        if verification_state.evidence_summary:
            verification_parts.append(f"evidence={verification_state.evidence_summary[:120]}")
        if verification_state.last_verification_note:
            verification_parts.append(verification_state.last_verification_note)
        verification_summary = ", ".join(verification_parts)

    budget_summary = ""
    if budget_signals:
        remaining = (
            "open"
            if budget_signals.remaining_steps is None
            else str(budget_signals.remaining_steps)
        )
        budget_summary = (
            f"remaining_steps={remaining}, "
            f"tool_errors={budget_signals.tool_error_count}, "
            f"saw_tool_result={budget_signals.saw_tool_result}"
        )

    return StableTaskPack(
        task_title=str(getattr(task, "title", "") or ""),
        task_goal=str(getattr(task, "goal", "") or ""),
        task_description=str(getattr(task, "description", "") or ""),
        intent_type=str(metadata.get("intent_type", "") or ""),
        action_type=str(metadata.get("action_type", "") or ""),
        task_graph_summary=_summarize_task_graph(task_graph, task_slot_key),
        protected_context=list(protected_context or []),
        latest_tool_result_summary=latest_tool_result_summary,
        progress_summary=progress_summary,
        verification_summary=verification_summary,
        budget_summary=budget_summary,
    )


def build_turn_coda_summary(
    *,
    turn_state: TurnRecurrentState,
    context_usage: float,
) -> TurnCodaSummary:
    """Build a normalized turn summary for coda/finalization logic."""

    task_state = turn_state.final_task_state()
    success = task_state is TaskState.COMPLETED
    if turn_state.stop_reason == "await_user":
        result_summary = (
            f"Turn paused after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    elif turn_state.stop_reason == "max_steps":
        result_summary = (
            f"Turn stopped at the max step budget after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    else:
        result_summary = (
            f"Turn finished with stop_reason={turn_state.stop_reason or 'implicit'}, "
            f"{turn_state.step} steps, {turn_state.tool_error_count} errors"
        )
    return TurnCodaSummary(
        step=turn_state.step,
        tool_error_count=turn_state.tool_error_count,
        success=success,
        result_summary=result_summary,
        error_rate=turn_state.tool_error_count / max(turn_state.step, 1),
        avg_latency=turn_state.step * 2.0,
        context_usage=context_usage,
        task_state=task_state,
        stop_reason=turn_state.stop_reason,
    )


def finalize_work_chain_task(
    *,
    task: Any | None,
    auditor: Any | None,
    coda_summary: TurnCodaSummary,
    success_outcome: Any,
    failure_outcome: Any,
) -> None:
    """Apply final task state and audit completion during coda."""

    if task is None:
        return

    task.set_state(coda_summary.task_state)
    task.result_summary = coda_summary.result_summary

    if auditor is None:
        return

    auditor.complete_decision(
        success_outcome if coda_summary.success else failure_outcome,
        coda_summary.step * 100.0,
        task.result_summary,
        task.error_message if not coda_summary.success else "",
    )


def decide_assistant_turn(
    *,
    turn_state: TurnRecurrentState,
    step_content: str,
    step_kind: str | None,
    stop_reason: str | None,
    block_types: list[str] | None,
    ignored_block_types: list[str] | None,
    is_empty: bool,
    treat_as_progress: bool,
    is_recoverable_thinking_stop: bool,
    format_diagnostics: Callable[[str | None, list[str] | None, list[str] | None], str],
    nudge_continue: str,
    nudge_after_tool_result: str,
    resume_after_pause: str,
    resume_after_max_tokens: str,
    nudge_after_empty_response: str,
    nudge_after_empty_no_tools: str,
    step_policy: TurnStepPolicy | None = None,
) -> AssistantTurnDecision:
    """Decide how the loop should react to an assistant-only step."""

    if treat_as_progress:
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=step_content,
            user_content=_step_aware_followup_nudge(
                step_policy=step_policy,
                saw_tool_result=turn_state.saw_tool_result and step_kind != "progress",
                nudge_continue=nudge_continue,
                nudge_after_tool_result=nudge_after_tool_result,
            ),
        )

    if is_recoverable_thinking_stop and turn_state.can_retry_recoverable_thinking():
        turn_state.record_recoverable_thinking_retry()
        progress_content = (
            "Model hit max_tokens during thinking; requesting the next step."
            if stop_reason == "max_tokens"
            else "Model returned pause_turn; requesting the next step."
        )
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=progress_content,
            user_content=(
                resume_after_pause
                if stop_reason == "pause_turn"
                else resume_after_max_tokens
            ),
            runtime_event_category="recovery",
        )

    if is_empty and turn_state.can_retry_empty_response():
        turn_state.record_empty_response_retry()
        retry_nudge = (
            "Your last response was empty during verification mode. Resume with a "
            "single concrete validation step or state the exact blocker."
            if step_policy is not None and step_policy.phase == "verify"
            else (
                "Your last response was empty after the current line of attack stalled. "
                "Resume with one wider search step or explicitly compare the next two options."
                if step_policy is not None and step_policy.allow_widening
                else (
                    nudge_after_empty_response
                    if turn_state.saw_tool_result
                    else nudge_after_empty_no_tools
                )
            )
        )
        return AssistantTurnDecision(
            kind="retry",
            user_content=retry_nudge,
        )

    if is_empty:
        late_verify = bool(
            step_policy is not None
            and step_policy.phase == "verify"
            and turn_state.verification_state.requires_explicit_final
        )
        widen_ready = bool(step_policy is not None and step_policy.allow_widening)
        diagnostics_suffix = format_diagnostics(
            stop_reason,
            block_types,
            ignored_block_types,
        )
        if turn_state.saw_tool_result:
            fallback = (
                "Model returned an empty response after tool execution and the turn "
                "was stopped. There were "
                f"{turn_state.tool_error_count} tool error(s); retry, adjust the "
                f"command, or choose a different approach.{diagnostics_suffix}"
                if turn_state.tool_error_count > 0
                else "Model returned an empty response after tool execution and the "
                "turn was stopped. Retry or ask the model to continue the remaining "
                f"steps.{diagnostics_suffix}"
            )
        else:
            fallback = (
                "Model returned an empty response and the turn was stopped."
                f"{diagnostics_suffix}"
            )
        typed_stop_reason: TurnStopReason = "blocked"
        if late_verify and turn_state.saw_tool_result:
            typed_stop_reason = "verification_failed"
            fallback += (
                " The turn had already shifted into verification mode, so this run "
                "ended as a verification failure rather than an ordinary block."
            )
        elif widen_ready:
            typed_stop_reason = "widen_needed"
            fallback += (
                " Depth stopped paying off after repeated pressure, so a wider search "
                "or handoff is now justified."
            )
        return AssistantTurnDecision(
            kind="fallback",
            assistant_content=fallback,
            stop_reason=typed_stop_reason,
        )

    if (
        step_policy is not None
        and step_policy.phase == "verify"
        and turn_state.verification_state.requires_evidence
        and not _content_mentions_evidence(
            step_content,
            turn_state.verification_state.evidence_summary or turn_state.latest_tool_result_summary,
        )
    ):
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=(
                "Verification guard: final answer withheld until it cites concrete "
                "evidence from this run."
            ),
            user_content=build_verification_evidence_nudge(
                turn_state.verification_state.evidence_summary
                or turn_state.latest_tool_result_summary
            ),
            runtime_event_category="guard",
        )

    return AssistantTurnDecision(
        kind="final",
        assistant_content=step_content,
        protect_final_answer=True,
        stop_reason="done",
    )


def decide_tool_turn(
    *,
    tool_name: str,
    result_output: str,
    await_user: bool,
) -> ToolTurnDecision:
    """Keep tool-result ask-user handling on the same typed decision surface."""

    if await_user:
        return ToolTurnDecision(
            kind="await_user",
            assistant_content=result_output,
            stop_reason="await_user",
            progress_summary=f"awaiting user after {tool_name}",
        )
    return ToolTurnDecision(
        kind="continue",
        progress_summary=f"processed tool result from {tool_name}",
    )
