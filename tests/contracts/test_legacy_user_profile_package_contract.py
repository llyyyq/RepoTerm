"""Contracts for the read-only legacy USER.md compatibility module."""

from pathlib import Path

import repoterm.memory as memory
import repoterm.memory.legacy_user_profile as legacy
import repoterm.config as config
from repoterm.memory.legacy_user_profile import handle_user_command


_LEGACY_EXPORTS = (
    "UserProfile",
    "UserPreferences",
    "CodingStyle",
    "UserProfileManager",
    "handle_user_command",
    "parse_user_md",
    "serialize_user_md",
)


def test_legacy_user_profile_has_an_explicit_package_boundary() -> None:
    """Legacy APIs are importable only from their explicit implementation path."""

    assert legacy.handle_user_command is handle_user_command
    assert Path(legacy.__file__).as_posix().endswith(
        "repoterm/memory/legacy_user_profile.py"
    )
    assert not Path("repoterm/user_profile.py").exists()
    assert not any(hasattr(memory, name) for name in _LEGACY_EXPORTS)
    assert not any(name in memory.__all__ for name in _LEGACY_EXPORTS)


def test_legacy_user_profile_remains_read_only_and_readable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Legacy commands read isolated USER.md files but never mutate or delete them."""

    profile_dir = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    project_profile_dir = workspace / ".repoterm"
    profile_dir.mkdir()
    project_profile_dir.mkdir(parents=True)
    workspace.mkdir(exist_ok=True)

    global_path = profile_dir / "USER.md"
    project_path = project_profile_dir / "USER.md"
    global_path.write_text(
        "# User Profile\n\n"
        "## Preferences\n"
        "- **Language**: Chinese\n"
        "- **Verbosity**: concise\n\n"
        "## Common Patterns\n"
        "- prefer pytest\n",
        encoding="utf-8",
    )
    project_path.write_text(
        "# User Profile\n\n"
        "## Project Context\n"
        "RepoTerm compatibility contract\n",
        encoding="utf-8",
    )
    before = {
        global_path: global_path.read_bytes(),
        project_path: project_path.read_bytes(),
    }
    monkeypatch.setattr(config, "REPOTERM_DIR", profile_dir)

    for command in (
        "set preferences.language English",
        "reset",
        "reset-global",
    ):
        result = handle_user_command(command, cwd=workspace)
        assert "read-only" in result
        assert {path: (path.exists(), path.read_bytes()) for path in before} == {
            path: (True, content) for path, content in before.items()
        }

    show = handle_user_command("show", cwd=workspace)
    global_result = handle_user_command("global", cwd=workspace)
    project_result = handle_user_command("project", cwd=workspace)
    paths = handle_user_command("paths", cwd=workspace)
    search = handle_user_command("search Chinese", cwd=workspace)

    assert "Language: Chinese" in show
    assert "Global Profile" in global_result
    assert "Project Profile" in project_result
    assert "Global:" in paths and "Project:" in paths
    assert "preference.language = Chinese" in search
    assert {path: (path.exists(), path.read_bytes()) for path in before} == {
        path: (True, content) for path, content in before.items()
    }
    assert not (profile_dir / "memory.sqlite3").exists()
