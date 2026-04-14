# Greeks 收益归因验证模块设计

## 职责

Greeks 收益归因验证模块用于解释回测账户损益来源，将每日实际 PnL 拆分为 Delta、Gamma、Theta、Vega、交易成本和残差。该模块定位为验证与诊断模块，不负责生成交易信号，不参与订单执行，也不在可视化层重算。

模块输入来自已有标准输出：

- 回测引擎：`equity_curve`、`trade_records`、`position_records`、`fills`。
- Greeks 模块：逐日持仓合约的 `delta`、`gamma`、`theta`、`vega`。
- 波动率模块：逐合约 `iv` 和 `iv_status`。注意不是只使用市场 ATM IV。
- 数据加载模块：标的 ETF 收盘价时间序列。

模块输出：

- `greeks_attribution.csv`: 每日 Greeks PnL 归因表。
- `greeks_attribution_cumulative.csv`: 累计归因表。
- `attribution_quality.csv`: 残差比例、缺失数据、异常 IV 等质量诊断表。

可视化模块只消费上述输出画图，不在图表层重新计算归因。

## 理论判断

用户提供的理论框架合理：用期权价值对标的价格、时间和隐含波动率的泰勒展开二阶近似解释期权组合 PnL：

```text
dV ~= delta * dS + 0.5 * gamma * (dS)^2 + theta * dt + vega * d_sigma + residual
```

需要在实现中明确以下修正：

- `vega` 符号不使用 `V`，避免和组合价值 `V` 混淆。
- `dS` 是标的价格的绝对变化，不是收益率；若使用收益率，必须乘以 `S_{t-1}^2` 还原价格平方。
- `theta` 口径必须和 Greeks 模块一致。当前 Greeks 模块输出日 theta，因此 `dt=1` 个交易日。
- `vega` 口径必须和 Greeks 模块一致。当前 Greeks 模块输出波动率变动 `1.00` 的价格敏感度，因此 `d_sigma` 使用 IV 小数变化，如 `0.22 - 0.20 = 0.02`。
- Vega 归因必须使用持仓合约自身 IV 的变化，按持仓 Vega 权重聚合，不能用 ATM IV 或指数 IV 替代。
- 交易成本、滑点和对冲 ETF PnL 不应塞进残差，需要单独列示。
- 残差用于诊断模型误差和路径依赖，不要求每天趋近于 0。第一版可用 `abs(residual) / max(abs(actual_pnl), eps)` 监控，默认警戒阈值 `10%`，但不作为硬性失败条件。

## 解耦原则

- 归因模块不调用策略模块。
- 归因模块不调用回测引擎重新运行回测。
- 归因模块不调用可视化模块。
- 回测引擎只负责产出标准交易和持仓表；归因模块消费这些表并输出归因表。
- 可视化模块只读取归因结果画图。

推荐接口：

```python
class GreeksPnLAttribution:
    def attribute_daily(
        self,
        equity_curve: DataFrame,
        trade_records: DataFrame,
        position_records: DataFrame,
        greeks_history: DataFrame,
        iv_history: DataFrame,
        underlying_history: DataFrame,
    ) -> AttributionResult: ...

    def export_csv(self, result: AttributionResult, output_dir: Path) -> dict[str, Path]: ...
```

当前实现位置：

- `gamma_scalping.attribution.GreeksPnLAttribution`
- `gamma_scalping.attribution.AttributionResult`
- `gamma_scalping.attribution.AttributionConfig`

第一版实现边界：

- `exposure_mode` 仅支持 `previous_close`，即使用上一交易日收盘后持仓和上一交易日 Greeks。
- 日频归因不加入主观 Gamma 路径依赖折减，路径效应进入残差。
- Vega 使用持仓合约自身的 `contract_id` 级别 IV 变化；缺失 IV 时该合约 Vega PnL 置 0，并在质量表记录 `missing_iv`。
- `weighted_position_iv` 和 `weighted_position_iv_change` 使用持仓 Vega 绝对值加权，便于后续诊断持仓实际 IV 变化口径。

## 输入数据契约

### underlying_history

必须包含：

- `trading_date`
- `underlying`
- `close`

用于计算：

```text
dS = close_t - close_{t-1}
```

### position_records

来自回测引擎，必须包含：

- `trading_date`
- `instrument_id`
- `instrument_type`
- `quantity`
- `avg_price`
- `multiplier`
- `liquidation_price`
- `theoretical_unrealized_pnl`
- `role`

只对 `instrument_type == "option"` 的行做 Greeks 归因。ETF 对冲腿单独计算 `hedge_pnl`。

### greeks_history

逐日逐合约 Greeks 表，必须包含：

- `trading_date`
- `contract_id`
- `delta`
- `gamma`
- `theta`
- `vega`
- `multiplier`
- `greeks_status`

要求这些 Greeks 是单合约、未乘持仓数量的值。归因模块内部按：

```text
portfolio_greek = greek_per_contract * quantity * multiplier
```

聚合到组合层。

### iv_history

逐日逐合约 IV 表，必须包含：

- `trading_date`
- `contract_id`
- `iv`
- `iv_status`

Vega 归因不得使用 `atm_iv` 替代 `contract_id` 级别 IV。若某合约当日或前一日 IV 缺失，Vega PnL 记为 `NaN` 或按配置置 0，并在质量表记录。

### trade_records

用于计算交易成本和成交相关 PnL：

- `trading_date`
- `instrument_id`
- `instrument_type`
- `side`
- `quantity`
- `price`
- `trade_amount`
- `fee`
- `reason`

第一版将 `fee` 直接列入 `cost_pnl = -fee`。滑点若已体现在成交价中，暂不单独拆解；后续可由执行模型输出 `slippage` 字段后再分离。

## 每日归因口径

默认使用逐日归因：

```text
actual_pnl_t = equity_t - equity_{t-1}
```

首个回测交易日没有前一日基期，第一版将 `actual_pnl` 和所有归因项置为 `0`，并在 `quality_flags` / `notes` 中记录 `first_row`。这样避免把建仓首日手续费或成交价差归入缺少基期的单日 PnL。

期权 Greeks PnL 使用上一交易日持仓暴露作为基准，避免使用当日收盘后才知道的持仓造成前视：

```text
delta_pnl_t = option_delta_{t-1} * (S_t - S_{t-1})
gamma_pnl_t = 0.5 * option_gamma_{t-1} * (S_t - S_{t-1})^2
theta_pnl_t = option_theta_{t-1} * 1
vega_pnl_t = sum(position_vega_{i,t-1} * (iv_{i,t} - iv_{i,t-1}))
```

其中：

```text
position_vega_{i,t-1} = vega_{i,t-1} * quantity_{i,t-1} * multiplier_i
```

对冲腿 PnL：

```text
hedge_pnl_t = etf_quantity_{t-1} * (S_t - S_{t-1})
```

交易成本：

```text
cost_pnl_t = - sum(fee_t)
```

理论合计：

```text
explained_pnl_t = delta_pnl_t + gamma_pnl_t + theta_pnl_t + vega_pnl_t + hedge_pnl_t + cost_pnl_t
residual_pnl_t = actual_pnl_t - explained_pnl_t
```

Gamma scalping 核心净额：

```text
gamma_theta_pnl_t = gamma_pnl_t + theta_pnl_t
```

如果 `gamma_theta_pnl_t < 0` 但总 PnL 为正，说明盈利更可能来自 Vega 或对冲路径，而非 gamma scalping 核心 alpha。

## 平均 Greeks 口径

第一版默认使用 `t-1` 暴露。后续可配置为平均暴露：

```text
avg_delta = 0.5 * (delta_{t-1} + delta_t)
avg_gamma = 0.5 * (gamma_{t-1} + gamma_t)
avg_theta = 0.5 * (theta_{t-1} + theta_t)
avg_vega = 0.5 * (vega_{t-1} + vega_t)
```

平均暴露适合事后解释，但不适合实时监控，因为它使用当日收盘后的 Greeks。设计上必须在输出中记录 `exposure_mode`。

## 日频路径依赖

日频数据无法观察盘中路径，Gamma PnL 只使用收盘到收盘的 `dS` 近似。该口径可能低估或高估实际离散对冲收益，残差会吸收：

- 盘中先涨后跌或先跌后涨的路径效应。
- 触发阈值对冲导致的路径依赖。
- 高阶 Greeks，如 charm、vanna、volga。
- IV 曲面形变和合约离散选择误差。

第一版不加入经验性的 `5%-15%` Gamma 调整项，因为该调整依赖市场和对冲频率，容易引入主观偏差。可以在质量报告中输出 `path_dependency_note`。

## 输出表

`greeks_attribution.csv` 字段：

- `trading_date`
- `underlying`
- `actual_pnl`
- `delta_pnl`
- `gamma_pnl`
- `theta_pnl`
- `vega_pnl`
- `hedge_pnl`
- `cost_pnl`
- `explained_pnl`
- `residual_pnl`
- `residual_ratio`
- `gamma_theta_pnl`
- `option_delta_exposure`
- `option_gamma_exposure`
- `option_theta_exposure`
- `option_vega_exposure`
- `weighted_position_iv`
- `weighted_position_iv_change`
- `exposure_mode`
- `quality_flags`

`greeks_attribution_cumulative.csv` 字段：

- `trading_date`
- `cum_actual_pnl`
- `cum_delta_pnl`
- `cum_gamma_pnl`
- `cum_theta_pnl`
- `cum_vega_pnl`
- `cum_hedge_pnl`
- `cum_cost_pnl`
- `cum_explained_pnl`
- `cum_residual_pnl`
- `cum_gamma_theta_pnl`

该表是可视化 Greeks 累计贡献曲线的唯一数据源。后续图表应直接绘制：

- `cum_delta_pnl`
- `cum_gamma_pnl`
- `cum_theta_pnl`
- `cum_vega_pnl`

可选叠加：

- `cum_actual_pnl`
- `cum_explained_pnl`
- `cum_residual_pnl`
- `cum_gamma_theta_pnl`

可视化层不得根据持仓、Greeks 和 IV 重新计算累计归因。

`attribution_quality.csv` 字段：

- `trading_date`
- `residual_ratio`
- `residual_warning`
- `missing_greeks_count`
- `missing_iv_count`
- `failed_iv_count`
- `option_position_count`
- `notes`

## 与可视化模块的关系

可视化模块只读取归因模块输出：

- 绘制 Delta/Gamma/Theta/Vega/成本/残差的堆叠柱状图。
- 绘制累计归因曲线，尤其是 `cum_delta_pnl`、`cum_gamma_pnl`、`cum_theta_pnl`、`cum_vega_pnl` 在整个回测过程中的变化。
- 绘制 `gamma_theta_pnl` 与总 PnL 对比。
- 绘制残差比例和质量告警。

可视化模块不得自行读取持仓、Greeks 和 IV 表重新计算归因。

## 测试重点

- `dS` 使用价格绝对变化，不使用收益率。
- Vega 使用持仓合约自身 IV 变化，不使用 ATM IV。
- 日 theta 口径下 `dt=1`。
- ETF 对冲 PnL 单独列示，不混入 Delta PnL。
- 费用单独列示为 `cost_pnl`。
- 残差计算与实际净值变化一致。
- 缺失 IV/Greeks 时质量表有记录。
- 累计归因等于每日归因累加。
