from __future__ import annotations

import json

import pytest

from gamma_scalping.config import UnifiedBacktestConfig, load_unified_config, save_unified_config


def test_unified_config_loads_defaults_and_applies_dotted_overrides(tmp_path) -> None:
    path = tmp_path / "config.json"
    save_unified_config(UnifiedBacktestConfig(), path)

    config = load_unified_config(
        path,
        [
            "data.start_date=2024-04-08",
            "data.end_date=2024-04-10",
            "strategy.premium_budget_pct=0.25",
            "strategy.use_vol_filter=true",
            "strategy.entry_max_iv_rv_ratio=0.85",
            "strategy.exit_on_vol_edge_filled=true",
            "volatility.rv_reference_mode=rolling_quantile",
            "volatility.rv_distribution_quantile=0.6",
            "risk.max_abs_order_quantity=10",
            "backtest.run_id=manual_run",
            "attribution.residual_warning_threshold=0.2",
            "performance.iv_hv_capture_min_return_observations=3",
        ],
    )

    assert config.data.start_date.isoformat() == "2024-04-08"
    assert config.data.end_date.isoformat() == "2024-04-10"
    assert config.strategy.premium_budget_pct == pytest.approx(0.25)
    assert config.strategy.use_vol_filter is True
    assert config.strategy.entry_max_iv_rv_ratio == pytest.approx(0.85)
    assert config.strategy.exit_on_vol_edge_filled is True
    assert config.volatility.rv_reference_mode == "rolling_quantile"
    assert config.volatility.rv_distribution_quantile == pytest.approx(0.6)
    assert config.risk.max_abs_order_quantity == pytest.approx(10.0)
    assert config.backtest.run_id == "manual_run"
    assert config.attribution.residual_warning_threshold == pytest.approx(0.2)
    assert config.performance.iv_hv_capture_min_return_observations == 3


def test_unified_config_propagates_common_fields() -> None:
    config = UnifiedBacktestConfig().with_overrides(
        [
            "common.annual_trading_days=244",
            "common.strategy_tag=custom_gamma",
        ]
    )

    assert config.data.calendar.annual_trading_days == 244
    assert config.greeks.annual_trading_days == 244
    assert config.volatility.annual_trading_days == 244
    assert config.performance.annual_trading_days == 244
    assert config.strategy.strategy_tag == "custom_gamma"
    assert config.backtest.strategy_tag == "custom_gamma"


def test_unified_config_rejects_unknown_override_field() -> None:
    with pytest.raises(KeyError, match="strategy.unknown"):
        UnifiedBacktestConfig().with_overrides(["strategy.unknown=1"])


def test_unified_config_rejects_unknown_file_field(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"strategy": {"unknown": 1}}), encoding="utf-8")

    with pytest.raises(KeyError, match="StrategyConfig"):
        load_unified_config(path)


def test_unified_config_loads_json_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "data": {"underlying": "510300.XSHG"},
                "backtest": {"initial_cash": 12345},
                "report": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    config = load_unified_config(path)

    assert config.data.underlying == "510300.XSHG"
    assert config.backtest.initial_cash == pytest.approx(12345.0)
    assert config.report.enabled is False
