from __future__ import annotations

from pathlib import Path

from repoterm.evidence_safety import (
    find_sensitive_payload_leaks,
    find_sensitive_text_leaks,
    normalize_evidence_paths,
    redact_sensitive_payload,
    redact_sensitive_text,
)


def test_normalize_evidence_paths_removes_machine_specific_prefixes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    payload = {
        "trace": str(repo / "runs" / "trace.json"),
        "settings": str(home / ".repoterm" / "settings.json"),
    }

    normalized = normalize_evidence_paths(payload, repo_root=repo, home=home)

    assert normalized == {
        "trace": "runs/trace.json",
        "settings": "~/.repoterm/settings.json",
    }


def test_redaction_protects_structured_and_free_text_credentials() -> None:
    payload = {
        "authToken": "secret-token-123456",
        "baseUrl": "https://example.com",
        "message": "Authorization: Bearer abcdefghijklmnop",
    }

    redacted = redact_sensitive_payload(payload)

    assert redacted["authToken"] == "[REDACTED]"
    assert redacted["baseUrl"] == "https://example.com"
    assert "abcdefghijklmnop" not in redacted["message"]
    assert find_sensitive_payload_leaks(redacted) == []
    assert find_sensitive_text_leaks(str(redacted)) == []


def test_text_redaction_preserves_safe_placeholders() -> None:
    text = "OPENAI_API_KEY=sk-realvalue123\nexample=sk-..."

    redacted = redact_sensitive_text(text)

    assert "sk-realvalue123" not in redacted
    assert "sk-..." in redacted
