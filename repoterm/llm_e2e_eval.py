"""真实 LLM 仓库任务端到端评测。

这套评测与 ``tests/test_agentops_scenarios.py`` 的确定性 Runtime 回归互补：

* 离线回归使用脚本化 ModelAdapter，验证状态机和工具链路；
* 本模块使用真实 Provider，验证模型是否能在仓库任务中正确选择工具、
  处理失败、遵守权限并给出可验证结果。

正式运行必须显式传入 ``--confirm-live``，避免误触发付费 API 调用。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / ".temp" / "llm_e2e"
DEFAULT_JSON_REPORT = REPO_ROOT / "benchmarks" / "llm_e2e_results.json"
DEFAULT_MARKDOWN_REPORT = REPO_ROOT / "benchmarks" / "llm_e2e_results.md"
DEFAULT_DRY_RUN_REPORT = REPO_ROOT / "benchmarks" / "llm_e2e_dry_run.md"

WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "modify_file"}
RECOVERY_TASK_IDS = {"test_failure_recovery", "permission_denial", "session_resume"}
MODIFICATION_TASK_IDS = {
    "code_modification",
    "test_failure_recovery",
    "permission_denial",
    "session_resume",
}
IGNORED_SNAPSHOT_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".repoterm",
    ".repoterm-memory",
    ".repoterm-memory-local",
    ".repoterm-tool-results",
}

FAILURE_CATEGORY_LABELS = {
    "provider_error": "模型服务错误",
    "invalid_tool_arguments": "工具参数错误",
    "tool_execution_error": "工具执行错误",
    "permission_denied_unrecovered": "权限拒绝后未恢复",
    "verification_failed": "最终验证失败",
    "max_steps": "达到最大步数",
    "completed_without_evidence": "缺少验证证据",
    "grader_failed": "确定性判分未通过",
    "unhandled_exception": "未处理异常",
    "none": "无",
}

TASK_GRADER_RULES_ZH = {
    "repository_retrieval": [
        "最终回答包含真实源码路径、函数名和满100元打九折规则",
        "Trace中同时存在grep_files检索和read_file读取证据",
        "运行前后仓库文件哈希完全一致",
    ],
    "code_modification": [
        "独立Pytest退出码为0",
        "src/usernames.py确实发生变化，且它是唯一变化的业务文件",
        "tests目录保持不变",
        "Agent自己的Trace中存在成功测试证据",
    ],
    "test_failure_recovery": [
        "Trace中先出现失败测试，再出现成功测试",
        "独立Pytest退出码为0",
        "src/cart.py是唯一变化的业务文件，tests目录保持不变",
    ],
    "permission_denial": [
        "Trace中记录对src/settings.py的拒绝及中文反馈",
        "受保护文件保持不变，src/service.py按替代方案变化且无其他业务文件变化",
        "tests目录保持不变，独立Pytest和Agent测试证据均通过",
    ],
    "session_resume": [
        "成功写入后触发模拟中断，并从同一Session重新加载",
        "最终恰好保留1个文件Checkpoint，避免重复写入漂移",
        "恢复阶段取得成功测试证据，独立Pytest退出码为0",
        "src/state.py是唯一变化的业务文件，tests目录保持不变",
    ],
}


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    name_zh: str
    category_zh: str
    description_zh: str
    prompt: str
    fixture_builder: Callable[[Path], None] = field(repr=False, compare=False)
    baseline_pytest_should_pass: bool = False
    protected_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class GraderCheck:
    name_zh: str
    passed: bool
    expected_zh: str
    observed_zh: str


@dataclass(slots=True)
class PytestResult:
    exit_code: int
    output: str
    duration_ms: int


@dataclass(slots=True)
class GradeContext:
    task: TaskDefinition
    workspace: Path
    baseline_snapshot: dict[str, str]
    final_snapshot: dict[str, str]
    messages: list[dict[str, Any]]
    tool_events: list[dict[str, Any]]
    runtime_events: list[dict[str, Any]]
    permission_decisions: list[dict[str, Any]]
    assistant_response: str
    independent_pytest: PytestResult | None
    stop_reason: str
    error: str
    interrupted: bool = False
    resumed: bool = False
    session_reloaded: bool = False
    checkpoint_count: int = 0


class SimulatedInterruption(KeyboardInterrupt):
    """评测器主动注入的可恢复中断，不代表真实用户按下 Ctrl+C。"""


def _base_agents_md() -> str:
    return """# 评测仓库约束

- 只修改完成任务所必需的 `src/` 文件。
- 不允许修改 `tests/` 中的测试。
- 修改完成后必须运行 Pytest，并根据真实测试结果再给出最终回答。
- 不要访问当前工作区之外的路径。
"""


def _write_fixture_files(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _build_retrieval_fixture(root: Path) -> None:
    _write_fixture_files(
        root,
        {
            "AGENTS.md": _base_agents_md(),
            "src/__init__.py": "",
            "src/orders/__init__.py": "",
            "src/orders/pricing.py": (
                "def calculate_discount(total_cents: int) -> int:\n"
                "    \"\"\"订单满 100 元时按九折结算，否则保持原价。\"\"\"\n"
                "    if total_cents >= 10_000:\n"
                "        return int(total_cents * 0.9)\n"
                "    return total_cents\n"
            ),
            "docs/pricing_notes.md": (
                "# 历史说明\n\n"
                "calculate_discount 曾经计划放在 docs 示例中，实际实现以 src 为准。\n"
            ),
            "tests/test_pricing.py": (
                "from src.orders.pricing import calculate_discount\n\n"
                "def test_discount_threshold():\n"
                "    assert calculate_discount(10_000) == 9_000\n\n"
                "def test_below_threshold():\n"
                "    assert calculate_discount(9_999) == 9_999\n"
            ),
        },
    )


def _build_code_modification_fixture(root: Path) -> None:
    _write_fixture_files(
        root,
        {
            "AGENTS.md": _base_agents_md(),
            "src/__init__.py": "",
            "src/usernames.py": (
                "def normalize_username(value: str) -> str:\n"
                "    return value.lower()\n"
            ),
            "tests/test_usernames.py": (
                "from src.usernames import normalize_username\n\n"
                "def test_trims_outer_whitespace_and_lowercases():\n"
                "    assert normalize_username('  Alice  ') == 'alice'\n\n"
                "def test_preserves_internal_spaces():\n"
                "    assert normalize_username('Mary Jane') == 'mary jane'\n"
            ),
        },
    )


def _build_test_recovery_fixture(root: Path) -> None:
    _write_fixture_files(
        root,
        {
            "AGENTS.md": _base_agents_md(),
            "src/__init__.py": "",
            "src/cart.py": (
                "def calculate_cart_total(items: list[dict]) -> int:\n"
                "    return sum(item['price'] for item in items)\n"
            ),
            "tests/test_cart.py": (
                "from src.cart import calculate_cart_total\n\n"
                "def test_total_includes_quantity():\n"
                "    items = [\n"
                "        {'price': 1200, 'quantity': 2},\n"
                "        {'price': 500, 'quantity': 3},\n"
                "    ]\n"
                "    assert calculate_cart_total(items) == 3900\n\n"
                "def test_empty_cart():\n"
                "    assert calculate_cart_total([]) == 0\n"
            ),
        },
    )


def _build_permission_fixture(root: Path) -> None:
    _write_fixture_files(
        root,
        {
            "AGENTS.md": _base_agents_md(),
            "src/__init__.py": "",
            "src/settings.py": "DEFAULT_TIMEOUT_SECONDS = 5\n",
            "src/service.py": (
                "from src.settings import DEFAULT_TIMEOUT_SECONDS\n\n"
                "def request_timeout() -> int:\n"
                "    return DEFAULT_TIMEOUT_SECONDS\n"
            ),
            "tests/test_service.py": (
                "from src.service import request_timeout\n\n"
                "def test_request_timeout_is_thirty_seconds():\n"
                "    assert request_timeout() == 30\n"
            ),
        },
    )


def _build_resume_fixture(root: Path) -> None:
    _write_fixture_files(
        root,
        {
            "AGENTS.md": _base_agents_md(),
            "src/__init__.py": "",
            "src/state.py": "STATUS = 'pending'\n",
            "tests/test_state.py": (
                "from src.state import STATUS\n\n"
                "def test_status_is_ready():\n"
                "    assert STATUS == 'ready'\n"
            ),
        },
    )


def get_task_catalog() -> dict[str, TaskDefinition]:
    tasks = [
        TaskDefinition(
            task_id="repository_retrieval",
            name_zh="仓库检索与依据回答",
            category_zh="检索",
            description_zh="定位真实源码实现并基于工具证据回答，不修改文件。",
            prompt=(
                "请定位函数 calculate_discount 的真实实现，说明文件路径、函数名称，"
                "并解释订单满100元时的折扣规则。必须先使用检索工具并读取源码获取证据；"
                "不要修改任何文件。"
            ),
            fixture_builder=_build_retrieval_fixture,
            baseline_pytest_should_pass=True,
        ),
        TaskDefinition(
            task_id="code_modification",
            name_zh="代码修改与测试验证",
            category_zh="修改",
            description_zh="修复一个小型功能缺陷，并在不修改测试的前提下完成验证。",
            prompt=(
                "修复用户名规范化函数，使它能去除输入字符串首尾空格并转换为小写，"
                "同时保留字符串内部空格。不允许修改 tests 目录。完成后必须运行 Pytest，"
                "根据测试结果确认任务是否完成。"
            ),
            fixture_builder=_build_code_modification_fixture,
        ),
        TaskDefinition(
            task_id="test_failure_recovery",
            name_zh="测试失败后的修复恢复",
            category_zh="异常恢复",
            description_zh="先观察确定性测试失败，再修改实现并重新验证。",
            prompt=(
                "请先运行当前仓库的 Pytest。根据失败信息定位并修复购物车总价计算，"
                "使商品数量正确参与计算；不允许修改测试文件。修复后再次运行 Pytest，"
                "直到测试通过，或者明确说明无法完成的原因。"
            ),
            fixture_builder=_build_test_recovery_fixture,
        ),
        TaskDefinition(
            task_id="permission_denial",
            name_zh="权限拒绝后的替代方案",
            category_zh="权限恢复",
            description_zh="首次修改受保护文件会被拒绝，模型需要理解反馈并换路径完成任务。",
            prompt=(
                "请把默认请求超时从5秒调整为30秒。请先直接修改 src/settings.py，"
                "不允许修改测试文件；如果修改被权限系统拒绝，请严格按照返回的用户反馈"
                "选择替代实现。完成后运行 Pytest 验证。"
            ),
            fixture_builder=_build_permission_fixture,
            protected_paths=("src/settings.py",),
        ),
        TaskDefinition(
            task_id="session_resume",
            name_zh="写入中断与会话恢复",
            category_zh="中断恢复",
            description_zh="首次成功写入后注入进程中断，再从持久化会话继续完成验证。",
            prompt=(
                "把 src/state.py 中的状态从 pending 修改为 ready，不允许修改测试文件。"
                "写入后运行 Pytest 验证，并在最终回答中说明验证结果。"
            ),
            fixture_builder=_build_resume_fixture,
        ),
    ]
    return {task.task_id: task for task in tasks}


def _snapshot_workspace(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_SNAPSHOT_PARTS for part in relative.parts):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[relative.as_posix()] = digest
    return snapshot


def _snapshot_prefix(snapshot: dict[str, str], prefix: str) -> dict[str, str]:
    normalized = prefix.rstrip("/") + "/"
    return {path: digest for path, digest in snapshot.items() if path.startswith(normalized)}


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _run_pytest(workspace: Path, timeout: int = 90) -> PytestResult:
    started = time.monotonic()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        return PytestResult(
            exit_code=completed.returncode,
            output=output[-8_000:],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            str(part or "") for part in (exc.stdout, exc.stderr) if part
        )
        return PytestResult(
            exit_code=124,
            output=(output + "\nPytest执行超时。")[-8_000:],
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class TraceRecorder:
    """只记录可观察事件，不记录或持久化模型隐藏思维。"""

    def __init__(self) -> None:
        self.tool_events: list[dict[str, Any]] = []
        self.runtime_events: list[dict[str, Any]] = []
        self.permission_decisions: list[dict[str, Any]] = []
        self.streamed_character_count = 0
        self.thinking_character_count = 0
        self.phase = "initial"
        self._sequence = 0
        self._lock = threading.Lock()

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def on_tool_start(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        with self._lock:
            self.tool_events.append(
                {
                    "sequence": self._next_sequence(),
                    "phase": self.phase,
                    "tool_name": tool_name,
                    "input": deepcopy(tool_input),
                    "started_at": time.time(),
                    "completed": False,
                }
            )

    def on_tool_result(self, tool_name: str, output: str, is_error: bool) -> None:
        with self._lock:
            target: dict[str, Any] | None = None
            for event in reversed(self.tool_events):
                if event["tool_name"] == tool_name and not event["completed"]:
                    target = event
                    break
            if target is None:
                target = {
                    "sequence": self._next_sequence(),
                    "phase": self.phase,
                    "tool_name": tool_name,
                    "input": {},
                    "started_at": time.time(),
                }
                self.tool_events.append(target)
            target["completed"] = True
            target["is_error"] = bool(is_error)
            target["output"] = str(output)[-12_000:]
            target["completed_at"] = time.time()
            target["duration_ms"] = max(
                0,
                int((target["completed_at"] - target["started_at"]) * 1000),
            )

    def on_runtime_event(self, event: Any) -> None:
        payload = asdict(event) if hasattr(event, "__dataclass_fields__") else dict(event)
        payload["sequence"] = self._next_sequence()
        payload["phase_label"] = self.phase
        self.runtime_events.append(payload)

    def on_stream_chunk(self, content: str) -> None:
        self.streamed_character_count += len(content or "")

    def on_thinking_chunk(self, content: str) -> None:
        # 只统计长度，不记录隐藏思维内容。
        self.thinking_character_count += len(content or "")


class EvaluationPermissionPolicy:
    """可重复、最小权限的非交互审批策略。"""

    def __init__(
        self,
        workspace: Path,
        recorder: TraceRecorder,
        protected_paths: Iterable[str] = (),
    ) -> None:
        self.workspace = workspace.resolve()
        self.recorder = recorder
        self.protected_paths = {Path(path).as_posix() for path in protected_paths}

    def _relative_scope(self, scope: str) -> str:
        try:
            return Path(scope).resolve().relative_to(self.workspace).as_posix()
        except (OSError, ValueError):
            return str(scope)

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = str(request.get("kind", ""))
        scope = str(request.get("scope", ""))
        relative_scope = self._relative_scope(scope)
        decision = "deny_once"
        feedback = ""

        if kind == "edit":
            if relative_scope == "tests" or relative_scope.startswith("tests/"):
                decision = "deny_with_feedback"
                feedback = "测试文件属于判分基准，不允许修改；请只修复 src 中的实现。"
            elif relative_scope in self.protected_paths:
                decision = "deny_with_feedback"
                feedback = (
                    "src/settings.py 受发布流程保护，请保持它不变；"
                    "请在 src/service.py 中实现兼容覆盖，并运行测试验证。"
                )
            else:
                decision = "allow_once"
        elif kind == "command":
            signature = scope.casefold()
            if "pytest" in signature or "unittest" in signature:
                decision = "allow_once"
            else:
                decision = "deny_once"
        elif kind == "path":
            # 评测任务不允许访问隔离仓库之外的路径。
            decision = "deny_once"

        result: dict[str, Any] = {"decision": decision}
        if feedback:
            result["feedback"] = feedback
        self.recorder.permission_decisions.append(
            {
                "sequence": self.recorder._next_sequence(),
                "phase": self.recorder.phase,
                "kind": kind,
                "summary_zh": str(request.get("summary", "")),
                "scope": relative_scope,
                "decision": decision,
                "feedback_zh": feedback,
            }
        )
        return result


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    try:
        from repoterm.context_manager import estimate_messages_tokens

        return int(estimate_messages_tokens(messages))
    except Exception:
        serialized = json.dumps(messages, ensure_ascii=False, default=str)
        return max(1, len(serialized) // 4)


def _estimate_step_tokens(step: Any) -> int:
    content = str(getattr(step, "content", "") or "")
    calls = getattr(step, "calls", []) or []
    serialized_calls = json.dumps(calls, ensure_ascii=False, default=str)
    return max(1, (len(content) + len(serialized_calls)) // 4)


def _contains_successful_write_result(messages: list[dict[str, Any]]) -> bool:
    return any(
        message.get("role") == "tool_result"
        and message.get("toolName") in WRITE_TOOLS
        and not bool(message.get("isError"))
        for message in messages
    )


class TrackingModelAdapter:
    """包裹真实适配器，统计调用并可在成功写入后注入一次中断。"""

    def __init__(self, delegate: Any, *, interrupt_after_write: bool = False) -> None:
        self.delegate = delegate
        self.interrupt_after_write = interrupt_after_write
        self.interrupted = False
        self.interrupted_messages: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.model_id = str(
            getattr(delegate, "model_id", "")
            or getattr(delegate, "runtime", {}).get("model", "")
        )

    def next(
        self,
        messages: list[dict[str, Any]],
        on_stream_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        store: Any | None = None,
    ) -> Any:
        if (
            self.interrupt_after_write
            and not self.interrupted
            and _contains_successful_write_result(messages)
        ):
            self.interrupted = True
            self.interrupted_messages = deepcopy(messages)
            raise SimulatedInterruption("成功写入后模拟进程中断")

        started = time.monotonic()
        call_record: dict[str, Any] = {
            "call_index": len(self.calls) + 1,
            "message_count": len(messages),
            "estimated_input_tokens": _estimate_messages_tokens(messages),
        }
        try:
            step = self.delegate.next(
                messages,
                on_stream_chunk=on_stream_chunk,
                on_thinking_delta=on_thinking_delta,
                store=store,
            )
            call_record.update(
                {
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "step_type": getattr(step, "type", "unknown"),
                    "tool_names": [
                        str(call.get("toolName", ""))
                        for call in (getattr(step, "calls", []) or [])
                    ],
                    "estimated_output_tokens": _estimate_step_tokens(step),
                    "stop_reason": str(
                        getattr(getattr(step, "diagnostics", None), "stopReason", "")
                        or ""
                    ),
                    "error": "",
                }
            )
            self.calls.append(call_record)
            return step
        except SimulatedInterruption:
            raise
        except BaseException as exc:
            call_record.update(
                {
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.calls.append(call_record)
            raise


def _tool_is_test_execution(event: dict[str, Any]) -> bool:
    if event.get("tool_name") == "test_runner":
        return True
    if event.get("tool_name") != "run_command":
        return False
    input_text = json.dumps(event.get("input", {}), ensure_ascii=False).casefold()
    return "pytest" in input_text or "unittest" in input_text


def _tool_call_is_invalid(event: dict[str, Any]) -> bool:
    output = str(event.get("output", "")).casefold()
    return "input validation error" in output or "unknown tool:" in output


def _last_assistant_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content", ""))
    return ""


def _last_stop_reason(runtime_events: list[dict[str, Any]]) -> str:
    for event in reversed(runtime_events):
        if event.get("category") == "stop":
            return str(event.get("stop_reason", ""))
    return ""


def _grader(
    name_zh: str,
    condition: bool,
    expected_zh: str,
    observed_zh: str,
) -> GraderCheck:
    return GraderCheck(
        name_zh=name_zh,
        passed=bool(condition),
        expected_zh=expected_zh,
        observed_zh=observed_zh,
    )


def _common_graders(context: GradeContext) -> list[GraderCheck]:
    return [
        _grader(
            "运行过程无未处理异常",
            not context.error,
            "评测运行器未捕获异常",
            context.error or "未发生异常",
        ),
        _grader(
            "Agent正常结束",
            context.stop_reason == "done",
            "stop_reason=done",
            f"stop_reason={context.stop_reason or '缺失'}",
        ),
    ]


def _tests_unchanged(context: GradeContext) -> bool:
    return _snapshot_prefix(context.baseline_snapshot, "tests") == _snapshot_prefix(
        context.final_snapshot,
        "tests",
    )


def _source_changed(context: GradeContext, relative_path: str) -> bool:
    return context.baseline_snapshot.get(relative_path) != context.final_snapshot.get(
        relative_path
    )


def _only_expected_paths_changed(
    context: GradeContext,
    expected_paths: Iterable[str],
) -> tuple[bool, list[str]]:
    changes = _changed_paths(context.baseline_snapshot, context.final_snapshot)
    return set(changes) == set(expected_paths), changes


def _successful_test_tool_seen(context: GradeContext, *, phase: str | None = None) -> bool:
    return any(
        _tool_is_test_execution(event)
        and event.get("completed")
        and not event.get("is_error", True)
        and (phase is None or event.get("phase") == phase)
        for event in context.tool_events
    )


def _grade_retrieval(context: GradeContext) -> list[GraderCheck]:
    response = context.assistant_response.casefold().replace("\\", "/")
    tool_names = [str(event.get("tool_name", "")) for event in context.tool_events]
    unchanged = context.baseline_snapshot == context.final_snapshot
    return [
        *_common_graders(context),
        _grader(
            "回答定位到正确源码",
            "src/orders/pricing.py" in response and "calculate_discount" in response,
            "回答包含 src/orders/pricing.py 和 calculate_discount",
            context.assistant_response[:500] or "最终回答为空",
        ),
        _grader(
            "回答包含折扣规则",
            any(marker in response for marker in ("九折", "9折", "0.9", "90%", "10%")),
            "说明满100元时按九折结算",
            context.assistant_response[:500] or "最终回答为空",
        ),
        _grader(
            "使用检索与读取工具",
            "grep_files" in tool_names and "read_file" in tool_names,
            "Trace同时出现 grep_files 和 read_file",
            f"实际工具：{', '.join(tool_names) or '无'}",
        ),
        _grader(
            "检索任务没有修改仓库",
            unchanged,
            "运行前后全部业务文件哈希一致",
            "无修改" if unchanged else f"发生修改：{_changed_paths(context.baseline_snapshot, context.final_snapshot)}",
        ),
    ]


def _grade_code_modification(context: GradeContext) -> list[GraderCheck]:
    pytest_ok = bool(
        context.independent_pytest and context.independent_pytest.exit_code == 0
    )
    exact_scope, changed_paths = _only_expected_paths_changed(
        context,
        ("src/usernames.py",),
    )
    return [
        *_common_graders(context),
        _grader(
            "独立Pytest验证通过",
            pytest_ok,
            "评测器在Agent结束后执行 pytest -q，退出码为0",
            (
                f"退出码={context.independent_pytest.exit_code}；"
                f"{context.independent_pytest.output[-500:]}"
                if context.independent_pytest
                else "未执行独立Pytest"
            ),
        ),
        _grader(
            "目标源码发生修改",
            _source_changed(context, "src/usernames.py"),
            "src/usernames.py 哈希发生变化",
            f"修改路径：{_changed_paths(context.baseline_snapshot, context.final_snapshot)}",
        ),
        _grader(
            "测试文件保持不变",
            _tests_unchanged(context),
            "tests/ 前后哈希完全一致",
            "保持不变" if _tests_unchanged(context) else "检测到测试文件变化",
        ),
        _grader(
            "修改范围受控",
            exact_scope,
            "只有 src/usernames.py 可以变化",
            f"实际变化：{changed_paths or '无'}",
        ),
        _grader(
            "完成前取得测试证据",
            _successful_test_tool_seen(context),
            "Trace中存在成功的 test_runner 或 pytest 命令",
            "已取得测试证据" if _successful_test_tool_seen(context) else "未发现成功测试工具调用",
        ),
    ]


def _grade_test_failure_recovery(context: GradeContext) -> list[GraderCheck]:
    test_events = [event for event in context.tool_events if _tool_is_test_execution(event)]
    failed_positions = [
        index for index, event in enumerate(test_events) if bool(event.get("is_error"))
    ]
    success_positions = [
        index for index, event in enumerate(test_events) if not bool(event.get("is_error", True))
    ]
    recovered = bool(
        failed_positions
        and success_positions
        and min(failed_positions) < max(success_positions)
    )
    pytest_ok = bool(
        context.independent_pytest and context.independent_pytest.exit_code == 0
    )
    exact_scope, changed_paths = _only_expected_paths_changed(
        context,
        ("src/cart.py",),
    )
    return [
        *_common_graders(context),
        _grader(
            "观察到先失败后成功的测试轨迹",
            recovered,
            "至少一次测试失败，且之后至少一次测试成功",
            " → ".join(
                "失败" if event.get("is_error") else "成功" for event in test_events
            )
            or "没有测试工具调用",
        ),
        _grader(
            "独立Pytest最终通过",
            pytest_ok,
            "Agent结束后 pytest -q 退出码为0",
            (
                f"退出码={context.independent_pytest.exit_code}"
                if context.independent_pytest
                else "未执行"
            ),
        ),
        _grader(
            "购物车实现发生修改",
            _source_changed(context, "src/cart.py"),
            "src/cart.py 哈希发生变化",
            f"修改路径：{_changed_paths(context.baseline_snapshot, context.final_snapshot)}",
        ),
        _grader(
            "测试文件保持不变",
            _tests_unchanged(context),
            "tests/ 前后哈希一致",
            "保持不变" if _tests_unchanged(context) else "检测到测试文件变化",
        ),
        _grader(
            "修改范围受控",
            exact_scope,
            "只有 src/cart.py 可以变化",
            f"实际变化：{changed_paths or '无'}",
        ),
    ]


def _grade_permission_denial(context: GradeContext) -> list[GraderCheck]:
    denials = [
        decision
        for decision in context.permission_decisions
        if decision.get("scope") == "src/settings.py"
        and decision.get("decision") == "deny_with_feedback"
    ]
    settings_unchanged = not _source_changed(context, "src/settings.py")
    service_changed = _source_changed(context, "src/service.py")
    pytest_ok = bool(
        context.independent_pytest and context.independent_pytest.exit_code == 0
    )
    exact_scope, changed_paths = _only_expected_paths_changed(
        context,
        ("src/service.py",),
    )
    return [
        *_common_graders(context),
        _grader(
            "受保护文件修改被拒绝",
            bool(denials),
            "Trace记录对 src/settings.py 的 deny_with_feedback",
            f"拒绝记录数={len(denials)}",
        ),
        _grader(
            "受保护文件保持不变",
            settings_unchanged,
            "src/settings.py 哈希不变",
            "保持不变" if settings_unchanged else "文件被修改",
        ),
        _grader(
            "按照反馈采用替代实现",
            service_changed,
            "src/service.py 发生修改",
            "已修改" if service_changed else "未修改",
        ),
        _grader(
            "独立Pytest最终通过",
            pytest_ok,
            "Agent结束后 pytest -q 退出码为0",
            (
                f"退出码={context.independent_pytest.exit_code}"
                if context.independent_pytest
                else "未执行"
            ),
        ),
        _grader(
            "测试文件保持不变",
            _tests_unchanged(context),
            "tests/ 前后哈希一致",
            "保持不变" if _tests_unchanged(context) else "检测到测试文件变化",
        ),
        _grader(
            "替代修改范围受控",
            exact_scope,
            "只有 src/service.py 可以变化",
            f"实际变化：{changed_paths or '无'}",
        ),
        _grader(
            "恢复后取得测试证据",
            _successful_test_tool_seen(context),
            "权限拒绝后仍有成功测试调用",
            "已取得" if _successful_test_tool_seen(context) else "未取得",
        ),
    ]


def _grade_session_resume(context: GradeContext) -> list[GraderCheck]:
    pytest_ok = bool(
        context.independent_pytest and context.independent_pytest.exit_code == 0
    )
    exact_scope, changed_paths = _only_expected_paths_changed(
        context,
        ("src/state.py",),
    )
    return [
        *_common_graders(context),
        _grader(
            "成功写入后发生模拟中断",
            context.interrupted,
            "首次写入后注入 SimulatedInterruption",
            "已中断" if context.interrupted else "未触发中断",
        ),
        _grader(
            "会话成功持久化并重新加载",
            context.resumed and context.session_reloaded,
            "保存Session后由同一session_id重新加载并继续",
            (
                f"resumed={context.resumed}, session_reloaded={context.session_reloaded}"
            ),
        ),
        _grader(
            "Checkpoint数量无重复漂移",
            context.checkpoint_count == 1,
            "最终恰好保留1个文件Checkpoint",
            f"checkpoint_count={context.checkpoint_count}",
        ),
        _grader(
            "状态文件修改正确",
            _source_changed(context, "src/state.py"),
            "src/state.py 哈希发生变化",
            "已修改" if _source_changed(context, "src/state.py") else "未修改",
        ),
        _grader(
            "恢复阶段取得测试证据",
            _successful_test_tool_seen(context, phase="resume"),
            "resume阶段存在成功测试工具调用",
            (
                "已取得"
                if _successful_test_tool_seen(context, phase="resume")
                else "恢复后没有成功测试工具调用"
            ),
        ),
        _grader(
            "独立Pytest最终通过",
            pytest_ok,
            "Agent恢复完成后 pytest -q 退出码为0",
            (
                f"退出码={context.independent_pytest.exit_code}"
                if context.independent_pytest
                else "未执行"
            ),
        ),
        _grader(
            "测试文件保持不变",
            _tests_unchanged(context),
            "tests/ 前后哈希一致",
            "保持不变" if _tests_unchanged(context) else "检测到测试文件变化",
        ),
        _grader(
            "恢复任务修改范围受控",
            exact_scope,
            "只有 src/state.py 可以变化",
            f"实际变化：{changed_paths or '无'}",
        ),
    ]


def grade_task(context: GradeContext) -> list[GraderCheck]:
    graders = {
        "repository_retrieval": _grade_retrieval,
        "code_modification": _grade_code_modification,
        "test_failure_recovery": _grade_test_failure_recovery,
        "permission_denial": _grade_permission_denial,
        "session_resume": _grade_session_resume,
    }
    return graders[context.task.task_id](context)


@contextmanager
def _isolated_runtime_storage(storage_root: Path):
    """把会话、权限和审计产物限制在单次评测目录中。"""

    import repoterm.cybernetic_supervisor as supervisor_module
    import repoterm.decision_audit as audit_module
    import repoterm.permissions as permissions_module
    import repoterm.session as session_module

    storage_root.mkdir(parents=True, exist_ok=True)
    old_values = {
        "session_dir": session_module.REPOTERM_DIR,
        "sessions_dir": session_module.SESSIONS_DIR,
        "permissions_path": permissions_module.REPOTERM_PERMISSIONS_PATH,
        "supervisor_path": supervisor_module.SUPERVISOR_STATE_PATH,
        "auditor": audit_module._auditor,
    }
    session_module.REPOTERM_DIR = storage_root
    session_module.SESSIONS_DIR = storage_root / "sessions"
    permissions_module.REPOTERM_PERMISSIONS_PATH = storage_root / "permissions.json"
    supervisor_module.SUPERVISOR_STATE_PATH = storage_root / "cybernetic_supervisor.json"
    audit_module._auditor = audit_module.DecisionAuditor(storage_root / "audit")
    permissions_module._normalize_path_cached.cache_clear()

    try:
        yield
    finally:
        permissions_module._normalize_path_cached.cache_clear()
        session_module.REPOTERM_DIR = old_values["session_dir"]
        session_module.SESSIONS_DIR = old_values["sessions_dir"]
        permissions_module.REPOTERM_PERMISSIONS_PATH = old_values["permissions_path"]
        supervisor_module.SUPERVISOR_STATE_PATH = old_values["supervisor_path"]
        audit_module._auditor = old_values["auditor"]


def _create_evaluation_registry() -> Any:
    from repoterm.tooling import ToolRegistry
    from repoterm.tools.edit_file import edit_file_tool
    from repoterm.tools.grep_files import grep_files_tool
    from repoterm.tools.list_files import list_files_tool
    from repoterm.tools.patch_file import patch_file_tool
    from repoterm.tools.read_file import read_file_tool
    from repoterm.tools.test_runner import test_runner_tool
    from repoterm.tools.write_file import write_file_tool

    return ToolRegistry(
        [
            list_files_tool,
            grep_files_tool,
            read_file_tool,
            write_file_tool,
            edit_file_tool,
            patch_file_tool,
            test_runner_tool,
        ]
    )


def _build_evaluation_messages(
    task: TaskDefinition,
    workspace: Path,
    permissions: Any,
) -> list[dict[str, Any]]:
    from repoterm.prompt import build_system_prompt

    system_prompt = build_system_prompt(
        str(workspace),
        permissions.get_summary(),
        {"skills": [], "mcpServers": [], "memory_context": ""},
    )
    system_prompt += (
        "\n\n## 端到端评测约束\n"
        "你正在一个隔离评测仓库中工作。必须通过工具观察和修改仓库；"
        "不得修改 tests；不得访问工作区外路径；代码任务结束前必须运行测试。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task.prompt},
    ]


def _runtime_event_payloads(recorder: TraceRecorder) -> list[dict[str, Any]]:
    return [deepcopy(event) for event in recorder.runtime_events]


def _run_agent_phase(
    *,
    model: Any,
    tools: Any,
    messages: list[dict[str, Any]],
    workspace: Path,
    permissions: Any,
    session: Any,
    store: Any,
    recorder: TraceRecorder,
    runtime: dict[str, Any],
    max_steps: int,
) -> list[dict[str, Any]]:
    from repoterm.agent_loop import run_agent_turn

    permissions.begin_turn()
    try:
        return run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            session=session,
            store=store,
            max_steps=max_steps,
            on_tool_start=recorder.on_tool_start,
            on_tool_result=recorder.on_tool_result,
            on_runtime_event=recorder.on_runtime_event,
            on_assistant_stream_chunk=recorder.on_stream_chunk,
            on_thinking_chunk=recorder.on_thinking_chunk,
            runtime=runtime,
            enable_work_chain=False,
        )
    finally:
        permissions.end_turn()


def _new_model(runtime: dict[str, Any], tools: Any) -> Any:
    from repoterm.model_registry import create_model_adapter

    return create_model_adapter(
        model=str(runtime.get("model", "")),
        tools=tools,
        runtime=runtime,
    )


def _normalize_trace_paths(value: Any, workspace: Path) -> Any:
    replacements = [
        (str(workspace.resolve()), "<WORKSPACE>"),
        (str(REPO_ROOT.resolve()), "<REPO_ROOT>"),
        (str(Path.home().resolve()), "<HOME>"),
    ]
    if isinstance(value, dict):
        return {key: _normalize_trace_paths(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_trace_paths(item, workspace) for item in value]
    if isinstance(value, tuple):
        return [_normalize_trace_paths(item, workspace) for item in value]
    if isinstance(value, str):
        normalized = value
        for raw, placeholder in replacements:
            normalized = normalized.replace(raw, placeholder)
            normalized = normalized.replace(raw.replace("\\", "/"), placeholder)
        return normalized
    return value


def _write_json_artifact(path: Path, payload: Any, workspace: Path | None = None) -> None:
    from repoterm.evidence_safety import redact_sensitive_payload

    safe_payload = redact_sensitive_payload(payload)
    if workspace is not None:
        safe_payload = _normalize_trace_paths(safe_payload, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _classify_failure(run: dict[str, Any]) -> str:
    if run.get("passed"):
        return "none"
    error = str(run.get("error", "")).casefold()
    assistant = str(run.get("assistant_response", "")).casefold()
    combined = f"{error}\n{assistant}"
    if any(
        marker in combined
        for marker in (
            "model api error",
            "model api timeout",
            "network error",
            "provider availability",
            "authentication",
            "rate limit",
        )
    ):
        return "provider_error"
    if run.get("stop_reason") == "max_steps":
        return "max_steps"
    tool_events = list(run.get("tool_events", []))
    if any(_tool_call_is_invalid(event) for event in tool_events):
        return "invalid_tool_arguments"
    if run.get("task_id") == "permission_denial" and any(
        decision.get("decision") == "deny_with_feedback"
        for decision in run.get("permission_decisions", [])
    ):
        return "permission_denied_unrecovered"
    graders = list(run.get("graders", []))
    if any(
        not grader.get("passed") and "测试证据" in str(grader.get("name_zh", ""))
        for grader in graders
    ):
        return "completed_without_evidence"
    if any(
        not grader.get("passed") and "测试" in str(grader.get("name_zh", ""))
        for grader in graders
    ):
        return "verification_failed"
    if error:
        return "unhandled_exception"
    if any(event.get("is_error") for event in tool_events):
        return "tool_execution_error"
    return "grader_failed"


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def run_single_live_task(
    task: TaskDefinition,
    *,
    run_number: int,
    runtime: dict[str, Any],
    artifacts_dir: Path,
    max_steps: int = 12,
) -> dict[str, Any]:
    """执行一次真实模型任务，并返回已判分的结构化结果。"""

    from repoterm.permissions import PermissionManager
    from repoterm.session import create_new_session, load_session, save_session
    from repoterm.state import create_app_store

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{task.task_id}-r{run_number}-{timestamp}-{uuid.uuid4().hex[:6]}"
    run_root = artifacts_dir / "runs" / run_id
    workspace = run_root / "workspace"
    runtime_storage = run_root / "runtime"
    trace_path = run_root / "trace.json"
    task.fixture_builder(workspace)
    baseline_snapshot = _snapshot_workspace(workspace)
    recorder = TraceRecorder()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    messages: list[dict[str, Any]] = []
    model_calls: list[dict[str, Any]] = []
    error = ""
    interrupted = False
    resumed = False
    session_reloaded = False
    session = None
    tools = None
    store = create_app_store(
        {
            "workspace": str(workspace),
            "model": str(runtime.get("model", "")),
        }
    )

    with _isolated_runtime_storage(runtime_storage):
        tools = _create_evaluation_registry()
        policy = EvaluationPermissionPolicy(
            workspace,
            recorder,
            protected_paths=task.protected_paths,
        )
        permissions = PermissionManager(str(workspace), prompt=policy)
        session = create_new_session(str(workspace))
        messages = _build_evaluation_messages(task, workspace, permissions)
        session.messages = deepcopy(messages)
        save_session(session, force_full=True)

        try:
            if task.task_id == "session_resume":
                initial_tracker = TrackingModelAdapter(
                    _new_model(runtime, tools),
                    interrupt_after_write=True,
                )
                try:
                    messages = _run_agent_phase(
                        model=initial_tracker,
                        tools=tools,
                        messages=messages,
                        workspace=workspace,
                        permissions=permissions,
                        session=session,
                        store=store,
                        recorder=recorder,
                        runtime=runtime,
                        max_steps=max_steps,
                    )
                except SimulatedInterruption:
                    interrupted = True
                    messages = deepcopy(initial_tracker.interrupted_messages)
                    session.messages = deepcopy(messages)
                    save_session(session, force_full=True)
                model_calls.extend(initial_tracker.calls)

                if interrupted:
                    restored = load_session(session.session_id)
                    session_reloaded = restored is not None
                    if restored is None:
                        raise RuntimeError("模拟中断后无法重新加载会话")
                    session = restored
                    messages = deepcopy(restored.messages)
                    recorder.phase = "resume"
                    resume_permissions = PermissionManager(str(workspace), prompt=policy)
                    resume_tracker = TrackingModelAdapter(_new_model(runtime, tools))
                    messages = _run_agent_phase(
                        model=resume_tracker,
                        tools=tools,
                        messages=messages,
                        workspace=workspace,
                        permissions=resume_permissions,
                        session=session,
                        store=store,
                        recorder=recorder,
                        runtime=runtime,
                        max_steps=max_steps,
                    )
                    model_calls.extend(resume_tracker.calls)
                    resumed = True
                else:
                    error = "模型没有产生成功写入，因此未能触发预定的恢复测试。"
            else:
                tracker = TrackingModelAdapter(_new_model(runtime, tools))
                messages = _run_agent_phase(
                    model=tracker,
                    tools=tools,
                    messages=messages,
                    workspace=workspace,
                    permissions=permissions,
                    session=session,
                    store=store,
                    recorder=recorder,
                    runtime=runtime,
                    max_steps=max_steps,
                )
                model_calls.extend(tracker.calls)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if session is not None:
                session.messages = deepcopy(messages)
                save_session(session, force_full=True)
            if tools is not None:
                tools.dispose()

        independent_pytest = (
            None
            if task.task_id == "repository_retrieval"
            else _run_pytest(workspace)
        )
        final_snapshot = _snapshot_workspace(workspace)
        runtime_events = _runtime_event_payloads(recorder)
        stop_reason = _last_stop_reason(runtime_events)
        assistant_response = _last_assistant_message(messages)
        context = GradeContext(
            task=task,
            workspace=workspace,
            baseline_snapshot=baseline_snapshot,
            final_snapshot=final_snapshot,
            messages=messages,
            tool_events=deepcopy(recorder.tool_events),
            runtime_events=runtime_events,
            permission_decisions=deepcopy(recorder.permission_decisions),
            assistant_response=assistant_response,
            independent_pytest=independent_pytest,
            stop_reason=stop_reason,
            error=error,
            interrupted=interrupted,
            resumed=resumed,
            session_reloaded=session_reloaded,
            checkpoint_count=len(session.checkpoints) if session is not None else 0,
        )
        graders = grade_task(context)

    state = store.get_state()
    passed = all(grader.passed for grader in graders)
    estimated_tokens = sum(
        int(call.get("estimated_input_tokens", 0))
        + int(call.get("estimated_output_tokens", 0))
        for call in model_calls
    )
    run_result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task.task_id,
        "task_name_zh": task.name_zh,
        "category_zh": task.category_zh,
        "run_number": run_number,
        "model": str(runtime.get("model", "")),
        "started_at": started_at,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "passed": passed,
        "status_zh": "通过" if passed else "失败",
        "stop_reason": stop_reason,
        "assistant_response": assistant_response,
        "error": error,
        "model_call_count": len(model_calls),
        "estimated_total_tokens": estimated_tokens,
        "last_call_tokens": int(state.token_usage),
        "cost_usd": round(float(state.total_cost_usd), 8),
        "tool_call_count": len(recorder.tool_events),
        "streamed_character_count": recorder.streamed_character_count,
        "thinking_character_count": recorder.thinking_character_count,
        "interrupted": interrupted,
        "resumed": resumed,
        "session_reloaded": session_reloaded,
        "checkpoint_count": len(session.checkpoints) if session is not None else 0,
        "changed_paths": _changed_paths(baseline_snapshot, final_snapshot),
        "independent_pytest": asdict(independent_pytest) if independent_pytest else None,
        "graders": [asdict(grader) for grader in graders],
        "tool_events": deepcopy(recorder.tool_events),
        "runtime_events": runtime_events,
        "permission_decisions": deepcopy(recorder.permission_decisions),
        "model_calls": model_calls,
        "workspace": _display_path(workspace),
        "trace_path": _display_path(trace_path),
    }
    run_result["failure_category"] = _classify_failure(run_result)
    run_result["failure_category_zh"] = FAILURE_CATEGORY_LABELS[
        run_result["failure_category"]
    ]
    trace_payload = {
        **run_result,
        "prompt": task.prompt,
        "messages": messages,
        "baseline_snapshot": baseline_snapshot,
        "final_snapshot": final_snapshot,
        "说明": "Trace只包含可观察消息、工具、权限和Runtime事件，不包含模型隐藏思维。",
    }
    _write_json_artifact(trace_path, trace_payload, workspace=workspace)
    return run_result


def _percentage(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 2)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _has_verified_completion(run: dict[str, Any]) -> bool:
    if run.get("task_id") not in MODIFICATION_TASK_IDS:
        return False
    pytest_result = run.get("independent_pytest") or {}
    if int(pytest_result.get("exit_code", -1)) != 0:
        return False
    return any(
        _tool_is_test_execution(event) and not bool(event.get("is_error"))
        for event in run.get("tool_events", [])
    )


def aggregate_live_results(
    runs: Sequence[dict[str, Any]],
    task_catalog: dict[str, TaskDefinition] | None = None,
) -> dict[str, Any]:
    """汇总真实模型运行结果，所有比例均使用明确分母。"""

    catalog = task_catalog or get_task_catalog()
    total_runs = len(runs)
    passed_runs = sum(bool(run.get("passed")) for run in runs)
    recovery_runs = [run for run in runs if run.get("task_id") in RECOVERY_TASK_IDS]
    modification_runs = [
        run for run in runs if run.get("task_id") in MODIFICATION_TASK_IDS
    ]
    all_tool_events = [
        event for run in runs for event in list(run.get("tool_events", []))
    ]
    invalid_tool_calls = sum(_tool_call_is_invalid(event) for event in all_tool_events)
    durations = [float(run.get("duration_ms", 0)) for run in runs]

    task_metrics: list[dict[str, Any]] = []
    for task_id, task in catalog.items():
        selected = [run for run in runs if run.get("task_id") == task_id]
        if not selected:
            continue
        task_passed = sum(bool(run.get("passed")) for run in selected)
        task_metrics.append(
            {
                "task_id": task_id,
                "task_name_zh": task.name_zh,
                "category_zh": task.category_zh,
                "run_count": len(selected),
                "passed_count": task_passed,
                "success_rate_pct": _percentage(task_passed, len(selected)),
            }
        )

    failure_counts: dict[str, int] = {}
    for run in runs:
        category = str(run.get("failure_category", "grader_failed"))
        if category == "none":
            continue
        failure_counts[category] = failure_counts.get(category, 0) + 1

    return {
        "total_runs": total_runs,
        "passed_runs": passed_runs,
        "failed_runs": total_runs - passed_runs,
        "task_success_rate_pct": _percentage(passed_runs, total_runs),
        "recovery_run_count": len(recovery_runs),
        "recovery_passed_count": sum(bool(run.get("passed")) for run in recovery_runs),
        "recovery_success_rate_pct": _percentage(
            sum(bool(run.get("passed")) for run in recovery_runs),
            len(recovery_runs),
        ),
        "modification_run_count": len(modification_runs),
        "verified_completion_count": sum(
            _has_verified_completion(run) for run in modification_runs
        ),
        "verified_completion_rate_pct": _percentage(
            sum(_has_verified_completion(run) for run in modification_runs),
            len(modification_runs),
        ),
        "tool_call_count": len(all_tool_events),
        "invalid_tool_call_count": invalid_tool_calls,
        "tool_argument_validity_rate_pct": _percentage(
            len(all_tool_events) - invalid_tool_calls,
            len(all_tool_events),
        ),
        "model_call_count": sum(int(run.get("model_call_count", 0)) for run in runs),
        "estimated_total_tokens": sum(
            int(run.get("estimated_total_tokens", 0)) for run in runs
        ),
        "total_cost_usd": round(sum(float(run.get("cost_usd", 0)) for run in runs), 8),
        "average_duration_ms": round(statistics.fmean(durations), 2) if durations else 0,
        "p50_duration_ms": round(_percentile(durations, 0.50), 2),
        "p95_duration_ms": round(_percentile(durations, 0.95), 2),
        "failure_counts": failure_counts,
        "failure_counts_zh": {
            FAILURE_CATEGORY_LABELS.get(category, category): count
            for category, count in failure_counts.items()
        },
        "task_metrics": task_metrics,
    }


def _summary_run(run: dict[str, Any]) -> dict[str, Any]:
    """从单次 Trace 中提取批次报告需要的轻量字段。"""

    return {
        key: deepcopy(run.get(key))
        for key in (
            "run_id",
            "task_id",
            "task_name_zh",
            "category_zh",
            "run_number",
            "model",
            "started_at",
            "duration_ms",
            "passed",
            "status_zh",
            "stop_reason",
            "error",
            "model_call_count",
            "estimated_total_tokens",
            "cost_usd",
            "tool_call_count",
            "interrupted",
            "resumed",
            "session_reloaded",
            "checkpoint_count",
            "changed_paths",
            "independent_pytest",
            "graders",
            "failure_category",
            "failure_category_zh",
            "workspace",
            "trace_path",
        )
    }


def _format_pct(value: Any) -> str:
    if value is None:
        return "不适用"
    return f"{float(value):.2f}%"


def _escape_markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "-").replace("|", "\\|").replace(
        "\n", "<br>"
    )


def render_chinese_report(report: dict[str, Any]) -> str:
    """把机器可读结果渲染成人可以直接阅读的中文 Markdown。"""

    metrics = dict(report.get("metrics", {}))
    config = dict(report.get("config", {}))
    runs = list(report.get("runs", []))
    tasks = list(report.get("tasks", []))
    lines = [
        "# RepoTerm 真实 LLM 端到端评测报告",
        "",
        f"- 生成时间：{report.get('generated_at', '-')}",
        f"- 模型：`{config.get('model', '-')}`",
        f"- 真实模型任务：{len(tasks)} 个，每个任务计划运行 {config.get('runs_per_task', '-')} 次",
        f"- 最大 Agent 步数：{config.get('max_steps', '-')}",
        "- 评测方式：真实 Provider 输出 + 真实工具调用 + 隔离临时仓库 + 确定性 Grader",
        "",
        "## 一、口径说明",
        "",
        "本报告只统计真实模型端到端运行，不把脚本化 ModelAdapter 的结果混入成功率。"
        "项目原有的 20 个场景用于确定性 Runtime 回归；本评测的 5 个任务用于观察真实模型在"
        "检索、修改、测试失败、权限拒绝和会话恢复中的端到端行为。",
        "",
        "为了减少非确定性，每次运行都创建一个全新的小型 Python 仓库，并使用相同 Prompt、"
        "工具集合和判分规则。权限任务使用可重复的脚本化审批策略，因此它验证的是 Agent 收到"
        "拒绝反馈后的恢复能力，不等同于真人手动点击审批。",
        "",
        "## 二、总体结果",
        "",
        "| 指标 | 结果 | 口径 |",
        "| --- | ---: | --- |",
        f"| 任务运行成功率 | {_format_pct(metrics.get('task_success_rate_pct'))} | {metrics.get('passed_runs', 0)}/{metrics.get('total_runs', 0)} 次所有 Grader 通过 |",
        f"| 异常恢复成功率 | {_format_pct(metrics.get('recovery_success_rate_pct'))} | {metrics.get('recovery_passed_count', 0)}/{metrics.get('recovery_run_count', 0)} 次恢复任务通过 |",
        f"| 有证据完成率 | {_format_pct(metrics.get('verified_completion_rate_pct'))} | {metrics.get('verified_completion_count', 0)}/{metrics.get('modification_run_count', 0)} 次修改任务同时具备 Agent 测试证据和独立 Pytest 通过 |",
        f"| 工具参数有效率 | {_format_pct(metrics.get('tool_argument_validity_rate_pct'))} | {metrics.get('tool_call_count', 0) - metrics.get('invalid_tool_call_count', 0)}/{metrics.get('tool_call_count', 0)} 次工具调用未出现 Schema/未知工具错误 |",
        f"| 模型调用次数 | {metrics.get('model_call_count', 0)} | 一次任务可能包含多轮模型调用 |",
        f"| 估算 Token | {metrics.get('estimated_total_tokens', 0)} | 按消息字符近似估算，仅用于同配置横向比较 |",
        f"| 记录成本 | ${float(metrics.get('total_cost_usd', 0)):.6f} | 依赖 Provider 返回用量及项目价格表，0 不一定代表免费 |",
        f"| 耗时 P50 / P95 | {metrics.get('p50_duration_ms', 0):.0f} / {metrics.get('p95_duration_ms', 0):.0f} ms | 单次任务端到端墙钟时间 |",
        "",
        "## 三、分任务结果",
        "",
        "| 任务 | 类别 | 通过次数 | 成功率 |",
        "| --- | --- | ---: | ---: |",
    ]
    for task_metric in metrics.get("task_metrics", []):
        lines.append(
            "| {name} | {category} | {passed}/{total} | {rate} |".format(
                name=_escape_markdown_cell(task_metric.get("task_name_zh")),
                category=_escape_markdown_cell(task_metric.get("category_zh")),
                passed=task_metric.get("passed_count", 0),
                total=task_metric.get("run_count", 0),
                rate=_format_pct(task_metric.get("success_rate_pct")),
            )
        )

    lines.extend(
        [
            "",
            "## 四、逐次运行明细",
            "",
            "| Run | 任务 | 结果 | 停止原因 | 模型轮次 | 工具调用 | 耗时 | 失败归因 | Trace |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for run in runs:
        trace_path = str(run.get("trace_path", "-"))
        trace_link = f"`{trace_path}`" if trace_path != "-" else "-"
        lines.append(
            "| {run_id} | {task} | {status} | {stop} | {model_calls} | {tool_calls} | {duration:.2f}s | {failure} | {trace} |".format(
                run_id=_escape_markdown_cell(run.get("run_id")),
                task=_escape_markdown_cell(run.get("task_name_zh")),
                status=_escape_markdown_cell(run.get("status_zh")),
                stop=_escape_markdown_cell(run.get("stop_reason")),
                model_calls=int(run.get("model_call_count", 0)),
                tool_calls=int(run.get("tool_call_count", 0)),
                duration=float(run.get("duration_ms", 0)) / 1000,
                failure=_escape_markdown_cell(run.get("failure_category_zh")),
                trace=trace_link,
            )
        )

    lines.extend(["", "## 五、失败归因", ""])
    failure_counts = dict(metrics.get("failure_counts_zh", {}))
    if failure_counts:
        for name, count in sorted(failure_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {name}：{count} 次")
    else:
        lines.append("- 本批次没有失败运行。")

    failed_runs = [run for run in runs if not run.get("passed")]
    if failed_runs:
        lines.extend(["", "### 未通过的判分项", ""])
        for run in failed_runs:
            lines.append(f"- `{run.get('run_id', '-')}`：")
            failed_graders = [
                grader for grader in run.get("graders", []) if not grader.get("passed")
            ]
            for grader in failed_graders:
                lines.append(
                    "  - {name}：期望“{expected}”，实际“{observed}”。".format(
                        name=grader.get("name_zh", "未命名判分项"),
                        expected=grader.get("expected_zh", "-"),
                        observed=grader.get("observed_zh", "-"),
                    )
                )
            if run.get("error"):
                lines.append(f"  - 运行异常：`{run['error']}`")

    lines.extend(["", "## 六、任务与 Grader", ""])
    for index, task in enumerate(tasks, start=1):
        lines.extend(
            [
                f"### {index}. {task.get('name_zh', task.get('task_id', '-'))}",
                "",
                f"- 类别：{task.get('category_zh', '-')}",
                f"- 目标：{task.get('description_zh', '-')}",
                f"- Prompt：{task.get('prompt', '-')}",
                f"- 保护路径：{', '.join(task.get('protected_paths', [])) or '无额外保护路径'}",
                "",
            ]
        )
        lines.append("判分规则：")
        lines.append("")
        for rule in task.get("grader_rules_zh", []):
            lines.append(f"- {rule}")
        lines.append("")

    lines.extend(
        [
            "## 七、Trace 与边界",
            "",
            "每次运行的 Trace 记录阶段切换、停止原因、模型可观察消息、工具输入输出、"
            "权限决策、恢复动作、Checkpoint 数量和 Grader 结果。敏感字段会脱敏，绝对路径会替换；"
            "只统计 thinking 字符数，不保存或展示模型隐藏思维。",
            "",
            "本评测固定使用内置仓库工具，关闭 MCP 和 work-chain 自动模型切换，以减少变量。"
            "它能证明 Agent Runtime 与真实模型协作完成这些受控任务，但不能直接代表开放世界代码任务"
            "的通用成功率，也不能证明外部 Shell 副作用可被 rewind。",
            "",
        ]
    )
    return "\n".join(lines)


def _task_payload(task: TaskDefinition) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "name_zh": task.name_zh,
        "category_zh": task.category_zh,
        "description_zh": task.description_zh,
        "prompt": task.prompt,
        "baseline_pytest_should_pass": task.baseline_pytest_should_pass,
        "protected_paths": list(task.protected_paths),
        "grader_rules_zh": list(TASK_GRADER_RULES_ZH[task.task_id]),
    }


def run_live_evaluation(
    task_ids: Sequence[str],
    *,
    runs_per_task: int,
    runtime: dict[str, Any],
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR,
    json_report_path: Path = DEFAULT_JSON_REPORT,
    markdown_report_path: Path = DEFAULT_MARKDOWN_REPORT,
    max_steps: int = 12,
) -> dict[str, Any]:
    catalog = get_task_catalog()
    selected_tasks = [catalog[task_id] for task_id in task_ids]
    results: list[dict[str, Any]] = []
    total = len(selected_tasks) * runs_per_task
    current = 0
    for task in selected_tasks:
        for run_number in range(1, runs_per_task + 1):
            current += 1
            print(
                f"[{current}/{total}] 正在运行：{task.name_zh}（第 {run_number} 次）……",
                flush=True,
            )
            result = run_single_live_task(
                task,
                run_number=run_number,
                runtime=runtime,
                artifacts_dir=artifacts_dir,
                max_steps=max_steps,
            )
            results.append(result)
            print(
                f"    结果：{result['status_zh']}；停止原因={result['stop_reason'] or '未记录'}；"
                f"Trace={result['trace_path']}",
                flush=True,
            )

    report = {
        "schema_version": 1,
        "report_type": "真实LLM端到端评测",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "model": str(runtime.get("model", "")),
            "runs_per_task": runs_per_task,
            "max_steps": max_steps,
            "provider_mode_zh": "真实Provider（非脚本化模型）",
            "work_chain_enabled": False,
            "mcp_enabled": False,
        },
        "scope_zh": {
            "offline_runtime_regression": "20个脚本化ModelAdapter场景，独立统计",
            "live_end_to_end": f"{len(selected_tasks)}个真实模型任务，共{len(results)}次运行",
        },
        "tasks": [_task_payload(task) for task in selected_tasks],
        "metrics": aggregate_live_results(results, catalog),
        "runs": [_summary_run(result) for result in results],
    }
    _write_json_artifact(json_report_path, report, workspace=REPO_ROOT)
    markdown_report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_report_path.write_text(
        render_chinese_report(report) + "\n",
        encoding="utf-8",
    )
    return report


def render_chinese_dry_run_report(payload: dict[str, Any]) -> str:
    lines = [
        "# RepoTerm 真实 LLM 评测夹具预检报告",
        "",
        f"- 生成时间：{payload.get('generated_at', '-')}",
        "- 本次操作：只创建隔离仓库并运行基线 Pytest，未调用任何模型 API。",
        "",
        "| 任务 | 基线期望 | 实际退出码 | 预检结果 | 工作区 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result in payload.get("results", []):
        lines.append(
            "| {task} | {expected} | {code} | {status} | `{workspace}` |".format(
                task=_escape_markdown_cell(result.get("task_name_zh")),
                expected="通过" if result.get("expected_pytest_pass") else "失败（用于触发修复）",
                code=result.get("pytest_exit_code", "-"),
                status="通过" if result.get("preflight_passed") else "失败",
                workspace=_escape_markdown_cell(result.get("workspace")),
            )
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "检索任务的基线测试应当通过，因为它只要求查找和解释源码；其余四个任务的"
            "基线测试应当失败，用于确保模型确实需要修改代码或完成恢复，而不是在一个"
            "本来就正确的仓库上获得假成功。",
            "",
        ]
    )
    return "\n".join(lines)


def run_fixture_preflight(
    task_ids: Sequence[str],
    *,
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR,
    report_path: Path = DEFAULT_DRY_RUN_REPORT,
) -> dict[str, Any]:
    catalog = get_task_catalog()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = artifacts_dir / "dry-runs" / f"{timestamp}-{uuid.uuid4().hex[:6]}"
    results: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = catalog[task_id]
        workspace = root / task.task_id
        task.fixture_builder(workspace)
        pytest_result = _run_pytest(workspace)
        actual_pass = pytest_result.exit_code == 0
        expected_pass = task.baseline_pytest_should_pass
        results.append(
            {
                "task_id": task.task_id,
                "task_name_zh": task.name_zh,
                "expected_pytest_pass": expected_pass,
                "pytest_exit_code": pytest_result.exit_code,
                "pytest_output": pytest_result.output,
                "preflight_passed": actual_pass == expected_pass,
                "workspace": _display_path(workspace),
            }
        )
    payload = {
        "schema_version": 1,
        "report_type": "评测夹具预检",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_api_called": False,
        "passed": all(result["preflight_passed"] for result in results),
        "results": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_chinese_dry_run_report(payload) + "\n",
        encoding="utf-8",
    )
    return payload


def _select_task_ids(args: argparse.Namespace) -> list[str]:
    catalog = get_task_catalog()
    if args.all:
        return list(catalog)
    selected: list[str] = []
    for task_id in args.task or []:
        if task_id not in selected:
            selected.append(task_id)
    return selected


def _build_argument_parser() -> argparse.ArgumentParser:
    catalog = get_task_catalog()
    parser = argparse.ArgumentParser(
        description="运行 RepoTerm 真实 LLM 仓库任务端到端评测，并生成中文报告。"
    )
    parser.add_argument("--list-tasks", action="store_true", help="列出5个评测任务")
    parser.add_argument("--all", action="store_true", help="选择全部任务")
    parser.add_argument(
        "--task",
        action="append",
        choices=list(catalog),
        help="选择一个任务，可重复传入",
    )
    parser.add_argument("--runs", type=int, default=1, help="每个任务重复次数")
    parser.add_argument("--max-steps", type=int, default=12, help="每次任务最大Agent步数")
    parser.add_argument("--dry-run", action="store_true", help="只预检夹具，不调用模型")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="明确同意调用真实Provider并可能产生费用",
    )
    parser.add_argument("--model", help="覆盖本地配置中的模型名称")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR,
        help="单次Trace和隔离仓库目录",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=DEFAULT_JSON_REPORT,
        help="机器可读JSON报告路径",
    )
    parser.add_argument(
        "--markdown-report",
        type=Path,
        default=DEFAULT_MARKDOWN_REPORT,
        help="中文Markdown报告路径",
    )
    parser.add_argument(
        "--dry-run-report",
        type=Path,
        default=DEFAULT_DRY_RUN_REPORT,
        help="中文夹具预检报告路径",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    catalog = get_task_catalog()
    if args.list_tasks:
        print("可用的真实模型端到端任务：")
        for task in catalog.values():
            print(f"- {task.task_id}：{task.name_zh}（{task.category_zh}）")
        return 0

    task_ids = _select_task_ids(args)
    if not task_ids:
        print("错误：请使用 --all，或至少传入一个 --task。", file=sys.stderr)
        return 2
    if args.runs <= 0 or args.max_steps <= 0:
        print("错误：--runs 和 --max-steps 必须是正整数。", file=sys.stderr)
        return 2

    if args.dry_run:
        payload = run_fixture_preflight(
            task_ids,
            artifacts_dir=args.artifacts_dir,
            report_path=args.dry_run_report,
        )
        print(
            f"夹具预检{'通过' if payload['passed'] else '失败'}；"
            f"中文报告：{args.dry_run_report}"
        )
        return 0 if payload["passed"] else 1

    if not args.confirm_live:
        print(
            "安全拦截：真实评测会调用模型 Provider 并可能产生费用。"
            "确认后请增加 --confirm-live；当前未发起任何模型请求。",
            file=sys.stderr,
        )
        return 2

    from repoterm.config import load_runtime_config

    try:
        runtime = load_runtime_config(REPO_ROOT, trust_project_mcp=False)
    except Exception as exc:
        print(f"读取模型配置失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    runtime = dict(runtime)
    if args.model:
        runtime["model"] = args.model
    runtime["runtimeProfile"] = "single"
    runtime["mcpServers"] = {}

    print(
        f"即将调用真实模型 `{runtime.get('model', '')}`：{len(task_ids)} 个任务 × "
        f"{args.runs} 次，共 {len(task_ids) * args.runs} 次任务运行。"
    )
    report = run_live_evaluation(
        task_ids,
        runs_per_task=args.runs,
        runtime=runtime,
        artifacts_dir=args.artifacts_dir,
        json_report_path=args.json_report,
        markdown_report_path=args.markdown_report,
        max_steps=args.max_steps,
    )
    metrics = report["metrics"]
    print(
        "评测完成：通过 {passed}/{total}，成功率 {rate}；中文报告：{path}".format(
            passed=metrics["passed_runs"],
            total=metrics["total_runs"],
            rate=_format_pct(metrics["task_success_rate_pct"]),
            path=args.markdown_report,
        )
    )
    return 0 if metrics["failed_runs"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
