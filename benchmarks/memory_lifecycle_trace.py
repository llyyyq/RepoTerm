"""Generate a deterministic public trace for memory conflict/update/delete lifecycle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_REPORT = REPO_ROOT / "benchmarks" / "traces" / "memory-conflict-update-delete.json"
DEFAULT_MARKDOWN_REPORT = REPO_ROOT / "benchmarks" / "traces" / "memory-conflict-update-delete.md"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / ".temp" / "memory_lifecycle"

WRONG_MEMORY = "项目测试命令使用 npm test，禁止运行 pytest。"
CORRECT_MEMORY = "项目测试命令使用 pytest -q，不使用 npm test。"
STABLE_MEMORY = "Python代码格式化统一使用 ruff format。"
QUERY = "项目测试命令"


def _entry_view(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "scope": entry.scope.value,
        "category": entry.category,
        "content": entry.content,
        "tier": entry.tier.value,
        "usage_count": entry.usage_count,
    }


def run_memory_lifecycle(artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR) -> dict[str, Any]:
    import repoterm.memory as memory_module
    from repoterm.memory import MemoryManager, MemoryScope

    run_id = f"memory-lifecycle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    run_root = artifacts_dir / run_id
    workspace = run_root / "workspace"
    isolated_user_root = run_root / "user-state"
    workspace.mkdir(parents=True, exist_ok=True)
    isolated_user_root.mkdir(parents=True, exist_ok=True)

    old_repoterm_dir = memory_module.REPOTERM_DIR
    memory_module.REPOTERM_DIR = isolated_user_root
    events: list[dict[str, Any]] = []
    sequence = 0

    def record(action_zh: str, input_value: Any, output_value: Any) -> None:
        nonlocal sequence
        sequence += 1
        events.append(
            {
                "sequence": sequence,
                "action_zh": action_zh,
                "input": input_value,
                "output": output_value,
            }
        )

    try:
        manager = MemoryManager(workspace=workspace)
        write_result = manager.handle_user_memory_input(
            f"/memory add project: {WRONG_MEMORY}"
        )
        wrong_entry = manager.memories[MemoryScope.PROJECT].entries[-1]
        initial_write_ok = bool(write_result) and wrong_entry.content == WRONG_MEMORY
        stable_entry = manager.add_entry(
            MemoryScope.PROJECT,
            category="convention",
            content=STABLE_MEMORY,
            tags=["python", "format"],
        )
        record(
            "显式写入错误记忆",
            {"command": "/memory add project: <错误测试命令>"},
            {"message": write_result, "entry": _entry_view(wrong_entry)},
        )
        record(
            "写入无关稳定记忆作为删除边界",
            {"content": STABLE_MEMORY},
            {"entry": _entry_view(stable_entry)},
        )

        before_entries = manager.search(
            QUERY,
            scope=MemoryScope.PROJECT,
            limit=5,
            min_relevance=0.0,
        )
        before_context = manager.get_relevant_context(query=QUERY, max_tokens=1000)
        record(
            "检索并注入错误记忆",
            {"query": QUERY, "token_budget": 1000},
            {
                "retrieved": [_entry_view(entry) for entry in before_entries],
                "prompt_context": before_context,
            },
        )

        conflicts = manager.detect_conflicts(
            CORRECT_MEMORY,
            scope=MemoryScope.PROJECT,
            threshold=0.15,
        )
        record(
            "检测新事实与旧记忆冲突",
            {"candidate": CORRECT_MEMORY, "threshold": 0.15},
            {
                "conflicts": [
                    {"entry": _entry_view(entry), "similarity": round(score, 4)}
                    for entry, score in conflicts
                ]
            },
        )

        updated = manager.update_entry(
            MemoryScope.PROJECT,
            wrong_entry.id,
            CORRECT_MEMORY,
        )
        reloaded_after_update = MemoryManager(workspace=workspace)
        updated_entries = reloaded_after_update.search(
            QUERY,
            scope=MemoryScope.PROJECT,
            limit=5,
            min_relevance=0.0,
        )
        updated_context = reloaded_after_update.get_relevant_context(
            query=QUERY,
            max_tokens=1000,
        )
        record(
            "更新错误记忆并重新加载",
            {"entry_id": wrong_entry.id, "replacement": CORRECT_MEMORY},
            {
                "updated": updated,
                "retrieved_after_reload": [
                    _entry_view(entry) for entry in updated_entries
                ],
                "prompt_context": updated_context,
            },
        )

        deleted = reloaded_after_update.delete_entry(
            MemoryScope.PROJECT,
            wrong_entry.id,
        )
        reloaded_after_delete = MemoryManager(workspace=workspace)
        final_entries = reloaded_after_delete.search(
            QUERY,
            scope=MemoryScope.PROJECT,
            limit=5,
            min_relevance=0.0,
        )
        final_context = reloaded_after_delete.get_relevant_context(
            query=QUERY,
            max_tokens=1000,
        )
        all_final_contents = [
            entry.content
            for entry in reloaded_after_delete.memories[MemoryScope.PROJECT].entries
        ]
        record(
            "删除冲突记忆并再次重新加载",
            {"entry_id": wrong_entry.id},
            {
                "deleted": deleted,
                "retrieved_after_delete": [
                    _entry_view(entry) for entry in final_entries
                ],
                "prompt_context": final_context,
                "remaining_project_memories": all_final_contents,
            },
        )

        conflict_ids = {entry.id for entry, _score in conflicts}
        graders = [
            {
                "name_zh": "错误记忆由显式用户指令写入",
                "passed": initial_write_ok,
                "evidence_zh": str(write_result),
            },
            {
                "name_zh": "写入后检索和Prompt注入命中错误记忆",
                "passed": WRONG_MEMORY in before_context
                and any(entry.id == wrong_entry.id for entry in before_entries),
                "evidence_zh": before_context,
            },
            {
                "name_zh": "新事实触发旧记忆冲突检测",
                "passed": wrong_entry.id in conflict_ids,
                "evidence_zh": f"conflict_ids={sorted(conflict_ids)}",
            },
            {
                "name_zh": "更新结果持久化并替换Prompt内容",
                "passed": updated
                and CORRECT_MEMORY in updated_context
                and WRONG_MEMORY not in updated_context,
                "evidence_zh": updated_context,
            },
            {
                "name_zh": "删除后冲突记忆不再检索或注入",
                "passed": deleted
                and WRONG_MEMORY not in final_context
                and CORRECT_MEMORY not in final_context
                and all(entry.id != wrong_entry.id for entry in final_entries),
                "evidence_zh": final_context or "最终Prompt上下文为空",
            },
            {
                "name_zh": "删除仅影响目标记忆",
                "passed": STABLE_MEMORY in all_final_contents,
                "evidence_zh": f"remaining={all_final_contents}",
            },
        ]
        return {
            "schema_version": 1,
            "report_type": "错误记忆更新与删除Trace",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "provider_called": False,
            "scope": "project",
            "query": QUERY,
            "passed": all(grader["passed"] for grader in graders),
            "events": events,
            "graders": graders,
            "lifecycle_boundary_zh": (
                "本样例验证显式写入、检索注入、冲突检测、更新、删除和重新加载；"
                "时间衰减与分层归档由独立单元测试覆盖，不把它们混入本Trace。"
            ),
        }
    finally:
        memory_module.REPOTERM_DIR = old_repoterm_dir


def _compact(value: Any, limit: int = 260) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(value: Any) -> str:
    return _compact(value).replace("|", "\\|") or "-"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Trace：错误记忆的更新与删除",
        "",
        "该场景不调用LLM，使用真实MemoryManager验证一条错误项目记忆从写入、命中、冲突、更新到删除的完整生命周期。",
        "",
        "## 结果摘要",
        "",
        f"- Run ID：`{report['run_id']}`",
        f"- Scope：`{report['scope']}`",
        f"- 查询：`{report['query']}`",
        f"- 是否调用Provider：{'是' if report['provider_called'] else '否'}",
        f"- 最终结果：{'通过' if report['passed'] else '失败'}",
        "",
        "## 生命周期Trace",
        "",
        "| Seq | 动作 | 输入 | 输出/证据 |",
        "| ---: | --- | --- | --- |",
    ]
    for event in report["events"]:
        lines.append(
            f"| {event['sequence']} | {_escape(event['action_zh'])} | "
            f"{_escape(event['input'])} | {_escape(event['output'])} |"
        )
    lines.extend(
        [
            "",
            "## Grader",
            "",
            "| 判分项 | 结果 | 证据 |",
            "| --- | --- | --- |",
        ]
    )
    for grader in report["graders"]:
        lines.append(
            f"| {_escape(grader['name_zh'])} | "
            f"{'通过' if grader['passed'] else '失败'} | "
            f"{_escape(grader['evidence_zh'])} |"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            report["lifecycle_boundary_zh"],
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    from repoterm.evidence_safety import redact_sensitive_payload

    safe_report = redact_sensitive_payload(report)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(safe_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(safe_report) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成错误记忆更新/删除公开Trace")
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    args = parser.parse_args(argv)
    report = run_memory_lifecycle(args.artifacts_dir)
    write_reports(report, args.json_report, args.markdown_report)
    print(
        f"记忆生命周期Trace{'通过' if report['passed'] else '失败'}；"
        f"中文报告：{args.markdown_report}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
