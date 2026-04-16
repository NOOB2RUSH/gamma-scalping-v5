from __future__ import annotations

from dataclasses import replace
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

from gamma_scalping.optimization.models import DataSplit, OptimizationConfig, OptimizationStudyConfig, TrialPlan


def load_optimization_config(path: Path | str, *, stage: str | None = None) -> OptimizationConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    selected = dict(raw)
    if stage is not None and "stages" in raw:
        stages = raw["stages"]
        if stage not in stages:
            raise KeyError(f"Unknown optimization stage: {stage}")
        stage_raw = stages[stage]
        selected["parameters"] = stage_raw.get("parameters", raw.get("parameters", {}))
        selected["objective"] = stage_raw.get("objective", raw.get("objective", {}))
        study = dict(raw.get("study", {}))
        study.update(stage_raw.get("study", {}))
        selected["study"] = study

    study_config = _study_config(selected.get("study", {}))
    data_splits = tuple(DataSplit(**item) for item in selected.get("data_splits", []))
    if not data_splits:
        data_splits = (DataSplit(name="full"),)
    parameters = {key: tuple(value) for key, value in selected.get("parameters", {}).items()}
    return OptimizationConfig(
        study=study_config,
        data_splits=data_splits,
        parameters=parameters,
        objective=selected.get("objective", {}),
    )


def generate_trial_plan(config: OptimizationConfig, *, stage: str = "default") -> list[TrialPlan]:
    raw_combinations = _parameter_product(config.parameters)
    plans: list[TrialPlan] = []
    seen_hashes: set[str] = set()
    counter = 1
    for split in config.data_splits:
        for raw_parameters in raw_combinations:
            parameters = _effective_parameters(raw_parameters)
            if parameters is None:
                continue
            trial_hash = _stable_hash({"split": split.__dict__, "parameters": parameters})
            if trial_hash in seen_hashes:
                continue
            seen_hashes.add(trial_hash)
            trial_id = f"trial_{counter:06d}"
            run_id = f"{stage}_{split.name}_{trial_id}"
            plans.append(
                TrialPlan(
                    trial_id=trial_id,
                    run_id=run_id,
                    stage=stage,
                    split=split,
                    overrides=tuple(_overrides(parameters)),
                    parameters=parameters,
                    hash=trial_hash,
                )
            )
            counter += 1
            if config.study.max_trials is not None and len(plans) >= config.study.max_trials:
                return plans
    return plans


def _study_config(raw: dict[str, Any]) -> OptimizationStudyConfig:
    config = OptimizationStudyConfig(**raw)
    return replace(config, output_dir=Path(config.output_dir), base_config=Path(config.base_config))


def _parameter_product(parameters: dict[str, tuple[Any, ...]]) -> list[dict[str, Any]]:
    if not parameters:
        return [{}]
    keys = list(parameters)
    values = [parameters[key] for key in keys]
    return [dict(zip(keys, item)) for item in itertools.product(*values)]


def _effective_parameters(parameters: dict[str, Any]) -> dict[str, Any] | None:
    effective = dict(parameters)

    min_ttm = effective.get("strategy.min_ttm_days")
    target_ttm = effective.get("strategy.target_ttm_days")
    max_ttm = effective.get("strategy.max_ttm_days")
    if min_ttm is not None and target_ttm is not None and int(min_ttm) > int(target_ttm):
        return None
    if target_ttm is not None and max_ttm is not None and int(target_ttm) > int(max_ttm):
        return None

    if effective.get("strategy.max_open_positions", 1) != 1:
        return None
    if "strategy.exit_min_ttm_days" in effective:
        return None

    hv_windows = effective.get("volatility.hv_windows")
    hv_column = effective.get("volatility.rv_reference_hv_column")
    if hv_windows is not None and hv_column is not None:
        valid_columns = {f"hv_{int(window)}" for window in hv_windows}
        if hv_column not in valid_columns:
            return None

    mode = effective.get("volatility.rv_reference_mode")
    if mode == "current_hv":
        for key in [
            "volatility.rv_distribution_lookback_days",
            "volatility.rv_distribution_min_observations",
            "volatility.rv_distribution_quantile",
        ]:
            effective.pop(key, None)

    if effective.get("strategy.exit_on_vol_edge_filled") is False:
        for key in [
            "strategy.exit_max_rv_iv_edge",
            "strategy.exit_min_iv_rv_ratio",
            "strategy.exit_iv_reference_mode",
        ]:
            effective.pop(key, None)
    return dict(sorted(effective.items()))


def _overrides(parameters: dict[str, Any]) -> list[str]:
    return [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(parameters.items())]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
