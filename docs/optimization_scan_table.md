# 参数细化扫描表

本文档给出比 `config/optimization.default.json` 更细的参数扫描范围。它面向后续远程分阶段扫描，不建议一次性全量笛卡尔积执行。

当前约束：

- `strategy.max_open_positions` 固定为 `1`，当前策略不支持多 episode 同时开仓。
- 不扫描 `strategy.exit_min_ttm_days`，该参数尚未实现。
- 所有扫描都启用 `strategy.use_vol_filter=true`。
- 扫描数据范围统一为 `2018-01-01` 至 `2025-12-31`，不区分训练集和测试集。
- 交易成本、滑点和价格口径是外生执行假设，不作为策略参数择优；只能用于最终候选的敏感性验证。
- 每个 stage 使用新的 `OPT_STUDY_ID`，避免结果目录混写。

## 总览

| Stage | 目标 | 建议用途 | 估算组合数 |
| --- | --- | --- | ---: |
| `vol_entry_screen` | 中等粒度 IV/HV 开仓择时 | 第一轮筛选，优先运行 | 468 |
| `vol_entry_fine` | 细化 IV/HV 开仓择时 | 基于 screen 结果局部加密，不建议直接全量 | 5,880 |
| `vol_entry_refine_best` | 围绕当前最优区域局部加密 | 基于 `trial_000350` 附近继续搜索 | 360 |
| `vol_exit_fine` | 细化 IV/HV 平仓条件 | 基于开仓较优区域继续扫描 | 约 900 |
| `contract_fine` | 细化 DTE、ATM IV、流动性过滤 | 期权选择敏感性 | 约 6,000-20,000，需抽样或缩小范围 |
| `position_hedge_fine` | 仓位和 delta 对冲强度 | 资金利用率/回撤权衡 | 25 |
| `cost_sensitivity` | 成本、滑点、价格口径敏感性验证 | 最终候选稳健性检查，不参与择优 | 576 |

## Stage 1A: vol_entry_screen

目标：用较小组合数先筛选 RV 参考、entry ratio 和 edge 的有效区域。该阶段应优先于 `vol_entry_fine` 运行。

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| RV 参考模式 | `volatility.rv_reference_mode` | `current_hv`, `rolling_quantile`, `max_current_and_quantile` | 3 |
| RV 使用 HV 列 | `volatility.rv_reference_hv_column` | `hv_20`, `hv_60` | 2 |
| RV 回看长度 | `volatility.rv_distribution_lookback_days` | `252`, `504` | 2 |
| RV 最小样本 | `volatility.rv_distribution_min_observations` | `120` | 1 |
| RV 分位数 | `volatility.rv_distribution_quantile` | `0.45`, `0.50`, `0.55` | 3 |
| 最大 IV/RV | `strategy.entry_max_iv_rv_ratio` | `0.80`, `0.85`, `0.90` | 3 |
| 最小 RV-IV edge | `strategy.min_hv_iv_edge` | `0.00`, `0.01`, `0.02` | 3 |
| 是否 edge 填补退出 | `strategy.exit_on_vol_edge_filled` | `false`, `true` | 2 |

条件化后估算：

```text
current_hv:
2 hv * 3 ratio * 3 edge * 2 exit = 36

rolling_quantile + max_current_and_quantile:
2 modes * 2 hv * 2 lookback * 1 min_obs * 3 quantile * 3 ratio * 3 edge * 2 exit = 432

合计 468
```

推荐远程命令：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.fine.json \
  --stage vol_entry_screen \
  --study-id vol_entry_screen_2018_2025
```

## Stage 1B: vol_entry_fine

目标：细化“什么时候买入 straddle”。固定仓位和退出口径，重点扫描 RV 参考、entry ratio 和 edge。该 stage 组合数较大，应在 `vol_entry_screen` 找到有效区域后再局部加密；不建议作为第一步直接全量运行。

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| 启用波动率过滤 | `strategy.use_vol_filter` | `true` | 1 |
| 最大持仓数 | `strategy.max_open_positions` | `1` | 1 |
| 权利金预算 | `strategy.premium_budget_pct` | `0.05` | 1 |
| Delta 对冲阈值 | `strategy.delta_threshold_pct` | `0.005` | 1 |
| HV 窗口集合 | `volatility.hv_windows` | `[10,20,60]` | 1 |
| RV 参考模式 | `volatility.rv_reference_mode` | `current_hv`, `rolling_quantile`, `max_current_and_quantile` | 3 |
| RV 使用 HV 列 | `volatility.rv_reference_hv_column` | `hv_10`, `hv_20`, `hv_60` | 3 |
| RV 回看长度 | `volatility.rv_distribution_lookback_days` | `126`, `252`, `504` | 3 |
| RV 最小样本 | `volatility.rv_distribution_min_observations` | `60`, `120` | 2 |
| RV 分位数 | `volatility.rv_distribution_quantile` | `0.40`, `0.50`, `0.60`, `0.70` | 4 |
| 最大 IV/RV | `strategy.entry_max_iv_rv_ratio` | `0.75`, `0.80`, `0.85`, `0.90`, `0.95` | 5 |
| 最小 RV-IV edge | `strategy.min_hv_iv_edge` | `0.00`, `0.01`, `0.02`, `0.04` | 4 |
| 是否 edge 填补退出 | `strategy.exit_on_vol_edge_filled` | `false`, `true` | 2 |
| 退出 edge | `strategy.exit_max_rv_iv_edge` | `0.00` | 1 |
| 退出 IV/RV | `strategy.exit_min_iv_rv_ratio` | `1.00` | 1 |
| 退出 IV 口径 | `strategy.exit_iv_reference_mode` | `position_average_iv` | 1 |

条件化后估算：

```text
current_hv:
3 hv * 5 ratio * 4 edge * 2 exit = 120

rolling_quantile + max_current_and_quantile:
2 modes * 3 hv * 3 lookback * 2 min_obs * 4 quantile * 5 ratio * 4 edge * 2 exit = 5,760

合计 5,880
```

推荐远程命令：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.fine.json \
  --stage vol_entry_fine \
  --study-id vol_entry_fine_2018_2025
```

## Stage 1C: vol_entry_refine_best

目标：围绕 `vol_entry_screen_2018_2025_v3` 的最优区域继续加密。该阶段固定仓位和退出逻辑，只在 RV 参考和开仓阈值附近做局部搜索。

当前基准最优参数：

```text
volatility.rv_reference_mode = max_current_and_quantile
volatility.rv_reference_hv_column = hv_20
volatility.rv_distribution_lookback_days = 504
volatility.rv_distribution_quantile = 0.55
strategy.entry_max_iv_rv_ratio = 0.85
strategy.min_hv_iv_edge = 0.00 / 0.01 / 0.02
strategy.exit_on_vol_edge_filled = true
```

局部加密范围：

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| RV 参考模式 | `volatility.rv_reference_mode` | `rolling_quantile`, `max_current_and_quantile` | 2 |
| RV 使用 HV 列 | `volatility.rv_reference_hv_column` | `hv_20` | 1 |
| RV 回看长度 | `volatility.rv_distribution_lookback_days` | `378`, `504`, `630` | 3 |
| RV 最小样本 | `volatility.rv_distribution_min_observations` | `120` | 1 |
| RV 分位数 | `volatility.rv_distribution_quantile` | `0.50`, `0.525`, `0.55`, `0.575`, `0.60` | 5 |
| 最大 IV/RV | `strategy.entry_max_iv_rv_ratio` | `0.825`, `0.85`, `0.875` | 3 |
| 最小 RV-IV edge | `strategy.min_hv_iv_edge` | `0.00`, `0.005`, `0.01`, `0.02` | 4 |
| 是否 edge 填补退出 | `strategy.exit_on_vol_edge_filled` | `true` | 1 |

估算：

```text
2 * 3 * 5 * 3 * 4 = 360
```

推荐远程命令：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.fine.json \
  --stage vol_entry_refine_best \
  --study-id vol_entry_refine_best_2018_2025 \
  --workers 32
```

## Stage 2: vol_exit_fine

目标：在较优开仓条件附近，细化“何时平仓”。默认围绕当前最优区域：`rolling_quantile`, `hv_20`, `quantile=0.5`, `entry_max_iv_rv_ratio` 约 `0.85`。

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| 权利金预算 | `strategy.premium_budget_pct` | `0.05`, `0.10` | 2 |
| Delta 对冲阈值 | `strategy.delta_threshold_pct` | `0.005`, `0.01` | 2 |
| RV 参考模式 | `volatility.rv_reference_mode` | `rolling_quantile` | 1 |
| RV 使用 HV 列 | `volatility.rv_reference_hv_column` | `hv_20` | 1 |
| RV 回看长度 | `volatility.rv_distribution_lookback_days` | `126`, `252`, `504` | 3 |
| RV 分位数 | `volatility.rv_distribution_quantile` | `0.40`, `0.50`, `0.60` | 3 |
| 最大 IV/RV | `strategy.entry_max_iv_rv_ratio` | `0.80`, `0.85`, `0.90` | 3 |
| 最小 RV-IV edge | `strategy.min_hv_iv_edge` | `0.00`, `0.01`, `0.02` | 3 |
| 是否 edge 填补退出 | `strategy.exit_on_vol_edge_filled` | `false`, `true` | 2 |
| 退出 edge | `strategy.exit_max_rv_iv_edge` | `0.00`, `0.005`, `0.01`, `0.02` | 4 |
| 退出 IV/RV | `strategy.exit_min_iv_rv_ratio` | `0.95`, `1.00`, `1.05` | 3 |
| 退出 IV 口径 | `strategy.exit_iv_reference_mode` | `position_average_iv`, `held_position_vega_weighted_iv` | 2 |

条件化后估算：

```text
exit_on_vol_edge_filled=false: 1 个 exit 组合
exit_on_vol_edge_filled=true: 4 * 3 * 2 = 24 个 exit 组合
exit 合计 25

2 budget * 2 hedge * 3 lookback * 3 quantile * 3 ratio * 3 edge * 25 exit = 8,100
```

该 stage 较大，建议先限制：

```bash
OPT_STAGE=vol_exit_fine \
OPT_STUDY_ID=vol_exit_fine_2018_2025 \
OPT_MAX_TRIALS=1000 \
scripts/remote_optimization.sh run
```

当前 `OPT_MAX_TRIALS` 是顺序截断，不是随机抽样；正式随机抽样需要后续扩展优化模块。

## Stage 3: contract_fine

目标：在较优波动率择时条件附近，扫描合约选择和流动性过滤。

| 参数 | 配置路径 | 扫描值 |
| --- | --- | --- |
| DTE 下限 | `strategy.min_ttm_days` | `5`, `7`, `10`, `15` |
| 目标 DTE | `strategy.target_ttm_days` | `10`, `15`, `20`, `30`, `45` |
| DTE 上限 | `strategy.max_ttm_days` | `20`, `30`, `45`, `60` |
| ATM IV DTE 下限 | `atm_iv.min_ttm_days` | `5`, `7`, `10` |
| ATM IV DTE 上限 | `atm_iv.max_ttm_days` | `20`, `30`, `45` |
| ATM IV 聚合 | `atm_iv.aggregation` | `mean`, `median` |
| ATM 是否允许单边 | `atm_iv.allow_single_side` | `false`, `true` |
| 最小期权成交量 | `strategy.min_option_volume` | `1`, `50`, `100`, `300`, `500` |
| 最小持仓量 | `strategy.min_open_interest` | `0`, `500`, `1000`, `3000`, `5000` |
| 最大价差比例 | `strategy.max_spread_pct` | `0.10`, `0.20`, `0.35`, `0.50` |
| 最小期权价格 | `strategy.min_option_price` | `0.0001`, `0.003`, `0.005`, `0.01` |
| 最大持仓天数 | `strategy.max_holding_days` | `null`, `10`, `20`, `30` |

约束：

```text
strategy.min_ttm_days <= strategy.target_ttm_days <= strategy.max_ttm_days
atm_iv.min_ttm_days <= atm_iv.max_ttm_days
```

该 stage 全量会很大，建议先选较小子集：

- 固定 `atm_iv.allow_single_side=false`。
- 固定 `strategy.min_open_interest=0,1000`。
- 固定 `strategy.min_option_volume=1,100,500`。
- 固定较优波动率参数。

## Stage 4: position_hedge_fine

目标：在较优择时和合约参数附近，扫描仓位和对冲强度。

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| 权利金预算 | `strategy.premium_budget_pct` | `0.03`, `0.05`, `0.075`, `0.10`, `0.15` | 5 |
| Delta 对冲阈值 | `strategy.delta_threshold_pct` | `0.0025`, `0.005`, `0.01`, `0.015`, `0.02` | 5 |
| 最大持仓数 | `strategy.max_open_positions` | `1` | 1 |

估算：

```text
5 * 5 = 25
```

## Stage 5: cost_sensitivity

目标：对最终候选做成本和价格口径敏感性验证。

该 stage 不是参数优化，不用于选择“最优策略参数”。交易成本、滑点和价格口径应来自真实交易约束或保守假设；在主扫描中固定。只有当 `vol_entry_fine`、`vol_exit_fine`、`position_hedge_fine` 和必要的 `contract_fine` 得到候选参数后，才用本 stage 检查候选结果是否对执行成本过度敏感。

| 参数 | 配置路径 | 扫描值 | 数量 |
| --- | --- | --- | ---: |
| 价格口径 | `data.price_policy` | `mid_first`, `bid_ask_conservative` | 2 |
| 期权滑点 bps | `execution.option_slippage_bps` | `0`, `2`, `5`, `10` | 4 |
| ETF 滑点 bps | `execution.etf_slippage_bps` | `0`, `1`, `2`, `5` | 4 |
| 期权每张费用 | `execution.option_fee_per_contract` | `0`, `1`, `2` | 3 |
| 期权金额费率 bps | `execution.option_fee_bps` | `0`, `1` | 2 |
| ETF 费率 bps | `execution.etf_fee_bps` | `0`, `1`, `3` | 3 |

估算：

```text
2 * 4 * 4 * 3 * 2 * 3 = 576
```

## 推荐执行顺序

1. `vol_entry_screen`: 用 468 组先找择时区域。
2. `vol_entry_refine_best`: 围绕 screen 中较优区域局部加密。
3. `vol_exit_fine`: 在较优择时区域细化退出。
4. `position_hedge_fine`: 在较优择时/退出参数上调仓位和对冲。
5. `contract_fine`: 只对少数较优择时参数做合约选择扫描。
6. `cost_sensitivity`: 对最终候选做敏感性验证，不参与主榜排名和参数择优。

## 执行性能约定

优化执行默认启用：

- `study.workers=32`
- `study.diagnostics=false`
- `study.save_trial_outputs=false`
- `study.cache_market_calculations=true`
- `study.prewarm_market_cache=true`
- `study.write_results_every=50`

含义：

- 第一轮搜索只计算核心回测和绩效指标，不为每个 trial 导出完整 CSV，也不跑 Greeks 归因和 pricing reconciliation。
- IV/Greeks 市场计算会在父进程预热后通过 `fork` 共享给 worker，减少每个 worker 重复预热。
- `summary.csv` 每 50 个 trial 批量落盘一次，避免 5,000 组以上扫描时出现 O(n²) 写盘开销。
- 对 Top 候选再单独复跑完整诊断，启用 `diagnostics=true` 和 `save_trial_outputs=true`。

## 当前最优参数附近的细化建议

上一轮 `remote_vol_timing` 中，年化收益率和夏普比率最优为：

```text
strategy.premium_budget_pct = 0.10
strategy.delta_threshold_pct = 0.005
strategy.entry_max_iv_rv_ratio = 0.85
strategy.min_hv_iv_edge = 0.00 或 0.02
strategy.exit_on_vol_edge_filled = true
strategy.exit_max_rv_iv_edge = 0.00
strategy.exit_min_iv_rv_ratio = 1.00
volatility.rv_reference_mode = rolling_quantile
volatility.rv_reference_hv_column = hv_20
volatility.rv_distribution_quantile = 0.50
```

因此优先加密：

- `entry_max_iv_rv_ratio`: `0.80`, `0.825`, `0.85`, `0.875`, `0.90`
- `rv_distribution_quantile`: `0.40`, `0.45`, `0.50`, `0.55`, `0.60`
- `min_hv_iv_edge`: `0.00`, `0.005`, `0.01`, `0.02`, `0.03`
- `premium_budget_pct`: `0.05`, `0.075`, `0.10`, `0.125`, `0.15`
- `delta_threshold_pct`: `0.0025`, `0.005`, `0.0075`, `0.01`, `0.015`
