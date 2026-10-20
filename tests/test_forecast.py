import numpy as np
import pandas as pd
import pytest

from load_storage.forecast import ForecastUnavailable, forecast_day, prediction_metrics


def history_frame():
    index = pd.date_range("2010-01-01", periods=48, freq="h", name="timestamp")
    return pd.DataFrame(
        {"load_kw": np.arange(48, dtype=float), "observed_count": 60, "imputed_count": 0},
        index=index,
    )


def test_yesterday_is_explicitly_aligned_by_date_and_hour():
    history = history_frame().iloc[::-1]
    forecast = forecast_day(history, "2010-01-03")
    assert forecast.index.equals(pd.date_range("2010-01-03", periods=24, freq="h", name="timestamp"))
    np.testing.assert_array_equal(forecast["forecast_kw"], np.arange(24, 48))


def test_missing_hour_is_not_replaced_with_an_earlier_day():
    history = history_frame().drop(pd.Timestamp("2010-01-02 10:00"))
    with pytest.raises(ForecastUnavailable):
        forecast_day(history, "2010-01-03")


def test_nan_and_incomplete_hour_cannot_be_forecast():
    history = history_frame()
    history.loc["2010-01-02 10:00", "load_kw"] = np.nan
    with pytest.raises(ForecastUnavailable):
        forecast_day(history, "2010-01-03")
    history = history_frame()
    history.loc["2010-01-02 10:00", "observed_count"] = 59
    with pytest.raises(ForecastUnavailable):
        forecast_day(history, "2010-01-03")


def test_causal_imputation_is_allowed_as_history():
    history = history_frame()
    history.loc["2010-01-02 10:00", ["observed_count", "imputed_count"]] = [59, 1]
    assert len(forecast_day(history, "2010-01-03")) == 24


def test_history_containing_target_or_future_data_is_rejected():
    history = history_frame()
    with pytest.raises(ValueError, match="未来数据"):
        forecast_day(history, "2010-01-02")


def test_changing_target_day_and_future_does_not_change_forecast():
    full = history_frame()
    target = pd.Timestamp("2010-01-02")
    before = forecast_day(full.loc[full.index < target], target)
    full.loc[full.index >= target, "load_kw"] = 10000.0
    after = forecast_day(full.loc[full.index < target], target)
    pd.testing.assert_frame_equal(before, after)


def test_duplicate_or_timezone_aware_history_is_rejected():
    history = history_frame()
    with pytest.raises(ValueError, match="重复"):
        forecast_day(pd.concat([history, history.iloc[[0]]]), "2010-01-03")
    history.index = history.index.tz_localize("UTC")
    with pytest.raises(ValueError, match="时区"):
        forecast_day(history, "2010-01-03")


def test_metric_values_and_empty_set():
    metrics = prediction_metrics([1, 2, 3], [2, 2, 1])
    assert metrics["sample_count"] == 3
    assert metrics["mae_kw"] == pytest.approx(1)
    assert metrics["rmse_kw"] == pytest.approx(np.sqrt(5 / 3))
    assert prediction_metrics([], []) == {"mae_kw": None, "rmse_kw": None, "sample_count": 0}


@pytest.mark.parametrize(
    "predicted,actual",
    [([1], [1, 2]), ([[1]], [[1]]), ([np.nan], [1]), ([1], [np.inf]), ([-1], [1])],
)
def test_metrics_reject_invalid_pairs(predicted, actual):
    with pytest.raises(ValueError):
        prediction_metrics(predicted, actual)
