from __future__ import annotations

from Main.RepoTermFrontline.Src.Application.Entry.RepoTermFrontline import (
    entry_surface_names,
)
from Main.RepoTermFrontline.Src.Application.Dto.AppProjection import (
    CURRENT_IMPLEMENTATION_ROOT,
    ENTRY_SURFACES,
    LOGICAL_PRODUCT_APP,
)


def test_repoterm_frontline_entry_contract_tracks_current_runtime_root() -> None:
    assert LOGICAL_PRODUCT_APP == "product/app/repoterm_frontline"
    assert CURRENT_IMPLEMENTATION_ROOT == "repoterm"


def test_repoterm_frontline_entry_contract_names_observable_surfaces() -> None:
    assert entry_surface_names() == (
        "interactive-cli",
        "headless-runner",
        "readiness-checker",
        "local-command-surface",
        "product-snapshot",
    )
    assert all(surface.currentPoint for surface in ENTRY_SURFACES)
    assert all(surface.observableResult for surface in ENTRY_SURFACES)
