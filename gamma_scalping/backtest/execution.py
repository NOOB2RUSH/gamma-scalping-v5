from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.strategy import OrderIntent


@dataclass(frozen=True)
class Fill:
    trading_date: object
    instrument_id: str
    instrument_type: str
    side: str
    quantity: float
    price: float
    multiplier: float
    fee: float
    reason: str
    role: str = ""


@dataclass(frozen=True)
class ExecutionModel:
    etf_slippage_bps: float = 0.0
    option_slippage_bps: float = 0.0
    etf_fee_bps: float = 0.0
    option_fee_per_contract: float = 0.0
    option_fee_bps: float = 0.0

    def fill(self, orders: tuple[OrderIntent, ...], snapshot: MarketSnapshot) -> tuple[Fill, ...]:
        if not orders:
            return ()
        option_frame = snapshot.option_chain.frame.set_index("contract_id")
        fills = []
        for order in orders:
            if order.quantity <= 0:
                continue
            if order.instrument_type == "etf":
                price = self._etf_price(snapshot.etf_bar.close, order.side)
                multiplier = 1.0
                notional = order.quantity * price
                fee = notional * self.etf_fee_bps / 10000.0
            elif order.instrument_type == "option":
                if order.instrument_id not in option_frame.index:
                    raise KeyError(f"Missing option quote for order {order.instrument_id}")
                row = option_frame.loc[order.instrument_id]
                price = self._option_price(row, order.side)
                multiplier = float(row["multiplier"])
                notional = order.quantity * price * multiplier
                fee = order.quantity * self.option_fee_per_contract + notional * self.option_fee_bps / 10000.0
            else:
                raise ValueError(f"Unsupported instrument_type: {order.instrument_type}")
            fills.append(
                Fill(
                    trading_date=order.trading_date,
                    instrument_id=order.instrument_id,
                    instrument_type=order.instrument_type,
                    side=order.side,
                    quantity=float(order.quantity),
                    price=float(price),
                    multiplier=float(multiplier),
                    fee=float(fee),
                    reason=order.reason,
                    role=order.role,
                )
            )
        return tuple(fills)

    def _etf_price(self, close: float, side: str) -> float:
        adjustment = self.etf_slippage_bps / 10000.0
        if side == "buy":
            return close * (1.0 + adjustment)
        return close * (1.0 - adjustment)

    def _option_price(self, row: pd.Series, side: str) -> float:
        if side == "buy":
            price = row.get("buy_price", row.get("ask", row.get("mark_price", row.get("last"))))
            adjustment = 1.0 + self.option_slippage_bps / 10000.0
        else:
            price = row.get("sell_price", row.get("bid", row.get("mark_price", row.get("last"))))
            adjustment = 1.0 - self.option_slippage_bps / 10000.0
        if pd.isna(price) or float(price) <= 0:
            price = row.get("last", row.get("mark_price"))
        if pd.isna(price) or float(price) <= 0:
            raise ValueError(f"Invalid option fill price for {row.name}")
        return float(price) * adjustment


@dataclass(frozen=True)
class RiskChecker:
    max_abs_order_quantity: float | None = None

    def check(self, orders: tuple[OrderIntent, ...], snapshot: MarketSnapshot) -> tuple[OrderIntent, ...]:
        checked = []
        for order in orders:
            if order.quantity < 0:
                raise ValueError(f"Order quantity must be non-negative: {order}")
            if self.max_abs_order_quantity is not None and order.quantity > self.max_abs_order_quantity:
                raise ValueError(f"Order quantity exceeds max_abs_order_quantity: {order}")
            checked.append(order)
        return tuple(checked)
