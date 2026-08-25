"""核心 Package 中文架构文档、导航和链接契约。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

DOCUMENTS = (
    "repoterm/app/README.zh-CN.md",
    "repoterm/contracts/README.zh-CN.md",
    "repoterm/runtime/README.zh-CN.md",
    "repoterm/runtime/planning/README.zh-CN.md",
    "repoterm/runtime/control/README.zh-CN.md",
    "repoterm/providers/README.zh-CN.md",
    "repoterm/context/README.zh-CN.md",
    "repoterm/safety/README.zh-CN.md",
    "repoterm/session/README.zh-CN.md",
    "repoterm/memory/README.zh-CN.md",
    "repoterm/observability/README.zh-CN.md",
    "repoterm/tools/README.zh-CN.md",
    "repoterm/integrations/README.zh-CN.md",
    "repoterm/ui/README.zh-CN.md",
    "repoterm/ui/tui/README.zh-CN.md",
    "benchmarks/evaluation/README.zh-CN.md",
)

NAVIGATION_FILES = (ROOT / "README.md", ROOT / "Docs/Documentation/engineering/package-structure-refactor-report.md")
BANNED_PHRASES = ("面试官", "简历要点", "面试演示", "八股", "吹牛")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _local_link_targets(path: Path, text: str) -> list[Path]:
    targets: list[Path] = []
    for raw_target in LINK_RE.findall(text):
        target = raw_target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        targets.append((path.parent / target).resolve())
    return targets


def test_all_package_architecture_documents_exist_and_have_required_sections() -> None:
    required_concepts = (
        ("design", ("设计目标", "设计边界", "非目标")),
        ("architecture", ("架构", "模块关系", "依赖方向")),
        ("flow", ("运行流程", "调用流程", "数据流", "生命周期", "流程")),
        ("failure", ("失败处理", "失败与", "已知边界", "安全边界")),
        ("verification", ("测试与", "测试", "验证", "评测")),
        ("maintenance", ("阅读与维护", "维护准则", "阅读代码")),
    )
    for relative_path in DOCUMENTS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        text = path.read_text(encoding="utf-8")
        assert len(re.findall(r"^```mermaid$", text, re.MULTILINE)) <= 2, relative_path
        for concept, markers in required_concepts:
            assert any(marker in text for marker in markers), (relative_path, concept)
        for phrase in BANNED_PHRASES:
            assert phrase not in text, (relative_path, phrase)


def test_readme_and_refactor_report_index_all_package_documents() -> None:
    readme = _read("README.md")
    report = _read("Docs/Documentation/engineering/package-structure-refactor-report.md")
    assert "模块设计文档" in readme
    assert "模块设计文档索引" in report
    assert "| Package | 核心责任 | 设计文档 |" in report
    for relative_path in DOCUMENTS:
        assert relative_path in readme, relative_path
        assert relative_path in report, relative_path


def test_architecture_docs_name_current_public_objects_and_cross_package_edges() -> None:
    contracts_doc = _read("repoterm/contracts/README.zh-CN.md")
    contracts_source = _read("repoterm/contracts/state.py")
    for symbol in ("AppState", "Store", "create_app_store"):
        assert symbol in contracts_source
        assert symbol in contracts_doc
    assert "`RuntimeState`" not in contracts_doc
    assert "`StateStore`" not in contracts_doc

    planning_doc = _read("repoterm/runtime/planning/README.zh-CN.md")
    planning_sources = "\n".join(
        _read(relative_path)
        for relative_path in (
            "repoterm/runtime/planning/router.py",
            "repoterm/runtime/planning/smart_router.py",
            "repoterm/runtime/planning/task_graph.py",
        )
    )
    for marker in (
        "repoterm.providers",
        "ModelSwitcher",
        "WorktreeIsolator",
        "git worktree",
    ):
        assert marker in planning_sources
    for marker in ("Provider", "ModelSwitcher", "WorktreeIsolator", "git worktree"):
        assert marker in planning_doc
    assert "不依赖 Provider" not in planning_doc

    control_doc = _read("repoterm/runtime/control/README.zh-CN.md")
    control_source = _read("repoterm/runtime/control/cybernetic_orchestrator.py")
    for marker in ("repoterm.providers", "ModelSwitcher", "MemoryService"):
        assert marker in control_source
    for marker in ("Provider", "ModelSwitcher", "MemoryService"):
        assert marker in control_doc
    assert "不应依赖 App、UI、Benchmark 或具体 Provider" not in control_doc

    memory_doc = _read("repoterm/memory/README.zh-CN.md")
    assert "`legacy_user_profile.py`" in memory_doc
    assert "`user_profile.py`" not in memory_doc


def test_package_document_local_links_resolve() -> None:
    files = [ROOT / relative_path for relative_path in DOCUMENTS]
    files.extend(NAVIGATION_FILES)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for target in _local_link_targets(path, text):
            assert target.is_file() or target.is_dir(), (path, target)


def test_document_scope_is_markdown_only() -> None:
    for relative_path in DOCUMENTS:
        assert relative_path.endswith("README.zh-CN.md")
