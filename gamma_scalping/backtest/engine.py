from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from gamma_scalping.backtest.contract_name import option_contract_name
from gamma_scalping.backtest.execution import ExecutionModel, Fill, RiskChecker
from gamma_scalping.backtest.portfolio import Portfolio
from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.export_format import format_for_csv
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.strategy import GammaScalpingStrategy, OrderIntent, StrategyDecision
from gamma_scalping.utils import row_for_date
from gamma_scalping.volatility import AtmIvConfig, VolatilityEngine


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    strategy_tag: str = "gamma_scalping"
    output_dir: Path | str | None = None
    run_id: str | None = None
    position_zero_tolerance: float = 1e-12


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    equity_curve: pd.DataFrame
    fills: pd.DataFrame
    trade_records: pd.DataFrame
    position_records: pd.DataFrame
    episode_records: pd.DataFrame
    decisions: pd.DataFrame
    expiry_events: pd.DataFrame
    final_positions: pd.DataFrame
    greeks_history: pd.DataFrame
    iv_history: pd.DataFrame
    config: dict[str, object]
    metadata: dict[str, object]

    def export_csv(self, output_root: Path | str) -> dict[str, Path]:
        run_dir = Path(output_root) / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "run_dir": run_dir,
            "config": run_dir / "config.json",
            "metadata": run_dir / "metadata.json",
            "trade_records": run_dir / "trade_records.csv",
            "position_records": run_dir / "position_records.csv",
            "episode_records": run_dir / "episode_records.csv",
            "equity_curve": run_dir / "equity_curve.csv",
            "decisions": run_dir / "decisions.csv",
            "fills": run_dir / "fills.csv",
            "expiry_events": run_dir / "expiry_events.csv",
            "final_positions": run_dir / "final_positions.csv",
            "greeks_history": run_dir / "greeks_history.csv",
            "iv_history": run_dir / "iv_history.csv",
        }
        paths["config"].write_text(json.dumps(self.config, ensure_ascii=False, indent=2, default=str))
        paths["metadata"].write_text(json.dumps(self.metadata, ensure_ascii=False, indent=2, default=str))
        format_for_csv(self.trade_records).to_csv(paths["trade_records"], index=False)
        format_for_csv(self.position_records).to_csv(paths["position_records"], index=False)
        format_for_csv(self.episode_records).to_csv(paths["episode_records"], index=False)
        format_for_csv(self.equity_curve).to_csv(paths["equity_curve"], index=False)
        format_for_csv(self.decisions).to_csv(paths["decisions"], index=False)
        format_for_csv(self.fills).to_csv(paths["fills"], index=False)
        format_for_csv(self.expiry_events).to_csv(paths["expiry_events"], index=False)
        format_for_csv(self.final_positions).to_csv(paths["final_positions"], index=False)
        format_for_csv(self.greeks_history).to_csv(paths["greeks_history"], index=False)
        format_for_csv(self.iv_history).to_csv(paths["iv_history"], index=False)
        return paths


class BacktestEngine:
    def __init__(
        self,
        *,
        strategy: GammaScalpingStrategy,
        greeks_calculator: GreeksCalculator | None = None,
        volatility_engine: VolatilityEngine | None = None,
        execution_model: ExecutionModel | None = None,
        risk_checker: RiskChecker | None = None,
        atm_iv_config: AtmIvConfig | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.greeks_calculator = greeks_calculator or GreeksCalculator()
        self.volatility_engine = volatility_engine or VolatilityEngine()
        self.execution_model = execution_model or ExecutionModel()
        self.risk_checker = risk_checker or RiskChecker()
        self.atm_iv_config = atm_iv_config or AtmIvConfig()
        self.config = config or BacktestConfig()

    def run(self, snapshots: Iterable[MarketSnapshot], etf_history: pd.DataFrame | None = None) -> BacktestResult:
        snapshots = list(snapshots)
        run_id = self._run_id(snapshots)
        if not snapshots:
            return BacktestResult(
                run_id=run_id,
                equity_curve=pd.DataFrame(),
                fills=pd.DataFrame(),
                trade_records=pd.DataFrame(),
                position_records=pd.DataFrame(),
                episode_records=pd.DataFrame(),
                decisions=pd.DataFrame(),
                expiry_events=pd.DataFrame(),
                final_positions=pd.DataFrame(),
                greeks_history=pd.DataFrame(),
                iv_history=pd.DataFrame(),
                config=self._config_dict(),
                metadata=self._metadata(snapshots, run_id=run_id),
            )

        portfolio = Portfolio(
            self.config.initial_cash,
            strategy_tag=self.config.strategy_tag,
            position_zero_tolerance=self.config.position_zero_tolerance,
        )
        history = etf_history if etf_history is not None else self._history_from_snapshots(snapshots)
        hv = self.volatility_engine.compute_hv(history)
        equity_rows: list[dict[str, object]] = []
        fill_rows: list[dict[str, object]] = []
        trade_record_rows: list[dict[str, object]] = []
        position_record_rows: list[dict[str, object]] = []
        decision_rows: list[dict[str, object]] = []
        expiry_rows: list[dict[str, object]] = []
        greeks_history_rows: list[dict[str, object]] = []
        iv_history_rows: list[dict[str, object]] = []
        episode_registry: dict[str, dict[str, object]] = {}

        for snapshot in snapshots:
            expiry_events = portfolio.handle_expiry_and_settlement(snapshot)
            expiry_rows.extend(expiry_events)
            expiry_hedge_fills = self._close_expired_hedges(expiry_events, portfolio, snapshot)
            fill_rows.extend(fill.__dict__ for fill in expiry_hedge_fills)
            self._update_expired_episodes(episode_registry, expiry_events, snapshot.trading_date)
            surface = self.volatility_engine.solve_iv_chain(snapshot)
            vol_signal = self.volatility_engine.build_signal(
                surface,
                row_for_date(hv, snapshot.trading_date),
                self.atm_iv_config,
                trading_date=snapshot.trading_date,
                underlying=snapshot.underlying,
                hv_history=hv,
            )
            greeks = self.greeks_calculator.enrich_chain(
                snapshot.option_chain,
                spot=snapshot.etf_bar.close,
                sigma=surface.set_index("contract_id")["iv"],
            )
            greeks = greeks.drop(columns=["iv", "iv_status"], errors="ignore").merge(
                surface[["contract_id", "iv", "iv_status"]],
                on="contract_id",
                how="left",
            )
            greeks_history_rows.extend(self._greeks_history_rows(snapshot, greeks))
            iv_history_rows.extend(self._iv_history_rows(snapshot, surface))
            mtm_before_decision = portfolio.mark_to_market(snapshot)
            decision = self.strategy.on_snapshot(snapshot, greeks, vol_signal, portfolio.to_strategy_state(snapshot))
            checked_orders = self.risk_checker.check(decision.order_intents, snapshot)
            fills = self.execution_model.fill(checked_orders, snapshot)
            self._register_episode(episode_registry, decision, fills, snapshot, self.config.strategy_tag)
            portfolio.apply_fills(fills)
            self._update_closed_episodes(episode_registry, portfolio, fills, snapshot.trading_date)
            mtm_after_fills = portfolio.mark_to_market(snapshot)

            fill_rows.extend(fill.__dict__ for fill in fills)
            trade_record_rows.extend(self._trade_record_rows(snapshot, expiry_hedge_fills + fills))
            position_record_rows.extend(portfolio.position_records(snapshot))
            decision_rows.append(self._decision_row(decision))
            equity_rows.append(
                {
                    "trading_date": snapshot.trading_date,
                    "cash": mtm_after_fills["cash"],
                    "market_value": mtm_after_fills["market_value"],
                    "equity": mtm_after_fills["equity"],
                    "pre_trade_market_value": mtm_before_decision["market_value"],
                    "pre_trade_equity": mtm_before_decision["equity"],
                    "cumulative_fee": portfolio.cumulative_fee,
                    "realized_pnl": portfolio.realized_pnl,
                    "action": decision.action,
                }
            )

        result = BacktestResult(
            run_id=run_id,
            equity_curve=pd.DataFrame(equity_rows),
            fills=pd.DataFrame(fill_rows),
            trade_records=pd.DataFrame(trade_record_rows),
            position_records=pd.DataFrame(position_record_rows),
            episode_records=pd.DataFrame(episode_registry.values()),
            decisions=pd.DataFrame(decision_rows),
            expiry_events=pd.DataFrame(expiry_rows),
            final_positions=portfolio.positions_frame(),
            greeks_history=pd.DataFrame(greeks_history_rows),
            iv_history=pd.DataFrame(iv_history_rows),
            config=self._config_dict(),
            metadata=self._metadata(snapshots, run_id=run_id),
        )
        if self.config.output_dir is not None:
            result.export_csv(self.config.output_dir)
        return result

    @staticmethod
    def _history_from_snapshots(snapshots: list[MarketSnapshot]) -> pd.DataFrame:
        return pd.DataFrame(
            {"close": [snapshot.etf_bar.close for snapshot in snapshots]},
            index=pd.Index([snapshot.trading_date for snapshot in snapshots], name="date"),
        )

    @staticmethod
    def _decision_row(decision: StrategyDecision) -> dict[str, object]:
        return {
            "trading_date": decision.trading_date,
            "action": decision.action,
            "reason": decision.reason,
            "risk_flags": ",".join(decision.risk_flags),
            "selected_contracts": ",".join(decision.selected_contracts),
            "order_count": len(decision.order_intents),
            "episode_id": decision.episode_id,
            "entry_atm_iv": decision.entry_atm_iv,
            "entry_hv_20": decision.entry_hv_20,
            "entry_spot": decision.entry_spot,
            "entry_edge": decision.entry_edge,
            "entry_ratio": decision.entry_ratio,
            "rv_reference": decision.rv_reference,
            "rv_reference_source": decision.rv_reference_source,
        }

    @staticmethod
    def _trade_record_rows(snapshot: MarketSnapshot, fills: tuple[Fill, ...]) -> list[dict[str, object]]:
        if not fills:
            return [
                {
                    "trading_date": snapshot.trading_date,
                    "instrument_id": "",
                    "option_contract_name": "",
                    "instrument_type": "",
                    "side": "",
                    "quantity": 0.0,
                    "price": 0.0,
                    "multiplier": 1.0,
                    "trade_amount": 0.0,
                    "fee": 0.0,
                    "reason": "",
                    "role": "",
                    "episode_id": "",
                }
            ]
        option_frame = snapshot.option_chain.frame.set_index("contract_id")
        rows = []
        for fill in fills:
            option_row = option_frame.loc[fill.instrument_id] if fill.instrument_id in option_frame.index else None
            rows.append(
                {
                    "trading_date": fill.trading_date,
                    "instrument_id": fill.instrument_id,
                    "option_contract_name": option_contract_name(option_row, snapshot.underlying),
                    "instrument_type": fill.instrument_type,
                    "side": fill.side,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "multiplier": fill.multiplier,
                    "trade_amount": fill.quantity * fill.price * fill.multiplier,
                    "fee": fill.fee,
                    "reason": fill.reason,
                    "role": fill.role,
                    "episode_id": fill.episode_id,
                }
            )
        return rows

    @staticmethod
    def _greeks_history_rows(snapshot: MarketSnapshot, greeks: pd.DataFrame) -> list[dict[str, object]]:
        columns = [
            "contract_id",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "iv",
            "iv_status",
            "greeks_status",
        ]
        available = [column for column in columns if column in greeks.columns]
        rows = []
        for row in greeks[available].to_dict("records"):
            row["trading_date"] = snapshot.trading_date
            row["underlying"] = snapshot.underlying
            rows.append(row)
        return rows

    @staticmethod
    def _iv_history_rows(snapshot: MarketSnapshot, surface: pd.DataFrame) -> list[dict[str, object]]:
        columns = ["contract_id", "iv", "iv_status", "iv_price", "moneyness", "ttm_years"]
        available = [column for column in columns if column in surface.columns]
        rows = []
        for row in surface[available].to_dict("records"):
            row["trading_date"] = snapshot.trading_date
            row["underlying"] = snapshot.underlying
            rows.append(row)
        return rows

    @staticmethod
    def _register_episode(
        episode_registry: dict[str, dict[str, object]],
        decision: StrategyDecision,
        fills: tuple[Fill, ...],
        snapshot: MarketSnapshot,
        strategy_tag: str,
    ) -> None:
        if decision.action != "open" or not decision.episode_id or decision.episode_id in episode_registry:
            return
        episode_fills = [fill for fill in fills if fill.episode_id == decision.episode_id]
        option_fills = [fill for fill in episode_fills if fill.instrument_type == "option"]
        if not option_fills:
            return
        call_contract_id = next((fill.instrument_id for fill in option_fills if fill.role == "call_leg"), "")
        put_contract_id = next((fill.instrument_id for fill in option_fills if fill.role == "put_leg"), "")
        hedge_fill = next((fill for fill in episode_fills if fill.instrument_type == "etf"), None)
        episode_registry[decision.episode_id] = {
            "episode_id": decision.episode_id,
            "strategy_tag": strategy_tag,
            "underlying": snapshot.underlying,
            "status": "open",
            "opened_at": snapshot.trading_date,
            "closed_at": "",
            "open_reason": decision.reason,
            "close_reason": "",
            "call_contract_id": call_contract_id,
            "put_contract_id": put_contract_id,
            "entry_spot": decision.entry_spot,
            "entry_atm_iv": decision.entry_atm_iv,
            "entry_hv_20": decision.entry_hv_20,
            "entry_edge": decision.entry_edge,
            "entry_ratio": decision.entry_ratio,
            "rv_reference": decision.rv_reference,
            "rv_reference_source": decision.rv_reference_source,
            "contract_quantity": float(option_fills[0].quantity) if option_fills else 0.0,
            "initial_hedge_quantity": (
                (1.0 if hedge_fill.side == "buy" else -1.0) * hedge_fill.quantity if hedge_fill else 0.0
            ),
            "final_cash_pnl": 0.0,
            "notes": "",
        }

    @staticmethod
    def _update_closed_episodes(
        episode_registry: dict[str, dict[str, object]],
        portfolio: Portfolio,
        fills: tuple[Fill, ...],
        trading_date: object,
    ) -> None:
        episode_ids = {fill.episode_id for fill in fills if fill.episode_id}
        open_option_episode_ids = {
            holding.episode_id
            for holding in portfolio.holdings.values()
            if holding.episode_id and holding.instrument_type == "option" and holding.quantity != 0
        }
        for episode_id in episode_ids:
            record = episode_registry.get(episode_id)
            if record is None or record["status"] != "open" or episode_id in open_option_episode_ids:
                continue
            record["status"] = "closed"
            record["closed_at"] = trading_date
            close_fill = next((fill for fill in fills if fill.episode_id == episode_id and fill.reason), None)
            record["close_reason"] = close_fill.reason if close_fill else "closed"

    @staticmethod
    def _update_expired_episodes(
        episode_registry: dict[str, dict[str, object]],
        expiry_events: list[dict[str, object]],
        trading_date: object,
    ) -> None:
        for event in expiry_events:
            episode_id = str(event.get("episode_id", ""))
            if not episode_id or episode_id not in episode_registry:
                continue
            episode_registry[episode_id]["status"] = "expired_settled"
            episode_registry[episode_id]["closed_at"] = trading_date
            episode_registry[episode_id]["close_reason"] = "expiry_settlement"

    def _close_expired_hedges(
        self,
        expiry_events: list[dict[str, object]],
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
    ) -> tuple[Fill, ...]:
        episode_ids = {str(event.get("episode_id", "")) for event in expiry_events}
        episode_ids.discard("")
        if not episode_ids:
            return ()

        orders = []
        for holding in portfolio.holdings.values():
            if holding.instrument_type != "etf" or holding.quantity == 0 or holding.episode_id not in episode_ids:
                continue
            orders.append(
                OrderIntent(
                    trading_date=snapshot.trading_date,
                    instrument_id=holding.instrument_id,
                    instrument_type="etf",
                    side="sell" if holding.quantity > 0 else "buy",
                    quantity=abs(float(holding.quantity)),
                    reason="expiry_hedge_close",
                    role=holding.role,
                    episode_id=holding.episode_id,
                )
            )
        fills = self.execution_model.fill(tuple(orders), snapshot)
        portfolio.apply_fills(fills)
        return fills

    def _run_id(self, snapshots: list[MarketSnapshot]) -> str:
        if self.config.run_id:
            return self.config.run_id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        underlying = snapshots[0].underlying if snapshots else "unknown"
        return f"{timestamp}_{self.config.strategy_tag}_{underlying}"

    def _config_dict(self) -> dict[str, object]:
        return {
            "backtest": asdict(self.config),
            "strategy": asdict(self.strategy.config),
            "atm_iv": asdict(self.atm_iv_config),
            "execution": asdict(self.execution_model),
            "risk": asdict(self.risk_checker),
        }

    def _metadata(self, snapshots: list[MarketSnapshot], *, run_id: str) -> dict[str, object]:
        return {
            "run_id": run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "snapshot_count": len(snapshots),
            "start_date": snapshots[0].trading_date if snapshots else None,
            "end_date": snapshots[-1].trading_date if snapshots else None,
            "underlying": snapshots[0].underlying if snapshots else None,
        }
