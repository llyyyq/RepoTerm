from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable

from repoterm.observability.cost import CostTracker
from repoterm.safety.permissions import PermissionManager
from repoterm.session import AutosaveManager, SessionData
from repoterm.contracts.state import AppState, Store
from repoterm.tools.registry import ToolRegistry
from repoterm.ui.tui.types import TranscriptEntry
from repoterm.contracts.types import ChatMessage, ModelAdapter
from repoterm.ui.tui.ui_events import UiEventQueue


@dataclass
class TtyAppArgs:
    runtime: dict | None
    tools: ToolRegistry
    model: ModelAdapter
    messages: list[ChatMessage]
    cwd: str
    permissions: PermissionManager
    memory_manager: Any | None = None
    context_manager: Any | None = None
    prompt_bundle: Any | None = None
    product_snapshot: dict[str, Any] | None = None


@dataclass
class PendingApproval:
    request: dict[str, Any]
    resolve: Callable[[dict[str, Any]], None]
    details_expanded: bool = False
    details_scroll_offset: int = 0
    selected_choice_index: int = 0
    feedback_mode: bool = False
    feedback_input: str = ""


@dataclass
class AggregatedEditProgress:
    entry_id: int
    tool_name: str
    path: str
    total: int = 1
    completed: int = 0
    errors: int = 0
    last_output: str = ""


@dataclass
class ScreenState:
    input: str = ""
    cursor_offset: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    transcript_scroll_offset: int = 0
    transcript_revision: int = 0
    selected_slash_index: int = 0
    status: str | None = None
    active_tool: str | None = None
    recent_tools: list[dict[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    history_index: int = 0
    history_draft: str = ""
    next_entry_id: int = 1
    pending_approval: PendingApproval | None = None
    is_busy: bool = False
    session: SessionData | None = None
    autosave: AutosaveManager | None = None
    app_state: Store[AppState] | None = None
    cost_tracker: CostTracker | None = None
    agent_thread: Any = None
    agent_result: dict | None = None
    agent_lock: Any = None
    tool_start_time: float | None = None
    # 最近一次渲染计算出的布局结果，供导航和渲染共用同一份高度预算。
    layout_metrics: Any | None = None
    # 创建该 ScreenState 的线程是唯一可以消费 UI 事件并修改界面的 owner。
    ui_owner_thread_id: int = field(default_factory=threading.get_ident)
    ui_event_queue: UiEventQueue = field(default_factory=UiEventQueue, repr=False)
    lifecycle: Any = field(default=None, repr=False)
    approval_manager: Any = field(default=None, repr=False)
    pending_approval_queue: list[PendingApproval] = field(default_factory=list, repr=False)
    active_turn_id: str | None = field(default=None, repr=False)
    ui_event_handler: Any = field(default=None, repr=False)
    shortcut_thread: Any = field(default=None, repr=False)
