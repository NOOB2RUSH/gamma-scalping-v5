from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from gamma_scalping.backtest.execution import Fill
from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.strategy import PortfolioState, StrategyPosition


@dataclass
class Holding:
    instrument_id: str
    instrument_type: str
    quantity: float
    avg_price: float
    multiplier: float
    role: str = ""
    strategy_tag: str = "gamma_scalping"
    entry_trading_date: object | None = None


class Portfolio:
    def __init__(self, initial_cash: float, strategy_tag: str = "gamma_scalping") -> None:
        self.cash = float(initial_cash)
        self.strategy_tag = strategy_tag
        self.holdings: dict[str, Holding] = {}
        self.cumulative_fee = 0.0
        self.realized_pnl = 0.0

    def to_strategy_state(self, snapshot: MarketSnapshot) -> PortfolioState:
        return PortfolioState(
            equity=self.equity(snapshot),
            positions=tuple(
                StrategyPosition(
                    instrument_id=holding.instrument_id,
                    instrument_type=holding.instrument_type,  # type: ignore[arg-type]
                    quantity=holding.quantity,
                    multiplier=holding.multiplier,
                    strategy_tag=holding.strategy_tag,
                    role=holding.role,
                    entry_trading_date=holding.entry_trading_date,
                )
                for holding in self.holdings.values()
                if holding.quantity != 0
            ),
        )

    def apply_fills(self, fills: tuple[Fill, ...]) -> None:
        for fill in fills:
            signed_quantity = fill.quantity if fill.side == "buy" else -fill.quantity
            cash_delta = -signed_quantity * fill.price * fill.multiplier - fill.fee
            self.cash += cash_delta
            self.cumulative_fee += fill.fee
            self._update_holding(fill, signed_quantity)

    def handle_expiry_and_settlement(self, snapshot: MarketSnapshot) -> list[dict[str, object]]:
        option_frame = snapshot.option_chain.frame.set_index("contract_id")
        events = []
        for instrument_id, holding in list(self.holdings.items()):
            if holding.instrument_type != "option" or holding.quantity == 0:
                continue
            if instrument_id not in option_frame.index:
                continue
            row = option_frame.loc[instrument_id]
            if int(row.get("ttm_trading_days", 1)) > 0:
                continue
            payoff = self._option_payoff(row, snapshot.etf_bar.close) * holding.multiplier * holding.quantity
            self.cash += payoff
            self.realized_pnl += payoff - holding.avg_price * holding.multiplier * holding.quantity
            events.append(
                {
                    "trading_date": snapshot.trading_date,
                    "instrument_id": instrument_id,
                    "event": "expiry_settlement",
                    "cash_flow": payoff,
                }
            )
            del self.holdings[instrument_id]
        return events

    def market_value(self, snapshot: MarketSnapshot) -> float:
        option_frame = snapshot.option_chain.frame.set_index("contract_id")
        value = 0.0
        for holding in self.holdings.values():
            if holding.instrument_type == "etf":
                value += holding.quantity * snapshot.etf_bar.close
            elif holding.instrument_id in option_frame.index:
                row = option_frame.loc[holding.instrument_id]
                price = row.get("mark_price", row.get("mid", row.get("last", 0.0)))
                if pd.isna(price):
                    price = 0.0
                value += holding.quantity * float(price) * holding.multiplier
        return float(value)

    def equity(self, snapshot: MarketSnapshot) -> float:
        return self.cash + self.market_value(snapshot)

    def mark_to_market(self, snapshot: MarketSnapshot) -> dict[str, float]:
        market_value = self.market_value(snapshot)
        return {
            "cash": self.cash,
            "market_value": market_value,
            "equity": self.cash + market_value,
        }

    def positions_frame(self) -> pd.DataFrame:
        return pd.DataFrame([holding.__dict__ for holding in self.holdings.values()])

    def position_records(self, snapshot: MarketSnapshot) -> list[dict[str, object]]:
        if not self.holdings:
            return [
                {
                    "trading_date": snapshot.trading_date,
                    "instrument_id": "",
                    "instrument_type": "",
                    "quantity": 0.0,
                    "avg_price": 0.0,
                    "multiplier": 1.0,
                    "mark_price": 0.0,
                    "liquidation_price": 0.0,
                    "market_value": 0.0,
                    "liquidation_value": 0.0,
                    "cost_basis_value": 0.0,
                    "theoretical_unrealized_pnl": 0.0,
                    "role": "",
                    "strategy_tag": self.strategy_tag,
                    "entry_trading_date": "",
                }
            ]

        option_frame = snapshot.option_chain.frame.set_index("contract_id")
        return [self._position_record(holding, snapshot, option_frame) for holding in self.holdings.values()]

    def _update_holding(self, fill: Fill, signed_quantity: float) -> None:
        current = self.holdings.get(fill.instrument_id)
        if current is None:
            if signed_quantity == 0:
                return
            self.holdings[fill.instrument_id] = Holding(
                instrument_id=fill.instrument_id,
                instrument_type=fill.instrument_type,
                quantity=signed_quantity,
                avg_price=fill.price,
                multiplier=fill.multiplier,
                role=fill.role,
                strategy_tag=self.strategy_tag,
                entry_trading_date=fill.trading_date,
            )
            return

        new_quantity = current.quantity + signed_quantity
        if current.quantity == 0 or (current.quantity > 0) == (signed_quantity > 0):
            total_cost = current.avg_price * abs(current.quantity) + fill.price * abs(signed_quantity)
            current.avg_price = total_cost / abs(new_quantity) if new_quantity else 0.0
        else:
            closed_quantity = min(abs(current.quantity), abs(signed_quantity))
            direction = 1.0 if current.quantity > 0 else -1.0
            self.realized_pnl += direction * (fill.price - current.avg_price) * closed_quantity * current.multiplier
            if abs(signed_quantity) > abs(current.quantity):
                current.avg_price = fill.price

        current.quantity = new_quantity
        if abs(current.quantity) < 1e-12:
            del self.holdings[fill.instrument_id]

    @staticmethod
    def _option_payoff(row: pd.Series, spot: float) -> float:
        if str(row["option_type"]).upper() == "C":
            return max(spot - float(row["strike"]), 0.0)
        return max(float(row["strike"]) - spot, 0.0)

    def _position_record(
        self,
        holding: Holding,
        snapshot: MarketSnapshot,
        option_frame: pd.DataFrame,
    ) -> dict[str, object]:
        mark_price = self._mark_price(holding, snapshot, option_frame)
        liquidation_price = self._liquidation_price(holding, snapshot, option_frame)
        market_value = holding.quantity * mark_price * holding.multiplier
        liquidation_value = holding.quantity * liquidation_price * holding.multiplier
        cost_basis_value = holding.quantity * holding.avg_price * holding.multiplier
        return {
            "trading_date": snapshot.trading_date,
            "instrument_id": holding.instrument_id,
            "instrument_type": holding.instrument_type,
            "quantity": holding.quantity,
            "avg_price": holding.avg_price,
            "multiplier": holding.multiplier,
            "mark_price": mark_price,
            "liquidation_price": liquidation_price,
            "market_value": market_value,
            "liquidation_value": liquidation_value,
            "cost_basis_value": cost_basis_value,
            "theoretical_unrealized_pnl": liquidation_value - cost_basis_value,
            "role": holding.role,
            "strategy_tag": holding.strategy_tag,
            "entry_trading_date": holding.entry_trading_date,
        }

    def _mark_price(self, holding: Holding, snapshot: MarketSnapshot, option_frame: pd.DataFrame) -> float:
        if holding.instrument_type == "etf":
            return float(snapshot.etf_bar.close)
        if holding.instrument_id not in option_frame.index:
            return 0.0
        row = option_frame.loc[holding.instrument_id]
        price = row.get("mark_price", row.get("mid", row.get("last", 0.0)))
        return 0.0 if pd.isna(price) else float(price)

    def _liquidation_price(self, holding: Holding, snapshot: MarketSnapshot, option_frame: pd.DataFrame) -> float:
        if holding.instrument_type == "etf":
            return float(snapshot.etf_bar.close)
        if holding.instrument_id not in option_frame.index:
            return 0.0
        row = option_frame.loc[holding.instrument_id]
        if holding.quantity >= 0:
            price = row.get("sell_price", row.get("bid", row.get("mark_price", row.get("last", 0.0))))
        else:
            price = row.get("buy_price", row.get("ask", row.get("mark_price", row.get("last", 0.0))))
        return 0.0 if pd.isna(price) else float(price)
