"""Cross-check the published report against its compact experimental records."""

import csv
from hashlib import sha256
import json
from pathlib import Path
import re
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/experiments"


def read_csv(name):
    with (EVIDENCE / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def summary():
    return json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))


def test_published_costs_use_common_valid_dates_and_keep_failures():
    records = read_csv("cost_comparison.csv")
    for scenario, experiment in summary()["scenarios"].items():
        rows = [row for row in records if row["scenario"] == scenario]
        dates = experiment["requested_dates"]
        assert len(rows) == len(dates) * 3
        assert len({(r["date"], r["strategy"]) for r in rows}) == len(rows)
        assert {r["date"] for r in rows} == set(dates)
        assert {r["strategy"] for r in rows} == {"no_storage", "fixed_rule", "optimized"}
        valid = [day for day in dates if all(
            r["replay_valid"] == "True" for r in rows if r["date"] == day)]
        assert valid == experiment["common_valid_dates"]
        assert experiment["coverage_rate"] == pytest.approx(len(valid) / len(dates))
        for row in rows:
            assert row["plan_valid"] == "True"
            if row["replay_valid"] == "False":
                assert row["actual_cost"] == ""
                assert row["failure_reason"] == "discharge_exceeds_load_kw"
                assert row["first_failure_timestamp"].startswith(row["date"])
            else:
                assert float(row["actual_cost"]) >= 0
        baseline = sum(float(r["actual_cost"]) for r in rows
                       if r["strategy"] == "no_storage" and r["date"] in valid)
        for strategy, totals in experiment["cost_totals_on_common_dates"].items():
            cost = sum(float(r["actual_cost"]) for r in rows
                       if r["strategy"] == strategy and r["date"] in valid)
            assert cost == pytest.approx(totals["actual_cost"])
            assert baseline - cost == pytest.approx(totals["savings"])
            assert 1 - cost / baseline == pytest.approx(totals["savings_rate"])
            failures = sum(r["strategy"] == strategy and r["replay_valid"] == "False" for r in rows)
            assert failures == experiment["strategy_failures"][strategy]["replay_infeasible_days"]


def test_reported_forecast_and_cost_metrics_match_records():
    rows = read_csv("predictions.csv")
    assert len({r["timestamp"] for r in rows}) == len(rows) == 168
    errors = [float(r["forecast_kw"]) - float(r["actual_kw"]) for r in rows]
    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
    documents = [(ROOT / "README.md").read_text(encoding="utf-8"),
                 (ROOT / "docs/report/main.tex").read_text(encoding="utf-8")]
    for experiment in summary()["scenarios"].values():
        assert experiment["forecast"]["sample_count"] == len(rows)
        assert experiment["forecast"]["mae_kw"] == pytest.approx(mae)
        assert experiment["forecast"]["rmse_kw"] == pytest.approx(rmse)
    default = summary()["scenarios"]["5kwh"]
    values = [f"{mae:.6f}", f"{rmse:.6f}", f"{100*default['coverage_rate']:.2f}"]
    for total in default["cost_totals_on_common_dates"].values():
        values.append(f"{total['actual_cost']:.6f}")
    values.append(f"{100*default['cost_totals_on_common_dates']['optimized']['savings_rate']:.4f}")
    for text in documents:
        for value in values:
            assert value in text


def test_example_dispatch_recomputes_energy_and_actual_cost():
    config = tomllib.loads((ROOT / "configs/example.toml").read_text(encoding="utf-8"))
    b = config["battery"]
    rows = read_csv("schedule_2010-01-10.csv")
    assert len(rows) == 24
    energy, cost = b["initial_energy_kwh"], 0.0
    for hour, row in enumerate(rows):
        assert row["timestamp"] == f"2010-01-10 {hour:02d}:00:00"
        assert row["replay_valid"] == row["plan_valid"] == "True"
        charge, discharge = float(row["charge_kw"]), float(row["discharge_kw"])
        assert 0 <= charge <= b["max_charge_kw"] + 1e-6
        assert 0 <= discharge <= b["max_discharge_kw"] + 1e-6
        assert charge * discharge == pytest.approx(0, abs=1e-6)
        assert float(row["energy_start_kwh"]) == pytest.approx(energy, abs=1e-6)
        energy += b["charge_efficiency"] * charge - discharge / b["discharge_efficiency"]
        assert float(row["energy_end_kwh"]) == pytest.approx(energy, abs=1e-6)
        assert b["min_energy_kwh"] - 1e-6 <= energy <= b["max_energy_kwh"] + 1e-6
        actual = float(row["actual_kw"])
        assert discharge <= actual + 1e-6
        grid = actual + charge - discharge
        assert float(row["actual_grid_kw"]) == pytest.approx(grid, abs=1e-6)
        cost += grid * config["tariff"]["hourly_prices"][hour]
    assert energy == pytest.approx(b["initial_energy_kwh"], abs=1e-6)
    match = next(r for r in read_csv("cost_comparison.csv") if
                 (r["scenario"], r["strategy"], r["date"]) == ("5kwh", "optimized", "2010-01-10"))
    assert cost == pytest.approx(float(match["actual_cost"]))


def test_evidence_hashes_configurations_and_document_links():
    data = summary()
    assert re.fullmatch(r"[0-9a-f]{64}", data["data"]["sha256"])
    for name, digest in data["files_sha256"].items():
        assert sha256((EVIDENCE / name).read_bytes()).hexdigest() == digest
    for scenario in data["scenarios"].values():
        assert sha256((ROOT / scenario["config"]).read_bytes()).hexdigest() == scenario["config_sha256"]
    for document in [ROOT / "README.md", ROOT / "docs/技术方案.md"]:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if not re.match(r"https?://|#", target):
                assert (document.parent / target.split("#", 1)[0]).exists(), target
    source = ROOT / "docs/report/main.tex"
    for image in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", source.read_text(encoding="utf-8")):
        assert (source.parent / image).is_file(), image
