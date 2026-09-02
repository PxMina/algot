# 01. Architecture

**定位**：algot 项目骨架 / 模块边界 / 数据流 / 依赖关系。

> **本文档回答 4 个问题**：
> 1. algot 由哪些模块组成？
> 2. 一 bar 从 sqlite 到 Signal 到 broker 走什么路径？
> 3. Plugin 在数据流哪个环节？
> 4. 扩展点在哪？
>
> **本文档不回答**：单 module 内部设计 → 见 02-data-layer / 03-algorithms / 05-signals。

---

## 1. 模块图（来自 00 §4）

```
algot/
├── config/             # YAML 加载 + 校验（strategy.yaml / algot.yaml）
├── data/               # 数据层：sqlite → Sequence
├── algo/               # 算法层：plugin 机制 + 注册表（built-in + user 平铺）
├── strategy/           # 策略层：组合 plugin + 决策 → Signal
├── backtest/           # 回测引擎：Signal + 历史 → PnL/metrics
├── broker/             # 经纪层接口：Signal → 订单（v1 stub / v2 真实接入）
├── engine/             # 计算引擎：bar 推进 + warmup + live 约束 + state 持久化
└── cli/                # 命令行（algot run / algot backtest / algot live）
```

**版本**：v0.3（2026-09-02）。

---

## 2. 数据流（一 bar 全链路）

```
┌─────────┐   ┌──────┐   ┌──────────┐   ┌────────┐   ┌──────────┐   ┌─────────┐   ┌─────────┐
│ sqlite  │──▶│ data │──▶│ Sequence │──▶│ factor │──▶│ Sequence │──▶│ signal  │──▶│ Signal  │
│ (1d/5m) │   │ load │   │ (1D,meta)│   │ (sma)  │   │ (派生)   │   │(wyckoff)│   │ 发出    │
└─────────┘   └──────┘   └──────────┘   └────────┘   └──────────┘   └─────────┘   └─────────┘
                                                                                     │
                                                                                     ▼
                                                                          ┌──────────────────┐
                                                                          │ strategy emit    │
                                                                          │ (组合 + 决策)    │
                                                                          └──────────────────┘
                                                                                     │
                                              ┌──────────────────────────────────────┼────────────┐
                                              │                                      │            │
                                              ▼                                      ▼            ▼
                                       ┌────────────┐                       ┌────────────┐   ┌──────────┐
                                       │  backtest  │                       │   broker   │   │paper     │
                                       │  (历史)    │                       │  (live 真) │   │broker    │
                                       │ → PnL/指标 │                       │  → 订单    │   │(live 测) │
                                       └────────────┘                       └────────────┘   └──────────┘
```

**关键节点**：

| 节点 | 输入 | 输出 | 模块 | spec 引用 |
|---|---|---|---|---|
| **Source** | sqlite 文件 | raw bar 序列 | `data/` | 02 §? |
| **Sequence** | raw bar | 1D np.ndarray + meta | `data/` | 00 §2 |
| **factor plugin** | Sequence | Sequence | `algo/` | 00 §3.4 (warmup) |
| **signal plugin** | Sequence(S) | Signal or None | `algo/` | 00 §3.4 + §6.5 |
| **strategy** | plugin 输出组合 | Signal | `strategy/` | 00 §1.2 (Signal 解耦) |
| **backtest** | Signal + 历史 | PnL / metrics | `backtest/` | 00 §1 |
| **broker** | Signal | 订单（真实/模拟）| `broker/` | 00 §6.3 |

---

## 3. Plugin 在数据流中的位置

**Plugin = 数据流节点，不是装饰**。plugin 接收 Sequence / ndarray，输出 Sequence / Signal。

**Plugin 分类与执行时机**：

| Category | 何时调用 | 输入 | 输出 | v1 |
|---|---|---|---|---|
| `source` | strategy 初始化 | query | Sequence | ✅（隐藏，data 层内部）|
| `factor` | 每 bar | Sequence | Sequence / ndarray | ✅ |
| `signal` | 每 bar | Sequence(S) | Signal or None | ✅ |
| `sizer` | Signal emit 后 | Signal + portfolio | Sized Signal | v1.x |
| `risk` | Signal emit 后 | Signal + positions | Adjusted Signal | v1.x |
| `scheduler` | bar 推进控制 | time | 跳过/继续 | v1.x |
| `executor` | Signal → 订单 | Signal | Order | v2+ |

**Plugin 调用顺序**（每 bar）：

```
1. scheduler 决定本 bar 是否参与计算（如 skip 非交易日）
2. factor 计算 → cache Sequence
3. signal 计算 → emit Signal（warmup 期 framework drop，见 §3.4）
4. sizer/risk（v1.x）→ 调整 Signal
5. strategy emit → 决策层
6. backtest / broker consume Signal
```

---

## 4. 关键架构原则

### 4.1 Signal 解耦（§1.2）

Strategy 只产 Signal，不知道下游是 backtest 还是 broker。

```python
# strategy.py - 用户代码
def my_strategy(close, volume):
    sma20 = sma(close, 20)
    if close[0] > sma20[0] and close[1] <= sma20[1]:
        return Signal(direction=OPEN_LONG, size=100, validity=5)
    return None
```

Strategy **不 import** `backtest` / `broker`；Signal 是唯一契约。

### 4.2 一切皆插件（§6.4）

| 不写死 | 原因 |
|---|---|
| Plugin 内部实现 | numba / 外部进程 / GPU 都可，框架只认 I/O |
| Plugin 注册源 | built-in + user 平铺（无 builtin/contrib 之分） |
| Plugin 数量 | factor/signal 起步，6 类全开后置 |

### 4.3 一切皆 Sequence（§2）

所有 I/O 走 `Sequence(data, meta, index)`：

- raw bar → Sequence
- factor 输出 → Sequence
- 标量值 → Sequence (length=1)

**v1 = 1D only**（§6.2）；2D 多 symbol 留 v2。

### 4.4 默认安全 + 灵活逃生口

| 决策 | 默认 | 逃生口 |
|---|---|---|
| Live partial bar | closed（最安全）| `live=True` / `live_by_tf` |
| Warmup signal | drop | `min_bars=0` |
| Stateful plugin | 无 state | `stateful=True` |
| Data quality | NaN passthrough | 自定义 plugin 抛错 |
| Negative index | raise | （不允许；v1 禁用）|

---

## 5. Execution Mode

### 5.1 Backtest（历史）

```
sqlite → Sequence → plugin → Signal → backtest → PnL/metrics
```

- 数据预加载到内存
- 按 bar 顺序推进
- Signal emit 后立即 simulate 撮合（用 `time + exec_lag` open，§6.5 G2）
- 输出：equity curve, trades, sharpe, drawdown

### 5.2 Live（实时）

```
exchange feed → Sequence (rolling) → plugin → Signal → broker → 订单
                                            │
                                            ├─ live mode 框架（partial bar / staleness / state persist）
                                            └─ paper broker（v1 模拟撮合，真实 broker v2+）
```

| 项 | backtest | live |
|---|---|---|
| 数据来源 | sqlite（预加载）| exchange feed（流式）|
| Bar 推进 | 顺序读 | 时间触发 + 推送 |
| Partial bar | 默认 N/A | §3.1 live 语义 4 级优先级 |
| State | 内存 | §3.5 G3 持久化到 disk |
| Staleness | 不检查 | §3.6 G4 per-TF 阈值 |
| Broker | 模拟撮合 | paper broker（v1）/ 真实（v2+）|

---

## 6. 依赖与扩展

### 6.1 v1 依赖（最小集）

| 包 | 用途 | 必须 |
|---|---|---|
| `numpy` | Sequence.data + 数值计算 | ✅ |
| `pandas` | DatetimeIndex（可选） | ⚠️ |
| `pyyaml` | config 加载 | ✅ |
| `pydantic` | config 校验 | ✅ |
| `sqlite3` | sqlite 读取 | ✅ |
| `pytest` | 测试 | ✅ |

**v1 不引入**：`pandas-ta` / `ta-lib` / `numba` / `ibapi` / `ccxt` / `numpy-financial`。

### 6.2 v2+ 可选依赖

| 包 | 用途 |
|---|---|
| `numba` | factor 加速 |
| `ibapi` | Interactive Brokers |
| `ccxt` | 加密货币交易所 |
| `ta-lib` | 技术指标参考实现 |
| `numpy-financial` | IRR / NPV 等金融函数 |

### 6.3 扩展点

| 想扩展 | 加在哪 | 文档 |
|---|---|---|
| 新指标 | `algo/` 加 plugin + `@algot.plugin` | 03 §? |
| 新 broker | `broker/` 加 `BaseBroker` 子类 | 06 §? |
| 新数据源 | `data/` 加 `BaseSource` 子类 | 02 §? |
| 新 execution 模式 | `engine/` 加 executor | 本 §5 |

---

## 7. 与 TradingView 对齐

| TradingView | algot | 备注 |
|---|---|---|
| Pine Script (DSL) | Python function call | algot v2+ 可加 DSL |
| `indicator()` | `@algot.plugin(category="factor")` | plugin 装饰器 |
| `strategy()` | `@algot.plugin(category="signal")` | 输出 Signal |
| `var` 持久化 | `stateful=True, state={...}` | 00 §3.5 G3 |
| `ta.sma(close, 20)` | `sma(close, 20)` | 同形 |
| `[1]` / `[0]` | `seq[1]` / `seq[0]` | 00 §3.2（禁用负数）|
| `strategy.entry()` | `Signal → broker` | 00 §1.2 |
| `plot()` | （v2 报告/可视化）| v1 不做图表 |

**关键差异**：algot v1 = Python 函数，v2+ = 可选 DSL；TV 始终是 DSL。

---

## 8. 与其他文档关系

```
00-vision.md          ← 顶层（产品定位 / 范围 / 决策）
   │
   ├── 01-architecture.md   ← 本文档（模块图 / 数据流）
   │      │
   │      ├── 02-data-layer.md       （SQLite / Sequence / 数据源）
   │      ├── 03-algorithms.md       （plugin 机制 / 注册表 / 生命周期）
   │      ├── 04-multi-timeframe.md  ✅（已写，resample() / live 语义）
   │      └── 05-signals.md          （Signal 数据结构 / emit / consume）
   │
   └── 06-brokers.md       （broker 接口 / paper / v2+ 真实接入）
```

**当前进度**：

| 文档 | 状态 |
|---|---|
| 00-vision | ✅ v0.3 closed |
| 01-architecture | ✅ 本文 |
| 02-data-layer | ⏳ |
| 03-algorithms | ⏳ |
| 04-multi-timeframe | ✅ v0.2 |
| 05-signals | ⏳ |
| 06-brokers | ⏳ |

---

## 9. 版本

- **v0.3**（2026-09-02）：初版。模块图与 00 §4 对齐；数据流 + execution mode + 扩展点 + 文档索引。