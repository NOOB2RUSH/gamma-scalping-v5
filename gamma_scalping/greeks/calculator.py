from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Literal

import numpy as np
import pandas as pd

from gamma_scalping.data.models import OptionChain
from gamma_scalping.greeks.models import OptionGreeks, PortfolioGreeks, Position

Backend = Literal["py_vollib_vectorized", "black_scholes"]


@dataclass(frozen=True)
class GreeksConfig:
    risk_free_rate: float = 0.0
    dividend_rate: float = 0.0
    annual_trading_days: int = 252
    backend: Backend = "py_vollib_vectorized"
    allow_fallback: bool = True


class GreeksCalculator:
    def __init__(self, config: GreeksConfig | None = None, backend: Backend | None = None) -> None:
        if config is None:
            config = GreeksConfig(backend=backend or "py_vollib_vectorized")
        elif backend is not None:
            config = GreeksConfig(
                risk_free_rate=config.risk_free_rate,
                dividend_rate=config.dividend_rate,
                annual_trading_days=config.annual_trading_days,
                backend=backend,
                allow_fallback=config.allow_fallback,
            )
        self.config = config

    def price(
        self,
        option_type: str,
        spot: float,
        strike: float,
        ttm_trading_days: int,
        sigma: float,
        *,
        risk_free_rate: float | None = None,
        dividend_rate: float | None = None,
    ) -> float:
        greeks = self.greeks(
            option_type=option_type,
            spot=spot,
            strike=strike,
            ttm_trading_days=ttm_trading_days,
            sigma=sigma,
            risk_free_rate=risk_free_rate,
            dividend_rate=dividend_rate,
        )
        return greeks.price

    def greeks(
        self,
        option_type: str,
        spot: float,
        strike: float,
        ttm_trading_days: int,
        sigma: float,
        *,
        risk_free_rate: float | None = None,
        dividend_rate: float | None = None,
    ) -> OptionGreeks:
        rate = self.config.risk_free_rate if risk_free_rate is None else risk_free_rate
        dividend = self.config.dividend_rate if dividend_rate is None else dividend_rate
        frame = pd.DataFrame(
            {
                "option_type": [option_type],
                "strike": [strike],
                "ttm_trading_days": [ttm_trading_days],
                "sigma": [sigma],
            }
        )
        result = self._compute_frame(frame, spot=spot, risk_free_rate=rate, dividend_rate=dividend)
        row = result.iloc[0]
        return OptionGreeks(
            price=float(row["theoretical_price"]),
            delta=float(row["delta"]),
            gamma=float(row["gamma"]),
            vega=float(row["vega"]),
            theta=float(row["theta"]),
            rho=float(row["rho"]),
            status=str(row["greeks_status"]),
        )

    def enrich_chain(
        self,
        chain: OptionChain,
        *,
        spot: float,
        sigma: float | pd.Series | pd.DataFrame,
        risk_free_rate: float | None = None,
        dividend_rate: float | None = None,
    ) -> pd.DataFrame:
        frame = chain.frame.copy()
        frame["sigma"] = self._align_sigma(frame, sigma)
        rate = self.config.risk_free_rate if risk_free_rate is None else risk_free_rate
        dividend = self.config.dividend_rate if dividend_rate is None else dividend_rate
        greeks = self._compute_frame(frame, spot=spot, risk_free_rate=rate, dividend_rate=dividend)
        for column in ["theoretical_price", "delta", "gamma", "vega", "theta", "rho", "greeks_status"]:
            frame[column] = greeks[column].to_numpy()
        for column in ["delta", "gamma", "vega", "theta", "rho"]:
            frame[f"{column}_notional_per_contract"] = frame[column] * frame["multiplier"]
        return frame

    def portfolio_greeks(self, positions: list[Position], greeks: pd.DataFrame) -> PortfolioGreeks:
        if not positions:
            return PortfolioGreeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

        greeks_by_contract = greeks.set_index("contract_id")
        totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
        for position in positions:
            if position.instrument_type.lower() in {"etf", "underlying"}:
                totals["delta"] += position.quantity
                continue
            if position.instrument_id not in greeks_by_contract.index:
                raise KeyError(f"Missing greeks for position {position.instrument_id}")
            row = greeks_by_contract.loc[position.instrument_id]
            multiplier = float(row.get("multiplier", position.multiplier))
            for key in totals:
                totals[key] += float(row[key]) * position.quantity * multiplier
        return PortfolioGreeks(**totals)

    def hedge_order_quantity(self, positions: list[Position], greeks: pd.DataFrame) -> float:
        portfolio = self.portfolio_greeks(positions, greeks)
        return -portfolio.delta

    def _compute_frame(
        self,
        frame: pd.DataFrame,
        *,
        spot: float,
        risk_free_rate: float,
        dividend_rate: float,
    ) -> pd.DataFrame:
        prepared = self._prepare_inputs(frame, spot=spot)
        valid = prepared["greeks_status"].eq("ok")
        result = pd.DataFrame(
            {
                "theoretical_price": np.nan,
                "delta": np.nan,
                "gamma": np.nan,
                "vega": np.nan,
                "theta": np.nan,
                "rho": np.nan,
                "greeks_status": prepared["greeks_status"],
            },
            index=frame.index,
        )
        if not valid.any():
            return result

        try:
            computed = self._compute_with_backend(
                prepared.loc[valid],
                risk_free_rate=risk_free_rate,
                dividend_rate=dividend_rate,
            )
        except Exception:
            if self.config.backend != "py_vollib_vectorized" or not self.config.allow_fallback:
                raise
            computed = self._compute_black_scholes_frame(
                prepared.loc[valid],
                risk_free_rate=risk_free_rate,
                dividend_rate=dividend_rate,
            )

        result.loc[valid, ["theoretical_price", "delta", "gamma", "vega", "theta", "rho"]] = computed[
            ["theoretical_price", "delta", "gamma", "vega", "theta", "rho"]
        ]
        result.loc[valid, "greeks_status"] = "ok"
        invalid_numeric = result[["theoretical_price", "delta", "gamma", "vega", "theta", "rho"]].isna().any(axis=1)
        result.loc[invalid_numeric & valid, "greeks_status"] = "failed"
        return result

    def _compute_with_backend(
        self,
        prepared: pd.DataFrame,
        *,
        risk_free_rate: float,
        dividend_rate: float,
    ) -> pd.DataFrame:
        if self.config.backend == "black_scholes":
            return self._compute_black_scholes_frame(
                prepared,
                risk_free_rate=risk_free_rate,
                dividend_rate=dividend_rate,
            )
        return self._compute_py_vollib_vectorized(
            prepared,
            risk_free_rate=risk_free_rate,
            dividend_rate=dividend_rate,
        )

    def _compute_py_vollib_vectorized(
        self,
        prepared: pd.DataFrame,
        *,
        risk_free_rate: float,
        dividend_rate: float,
    ) -> pd.DataFrame:
        if dividend_rate != 0:
            return self._compute_black_scholes_frame(
                prepared,
                risk_free_rate=risk_free_rate,
                dividend_rate=dividend_rate,
            )

        os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
        import py_vollib_vectorized as pvv

        flags = prepared["flag"].to_list()
        spot = prepared["spot"].to_numpy(dtype=float)
        strike = prepared["strike"].to_numpy(dtype=float)
        ttm = prepared["ttm_years"].to_numpy(dtype=float)
        sigma = prepared["sigma"].to_numpy(dtype=float)
        rates = np.full(len(prepared), risk_free_rate, dtype=float)
        prices = pvv.vectorized_black_scholes(
            flags,
            spot,
            strike,
            ttm,
            rates,
            sigma,
            return_as="dataframe",
        )
        greeks = pvv.get_all_greeks(
            flags,
            spot,
            strike,
            ttm,
            rates,
            sigma,
            model="black_scholes",
            return_as="dataframe",
        )
        out = pd.DataFrame(index=prepared.index)
        out["theoretical_price"] = prices["Price"].to_numpy(dtype=float)
        out["delta"] = greeks["delta"].to_numpy(dtype=float)
        out["gamma"] = greeks["gamma"].to_numpy(dtype=float)
        out["vega"] = greeks["vega"].to_numpy(dtype=float) * 100.0
        out["theta"] = greeks["theta"].to_numpy(dtype=float)
        out["rho"] = greeks["rho"].to_numpy(dtype=float) * 100.0
        return out

    def _compute_black_scholes_frame(
        self,
        prepared: pd.DataFrame,
        *,
        risk_free_rate: float,
        dividend_rate: float,
    ) -> pd.DataFrame:
        rows = [
            _black_scholes_merton(
                flag=row.flag,
                spot=float(row.spot),
                strike=float(row.strike),
                ttm_years=float(row.ttm_years),
                risk_free_rate=risk_free_rate,
                dividend_rate=dividend_rate,
                sigma=float(row.sigma),
                annual_trading_days=self.config.annual_trading_days,
            )
            for row in prepared.itertuples()
        ]
        return pd.DataFrame(rows, index=prepared.index)

    def _prepare_inputs(self, frame: pd.DataFrame, *, spot: float) -> pd.DataFrame:
        required = {"option_type", "strike", "ttm_trading_days", "sigma"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing columns for Greeks calculation: {sorted(missing)}")

        prepared = pd.DataFrame(index=frame.index)
        prepared["flag"] = frame["option_type"].astype("string").str.lower().map({"c": "c", "p": "p"})
        prepared["spot"] = float(spot)
        prepared["strike"] = pd.to_numeric(frame["strike"], errors="coerce")
        prepared["ttm_trading_days"] = pd.to_numeric(frame["ttm_trading_days"], errors="coerce")
        prepared["ttm_years"] = prepared["ttm_trading_days"] / self.config.annual_trading_days
        prepared["sigma"] = pd.to_numeric(frame["sigma"], errors="coerce")
        prepared["greeks_status"] = "ok"
        prepared.loc[prepared["flag"].isna(), "greeks_status"] = "invalid_option_type"
        prepared.loc[prepared["spot"] <= 0, "greeks_status"] = "invalid_spot"
        prepared.loc[prepared["strike"].isna() | (prepared["strike"] <= 0), "greeks_status"] = "invalid_strike"
        prepared.loc[
            prepared["ttm_years"].isna() | (prepared["ttm_years"] <= 0),
            "greeks_status",
        ] = "expired"
        prepared.loc[prepared["sigma"].isna() | (prepared["sigma"] <= 0), "greeks_status"] = "invalid_sigma"
        return prepared

    @staticmethod
    def _align_sigma(frame: pd.DataFrame, sigma: float | pd.Series | pd.DataFrame) -> pd.Series:
        if isinstance(sigma, (float, int)):
            return pd.Series(float(sigma), index=frame.index)
        if isinstance(sigma, pd.Series):
            if sigma.index.equals(frame.index):
                return sigma.astype(float)
            if "contract_id" in frame.columns:
                mapped = frame["contract_id"].map(sigma)
                if not mapped.isna().all():
                    return mapped.astype(float)
            return sigma.reindex(frame.index).astype(float)
        if isinstance(sigma, pd.DataFrame):
            if "sigma" in sigma.columns:
                series = sigma["sigma"]
            elif "iv" in sigma.columns:
                series = sigma["iv"]
            else:
                raise ValueError("sigma DataFrame must contain 'sigma' or 'iv' column")
            if "contract_id" in sigma.columns and "contract_id" in frame.columns:
                mapped = frame["contract_id"].map(sigma.set_index("contract_id")[series.name])
                return mapped.astype(float)
            return series.reindex(frame.index).astype(float)
        raise TypeError("sigma must be a float, pandas Series, or pandas DataFrame")


def _black_scholes_merton(
    *,
    flag: str,
    spot: float,
    strike: float,
    ttm_years: float,
    risk_free_rate: float,
    dividend_rate: float,
    sigma: float,
    annual_trading_days: int,
) -> dict[str, float]:
    sqrt_t = math.sqrt(ttm_years)
    d1 = (math.log(spot / strike) + (risk_free_rate - dividend_rate + 0.5 * sigma * sigma) * ttm_years) / (
        sigma * sqrt_t
    )
    d2 = d1 - sigma * sqrt_t
    discount_r = math.exp(-risk_free_rate * ttm_years)
    discount_q = math.exp(-dividend_rate * ttm_years)
    pdf_d1 = _normal_pdf(d1)

    if flag == "c":
        price = spot * discount_q * _normal_cdf(d1) - strike * discount_r * _normal_cdf(d2)
        delta = discount_q * _normal_cdf(d1)
        theta_annual = (
            -spot * discount_q * pdf_d1 * sigma / (2.0 * sqrt_t)
            - risk_free_rate * strike * discount_r * _normal_cdf(d2)
            + dividend_rate * spot * discount_q * _normal_cdf(d1)
        )
        rho = strike * ttm_years * discount_r * _normal_cdf(d2)
    else:
        price = strike * discount_r * _normal_cdf(-d2) - spot * discount_q * _normal_cdf(-d1)
        delta = discount_q * (_normal_cdf(d1) - 1.0)
        theta_annual = (
            -spot * discount_q * pdf_d1 * sigma / (2.0 * sqrt_t)
            + risk_free_rate * strike * discount_r * _normal_cdf(-d2)
            - dividend_rate * spot * discount_q * _normal_cdf(-d1)
        )
        rho = -strike * ttm_years * discount_r * _normal_cdf(-d2)

    gamma = discount_q * pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * discount_q * pdf_d1 * sqrt_t
    theta = theta_annual / annual_trading_days
    return {
        "theoretical_price": price,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
    }


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)
