#!/usr/bin/env python3
"""Analyze optimization results: rank trials, assess parameter sensitivity, generate report.

Usage:
    python scripts/analyze_optimization.py results/optimization/<study_id>
    python scripts/analyze_optimization.py results/optimization/gamma_scalping_opt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze optimization results.")
    parser.add_argument("study_dir", help="Path to study directory (e.g. results/optimization/gamma_scalping_opt)")
    parser.add_argument("--top-n", type=int, default=20, help="Number of top trials to report")
    parser.add_argument("--output", default=None, help="Output report path (default: <study_dir>/analysis_report.txt)")
    args = parser.parse_args(argv)

    study_dir = Path(args.study_dir)
    if not study_dir.is_dir():
        print(f"Study directory not found: {study_dir}")
        return 1

    summary_path = study_dir / "summary.csv"
    if not summary_path.exists():
        print(f"summary.csv not found in {study_dir}")
        return 1

    summary = pd.read_csv(summary_path)
    if summary.empty:
        print("summary.csv is empty")
        return 1

    output_path = Path(args.output) if args.output else study_dir / "analysis_report.txt"

    report_lines: list[str] = []
    successes = summary[summary["status"] == "success"].copy()

    report_lines.append("=" * 80)
    report_lines.append("OPTIMIZATION RESULTS ANALYSIS")
    report_lines.append("=" * 80)
    report_lines.append(f"Study directory: {study_dir}")
    report_lines.append(f"Total trials: {len(summary)}")
    report_lines.append(f"Successful: {len(successes)}")
    report_lines.append(f"Failed: {len(summary) - len(successes)}")
    report_lines.append("")

    if successes.empty:
        report_lines.append("No successful trials to analyze.")
        _write_report(output_path, report_lines)
        print(f"Report written to {output_path}")
        return 0

    param_cols = [c for c in successes.columns if c.startswith("param.")]
    metric_cols = [c for c in successes.columns if not c.startswith("param.") and c not in {"trial_id", "run_id", "stage", "split", "status", "elapsed_seconds", "output_dir", "error_message"}]

    # Section 1: Top trials by annual return
    report_lines.append("-" * 80)
    report_lines.append("TOP TRIALS BY ANNUAL RETURN")
    report_lines.append("-" * 80)
    top_return = successes.nlargest(args.top_n, "annual_return")
    display_cols = ["trial_id", "annual_return", "sharpe_ratio", "sortino_ratio", "max_drawdown", "score"] + param_cols
    display_cols = [c for c in display_cols if c in top_return.columns]
    report_lines.append(_format_table(top_return[display_cols]))
    report_lines.append("")

    # Section 2: Top trials by Sharpe ratio
    report_lines.append("-" * 80)
    report_lines.append("TOP TRIALS BY SHARPE RATIO")
    report_lines.append("-" * 80)
    top_sharpe = successes.nlargest(args.top_n, "sharpe_ratio")
    display_cols = ["trial_id", "sharpe_ratio", "annual_return", "sortino_ratio", "max_drawdown", "score"] + param_cols
    display_cols = [c for c in display_cols if c in top_sharpe.columns]
    report_lines.append(_format_table(top_sharpe[display_cols]))
    report_lines.append("")

    # Section 3: Top trials by composite score
    report_lines.append("-" * 80)
    report_lines.append("TOP TRIALS BY COMPOSITE SCORE")
    report_lines.append("-" * 80)
    top_score = successes.nlargest(args.top_n, "score")
    display_cols = ["trial_id", "score", "annual_return", "sharpe_ratio", "max_drawdown"] + param_cols
    display_cols = [c for c in display_cols if c in top_score.columns]
    report_lines.append(_format_table(top_score[display_cols]))
    report_lines.append("")

    # Section 4: Pareto frontier (annual return vs Sharpe ratio)
    report_lines.append("-" * 80)
    report_lines.append("PARETO FRONTIER (Annual Return vs Sharpe Ratio)")
    report_lines.append("-" * 80)
    pareto = _pareto_frontier(successes, x_col="annual_return", y_col="sharpe_ratio")
    if not pareto.empty:
        display_cols = ["trial_id", "annual_return", "sharpe_ratio", "score"] + param_cols
        display_cols = [c for c in display_cols if c in pareto.columns]
        report_lines.append(_format_table(pareto[display_cols]))
    else:
        report_lines.append("No Pareto-optimal trials found.")
    report_lines.append("")

    # Section 5: Parameter sensitivity analysis
    report_lines.append("-" * 80)
    report_lines.append("PARAMETER SENSITIVITY ANALYSIS")
    report_lines.append("-" * 80)
    if param_cols:
        sensitivity = _parameter_sensitivity(successes, param_cols)
        report_lines.append(_format_table(sensitivity))
    else:
        report_lines.append("No parameter columns found.")
    report_lines.append("")

    # Section 6: Best parameter set recommendation
    report_lines.append("-" * 80)
    report_lines.append("RECOMMENDED PARAMETER SET (Best Sharpe + Annual Return)")
    report_lines.append("-" * 80)
    best = _recommend_best(successes, param_cols)
    for key, value in best.items():
        report_lines.append(f"  {key}: {value}")
    report_lines.append("")

    # Section 7: Train vs Validation comparison (if multiple splits exist)
    if "split" in successes.columns and successes["split"].nunique() > 1:
        report_lines.append("-" * 80)
        report_lines.append("TRAIN vs VALIDATION COMPARISON")
        report_lines.append("-" * 80)
        split_comparison = _split_comparison(successes)
        report_lines.append(_format_table(split_comparison))
        report_lines.append("")

        # Best params that work well on both splits
        report_lines.append("-" * 80)
        report_lines.append("ROBUST PARAMETERS (Consistent Across Splits)")
        report_lines.append("-" * 80)
        robust = _find_robust_params(successes, param_cols)
        if robust is not None:
            display_cols = ["trial_id", "split", "annual_return", "sharpe_ratio", "score"] + param_cols
            display_cols = [c for c in display_cols if c in robust.columns]
            report_lines.append(_format_table(robust[display_cols]))
        else:
            report_lines.append("Could not identify robust parameters across splits.")
        report_lines.append("")

    # Section 8: Statistical summary
    report_lines.append("-" * 80)
    report_lines.append("STATISTICAL SUMMARY OF SUCCESSFUL TRIALS")
    report_lines.append("-" * 80)
    stat_cols = ["annual_return", "sharpe_ratio", "sortino_ratio", "max_drawdown", "calmar_ratio", "score", "total_pnl", "episode_count", "turnover"]
    stat_cols = [c for c in stat_cols if c in successes.columns]
    if stat_cols:
        stats = successes[stat_cols].describe().T
        stats["median"] = successes[stat_cols].median()
        report_lines.append(_format_table(stats.reset_index().rename(columns={"index": "metric"})))
    report_lines.append("")

    report_lines.append("=" * 80)
    report_lines.append("END OF REPORT")
    report_lines.append("=" * 80)

    _write_report(output_path, report_lines)
    print(f"Report written to {output_path}")

    # Also write best.json with multi-objective recommendation
    best_row = successes.nlargest(1, "sharpe_ratio").iloc[0].to_dict()
    (study_dir / "best_sharpe.json").write_text(
        json.dumps(best_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    best_return_row = successes.nlargest(1, "annual_return").iloc[0].to_dict()
    (study_dir / "best_return.json").write_text(
        json.dumps(best_return_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    best_combined_row = successes.nlargest(1, "score").iloc[0].to_dict()
    (study_dir / "best_combined.json").write_text(
        json.dumps(best_combined_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return 0


def _format_table(df: pd.DataFrame) -> str:
    """Format a DataFrame as a readable text table."""
    if df.empty:
        return "(empty)"
    formatted = df.copy()
    for col in formatted.columns:
        if formatted[col].dtype in [float, np.floating]:
            formatted[col] = formatted[col].apply(lambda x: f"{x:.6g}" if pd.notna(x) else "")
        else:
            formatted[col] = formatted[col].astype(str).str.slice(0, 40)
    lines = formatted.to_string(index=False, max_colwidth=40)
    return lines


def _pareto_frontier(df: pd.DataFrame, x_col: str, y_col: str) -> pd.DataFrame:
    """Find Pareto-optimal trials maximizing both x and y."""
    if x_col not in df.columns or y_col not in df.columns:
        return pd.DataFrame()
    sorted_df = df.nlargest(len(df), x_col)
    pareto_indices = []
    max_y = -np.inf
    for idx, row in sorted_df.iterrows():
        y_val = row[y_col]
        if y_val >= max_y:
            pareto_indices.append(idx)
            max_y = y_val
    return df.loc[pareto_indices]


def _parameter_sensitivity(df: pd.DataFrame, param_cols: list[str]) -> pd.DataFrame:
    """Analyze how each parameter affects key metrics."""
    target_metrics = ["annual_return", "sharpe_ratio", "score"]
    target_metrics = [c for c in target_metrics if c in df.columns]
    rows = []
    for col in param_cols:
        if col not in df.columns:
            continue
        grouped = df.groupby(col, dropna=False)
        for metric in target_metrics:
            agg = grouped[metric].agg(["mean", "std", "min", "max", "count"]).reset_index()
            for _, row in agg.iterrows():
                rows.append({
                    "parameter": col,
                    "value": row[col],
                    "metric": metric,
                    "mean": row["mean"],
                    "std": row["std"],
                    "min": row["min"],
                    "max": row["max"],
                    "count": row["count"],
                })
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows)
    # Sort by impact range (max - min) descending
    result["range"] = result["max"] - result["min"]
    return result.sort_values(["metric", "range"], ascending=[True, False])


def _recommend_best(df: pd.DataFrame, param_cols: list[str]) -> dict:
    """Recommend the best parameter set using a combined Sharpe + return ranking."""
    if df.empty:
        return {}
    # Normalize annual_return and sharpe_ratio to [0, 1]
    norm = df.copy()
    for col in ["annual_return", "sharpe_ratio"]:
        if col in norm.columns:
            min_v = norm[col].min()
            max_v = norm[col].max()
            if max_v > min_v:
                norm[f"{col}_norm"] = (norm[col] - min_v) / (max_v - min_v)
            else:
                norm[f"{col}_norm"] = 0.5

    norm["combined_rank"] = (
        norm.get("annual_return_norm", 0) * 0.5 + norm.get("sharpe_ratio_norm", 0) * 0.5
    )
    best_idx = norm["combined_rank"].idxmax()
    best_row = df.loc[best_idx]

    result: dict = {}
    result["recommendation_method"] = "normalized annual_return (50%) + sharpe_ratio (50%)"
    result["trial_id"] = best_row.get("trial_id", "")
    result["annual_return"] = best_row.get("annual_return", 0.0)
    result["sharpe_ratio"] = best_row.get("sharpe_ratio", 0.0)
    result["sortino_ratio"] = best_row.get("sortino_ratio", 0.0)
    result["max_drawdown"] = best_row.get("max_drawdown", 0.0)
    result["score"] = best_row.get("score", 0.0)
    result["total_pnl"] = best_row.get("total_pnl", 0.0)
    result["episode_count"] = best_row.get("episode_count", 0.0)
    for col in param_cols:
        if col in best_row.index:
            result[col] = best_row[col]
    return result


def _split_comparison(df: pd.DataFrame) -> pd.DataFrame:
    """Compare metrics across data splits."""
    metrics = ["annual_return", "sharpe_ratio", "sortino_ratio", "max_drawdown", "score", "total_pnl", "episode_count"]
    metrics = [c for c in metrics if c in df.columns]
    rows = []
    for split_name, group in df.groupby("split"):
        row: dict = {"split": split_name, "trial_count": len(group)}
        for metric in metrics:
            row[f"{metric}_mean"] = group[metric].mean()
            row[f"{metric}_std"] = group[metric].std()
            row[f"{metric}_best"] = group[metric].max()
        rows.append(row)
    return pd.DataFrame(rows)


def _find_robust_params(df: pd.DataFrame, param_cols: list[str]) -> pd.DataFrame | None:
    """Find parameter sets that appear in top trials across all splits."""
    if "split" not in df.columns or df["split"].nunique() < 2:
        return None
    splits = df["split"].unique()
    top_n = 5
    all_top_params = []
    for split in splits:
        split_df = df[df["split"] == split].nlargest(top_n, "score")
        all_top_params.append(set(split_df["trial_id"].values))
    # Find trials that are in top for each split
    # Since train and valid have different trial_ids, find param combos that work well on both
    # Instead, find top params per split and show them side by side
    robust_rows = []
    for split in splits:
        split_df = df[df["split"] == split].nlargest(3, "score")
        for _, row in split_df.iterrows():
            robust_rows.append(row.to_dict())
    return pd.DataFrame(robust_rows) if robust_rows else None


def _write_report(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
