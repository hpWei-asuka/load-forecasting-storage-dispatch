"""使用UCI格式的人工数据验证离线流程。"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from load_storage import cli
from load_storage.config import PROJECT_ROOT, load_config


@pytest.fixture
def experiment(tmp_path):
    index = pd.date_range("2010-01-01", periods=3 * 24 * 60, freq="min")
    raw = pd.DataFrame({
        "Date": index.strftime("%d/%m/%Y"),
        "Time": index.strftime("%H:%M:%S"),
        "Global_active_power": np.ones(len(index)),
    })
    data_path = tmp_path / "artificial_minutes.txt"
    raw.to_csv(data_path, sep=";", index=False)
    config_path = tmp_path / "test.toml"
    text = (PROJECT_ROOT / "configs" / "example.toml").read_text(encoding="utf-8")
    text = text.replace('test_start = "2010-01-04"', 'test_start = "2010-01-02"')
    text = text.replace('test_end = "2010-01-10"', 'test_end = "2010-01-03"')
    text = text.replace('output_dir = "outputs"', f'output_dir = "{(tmp_path / "outputs").as_posix()}"')
    text = text.replace('path = "data/raw/household_power_consumption.txt"', f'path = "{data_path.as_posix()}"')
    text = text.replace(
        'source_url = "https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption"',
        'source_url = "https://example.invalid/artificial-test-data"',
    )
    config_path.write_text(text, encoding="utf-8")
    return config_path, data_path, raw


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(config_path):
    return cli.run_experiment(load_config(config_path))


def test_offline_pipeline_exports_reproducible_tables_and_figures(experiment):
    config_path, _, _ = experiment
    first_code, first = run(config_path)
    second_code, second = run(config_path)
    assert first_code == second_code == 0
    assert first != second
    for output in (first, second):
        for filename in (
            "config.toml", "metadata.json", "data_quality.json", "summary.json",
            "diagnostics.json", "run.log", "hourly.csv", "predictions.csv", "schedule.csv",
            "cost_comparison.csv", "solver_logs/2010-01-02.log", "solver_logs/2010-01-03.log",
        ):
            assert (output / filename).stat().st_size > 0, filename
        for stem in ("prediction_comparison", "cost_comparison", "schedule_2010-01-02", "schedule_2010-01-03"):
            for suffix in ("png", "svg"):
                image = output / "figures" / f"{stem}.{suffix}"
                assert image.stat().st_size > 1000
                if suffix == "png":
                    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                else:
                    assert "<svg" in image.read_text(encoding="utf-8")
        for filename in ("hourly.csv", "predictions.csv", "schedule.csv", "cost_comparison.csv"):
            assert (output / filename).read_bytes().startswith(b"\xef\xbb\xbf")
        metadata = read_json(output / "metadata.json")
        assert metadata["status"] == "completed"
        assert metadata["cbc"]["objective"] == 2
        assert len(metadata["data"]["sha256"]) == 64
        assert len(metadata["config_sha256"]) == 64
        assert metadata["data"]["declared_source_url"] == "https://example.invalid/artificial-test-data"
        costs = pd.read_csv(output / "cost_comparison.csv")
        assert len(costs) == 6
        assert set(costs["status"]) == {"valid"}
        assert costs["actual_cost"].notna().all()
        summary = read_json(output / "summary.json")
        assert summary["common_valid_days"] == 2
        assert summary["forecast"]["overall"] == {"mae_kw": 0.0, "rmse_kw": 0.0, "sample_count": 48}
        assert summary["cost_totals_on_common_dates"]["no_storage"]["actual_cost"] == pytest.approx(28.6)
    for filename in ("predictions.csv", "schedule.csv"):
        pd.testing.assert_frame_equal(pd.read_csv(first / filename), pd.read_csv(second / filename), atol=1e-6, rtol=0)
    first_costs = pd.read_csv(first / "cost_comparison.csv").drop(columns="solve_seconds")
    second_costs = pd.read_csv(second / "cost_comparison.csv").drop(columns="solve_seconds")
    pd.testing.assert_frame_equal(first_costs, second_costs, atol=1e-6, rtol=0)
    assert read_json(first / "summary.json") == read_json(second / "summary.json")


def test_changed_target_observations_do_not_change_frozen_target_plan(experiment, monkeypatch):
    config_path, data_path, raw = experiment
    # 绘图已由前一测试覆盖，这里只检查未来观测是否影响计划。
    monkeypatch.setattr(cli, "plot_results", lambda *args: None)
    code, before = run(config_path)
    assert code == 0
    raw.loc[24 * 60:, "Global_active_power"] = 2.0
    raw.to_csv(data_path, sep=";", index=False)
    code, after = run(config_path)
    assert code == 0
    before_plan = pd.read_csv(before / "schedule.csv")
    after_plan = pd.read_csv(after / "schedule.csv")
    plan_columns = ["strategy", "timestamp", "charge_kw", "discharge_kw", "energy_start_kwh", "energy_end_kwh", "planned_grid_kw", "forecast_kw"]
    left = before_plan.loc[before_plan["date"] == "2010-01-02", plan_columns].reset_index(drop=True)
    right = after_plan.loc[after_plan["date"] == "2010-01-02", plan_columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, atol=1e-6, rtol=0)
    predictions = pd.read_csv(after / "predictions.csv")
    target = predictions.loc[predictions["date"] == "2010-01-02"]
    assert target["forecast_kw"].eq(1.0).all()
    assert target["actual_kw"].eq(2.0).all()


def test_missing_original_measurement_excludes_all_three_costs(experiment, monkeypatch):
    config_path, data_path, raw = experiment
    monkeypatch.setattr(cli, "plot_results", lambda *args: None)
    raw["Global_active_power"] = raw["Global_active_power"].astype(object)
    raw.loc[24 * 60 + 100, "Global_active_power"] = "?"
    raw.to_csv(data_path, sep=";", index=False)
    code, output = run(config_path)
    assert code == 1
    costs = pd.read_csv(output / "cost_comparison.csv")
    excluded = costs.loc[costs["date"] == "2010-01-02"]
    assert len(excluded) == 3
    assert set(excluded["status"]) == {"data_excluded"}
    assert excluded[["actual_cost", "savings", "savings_rate"]].isna().all().all()
    assert not excluded["replay_valid"].any()
    summary = read_json(output / "summary.json")
    assert summary["data_excluded_days"] == 1
    assert summary["common_valid_dates"] == ["2010-01-03"]
    assert summary["forecast"]["overall"]["sample_count"] == 24
    quality = read_json(output / "data_quality.json")
    assert quality["imputed_minutes"] == 1
    assert not quality["test_date_screening"][0]["evaluation_eligible"]


def test_missing_yesterday_keeps_actual_baseline_but_records_forecast_failure(experiment, monkeypatch):
    config_path, data_path, raw = experiment
    monkeypatch.setattr(cli, "plot_results", lambda *args: None)
    raw.iloc[24 * 60:].to_csv(data_path, sep=";", index=False)
    code, output = run(config_path)
    assert code == 1
    costs = pd.read_csv(output / "cost_comparison.csv")
    day = costs.loc[costs["date"] == "2010-01-02"].set_index("strategy")
    assert day.loc["no_storage", "status"] == "valid"
    assert day.loc["no_storage", "actual_cost"] == pytest.approx(14.3)
    for strategy in ("fixed_rule", "optimized"):
        assert day.loc[strategy, "status"] == "forecast_unavailable"
        assert pd.isna(day.loc[strategy, "actual_cost"])
        assert "前一日" in day.loc[strategy, "failure_reason"]
    summary = read_json(output / "summary.json")
    assert summary["forecast_unavailable_days"] == 1
    assert summary["common_valid_dates"] == ["2010-01-03"]


def test_missing_cbc_returns_nonzero_and_preserves_fatal_diagnostics(experiment, capsys):
    config_path, _, _ = experiment
    text = config_path.read_text(encoding="utf-8")
    missing = (config_path.parent / "missing-cbc.exe").as_posix()
    text = text.replace("# cbc_path = \"C:/path/to/cbc.exe\"", f'cbc_path = "{missing}"')
    config_path.write_text(text, encoding="utf-8")
    code = cli.main(["run", "--config", str(config_path)])
    assert code == 2
    assert "结果目录" in capsys.readouterr().out
    outputs = list((config_path.parent / "outputs").iterdir())
    assert len(outputs) == 1
    error = read_json(outputs[0] / "run_error.json")
    assert error["status"] == "fatal_error"
    assert "CBC" in error["failure_reason"]
    assert read_json(outputs[0] / "metadata.json")["status"] == "fatal_error"
    assert not (outputs[0] / "cost_comparison.csv").exists()
