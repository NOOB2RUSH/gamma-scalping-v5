# 回测引擎模块设计

## 职责

回测引擎负责按交易日推进事件循环，接收策略订单意图，模拟成交，维护现金、持仓、估值、费用、PnL 和风险记录。它不内嵌具体策略规则。

## 事件循环

第一版日频流程：

```text
for snapshot in data_loader.iter_snapshots():
    update_market_state(snapshot)
    handle_expiry_and_settlement()
    compute_volatility()
    compute_greeks()
    mark_to_market_portfolio()
    decision = strategy.on_snapshot(...)
    orders = risk_check(decision.order_intents)
    fills = execution_model.fill(orders, snapshot)
    portfolio.apply_fills(fills)
    record_daily_state()
```

## 成交模型

默认成交价格：

- 买入期权：`ask`
- 卖出期权：`bid`
- 若 bid/ask 无效：按 `last/mark_price` 回退，并继续叠加配置的期权滑点。
- ETF：使用 `close`，可配置固定 bps 滑点。

手续费：

- ETF 按成交金额 bps。
- 期权按每张固定费用或金额 bps，二者可配置。

第一版实现边界：

- 使用日频收盘后决策和成交假设，不模拟分钟级路径。
- 期权成交使用方向性价格：买入用 `buy_price/ask`，卖出用 `sell_price/bid`，缺失时回退到 `last/mark_price`，所有路径均叠加配置的期权滑点。
- ETF 成交使用 `close` 并叠加可配置 bps 滑点。
- 支持固定每张期权手续费、期权金额 bps 费用和 ETF 金额 bps 费用。
- 不模拟盘口深度、成交量约束、保证金、行权指派和交易所特殊结算规则。
- V1 增加 `RiskChecker` 风控检查扩展点，默认 passthrough，可配置最大单笔订单数量；后续可扩展资金、集中度、保证金和换手约束。

## 持仓估值

期权持仓：

```text
market_value = quantity * mark_price * contract_multiplier
```

ETF 持仓：

```text
market_value = quantity * etf_close
```

账户净值：

```text
equity = cash + sum(position_market_value)
```

第一版回测结果输出：

结果目录结构：

```text
results/
└── {run_id}/
    ├── config.json
    ├── metadata.json
    ├── trade_records.csv
    ├── position_records.csv
    ├── equity_curve.csv
    ├── decisions.csv
    ├── fills.csv
    ├── expiry_events.csv
    └── final_positions.csv
```

`run_id` 可以由配置指定；若未指定，由时间戳、策略标签和标的生成。

- `equity_curve`: `trading_date`、`cash`、`market_value`、`equity`、`cumulative_fee`、`realized_pnl`、`action`。
- `equity_curve` 同时记录 `pre_trade_market_value` 和 `pre_trade_equity`，作为显式 mark-to-market 扩展点，用于后续保证金或风险逻辑。
- `fills`: 成交流水。
- `trade_records`: 按 bar 时间序列排序的交易记录 CSV 源表。每笔交易一行，同一交易日多笔交易使用相同 `trading_date` 输出多行；无交易日输出一行空交易记录，便于按日对齐。
- `position_records`: 按 bar 时间序列排序的持仓状态 CSV 源表。每个交易日每个持仓一行；无持仓日输出一行空持仓记录。
- `decisions`: 策略每日决策、原因、风险标记、订单数量。
- `expiry_events`: 到期结算事件。
- `final_positions`: 回测结束持仓。

`trade_records` 字段：

- `trading_date`
- `instrument_id`
- `instrument_type`
- `side`
- `quantity`
- `price`
- `multiplier`
- `trade_amount`
- `fee`
- `reason`
- `role`

`position_records` 字段：

- `trading_date`
- `instrument_id`
- `instrument_type`
- `quantity`
- `avg_price`
- `multiplier`
- `mark_price`
- `liquidation_price`
- `market_value`
- `liquidation_value`
- `cost_basis_value`
- `theoretical_unrealized_pnl`
- `role`
- `strategy_tag`
- `entry_trading_date`

其中 `theoretical_unrealized_pnl` 表示该 bar 结束后若立刻全部平仓的理论未实现盈亏。期权多头用 `sell_price/bid` 作为立刻平仓价，空头用 `buy_price/ask`；ETF 使用当日 `close`。该表必须支持按指定日期过滤出当日全部持仓。

## PnL 归因

建议每日记录：

- `option_pnl`
- `hedge_pnl`
- `fee`
- `slippage`
- `theta_estimate`
- `gamma_scalping_pnl`
- `theoretical_vol_edge_pnl`
- `entry_atm_iv`
- `realized_vol_holding`
- `delta_exposure`
- `gamma_exposure`
- `vega_exposure`

第一版可以先完成总 PnL 和期权/ETF 分腿 PnL。若要计算 IV/HV 差值捕获率，需要在交易级别额外记录入场 IV、持有期实际波动、逐日 gamma 和理论波动率边际 PnL。

## 到期处理

到期日合约应按内在价值结算。到期处理日期使用统一交易日历；若合约标注到期日不是有效交易日，按前一个有效交易日处理：

```text
call_payoff = max(spot - strike, 0) * multiplier
put_payoff = max(strike - spot, 0) * multiplier
```

结算后移除期权持仓。若真实市场存在提前摘牌或特殊结算价，后续单独扩展。

## 接口草案

```python
class BacktestEngine:
    def run(self, config: BacktestConfig) -> BacktestResult: ...

class ExecutionModel:
    def fill(self, orders: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]: ...

class Portfolio:
    def mark_to_market(self, snapshot: MarketSnapshot) -> PortfolioState: ...
    def apply_fills(self, fills: list[Fill]) -> None: ...
```

## 测试重点

- 买卖方向与现金变化。
- 乘数对期权市值和 PnL 的影响。
- 期权 bid/ask 成交规则。
- 到期结算和非交易日到期日调整。
- 调仓后持仓均价与数量。
- 空数据日和无成交日。
