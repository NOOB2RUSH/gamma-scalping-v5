# 用户可用入口说明

本文档记录当前已经暴露给用户使用的脚本入口、Python API 入口、尚未实现的入口，以及推荐调用方式。

## 当前结论

当前项目已有统一回测脚本入口：

- 脚本：`scripts/run_backtest.py`
- 默认配置：`config/backtest.default.json`
- 参数覆盖：仅通过可重复的 `--params section.field=value` 指定。
- 脚本不暴露 `--initial-cash`、`--start-date` 等孤立参数；用户未在 `--params` 指定的值，一律来自统一配置文件。

当前项目已有第一版参数调优脚本入口：

- 脚本：`scripts/run_optimization.py`
- 远程运行脚本：`scripts/remote_optimization.sh`
- 默认搜索空间：`config/optimization.default.json`
- 远程配置模板：`config/remote.example.env`
- 实现模块：`gamma_scalping/optimization`
- 设计文档：`docs/modules/parameter_tuning.md`
- 远程运行方案：`docs/remote_optimization.md`
- 第一版支持：条件化网格、单进程 trial runner、断点续跑、`summary.csv`、`failed.csv`、`best.json`

## 回测入口

推荐使用脚本入口：

```bash
scripts/run_backtest.py \
  --config config/backtest.default.json \
  --params data.start_date=2024-04-08 \
  --params data.end_date=2024-04-10 \
  --params backtest.run_id=example_run
```

常用覆盖示例：

```bash
scripts/run_backtest.py \
  --params strategy.premium_budget_pct=0.2 \
  --params strategy.delta_threshold_pct=0.015 \
  --params backtest.initial_cash=2000000 \
  --params common.annual_trading_days=244 \
  --params report.enabled=false
```

规则：

- `--config` 指向统一 JSON 配置文件；不指定时默认使用 `config/backtest.default.json`。
- `--params` 使用 `section.field=value`，可重复传入。
- `value` 支持 JSON 标量，例如 `true`、`false`、`null`、数字、字符串。
- 未在 `--params` 指定的参数使用配置文件中的值。
- 未知 section 或未知字段会直接报错，避免静默拼写错误。
- 如果将 `backtest.output_dir` 设为 `null`，脚本不会自动写入默认目录；此时若 `report.enabled=true`，必须显式设置 `report.output_dir`。

当前统一配置入口是：

```python
from gamma_scalping.config import UnifiedBacktestConfig, load_unified_config, save_unified_config
```

统一配置包含以下 section：

```text
common
data
greeks
volatility
atm_iv
strategy
execution
risk
backtest
attribution
performance
report
```

其中 `common.annual_trading_days` 会同步传播到 `data.calendar`、`greeks`、`volatility`、`performance`；`common.strategy_tag` 会同步传播到 `strategy` 和 `backtest`。需要保持全系统口径一致时，优先修改 `common`，不要分别修改多个 section。

Python API 仍可用于测试、研究和二次开发：

回测入口是：

```python
from gamma_scalping.backtest import BacktestConfig, BacktestEngine, ExecutionModel, RiskChecker
```

核心对象：

- `BacktestEngine.run(snapshots, etf_history=None) -> BacktestResult`
- `BacktestConfig`
- `ExecutionModel`
- `RiskChecker`

最小调用流程：

```python
import pandas as pd

from gamma_scalping.backtest import BacktestEngine
from gamma_scalping.config import load_unified_config
from gamma_scalping.data import MarketDataLoader
from gamma_scalping.greeks import GreeksCalculator
from gamma_scalping.strategy import GammaScalpingStrategy
from gamma_scalping.volatility import VolatilityEngine

config = load_unified_config(
    "config/backtest.default.json",
    [
        "data.start_date=2024-04-08",
        "data.end_date=2024-04-12",
        "backtest.run_id=example_run",
    ],
)

loader = MarketDataLoader(config.data)
snapshots = list(loader.iter_snapshots())
etf_history = pd.DataFrame(
    {"close": [snapshot.etf_bar.close for snapshot in snapshots]},
    index=pd.Index([snapshot.trading_date for snapshot in snapshots], name="date"),
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
```

如果设置 `BacktestConfig.output_dir`，回测会自动导出：

```text
results/
└── {run_id}/
    ├── config.json
    ├── metadata.json
    ├── trade_records.csv
    ├── position_records.csv
    ├── episode_records.csv
    ├── equity_curve.csv
    ├── decisions.csv
    ├── fills.csv
    ├── expiry_events.csv
    ├── final_positions.csv
    ├── greeks_history.csv
    └── iv_history.csv
```

也可以手动导出：

```python
paths = result.export_csv(config.backtest.output_dir)
```

## 回测参数配置入口

用户运行回测时应使用统一配置文件 `config/backtest.default.json`。脚本入口只读取该配置，并允许通过 `--params section.field=value` 做临时覆盖。

底层模块仍保留各自的 dataclass 配置对象，作为统一配置的 schema 和模块内部依赖：

```python
from gamma_scalping.data import MarketDataConfig
from gamma_scalping.greeks import GreeksConfig
from gamma_scalping.volatility import VolatilityConfig, AtmIvConfig
from gamma_scalping.strategy import StrategyConfig
from gamma_scalping.backtest import BacktestConfig, ExecutionModel, RiskChecker
```

常用参数请直接查看和修改 `config/backtest.default.json`。如果只是临时实验，使用 `--params section.field=value` 或 `load_unified_config(..., overrides=[...])` 覆盖，不建议在用户脚本里分散创建各模块配置对象。

注意：

- 当前支持统一 JSON 配置文件加载；暂未提供 YAML 加载。
- 当前 CLI 只支持 `--config` 与 `--params`，不新增孤立参数入口。
- `MarketDataLoader` 当前没有 `load_etf_history()` 方法；若需要传入 `BacktestEngine.run(..., etf_history=...)` 或波动率时间序列，应由用户从本地数据或 `snapshots` 组装包含 `close` 列、以交易日期为索引的 DataFrame。
- 参数调优已有脚本入口 `scripts/run_optimization.py`；当前没有 `ParameterTuner.run_all()` 这个 Python API。

## 参数调优入口

推荐使用脚本入口：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.default.json \
  --stage vol_timing \
  --study-id vol_timing_2020_2025
```

快速 smoke test 可以限制 trial 数：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.default.json \
  --stage smoke \
  --study-id smoke_opt \
  --max-trials 1
```

默认输出目录：

```text
results/optimization/{study_id}/
├── study_config.json
├── plan.csv
├── summary.csv
├── failed.csv
├── best.json
└── runs/
    └── {run_id}/
        ├── equity_curve.csv
        ├── trade_records.csv
        ├── greeks_attribution.csv
        ├── pricing_reconciliation_daily.csv
        ├── metrics.json
        └── unified_config.json
```

当前限制：

- 第一版是单进程执行；`workers` 字段保留但暂不启用并行。
- `strategy.max_open_positions` 在可执行搜索空间中固定为 `1`。
- 尚未实现 `strategy.exit_min_ttm_days`，优化器会过滤包含该字段的 trial。
- 不支持 YAML 搜索空间。

### 远程参数调优入口

参数调优开销较大，正式扫描推荐在远程服务器运行。先复制并填写远程配置：

```bash
cp config/remote.example.env config/remote.env
```

最小配置：

```bash
REMOTE_HOST="user@host"
REMOTE_PROJECT_DIR="/home/user/gamma_scalping_v5"
REMOTE_VENV=".venv"
OPT_STAGE="vol_timing"
OPT_STUDY_ID="remote_vol_timing"
```

同步代码到远程：

```bash
scripts/remote_optimization.sh sync
```

远程 smoke test：

```bash
scripts/remote_optimization.sh smoke
scripts/remote_optimization.sh status
```

正式远程后台运行：

```bash
scripts/remote_optimization.sh run
```

拉取远程结果：

```bash
scripts/remote_optimization.sh fetch
```

详细流程见 `docs/remote_optimization.md`。默认不会同步 `data/` 和 `results/`，远程服务器应提前准备好行情数据。

## Greeks 归因入口

统一 CLI 会在回测完成后自动执行 Greeks PnL 归因。默认输出目录为：

```text
results/{run_id}/
├── greeks_history.csv
├── iv_history.csv
├── greeks_attribution.csv
├── greeks_attribution_by_episode.csv
├── greeks_attribution_cumulative.csv
├── attribution_quality.csv
├── pricing_reconciliation.csv
└── pricing_reconciliation_daily.csv
```

若 `report.enabled=true`，脚本还会把归因结果传给绩效报告，额外生成：

```text
results/{run_id}/report/
├── greeks_attribution_daily.png
├── greeks_attribution_cumulative.png
└── iv_hv_capture_episodes.csv
```

手工调用时，归因入口是：

```python
from gamma_scalping.attribution import GreeksPnLAttribution, PricingReconciliation, PricingReconciliationConfig
```

调用方式：

```python
import pandas as pd

underlying_history = pd.DataFrame(
    {
        "trading_date": [snapshot.trading_date for snapshot in snapshots],
        "underlying": [snapshot.underlying for snapshot in snapshots],
        "close": [snapshot.etf_bar.close for snapshot in snapshots],
    }
)

attribution = GreeksPnLAttribution().attribute_daily(
    equity_curve=result.equity_curve,
    trade_records=result.trade_records,
    position_records=result.position_records,
    greeks_history=result.greeks_history,
    iv_history=result.iv_history,
    underlying_history=underlying_history,
)

attribution.export_csv("results/example_run")

PricingReconciliation(
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
).export_csv("results/example_run")
```

输出：

```text
greeks_attribution.csv
greeks_attribution_by_episode.csv
greeks_attribution_cumulative.csv
attribution_quality.csv
pricing_reconciliation.csv
pricing_reconciliation_daily.csv
```

当前注意点：

- 使用统一 CLI 时，`greeks_history` 和 `iv_history` 由 `BacktestEngine` 自动收集并导出。
- 手工调用 `GreeksPnLAttribution` 时，仍需要用户显式提供 `greeks_history` 和 `iv_history`。
- `greeks_attribution_by_episode.csv` 依赖回测输出中的 `episode_id`。
- episode 级归因只包含可精确定义的分量 PnL，不包含 episode 级 `actual_pnl` 和 `residual_pnl`。
- `pricing_reconciliation_daily.csv` 用于人工检查市场盯市 PnL、BSM 模型重定价 PnL、有限差分模型拆分和 previous-close Greeks 泰勒近似之间的差异。

## 绩效与可视化入口

绩效入口是：

```python
from gamma_scalping.performance import IvHvCaptureAnalyzer, PerformanceAnalyzer, Visualizer
```

生成指标和报告：

```python
from pathlib import Path

from gamma_scalping.config import load_unified_config
from gamma_scalping.performance import PerformanceAnalyzer

config = load_unified_config("config/backtest.default.json")
volatility_series = volatility_engine.build_signal_series(snapshots, etf_history, config.atm_iv)
report_dir = (
    Path(config.report.output_dir)
    if config.report.output_dir
    else Path(config.backtest.output_dir) / result.run_id / "report"
)

report = PerformanceAnalyzer(config.performance).build_report(
    result,
    output_dir=report_dir,
    attribution=attribution,
    volatility=volatility_series,
    underlying_history=underlying_history,
)
```

输出：

```text
report/
├── performance_metrics.csv
├── daily_returns.csv
├── monthly_returns.csv
├── performance_report.html
├── equity_curve.png
├── drawdown.png
├── volatility_series.png
├── greeks_attribution_daily.png
├── greeks_attribution_cumulative.png
└── iv_hv_capture_episodes.csv
```

也可以只计算指标：

```python
metrics = PerformanceAnalyzer(config.performance).compute_metrics(
    result,
    attribution=attribution,
    volatility=volatility_series,
    underlying_history=underlying_history,
)

print(metrics.summary)
```

单独计算 IV/HV 捕获率：

```python
from gamma_scalping.config import load_unified_config
from gamma_scalping.performance import IvHvCaptureAnalyzer, IvHvCaptureConfig

config = load_unified_config("config/backtest.default.json")

capture = IvHvCaptureAnalyzer(
    IvHvCaptureConfig(
        annual_trading_days=config.performance.annual_trading_days,
        denominator_eps=config.performance.iv_hv_capture_denominator_eps,
        min_return_observations=config.performance.iv_hv_capture_min_return_observations,
    )
).compute(
    episode_records=result.episode_records,
    attribution=attribution,
    underlying_history=underlying_history,
)

capture.export_csv("results/example_run/report")
print(capture.summary)
```

注意：

- `underlying_history` 必须包含 `trading_date` 和 `close`，或以交易日期为索引并包含 `close` 列。
- 捕获率依赖 `result.episode_records` 和 `attribution.by_episode`。
- 无效 episode 会在 `iv_hv_capture_episodes.csv` 中标记 `valid=False` 和 `invalid_reason`。

## 波动率时间序列入口

波动率模块入口是：

```python
from gamma_scalping.volatility import AtmIvConfig, VolatilityConfig, VolatilityEngine
```

常用调用：

```python
from gamma_scalping.config import load_unified_config
from gamma_scalping.volatility import VolatilityEngine

config = load_unified_config("config/backtest.default.json")
volatility_engine = VolatilityEngine(config.volatility)

volatility_series = volatility_engine.build_signal_series(
    snapshots,
    etf_history,
    atm_config=config.atm_iv,
)
```

当前限制：

- IV 第一版只支持 `dividend_rate=0`。
- ATM IV 聚合口径由 `AtmIvConfig` 控制。

## 尚未实现的用户入口

以下内容目前还没有可调用实现：

- 参数调优 Python API，例如 `ParameterTuner.run_all(...)`
- YAML 配置文件加载
- `pyproject.toml` 中的 `console_scripts` 安装入口，例如 `gamma-scalping backtest --config config.yml`

这些入口后续实现后，应同步更新本文档。
