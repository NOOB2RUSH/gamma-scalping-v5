from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def build_trial_metrics(
    *,
    summary: dict[str, float],
    episode_records: pd.DataFrame,
    trade_records: pd.DataFrame,
    attribution_daily: pd.DataFrame,
    reconciliation_daily: pd.DataFrame,
    initial_cash: float,
) -> dict[str, float]:
    metrics = {key: _to_float(value) for key, value in summary.items()}
    metrics["total_pnl"] = metrics.get("final_equity", 0.0) - metrics.get("initial_equity", 0.0)
    metrics.update(_episode_metrics(episode_records))
    metrics.update(_trade_metrics(trade_records, initial_cash=initial_cash))
    metrics.update(_attribution_metrics(attribution_daily))
    metrics.update(_reconciliation_metrics(reconciliation_daily))
    return _sanitize(metrics)


def score_metrics(metrics: dict[str, float], *, initial_cash: float) -> float:
    annual_return = metrics.get("annual_return", 0.0)
    sharpe = metrics.get("sharpe_ratio", 0.0)
    max_drawdown = metrics.get("max_drawdown", 0.0)
    sortino = metrics.get("sortino_ratio", 0.0)
    gamma_theta = metrics.get("total_gamma_theta_pnl", 0.0)
    residual = metrics.get("total_residual_pnl", 0.0)
    denominator = initial_cash if initial_cash else 1.0
    return float(annual_return + 0.5 * sharpe - 0.8 * abs(max_drawdown) + 0.3 * sortino + 0.2 * gamma_theta / denominator - 0.2 * abs(residual) / denominator)


def _episode_metrics(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {"episode_count": 0.0, "avg_holding_days": 0.0}
    records = frame.copy()
    if "episode_id" in records.columns:
        records = records[records["episode_id"].fillna("").astype(str) != ""]
    episode_count = float(len(records))
    if {"opened_at", "closed_at"}.issubset(records.columns):
        opened = pd.to_datetime(records["opened_at"], errors="coerce")
        closed = pd.to_datetime(records["closed_at"], errors="coerce")
        holding_days = (closed - opened).dt.days
        avg_holding = float(holding_days.dropna().mean()) if not holding_days.dropna().empty else 0.0
    else:
        avg_holding = 0.0
    return {"episode_count": episode_count, "avg_holding_days": avg_holding}


def _trade_metrics(frame: pd.DataFrame, *, initial_cash: float) -> dict[str, float]:
    if frame.empty:
        return {"option_trade_count": 0.0, "hedge_trade_count": 0.0, "turnover": 0.0}
    records = frame.copy()
    if "instrument_id" in records.columns:
        records = records[records["instrument_id"].fillna("").astype(str) != ""]
    option_count = float(records["instrument_type"].astype(str).str.lower().eq("option").sum()) if "instrument_type" in records else 0.0
    hedge_count = (
        float((records["instrument_type"].astype(str).str.lower().eq("etf") & records.get("role", "").astype(str).eq("hedge")).sum())
        if {"instrument_type", "role"}.issubset(records.columns)
        else 0.0
    )
    amount = pd.to_numeric(records.get("trade_amount", 0.0), errors="coerce").fillna(0.0).abs().sum()
    turnover = float(amount / initial_cash) if initial_cash else 0.0
    return {"option_trade_count": option_count, "hedge_trade_count": hedge_count, "turnover": turnover}


def _attribution_metrics(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {}
    mapping = {
        "delta_pnl": "total_delta_pnl",
        "gamma_pnl": "total_gamma_pnl",
        "theta_pnl": "total_theta_pnl",
        "hedge_pnl": "total_hedge_pnl",
        "cost_pnl": "total_cost_pnl",
        "residual_pnl": "total_residual_pnl",
        "explained_pnl": "total_explained_pnl",
    }
    metrics = {}
    for source, target in mapping.items():
        if source in frame.columns:
            metrics[target] = float(pd.to_numeric(frame[source], errors="coerce").fillna(0.0).sum())
    return metrics


def _reconciliation_metrics(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {}
    columns = [
        "mark_pnl",
        "model_repricing_pnl",
        "model_spot_pnl",
        "model_theta_pnl",
        "model_vega_pnl",
        "market_model_basis_pnl",
        "taylor_residual_pnl",
        "mark_residual_pnl",
    ]
    return {
        f"total_{column}": float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())
        for column in columns
        if column in frame.columns
    }


def _sanitize(metrics: dict[str, float]) -> dict[str, float]:
    return {key: _to_float(value) for key, value in metrics.items()}


def _to_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if np.isnan(number) or np.isinf(number):
        return 0.0
    return number
