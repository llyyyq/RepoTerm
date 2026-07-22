from __future__ import annotations

from benchmarks.runtime_regression_eval import (
    SCENARIOS,
    build_report,
    parse_pytest_scenarios,
    render_markdown,
)


def _round(round_number: int) -> dict:
    return {
        "round": round_number,
        "exit_code": 0,
        "duration_ms": 1000,
        "collected_count": len(SCENARIOS),
        "passed_count": len(SCENARIOS),
        "results": [
            {"test_name": scenario.test_name, "status": "passed", "passed": True}
            for scenario in SCENARIOS
        ],
        "pytest_output_tail": "20 passed",
    }


def test_runtime_regression_catalog_has_twenty_unique_scenarios():
    assert len(SCENARIOS) == 20
    assert len({scenario.test_name for scenario in SCENARIOS}) == 20
    assert all(scenario.name_zh and scenario.grader_zh for scenario in SCENARIOS)


def test_parse_verbose_pytest_scenario_results():
    output = "\n".join(
        [
            "tests/test_agentops_scenarios.py::test_alpha PASSED [ 50%]",
            "tests/test_agentops_scenarios.py::test_beta FAILED [100%]",
        ]
    )

    assert parse_pytest_scenarios(output) == {
        "test_alpha": "passed",
        "test_beta": "failed",
    }


def test_report_separates_scripted_runtime_from_live_provider_scope():
    report = build_report([_round(1), _round(2), _round(3)])

    assert report["methodology"]["provider_called"] is False
    assert report["metrics"]["total_executions"] == 60
    assert report["metrics"]["passed_executions"] == 60
    markdown = render_markdown(report)
    assert "20个场景 × 3轮" in markdown
    assert "真实模型评测的分工" in markdown
    assert "不能证明" in markdown
