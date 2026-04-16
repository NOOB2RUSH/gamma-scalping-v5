#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamma_scalping.optimization import OptimizationStudy, load_optimization_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run gamma scalping parameter optimization.")
    parser.add_argument("--space", default=str(ROOT / "config" / "optimization.default.json"), help="Path to optimization JSON config.")
    parser.add_argument("--stage", default="default", help="Optimization stage name. Used for run_id prefixes and optional stage config selection.")
    parser.add_argument("--study-id", default=None, help="Override study.study_id from the optimization config.")
    parser.add_argument("--max-trials", type=int, default=None, help="Limit generated trials for smoke tests or sampling.")
    parser.add_argument("--workers", type=int, default=None, help="Override study.workers from the optimization config.")
    args = parser.parse_args(argv)

    config = load_optimization_config(args.space, stage=args.stage)
    if args.study_id is not None or args.max_trials is not None or args.workers is not None:
        from dataclasses import replace

        config = replace(
            config,
            study=replace(
                config.study,
                study_id=args.study_id or config.study.study_id,
                max_trials=args.max_trials if args.max_trials is not None else config.study.max_trials,
                workers=args.workers if args.workers is not None else config.study.workers,
            ),
        )
    results = OptimizationStudy(config, stage=args.stage).run()
    successes = sum(1 for result in results if result.status == "success")
    failures = len(results) - successes
    output_dir = Path(config.study.output_dir) / config.study.study_id
    print(f"study_id={config.study.study_id}")
    print(f"output_dir={output_dir}")
    print(f"trials={len(results)} successes={successes} failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
