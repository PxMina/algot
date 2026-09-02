# 03. Algorithms & Plugins

**定位**：algot 算法层。Plugin 注册、调用、生命周期、状态管理，是数据流**核心**。

> **本文档回答**：
> 1. Plugin 怎么写 / 怎么注册？
> 2. 7 类 plugin 分别做什么？v1 实现哪些？
> 3. Stateful plugin 怎么持久化？（G3）
> 4. Warmup 怎么处理？（G1）
> 5. dtype / shape contract 怎么校验？（§6.5）
> 6. 错误怎么处理？NaN passthrough vs throw？
> 7. Plugin 在每一 bar 怎么被调用？

---

## 1. 数据流位置

```
Sequence (1D + OHLCVSequence)
   │
   ▼
[03] algo layer  ← 本文档
   │  • Plugin 装饰器 + 注册表
   │  • 7 类 plugin 生命周期
   │  • Stateful / warmup / dtype contract
   ▼
Sequence (factor output) / Signal (signal output)
   │
   ▼
[Strategy emit] → [Backtest / Broker]
```

---

## 2. Plugin 接口

### 2.1 装饰器签名

```python
@algot.plugin(
    category: str,                    # 必填: factor / signal / source / sizer / risk / scheduler
    shape_in: dict[str, str] | None = None,    # 输入契约: {"close": "Sequence", "n": "int"}
    shape_out: str | None = None,     # 输出契约: "Sequence[float64]" / "Signal" / "None"
    pure: bool = True,                # 是否有副作用（False = stateful / I/O）
    deps: list[str] | None = None,    # 依赖的其他 plugin 名（composition 顺序）
    version: str = "0.1.0",           # 插件版本
    min_bars: int = 0,                # G1 warmup 所需最小 bar 数
    stateful: bool = False,           # G3 是否持久化 state
    state_type: type = dict,          # G3 state 容器（dict / dataclass）
)
def my_plugin(...): ...
```

### 2.2 字段约束（来自 00 + 02）

| 字段 | 约束 |
|---|---|
| `category` | 必填；非法值 → `ValueError` |
| `shape_in` | dict 形式 `{参数名: 类型字符串}`；类型白名单（详见 §6）|
| `shape_out` | 同类型白名单；signal 类允许 `"Signal" \| "None"` |
| `pure` | v1 默认 `True`；`False` 必须配合 `stateful=True` |
| `min_bars` | ≥ 0；`< 0` → ValueError |
| `stateful` | `True` 必须声明 `state_type`；否则 → `StatefulPluginMissingFlag` |
| `state_type` | `dict`（默认）/ dataclass / 任意 JSON-serializable |

---

## 3. Plugin 类别（7 类，v1 实现 2 类）

### 3.1 总览

| Category | 作用 | v1 状态 | 输入 | 输出 |
|---|---|---|---|---|
| **factor** | 序列运算（sma / rsi / ...）| ✅ | Sequence / OHLCVSequence | Sequence / ndarray |
| **signal** | 决策信号（开仓 / 平仓）| ✅ | Sequence / OHLCVSequence | Signal \| None |
| source | 数据源适配（sqlite / parquet）| v2 | (symbol, tf, range) | Sequence / OHLCVSequence |
| sizer | 仓位大小（fixed / risk-pct）| v2 | Signal + portfolio state | size (shares / pct) |
| risk | 风控（max-pos / drawdown）| v2 | Signal + portfolio state | bool (pass/fail) |
| scheduler | 调度（cron / event）| v2 | time | Signal |
| (deferred) | 后续按需加 | v2+ | - | - |

**v1 边界**：factor + signal 全实现；其他 4 类留 v2，但 `sizer/risk` 的**部分逻辑**会被 strategy 层 inline 写（详见 05-signals）。

### 3.2 Factor plugin

```python
@algot.plugin(
    category="factor",
    shape_in={"close": "Sequence[float64]", "n": "int"},
    shape_out="Sequence[float64]",
    min_bars=20,    # G1 warmup
)
def sma(close: Sequence, n: int = 20) -> Sequence:
    data = np.full(len(close), np.nan)
    for i in range(n - 1, len(close)):
        data[i] = np.nanmean(close.data[i - n + 1:i + 1])
    return Sequence(
        data=data,
        meta={**close.meta, "dtype": np.float64},
        index=close.index,
    )
```

### 3.3 Signal plugin

```python
@algot.plugin(
    category="signal",
    shape_in={"close": "Sequence[float64]"},
    shape_out="Signal | None",
    stateful=True,
    min_bars=1,
)
def crossover_above(close: Sequence, threshold: float = 100.0, state: dict | None = None) -> Signal | None:
    # state 是框架注入的 kwarg 参数（详见 §8.2）
    if state["prev_close"] is None:
        state["prev_close"] = close[0]
        return None

    crossed = (state["prev_close"] < threshold) and (close[0] >= threshold)
    state["prev_close"] = close[0]

    if crossed:
        # 完整 Signal API 见 05 §7；导入：from algot import Direction, MarketOrder, FixedSize
        return Signal(
            direction=Direction.LONG,
            price=MarketOrder(),
            size=FixedSize(shares=100),
            bar_time=close.index[-1],  # 当前 bar START (02 §5.1)
            validity=1,                 # 单 bar 有效
            tags={"reason": "crossover_above"},
        )
    return None
```

**Signal 数据结构**（详见 05-signals §7，本文档不展开）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `direction` | `Direction` enum | long / short / flat / close_long / close_short |
| `price` | `MarketOrder` \| `LimitOrder` \| `LimitRange` | 市价 / 限价 / 限价区间 |
| `size` | `FixedSize` \| `PctSize` \| `RiskSize` | 固定股数 / 占比 / 风险驱动 |
| `bar_time` | `datetime` | bar START time (UTC) |
| `validity` | `int` | 有效 bar 数（1 = 仅当前 bar；-1 = 永久）|
| `signal_id` | `str` (UUID) | 自动生成 |
| `tags` | `dict` | 用户 metadata |

---

## 4. Registration（注册机制）

### 4.1 三种注册方式

**方式 A：装饰器 + 自动发现（v1 主流）**

```python
# 在 algo/ 目录下任一 .py 文件
import algot
import numpy as np

@algot.plugin(category="factor", shape_in={"x": "Sequence[float64]"}, shape_out="Sequence[float64]")
def sma(x, n=20):
    ...
```

框架启动时扫描 `algo/` 目录（详见 §5），自动 import + 注册。

**方式 B：手动注册**

```python
import algot

def my_signal(close):
    ...

algot.register(my_signal, category="signal", shape_in={"close": "Sequence[float64]"}, ...)
```

**方式 C：库级导入（package author）**

```python
# algot_contrib/my_indicators/__init__.py
from algot import plugin

@plugin(category="factor", ...)
def rsi(x, n=14):
    ...

# user strategy.py
import algot_contrib.my_indicators  # 注册副作用
```

### 4.2 注册表结构

```python
# 内部维护（用户通常不直接读）
_REGISTRY: dict[str, PluginMeta] = {
    "sma": PluginMeta(
        name="sma",
        category="factor",
        func=<function sma>,
        shape_in={"close": "Sequence[float64]", "n": "int"},
        shape_out="Sequence[float64]",
        pure=True,
        stateful=False,
        min_bars=0,
        version="0.1.0",
        deps=[],
    ),
    ...
}
```

**查询**：
```python
algot.get_plugin("sma")           # → PluginMeta
algot.list_plugins(category="factor")  # → [PluginMeta, ...]
```

### 4.3 重复注册 / 命名冲突

```python
# 同名 plugin 第二次注册 → 覆盖前一个（不抛错）
# 不同 category 同名 → 允许（各自 category 维度命名空间独立）
# 启动时重复注册 → INFO log，不抛错（幂等）
```

---

## 5. Discovery（自动发现机制）

### 5.1 扫描规则

v1 启动时扫描 `algo/` 目录（与 `algot.config.strategy_dir` 配置一致）：

```
strategy_dir/
├── my_sma.py              # 每个 .py 文件被 import
├── rsi_cross/
│   └── __init__.py        # 包也支持
└── utils.py               # 普通 helper（非 plugin 函数，不注册）
```

**扫描步骤**：
1. `os.walk(strategy_dir)` 找到所有 `.py` 文件和包
2. `importlib.import_module()` 每个文件/包（**静默 import，不执行用户代码顶层副作用**）
3. 装饰器在 import 时执行，把 plugin 加入 `_REGISTRY`
4. 重复注册幂等（同名覆盖 + INFO log）

### 5.2 顶层副作用警告

```python
# my_strategy.py（坏做法）
print("importing...")   # ← 框架扫到非装饰器顶层调用 → WARN log

@algot.plugin(category="factor", ...)
def sma(close, n=20):
    ...
```

**规则**：plugin 文件**只**放装饰器 + 函数定义。CLI / data 加载等副作用放 strategy.yaml 配置里（详见 01 §1）。

### 5.3 包发现策略

- v1 = **平铺 `algo/`**，无 `builtin/contrib` 两层（00 §6.4 M3 决定）
- v2 可加 `algot_contrib/` 第三方包标准（pip install + auto-discover）

---

## 6. Dtype / Shape Contract（00 §6.5 锁死）

### 6.1 类型白名单

`shape_in` / `shape_out` 支持的类型字符串：

| 类型 | 含义 |
|---|---|
| `"int"` | Python int |
| `"float"` | Python float |
| `"str"` | str |
| `"bool"` | bool |
| `"datetime"` | `datetime.datetime` |
| `"Sequence"` | 默认 np.float64 |
| `"Sequence[float64]"` | 严苛 fp64 |
| `"Sequence[float32]"` | fp32（允许但 plugin 输出通常 fp64）|
| `"OHLCVSequence"` | 5 字段 sequence（02 §2.1.1）|
| `"ndarray"` | np.ndarray (任意 dtype) |
| `"Signal"` | Signal 对象（05-signals）|
| `"None"` | 无输出 / 无输入 |
| `"int \| None"` | union 类型 |

### 6.2 校验时机

| 时机 | 校验内容 | 失败行为 |
|---|---|---|
| Plugin 调用前 | 实参类型 vs `shape_in` | `TypeError` |
| Plugin 调用后 | 返回值类型 vs `shape_out` | `TypeError` |
| 注册时 | `shape_in` / `shape_out` 字符串合法性 | `ValueError` |

**例**：
```python
@algot.plugin(category="factor", shape_in={"close": "Sequence[float64]"}, shape_out="Sequence[float64]")
def sma(close, n=20): ...

algot.run(...)
# sma(close="not a sequence") → TypeError: shape_in violation: 'close' expected Sequence[float64]
```

### 6.3 dtype baseline = fp64

所有 plugin 默认 fp64（除非严苛声明 fp32）。**与 numpy 默认一致**，避免精度漂移。

**v2 可加 tensor / 自定义 dtype**（详见 §12）。

---

## 7. NaN Passthrough vs Throw（§6.5 锁死）

**两条规则并存**：

### 7.1 NaN passthrough（输入侧）

```python
@algot.plugin(category="factor", shape_in={"close": "Sequence[float64]"}, shape_out="Sequence[float64]")
def sma(close, n=20):
    # close.data 可能有 NaN（G4 gap fill 或 G1 warmup）
    # plugin 不需要 if-guard，np.nanmean 自动跳过
    return np.nanmean(close.data[-n:])
```

**机制**：上游 `Sequence.data` 含 NaN（gap / warmup），plugin **不感知**，正常处理（依赖 numpy 自身 NaN-safe 操作）。

**warmup 期间**（bar 0..min_bars-1）：
- 框架在 plugin 调用前把 `close.data[i]` 全置 NaN（i < min_bars）
- plugin 输出也 NaN
- 框架在 strategy emit 时**静默丢弃** warmup-period Signal（00 §3.4 S5）

### 7.2 默认 throw（异常侧）

```python
@algot.plugin(category="factor", shape_in={"close": "Sequence[float64]"}, shape_out="Sequence[float64]")
def divide_by_close(x, close, n=20):
    # 假设计算需要 close != 0
    return x.data / close[0]   # 若 close[0] = 0 → 0 除 → RuntimeWarning
```

**plugin 自己抛的错（不是上游 NaN），框架不吞**：
- `RuntimeError` / `ZeroDivisionError` / 自定义异常 → 向上抛
- 框架**不** try/except 捕获并继续

**理由**（00 §6.5 锁死）："NaN passthrough does not mask data issues"——如果 plugin 计算异常，要让用户看到，不是默默 NaN 继续跑出错的策略。

### 7.3 错误来源分类

| 错误来源 | 行为 |
|---|---|
| 上游 Sequence 含 NaN（G1/G4 注入）| NaN passthrough |
| Plugin 计算异常（自己 raise）| throw |
| Shape contract 违反 | framework raise `TypeError` |
| Stateful plugin state 不 JSON-serializable | framework raise `TypeError` at 注册 |

---

## 8. Stateful Plugin（G3 完整实现）

### 8.1 装饰器声明

```python
@algot.plugin(
    category="signal",
    stateful=True,
    state_type=dict,   # 或 dataclass 类型
    min_bars=10,
    shape_in={"close": "Sequence[float64]"},
    shape_out="Signal | None",
)
def my_signal(close, state):
    sma_val = sma(close, 20)[0]

    if state["last_sma"] is None:
        state["last_sma"] = sma_val
        return None

    crossed = state["last_sma"] < sma_val
    state["last_sma"] = sma_val

    return Signal(...) if crossed else None
```

**关键细节**：
- `state` 是**框架注入**的 kwarg 参数（dict / dataclass 实例），必须出现在函数签名里
- Plugin 函数体通过 `state["key"]` 读写 (dict schema) 或 `state.key` (dataclass 实例经 StatefulState 包装后通过 `state["key"]`)
- 框架在每次 plugin 调用前注入 `state`，调用后读取 `state` 写盘
- **设计变更 (M2 实施后)**: 原 spec 写 "无需参数声明"，但 Python 函数体内访问 `state` 必须通过参数/closure/global。原 v1 采用 kwarg 注入形式 (`def f(x, state):`)；bytecode magic-local 形式可行但脆弱，未采纳。
- 装饰器装饰时校验: stateful=True 必须有 `state` 参数 (否则 ValueError 早爆)

### 8.2 state_type 选项

```python
# dict（默认，最简单）
stateful=True, state_type=dict
# state = {"last_sma": None, "position": 0}

# dataclass（复杂场景：Wyckoff / Kalman / RL）
@dataclass
class MyState:
    last_sma: float | None = None
    position: int = 0
    history: list[float] = field(default_factory=list)

stateful=True, state_type=MyState
# state = MyState()
```

**`make_state_from_schema()` helper**（00 §3.5 锁死）：
```python
state = algot.make_state_from_schema(MyState)
# → MyState() 零初始化（dataclass 默认值）
```

### 8.3 持久化时机

**双触发**（00 §3.5 G3 锁死）：

1. **每 10 bar（默认）** → framework 遍历所有 stateful plugin，写盘
2. **每次 plugin emit Signal 后** → 立即写盘（live crash-recovery）

```python
# 内部实现（伪代码）
for bar in bars:
    stateful_plugins.run(bar)              # 调用 plugin，state 累积
    for plugin in stateful_plugins:
        plugin.state.persist_to_disk()     # 每次调用后立即写（live 模式）
    
    if bar_count % 10 == 0:
        for plugin in stateful_plugins:
            plugin.state.persist_to_disk()  # 每 10 bar 写一次（兜底）
```

**Backtest 模式**：每 10 bar 写盘（emit 后不强制写，因为 backtest 结束后丢弃）。**Live 模式**：emit 后立即写 + 每 10 bar 写。

### 8.7 Live mode 重启 state 加载（M1 明确）

**机制**（live mode 启动时自动执行）：

1. 框架读 `state_dir/`（默认 `./.algot_state/`）下每个 stateful plugin 的 latest pickle
2. 反序列化 → 注入 plugin 下次调用（替换默认 state）
3. framework 从 last persisted bar 继续推进

```python
# 引擎启动伪代码
def startup_restore(self):
    for plugin in self.stateful_plugins:
        state_path = Path(f".algot_state/{plugin.name}.pkl")
        if state_path.exists():
            with open(state_path, "rb") as f:
                saved_state = pickle.load(f)
            plugin.state = saved_state
            log.info(f"[restore] {plugin.name} state loaded, bar_count={saved_state.get('bar_count', '?')}")
        else:
            plugin.state = make_state_from_schema(plugin.state_type)
            log.info(f"[init] {plugin.name} state = {plugin.state_type}() fresh")
```

**CLI 启动选项**：
- `algot run` — 自动 restore（默认）
- `algot run --reset` — 忽略磁盘，state = 新实例（开发 / 重置用）

**失败模式**（safe default + observable，对齐 G4）：
- 损坏 pickle / unpickle 失败 → **WARN + 用默认 state**（不 raise）
- schema 不匹配（旧 plugin version 与新 pickle 不一致） → **WARN + 用默认 state**
- pickle 太旧（跨大版本） → **WARN + 用默认 state**

**Backtest 模式不 restore**：每次 backtest 都是 fresh state（per 06 §3）。

### 8.4 Warmup 期间不写盘

```python
@algot.plugin(category="signal", stateful=True, min_bars=10, ...)
def my_signal(close):
    state["counter"] += 1
    return Signal(...)
```

**warmup 期间**（bar 0..9）：`state["counter"]` 累积，**但不写盘**（避免 warmup 中间态污染）。
**warmup 完成后**（bar 10 起）：开始正常持久化。

**理由**：warmup 期的 state 是"中间态"，崩了重跑也要重 warmup。没必要保存中间结果。

### 8.5 JSON-serializable 要求

```python
# ✅ 允许
state = {"last_sma": 100.5, "position": 0, "trades": [1, 2, 3]}
state = MyDataclass(last_sma=100.5, ...)

# ❌ 拒绝
state = {"lock": threading.Lock()}              # 不可序列化
state = {"fn": lambda x: x}                    # lambda / closure
state = {"conn": sqlite3.Connection("db")}     # 非 JSON
```

**注册时校验**（每个 stateful plugin 跑一次 `json.dumps(default_state)`）：
```python
algot.register(...)  # 框架注入 default_state → json.dumps → 若失败 → ValueError
```

### 8.6 state_scope（v1 placeholder）

```python
@algot.plugin(category="signal", stateful=True, state_scope="global", ...)
# v1 仅支持 "global"（单 symbol 场景）
# v2 支持 "per_symbol"
```

v1 = single symbol 所以 `state_scope="global"` 即可。v2 multi-symbol 时拆 per-symbol dict。

---

## 9. Warmup（G1 完整实现）

### 9.1 装饰器声明

```python
@algot.plugin(category="factor", min_bars=20, ...)
def sma(close, n=20): ...
```

**`min_bars` 语义**：plugin 输出在 `bar_idx < min_bars` 期间为 NaN（已对齐 00 §3.4）。

### 9.2 多 plugin 串联取 max

```python
@algot.plugin(category="factor", min_bars=20)
def sma(close, n=20): ...

@algot.plugin(category="factor", min_bars=50)
def ema(close, n=50): ...

@algot.plugin(category="signal", min_bars=50)  # ← max(20, 50) = 50
def golden_cross(close):
    sma20 = sma(close, 20)
    ema50 = ema(close, 50)
    ...
```

**chained min_bars = max**，框架在调用前计算 dependency graph 求最长依赖链。

### 9.3 框架行为

**Backtest 模式**：
- bar 0..min_bars-1：plugin 输出 NaN（plugin 代码**不感知**，由框架 pre-fill input 实现）
- bar ≥ min_bars：plugin 正常输出
- 回测结束 → INFO log "skipped N bar warmup signals"

**Live 模式**：
- bar 0..min_bars-1：`signal.status = "warmup"`，**不下单**
- bar ≥ min_bars：emit Signal 正常

**机制**：framework 检查 `bar_count < plugin.min_bars` → 静默 drop Signal，plugin 代码无需 if-guard（00 §3.4 S5）。

### 9.4 框架只挡 Signal，factor 输出 NaN 自然传播

```python
# factor 输出 NaN
@algot.plugin(category="factor", min_bars=20)
def sma(close, n=20):
    return np.nanmean(close.data[-n:])  # bar 0..19 → NaN

# signal 消费 NaN 的 sma
@algot.plugin(category="signal", min_bars=20)
def cross_signal(close, n=20):
    sma_val = sma(close, n)[0]
    if np.isnan(sma_val):  # ← 框架不要求 plugin 自己判断，但 plugin 仍可能想判断
        return None
    return Signal(...)
```

**两种机制并存**：
1. **Factor 端**：warmup 期输出 NaN（numpy's `nanmean` 等自动）
2. **Signal 端**：framework drop warmup-period Signal（在 emit 层）
3. **Plugin 端**：可自行 `if np.isnan(...): return None`（额外保护，非强制）

---

## 10. Per-Bar Execution（每 bar 怎么调）

### 10.0 Plugin execution order / DAG

**Plugin 顺序来源**：

1. **DAG from `deps=`**：框架从 `@algot.plugin(deps=[...])` 构建依赖图，topological sort 决定执行顺序
2. **同类内**：factor 按 DAG 顺序；signal 按注册顺序（同 topo level 内）
3. **跨类**：factor 永远先于 signal（signal 通常 deps 指向 factor 输出）

**DAG 构建示例**：
```python
@algot.plugin(category="factor")  # 无 deps
def sma(close, n=20): ...

@algot.plugin(category="signal", deps=["sma"])  # ← 依赖 sma 输出
def golden_cross(close, sma):
    ...
```

框架自动：
1. 解析 `deps` → 构建图
2. topological sort → factor 顺序 `[sma]`，signal 顺序 `[golden_cross]`
3. 每 bar 调用 sma → 缓存输出到 `_store["sma"]` → 调用 `golden_cross(close, sma=...)`

**Plugin 调用时机**（M5 明确）：
- **Backtest 模式**：每 bar **close 后**调用 plugin（plugin 看到的是已完成 bar 的完整数据）
- **Live 模式**：每新 tick 推送（partial bar，per 04 §3.1 live priority）
- Plugin emit Signal 时 `Signal.bar_time` = 当前调用 bar 的 START time（02 §5.1）

**Cycle 处理**：deps 形成环 → **注册时 raise**（plugin 不能形成循环依赖）

**未声明 deps**：plugin 顺序按 registration 顺序（file order）；推荐手动 `@algot.plugin(deps=["plugin_a"], ...)` 强制声明，避免隐式顺序依赖。

### 10.1 顺序

```python
# 每 bar 推进时，框架做的事：
for plugin in registered_plugins:
    if plugin.category == "factor":
        output = plugin(new_bar_input)         # 更新 factor 输出
        store[plugin.name] = output             # 缓存到 plugin store
    
    elif plugin.category == "signal":
        signal = plugin(stored_factors)         # 用最新 factor 输出做决策
        if signal is not None:
            strategy.emit(signal)               # emit Signal
        # framework 检查 bar_idx < plugin.min_bars → drop signal
    
    elif plugin.category in ("source", "sizer", "risk", "scheduler"):
        # v1 占位
        pass
```

**调用顺序保证**：factor 先于 signal（否则 signal 用旧 factor）。

### 10.2 Plugin store（中间状态）

```python
# 框架维护（每个 plugin 一份最新输出）
_store: dict[str, Any] = {
    "sma_20": Sequence(...),    # 最近一次 sma(close, 20) 输出
    "rsi_14": Sequence(...),
    "my_signal_last": Signal(...),
}
```

**Signal plugin 访问 factor 输出**：
```python
@algot.plugin(category="signal", ...)
def my_signal(close):
    sma20 = algot.get_factor("sma_20")   # ← 框架注入 helper
    ...
```

或直接通过依赖注入：
```python
@algot.plugin(category="signal", deps=["sma_20"], ...)
def my_signal(close, sma_20):           # ← deps 自动注入参数
    ...
```

**v1 推荐 deps 注入**（更声明式）。

### 10.3 Backtest vs Live 差异

| 行为 | Backtest | Live |
|---|---|---|
| 调用 plugin | 每 bar 末（bar 关闭后）| 每新 tick 推送（partial bar）|
| Warmup drop Signal | bar 0..N-1 → drop | bar 0..N-1 → drop |
| State 持久化 | 每 10 bar 写 | 每 10 bar + emit 后写 |
| 异常处理 | throw + 终止 | throw + 报警（取决于 broker 配置）|

---

## 11. Built-in Library（v1 自带）

### 11.1 Factor built-ins（v1）

| Plugin | 输入 | 输出 | min_bars |
|---|---|---|---|
| `sma(close, n)` | Sequence[float64], int | Sequence[float64] | n |
| `ema(close, n)` | Sequence[float64], int | Sequence[float64] | n |
| `rsi(close, n=14)` | Sequence[float64], int | Sequence[float64] | n |
| `atr(bars, n=14)` | OHLCVSequence, int | Sequence[float64] | n |
| `adx(bars, n=14)` | OHLCVSequence, int | Sequence[float64] | 2n |
| `stddev(close, n)` | Sequence[float64], int | Sequence[float64] | n |
| `vwap(bars)` | OHLCVSequence | Sequence[float64] | 1 |
| `donchian_high(bars, n)` | OHLCVSequence, int | Sequence[float64] | n |
| `donchian_low(bars, n)` | OHLCVSequence, int | Sequence[float64] | n |
| `crossover(a, b)` | Sequence, Sequence | Sequence[bool] | 1 |
| `crossunder(a, b)` | Sequence, Sequence | Sequence[bool] | 1 |
| `resample(close, n, unit, *, agg, live)` | Sequence, int, str | Sequence[float64] | 0（详见 04）|
| `shift(close, n)` | Sequence[float64], int | Sequence[float64] | n |

**实现位置**：`algot/builtins/factor.py`

### 11.2 Signal built-ins

**v1 不带**。理由：signal 高度依赖策略逻辑（开仓方向 / 平仓条件 / 仓位大小因策略而异）。Framework 提供机制 + 示例；用户写自己的 signal。

**示例**：在 `examples/` 目录放 `golden_cross.py` / `rsi_mean_reversion.py` 等参考实现（不写入 `_REGISTRY`）。

### 11.3 v2 built-ins（计划）

- factor: `macd`, `bollinger`, `keltner`, `ichimoku`, `obv`, `mfi`, `cci`, `stochastic`, `williams_r`
- sizer: `fixed`, `risk_pct`, `kelly`
- risk: `max_position`, `max_drawdown`, `daily_loss_limit`

---

## 12. TradingView 对齐

| Pine Script | algot plugin | 备注 |
|---|---|---|
| `ta.sma(src, len)` | `sma(src, n)` | 同语义，参数名 n / len 差异 |
| `ta.ema(src, len)` | `ema(src, n)` | 同 |
| `ta.rsi(src, len)` | `rsi(src, n)` | 同 |
| `ta.atr(len)` | `atr(bars, n)` | algot 显式传 OHLCVSequence；Pine 自动从内置 series 取 |
| `ta.crossover(a, b)` | `crossover(a, b)` | 同 |
| `ta.crossunder(a, b)` | `crossunder(a, b)` | 同 |
| `ta.vwap()` | `vwap(bars)` | algot 需显式传 bars |
| `ta.rescale(src, ...)` | `resample(...)` | 04 §2 实现 |
| `var float x = na` | `@algot.plugin(stateful=True, state_type=SomeState)` | 显式 dataclass / dict |
| `ta.barssince(condition)` | `barssince(seq)`（v2）| v1 不带 |
| `ta.valuewhen(condition, src, n)` | `valuewhen(...)`（v2）| v1 不带 |

---

## 13. v2 / YAGNI

- 第三方包发现（`algot_contrib/` pip install + auto-discover）
- Stateful plugin `state_scope="per_symbol"`（multi-symbol 准备）
- Plugin 内嵌 numba JIT 装饰（`@algot.plugin(jit=True)`）
- Plugin 性能 profiling hook（每次调用计时）
- Plugin composition DAG（v2 DSL 替代函数调用）
- Tensor dtype（torch / jax 后端）
- Plugin hot reload（修改 .py 自动重新注册）
- async plugin（live I/O 异步）

---

## 14. 跨文档引用

| 引用 | 关系 |
|---|---|
| 00 §3.4 G1 | Warmup / min_bars（本文档 §9 实现） |
| 00 §3.5 G3 | Stateful plugin（本文档 §8 实现） |
| 00 §3.6 G4 | NaN passthrough（本文档 §7.1 实现）|
| 00 §6.4 | Plugin 架构（本文档 §2/§3/§4 实现） |
| 00 §6.5 | 插件 I/O dtype / shape / throw（本文档 §6/§7 实现）|
| 02 §2.1 | Sequence（本文档消费） |
| 02 §2.1.1 | OHLCVSequence（本文档 §3.3 atr/adx 等消费）|
| 04 §2 | resample() 签名（本文档 §11.1 built-in）|
| 05 §7 | Signal 数据结构（本文档 §3.3 引用）|
| 01 §5 | Backtest vs live 执行差异（本文档 §10.3 对齐）|

---

## 15. 版本

- **v0.3**（2026-09-02）：初版。Plugin 装饰器 + 注册表 + 7 类 + stateful + warmup + dtype/shape contract + 12 built-in factor。
- §3 plugin categories：v1 = factor + signal；4 类留 v2
- §11 built-in library：v1 = 13 个 factor + 0 个 signal（示例进 `examples/`）