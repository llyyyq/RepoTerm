"""Reviewed, checkpointed deletion of one UTF-8 workspace file."""

from __future__ import annotations

import stat
from pathlib import Path

from repoterm.safety.file_review import build_unified_diff
from repoterm.safety.workspace import resolve_tool_path
from repoterm.session import create_file_checkpoint
from repoterm.tools.registry import ToolContext, ToolDefinition, ToolResult


MAX_DELETE_FILE_BYTES = 1024 * 1024
MAX_DIFF_PREVIEW_CHARS = 12_000


def _validate(input_data: dict) -> dict:
    path = input_data.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required")
    if "recursive" in input_data:
        raise ValueError("delete_file cannot delete directories recursively")
    return {"path": path.strip()}


def _run(input_data: dict, context: ToolContext) -> ToolResult:
    path = input_data["path"]
    root = Path(context.cwd).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        return ToolResult(ok=False, output="delete_file requires a relative workspace path")
    if context.session is None or context.permissions is None:
        return ToolResult(ok=False, output="delete_file requires an active session and approval")

    try:
        lexical_target = root / candidate
        if lexical_target.is_symlink():
            return ToolResult(ok=False, output="delete_file cannot delete a symlink")
        target = resolve_tool_path(context, path, "delete")
        if not target.is_relative_to(root):
            return ToolResult(ok=False, output="delete_file cannot access a path outside the workspace")
        before_stat = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(before_stat.st_mode):
            return ToolResult(ok=False, output="delete_file only accepts a regular file")
        if before_stat.st_size > MAX_DELETE_FILE_BYTES:
            return ToolResult(ok=False, output="File is too large for a durable deletion checkpoint")
        previous_content = target.read_bytes().decode("utf-8")
        diff = build_unified_diff(path, previous_content, "")
        if len(diff) > MAX_DIFF_PREVIEW_CHARS:
            diff = diff[:MAX_DIFF_PREVIEW_CHARS] + "\n... deletion preview truncated"
        context.permissions.ensure_delete(str(target), diff)
        after_stat = target.stat(follow_symlinks=False)
        before_identity = (
            before_stat.st_dev, before_stat.st_ino,
            before_stat.st_size, before_stat.st_mtime_ns,
        )
        after_identity = (
            after_stat.st_dev, after_stat.st_ino,
            after_stat.st_size, after_stat.st_mtime_ns,
        )
        if before_identity != after_identity or not stat.S_ISREG(after_stat.st_mode):
            return ToolResult(ok=False, output="File changed during approval; deletion cancelled")
        checkpoint = create_file_checkpoint(
            context.session,
            file_path=str(target),
            existed=True,
            previous_content=previous_content,
            kind="delete",
        )
        target.unlink()
        return ToolResult(
            ok=True,
            output=f"Deleted file: {path} (checkpoint {checkpoint.checkpoint_id}; rewind can restore it)",
        )
    except (OSError, UnicodeError, PermissionError, RuntimeError, ValueError) as error:
        return ToolResult(ok=False, output=f"Delete failed: {error}")


delete_file_tool = ToolDefinition(
    name="delete_file",
    description=(
        "Delete one UTF-8 regular file inside the workspace after explicit review. "
        "Requires an active session and creates a rewind checkpoint. "
        "Never deletes directories, symlinks, or files outside the workspace."
    ),
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path of one file"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    validator=_validate,
    run=_run,
)
