from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp
from pathlib import Path
import time
import traceback

from gamma_scalping.optimization.models import OptimizationConfig, TrialResult
from gamma_scalping.optimization.runner import load_completed_trial, prewarm_trial_market_cache, run_trial
from gamma_scalping.optimization.space import generate_trial_plan
from gamma_scalping.optimization.store import prepare_study_dir, write_results, write_study_inputs


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


class OptimizationStudy:
    def __init__(self, config: OptimizationConfig, *, stage: str = "default") -> None:
        self.config = config
        self.stage = stage

    def run(self) -> list[TrialResult]:
        root = prepare_study_dir(self.config)
        plans = generate_trial_plan(self.config, stage=self.stage)
        write_study_inputs(root, self.config, plans)
        runs_dir = root / "runs"
        results: list[TrialResult] = []
        total = len(plans)
        started_at = time.monotonic()
        workers = max(1, int(self.config.study.workers))
        print(f"optimization_start stage={self.stage} total_trials={total} workers={workers}", flush=True)

        pending = []
        write_every = max(1, int(self.config.study.write_results_every))
        for plan in plans:
            result = load_completed_trial(plan, runs_dir=runs_dir) if self.config.study.resume else None
            if result is not None:
                results.append(result)
                if len(results) == total or len(results) % write_every == 0:
                    write_results(root, results)
                self._print_progress(started_at, len(results), total, result, resumed=True)
            else:
                pending.append(plan)

        if workers == 1:
            for plan in pending:
                result = run_trial(
                    plan,
                    base_config_path=self.config.study.base_config,
                    runs_dir=runs_dir,
                    study_config=self.config.study,
                )
                results.append(result)
                if len(results) == total or len(results) % write_every == 0:
                    write_results(root, results)
                self._print_progress(started_at, len(results), total, result, resumed=False)
            write_results(root, results)
            return results

        if pending and self.config.study.prewarm_market_cache and self.config.study.cache_market_calculations:
            prewarm_start = time.monotonic()
            print("optimization_prewarm_market_cache start", flush=True)
            prewarm_trial_market_cache(
                pending[0],
                base_config_path=self.config.study.base_config,
                study_config=self.config.study,
            )
            print(
                f"optimization_prewarm_market_cache done elapsed={_format_duration(time.monotonic() - prewarm_start)}",
                flush=True,
            )

        mp_context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None
        executor_kwargs = {"max_workers": workers}
        if mp_context is not None:
            executor_kwargs["mp_context"] = mp_context

        with ProcessPoolExecutor(**executor_kwargs) as executor:
            futures = {
                executor.submit(
                    run_trial,
                    plan,
                    base_config_path=self.config.study.base_config,
                    runs_dir=runs_dir,
                    study_config=self.config.study,
                ): plan
                for plan in pending
            }
            for future in as_completed(futures):
                plan = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    run_dir = runs_dir / plan.run_id
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                    result = TrialResult(
                        trial_id=plan.trial_id,
                        run_id=plan.run_id,
                        stage=plan.stage,
                        split=plan.split.name,
                        status="failed",
                        elapsed_seconds=0.0,
                        parameters=plan.parameters,
                        metrics={},
                        score=0.0,
                        output_dir=str(run_dir),
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                results.append(result)
                if len(results) == total or len(results) % write_every == 0:
                    write_results(root, results)
                self._print_progress(started_at, len(results), total, result, resumed=False)
        write_results(root, results)
        return results

    @staticmethod
    def _print_progress(started_at: float, completed: int, total: int, result: TrialResult, *, resumed: bool) -> None:
        elapsed = time.monotonic() - started_at
        average = elapsed / completed if completed else 0.0
        eta = average * (total - completed)
        print(
            "optimization_progress "
            f"completed={completed}/{total} "
            f"trial_id={result.trial_id} "
            f"status={result.status} "
            f"resumed={str(resumed).lower()} "
            f"elapsed={_format_duration(elapsed)} "
            f"eta={_format_duration(eta)}",
            flush=True,
        )
