#!/usr/bin/env python3
"""Run full Greeks attribution backtests for optimized candidates.

Reads config/optimized_candidates.json, runs the backtest engine with
pricing reconciliation for each candidate, then prints a consolidated
attribution summary comparing both.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamma_scalping.attribution import GreeksPnLAttribution, PricingReconciliation, PricingReconciliationConfig
from gamma_scalping.backtest import BacktestEngine
from gamma_scalping.config import load_unified_config
from gamma_scalping.data import MarketDataLoader
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.strategy import GammaScalpingStrategy
from gamma_scalping.volatility import VolatilityEngine


def load_candidates(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def run_candidate_backtest(
    candidate_id: str,
    params: dict,
    base_config_path: Path,
    output_dir: Path,
) -> dict:
    """Run a single candidate backtest with full attribution.

    Returns a dict with paths to all output files.
    """
    # Build --params overrides from candidate params
    param_overrides = []
    for key, value in params.items():
        section, field = key.split(".", 1)
        param_overrides.append(f"{section}.{field}={value}")

    config = load_unified_config(str(base_config_path), param_overrides)

    loader = MarketDataLoader(config.data)
    snapshots = list(loader.iter_snapshots())
    etf_history = pd.DataFrame(
        {"close": [s.etf_bar.close for s in snapshots]},
        index=pd.Index([s.trading_date for s in snapshots], name="date"),
    )
    underlying_history = pd.DataFrame(
        {
            "trading_date": [s.trading_date for s in snapshots],
            "underlying": [s.underlying for s in snapshots],
            "close": [s.etf_bar.close for s in snapshots],
        }
    )

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
    result = engine.run(snapshots, etf_history=etf_history)

    run_dir = output_dir / candidate_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Greeks PnL attribution
    attribution = GreeksPnLAttribution(config.attribution).attribute_daily(
        equity_curve=result.equity_curve,
        trade_records=result.trade_records,
        position_records=result.position_records,
        greeks_history=result.greeks_history,
        iv_history=result.iv_history,
        underlying_history=underlying_history,
    )
    paths = attribution.export_csv(run_dir)

    # Pricing reconciliation
    recon_paths = PricingReconciliation(
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
    ).export_csv(run_dir)

    return {
        "run_id": result.run_id,
        "run_dir": run_dir,
        "attribution_daily": paths["greeks_attribution"],
        "attribution_cumulative": paths["greeks_attribution_cumulative"],
        "attribution_episode": paths["greeks_attribution_by_episode"],
        "attribution_quality": paths["attribution_quality"],
        "pricing_reconciliation": recon_paths.get("pricing_reconciliation"),
        "pricing_reconciliation_daily": recon_paths.get("pricing_reconciliation_daily"),
    }


def print_attribution_summary(candidate_id: str, daily: pd.DataFrame, cumulative: pd.DataFrame) -> None:
    """Print attribution summary for a single candidate."""
    print(f"\n{'='*70}")
    print(f"  {candidate_id}")
    print(f"{'='*70}")

    if daily.empty:
        print("  No attribution data.")
        return

    totals = {
        "actual_pnl": daily["actual_pnl"].sum(),
        "delta_pnl": daily["delta_pnl"].sum(),
        "gamma_pnl": daily["gamma_pnl"].sum(),
        "theta_pnl": daily["theta_pnl"].sum(),
        "vega_pnl": daily["vega_pnl"].sum(),
        "hedge_pnl": daily["hedge_pnl"].sum(),
        "cost_pnl": daily["cost_pnl"].sum(),
        "explained_pnl": daily["explained_pnl"].sum(),
        "residual_pnl": daily["residual_pnl"].sum(),
        "gamma_theta_pnl": daily["gamma_theta_pnl"].sum(),
    }

    print(f"\n  {'Component':<25} {'Total PnL':>15}")
    print(f"  {'-'*42}")
    for key, val in totals.items():
        print(f"  {key:<25} {val:>15,.2f}")

    print(f"\n  Diagnosis:")
    gt = totals["gamma_theta_pnl"]
    vega = totals["vega_pnl"]
    residual = totals["residual_pnl"]
    actual = totals["actual_pnl"]

    if gt > 0:
        print(f"    gamma_theta_pnl = +{gt:,.2f}  [POSITIVE — gamma scalping alpha exists]")
    else:
        print(f"    gamma_theta_pnl = {gt:,.2f}  [NEGATIVE — core alpha absent]")

    if vega > 0:
        pct = vega / actual * 100 if actual != 0 else 0
        print(f"    vega_pnl        = +{vega:,.2f}  [{pct:.1f}% of actual PnL — IV capture contribution]")
    else:
        print(f"    vega_pnl        = {vega:,.2f}")

    residual_pct = abs(residual) / max(abs(actual), 1e-8) * 100
    print(f"    residual_pnl    = {residual:,.2f}  [{residual_pct:.1f}% of |actual|]")

    # Quality stats
    quality_flags = daily["quality_flags"].str.split(",").explode()
    if not quality_flags.empty:
        flag_counts = quality_flags[quality_flags != ""].value_counts()
        if not flag_counts.empty:
            print(f"\n  Quality flags:")
            for flag, count in flag_counts.items():
                print(f"    {flag}: {count} days")


def print_pricing_reconciliation_summary(candidate_id: str, recon_path: Path) -> None:
    """Print pricing reconciliation summary."""
    if recon_path is None or not recon_path.exists():
        print(f"\n  {candidate_id}: pricing reconciliation not available.")
        return

    df = pd.read_csv(recon_path)
    if df.empty:
        return

    print(f"\n  Pricing Reconciliation ({candidate_id}):")
    print(f"  {'Component':<35} {'Total':>15}")
    print(f"  {'-'*52}")

    for col in [
        "mark_pnl",
        "model_repricing_pnl",
        "market_model_basis_pnl",
        "model_spot_pnl",
        "model_theta_pnl",
        "model_vega_pnl",
        "model_cross_residual_pnl",
        "taylor_residual_pnl",
        "mark_residual_pnl",
    ]:
        if col in df.columns:
            total = df[col].sum()
            print(f"  {col:<35} {total:>15,.2f}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run Greeks attribution for optimized candidates.")
    parser.add_argument(
        "--candidates",
        default=str(ROOT / "config" / "optimized_candidates.json"),
        help="Path to optimized_candidates.json.",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "backtest.default.json"),
        help="Path to base config file.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "results" / "attribution_candidates"),
        help="Output directory for attribution results.",
    )
    parser.add_argument(
        "--candidate",
        default=None,
        help="Run only a specific candidate by ID (e.g. return_focus_trial_000309).",
    )
    args = parser.parse_args(argv)

    candidates = load_candidates(Path(args.candidates))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for candidate_id, candidate_data in candidates["candidates"].items():
        if args.candidate and candidate_id != args.candidate:
            continue
        print(f"\nRunning backtest for {candidate_id}...")
        try:
            paths = run_candidate_backtest(
                candidate_id=candidate_id,
                params=candidate_data["params"],
                base_config_path=Path(args.config),
                output_dir=output_dir,
            )
            results[candidate_id] = paths
            print(f"  run_id: {paths['run_id']}")
            print(f"  output: {paths['run_dir']}")
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            results[candidate_id] = None

    # Print attribution summaries
    for candidate_id, paths in results.items():
        if paths is None:
            continue
        daily_path = paths["attribution_daily"]
        cumulative_path = paths["attribution_cumulative"]
        if daily_path and daily_path.exists():
            daily = pd.read_csv(daily_path)
            cumulative = pd.read_csv(cumulative_path) if cumulative_path and cumulative_path.exists() else pd.DataFrame()
            print_attribution_summary(candidate_id, daily, cumulative)

            # Pricing reconciliation
            recon_path = paths.get("pricing_reconciliation_daily") or paths.get("pricing_reconciliation")
            print_pricing_reconciliation_summary(candidate_id, recon_path)

    # Comparative summary
    print(f"\n\n{'='*70}")
    print(f"  COMPARATIVE SUMMARY")
    print(f"{'='*70}")

    summary_rows = []
    for candidate_id, paths in results.items():
        if paths is None:
            continue
        daily_path = paths["attribution_daily"]
        if not daily_path or not daily_path.exists():
            continue
        daily = pd.read_csv(daily_path)
        summary_rows.append({
            "candidate": candidate_id,
            "actual_pnl": daily["actual_pnl"].sum(),
            "gamma_theta_pnl": daily["gamma_theta_pnl"].sum(),
            "vega_pnl": daily["vega_pnl"].sum(),
            "delta_pnl": daily["delta_pnl"].sum(),
            "hedge_pnl": daily["hedge_pnl"].sum(),
            "residual_pnl": daily["residual_pnl"].sum(),
            "gamma_theta_pct": daily["gamma_theta_pnl"].sum() / max(abs(daily["actual_pnl"].sum()), 1e-8) * 100,
            "vega_pct": daily["vega_pnl"].sum() / max(abs(daily["actual_pnl"].sum()), 1e-8) * 100,
        })

    if summary_rows:
        df = pd.DataFrame(summary_rows)
        print(f"\n  {'Candidate':<35} {'Actual':>12} {'Gamma+Theta':>12} {'Vega':>12} {'Residual':>12}")
        print(f"  {'-'*85}")
        for _, row in df.iterrows():
            print(
                f"  {row['candidate']:<35} "
                f"{row['actual_pnl']:>12,.2f} "
                f"{row['gamma_theta_pnl']:>12,.2f} "
                f"{row['vega_pnl']:>12,.2f} "
                f"{row['residual_pnl']:>12,.2f}"
            )

        print(f"\n  Key questions:")
        for _, row in df.iterrows():
            cid = row["candidate"]
            gt = row["gamma_theta_pnl"]
            vega = row["vega_pnl"]
            actual = row["actual_pnl"]
            print(f"\n  {cid}:")
            if gt > 0:
                print(f"    gamma_theta_pnl > 0: Strategy has genuine gamma scalping alpha.")
            else:
                print(f"    gamma_theta_pnl < 0: No gamma scalping alpha; profit relies on other sources.")
            if abs(vega) > abs(gt):
                print(f"    |vega_pnl| > |gamma_theta_pnl|: Strategy is more IV-capture than gamma-scalping.")
            else:
                print(f"    |gamma_theta_pnl| >= |vega_pnl|: Gamma scalping is the dominant PnL driver.")

    print(f"\nDone. Results saved to: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
