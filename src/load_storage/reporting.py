"""汇总共同有效日期的费用，导出表格和图表。"""

from pathlib import Path
import math

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
from matplotlib.ft2font import FT2Font
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .forecast import prediction_metrics

STRATEGIES = ("no_storage", "fixed_rule", "optimized")
LABELS = {"no_storage": "无储能", "fixed_rule": "固定规则", "optimized": "预测驱动优化"}


def configure_plotting() -> dict:
    required = {ord(char) for char in "负荷预测电池费用−"}
    candidates = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"]
    for name in candidates:
        try:
            path = font_manager.findfont(name, fallback_to_default=False)
        except ValueError:
            continue
        missing = required - set(FT2Font(path).get_charmap())
        if not missing:
            plt.rcParams.update({"font.family": name, "axes.unicode_minus": True,
                                 "figure.dpi": 120, "savefig.dpi": 160})
            return {"font_family": name, "font_path": path, "missing_glyphs": [], "ok": True}
    return {"font_family": "DejaVu Sans", "ok": False, "missing_glyphs": sorted(required)}


def summarize(costs: pd.DataFrame, predictions: pd.DataFrame, requested_dates: list[str],
              date_quality: list[dict] | None = None) -> dict:
    common = []
    for day in requested_dates:
        rows = costs[costs["date"] == day]
        if (set(rows["strategy"]) == set(STRATEGIES) and len(rows) == 3
                and rows["replay_valid"].all() and rows["actual_cost"].notna().all()):
            common.append(day)
    totals = None
    if common:
        selected = costs[costs["date"].isin(common)]
        totals = {strategy: float(selected.loc[selected["strategy"] == strategy, "actual_cost"].sum())
                  for strategy in STRATEGIES}
    base = totals["no_storage"] if totals else None
    financial = None if totals is None else {
        strategy: {"actual_cost": totals[strategy], "savings": base - totals[strategy],
                   "savings_rate": (base - totals[strategy]) / base if base != 0 else None}
        for strategy in STRATEGIES
    }
    daily_metrics = []
    if not predictions.empty:
        for day, values in predictions.groupby("date", sort=True):
            daily_metrics.append({"date": day, **prediction_metrics(values["forecast_kw"], values["actual_kw"])})
        overall_metrics = prediction_metrics(predictions["forecast_kw"], predictions["actual_kw"])
    else:
        overall_metrics = {"mae_kw": None, "rmse_kw": None, "sample_count": 0}
    excluded = sorted(set(costs.loc[costs["status"] == "data_excluded", "date"]))
    # 历史和真实观测可能同时缺失，两类原因分别计数。
    unavailable = (sorted(r["date"] for r in date_quality if not r["forecast_available"])
                   if date_quality is not None else
                   sorted(set(costs.loc[costs["status"] == "forecast_unavailable", "date"])))
    failed_rows = costs.loc[costs["status"] != "valid"]
    failures = []
    for row in failed_rows.to_dict("records"):
        failures.append({key: (None if isinstance(value, float) and math.isnan(value) else value)
                         for key, value in row.items()})
    return {
        "requested_days": len(requested_dates), "requested_dates": requested_dates,
        "data_excluded_days": len(excluded), "data_excluded_dates": excluded,
        "forecast_unavailable_days": len(unavailable), "forecast_unavailable_dates": unavailable,
        "common_valid_days": len(common), "common_valid_dates": common,
        "coverage_rate": len(common) / len(requested_dates) if requested_dates else 0,
        "cost_totals_on_common_dates": financial,
        "forecast": {"valid_dates": [r["date"] for r in daily_metrics],
                     "daily": daily_metrics, "overall": overall_metrics},
        "strategy_failures": {
            strategy: {
                "solve_failed_days": int(((costs["strategy"] == strategy) & (
                    costs["solver_status"].isin(["solver_error", "time_limit", "infeasible", "unbounded", "not_proven_optimal"])
                    if "solver_status" in costs else costs["status"].eq("solve_failed"))).sum()),
                "plan_invalid_days": int(((costs["strategy"] == strategy) & (
                    costs["plan_status"].isin(["validation_failed", "invalid_input"])
                    if "plan_status" in costs else costs["status"].eq("plan_invalid"))).sum()),
                "replay_infeasible_days": int(((costs["strategy"] == strategy) & (costs["status"] == "replay_infeasible")).sum()),
            } for strategy in STRATEGIES
        },
        "failed_days": len(set(failed_rows["date"])), "failures": failures,
        "cost_unit": "tariff.currency", "power_unit": "kW", "energy_unit": "kWh",
        "comparison_basis": "仅三策略共同有效日期；总费用计算整体节省率",
        "scope": "独立日实验；仅购电运行费用；不包含设备投资与老化成本",
    }


def _save(fig, destination: Path):
    fig.savefig(destination.with_suffix(".png"), bbox_inches="tight")
    fig.savefig(destination.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)


def save_tables(output: Path, hourly, predictions, schedule, costs):
    hourly.to_csv(output / "hourly.csv", encoding="utf-8-sig", index_label="timestamp")
    predictions.to_csv(output / "predictions.csv", encoding="utf-8-sig", index=False)
    schedule.to_csv(output / "schedule.csv", encoding="utf-8-sig", index=False)
    costs.to_csv(output / "cost_comparison.csv", encoding="utf-8-sig", index=False)


def plot_results(output: Path, predictions: pd.DataFrame, schedule: pd.DataFrame,
                 costs: pd.DataFrame, summary: dict, currency: str):
    figures = output / "figures"
    figures.mkdir(exist_ok=True)
    font = configure_plotting()
    if not font["ok"]:
        raise ValueError("未找到支持中文及负号的字体；请安装 Microsoft YaHei 或 Noto Sans CJK SC")
    fig, axis = plt.subplots(figsize=(11, 4), layout="constrained")
    if predictions.empty:
        axis.text(.5, .5, "无有效预测评估日期", ha="center", transform=axis.transAxes)
    else:
        # 缺失日期在曲线上留空。
        values = predictions.copy()
        values["timestamp"] = pd.to_datetime(values["timestamp"])
        values = values.set_index("timestamp").reindex(pd.date_range(
            values["timestamp"].min(), values["timestamp"].max(), freq="h"))
        axis.plot(values.index, values["actual_kw"], label="真实负荷", linewidth=1.2)
        axis.plot(values.index, values["forecast_kw"], label="前一日同小时预测", linewidth=1.0, alpha=.8)
        axis.legend()
    axis.set(title="负荷预测与原始观测对比", ylabel="功率 (kW)", xlabel="当地钟表时间")
    axis.grid(alpha=.2)
    _save(fig, figures / "prediction_comparison")

    for day, rows in schedule.groupby("date", sort=True):
        fig, axes = plt.subplots(2, 2, figsize=(12, 6), layout="constrained", squeeze=False)
        for col, strategy in enumerate(("fixed_rule", "optimized")):
            values = rows.loc[rows["strategy"] == strategy]
            cost_row = costs.loc[(costs["date"] == day) & (costs["strategy"] == strategy)].iloc[0]
            title = f"{day} {LABELS[strategy]} · " + ("有效" if cost_row["replay_valid"] else "无效/未执行")
            if values.empty:
                axes[0, col].text(.5, .5, str(cost_row["status"]), ha="center", transform=axes[0, col].transAxes)
            else:
                hours = np.arange(24)
                axes[0, col].bar(hours, values["charge_kw"], label="充电", color="#477fa8")
                axes[0, col].bar(hours, -values["discharge_kw"], label="放电 (负方向)", color="#bd7840")
                energy = np.r_[values["energy_start_kwh"].iloc[0], values["energy_end_kwh"].to_numpy()]
                axes[1, col].step(np.arange(25), energy, where="post", color="#477fa8")
                axes[0, col].legend()
            axes[0, col].set(title=title, ylabel="功率 (kW)")
            axes[1, col].set(xlabel="小时", ylabel="电池电量 (kWh)", xlim=(0, 24))
            for row in range(2):
                axes[row, col].grid(axis="y", alpha=.2)
        _save(fig, figures / f"schedule_{day}")

    fig, axis = plt.subplots(figsize=(7, 4), layout="constrained")
    totals = summary["cost_totals_on_common_dates"]
    if totals is None:
        axis.text(.5, .5, "共同有效日期为 0，不报告整体收益", ha="center", transform=axis.transAxes)
        axis.set_xticks([])
    else:
        bars = axis.bar([LABELS[s] for s in STRATEGIES], [totals[s]["actual_cost"] for s in STRATEGIES],
                        color=["#9a9a9a", "#b6905f", "#477fa8"])
        axis.bar_label(bars, fmt="%.3f", padding=3)
        axis.margins(y=.15)
    axis.set(title=f"实际购电费用 · 共同有效 {summary['common_valid_days']}/{summary['requested_days']} 天",
             ylabel=f"实际费用 ({currency})")
    _save(fig, figures / "cost_comparison")
    return font
