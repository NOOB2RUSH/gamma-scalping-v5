from __future__ import annotations

import pandas as pd

from gamma_scalping.export_format import format_for_csv


def test_format_for_csv_rounds_money_prices_greeks_and_volatility() -> None:
    frame = pd.DataFrame(
        {
            "trade_amount": [30419.999999999996],
            "theoretical_unrealized_pnl": [12.345],
            "price": [0.05123456],
            "quantity": [84027.49556277],
            "delta": [0.123456],
            "option_gamma_exposure": [123.456789],
            "atm_iv": [0.15811642],
            "hv_20": [0.1234567],
            "sortino_ratio": [51.123456],
        }
    )

    formatted = format_for_csv(frame)

    assert formatted.loc[0, "trade_amount"] == "30420.00"
    assert formatted.loc[0, "theoretical_unrealized_pnl"] == "12.35"
    assert formatted.loc[0, "price"] == "0.0512"
    assert formatted.loc[0, "quantity"] == "84027.4956"
    assert formatted.loc[0, "delta"] == "0.1235"
    assert formatted.loc[0, "option_gamma_exposure"] == "123.4568"
    assert formatted.loc[0, "atm_iv"] == "0.1581"
    assert formatted.loc[0, "hv_20"] == "0.1235"
    assert formatted.loc[0, "sortino_ratio"] == "51.1235"
