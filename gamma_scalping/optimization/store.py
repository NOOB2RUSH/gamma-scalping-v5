from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from gamma_scalping.export_format import format_for_csv
from gamma_scalping.optimization.models import OptimizationConfig, TrialPlan, TrialResult


def prepare_study_dir(config: OptimizationConfig) -> Path:
    root = Path(config.study.output_dir) / config.study.study_id
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def write_study_inputs(root: Path, config: OptimizationConfig, plans: list[TrialPlan]) -> None:
    (root / "study_config.json").write_text(json.dumps(_study_payload(config), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (root / "plan.csv").write_text("", encoding="utf-8")
    plan_rows = [
        {
            "trial_id": plan.trial_id,
            "run_id": plan.run_id,
            "stage": plan.stage,
            "split": plan.split.name,
            "start_date": plan.split.start_date,
            "end_date": plan.split.end_date,
            "hash": plan.hash,
            **{f"param.{key}": value for key, value in plan.parameters.items()},
        }
        for plan in plans
    ]
    format_for_csv(pd.DataFrame(plan_rows)).to_csv(root / "plan.csv", index=False)


def write_results(root: Path, results: list[TrialResult]) -> None:
    rows = [result.to_summary_row() for result in results]
    summary = pd.DataFrame(rows)
    if summary.empty:
        summary.to_csv(root / "summary.csv", index=False)
        pd.DataFrame().to_csv(root / "failed.csv", index=False)
        (root / "best.json").write_text("{}", encoding="utf-8")
        return
    format_for_csv(summary).to_csv(root / "summary.csv", index=False)
    failed = summary[summary["status"].ne("success")].copy()
    format_for_csv(failed).to_csv(root / "failed.csv", index=False)
    successes = summary[summary["status"].eq("success")].copy()
    if successes.empty:
        (root / "best.json").write_text("{}", encoding="utf-8")
        return
    best = successes.sort_values("score", ascending=False).iloc[0].to_dict()
    (root / "best.json").write_text(json.dumps(best, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _study_payload(config: OptimizationConfig) -> dict[str, Any]:
    return {
        "study": config.study.__dict__,
        "data_splits": [item.__dict__ for item in config.data_splits],
        "parameters": config.parameters,
        "objective": config.objective,
    }
