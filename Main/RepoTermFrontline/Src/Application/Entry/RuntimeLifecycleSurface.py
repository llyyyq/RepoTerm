from __future__ import annotations

from dataclasses import dataclass

from Main.RepoTermFrontline.Src.Application.Dto.AppProjection import (
    LOGICAL_PRODUCT_APP,
)


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleEntry:
    name: str
    scriptName: str
    moduleTarget: str
    commandSurface: str
    lifecycleRole: str


RUNTIME_LIFECYCLE_ENTRIES = (
    RuntimeLifecycleEntry(
        name="interactive-cli",
        scriptName="repoterm",
        moduleTarget="repoterm.main:main",
        commandSurface="python -m repoterm.main",
        lifecycleRole="interactive product app lifecycle",
    ),
    RuntimeLifecycleEntry(
        name="headless-runner",
        scriptName="repoterm-headless",
        moduleTarget="repoterm.headless:main",
        commandSurface="python -m repoterm.headless",
        lifecycleRole="non-interactive automation lifecycle",
    ),
    RuntimeLifecycleEntry(
        name="readiness-checker",
        scriptName="repoterm-readiness",
        moduleTarget="repoterm.readiness:main",
        commandSurface="python -m repoterm.readiness",
        lifecycleRole="provider readiness diagnostic lifecycle",
    ),
)


def lifecycle_script_targets() -> dict[str, str]:
    return {
        entry.scriptName: entry.moduleTarget
        for entry in RUNTIME_LIFECYCLE_ENTRIES
    }


def lifecycle_contract_payload() -> dict[str, object]:
    return {
        "logicalProductApp": LOGICAL_PRODUCT_APP,
        "entryCount": len(RUNTIME_LIFECYCLE_ENTRIES),
        "scripts": lifecycle_script_targets(),
    }
