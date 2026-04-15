from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
import json

from gamma_scalping.attribution import AttributionConfig
from gamma_scalping.backtest import BacktestConfig, ExecutionModel, RiskChecker
from gamma_scalping.data import MarketDataConfig
from gamma_scalping.greeks import GreeksConfig
from gamma_scalping.performance import PerformanceConfig
from gamma_scalping.strategy import StrategyConfig
from gamma_scalping.volatility import AtmIvConfig, VolatilityConfig


@dataclass(frozen=True)
class CommonConfig:
    annual_trading_days: int | None = None
    strategy_tag: str | None = None


@dataclass(frozen=True)
class ReportConfig:
    enabled: bool = True
    output_dir: str | None = None
    matplotlib_config_dir: str | None = "/tmp/matplotlib"


@dataclass(frozen=True)
class UnifiedBacktestConfig:
    common: CommonConfig = field(default_factory=CommonConfig)
    data: MarketDataConfig = field(default_factory=MarketDataConfig)
    greeks: GreeksConfig = field(default_factory=GreeksConfig)
    volatility: VolatilityConfig = field(default_factory=VolatilityConfig)
    atm_iv: AtmIvConfig = field(default_factory=AtmIvConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    risk: RiskChecker = field(default_factory=RiskChecker)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> UnifiedBacktestConfig:
        allowed_sections = {item.name for item in fields(cls)}
        unknown_sections = sorted(set(raw) - allowed_sections)
        if unknown_sections:
            raise KeyError(f"Unknown config sections: {unknown_sections}")
        common = _build_dataclass(CommonConfig, raw.get("common", {}))
        raw = _propagate_common(raw, common)
        data = _build_dataclass(MarketDataConfig, raw.get("data", {}))
        if common.annual_trading_days is not None:
            data = replace(data, calendar=replace(data.calendar, annual_trading_days=common.annual_trading_days))
        return cls(
            common=common,
            data=data,
            greeks=_build_dataclass(GreeksConfig, raw.get("greeks", {})),
            volatility=_build_dataclass(VolatilityConfig, raw.get("volatility", {})),
            atm_iv=_build_dataclass(AtmIvConfig, raw.get("atm_iv", {})),
            strategy=_build_dataclass(StrategyConfig, raw.get("strategy", {})),
            execution=_build_dataclass(ExecutionModel, raw.get("execution", {})),
            risk=_build_dataclass(RiskChecker, raw.get("risk", {})),
            backtest=_build_dataclass(BacktestConfig, raw.get("backtest", {})),
            attribution=_build_dataclass(AttributionConfig, raw.get("attribution", {})),
            performance=_build_dataclass(PerformanceConfig, raw.get("performance", {})),
            report=_build_dataclass(ReportConfig, raw.get("report", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def with_overrides(self, overrides: list[str] | tuple[str, ...]) -> UnifiedBacktestConfig:
        data = self.to_dict()
        for override in overrides:
            key, value = _parse_override(override)
            _set_nested(data, key.split("."), value)
        return UnifiedBacktestConfig.from_dict(data)


def load_unified_config(path: Path | str | None = None, overrides: list[str] | tuple[str, ...] = ()) -> UnifiedBacktestConfig:
    if path is None:
        config = UnifiedBacktestConfig()
    else:
        with Path(path).open("r", encoding="utf-8") as handle:
            config = UnifiedBacktestConfig.from_dict(json.load(handle))
    return config.with_overrides(overrides)


def save_unified_config(config: UnifiedBacktestConfig, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _build_dataclass(cls: type[Any], raw: dict[str, Any]) -> Any:
    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise KeyError(f"Unknown fields for {cls.__name__}: {unknown}")
    return cls(**raw)


def _propagate_common(raw: dict[str, Any], common: CommonConfig) -> dict[str, Any]:
    propagated = {key: (value.copy() if isinstance(value, dict) else value) for key, value in raw.items()}
    if common.annual_trading_days is not None:
        for section in ["greeks", "volatility", "performance"]:
            propagated.setdefault(section, {})
            propagated[section]["annual_trading_days"] = common.annual_trading_days
    if common.strategy_tag is not None:
        for section in ["strategy", "backtest"]:
            propagated.setdefault(section, {})
            propagated[section]["strategy_tag"] = common.strategy_tag
    return propagated


def _parse_override(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ValueError(f"--params must use key=value format: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key or "." not in key:
        raise ValueError(f"--params key must be dotted, e.g. strategy.premium_budget_pct=0.1: {raw}")
    return key, _parse_value(value.strip())


def _parse_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null"}:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_nested(data: dict[str, Any], keys: list[str], value: Any) -> None:
    if len(keys) < 2:
        raise ValueError("override key must include section and field")
    cursor: dict[str, Any] = data
    for key in keys[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            raise KeyError(f"Unknown config section: {'.'.join(keys[:-1])}")
        cursor = cursor[key]
    field_name = keys[-1]
    if field_name not in cursor:
        raise KeyError(f"Unknown config field: {'.'.join(keys)}")
    cursor[field_name] = value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items() if key != "calendar"}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
