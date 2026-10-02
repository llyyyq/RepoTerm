"""The default deletion capability is single-file, approved, and rewindable."""

from pathlib import Path

import pytest

import repoterm.safety.permissions as permissions_module
import repoterm.session.service as session_module
from repoterm.safety.permissions import PermissionManager
from repoterm.session import create_new_session, load_session, rewind_session_data
from repoterm.runtime.turn_kernel import classify_tool_result
from repoterm.tools import create_default_tool_registry
from repoterm.tools.delete_file import delete_file_tool
from repoterm.tools.registry import ToolContext


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(session_module, "REPOTERM_DIR", runtime_root)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(
        permissions_module, "REPOTERM_PERMISSIONS_PATH", runtime_root / "permissions.json"
    )
    permissions_module._normalize_path_cached.cache_clear()
    yield
    permissions_module._normalize_path_cached.cache_clear()


def test_delete_file_requires_fresh_review_and_rewinds_exact_bytes(
    tmp_path: Path, isolated_storage: None,
) -> None:
    target = tmp_path / "scratch.py"
    original = b"first\r\nsecond\r\n"
    target.write_bytes(original)
    requests = []
    permissions = PermissionManager(
        str(tmp_path), prompt=lambda request: requests.append(request) or {"decision": "allow_once"}
    )
    session = create_new_session(str(tmp_path))

    result = delete_file_tool.run(
        {"path": "scratch.py"},
        ToolContext(cwd=str(tmp_path), permissions=permissions, session=session),
    )

    assert result.ok is True
    assert not target.exists()
    assert len(session.checkpoints) == 1
    assert session.checkpoints[0].kind == "delete"
    assert load_session(session.session_id).checkpoints[0].kind == "delete"
    assert "DELETE" in requests[0]["summary"]
    assert "-first" in "\n".join(requests[0]["details"])
    assert {choice["decision"] for choice in requests[0]["choices"]} == {
        "allow_once", "deny_once", "deny_with_feedback",
    }

    rewind_session_data(session)
    assert target.read_bytes() == original


def test_delete_denial_preserves_file_and_does_not_reuse_edit_grant(
    tmp_path: Path, isolated_storage: None,
) -> None:
    target = tmp_path / "scratch.py"
    target.write_text("keep", encoding="utf-8")
    requests = []
    permissions = PermissionManager(
        str(tmp_path), prompt=lambda request: requests.append(request) or {"decision": "deny_once"}
    )
    permissions.session_allowed_edits.add(str(target))
    session = create_new_session(str(tmp_path))

    result = delete_file_tool.run(
        {"path": "scratch.py"},
        ToolContext(cwd=str(tmp_path), permissions=permissions, session=session),
    )
    assert result.ok is False
    assert "Delete denied" in result.output
    assert target.read_text(encoding="utf-8") == "keep"
    assert session.checkpoints == []
    assert len(requests) == 1


def test_delete_rejects_directory_escape_and_missing_approval(
    tmp_path: Path, isolated_storage: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "scratch.py"
    target.write_text("keep", encoding="utf-8")
    session = create_new_session(str(workspace))
    permissions = PermissionManager(str(workspace), prompt=lambda _: {"decision": "allow_once"})
    context = ToolContext(cwd=str(workspace), permissions=permissions, session=session)

    for path in (".", "../outside.py"):
        assert delete_file_tool.run({"path": path}, context).ok is False
    assert delete_file_tool.run(
        {"path": "scratch.py"}, ToolContext(cwd=str(workspace), permissions=permissions)
    ).ok is False
    assert target.exists()
    assert session.checkpoints == []


def test_delete_cancels_if_file_changes_while_approval_is_open(
    tmp_path: Path, isolated_storage: None,
) -> None:
    target = tmp_path / "scratch.py"
    target.write_text("before", encoding="utf-8")

    def change_during_approval(_request):
        target.write_text("after", encoding="utf-8")
        return {"decision": "allow_once"}

    permissions = PermissionManager(str(tmp_path), prompt=change_during_approval)
    session = create_new_session(str(tmp_path))
    result = delete_file_tool.run(
        {"path": "scratch.py"},
        ToolContext(cwd=str(tmp_path), permissions=permissions, session=session),
    )
    assert result.ok is False
    assert "changed during approval" in result.output
    assert target.read_text(encoding="utf-8") == "after"
    assert session.checkpoints == []


def test_default_registry_exposes_safe_delete_not_legacy_batch_delete(tmp_path: Path) -> None:
    names = {tool.name for tool in create_default_tool_registry(str(tmp_path)).list()}
    assert "delete_file" in names
    assert "batch_delete" not in names
    with pytest.raises(ValueError, match="cannot delete directories"):
        delete_file_tool.validator({"path": "src", "recursive": True})


def test_delete_tool_result_is_a_change_not_validation() -> None:
    evidence = classify_tool_result(
        tool_name="delete_file", tool_input={"path": "scratch.py"}, ok=True,
        result_output="Deleted file: scratch.py", source_session_id=None,
        source_turn_id=None,
    )
    assert evidence is not None
    assert evidence.level.value == "change"
