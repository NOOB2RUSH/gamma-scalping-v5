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

暂不实现滚仓：

- `roll_min_ttm_days` 和同日滚仓逻辑先保留注释，不参与第一版执行。
- 平仓后是否重新开仓交给下一交易日重新评估。

`hv_iv_edge` 机制：

- 第一版保留 `use_vol_filter` 和 `min_hv_iv_edge` 配置占位，但默认 `use_vol_filter=False`。
- 具体阈值和入场/退出机制等回测结果可信后再补充，不在第一版细化。

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
