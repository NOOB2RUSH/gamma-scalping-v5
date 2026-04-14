from __future__ import annotations

import pandas as pd


def row_for_date(frame: pd.DataFrame, trading_date: object) -> pd.Series:
    if trading_date in frame.index:
        return frame.loc[trading_date]
    timestamp = pd.Timestamp(trading_date)
    if timestamp in frame.index:
        return frame.loc[timestamp]
    return pd.Series(dtype=float)

