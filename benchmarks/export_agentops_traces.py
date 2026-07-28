"""Export small, redacted, GitHub-friendly traces from the live E2E report."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / "benchmarks" / "llm_e2e_results.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "benchmarks" / "traces"

TARGETS = (
    ("normal-edit", "code_modification", "正常修改"),
    ("tool-failure-recovery", "test_failure_recovery", "工具失败与修复恢复"),
    ("permission-denial", "permission_denial", "权限拒绝与替代路径"),
    ("session-resume", "session_resume", "中断后的Session恢复"),
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_project_identity(value: Any) -> Any:
    """Rewrite legacy product labels in historical run artifacts."""
    if isinstance(value, dict):
        return {
            key: _normalize_project_identity(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_project_identity(item) for item in value]
    if isinstance(value, str):
        return (
            value.replace("MiniCode", "RepoTerm")
            .replace("minicode", "repoterm")
            .replace("mini-code", "repoterm")
        )
    return value


def _compact(value: Any, limit: int = 220) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value or "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _escape_cell(value: Any) -> str:
    return _compact(value).replace("|", "\\|") or "-"


def _select_run(report: dict[str, Any], task_id: str) -> tuple[dict, dict]:
    candidates = [run for run in report.get("runs", []) if run.get("task_id") == task_id]
    if not candidates:
        raise RuntimeError(f"正式报告中没有任务：{task_id}")

    loaded: list[tuple[dict, dict]] = []
    for run in candidates:
        trace_path = REPO_ROOT / str(run["trace_path"])
        if trace_path.exists():
            loaded.append((run, _load_json(trace_path)))
    if not loaded:
        raise RuntimeError(f"任务{task_id}的本地Trace均不存在")

    if task_id == "permission_denial":
        for pair in loaded:
            if any(
                event.get("category") == "phase" and event.get("phase") == "verify"
                for event in pair[1].get("runtime_events", [])
            ):
                return pair
    return min(loaded, key=lambda pair: int(pair[0].get("tool_call_count", 0)))


def _public_payload(trace: dict[str, Any], title_zh: str) -> dict[str, Any]:
    from repoterm.evidence_safety import redact_sensitive_payload

    payload = {
        "schema_version": 1,
        "title_zh": title_zh,
        "source_run_id": trace.get("run_id"),
        "task_id": trace.get("task_id"),
        "task_name_zh": trace.get("task_name_zh"),
        "model": trace.get("model"),
        "passed": trace.get("passed"),
        "stop_reason": trace.get("stop_reason"),
        "prompt": trace.get("prompt"),
        "assistant_response": trace.get("assistant_response"),
        "changed_paths": trace.get("changed_paths", []),
        "checkpoint_count": trace.get("checkpoint_count", 0),
        "interrupted": trace.get("interrupted", False),
        "resumed": trace.get("resumed", False),
        "session_reloaded": trace.get("session_reloaded", False),
        "independent_pytest": trace.get("independent_pytest"),
        "runtime_events": deepcopy(trace.get("runtime_events", [])),
        "tool_events": deepcopy(trace.get("tool_events", [])),
        "permission_decisions": deepcopy(trace.get("permission_decisions", [])),
        "graders": deepcopy(trace.get("graders", [])),
        "说明": "仅导出可观察事件，不包含模型隐藏思维；敏感字段和本地绝对路径已脱敏。",
    }
    return _normalize_project_identity(redact_sensitive_payload(payload))


def _timeline(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in payload.get("runtime_events", []):
        category = event.get("category", "runtime")
        detail = event.get("message", "")
        if category == "stop":
            detail = f"stop_reason={event.get('stop_reason')}; {detail}"
        events.append(
            {
                "sequence": int(event.get("sequence", 0)),
                "type": f"Runtime/{category}",
                "action": event.get("phase") or event.get("phase_label") or "-",
                "result": detail,
            }
        )
    for event in payload.get("tool_events", []):
        events.append(
            {
                "sequence": int(event.get("sequence", 0)),
                "type": "Tool",
                "action": f"{event.get('tool_name')}({_compact(event.get('input', {}), 120)})",
                "result": ("失败：" if event.get("is_error") else "成功：")
                + _compact(event.get("output", ""), 260),
            }
        )
    for event in payload.get("permission_decisions", []):
        events.append(
            {
                "sequence": int(event.get("sequence", 0)),
                "type": "Permission",
                "action": f"{event.get('kind')}:{event.get('scope')}",
                "result": f"{event.get('decision')} {_compact(event.get('feedback_zh', ''), 180)}",
            }
        )
    return sorted(events, key=lambda item: (item["sequence"], item["type"]))


def render_markdown(payload: dict[str, Any]) -> str:
    pytest_result = payload.get("independent_pytest") or {}
    lines = [
        f"# Trace：{payload['title_zh']}",
        "",
        "> 这是从真实模型端到端评测中导出的脱敏证据，只包含可观察消息、工具、权限、Runtime事件和Grader，不包含模型隐藏思维。",
        "",
        "## 结果摘要",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| Run ID | `{payload.get('source_run_id', '-')}` |",
        f"| 任务 | {payload.get('task_name_zh', '-')} |",
        f"| 模型 | `{payload.get('model', '-')}` |",
        f"| 任务结果 | {'通过' if payload.get('passed') else '失败'} |",
        f"| 停止原因 | `{payload.get('stop_reason', '-')}` |",
        f"| 修改路径 | `{', '.join(payload.get('changed_paths', [])) or '无'}` |",
        f"| Checkpoint | {payload.get('checkpoint_count', 0)} |",
        f"| 中断/恢复/重载 | {payload.get('interrupted', False)} / {payload.get('resumed', False)} / {payload.get('session_reloaded', False)} |",
        f"| 独立Pytest退出码 | {pytest_result.get('exit_code', '不适用')} |",
        "",
        "## 用户任务",
        "",
        payload.get("prompt", "-"),
        "",
        "## 可观察时间线",
        "",
        "| Seq | 类型 | 动作 | 结果/证据 |",
        "| ---: | --- | --- | --- |",
    ]
    for event in _timeline(payload):
        lines.append(
            f"| {event['sequence']} | {_escape_cell(event['type'])} | "
            f"{_escape_cell(event['action'])} | {_escape_cell(event['result'])} |"
        )
    lines.extend(
        [
            "",
            "## Grader",
            "",
            "| 判分项 | 结果 | 期望 | 实际 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for grader in payload.get("graders", []):
        lines.append(
            f"| {_escape_cell(grader.get('name_zh'))} | "
            f"{'通过' if grader.get('passed') else '失败'} | "
            f"{_escape_cell(grader.get('expected_zh'))} | "
            f"{_escape_cell(grader.get('observed_zh'))} |"
        )
    lines.extend(
        [
            "",
            "## 最终回答",
            "",
            payload.get("assistant_response", "-"),
            "",
        ]
    )
    return "\n".join(lines)


def export_traces(report_path: Path, output_dir: Path) -> list[dict[str, Any]]:
    report = _load_json(report_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for slug, task_id, title_zh in TARGETS:
        run, trace = _select_run(report, task_id)
        payload = _public_payload(trace, title_zh)
        json_path = output_dir / f"{slug}.json"
        markdown_path = output_dir / f"{slug}.md"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(render_markdown(payload) + "\n", encoding="utf-8")
        exported.append(
            {
                "slug": slug,
                "title_zh": title_zh,
                "task_id": task_id,
                "run_id": run.get("run_id"),
                "markdown": markdown_path.name,
                "json": json_path.name,
            }
        )

    index_lines = [
        "# AgentOps 精选 Trace",
        "",
        "这些Trace来自真实Provider端到端评测，已删除完整消息历史和隐藏思维，仅保留公开面试所需的运行证据。",
        "",
        "| 场景 | 任务 | Markdown | JSON |",
        "| --- | --- | --- | --- |",
    ]
    for item in exported:
        index_lines.append(
            f"| {item['title_zh']} | `{item['task_id']}` | "
            f"[{item['markdown']}](./{item['markdown']}) | "
            f"[{item['json']}](./{item['json']}) |"
        )
    index_lines.extend(
        [
            "",
            f"导出时间：{datetime.now(timezone.utc).isoformat()}",
            "",
        ]
    )
    (output_dir / "README.md").write_text("\n".join(index_lines), encoding="utf-8")
    return exported


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出可公开的AgentOps精选Trace")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    exported = export_traces(args.report, args.output_dir)
    for item in exported:
        print(f"已导出：{item['title_zh']} -> {args.output_dir / item['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
