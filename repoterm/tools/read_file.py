from __future__ import annotations

import time
from pathlib import Path

from repoterm.tools.registry import ToolDefinition, ToolResult
from repoterm.safety.workspace import resolve_tool_path

DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 20000

# 文件内容缓存，避免重复读取同一文件
# 缓存键：(文件路径，修改时间) -> (内容, 缓存时间)
_file_cache: dict[tuple[str, float], tuple[str, float]] = {}
_FILE_CACHE_TTL = 2.0  # 缓存有效期 2 秒


def _get_cached_file_content(target: Path) -> str:
    """获取文件内容，使用缓存避免重复读取"""
    try:
        stat = target.stat()
        mtime = stat.st_mtime
        cache_key = (str(target), mtime)
        
        if cache_key in _file_cache:
            content, cache_time = _file_cache[cache_key]
            # 检查是否过期
            now = time.monotonic()
            if now - cache_time <= _FILE_CACHE_TTL:
                return content
        
        # 清理过期缓存
        now = time.monotonic()
        expired_keys = [k for k, (c, t) in _file_cache.items() if now - t > _FILE_CACHE_TTL]
        for k in expired_keys:
            del _file_cache[k]
        
        # 读取并缓存
        content = target.read_text(encoding="utf-8")
        _file_cache[cache_key] = (content, time.monotonic())
        return content
    except OSError:
        return ""


def _validate(input_data: dict) -> dict:
    path = input_data.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    offset = int(input_data.get("offset", 0))
    limit = int(input_data.get("limit", DEFAULT_READ_LIMIT))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 1 or limit > MAX_READ_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READ_LIMIT}")
    start_line = input_data.get("start_line")
    line_count = input_data.get("line_count", 80)
    if start_line is not None:
        start_line = int(start_line)
        line_count = int(line_count)
        if start_line < 1 or not 1 <= line_count <= 400:
            raise ValueError("start_line must be >= 1 and line_count must be between 1 and 400")
        if "offset" in input_data or "limit" in input_data:
            raise ValueError("line mode cannot be combined with character offset/limit")
    return {"path": path, "offset": offset, "limit": limit, "start_line": start_line, "line_count": line_count}


def _run(input_data: dict, context) -> ToolResult:
    target = resolve_tool_path(context, input_data["path"], "read")

    try:
        # 使用缓存读取
        content = _get_cached_file_content(target)
    except UnicodeDecodeError:
        return ToolResult(
            ok=False,
            output=f"File {input_data['path']} appears to be binary. Cannot read as text.",
        )
    
    start_line = input_data.get("start_line")
    if start_line is not None:
        lines = content.splitlines(keepends=True)
        start = start_line - 1
        if start >= len(lines):
            return ToolResult(
                ok=False,
                output=f"start_line {start_line} is past EOF ({len(lines)} lines) in {input_data['path']}",
            )
        end_line = min(len(lines), start + input_data["line_count"])
        numbered = "".join(f"{number}: {line}" for number, line in enumerate(lines[start:end_line], start=start_line))
        output_truncated = len(numbered) > MAX_READ_LIMIT
        header = (
            f"FILE: {input_data['path']}\nSTART_LINE: {start_line}\n"
            f"END_LINE: {end_line}\nTOTAL_LINES: {len(lines)}\n"
            f"TRUNCATED: {'yes - call read_file again with start_line ' + str(end_line + 1) if end_line < len(lines) else 'no'}\n"
            f"OUTPUT_TRUNCATED: {'yes - request fewer lines or use character mode' if output_truncated else 'no'}\n\n"
        )
        return ToolResult(ok=True, output=header + numbered[:MAX_READ_LIMIT])

    offset = input_data["offset"]
    limit = input_data["limit"]
    end = min(len(content), offset + limit)
    chunk = content[offset:end]
    truncated = end < len(content)
    header = "\n".join(
        [
            f"FILE: {input_data['path']}",
            f"OFFSET: {offset}",
            f"END: {end}",
            f"TOTAL_CHARS: {len(content)}",
            f"TRUNCATED: {'yes - call read_file again with offset ' + str(end) if truncated else 'no'}",
            "",
        ]
    )
    return ToolResult(ok=True, output=header + chunk)


read_file_tool = ToolDefinition(
    name="read_file",
    description="Read a UTF-8 workspace file. Use start_line and line_count for numbered source lines (for example a line number from grep or a traceback). Legacy offset and limit count characters, not lines. Do not combine line and character parameters.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "description": "Zero-based character offset, not a line number."}, "limit": {"type": "integer", "description": "Maximum characters in character mode."}, "start_line": {"type": "integer", "description": "One-based source line number."}, "line_count": {"type": "integer", "description": "Number of source lines, at most 400."}}, "required": ["path"]},
    validator=_validate,
    run=_run,
)
