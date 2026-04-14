from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from gamma_scalping.backtest import BacktestConfig, BacktestEngine, ExecutionModel, Portfolio, RiskChecker
from gamma_scalping.data.models import ETFBar, MarketSnapshot, OptionChain
from gamma_scalping.strategy import GammaScalpingStrategy, OrderIntent, StrategyConfig


def _snapshot(trading_date: date = date(2024, 4, 8), *, ttm: int = 10, spot: float = 2.8) -> MarketSnapshot:
    frame = pd.DataFrame(
        [
            {
                "contract_id": "CALL_ATM",
                "strike": 2.8,
                "maturity_date": date(2024, 4, 22),
                "option_type": "C",
                "bid": 0.048,
                "ask": 0.05,
                "mid": 0.049,
                "last": 0.049,
                "buy_price": 0.05,
                "sell_price": 0.048,
                "mark_price": 0.049,
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "ttm_trading_days": ttm,
                "maturity_session": trading_date,
            },
            {
                "contract_id": "PUT_ATM",
                "strike": 2.8,
                "maturity_date": date(2024, 4, 22),
                "option_type": "P",
                "bid": 0.048,
                "ask": 0.05,
                "mid": 0.049,
                "last": 0.049,
                "buy_price": 0.05,
                "sell_price": 0.048,
                "mark_price": 0.049,
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "ttm_trading_days": ttm,
                "maturity_session": trading_date,
            },
        ]
    )
    return MarketSnapshot(
        trading_date=trading_date,
        underlying="510050.XSHG",
        etf_bar=ETFBar(
            trading_date=trading_date,
            underlying="510050.XSHG",
            open=spot,
            close=spot,
            high=spot,
            low=spot,
            volume=1000,
            turnover=spot * 1000,
        ),
        option_chain=OptionChain(trading_date, "510050.XSHG", frame),
    )


def test_execution_model_uses_directional_option_prices() -> None:
    snapshot = _snapshot()
    execution = ExecutionModel(option_fee_per_contract=1.0)
    orders = (
        OrderIntent(snapshot.trading_date, "CALL_ATM", "option", "buy", 2, "test", "call_leg"),
        OrderIntent(snapshot.trading_date, "PUT_ATM", "option", "sell", 3, "test", "put_leg"),
    )

    fills = execution.fill(orders, snapshot)

    assert fills[0].price == pytest.approx(0.05)
    assert fills[0].fee == pytest.approx(2.0)
    assert fills[1].price == pytest.approx(0.048)
    assert fills[1].fee == pytest.approx(3.0)


def test_execution_model_applies_option_slippage_to_fallback_price() -> None:
    snapshot = _snapshot()
    snapshot.option_chain.frame.loc[0, ["buy_price", "ask"]] = 0.0
    execution = ExecutionModel(option_slippage_bps=100.0)

    fill = execution.fill(
        (OrderIntent(snapshot.trading_date, "CALL_ATM", "option", "buy", 1, "test", "call_leg"),),
        snapshot,
    )[0]

    assert fill.price == pytest.approx(0.049 * 1.01)


def test_portfolio_cash_positions_and_mark_to_market() -> None:
    snapshot = _snapshot()
    fills = ExecutionModel().fill(
        (OrderIntent(snapshot.trading_date, "CALL_ATM", "option", "buy", 1, "test", "call_leg"),),
        snapshot,
    )
    portfolio = Portfolio(initial_cash=10000)

    portfolio.apply_fills(fills)

    assert portfolio.cash == pytest.approx(9500)
    assert portfolio.holdings["CALL_ATM"].quantity == 1
    assert portfolio.market_value(snapshot) == pytest.approx(490)
    assert portfolio.equity(snapshot) == pytest.approx(9990)


def test_portfolio_updates_avg_price_when_position_reverses() -> None:
    snapshot = _snapshot()
    portfolio = Portfolio(initial_cash=10000)
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(snapshot.trading_date, "510050.XSHG", "etf", "buy", 1000, "test", "hedge"),),
            snapshot,
        )
    )
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(snapshot.trading_date, "510050.XSHG", "etf", "sell", 1500, "test", "hedge"),),
            snapshot,
        )
    )

    holding = portfolio.holdings["510050.XSHG"]
    assert holding.quantity == pytest.approx(-500)
    assert holding.avg_price == pytest.approx(2.8)


def test_portfolio_expiry_settlement_removes_option_position() -> None:
    entry = _snapshot()
    expiry = _snapshot(date(2024, 4, 22), ttm=0, spot=3.0)
    portfolio = Portfolio(initial_cash=10000)
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(entry.trading_date, "CALL_ATM", "option", "buy", 1, "test", "call_leg"),),
            entry,
        )
    )

    events = portfolio.handle_expiry_and_settlement(expiry)

    assert events[0]["event"] == "expiry_settlement"
    assert events[0]["cash_flow"] == pytest.approx(2000)
    assert "CALL_ATM" not in portfolio.holdings


def test_backtest_engine_runs_strategy_loop() -> None:
    snapshots = [_snapshot(date(2024, 4, 8), ttm=10), _snapshot(date(2024, 4, 9), ttm=9)]
    strategy = GammaScalpingStrategy(
        StrategyConfig(
            premium_budget_pct=0.5,
            delta_threshold_pct=0.01,
            underlying_instrument_id="510050.XSHG",
        )
    )
    engine = BacktestEngine(
        strategy=strategy,
        execution_model=ExecutionModel(),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run(snapshots)

    assert len(result.equity_curve) == 2
    assert result.decisions.loc[0, "action"] == "open"
    assert not result.fills.empty
    assert {"cash", "market_value", "equity"}.issubset(result.equity_curve.columns)
    assert {"pre_trade_market_value", "pre_trade_equity"}.issubset(result.equity_curve.columns)


def test_backtest_engine_applies_risk_checker() -> None:
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        risk_checker=RiskChecker(max_abs_order_quantity=1),
        config=BacktestConfig(initial_cash=100000),
    )

    with pytest.raises(ValueError, match="max_abs_order_quantity"):
        engine.run([_snapshot(date(2024, 4, 8), ttm=10)])


def test_backtest_engine_handles_existing_iv_columns_without_merge_suffixes() -> None:
    snapshot = _snapshot(date(2024, 4, 8), ttm=10)
    snapshot.option_chain.frame["iv"] = 0.1
    snapshot.option_chain.frame["iv_status"] = "stale"
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run([snapshot])

    assert result.decisions.loc[0, "action"] == "open"


def test_backtest_outputs_trade_and_position_records() -> None:
    snapshots = [_snapshot(date(2024, 4, 8), ttm=10), _snapshot(date(2024, 4, 9), ttm=9)]
    strategy = GammaScalpingStrategy(
        StrategyConfig(
            premium_budget_pct=0.5,
            delta_threshold_pct=10.0,
            underlying_instrument_id="510050.XSHG",
        )
    )
    engine = BacktestEngine(
        strategy=strategy,
        execution_model=ExecutionModel(),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run(snapshots)

    first_day_trades = result.trade_records[result.trade_records["trading_date"].eq(date(2024, 4, 8))]
    second_day_trades = result.trade_records[result.trade_records["trading_date"].eq(date(2024, 4, 9))]
    assert len(first_day_trades) == 3
    assert first_day_trades["instrument_id"].tolist()[:2] == ["CALL_ATM", "PUT_ATM"]
    assert {"side", "quantity", "trade_amount", "reason"}.issubset(result.trade_records.columns)
    assert len(second_day_trades) == 1
    assert second_day_trades.iloc[0]["instrument_id"] == ""
    assert second_day_trades.iloc[0]["trade_amount"] == 0.0

    day_positions = result.position_records[result.position_records["trading_date"].eq(date(2024, 4, 8))]
    assert {"instrument_id", "quantity", "liquidation_price", "theoretical_unrealized_pnl"}.issubset(
        result.position_records.columns
    )
    call_position = day_positions[day_positions["instrument_id"].eq("CALL_ATM")].iloc[0]
    assert call_position["theoretical_unrealized_pnl"] == pytest.approx(
        (0.048 - 0.05) * call_position["quantity"] * call_position["multiplier"]
    )


def test_backtest_result_exports_csv_files(tmp_path) -> None:
    snapshots = [_snapshot(date(2024, 4, 8), ttm=10)]
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        config=BacktestConfig(initial_cash=100000, run_id="test_run"),
    )

    result = engine.run(snapshots)
    paths = result.export_csv(tmp_path)

    assert paths["run_dir"] == tmp_path / "test_run"
    assert paths["trade_records"].exists()
    assert paths["position_records"].exists()
    assert paths["config"].exists()
    assert paths["metadata"].exists()
    assert paths["expiry_events"].exists()
    assert paths["final_positions"].exists()
    exported_trades = pd.read_csv(paths["trade_records"])
    exported_positions = pd.read_csv(paths["position_records"])
    assert "trade_amount" in exported_trades.columns
    assert "theoretical_unrealized_pnl" in exported_positions.columns


def test_backtest_auto_exports_to_run_id_subdirectory(tmp_path) -> None:
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        config=BacktestConfig(initial_cash=100000, output_dir=tmp_path, run_id="auto_run"),
    )

    result = engine.run([_snapshot(date(2024, 4, 8), ttm=10)])

    assert result.run_id == "auto_run"
    assert (tmp_path / "auto_run" / "trade_records.csv").exists()
    assert (tmp_path / "auto_run" / "position_records.csv").exists()
    assert (tmp_path / "auto_run" / "config.json").exists()
    assert (tmp_path / "auto_run" / "metadata.json").exists()
