"""Strict configuration validation; all relative paths use the project root."""

from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
import math
import re
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ExperimentConfig:
    name: str
    test_start: date
    test_end: date
    output_dir: Path


@dataclass(frozen=True)
class DataConfig:
    path: Path
    source_url: str
    history_ffill_limit_minutes: int = 5
    evaluation_require_observed: bool = True


@dataclass(frozen=True)
class TariffConfig:
    currency: str
    source: str
    hourly_prices: tuple[float, ...]


@dataclass(frozen=True)
class BatteryConfig:
    source: str
    capacity_kwh: float
    min_energy_kwh: float
    max_energy_kwh: float
    initial_energy_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float


@dataclass(frozen=True)
class FixedRuleConfig:
    charge_hours: tuple[int, ...]
    discharge_hours: tuple[int, ...]


@dataclass(frozen=True)
class SolverConfig:
    time_limit_seconds: float = 30
    threads: int = 1
    cbc_path: Path | None = None


@dataclass(frozen=True)
class ValidationConfig:
    absolute_tolerance: float = 1e-6


@dataclass(frozen=True)
class Config:
    experiment: ExperimentConfig
    data: DataConfig
    tariff: TariffConfig
    battery: BatteryConfig
    fixed_rule: FixedRuleConfig
    solver: SolverConfig
    validation: ValidationConfig
    config_path: Path
    project_root: Path


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: 必须是有限数值")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name}: 必须是有限{'正数' if positive else '非负数'}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: 必须是非空字符串")


def _date(value, name):
    try:
        # TOML permits both ISO date values and quoted date strings.
        if isinstance(value, date) and type(value) is date:
            return value
        if not isinstance(value, str):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name}: 使用 YYYY-MM-DD 日期") from exc


def _path(value, root, name):
    _text(value, name)
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(path: str | Path, *, project_root: Path | None = None) -> Config:
    root = (project_root or PROJECT_ROOT).resolve()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"配置 {config_path}: {exc}") from exc
    classes = {
        "experiment": ExperimentConfig, "data": DataConfig,
        "tariff": TariffConfig, "battery": BatteryConfig,
        "fixed_rule": FixedRuleConfig, "solver": SolverConfig,
        "validation": ValidationConfig,
    }
    if set(raw) != set(classes):
        raise ValueError(f"配置分组不符：需要 {', '.join(classes)}")
    sections = {}
    for name, cls in classes.items():
        values = raw[name]
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(cls)}:
            raise ValueError(f"[{name}]: 存在未知字段或格式不正确")
        sections[name] = values.copy()

    ex, data, tariff, battery, rule, solver, validation = (
        sections[n] for n in classes
    )
    try:
        _text(ex["name"], "experiment.name")
        if not re.fullmatch(r"[\w-]+", ex["name"]):
            raise ValueError("experiment.name: 只允许文字、数字、下划线和连字符")
        ex["test_start"] = _date(ex["test_start"], "experiment.test_start")
        ex["test_end"] = _date(ex["test_end"], "experiment.test_end")
        if ex["test_start"] > ex["test_end"]:
            raise ValueError("experiment.test_start: 不能晚于 test_end")
        ex["output_dir"] = _path(ex["output_dir"], root, "experiment.output_dir")
        data["path"] = _path(data["path"], root, "data.path")
        _text(data["source_url"], "data.source_url")
        fill = data.get("history_ffill_limit_minutes", 5)
        if type(fill) is not int or not 0 <= fill <= 5:
            raise ValueError("data.history_ffill_limit_minutes: 必须为 0 到 5 的整数")
        if data.get("evaluation_require_observed", True) is not True:
            raise ValueError("data.evaluation_require_observed: 正式评估必须为 true")
        _text(tariff["currency"], "tariff.currency")
        _text(tariff["source"], "tariff.source")
        prices = tariff["hourly_prices"]
        if not isinstance(prices, list) or len(prices) != 24:
            raise ValueError("tariff.hourly_prices: 必须恰好有 24 个电价")
        for price in prices:
            _number(price, "tariff.hourly_prices")
        tariff["hourly_prices"] = tuple(prices)
        _text(battery["source"], "battery.source")
        for f in fields(BatteryConfig):
            if f.name != "source":
                _number(battery[f.name], f"battery.{f.name}", positive=f.name in {
                    "capacity_kwh", "max_charge_kw", "max_discharge_kw",
                    "charge_efficiency", "discharge_efficiency",
                })
        if not (0 <= battery["min_energy_kwh"] <= battery["initial_energy_kwh"]
                <= battery["max_energy_kwh"] <= battery["capacity_kwh"]):
            raise ValueError("battery: 必须满足 0 ≤ min ≤ initial ≤ max ≤ capacity")
        if max(battery["charge_efficiency"], battery["discharge_efficiency"]) > 1:
            raise ValueError("battery: 充放电效率必须在 (0,1] 内")
        for key in ("charge_hours", "discharge_hours"):
            hours = rule[key]
            if (not isinstance(hours, list) or not hours
                    or any(type(h) is not int or not 0 <= h <= 23 for h in hours)
                    or hours != sorted(set(hours))):
                raise ValueError(f"fixed_rule.{key}: 需要非空、严格递增的 0–23 整数小时")
            rule[key] = tuple(hours)
        if max(rule["charge_hours"]) >= min(rule["discharge_hours"]):
            raise ValueError("fixed_rule: 全部充电时段必须早于全部放电时段")
        _number(solver.get("time_limit_seconds", 30), "solver.time_limit_seconds", positive=True)
        if type(solver.get("threads", 1)) is not int or solver.get("threads", 1) != 1:
            raise ValueError("solver.threads: 首版要求单线程 1")
        if "cbc_path" in solver:
            solver["cbc_path"] = _path(solver["cbc_path"], root, "solver.cbc_path")
        tolerance = validation.get("absolute_tolerance", 1e-6)
        _number(tolerance, "validation.absolute_tolerance", positive=True)
        if tolerance > 1e-6:
            raise ValueError("validation.absolute_tolerance: 不能大于 1e-6")
        objects = {name: cls(**sections[name]) for name, cls in classes.items()}
    except (KeyError, TypeError) as exc:
        raise ValueError(f"配置缺少字段或类型不正确：{exc}") from exc
    return Config(**objects, config_path=config_path.resolve(), project_root=root)
