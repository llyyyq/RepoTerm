"""Bounded, read-only Python symbol map for repository localization."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from repoterm.safety.workspace import resolve_tool_path
from repoterm.tools.grep_files import SKIP_DIRS
from repoterm.tools.registry import ToolDefinition, ToolResult

MAX_FILES = 1500
MAX_BYTES = 300_000
MAX_RESULTS = 8


def _validate(data: dict) -> dict:
    query = data.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must be a non-empty string of at most 500 characters")
    path = data.get("path", ".")
    if not isinstance(path, str) or not path or len(path) > 500:
        raise ValueError("path must be a non-empty string")
    return {"query": query.strip(), "path": path}


def _python_files(root: Path):
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(directory) / name
                count += 1
                if count >= MAX_FILES:
                    return


def _run(data: dict, context) -> ToolResult:
    root = resolve_tool_path(context, data["path"], "analyze")
    if not root.exists():
        return ToolResult(ok=False, output="Path does not exist")
    workspace = Path(context.cwd).resolve()
    terms = set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*", data["query"].lower()))
    terms -= {"file", "line", "error", "test", "the", "in", "at", "py"}
    hinted_paths = {p.replace("\\", "/").lower() for p in re.findall(r"[\w./\\-]+\.py", data["query"])}
    ranked = []
    scanned = 0
    for path in _python_files(root):
        scanned += 1
        try:
            path.resolve().relative_to(workspace)
            if path.stat().st_size > MAX_BYTES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(workspace).as_posix()
        except (OSError, ValueError, UnicodeError, SyntaxError):
            continue
        path_l = relative.lower()
        path_score = 12 if any(path_l.endswith(hint) for hint in hinted_paths) else 0
        path_score += sum(2 for term in terms if term in path_l)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        graph_terms = {part.lower() for name in imports for part in name.split(".")}
        edge_score = sum(1 for term in terms if term in graph_terms)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name.lower()
            exact = any(name == term for term in terms)
            calls = {
                child.func.id if isinstance(child.func, ast.Name) else child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, (ast.Name, ast.Attribute))
            }
            call_score = sum(2 for call in calls if call.lower() in terms)
            score = path_score + edge_score + call_score + (15 if exact else 0)
            score += sum(3 for term in terms if len(term) >= 3 and term in name)
            if score:
                relevant_imports = [item for item in sorted(imports) if any(term in item.lower() for term in terms)]
                relevant_calls = [item for item in sorted(calls) if item.lower() in terms]
                ranked.append((score, relative, node.lineno, node.name, node.end_lineno or node.lineno, relevant_imports[:3], relevant_calls[:3]))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    if not ranked:
        return ToolResult(ok=True, output=f"Scanned {scanned} Python files; no candidate symbols. Try a more specific path or symbol.")
    lines = [f"Python repository map ({scanned} files scanned; top {MAX_RESULTS} candidates):"]
    for score, path, start, name, end, imports, calls in ranked[:MAX_RESULTS]:
        lines.append(f"{path}:{start}-{end} {name} [rank={score}; linked imports={','.join(imports) or '-'}; calls={','.join(calls) or '-'}]")
    lines.append("Rank is a navigation hint, not proof of a defect; inspect candidate code and verify.")
    return ToolResult(ok=True, output="\n".join(lines))


repository_map_tool = ToolDefinition(
    name="repository_map",
    description="Locate likely Python files and symbols from an error, traceback, path, or function name. Returns a bounded AST/import map; use before broad repeated grep, then inspect and verify candidates.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Error text, traceback frame, or symbol to locate"},
            "path": {"type": "string", "description": "Optional workspace subdirectory (default: .)"},
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
)
