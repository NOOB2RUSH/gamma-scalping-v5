# 策略逻辑模块设计

## 职责

策略模块根据行情、Greeks 和波动率信号生成目标持仓或订单意图。它不直接读文件，不直接修改账户，也不自行计算成交价格。

## 第一版策略

默认策略：买入 ATM straddle + ETF delta hedge。

交易流程：

1. 在候选到期日中选择目标期限，如剩余 `5-20` 个交易日，默认与 `AtmIvConfig` 的 DTE/TTM 区间保持一致。
2. 在该期限内选择最接近 ATM 的 call 和 put。
3. 通过流动性与价差过滤。
4. 按风险预算计算期权张数。
5. 建立正 gamma 组合。
6. 每个交易日根据组合 delta 计算 ETF 对冲目标。
7. 第一版暂不考虑滚仓；当到期、合约质量恶化或风险条件触发时只平仓，不同日换新仓。
8. 最大同时持有 straddle 组数由 `max_open_positions` 控制，默认 `1`。

## 合约选择规则

默认过滤：

- `min_ttm_days <= ttm_trading_days <= max_ttm_days`
- `volume >= min_option_volume`
- `open_interest >= min_open_interest`
- `spread_pct = (ask - bid) / mid <= max_spread_pct`
- `mid > min_option_price`

ATM 选择：

```text
atm_score = abs(strike / spot - 1)
```

straddle 第一版必须由同到期日、同行权价的一对 call/put 构成。选择顺序为：先构造所有同到期日、同行权价的 call/put 配对，再在这些配对中选择 `strike` 最接近现货、且 `ttm_trading_days` 最接近 `target_ttm_days` 的一对。若不存在同到期日、同行权价配对，则不建仓；近似 straddle/strangle 留作后续扩展。

## 仓位 sizing

建议第一版使用权利金预算：

```text
option_premium_budget = equity * premium_budget_pct
contracts = floor(option_premium_budget / straddle_premium_per_contract)
```

其中：

```text
straddle_premium_per_contract = (call_buy_price * call_multiplier) + (put_buy_price * put_multiplier)
```

开仓 sizing 优先使用 `buy_price`，不使用 `mid`，以避免低估买入成本。若策略输入来自非标准链路且缺少 `buy_price`，策略模块按 `ask -> mark_price -> mid -> theoretical_price` 的顺序派生 `buy_price`，并保留该选择用于决策追溯。后续可增加 gamma 目标：

```text
target_gamma_notional = equity * gamma_budget_pct / spot
```

## 持仓与对冲

第一版只处理：

- 开仓：无本策略期权持仓，且 `open_straddle_count < max_open_positions`。
- 持仓：已有 straddle 时不新增期权腿。
- 对冲：当 `abs(portfolio_delta * spot) / equity > delta_threshold_pct` 时输出 ETF 对冲订单。
- 平仓：合约到期、质量恶化、最大持仓天数达到上限或无法计算 Greeks 时退出期权腿。

## IV/HV 过滤机制

Gamma Scalping 做多 Gamma，核心假设是持有期实现波动率能够覆盖买入期权支付的隐含波动率、theta、交易成本和离散对冲误差。因此 IV/HV 过滤不是简单的行情展示指标，而是策略入场和平仓条件的一部分。

基本方向：

```text
当 IV < 预期 RV/HV：期权相对便宜，允许买入 ATM straddle。
当 IV >= 预期 RV/HV：波动率价差被填补或反转，考虑平仓。
```

其中 RV/HV 是可观测历史实现波动率对未来实现波动率的代理。策略不得直接使用未来实现波动率。

### 避免未来函数

“利用回测范围数据计算 HV 分布”只能用于研究校准，不能直接用于正式交易判断。正式回测默认采用 walk-forward 口径：

```text
vol_filter_calibration_mode = "walk_forward"
```

该字段归属 `VolatilityConfig`。策略只接收波动率模块已经计算好的 `rv_reference` 信号。

含义：

- 当日信号只能使用当日及以前已经可观测的数据。
- 滚动 HV 分布只能使用历史窗口内的数据，例如过去 252 个交易日。
- 不得用完整回测区间的 HV 分布决定历史任意一天是否开仓。

若后续增加研究模式：

```text
vol_filter_calibration_mode = "full_sample_calibration"
```

该模式只能用于参数探索和诊断报告，不得作为正式绩效口径。

### RV 参考值

策略不负责计算 RV 参考值，也不持有滚动 HV 分布参数。`rv_reference`、`rv_iv_edge`、`iv_rv_ratio` 和样本充足性由波动率模块的 `build_signal` / `build_signal_series` 统一计算并输出。

第一版策略消费的信号为：

```text
atm_iv
hv_20
rv_reference
rv_reference_source
rv_reference_status
rv_observation_count
rv_iv_edge = rv_reference - atm_iv
iv_rv_ratio = atm_iv / rv_reference
```

`vol_filter_calibration_mode`、`rv_reference_mode`、`rv_reference_hv_column`、`rv_distribution_lookback_days`、`rv_distribution_min_observations`、`rv_distribution_quantile` 等字段归属 `VolatilityConfig`，详见波动率模块设计。若 `rv_reference` 缺失、为非正数，或 `rv_reference_status != "ok"`，策略不得因为 IV/HV 过滤而开新仓。

### 入场条件

入场 IV 口径使用波动率模块输出的 `atm_iv`，该值必须与 `AtmIvConfig` 的 DTE、期权类型和聚合口径一致。

定义：

```text
entry_edge = rv_reference - atm_iv
entry_ratio = atm_iv / rv_reference
```

允许开仓的条件：

```text
use_vol_filter = True
atm_iv 有效且 > 0
rv_reference 有效且 > 0
rv_reference_status == "ok"
entry_edge >= min_hv_iv_edge
entry_ratio <= entry_max_iv_rv_ratio
```

推荐第一版默认配置：

```python
StrategyConfig(
    use_vol_filter=False,
    min_hv_iv_edge=0.02,
    entry_max_iv_rv_ratio=0.90,
)
```

说明：

- `min_hv_iv_edge=0.02` 表示 RV 参考值至少比 IV 高 2 个 vol points。字段名暂沿用既有配置，实际语义是 `rv_reference - atm_iv` 的最低要求。
- `entry_max_iv_rv_ratio=0.90` 表示 IV 不高于 RV 参考值的 90%。
- 同时使用 edge 和 ratio 是为了避免低波动环境下绝对差值过小、或高波动环境下相对折价不足。

入场交易理由：

```text
reason = "open_atm_straddle_vol_edge"
risk_flags = ()
```

若过滤不通过：

```text
reason = "vol_filter_not_satisfied"
risk_flags = ("vol_filter",)
```

策略决策记录应保留：

- `atm_iv`
- `rv_reference`
- `entry_edge`
- `entry_ratio`
- `rv_reference_source`

### 持仓期间退出条件

持仓后，原有 call/put 可能不再是 ATM，因此退出判断不能默认继续使用市场 ATM IV。策略的 `on_snapshot` 已接收逐合约 `greeks` DataFrame，该表包含每只合约的 `iv` 和 `iv_status`。退出判断直接从 `greeks` 中读取当前持仓 call/put 合约行，无需让 `VolatilitySignal` 增加逐合约 IV 接口。

退出 IV 参考值优先级：

```text
1. held_position_vega_weighted_iv
2. held_position_average_iv
3. current atm_iv fallback
```

第一版建议先实现：

```text
exit_iv_reference_mode = "position_average_iv"
```

即使用当前持仓 call/put 合约的 IV 简单平均值。后续再升级为 Vega 加权：

```text
held_iv_reference = sum(abs(vega_i) * iv_i) / sum(abs(vega_i))
```

定义：

```text
exit_edge = rv_reference - held_iv_reference
exit_ratio = held_iv_reference / rv_reference
```

触发平仓的条件：

```text
exit_on_vol_edge_filled = True
exit_edge <= exit_max_rv_iv_edge
或
exit_ratio >= exit_min_iv_rv_ratio
```

推荐第一版默认配置：

```python
StrategyConfig(
    exit_on_vol_edge_filled=False,
    exit_max_rv_iv_edge=0.00,
    exit_min_iv_rv_ratio=1.00,
    exit_iv_reference_mode="position_average_iv",
)
```

若希望降低频繁进出，可使用滞后区间：

```text
入场：IV <= RV * 0.90
退出：IV >= RV * 0.98 或 IV >= RV * 1.00
```

平仓交易理由：

```text
reason = "exit_vol_edge_filled"
risk_flags = ("vol_edge_filled",)
```

如果持仓合约 IV 缺失或 `iv_status != "ok"`，不得用波动率退出逻辑做判断，应进入已有质量风控退出：

```text
reason = "exit_risk_condition"
risk_flags = ("bad_iv",)
```

### 策略日循环顺序

加入 IV/HV 过滤后，策略日循环顺序应为：

```text
1. 检查硬风险退出：
   到期、bad_greeks、bad_iv、missing_contract、max_holding_days。

2. 检查波动率退出：
   已持仓且 exit_on_vol_edge_filled=True，
   若 IV/RV 价差已经填补，则平掉同 episode 的 option legs 和 ETF hedge。

3. 若仍持仓，则执行 delta hedge。

4. 若无持仓，则检查开仓条件：
   max_open_positions、IV/HV 入场过滤、合约选择、sizing。
```

波动率退出应在日常对冲之前执行，避免当天已经满足平仓条件时仍然先做不必要的 ETF hedge。

## 显式 Episode 机制

IV/HV 差值捕获率需要按“开仓 straddle -> 持有与对冲 -> 平仓/到期”生命周期聚合，因此策略层必须在开仓时生成并透传显式 `episode_id`，不能让绩效模块从持仓表反推。

Episode 定义：

```text
一个 episode = 一组由同到期日、同行权价 call/put 构成的 long straddle 及其 ETF 对冲腿的完整生命周期。
```

第一版 `episode_id` 生成规则：

```text
{strategy_tag}:{trading_date:%Y%m%d}:{call_contract_id}:{put_contract_id}
```

约束：

- 同一 episode 内，call leg、put leg、初始 ETF hedge、后续 ETF hedge、平仓订单必须使用同一个 `episode_id`。
- `OrderIntent` 增加 `episode_id` 字段；开仓订单由策略生成新的 `episode_id`，平仓和对冲订单从现有 `StrategyPosition.episode_id` 继承。
- `StrategyPosition` 增加 `episode_id` 字段；组合状态按 `episode_id` 识别持仓组。
- 当前 `max_open_positions=1` 时实现简单；后续允许多组并行持仓时，策略必须按 `episode_id` 分组计算期权 Greeks、ETF 对冲目标和退出条件。
- 多 episode 并行时采用“按 episode 独立对冲”规则：每个 episode 只使用该 episode 下的期权腿和 ETF hedge 持仓计算净 delta，并独立发出带同一 `episode_id` 的 ETF 对冲订单。禁止先计算账户总 delta 再按比例拆分到 episode，因为比例拆分会让对冲 PnL 归属随其他 episode 的开平仓变化而漂移。
- 不允许把没有 `episode_id` 的 ETF hedge 订单计入 gamma scalping episode；否则 IV/HV 捕获率分子会被污染。
- episode 退出时必须同时关闭同一 `episode_id` 下的 ETF hedge；否则 episode 关闭后仍会残留对冲腿 PnL，污染后续归因。

episode 对冲目标：

```text
episode_option_delta = sum(option_delta_i * quantity_i * multiplier_i) within episode
episode_etf_delta = etf_quantity within episode
episode_net_delta = episode_option_delta + episode_etf_delta
episode_hedge_order_quantity = -episode_net_delta
```

只有当该 episode 的 delta 偏离超过阈值时，才输出该 episode 的 ETF hedge 订单。

开仓决策需要在 `StrategyDecision` 中记录：

- `selected_contracts`
- `episode_id`
- `entry_atm_iv`
- `entry_hv_20`
- `entry_spot`

其中 `entry_atm_iv` 来自波动率模块输出的 ATM IV，用于后续 IV/HV 捕获率分母；不得在绩效层重新选择 IV 口径。

暂不实现滚仓：

- `roll_min_ttm_days` 和同日滚仓逻辑先保留注释，不参与第一版执行。
- 平仓后是否重新开仓交给下一交易日重新评估。

兼容旧字段：

- `hv_iv_edge = hv_20 - atm_iv` 保留作为简单信号和历史兼容字段。
- 新策略逻辑优先使用 `rv_reference - iv_reference`，不再直接绑定 `hv_20`。
- 第一版继续保留 `min_hv_iv_edge` 作为入场绝对 edge 阈值，不新增含义重复的入场 edge 字段。后续若重命名，应一次性迁移并删除旧字段，而不是两者共存。

## 配置草案

```python
StrategyConfig(
    min_ttm_days=5,
    max_ttm_days=20,
    target_ttm_days=10,
    max_open_positions=1,
    premium_budget_pct=0.1,
    delta_threshold_pct=0.01,
    min_option_volume=1,
    min_open_interest=0,
    max_spread_pct=0.5,
    min_option_price=0.0001,
    max_holding_days=None,
    use_vol_filter=False,
    min_hv_iv_edge=0.0,
    entry_max_iv_rv_ratio=0.90,
    exit_on_vol_edge_filled=False,
    exit_max_rv_iv_edge=0.00,
    exit_min_iv_rv_ratio=1.00,
    exit_iv_reference_mode="position_average_iv",
)
```

## 输出接口

```python
class GammaScalpingStrategy:
    def on_snapshot(
        self,
        snapshot: MarketSnapshot,
        greeks: OptionChainGreeks,
        vol_signal: VolSignal,
        portfolio: PortfolioState,
    ) -> StrategyDecision: ...
```

`StrategyDecision` 包含：

- `action`
- `target_positions`
- `order_intents`
- `reason`
- `risk_flags`
- `selected_contracts`
- `episode_id`
- `entry_atm_iv`
- `entry_hv_20`
- `entry_spot`
- `entry_edge`
- `entry_ratio`
- `rv_reference`
- `rv_reference_source`

## 风控规则

- 权利金占净值比例上限。
- 最大同时持仓 straddle 组数，默认 `1`。
- 单日换手上限。
- ETF 对冲后绝对 delta 上限。
- 合约到期强制退出。
- 期权链缺失或 Greeks 质量差时只允许降风险，不允许新增风险。

## 测试重点

- ATM straddle 合约选择。
- 只有同到期日、同行权价 call/put 配对才允许建仓。
- `max_open_positions=1` 时已有持仓不重复开仓。
- 缺失 call 或 put 时不建仓。
- delta hedge 触发逻辑。
- 到期平仓逻辑，剩余期限按交易日历计算。
- 流动性过滤与风险预算。
- 输入缺少 `buy_price` 时从 `ask` 等价格列派生，不因非标准 Greeks DataFrame 直接崩溃。
- IV/HV 入场过滤不得使用未来数据。
- `atm_iv < rv_reference` 且满足 edge/ratio 阈值时才允许开仓。
- 持仓期间 `held_iv_reference >= rv_reference` 或达到配置比例时触发 `exit_vol_edge_filled`。
- 波动率退出优先于日常 delta hedge。
