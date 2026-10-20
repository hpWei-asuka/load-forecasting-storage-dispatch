"""处理UCI分钟负荷数据；小时平均功率乘1小时得到电量。"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# 突变仅作标记，不删除原值；阈值在实验前固定。
SPIKE_CHANGE_KW = 5.0


class DataUnavailable(ValueError):
    """A requested day lacks a complete set of original measurements."""


@dataclass(frozen=True)
class DataResult:
    hourly: pd.DataFrame
    quality: dict


def _day_index(target_date) -> pd.DatetimeIndex:
    start = pd.Timestamp(target_date)
    if start.tzinfo is not None or start != start.normalize():
        raise ValueError("target_date 必须是不含时区的当地日历日期。")
    return pd.date_range(start, periods=24, freq="h", name="timestamp")


def evaluation_day(hourly: pd.DataFrame, target_date) -> pd.DataFrame:
    """Return a full observed day, refusing imputed or ambiguous measurements."""
    expected = _day_index(target_date)
    if not isinstance(hourly.index, pd.DatetimeIndex) or hourly.index.tz is not None:
        raise ValueError("小时负荷必须使用不含时区的 DatetimeIndex。")
    if not hourly.index.is_unique:
        raise DataUnavailable("小时表时间戳不唯一。")
    required = {"load_kw", "observed_count", "imputed_count"}
    if not required.issubset(hourly.columns):
        raise ValueError(f"小时表缺少字段：{sorted(required - set(hourly.columns))}")
    selected = hourly.reindex(expected)
    observed = selected["observed_count"].eq(60)
    imputed = selected["imputed_count"].eq(0)
    load = pd.to_numeric(selected["load_kw"], errors="coerce")
    finite_load = pd.Series(np.isfinite(load) & load.ge(0), index=expected)
    if not bool((observed & imputed & finite_load).all()):
        bad = expected[~(observed & imputed & finite_load)]
        raise DataUnavailable(
            f"{expected[0].date()} 缺少完整原始观测：{len(bad)} 个小时不合格，"
            f"首个时段 {bad[0].isoformat()}；每小时须有 60 个原始有效分钟且无填补。"
        )
    return selected.copy()


def load_hourly(path: Path, history_ffill_limit_minutes: int = 5) -> DataResult:
    """读取本地数据并聚合到小时，冲突记录阻断前向填补。"""
    if (
        isinstance(history_ffill_limit_minutes, bool)
        or not isinstance(history_ffill_limit_minutes, int)
        or not 0 <= history_ffill_limit_minutes <= 5
    ):
        raise ValueError("history_ffill_limit_minutes 必须是 0 至 5 的整数。")
    path = Path(path)
    fields = ["Date", "Time", "Global_active_power"]
    try:
        raw = pd.read_csv(path, sep=";", usecols=fields, dtype=str, keep_default_na=False)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, ValueError) as exc:
        raise ValueError(f"无法读取 UCI 分号文件 {path}，需要字段 {fields}：{exc}") from exc
    if raw.empty:
        raise ValueError(f"数据文件没有记录：{path}")
    source_rows = len(raw)
    duplicate_rows = int(raw.duplicated(subset=fields).sum())
    raw = raw.drop_duplicates(subset=fields).copy()
    timestamp = pd.to_datetime(
        raw["Date"].str.strip() + " " + raw["Time"].str.strip(),
        format="%d/%m/%Y %H:%M:%S",
        errors="coerce",
    )
    valid_time = timestamp.notna() & timestamp.eq(timestamp.dt.floor("min"))
    invalid_timestamp_rows = int((~valid_time).sum())
    numeric = pd.to_numeric(raw["Global_active_power"].str.strip(), errors="coerce")
    missing_numeric = numeric.isna()
    nonfinite_numeric = ~np.isfinite(numeric) & ~missing_numeric
    negative_numeric = numeric.lt(0) & ~missing_numeric
    invalid_numeric = missing_numeric | nonfinite_numeric | negative_numeric

    frame = pd.DataFrame(
        {"timestamp": timestamp.loc[valid_time], "raw_kw": numeric.loc[valid_time]}
    )
    if frame.empty:
        raise ValueError(f"数据文件没有可解析的整分钟时间戳：{path}")

    # 缺失值也参与冲突判断，不能只挑同一时刻的有效记录。
    grouped = frame.groupby("timestamp", sort=True)["raw_kw"]
    conflicting = grouped.nunique(dropna=False).gt(1)
    readings = grouped.first()
    readings = readings.mask(~np.isfinite(readings) | readings.lt(0) | conflicting)
    start = readings.index[0].floor("h")
    stop = readings.index[-1].floor("h") + pd.Timedelta(59, unit="min")
    minute_index = pd.date_range(start, stop, freq="min", name="timestamp")
    original = readings.reindex(minute_index)
    conflicts = conflicting.reindex(minute_index, fill_value=False)
    original_observed = original.notna()

    if history_ffill_limit_minutes:
        if conflicts.any():
            # 前值不能跨过冲突时刻继续填补。
            cleaned = original.groupby(conflicts.cumsum()).ffill(limit=history_ffill_limit_minutes)
        else:
            cleaned = original.ffill(limit=history_ffill_limit_minutes)
        cleaned = cleaned.mask(conflicts)
    else:
        cleaned = original.copy()
    imputed = cleaned.notna() & ~original_observed
    counts = cleaned.resample("h").count()
    hourly = pd.DataFrame(
        {
            "load_kw": cleaned.resample("h").mean().where(counts.eq(60)),
            "observed_count": original_observed.resample("h").sum().astype(int),
            "imputed_count": imputed.resample("h").sum().astype(int),
            "conflict_count": conflicts.resample("h").sum().astype(int),
        }
    )
    hourly.insert(1, "load_kwh", hourly["load_kw"] * 1.0)
    hourly.index.name = "timestamp"
    hourly["missing_count"] = 60 - hourly["observed_count"] - hourly["imputed_count"]

    excluded_dates = {}
    for day in pd.date_range(start.normalize(), stop.normalize(), freq="D"):
        try:
            evaluation_day(hourly, day)
        except DataUnavailable as exc:
            excluded_dates[str(day.date())] = str(exc)
    spike_count = int(original.diff().abs().gt(SPIKE_CHANGE_KW).sum())
    quality = {
        "source_rows": source_rows,
        "duplicate_rows": duplicate_rows,
        "duplicate_definition": "Date、Time、Global_active_power 三个读取字段完全相同",
        "invalid_timestamp_rows": invalid_timestamp_rows,
        "unparseable_or_missing_power_rows": int(missing_numeric.sum()),
        "nonfinite_power_rows": int(nonfinite_numeric.sum()),
        "negative_power_rows": int(negative_numeric.sum()),
        "invalid_power_rows": int(invalid_numeric.sum()),
        "conflicting_timestamps": int(conflicting.sum()),
        "conflict_timestamps_sample": [str(t) for t in conflicting[conflicting].index[:20]],
        "missing_timestamp_minutes": int(len(minute_index) - len(readings)),
        "observed_minutes": int(original_observed.sum()),
        "imputed_minutes": int(imputed.sum()),
        "unavailable_minutes_after_fill": int(cleaned.isna().sum()),
        "history_ffill_limit_minutes": history_ffill_limit_minutes,
        "spike_change_threshold_kw": SPIKE_CHANGE_KW,
        "flagged_spike_minutes": spike_count,
        "spike_action": "仅标记，不删除或修改功率",
        "time_basis": "源数据当地钟表时间；不推断时区",
        "first_timestamp": str(minute_index[0]),
        "last_timestamp": str(minute_index[-1]),
        "hour_count": len(hourly),
        "complete_observed_days": int((stop.normalize() - start.normalize()).days + 1 - len(excluded_dates)),
        "excluded_date_count": len(excluded_dates),
        "excluded_dates": excluded_dates,
        "units": {"load_kw": "kW", "load_kwh": "kWh", "time_step_hours": 1},
    }
    return DataResult(hourly=hourly, quality=quality)
