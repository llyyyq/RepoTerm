from __future__ import annotations

from pathlib import Path

from benchmarks.export_agentops_traces import render_markdown as render_trace_markdown
from benchmarks.memory_lifecycle_trace import (
    CORRECT_MEMORY,
    STABLE_MEMORY,
    WRONG_MEMORY,
    render_markdown as render_memory_markdown,
    run_memory_lifecycle,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_EVIDENCE_PATHS = (
    "benchmarks/eval-methodology.md",
    "benchmarks/runtime_regression_results.md",
    "benchmarks/llm_e2e_results.md",
    "benchmarks/traces/README.md",
    "benchmarks/traces/normal-edit.md",
    "benchmarks/traces/tool-failure-recovery.md",
    "benchmarks/traces/permission-denial.md",
    "benchmarks/traces/session-resume.md",
)
PROJECT_README_HEADINGS = (
    "## Core Highlights",
    "## Quick Start",
    "## Architecture",
    "## Implementation Index",
    "## Evaluation",
    "## Runtime Flow",
    "## Trace",
    "## Failure Recovery",
    "## Reproduce",
)


def test_public_trace_markdown_contains_runtime_tools_permissions_and_graders():
    payload = {
        "title_zh": "权限拒绝与替代路径",
        "source_run_id": "permission-r1",
        "task_name_zh": "权限拒绝",
        "model": "test-model",
        "passed": True,
        "stop_reason": "done",
        "changed_paths": ["src/service.py"],
        "checkpoint_count": 1,
        "interrupted": False,
        "resumed": False,
        "session_reloaded": False,
        "independent_pytest": {"exit_code": 0},
        "prompt": "修改超时设置",
        "assistant_response": "已按反馈完成替代实现。",
        "runtime_events": [
            {
                "sequence": 1,
                "category": "phase",
                "phase": "explore",
                "message": "Runtime phase: explore",
            },
            {
                "sequence": 5,
                "category": "stop",
                "phase": "verify",
                "stop_reason": "done",
                "message": "verified",
            },
        ],
        "tool_events": [
            {
                "sequence": 2,
                "tool_name": "edit_file",
                "input": {"path": "src/settings.py"},
                "is_error": True,
                "output": "Edit denied",
            }
        ],
        "permission_decisions": [
            {
                "sequence": 3,
                "kind": "edit",
                "scope": "src/settings.py",
                "decision": "deny_with_feedback",
                "feedback_zh": "请修改service.py",
            }
        ],
        "graders": [
            {
                "name_zh": "权限恢复",
                "passed": True,
                "expected_zh": "按反馈恢复",
                "observed_zh": "service.py已修改",
            }
        ],
    }

    markdown = render_trace_markdown(payload)

    assert "Runtime/phase" in markdown
    assert "edit_file" in markdown
    assert "deny_with_feedback" in markdown
    assert "权限恢复" in markdown
    assert "不包含模型隐藏思维" in markdown


def test_memory_lifecycle_trace_updates_deletes_and_preserves_unrelated_memory(
    tmp_path: Path,
):
    report = run_memory_lifecycle(tmp_path / "artifacts")

    assert report["passed"] is True
    assert all(grader["passed"] for grader in report["graders"])
    serialized = str(report)
    assert WRONG_MEMORY in serialized
    assert CORRECT_MEMORY in serialized
    assert STABLE_MEMORY in serialized
    markdown = render_memory_markdown(report)
    assert "显式写入错误记忆" in markdown
    assert "检测新事实与旧记忆冲突" in markdown
    assert "删除冲突记忆" in markdown


def test_public_agentops_evidence_exists_and_is_linked_from_both_readmes():
    readmes = [
        (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8"),
        (REPOSITORY_ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
    ]

    for relative_path in PUBLIC_EVIDENCE_PATHS:
        assert (REPOSITORY_ROOT / relative_path).is_file(), relative_path
        link = f"./{relative_path}"
        assert all(link in readme for readme in readmes), link

    for heading in PROJECT_README_HEADINGS:
        assert all(heading in readme for readme in readmes), heading
