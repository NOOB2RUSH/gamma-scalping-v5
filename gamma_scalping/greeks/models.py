from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptionGreeks:
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    status: str = "ok"


@dataclass(frozen=True)
class Position:
    instrument_id: str
    instrument_type: str
    quantity: float
    multiplier: float = 1.0


@dataclass(frozen=True)
class PortfolioGreeks:
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float

