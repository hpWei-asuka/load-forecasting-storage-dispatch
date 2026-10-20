"""用人工分钟数据检查清洗和单位换算。"""

import numpy as np
import pandas as pd
import pytest

from load_storage.data import DataUnavailable, evaluation_day, load_hourly


def write_minutes(tmp_path, values, start="2010-01-01", name="minute.txt"):
    index = pd.date_range(start, periods=len(values), freq="min")
    frame = pd.DataFrame(
        {
            "Date": index.strftime("%d/%m/%Y"),
            "Time": index.strftime("%H:%M:%S"),
            "Global_active_power": values,
        }
    )
    path = tmp_path / name
    frame.to_csv(path, sep=";", index=False)
    return path, frame


def test_minute_power_is_hourly_mean_not_sum(tmp_path):
    path, _ = write_minutes(tmp_path, [1.0] * 60)
    result = load_hourly(path)
    hour = result.hourly.iloc[0]
    assert hour["load_kw"] == 1.0
    assert hour["load_kwh"] == 1.0
    assert hour["observed_count"] == 60
    assert hour["imputed_count"] == 0


def test_identical_duplicates_do_not_double_count(tmp_path):
    path, frame = write_minutes(tmp_path, [1.0] * 60)
    pd.concat([frame, frame.iloc[[10]]]).to_csv(path, sep=";", index=False)
    result = load_hourly(path)
    assert result.quality["duplicate_rows"] == 1
    assert result.hourly.iloc[0]["observed_count"] == 60
    assert result.hourly.iloc[0]["load_kwh"] == 1.0


def test_conflict_is_unavailable_and_a_fill_barrier(tmp_path):
    values = [1.0] * 60
    values[16] = "?"
    path, frame = write_minutes(tmp_path, values)
    conflicting = frame.iloc[[15]].copy()
    conflicting["Global_active_power"] = 2.0
    pd.concat([frame, conflicting]).to_csv(path, sep=";", index=False)
    result = load_hourly(path)
    assert result.quality["conflicting_timestamps"] == 1
    assert result.hourly.iloc[0]["observed_count"] == 58
    assert result.hourly.iloc[0]["imputed_count"] == 0
    assert np.isnan(result.hourly.iloc[0]["load_kw"])


def test_valid_and_missing_same_timestamp_is_a_conflict(tmp_path):
    path, frame = write_minutes(tmp_path, [1.0] * 60)
    missing = frame.iloc[[10]].copy()
    missing["Global_active_power"] = "?"
    pd.concat([frame, missing]).to_csv(path, sep=";", index=False)
    result = load_hourly(path)
    assert result.quality["conflicting_timestamps"] == 1
    assert result.hourly.iloc[0]["observed_count"] == 59
    assert result.hourly.iloc[0]["imputed_count"] == 0


def test_fill_is_forward_only_and_limited_to_five_minutes(tmp_path):
    values = [1.0] * 120
    values[0] = "?"
    values[50:56] = ["?"] * 6
    path, _ = write_minutes(tmp_path, values)
    result = load_hourly(path)
    assert result.hourly.iloc[0]["imputed_count"] == 5
    assert result.hourly.iloc[0]["missing_count"] == 2
    assert np.isnan(result.hourly.iloc[0]["load_kw"])
    assert result.hourly.iloc[1]["load_kw"] == 1.0


def test_fill_limit_does_not_reset_at_hour_boundaries(tmp_path):
    values = [1.0] * 120
    values[59:65] = ["?"] * 6
    path, _ = write_minutes(tmp_path, values)
    result = load_hourly(path)
    assert result.hourly.iloc[0]["imputed_count"] == 1
    assert result.hourly.iloc[1]["imputed_count"] == 4
    assert result.hourly.iloc[1]["missing_count"] == 1


def test_zero_fill_keeps_missing_and_missing_timestamp_is_gridded(tmp_path):
    path, frame = write_minutes(tmp_path, [1.0] * 60)
    frame.drop(index=10).to_csv(path, sep=";", index=False)
    result = load_hourly(path, 0)
    assert result.quality["missing_timestamp_minutes"] == 1
    assert result.hourly.iloc[0]["observed_count"] == 59
    assert result.hourly.iloc[0]["imputed_count"] == 0
    assert np.isnan(result.hourly.iloc[0]["load_kwh"])


def test_negative_infinite_and_unparseable_values_are_not_observations(tmp_path):
    values = [1.0] * 60
    values[10], values[20], values[30] = -2, "inf", "broken"
    path, _ = write_minutes(tmp_path, values)
    result = load_hourly(path)
    assert result.quality["negative_power_rows"] == 1
    assert result.quality["nonfinite_power_rows"] == 1
    assert result.quality["unparseable_or_missing_power_rows"] == 1
    assert result.hourly.iloc[0]["observed_count"] == 57
    assert result.hourly.iloc[0]["imputed_count"] == 3
    assert result.hourly.iloc[0]["load_kw"] == 1.0


def test_spikes_are_flagged_and_retained(tmp_path):
    values = [1.0] * 60
    values[10] = 20.0
    path, _ = write_minutes(tmp_path, values)
    result = load_hourly(path)
    assert result.quality["flagged_spike_minutes"] == 2
    assert result.hourly.iloc[0]["load_kw"] == pytest.approx(79 / 60)


def test_full_observed_day_is_accepted_and_imputed_day_rejected(tmp_path):
    path, frame = write_minutes(tmp_path, [1.0] * (24 * 60))
    good = load_hourly(path)
    assert len(evaluation_day(good.hourly, "2010-01-01")) == 24
    assert good.quality["complete_observed_days"] == 1
    frame["Global_active_power"] = frame["Global_active_power"].astype(object)
    frame.loc[100, "Global_active_power"] = "?"
    frame.to_csv(path, sep=";", index=False)
    bad = load_hourly(path)
    assert bad.hourly["load_kw"].notna().all()
    assert bad.quality["imputed_minutes"] == 1
    with pytest.raises(DataUnavailable, match="原始观测"):
        evaluation_day(bad.hourly, "2010-01-01")
    assert "2010-01-01" in bad.quality["excluded_dates"]


def test_partial_day_cannot_be_padded_into_evaluation(tmp_path):
    path, _ = write_minutes(tmp_path, [1.0] * 60, start="2010-01-01 12:00")
    with pytest.raises(DataUnavailable):
        evaluation_day(load_hourly(path).hourly, "2010-01-01")


def test_sorting_and_invalid_timestamps_are_recorded(tmp_path):
    path, frame = write_minutes(tmp_path, [1.0] * 60)
    bad = frame.iloc[[0]].copy()
    bad["Date"] = "99/99/2010"
    seconds = frame.iloc[[0]].copy()
    seconds["Time"] = "00:00:30"
    pd.concat([frame.iloc[::-1], bad, seconds]).to_csv(path, sep=";", index=False)
    result = load_hourly(path)
    assert result.quality["invalid_timestamp_rows"] == 2
    assert result.hourly.iloc[0]["load_kw"] == 1.0


@pytest.mark.parametrize("limit", [-1, 6, 1.5, True])
def test_fill_limit_is_validated(tmp_path, limit):
    path, _ = write_minutes(tmp_path, [1.0] * 60)
    with pytest.raises(ValueError, match="0 至 5"):
        load_hourly(path, limit)


def test_missing_fields_and_no_valid_timestamps_fail_clearly(tmp_path):
    path = tmp_path / "wrong.txt"
    path.write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="需要字段"):
        load_hourly(path)
    path.write_text("Date;Time;Global_active_power\nbad;time;1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="没有可解析"):
        load_hourly(path)
