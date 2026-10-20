"""命令行入口：生成计划后，再读取目标日真实负荷。"""

import argparse
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
import tempfile
import uuid

import numpy as np
import pandas as pd

from .backtest import replay_day
from .config import PROJECT_ROOT, SolverConfig, load_config
from .data import DataUnavailable, evaluation_day, load_hourly
from .forecast import ForecastUnavailable, forecast_day
from .optimization import check_solver, optimize_day
from .provenance import data_metadata, download_data, runtime_metadata, sha256_file, write_json
from .reporting import STRATEGIES, configure_plotting, plot_results, save_tables, summarize
from .strategies import fixed_rule_day, no_storage_day

PREDICTION_COLUMNS = ["date", "timestamp", "forecast_kw", "actual_kw"]
SCHEDULE_COLUMNS = ["date", "strategy", "timestamp", "charge_kw", "discharge_kw",
                    "energy_start_kwh", "energy_end_kwh", "planned_grid_kw",
                    "forecast_kw", "actual_kw", "actual_grid_kw", "plan_valid", "replay_valid"]


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _row(day, strategy, currency):
    return {"date": day, "strategy": strategy, "status": None,
            "solver_status": "not_applicable", "plan_status": "not_generated",
            "plan_valid": False, "replay_valid": False,
            "predicted_cost": None, "actual_cost": None, "savings": None, "savings_rate": None,
            "solve_seconds": 0.0, "failure_reason": None,
            "first_failure_timestamp": None, "violation": None, "currency": currency}


def _make_logger(output):
    logger = logging.getLogger(f"load_storage.{output.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in (logging.FileHandler(output / "run.log", encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def run_experiment(config):
    """运行实验，返回退出码和结果目录，保留失败记录。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = config.experiment.output_dir / f"{config.experiment.name}_{stamp}_{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    (output / "solver_logs").mkdir()
    # 从快照解析参数，避免运行期间的配置修改造成记录不一致。
    config_bytes = config.config_path.read_bytes()
    (output / "config.toml").write_bytes(config_bytes)
    from .config import load_config as parse_snapshot
    config = parse_snapshot(output / "config.toml", project_root=config.project_root)
    logger = _make_logger(output)
    metadata = runtime_metadata(config.project_root)
    metadata.update(run_id=output.name, started_at_utc=datetime.now(timezone.utc).isoformat(),
                    config_sha256=sha256_file(output / "config.toml"),
                    tariff_source=config.tariff.source, battery_source=config.battery.source,
                    currency=config.tariff.currency)
    try:
        write_json(output / "metadata.json", metadata)
        metadata["cbc"] = check_solver(config.solver)
        metadata["plotting"] = configure_plotting()
        if not metadata["plotting"]["ok"]:
            raise ValueError("绘图缺少中文字体，请运行 doctor 并安装 Microsoft YaHei 或 Noto Sans CJK SC")
        metadata["data"] = data_metadata(config.data.path, config.data.source_url)
        write_json(output / "metadata.json", metadata)
        logger.info("读取数据 %s", config.data.path)
        data = load_hourly(config.data.path, config.data.history_ffill_limit_minutes)
        days = pd.date_range(config.experiment.test_start, config.experiment.test_end, freq="D")
        cost_rows, prediction_frames, schedule_frames, date_quality, diagnostics = [], [], [], [], {}
        tolerance = config.validation.absolute_tolerance
        prices, battery = config.tariff.hourly_prices, config.battery

        for target in days:
            day = target.date().isoformat()
            records = {s: _row(day, s, config.tariff.currency) for s in STRATEGIES}
            forecast, forecast_error, plans = None, None, {}
            try:
                # 每日单独截取此前历史。
                forecast = forecast_day(data.hourly.loc[data.hourly.index < target].copy(), target)
                plans["no_storage"] = no_storage_day(forecast, prices, battery)
                plans["fixed_rule"] = fixed_rule_day(
                    forecast, prices, battery, config.fixed_rule.charge_hours,
                    config.fixed_rule.discharge_hours, tolerance=tolerance)
                plans["optimized"] = optimize_day(
                    forecast, prices, battery, config.solver, tolerance=tolerance,
                    log_path=output / "solver_logs" / f"{day}.log")
            except ForecastUnavailable as exc:
                forecast_error = str(exc)

            # 此时计划已确定，真实负荷仅用于回测。
            try:
                actual = evaluation_day(data.hourly, target)
                actual_error = None
            except DataUnavailable as exc:
                actual, actual_error = None, str(exc)
            date_quality.append({"date": day, "evaluation_eligible": actual is not None,
                                 "exclusion_reason": actual_error,
                                 "forecast_available": forecast is not None,
                                 "forecast_unavailable_reason": forecast_error})

            if actual is not None and forecast is not None:
                prediction = forecast.copy()
                prediction["actual_kw"] = actual["load_kw"]
                prediction["date"] = day
                prediction_frames.append(prediction.rename_axis("timestamp").reset_index()[PREDICTION_COLUMNS])

            for strategy, row in records.items():
                result = plans.get(strategy)
                if result is not None:
                    row.update(solver_status=result.status if strategy == "optimized" else "not_applicable",
                               plan_status=result.status, plan_valid=result.valid, predicted_cost=result.predicted_cost,
                               solve_seconds=result.solve_seconds)
                    diagnostics[f"{day}/{strategy}"] = result.diagnostics.copy()
                if actual is None:
                    row.update(status="data_excluded", failure_reason=actual_error)
                elif forecast is None:
                    if strategy == "no_storage":
                        # 无储能费用不依赖预测。
                        row.update(status="valid", plan_valid=True, replay_valid=True,
                                   actual_cost=float(np.dot(prices, actual["load_kw"])))
                    else:
                        row.update(status="forecast_unavailable", failure_reason=forecast_error)
                elif not result.valid:
                    status = "plan_invalid" if result.status in {"validation_failed", "invalid_input"} else "solve_failed"
                    row.update(status=status, failure_reason=result.failure_reason)
                else:
                    replay = replay_day(result.plan.copy(deep=True), actual["load_kw"].copy(),
                                        battery, prices, tolerance=tolerance)
                    row.update(status=replay.status, replay_valid=replay.valid,
                               actual_cost=replay.actual_cost, failure_reason=replay.failure_reason,
                               first_failure_timestamp=replay.first_failure_timestamp, violation=replay.violation)
                    diagnostics[f"{day}/{strategy}"]["replay"] = replay.diagnostics
                if result is not None and result.plan is not None:
                    frame = result.plan.copy(deep=True)
                    frame["date"], frame["strategy"] = day, strategy
                    frame["forecast_kw"] = forecast["forecast_kw"]
                    frame["actual_kw"] = actual["load_kw"] if actual is not None else np.nan
                    frame["actual_grid_kw"] = (frame["actual_kw"] + frame["charge_kw"] - frame["discharge_kw"]
                                                if row["replay_valid"] else np.nan)
                    frame["plan_valid"], frame["replay_valid"] = row["plan_valid"], row["replay_valid"]
                    schedule_frames.append(frame.rename_axis("timestamp").reset_index()[SCHEDULE_COLUMNS])
            baseline = records["no_storage"]["actual_cost"]
            for row in records.values():
                if row["replay_valid"] and baseline is not None:
                    row["savings"] = baseline - row["actual_cost"]
                    row["savings_rate"] = row["savings"] / baseline if baseline != 0 else None
                cost_rows.append(row)
            logger.info("%s %s", day, ", ".join(f"{s}={r['status']}" for s, r in records.items()))

        predictions = pd.concat(prediction_frames, ignore_index=True) if prediction_frames else pd.DataFrame(columns=PREDICTION_COLUMNS)
        schedule = pd.concat(schedule_frames, ignore_index=True) if schedule_frames else pd.DataFrame(columns=SCHEDULE_COLUMNS)
        costs = pd.DataFrame(cost_rows)
        summary = summarize(costs, predictions, [day.date().isoformat() for day in days], date_quality)
        summary["cost_unit"] = config.tariff.currency
        quality = {**data.quality, "test_date_screening": date_quality,
                   "selection_basis": "配置预先固定日期；仅按观测完整性筛选，不按收益筛选"}
        write_json(output / "data_quality.json", _json_safe(quality))
        write_json(output / "summary.json", _json_safe(summary))
        write_json(output / "diagnostics.json", _json_safe(diagnostics))
        used = data.hourly.loc[(data.hourly.index >= days[0] - pd.Timedelta(1, unit="D"))
                              & (data.hourly.index < days[-1] + pd.Timedelta(1, unit="D"))]
        save_tables(output, used, predictions, schedule, costs)
        plot_results(output, predictions, schedule, costs, summary, config.tariff.currency)
        metadata.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
                        status="completed" if summary["failed_days"] == 0 else "completed_with_failures")
        write_json(output / "metadata.json", metadata)
        logger.info("共同有效 %d/%d 天；失败 %d 天", summary["common_valid_days"], summary["requested_days"], summary["failed_days"])
        return (0 if summary["failed_days"] == 0 else 1), output
    except Exception as exc:
        logger.exception("实验失败：%s", exc)
        failure = {"status": "fatal_error", "error_type": type(exc).__name__, "failure_reason": str(exc)}
        write_json(output / "run_error.json", failure)
        metadata.update(status="fatal_error", completed_at_utc=datetime.now(timezone.utc).isoformat())
        write_json(output / "metadata.json", _json_safe(metadata))
        return 2, output
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def doctor(solver=None):
    import matplotlib.pyplot as plt
    result = {"python": sys.version.split()[0], "python_compatible": sys.version_info[:2] == (3, 12)}
    result["cbc"] = check_solver(solver or SolverConfig())
    result["plotting"] = configure_plotting()
    if not result["plotting"]["ok"]:
        raise ValueError("未找到支持中文和负号的字体；请安装 Microsoft YaHei 或 Noto Sans CJK SC")
    with tempfile.TemporaryDirectory(prefix="load-storage-plot-") as temporary:
        fig, axis = plt.subplots()
        axis.plot([-1, 0, 1], [-1, 0, 1])
        axis.set_title("负荷预测与电池费用 −1")
        image = Path(temporary) / "doctor.png"
        fig.savefig(image)
        plt.close(fig)
        result["plotting"]["rendered_bytes"] = image.stat().st_size
    if not result["python_compatible"]:
        raise ValueError("请使用 Python 3.12 并执行 uv sync --locked")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="用电负荷预测与储能调度优化（本地独立日实验）")
    commands = parser.add_subparsers(dest="command", required=True)
    health = commands.add_parser("doctor", help="实际验证 Python、CBC 求解和中文绘图")
    health.add_argument("--config", help="可选，检查配置中的独立 CBC 路径")
    run = commands.add_parser("run", help="从本地数据运行实验，不联网")
    run.add_argument("--config", default="configs/example.toml")
    download = commands.add_parser("download-data", help="明确下载 UCI 数据并保存来源；不覆盖已有文件")
    download.add_argument("--output", default="data/raw/household_power_consumption.txt")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            solver = load_config(args.config).solver if args.config else None
            import json
            print(json.dumps(doctor(solver), ensure_ascii=False, indent=2))
            return 0
        if args.command == "download-data":
            destination = Path(args.output)
            if not destination.is_absolute():
                destination = PROJECT_ROOT / destination
            result = download_data(destination)
            print(f"数据：{destination}\nSHA-256：{result['sha256']}")
            return 0
        code, output = run_experiment(load_config(args.config))
        print(f"结果目录：{output}")
        return code
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
