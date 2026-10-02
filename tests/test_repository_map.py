from repoterm.tools.registry import ToolContext, ToolRegistry
from repoterm.tools.repository_map import repository_map_tool


def test_repository_map_ranks_traceback_symbol_and_is_read_only(tmp_path):
    package = tmp_path / "fastapi"
    package.mkdir()
    (package / "routing.py").write_text(
        "from fastapi import responses\n\n"
        "def unrelated():\n    pass\n\n"
        "def serialize_response(value):\n    return value\n", encoding="utf-8",
    )
    registry = ToolRegistry([repository_map_tool])
    result = registry.execute(
        "repository_map", {"query": "fastapi/routing.py:6 serialize_response"},
        ToolContext(cwd=str(tmp_path)),
    )
    assert result.ok
    assert "fastapi/routing.py:6-7 serialize_response" in result.output
    assert result.output.index("serialize_response") < result.output.index("unrelated")
    assert repository_map_tool.is_read_only


def test_repository_map_rejects_workspace_escape(tmp_path):
    result = ToolRegistry([repository_map_tool]).execute(
        "repository_map", {"query": "secret", "path": "../"},
        ToolContext(cwd=str(tmp_path)),
    )
    assert not result.ok
