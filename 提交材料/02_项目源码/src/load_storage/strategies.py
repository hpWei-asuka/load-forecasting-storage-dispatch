"""无储能和固定窗口对照策略，只读取预测负荷。"""

from __future__ import annotations

import numpy as np

from .config import BatteryConfig
from .optimization import PlanResult, load_series, make_plan, price_array, validate_plan


def no_storage_day(forecast, prices, battery: BatteryConfig) -> PlanResult:
    loads = load_series(forecast)
    price = price_array(prices)
    plan = make_plan(loads, np.zeros(24), np.zeros(24), battery)
    return PlanResult("valid", plan, float(np.dot(price, loads)))


def fixed_rule_day(forecast, prices, battery: BatteryConfig, charge_hours, discharge_hours, *,
                   tolerance: float = 1e-6) -> PlanResult:
    loads = load_series(forecast)
    price = price_array(prices)
    charge_hours, discharge_hours = list(charge_hours), list(discharge_hours)
    hours = charge_hours+discharge_hours
    if (not charge_hours or not discharge_hours or
        any(isinstance(h, bool) or not isinstance(h, (int, np.integer)) or h < 0 or h > 23 for h in hours) or
        len(set(hours)) != len(hours) or charge_hours != sorted(charge_hours) or
        discharge_hours != sorted(discharge_hours) or max(charge_hours) >= min(discharge_hours)):
        raise ValueError("固定规则窗口必须有序、互不重叠，全部充电小时早于放电小时")
    # amount为电池内部电量，与电网侧电量按效率换算。
    amount = min(
        battery.max_energy_kwh-battery.initial_energy_kwh,
        battery.charge_efficiency*battery.max_charge_kw*len(charge_hours),
        sum(min(battery.max_discharge_kw, float(loads.iloc[h])) for h in discharge_hours)
        / battery.discharge_efficiency,
    )
    charge, discharge = np.zeros(24), np.zeros(24)
    remaining = amount
    for hour in charge_hours:
        charge[hour] = min(battery.max_charge_kw, max(0.0, remaining)/battery.charge_efficiency)
        remaining -= charge[hour]*battery.charge_efficiency
    remaining = amount
    for hour in discharge_hours:
        discharge[hour] = min(battery.max_discharge_kw, float(loads.iloc[hour]),
                              max(0.0, remaining)*battery.discharge_efficiency)
        remaining -= discharge[hour]/battery.discharge_efficiency
    plan = make_plan(loads, charge, discharge, battery)
    predicted_cost = float(np.dot(price, plan["planned_grid_kw"]))
    violations = validate_plan(plan, loads, battery, tolerance=tolerance,
                               prices=price, predicted_cost=predicted_cost)
    if violations:
        return PlanResult("validation_failed", failure_reason="固定规则未通过独立约束检查",
                          diagnostics={"violations": violations})
    return PlanResult("valid", plan, predicted_cost, diagnostics={"cycle_internal_energy_kwh": float(amount)})
