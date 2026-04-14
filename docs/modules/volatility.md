# 波动率计算模块设计

## 职责

波动率模块负责从标的和期权价格中生成策略所需的波动率输入和信号，包括历史波动率、隐含波动率、ATM IV、期限结构、IV/HV spread 和波动率质量标记。

## 历史波动率

输入为 ETF 收盘价序列。

基础实现：

```text
log_return[t] = ln(close[t] / close[t-1])
hv_window = std(log_return, window) * sqrt(annualization_days)
```

`annualization_days` 必须来自统一交易日历配置，默认 `252`，不得使用自然日 `365`。滚动窗口按有效交易日计数，自动跳过周末和中国大陆法定节假日。

推荐默认窗口：

- `hv_10`
- `hv_20`
- `hv_60`

## 隐含波动率

使用期权市场价格反解 Black-Scholes 隐含波动率。第一版默认通过 `py_vollib_vectorized` 批量求解，但只支持 `dividend_rate = 0`：

- 价格输入优先使用 `mid`。
- 若 bid/ask 无效，则按配置回退到 `close` 或剔除。
- 业务层负责过滤低于内在价值或高于理论上界的无效价格。
- 求解失败时保留合约但设置 `iv_status='failed'`。
- 若 `py_vollib_vectorized` 不可用，再使用本地 Brent 二分 fallback，边界如 `[0.0001, 5.0]`。
- 若配置 `dividend_rate != 0`，第一版显式报错，不做隐式 fallback。原因是 IV 求解会直接影响策略信号，非零分红率口径需要单独验证后再开放。

## 曲面快照

第一版不做复杂曲面拟合，只生成离散快照：

```python
VolSurfaceSnapshot(
    trading_date,
    underlying,
    rows=[
        contract_id,
        maturity_date,
        ttm,
        strike,
        moneyness,
        option_type,
        iv,
        price_quality
    ]
)
```

ATM IV 聚合：

- 按 `abs(strike / spot - 1)` 最小选择 ATM。
- 可分别计算 call ATM IV、put ATM IV。
- 策略使用的 ATM IV 必须通过配置指定 DTE/TTM 区间、期权类型和聚合方法，不固定绑定近月或次近月。
- 默认配置建议为 `min_ttm_days=5`、`max_ttm_days=20`、`option_types=["C", "P"]`、`per_maturity_atm_only=True`、`aggregation="mean"`。
- `per_maturity_atm_only=True` 时，先在每个到期日内分别选择最接近 ATM 的 call/put，再对这些 ATM 合约的 IV 聚合。
- 缺一侧时可按配置选择使用可用侧或丢弃该到期日；第一版默认使用可用侧。
- 输出需要包含参与聚合的合约 ID、到期日、`ttm_trading_days` 和样本数，避免信号不可追溯。

示例：

```python
AtmIvConfig(
    min_ttm_days=5,
    max_ttm_days=20,
    option_types=("C", "P"),
    per_maturity_atm_only=True,
    aggregation="mean",
    allow_single_side=True,
)
```

上述配置表示：输出剩余交易日处于 `5-20` 天的 ATM 期权合约平均 IV。

## 时间序列输出

为支持后续可视化模块绘制 `atm_iv`、`hv_10`、`hv_20`、`hv_60`、`iv_hv_spread` 等曲线，波动率模块必须输出标准化时间序列表。可视化模块只消费该表，不自行重算 IV/HV。

建议对象：

```python
VolatilityTimeSeries(
    underlying,
    frame=[
        trading_date,
        atm_iv,
        atm_iv_contract_count,
        atm_iv_contract_ids,
        atm_iv_maturities,
        atm_iv_min_ttm_days,
        atm_iv_max_ttm_days,
        hv_10,
        hv_20,
        hv_60,
        iv_hv_spread,
        hv_iv_edge,
        term_slope,
        iv_valid_count,
        iv_failed_count,
        iv_status_summary,
    ]
)
```

其中：

- `atm_iv_contract_ids` 和 `atm_iv_maturities` 用于追溯每个交易日曲线点来自哪些合约。
- `atm_iv_min_ttm_days`、`atm_iv_max_ttm_days` 记录本次聚合配置，避免不同配置的曲线混用。
- `iv_hv_spread = atm_iv - hv_20`。
- `hv_iv_edge = hv_20 - atm_iv`，买 gamma 策略可以直接读取该方向的波动率边际。
- `iv_status_summary` 记录逐合约 IV 求解状态计数，供图表展示数据质量。

## 策略信号

建议输出：

- `atm_iv`
- `atm_iv_contract_count`
- `atm_iv_contract_ids`
- `hv_20`
- `iv_hv_spread = atm_iv - hv_20`
- `rv_iv_spread = realized_vol_holding - entry_atm_iv`，用于事后评估。
- `term_slope = next_month_atm_iv - front_month_atm_iv`
- `iv_rank` 或 `iv_percentile`
- `realized_vol_proxy`：可选，用 high-low 估计的 Parkinson 波动率。

## IV/HV 差值捕获率

建议增加该指标，但应放在绩效模块做事后评估，波动率模块只提供所需基础字段。原因是捕获率需要实际持仓 PnL、对冲成本和持有期已实现波动，单靠信号模块无法闭环。

建议定义两层指标：

- 信号命中率：入场时 `expected_edge = hv_forecast - entry_atm_iv`，持有期结束后检查 `realized_edge = realized_vol_holding - entry_atm_iv` 是否同向。
- PnL 捕获率：`iv_hv_capture_rate = net_gamma_scalping_pnl / theoretical_vol_edge_pnl`。

其中 `theoretical_vol_edge_pnl` 使用近似公式：

```text
theoretical_vol_edge_pnl = sum(0.5 * gamma_t * S_t^2 * (realized_var_t - implied_var_t) * dt)
```

当分母绝对值过小或为负时，该交易的捕获率不参与均值统计，只记录为不可解释样本。

## 接口草案

```python
class VolatilityEngine:
    def __init__(self, iv_backend: str = "py_vollib_vectorized") -> None: ...
    def compute_hv(self, etf_history: DataFrame, windows: list[int]) -> HVSeries: ...
    def solve_iv_chain(self, snapshot: MarketSnapshot, model_inputs: ModelInputs) -> VolSurfaceSnapshot: ...
    def atm_iv(self, surface: VolSurfaceSnapshot, config: AtmIvConfig) -> AtmIvResult: ...
    def build_signal(self, surface: VolSurfaceSnapshot, hv_state: HVState) -> VolSignal: ...
    def build_signal_series(self, snapshots: Iterable[MarketSnapshot], etf_history: DataFrame, config: AtmIvConfig) -> VolatilityTimeSeries: ...
```

## 测试重点

- HV 年化口径。
- HV 窗口按交易日计数，并正确跳过中国大陆法定节假日。
- IV 求解价格边界：市场价低于内在价值、高于上界。
- `dividend_rate != 0` 时显式报错。
- ATM IV 聚合配置，如 `5-20` 个剩余交易日的 ATM 合约平均 IV。
- `build_signal_series` 输出可直接绘制的 `atm_iv`、`hv_10`、`hv_20`、`hv_60` 时间序列。
- 时间序列每个 `atm_iv` 点必须能追溯到合约 ID 和到期日。
- 同一到期日 call/put IV 合并。
- 缺失历史窗口时的行为。
- `py_vollib_vectorized` 批量 IV 输出与少量单合约基准结果一致。
