# 数据加载模块设计

## 职责

数据加载模块负责把磁盘上的 parquet 文件转换为统一、可迭代、经过基础校验的行情对象。它不计算信号，不做 Greeks，不维护账户。

## 输入

- `data_root`: 默认 `data/`
- `underlying`: 默认 `510050.XSHG`
- `start_date` / `end_date`
- `frequency`: 第一版固定为 `1d`
- `price_policy`: `mid_first`、`close_only`、`bid_ask_conservative`
- `calendar`: 中国大陆交易日历配置，默认自动跳过周末和中国大陆法定节假日。

## 输出

- `TradingCalendar`: 可用交易日序列。
- `ETFBar`: 单日 ETF 行情。
- `OptionChain`: 单日期权链。
- `MarketSnapshot`: ETF 行情与期权链组合后的交易日快照。

## 交易日历

交易日历是全系统共享基础组件，用于：

- 生成回测日期序列。
- 计算期权剩余交易日 `ttm_trading_days`。
- 计算滚仓、持仓天数、调仓周期。
- 统一 HV 年化、Greeks 的 `T`、绩效年化口径。

实现要求：

- 自动跳过周末和中国大陆法定节假日。
- 优先使用可靠交易日历源；本地数据文件日期只作为可用行情交集，不应替代日历定义。
- 若交易日历认为某日应交易但本地 ETF 或期权文件缺失，按配置 `missing_data_policy` 选择报错、跳过或记录质量标记。
- 若本地文件日期不在交易日历内，应记录异常，默认不纳入回测。
- 年化交易日数默认 `252`，可配置。

第一版实现说明：

- 当前环境没有可用的交易日历依赖库，因此先实现依赖无关的 `TradingCalendar`，内置覆盖现有数据范围的中国大陆法定休市日表。
- `TradingCalendar` 支持注入额外节假日，后续可替换为专用交易日历库后端；数据加载器只依赖 `is_session`、`sessions`、`previous_session`、`trading_days_between` 这些稳定接口。
- 本地行情文件日期只与交易日历取交集，不作为日历定义来源。

## 字段标准化

ETF：

| 标准字段 | 来源字段 | 说明 |
| --- | --- | --- |
| `trading_date` | parquet index 或文件名 | 交易日期 |
| `open` | `open` | 开盘价 |
| `close` | `close` | 收盘价 |
| `high` | `high` | 最高价 |
| `low` | `low` | 最低价 |
| `volume` | `volume` | 成交量 |
| `turnover` | `money` | 成交额 |

期权：

| 标准字段 | 来源字段 | 说明 |
| --- | --- | --- |
| `contract_id` | `order_book_id` | 合约 ID |
| `strike` | `strike_price` | 行权价 |
| `maturity_date` | `maturity_date` | 到期日 |
| `option_type` | `option_type` | `C` 或 `P` |
| `bid` | `bid` | 买一价 |
| `ask` | `ask` | 卖一价 |
| `mid` | `(bid + ask) / 2` | 基础估值价格 |
| `buy_price` | `ask` 或回退价 | 保守买入价格 |
| `sell_price` | `bid` 或回退价 | 保守卖出价格 |
| `mark_price` | 按 `price_policy` 选择 | 回测估值价格 |
| `price_quality` | 派生字段 | 价格质量标记 |
| `last` | `close` | 收盘价 |
| `volume` | `volume` | 成交量 |
| `open_interest` | `open_interest` | 持仓量 |
| `multiplier` | `contract_multiplier` | 合约乘数 |

价格策略：

- `mid_first`: `mid=(bid+ask)/2`，bid/ask 无效时回退到 `close`。
- `close_only`: 直接使用 `close` 作为 `mark_price`。
- `bid_ask_conservative`: `mark_price` 使用 `buy_price=ask`，并同时输出 `sell_price=bid`，供后续执行模型按订单方向使用；bid/ask 无效时买卖价格都回退到 `close`。

到期字段：

- `maturity_date`: 保留数据源原始到期日。
- `maturity_session`: 若原始到期日不是交易日，按交易日历映射到前一个有效交易日。
- `ttm_trading_days`: 从当前交易日到 `maturity_session` 的剩余交易日数，默认不包含当前交易日、包含到期交易日。

## 接口草案

```python
class MarketDataLoader:
    def trading_calendar(self) -> TradingCalendar: ...
    def list_trading_dates(self) -> list[date]: ...
    def trading_days_between(self, start: date, end: date) -> int: ...
    def load_etf_bar(self, trading_date: date) -> ETFBar: ...
    def load_option_chain(self, trading_date: date) -> OptionChain: ...
    def load_snapshot(self, trading_date: date) -> MarketSnapshot: ...
    def iter_snapshots(self) -> Iterator[MarketSnapshot]: ...
```

## 校验规则

- ETF 文件与期权链文件必须按交易日一一对齐；缺失时按配置选择跳过或报错。
- `maturity_date` 必须晚于或等于交易日期；已到期合约由策略过滤。
- `maturity_date` 与 `trading_date` 的剩余期限必须通过交易日历计算，不使用自然日差。
- `bid <= ask`；若 bid/ask 无效，则 `mid` 回退到 `close`，并打上质量标记。
- `contract_multiplier > 0`。
- `option_type` 仅允许 `C`、`P`。
- 标的价格必须大于 0。

## 缓存策略

第一版优先简单可靠：

- 单日文件读取后可用 LRU 缓存。
- 不预先合并所有 parquet，避免内存峰值过高。
- 参数调优阶段可以增加日期级缓存或预处理后的 feather/parquet 分区文件。

## 测试重点

- 文件名日期解析。
- 缺失 ETF 或期权文件时的行为。
- bid/ask 异常时价格回退。
- `bid_ask_conservative` 输出 `buy_price`、`sell_price` 和保守 `mark_price`。
- 日期范围过滤。
- 中国大陆法定节假日和周末自动跳过。
- 期权链字段标准化与类型转换。
