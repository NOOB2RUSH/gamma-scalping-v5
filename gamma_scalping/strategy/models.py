from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


InstrumentType = Literal["option", "etf"]
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class StrategyPosition:
    instrument_id: str
    instrument_type: InstrumentType
    quantity: float
    multiplier: float = 1.0
    strategy_tag: str = "gamma_scalping"
    role: str = ""
    entry_trading_date: date | None = None
    episode_id: str = ""


@dataclass(frozen=True)
class PortfolioState:
    equity: float
    positions: tuple[StrategyPosition, ...] = field(default_factory=tuple)

    def positions_for_strategy(self, strategy_tag: str) -> tuple[StrategyPosition, ...]:
        return tuple(position for position in self.positions if position.strategy_tag == strategy_tag)

    def option_positions_for_strategy(self, strategy_tag: str) -> tuple[StrategyPosition, ...]:
        return tuple(
            position
            for position in self.positions
            if position.strategy_tag == strategy_tag and position.instrument_type == "option" and position.quantity != 0
        )

    def etf_position_for_strategy(self, strategy_tag: str) -> StrategyPosition | None:
        for position in self.positions:
            if (
                position.strategy_tag == strategy_tag
                and position.instrument_type == "etf"
                and position.quantity != 0
            ):
                return position
        return None


@dataclass(frozen=True)
class OrderIntent:
    trading_date: date
    instrument_id: str
    instrument_type: InstrumentType
    side: OrderSide
    quantity: float
    reason: str
    role: str = ""
    episode_id: str = ""


@dataclass(frozen=True)
class StrategyDecision:
    trading_date: date
    action: str
    order_intents: tuple[OrderIntent, ...]
    selected_contracts: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    episode_id: str = ""
    entry_atm_iv: float | None = None
    entry_hv_20: float | None = None
    entry_spot: float | None = None
    entry_edge: float | None = None
    entry_ratio: float | None = None
    rv_reference: float | None = None
    rv_reference_source: str = ""
