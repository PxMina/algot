# 02. Data Layer

**定位**：algot 数据契约层。负责 sqlite → Sequence，是数据流的**起点**（详见 01 §2）。

> **本文档回答**：
> 1. Sequence 怎么实现？索引怎么写？
> 2. 数据源怎么抽象？sqlite 怎么用？
> 3. 多 TF 怎么加载？
> 4. Bar timestamp / session 边界 v1 怎么处理？
> 5. Gap / staleness 数据层 vs engine 层怎么分工？

---

## 1. 数据流位置

```
sqlite (raw bars)
   │
   ▼
[02] data layer  ← 本文档
   │  • Source (sqlite / future)
   │  • Sequence 构造 + 索引语义
   │  • Multi-TF load
   │  • Gap fill
   ▼
Sequence (1D + meta + index)
   │
   ▼
[03] algo layer (factor / signal plugin)
   │
   ▼
[03/04] strategy emit → Signal
   │
   ▼
[05] backtest / broker consume
```

---

## 2. Sequence（具体实现）

### 2.1 数据结构（00 §2 + S3 锁死）

```python
@dataclass
class Sequence:
    data: np.ndarray             # 1D, v1 默认 np.float64
    meta: dict                   # {symbol, timeframe, unit, dtype}
    index: pd.DatetimeIndex | np.ndarray[int64]  # 时间戳 / bar 序号
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data` | `np.ndarray` (1D) | 默认 `np.float64`；plugin 可声明严苛 dtype（00 §6.5）|
| `meta["symbol"]` | `str` | 标的（AAPL / BTCUSDT） |
| `meta["timeframe"]` | `tuple[int, str]` | `(N, unit)` 短长归一化（`"min"` / `"day"`）|
| `meta["unit"]` | `str` | 与 timeframe unit 同（冗余但方便）|
| `meta["dtype"]` | `np.dtype` | 派生自 `data.dtype`（自动同步）|
| `index` | `pd.DatetimeIndex` 或 `np.ndarray[int64]` | bar START 时间戳（详见 §5）|

### 2.1.1 OHLCVSequence（v1 全暴露）

数据库本身存了完整 OHLCV 5 列，v1 全暴露。`OHLCVSequence` 持有 5 个同 meta/index 的 `Sequence` 实例（**不是** `Sequence` 子类，保持 Sequence = 1D 语义不变）：

```python
@dataclass
class OHLCVSequence:
    open: Sequence
    high: Sequence
    low: Sequence
    close: Sequence
    volume: Sequence
    
    @property
    def meta(self) -> dict:
        return self.close.meta        # 5 个 Sequence 共享 meta + index
    
    @property
    def index(self):
        return self.close.index
```

**使用场景**：
```python
bars = source.load_ohlcv("AAPL", tf=(1, "min"))
bars.close[0]   # 当前 close
bars.high[1]    # 1 bar ago high
bars.volume[0]  # 当前 bar 累计 volume
```

**Plugin 签名（按需声明）**：
```python
# close-only 因子（v1 主流）
@algot.plugin(category="factor", shape_in={"close": "Sequence"})
def sma(close, n=20): ...

# OHLCV 因子
@algot.plugin(category="factor", shape_in={"bars": "OHLCVSequence"})
def atr(bars, n=14):
    tr = max(bars.high[0] - bars.low[0],
             abs(bars.high[0] - bars.close[1]),
             abs(bars.low[0] - bars.close[1]))
    ...
```

**Live partial bar 语义**（与 backtest 对齐）：
- `open[0]` = 当前 partial bar 第一个 tick
- `high[0]` = 当前 partial bar 见到过的最高
- `low[0]` = 当前 partial bar 见到过的最低
- `close[0]` = 最新 tick 价格
- `volume[0]` = 累计成交量

### 2.2 索引语义（00 §3.2 + §6.6 锁死）

**约定**：`seq[N]` = **N 步前**，`seq[0]` = 当前 bar。

```python
# 内部存储（oldest → newest）
data = [5.2, 4.09, 3.7, 2.1, 1.05, 0.55]  # 0.55 = 当前

# 用户视角
seq[0]   → 0.55   # 当前
seq[1]   → 1.05   # 1 步前
seq[3]   → 3.7    # 3 步前
seq[0, 3] → [0.55, 1.05, 2.1, 3.7]    # 闭区间, 新→旧
seq[3, 0] → [3.7, 2.1, 1.05, 0.55]    # 闭区间, 旧→新 (A>B 反方向)
```

**v1 禁用负数索引**（00 §6.6，对齐 Pine Script series）：

```python
seq[-1]    → NotImplementedError
seq[0, -1] → NotImplementedError
seq[-5, 3] → NotImplementedError
```

### 2.3 实现要点

`seq[N]` → `data[-(N+1)]`（数组末尾是当前 bar）。

```python
def __getitem__(self, key):
    if isinstance(key, int):
        if key < 0:
            raise NotImplementedError("v1 禁用负数索引 (§6.6)")
        if key >= len(self.data):
            raise IndexError(f"bar_idx {key} out of range (len={len(self.data)})")
        return self.data[-(key + 1)]   # seq[0] = data[-1]
    
    if isinstance(key, tuple) and len(key) == 2:
        start, end = key
        if start < 0 or end < 0:
            raise NotImplementedError(...)
        # 闭区间 [A, B]，方向由 A<B vs A>B
        if start <= end:
            indices = [-(i + 1) for i in range(start, end + 1)]
        else:
            indices = [-(i + 1) for i in range(start, end - 1, -1)]
        return self.data[indices]
    
    raise TypeError(f"unsupported key type: {type(key)}")
```

**性能**：单元素索引 O(1)；切片 O(k)；不要在循环里反复 `seq[0]`。

---

## 3. Source 抽象

### 3.1 BaseSource 接口

```python
class BaseSource(ABC):
    @abstractmethod
    def load(self, symbol: str, tf: tuple[int, str],
             start: datetime | None = None,
             end: datetime | None = None) -> Sequence:
        """Load bars for [start, end] (inclusive both ends).
        
        Args:
            symbol: 'AAPL' / 'BTCUSDT'
            tf: (N, unit), e.g. (1, 'min') / (1, 'day')
            start: inclusive (None = earliest available)
            end: inclusive (None = latest available)
        
        Returns:
            Sequence with data + meta + index
        """
        ...
```

### 3.2 SqliteSource (v1)

**Schema 约定**（v1）：

```sql
CREATE TABLE bars (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,   -- bar START 时间（unix seconds，UTC）
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (symbol, timestamp)
);
CREATE INDEX idx_bars_symbol_time ON bars(symbol, timestamp);
```

**实现要点**：

```python
class SqliteSource(BaseSource):
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")  # 多 reader 不阻 writer
    
    def load(self, symbol, tf, start=None, end=None, field="close") -> Sequence:
        # 1. 解析 tf → seconds（unit 短长归一化）
        unit_seconds = {"s":1, "min":60, "h":3600, "day":86400, "week":604800, "mo":2592000}
        if tf[1] not in unit_seconds:
            raise ValueError(f"unsupported unit: {tf[1]}")
        bar_seconds = tf[0] * unit_seconds[tf[1]]
        
        # 2. SQL 查询
        rows = self.conn.execute(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM bars WHERE symbol=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (symbol, start_ts, end_ts)
        ).fetchall()
        
        # 3. 构造 Sequence（默认 close；其他 field 可选）
        field_idx = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}[field]
        data = np.array([r[field_idx] for r in rows], dtype=np.float64)
        index = pd.DatetimeIndex([pd.Timestamp(r[0], unit='s', tz='UTC') for r in rows])
        meta = {"symbol": symbol, "timeframe": tf, "unit": tf[1], "dtype": np.float64}
        
        return Sequence(data=data, meta=meta, index=index)
    
    def load_ohlcv(self, symbol, tf, start=None, end=None) -> OHLCVSequence:
        """加载完整 OHLCV（数据库本身已有 5 列，v1 全暴露）。"""
        return OHLCVSequence(
            open=self.load(symbol, tf, start, end, field="open"),
            high=self.load(symbol, tf, start, end, field="high"),
            low=self.load(symbol, tf, start, end, field="low"),
            close=self.load(symbol, tf, start, end, field="close"),
            volume=self.load(symbol, tf, start, end, field="volume"),
        )
```

**v1 全暴露 OHLCV**：数据库本身存了 5 列（open/high/low/close/volume），通过 `load_ohlcv()` 返回 `OHLCVSequence`（详见 §3.2.5）；单独字段用 `load(field=...)`。

### 3.3 多 TF 加载

**v1 = 多次 load，每个 TF 一个 Sequence**：

```python
source = SqliteSource(Path("data/algot.db"))
close_1min = source.load("AAPL", tf=(1, "min"), start=datetime(2024, 1, 1))
close_1day = source.load("AAPL", tf=(1, "day"), start=datetime(2024, 1, 1))
```

**v2 可加 joint loader**（§5.4 TBD）：

```python
# v2 (示意)
data = source.load_multi(["AAPL"], tfs=[(1, "min"), (1, "day")], start=...)
# → MultiTFSequence 含 2D data (TF × time)
```

---

## 4. Unit 归一化（与 04 §2.1 对齐）

v1 支持的 unit alias（`tf=(N, unit)` 的 unit 部分）：

| Long | Short | Seconds |
|---|---|---|
| `second` | `s` | 1 |
| `min` | `m` | 60 |
| `hour` | `h` | 3600 |
| `day` | `d` | 86400 |
| `week` | `w` | 604800 |
| `month` | `mo` | 2592000 |

**归一化规则**（与 04 §2.1 一致）：
- `m` ≠ `mo`：minute 用 `m`，month 用 `mo`（避歧义）
- 内部存储用 long form（`min` / `day`）；short 仅在 user API 接受

---

## 5. Bar timestamp 语义（§5.1 TBD → v1 定）

**v1 决定**：bar timestamp = **bar START（open）时间**。

```
14:00 bar  →  start=14:00:00, end=14:01:00
             data covers [14:00:00, 14:01:00)
```

**理由**：
- 与大多数 CEX feed 一致（Binance / Bybit / OKX 都用 open time）
- 与 `resample()` 对齐（04 §2.3 "固定边界 [00:00, 00:05)"）
- 与 Pine Script 默认 `time` 一致（v5）

**实现细节**：
- sqlite 存 unix seconds（UTC，无时区歧义）
- Sequence.index 用 `pd.DatetimeIndex(tz='UTC')` 输出
- live mode 当前 wall-clock 与 bar timestamp 比较时也要转 UTC

**未来兼容**：v2 可加 `bar.end_timestamp` 字段，存 end time（pine `time_close`）；v1 不需要。

---

## 6. Session boundary（§5.2 TBD → v1 YAGNI）

**v1 决定**：**不引入 session 概念**。bar timestamp = wall-clock，24/7 视角。

**理由**：
- v1 主用例 = 加密货币（24/7，session 不适用）
- 股票（09:30-16:00 ET）需要 trading calendar；v1 不做
- resample() 已经在 04 §2.3 决定"固定边界"，session 是另一层

**v2 评估**：
- 引入 `Calendar` 抽象（per-symbol / per-exchange）
- 算法层 `in_session()` helper
- resample() 可选 calendar-aware 模式

---

## 7. Gap 处理（00 §3.6 G4）

**数据层职责**：检测 gap，fill NaN，记录 log。

### 7.1 Backtest（sqlite load）

```python
def load(self, symbol, tf, start, end) -> Sequence:
    rows = self._query_db(symbol, tf, start, end)
    timestamps = [r[0] for r in rows]
    closes = [r[4] for r in rows]
    
    # 检测 gap：相邻 bar 间隔 > 预期
    expected_interval = tf[0] * unit_seconds[tf[1]]
    gaps = []
    for i in range(1, len(timestamps)):
        if timestamps[i] - timestamps[i-1] > expected_interval * 1.5:
            # gap detected
            n_missing = (timestamps[i] - timestamps[i-1]) // expected_interval - 1
            gaps.append((i, n_missing, timestamps[i-1], timestamps[i]))
    
    if gaps:
        # fill with NaN
        new_timestamps = []
        new_closes = []
        for i, row in enumerate(rows):
            if i > 0 and gaps_match(i, gaps):
                # insert NaN bars
                missing_timestamps = generate_missing(timestamps[i-1], timestamps[i], expected_interval)
                new_timestamps.extend(missing_timestamps)
                new_closes.extend([np.nan] * len(missing_timestamps))
                log.info(f"[data gap] {symbol} {tf}: inserted {len(missing_timestamps)} NaN bars "
                         f"between {ts_to_str(timestamps[i-1])} and {ts_to_str(timestamps[i])}")
            new_timestamps.append(timestamps[i])
            new_closes.append(row[4])
        
        return Sequence(np.array(new_closes), meta, pd.DatetimeIndex(new_timestamps))
    
    return Sequence(np.array(closes), meta, pd.DatetimeIndex(timestamps))
```

### 7.2 Live（stream ingest）

Stream ingest 阶段同样的检测：相邻 bar timestamp gap > 1.5x 期望间隔 → NaN fill + INFO log。

### 7.3 数据层 vs engine 层

| 层 | Gap | Staleness |
|---|---|---|
| **data** | 检测 + NaN fill + log | 不管 |
| **engine** | 不再处理（已 NaN）| 检查 threshold + drop signal + log |

---

## 8. Staleness 接口（00 §3.6 G4）

**数据层暴露** `last_bar_time(sym, tf) -> datetime`，**engine 层检查**。

```python
class BaseSource(ABC):
    @abstractmethod
    def last_bar_time(self, symbol: str, tf: tuple[int, str]) -> datetime | None:
        """Live mode: timestamp of most recent bar. None = no data yet."""
        ...
```

**engine 层使用**：

```python
# engine/executor.py
def check_staleness(source, sym, tf, threshold):
    last = source.last_bar_time(sym, tf)
    if last is None:
        return StalenessResult(stale=True, reason="no_data")
    age = (datetime.utcnow() - last).total_seconds()
    if age > threshold:
        log.warn(f"[data stale] {sym} {tf}: last seen {last} (now {datetime.utcnow()}, {age}s ago)")
        return StalenessResult(stale=True, reason="timeout", age=age)
    return StalenessResult(stale=False)
```

**`staleness` 配置**（在 strategy.yaml，详见 00 §3.6）：

```yaml
staleness:
  "1min": 90s
  "5min": 7min
  "1d": 25h
```

**数据层只暴露查询接口，不做 staleness 决策**（决策权在 engine，跟 §4 默认安全原则一致）。

---

## 9. 持久化（data 层负责的）

| 持久化内容 | 谁负责 |
|---|---|
| **Bar data (sqlite)** | data 层（SqliteSource 直接读）|
| **Plugin state** | engine 层（00 §3.5 G3，serial JSON）|
| **Position / account state** | backtest/broker 层 |
| **Time / bar index** | engine 层 |

data 层只管 bar data；plugin state / position state 不在 data 层职责范围。

---

## 10. 与 TradingView 对齐

| TradingView | algot data 层 | 备注 |
|---|---|---|
| `close` series（内置）| `Sequence` (1D np) | 同语义 |
| `time` (bar open time) | `Sequence.index` (pd.DatetimeIndex) | 同语义 |
| `time_close` (bar close time) | （v2 加 `bar.end_timestamp`）| v1 不需要 |
| `request.security(sym, tf)` | `source.load(sym, tf)` | 同步调用 |
| 数据源（feed 选）| `BaseSource` 抽象 | v1 只有 SqliteSource |
| Timezone 处理 | UTC 统一存 / 输出 | TV 用 exchange TZ；algot 用 UTC |

**关键差异**：algot 用 UTC 存储，避免 DST 歧义；TV 跟随 exchange。

---

## 11. 留 v2 / YAGNI

- 多 symbol × multi-TF joint loader（§5.4 TBD）→ 2D Sequence
- Trading calendar / session boundary（§5.2 TBD）→ Calendar 抽象
- `time_close` 字段（end timestamp）→ bar.end_timestamp
- InfluxDB / Parquet / CSV source → BaseSource 子类
- Bar data 增量写入（algot 不负责抓数据，但 v2 可加 ingest CLI）
- 数据源健康度监控（latency / coverage）→ 00 §3.6 留 v2

---

## 12. 跨文档引用

| 引用 | 关系 |
|---|---|
| 00 §2 | Sequence 数据结构（本文档实现） |
| 00 §3.2 | 索引语法（本文档 §2.2 实现） |
| 00 §3.6 G4 | Gap / staleness（本文档 §7 / §8 实现） |
| 00 §6.2 | v1 单 symbol（影响 §3.3 多 TF 加载） |
| 00 §6.6 | 禁用负数索引（本文档 §2.2 实现） |
| 04 §2.1 | Unit alias 表（本文档 §4 复用） |
| 04 §2.3 | Bar boundary 固定边界（本文档 §5 一致） |
| 03 §6 | plugin 怎么 consume Sequence（详见 03-algorithms） |
| 01 §5 | Backtest vs live 模式（本文档职责在 live 分支） |

---

## 13. 版本

- **v0.3**（2026-09-02）：初版。Sequence 实现 / Source 抽象 / Bar 时间语义 / Gap+Staleness 分工。
- §5.1 bar timestamp endpoint → v1 决定为 start time（UTC）。
- §5.2 session boundary → v1 YAGNI。
- §5.4 multi-symbol × multi-TF → v1 YAGNI（v2）。