"""Thread-safe UI event transport used by the TTY main loop.

Worker threads are deliberately kept ignorant of ``ScreenState``.  They may
only publish immutable event payloads here; the TTY thread drains the queue
and applies the events to the live UI state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import threading
from types import MappingProxyType
from typing import Any, Mapping


class UiEventType(str, Enum):
    """The UI-side events emitted by agent and tool workers."""

    ASSISTANT_MESSAGE = "assistant_message"
    ASSISTANT_STREAM_CHUNK = "assistant_stream_chunk"
    THINKING_CHUNK = "thinking_chunk"
    PROGRESS_MESSAGE = "progress_message"
    RUNTIME_EVENT = "runtime_event"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    SHORTCUT_COMPLETED = "shortcut_completed"
    SHORTCUT_FAILED = "shortcut_failed"
    TOOL_COLLAPSE = "tool_collapse"
    PERMISSION_REQUESTED = "permission_requested"


# A shorter alias is useful for callers that treat event names as a protocol.
UiEventKind = UiEventType

# This is a character budget for one queued stream event, not a queue
# capacity.  Control events must remain lossless even when a model streams
# faster than the UI can drain.
STREAM_COALESCE_CHAR_LIMIT = 8192


def _freeze(value: Any) -> Any:
    """Recursively convert common mutable payload containers to immutable ones."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def thaw_payload(value: Any) -> Any:
    """Make a private mutable copy for the main-thread event reducer."""
    if isinstance(value, Mapping):
        return {key: thaw_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_payload(item) for item in value]
    if isinstance(value, frozenset):
        return {thaw_payload(item) for item in value}
    return value


@dataclass(frozen=True, slots=True)
class UiEvent:
    """An immutable event published by a worker and consumed by the TTY thread."""

    event_type: str
    source_id: str
    payload: Mapping[str, Any]
    sequence: int = 0

    def __post_init__(self) -> None:
        event_type = self.event_type.value if isinstance(self.event_type, Enum) else str(self.event_type)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "source_id", str(self.source_id))
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))

    @property
    def kind(self) -> str:
        """Compatibility spelling for code that calls the event type ``kind``."""
        return self.event_type

    @property
    def type(self) -> str:
        """Compatibility spelling matching common event protocol objects."""
        return self.event_type


class UiEventQueue:
    """Thread-safe FIFO with publish-side stream coalescing and bounded drain.

    The queue itself is unbounded so a worker never loses a control event.
    Consecutive same-source stream chunks are merged at the tail up to a
    character budget, while consumers still use a per-drain limit.
    """

    _COALESCIBLE = {
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.THINKING_CHUNK.value,
    }

    def __init__(self) -> None:
        self._events: deque[UiEvent] = deque()
        self._lock = threading.Lock()
        self._next_sequence = 1
        self._closed = False

    @classmethod
    def _can_coalesce(cls, previous: UiEvent, current: UiEvent) -> bool:
        """Return whether two adjacent queued events may be merged."""

        if previous.event_type != current.event_type:
            return False
        if previous.source_id != current.source_id:
            return False
        if current.event_type not in cls._COALESCIBLE:
            return False
        return (
            isinstance(previous.payload.get("content"), str)
            and isinstance(current.payload.get("content"), str)
            and len(previous.payload["content"])
            + len(current.payload["content"])
            <= STREAM_COALESCE_CHAR_LIMIT
        )

    def publish(
        self,
        event_or_type: UiEvent | str | UiEventType,
        source_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        sequence: int | None = None,
    ) -> bool:
        """Publish one event, returning ``False`` after the queue is closed."""
        with self._lock:
            if self._closed:
                return False
            if isinstance(event_or_type, UiEvent):
                event = event_or_type
                assigned_sequence = max(self._next_sequence, event.sequence or 0)
                event = UiEvent(
                    event.event_type,
                    event.source_id,
                    event.payload,
                    assigned_sequence,
                )
            else:
                if source_id is None:
                    raise ValueError("source_id is required for a new UI event")
                assigned_sequence = max(self._next_sequence, sequence or 0)
                event = UiEvent(
                    event_or_type,
                    source_id,
                    payload or {},
                    assigned_sequence,
                )
            self._next_sequence = max(self._next_sequence, event.sequence + 1)

            if self._events and self._can_coalesce(self._events[-1], event):
                previous = self._events[-1]
                payload = thaw_payload(previous.payload)
                payload["content"] = str(payload["content"]) + str(event.payload["content"])
                self._events[-1] = UiEvent(
                    previous.event_type,
                    previous.source_id,
                    payload,
                    previous.sequence,
                )
                return True
            self._events.append(event)
            return True

    publish_event = publish

    def drain(self, max_events: int = 100) -> list[UiEvent]:
        """Remove at most ``max_events`` already-coalesced events in FIFO order."""
        if max_events <= 0:
            return []
        with self._lock:
            events: list[UiEvent] = []
            while self._events and len(events) < max_events:
                events.append(self._events.popleft())
            return events

    def close(self) -> None:
        """Reject future events; already queued events remain inspectable."""
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._events)


class ApprovalTicket:
    """One permission request with an independent completion signal."""

    def __init__(self, request_id: str, request: Mapping[str, Any]) -> None:
        self.request_id = request_id
        self.request = thaw_payload(_freeze(dict(request)))
        self.completion = threading.Event()
        self._lock = threading.Lock()
        self._result: dict[str, Any] | None = None
        self._resolved = False
        self._closed = False

    def resolve(self, result: Mapping[str, Any]) -> bool:
        """Resolve at most once and wake the worker waiting on this ticket."""
        with self._lock:
            if self._resolved:
                return False
            self._result = dict(result)
            self._resolved = True
            self.completion.set()
            return True

    def close(self, result: Mapping[str, Any] | None = None) -> bool:
        """Complete an outstanding request as cancelled/denied."""
        with self._lock:
            self._closed = True
        return self.resolve(result or {"decision": "deny_once", "reason": "ui_closed"})

    def wait(self, poll_seconds: float = 0.1) -> dict[str, Any]:
        """Wait without an unbounded single ``Event.wait`` call."""
        while not self.completion.wait(poll_seconds):
            with self._lock:
                if self._resolved:
                    break
        with self._lock:
            return dict(self._result or {"decision": "deny_once", "reason": "ui_closed"})

    @property
    def resolved(self) -> bool:
        with self._lock:
            return self._resolved

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def result(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._result) if self._result is not None else None


class ApprovalTicketManager:
    """Own request-to-response routing for all active permission tickets."""

    def __init__(self, queue: UiEventQueue) -> None:
        self.queue = queue
        self._lock = threading.Lock()
        self._counter = 0
        self._tickets: dict[str, ApprovalTicket] = {}
        self._closed = False

    def create(self, request: Mapping[str, Any], source_id: str) -> ApprovalTicket:
        """Create a ticket and publish exactly one permission event."""
        with self._lock:
            self._counter += 1
            request_id = f"approval-{self._counter}"
            ticket = ApprovalTicket(request_id, request)
            self._tickets[request_id] = ticket
            closed = self._closed
        if closed or not self.queue.publish(
            UiEventType.PERMISSION_REQUESTED,
            source_id,
            {"request_id": request_id, "request": request},
        ):
            ticket.close()
        return ticket

    def get(self, request_id: str) -> ApprovalTicket | None:
        with self._lock:
            return self._tickets.get(request_id)

    def resolve(self, request_id: str, result: Mapping[str, Any]) -> bool:
        """Resolve only the ticket named by the current UI request."""
        ticket = self.get(request_id)
        return ticket.resolve(result) if ticket is not None else False

    def close(self) -> None:
        """Cancel all outstanding tickets and reject future requests."""
        with self._lock:
            self._closed = True
            tickets = list(self._tickets.values())
        for ticket in tickets:
            if not ticket.resolved:
                ticket.close()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def pending(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(ticket_id for ticket_id, ticket in self._tickets.items() if not ticket.resolved)
