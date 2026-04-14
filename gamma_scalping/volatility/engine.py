from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
import os
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.utils import row_for_date

Aggregation = Literal["mean", "median"]


@dataclass(frozen=True)
class VolatilityConfig:
    risk_free_rate: float = 0.0
    dividend_rate: float = 0.0
    annual_trading_days: int = 252
    hv_windows: tuple[int, ...] = (10, 20, 60)
    iv_price_column: str = "mid"
    iv_fallback_price_column: str = "last"
    iv_backend: Literal["py_vollib_vectorized"] = "py_vollib_vectorized"


@dataclass(frozen=True)
class AtmIvConfig:
    min_ttm_days: int = 5
    max_ttm_days: int = 20
    option_types: tuple[str, ...] = ("C", "P")
    per_maturity_atm_only: bool = True
    aggregation: Aggregation = "mean"
    allow_single_side: bool = True


@dataclass(frozen=True)
class AtmIvResult:
    atm_iv: float
    contract_ids: tuple[str, ...]
    maturities: tuple[date, ...]
    contract_count: int
    min_ttm_days: int
    max_ttm_days: int
    aggregation: str
    status: str = "ok"


@dataclass(frozen=True)
class VolatilitySignal:
    trading_date: date
    underlying: str
    atm_iv: float
    hv_20: float
    iv_hv_spread: float
    hv_iv_edge: float
    atm_iv_contract_count: int
    atm_iv_contract_ids: tuple[str, ...]
    atm_iv_maturities: tuple[date, ...]
    iv_valid_count: int
    iv_failed_count: int
    iv_status_summary: dict[str, int]


@dataclass(frozen=True)
class VolatilityTimeSeries:
    underlying: str
    frame: pd.DataFrame


class VolatilityEngine:
    def __init__(self, config: VolatilityConfig | None = None) -> None:
        self.config = config or VolatilityConfig()
        if self.config.dividend_rate != 0:
            raise ValueError("VolatilityEngine first version supports only dividend_rate=0 for IV solving.")

    def compute_hv(self, etf_history: pd.DataFrame, windows: Iterable[int] | None = None) -> pd.DataFrame:
        if "close" not in etf_history.columns:
            raise ValueError("etf_history must contain a 'close' column")
        windows = tuple(windows or self.config.hv_windows)
        history = etf_history.copy()
        history = history.sort_index()
        close = pd.to_numeric(history["close"], errors="coerce")
        log_return = np.log(close / close.shift(1))

        result = pd.DataFrame(index=history.index)
        result["log_return"] = log_return
        for window in windows:
            result[f"hv_{window}"] = log_return.rolling(window=window, min_periods=window).std() * math.sqrt(
                self.config.annual_trading_days
            )
        return result

    def solve_iv_chain(self, snapshot: MarketSnapshot) -> pd.DataFrame:
        if self.config.dividend_rate != 0:
            raise ValueError("IV solving supports only dividend_rate=0 in the first version.")

        frame = snapshot.option_chain.frame.copy()
        price = self._select_iv_price(frame)
        spot = float(snapshot.etf_bar.close)
        frame["iv_price"] = price
        frame["moneyness"] = frame["strike"] / spot
        frame["ttm_years"] = frame["ttm_trading_days"] / self.config.annual_trading_days
        frame["iv"] = np.nan
        frame["iv_status"] = self._initial_iv_status(frame, spot=spot)

        valid = frame["iv_status"].eq("ok")
        if valid.any():
            try:
                frame.loc[valid, "iv"] = self._solve_iv_vectorized(frame.loc[valid], spot=spot)
            except Exception:
                frame.loc[valid, "iv"] = self._solve_iv_bisection(frame.loc[valid], spot=spot)
            invalid_iv = frame["iv"].isna() | (frame["iv"] <= 0) | np.isinf(frame["iv"])
            frame.loc[invalid_iv & valid, "iv_status"] = "failed"
        return frame

    def atm_iv(self, surface: pd.DataFrame, config: AtmIvConfig | None = None) -> AtmIvResult:
        config = config or AtmIvConfig()
        option_types = {option_type.upper() for option_type in config.option_types}
        eligible = surface[
            surface["iv_status"].eq("ok")
            & surface["option_type"].isin(option_types)
            & surface["ttm_trading_days"].between(config.min_ttm_days, config.max_ttm_days)
        ].copy()
        if eligible.empty:
            return AtmIvResult(
                atm_iv=np.nan,
                contract_ids=(),
                maturities=(),
                contract_count=0,
                min_ttm_days=config.min_ttm_days,
                max_ttm_days=config.max_ttm_days,
                aggregation=config.aggregation,
                status="empty",
            )

        eligible["atm_score"] = (eligible["moneyness"] - 1.0).abs()
        selected = self._select_atm_contracts(eligible, config)
        if selected.empty:
            return AtmIvResult(
                atm_iv=np.nan,
                contract_ids=(),
                maturities=(),
                contract_count=0,
                min_ttm_days=config.min_ttm_days,
                max_ttm_days=config.max_ttm_days,
                aggregation=config.aggregation,
                status="empty",
            )

        if config.aggregation == "mean":
            atm_iv = float(selected["iv"].mean())
        elif config.aggregation == "median":
            atm_iv = float(selected["iv"].median())
        else:
            raise ValueError(f"Unsupported ATM IV aggregation: {config.aggregation}")

        return AtmIvResult(
            atm_iv=atm_iv,
            contract_ids=tuple(str(value) for value in selected["contract_id"].tolist()),
            maturities=tuple(selected["maturity_date"].tolist()),
            contract_count=int(len(selected)),
            min_ttm_days=config.min_ttm_days,
            max_ttm_days=config.max_ttm_days,
            aggregation=config.aggregation,
        )

    def build_signal(
        self,
        surface: pd.DataFrame,
        hv_state: pd.Series,
        atm_config: AtmIvConfig | None = None,
        *,
        trading_date: date,
        underlying: str,
    ) -> VolatilitySignal:
        atm = self.atm_iv(surface, atm_config)
        hv_20 = float(hv_state.get("hv_20", np.nan))
        iv_hv_spread = atm.atm_iv - hv_20 if not (math.isnan(atm.atm_iv) or math.isnan(hv_20)) else np.nan
        status_summary = surface["iv_status"].value_counts(dropna=False).to_dict()
        return VolatilitySignal(
            trading_date=trading_date,
            underlying=underlying,
            atm_iv=atm.atm_iv,
            hv_20=hv_20,
            iv_hv_spread=iv_hv_spread,
            hv_iv_edge=-iv_hv_spread if not math.isnan(iv_hv_spread) else np.nan,
            atm_iv_contract_count=atm.contract_count,
            atm_iv_contract_ids=atm.contract_ids,
            atm_iv_maturities=atm.maturities,
            iv_valid_count=int(status_summary.get("ok", 0)),
            iv_failed_count=int(len(surface) - status_summary.get("ok", 0)),
            iv_status_summary={str(key): int(value) for key, value in status_summary.items()},
        )

    def build_signal_series(
        self,
        snapshots: Iterable[MarketSnapshot],
        etf_history: pd.DataFrame,
        atm_config: AtmIvConfig | None = None,
    ) -> VolatilityTimeSeries:
        hv = self.compute_hv(etf_history)
        rows: list[dict[str, object]] = []
        underlying = ""
        for snapshot in snapshots:
            underlying = snapshot.underlying
            surface = self.solve_iv_chain(snapshot)
            hv_state = row_for_date(hv, snapshot.trading_date)
            signal = self.build_signal(
                surface,
                hv_state,
                atm_config,
                trading_date=snapshot.trading_date,
                underlying=snapshot.underlying,
            )
            row = {
                "trading_date": signal.trading_date,
                "underlying": signal.underlying,
                "atm_iv": signal.atm_iv,
                "atm_iv_contract_count": signal.atm_iv_contract_count,
                "atm_iv_contract_ids": json.dumps(signal.atm_iv_contract_ids),
                "atm_iv_maturities": json.dumps([str(value) for value in signal.atm_iv_maturities]),
                "atm_iv_min_ttm_days": (atm_config or AtmIvConfig()).min_ttm_days,
                "atm_iv_max_ttm_days": (atm_config or AtmIvConfig()).max_ttm_days,
                "hv_10": float(hv_state.get("hv_10", np.nan)),
                "hv_20": float(hv_state.get("hv_20", np.nan)),
                "hv_60": float(hv_state.get("hv_60", np.nan)),
                "iv_hv_spread": signal.iv_hv_spread,
                "hv_iv_edge": signal.hv_iv_edge,
                "term_slope": np.nan,
                "iv_valid_count": signal.iv_valid_count,
                "iv_failed_count": signal.iv_failed_count,
                "iv_status_summary": json.dumps(signal.iv_status_summary, sort_keys=True),
            }
            rows.append(row)
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame = frame.sort_values("trading_date").reset_index(drop=True)
        return VolatilityTimeSeries(underlying=underlying, frame=frame)

    def _select_iv_price(self, frame: pd.DataFrame) -> pd.Series:
        if self.config.iv_price_column not in frame.columns:
            raise ValueError(f"Missing IV price column: {self.config.iv_price_column}")
        price = pd.to_numeric(frame[self.config.iv_price_column], errors="coerce")
        if self.config.iv_fallback_price_column in frame.columns:
            fallback = pd.to_numeric(frame[self.config.iv_fallback_price_column], errors="coerce")
            price = price.where(price.notna() & (price > 0), fallback)
        return price

    def _initial_iv_status(self, frame: pd.DataFrame, *, spot: float) -> pd.Series:
        status = pd.Series("ok", index=frame.index, dtype="object")
        price = pd.to_numeric(frame["iv_price"], errors="coerce")
        strike = pd.to_numeric(frame["strike"], errors="coerce")
        ttm_years = pd.to_numeric(frame["ttm_years"], errors="coerce")
        option_type = frame["option_type"].astype("string").str.upper()
        intrinsic = pd.Series(0.0, index=frame.index)
        intrinsic.loc[option_type.eq("C")] = np.maximum(spot - strike.loc[option_type.eq("C")], 0.0)
        intrinsic.loc[option_type.eq("P")] = np.maximum(strike.loc[option_type.eq("P")] - spot, 0.0)
        upper = pd.Series(np.nan, index=frame.index)
        upper.loc[option_type.eq("C")] = spot
        upper.loc[option_type.eq("P")] = strike.loc[option_type.eq("P")]

        invalid_option_type = ~option_type.isin(["C", "P"])
        invalid_price = price.isna() | (price <= 0)
        invalid_strike = strike.isna() | (strike <= 0)
        expired = ttm_years.isna() | (ttm_years <= 0)
        structurally_valid = ~(invalid_option_type | invalid_price | invalid_strike | expired)

        status.loc[structurally_valid & (price < intrinsic)] = "below_intrinsic"
        status.loc[structurally_valid & (price > upper)] = "above_upper_bound"
        status.loc[invalid_option_type] = "invalid_option_type"
        status.loc[invalid_price] = "invalid_price"
        status.loc[invalid_strike] = "invalid_strike"
        status.loc[expired] = "expired"
        return status

    def _solve_iv_vectorized(self, frame: pd.DataFrame, *, spot: float) -> pd.Series:
        os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
        import py_vollib_vectorized as pvv

        iv = pvv.vectorized_implied_volatility(
            frame["iv_price"].to_numpy(dtype=float),
            np.full(len(frame), spot, dtype=float),
            frame["strike"].to_numpy(dtype=float),
            frame["ttm_years"].to_numpy(dtype=float),
            np.full(len(frame), self.config.risk_free_rate, dtype=float),
            frame["option_type"].astype("string").str.lower().to_list(),
            model="black_scholes",
            on_error="ignore",
            return_as="dataframe",
        )
        column = "IV" if "IV" in iv.columns else iv.columns[0]
        return pd.Series(iv[column].to_numpy(dtype=float), index=frame.index)

    def _solve_iv_bisection(self, frame: pd.DataFrame, *, spot: float) -> pd.Series:
        values = []
        for row in frame.itertuples():
            values.append(
                _implied_vol_bisection(
                    target_price=float(row.iv_price),
                    flag=str(row.option_type).lower(),
                    spot=spot,
                    strike=float(row.strike),
                    ttm_years=float(row.ttm_years),
                    risk_free_rate=self.config.risk_free_rate,
                )
            )
        return pd.Series(values, index=frame.index)

    @staticmethod
    def _select_atm_contracts(eligible: pd.DataFrame, config: AtmIvConfig) -> pd.DataFrame:
        if not config.per_maturity_atm_only:
            return eligible

        selected_parts: list[pd.DataFrame] = []
        option_types = {option_type.upper() for option_type in config.option_types}
        for _, maturity_group in eligible.groupby("maturity_date", sort=True):
            side_parts: list[pd.DataFrame] = []
            for option_type, side in maturity_group.groupby("option_type", sort=True):
                if option_type not in option_types:
                    continue
                side_parts.append(side.nsmallest(1, "atm_score"))
            if not side_parts:
                continue
            selected = pd.concat(side_parts)
            if not config.allow_single_side and set(selected["option_type"]) != option_types:
                continue
            selected_parts.append(selected)
        if not selected_parts:
            return eligible.iloc[0:0]
        return pd.concat(selected_parts).sort_values(["maturity_date", "option_type", "atm_score"])

def _implied_vol_bisection(
    *,
    target_price: float,
    flag: str,
    spot: float,
    strike: float,
    ttm_years: float,
    risk_free_rate: float,
    lower: float = 0.0001,
    upper: float = 5.0,
    tolerance: float = 1e-8,
    max_iterations: int = 100,
) -> float:
    low = lower
    high = upper
    low_price = _black_scholes_price(flag, spot, strike, ttm_years, risk_free_rate, low)
    high_price = _black_scholes_price(flag, spot, strike, ttm_years, risk_free_rate, high)
    if target_price < low_price or target_price > high_price:
        return np.nan
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        mid_price = _black_scholes_price(flag, spot, strike, ttm_years, risk_free_rate, mid)
        if abs(mid_price - target_price) < tolerance:
            return mid
        if mid_price < target_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _black_scholes_price(
    flag: str,
    spot: float,
    strike: float,
    ttm_years: float,
    risk_free_rate: float,
    sigma: float,
) -> float:
    sqrt_t = math.sqrt(ttm_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * ttm_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-risk_free_rate * ttm_years)
    if flag == "c":
        return spot * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)
    return strike * discount * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
