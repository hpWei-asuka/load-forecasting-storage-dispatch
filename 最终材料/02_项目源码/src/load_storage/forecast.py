"""Yesterday's same-hour baseline with an explicit information boundary."""

import numpy as np
import pandas as pd

from .data import _day_index


class ForecastUnavailable(ValueError):
    """Yesterday does not contain 24 usable hourly values."""


def forecast_day(history: pd.DataFrame, target_date) -> pd.DataFrame:
    """Predict D from calendar day D-1; never use 'last 24 available rows'."""
    expected = _day_index(target_date)
    cutoff = expected[0]
    if not isinstance(history.index, pd.DatetimeIndex) or history.index.tz is not None:
        raise ValueError("预测历史必须使用不含时区的 DatetimeIndex。")
    if not history.index.is_unique:
        raise ValueError("预测历史包含重复时间戳。")
    if bool((history.index >= cutoff).any()):
        raise ValueError("预测历史包含目标日或未来数据；必须先在目标日 00:00 截断。")
    if "load_kw" not in history:
        raise ValueError("预测历史缺少 load_kw 字段。")
    yesterday = expected - pd.Timedelta(1, unit="D")
    selected = history.reindex(yesterday)
    values = pd.to_numeric(selected["load_kw"], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ForecastUnavailable(f"{cutoff.date()} 的前一日没有 24 个合格的同小时负荷。")
    if {"observed_count", "imputed_count"}.issubset(selected.columns):
        if not (selected["observed_count"] + selected["imputed_count"]).eq(60).all():
            raise ForecastUnavailable(f"{cutoff.date()} 的前一日存在不足 60 个有效分钟的小时。")
    return pd.DataFrame({"forecast_kw": values.copy()}, index=expected)


def prediction_metrics(predicted, actual) -> dict:
    """Compute metrics over the supplied observed evaluation set, in kW."""
    predicted_values = np.asarray(predicted, dtype=float)
    actual_values = np.asarray(actual, dtype=float)
    if (
        predicted_values.ndim != 1
        or actual_values.ndim != 1
        or predicted_values.shape != actual_values.shape
    ):
        raise ValueError("预测与真实负荷必须是形状一致的一维序列。")
    if len(predicted_values) == 0:
        return {"mae_kw": None, "rmse_kw": None, "sample_count": 0}
    if (
        not np.isfinite(predicted_values).all()
        or not np.isfinite(actual_values).all()
        or (predicted_values < 0).any()
        or (actual_values < 0).any()
    ):
        raise ValueError("预测指标只能使用有限且非负的完整预测与原始观测。")
    error = predicted_values - actual_values
    return {
        "mae_kw": float(np.mean(np.abs(error))),
        "rmse_kw": float(np.sqrt(np.mean(np.square(error)))),
        "sample_count": int(len(error)),
    }
