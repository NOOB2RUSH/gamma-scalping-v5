# 绩效分析与可视化模块设计

## 职责

绩效模块负责把 `BacktestResult` 转换为可解释的收益风险指标、图表和报告。它不改变回测结果。

## 核心指标

收益类：

- 累计收益率
- 年化收益率
- 日收益率序列
- 月度收益率

风险类：

- 年化波动率
- 最大回撤
- Sharpe ratio
- Sortino ratio
- Calmar ratio
- VaR / CVaR，可选

交易类：

- 总成交额
- 换手率
- 手续费与滑点占比
- 胜率
- 平均持仓天数
- 调仓次数

期权策略专项：

- 平均 delta/gamma/vega/theta 敞口
- 对冲后 delta 分布
- gamma scalping PnL 与 option carry PnL
- IV/HV spread 分组收益
- IV/HV 差值捕获率
- IV/HV 信号命中率
- 到期月份收益分布

## Gamma Scalping 专项指标

IV/HV 差值捕获率有必要加入，因为 gamma scalping 的核心问题不是单纯收益率，而是策略是否把波动率价差转化为对冲后的净 PnL。

建议口径：

```text
iv_hv_capture_rate = net_gamma_scalping_pnl / theoretical_vol_edge_pnl
```

其中：

- `net_gamma_scalping_pnl`：期权腿和 ETF 对冲腿合并后的持有期净 PnL，扣除手续费和滑点。
- `theoretical_vol_edge_pnl`：基于持有期逐日 gamma、标的价格、实际方差和隐含方差估计的理论波动率价差收益。
- `realized_vol_holding`：持有期交易日收益率年化波动率。
- `entry_atm_iv`：入场时所选期限的 ATM IV。

同时输出更稳健的辅助指标：

- `iv_hv_signal_hit_rate`: `sign(hv_forecast - entry_atm_iv) == sign(realized_vol_holding - entry_atm_iv)` 的交易占比。
- `capture_rate_median`: 捕获率中位数，降低极端分母影响。
- `capture_rate_valid_count`: 分母足够大且方向可解释的样本数量。

## 图表

第一版图表：

- 净值曲线
- 回撤曲线
- 日收益分布
- 月度收益热力图
- 持仓 Greeks 时间序列
- 波动率时间序列：`atm_iv`、`hv_10`、`hv_20`、`hv_60`、`iv_hv_spread`、`hv_iv_edge`
- ATM IV 聚合样本数和 IV 求解失败数，用于检查数据质量
- Greeks PnL 归因图：Delta/Gamma/Theta/Vega/对冲/成本/残差的每日堆叠柱状图和累计曲线
- Greeks 累计贡献曲线：读取 `greeks_attribution_cumulative.csv` 中的 `cum_delta_pnl`、`cum_gamma_pnl`、`cum_theta_pnl`、`cum_vega_pnl`
- 期权和 ETF 分腿 PnL
- 交易成本时间序列
- IV/HV 捕获率按交易、月份和参数分组的分布图

可视化模块不重新计算 IV/HV，只消费波动率模块输出的 `VolatilityTimeSeries`，确保图表与策略信号使用同一份数据。

可视化模块不重新计算 Greeks PnL 归因，只消费验证模块输出的 `greeks_attribution.csv`、`greeks_attribution_cumulative.csv` 和 `attribution_quality.csv`。其中 `greeks_attribution_cumulative.csv` 是绘制 Greeks 累计贡献曲线的唯一数据源。

输出格式：

- 静态图片：`png`
- 表格：`csv` 或 `parquet`
- 报告：`html`

## 接口草案

```python
class PerformanceAnalyzer:
    def compute_metrics(self, result: BacktestResult) -> PerformanceMetrics: ...
    def build_report(self, result: BacktestResult, output_dir: Path) -> PerformanceReport: ...

class Visualizer:
    def plot_equity_curve(self, result: BacktestResult) -> Figure: ...
    def plot_drawdown(self, result: BacktestResult) -> Figure: ...
    def plot_greeks(self, result: BacktestResult) -> Figure: ...
    def plot_volatility_series(self, volatility: VolatilityTimeSeries) -> Figure: ...
    def plot_greeks_attribution(self, attribution: AttributionResult) -> Figure: ...
```

当前实现位置：

- `gamma_scalping.performance.PerformanceAnalyzer`
- `gamma_scalping.performance.PerformanceMetrics`
- `gamma_scalping.performance.PerformanceReport`
- `gamma_scalping.performance.Visualizer`

第一版实现边界：

- `PerformanceAnalyzer.compute_metrics()` 消费 `BacktestResult` 或等价对象的 `equity_curve`、`trade_records`，可选消费 `AttributionResult` 和 `VolatilityTimeSeries`。
- 年化指标默认使用 `252` 个交易日，支持通过 `PerformanceConfig.annual_trading_days` 配置。
- 已实现核心收益风险指标：累计收益率、年化收益率、年化波动率、最大回撤、Sharpe、Sortino、Calmar、VaR、CVaR。
- 已实现交易摘要：总成交额、总手续费、交易笔数、发生交易的 bar 数。
- 已实现归因摘要：平均 Delta/Gamma/Theta/Vega 敞口、累计 `gamma_theta_pnl`、累计 `vega_pnl`、平均残差比例。
- 已实现波动率摘要：平均 `atm_iv`、`hv_20`、`iv_hv_spread`、`hv_iv_edge` 和 IV 失败数。
- `Visualizer` 只读取传入的结果表绘图，不重新计算 IV/HV 或 Greeks 归因。
- matplotlib 使用非交互 `Agg` 后端，缓存目录设置到 `/tmp/matplotlib`，适合批量回测和无 GUI 环境。

`build_report()` 输出目录：

```text
report_dir/
├── performance_metrics.csv
├── daily_returns.csv
├── monthly_returns.csv
├── performance_report.html
├── equity_curve.png
├── drawdown.png
├── volatility_series.png                  # 传入 VolatilityTimeSeries 时输出
├── greeks_attribution_daily.png           # 传入 AttributionResult.daily 时输出
└── greeks_attribution_cumulative.png      # 传入 AttributionResult.cumulative 时输出
```

其中 `greeks_attribution_cumulative.png` 直接绘制 `AttributionResult.cumulative` / `greeks_attribution_cumulative.csv` 的累计列，不从持仓、Greeks、IV 表重算。

## 报告内容

建议 HTML 报告包含：

1. 回测配置摘要。
2. 数据范围与标的。
3. 核心绩效指标表。
4. 交易成本与换手摘要。
5. 净值、回撤和收益分布。
6. Greeks 暴露与对冲质量。
7. ATM IV、HV 与 IV/HV spread 曲线。
8. ATM IV 聚合合约样本数与 IV 求解质量。
9. Greeks PnL 归因和残差质量图。
10. 参数与版本信息。

## 测试重点

- 收益率与年化指标口径。
- 年化指标统一使用交易日历，默认 `252` 个交易日。
- 最大回撤计算。
- IV/HV 捕获率在分母接近 0、负理论边际和无持仓样本下的处理。
- 波动率曲线使用 `VolatilityTimeSeries`，不在可视化层重新计算。
- Greeks PnL 归因图使用归因模块输出，不在可视化层重新计算。
- 空交易但有净值曲线的情况。
- 图表输出路径与文件存在性。
- 指标中的 NaN/inf 清理。
