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
- `greeks_attribution_by_episode.csv`: 按 `episode_id` 拆分的每日 Greeks PnL 归因表。
- `greeks_attribution_cumulative.csv`: 累计归因表。
- `attribution_quality.csv`: 残差比例、缺失数据、异常 IV 等质量诊断表。
- `pricing_reconciliation.csv`: 逐持仓价格链路诊断表，用于区分市场盯市 PnL、模型重定价 PnL 和 Greeks 近似误差。
- `pricing_reconciliation_daily.csv`: 上述诊断的日度聚合表。

可视化模块只消费上述输出画图，不在图表层重新计算归因。

## 理论判断

用户提供的理论框架合理：用期权价值对标的价格、时间和隐含波动率的泰勒展开二阶近似解释期权组合 PnL：

```text
dV ~= delta * dS + 0.5 * gamma * (dS)^2 + theta * dt + vega * d_sigma + residual
```

需要在实现中明确以下修正：

- `vega` 符号不使用 `V`，避免和组合价值 `V` 混淆。
- `dS` 是标的价格的绝对变化，不是收益率；若使用收益率，必须乘以 `S_{t-1}^2` 还原价格平方。
- `theta` 口径必须和 Greeks 模块一致。当前 Greeks 模块输出日 theta；逐日归因区间使用相邻回测交易日之间的自然日间隔 `calendar_gap = date_t - date_{t-1}`，因此周末和节假日会体现为多天 theta。
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
- 归因模块按回测输出的显式 `episode_id` 分组，不反推开平仓生命周期。

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
- `gamma_scalping.attribution.PricingReconciliation`
- `gamma_scalping.attribution.PricingReconciliationResult`
- `gamma_scalping.attribution.PricingReconciliationConfig`

第一版实现边界：

- `exposure_mode` 仅支持 `previous_close`，即使用上一交易日收盘后持仓和上一交易日 Greeks。
- `previous_close` 是日频泰勒近似，不是连续积分；高 Gamma、临近到期、大波动和平仓日的路径误差进入残差。
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
- `episode_id`

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

价格链路诊断若需要做模型重定价拆分，还需要：

- `option_type`
- `strike`
- `ttm_trading_days`
- `theoretical_price`
- `iv`

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
- `episode_id`

第一版将 `fee` 直接列入 `cost_pnl = -fee`。滑点若已体现在成交价中，暂不单独拆解；后续可由执行模型输出 `slippage` 字段后再分离。

### episode_records

来自回测引擎，必须包含：

- `episode_id`
- `status`
- `opened_at`
- `closed_at`
- `entry_spot`
- `entry_atm_iv`
- `entry_hv_20`
- `call_contract_id`
- `put_contract_id`

归因模块不使用 `episode_records` 推断持仓，只用它校验 `position_records` / `trade_records` 中的 `episode_id` 是否属于合法 episode，并将 episode 元数据透传给绩效模块。

## 每日归因口径

默认使用逐日归因：

```text
actual_pnl_t = equity_t - equity_{t-1}
```

首个回测交易日没有前一日基期，第一版将 `actual_pnl` 和所有归因项置为 `0`，并在 `quality_flags` / `notes` 中记录 `first_row`。这样避免把建仓首日手续费或成交价差归入缺少基期的单日 PnL。

期权 Greeks PnL 使用上一交易日持仓暴露作为基准，避免使用当日收盘后才知道的持仓造成前视：

```text
calendar_gap_t = date_t - date_{t-1}
delta_pnl_t = option_delta_{t-1} * (S_t - S_{t-1})
gamma_pnl_t = 0.5 * option_gamma_{t-1} * (S_t - S_{t-1})^2
theta_pnl_t = option_theta_{t-1} * calendar_gap_t
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

## 价格链路诊断

`greeks_attribution.csv` 的 `residual_pnl` 是账户实际 PnL 与 previous-close Greeks 泰勒近似之间的差值。该残差可能同时混合以下来源：

- 市场盯市价和 BSM 模型价的价格基差。
- previous-close Greeks 近似与模型直接重定价之间的误差。
- 到期、平仓、缺失 IV/Greeks 和离散对冲路径误差。

根据 2020-2025 日频回测诊断，残差的主要来源不是传统意义上的高阶 Greeks 项。当前更重要的来源是：

- `market_model_basis_pnl`: 市场盯市价与 BSM 模型价之间的差异。
- `taylor_residual_pnl`: previous-close Greeks 泰勒近似和模型直接重定价之间的差异。
- 到期和平仓日的价格口径切换。

高阶项如 charm、vanna、volga 仍可能存在，但在当前系统里不应被默认视为残差主因。若 `model_cross_residual_pnl` 接近 0，而 `market_model_basis_pnl` 或 `taylor_residual_pnl` 较大，应优先排查价格口径、模型重定价口径和日频近似误差。

因此统一回测脚本额外输出 `pricing_reconciliation.csv` 和 `pricing_reconciliation_daily.csv`，将价格链路拆成三层：

```text
mark_pnl
= model_repricing_pnl
  + market_model_basis_pnl

model_repricing_pnl
= model_spot_pnl
  + model_theta_pnl
  + model_vega_pnl
  + model_cross_residual_pnl

model_repricing_pnl
= greeks_explained_pnl
  + taylor_residual_pnl
```

字段含义：

- `mark_pnl`: 使用回测持仓记录中的市场盯市价计算的真实持仓价格变化。
- `model_repricing_pnl`: 使用 BSM 模型价直接从 `t-1` 重定价到 `t` 的理论 PnL。到期日若模型 Greeks 已失效，诊断层允许使用内在价值作为到期模型价。
- `model_spot_pnl`: 固定上一日 IV 和上一日剩余期限，只把标的价格从 `S_{t-1}` 改为 `S_t` 后的模型重定价贡献。该项包含 Delta、Gamma 及更高阶标的价格非线性。
- `model_theta_pnl`: 在标的价格已更新为 `S_t`、IV 固定为上一日 IV 的条件下，把剩余期限从 `T_{t-1}` 改为 `T_t` 的模型重定价贡献。
- `model_vega_pnl`: 在标的价格和剩余期限已更新到当日的条件下，把 IV 从 `iv_{t-1}` 改为 `iv_t` 的模型重定价贡献。
- `model_cross_residual_pnl`: 上述有限差分顺序未解释的模型残差；当前顺序为 spot -> time -> IV，正常应接近 0。
- `market_model_basis_pnl`: 市场盯市 PnL 与 BSM 模型重定价 PnL 的差异，用于识别市场价格、报价回退、到期结算、模型假设不一致带来的差距。
- `taylor_residual_pnl`: BSM 模型直接重定价 PnL 与 previous-close Greeks 泰勒近似之间的差异，用于衡量泰勒近似是否足够解释模型价变化。
- `mark_residual_pnl`: 市场盯市 PnL 与 previous-close Greeks 泰勒近似之间的总差异。

解释策略盈利和亏损时，优先使用 `pricing_reconciliation_daily.csv` 的模型有限差分字段判断模型层来源，再结合 `market_model_basis_pnl` 判断市场价与模型价是否产生明显偏离。`greeks_attribution.csv` 仍作为可视化累计 Greeks 贡献的主表，但不应把其残差简单理解为单一错误来源。

### 当前回测解释样例

以最近一次 2020-2025 全周期回测为例，账户实际收益和 Greeks 分解为：

```text
actual_pnl       = +63,160.90
delta_pnl        = +36,618.93
gamma_pnl        = +884,320.97
theta_pnl        = -848,691.70
vega_pnl         = +94,315.15
hedge_pnl        = -36,559.16
cost_pnl         = 0.00
explained_pnl    = +130,004.21
residual_pnl     = -66,843.40
gamma_theta_pnl  = +35,629.20
```

该结果说明：

- 策略确实获得了显著 Gamma 收益。
- 最大亏损来源是 Theta 时间价值损耗。
- Gamma 与 Theta 抵消后只剩小幅正贡献，策略核心 gamma scalping alpha 并不强。
- Vega 为正贡献，但不是主要收益来源。
- ETF 对冲腿小幅亏损。

价格链路诊断进一步显示：

```text
mark_pnl                 = +48,594.90
model_repricing_pnl      = -96,116.08
market_model_basis_pnl   = +144,710.98
taylor_residual_pnl      = -226,120.38
mark_residual_pnl        = -81,409.40
```

期权腿的模型有限差分分解为：

```text
option model_repricing_pnl = -59,556.90
option model_spot_pnl      = +692,989.67
option model_theta_pnl     = -795,721.79
option model_vega_pnl      = +43,175.12
```

因此，当前策略盈利/亏损的主线应解释为：标的波动和 Gamma 暴露贡献为正，但大部分被 Theta 消耗抵消；最终账户盈利还受市场盯市价相对 BSM 模型价的正向基差影响。高阶 Greeks 不是当前残差的主要解释。

## Episode 归因口径

为支持 IV/HV 差值捕获率，归因模块需要在全账户日度归因之外输出 `greeks_attribution_by_episode.csv`。

按 episode 归因时：

```text
episode_delta_pnl_t = episode_option_delta_{t-1} * (S_t - S_{t-1})
episode_gamma_pnl_t = 0.5 * episode_option_gamma_{t-1} * (S_t - S_{t-1})^2
episode_theta_pnl_t = episode_option_theta_{t-1}
episode_vega_pnl_t = sum(position_vega_{i,t-1} * (iv_{i,t} - iv_{i,t-1})) within episode
episode_hedge_pnl_t = episode_etf_quantity_{t-1} * (S_t - S_{t-1})
episode_cost_pnl_t = - sum(fee_t) within episode
```

要求：

- `episode_id` 来自回测输出，不由归因模块从持仓断点推断。
- 没有 `episode_id` 的持仓或交易可进入全账户归因，但不得进入有效 episode 归因样本。
- 多 episode 并行时，每个 episode 生成独立日度行；全账户归因应等于所有 episode 加未分配行的合计。
- ETF 对冲腿必须通过 `episode_id` 归属到具体 episode，不能把账户级 ETF 净持仓平均分摊。
- `actual_pnl` 和 `residual_pnl` 保留在全账户归因表，不进入 episode 归因表。原因是账户净值变动可严格定义为 `equity_t - equity_{t-1}`，但 episode 级实际 PnL 若要严谨计算，需要逐持仓市值变动与现金流归属账本；第一版 episode 归因只输出可直接由持仓、Greeks、IV、费用精确定义的分量 PnL。
- episode 级校验按分量执行，例如：

```text
account_delta_pnl_t = sum(episode_delta_pnl_t) + unassigned_delta_pnl_t
account_gamma_pnl_t = sum(episode_gamma_pnl_t) + unassigned_gamma_pnl_t
account_theta_pnl_t = sum(episode_theta_pnl_t) + unassigned_theta_pnl_t
account_vega_pnl_t = sum(episode_vega_pnl_t) + unassigned_vega_pnl_t
account_hedge_pnl_t = sum(episode_hedge_pnl_t) + unassigned_hedge_pnl_t
account_cost_pnl_t = sum(episode_cost_pnl_t) + unassigned_cost_pnl_t
```

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

注意：高阶 Greeks 是理论上的残差来源之一，但当前诊断模块已通过 `model_spot_pnl`、`model_theta_pnl`、`model_vega_pnl` 和 `model_cross_residual_pnl` 对模型重定价进行有限差分拆分。若有限差分闭合良好，则说明未解释项主要不来自高阶项，而来自市场价与模型价的基差、日频 previous-close 泰勒近似和交易/结算口径差异。

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

`greeks_attribution_by_episode.csv` 字段：

- `trading_date`
- `episode_id`
- `underlying`
- `delta_pnl`
- `gamma_pnl`
- `theta_pnl`
- `vega_pnl`
- `hedge_pnl`
- `cost_pnl`
- `explained_pnl`
- `gamma_theta_pnl`
- `option_delta_exposure`
- `option_gamma_exposure`
- `option_theta_exposure`
- `option_vega_exposure`
- `weighted_position_iv`
- `weighted_position_iv_change`
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
