#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamma_scalping.backtest import BacktestEngine
from gamma_scalping.attribution import GreeksPnLAttribution
from gamma_scalping.config import load_unified_config
from gamma_scalping.data import MarketDataLoader
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.performance import PerformanceAnalyzer
from gamma_scalping.strategy import GammaScalpingStrategy
from gamma_scalping.volatility import VolatilityEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run gamma scalping backtest from unified config.")
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "backtest.default.json"),
        help="Path to unified JSON config file.",
    )
    parser.add_argument(
        "--params",
        action="append",
        default=[],
        metavar="SECTION.FIELD=VALUE",
        help="Override a config value. Repeat for multiple values, e.g. --params strategy.premium_budget_pct=0.2.",
    )
    args = parser.parse_args(argv)

    config = load_unified_config(args.config, args.params)
    loader = MarketDataLoader(config.data)
    snapshots = list(loader.iter_snapshots())
    etf_history = _etf_history_from_snapshots(snapshots)

    volatility_engine = VolatilityEngine(config.volatility)
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(config.strategy),
        greeks_calculator=GreeksCalculator(config.greeks),
        volatility_engine=volatility_engine,
        execution_model=config.execution,
        risk_checker=config.risk,
        atm_iv_config=config.atm_iv,
        config=config.backtest,
    )
    result = engine.run(snapshots, etf_history=etf_history)
    underlying_history = _underlying_history_from_snapshots(snapshots)

    if config.backtest.output_dir is not None:
        run_dir = Path(config.backtest.output_dir) / result.run_id
    else:
        run_dir = None

    attribution = GreeksPnLAttribution(config.attribution).attribute_daily(
        equity_curve=result.equity_curve,
        trade_records=result.trade_records,
        position_records=result.position_records,
        greeks_history=result.greeks_history,
        iv_history=result.iv_history,
        underlying_history=underlying_history,
    )
    if run_dir is not None:
        attribution.export_csv(run_dir)

    if config.report.enabled:
        if config.report.output_dir is not None:
            report_dir = Path(config.report.output_dir)
        elif run_dir is not None:
            report_dir = run_dir / "report"
        else:
            raise ValueError("report.output_dir must be set when backtest.output_dir is null and report.enabled is true.")
        volatility_series = volatility_engine.build_signal_series(snapshots, etf_history, config.atm_iv)
        PerformanceAnalyzer(config.performance).build_report(
            result,
            report_dir,
            attribution=attribution,
            volatility=volatility_series,
            underlying_history=underlying_history,
            matplotlib_config_dir=config.report.matplotlib_config_dir,
        )

    print(f"run_id={result.run_id}")
    print(f"output_dir={run_dir if run_dir is not None else ''}")
    return 0


def _etf_history_from_snapshots(snapshots) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [snapshot.etf_bar.close for snapshot in snapshots]},
        index=pd.Index([snapshot.trading_date for snapshot in snapshots], name="date"),
    )


def _underlying_history_from_snapshots(snapshots) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trading_date": [snapshot.trading_date for snapshot in snapshots],
            "underlying": [snapshot.underlying for snapshot in snapshots],
            "close": [snapshot.etf_bar.close for snapshot in snapshots],
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
