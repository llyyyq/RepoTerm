from __future__ import annotations

"""Small validation helpers used by the local runtime-readiness CLI."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoterm.evidence_safety import find_sensitive_text_leaks


@dataclass(frozen=True, slots=True)
class ReadinessValidation:
    status: str
    summary: str


def build_artifact_manifest(artifacts: dict[str, str | Path]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for label, raw_path in sorted(artifacts.items()):
        path = Path(raw_path)
        entry: dict[str, Any] = {
            "label": str(label),
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": 0,
            "sha256": "",
        }
        if path.exists() and path.is_file():
            try:
                data = path.read_bytes()
                entry["size_bytes"] = len(data)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
            except OSError as exc:
                entry["error"] = str(exc)
        manifest.append(entry)
    return manifest


def write_artifact_manifest(
    path: str | Path,
    artifacts: dict[str, str | Path],
) -> list[dict[str, Any]]:
    manifest = build_artifact_manifest(artifacts)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def check_fallback_patch_preview_payload(payload: Any) -> ReadinessValidation:
    errors: list[str] = []
    if not isinstance(payload, dict):
        errors.append("fallback patch preview payload is not an object")
        payload = {}

    status = str(payload.get("status") or "").strip()
    risk_scope = str(payload.get("risk_scope") or "").strip()
    previews = payload.get("fallback_settings_patch_preview")
    if status not in {"ready", "warning", "blocked"}:
        errors.append(f"fallback patch preview has invalid status: {status}")
    if not risk_scope:
        errors.append("fallback patch preview missing risk_scope")
    if not isinstance(previews, list):
        errors.append("fallback patch preview missing fallback_settings_patch_preview list")
        previews = []
    elif status != "ready" and risk_scope != "none" and not previews:
        errors.append("fallback patch preview has no actionable preview")

    required_note_fragments = (
        "Review the selected provider patch",
        "Replace placeholder credentials locally",
        "Merge only one selected patch",
        "Run repoterm-readiness --json --fail-on blocked",
    )
    for index, preview in enumerate(previews):
        if not isinstance(preview, dict):
            errors.append(f"fallback patch preview[{index}] is not an object")
            continue
        if not str(preview.get("label") or "").strip():
            errors.append(f"fallback patch preview[{index}] missing label")
        if not str(preview.get("target_path") or "").strip():
            errors.append(f"fallback patch preview[{index}] missing target_path")
        if preview.get("safety") != "preview-only; no settings are modified":
            errors.append(f"fallback patch preview[{index}] has invalid safety")
        merge_patch = preview.get("merge_patch")
        if not isinstance(merge_patch, dict) or not merge_patch:
            errors.append(f"fallback patch preview[{index}] missing non-empty merge_patch")
        apply_notes = preview.get("apply_notes")
        if not isinstance(apply_notes, list) or not apply_notes:
            errors.append(f"fallback patch preview[{index}] missing apply_notes")
        else:
            joined_notes = "\n".join(str(item) for item in apply_notes)
            for fragment in required_note_fragments:
                if fragment not in joined_notes:
                    errors.append(
                        f"fallback patch preview[{index}] missing apply note: {fragment}"
                    )

    leaks = find_sensitive_text_leaks(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if leaks:
        errors.append(leaks[0])
    if errors:
        return ReadinessValidation(status="failed", summary=errors[0])
    return ReadinessValidation(
        status="passed",
        summary=f"fallback patch preview valid: {len(previews)} preview(s) ({risk_scope})",
    )
