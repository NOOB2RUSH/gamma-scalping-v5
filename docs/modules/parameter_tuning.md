# 参数调优模块设计

## 职责

参数调优模块负责批量生成参数组合、运行回测、汇总结果、筛选稳健参数，并支持训练/验证切分。它不复制策略逻辑。

## 参数空间

第一版建议调优参数：

- `min_ttm_days`：最小剩余交易日数。
- `max_ttm_days`：最大剩余交易日数。
- `roll_min_ttm_days`：触发滚仓的最小剩余交易日数。
- `delta_threshold`
- `premium_budget_pct`
- `max_spread_pct`
- `min_option_volume`
- `min_open_interest`
- `max_entry_iv_hv_spread`
- `hedge_frequency`

## 搜索方式

第一版：

- 网格搜索。
- 按参数组合顺序运行。
- 将每次结果落盘，支持断点续跑。

后续：

- 随机搜索。
- Bayesian optimization。
- Walk-forward optimization。
- 多进程并行。

## 目标函数

默认优化目标不应只看收益。建议支持：

```text
score = annual_return - lambda_drawdown * max_drawdown - lambda_turnover * turnover
```

`annual_return`、`turnover` 等年化指标必须使用统一交易日历，默认按 `252` 个交易日年化。

也可以使用：

- Sharpe ratio
- Calmar ratio
- 年化收益 / 最大回撤
- 样本外收益稳定性
- 成本后收益
- IV/HV 差值捕获率，需同时约束有效样本数量。

## 稳健性约束

参数组合必须满足：

- 总交易次数大于最小阈值。
- 最大回撤低于阈值。
- 年化换手低于阈值。
- IV/HV 捕获率有效样本数量大于阈值。
- 相邻参数区域表现不过度跳变。
- 训练集和验证集方向一致。

## 接口草案

```python
class ParameterTuner:
    def generate_grid(self, search_space: dict[str, list]) -> list[StrategyConfig]: ...
    def run_one(self, config: StrategyConfig) -> OptimizationTrial: ...
    def run_all(self, search_space: dict[str, list]) -> OptimizationResult: ...
    def select_best(self, result: OptimizationResult, constraints: TuningConstraints) -> list[OptimizationTrial]: ...
```

## 结果存储

建议每个 trial 保存：

- 参数配置 JSON。
- 回测指标 JSON。
- 净值曲线 parquet。
- 交易流水 parquet。
- 错误日志。

汇总文件：

- `optimization_summary.csv`
- `best_configs.json`
- `tuning_report.html`

## 防止过拟合

- 日期切分：如 2018-2023 训练，2024-2025 验证，2026 留作最终观察，具体以数据可用范围为准。
- 不允许用验证集反复手工挑参数后再声明为样本外。
- 输出训练集和验证集的指标对比。
- 对最优参数周围做局部敏感性分析。

## 测试重点

- 参数网格组合数量。
- trial 失败后不中断整体任务。
- 断点续跑不会重复已完成 trial。
- 约束筛选逻辑。
- 结果排序与目标函数。
