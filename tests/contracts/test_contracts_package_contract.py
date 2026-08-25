"""验证 Contracts Package 的类型契约与轻量边界。"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
from pathlib import Path
import subprocess
import sys

import repoterm.contracts.state as contract_state
import repoterm.contracts.types as contract_types
from repoterm.contracts.state import (
    AppState,
    Store,
    add_cost,
    create_app_store,
    format_app_state_summary,
    get_global_store,
    handle_state_command,
    increment_tool_calls,
    record_api_error,
    set_busy,
    set_global_store,
    set_idle,
    update_message_count,
    update_context_usage,
)
from repoterm.contracts.types import (
    AgentStep,
    ChatMessage,
    ModelAdapter,
    RuntimeEvent,
    RuntimeEventCategory,
    StepDiagnostics,
    ToolCall,
)


ROOT = Path(__file__).resolve().parents[2]


def test_contracts_package_layout_and_lightweight_init() -> None:
    """Contracts Package 只保留具体模块，不批量重导出实现。"""
    init_path = ROOT / "repoterm/contracts/__init__.py"
    assert init_path.exists()
    assert (ROOT / "repoterm/contracts/types.py").exists()
    assert (ROOT / "repoterm/contracts/state.py").exists()
    assert not (ROOT / "repoterm/types.py").exists()
    assert not (ROOT / "repoterm/state.py").exists()

    source = init_path.read_text(encoding="utf-8")
    assert "__all__: tuple[str, ...] = ()" in source
    assert "import *" not in source
    assert "__getattr__" not in source
    assert "importlib" not in source
    assert "sys.modules" not in source


def test_types_public_symbols_and_modules_are_stable() -> None:
    """公共类型从新具体模块导出，且未发生模块漂移。"""
    public_symbols = (
        ChatMessage,
        ToolCall,
        StepDiagnostics,
        AgentStep,
        RuntimeEvent,
        ModelAdapter,
    )
    assert all(symbol.__module__ == "repoterm.contracts.types" for symbol in public_symbols)
    assert RuntimeEventCategory is contract_types.RuntimeEventCategory


def test_agent_step_shape_and_defaults_are_unchanged() -> None:
    """AgentStep 保持 slots dataclass、字段顺序和默认值。"""
    assert dataclasses.is_dataclass(AgentStep)
    assert hasattr(AgentStep, "__slots__")
    assert [field.name for field in dataclasses.fields(AgentStep)] == [
        "type",
        "content",
        "kind",
        "calls",
        "contentKind",
        "diagnostics",
    ]

    step = AgentStep("assistant")
    assert step.content == ""
    assert step.kind is None
    assert step.calls == []
    assert step.contentKind is None
    assert step.diagnostics is None


def test_runtime_event_shape_defaults_and_frozen_slots_are_unchanged() -> None:
    """RuntimeEvent 保持 frozen + slots dataclass 及其字段默认值。"""
    assert dataclasses.is_dataclass(RuntimeEvent)
    assert hasattr(RuntimeEvent, "__slots__")
    assert RuntimeEvent.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(RuntimeEvent)] == [
        "category",
        "message",
        "step",
        "profile",
        "phase",
        "verification_focus",
        "stop_reason",
        "widening_reason",
        "evidence_summary",
    ]

    event = RuntimeEvent("phase", "message")
    assert event.step is None
    assert event.profile == ""
    assert event.phase == ""
    assert event.verification_focus == ""
    assert event.stop_reason == ""
    assert event.widening_reason == ""
    assert event.evidence_summary == ""


def test_model_adapter_next_parameter_semantics_are_unchanged() -> None:
    """ModelAdapter.next 保持原有参数顺序与名称。"""
    assert list(inspect.signature(ModelAdapter.next).parameters) == [
        "self",
        "messages",
        "on_stream_chunk",
        "store",
    ]


def test_contract_types_use_only_standard_library_imports() -> None:
    """Contracts types 作为叶子模块不能反向依赖产品包或 benchmarks。"""
    source = (ROOT / "repoterm/contracts/types.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert all(
        not module.startswith("repoterm") and not module.startswith("benchmarks")
        for module in imported_modules
    )


def test_state_public_symbols_and_module_identity_are_stable() -> None:
    """State 公共入口从新具体模块导出，且没有改变模块归属。"""
    public_symbols = (
        Store,
        AppState,
        create_app_store,
        format_app_state_summary,
        increment_tool_calls,
        update_context_usage,
        add_cost,
        record_api_error,
        set_busy,
        set_idle,
        get_global_store,
        set_global_store,
        handle_state_command,
        update_message_count,
    )
    assert all(symbol.__module__ == "repoterm.contracts.state" for symbol in public_symbols)
    assert isinstance(create_app_store(), Store)
    assert isinstance(create_app_store().get_state(), AppState)


def test_state_store_initial_override_updates_and_listener_lifecycle() -> None:
    """Store 的初始覆盖、更新、Listener 和取消订阅行为保持不变。"""
    store = create_app_store({"session_id": "session-1", "message_count": 2})
    assert store.get_state().session_id == "session-1"
    assert store.get_state().message_count == 2

    notifications: list[str] = []
    unsubscribe = store.subscribe(lambda: notifications.append("called"))
    store.set_state(lambda state: dataclasses.replace(state, status_message="updated"))
    assert store.get_state().status_message == "updated"
    assert notifications == ["called"]

    unsubscribe()
    store.set_state(lambda state: dataclasses.replace(state, status_message="again"))
    assert store.get_state().status_message == "again"
    assert notifications == ["called"]


def test_state_listener_exceptions_do_not_break_updates() -> None:
    """Listener 异常被隔离，状态更新仍然完成。"""
    store = create_app_store()

    def raising_listener() -> None:
        raise RuntimeError("listener failure")

    unsubscribe = store.subscribe(raising_listener)
    store.set_state(lambda state: dataclasses.replace(state, workspace="workspace"))
    assert store.get_state().workspace == "workspace"
    unsubscribe()


def test_state_updaters_preserve_existing_semantics() -> None:
    """状态更新辅助函数保持计数、百分比、成本和忙闲状态语义。"""
    store = create_app_store()
    store.set_state(increment_tool_calls())
    assert store.get_state().tool_call_count == 1
    store.set_state(increment_tool_calls())
    assert store.get_state().tool_call_count == 2

    store.set_state(update_message_count(3))
    assert store.get_state().message_count == 3

    store.set_state(update_context_usage(50, 100))
    assert store.get_state().token_usage == 50
    assert store.get_state().context_window_size == 100
    assert store.get_state().context_usage_percentage == 50.0

    store.set_state(add_cost(1.25))
    assert store.get_state().total_cost_usd == 1.25
    assert store.get_state().api_calls == 1
    store.set_state(record_api_error())
    assert store.get_state().api_errors == 1
    assert store.get_state().api_calls == 2

    store.set_state(set_busy("read_file"))
    assert store.get_state().is_busy is True
    assert store.get_state().active_tool == "read_file"
    store.set_state(set_idle())
    assert store.get_state().is_busy is False
    assert store.get_state().active_tool is None
    assert store.get_state().status_message == "Ready"

    summary = format_app_state_summary(store.get_state())
    assert "Application State" in summary
    assert "Tool calls: 2" in summary
    assert "API errors: 1" in summary


def test_global_store_injection_and_state_command_are_stable() -> None:
    """全局 Store 可注入、读取，且测试结束后恢复原始单例。"""
    original = contract_state._global_store
    try:
        custom = create_app_store({"session_id": "global-session"})
        set_global_store(custom)
        assert get_global_store() is custom
        assert "global-s" in handle_state_command()
    finally:
        contract_state._global_store = original


def test_state_uses_only_standard_library_imports() -> None:
    """Contracts state 作为叶子模块不能反向依赖产品包或 benchmarks。"""
    source = (ROOT / "repoterm/contracts/state.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert all(
        not module.startswith("repoterm") and not module.startswith("benchmarks")
        for module in imported_modules
    )


def _run_import_order(imports: str) -> None:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", imports],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_contract_import_orders_are_stable_and_old_modules_are_absent() -> None:
    """正反向导入顺序都成功，且旧顶层模块不再存在。"""
    forward = """
import importlib.util
import repoterm.contracts.types
import repoterm.contracts.state
import repoterm.providers.anthropic
import repoterm.runtime.loop
import repoterm.app.interactive
assert importlib.util.find_spec('repoterm.types') is None
assert importlib.util.find_spec('repoterm.state') is None
"""
    reverse = """
import importlib.util
import repoterm.app.interactive
import repoterm.runtime.loop
import repoterm.providers.anthropic
import repoterm.contracts.state
import repoterm.contracts.types
assert importlib.util.find_spec('repoterm.types') is None
assert importlib.util.find_spec('repoterm.state') is None
"""
    _run_import_order(forward)
    _run_import_order(reverse)
