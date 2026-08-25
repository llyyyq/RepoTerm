"""Small lifecycle boundary for UI worker threads.

This module does not cancel Runtime or Tool work.  It only prevents new UI
work after shutdown, wakes permission waiters, and performs bounded joins.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
import time
from typing import Callable

from repoterm.ui.tui.ui_events import ApprovalTicketManager, UiEventQueue


logger = logging.getLogger("repoterm.ui.worker_lifecycle")


class UiLifecycleState(str, Enum):
    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class UiWorkerThread(threading.Thread):
    """Worker thread with ordinary ``Thread.join`` semantics."""

    # Intentionally no join override: joining is synchronization only and
    # must never consume UI events or invoke a renderer.
    pass


class WorkerLifecycle:
    """Track UI workers and provide one idempotent close/join operation."""

    def __init__(self, queue: UiEventQueue) -> None:
        self.queue = queue
        self._state = UiLifecycleState.RUNNING
        self._lock = threading.Lock()
        self._workers: list[UiWorkerThread] = []

    @property
    def state(self) -> UiLifecycleState:
        with self._lock:
            return self._state

    @property
    def accepting(self) -> bool:
        return self.state is UiLifecycleState.RUNNING

    def start(
        self,
        target: Callable[[], None],
        *,
        name: str,
    ) -> UiWorkerThread:
        """Start a worker only while the UI is still accepting work."""
        with self._lock:
            if self._state is not UiLifecycleState.RUNNING:
                raise RuntimeError("UI lifecycle is closing")
            worker = UiWorkerThread(target=target, name=name, daemon=True)
            self._workers.append(worker)
            worker.start()
            return worker

    def close(self, approvals: ApprovalTicketManager | None = None) -> None:
        """Enter CLOSING once, cancel approvals, and close the event channel."""
        with self._lock:
            if self._state is UiLifecycleState.CLOSED:
                return
            self._state = UiLifecycleState.CLOSING
        if approvals is not None:
            approvals.close()
        self.queue.close()

    def join_all(self, timeout: float = 0.5) -> bool:
        """Join workers for at most ``timeout`` seconds in total."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            workers = list(self._workers)
        all_finished = True
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(remaining)
            if worker.is_alive():
                all_finished = False
                logger.warning("UI worker did not finish before bounded shutdown: %s", worker.name)
        with self._lock:
            self._state = UiLifecycleState.CLOSED
        return all_finished

    def close_and_join(self, approvals: ApprovalTicketManager | None = None, timeout: float = 0.5) -> bool:
        """Idempotently close the queue and perform bounded worker joins."""
        self.close(approvals)
        return self.join_all(timeout)
