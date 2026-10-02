"""Bounded, tool-agnostic governance of an agent's action/observation trajectory.

This module does not validate tools, grant permissions, or decide whether a
task is complete. It only detects sustained lack of observable progress.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from repoterm.memory.models import EvidenceLevel, VerificationEvidence


GovernanceAction = Literal["allow", "nudge", "require_replan", "stop_stuck"]
PreflightAction = Literal["allow", "defer", "stop_stuck"]
TaskGap = Literal["locate", "verify_change", "review_result"]
RecoveryOutcome = Literal["", "weak_evidence", "not_recovered", "recovered"]


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    action: PreflightAction
    reason: str = ""
    event_id: str = ""

    @property
    def guidance(self) -> str:
        if self.action == "allow":
            return ""
        return (
            "This proposed observation was not executed: the same source has "
            "repeatedly returned already-seen evidence. Choose a different "
            "source or method, inspect the located code, make a justified "
            "change, or run a focused check. A changed query alone is not "
            "a new plan."
        )


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    action: GovernanceAction
    reason: str = ""
    event_id: str = ""
    stagnant_actions: int = 0
    task_gap: TaskGap = "locate"
    recovery_outcome: RecoveryOutcome = ""
    intervention_event_id: str = ""

    @property
    def guidance(self) -> str:
        gap = (
            "The latest change still needs an independent, focused check. "
            if self.task_gap == "verify_change"
            else "Identify what task-relevant evidence is still missing. "
        )
        if self.action == "nudge":
            return (
                gap + "Recent actions did not add task evidence. Compare the latest "
                "observations with what is already known, then choose a "
                "discriminating next action; do not repeat a call solely by "
                "changing its wording."
            )
        if self.action == "require_replan":
            return (
                gap + "The current approach is not advancing. Before another action, "
                "state one falsifiable hypothesis and choose a different "
                "information source, a justified change, or a focused check. "
                "If repetition is necessary, explain the bounded wait."
            )
        return ""


def _digest(value: object) -> str:
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = repr(type(value))
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()


_LOCATION = re.compile(r"^(.+):(\d+):\s", re.MULTILINE)
_FILE_HEADER = re.compile(r"(?m)^FILE:\s*(.+)$")
_RANGE_HEADER = re.compile(r"(?m)^START_LINE:\s*(\d+)$")
_QUERY_WORD = re.compile(r"\w+", re.UNICODE)
_SCOPE_FIELDS = frozenset({"path", "file_path", "root", "directory", "url"})


def _observation_keys(output: str) -> tuple[str, ...]:
    """Extract bounded source locations, regardless of which tool found them.

    An opaque output remains a weak observation. Changing search wording or
    snippets at an already-known location cannot manufacture new locations.
    """

    locations = {(path.strip(), int(line)) for path, line in _LOCATION.findall(output)}
    file_match = _FILE_HEADER.search(output)
    range_match = _RANGE_HEADER.search(output)
    if file_match and range_match:
        locations.add((file_match.group(1).strip(), int(range_match.group(1))))
    if locations:
        return tuple(sorted(_digest(location) for location in locations)[:128])
    return (_digest(" ".join(output.split())),)


def _action_scope(tool_input: object) -> str | None:
    """Scope a read-only observation without retaining raw paths or queries."""

    if not isinstance(tool_input, dict):
        return None
    for key in ("path", "file_path", "root", "directory", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _digest((key, value.strip().replace("\\", "/").casefold()))
    return None


def _question_tokens(tool_input: object) -> frozenset[str]:
    """Hash non-scope argument words for bounded similarity comparisons."""

    if not isinstance(tool_input, dict):
        return frozenset()
    values = [value for key, value in tool_input.items() if key not in _SCOPE_FIELDS]
    words: set[str] = set()
    for value in values:
        for word in _QUERY_WORD.findall(str(value).casefold()):
            words.add(_digest(word))
            if len(words) >= 64:
                return frozenset(words)
    return frozenset(words)


def _similar_question(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return not left and not right
    return len(left & right) / len(left | right) >= 0.65


@dataclass(slots=True)
class ProgressGovernor:
    """Use only observable novelty; keep no raw source or tool output.

    The thresholds are conservative initial limits. They are independent of
    tool names and should be calibrated against replayed real trajectories.
    """

    nudge_after: int = 4
    replan_after: int = 6
    stop_after: int = 8
    stagnant_actions: int = 0
    stage: GovernanceAction = "allow"
    task_gap: TaskGap = "locate"
    weak_observation_credit: int = 3
    _weak_observations_since_progress: int = 0
    _repeated_actions: int = 0
    _same_observation_streak: int = 0
    _last_observation_key: str = ""
    _seen_observations: dict[str, None] = field(default_factory=dict)
    _seen_changes: dict[str, None] = field(default_factory=dict)
    _seen_validations: dict[str, None] = field(default_factory=dict)
    _unproductive_scopes: dict[tuple[str, str], int] = field(default_factory=dict)
    _unproductive_questions: dict[tuple[str, str], deque[frozenset[str]]] = field(
        default_factory=dict
    )
    _deferred_scopes: dict[tuple[str, str], int] = field(default_factory=dict)
    _recent_pairs: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=8)
    )
    _last_event_id: str = ""
    _intervention_event_id: str = ""
    _followup_actions: int = 0
    _reported_weak_evidence: bool = False
    _reported_not_recovered: bool = False

    STATE_VERSION = 1

    def to_state(self) -> dict[str, Any]:
        """Return a bounded JSON-safe checkpoint for Session persistence."""

        return {
            "version": self.STATE_VERSION,
            "stagnant_actions": self.stagnant_actions,
            "stage": self.stage,
            "task_gap": self.task_gap,
            "weak_observations_since_progress": self._weak_observations_since_progress,
            "repeated_actions": self._repeated_actions,
            "same_observation_streak": self._same_observation_streak,
            "last_observation_key": self._last_observation_key,
            "seen_observations": list(self._seen_observations)[-128:],
            "seen_changes": list(self._seen_changes)[-128:],
            "seen_validations": list(self._seen_validations)[-128:],
            "unproductive_scopes": [
                [tool, scope, count]
                for (tool, scope), count in list(self._unproductive_scopes.items())[-128:]
            ],
            "unproductive_questions": [
                [tool, scope, [sorted(question) for question in questions]]
                for (tool, scope), questions in list(self._unproductive_questions.items())[-128:]
            ],
            "deferred_scopes": [
                [tool, scope, count]
                for (tool, scope), count in list(self._deferred_scopes.items())[-128:]
            ],
            "recent_pairs": [list(pair) for pair in self._recent_pairs],
            "last_event_id": self._last_event_id,
            "intervention_event_id": self._intervention_event_id,
            "followup_actions": self._followup_actions,
            "reported_weak_evidence": self._reported_weak_evidence,
            "reported_not_recovered": self._reported_not_recovered,
        }

    @classmethod
    def from_state(cls, raw: object) -> "ProgressGovernor":
        """Restore a checkpoint conservatively; malformed legacy data resets."""

        governor = cls()
        if not isinstance(raw, Mapping) or raw.get("version") != cls.STATE_VERSION:
            return governor
        try:
            stage = str(raw.get("stage", "allow"))
            task_gap = str(raw.get("task_gap", "locate"))
            if stage not in {"allow", "nudge", "require_replan", "stop_stuck"}:
                return governor
            if task_gap not in {"locate", "verify_change", "review_result"}:
                return governor
            governor.stage = stage  # type: ignore[assignment]
            governor.task_gap = task_gap  # type: ignore[assignment]
            governor.stagnant_actions = max(0, int(raw.get("stagnant_actions", 0)))
            governor._weak_observations_since_progress = max(
                0, int(raw.get("weak_observations_since_progress", 0))
            )
            governor._repeated_actions = max(0, int(raw.get("repeated_actions", 0)))
            governor._same_observation_streak = max(
                0, int(raw.get("same_observation_streak", 0))
            )
            governor._last_observation_key = str(raw.get("last_observation_key", ""))
            governor._seen_observations = {
                str(item): None for item in list(raw.get("seen_observations", []))[-128:]
            }
            governor._seen_changes = {
                str(item): None for item in list(raw.get("seen_changes", []))[-128:]
            }
            governor._seen_validations = {
                str(item): None for item in list(raw.get("seen_validations", []))[-128:]
            }
            governor._unproductive_scopes = {
                (str(item[0]), str(item[1])): max(0, int(item[2]))
                for item in list(raw.get("unproductive_scopes", []))[-128:]
                if isinstance(item, list) and len(item) == 3
            }
            governor._unproductive_questions = {
                (str(item[0]), str(item[1])): deque(
                    (frozenset(str(token) for token in question) for question in item[2]),
                    maxlen=4,
                )
                for item in list(raw.get("unproductive_questions", []))[-128:]
                if isinstance(item, list) and len(item) == 3 and isinstance(item[2], list)
            }
            governor._deferred_scopes = {
                (str(item[0]), str(item[1])): max(0, int(item[2]))
                for item in list(raw.get("deferred_scopes", []))[-128:]
                if isinstance(item, list) and len(item) == 3
            }
            governor._recent_pairs = deque(
                (
                    (str(item[0]), str(item[1]))
                    for item in list(raw.get("recent_pairs", []))[-8:]
                    if isinstance(item, list) and len(item) == 2
                ),
                maxlen=8,
            )
            governor._last_event_id = str(raw.get("last_event_id", ""))
            governor._intervention_event_id = str(raw.get("intervention_event_id", ""))
            governor._followup_actions = max(0, int(raw.get("followup_actions", 0)))
            governor._reported_weak_evidence = bool(raw.get("reported_weak_evidence", False))
            governor._reported_not_recovered = bool(raw.get("reported_not_recovered", False))
        except (TypeError, ValueError):
            return cls()
        return governor

    def note_task_state(self, *, changed: bool, verified: bool) -> None:
        """Use the turn's existing evidence ledger; do not infer success from prose."""

        self.task_gap = (
            "verify_change" if changed and not verified
            else "review_result" if verified else "locate"
        )

    def before_action(
        self,
        *,
        event_id: str,
        tool_name: str,
        tool_input: object,
        read_only: bool,
        poll_remaining: int = 0,
    ) -> PreflightDecision:
        """Defer one repeatedly unproductive observation, never a tool class.

        This is a recovery hold, not a permission denial or a Tool Runtime
        failure. Unknown scopes, changes and verification are never held.
        """

        if self.stage != "require_replan" or not read_only or poll_remaining > 0:
            return PreflightDecision("allow", event_id=event_id)
        scope = _action_scope(tool_input)
        if scope is None:
            return PreflightDecision("allow", event_id=event_id)
        key = (tool_name, scope)
        if self._unproductive_scopes.get(key, 0) < 3:
            return PreflightDecision("allow", event_id=event_id)
        question = _question_tokens(tool_input)
        if not any(
            _similar_question(question, previous)
            for previous in self._unproductive_questions.get(key, ())
        ):
            return PreflightDecision("allow", "distinct_question", event_id)
        count = self._deferred_scopes.get(key, 0) + 1
        self._deferred_scopes[key] = count
        if count > 1:
            return PreflightDecision("stop_stuck", "repeated_deferred_action", event_id)
        return PreflightDecision("defer", "repeated_observation_scope", event_id)

    def observe(
        self,
        *,
        event_id: str,
        tool_name: str,
        tool_input: object,
        output: str,
        ok: bool,
        evidence: VerificationEvidence | None = None,
        poll_remaining: int = 0,
    ) -> GovernanceDecision:
        """Observe a completed action; tool failures belong to Tool Runtime."""

        self._last_event_id = event_id
        if not ok and (evidence is None or evidence.level != EvidenceLevel.VALIDATION):
            return self._decision("allow", "tool_error_out_of_scope")
        if poll_remaining > 0:
            return self._decision("allow", "bounded_poll")

        action_key = _digest((tool_name, tool_input))
        observation_keys = _observation_keys(str(output))
        observation_key = _digest(observation_keys)
        pair = (action_key, observation_key)

        novel = False
        if evidence is not None and evidence.ok and evidence.level == EvidenceLevel.CHANGE:
            key = evidence.fingerprint
            novel = key not in self._seen_changes
            self._remember(self._seen_changes, key)
            if novel:
                recovered_from = self._intervention_event_id
                self._reset_stagnation()
                return self._decision("allow", "new_change", "recovered" if recovered_from else "", recovered_from)
        elif (
            evidence is not None
            and evidence.ok
            and evidence.level == EvidenceLevel.VALIDATION
            and evidence.supports_experience
        ):
            validation_key = _digest((evidence.fingerprint, observation_key))
            novel = validation_key not in self._seen_validations
            self._remember(self._seen_validations, validation_key)
            if novel:
                recovered_from = self._intervention_event_id
                self._reset_stagnation()
                return self._decision("allow", "new_validation", "recovered" if recovered_from else "", recovered_from)
        else:
            # A location or result not seen before is a useful but weak
            # observation; it must not erase an existing recovery episode.
            novel = any(key not in self._seen_observations for key in observation_keys)
            for key in observation_keys:
                self._remember(self._seen_observations, key)

        if novel:
            outcome = self._followup(weak=True)
            # A distinct observation does not prove task progress, but it does
            # break a consecutive action/observation loop.
            self._repeated_actions = 0
            self._same_observation_streak = 0
            self._last_observation_key = observation_key
            self._recent_pairs.clear()
            self._weak_observations_since_progress += 1
            scope = _action_scope(tool_input)
            if scope is not None and self._weak_observations_since_progress <= self.weak_observation_credit:
                scope_key = (tool_name, scope)
                self._unproductive_scopes.pop(scope_key, None)
                self._unproductive_questions.pop(scope_key, None)
                self._deferred_scopes.pop(scope_key, None)
            # A fresh location is useful for initial orientation, but it is
            # not equivalent to a change or a successful check. A stream of
            # ever-new grep hits must eventually trigger a change of method.
            if self._weak_observations_since_progress <= self.weak_observation_credit:
                return self._decision("allow", "new_observation", outcome)
            self._mark_unproductive(tool_name, tool_input)
            return self._record_no_progress(pair, weak=True, recovery_outcome=outcome)

        outcome = self._followup(weak=False)
        self._mark_unproductive(tool_name, tool_input)
        self._repeated_actions += 1
        self._same_observation_streak = (
            self._same_observation_streak + 1
            if observation_key == self._last_observation_key else 1
        )
        self._last_observation_key = observation_key
        return self._record_no_progress(pair, recovery_outcome=outcome)

    def _mark_unproductive(self, tool_name: str, tool_input: object) -> None:
        scope = _action_scope(tool_input)
        if scope is not None:
            key = (tool_name, scope)
            self._unproductive_scopes[key] = self._unproductive_scopes.get(key, 0) + 1
            questions = self._unproductive_questions.setdefault(key, deque(maxlen=4))
            questions.append(_question_tokens(tool_input))
            if len(self._unproductive_scopes) > 128:
                oldest = next(iter(self._unproductive_scopes))
                del self._unproductive_scopes[oldest]
                self._unproductive_questions.pop(oldest, None)

    def observe_progress_message(
        self, *, event_id: str, content: str
    ) -> GovernanceDecision:
        """A model progress message is not external task evidence."""

        self._last_event_id = event_id
        digest = _digest(" ".join(content.split()))
        outcome = self._followup(weak=False)
        self._repeated_actions += 1
        self._same_observation_streak = (
            self._same_observation_streak + 1
            if digest == self._last_observation_key else 1
        )
        self._last_observation_key = digest
        return self._record_no_progress(("assistant_progress", digest), recovery_outcome=outcome)

    def _followup(self, *, weak: bool) -> RecoveryOutcome:
        """Assess the first bounded actions after a nudge, without trusting prose."""

        if not self._intervention_event_id:
            return ""
        self._followup_actions += 1
        if weak and not self._reported_weak_evidence:
            self._reported_weak_evidence = True
            return "weak_evidence"
        if self._followup_actions >= 2 and not self._reported_not_recovered:
            self._reported_not_recovered = True
            return "not_recovered"
        return ""

    def _record_no_progress(
        self, pair: tuple[str, str], *, weak: bool = False,
        recovery_outcome: RecoveryOutcome = "",
    ) -> GovernanceDecision:
        self.stagnant_actions += 1
        self._recent_pairs.append(pair)
        exact_loop = (
            len(self._recent_pairs) >= 3
            and len(set(list(self._recent_pairs)[-3:])) == 1
        )
        alternating_loop = (
            len(self._recent_pairs) >= 4
            and self._recent_pairs[-4] == self._recent_pairs[-2]
            and self._recent_pairs[-3] == self._recent_pairs[-1]
            and self._recent_pairs[-4] != self._recent_pairs[-3]
        )
        # Exploration age can request a decision, but cannot prove a loop.
        # Stop only when a repeated action/observation cycle persists through
        # the recovery window. A new observation breaks that cycle above.
        if (
            self.stage == "require_replan"
            and self.stagnant_actions >= self.stop_after
            and not weak
            and (exact_loop or alternating_loop or self._same_observation_streak >= self.stop_after)
        ):
            self.stage = "stop_stuck"
            return self._decision("stop_stuck", "recovery_exhausted", recovery_outcome)
        if self.stagnant_actions >= self.replan_after:
            if self.stage != "require_replan":
                self.stage = "require_replan"
                return self._decision("require_replan", "no_progress_after_nudge", recovery_outcome)
            return self._decision("allow", "replan_window", recovery_outcome)
        if self.stagnant_actions >= self.nudge_after or exact_loop or alternating_loop:
            if self.stage == "allow":
                self.stage = "nudge"
                self._intervention_event_id = self._last_event_id
                self._followup_actions = 0
                self._reported_weak_evidence = False
                self._reported_not_recovered = False
                return self._decision("nudge", "repeated_trajectory")
        return self._decision("allow", "watching", recovery_outcome)

    def _decision(
        self, action: GovernanceAction, reason: str,
        recovery_outcome: RecoveryOutcome = "", intervention_event_id: str = "",
    ) -> GovernanceDecision:
        return GovernanceDecision(
            action=action,
            reason=reason,
            event_id=self._last_event_id,
            stagnant_actions=self.stagnant_actions,
            task_gap=self.task_gap,
            recovery_outcome=recovery_outcome,
            intervention_event_id=intervention_event_id or self._intervention_event_id,
        )

    def _reset_stagnation(self) -> None:
        self.stagnant_actions = 0
        self._weak_observations_since_progress = 0
        self._repeated_actions = 0
        self._same_observation_streak = 0
        self._last_observation_key = ""
        self.stage = "allow"
        self._recent_pairs.clear()
        self._unproductive_scopes.clear()
        self._unproductive_questions.clear()
        self._deferred_scopes.clear()
        self._intervention_event_id = ""
        self._followup_actions = 0
        self._reported_weak_evidence = False
        self._reported_not_recovered = False

    @staticmethod
    def _remember(seen: dict[str, None], key: str) -> None:
        seen[key] = None
        if len(seen) > 128:
            del seen[next(iter(seen))]
