from __future__ import annotations

import pandas as pd
import numpy as np


MONEY_COLUMNS = {
    "cash",
    "market_value",
    "equity",
    "pre_trade_market_value",
    "pre_trade_equity",
    "cumulative_fee",
    "realized_pnl",
    "trade_amount",
    "fee",
    "liquidation_value",
    "cost_basis_value",
    "theoretical_unrealized_pnl",
    "cash_flow",
    "final_cash_pnl",
    "initial_equity",
    "final_equity",
    "total_trade_amount",
    "total_fee",
    "net_gamma_scalping_pnl",
    "theoretical_vol_edge_pnl",
}

PRICE_COLUMNS = {
    "price",
    "avg_price",
    "mark_price",
    "liquidation_price",
    "entry_spot",
}

QUANTITY_COLUMNS = {
    "quantity",
    "contract_quantity",
    "initial_hedge_quantity",
}


def format_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    formatted = frame.copy()
    for column in formatted.columns:
        if not pd.api.types.is_numeric_dtype(formatted[column]):
            continue
        decimals = _decimals_for_column(str(column))
        if decimals is not None:
            formatted[column] = _format_decimal_series(formatted[column], decimals)
    return formatted


def _format_decimal_series(series: pd.Series, decimals: int) -> pd.Series:
    rounded = _round_half_away_from_zero(series, decimals)
    return rounded.map(lambda value: "" if pd.isna(value) else f"{value:.{decimals}f}")


def _round_half_away_from_zero(series: pd.Series, decimals: int) -> pd.Series:
    factor = 10.0**decimals
    values = series.astype(float)
    rounded = np.sign(values) * np.floor(np.abs(values) * factor + 0.5) / factor
    return pd.Series(rounded, index=series.index)


def _decimals_for_column(column: str) -> int | None:
    if column in MONEY_COLUMNS or column.endswith("_pnl"):
        return 2
    if column in PRICE_COLUMNS:
        return 4
    if column in QUANTITY_COLUMNS:
        return 4
    if _is_greeks_column(column) or _is_volatility_column(column) or _is_ratio_column(column):
        return 4
    return None


def _is_greeks_column(column: str) -> bool:
    greeks = ("delta", "gamma", "theta", "vega", "rho")
    return column in greeks or any(token in column for token in greeks)


def _is_volatility_column(column: str) -> bool:
    return (
        column == "iv"
        or column == "atm_iv"
        or column == "rv_reference"
        or column == "entry_edge"
        or column.startswith("hv_")
        or column.endswith("_iv")
        or "_iv_" in column
        or column.endswith("_vol")
        or column.endswith("_volatility")
        or "volatility" in column
        or column == "realized_vol_holding"
    )


def _is_ratio_column(column: str) -> bool:
    return (
        column.endswith("_ratio")
        or column.endswith("_rate")
        or column.endswith("_return")
        or column in {"return", "monthly_return", "var", "cvar"}
    )
