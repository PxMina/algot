# algot — v1 开发计划

> **状态**: v0.3 spec 全部 closed（13 个 review issues 已修）。**进入 v1 实现阶段**。
> **owner**: William + Crode
> **last update**: 2026-09-02

---

## 1. 目标（v1 release scope）

可用、可发布的 v1：
- ✅ `algot backtest strategy.yaml` 跑通端到端
- ✅ `algot run strategy.yaml` 跑 paper live mode（带 state 持久化）
- ✅ 13 built-in factors（sma/ema/rsi/atr/adx/stddev/vwap/donchian/crossover/crossunder/resample/shift/OHLCV 5 字段）
- ✅ user-defined plugins（@algot.plugin 装饰器）
- ✅ 多 strategy（long/short direction-typed，独立资金池）
- ✅ Live mode crash-recovery（state pickle 持久化 + 重启 restore）

**不在 v1**（v2+）：真实 broker / multi-symbol / DAG / DSL / GUI / web

---

## 2. Milestones（按依赖顺序）

| M | 名称 | 依赖 | 验证 | 估时 |
|---|---|---|---|---|
| **M1** | 骨架 + Sequence + SqliteSource | — | pytest + load AAPL | 半天 |
| **M2** | Plugin 框架 + sma | M1 | pytest + 注册 sma 跑通 | 半天 |
| **M3** | 13 built-in factors | M2 | pytest + smoke | 1-2 天 |
| **M4** | Signal + BacktestBroker + engine | M3 | pytest + golden_cross 完整回测 | 2-3 天 |
| **M5** | CLI backtest + strategy.yaml | M4 | `algot backtest` 跑通 | 1 天 |
| **M6** | PaperBroker + live state 持久化 | M5 | `algot run` + restart 恢复 | 1 天 |
| **M7** | Examples + README + 收尾 | M6 | clone → install → 跑通 | 1 天 |
| **总计** | | | | **~7-9 天**（单人全力）|

每个 milestone commit + tag（`M1`, `M2`, ...）。M7 后打 `v1.0.0` tag + release。

---

## 3. M1 详细 — 骨架 + Sequence + SqliteSource

### 3.1 目标

最小可 import + Sequence 数据层可用。

### 3.2 文件清单

```
~/algot/
├── pyproject.toml              # package metadata + deps
├── README.md                   # 1 段简介
├── algot/
│   ├── __init__.py             # public API exports
│   ├── sequence.py             # Sequence + OHLCVSequence (02 §2 + §2.1.1)
│   └── source/
│       ├── __init__.py
│       ├── base.py             # BaseSource ABC (02 §3.1)
│       └── sqlite.py           # SqliteSource impl (02 §3.2)
└── tests/
    ├── conftest.py             # pytest fixture: 生成 100-bar mock sqlite (in-memory 或 tmp)
    ├── test_sequence.py        # index semantics (02 §2.2 / 00 §3.2 / 00 §6.6)
    └── test_source_sqlite.py   # load roundtrip + gap fill (02 §3.2 / 02 §7)
```

> **数据路径由用户配置**（per William 反馈）：algot 包**不打包 sample data**；用户自带 sqlite，通过 `strategy.yaml` 的 `data.path` 字段指定（详 M5）。
> pytest 自己生成 in-memory sqlite fixture，不依赖外部文件。

### 3.3 关键代码要点

**`algot/sequence.py`** (per 02 §2 + §2.1.1 + §2.2 + §2.3):
```python
@dataclass
class Sequence:
    data: np.ndarray           # 1D, np.float64 default
    meta: dict                 # {symbol, timeframe, unit, dtype}
    index: pd.DatetimeIndex | np.ndarray[int64]
    
    def __getitem__(self, key): ...   # seq[N], seq[A,B], NotImplementedError on negative

@dataclass
class OHLCVSequence:
    open: Sequence
    high: Sequence
    low: Sequence
    close: Sequence
    volume: Sequence
```

**`algot/source/sqlite.py`** (per 02 §3.2 + §4 + §7 + §8):
```python
class SqliteSource(BaseSource):
    def load(self, symbol, tf, start=None, end=None, field="close") -> Sequence: ...
    def load_ohlcv(self, symbol, tf, start=None, end=None) -> OHLCVSequence: ...
    def last_bar_time(self, symbol, tf) -> datetime | None: ...
    # 内部: unit alias 归一化, NaN fill on gap, INFO log
```

### 3.4 验证

```bash
cd ~/algot
pip install -e .[dev]
pytest tests/test_sequence.py tests/test_source_sqlite.py -v

# smoke (用 placeholder path，用户填自己 sqlite 路径)
python -c "
from algot import Sequence, OHLCVSequence
from algot.source import SqliteSource
# ↓ 用户填实际路径 (详 M5 strategy.yaml data.path)
src = SqliteSource('<USER_DB_PATH>')
bars = src.load_ohlcv('AAPL', (1, 'min'))
print(f'loaded {len(bars.close)} bars')
print(f'current close: {bars.close[0]}')
print(f'5 bars ago high: {bars.high[5]}')
print(f'slice [0, 3]: {bars.close[0, 3]}')
print(f'negative index raises:')
try: bars.close[-1]
except NotImplementedError as e: print(f'  OK: {e}')
"
```

### 3.5 风险

- **macOS arm64** + numpy/pandas 兼容性：本机已验证（`~/qlibex` 用同样的栈）
- **DB schema 不匹配**：用户 sqlite 可能用不同 schema。M1 SqliteSource 实现必须**先 detect schema**，缺列时报清晰错误（不静默 fallback）。详见 02 §3.2。

---

## 4. M2 详细 — Plugin 框架 + sma

### 4.1 目标

Plugin 注册 / 调用 / 装饰器可用，1 个 factor (sma) 跑通。

### 4.2 文件清单

```
algot/
├── algo/
│   ├── __init__.py
│   ├── plugin.py             # @algot.plugin decorator + _REGISTRY (03 §2 + §4)
│   ├── contract.py           # dtype/shape validator (03 §6)
│   └── builtins/
│       ├── __init__.py
│       └── factor.py         # sma (03 §11.1 + 03 §3.2)
└── tests/
    ├── test_plugin.py        # decorator + contract violation
    └── test_factors.py       # sma accuracy + warmup NaN
```

### 4.3 关键代码要点

**`algot/algo/plugin.py`** (per 03 §2 + §4 + §6 + §8):
```python
_REGISTRY: dict[str, PluginMeta] = {}

def plugin(category, *, shape_in=None, shape_out=None, pure=True, deps=None,
           version="0.1.0", min_bars=0, stateful=False, state_type=dict,
           state_scope="global"):
    def decorator(func):
        # 校验 category / shape_in / shape_out / min_bars / state_type
        # 注册到 _REGISTRY
        # 返回 func（v1 不包装，state 由 framework 注入）
        return func
    return decorator

def register(func, **kwargs): ...        # 手动注册
def get_plugin(name) -> PluginMeta: ...  # 查询
def list_plugins(category=None): ...
```

**`algot/algo/contract.py`** (per 03 §6):
```python
TYPE_WHITELIST = {"int", "float", "str", "bool", "datetime",
                  "Sequence", "Sequence[float64]", "Sequence[float32]",
                  "OHLCVSequence", "ndarray", "Signal", "None"}

def validate_shape_in(args, shape_in): ...  # 实参类型 vs shape_in
def validate_shape_out(return_value, shape_out): ...
```

**`algot/algo/builtins/factor.py`** (per 03 §3.2 + §11.1):
```python
@algot.plugin(category="factor",
              shape_in={"close": "Sequence[float64]", "n": "int"},
              shape_out="Sequence[float64]",
              min_bars=20)
def sma(close, n=20):
    data = np.full(len(close), np.nan)
    for i in range(n - 1, len(close)):
        data[i] = np.nanmean(close.data[i - n + 1:i + 1])
    return Sequence(data=data, meta=close.meta, index=close.index)
```

### 4.4 验证

```python
from algot.algo.plugin import get_plugin, list_plugins
from algot.algo.builtins.factor import sma

# 注册验证
assert "sma" in [p.name for p in list_plugins(category="factor")]
assert get_plugin("sma").min_bars == 20

# 跑通验证
close = source.load("AAPL", (1, "min"))
out = sma(close, 20)
assert len(out) == len(close)
assert np.isnan(out[19])     # warmup
assert out[20] == pytest.approx(close[0:20].mean())

# Contract violation
@algot.plugin(category="factor", shape_in={"x": "Sequence"})
def bad(x): return x
try:
    bad(close="not a sequence")  # 期望 raise TypeError at call
except TypeError: pass
```

---

## 5. M3 详细 — 13 built-in factors

### 5.1 目标

补完 `algot/algo/builtins/factor.py`（per 03 §11.1）。

### 5.2 Factor 清单（13 个）

| Factor | 复杂度 | 测试要点 |
|---|---|---|
| `sma` | 简单 | 边界值 + NaN 传播 |
| `ema` | 中 | alpha = 2/(N+1)；初始值用 SMA |
| `rsi` | 中 | overbought/oversold 阈值 |
| `atr` | 中 | Wilder smoothing vs SMA 区分 |
| `adx` | 高 | 多步：TR / +DM / -DM / DX / ADX |
| `stddev` | 简单 | population vs sample 选项 |
| `vwap` | 中 | cumulative (price * volume) / cumulative volume |
| `donchian_high/low` | 简单 | rolling max/min |
| `crossover/crossunder` | 简单 | 比较 a[0] vs b[0], a[1] vs b[1] |
| `resample` | 高 | 跨 TF 聚合 + live priority（04 §2）|
| `shift` | 简单 | 序列位移 + NaN fill at head |

### 5.3 验证

```bash
pytest tests/test_factors.py -v
# 每个 factor 至少 1 个 round-trip
# resample: 1min → 5min close 检查
# atr/adx: 给定 fixture OHLCV, 检查 vs talib 或手算 reference
```

### 5.4 风险

- **resample live priority**：4 级优先级（per-call > per-TF > run-level > closed），需单元测试覆盖每层
- **adx 实现复杂度**：考虑用现有 numpy 实现，先不强求 talib

---

## 6. M4 详细 — Signal + BacktestBroker + Engine

### 6.1 目标

完整信号 → 撮合 → PnL 链路。

### 6.2 文件清单

```
algot/
├── signal.py                       # Direction + Signal + Price/Size (05 §2-7)
├── broker/
│   ├── __init__.py
│   ├── base.py                     # BaseBroker + StrategyType + Order (06 §2 + §9)
│   └── backtest.py                 # BacktestBroker full (06 §6)
└── engine/
    ├── __init__.py
    └── executor.py                 # per-bar loop + plugin store (03 §10)
└── tests/
    ├── test_signal.py              # __post_init__ 校验 + Direction semantics (05 §7)
    ├── test_broker.py              # Q1-Q4 + same-bar order (06 §4-5)
    └── test_integration.py         # 端到端 smoke
```

### 6.3 关键设计

**`algot/signal.py`** (per 05 §7):
```python
class Direction(str, Enum):
    LONG = "long"; SHORT = "short"; FLAT = "flat"
    CLOSE_LONG = "close_long"; CLOSE_SHORT = "close_short"

@dataclass
class MarketOrder: pass
@dataclass
class LimitOrder:
    price: float
@dataclass
class LimitRange:
    min_price: float
    max_price: float

@dataclass
class FixedSize:
    shares: float
@dataclass
class PctSize:
    pct: float
@dataclass
class RiskSize:
    risk_amount: float
    stop_loss: float

@dataclass
class Signal:
    direction: Direction
    price: "MarketOrder | LimitOrder | LimitRange"
    size: "FixedSize | PctSize | RiskSize"
    bar_time: datetime
    validity: int = 1
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tags: dict = field(default_factory=dict)
    
    def __post_init__(self): self._validate()
    @classmethod
    def flat(cls, bar_time, **kwargs): ...
```

**`algot/broker/base.py`** (per 06 §2 + §9):
```python
class StrategyType(str, Enum):
    LONG = "long"; SHORT = "short"

class BaseBroker(ABC):
    @abstractmethod
    def submit(self, strategy_id, strategy_type, signals, bar_time,
               fill_price_lookup, exec_lag=1) -> list[Order]: ...
    @abstractmethod
    def get_position(self, strategy_id, symbol) -> PositionSlot: ...
    @abstractmethod
    def get_cash(self, strategy_id) -> float: ...
    @abstractmethod
    def get_realized_pnl(self, strategy_id) -> float: ...

@dataclass
class PositionSlot:
    strategy_id: str; symbol: str
    direction: StrategyType
    shares: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0

@dataclass
class CashPool:
    strategy_id: str
    initial_capital: float
    current_cash: float
    total_realized_pnl: float = 0.0

@dataclass
class Order:
    signal_id: str; strategy_id: str; symbol: str
    direction: Direction
    status: Literal["FILLED", "REJECTED", "EXPIRED", "PENDING"]
    requested_shares: float
    filled_shares: float = 0.0
    fill_price: float | None = None
    fill_time: datetime | None = None
    fee: float = 0.0
    slippage: float = 0.0
    rejection_reason: str | None = None
```

**`algot/broker/backtest.py`** (per 06 §6):
- 全部 `apply_*` 函数（06 §4）
- exec_lag 处理（06 §6.1）
- finalize 输出 PnL
- Q1-Q4 全部实现

**`algot/engine/executor.py`** (per 03 §10):
```python
def run_backtest(strategy, source, start, end, broker):
    """per 03 §10 per-bar loop."""
    for bar in bars:
        # 1. 推 bar (data layer)
        # 2. 调 factor plugins → 缓存
        # 3. 调 signal plugins → emit Signal
        # 4. framework drop warmup (G1)
        # 5. broker.submit(signals)
        # 6. 更新 portfolio state
    broker.finalize()
```

### 6.4 验证

```python
# test_signal.py
Signal(direction="not_enum", ...)  # raise ValueError
Signal(direction=Direction.LONG, price=LimitOrder(price=-1), ...)  # raise ValueError
Signal.flat(bar_time=now)  # OK

# test_broker.py
# Q1: 加权平均 cost (06 §4.1)
# Q2: close > current → close all + WARN (06 §4.3)
# Q3: 独立资金池 (06 §3.2)
# Q4: 同 bar 多 Signal 顺序应用 (06 §5.4)

# test_integration.py
# golden_cross strategy: AAPL 1min backtest → trades > 0, sharpe is float
```

### 6.5 风险

- **Engine 与 plugin store 集成**：factor 输出如何注入 signal 的 deps？03 §10.2 / §10.0
- **Broker state 序列化**（for M6）：暂用 dataclass + pickle，v1.x 优化

---

## 7. M5 详细 — CLI backtest + strategy.yaml

### 7.1 目标

`algot backtest examples/golden_cross.yaml` 跑通。

### 7.2 文件清单

```
algot/
├── config.py                       # strategy.yaml schema (pydantic)
└── cli/
    ├── __init__.py
    └── main.py                     # argparse / click

pyproject.toml:
  [project.scripts]
  algot = "algot.cli.main:cli"

examples/
└── golden_cross.yaml              # sample config
```

### 7.3 strategy.yaml schema

**数据路径由用户在 `data.path` 指定**（per William 反馈，无全局 algot.yaml 配置）：

```yaml
# examples/golden_cross.yaml
data:
  source: sqlite
  path: /Users/william/data/algot.db   # ← 用户 sqlite 绝对路径

strategies:
  - name: aapl_long
    type: long                # ← direction-typed
    initial_capital: 10000
    symbols: [AAPL]
    plugins:
      - name: sma_20
        func: sma
        params: {n: 20}
      - name: sma_50
        func: sma
        params: {n: 50}
      - name: golden_cross
        func: algot.strategies.golden_cross
        deps: [sma_20, sma_50]
        emit:
          direction: long
          price: market
          size: {risk_amount: 100, stop_loss_pct: 0.05}

backtest:
  start: 2024-01-01
  end: 2024-12-31
  timeframe: (1, "min")
  exec_lag: 1

staleness:  # per-TF G4
  "1min": 90s
  "5min": 7min
```

### 7.4 验证

```bash
algot backtest examples/golden_cross.yaml
# stdout:
#   loaded 50000 bars
#   strategy: aapl_long
#   trades: 47
#   realized_pnl: $1234.56
#   unrealized_pnl: $0
#   sharpe: 1.23
#   max_drawdown: -5.6%
```

---

## 8. M6 详细 — PaperBroker + live state 持久化

### 8.1 目标

`algot run strategy.yaml` 跑通 + 中途 kill + 重启 state 恢复。

### 8.2 文件清单

```
algot/
├── broker/paper.py              # PaperBroker full (06 §7)
└── engine/state.py              # state load/save (03 §8.3 + §8.7)

cli/main.py:
  algot run [--reset] strategy.yaml
```

### 8.3 关键代码

**`algot/broker/paper.py`** (per 06 §7):
- 每 tick 撮合（无 exec_lag）
- mid price 填
- state 每次 fill 写盘
- 失败模式 = WARN (同 BacktestBroker)

**`algot/engine/state.py`** (per 03 §8.3 + §8.7):
```python
class StateManager:
    def __init__(self, state_dir=".algot_state"): ...
    def save(self, plugin_name, state): pickle.dump(...)
    def load(self, plugin_name, state_type) -> State: ...
    def restore_all(self, plugins): ...
```

### 8.4 验证

```bash
# T1: 启动
algot run examples/golden_cross.yaml &
sleep 10
kill %1

# T2: 重启，state 应自动恢复
algot run examples/golden_cross.yaml
# log: "[restore] sma_20 state loaded" / "[restore] golden_cross state loaded"

# T3: --reset 强制 fresh
algot run --reset examples/golden_cross.yaml
# log: "[init] sma_20 state fresh"
```

---

## 9. M7 详细 — Examples + README + 收尾

### 9.1 文件清单

```
~/algot/
├── README.md                         # quick start + concept + examples
├── CHANGELOG.md
├── LICENSE
├── examples/
│   ├── golden_cross.py              # user strategy
│   ├── golden_cross.yaml            # backtest config
│   ├── rsi_mean_reversion.py
│   ├── rsi_mean_reversion.yaml
│   └── data/
│       └── AAPL.sqlite              # sample data
└── docs/
    ├── API.md                       # auto-generated via mkdocstrings
    └── QUICKSTART.md
```

### 9.2 README 内容

- 1 段: 项目介绍（"algot = 算法交易工作台，Python 函数式策略，回回回测 + paper live"）
- 安装: `pip install algot`
- 5 行 quick start: load data → define strategy → run backtest
- 1 个完整 golden_cross 示例 + yaml
- spec 文档链接（docs/）
- 贡献 / 测试 / 许可证

### 9.3 发布准备

- pyproject.toml metadata 完整
- GitHub Actions CI: pytest on push
- v1.0.0 tag + GitHub release

---

## 10. 测试策略

| 类型 | 覆盖 | 工具 |
|---|---|---|
| **Unit** | 每个 module 一份 test_*.py | pytest |
| **Integration** | test_integration.py — 端到端最小 | pytest |
| **Smoke** | 每个 M 一个 smoke (load AAPL, run strategy) | pytest + script |
| **Coverage** | > 80% | pytest-cov |

---

## 11. 风险 / YAGNI

### 11.1 风险

| 风险 | 缓解 |
|---|---|
| macOS arm64 + numpy/pandas | 本机已验证（qlibex 同栈）|
| 用户 sqlite schema 不一致 | M1 detect schema + 缺列清晰报错（不静默） |
| adx / resample 复杂度 | 单元测试用 fixture + reference 手算 |
| Stateful plugin 序列化 | dict + dataclass 都行；pickle 暂用 |
| Engine ↔ plugin store 集成 | M4 内分解，03 §10.2 明 |
| DB path 写错 / 文件不存在 | strategy.yaml 启动时 validate + 清晰报错 |

### 11.1.1 配置模型（v1）

- **不打包 sample data**：用户自带 sqlite（per William 反馈）
- **数据路径**：仅 `strategy.yaml` `data.path`（无全局 `~/.algot/config.yaml`）
- **CLI override**：v2+ 加 `--data-path` / `--db` flag（M7 不做）
- **多 strategy 共享 path**：用同一份 `strategy.yaml` 配多个 `strategies:` 条目即可

### 11.2 YAGNI（v1 不做）

- 真实 broker（v2+）
- Multi-symbol（v2+）
- DAG plugin composition（v2+）
- DSL（v2+）
- GUI / 可视化（v2+）
- Web UI / 多用户（never）
- Performance 调优（v1 先 correctness）
- 复杂订单（bracket / OCO / TWAP，v2+）

---

## 12. Spec → Code 映射（实时更新）

| Spec 章节 | 实现位置 | 状态 |
|---|---|---|
| 00 §3.2 Sequence 索引 | `algot/sequence.py` | M1 |
| 00 §3.4 G1 warmup | `algot/algo/plugin.py` + `algot/engine/executor.py` | M2 + M4 |
| 00 §3.5 G3 state | `algot/algo/plugin.py` + `algot/engine/state.py` | M2 + M6 |
| 00 §3.6 G4 data quality | `algot/source/sqlite.py` + `algot/engine/executor.py` | M1 + M4 |
| 02 §2 Sequence | `algot/sequence.py` | M1 |
| 02 §2.1.1 OHLCVSequence | `algot/sequence.py` | M1 |
| 02 §3 Source | `algot/source/*.py` | M1 |
| 02 §4 unit alias | `algot/source/sqlite.py` | M1 |
| 02 §5.1 bar timestamp | `algot/source/sqlite.py` | M1 |
| 02 §7 gap fill | `algot/source/sqlite.py` | M1 |
| 02 §8 staleness interface | `algot/source/sqlite.py` + `algot/engine/executor.py` | M1 + M4 |
| 03 §2 Plugin decorator | `algot/algo/plugin.py` | M2 |
| 03 §4 registration | `algot/algo/plugin.py` | M2 |
| 03 §6 dtype/shape | `algot/algo/contract.py` | M2 |
| 03 §7 NaN vs throw | `algot/algo/plugin.py` | M2 |
| 03 §8 stateful | `algot/algo/plugin.py` + `algot/engine/state.py` | M2 + M6 |
| 03 §9 warmup | `algot/algo/plugin.py` + `algot/engine/executor.py` | M2 + M4 |
| 03 §10 per-bar exec / DAG | `algot/engine/executor.py` | M4 |
| 03 §11 built-ins | `algot/algo/builtins/factor.py` | M3 |
| 04 §2 resample | `algot/algo/builtins/factor.py` | M3 |
| 04 §3 live priority | `algot/algo/builtins/factor.py` | M3 |
| 05 §2 Direction | `algot/signal.py` | M4 |
| 05 §3 Price union | `algot/signal.py` | M4 |
| 05 §4 Size union | `algot/signal.py` | M4 |
| 05 §5 validity | `algot/signal.py` | M4 |
| 05 §7 Signal dataclass | `algot/signal.py` | M4 |
| 05 §8 lifecycle | `algot/engine/executor.py` | M4 |
| 05 §9 per-strategy 约束 | `algot/engine/executor.py` | M4 |
| 05 §10 same-bar 多 Signal | `algot/broker/backtest.py` | M4 |
| 06 §2 BaseBroker + StrategyType | `algot/broker/base.py` | M4 |
| 06 §3 PositionSlot + CashPool | `algot/broker/base.py` | M4 |
| 06 §4 cash flow | `algot/broker/backtest.py` | M4 |
| 06 §6 BacktestBroker | `algot/broker/backtest.py` | M4 |
| 06 §7 PaperBroker | `algot/broker/paper.py` | M6 |
| 06 §9 Order | `algot/broker/base.py` | M4 |
| 06 §10 P&L | `algot/broker/backtest.py` | M4 |

---

## 13. 跟踪与里程碑

- [x] **M1**: 骨架 + Sequence + SqliteSource (commit ca40fb2, tag M1, 2026-09-02)
- [x] **M2**: Plugin 框架 + sma (commit a6a479d, tag M2, 2026-09-02)
- [ ] **M3**: 13 built-in factors
- [ ] **M4**: Signal + BacktestBroker + engine
- [ ] **M5**: CLI backtest + strategy.yaml
- [ ] **M6**: PaperBroker + live state 持久化
- [ ] **M7**: Examples + README + 收尾

每完成一个 milestone:
1. Commit + push
2. Git tag `M1` / `M2` / ...
3. 更新本 PLAN.md 状态（`- [x]` + commit 日期）
4. 写 daily memory

---

## 14. 相关资源

- spec 文档: `docs/00-vision.md` ~ `docs/06-brokers.md`
- qlibex 参考: `~/qlibex/PLAN.md`（同类项目模板）
- 09-vision 已对齐原则: 默认安全 + 灵活逃生口
- MEMORY.md 项目索引: algot 行