# Greeks 计算模块设计

## 职责

Greeks 模块负责期权定价、单合约 Greeks、组合 Greeks 和 delta 对冲需求计算。它不负责选择合约，也不负责成交。

## 模型范围

第一版使用 `py_vollib_vectorized` 作为默认计算后端，模型口径为 Black-Scholes，ETF 期权按欧式近似处理：

- 输入：标的价格 `S`、行权价 `K`、到期时间 `T`、无风险利率 `r`、分红率 `q`、波动率 `sigma`、期权类型。
- 输出：理论价格、delta、gamma、vega、theta、rho。

实现要求：

- 业务层封装 `py_vollib_vectorized`，不在策略模块中直接调用第三方库。
- 将本地 `C`/`P` 类型转换为库需要的 flag。
- 对整条期权链做向量化计算，避免逐合约 Python 循环。
- 统一处理 `T`、`sigma`、`r`、`q`、价格单位和合约乘数。
- 无风险利率 `r` 从配置读取，默认 `0`，不得在函数内部硬编码。
- 自研 Black-Scholes 公式仅作为测试基准或依赖不可用时的 fallback。
- 第一版默认分红率 `q=0`，使用 `py_vollib_vectorized` 的 Black-Scholes 接口。
- 当 `dividend_rate != 0` 时，必须跳过 `py_vollib_vectorized`，使用本地 Black-Scholes-Merton fallback；原因是当前验证发现第三方库 BSM rho 口径与 Black-Scholes 基准不一致，直接使用会污染组合 rho。

后续可以扩展：

- BAW 或二叉树近似美式行权。
- 本地波动率或波动率曲面插值。
- 对股息、除权和特殊到期日做更精细处理。

## 接口草案

```python
class GreeksCalculator:
    def __init__(self, backend: str = "py_vollib_vectorized") -> None: ...
    def price(self, contract: OptionContract, market: MarketState, sigma: float) -> float: ...
    def greeks(self, contract: OptionContract, market: MarketState, sigma: float) -> OptionGreeks: ...
    def enrich_chain(self, chain: OptionChain, market: MarketState, vol_input: VolInput) -> OptionChainGreeks: ...
    def portfolio_greeks(self, positions: list[Position], greeks: OptionChainGreeks) -> PortfolioGreeks: ...
```

## 时间与单位

- `T` 统一使用交易日口径：`T = remaining_trading_days / annual_trading_days`。
- `remaining_trading_days` 由中国大陆交易日历计算，自动跳过周末和中国大陆法定节假日。
- `annual_trading_days` 默认 `252`，可配置；统一回测入口应通过 `common.annual_trading_days` 同步传播到全系统，避免各模块口径不一致。
- 当到期日不是交易日时，先按交易日历调整到前一个有效交易日，再计算 `T`。
- 无风险利率 `r` 是模型配置项，默认 `0`；分红率 `q` 也作为配置项，默认 `0`。
- `vega` 输出为波动率变动 1.00 的价格敏感度；`py_vollib_vectorized` 原始 vega 为 1 vol point 口径，封装层需要乘以 `100`。
- `rho` 输出为利率变动 1.00 的价格敏感度；`py_vollib_vectorized` 原始 rho 为 1 percentage point 口径，封装层需要乘以 `100`。
- `theta` 输出为日 theta，用于回测归因。
- 单合约 Greeks 需要乘以 `contract_multiplier` 后才能进入组合风险。

## Delta 对冲需求

组合总 delta：

```text
portfolio_delta = option_delta_notional + etf_quantity
```

其中：

```text
option_delta_notional = sum(option_delta * contract_multiplier * option_quantity)
```

目标 ETF 对冲数量：

```text
target_etf_quantity = - option_delta_notional
hedge_order_quantity = target_etf_quantity - current_etf_quantity
```

第一版按 ETF 份额取整，后续可加入最小交易单位。

## 异常处理

- `T <= 0` 的合约不计算新 Greeks，交由到期结算模块处理。
- `sigma <= 0`、输入为空或第三方库返回无效值时返回质量标记，策略模块应过滤。
- `dividend_rate != 0` 不视为异常，但必须走本地 Black-Scholes-Merton fallback，并在测试中覆盖。
- 深度实值/虚值造成数值不稳定时，需要限制 `d1/d2` 和牛顿法迭代边界。

## 测试重点

- call/put 平价关系。
- delta、gamma、vega 的合理符号。
- 到期时间接近 0 的边界行为。
- 交易日历口径下的 `T` 计算，包括春节、国庆等中国大陆法定节假日。
- 无风险利率默认值为 `0`，配置覆盖后传递到 `py_vollib_vectorized`。
- 非零分红率场景跳过 `py_vollib_vectorized`，使用本地 Black-Scholes-Merton fallback。
- 组合 Greeks 乘数与数量方向。
- ETF 对冲数量计算。
- `py_vollib_vectorized` 向量化输出与单合约基准结果一致。
