from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from gamma_scalping.data.models import ETFBar, MarketSnapshot, OptionChain
from gamma_scalping.strategy import GammaScalpingStrategy, PortfolioState, StrategyConfig, StrategyPosition
from gamma_scalping.volatility import VolatilitySignal


def _snapshot() -> MarketSnapshot:
    etf_bar = ETFBar(
        trading_date=date(2024, 4, 8),
        underlying="510050.XSHG",
        open=2.8,
        close=2.8,
        high=2.85,
        low=2.75,
        volume=1000,
        turnover=2800,
    )
    return MarketSnapshot(
        trading_date=date(2024, 4, 8),
        underlying="510050.XSHG",
        etf_bar=etf_bar,
        option_chain=OptionChain(date(2024, 4, 8), "510050.XSHG", pd.DataFrame()),
    )


def _greeks_frame(*, ttm: int = 10, greeks_status: str = "ok", iv_status: str = "ok") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_id": "CALL_ATM",
                "option_type": "C",
                "strike": 2.8,
                "maturity_date": date(2024, 4, 22),
                "ttm_trading_days": ttm,
                "buy_price": 0.05,
                "mid": 0.049,
                "bid": 0.048,
                "ask": 0.05,
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "delta": 0.52,
                "gamma": 1.0,
                "vega": 0.1,
                "theta": -0.01,
                "rho": 0.01,
                "greeks_status": greeks_status,
                "iv_status": iv_status,
            },
            {
                "contract_id": "PUT_ATM",
                "option_type": "P",
                "strike": 2.8,
                "maturity_date": date(2024, 4, 22),
                "ttm_trading_days": ttm,
                "buy_price": 0.05,
                "mid": 0.049,
                "bid": 0.048,
                "ask": 0.05,
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "delta": -0.48,
                "gamma": 1.0,
                "vega": 0.1,
                "theta": -0.01,
                "rho": -0.01,
                "greeks_status": greeks_status,
                "iv_status": iv_status,
            },
            {
                "contract_id": "CALL_OTM",
                "option_type": "C",
                "strike": 3.0,
                "maturity_date": date(2024, 4, 22),
                "ttm_trading_days": ttm,
                "buy_price": 0.02,
                "mid": 0.019,
                "bid": 0.018,
                "ask": 0.02,
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "delta": 0.2,
                "gamma": 0.8,
                "vega": 0.1,
                "theta": -0.01,
                "rho": 0.01,
                "greeks_status": greeks_status,
                "iv_status": iv_status,
            },
        ]
    )


def _without_same_strike_pair() -> pd.DataFrame:
    frame = _greeks_frame()
    frame.loc[frame["contract_id"].eq("PUT_ATM"), "strike"] = 2.75
    return frame


def _vol_signal(hv_iv_edge: float = 0.0) -> VolatilitySignal:
    hv_20 = 0.2 + hv_iv_edge
    return VolatilitySignal(
        trading_date=date(2024, 4, 8),
        underlying="510050.XSHG",
        atm_iv=0.2,
        hv_20=hv_20,
        iv_hv_spread=-hv_iv_edge,
        hv_iv_edge=hv_iv_edge,
        rv_reference=hv_20,
        rv_reference_source="current_hv:hv_20",
        rv_reference_status="ok",
        rv_observation_count=20,
        rv_iv_edge=hv_iv_edge,
        iv_rv_ratio=0.2 / hv_20,
        atm_iv_contract_count=2,
        atm_iv_contract_ids=("CALL_ATM", "PUT_ATM"),
        atm_iv_maturities=(date(2024, 4, 22),),
        iv_valid_count=2,
        iv_failed_count=0,
        iv_status_summary={"ok": 2},
    )


def test_strategy_opens_atm_straddle_and_initial_hedge() -> None:
    strategy = GammaScalpingStrategy(
        StrategyConfig(premium_budget_pct=0.5, underlying_instrument_id="510050.XSHG")
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(), PortfolioState(equity=100000))

    assert decision.action == "open"
    assert decision.selected_contracts == ("CALL_ATM", "PUT_ATM")
    assert [(order.instrument_id, order.side, order.quantity, order.role) for order in decision.order_intents[:2]] == [
        ("CALL_ATM", "buy", 50.0, "call_leg"),
        ("PUT_ATM", "buy", 50.0, "put_leg"),
    ]
    assert decision.order_intents[2].instrument_id == "510050.XSHG"
    assert decision.order_intents[2].side == "sell"
    assert decision.order_intents[2].quantity == pytest.approx(20000.0)
    assert decision.order_intents[2].role == "hedge"
    assert decision.episode_id == "gamma_scalping:20240408:CALL_ATM:PUT_ATM"
    assert decision.entry_atm_iv == pytest.approx(0.2)
    assert decision.entry_edge == pytest.approx(0.0)
    assert decision.entry_ratio == pytest.approx(1.0)
    assert decision.rv_reference_source == "current_hv:hv_20"
    assert all(order.episode_id == decision.episode_id for order in decision.order_intents)


def test_strategy_does_not_open_when_budget_is_insufficient() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(premium_budget_pct=0.001))

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(), PortfolioState(equity=100000))

    assert decision.action == "hold"
    assert decision.risk_flags == ("insufficient_budget",)
    assert decision.order_intents == ()


def test_strategy_requires_same_maturity_and_strike_pair() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(premium_budget_pct=0.5))

    decision = strategy.on_snapshot(
        _snapshot(),
        _without_same_strike_pair(),
        _vol_signal(),
        PortfolioState(equity=100000),
    )

    assert decision.action == "hold"
    assert decision.reason == "no_eligible_straddle"
    assert decision.risk_flags == ("no_eligible_straddle",)


def test_strategy_derives_buy_price_when_greeks_input_has_only_ask() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(premium_budget_pct=0.5))
    greeks = _greeks_frame().drop(columns=["buy_price", "mid"])

    decision = strategy.on_snapshot(_snapshot(), greeks, _vol_signal(), PortfolioState(equity=100000))

    assert decision.action == "open"
    assert decision.selected_contracts == ("CALL_ATM", "PUT_ATM")


def test_strategy_existing_position_only_hedges_not_reopen() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(delta_threshold_pct=0.001))
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_ATM", "option", quantity=1, multiplier=10000, role="call_leg"),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg"),
            StrategyPosition("510050.XSHG", "etf", quantity=0, role="hedge"),
        ),
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(), portfolio)

    assert decision.action == "hedge"
    assert len(decision.order_intents) == 1
    assert decision.order_intents[0].instrument_type == "etf"
    assert decision.order_intents[0].side == "sell"


def test_strategy_hedges_existing_episode_with_same_episode_id() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(delta_threshold_pct=0.001))
    episode_id = "gamma_scalping:20240408:CALL_ATM:PUT_ATM"
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_ATM", "option", quantity=1, multiplier=10000, role="call_leg", episode_id=episode_id),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg", episode_id=episode_id),
            StrategyPosition("510050.XSHG", "etf", quantity=0, role="hedge", episode_id=episode_id),
        ),
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(), portfolio)

    assert decision.action == "hedge"
    assert decision.episode_id == episode_id
    assert decision.order_intents[0].episode_id == episode_id


def test_strategy_holds_when_delta_is_within_threshold() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(delta_threshold_pct=0.1))
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_ATM", "option", quantity=1, multiplier=10000, role="call_leg"),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg"),
            StrategyPosition("510050.XSHG", "etf", quantity=-400, role="hedge"),
        ),
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(), portfolio)

    assert decision.action == "hold"
    assert decision.reason == "delta_within_threshold"
    assert decision.order_intents == ()


def test_strategy_closes_on_expired_or_bad_quality_position() -> None:
    strategy = GammaScalpingStrategy()
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_ATM", "option", quantity=1, multiplier=10000, role="call_leg"),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg"),
        ),
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(ttm=0), _vol_signal(), portfolio)

    assert decision.action == "close"
    assert decision.risk_flags == ("expired",)
    assert [(order.instrument_id, order.side, order.quantity) for order in decision.order_intents] == [
        ("CALL_ATM", "sell", 1.0),
        ("PUT_ATM", "sell", 1.0),
    ]


def test_strategy_skips_close_when_position_quote_is_missing() -> None:
    strategy = GammaScalpingStrategy()
    episode_id = "episode"
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_MISSING", "option", quantity=1, multiplier=10000, role="call_leg", episode_id=episode_id),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg", episode_id=episode_id),
        ),
    )

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(ttm=0), _vol_signal(), portfolio)

    assert decision.action == "hold"
    assert decision.reason == "missing_contract_quote_skip"
    assert "missing_contract" in decision.risk_flags
    assert decision.order_intents == ()
    assert decision.episode_id == episode_id


def test_strategy_vol_filter_placeholder_can_block_entry() -> None:
    strategy = GammaScalpingStrategy(StrategyConfig(use_vol_filter=True, min_hv_iv_edge=0.05))

    decision = strategy.on_snapshot(_snapshot(), _greeks_frame(), _vol_signal(hv_iv_edge=0.01), PortfolioState(100000))

    assert decision.action == "hold"
    assert decision.risk_flags == ("vol_filter",)


def test_strategy_vol_filter_requires_ratio_and_status() -> None:
    strategy = GammaScalpingStrategy(
        StrategyConfig(use_vol_filter=True, min_hv_iv_edge=0.01, entry_max_iv_rv_ratio=0.9)
    )

    ratio_blocked = strategy.on_snapshot(
        _snapshot(),
        _greeks_frame(),
        _vol_signal(hv_iv_edge=0.02),
        PortfolioState(100000),
    )

    assert ratio_blocked.action == "hold"
    assert ratio_blocked.reason == "vol_filter_not_satisfied"

    signal = _vol_signal(hv_iv_edge=0.05)
    bad_status = VolatilitySignal(
        **{**signal.__dict__, "rv_reference_status": "insufficient_history", "rv_iv_edge": 0.05, "iv_rv_ratio": 0.8}
    )
    status_blocked = strategy.on_snapshot(_snapshot(), _greeks_frame(), bad_status, PortfolioState(100000))

    assert status_blocked.action == "hold"
    assert status_blocked.risk_flags == ("vol_filter",)


def test_strategy_closes_when_vol_edge_is_filled_before_hedging() -> None:
    episode_id = "gamma_scalping:20240408:CALL_ATM:PUT_ATM"
    strategy = GammaScalpingStrategy(
        StrategyConfig(exit_on_vol_edge_filled=True, exit_min_iv_rv_ratio=1.0, delta_threshold_pct=0.001)
    )
    greeks = _greeks_frame()
    greeks.loc[greeks["contract_id"].isin(["CALL_ATM", "PUT_ATM"]), "iv"] = [0.25, 0.27]
    portfolio = PortfolioState(
        equity=100000,
        positions=(
            StrategyPosition("CALL_ATM", "option", quantity=1, multiplier=10000, role="call_leg", episode_id=episode_id),
            StrategyPosition("PUT_ATM", "option", quantity=1, multiplier=10000, role="put_leg", episode_id=episode_id),
            StrategyPosition("510050.XSHG", "etf", quantity=-100, role="hedge", episode_id=episode_id),
        ),
    )
    signal = _vol_signal(hv_iv_edge=0.04)

    decision = strategy.on_snapshot(_snapshot(), greeks, signal, portfolio)

    assert decision.action == "close"
    assert decision.reason == "exit_vol_edge_filled"
    assert decision.risk_flags == ("vol_edge_filled",)
    assert [order.instrument_type for order in decision.order_intents] == ["option", "option", "etf"]
    assert decision.entry_edge == pytest.approx(-0.02)
    assert decision.entry_ratio == pytest.approx(0.26 / 0.24)
