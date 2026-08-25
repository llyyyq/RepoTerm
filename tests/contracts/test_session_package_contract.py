"""Contracts for the Session package boundary introduced by C2."""

from pathlib import Path

import repoterm.session as session
import repoterm.session.service as service


def test_session_package_reexports_service_symbols() -> None:
    """The public package must expose the same objects as its implementation module."""

    assert session.SessionData is service.SessionData
    assert session.FileCheckpoint is service.FileCheckpoint
    assert session.create_new_session is service.create_new_session
    assert session.save_session is service.save_session
    assert session.load_session is service.load_session


def test_session_package_persists_through_service_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Public create/save/load calls must use the implementation module's storage paths."""

    runtime_root = tmp_path / "runtime-state"
    sessions_root = runtime_root / "sessions"
    monkeypatch.setattr(service, "REPOTERM_DIR", runtime_root)
    monkeypatch.setattr(service, "SESSIONS_DIR", sessions_root)

    created = session.create_new_session(str(tmp_path / "workspace"))
    created.messages = [{"role": "user", "content": "session package contract"}]
    session.save_session(created, force_full=True)

    assert (sessions_root / f"{created.session_id}.json").is_file()
    loaded = session.load_session(created.session_id)
    assert loaded is not None
    assert loaded.session_id == created.session_id
    assert loaded.messages == created.messages
    assert Path(session.__file__).name == "__init__.py"
    assert Path(service.__file__).name == "service.py"
    assert not (Path(service.__file__).parent.parent / "session.py").exists()
    assert not hasattr(session, "REPOTERM_DIR")
    assert not hasattr(session, "SESSIONS_DIR")
