from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

import pandas as pd


UNDERLYING_DISPLAY_NAMES = {
    "510050.XSHG": "50ETF",
    "510300.XSHG": "300ETF",
    "159919.XSHE": "300ETF",
}


def option_contract_name(row: pd.Series | None, underlying: str) -> str:
    if row is None:
        return ""
    try:
        maturity = pd.Timestamp(row["maturity_date"])
        option_type = str(row["option_type"]).upper()[0]
        strike = _strike_code(row["strike"])
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    if pd.isna(maturity) or option_type not in {"C", "P"} or not strike:
        return ""
    return f"{_underlying_name(underlying)}{maturity:%Y%m}{option_type}{strike}"


def _underlying_name(underlying: str) -> str:
    if underlying in UNDERLYING_DISPLAY_NAMES:
        return UNDERLYING_DISPLAY_NAMES[underlying]
    base = str(underlying).split(".", 1)[0]
    return re.sub(r"[^0-9A-Za-z]", "", base)


def _strike_code(value: object) -> str:
    try:
        scaled = (Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ""
    return f"{int(scaled):04d}"
