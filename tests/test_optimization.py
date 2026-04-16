from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from gamma_scalping.optimization.evaluator import build_trial_metrics, score_metrics
from gamma_scalping.optimization.models import DataSplit, OptimizationConfig, OptimizationStudyConfig
from gamma_scalping.optimization.space import generate_trial_plan


def _write_mock_study(tmp_path: Path, n_trials: int = 5, n_failures: int = 1) -> Path:
    """Write a mock study directory with summary.csv for analysis testing."""
    study_dir = tmp_path / "results" / "optimization" / "test_study"
    study_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n_trials):
        row: dict[str, Any] = {
            "trial_id": f"trial_{i+1:06d}",
            "run_id": f"test_split_trial_{i+1:06d}",
            "stage": "test",
            "split": "train" if i % 2 == 0 else "valid",
            "status": "failed" if i < n_failures else "success",
            "elapsed_seconds": 10.0,
            "score": float(i) * 0.1,
            "output_dir": str(study_dir / "runs" / f"test_split_trial_{i+1:06d}"),
            "error_message": "boom" if i < n_failures else "",
            "param.strategy.premium_budget_pct": 0.05 + i * 0.01,
            "param.strategy.delta_threshold_pct": 0.005,
            "annual_return": 0.01 * (i + 1),
            "sharpe_ratio": 0.2 * (i + 1),
            "sortino_ratio": 0.3 * (i + 1),
            "max_drawdown": -0.05 * (i + 1),
            "calmar_ratio": 0.1 * (i + 1),
            "total_pnl": 100.0 * (i + 1),
            "episode_count": 20.0 + i,
            "turnover": 10.0 + i,
        }
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(study_dir / "summary.csv", index=False)
    return study_dir


def test_generate_trial_plan_applies_conditional_parameters_and_rejects_unsupported_fields() -> None:
    config = OptimizationConfig(
        study=OptimizationStudyConfig(max_trials=None),
        data_splits=(DataSplit(name="train", start_date="2020-01-01", end_date="2020-01-03"),),
        parameters={
            "strategy.max_open_positions": (1, 2),
            "strategy.exit_on_vol_edge_filled": (False, True),
            "strategy.exit_max_rv_iv_edge": (0.0, 0.01),
            "strategy.exit_min_iv_rv_ratio": (1.0,),
            "strategy.exit_iv_reference_mode": ("position_average_iv",),
            "volatility.rv_reference_mode": ("current_hv", "rolling_quantile"),
            "volatility.rv_distribution_quantile": (0.5, 0.6),
            "strategy.min_ttm_days": (5,),
            "strategy.target_ttm_days": (10,),
            "strategy.max_ttm_days": (20,),
        },
    )

    plans = generate_trial_plan(config, stage="unit")

    assert plans
    assert all(plan.parameters["strategy.max_open_positions"] == 1 for plan in plans)
    assert all("strategy.exit_min_ttm_days" not in plan.parameters for plan in plans)
    current_hv = [plan for plan in plans if plan.parameters["volatility.rv_reference_mode"] == "current_hv"]
    assert current_hv
    assert all("volatility.rv_distribution_quantile" not in plan.parameters for plan in current_hv)
    exit_disabled = [plan for plan in plans if plan.parameters["strategy.exit_on_vol_edge_filled"] is False]
    assert exit_disabled
    assert all("strategy.exit_max_rv_iv_edge" not in plan.parameters for plan in exit_disabled)


def test_generate_trial_plan_filters_invalid_ttm_order() -> None:
    config = OptimizationConfig(
        data_splits=(DataSplit(name="train"),),
        parameters={
            "strategy.min_ttm_days": (20,),
            "strategy.target_ttm_days": (10,),
            "strategy.max_ttm_days": (30,),
        },
    )

    assert generate_trial_plan(config, stage="unit") == []


def test_generate_trial_plan_filters_unimplemented_exit_min_ttm_days() -> None:
    config = OptimizationConfig(
        data_splits=(DataSplit(name="train"),),
        parameters={
            "strategy.exit_min_ttm_days": (3,),
        },
    )

    assert generate_trial_plan(config, stage="unit") == []


def test_build_trial_metrics_uses_existing_summary_and_extra_aggregations() -> None:
    summary = {
        "initial_equity": 1000.0,
        "final_equity": 1100.0,
        "annual_return": 0.2,
        "max_drawdown": -0.1,
        "sharpe_ratio": 1.2,
        "sortino_ratio": 1.5,
        "total_gamma_theta_pnl": 30.0,
        "iv_hv_capture_rate_valid_count": 2.0,
    }
    episode_records = pd.DataFrame(
        {
            "episode_id": ["e1", "e2"],
            "opened_at": ["2024-01-01", "2024-01-03"],
            "closed_at": ["2024-01-05", "2024-01-08"],
        }
    )
    trade_records = pd.DataFrame(
        {
            "instrument_id": ["CALL1", "510050.XSHG"],
            "instrument_type": ["option", "etf"],
            "role": ["call_leg", "hedge"],
            "trade_amount": [100.0, 200.0],
        }
    )
    attribution_daily = pd.DataFrame(
        {
            "delta_pnl": [1.0],
            "gamma_pnl": [10.0],
            "theta_pnl": [-4.0],
            "hedge_pnl": [-1.0],
            "cost_pnl": [0.0],
            "residual_pnl": [2.0],
            "explained_pnl": [6.0],
        }
    )
    reconciliation_daily = pd.DataFrame(
        {
            "mark_pnl": [8.0],
            "model_repricing_pnl": [7.0],
            "model_spot_pnl": [11.0],
            "model_theta_pnl": [-5.0],
            "model_vega_pnl": [1.0],
            "market_model_basis_pnl": [1.0],
            "taylor_residual_pnl": [3.0],
            "mark_residual_pnl": [2.0],
        }
    )

    metrics = build_trial_metrics(
        summary=summary,
        episode_records=episode_records,
        trade_records=trade_records,
        attribution_daily=attribution_daily,
        reconciliation_daily=reconciliation_daily,
        initial_cash=1000.0,
    )

    assert metrics["total_pnl"] == pytest.approx(100.0)
    assert metrics["episode_count"] == pytest.approx(2.0)
    assert metrics["option_trade_count"] == pytest.approx(1.0)
    assert metrics["hedge_trade_count"] == pytest.approx(1.0)
    assert metrics["turnover"] == pytest.approx(0.3)
    assert metrics["total_delta_pnl"] == pytest.approx(1.0)
    assert metrics["total_residual_pnl"] == pytest.approx(2.0)
    assert metrics["total_market_model_basis_pnl"] == pytest.approx(1.0)
    expected_score = (
        metrics["annual_return"]
        + 0.5 * metrics["sharpe_ratio"]
        - 0.8 * abs(metrics["max_drawdown"])
        + 0.3 * metrics["sortino_ratio"]
        + 0.2 * metrics["total_gamma_theta_pnl"] / 1000.0
        - 0.2 * abs(metrics["total_residual_pnl"]) / 1000.0
    )
    assert score_metrics(metrics, initial_cash=1000.0) == pytest.approx(expected_score)


def test_analyze_optimization_generates_report(tmp_path: Path) -> None:
    study_dir = _write_mock_study(tmp_path)
    from scripts.analyze_optimization import main as analyze_main

    report_path = study_dir / "analysis_report.txt"
    rc = analyze_main([str(study_dir), "--output", str(report_path)])
    assert rc == 0
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "TOP TRIALS BY ANNUAL RETURN" in content
    assert "TOP TRIALS BY SHARPE RATIO" in content
    assert "TOP TRIALS BY COMPOSITE SCORE" in content
    assert "PARETO FRONTIER" in content
    assert "PARAMETER SENSITIVITY" in content
    assert "RECOMMENDED PARAMETER SET" in content
    assert "TRAIN vs VALIDATION" in content
    assert "STATISTICAL SUMMARY" in content
    assert (study_dir / "best_sharpe.json").exists()
    assert (study_dir / "best_return.json").exists()
    assert (study_dir / "best_combined.json").exists()


def test_analyze_optimization_with_only_failures(tmp_path: Path) -> None:
    study_dir = _write_mock_study(tmp_path, n_trials=3, n_failures=3)
    from scripts.analyze_optimization import main as analyze_main

    report_path = study_dir / "analysis_report.txt"
    rc = analyze_main([str(study_dir), "--output", str(report_path)])
    assert rc == 0
    content = report_path.read_text(encoding="utf-8")
    assert "No successful trials" in content
