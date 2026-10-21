"""从已保存的小时预测绘制诊断图，不重新拟合模型。"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "outputs" / ".mplconfig"))

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def read_predictions(path):
    data = pd.read_csv(path, encoding="utf-8-sig")
    required = {"date", "timestamp", "forecast_kw", "actual_kw"}
    if not required.issubset(data.columns) or data.empty:
        raise ValueError("预测表缺少必要字段或没有样本")
    values = data[["forecast_kw", "actual_kw"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("预测与观测必须是非负有限数值")
    if data["timestamp"].duplicated().any():
        raise ValueError("预测表存在重复时刻")
    data["error_kw"] = data["forecast_kw"] - data["actual_kw"]
    return data


def draw(data, output):
    available = {font.name for font in font_manager.fontManager.ttflist}
    font = next((name for name in ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei") if name in available), "DejaVu Sans")
    matplotlib.rcParams.update({
        "font.family": font, "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#8A959E", "axes.labelcolor": "#273640",
        "xtick.color": "#53616A", "ytick.color": "#53616A",
        "axes.unicode_minus": True, "pdf.fonttype": 42, "svg.fonttype": "none",
    })
    actual = data["actual_kw"].to_numpy()
    predicted = data["forecast_kw"].to_numpy()
    error = data["error_kw"].to_numpy()
    mae = float(np.abs(error).mean())
    rmse = float(np.sqrt((error**2).mean()))
    fig = plt.figure(figsize=(10.0, 4.4))
    grid = fig.add_gridspec(2, 3, width_ratios=[4, 0.7, 4], height_ratios=[0.65, 4],
                          left=0.075, right=0.97, bottom=0.18, top=0.96,
                          wspace=0.24, hspace=0.06)
    scatter = fig.add_subplot(grid[1, 0])
    top = fig.add_subplot(grid[0, 0], sharex=scatter)
    side = fig.add_subplot(grid[1, 1], sharey=scatter)
    histogram = fig.add_subplot(grid[:, 2])

    upper = float(np.ceil(max(actual.max(), predicted.max()) * 1.05 * 2) / 2)
    load_bins = np.linspace(0, upper, 15)
    scatter.scatter(actual, predicted, s=21, facecolors="none", edgecolors="#426F90",
                    linewidths=0.9, alpha=0.78)
    scatter.plot([0, upper], [0, upper], "--", color="#929AA0", linewidth=1.1,
                 label="预测等于真实值")
    scatter.set(xlim=(0, upper), ylim=(0, upper), xlabel="真实负荷（kW）", ylabel="预测负荷（kW）")
    scatter.grid(alpha=0.12)
    scatter.legend(loc="upper left", frameon=False, fontsize=8.5)
    top.hist(actual, bins=load_bins, color="#AABCC9", edgecolor="white", linewidth=0.6)
    side.hist(predicted, bins=load_bins, orientation="horizontal", color="#AABCC9",
              edgecolor="white", linewidth=0.6)
    for axis in (top, side):
        axis.set_axis_off()

    bins = np.linspace(-float(np.ceil(np.abs(error).max())), float(np.ceil(np.abs(error).max())), 19)
    histogram.hist(error, bins=bins, color="#7F9EB4", edgecolor="white", linewidth=0.8)
    histogram.axvline(0, color="#737E86", linewidth=1.2, linestyle="--")
    histogram.set(xlabel="预测误差：预测值 - 真实值（kW）", ylabel="小时数")
    histogram.grid(axis="y", alpha=0.12)
    histogram.text(0.97, 0.96, f"样本数：{len(data)}小时\nMAE：{mae:.3f} kW\nRMSE：{rmse:.3f} kW",
                   transform=histogram.transAxes, ha="right", va="top", fontsize=9,
                   linespacing=1.7)
    fig.text(0.32, 0.045, "（a）预测与真实负荷；边缘为各自分布", ha="center", fontsize=9)
    fig.text(0.79, 0.045, "（b）误差分布", ha="center", fontsize=9)
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".png", ".pdf", ".svg"):
        fig.savefig(output.with_suffix(suffix), dpi=240, bbox_inches="tight")
    plt.close(fig)
    return {"sample_count": len(data), "mae_kw": mae, "rmse_kw": rmse}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "figures/fig_prediction_diagnostics")
    args = parser.parse_args()
    data = read_predictions(args.input)
    metrics = draw(data, args.output)
    data.to_csv(args.output.with_name(args.output.name + "_data.csv"), index=False, encoding="utf-8-sig")
    record = {**metrics, "source_file": args.input.name,
              "source_sha256": sha256(args.input.read_bytes()).hexdigest(),
              "dates": sorted(data["date"].unique().tolist()),
              "error_definition": "forecast_kw - actual_kw"}
    args.output.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
