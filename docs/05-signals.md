# 05. Signals

**定位**：algot 信号层。Strategy 决策 → emit Signal → framework drop/forward → broker 撮合。

> **本文档回答**：
> 1. Signal 长什么样（dataclass）？
> 2. 5 个 direction 各自的语义？
> 3. Price / Size / Validity 怎么表达？
> 4. 校验规则（构造时）？
> 5. Signal 生命周期（emit → drop / 撮合）？
> 6. 同 bar 多 Signal 怎么应用？

---

## 1. 数据流位置

```
Strategy (组合 plugin + 决策)
   │
   ▼
[05] Signal layer  ← 本文档
   │  • Signal 数据结构（5 字段 + 校验）
   │  • Direction 5 状态语义
   │  • 生命周期：emit → drop → broker
   ▼
Signal（不可变 intent）
   │
   ▼
[06] Broker 撮合 → Order（execution）
```

**Signal = intent**（不可变快照）。**Order = execution**（broker 撮合后产物）。两者分开。

---

## 2. Direction enum（5 状态，v1 全实现）

```python
class Direction(str, Enum):
    LONG       = "long"
    SHORT      = "short"
    FLAT       = "flat"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
```

### 2.1 语义矩阵

| 当前 long slot | 当前 short slot | `LONG` | `SHORT` | `FLAT` | `CLOSE_LONG` | `CLOSE_SHORT` |
|---|---|---|---|---|---|---|
| 空 | 空 | 加多 | 加空 | no-op | no-op + WARN | no-op + WARN |
| 有多 | 空 | 加多 | no-op | 平多 | 平多 | no-op |
| 空 | 有空 | no-op | 加空 | 平空 | no-op | 平空 |
| 有多 | 有空 | 加多 | no-op | **平双** | 仅平多 | 仅平空 |

**关键规则**：
- `LONG` / `SHORT` 永远 = 加仓到对应 slot（**不**反向平另一 slot）
- `CLOSE_LONG` / `CLOSE_SHORT` = 减仓对应 slot，无持仓时 no-op + WARN
- `FLAT` = 关闭**当前 strategy 自己的所有持仓**（不是双 slot；FLAT 局限在 strategy 作用域）
- 无 "reverse" 概念。从 net long → net short：emit `CLOSE_LONG` + `SHORT`（两条 Signal）

### 2.2 v1 单仓位 vs 双仓位

**v1 不限制同一 ticker 的 long/short 双开**（per William 决定）。同一 ticker 出现 long + short 双仓位 = 对冲 hedge。

**每个 slot 独立追踪**（canonical 定义见 06 §3.1，本节概要）：
- `shares`: 当前持仓
- `avg_cost`: 加权平均成本
- `realized_pnl`: 已平仓 PnL（累加）
- + `strategy_id` / `symbol` / `direction`（broker 维护，direction 锁 LONG 或 SHORT per StrategyType）

---

## 3. Price（标记 union）

```python
@dataclass
class MarketOrder:
    """市价单。v1 默认。"""
    pass

@dataclass
class LimitOrder:
    """限价单。"""
    price: float

@dataclass
class LimitRange:
    """价格区间单（VWAP / bracket）。"""
    min_price: float
    max_price: float
```

### 3.1 v1 主流 = MarketOrder

```python
Signal(
    direction=LONG,
    price=MarketOrder(),
    size=FixedSize(shares=100),
    bar_time=...
)
```

**v1 paper broker**：MarketOrder 在 next bar open 撮合（per G2 exec_lag=1）。

### 3.2 LimitOrder（v1 支持但 broker 默认拒收）

v1 paper broker 暂不支持限价撮合仿真。**emit `LimitOrder` 时**：broker 简单按 MarketOrder 撮合 + WARN `"limit orders not simulated in v1 paper broker, filled at market"`。

**v2 真实 broker** 完整支持 LimitOrder / LimitRange。

---

## 4. Size（标记 union）

```python
@dataclass
class FixedSize:
    """固定股数。"""
    shares: float

@dataclass
class PctSize:
    """可用资金 / 当前持仓的百分比。"""
    pct: float  # 0-1

@dataclass
class RiskSize:
    """风险金额 + 止损价 → broker 算 shares。"""
    risk_amount: float  # $ 风险金额
    stop_loss: float    # 止损价
```

### 4.1 FixedSize（v1 主流）

```python
FixedSize(shares=100)  # 开 / 平 100 股
```

### 4.2 PctSize（v1 支持）

**语义**取决于 Signal direction：

| Direction | PctSize 语义 |
|---|---|
| `LONG` / `SHORT` | `pct` = 当前 pool cash 的 pct → shares = pool_cash × pct / price |
| `CLOSE_LONG` / `CLOSE_SHORT` | `pct` = 当前持仓的 pct → shares = position_shares × pct |
| `FLAT` | **不允许 PctSize**（必须 `FixedSize(shares=0)` 或 `MarketOrder` 默认全平）|

**例**：
```python
# pool cash = $10000, price = $50
PctSize(pct=0.5)  # LONG: shares = 5000 / 50 = 100
PctSize(pct=0.5)  # CLOSE_LONG: 关闭当前 long 持仓的 50%
```

### 4.3 RiskSize（v1 支持）

```python
RiskSize(risk_amount=100, stop_loss=95)
# entry = $100, stop = $95, risk_per_share = $5
# shares = risk_amount / risk_per_share = 100 / 5 = 20
```

**计算**：broker 在撮合时按 entry price 计算 shares。

**特殊 case**：
- `risk_amount > pool_cash` → WARN + cap to pool_cash（shares 调整）
- `stop_loss` 与 entry 同方向错乱（如 LONG 但 stop_loss > entry）→ `ValueError` at construction

### 4.4 Size 对 FLAT 的约束

```python
Signal(direction=FLAT, price=MarketOrder(), size=FixedSize(shares=0), bar_time=...)
# size 字段 FLAT 不使用，固定传 FixedSize(shares=0) 占位
```

**或** framework 提供 helper：
```python
flat_signal = Signal.flat(bar_time=current_bar_time)
# → Signal(direction=FLAT, price=MarketOrder(), size=FixedSize(shares=0), ...)
```

---

## 5. Validity

```python
@dataclass
class Signal:
    ...
    validity: int = 1
```

| 值 | 语义 |
|---|---|
| `1`（默认）| 仅当前 bar 有效 |
| `N` (N ≥ 1) | N bar 内有效，过期 drop |
| `-1` | 永久有效（直到 filled 或 cancelled）|

**Broker 视角**：validity 决定 order 的 expire_after。

**v1 paper broker**：validity=1 简单（next bar 即撮合）。validity=N / -1 在 v1 partial 实现（broker 内部记 expiry bar）。

---

## 6. signal_id / tags

```python
@dataclass
class Signal:
    ...
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tags: dict = field(default_factory=dict)
```

| 字段 | 用途 |
|---|---|
| `signal_id` | UUID 自动生成；backtest 结果追溯 / log 关联 |
| `tags` | 用户自填 metadata（strategy_id / reason / debug info）|

**v1 默认**：signal_id 自动 UUID，tags={}。用户填 tags：

```python
Signal(
    direction=LONG,
    price=MarketOrder(),
    size=FixedSize(shares=100),
    bar_time=current_time,
    tags={"reason": "golden_cross", "sma20": 105.3, "sma50": 102.1},
)
```

---

## 7. Signal dataclass（完整定义）

```python
@dataclass
class Signal:
    direction: Direction
    price: MarketOrder | LimitOrder | LimitRange
    size: FixedSize | PctSize | RiskSize
    bar_time: datetime
    validity: int = 1
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tags: dict = field(default_factory=dict)
    
    def __post_init__(self):
        self._validate()
    
    def _validate(self):
        # direction
        if not isinstance(self.direction, Direction):
            raise ValueError(f"direction must be Direction enum, got {type(self.direction)}")
        
        # price
        if isinstance(self.price, LimitOrder):
            if self.price.price <= 0:
                raise ValueError(f"LimitOrder.price must be > 0, got {self.price.price}")
        elif isinstance(self.price, LimitRange):
            if self.price.min_price >= self.price.max_price:
                raise ValueError(f"LimitRange: min_price ({self.price.min_price}) must be < max_price ({self.price.max_price})")
        
        # size
        if isinstance(self.size, FixedSize):
            if self.size.shares <= 0:
                raise ValueError(f"FixedSize.shares must be > 0, got {self.size.shares}")
        elif isinstance(self.size, PctSize):
            if not (0 < self.size.pct <= 1):
                raise ValueError(f"PctSize.pct must be in (0, 1], got {self.size.pct}")
            if self.direction == Direction.FLAT:
                raise ValueError("PctSize cannot be used with FLAT (use FixedSize(0) or Signal.flat() helper)")
        elif isinstance(self.size, RiskSize):
            if self.size.risk_amount <= 0:
                raise ValueError(f"RiskSize.risk_amount must be > 0, got {self.size.risk_amount}")
            if self.size.stop_loss <= 0:
                raise ValueError(f"RiskSize.stop_loss must be > 0, got {self.size.stop_loss}")
            if self.direction in (Direction.LONG,) and self.size.stop_loss >= ...:  # placeholder
                raise ValueError("RiskSize.stop_loss must be < entry price for LONG")
            # 类似 SHORT: stop_loss > entry
        elif isinstance(self.size, FixedSize) and self.size.shares == 0 and self.direction != Direction.FLAT:
            raise ValueError("FixedSize(shares=0) only valid with FLAT")
        
        # validity
        if self.validity == 0:
            raise ValueError("validity cannot be 0; use 1 for current bar, -1 for permanent")
    
    @classmethod
    def flat(cls, bar_time: datetime, **kwargs) -> "Signal":
        """Helper for FLAT signal (size 占位)."""
        return cls(
            direction=Direction.FLAT,
            price=MarketOrder(),
            size=FixedSize(shares=0),
            bar_time=bar_time,
            **kwargs,
        )
```

---

## 8. Signal 生命周期

```
plugin.emit(signal)
        │
        ▼
[Framework 拦截]  (per 00 §3.4 / §3.6)
   │
   ├── bar_idx < plugin.min_bars → DROP (G1 warmup, 静默)
   ├── data stale → DROP + WARN (G4)
   ├── Signal 校验失败 → raise (no catch)
   │
        ▼
[Strategy emit 收集]
   │
        ▼
[Broker 接收（按 strategy 作用域）]
   │
   ├── 同 bar 多 Signal? → 按 emit 顺序应用（Q4）
   ├── Cash / position 校验
   ├── WARN 行为（如 close > current）
   │
        ▼
[撮合]
   │
   ├── MarketOrder → next bar open 撮合（G2 exec_lag=1）
   ├── LimitOrder → v1 paper 简化为 market + WARN
   │
        ▼
[Order（execution object，独立于 Signal）]
   │
        ├── fill_price
        ├── fill_time
        ├── fee
        └── slippage
```

**关键**：Signal 在 framework 层**只读不改**，进 broker 后由 broker 维护 in-flight order 状态。

---

## 9. Per-Strategy Emission 约束

**每个 strategy 限定 emit 子集**（per direction type）：

| Strategy type | 允许 emit | 不允许（raise） |
|---|---|---|
| `LONG` strategy | `LONG` / `CLOSE_LONG` / `FLAT` | `SHORT` / `CLOSE_SHORT` |
| `SHORT` strategy | `SHORT` / `CLOSE_SHORT` / `FLAT` | `LONG` / `CLOSE_LONG` |

**框架校验**：strategy emit Signal 时检查 direction 是否合法；非法 → raise `ValueError`（开发期立即暴露 bug）。

**FLAT 在两种 strategy 中都允许**：语义不同——`LONG strategy` 的 FLAT = 关所有多单；`SHORT strategy` 的 FLAT = 关所有空单。

---

## 10. Same-Bar 多 Signal（Q4 决定：顺序应用）

### 10.1 规则

**Broker 按 emit 顺序依次应用，每条 Signal 看到的是前一条应用后的 state。**

### 10.2 例 1：先开再平

```
Initial: long_shares=0, long_pool_cash=$10000, current_bar=T
Emit:
  - LONG 100 @ $50
  - CLOSE_LONG 30 @ $52

Apply:
  Step 1: LONG 100 @ $50
    long_shares = 100
    long_pool_cash = 10000 - 5000 = $5000
    long_avg_cost = $50
  
  Step 2: CLOSE_LONG 30 @ $52
    long_shares = 100 - 30 = 70
    long_pool_cash = 5000 + 30×52 = 5000 + 1560 = $6560
    realized_pnl_long += (52 - 50) × 30 = $60
  
Final: long=70, cash=$6560, realized_pnl=+$60
```

### 10.3 例 2：平仓超量 + 同 bar 加仓

```
Initial: long_shares=50, long_pool_cash=$10000, avg_cost=$40
Emit:
  - LONG 20 @ $50
  - CLOSE_LONG 100 @ $55

Apply:
  Step 1: LONG 20 @ $50
    long_shares = 50 + 20 = 70
    long_avg_cost = (50×40 + 20×50) / 70 = (2000 + 1000) / 70 = $42.86
    long_pool_cash = 10000 - 1000 = $9000
  
  Step 2: CLOSE_LONG 100 @ $55 (only 70 available)
    long_shares = 70 - 70 = 0
    WARN: "CLOSE_LONG requested 100 shares, only 70 closed (long slot)"
    long_pool_cash = 9000 + 70×55 = 9000 + 3850 = $12850
    realized_pnl_long += (55 - 42.86) × 70 = $850.20
  
Final: long=0, cash=$12850, realized_pnl=+$850.20
```

**关键**：WARN 基于"step 1 后的 70 股"（不是"初始 50 股"）。

### 10.4 例 3：FLAT + LONG 同 bar

```
Initial: long_shares=50, cash=$10000
Emit:
  - FLAT (close all)
  - LONG 100 @ $50

Apply:
  Step 1: FLAT
    long_shares = 0
    cash = 10000 + 50×current_price
    realized_pnl_long += ...
  
  Step 2: LONG 100 @ $50
    long_shares = 100
    cash = (10000 + 50×p) - 5000
    avg_cost = $50

Final: long=100, cash as computed
```

**允许**这种 pattern（reset → 重建仓位）。Order matters for cash flow。

---

## 11. Signal → Broker 接口

### 11.1 框架内 broker 接收

```python
class BaseBroker(ABC):
    @abstractmethod
    def submit(self, strategy_id: str, signals: list[Signal], bar_time: datetime) -> list[Order]:
        """接收 strategy emit 的 Signal 列表（已按 emit 顺序），撮合，返回 Order。"""
        ...
```

**返回 Order 列表**：每条 Signal 一个 Order（filled or rejected 都返回）。

### 11.2 Order 数据结构（brief，详细见 06）

```python
@dataclass
class Order:
    signal_id: str
    strategy_id: str
    symbol: str
    direction: Direction
    status: "FILLED" | "REJECTED" | "EXPIRED" | "PENDING"
    fill_price: float | None
    fill_time: datetime | None
    fee: float = 0.0
    slippage: float = 0.0
    rejection_reason: str | None = None
```

**v1 paper broker**：status 只有 FILLED / REJECTED（no PENDING，limit 单 v1 简化为 market）。

---

## 12. Signal 实例参考

```python
# 1. 简单 LONG
Signal(
    direction=Direction.LONG,
    price=MarketOrder(),
    size=FixedSize(shares=100),
    bar_time=current_bar_start,
    tags={"reason": "sma_cross"},
)

# 2. 限价 LONG
Signal(
    direction=Direction.LONG,
    price=LimitOrder(price=105.50),
    size=FixedSize(shares=100),
    bar_time=current_bar_start,
)

# 3. 风险驱动 sizing
Signal(
    direction=Direction.LONG,
    price=MarketOrder(),
    size=RiskSize(risk_amount=200, stop_loss=95.0),  # entry=100, risk=5/share → 40 shares
    bar_time=current_bar_start,
)

# 4. 一键全平
Signal.flat(bar_time=current_bar_start, tags={"reason": "stop_loss_triggered"})

# 5. 平多仓
Signal(
    direction=Direction.CLOSE_LONG,
    price=MarketOrder(),
    size=FixedSize(shares=50),  # 只平 50 股（剩余保留）
    bar_time=current_bar_start,
)
```

---

## 13. TradingView 对齐

| Pine Script | algot Signal | 备注 |
|---|---|---|
| `strategy.entry("L", strategy.long)` | `Signal(direction=LONG, price=MarketOrder(), size=FixedSize(shares=N))` | algot 显式传 size |
| `strategy.entry("S", strategy.short, qty=N)` | `Signal(direction=SHORT, ...)` | 同上 |
| `strategy.close("L")` | `Signal(direction=CLOSE_LONG, size=FixedSize(shares=position_shares))` | algot 显式传当前仓位 |
| `strategy.close_all()` | `Signal.flat(...)` | helper |
| `strategy.exit("L", stop=..., limit=...)` | ❌ v1 不带 | 用两条 Signal（entry + CLOSE_LONG）实现 |
| `strategy.exit("L", qty_percent=50)` | `Signal(direction=CLOSE_LONG, size=PctSize(pct=0.5))` | pct of current position |
| `strategy.risk.max_position_size()` | `Signal(size=RiskSize(...))` | 风险 sizing |
| `strategy.risk.allow_entry_in(strategy.direction)` | framework raise | 不允许 emit 越界 direction |

---

## 14. v2 / YAGNI

- Bracket orders（`Signal.bracket` 字段：SL/TP 绑定 entry）→ v2
- `Signal.parent_id`（entry/exit 关联）→ v2 multi-leg
- Trailing stop / take-profit 复杂订单类型 → v2 真实 broker
- Iceberg / TWAP / VWAP → v2
- `Signal.expires_at`（绝对时间，与 validity 二选一）→ v2
- async / scheduled Signal（cron-style emit）→ v2 scheduler category

---

## 15. 跨文档引用

| 引用 | 关系 |
|---|---|
| 00 §3.4 G1 | Warmup drop Signal（本文档 §8 lifecycle）|
| 00 §3.5 G3 | Stateful plugin emit → state 立即写盘 |
| 00 §3.6 G4 | Stale drop Signal |
| 00 §6.4 plugin architecture | plugin 是 Signal emit 的唯一来源 |
| 00 §6.5 plugin I/O | signal 输出 `Signal \| None` |
| 02 §2.1 | bar_time 取自 Sequence.index[-1]（bar START）|
| 02 §5.1 | UTC bar 时间 |
| 03 §3.3 | Signal plugin 返回类型 |
| 04 §2.1 | live priority（影响 Signal emit 时机）|
| 06 §4 | Signal → Order 撮合（详见 06-brokers §4 cash flow + §9 Order）|

---

## 16. 版本

- **v0.3**（2026-09-02）：初版。Direction 5-state / Price union / Size union / Validity / Lifecycle / Same-bar 顺序应用 / Per-strategy 约束。
- Q1 加权平均 cost → broker 实现（06）
- Q2 close > current → close all + WARN（broker 实现）
- Q3 独立资金池 per strategy → broker 实现（06 §3.2）
- Q4 顺序应用 → broker 实现（06 §5.4）
- Direction 5-state v1 全实现（William 决定）
- OHLCV + Position slot 独立管理（William 决定）
- Strategy model = 独立 long/short 双策略（William 决定）