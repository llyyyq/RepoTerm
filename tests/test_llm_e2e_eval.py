from __future__ import annotations

from pathlib import Path

import pytest

from repoterm.llm_e2e_eval import (
    EvaluationPermissionPolicy,
    SimulatedInterruption,
    TraceRecorder,
    TrackingModelAdapter,
    aggregate_live_results,
    get_task_catalog,
    main,
    render_chinese_report,
    run_fixture_preflight,
    run_single_live_task,
)
from repoterm.types import AgentStep


class _DummyModel:
    model_id = "dummy-model"

    def next(
        self,
        messages,
        on_stream_chunk=None,
        on_thinking_delta=None,
        store=None,
    ):
        return AgentStep(type="assistant", content="完成", kind="final")


class _ScriptedRepositoryModel:
    model_id = "scripted-repository-model"

    def __init__(self) -> None:
        self.calls = 0

    def next(
        self,
        messages,
        on_stream_chunk=None,
        on_thinking_delta=None,
        store=None,
    ):
        steps = [
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "grep-1",
                        "toolName": "grep_files",
                        "input": {
                            "pattern": "calculate_discount",
                            "path": ".",
                            "include": "*.py",
                        },
                    }
                ],
            ),
            AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "read-1",
                        "toolName": "read_file",
                        "input": {"path": "src/orders/pricing.py"},
                    }
                ],
            ),
            AgentStep(
                type="assistant",
                kind="final",
                content=(
                    "真实实现位于 src/orders/pricing.py 的 calculate_discount 函数。"
                    "订单金额满100元（10000分）时返回原价的90%，也就是九折；否则保持原价。"
                ),
            ),
        ]
        step = steps[self.calls]
        self.calls += 1
        return step


def _synthetic_run(*, passed: bool = True) -> dict:
    return {
        "run_id": "code_modification-r1",
        "task_id": "code_modification",
        "task_name_zh": "代码修改与测试验证",
        "category_zh": "修改",
        "run_number": 1,
        "model": "dummy-model",
        "duration_ms": 1200,
        "passed": passed,
        "status_zh": "通过" if passed else "失败",
        "stop_reason": "done",
        "error": "" if passed else "",
        "model_call_count": 2,
        "estimated_total_tokens": 100,
        "cost_usd": 0.001,
        "tool_call_count": 2,
        "independent_pytest": {"exit_code": 0, "output": "2 passed", "duration_ms": 80},
        "graders": [
            {
                "name_zh": "独立Pytest最终通过",
                "passed": passed,
                "expected_zh": "退出码为0",
                "observed_zh": "退出码=0" if passed else "未取得证据",
            }
        ],
        "tool_events": [
            {
                "tool_name": "test_runner",
                "input": {"path": "tests"},
                "output": "2 passed",
                "is_error": False,
            },
            {
                "tool_name": "edit_file",
                "input": {"path": "src/usernames.py"},
                "output": "ok",
                "is_error": False,
            },
        ],
        "failure_category": "none" if passed else "grader_failed",
        "failure_category_zh": "无" if passed else "确定性判分未通过",
        "trace_path": ".temp/llm_e2e/runs/example/trace.json",
    }


def test_task_catalog_contains_five_chinese_repository_tasks():
    catalog = get_task_catalog()

    assert list(catalog) == [
        "repository_retrieval",
        "code_modification",
        "test_failure_recovery",
        "permission_denial",
        "session_resume",
    ]
    assert all(task.name_zh and task.description_zh and task.prompt for task in catalog.values())
    assert catalog["repository_retrieval"].baseline_pytest_should_pass is True
    assert all(
        not catalog[task_id].baseline_pytest_should_pass
        for task_id in list(catalog)[1:]
    )


def test_fixture_preflight_has_expected_pass_and_failure_baselines(tmp_path: Path):
    report_path = tmp_path / "评测夹具预检.md"
    payload = run_fixture_preflight(
        list(get_task_catalog()),
        artifacts_dir=tmp_path / "artifacts",
        report_path=report_path,
    )

    assert payload["model_api_called"] is False
    assert payload["passed"] is True
    assert len(payload["results"]) == 5
    assert payload["results"][0]["pytest_exit_code"] == 0
    assert all(result["pytest_exit_code"] != 0 for result in payload["results"][1:])
    report_text = report_path.read_text(encoding="utf-8")
    assert "评测夹具预检报告" in report_text
    assert "未调用任何模型 API" in report_text


def test_permission_policy_protects_graders_and_provides_recovery_feedback(
    tmp_path: Path,
):
    recorder = TraceRecorder()
    policy = EvaluationPermissionPolicy(
        tmp_path,
        recorder,
        protected_paths=("src/settings.py",),
    )

    test_decision = policy(
        {"kind": "edit", "scope": str(tmp_path / "tests/test_service.py")}
    )
    protected_decision = policy(
        {"kind": "edit", "scope": str(tmp_path / "src/settings.py")}
    )
    normal_decision = policy(
        {"kind": "edit", "scope": str(tmp_path / "src/service.py")}
    )

    assert test_decision["decision"] == "deny_with_feedback"
    assert "不允许修改" in test_decision["feedback"]
    assert protected_decision["decision"] == "deny_with_feedback"
    assert "替代" in protected_decision["feedback"] or "覆盖" in protected_decision["feedback"]
    assert normal_decision == {"decision": "allow_once"}
    assert len(recorder.permission_decisions) == 3


def test_tracking_adapter_interrupts_before_second_model_call_after_write():
    tracker = TrackingModelAdapter(_DummyModel(), interrupt_after_write=True)
    initial_messages = [{"role": "user", "content": "修改文件"}]

    step = tracker.next(initial_messages)
    assert step.content == "完成"

    messages_after_write = [
        *initial_messages,
        {
            "role": "tool_result",
            "toolName": "edit_file",
            "toolUseId": "tool-1",
            "content": "写入成功",
            "isError": False,
        },
    ]
    with pytest.raises(SimulatedInterruption):
        tracker.next(messages_after_write)

    assert tracker.interrupted is True
    assert tracker.interrupted_messages == messages_after_write
    assert len(tracker.calls) == 1


def test_single_task_harness_crosses_real_agent_loop_tools_trace_and_graders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scripted_model = _ScriptedRepositoryModel()
    monkeypatch.setattr(
        "repoterm.llm_e2e_eval._new_model",
        lambda runtime, tools: scripted_model,
    )

    result = run_single_live_task(
        get_task_catalog()["repository_retrieval"],
        run_number=1,
        runtime={"model": "scripted-repository-model", "runtimeProfile": "single"},
        artifacts_dir=tmp_path / "artifacts",
        max_steps=6,
    )

    assert result["passed"] is True
    assert result["stop_reason"] == "done"
    assert [event["tool_name"] for event in result["tool_events"]] == [
        "grep_files",
        "read_file",
    ]
    assert result["model_call_count"] == 3
    trace_path = tmp_path / "artifacts" / "runs" / result["run_id"] / "trace.json"
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "不包含模型隐藏思维" in trace_text
    assert "src/orders/pricing.py" in trace_text


def test_aggregate_and_markdown_report_use_explicit_chinese_metrics():
    run = _synthetic_run()
    metrics = aggregate_live_results([run])
    report = {
        "generated_at": "2026-07-21T00:00:00+00:00",
        "config": {"model": "dummy-model", "runs_per_task": 1, "max_steps": 12},
        "tasks": [
            {
                "task_id": "code_modification",
                "name_zh": "代码修改与测试验证",
                "category_zh": "修改",
                "description_zh": "修改实现并验证",
                "prompt": "修复代码并运行测试",
                "protected_paths": [],
            }
        ],
        "metrics": metrics,
        "runs": [run],
    }

    assert metrics["task_success_rate_pct"] == 100.0
    assert metrics["verified_completion_rate_pct"] == 100.0
    assert metrics["tool_argument_validity_rate_pct"] == 100.0
    markdown = render_chinese_report(report)
    assert "总体结果" in markdown
    assert "任务运行成功率" in markdown
    assert "20 个场景用于确定性 Runtime 回归" in markdown
    assert "不保存或展示模型隐藏思维" in markdown


def test_live_cli_requires_explicit_confirmation_before_loading_provider(capsys):
    exit_code = main(["--task", "repository_retrieval"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "当前未发起任何模型请求" in captured.err
