# 参数调优模块设计

## 职责

参数调优模块负责批量生成参数组合、运行回测、汇总绩效和归因指标、筛选稳健参数，并支持训练/验证/测试切分。它不复制策略逻辑，不直接读行情文件细节，不重新实现回测流程。

模块必须复用现有统一配置体系：

- 基础配置来自 `config/backtest.default.json`。
- 单个 trial 通过 dotted override 修改配置，例如 `strategy.premium_budget_pct=0.1`。
- 回测入口复用 `BacktestEngine`、`GammaScalpingStrategy`、`VolatilityEngine`、`GreeksCalculator`、`PerformanceAnalyzer`、`GreeksPnLAttribution` 和 `PricingReconciliation`。

第一版目标不是寻找“最赚钱”的单一参数，而是寻找在统一回测区间和归因口径下都能解释清楚的参数区域。交易成本和滑点属于外生执行假设，不参与策略参数择优，只用于最终候选的敏感性验证。

## 参数生命周期

### 数据和价格口径

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 行情价格策略 | `data.price_policy` | `mid_first`, `bid_ask_conservative` | 2 | 影响成交、IV、估值口径。第一轮不作为核心搜索，作为稳健性场景。 |

### 仓位控制

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 权利金预算比例 | `strategy.premium_budget_pct` | `0.05`, `0.10`, `0.15`, `0.20` | 4 | 直接决定收益、回撤和资金利用率。 |
| 最大同时持仓数 | `strategy.max_open_positions` | 固定 `1` | 1 | 当前策略在已有期权仓位时会直接进入对冲决策，不会继续开新 episode；`2` 在第一版中不会产生有效差异。 |
| 单笔最大数量 | `risk.max_abs_order_quantity` | `null`, `100`, `500` | 3 | 实盘约束，研究阶段可固定为 `null`。 |

### Delta 对冲

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| Delta 对冲阈值 | `strategy.delta_threshold_pct` | `0.005`, `0.01`, `0.02`, `0.03` | 4 | 影响对冲频率、路径收益、成本和残差。 |

当前系统没有独立 `hedge_frequency` 参数，对冲由日频事件循环和 delta 阈值共同决定。因此第一版调优不引入 `hedge_frequency`。

### 期权选择

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 最小 DTE | `strategy.min_ttm_days` | `5`, `10` | 2 | 开仓时排除临近到期合约。 |
| 最大 DTE | `strategy.max_ttm_days` | `20`, `30`, `45` | 3 | 控制近月/次近月范围。 |
| 目标 DTE | `strategy.target_ttm_days` | `10`, `15`, `20`, `30` | 4 | 选择同一窗口内最接近目标期限的 ATM straddle。 |
| 最小期权价格 | `strategy.min_option_price` | `0.0001`, `0.005`, `0.01` | 3 | 防止极低价合约引入噪声。 |

约束：

```text
strategy.min_ttm_days <= strategy.target_ttm_days <= strategy.max_ttm_days
```

### ATM IV 聚合

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| ATM IV DTE 下限 | `atm_iv.min_ttm_days` | 跟随 `strategy.min_ttm_days` | - | 建议默认与交易合约窗口一致。 |
| ATM IV DTE 上限 | `atm_iv.max_ttm_days` | 跟随 `strategy.max_ttm_days` | - | 避免信号期限和交易合约期限错配。 |
| 聚合方式 | `atm_iv.aggregation` | `mean`, `median` | 2 | `median` 对异常 IV 更稳。 |
| 是否允许单边 | `atm_iv.allow_single_side` | `false`, `true` | 2 | 第一轮建议固定 `false`，减少脏信号。 |

### 流动性过滤

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 最小成交量 | `strategy.min_option_volume` | `1`, `100`, `500` | 3 | 成交质量过滤。 |
| 最小持仓量 | `strategy.min_open_interest` | `0`, `1000`, `5000` | 3 | 数据支持时启用。 |
| 最大价差比例 | `strategy.max_spread_pct` | `0.10`, `0.20`, `0.50` | 3 | 影响可交易合约数量和真实成本。 |

### HV/RV 选择

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| HV 窗口集合 | `volatility.hv_windows` | `[10,20,60]`, `[20,60,120]` | 2 | 决定可用 HV 列。 |
| RV 参考模式 | `volatility.rv_reference_mode` | `current_hv`, `rolling_quantile`, `max_current_and_quantile` | 3 | 波动率择时核心参数。 |
| RV 使用的 HV 列 | `volatility.rv_reference_hv_column` | `hv_10`, `hv_20`, `hv_60` | 3 | 短窗敏感，长窗稳定。 |
| RV 分布回看长度 | `volatility.rv_distribution_lookback_days` | `126`, `252`, `504` | 3 | 仅 rolling 模式有效。 |
| RV 分布最小样本 | `volatility.rv_distribution_min_observations` | `60`, `120` | 2 | 样本不足时不开信号。 |
| RV 分位数 | `volatility.rv_distribution_quantile` | `0.4`, `0.5`, `0.6`, `0.7` | 4 | 分位数越高，开仓门槛越严格。 |

约束：

```text
volatility.rv_reference_hv_column 必须存在于 volatility.hv_windows 对应输出列中
current_hv 模式忽略 distribution lookback / quantile
rolling_quantile 和 max_current_and_quantile 模式才使用 distribution 参数
```

### IV/HV 开仓择时

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 是否启用 vol filter | `strategy.use_vol_filter` | `true` | 1 | 参数调优阶段默认启用。 |
| 最小 RV-IV edge | `strategy.min_hv_iv_edge` | `0.00`, `0.02`, `0.04` | 3 | 绝对低估门槛。 |
| 最大 IV/RV 比例 | `strategy.entry_max_iv_rv_ratio` | `0.75`, `0.85`, `0.95` | 3 | 相对低估门槛。 |

开仓条件：

```text
rv_reference - atm_iv >= strategy.min_hv_iv_edge
atm_iv / rv_reference <= strategy.entry_max_iv_rv_ratio
```

### IV/HV 平仓择时

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 是否 edge 填补退出 | `strategy.exit_on_vol_edge_filled` | `false`, `true` | 2 | 开启后可减少 IV 修复后的回吐。 |
| 退出 RV-IV edge | `strategy.exit_max_rv_iv_edge` | `0.00`, `0.01`, `0.02` | 3 | edge 已被填补时退出。 |
| 退出 IV/RV 比例 | `strategy.exit_min_iv_rv_ratio` | `0.95`, `1.00`, `1.05` | 3 | IV 回升到 RV 附近或以上时退出。 |
| 退出 IV 口径 | `strategy.exit_iv_reference_mode` | `position_average_iv`, `held_position_vega_weighted_iv` | 2 | 比较简单平均和 Vega 加权持仓 IV。 |

约束：

```text
exit_on_vol_edge_filled=false 时忽略 exit_* 参数
```

### 到期与持仓周期

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 最大持仓天数 | `strategy.max_holding_days` | `null`, `10`, `20`, `30` | 4 | 当前按自然日计算，后续应改为交易日持仓天数。 |
| 到期日前提前平仓 DTE | 尚未实现，建议新增 `strategy.exit_min_ttm_days` | `1`, `3`, `5` | 3 | 与开仓 `min_ttm_days` 不同，用于持仓后提前退出。 |

`exit_min_ttm_days` 是第一版调优前建议补齐的参数。逻辑：

```text
若任一持仓期权 ttm_trading_days <= strategy.exit_min_ttm_days，则关闭该 episode 的期权腿和 ETF 对冲腿。
```

在该策略参数合入前，优化模块不得把 `strategy.exit_min_ttm_days` 放入可执行扫描空间，否则 dotted override 会因 `StrategyConfig` 不存在该字段而失败。

### 成本与滑点

| 参数 | 配置路径 | 建议扫描值 | 数量 | 说明 |
| --- | --- | --- | ---: | --- |
| 期权滑点 | `execution.option_slippage_bps` | 固定基准值；敏感性验证可用 `0`, `5`, `10` | 3 | 不参与主扫描择优。 |
| ETF 滑点 | `execution.etf_slippage_bps` | 固定基准值；敏感性验证可用 `0`, `1`, `3` | 3 | 不参与主扫描择优。 |
| ETF 费用 | `execution.etf_fee_bps` | 固定实际费率；敏感性验证场景化 | 2-3 | 根据真实交易成本设置。 |
| 期权按张费用 | `execution.option_fee_per_contract` | 固定实际费率；敏感性验证场景化 | 2-3 | 根据真实交易成本设置。 |
| 期权按金额费用 | `execution.option_fee_bps` | 固定实际费率；敏感性验证场景化 | 2-3 | 根据真实交易成本设置。 |

## 全量组合数量估算

如果对上述参数粗暴做笛卡尔积，组合数会非常大。保守估算：

```text
期权选择与流动性:
约 4 个有效 DTE 组合
* 2 个 ATM IV 聚合方式
* 3 * 3 * 3 个流动性组合
= 216

仓位与对冲:
4 个 premium_budget_pct
* 4 个 delta_threshold_pct
* 1 个 max_open_positions
= 16

波动率择时:
约 3 * 3 * 3 * 4 个 RV 参考组合
* 3 * 3 个开仓条件
* 2 * 3 * 3 * 2 个退出条件
= 34,992

全量:
216 * 16 * 34,992 ~= 120,932,352
```

全量扫描不可行。第一版必须使用分阶段、条件化、可恢复的搜索。

## 分阶段扫描方案

### Stage 0: baseline

固定默认配置，跑一次完整回测。

```text
组合数: 1
目标: 建立 baseline，确认输出完整。
```

### Stage 1: 波动率择时

固定仓位、期权选择、流动性和成本，只搜索 IV/HV 逻辑。

建议参数：

```text
volatility.rv_reference_mode:
  current_hv
  rolling_quantile
  max_current_and_quantile

volatility.rv_reference_hv_column:
  hv_10
  hv_20
  hv_60

volatility.rv_distribution_lookback_days:
  126
  252
  504

volatility.rv_distribution_quantile:
  0.5
  0.6
  0.7

strategy.entry_max_iv_rv_ratio:
  0.75
  0.85
  0.95

strategy.min_hv_iv_edge:
  0.00
  0.02

strategy.exit_on_vol_edge_filled:
  false
  true

strategy.exit_max_rv_iv_edge:
  0.00
  0.01

strategy.exit_min_iv_rv_ratio:
  1.00
  1.05

strategy.exit_iv_reference_mode:
  position_average_iv
  held_position_vega_weighted_iv
```

条件化后估算：

```text
current_hv:
3 hv * 6 entry * 9 exit = 162

rolling_quantile:
3 hv * 3 lookback * 3 quantile * 6 entry * 9 exit ~= 1,458

max_current_and_quantile:
同上 ~= 1,458

Stage 1 总计 ~= 3,078
```

其中 `9 exit` 表示：

```text
exit_on_vol_edge_filled=false: 1 个组合，忽略 exit_max_rv_iv_edge / exit_min_iv_rv_ratio / exit_iv_reference_mode
exit_on_vol_edge_filled=true: 2 * 2 * 2 = 8 个组合
合计: 9
```

第一版建议不要完整运行 `3,078` 组；先随机抽样 `200-300` 组，再对排名靠前区域局部加密。

### Stage 2: 期权选择和退出周期

取 Stage 1 前 `10-20` 组，扫描：

```text
DTE 组合:
  5-20 target 10
  5-30 target 15
  10-30 target 20
  20-45 target 30

atm_iv.aggregation:
  mean
  median

strategy.max_holding_days:
  null
  10
  20
  30

strategy.max_spread_pct:
  0.20
  0.50
```

估算：

```text
20 * 4 * 2 * 4 * 2 = 2,560
```

若取 Stage 1 前 `10` 组，则约 `1,280`。

`strategy.exit_min_ttm_days` 不进入第一版 Stage 2 扫描。若后续先实现该参数，可把它作为 Stage 2 的新增维度，组合数再乘以 `3`。

### Stage 3: 仓位和对冲

取 Stage 2 前 `20` 组，扫描：

```text
strategy.premium_budget_pct:
  0.05
  0.10
  0.15
  0.20

strategy.delta_threshold_pct:
  0.005
  0.01
  0.02
  0.03

strategy.max_open_positions:
  fixed 1
```

估算：

```text
20 * 4 * 4 = 320
```

`max_open_positions=2` 是未来能力，不属于第一版可执行调优空间。若后续实现多 episode 同时开仓逻辑，再把该参数放回 Stage 3。

### Stage 4: 成本与稳健性敏感性验证

该阶段不是参数优化，不参与按年化收益率或夏普比率的主榜择优。取 Stage 3 前 `20` 组候选参数，在固定参数不变的情况下，只改变外生执行假设，检查候选是否对成本过度敏感：

```text
成本场景:
  optimistic: option_slippage_bps=0, etf_slippage_bps=0
  base: option_slippage_bps=5, etf_slippage_bps=1
  conservative: option_slippage_bps=10, etf_slippage_bps=3

数据区间:
  2018-2025 full
```

估算：

```text
20 * 3 * 4 = 240
```

### 推荐总体规模

```text
Stage 0: 1
Stage 1: 200-3,078
Stage 2: 1,280-2,560
Stage 3: 320
Stage 4: 240

合计: 约 2,041 到 6,199 次回测
```

## 指标体系

参数调优不能只按收益排序。每个 trial 应汇总以下指标。

### 收益和风险

现有 `PerformanceAnalyzer.compute_metrics()` 可直接读取：

- `initial_equity`
- `final_equity`
- `cumulative_return`
- `annual_return`
- `annual_volatility`
- `max_drawdown`
- `sharpe_ratio`
- `sortino_ratio`
- `calmar_ratio`
- `var`
- `cvar`
- `observation_count`

优化模块需要额外派生：

- `total_pnl = final_equity - initial_equity`
- `total_return`: 不新增字段，统一使用现有 `cumulative_return`

### 交易行为

现有绩效摘要可直接读取：

- `total_trade_amount`
- `total_fee`
- `trade_count`
- `rebalance_count`

优化模块需要从回测输出额外聚合：

- `episode_count`: `episode_records.csv` 中有效 episode 数。
- `avg_holding_days`: `episode_records.closed_at - opened_at` 的平均值，未关闭 episode 可单独统计。
- `option_trade_count`: `trade_records.instrument_type == "option"` 的非空交易行数量。
- `hedge_trade_count`: `trade_records.instrument_type == "etf"` 且 `role == "hedge"` 的非空交易行数量。
- `turnover = total_trade_amount / initial_equity`。
- `avg_premium_usage`: 第一版暂不作为必需指标，除非后续在回测结果中输出每日权利金占用。

### Greeks 归因

现有 `PerformanceAnalyzer` 的 attribution summary 可直接读取：

- `avg_delta_exposure`
- `avg_gamma_exposure`
- `avg_theta_exposure`
- `avg_vega_exposure`
- `total_gamma_theta_pnl`
- `total_vega_pnl`
- `avg_residual_ratio`

优化模块需要从 `greeks_attribution.csv` 额外聚合：

- `total_delta_pnl = sum(delta_pnl)`
- `total_gamma_pnl = sum(gamma_pnl)`
- `total_theta_pnl = sum(theta_pnl)`
- `total_hedge_pnl = sum(hedge_pnl)`
- `total_cost_pnl = sum(cost_pnl)`
- `total_residual_pnl = sum(residual_pnl)`
- `total_explained_pnl = sum(explained_pnl)`

### 价格链路诊断

来自 `pricing_reconciliation_daily.csv`：

- `total_mark_pnl`
- `total_model_repricing_pnl`
- `total_model_spot_pnl`
- `total_model_theta_pnl`
- `total_model_vega_pnl`
- `total_market_model_basis_pnl`
- `total_taylor_residual_pnl`
- `total_mark_residual_pnl`

这些指标用于判断策略到底赚的是：

- Gamma / 标的波动的钱。
- Vega / IV 上升的钱。
- 市场价相对 BSM 模型价的基差。
- 或者仅来自无法稳定解释的残差。

高阶 Greeks 不应被默认视为残差主因。若 `market_model_basis_pnl` 或 `taylor_residual_pnl` 很大，应优先解释价格口径和日频近似误差。

### IV/HV 捕获

现有 `PerformanceAnalyzer.compute_metrics()` 合并 `IvHvCaptureResult.summary` 后可直接读取：

- `iv_hv_capture_rate_mean`
- `iv_hv_capture_rate_median`
- `iv_hv_capture_rate_weighted`
- `iv_hv_capture_rate_valid_count`
- `iv_hv_signal_hit_rate`

`iv_hv_capture_rate` 是 `iv_hv_capture_episodes.csv` 的 episode 明细字段，不是 summary 字段。

优化模块如需更细指标，需要从 `iv_hv_capture_episodes.csv` 额外聚合：

- `avg_theoretical_vol_edge_pnl = mean(theoretical_vol_edge_pnl)`
- `avg_net_gamma_scalping_pnl = mean(net_gamma_scalping_pnl)`
- `profitable_episode_ratio = mean(net_gamma_scalping_pnl > 0)`
- `avg_realized_vol_holding = mean(realized_vol_holding)`
- `avg_entry_atm_iv = mean(entry_atm_iv)`

如需 `avg_entry_edge`、`avg_entry_ratio`、`profitable_episode_ratio`，优化模块需要从 `episode_records.csv`、`decisions.csv` 或 `iv_hv_capture_episodes.csv` 额外聚合，不能假设 `PerformanceAnalyzer` 已直接输出。

## 目标函数

第一版建议支持可配置目标函数，但内置默认目标：

```text
score =
  annual_return
  - 0.8 * abs(max_drawdown)
  + 0.3 * sortino_ratio
  + 0.2 * total_gamma_theta_pnl / initial_cash
  - 0.2 * abs(total_residual_pnl) / initial_cash
```

硬约束：

```text
episode_count >= 20
max_drawdown >= -0.20
total_gamma_theta_pnl >= 0
abs(total_residual_pnl) / initial_cash <= 0.20
iv_hv_capture_rate_valid_count >= 10
```

目标函数字段来源：

| 字段 | 来源 | 计算方式 |
| --- | --- | --- |
| `annual_return` | `PerformanceAnalyzer.compute_metrics().summary` | 直接读取 |
| `max_drawdown` | `PerformanceAnalyzer.compute_metrics().summary` | 直接读取 |
| `sortino_ratio` | `PerformanceAnalyzer.compute_metrics().summary` | 直接读取 |
| `initial_cash` | `config.backtest.initial_cash` 或 `initial_equity` | 优先使用配置 |
| `total_gamma_theta_pnl` | 现有 attribution summary | 直接读取；也可由 `sum(gamma_theta_pnl)` 校验 |
| `total_residual_pnl` | `greeks_attribution.csv` | `sum(residual_pnl)` |
| `episode_count` | `episode_records.csv` | 有效 episode 数 |
| `iv_hv_capture_rate_valid_count` | IV/HV capture summary | 直接读取 |

输出至少两张排行榜：

1. **实际账户收益榜**：按 `score` 排序。
2. **Gamma Scalping 纯度榜**：优先看 `total_gamma_theta_pnl`、`total_vega_pnl` 依赖度、`market_model_basis_pnl` 和 `residual` 质量。

## 训练/验证/测试切分

默认切分建议：

```text
train: 2020-01-01 到 2022-12-31
valid: 2023-01-01 到 2023-12-31
test:  2024-01-01 到 2025-12-31
full:  2020-01-01 到 2025-12-31
```

原则：

- Stage 1-3 主要在 train 上搜索。
- valid 用于选择参数区域，不用于反复人工过拟合。
- test 只用于最终验收。
- full 只作为最终展示，不作为选择依据。

## 模块结构

建议新增：

```text
gamma_scalping/optimization/
├── __init__.py
├── models.py       # TrialPlan, TrialResult, OptimizationStudy 等 dataclass
├── space.py        # 参数空间定义、条件约束、组合生成
├── runner.py       # 执行单次回测
├── evaluator.py    # 指标提取和评分
├── store.py        # 结果保存、断点恢复
└── study.py        # Study orchestration
```

新增 CLI：

```text
scripts/run_optimization.py
```

调用示例：

```bash
python3 scripts/run_optimization.py \
  --config config/backtest.default.json \
  --space config/optimization.default.json \
  --study-id vol_timing_2020_2025 \
  --stage vol_timing \
  --workers 4
```

## 配置文件

新增：

```text
config/optimization.default.json
```

示例结构：

```json
{
  "study": {
    "study_id": "gamma_scalping_opt",
    "output_dir": "results/optimization",
    "base_config": "config/backtest.default.json",
    "workers": 4,
    "resume": true
  },
  "data_splits": [
    {
      "name": "full_2018_2025",
      "start_date": "2018-01-01",
      "end_date": "2025-12-31"
    }
  ],
  "parameters": {
    "strategy.premium_budget_pct": [0.05, 0.1, 0.15],
    "strategy.delta_threshold_pct": [0.005, 0.01, 0.02],
    "strategy.entry_max_iv_rv_ratio": [0.75, 0.85, 0.95],
    "strategy.min_hv_iv_edge": [0.0, 0.02],
    "volatility.rv_reference_mode": ["current_hv", "rolling_quantile"],
    "volatility.rv_reference_hv_column": ["hv_10", "hv_20", "hv_60"],
    "volatility.rv_distribution_quantile": [0.5, 0.6, 0.7]
  },
  "objective": {
    "primary": "score",
    "constraints": {
      "episode_count_min": 20,
      "max_drawdown_min": -0.2,
      "gamma_theta_pnl_min": 0.0
    }
  }
}
```

第一版不实现任意字符串表达式约束。约束函数写在 `space.py` 中，避免执行不可信表达式。

## 执行流程

单个 trial：

```text
1. 读取 base config。
2. 应用参数 overrides。
3. 设置 data.start_date / data.end_date / backtest.run_id。
4. 加载 snapshots。
5. 运行 BacktestEngine。
6. 导出回测结果。
7. 运行 GreeksPnLAttribution。
8. 运行 PricingReconciliation。
9. 运行 PerformanceAnalyzer.compute_metrics。
10. 汇总指标并计算 score。
11. 写入 trial 结果。
```

study：

```text
1. 生成参数计划 plan.csv。
2. 对每组参数生成 stable hash。
3. 若 resume=true 且已有成功结果，则跳过。
4. 执行未完成 trial。
5. 每完成一组立即落盘 summary.csv。
6. 失败写入 failed.csv，不中断整个 study。
7. 结束后生成 best.json。
```

## 结果目录

建议结构：

```text
results/optimization/{study_id}/
├── study_config.json
├── search_space.json
├── plan.csv
├── summary.csv
├── best.json
├── failed.csv
├── logs/
│   └── optimization.log
└── runs/
    ├── run_000001/
    │   ├── config.json
    │   ├── metrics.json
    │   ├── equity_curve.csv
    │   ├── trade_records.csv
    │   ├── greeks_attribution.csv
    │   └── pricing_reconciliation_daily.csv
    └── run_000002/
```

`summary.csv` 一行一个 trial，至少包含：

- `trial_id`
- `run_id`
- `stage`
- `split`
- `status`
- `elapsed_seconds`
- 参数字段
- 核心绩效指标
- 归因指标
- 价格链路诊断指标
- `score`
- `output_dir`
- `error_message`

## 并行与缓存

第一版优先实现单进程和断点续跑。并行是第二步。

并行实现建议：

- 使用 `concurrent.futures.ProcessPoolExecutor`。
- 每个 trial 写独立目录，避免并发写冲突。
- `summary.csv` 由主进程统一追加。
- 日志按 trial 独立保存。

缓存策略：

- 参数 hash 由 base config 路径、overrides、数据区间和代码版本组成。
- 若 trial 目录存在 `metrics.json` 且状态为 success，则 resume 时跳过。
- 不缓存中间行情对象，避免不同配置的数据口径混用。

## 前置依赖和第一版实现边界

### 当前可立即实现

当前代码不支持多 episode 同时开仓，也没有 `strategy.exit_min_ttm_days`。因此可立即实现的优化模块必须：

- 固定 `strategy.max_open_positions=1`。
- 不生成 `strategy.exit_min_ttm_days` override。
- 使用 `strategy.max_holding_days`、`strategy.min_ttm_days`、`strategy.max_ttm_days` 和已有风险退出逻辑控制持仓周期。

### 可选前置增强

若希望 Stage 2 覆盖“到期日前提前平仓”，需要先实现：

```python
StrategyConfig.exit_min_ttm_days: int | None = None
```

逻辑：

```text
若任一持仓期权 ttm_trading_days <= strategy.exit_min_ttm_days，则关闭该 episode 的期权腿和 ETF 对冲腿。
```

原因：

- `strategy.min_ttm_days` 只约束开仓。
- 持仓后临近到期的风险需要独立退出参数。
- 当前只靠到期、坏 Greeks、坏 IV 或最大持仓天数退出，不足以控制临近到期的 Gamma/Theta 非线性。

若希望扫描 `strategy.max_open_positions > 1`，需要先实现多 episode 同时开仓逻辑。当前代码在已有期权仓位时会直接返回 hedge 决策，因此 `max_open_positions=2` 不会真正增加持仓。

第一版实现：

- JSON 参数空间。
- 条件化网格生成。
- 单进程 trial runner。
- `summary.csv`、`failed.csv`、`best.json`。
- resume 跳过已成功 trial。
- Stage 1 和 Stage 2。

第一版暂不实现：

- Bayesian optimization。
- Optuna。
- 分布式执行。
- 任意表达式约束。
- 自动画调优报告 HTML。
- 多进程并行。
- 多 episode 同时开仓扫描。
- `exit_min_ttm_days` 扫描，除非先完成上述前置增强。

## 测试重点

- 参数网格组合数量正确。
- 条件约束正确过滤无效组合。
- dotted override 能正确生成 `UnifiedBacktestConfig`。
- trial 失败不会中断整个 study。
- resume 不重复运行已成功 trial。
- `summary.csv` 指标和单次回测输出一致。
- `best.json` 排序和目标函数一致。
- `exit_min_ttm_days` 触发时能同时关闭期权腿和同 episode 的 ETF 对冲腿。
