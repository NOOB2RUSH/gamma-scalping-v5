from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from gamma_scalping.backtest.execution import ExecutionModel, Fill, RiskChecker
from gamma_scalping.backtest.portfolio import Portfolio
from gamma_scalping.data.models import MarketSnapshot
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.strategy import GammaScalpingStrategy, StrategyDecision
from gamma_scalping.utils import row_for_date
from gamma_scalping.volatility import AtmIvConfig, VolatilityEngine


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    strategy_tag: str = "gamma_scalping"
    output_dir: Path | str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    equity_curve: pd.DataFrame
    fills: pd.DataFrame
    trade_records: pd.DataFrame
    position_records: pd.DataFrame
    decisions: pd.DataFrame
    expiry_events: pd.DataFrame
    final_positions: pd.DataFrame
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
            "equity_curve": run_dir / "equity_curve.csv",
            "decisions": run_dir / "decisions.csv",
            "fills": run_dir / "fills.csv",
            "expiry_events": run_dir / "expiry_events.csv",
            "final_positions": run_dir / "final_positions.csv",
        }
        paths["config"].write_text(json.dumps(self.config, ensure_ascii=False, indent=2, default=str))
        paths["metadata"].write_text(json.dumps(self.metadata, ensure_ascii=False, indent=2, default=str))
        self.trade_records.to_csv(paths["trade_records"], index=False)
        self.position_records.to_csv(paths["position_records"], index=False)
        self.equity_curve.to_csv(paths["equity_curve"], index=False)
        self.decisions.to_csv(paths["decisions"], index=False)
        self.fills.to_csv(paths["fills"], index=False)
        self.expiry_events.to_csv(paths["expiry_events"], index=False)
        self.final_positions.to_csv(paths["final_positions"], index=False)
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
                decisions=pd.DataFrame(),
                expiry_events=pd.DataFrame(),
                final_positions=pd.DataFrame(),
                config=self._config_dict(),
                metadata=self._metadata(snapshots, run_id=run_id),
            )

        portfolio = Portfolio(self.config.initial_cash, strategy_tag=self.config.strategy_tag)
        history = etf_history if etf_history is not None else self._history_from_snapshots(snapshots)
        hv = self.volatility_engine.compute_hv(history)
        equity_rows: list[dict[str, object]] = []
        fill_rows: list[dict[str, object]] = []
        trade_record_rows: list[dict[str, object]] = []
        position_record_rows: list[dict[str, object]] = []
        decision_rows: list[dict[str, object]] = []
        expiry_rows: list[dict[str, object]] = []

        for snapshot in snapshots:
            expiry_rows.extend(portfolio.handle_expiry_and_settlement(snapshot))
            surface = self.volatility_engine.solve_iv_chain(snapshot)
            vol_signal = self.volatility_engine.build_signal(
                surface,
                row_for_date(hv, snapshot.trading_date),
                self.atm_iv_config,
                trading_date=snapshot.trading_date,
                underlying=snapshot.underlying,
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
            mtm_before_decision = portfolio.mark_to_market(snapshot)
            decision = self.strategy.on_snapshot(snapshot, greeks, vol_signal, portfolio.to_strategy_state(snapshot))
            checked_orders = self.risk_checker.check(decision.order_intents, snapshot)
            fills = self.execution_model.fill(checked_orders, snapshot)
            portfolio.apply_fills(fills)
            mtm_after_fills = portfolio.mark_to_market(snapshot)

            fill_rows.extend(fill.__dict__ for fill in fills)
            trade_record_rows.extend(self._trade_record_rows(snapshot, fills))
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
            decisions=pd.DataFrame(decision_rows),
            expiry_events=pd.DataFrame(expiry_rows),
            final_positions=portfolio.positions_frame(),
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
        }

    @staticmethod
    def _trade_record_rows(snapshot: MarketSnapshot, fills: tuple[Fill, ...]) -> list[dict[str, object]]:
        if not fills:
            return [
                {
                    "trading_date": snapshot.trading_date,
                    "instrument_id": "",
                    "instrument_type": "",
                    "side": "",
                    "quantity": 0.0,
                    "price": 0.0,
                    "multiplier": 1.0,
                    "trade_amount": 0.0,
                    "fee": 0.0,
                    "reason": "",
                    "role": "",
                }
            ]
        rows = []
        for fill in fills:
            rows.append(
                {
                    "trading_date": fill.trading_date,
                    "instrument_id": fill.instrument_id,
                    "instrument_type": fill.instrument_type,
                    "side": fill.side,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "multiplier": fill.multiplier,
                    "trade_amount": fill.quantity * fill.price * fill.multiplier,
                    "fee": fill.fee,
                    "reason": fill.reason,
                    "role": fill.role,
                }
            )
        return rows

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
