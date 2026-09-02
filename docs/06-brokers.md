# 06. Brokers

**定位**：algot 撮合层。接收 Signal → 维护 position state + capital pool → 撮合 → 输出 Order。

> **本文档回答**：
> 1. Broker 接口长什么样？
> 2. Position state 怎么追踪？
> 3. Cash flow 怎么算？
> 4. Q1-Q4 决策怎么实现？
> 5. BacktestBroker / PaperBroker / RealBroker 三类的差异？

---

## 1. 数据流位置

```
Signal (intent, 05)
   │
   ▼
[06] broker layer  ← 本文档
   │  • PositionState 维护（per strategy + symbol）
   │  • CashPool 维护（per strategy）
   │  • Signal → Order 撮合
   │  • P&L 实时计算（realized + unrealized）
   ▼
Order (execution) → backtest results / live monitoring
```

---

## 2. Broker 接口

```python
from enum import Enum

class StrategyType(str, Enum):
    """Strategy 方向类型（区别于 Signal.direction；Direction 含 FLAT/CLOSE_*；StrategyType 仅 long/short）。"""
    LONG  = "long"
    SHORT = "short"


class BaseBroker(ABC):
    @abstractmethod
    def submit(
        self,
        strategy_id: str,
        strategy_type: StrategyType,           # LONG / SHORT (StrategyType, NOT Direction)
        signals: list[Signal],                 # 同 bar 内按 emit 顺序
        bar_time: datetime,                    # emit bar START time
        fill_price_lookup: Callable[[str, datetime], float],  # 给定 (symbol, time) 返回 fill price
        exec_lag: int = 1,                     # bar T emit → bar T+exec_lag open 撮合 (00 §6.5 G2)
    ) -> list[Order]:
        """撮合一组 Signals，返回 Order 列表。"""
        ...
    
    @abstractmethod
    def get_position(self, strategy_id: str, symbol: str) -> PositionSlot:
        """查询当前持仓（v1 单 slot per strategy）。"""
        ...
    
    @abstractmethod
    def get_cash(self, strategy_id: str) -> float:
        """查询当前可用现金。"""
        ...
    
    @abstractmethod
    def get_realized_pnl(self, strategy_id: str) -> float:
        """查询已实现 PnL。"""
        ...
```

**v1 实现**：
- `BacktestBroker`（详 §6）— 完整回测撮合
- `PaperBroker`（详 §7）— 实时模拟撮合（live mode 用）
- `RealBroker`（详 §8）— v2+ 真实 broker 接入，v1 仅 stub

---

## 3. 状态结构

### 3.1 PositionSlot（per strategy + symbol 单方向）

```python
@dataclass
class PositionSlot:
    strategy_id: str
    symbol: str
    direction: StrategyType          # ← LONG / SHORT（per StrategyType，not full Direction enum；v1 invariant: 恒等于 strategy_type）
    shares: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0
```

**v1 关键**：
- 每个 (strategy, symbol) 只跟踪**一个** slot（direction 由 strategy type 决定）
- 不用之前的 `PositionState(long_shares, short_shares, ...)` 模型——position 已经按 strategy 切分
- LONG strategy 的 AAPL slot ≠ SHORT strategy 的 AAPL slot（完全独立）

### 3.2 CashPool（per strategy）

```python
@dataclass
class CashPool:
    strategy_id: str
    initial_capital: float        # 启动时输入（Q3 决定: 各 strategy 单独 input）
    current_cash: float           # 当前可用现金
    total_realized_pnl: float = 0.0
```

**关键**：long 和 short 各 strategy 各自有 cash pool，**不共享**。

### 3.3 全局状态

```python
@dataclass
class BrokerState:
    pools: dict[str, CashPool]               # strategy_id → pool
    positions: dict[tuple[str, str], PositionSlot]  # (strategy_id, symbol) → slot
    fill_history: list[Order]                # 全部 Order 历史
```

---

## 4. Cash Flow 规则（按 Signal 类型）

### 4.1 LONG Signal（开多仓 / 加多仓）

```python
def apply_long(slot, pool, signal, fill_price):
    if isinstance(signal.size, FixedSize):
        shares = signal.size.shares
    elif isinstance(signal.size, PctSize):
        shares = (pool.current_cash * signal.size.pct) / fill_price
    elif isinstance(signal.size, RiskSize):
        # entry price 假设 = current fill_price (paper broker)
        risk_per_share = abs(fill_price - signal.size.stop_loss)
        if risk_per_share == 0:
            raise ValueError("RiskSize.stop_loss == entry price, infinite shares")
        shares = signal.size.risk_amount / risk_per_share
    
    # cash 检查
    required_cash = shares * fill_price
    if required_cash > pool.current_cash:
        actual_shares = pool.current_cash / fill_price
        WARN f"insufficient long pool cash: need ${required_cash}, have ${pool.current_cash}, "
             f"adjusted to {actual_shares:.2f} shares"
        shares = actual_shares
    
    # 加权平均 cost (Q1)
    new_total_cost = slot.shares * slot.avg_cost + shares * fill_price
    slot.shares += shares
    slot.avg_cost = new_total_cost / slot.shares
    
    pool.current_cash -= shares * fill_price
```

### 4.2 SHORT Signal（开空仓 / 加空仓）

```python
def apply_short(slot, pool, signal, fill_price):
    if isinstance(signal.size, FixedSize):
        shares = signal.size.shares
    elif isinstance(signal.size, PctSize):
        shares = (pool.current_cash * signal.size.pct) / fill_price
    elif isinstance(signal.size, RiskSize):
        risk_per_share = abs(signal.size.stop_loss - fill_price)
        shares = signal.size.risk_amount / risk_per_share
    
    # short 不需要 cash（v1 paper 假设 unlimited borrow，无 margin call）
    # 但 shares > 0 sanity check
    if shares <= 0:
        raise ValueError("SHORT shares must be > 0")
    
    # 加仓
    new_total_cost = slot.shares * slot.avg_cost + shares * fill_price
    slot.shares += shares
    slot.avg_cost = new_total_cost / slot.shares
    
    pool.current_cash += shares * fill_price  # 卖空收 proceeds
```

### 4.3 CLOSE_LONG / CLOSE_SHORT（减仓）

```python
def apply_close(slot, pool, signal, fill_price, direction):
    # 计算 shares to close
    if isinstance(signal.size, FixedSize):
        shares_to_close = signal.size.shares
    elif isinstance(signal.size, PctSize):
        shares_to_close = slot.shares * signal.size.pct
    
    # Q2: close > current → close all + WARN
    if shares_to_close > slot.shares:
        WARN f"close requested {shares_to_close} shares, only {slot.shares} available, closing all"
        shares_to_close = slot.shares
    
    # 减仓 + realized PnL
    if direction == Direction.CLOSE_LONG:
        realized_pnl = (fill_price - slot.avg_cost) * shares_to_close
        pool.current_cash += shares_to_close * fill_price  # 卖股收 cash
    elif direction == Direction.CLOSE_SHORT:
        realized_pnl = (slot.avg_cost - fill_price) * shares_to_close
        pool.current_cash -= shares_to_close * fill_price  # 买回归还
    
    slot.realized_pnl += realized_pnl
    pool.total_realized_pnl += realized_pnl
    slot.shares -= shares_to_close
    
    if slot.shares == 0:
        slot.avg_cost = 0.0  # reset
```

### 4.4 FLAT Signal（关闭 strategy 所有持仓）

```python
def apply_flat(strategy_id, broker_state, fill_price_lookup, bar_time):
    for slot in broker_state.positions.values():
        if slot.strategy_id != strategy_id or slot.shares == 0:
            continue
        # 等效 CLOSE_*(slot.shares)
        if slot.direction == StrategyType.LONG:
            close_signal = Signal(
                direction=Direction.CLOSE_LONG,
                price=MarketOrder(),
                size=FixedSize(shares=slot.shares),
                bar_time=bar_time,
            )
            apply_close(slot, broker_state.pools[strategy_id], close_signal, fill_price_lookup(slot.symbol, bar_time), Direction.CLOSE_LONG)
        elif slot.direction == StrategyType.SHORT:
            close_signal = Signal(
                direction=Direction.CLOSE_SHORT,
                price=MarketOrder(),
                size=FixedSize(shares=slot.shares),
                bar_time=bar_time,
            )
            apply_close(slot, broker_state.pools[strategy_id], close_signal, fill_price_lookup(slot.symbol, bar_time), Direction.CLOSE_SHORT)
```

**FLAT 局限 strategy 作用域**：LONG strategy 的 FLAT 只关该 strategy 的所有多单，不动 SHORT strategy。

### 4.5 Cash flow 速查表

| Signal | pool cash 变化 | slot.shares 变化 | realized_pnl 变化 |
|---|---|---|---|
| `LONG` | `− shares × fill_price` | `+ shares` | 0 |
| `SHORT` | `+ shares × fill_price` | `+ shares` | 0 |
| `CLOSE_LONG` | `+ shares × fill_price` | `− shares` | `+ (fill - avg_cost) × shares` |
| `CLOSE_SHORT` | `− shares × fill_price` | `− shares` | `+ (avg_cost - fill) × shares` |
| `FLAT` | 等效关所有 | 等效关所有 | 等效关所有 |

---

## 5. Q1-Q4 实现要点

### 5.1 Q1 加权平均 cost

```python
new_total_cost = slot.shares * slot.avg_cost + shares * fill_price
slot.shares += shares
slot.avg_cost = new_total_cost / slot.shares
```

**例**：100 股 @ $50 + 50 股 @ $60 → 150 股 @ $53.33。

### 5.2 Q2 close > current → close all + WARN

见 §4.3 注释。**不 raise ValueError**。WARN log 让用户知道。

### 5.3 Q3 独立资金池 per strategy

见 §3.2。每个 strategy 自己的 `CashPool`。**不共享** long_pool / short_pool（不再分）。

### 5.4 Q4 同 bar 多 Signal 顺序应用

```python
def submit(self, strategy_id, signals, bar_time, fill_price_lookup):
    orders = []
    for signal in signals:
        # 每条 Signal 应用前，state 是前一条应用后的结果
        order = self._apply_one(strategy_id, signal, bar_time, fill_price_lookup)
        orders.append(order)
    return orders
```

**WARN 基于当前 state**：如 §4.3，CLOSE_LONG 报 WARN 时用的是 step N-1 之后的 slot.shares，不是初始值。

---

## 6. BacktestBroker（v1 完整实现）

### 6.1 Fill price 语义

**Per 02 §5.1 + G2 exec_lag**：
- Signal 在 bar T emit（bar T START 时）
- broker 在 bar T+exec_lag OPEN 撮合（exec_lag=1 默认，per 00 §6.5 G2）
- fill_price = bar T+exec_lag 的 open price

```python
def submit(self, strategy_id, signals, bar_time, fill_price_lookup, exec_lag=1):
    """bar_time = emit time = bar T START
    fill 实际在 bar T+exec_lag OPEN（per 00 §6.5 G2）"""
    fill_bar_time = bar_time + exec_lag bar  # bar T+exec_lag
    orders = []
    for signal in signals:
        fill_price = fill_price_lookup(signal.symbol, fill_bar_time, field="open")
        order = self._apply_one(strategy_id, signal, fill_price, fill_bar_time)
        orders.append(order)
    return orders
```

### 6.2 状态初始化

```python
class BacktestBroker:
    def __init__(self, strategies: list[StrategyConfig]):
        self.state = BrokerState(
            pools={s.id: CashPool(s.id, s.initial_capital, s.initial_capital) for s in strategies},
            positions={},
            fill_history=[],
        )
```

**注意**：每 strategy 自己 input `initial_capital`（Q3 决定）。

### 6.3 回测结束

```python
def finalize(self):
    """回测结束：所有 open position 按 last bar close mark-to-market。"""
    for slot in self.state.positions.values():
        if slot.shares > 0:
            unrealized = compute_unrealized_pnl(slot, last_close_price)
            log.info(f"[{slot.strategy_id}][{slot.symbol}] unrealized PnL at end: ${unrealized}")
    
    for pool in self.state.pools.values():
        log.info(f"[{pool.strategy_id}] final cash: ${pool.current_cash}, "
                 f"realized PnL: ${pool.total_realized_pnl}, "
                 f"total return: ${(pool.current_cash - pool.initial_capital) + pool.total_realized_pnl}")
```

---

## 7. PaperBroker（v1 live mode 实现）

### 7.1 Fill price 语义

- Live mode `live=True`：每 tick 推送都触发 broker re-evaluate
- MarketOrder：立即按当前 bid/ask 撮合（paper 用 mid price）
- LimitOrder：v1 paper 简化为按当前市价 + WARN（per 05 §3.2）
- partial fill：v1 paper 假设 full fill（无 partial）

### 7.2 与 BacktestBroker 差异

| 维度 | BacktestBroker | PaperBroker |
|---|---|---|
| Fill timing | next bar open（exec_lag）| current tick |
| Fill price | bar open | mid price |
| Partial fill | 无（全 fill 或拒）| 无（v1）|
| Exec lag | 固定 = 1 bar | 0（live 立即）|
| 撮合模拟 | 简化 | 简化（v2 接入真实 order book）|

### 7.3 状态持久化

- BacktestBroker：状态仅在内存，回测结束输出 results
- PaperBroker：**每次 fill 后立即持久化**（live crash-recovery，00 §3.5 G3 同型）

```python
def _persist_state(self):
    """PaperBroker 每次 fill 后写盘。"""
    with open(self.state_path, "wb") as f:
        pickle.dump(self.state, f)
```

---

## 8. RealBroker（v2+ stub）

### 8.1 接口（v2 实现，v1 仅定义）

```python
class RealBroker(BaseBroker):
    """v2+ 真实 broker 接入（IB / Binance / Coinbase 等）。"""
    
    def __init__(self, broker_name: str, credentials: dict):
        self.broker_name = broker_name
        self.api = BrokerFactory.create(broker_name, credentials)
    
    def submit(self, ...):
        # 1. 转换 Signal → 真实 broker order（CCXT / IB API 等）
        # 2. 异步发送，等待 fill
        # 3. fill callback 更新 state
        ...
```

### 8.2 v1 仅 stub

v1 实现 BaseBroker 抽象，但 RealBroker 只暴露 stub（raise NotImplementedError）。**v1 paper broker 就是 RealBroker 的"诚实"版本**。

---

## 9. Order 数据结构

```python
@dataclass
class Order:
    signal_id: str
    strategy_id: str
    symbol: str
    direction: Direction
    size: FixedSize | PctSize | RiskSize   # 原始 size（不一定等于 filled shares）
    status: Literal["FILLED", "REJECTED", "EXPIRED", "PENDING"]
    requested_shares: float                # emit 时请求的 shares
    filled_shares: float = 0.0             # 实际成交（v1 = requested_shares 或 0）
    fill_price: float | None = None
    fill_time: datetime | None = None
    fee: float = 0.0
    slippage: float = 0.0
    rejection_reason: str | None = None    # REJECTED 时填
    rejection_kind: str | None = None      # "INSUFFICIENT_CASH" / "INVALID_SIZE" / "BROKER_ERROR"
```

**v1 paper broker 状态集**：`FILLED` / `REJECTED`（无 `PENDING`，无 `EXPIRED`）。
- `FILLED`：完全成交（v1 假设无 partial）
- `REJECTED`：cash 不够 / 无效 size / 其他 broker 拒绝

**v2 RealBroker**：增加 `PENDING`（挂单中）+ `EXPIRED`（validity 过期）。

---

## 10. P&L 报告

### 10.1 实时查询

```python
broker.get_position("aapl_long", "AAPL")     # → PositionSlot
broker.get_cash("aapl_long")                 # → float
broker.get_realized_pnl("aapl_long")         # → float
```

### 10.2 Unrealized PnL 计算

```python
def unrealized_pnl(slot: PositionSlot, current_price: float) -> float:
    if slot.direction == Direction.LONG:
        return (current_price - slot.avg_cost) * slot.shares
    elif slot.direction == Direction.SHORT:
        return (slot.avg_cost - current_price) * slot.shares
    return 0.0
```

**Backtest**：每 bar close 后计算。
**Live**：每 tick 计算（高频）。

### 10.3 Total account value

```python
def total_value(pool: CashPool, positions: dict[str, PositionSlot], current_prices: dict[str, float]) -> float:
    value = pool.current_cash + pool.total_realized_pnl
    for slot in positions.values():
        if slot.shares > 0:
            value += unrealized_pnl(slot, current_prices[slot.symbol])
    return value
```

---

## 11. TradingView 对齐

| Pine Script | algot broker | 备注 |
|---|---|---|
| `strategy.netprofit` | `pool.total_realized_pnl` | realized only |
| `strategy.equity` | `total_value(pool, ...)` | cash + realized + unrealized |
| `strategy.position_size` | `slot.shares` | v1 单 slot per strategy |
| `strategy.position_avg_price` | `slot.avg_cost` | 加权平均（Q1 一致）|
| `strategy.wintrades / losstrades` | 需 backtest 分析 | v1 不内建 |
| `strategy.max_drawdown` | 需 backtest 分析 | v1 不内建 |
| `strategy.closedtrades.profit` | `slot.realized_pnl` 累计 | v1 简单 sum |
| 实时 order book | PaperBroker v1 简化为 mid price | v2 真实 broker |

---

## 12. v2 / YAGNI

- Multi-leg orders（bracket / OCO）→ v2
- Partial fill simulation → v2（paper 升级 + 真实 broker）
- Margin call 模拟 → v2（v1 假设 unlimited）
- Borrow fee（short 借股成本）→ v2 真实 broker
- Dividend / corporate action 处理 → v2
- Cross-strategy portfolio 协调（aggregate exposure, drawdown limit）→ v2 multi-symbol
- Real-time order book 模拟（slippage by depth）→ v2 paper 升级

---

## 13. 跨文档引用

| 引用 | 关系 |
|---|---|
| 00 §3 G2 | exec_lag = 1 bar（BacktestBroker 实现） |
| 00 §3.5 G3 | stateful plugin 写盘时机（PaperBroker 同型）|
| 00 §3.6 G4 | data stale drop Signal（在 framework 层，broker 收到合法 Signal）|
| 00 §6.3 | v1 paper + backtest；v2+ real |
| 02 §5.1 | bar_time = bar START（fill = bar T+exec_lag OPEN，per 00 §6.5 G2）|
| 03 §3.3 | Signal plugin 返回 Signal（broker 接收）|
| 04 §2 | live priority（影响 broker 何时被调用）|
| 05 §7 | Signal 数据结构 / 校验 / lifecycle |

---

## 14. 版本

- **v0.3**（2026-09-02）：初版。BacktestBroker + PaperBroker + RealBroker stub + PositionSlot + CashPool + Q1-Q4 全部实现。
- Q1 加权平均 cost → §4.1 / §4.2
- Q2 close > current → close all + WARN → §4.3
- Q3 独立资金池 per strategy → §3.2
- Q4 顺序应用 → §5.4
- Position model：per-(strategy, symbol) 单 slot（per William 决定）
- Strategy model：独立 long/short strategy（per William 决定）
- FLAT 局限 strategy 作用域：05 §2.1