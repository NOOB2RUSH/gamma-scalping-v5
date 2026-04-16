# 参数候选简报

数据区间：`2018-01-01` 至 `2025-12-31`

来源：

- 第一轮筛选：`vol_entry_screen_2018_2025_v3`
- 局部加密：`vol_entry_refine_best_2018_2025`

本报告固化两个候选参数组合：

- `return_focus_trial_000309`：收益优先候选。
- `sharpe_focus_trial_000025`：风险调整收益优先候选。

参数已保存至：

```text
config/optimized_candidates.json
```

## 结果对比

| 候选 | 定位 | 年化收益 | 夏普 | Sortino | 最大回撤 | 总 PnL | Episode 数 | 换手 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `return_focus_trial_000309` | 收益优先 | 3.63% | 0.5810 | 1.8394 | -6.37% | 293,038.81 | 69 | 212.36 |
| `sharpe_focus_trial_000025` | 风险调整收益优先 | 2.40% | 0.6622 | 2.2477 | -3.91% | 186,648.04 | 26 | 98.44 |

## 收益优先候选

候选 ID：`return_focus_trial_000309`

适用定位：

- 追求更高绝对收益。
- 可接受较高换手和更多交易次数。
- 适合作为后续仓位扫描的主候选。

核心参数：

```text
strategy.premium_budget_pct = 0.05
strategy.delta_threshold_pct = 0.005
strategy.entry_max_iv_rv_ratio = 0.875
strategy.min_hv_iv_edge = 0.0
strategy.exit_on_vol_edge_filled = true
strategy.exit_max_rv_iv_edge = 0.0
strategy.exit_min_iv_rv_ratio = 1.0
strategy.exit_iv_reference_mode = position_average_iv

volatility.rv_reference_mode = max_current_and_quantile
volatility.rv_reference_hv_column = hv_20
volatility.rv_distribution_lookback_days = 630
volatility.rv_distribution_min_observations = 120
volatility.rv_distribution_quantile = 0.50
```

## 夏普优先候选

候选 ID：`sharpe_focus_trial_000025`

适用定位：

- 更重视回撤控制和风险调整收益。
- 交易次数更少，换手明显低于收益优先候选。
- 适合作为稳健参数候选和成本敏感性验证基准。

核心参数：

```text
strategy.premium_budget_pct = 0.05
strategy.delta_threshold_pct = 0.005
strategy.entry_max_iv_rv_ratio = 0.825
strategy.min_hv_iv_edge = 0.0
strategy.exit_on_vol_edge_filled = true
strategy.exit_max_rv_iv_edge = 0.0
strategy.exit_min_iv_rv_ratio = 1.0
strategy.exit_iv_reference_mode = position_average_iv

volatility.rv_reference_mode = rolling_quantile
volatility.rv_reference_hv_column = hv_20
volatility.rv_distribution_lookback_days = 378
volatility.rv_distribution_min_observations = 120
volatility.rv_distribution_quantile = 0.55
```

## 观察

- 两个候选都使用 `hv_20`，说明当前阶段不应继续把 `hv_60` 作为主搜索方向。
- 两个候选都要求 `exit_on_vol_edge_filled=true`，说明 IV/RV edge 填补退出是有效约束。
- `min_hv_iv_edge` 在 `0` 附近不敏感，后续可固定为 `0.0`，减少搜索维度。
- 收益优先候选使用 `max_current_and_quantile`，年化收益更高。
- 夏普优先候选使用 `rolling_quantile`，回撤和风险调整收益更好。

## 待验证

当前优化结果来自快扫模式，未对每个 trial 运行完整 Greeks 归因和 pricing reconciliation。后续必须对两个候选分别跑完整报告，重点检查：

- `gamma_theta_pnl` 是否为正。
- `vega_pnl` 是否是主要收益来源。
- `residual_pnl` 和 `market_model_basis_pnl` 是否可控。
- 成本敏感性下收益是否仍稳定。

若 `gamma_theta_pnl` 持续为负，而账户盈利主要来自 `vega_pnl`，该组合更应被解释为“低估 IV 捕获策略”，不能直接作为纯 Gamma Scalping Alpha 结论。

## 建议下一步

1. 分别对两个候选跑完整回测报告。
2. 以两个候选为基准扫描 `premium_budget_pct` 和 `delta_threshold_pct`。
3. 做固定交易成本场景下的敏感性验证，不把交易成本作为优化参数。
