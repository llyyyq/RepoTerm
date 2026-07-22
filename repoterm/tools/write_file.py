from __future__ import annotations

from repoterm.file_review import apply_reviewed_file_change
from repoterm.tooling import ToolDefinition
from repoterm.workspace import resolve_tool_path

# 验证输入是否符合要求
def _validate(input_data: dict) -> dict:
    # 读取路径
    path = input_data.get("path")
    # 读取内容
    content = input_data.get("content")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    return {"path": path, "content": content}

# 执行写入文件操作
def _run(input_data: dict, context):
    target = resolve_tool_path(context, input_data["path"], "write")
    return apply_reviewed_file_change(context, input_data["path"], target, input_data["content"])


write_file_tool = ToolDefinition(
    name="write_file",
    description="Write a UTF-8 text file relative to the workspace root.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    validator=_validate,
    run=_run,
)

