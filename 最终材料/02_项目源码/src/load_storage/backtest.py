"""按真实负荷回放既定计划，记录不可执行的时段。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import BatteryConfig
from .optimization import load_series, price_array, validate_plan


@dataclass
class ReplayResult:
    valid: bool
    status: str
    actual_cost: float | None = None
    failure_reason: str | None = None
    first_failure_timestamp: str | None = None
    violation: float | None = None
    diagnostics: dict = field(default_factory=dict)


def replay_day(plan, actual, battery: BatteryConfig, prices, *, tolerance: float = 1e-6) -> ReplayResult:
    try:
        # 合并表可能含预测列，回测只取真实负荷。
        if isinstance(actual, pd.DataFrame):
            for column in ("actual_kw", "load_kw"):
                if column in actual:
                    actual = actual[column]
                    break
            else:
                raise ValueError("真实负荷表需要 actual_kw 或 load_kw 列，不能用 forecast_kw 回测")
        loads = load_series(actual)
        price = price_array(prices)
    except (TypeError, ValueError) as exc:
        return ReplayResult(False, "actual_unavailable", failure_reason=str(exc))
    violations = validate_plan(plan, loads, battery, tolerance=tolerance, check_grid_column=False)
    if violations:
        first = violations[0]
        return ReplayResult(False, "replay_infeasible", failure_reason=first["constraint"],
                            first_failure_timestamp=first["timestamp"], violation=first["violation"],
                            diagnostics={"violations": violations})
    grid = loads.to_numpy() + plan["charge_kw"].to_numpy() - plan["discharge_kw"].to_numpy()
    # 超额放电已在上方返回；这里只消除容差内的浮点残差。
    grid = np.maximum(grid, 0.0)
    return ReplayResult(True, "valid", float(np.dot(price, grid)),
                        diagnostics={"actual_grid_kw": grid.tolist()})
