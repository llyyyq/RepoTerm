from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from repoterm.observability.cost import CostTracker
from repoterm.ui.history import load_history_entries
from repoterm.safety.permissions import PermissionManager
from repoterm.session import (
    AutosaveManager,
    SessionData,
    create_new_session,
    format_session_list,
    format_session_resume,
    get_latest_session,
    list_sessions,
    load_session,
    save_session,
)
from repoterm.contracts.state import create_app_store
from repoterm.ui.tui.state import ScreenState, TtyAppArgs
from repoterm.ui.tui.tool_lifecycle import _bump_transcript_revision
from repoterm.ui.tui.types import TranscriptEntry
from repoterm.ui.tui.ui_events import ApprovalTicketManager, UiEventQueue
from repoterm.ui.tui.worker_lifecycle import WorkerLifecycle


def handle_session_listing(
    cwd: str,
    list_sessions_only: bool,
    *,
    workspace_only: bool = False,
) -> bool:
    if not list_sessions_only:
        return False
    workspace = str(Path(cwd).resolve()) if workspace_only else None
    sessions = list_sessions(workspace=workspace)
    print(format_session_list(sessions))
    return True


def load_or_create_session(cwd: str, resume_session: str | None) -> SessionData:
    workspace = str(Path(cwd).resolve())
    if resume_session:
        if resume_session == "latest":
            session = get_latest_session(workspace=workspace)
            if session:
                print(format_session_resume(session))
                return session
            print("No previous session found for this workspace.")
            return create_new_session(workspace=workspace)

        session = load_session(resume_session)
        if not session:
            raise FileNotFoundError(f"Session '{resume_session}' not found.")
        print(format_session_resume(session))
        return session

    session = get_latest_session(workspace=workspace)
    if session:
        print(f"Previous session found: {session.session_id[:8]}")
        print("Use --resume to continue, or starting fresh session.")
        return create_new_session(workspace=workspace)

    return create_new_session(workspace=workspace)


def build_tty_runtime_state(
    runtime: dict | None,
    tools: Any,
    model: Any,
    messages: list[Any],
    cwd: str,
    permissions: PermissionManager,
    session: SessionData,
    memory_manager: Any | None = None,
    context_manager: Any | None = None,
    prompt_bundle: Any | None = None,
    product_snapshot: dict[str, Any] | None = None,
) -> tuple[TtyAppArgs, ScreenState]:
    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=model,
        messages=messages,
        cwd=cwd,
        permissions=permissions,
        memory_manager=memory_manager,
        context_manager=context_manager,
        prompt_bundle=prompt_bundle,
        product_snapshot=product_snapshot,
    )

    state = ScreenState(
        history=load_history_entries(),
        session=session,
        autosave=AutosaveManager(session),
        app_state=create_app_store({
            "session_id": session.session_id,
            "workspace": cwd,
            "model": runtime.get("model", "unknown") if runtime else "unknown",
        }),
        cost_tracker=CostTracker(),
    )
    state.ui_event_queue = UiEventQueue()
    state.lifecycle = WorkerLifecycle(state.ui_event_queue)
    state.history_index = len(state.history)

    if session.messages:
        args.messages.clear()
        args.messages.extend(session.messages)
        for entry_data in session.transcript_entries:
            state.transcript.append(TranscriptEntry(**entry_data))
        _bump_transcript_revision(state)
        print(f"Restored {len(session.messages)} messages, {len(state.transcript)} transcript entries.")

    return args, state


def install_permission_prompt(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Any,
) -> tuple[ApprovalTicketManager, dict[str, Any], Any]:
    """Install request-scoped approval routing without touching UI state in a worker.

    The returned manager is kept as the first tuple item for compatibility with
    the TTY loop's existing plumbing; unlike the former shared Event/result
    pair, every call creates its own ``ApprovalTicket``.
    """
    queue = getattr(state, "ui_event_queue", None)
    if queue is None:
        queue = UiEventQueue()
        state.ui_event_queue = queue
    approval_manager = ApprovalTicketManager(queue)
    state.approval_manager = approval_manager
    approval_result: dict[str, Any] = {}

    def _permission_prompt_handler(request: dict[str, Any]) -> dict[str, Any]:
        # This callback runs in the Agent/Tool worker.  It only creates a
        # ticket, publishes an event, and waits for the matching response.
        ticket = approval_manager.create(
            request,
            source_id="permission",
        )
        return ticket.wait()

    args.permissions.prompt = _permission_prompt_handler
    return approval_manager, approval_result, _permission_prompt_handler


def refresh_tty_session_snapshot(args: TtyAppArgs, state: ScreenState) -> None:
    """Sync in-memory session data from the live TTY state without saving."""
    if not state.session:
        return

    state.session.messages = list(args.messages)
    state.session.transcript_entries = [
        {
            "id": e.id,
            "kind": e.kind,
            "category": e.category,
            "runtimeKind": e.runtimeKind,
            "runtimeStep": e.runtimeStep,
            "runtimePhase": e.runtimePhase,
            "runtimeStopReason": e.runtimeStopReason,
            "runtimeVerificationFocus": e.runtimeVerificationFocus,
            "toolName": e.toolName,
            "status": e.status,
            "body": e.body,
            "collapsed": e.collapsed,
            "collapsedSummary": e.collapsedSummary,
            "collapsePhase": e.collapsePhase,
        }
        for e in state.transcript
    ]
    state.session.history = state.history
    state.session.permissions_summary = args.permissions.get_summary()
    state.session.skills = args.tools.get_skills()
    state.session.mcp_servers = args.tools.get_mcp_servers()
    product_snapshot = getattr(args, "product_snapshot", None)
    if product_snapshot:
        state.session.instruction_layers = list(
            product_snapshot.get("instruction_layers", [])
        )
        state.session.hook_status = dict(product_snapshot.get("hook_status", {}))
        state.session.delegated_tasks = list(
            product_snapshot.get("delegated_tasks", [])
        )
        state.session.delegation_status = dict(
            product_snapshot.get("delegation_status", {})
        )
        state.session.extension_manifests = list(
            product_snapshot.get("extension_manifests", [])
        )
        state.session.readiness_report = dict(
            product_snapshot.get("readiness_report", {})
        )
    if hasattr(state.session, "update_metadata"):
        state.session.update_metadata()


def finalize_tty_session(args: TtyAppArgs, state: ScreenState) -> None:
    if not state.session:
        return

    refresh_tty_session_snapshot(args, state)

    if state.autosave:
        state.autosave.force_save()
    else:
        save_session(state.session)

    print(f"\nSession saved: {state.session.session_id[:8]}")
