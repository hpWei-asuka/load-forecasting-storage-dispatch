"""24小时储能调度。功率在电网侧计量，电量在电池内部计量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
import re
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd
import pulp

from .config import BatteryConfig, SolverConfig


@dataclass
class PlanResult:
    status: str
    plan: pd.DataFrame | None = None
    predicted_cost: float | None = None
    solve_seconds: float = 0.0
    failure_reason: str | None = None
    diagnostics: dict = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.status in {"optimal", "valid"} and self.plan is not None


def load_series(load: pd.DataFrame | pd.Series) -> pd.Series:
    """Require a complete, uniquely aligned, nonnegative 24-hour day."""
    if isinstance(load, pd.DataFrame):
        column = next((name for name in ("forecast_kw", "actual_kw", "load_kw") if name in load), None)
        if column is None:
            raise ValueError("负荷表需要 forecast_kw、actual_kw 或 load_kw 列")
        series = load[column]
    else:
        series = load
    if not isinstance(series, pd.Series) or not isinstance(series.index, pd.DatetimeIndex):
        raise ValueError("负荷必须使用 DatetimeIndex")
    if len(series) != 24 or series.index.has_duplicates or series.index.tz is not None:
        raise ValueError("负荷需要当地钟表时间的唯一 24 小时")
    expected = pd.date_range(series.index[0].normalize(), periods=24, freq="h")
    if not series.index.equals(expected):
        raise ValueError("负荷必须按 00:00 至 23:00 连续对齐")
    values = np.asarray(series, dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("负荷必须是有限的非负 kW 数值")
    return pd.Series(values, index=series.index, name=series.name)


def price_array(prices) -> np.ndarray:
    values = np.asarray(prices, dtype=float)
    if values.shape != (24,) or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("必须提供 24 个有限的非负电价")
    return values


def make_plan(load, charge, discharge, battery: BatteryConfig) -> pd.DataFrame:
    series = load_series(load)
    charge, discharge = np.asarray(charge, dtype=float), np.asarray(discharge, dtype=float)
    energy = np.r_[battery.initial_energy_kwh,
                   battery.initial_energy_kwh + np.cumsum(
                       battery.charge_efficiency * charge - discharge / battery.discharge_efficiency)]
    return pd.DataFrame({
        "charge_kw": charge,
        "discharge_kw": discharge,
        "energy_start_kwh": energy[:-1],
        "energy_end_kwh": energy[1:],
        "planned_grid_kw": series.to_numpy() + charge - discharge,
    }, index=series.index.rename("timestamp"))


def validate_plan(
    plan: pd.DataFrame, load, battery: BatteryConfig, *, tolerance: float = 1e-6,
    prices=None, predicted_cost: float | None = None, check_grid_column: bool = True,
) -> list[dict]:
    """独立检查约束，返回违反项；空列表表示通过。

    违反量保留kW、kWh或费用单位，不舍入修正。回测时按真实负荷
    重算购电功率，因此可以关闭对计划购电列的检查。
    """
    violations: list[dict] = []

    def add(constraint, amount, timestamp=None):
        violations.append({"constraint": constraint,
                           "timestamp": None if timestamp is None else pd.Timestamp(timestamp).isoformat(),
                           "violation": float(amount) if math.isfinite(amount) else None})

    try:
        series = load_series(load)
        columns = ["charge_kw", "discharge_kw", "energy_start_kwh", "energy_end_kwh"]
        if not isinstance(plan, pd.DataFrame) or not plan.index.equals(series.index):
            raise ValueError("计划与负荷时间戳不一致")
        if not set(columns).issubset(plan.columns):
            raise ValueError("计划缺少充放电功率或首末电量列")
        values = plan[columns].to_numpy(dtype=float)
        if values.shape != (24, 4) or not np.isfinite(values).all():
            raise ValueError("计划必须包含 24 行有限数值")
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("容差必须为有限正数")
    except (ValueError, TypeError, KeyError) as exc:
        return [{"constraint": f"invalid_input: {exc}", "timestamp": None, "violation": None}]

    c, d, starts, ends = values.T
    loads = series.to_numpy()
    recomputed = battery.initial_energy_kwh
    for i, timestamp in enumerate(series.index):
        checks = {
            "charge_nonnegative_kw": -c[i],
            "discharge_nonnegative_kw": -d[i],
            "charge_power_limit_kw": c[i] - battery.max_charge_kw,
            "discharge_power_limit_kw": d[i] - battery.max_discharge_kw,
            "charge_discharge_exclusive_kw": min(c[i], d[i]),
            "discharge_exceeds_load_kw": d[i] - loads[i],
            "grid_nonnegative_kw": -(loads[i] + c[i] - d[i]),
            "energy_start_min_kwh": battery.min_energy_kwh - starts[i],
            "energy_start_max_kwh": starts[i] - battery.max_energy_kwh,
            "energy_end_min_kwh": battery.min_energy_kwh - ends[i],
            "energy_end_max_kwh": ends[i] - battery.max_energy_kwh,
            "energy_start_recomputed_kwh": abs(starts[i] - recomputed),
            "energy_conservation_kwh": abs(ends[i] - starts[i] - battery.charge_efficiency*c[i]
                                            + d[i]/battery.discharge_efficiency),
        }
        recomputed += battery.charge_efficiency*c[i] - d[i]/battery.discharge_efficiency
        checks["energy_end_recomputed_kwh"] = abs(ends[i] - recomputed)
        checks["recomputed_energy_min_kwh"] = battery.min_energy_kwh - recomputed
        checks["recomputed_energy_max_kwh"] = recomputed - battery.max_energy_kwh
        if check_grid_column and "planned_grid_kw" in plan:
            supplied_grid = float(plan["planned_grid_kw"].iloc[i])
            if not math.isfinite(supplied_grid):
                add("planned_grid_finite_kw", float("inf"), timestamp)
            else:
                checks["planned_grid_balance_kw"] = abs(supplied_grid - (loads[i] + c[i] - d[i]))
        for constraint, amount in checks.items():
            if amount > tolerance:
                add(constraint, amount, timestamp)
    for constraint, amount, timestamp in (
        ("initial_energy_kwh", abs(starts[0] - battery.initial_energy_kwh), series.index[0]),
        ("terminal_energy_kwh", abs(ends[-1] - battery.initial_energy_kwh), series.index[-1]),
        ("terminal_recomputed_energy_kwh", abs(recomputed - battery.initial_energy_kwh), series.index[-1]),
    ):
        if amount > tolerance:
            add(constraint, amount, timestamp)
    if prices is not None and predicted_cost is not None:
        try:
            recomputed_cost = float(np.dot(price_array(prices), loads+c-d))
            amount = abs(float(predicted_cost) - recomputed_cost)
            if not math.isfinite(float(predicted_cost)) or amount > tolerance:
                add("predicted_cost", amount if math.isfinite(amount) else float("inf"))
        except (ValueError, TypeError) as exc:
            violations.append({"constraint": f"invalid_cost: {exc}", "timestamp": None, "violation": None})
    return violations


def _cbc(solver: SolverConfig, log_path: Path):
    options = dict(msg=False, timeLimit=solver.time_limit_seconds, threads=solver.threads,
                   gapRel=0, gapAbs=0, logPath=str(log_path),
                   options=["randomSeed 1", "randomCbcSeed 1"])
    if solver.cbc_path is not None:
        command = pulp.COIN_CMD(path=str(solver.cbc_path), **options)
    else:
        command = pulp.PULP_CBC_CMD(**options)
    if not command.available():
        raise FileNotFoundError(f"CBC 不可用，请检查 solver.cbc_path 或重新执行 uv sync --locked: {command.path}")
    return command


def classify_termination(problem_status: int, solution_status: int, log_text: str) -> tuple[str, str | None]:
    """结合CBC日志判断最优性，避免把时限内的可行解当作最优解。"""
    log = log_text.lower()
    if "stopped on time" in log or "exiting on maximum time" in log or "result - stopped on time limit" in log:
        return "time_limit", "CBC 达到时限，未证明最优"
    if "within gap tolerance" in log:
        return "not_proven_optimal", "CBC 仅达到最优间隙容差，未证明精确最优"
    if problem_status == pulp.LpStatusInfeasible or "result - problem proven infeasible" in log or "problem is infeasible" in log:
        return "infeasible", "CBC 判定模型不可行"
    if problem_status == pulp.LpStatusUnbounded or "result - linear relaxation unbounded" in log or "problem is unbounded" in log:
        return "unbounded", "CBC 判定模型无界"
    proof = ("result - optimal solution found" in log or
             re.search(r"(?m)^optimal(?: - objective value| objective)\b", log) is not None)
    if problem_status == pulp.LpStatusOptimal and solution_status == pulp.LpSolutionOptimal and proof:
        return "optimal", None
    return "not_proven_optimal", "PuLP 状态、解状态和 CBC 日志未同时证明最优"


def _solve(problem, solver, log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = _cbc(solver, log_path)
    problem.solve(command)
    if log_path.exists():
        raw_log = log_path.read_bytes()
        try:
            log_text = raw_log.decode("utf-8")
        except UnicodeDecodeError:
            # Windows CBC的路径可能使用本地编码，日志统一存为UTF-8。
            log_text = raw_log.decode("mbcs" if sys.platform == "win32" else "utf-8", errors="replace")
        log_path.write_text(log_text, encoding="utf-8")
    else:
        log_text = ""
    status, reason = classify_termination(problem.status, problem.sol_status, log_text)
    return status, reason, {"problem_status": pulp.LpStatus.get(problem.status, str(problem.status)),
                            "solution_status": pulp.LpSolution.get(problem.sol_status, str(problem.sol_status)),
                            "cbc_path": str(command.path), "log_path": str(log_path)}


def check_solver(solver: SolverConfig | None = None) -> dict:
    """Check the executable and solve an integer problem with known optimum 2."""
    solver = SolverConfig() if solver is None else solver
    with tempfile.TemporaryDirectory(prefix="load-storage-doctor-") as temporary:
        log = Path(temporary) / "doctor.log"
        command = _cbc(solver, log)
        completed = subprocess.run([str(command.path), "-version"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        version_text = completed.stdout + completed.stderr
        version = re.search(r"Version:\s*([^\s]+)", version_text)
        problem = pulp.LpProblem("cbc_startup_check", pulp.LpMinimize)
        x = pulp.LpVariable("integer_x", lowBound=0, cat=pulp.LpInteger)
        problem += x
        problem += x >= 1.5
        status, reason, diagnostics = _solve(problem, solver, log)
        if status != "optimal" or x.value() is None or abs(x.value()-2.0) > 1e-6:
            raise RuntimeError(f"CBC 已知解自检失败: {status}: {reason}")
        return {"status": "ok", "path": str(command.path),
                "version": version.group(1) if version else "unknown", "objective": float(x.value()),
                "problem_status": diagnostics["problem_status"], "solution_status": diagnostics["solution_status"]}


def optimize_day(forecast, prices, battery: BatteryConfig, solver: SolverConfig, *,
                 tolerance: float = 1e-6, log_path: Path | None = None) -> PlanResult:
    started = time.perf_counter()
    try:
        loads = load_series(forecast)
        price = price_array(prices)
    except (TypeError, ValueError) as exc:
        return PlanResult("invalid_input", failure_reason=str(exc))
    problem = pulp.LpProblem("daily_storage_dispatch", pulp.LpMinimize)
    c = pulp.LpVariable.dicts("charge_kw", range(24), lowBound=0)
    d = pulp.LpVariable.dicts("discharge_kw", range(24), lowBound=0)
    g = pulp.LpVariable.dicts("grid_kw", range(24), lowBound=0)
    energy = pulp.LpVariable.dicts("energy_kwh", range(25), lowBound=battery.min_energy_kwh,
                                 upBound=battery.max_energy_kwh)
    mode = pulp.LpVariable.dicts("charge_mode", range(24), cat=pulp.LpBinary)
    problem += pulp.lpSum(float(price[t])*g[t] for t in range(24))
    problem += energy[0] == battery.initial_energy_kwh
    problem += energy[24] == battery.initial_energy_kwh
    for t in range(24):
        problem += g[t] == float(loads.iloc[t]) + c[t] - d[t]
        problem += c[t] <= battery.max_charge_kw * mode[t]
        problem += d[t] <= battery.max_discharge_kw * (1-mode[t])
        problem += d[t] <= float(loads.iloc[t])
        problem += energy[t+1] == energy[t] + battery.charge_efficiency*c[t] - d[t]/battery.discharge_efficiency
    try:
        if log_path is None:
            with tempfile.TemporaryDirectory(prefix="load-storage-solve-") as temporary:
                status, reason, diagnostics = _solve(problem, solver, Path(temporary)/"cbc.log")
                diagnostics["log_path"] = None
        else:
            status, reason, diagnostics = _solve(problem, solver, Path(log_path))
    except Exception as exc:
        return PlanResult("solver_error", solve_seconds=time.perf_counter()-started,
                          failure_reason=f"{type(exc).__name__}: {exc}")
    elapsed = time.perf_counter()-started
    if status != "optimal":
        return PlanResult(status, solve_seconds=elapsed, failure_reason=reason, diagnostics=diagnostics)
    try:
        # 保留求解器原值，交给约束检查处理浮点残差。
        plan = pd.DataFrame({
            "charge_kw": [c[t].value() for t in range(24)],
            "discharge_kw": [d[t].value() for t in range(24)],
            "energy_start_kwh": [energy[t].value() for t in range(24)],
            "energy_end_kwh": [energy[t+1].value() for t in range(24)],
            "planned_grid_kw": [g[t].value() for t in range(24)],
        }, index=loads.index.rename("timestamp"))
        predicted_cost = float(np.dot(price, plan["planned_grid_kw"].to_numpy(dtype=float)))
        violations = validate_plan(plan, loads, battery, tolerance=tolerance,
                                   prices=price, predicted_cost=predicted_cost)
        objective = pulp.value(problem.objective)
        if objective is not None and abs(float(objective)-predicted_cost) > tolerance:
            violations.append({"constraint": "solver_objective", "timestamp": None,
                               "violation": abs(float(objective)-predicted_cost)})
        if violations:
            diagnostics["violations"] = violations
            return PlanResult("validation_failed", solve_seconds=elapsed,
                              failure_reason="求解结果未通过独立约束检查", diagnostics=diagnostics)
        return PlanResult("optimal", plan, predicted_cost, elapsed, diagnostics=diagnostics)
    except (ValueError, TypeError, KeyError) as exc:
        return PlanResult("validation_failed", solve_seconds=elapsed,
                          failure_reason=f"无法读取或验证求解值: {exc}", diagnostics=diagnostics)
