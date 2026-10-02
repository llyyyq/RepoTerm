from repoterm.runtime.loop import _edit_retry_signature


def test_edit_retry_signature_tracks_exact_file_version(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("before\n", encoding="utf-8")
    call = {
        "toolName": "edit_file",
        "input": {"path": "sample.py", "old": "missing", "new": "after"},
    }
    first = _edit_retry_signature(call, tmp_path)
    assert first is not None
    assert first == _edit_retry_signature(call, tmp_path)
    target.write_text("new file version\n", encoding="utf-8")
    assert first != _edit_retry_signature(call, tmp_path)
    assert _edit_retry_signature({"toolName": "read_file", "input": call["input"]}, tmp_path) is None


def test_edit_retry_signature_ignores_outside_workspace(tmp_path) -> None:
    call = {"toolName": "edit_file", "input": {"path": "../outside.py", "old": "x", "new": "y"}}
    assert _edit_retry_signature(call, tmp_path) is None
