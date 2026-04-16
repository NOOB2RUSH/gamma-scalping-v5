from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

import pandas as pd

from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.strategy.models import OrderIntent, PortfolioState, StrategyDecision, StrategyPosition
from gamma_scalping.volatility import VolatilitySignal


@dataclass(frozen=True)
class StrategyConfig:
    min_ttm_days: int = 5
    max_ttm_days: int = 20
    target_ttm_days: int = 10
    max_open_positions: int = 1
    premium_budget_pct: float = 0.1
    delta_threshold_pct: float = 0.01
    min_option_volume: int = 1
    min_open_interest: int = 0
    max_spread_pct: float = 0.5
    min_option_price: float = 0.0001
    max_holding_days: int | None = None
    use_vol_filter: bool = False
    min_hv_iv_edge: float = 0.0
    entry_max_iv_rv_ratio: float = 0.90
    exit_on_vol_edge_filled: bool = False
    exit_max_rv_iv_edge: float = 0.0
    exit_min_iv_rv_ratio: float = 1.0
    exit_iv_reference_mode: str = "position_average_iv"
    strategy_tag: str = "gamma_scalping"
    underlying_instrument_id: str = "510050.XSHG"


class GammaScalpingStrategy:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def on_snapshot(
        self,
        snapshot: MarketSnapshot,
        greeks: pd.DataFrame,
        vol_signal: VolatilitySignal | None,
        portfolio: PortfolioState,
    ) -> StrategyDecision:
        existing_options = portfolio.option_positions_for_strategy(self.config.strategy_tag)
        exit_decision = self._maybe_exit(snapshot, greeks, vol_signal, portfolio, existing_options)
        if exit_decision is not None:
            return exit_decision

        if existing_options:
            return self._hedge_decision(snapshot, greeks, portfolio, reason="hedge_existing_position")

        open_count = self._open_straddle_count(existing_options)
        if open_count >= self.config.max_open_positions:
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                reason="max_open_positions_reached",
                risk_flags=("max_open_positions",),
            )

        vol_entry = self._vol_entry_state(vol_signal)
        if self.config.use_vol_filter and not vol_entry["passed"]:
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                reason="vol_filter_not_satisfied",
                risk_flags=("vol_filter",),
                entry_atm_iv=vol_entry["atm_iv"],
                entry_hv_20=vol_entry["hv_20"],
                entry_spot=float(snapshot.etf_bar.close),
                entry_edge=vol_entry["edge"],
                entry_ratio=vol_entry["ratio"],
                rv_reference=vol_entry["rv_reference"],
                rv_reference_source=vol_entry["rv_reference_source"],
            )

        selection = self._select_straddle(snapshot, greeks)
        if selection is None:
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                reason="no_eligible_straddle",
                risk_flags=("no_eligible_straddle",),
            )

        call, put = selection
        episode_id = self._episode_id(snapshot.trading_date, str(call["contract_id"]), str(put["contract_id"]))
        contracts = self._contracts_to_buy(call, put, portfolio.equity)
        if contracts < 1:
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                selected_contracts=(str(call["contract_id"]), str(put["contract_id"])),
                reason="insufficient_budget",
                risk_flags=("insufficient_budget",),
                entry_atm_iv=vol_entry["atm_iv"],
                entry_hv_20=vol_entry["hv_20"],
                entry_spot=float(snapshot.etf_bar.close),
                entry_edge=vol_entry["edge"],
                entry_ratio=vol_entry["ratio"],
                rv_reference=vol_entry["rv_reference"],
                rv_reference_source=vol_entry["rv_reference_source"],
            )

        option_delta = self._option_delta_for_rows([call, put], contracts)
        hedge_quantity = -option_delta
        orders = [
            OrderIntent(
                trading_date=snapshot.trading_date,
                instrument_id=str(call["contract_id"]),
                instrument_type="option",
                side="buy",
                quantity=float(contracts),
                reason="open_atm_straddle_vol_edge" if self.config.use_vol_filter else "open_atm_straddle",
                role="call_leg",
                episode_id=episode_id,
            ),
            OrderIntent(
                trading_date=snapshot.trading_date,
                instrument_id=str(put["contract_id"]),
                instrument_type="option",
                side="buy",
                quantity=float(contracts),
                reason="open_atm_straddle_vol_edge" if self.config.use_vol_filter else "open_atm_straddle",
                role="put_leg",
                episode_id=episode_id,
            ),
        ]
        if hedge_quantity != 0:
            orders.append(
                self._etf_order(
                    snapshot.trading_date,
                    hedge_quantity,
                    reason="initial_delta_hedge",
                    episode_id=episode_id,
                )
            )
        return StrategyDecision(
            trading_date=snapshot.trading_date,
            action="open",
            order_intents=tuple(orders),
            selected_contracts=(str(call["contract_id"]), str(put["contract_id"])),
            reason="open_atm_straddle_vol_edge" if self.config.use_vol_filter else "open_atm_straddle",
            episode_id=episode_id,
            entry_atm_iv=vol_entry["atm_iv"],
            entry_hv_20=vol_entry["hv_20"],
            entry_spot=float(snapshot.etf_bar.close),
            entry_edge=vol_entry["edge"],
            entry_ratio=vol_entry["ratio"],
            rv_reference=vol_entry["rv_reference"],
            rv_reference_source=vol_entry["rv_reference_source"],
        )

    def _maybe_exit(
        self,
        snapshot: MarketSnapshot,
        greeks: pd.DataFrame,
        vol_signal: VolatilitySignal | None,
        portfolio: PortfolioState,
        existing_options: tuple[StrategyPosition, ...],
    ) -> StrategyDecision | None:
        if not existing_options:
            return None

        greeks_by_contract = greeks.set_index("contract_id")
        risk_flags: list[str] = []
        for position in existing_options:
            if position.instrument_id not in greeks_by_contract.index:
                risk_flags.append("missing_contract")
                continue
            row = greeks_by_contract.loc[position.instrument_id]
            if int(row.get("ttm_trading_days", 0)) <= 0:
                risk_flags.append("expired")
            if row.get("greeks_status", "ok") != "ok":
                risk_flags.append("bad_greeks")
            if row.get("iv_status", "ok") != "ok":
                risk_flags.append("bad_iv")
            if self.config.max_holding_days is not None and position.entry_trading_date is not None:
                if (snapshot.trading_date - position.entry_trading_date).days >= self.config.max_holding_days:
                    risk_flags.append("max_holding_days")

        if "missing_contract" in risk_flags:
            episode_ids = tuple(sorted({position.episode_id for position in existing_options if position.episode_id}))
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                selected_contracts=tuple(position.instrument_id for position in existing_options),
                reason="missing_contract_quote_skip",
                risk_flags=tuple(sorted(set(risk_flags))),
                episode_id=episode_ids[0] if len(episode_ids) == 1 else "",
            )

        if not risk_flags:
            vol_exit = self._maybe_vol_exit(snapshot, greeks_by_contract, vol_signal, portfolio, existing_options)
            if vol_exit is not None:
                return vol_exit
            return None

        episode_ids = tuple(sorted({position.episode_id for position in existing_options if position.episode_id}))
        orders = self._close_episode_orders(
            snapshot,
            portfolio,
            existing_options,
            reason="exit_risk_condition",
            episode_ids=episode_ids,
        )
        return StrategyDecision(
            trading_date=snapshot.trading_date,
            action="close",
            order_intents=tuple(orders),
            selected_contracts=tuple(position.instrument_id for position in existing_options),
            reason="exit_risk_condition",
            risk_flags=tuple(sorted(set(risk_flags))),
            episode_id=episode_ids[0] if len(episode_ids) == 1 else "",
        )

    def _maybe_vol_exit(
        self,
        snapshot: MarketSnapshot,
        greeks_by_contract: pd.DataFrame,
        vol_signal: VolatilitySignal | None,
        portfolio: PortfolioState,
        existing_options: tuple[StrategyPosition, ...],
    ) -> StrategyDecision | None:
        if not self.config.exit_on_vol_edge_filled:
            return None
        rv_reference = _safe_float(getattr(vol_signal, "rv_reference", None))
        rv_status = getattr(vol_signal, "rv_reference_status", "")
        if vol_signal is None or rv_status != "ok" or not _is_positive(rv_reference):
            return None
        held_iv = self._held_iv_reference(greeks_by_contract, existing_options)
        if not _is_positive(held_iv):
            return None
        exit_edge = rv_reference - held_iv
        exit_ratio = held_iv / rv_reference
        if exit_edge > self.config.exit_max_rv_iv_edge and exit_ratio < self.config.exit_min_iv_rv_ratio:
            return None

        episode_ids = tuple(sorted({position.episode_id for position in existing_options if position.episode_id}))
        orders = self._close_episode_orders(
            snapshot,
            portfolio,
            existing_options,
            reason="exit_vol_edge_filled",
            episode_ids=episode_ids,
        )
        return StrategyDecision(
            trading_date=snapshot.trading_date,
            action="close",
            order_intents=tuple(orders),
            selected_contracts=tuple(position.instrument_id for position in existing_options),
            reason="exit_vol_edge_filled",
            risk_flags=("vol_edge_filled",),
            episode_id=episode_ids[0] if len(episode_ids) == 1 else "",
            entry_edge=exit_edge,
            entry_ratio=exit_ratio,
            rv_reference=rv_reference,
            rv_reference_source=getattr(vol_signal, "rv_reference_source", ""),
        )

    def _close_episode_orders(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        existing_options: tuple[StrategyPosition, ...],
        *,
        reason: str,
        episode_ids: tuple[str, ...],
    ) -> list[OrderIntent]:
        orders = [
            OrderIntent(
                trading_date=snapshot.trading_date,
                instrument_id=position.instrument_id,
                instrument_type="option",
                side="sell" if position.quantity > 0 else "buy",
                quantity=abs(float(position.quantity)),
                reason=reason,
                role=position.role,
                episode_id=position.episode_id,
            )
            for position in existing_options
        ]
        for position in portfolio.positions_for_strategy(self.config.strategy_tag):
            if position.instrument_type != "etf" or position.quantity == 0:
                continue
            if episode_ids and position.episode_id not in episode_ids:
                continue
            orders.append(
                OrderIntent(
                    trading_date=snapshot.trading_date,
                    instrument_id=position.instrument_id,
                    instrument_type="etf",
                    side="sell" if position.quantity > 0 else "buy",
                    quantity=abs(float(position.quantity)),
                    reason=reason,
                    role=position.role,
                    episode_id=position.episode_id,
                )
            )
        return orders

    def _vol_entry_state(self, vol_signal: VolatilitySignal | None) -> dict[str, object]:
        atm_iv = _optional_float(getattr(vol_signal, "atm_iv", None))
        hv_20 = _optional_float(getattr(vol_signal, "hv_20", None))
        rv_reference = _optional_float(getattr(vol_signal, "rv_reference", None))
        edge = _optional_float(getattr(vol_signal, "rv_iv_edge", None))
        ratio = _optional_float(getattr(vol_signal, "iv_rv_ratio", None))
        source = str(getattr(vol_signal, "rv_reference_source", ""))
        status = str(getattr(vol_signal, "rv_reference_status", ""))
        passed = (
            vol_signal is not None
            and status == "ok"
            and atm_iv is not None
            and rv_reference is not None
            and edge is not None
            and ratio is not None
            and edge >= self.config.min_hv_iv_edge
            and ratio <= self.config.entry_max_iv_rv_ratio
        )
        return {
            "passed": passed,
            "atm_iv": atm_iv,
            "hv_20": hv_20,
            "rv_reference": rv_reference,
            "edge": edge,
            "ratio": ratio,
            "rv_reference_source": source,
        }

    def _held_iv_reference(
        self,
        greeks_by_contract: pd.DataFrame,
        existing_options: tuple[StrategyPosition, ...],
    ) -> float:
        rows = []
        for position in existing_options:
            if position.instrument_id not in greeks_by_contract.index:
                continue
            row = greeks_by_contract.loc[position.instrument_id]
            iv = _safe_float(row.get("iv", math.nan))
            if not _is_positive(iv):
                continue
            rows.append((position, row, iv))
        if not rows:
            return math.nan

        mode = self.config.exit_iv_reference_mode
        if mode == "position_average_iv":
            return sum(item[2] for item in rows) / len(rows)
        if mode == "held_position_vega_weighted_iv":
            weighted_sum = 0.0
            weight_total = 0.0
            for position, row, iv in rows:
                vega = abs(_safe_float(row.get("vega", math.nan)) * float(position.quantity))
                if not _is_positive(vega):
                    continue
                weighted_sum += vega * iv
                weight_total += vega
            return weighted_sum / weight_total if weight_total > 0 else math.nan
        raise ValueError(f"Unsupported exit_iv_reference_mode: {mode}")

    def _hedge_decision(
        self,
        snapshot: MarketSnapshot,
        greeks: pd.DataFrame,
        portfolio: PortfolioState,
        *,
        reason: str,
    ) -> StrategyDecision:
        existing_options = portfolio.option_positions_for_strategy(self.config.strategy_tag)
        episode_ids = tuple(sorted({position.episode_id for position in existing_options}))
        orders = []
        for episode_id in episode_ids:
            portfolio_delta = self._portfolio_delta(greeks, portfolio, episode_id=episode_id)
            delta_notional_ratio = abs(portfolio_delta * snapshot.etf_bar.close) / portfolio.equity
            if delta_notional_ratio <= self.config.delta_threshold_pct:
                continue
            hedge_order_quantity = -portfolio_delta
            orders.append(
                self._etf_order(
                    snapshot.trading_date,
                    hedge_order_quantity,
                    reason=reason,
                    episode_id=episode_id,
                )
            )

        if not orders:
            return StrategyDecision(
                trading_date=snapshot.trading_date,
                action="hold",
                order_intents=(),
                reason="delta_within_threshold",
            )

        return StrategyDecision(
            trading_date=snapshot.trading_date,
            action="hedge",
            order_intents=tuple(orders),
            reason=reason,
            episode_id=orders[0].episode_id if len(orders) == 1 else "",
        )

    def _select_straddle(self, snapshot: MarketSnapshot, greeks: pd.DataFrame) -> tuple[pd.Series, pd.Series] | None:
        frame = self._normalize_price_columns(greeks)
        required = {
            "contract_id",
            "option_type",
            "strike",
            "ttm_trading_days",
            "buy_price",
            "volume",
            "open_interest",
            "multiplier",
            "delta",
            "greeks_status",
        }
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing columns for strategy selection: {sorted(missing)}")

        frame = frame[
            frame["option_type"].isin(["C", "P"])
            & frame["ttm_trading_days"].between(self.config.min_ttm_days, self.config.max_ttm_days)
            & frame["greeks_status"].eq("ok")
            & (frame["volume"] >= self.config.min_option_volume)
            & (frame["open_interest"] >= self.config.min_open_interest)
            & (frame["buy_price"] >= self.config.min_option_price)
        ].copy()
        if "iv_status" in frame.columns:
            frame = frame[frame["iv_status"].eq("ok")]
        if "ask" in frame.columns and "bid" in frame.columns and "mid" in frame.columns:
            spread_pct = (frame["ask"] - frame["bid"]) / frame["mid"].replace(0, pd.NA)
            frame = frame[spread_pct <= self.config.max_spread_pct]
        if frame.empty:
            return None

        pairs = self._build_straddle_pairs(frame, spot=snapshot.etf_bar.close)
        if not pairs:
            return None
        pairs.sort(key=lambda pair: (pair[0], pair[1]))
        _, _, call_index, put_index = pairs[0]
        return frame.loc[call_index], frame.loc[put_index]

    @staticmethod
    def _normalize_price_columns(greeks: pd.DataFrame) -> pd.DataFrame:
        frame = greeks.copy()
        if "buy_price" not in frame.columns:
            for column in ("ask", "mark_price", "mid", "theoretical_price"):
                if column in frame.columns:
                    frame["buy_price"] = frame[column]
                    break
        if "mid" not in frame.columns:
            for column in ("mark_price", "theoretical_price", "buy_price"):
                if column in frame.columns:
                    frame["mid"] = frame[column]
                    break
        return frame

    def _build_straddle_pairs(self, frame: pd.DataFrame, *, spot: float) -> list[tuple[float, float, object, object]]:
        pairs: list[tuple[float, float, object, object]] = []
        for (_, strike), group in frame.groupby(["maturity_date", "strike"], sort=False):
            call_candidates = group[group["option_type"].eq("C")]
            put_candidates = group[group["option_type"].eq("P")]
            if call_candidates.empty or put_candidates.empty:
                continue
            call = call_candidates.iloc[0]
            put = put_candidates.iloc[0]
            ttm_score = abs(float(call["ttm_trading_days"]) - self.config.target_ttm_days)
            atm_score = abs(float(strike) / spot - 1.0)
            pairs.append((ttm_score, atm_score, call.name, put.name))
        return pairs

    def _contracts_to_buy(self, call: pd.Series, put: pd.Series, equity: float) -> int:
        premium_budget = equity * self.config.premium_budget_pct
        straddle_premium = float(call["buy_price"]) * float(call["multiplier"]) + float(put["buy_price"]) * float(
            put["multiplier"]
        )
        if straddle_premium <= 0:
            return 0
        return int(math.floor(premium_budget / straddle_premium))

    @staticmethod
    def _option_delta_for_rows(rows: list[pd.Series], quantity: float) -> float:
        return sum(float(row["delta"]) * float(row["multiplier"]) * quantity for row in rows)

    def _portfolio_delta(self, greeks: pd.DataFrame, portfolio: PortfolioState, episode_id: str | None = None) -> float:
        greeks_by_contract = greeks.set_index("contract_id")
        total = 0.0
        for position in portfolio.positions_for_strategy(self.config.strategy_tag):
            if episode_id is not None and position.episode_id != episode_id:
                continue
            if position.instrument_type == "etf":
                total += position.quantity
                continue
            if position.instrument_id not in greeks_by_contract.index:
                continue
            row = greeks_by_contract.loc[position.instrument_id]
            total += float(row["delta"]) * float(row["multiplier"]) * position.quantity
        return total

    def _etf_order(self, trading_date: date, quantity: float, *, reason: str, episode_id: str = "") -> OrderIntent:
        return OrderIntent(
            trading_date=trading_date,
            instrument_id=self.config.underlying_instrument_id,
            instrument_type="etf",
            side="buy" if quantity > 0 else "sell",
            quantity=abs(float(quantity)),
            reason=reason,
            role="hedge",
            episode_id=episode_id,
        )

    def _episode_id(self, trading_date: date, call_contract_id: str, put_contract_id: str) -> str:
        return f"{self.config.strategy_tag}:{trading_date:%Y%m%d}:{call_contract_id}:{put_contract_id}"

    @staticmethod
    def _open_straddle_count(existing_options: tuple[StrategyPosition, ...]) -> int:
        return 1 if existing_options else 0


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _is_positive(value: object) -> bool:
    number = _safe_float(value)
    return not math.isnan(number) and not math.isinf(number) and number > 0


def _optional_float(value: object) -> float | None:
    number = _safe_float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number
