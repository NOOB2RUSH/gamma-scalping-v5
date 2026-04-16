from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OptimizationStudyConfig:
    study_id: str = "gamma_scalping_opt"
    output_dir: Path | str = "results/optimization"
    base_config: Path | str = "config/backtest.default.json"
    workers: int = 1
    resume: bool = True
    max_trials: int | None = None
    diagnostics: bool = False
    save_trial_outputs: bool = False
    cache_market_calculations: bool = True
    write_results_every: int = 25
    prewarm_market_cache: bool = True


@dataclass(frozen=True)
class DataSplit:
    name: str
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class OptimizationConfig:
    study: OptimizationStudyConfig = field(default_factory=OptimizationStudyConfig)
    data_splits: tuple[DataSplit, ...] = ()
    parameters: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    objective: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialPlan:
    trial_id: str
    run_id: str
    stage: str
    split: DataSplit
    overrides: tuple[str, ...]
    parameters: dict[str, Any]
    hash: str


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    run_id: str
    stage: str
    split: str
    status: str
    elapsed_seconds: float
    parameters: dict[str, Any]
    metrics: dict[str, float]
    score: float
    output_dir: str
    error_message: str = ""

    def to_summary_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "trial_id": self.trial_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "split": self.split,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "score": self.score,
            "output_dir": self.output_dir,
            "error_message": self.error_message,
        }
        row.update({f"param.{key}": value for key, value in self.parameters.items()})
        row.update(self.metrics)
        return row
