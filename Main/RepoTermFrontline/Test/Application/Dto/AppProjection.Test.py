from __future__ import annotations

from Main.RepoTermFrontline.Src.Application.Dto.AppProjection import (
    CURRENT_IMPLEMENTATION_ROOT,
    ENTRY_SURFACES,
    LOGICAL_PRODUCT_APP,
)


def test_app_projection_dto_declares_current_product_identity() -> None:
    assert LOGICAL_PRODUCT_APP == "product/app/repoterm_frontline"
    assert CURRENT_IMPLEMENTATION_ROOT == "repoterm"
    assert [surface.name for surface in ENTRY_SURFACES] == [
        "interactive-cli",
        "headless-runner",
        "readiness-checker",
        "local-command-surface",
        "product-snapshot",
    ]
