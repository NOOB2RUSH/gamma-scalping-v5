from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import time
import traceback

import pandas as pd

from gamma_scalping.attribution import GreeksPnLAttribution, PricingReconciliation, PricingReconciliationConfig
from gamma_scalping.backtest import BacktestEngine
from gamma_scalping.config import load_unified_config, save_unified_config
from gamma_scalping.data import MarketDataLoader
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.optimization.evaluator import build_trial_metrics, score_metrics
from gamma_scalping.optimization.models import OptimizationStudyConfig, TrialPlan, TrialResult
from gamma_scalping.performance import PerformanceAnalyzer
from gamma_scalping.strategy import GammaScalpingStrategy
from gamma_scalping.volatility import VolatilityEngine


_DATA_CACHE: dict[str, tuple[list, pd.DataFrame, pd.DataFrame]] = {}
_MARKET_CALC_CACHE: dict[str, dict[object, tuple[pd.DataFrame, pd.DataFrame]]] = {}


def run_trial(
    plan: TrialPlan,
    *,
    base_config_path: Path | str,
    runs_dir: Path | str,
    study_config: OptimizationStudyConfig | None = None,
) -> TrialResult:
    started = time.perf_counter()
    run_dir = Path(runs_dir) / plan.run_id
    study_config = study_config or OptimizationStudyConfig()
    try:
        overrides = list(plan.overrides)
        if plan.split.start_date is not None:
            overrides.append(f"data.start_date={plan.split.start_date}")
        if plan.split.end_date is not None:
            overrides.append(f"data.end_date={plan.split.end_date}")
        overrides.extend(
            [
                f"backtest.run_id={plan.run_id}",
                f"backtest.output_dir={json.dumps(str(Path(runs_dir))) if study_config.save_trial_outputs else 'null'}",
                f"backtest.collect_market_history={json.dumps(bool(study_config.diagnostics or study_config.save_trial_outputs))}",
                "report.enabled=false",
            ]
        )
        config = load_unified_config(base_config_path, overrides)
        run_dir.mkdir(parents=True, exist_ok=True)
        save_unified_config(config, run_dir / "unified_config.json")

        snapshots, etf_history, underlying_history = _cached_market_data(config.data)
        volatility_engine = VolatilityEngine(config.volatility)
        engine = BacktestEngine(
            strategy=GammaScalpingStrategy(config.strategy),
            greeks_calculator=GreeksCalculator(config.greeks),
            volatility_engine=volatility_engine,
            execution_model=config.execution,
            risk_checker=config.risk,
            atm_iv_config=config.atm_iv,
            config=config.backtest,
        )
        market_cache = (
            _cached_market_calculations(config, snapshots)
            if study_config.cache_market_calculations and not config.backtest.collect_market_history
            else None
        )
        result = engine.run(snapshots, etf_history=etf_history, market_cache=market_cache)
        attribution = None
        reconciliation_daily = pd.DataFrame()
        volatility_series = None
        if study_config.diagnostics:
            attribution = GreeksPnLAttribution(config.attribution).attribute_daily(
                equity_curve=result.equity_curve,
                trade_records=result.trade_records,
                position_records=result.position_records,
                greeks_history=result.greeks_history,
                iv_history=result.iv_history,
                underlying_history=underlying_history,
            )
            if study_config.save_trial_outputs:
                attribution.export_csv(run_dir)
            reconciliation = PricingReconciliation(
                PricingReconciliationConfig(
                    risk_free_rate=config.greeks.risk_free_rate,
                    dividend_rate=config.greeks.dividend_rate,
                    annual_trading_days=config.greeks.annual_trading_days,
                )
            ).reconcile(
                equity_curve=result.equity_curve,
                trade_records=result.trade_records,
                position_records=result.position_records,
                greeks_history=result.greeks_history,
                iv_history=result.iv_history,
                underlying_history=underlying_history,
            )
            reconciliation_daily = reconciliation.daily
            if study_config.save_trial_outputs:
                reconciliation.export_csv(run_dir)
            volatility_series = volatility_engine.build_signal_series(snapshots, etf_history, config.atm_iv)
        performance = PerformanceAnalyzer(config.performance).compute_metrics(
            result,
            attribution=attribution,
            volatility=volatility_series,
            underlying_history=underlying_history if attribution is not None else None,
        )
        metrics = build_trial_metrics(
            summary=performance.summary,
            episode_records=result.episode_records,
            trade_records=result.trade_records,
            attribution_daily=attribution.daily if attribution is not None else pd.DataFrame(),
            reconciliation_daily=reconciliation_daily,
            initial_cash=config.backtest.initial_cash,
        )
        score = score_metrics(metrics, initial_cash=config.backtest.initial_cash)
        payload = {"status": "success", "metrics": metrics, "score": score, "parameters": plan.parameters}
        (run_dir / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return TrialResult(
            trial_id=plan.trial_id,
            run_id=plan.run_id,
            stage=plan.stage,
            split=plan.split.name,
            status="success",
            elapsed_seconds=time.perf_counter() - started,
            parameters=plan.parameters,
            metrics=metrics,
            score=score,
            output_dir=str(run_dir),
        )
    except Exception as exc:
        run_dir.mkdir(parents=True, exist_ok=True)
        message = f"{type(exc).__name__}: {exc}"
        (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return TrialResult(
            trial_id=plan.trial_id,
            run_id=plan.run_id,
            stage=plan.stage,
            split=plan.split.name,
            status="failed",
            elapsed_seconds=time.perf_counter() - started,
            parameters=plan.parameters,
            metrics={},
            score=0.0,
            output_dir=str(run_dir),
            error_message=message,
        )


def prewarm_trial_market_cache(plan: TrialPlan, *, base_config_path: Path | str, study_config: OptimizationStudyConfig) -> None:
    """Warm process-local market data and IV/Greeks cache before worker fork."""
    overrides = list(plan.overrides)
    if plan.split.start_date is not None:
        overrides.append(f"data.start_date={plan.split.start_date}")
    if plan.split.end_date is not None:
        overrides.append(f"data.end_date={plan.split.end_date}")
    overrides.extend(
        [
            f"backtest.run_id={plan.run_id}",
            "backtest.output_dir=null",
            f"backtest.collect_market_history={json.dumps(bool(study_config.diagnostics or study_config.save_trial_outputs))}",
            "report.enabled=false",
        ]
    )
    config = load_unified_config(base_config_path, overrides)
    snapshots, _, _ = _cached_market_data(config.data)
    if study_config.cache_market_calculations and not config.backtest.collect_market_history:
        _cached_market_calculations(config, snapshots)


def load_completed_trial(plan: TrialPlan, *, runs_dir: Path | str) -> TrialResult | None:
    run_dir = Path(runs_dir) / plan.run_id
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        return None
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    if payload.get("status") != "success":
        return None
    return TrialResult(
        trial_id=plan.trial_id,
        run_id=plan.run_id,
        stage=plan.stage,
        split=plan.split.name,
        status="success",
        elapsed_seconds=0.0,
        parameters=plan.parameters,
        metrics={key: float(value) for key, value in payload.get("metrics", {}).items()},
        score=float(payload.get("score", 0.0)),
        output_dir=str(run_dir),
    )


def _etf_history_from_snapshots(snapshots) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [snapshot.etf_bar.close for snapshot in snapshots]},
        index=pd.Index([snapshot.trading_date for snapshot in snapshots], name="date"),
    )


def _cached_market_data(data_config) -> tuple[list, pd.DataFrame, pd.DataFrame]:
    key = json.dumps(asdict(data_config), ensure_ascii=False, sort_keys=True, default=str)
    cached = _DATA_CACHE.get(key)
    if cached is not None:
        return cached
    loader = MarketDataLoader(data_config)
    snapshots = list(loader.iter_snapshots())
    payload = (snapshots, _etf_history_from_snapshots(snapshots), _underlying_history_from_snapshots(snapshots))
    _DATA_CACHE[key] = payload
    return payload


def _cached_market_calculations(config, snapshots) -> dict[object, tuple[pd.DataFrame, pd.DataFrame]]:
    key = json.dumps(
        {
            "data": asdict(config.data),
            "greeks": asdict(config.greeks),
            "volatility_iv": {
                "risk_free_rate": config.volatility.risk_free_rate,
                "dividend_rate": config.volatility.dividend_rate,
                "annual_trading_days": config.volatility.annual_trading_days,
                "iv_price_column": config.volatility.iv_price_column,
                "iv_fallback_price_column": config.volatility.iv_fallback_price_column,
                "iv_backend": config.volatility.iv_backend,
                "iv_bisection_lower": config.volatility.iv_bisection_lower,
                "iv_bisection_upper": config.volatility.iv_bisection_upper,
                "iv_bisection_tolerance": config.volatility.iv_bisection_tolerance,
                "iv_bisection_max_iterations": config.volatility.iv_bisection_max_iterations,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    cached = _MARKET_CALC_CACHE.get(key)
    if cached is not None:
        return cached
    volatility_engine = VolatilityEngine(config.volatility)
    greeks_calculator = GreeksCalculator(config.greeks)
    calculated: dict[object, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for snapshot in snapshots:
        surface = volatility_engine.solve_iv_chain(snapshot)
        greeks = greeks_calculator.enrich_chain(
            snapshot.option_chain,
            spot=snapshot.etf_bar.close,
            sigma=surface.set_index("contract_id")["iv"],
        )
        greeks = greeks.drop(columns=["iv", "iv_status"], errors="ignore").merge(
            surface[["contract_id", "iv", "iv_status"]],
            on="contract_id",
            how="left",
        )
        calculated[snapshot.trading_date] = (surface, greeks)
    _MARKET_CALC_CACHE[key] = calculated
    return calculated


def _underlying_history_from_snapshots(snapshots) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trading_date": [snapshot.trading_date for snapshot in snapshots],
            "underlying": [snapshot.underlying for snapshot in snapshots],
            "close": [snapshot.etf_bar.close for snapshot in snapshots],
        }
    )
