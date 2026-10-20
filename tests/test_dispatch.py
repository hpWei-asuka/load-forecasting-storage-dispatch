from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pulp
import pytest

from load_storage.backtest import replay_day
from load_storage.config import BatteryConfig, SolverConfig
from load_storage.optimization import (
    check_solver, classify_termination, make_plan, optimize_day, validate_plan,
)
from load_storage.strategies import fixed_rule_day, no_storage_day


@pytest.fixture
def battery():
    return BatteryConfig(source="unit test", capacity_kwh=2.0, min_energy_kwh=0.0,
                         max_energy_kwh=2.0, initial_energy_kwh=0.0,
                         max_charge_kw=1.0, max_discharge_kw=1.0,
                         charge_efficiency=1.0, discharge_efficiency=1.0)


def forecast(values=1.0):
    if np.isscalar(values):
        values = np.full(24, values)
    return pd.DataFrame({"forecast_kw": values}, index=pd.date_range("2010-01-08", periods=24, freq="h"))


def actual(values=1.0):
    return forecast(values).rename(columns={"forecast_kw": "load_kw"})


def tariff():
    return np.array([0.3]*7 + [0.6]*10 + [1.0]*5 + [0.6]*2)


def test_solver_executes_known_integer_model():
    check = check_solver(SolverConfig())
    assert check["status"] == "ok"
    assert check["objective"] == 2.0
    assert Path(check["path"]).is_file()
    assert check["version"] != "unknown"


def test_known_arbitrage_optimum_and_conservation(battery, tmp_path):
    # 理想效率下，将2 kWh从0.30元转移到1.00元时段，节省1.40元。
    f = forecast()
    log = tmp_path/"cbc.log"
    result = optimize_day(f, tariff(), battery, SolverConfig(), log_path=log)
    assert result.valid and result.status == "optimal", result
    assert result.predicted_cost == pytest.approx(float(tariff().sum())-1.4, abs=1e-6)
    assert validate_plan(result.plan, f, battery, prices=tariff(), predicted_cost=result.predicted_cost) == []
    assert result.plan["energy_end_kwh"].iloc[-1] == pytest.approx(0.0)
    assert "Optimal" in log.read_text()
    replay = replay_day(result.plan, f.rename(columns={"forecast_kw": "load_kw"}), battery, tariff())
    assert replay.valid
    assert replay.actual_cost == pytest.approx(result.predicted_cost, abs=1e-6)


def test_efficiency_is_applied_once_on_each_side(battery):
    battery = replace(battery, charge_efficiency=0.8, discharge_efficiency=0.8)
    result = optimize_day(forecast(), tariff(), battery, SolverConfig())
    assert result.valid
    # 电池充入2 kWh需要购电2.5 kWh，之后可放电1.6 kWh。
    assert result.plan.charge_kw.sum() == pytest.approx(2.5, abs=1e-6)
    assert result.plan.discharge_kw.sum() == pytest.approx(1.6, abs=1e-6)
    assert result.predicted_cost == pytest.approx(tariff().sum()+2.5*0.3-1.6, abs=1e-6)


def test_flat_tariff_cannot_save_when_losses_present(battery):
    battery = replace(battery, charge_efficiency=0.9, discharge_efficiency=0.9)
    result = optimize_day(forecast(), np.ones(24), battery, SolverConfig())
    assert result.valid
    assert result.predicted_cost == pytest.approx(24.0, abs=1e-6)
    assert result.plan.charge_kw.sum() == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("price", [np.zeros(24), tariff()])
def test_zero_load_does_not_export_or_invent_savings(battery, price):
    result = optimize_day(forecast(0.0), price, battery, SolverConfig())
    assert result.valid, result
    assert result.predicted_cost == pytest.approx(0.0, abs=1e-6)
    assert np.all(result.plan.discharge_kw <= 1e-6)


def test_fixed_rule_uses_windows_and_returns_initial_energy(battery):
    battery = replace(battery, initial_energy_kwh=0.4, charge_efficiency=0.95,
                      discharge_efficiency=0.9)
    f = forecast(np.linspace(0.2, 2.0, 24))
    result = fixed_rule_day(f, tariff(), battery, [0, 1, 2], [17, 18, 19])
    assert result.valid
    plan = result.plan
    assert set(plan.index[plan.charge_kw > 1e-6].hour).issubset({0, 1, 2})
    assert set(plan.index[plan.discharge_kw > 1e-6].hour).issubset({17, 18, 19})
    assert plan.energy_end_kwh.iloc[-1] == pytest.approx(0.4, abs=1e-6)
    assert validate_plan(plan, f, battery) == []


def test_fixed_rule_forecast_limits_daily_cycle_before_charging(battery):
    values = np.zeros(24)
    values[17] = 0.25
    result = fixed_rule_day(forecast(values), tariff(), battery, [0, 1], [17, 18])
    assert result.valid
    assert result.plan.charge_kw.sum() == pytest.approx(0.25)
    assert result.plan.discharge_kw.sum() == pytest.approx(0.25)


def test_fixed_rule_idle_when_no_charge_headroom(battery):
    battery = replace(battery, initial_energy_kwh=battery.max_energy_kwh)
    result = fixed_rule_day(forecast(), tariff(), battery, [0, 1], [17, 18])
    assert result.valid
    assert result.plan.charge_kw.sum() == result.plan.discharge_kw.sum() == 0


@pytest.mark.parametrize("charge,discharge", [([0, 1], [1, 2]), ([18], [17]),
                                            ([1, 0], [17]), ([], [17]), ([0], [24])])
def test_fixed_rule_rejects_invalid_windows(battery, charge, discharge):
    with pytest.raises(ValueError):
        fixed_rule_day(forecast(), tariff(), battery, charge, discharge)


def test_no_storage_is_same_actual_cost_without_battery_actions(battery):
    result = no_storage_day(forecast(2.0), tariff(), battery)
    assert result.valid
    replay = replay_day(result.plan, actual(0.5), battery, tariff())
    assert replay.valid
    assert replay.actual_cost == pytest.approx(0.5*tariff().sum())


def test_replay_two_kw_plan_on_one_kw_actual_is_invalid_without_clipping(battery):
    battery = replace(battery, max_charge_kw=2.0, max_discharge_kw=2.0)
    result = fixed_rule_day(forecast(2.0), tariff(), battery, [0], [17])
    original = result.plan.copy(deep=True)
    replay = replay_day(result.plan, actual(1.0), battery, tariff())
    assert not replay.valid and replay.status == "replay_infeasible"
    assert replay.actual_cost is None
    assert replay.failure_reason == "discharge_exceeds_load_kw"
    assert replay.first_failure_timestamp == "2010-01-08T17:00:00"
    assert replay.violation == pytest.approx(1.0)
    pd.testing.assert_frame_equal(original, result.plan)


def test_replay_with_different_actual_uses_actual_grid_not_stored_planned_grid(battery):
    result = fixed_rule_day(forecast(), tariff(), battery, [0, 1], [17, 18])
    replay = replay_day(result.plan, actual(3.0), battery, tariff())
    assert replay.valid
    assert replay.actual_cost == pytest.approx(result.predicted_cost+2*tariff().sum(), abs=1e-6)


def test_replay_prefers_actual_when_given_merged_prediction_table(battery):
    result = fixed_rule_day(forecast(), tariff(), battery, [0, 1], [17, 18])
    actual = forecast()
    actual["actual_kw"] = 0.1
    replay = replay_day(result.plan, actual, battery, tariff())
    assert not replay.valid
    assert replay.actual_cost is None


def test_replay_rejects_forecast_only_table_as_actual_load(battery):
    result = no_storage_day(forecast(), tariff(), battery)
    replay = replay_day(result.plan, forecast(), battery, tariff())
    assert not replay.valid
    assert replay.status == "actual_unavailable"
    assert replay.actual_cost is None


@pytest.mark.parametrize("column,hour,increment,constraint", [
    ("charge_kw", 0, 2.0, "charge_power_limit_kw"),
    ("discharge_kw", 5, 2.0, "discharge_power_limit_kw"),
    ("energy_start_kwh", 2, 3.0, "energy_start_max_kwh"),
    ("energy_end_kwh", 23, 1.0, "terminal_energy_kwh"),
    ("planned_grid_kw", 4, 1.0, "planned_grid_balance_kw"),
])
def test_independent_checker_rejects_tampering(battery, column, hour, increment, constraint):
    plan = no_storage_day(forecast(), tariff(), battery).plan
    plan.iloc[hour, plan.columns.get_loc(column)] += increment
    violations = validate_plan(plan, forecast(), battery)
    assert constraint in {v["constraint"] for v in violations}


def test_independent_checker_checks_mutual_exclusion_and_reported_cost(battery):
    c, d = np.zeros(24), np.zeros(24)
    c[0] = d[0] = 0.5
    plan = make_plan(forecast(), c, d, battery)
    violations = validate_plan(plan, forecast(), battery, prices=tariff(), predicted_cost=123.0)
    assert {"charge_discharge_exclusive_kw", "predicted_cost"}.issubset(v["constraint"] for v in violations)


def test_replay_checks_energy_constraints_again(battery):
    plan = no_storage_day(forecast(), tariff(), battery).plan
    plan.iloc[-1, plan.columns.get_loc("energy_end_kwh")] = 1.0
    replay = replay_day(plan, actual(), battery, tariff())
    assert not replay.valid and replay.actual_cost is None
    assert replay.status == "replay_infeasible"


@pytest.mark.parametrize("status,solution,log,expected", [
    (pulp.LpStatusOptimal, pulp.LpSolutionOptimal, "Result - Optimal solution found", "optimal"),
    (pulp.LpStatusOptimal, pulp.LpSolutionIntegerFeasible, "Result - Stopped on time limit", "time_limit"),
    (pulp.LpStatusOptimal, pulp.LpSolutionIntegerFeasible, "Result - Optimal solution found", "not_proven_optimal"),
    (pulp.LpStatusOptimal, pulp.LpSolutionOptimal, "", "not_proven_optimal"),
    (pulp.LpStatusOptimal, pulp.LpSolutionOptimal, "Result - Optimal solution found (within gap tolerance)", "not_proven_optimal"),
    (pulp.LpStatusInfeasible, pulp.LpSolutionInfeasible, "Problem is infeasible", "infeasible"),
    (pulp.LpStatusUnbounded, pulp.LpSolutionUnbounded, "Problem is unbounded", "unbounded"),
])
def test_termination_requires_problem_solution_and_cbc_proof(status, solution, log, expected):
    assert classify_termination(status, solution, log)[0] == expected


@pytest.mark.parametrize("failure", ["infeasible", "unbounded", "time_limit", "not_proven_optimal"])
def test_unsuccessful_solver_cannot_return_schedule_or_cost(battery, monkeypatch, failure):
    import load_storage.optimization as optimization
    monkeypatch.setattr(optimization, "_solve", lambda *args: (failure, "simulated", {}))
    result = optimize_day(forecast(), tariff(), battery, SolverConfig())
    assert not result.valid
    assert result.status == failure
    assert result.plan is None and result.predicted_cost is None


def test_missing_cbc_is_explicit_failure_not_zero_plan(battery, tmp_path):
    solver = SolverConfig(cbc_path=tmp_path/"missing-cbc.exe")
    result = optimize_day(forecast(), tariff(), battery, solver)
    assert result.status == "solver_error"
    assert result.plan is None and result.predicted_cost is None
    assert "CBC" in result.failure_reason
    with pytest.raises(FileNotFoundError):
        check_solver(solver)


def test_invalid_load_timestamps_and_values_not_silently_aligned(battery):
    f = forecast()
    f.iloc[3, 0] = np.nan
    result = optimize_day(f, tariff(), battery, SolverConfig())
    assert result.status == "invalid_input"
    f = forecast()
    f.index = f.index + pd.Timedelta(1, unit="h")
    result = optimize_day(f, tariff(), battery, SolverConfig())
    assert result.status == "invalid_input"
