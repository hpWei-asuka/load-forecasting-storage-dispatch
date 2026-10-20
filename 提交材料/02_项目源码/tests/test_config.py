from pathlib import Path

import pytest

from load_storage.config import load_config


CONFIG = Path(__file__).parents[1] / "configs" / "example.toml"


def test_example_config_has_valid_windows_and_experiment_labels():
    config = load_config(CONFIG)
    assert config.experiment.test_start.isoformat() == "2010-01-04"
    assert len(config.tariff.hourly_prices) == 24
    assert config.data.path.name == "household_power_consumption.txt"
    assert "实验情景" in config.tariff.source
    assert max(config.fixed_rule.charge_hours) < min(config.fixed_rule.discharge_hours)


def test_rejects_bad_tariff_and_invalid_fractional_window(tmp_path):
    text = CONFIG.read_text(encoding="utf-8")
    bad_tariff = tmp_path / "bad-tariff.toml"
    bad_tariff.write_text(text.replace("0.30, 0.30, 0.30", "-0.30, 0.30, 0.30", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="hourly_prices"):
        load_config(bad_tariff, project_root=CONFIG.parents[1])
    bad_window = tmp_path / "bad-window.toml"
    bad_window.write_text(text.replace("charge_hours = [0, 1, 2, 3, 4, 5, 6]",
                                       "charge_hours = [0, 1.5, 2, 3, 4, 5, 6]"), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed_rule.charge_hours"):
        load_config(bad_window, project_root=CONFIG.parents[1])
