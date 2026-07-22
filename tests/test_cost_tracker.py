"""Regression tests for API usage cost calculation."""

import pytest

from repoterm.cost_tracker import calculate_cost


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-20250514",
        "deepseek-v4-pro[1m]",
    ],
)
def test_calculate_cost_supports_known_and_fallback_models(model: str) -> None:
    cost = calculate_cost(
        model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )

    assert isinstance(cost, float)
    assert cost == pytest.approx(22.05)


def test_calculate_cost_returns_zero_for_empty_usage() -> None:
    assert calculate_cost("deepseek-v4-pro[1m]") == 0.0
