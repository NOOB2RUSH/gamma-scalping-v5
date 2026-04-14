from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from gamma_scalping.data.models import OptionChain
from gamma_scalping.greeks import GreeksCalculator, GreeksConfig, Position


def _option_chain() -> OptionChain:
    return OptionChain(
        trading_date=date(2024, 4, 8),
        underlying="510050.XSHG",
        frame=pd.DataFrame(
            {
                "contract_id": ["CALL1", "PUT1"],
                "strike": [100.0, 100.0],
                "maturity_date": [date(2024, 5, 8), date(2024, 5, 8)],
                "option_type": ["C", "P"],
                "bid": [4.0, 4.0],
                "ask": [4.2, 4.2],
                "mid": [4.1, 4.1],
                "mark_price": [4.1, 4.1],
                "volume": [100, 100],
                "open_interest": [1000, 1000],
                "multiplier": [10000, 10000],
                "ttm_trading_days": [252, 252],
            }
        ),
    )


def test_scalar_greeks_have_expected_signs() -> None:
    calculator = GreeksCalculator(GreeksConfig(backend="black_scholes"))

    call = calculator.greeks("C", spot=100, strike=100, ttm_trading_days=252, sigma=0.2)
    put = calculator.greeks("P", spot=100, strike=100, ttm_trading_days=252, sigma=0.2)

    assert call.price == pytest.approx(7.965567, rel=1e-6)
    assert call.delta > 0
    assert put.delta < 0
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega == pytest.approx(put.vega)
    assert call.theta < 0
    assert put.theta < 0


def test_enrich_chain_vectorized_outputs_expected_columns() -> None:
    calculator = GreeksCalculator()

    enriched = calculator.enrich_chain(_option_chain(), spot=100, sigma=0.2)

    for column in [
        "theoretical_price",
        "delta",
        "gamma",
        "vega",
        "theta",
        "rho",
        "delta_notional_per_contract",
        "greeks_status",
    ]:
        assert column in enriched.columns
    assert enriched["greeks_status"].tolist() == ["ok", "ok"]
    assert enriched.loc[0, "delta"] > 0
    assert enriched.loc[1, "delta"] < 0
    assert enriched.loc[0, "vega"] > 1.0


def test_portfolio_greeks_apply_multiplier_and_etf_delta() -> None:
    calculator = GreeksCalculator(GreeksConfig(backend="black_scholes"))
    enriched = calculator.enrich_chain(_option_chain(), spot=100, sigma=0.2)
    positions = [
        Position("CALL1", "option", quantity=1),
        Position("PUT1", "option", quantity=1),
        Position("ETF", "etf", quantity=-1000),
    ]

    portfolio = calculator.portfolio_greeks(positions, enriched)
    hedge_order = calculator.hedge_order_quantity(positions, enriched)

    expected_delta = (enriched.loc[0, "delta"] + enriched.loc[1, "delta"]) * 10000 - 1000
    assert portfolio.delta == pytest.approx(expected_delta)
    assert portfolio.gamma == pytest.approx((enriched.loc[0, "gamma"] + enriched.loc[1, "gamma"]) * 10000)
    assert hedge_order == pytest.approx(-portfolio.delta)


def test_invalid_inputs_return_status_without_calling_backend() -> None:
    calculator = GreeksCalculator()
    chain = _option_chain()
    chain.frame.loc[0, "ttm_trading_days"] = 0

    enriched = calculator.enrich_chain(chain, spot=100, sigma=0.2)

    assert enriched.loc[0, "greeks_status"] == "expired"
    assert pd.isna(enriched.loc[0, "delta"])
    assert enriched.loc[1, "greeks_status"] == "ok"


def test_py_vollib_and_local_backend_match_on_core_outputs() -> None:
    vectorized = GreeksCalculator(GreeksConfig(backend="py_vollib_vectorized")).greeks(
        "C", spot=100, strike=100, ttm_trading_days=252, sigma=0.2
    )
    local = GreeksCalculator(GreeksConfig(backend="black_scholes")).greeks(
        "C", spot=100, strike=100, ttm_trading_days=252, sigma=0.2
    )

    assert vectorized.price == pytest.approx(local.price, rel=1e-6)
    assert vectorized.delta == pytest.approx(local.delta, rel=1e-6)
    assert vectorized.gamma == pytest.approx(local.gamma, rel=1e-6)
    assert vectorized.vega == pytest.approx(local.vega, rel=1e-5)
    assert vectorized.rho == pytest.approx(local.rho, rel=1e-4)
