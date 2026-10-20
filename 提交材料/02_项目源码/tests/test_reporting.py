import pandas as pd
import pytest

from load_storage.reporting import summarize


def test_summary_uses_only_three_strategy_common_valid_days_and_total_costs():
    rows = [
        {"date": "2010-01-01", "strategy": "no_storage", "status": "valid",
         "replay_valid": True, "actual_cost": 10.0},
        {"date": "2010-01-01", "strategy": "fixed_rule", "status": "valid",
         "replay_valid": True, "actual_cost": 8.0},
        {"date": "2010-01-01", "strategy": "optimized", "status": "valid",
         "replay_valid": True, "actual_cost": 9.0},
        {"date": "2010-01-02", "strategy": "no_storage", "status": "valid",
         "replay_valid": True, "actual_cost": 1.0},
        {"date": "2010-01-02", "strategy": "fixed_rule", "status": "valid",
         "replay_valid": True, "actual_cost": 0.0},
        {"date": "2010-01-02", "strategy": "optimized", "status": "replay_infeasible",
         "replay_valid": False, "actual_cost": None},
    ]
    forecasts = pd.DataFrame({"date": ["2010-01-01"], "forecast_kw": [2.0], "actual_kw": [1.0]})
    result = summarize(pd.DataFrame(rows), forecasts, ["2010-01-01", "2010-01-02"])
    assert result["common_valid_dates"] == ["2010-01-01"]
    assert result["cost_totals_on_common_dates"]["optimized"]["actual_cost"] == 9.0
    assert result["cost_totals_on_common_dates"]["optimized"]["savings_rate"] == 0.1
    assert result["strategy_failures"]["optimized"]["replay_infeasible_days"] == 1


def test_zero_common_dates_never_claim_a_saving():
    costs = pd.DataFrame([{
        "date": "2010-01-01", "strategy": strategy, "status": "replay_infeasible",
        "replay_valid": False, "actual_cost": None,
    } for strategy in ("no_storage", "fixed_rule", "optimized")])
    result = summarize(costs, pd.DataFrame(), ["2010-01-01"])
    assert result["common_valid_days"] == 0
    assert result["cost_totals_on_common_dates"] is None


def test_overlapping_failures_count_history_and_solver_independently():
    costs = pd.DataFrame([{
        "date": "2010-01-01", "strategy": strategy, "status": "data_excluded",
        "replay_valid": False, "actual_cost": None,
        "solver_status": "time_limit" if strategy == "optimized" else "not_applicable",
        "plan_status": "time_limit" if strategy == "optimized" else "not_generated",
    } for strategy in ("no_storage", "fixed_rule", "optimized")])
    quality = [{"date": "2010-01-01", "forecast_available": False}]
    result = summarize(costs, pd.DataFrame(), ["2010-01-01"], quality)
    assert result["data_excluded_days"] == 1
    assert result["forecast_unavailable_days"] == 1
    assert result["strategy_failures"]["optimized"]["solve_failed_days"] == 1


@pytest.mark.parametrize("baseline,optimized,expected_rate", [
    ([0.0, 0.0], [0.0, 0.0], None),
    ([10.0, 1.0], [8.0, 2.0], 1.0 / 11.0),
    ([10.0, 1.0], [12.0, 2.0], -3.0 / 11.0),
])
def test_total_rate_zero_baseline_and_negative_savings(baseline, optimized, expected_rate):
    rows = []
    for i, day in enumerate(["2010-01-01", "2010-01-02"]):
        for strategy, cost in [("no_storage", baseline[i]), ("fixed_rule", baseline[i]), ("optimized", optimized[i])]:
            rows.append({"date": day, "strategy": strategy, "status": "valid",
                         "replay_valid": True, "actual_cost": cost})
    result = summarize(pd.DataFrame(rows), pd.DataFrame(), ["2010-01-01", "2010-01-02"])
    rate = result["cost_totals_on_common_dates"]["optimized"]["savings_rate"]
    if expected_rate is None:
        assert rate is None
    else:
        assert rate == pytest.approx(expected_rate)
