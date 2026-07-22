"""Shared path-normalization and secret-redaction helpers for Agent evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def normalize_evidence_paths(
    value: Any,
    *,
    repo_root: Path,
    home: Path | None = None,
) -> Any:
    """Replace machine-specific repository and home prefixes in evidence payloads."""
    if isinstance(value, dict):
        return {
            key: normalize_evidence_paths(item, repo_root=repo_root, home=home)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            normalize_evidence_paths(item, repo_root=repo_root, home=home)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            normalize_evidence_paths(item, repo_root=repo_root, home=home)
            for item in value
        )
    if not isinstance(value, str):
        return value

    repo_text = str(repo_root.resolve())
    home_text = str((home or Path.home()).resolve())
    normalized = value
    replaced_path = False
    repo_prefix = f"{repo_text}{os.sep}"
    if repo_prefix in normalized:
        normalized = normalized.replace(repo_prefix, "")
        replaced_path = True
    if normalized == repo_text:
        normalized = "."
        replaced_path = True
    if home_text != repo_text:
        home_prefix = f"{home_text}{os.sep}"
        if home_prefix in normalized:
            normalized = normalized.replace(home_prefix, "~/")
            replaced_path = True
        if normalized == home_text:
            normalized = "~"
            replaced_path = True
    if replaced_path and os.sep != "/":
        normalized = normalized.replace(os.sep, "/")
    return normalized


_SENSITIVE_KEY_PARTS = (
    "apikey",
    "api_key",
    "auth_token",
    "authtoken",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)
_SAFE_PLACEHOLDERS = {
    "",
    "...",
    "sk-...",
    "sk-or-...",
    "<redacted>",
    "[redacted]",
}
_SENSITIVE_STRUCTURED_KEY_NAMES = (
    "apikey",
    "password",
    "token",
    "secret",
    "authtoken",
    "authorization",
    "bearer",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?P<key>[\"']?\b(?:apiKey|authToken|authorization|OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY)\b[\"']?"
        r"[ \t]*[:=][ \t]*[\"']?)(?P<value>[A-Za-z0-9._/-]{8,})(?P<quote>[\"']?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<key>\b(?:apiKey|authToken|authorization|OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY)\b"
        r"[ \t]+)(?P<value>[A-Za-z0-9._/-]{8,})",
        re.IGNORECASE,
    ),
)


def _looks_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    if normalized.endswith("base_url") or normalized.endswith("baseurl"):
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_sensitive_structured_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return any(
        normalized == name or normalized.endswith(name)
        for name in _SENSITIVE_STRUCTURED_KEY_NAMES
    )


def _is_safe_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() in _SAFE_PLACEHOLDERS


def find_sensitive_payload_leaks(value: Any, *, limit: int = 5) -> list[str]:
    findings: list[str] = []

    def visit(item: Any, path: str) -> None:
        if len(findings) >= limit:
            return
        if isinstance(item, dict):
            for raw_key, nested in item.items():
                key = str(raw_key)
                nested_path = f"{path}.{key}" if path else key
                if _is_sensitive_structured_key(key) and not _is_safe_placeholder(nested):
                    findings.append(f"sensitive value at {nested_path}")
                    if len(findings) >= limit:
                        return
                visit(nested, nested_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")
                if len(findings) >= limit:
                    return

    visit(value, "")
    return findings


def redact_sensitive_text(text: str) -> str:
    redacted = str(text)
    for pattern in _SECRET_TEXT_PATTERNS:
        if "value" in pattern.groupindex and "quote" in pattern.groupindex:
            redacted = pattern.sub(r"\g<key>[REDACTED]\g<quote>", redacted)
        elif "value" in pattern.groupindex:
            redacted = pattern.sub(r"\g<key>[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def find_sensitive_text_leaks(text: str, *, limit: int = 5) -> list[str]:
    findings: list[str] = []
    for pattern in _SECRET_TEXT_PATTERNS:
        for match in pattern.finditer(str(text)):
            value = match.group("value") if "value" in pattern.groupindex else match.group(0)
            if str(value).strip() in _SAFE_PLACEHOLDERS:
                continue
            findings.append(f"sensitive token near offset {match.start()}")
            if len(findings) >= limit:
                return findings
    return findings


def redact_sensitive_payload(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            item_key: redact_sensitive_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_payload(item) for item in value]
    if isinstance(value, str):
        if _looks_sensitive_key(key) and value.strip() not in _SAFE_PLACEHOLDERS:
            return "[REDACTED]"
        return redact_sensitive_text(value)
    return value
