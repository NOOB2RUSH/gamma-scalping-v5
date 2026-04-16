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


def _snapshot_with_expired_and_new_pair(trading_date: date = date(2024, 4, 22), *, spot: float = 3.0) -> MarketSnapshot:
    snapshot = _snapshot(trading_date, ttm=0, spot=spot)
    frame = snapshot.option_chain.frame.copy()
    new_rows = frame.copy()
    new_rows["contract_id"] = ["CALL_NEW", "PUT_NEW"]
    new_rows["ttm_trading_days"] = 10
    new_rows["maturity_session"] = date(2024, 5, 8)
    new_rows["maturity_date"] = date(2024, 5, 8)
    new_rows["strike"] = spot
    combined = pd.concat([frame, new_rows], ignore_index=True)
    return MarketSnapshot(
        trading_date=trading_date,
        underlying=snapshot.underlying,
        etf_bar=snapshot.etf_bar,
        option_chain=OptionChain(trading_date, snapshot.underlying, combined),
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


def test_portfolio_uses_configured_zero_tolerance() -> None:
    snapshot = _snapshot()
    portfolio = Portfolio(initial_cash=10000, position_zero_tolerance=0.01)
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(snapshot.trading_date, "510050.XSHG", "etf", "buy", 1.0, "test", "hedge"),),
            snapshot,
        )
    )
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(snapshot.trading_date, "510050.XSHG", "etf", "sell", 0.995, "test", "hedge"),),
            snapshot,
        )
    )

    assert "510050.XSHG" not in portfolio.holdings


def test_portfolio_keeps_episode_etf_positions_separate() -> None:
    snapshot = _snapshot()
    portfolio = Portfolio(initial_cash=10000)
    portfolio.apply_fills(
        ExecutionModel().fill(
            (
                OrderIntent(
                    snapshot.trading_date,
                    "510050.XSHG",
                    "etf",
                    "sell",
                    1000,
                    "hedge",
                    "hedge",
                    episode_id="episode_a",
                ),
                OrderIntent(
                    snapshot.trading_date,
                    "510050.XSHG",
                    "etf",
                    "sell",
                    500,
                    "hedge",
                    "hedge",
                    episode_id="episode_b",
                ),
            ),
            snapshot,
        )
    )

    assert portfolio.holdings[("510050.XSHG", "episode_a")].quantity == pytest.approx(-1000)
    assert portfolio.holdings[("510050.XSHG", "episode_b")].quantity == pytest.approx(-500)


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


def test_portfolio_remaps_option_contract_id_by_economic_terms() -> None:
    entry = _snapshot()
    portfolio = Portfolio(initial_cash=10000)
    portfolio.apply_fills(
        ExecutionModel().fill(
            (OrderIntent(entry.trading_date, "CALL_ATM", "option", "buy", 1, "test", "call_leg", episode_id="episode"),),
            entry,
        )
    )
    remapped = _snapshot(date(2024, 4, 9), ttm=9)
    remapped_frame = remapped.option_chain.frame.copy()
    remapped_frame.loc[remapped_frame["contract_id"].eq("CALL_ATM"), "contract_id"] = "CALL_RECODED"
    remapped = MarketSnapshot(
        trading_date=remapped.trading_date,
        underlying=remapped.underlying,
        etf_bar=remapped.etf_bar,
        option_chain=OptionChain(remapped.trading_date, remapped.underlying, remapped_frame),
    )

    events = portfolio.remap_option_contract_ids(remapped)

    assert events[0]["old_instrument_id"] == "CALL_ATM"
    assert events[0]["new_instrument_id"] == "CALL_RECODED"
    assert ("CALL_RECODED", "episode") in portfolio.holdings


def test_portfolio_settles_expired_missing_option_from_holding_terms() -> None:
    entry = _snapshot()
    expiry = _snapshot(date(2024, 4, 22), ttm=0, spot=3.0)
    expiry_frame = expiry.option_chain.frame[expiry.option_chain.frame["contract_id"].ne("CALL_ATM")].copy()
    expiry = MarketSnapshot(
        trading_date=expiry.trading_date,
        underlying=expiry.underlying,
        etf_bar=expiry.etf_bar,
        option_chain=OptionChain(expiry.trading_date, expiry.underlying, expiry_frame),
    )
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


def test_backtest_closes_episode_hedge_after_expiry_settlement() -> None:
    snapshots = [_snapshot(date(2024, 4, 8), ttm=10), _snapshot(date(2024, 4, 22), ttm=0, spot=3.0)]
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        execution_model=ExecutionModel(),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run(snapshots)

    assert result.final_positions.empty
    assert "expiry_hedge_close" in result.fills["reason"].tolist()
    expiry_trade = result.trade_records[result.trade_records["reason"].eq("expiry_hedge_close")].iloc[0]
    assert expiry_trade["instrument_type"] == "etf"
    assert result.episode_records.loc[0, "status"] == "expired_settled"


def test_backtest_can_open_new_episode_after_expiry_hedge_close_same_day() -> None:
    snapshots = [
        _snapshot(date(2024, 4, 8), ttm=10),
        _snapshot_with_expired_and_new_pair(date(2024, 4, 22), spot=3.0),
    ]
    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        execution_model=ExecutionModel(),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run(snapshots)

    assert "expiry_hedge_close" in result.fills["reason"].tolist()
    assert result.decisions.loc[1, "action"] == "open"
    assert result.decisions.loc[1, "episode_id"] == "gamma_scalping:20240422:CALL_NEW:PUT_NEW"
    assert set(result.episode_records["episode_id"]) == {
        "gamma_scalping:20240408:CALL_ATM:PUT_ATM",
        "gamma_scalping:20240422:CALL_NEW:PUT_NEW",
    }
    old_episode_positions = result.final_positions[
        result.final_positions["episode_id"].eq("gamma_scalping:20240408:CALL_ATM:PUT_ATM")
    ]
    assert old_episode_positions.empty
    assert result.final_positions["episode_id"].eq("gamma_scalping:20240422:CALL_NEW:PUT_NEW").any()


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
    assert not result.episode_records.empty
    assert result.episode_records.loc[0, "episode_id"] == "gamma_scalping:20240408:CALL_ATM:PUT_ATM"
    assert result.fills["episode_id"].nunique() == 1
    assert {"trading_date", "contract_id", "delta", "gamma", "theta", "vega"}.issubset(result.greeks_history.columns)
    assert {"trading_date", "contract_id", "iv", "iv_status"}.issubset(result.iv_history.columns)
    assert not result.greeks_history.empty
    assert not result.iv_history.empty


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


def test_backtest_does_not_register_episode_when_risk_checker_drops_orders() -> None:
    class DropAllRiskChecker(RiskChecker):
        def check(self, orders, snapshot):
            return ()

    engine = BacktestEngine(
        strategy=GammaScalpingStrategy(
            StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
        ),
        risk_checker=DropAllRiskChecker(),
        config=BacktestConfig(initial_cash=100000),
    )

    result = engine.run([_snapshot(date(2024, 4, 8), ttm=10)])

    assert result.decisions.loc[0, "action"] == "open"
    assert result.fills.empty
    assert result.episode_records.empty


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
    assert "episode_id" in result.trade_records.columns
    assert "option_contract_name" in result.trade_records.columns
    assert first_day_trades["option_contract_name"].tolist()[:2] == ["50ETF202404C0280", "50ETF202404P0280"]
    assert first_day_trades["option_contract_name"].tolist()[2] == ""
    assert len(second_day_trades) == 1
    assert second_day_trades.iloc[0]["instrument_id"] == ""
    assert second_day_trades.iloc[0]["trade_amount"] == 0.0
    assert second_day_trades.iloc[0]["option_contract_name"] == ""

    day_positions = result.position_records[result.position_records["trading_date"].eq(date(2024, 4, 8))]
    assert {"instrument_id", "quantity", "liquidation_price", "theoretical_unrealized_pnl"}.issubset(
        result.position_records.columns
    )
    assert "episode_id" in result.position_records.columns
    assert "option_contract_name" in result.position_records.columns
    call_position = day_positions[day_positions["instrument_id"].eq("CALL_ATM")].iloc[0]
    assert call_position["option_contract_name"] == "50ETF202404C0280"
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
    assert paths["episode_records"].exists()
    assert paths["config"].exists()
    assert paths["metadata"].exists()
    assert paths["expiry_events"].exists()
    assert paths["final_positions"].exists()
    assert paths["greeks_history"].exists()
    assert paths["iv_history"].exists()
    exported_trades = pd.read_csv(paths["trade_records"])
    exported_positions = pd.read_csv(paths["position_records"])
    assert "trade_amount" in exported_trades.columns
    assert "option_contract_name" in exported_trades.columns
    assert "theoretical_unrealized_pnl" in exported_positions.columns
    assert "option_contract_name" in exported_positions.columns
    assert exported_trades["trade_amount"].map(lambda value: len(str(value).split(".")[-1]) <= 2).all()
    assert exported_trades["price"].map(lambda value: len(str(value).split(".")[-1]) <= 4).all()


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
