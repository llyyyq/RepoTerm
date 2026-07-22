"""Run the 20 deterministic AgentOps scenarios and publish Chinese reports."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/test_agentops_scenarios.py"
DEFAULT_JSON_REPORT = REPO_ROOT / "benchmarks" / "runtime_regression_results.json"
DEFAULT_MARKDOWN_REPORT = REPO_ROOT / "benchmarks" / "runtime_regression_results.md"


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    test_name: str
    name_zh: str
    category_zh: str
    scripted_model_zh: str
    asserted_tools: tuple[str, ...]
    asserted_stop_reason: str
    grader_zh: str


SCENARIOS: tuple[ScenarioDefinition, ...] = (
    ScenarioDefinition("test_agentops_repository_retrieval_returns_grounded_symbol_location", "仓库检索返回真实符号位置", "检索", "先调用grep_files，再基于结果回答", ("grep_files",), "done", "断言源码位置、工具结果回传及最终回答"),
    ScenarioDefinition("test_agentops_code_modification_requires_diff_and_creates_checkpoint", "代码修改经过Diff审查并创建Checkpoint", "安全写入", "调用write_file后结束", ("write_file",), "done", "断言Diff、审批、文件内容、Checkpoint及持久化结果"),
    ScenarioDefinition("test_agentops_test_failure_is_returned_to_the_next_model_step", "测试失败返回下一轮模型决策", "错误恢复", "调用test_runner后解释失败", ("test_runner",), "done", "断言失败ToolResult进入下一轮消息并包含失败用例"),
    ScenarioDefinition("test_agentops_permission_denial_preserves_file_and_returns_guidance", "权限拒绝保留文件并返回用户反馈", "权限", "尝试write_file后根据拒绝反馈结束", ("write_file",), "done", "断言deny_with_feedback、文件不变且无Checkpoint"),
    ScenarioDefinition("test_agentops_interrupted_turn_can_resume_and_rewind_from_checkpoint", "中断后恢复并从Checkpoint回退", "会话恢复", "写入后中断；重载Session后read_file验证", ("write_file", "read_file"), "done", "断言中断持久化、恢复读取和rewind恢复原文件"),
    ScenarioDefinition("test_agentops_invalid_tool_arguments_become_validation_result", "非法工具参数归一化为校验结果", "工具契约", "生成空检索pattern后读取错误", ("grep_files",), "done", "断言JSON/validator错误进入ToolResult而不崩溃"),
    ScenarioDefinition("test_agentops_unknown_tool_is_normalized_without_crashing_turn", "未知工具归一化且不中断Agent Turn", "工具契约", "调用不存在工具后选择其他方案", ("repository_magic",), "done", "断言Unknown tool被结构化返回并正常结束"),
    ScenarioDefinition("test_agentops_tool_exception_is_normalized_and_returned_to_model", "工具运行异常归一化并返回模型", "工具契约", "调用会抛出RuntimeError的parse_repository", ("parse_repository",), "done", "断言异常类型、工具名和错误消息进入ToolResult"),
    ScenarioDefinition("test_agentops_large_tool_output_keeps_head_error_and_tail", "大输出保留头部、错误行和尾部", "上下文治理", "调用返回超长日志的run_command", ("run_command",), "done", "断言输出被截断且关键头/错误/尾证据保留"),
    ScenarioDefinition("test_agentops_workspace_escape_is_rejected_without_leaking_file", "工作区越界读取被拒绝且不泄露内容", "路径安全", "尝试read_file读取工作区外文件", ("read_file",), "done", "断言越界错误结构化返回且秘密内容未泄露"),
    ScenarioDefinition("test_agentops_dangerous_shell_command_is_denied_before_execution", "危险Shell命令执行前被拒绝", "命令安全", "尝试run_command执行git reset --hard", ("run_command",), "done", "断言危险命令触发审批、被拒绝且本地文件不变"),
    ScenarioDefinition("test_agentops_successful_test_run_is_returned_as_verification_evidence", "成功测试作为验证证据进入停止事件", "验证", "调用test_runner后基于通过结果回答", ("test_runner",), "done", "断言Pytest通过且stop evidence_summary包含测试证据"),
    ScenarioDefinition("test_agentops_rewind_removes_file_created_during_agent_turn", "Rewind删除Agent新建文件", "会话恢复", "调用write_file创建文件后结束", ("write_file",), "done", "断言Checkpoint记录原文件不存在且rewind删除新文件"),
    ScenarioDefinition("test_agentops_repeated_session_load_is_idempotent", "重复加载Session保持幂等", "会话恢复", "不调用模型，重复读取同一Session", (), "不适用", "断言消息、历史和持久化文件均无状态漂移"),
    ScenarioDefinition("test_agentops_delta_resume_reconstructs_messages_without_duplicates", "增量Delta恢复消息且不重复", "会话恢复", "不调用模型，保存全量快照后追加Delta", (), "不适用", "断言消息顺序、Transcript ID和数量正确"),
    ScenarioDefinition("test_agentops_max_step_budget_stops_repeating_tool_loop", "最大步数终止重复工具循环", "终止控制", "连续两次调用inspect_repository", ("inspect_repository",), "max_steps", "断言模型调用次数、工具结果数和max_steps停止原因"),
    ScenarioDefinition("test_agentops_empty_model_response_retries_then_completes", "空模型响应重试后完成", "异常恢复", "先返回空assistant，再返回最终答案", (), "done", "断言注入继续提示且第二次响应正常完成"),
    ScenarioDefinition("test_agentops_recoverable_thinking_pause_resumes_next_model_step", "可恢复thinking pause继续下一模型步骤", "异常恢复", "先返回pause_turn，再继续最终回答", (), "done", "断言pause进度、恢复提示和最终回答"),
    ScenarioDefinition("test_agentops_progress_message_does_not_terminate_turn", "Progress消息不会提前终止Turn", "响应协议", "先返回progress，再返回final", (), "done", "断言progress与final分流且仅final终止"),
    ScenarioDefinition("test_agentops_stable_task_pack_carries_latest_tool_evidence", "StableTaskPack保留最新工具证据", "上下文治理", "调用pytest_probe后根据证据完成", ("pytest_probe",), "done", "断言StableTaskPack包含任务证据、预算状态和stop evidence"),
)


_RESULT_PATTERN = re.compile(
    r"test_agentops_scenarios\.py::(?P<name>test_[A-Za-z0-9_]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED)"
)


def parse_pytest_scenarios(output: str) -> dict[str, str]:
    return {
        match.group("name"): match.group("status").lower()
        for match in _RESULT_PATTERN.finditer(output)
    }


def run_round(round_number: int) -> dict:
    started = time.monotonic()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST_FILE,
            "-vv",
            "--tb=short",
            "--color=no",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    parsed = parse_pytest_scenarios(output)
    results = []
    for scenario in SCENARIOS:
        status = parsed.get(scenario.test_name, "missing")
        results.append(
            {
                "test_name": scenario.test_name,
                "status": status,
                "passed": status == "passed",
            }
        )
    return {
        "round": round_number,
        "exit_code": completed.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "collected_count": len(parsed),
        "passed_count": sum(item["passed"] for item in results),
        "results": results,
        "pytest_output_tail": output[-4000:],
    }


def build_report(rounds: Sequence[dict]) -> dict:
    total_executions = len(SCENARIOS) * len(rounds)
    passed_executions = sum(
        item["passed"] for round_result in rounds for item in round_result["results"]
    )
    scenario_summaries = []
    for scenario in SCENARIOS:
        statuses = []
        for round_result in rounds:
            match = next(
                item
                for item in round_result["results"]
                if item["test_name"] == scenario.test_name
            )
            statuses.append(match["status"])
        scenario_summaries.append(
            {
                **asdict(scenario),
                "asserted_tools": list(scenario.asserted_tools),
                "round_statuses": statuses,
                "passed_count": sum(status == "passed" for status in statuses),
                "run_count": len(statuses),
            }
        )
    durations = [round_result["duration_ms"] for round_result in rounds]
    return {
        "schema_version": 1,
        "report_type": "确定性Runtime回归",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "model_mode_zh": "脚本化ScenarioModel，模型输出由测试预先设定",
            "provider_called": False,
            "test_file": TEST_FILE,
            "scenario_count": len(SCENARIOS),
            "round_count": len(rounds),
            "purpose_zh": "验证Agent控制逻辑、工具结果、权限边界、上下文和恢复链路，不代表真实LLM开放任务成功率。",
        },
        "metrics": {
            "total_executions": total_executions,
            "passed_executions": passed_executions,
            "failed_executions": total_executions - passed_executions,
            "success_rate_pct": round(passed_executions / total_executions * 100, 2)
            if total_executions
            else None,
            "average_round_duration_ms": round(statistics.fmean(durations), 2)
            if durations
            else 0,
        },
        "scenarios": scenario_summaries,
        "rounds": list(rounds),
    }


def render_markdown(report: dict) -> str:
    metrics = report["metrics"]
    method = report["methodology"]
    lines = [
        "# RepoTerm 确定性 Runtime 回归报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 模型模式：{method['model_mode_zh']}",
        f"- 是否调用真实Provider：{'是' if method['provider_called'] else '否'}",
        f"- 场景与轮次：{method['scenario_count']}个场景 × {method['round_count']}轮",
        f"- 执行结果：{metrics['passed_executions']}/{metrics['total_executions']}通过（{metrics['success_rate_pct']:.2f}%）",
        "",
        "## 一、定位与边界",
        "",
        method["purpose_zh"],
        "脚本化模型固定每一步assistant/tool_calls输出，使同一个控制逻辑故障可以稳定复现；真实模型行为由另一份端到端报告单独验证。",
        "",
        "## 二、分轮结果",
        "",
        "| 轮次 | 收集场景 | 通过 | Pytest退出码 | 耗时 |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for round_result in report["rounds"]:
        lines.append(
            f"| {round_result['round']} | {round_result['collected_count']} | "
            f"{round_result['passed_count']} | {round_result['exit_code']} | "
            f"{round_result['duration_ms'] / 1000:.2f}s |"
        )
    lines.extend(
        [
            "",
            "## 三、20个场景与判分规则",
            "",
            "| # | 场景 | 类别 | 脚本化模型输出 | 验证工具 | 停止原因 | 三轮结果 | Grader |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, scenario in enumerate(report["scenarios"], start=1):
        tools = ", ".join(scenario["asserted_tools"]) or "无"
        statuses = " / ".join(
            "通过" if status == "passed" else status for status in scenario["round_statuses"]
        )
        cells = [
            index,
            scenario["name_zh"],
            scenario["category_zh"],
            scenario["scripted_model_zh"],
            tools,
            scenario["asserted_stop_reason"],
            statuses,
            scenario["grader_zh"],
        ]
        escaped = [str(cell).replace("|", "\\|").replace("\n", "<br>") for cell in cells]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.extend(
        [
            "",
            "## 四、与真实模型评测的分工",
            "",
            "| 层级 | 模型输出 | 主要定位 | 不能证明 |",
            "| --- | --- | --- | --- |",
            "| 确定性Runtime回归 | 测试预先设定 | 状态机、ToolResult、权限、上下文、Session与终止逻辑 | 真实模型会自主选择正确动作 |",
            "| 真实模型端到端 | 真实Provider生成 | 模型能否选择工具、处理失败并完成受控仓库任务 | 开放世界仓库的通用成功率 |",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: dict, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成20场景确定性Runtime回归报告")
    parser.add_argument("--rounds", type=int, default=3, help="回归轮数，默认3")
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    args = parser.parse_args(argv)
    if args.rounds <= 0:
        parser.error("--rounds必须为正整数")

    round_results = []
    for round_number in range(1, args.rounds + 1):
        print(f"[{round_number}/{args.rounds}] 正在运行20个确定性AgentOps场景……", flush=True)
        result = run_round(round_number)
        round_results.append(result)
        print(
            f"    收集{result['collected_count']}个，通过{result['passed_count']}个，"
            f"退出码={result['exit_code']}。",
            flush=True,
        )
    report = build_report(round_results)
    write_reports(report, args.json_report, args.markdown_report)
    metrics = report["metrics"]
    print(
        f"回归完成：{metrics['passed_executions']}/{metrics['total_executions']}通过；"
        f"中文报告：{args.markdown_report}"
    )
    return 0 if metrics["failed_executions"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
