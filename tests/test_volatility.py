from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from gamma_scalping.data.models import ETFBar, MarketSnapshot, OptionChain
from gamma_scalping.greeks import GreeksCalculator, GreeksConfig
from gamma_scalping.volatility import AtmIvConfig, VolatilityConfig, VolatilityEngine


def _priced_option_chain() -> OptionChain:
    calculator = GreeksCalculator(GreeksConfig(backend="black_scholes"))
    rows = []
    for contract_id, option_type, strike, ttm in [
        ("C1", "C", 100.0, 10),
        ("P1", "P", 100.0, 10),
        ("C2", "C", 105.0, 10),
        ("P2", "P", 95.0, 10),
        ("C3", "C", 100.0, 30),
    ]:
        price = calculator.price(option_type, 100.0, strike, ttm, 0.2)
        rows.append(
            {
                "contract_id": contract_id,
                "strike": strike,
                "maturity_date": date(2024, 4, 22),
                "option_type": option_type,
                "bid": price * 0.99,
                "ask": price * 1.01,
                "mid": price,
                "last": price,
                "mark_price": price,
                "price_quality": "mid",
                "volume": 100,
                "open_interest": 1000,
                "multiplier": 10000,
                "ttm_trading_days": ttm,
                "maturity_session": date(2024, 4, 22),
            }
        )
    return OptionChain(
        trading_date=date(2024, 4, 8),
        underlying="TEST",
        frame=pd.DataFrame(rows),
    )


def _snapshot() -> MarketSnapshot:
    etf_bar = ETFBar(
        trading_date=date(2024, 4, 8),
        underlying="TEST",
        open=100.0,
        close=100.0,
        high=101.0,
        low=99.0,
        volume=1000.0,
        turnover=100000.0,
    )
    return MarketSnapshot(
        trading_date=date(2024, 4, 8),
        underlying="TEST",
        etf_bar=etf_bar,
        option_chain=_priced_option_chain(),
    )


def test_compute_hv_uses_trading_day_annualization() -> None:
    close = pd.Series([100.0, 101.0, 102.0, 101.5, 103.0], index=pd.date_range("2024-01-01", periods=5))
    history = pd.DataFrame({"close": close})
    engine = VolatilityEngine(VolatilityConfig(annual_trading_days=252, hv_windows=(2,)))

    hv = engine.compute_hv(history)

    returns = np.log(close / close.shift(1))
    expected = returns.rolling(2, min_periods=2).std() * np.sqrt(252)
    pd.testing.assert_series_equal(hv["hv_2"], expected, check_names=False)


def test_solve_iv_chain_recovers_input_sigma() -> None:
    surface = VolatilityEngine().solve_iv_chain(_snapshot())

    assert surface["iv_status"].value_counts().to_dict() == {"ok": 5}
    assert surface.loc[surface["contract_id"].eq("C1"), "iv"].iloc[0] == pytest.approx(0.2, rel=1e-4)
    assert surface.loc[surface["contract_id"].eq("P1"), "iv"].iloc[0] == pytest.approx(0.2, rel=1e-4)


def test_solve_iv_chain_uses_bisection_fallback(monkeypatch) -> None:
    engine = VolatilityEngine()

    def fail_vectorized(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(engine, "_solve_iv_vectorized", fail_vectorized)
    surface = engine.solve_iv_chain(_snapshot())

    assert surface["iv_status"].value_counts().to_dict() == {"ok": 5}
    assert surface.loc[surface["contract_id"].eq("C1"), "iv"].iloc[0] == pytest.approx(0.2, rel=1e-4)


def test_dividend_rate_nonzero_explicitly_fails() -> None:
    with pytest.raises(ValueError, match="dividend_rate=0"):
        VolatilityEngine(VolatilityConfig(dividend_rate=0.01))


def test_atm_iv_config_aggregates_5_to_20_dte_contracts() -> None:
    engine = VolatilityEngine()
    surface = engine.solve_iv_chain(_snapshot())

    result = engine.atm_iv(surface, AtmIvConfig(min_ttm_days=5, max_ttm_days=20, option_types=("c", "p")))

    assert result.atm_iv == pytest.approx(0.2, rel=1e-4)
    assert result.contract_count == 2
    assert result.contract_ids == ("C1", "P1")
    assert result.min_ttm_days == 5
    assert result.max_ttm_days == 20


def test_invalid_iv_price_keeps_row_with_status() -> None:
    snapshot = _snapshot()
    frame = snapshot.option_chain.frame.copy()
    frame.loc[0, "mid"] = -1.0
    frame.loc[0, "last"] = -1.0
    bad_snapshot = MarketSnapshot(
        trading_date=snapshot.trading_date,
        underlying=snapshot.underlying,
        etf_bar=snapshot.etf_bar,
        option_chain=OptionChain(snapshot.trading_date, snapshot.underlying, frame),
    )

    surface = VolatilityEngine().solve_iv_chain(bad_snapshot)

    assert surface.loc[0, "iv_status"] == "invalid_price"
    assert pd.isna(surface.loc[0, "iv"])
    assert surface.loc[1, "iv_status"] == "ok"


def test_build_signal_series_outputs_visualization_columns() -> None:
    snapshot = _snapshot()
    history = pd.DataFrame(
        {"close": np.linspace(90.0, 100.0, 80)},
        index=pd.date_range("2024-01-01", periods=80),
    )
    history.loc[pd.Timestamp("2024-04-08"), "close"] = 100.0
    engine = VolatilityEngine()

    series = engine.build_signal_series([snapshot], history, AtmIvConfig(min_ttm_days=5, max_ttm_days=20))
    frame = series.frame

    for column in [
        "trading_date",
        "atm_iv",
        "atm_iv_contract_count",
        "atm_iv_contract_ids",
        "hv_10",
        "hv_20",
        "hv_60",
        "iv_hv_spread",
        "hv_iv_edge",
        "iv_status_summary",
    ]:
        assert column in frame.columns
    assert frame.loc[0, "atm_iv"] == pytest.approx(0.2, rel=1e-4)
    assert frame.loc[0, "atm_iv_contract_count"] == 2
