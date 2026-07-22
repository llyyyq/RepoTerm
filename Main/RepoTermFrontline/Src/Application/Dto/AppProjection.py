from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntrySurface:
    name: str
    currentPoint: str
    observableResult: str
    appRole: str


LOGICAL_PRODUCT_APP = "product/app/repoterm_frontline"
CURRENT_IMPLEMENTATION_ROOT = "repoterm"


ENTRY_SURFACES = (
    EntrySurface(
        name="interactive-cli",
        currentPoint="repoterm | python -m repoterm.main",
        observableResult=(
            "terminal coding session with tools, permissions, model runtime, "
            "transcript, session commands, checkpoints, and rewind"
        ),
        appRole="product app lifecycle entry",
    ),
    EntrySurface(
        name="headless-runner",
        currentPoint="repoterm-headless | python -m repoterm.headless",
        observableResult="single prompt execution with optional message trace",
        appRole="product app automation entry",
    ),
    EntrySurface(
        name="readiness-checker",
        currentPoint="repoterm-readiness | python -m repoterm.readiness",
        observableResult="provider/runtime readiness report with risk scope and next actions",
        appRole="product app diagnostic entry",
    ),
    EntrySurface(
        name="local-command-surface",
        currentPoint="repoterm/cli_commands.py",
        observableResult=(
            "/session, /session-replay, /sessions, /checkpoints, /rewind, "
            "/readiness, and /extensions"
        ),
        appRole="product app operation surface",
    ),
    EntrySurface(
        name="product-snapshot",
        currentPoint="repoterm/product_surfaces.py",
        observableResult=(
            "instruction, hook, delegation, extension, readiness, and prompt "
            "bundle summaries"
        ),
        appRole="product app observability surface",
    ),
)
