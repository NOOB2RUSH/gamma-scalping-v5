# Gamma Scalping Quant System Design

## 1. 项目目标

本项目用于构建基于 ETF 期权的 gamma scalping 量化交易研究系统。系统第一阶段以离线日频回测为主，基于现有 `data/etf/` 与 `data/opt/` parquet 数据，完成从数据加载、Greeks 计算、波动率估计、策略生成、回测撮合、绩效分析到参数调优的完整闭环。

系统暂不直接接入实盘交易。所有模块需要保持可替换、可测试、可复现，后续可以扩展到分钟级数据、多标的、多定价模型和实盘风控。

## 2. 当前数据基础

现有数据目录约定：

- `data/etf/{underlying}_{date}_price.parquet`：标的 ETF 日频行情。
- `data/opt/{underlying}_{date}_chain.parquet`：对应交易日期的期权链快照。

抽样字段：

- ETF：`date` 索引，字段包含 `open`、`close`、`high`、`low`、`volume`、`money`。
- 期权链：`order_book_id`、`strike_price`、`maturity_date`、`option_type`、`bid`、`ask`、`volume`、`open_interest`、`contract_multiplier`、`close`。

第一版默认交易标的为 `510050.XSHG`，频率为日频，期权价格优先使用 `mid=(bid+ask)/2`，当 bid/ask 不可用时回退到 `close`。

## 3. 总体架构

数据流：

```text
Data Loader
  -> Volatility Engine
  -> Greeks Engine
  -> Strategy Logic
  -> Backtest Engine
  -> Greeks PnL Attribution
  -> Performance & Visualization
  -> Parameter Tuning
```

模块职责：

| 模块 | 主要职责 | 输出 |
| --- | --- | --- |
| 数据加载 | 发现文件、读取 parquet、清洗和对齐 ETF/期权数据 | `MarketSnapshot`、`OptionChain` |
| Greeks 计算 | 根据定价模型计算 delta/gamma/vega/theta/rho 与合约价值 | `OptionGreeks`、组合 Greeks |
| 波动率计算 | 计算历史波动率、隐含波动率、期限结构与波动率特征 | `VolSurfaceSnapshot`、`VolSignal` |
| 策略逻辑 | 选择期权组合、计算 delta 对冲、生成调仓信号 | `TargetPortfolio`、`OrderIntent` |
| 回测引擎 | 事件循环、成交、费用、保证金、持仓、PnL 归因 | `BacktestResult` |
| Greeks 收益归因验证 | 将账户实际 PnL 拆解为 Delta/Gamma/Theta/Vega/对冲/成本/残差，并输出市场价、模型价、Greeks 近似之间的价格链路诊断 | `AttributionResult`、`PricingReconciliationResult`、归因 CSV |
| 绩效分析与可视化 | 统计收益风险指标、绘图、导出报告 | `PerformanceReport` |
| 参数调优 | 批量运行参数网格、约束筛选、稳健性评估 | `OptimizationResult` |

推荐第三方依赖：

- `py_vollib_vectorized`：作为 `q=0` 场景下 Greeks 计算和隐含波动率求解的默认实现，降低自研 Black-Scholes 数值错误风险，并支持期权链批量计算。
- 自研 Black-Scholes/Brent 求解仅作为测试基准或依赖不可用时的备选实现。
- 交易日历组件：独立封装中国大陆交易所交易日历，自动跳过周末和中国大陆法定节假日，并与本地行情文件可用日期做交集校验。

## 4. 核心策略定义

Gamma scalping 的研究假设：

- 通过买入具有正 gamma 的期权组合获得凸性敞口。
- 使用 ETF 进行 delta 对冲，将方向性风险控制在目标阈值内。
- 策略收益主要来自标的实际波动与期权隐含波动、交易成本、theta 损耗之间的差额。

第一版策略范围：

- 期权腿：默认买入近月或次近月 ATM 跨式组合，也支持单腿 call/put 或 strangle。
- 对冲腿：使用 ETF 做 delta hedge。
- 调仓条件：按固定周期、delta 偏离阈值、到期剩余天数、流动性过滤共同触发。
- 风控：最大权利金占用、最大换手、最小成交量/持仓、最小到期天数、最大 bid-ask spread。

## 5. 关键工程约束

- 不在策略逻辑中直接读取文件；所有行情访问经数据加载模块完成。
- 所有到期时间、持仓天数、滚仓阈值、年化收益和波动率年化均使用统一交易日历；禁止在模块内部临时使用自然日 `/365` 口径。
- Greeks 和波动率模块只做计算，不持有账户状态。
- Greeks 和隐含波动率优先通过 `py_vollib_vectorized` 完成，业务代码只封装输入输出、单位转换和质量标记。
- 当 `dividend_rate != 0` 时，Greeks 计算不得调用 `py_vollib_vectorized` 的 Black-Scholes-Merton 后端，必须切换到本地 Black-Scholes-Merton fallback，直到第三方库 BSM rho 口径通过验证。
- 回测引擎负责账户、成交、费用和持仓状态，不内嵌具体策略规则。
- Greeks 收益归因验证模块只消费回测、Greeks、IV 和标的行情的标准输出，不重新运行回测，也不在可视化层重算归因。
- Greeks 归因残差不得默认归咎于高阶 Greeks。当前诊断口径要求优先拆分 `market_model_basis_pnl` 和 `taylor_residual_pnl`，用来区分市场盯市价与 BSM 模型价差异、日频 previous-close 泰勒近似误差和真正的未解释高阶项。
- 策略模块只输出目标组合或订单意图，不直接修改回测账户。
- IV/HV 捕获率依赖显式 episode 生命周期。`episode_id` 必须从策略订单、成交、持仓、归因到绩效模块全链路透传；绩效模块不得从持仓断点反推开平仓周期。
- 参数调优通过统一配置驱动回测，不复制策略代码。
- 所有回测结果必须保存输入参数、数据范围、版本信息和关键假设。

## 6. 统一数据结构

建议使用 dataclass 或 pydantic 定义核心对象：

```python
MarketSnapshot(
    trading_date,
    underlying,
    etf_bar,
    option_chain
)

OptionContract(
    order_book_id,
    strike,
    maturity_date,
    option_type,
    multiplier
)

OptionQuote(
    contract,
    bid,
    ask,
    mid,
    close,
    volume,
    open_interest
)

Position(
    instrument_id,
    instrument_type,
    quantity,
    avg_price,
    multiplier
)
```

## 7. 配置分层

配置建议拆分为：

- `data`: 数据目录、标的、日期范围、价格字段、缺失值策略、交易日历来源。
- `model`: 无风险利率、分红率、年化交易日数、Greeks/IV 计算后端、隐含波动率求解参数；无风险利率默认 `0`。
- `strategy`: 组合类型、到期选择、delta 阈值、调仓周期、风险预算。
- `execution`: 成交价格、滑点、手续费、冲击成本、保证金假设。
- `backtest`: 初始资金、账户币种、复权/现金处理、结果输出路径。
- `optimization`: 参数搜索空间、并行度、目标函数、训练/验证切分。

## 8. 里程碑

1. 完成数据加载和字段标准化，能遍历日期并返回 `MarketSnapshot`。
2. 基于 `py_vollib_vectorized` 完成 Black-Scholes Greeks 和隐含波动率求解封装，并通过已知样例测试。
3. 完成交易日历、历史波动率与 IV 特征，生成 ATM IV、IV/HV spread。
4. 实现最小策略：买入 ATM straddle + delta hedge。
5. 实现日频回测循环、成交、费用、持仓估值和 PnL 归因。
6. 实现 Greeks 收益归因验证，输出每日和累计归因表。
7. 实现绩效报告与基础图表。
8. 增加显式 episode 生命周期机制，并基于 episode 实现 gamma scalping 专项指标，包括 IV/HV 差值捕获率。
9. 实现参数网格搜索和样本外评估。

## 9. 详细设计索引

- [用户可用入口说明](user_entrypoints.md)
- [数据加载](modules/data_loading.md)
- [Greeks 计算](modules/greeks.md)
- [波动率计算](modules/volatility.md)
- [策略逻辑](modules/strategy_logic.md)
- [回测引擎](modules/backtest_engine.md)
- [Greeks 收益归因验证](modules/greeks_pnl_attribution.md)
- [绩效分析与可视化](modules/performance_visualization.md)
- [参数调优](modules/parameter_tuning.md)
