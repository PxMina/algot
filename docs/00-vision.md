# algot — 项目愿景与核心规范

> 状态：草案 v0.3（2026-09-02）
> 后续文档：`01-architecture.md` / `02-data-layer.md` / `03-algorithms.md` / `04-multi-timeframe.md` / ...

---

## 1. 项目定位

**algot = 算法交易工作台（Algorithmic Trading Workbench）**

覆盖完整的"策略研发 → 历史回测 → 实盘下单"闭环。**实盘下单属于 in-scope，但接口在 v1 就要定义清楚、实现可以晚做**，从而保证策略 / 回测的设计不被未来的实盘需求污染。

- 接入由其他工具抓取好的 OHLCV 等交易数据（sqlite 文件，路径由 config 配置）
- 提供一组可扩充的**算法 / 指标**（库 + 函数），由算法开发者自由组合
- 策略层**只产信号**（signal），不感知下游是回测引擎还是实盘下单
- v1 范围：**策略 + 回测 + 最小插件骨架**（factor / signal 两类 plugin + 元数据契约就够；完整 6 类 plugin 分类留 v1.x；实盘下单的 broker 接口定义好，留 stub / 不接真实券商）

### 1.1 非目标（v1 暂不做）

- ❌ 实时行情抓取（外部工具负责，algot 不抓）
- ❌ 实盘下单的**真实连接**（接口定义在 v1，真实券商对接留到 v2+）
- ❌ 多用户 / 权限 / Web 服务化（v1 是本地工具）
- ❌ 跨市场分布式计算（v1 单机）

### 1.2 关键架构约束：信号解耦

**这是因为"实盘下单会影响策略/回测"这个风险倒推出来的硬约束。**

策略层、回测层、实盘层之间的关系：

```
       策略层
    (只产 Signal)
         ↓
      ┌──┴──┐
      ↓     ↓
  回测层   实盘下单
  (消费 Signal)
```

- **策略**：消费 OHLCV / 指标序列，产出 Signal（symbol / time / direction / price / size 等）
- **回测**：消费 Signal + 历史数据，模拟成交产出 PnL / sharpe / drawdown
- **实盘下单**：消费 Signal，调用券商 API
- 三者对等挂在 Signal 总线上，**策略代码不感知下游是谁**

这条约束意味着 v1 必须先把 Signal 的接口形态定下来——否则后续接实盘时改策略是必然的。Signal 的字段、时序、订阅/分发机制属于 §6.5 待决策。

---

## 2. 核心数据流

```
┌─────────────┐    ┌────────────┐    ┌────────────┐    ┌────────┐
│ 外部抓取工具 │ →  │  数据层    │ →  │  算法层    │ →  │  结果  │
│ (sqlite文件) │    │ (加载/对齐) │    │ (插件式)   │    │ (指标) │
└─────────────┘    └────────────┘    └────────────┘    └────────┘
                       ↑
                  config.yaml
                指定数据目录 + 表名约定
```

**核心原则：**
- 数据层**不感知算法**，只把原始 OHLCV 序列暴露成统一接口
- 算法层**不感知存储**，只接收"对齐好的序列"作为输入
- 算法之间可以互相组合，组合后的结果**也**作为序列参与下一层

**Sequence 数据结构（最小字段，所有算法 I/O 走这个）**：

```
Sequence(data=..., meta={symbol, timeframe, unit, dtype}, index=...)
```

- `data`：一维数值（numpy array 或 pandas Series）
- `meta`：symbol / timeframe / unit / dtype 元信息
- `index`：时间戳 / bar 序号
- v1 一律 1D（单 symbol）；2D 留给多 symbol v2 扩展

所有 Sequence 必须支持 §3.2 索引语法（`seq[N]` / `seq[A, B]`）。

---

## 3. 核心规范：偏移指针（Offset Syntax）

**这是 algot 最重要的设计哲学：所有序列 —— 不管是原始 OHLCV 还是算法输出 —— 都支持同一套访问语法。**

### 3.1 时间步（bar index）

- 所有序列按 **bar** 索引，**0 = 当前 bar**
- `bar[i]` 表示"往前数第 i 根 bar"（即 `i` 步前）
- 在 1min timeframe 下，1 步 = 1 分钟（这是 1:1 巧合；**步 = bar，不是时间**）

### 3.2 索引语法

| 语法 | 含义 | 示例（`open = [5.2, 4.09, 3.7, 2.1, 1.05, 0.55]`，0.55 = 当前） |
|---|---|---|
| `seq[N]` | 标量：第 N 步前的单值 | `open[3]` → `3.7`（3 步前）|
| `seq[A, B]` | 切片：A→B 的子数组，**保留方向** | `open[0, 3]` → `[0.55, 1.05, 2.1, 3.7]`（从新到旧） |
| `seq[A, B]` | 同上，B>A 时反方向 | `open[3, 0]` → `[3.7, 2.1, 1.05, 0.55]`（从旧到新）|

**关键约定：**
- 切片的方向由 `A < B` 还是 `A > B` 决定，**不**额外加 reverse 标记
- 切片**包含两端**（不是 Python 半开区间）—— `[0, 3]` = 4 个元素
- 负数索引？**待定**（见 §6.6）

### 3.3 对算法的要求

任何算法（指标、组合、变换）输出都**必须**是带 bar 概念的序列（或标量），同样支持 §3.2 的语法。

例：
```
sma = sma(close, 20)            # 20 周期均线
sma[3]                          # 3 步前的 SMA 值
diff = close - sma              # 序列对序列运算，结果仍带 bar 概念
diff[5, 10]                     # 5~10 步前的差分数组
```

### 3.4 Plugin warmup / lookback 协议

**对齐 TradingView 默认行为**：silent NaN + 静默跳过暖机期信号。框架兜底，用户无需 `if not na(...)` 检查。

**Plugin 声明**：

```python
@algot.plugin(
    category="factor",
    min_bars=20,         # ← 静态声明，plugin 产生第一个有效输出所需的最少 bar 数
    ...
)
def sma(close, period=20):
    return close.rolling(period).mean()
```

- `min_bars`：静态值（不解析运行时参数）；默认 `min_bars=0`（无前置 bar 需求）

**框架行为**：

| 场景 | 行为 |
|---|---|
| **Backtest** | bar 0 ~ min_bars-1 输出 NaN；bar min_bars 起有效 |
| **Live** | bar_count < min_bars 时 signal 状态 = "warmup"，不发单 |
| **数据不足** | `len(data) < min_bars` → warning + 全 NaN（不 raise，对齐 TradingView） |
| **暖机期 Signal** | 框架隐式吞掉（user 无需 `if not na(signal)`） |
| **多 plugin 串联** | 总 `min_bars` = max(各 plugin min_bars)（框架自动算） |
| **Backtest 完成** | stdout INFO 提示跳过的暖机区间 |

**INFO 日志示例**：
```
[INFO] sma: 跳过前 19 bar 暖机期（min_bars=20）
[INFO] 实际回测区间：2020-01-21 ~ 2024-12-31（vs 数据 2020-01-01 ~ 2024-12-31）
```

**变长参数**（如 `sma(close, period=N)`）：plugin 静态声明 `min_bars` 为推荐默认；运行时 period > min_bars 时 plugin 自负责（`rolling(N).min_periods=N`），框架不解析参数。

**状态机 plugin**（Wyckoff / Kalman）：`min_bars` 同样适用，表示 state 稳定所需 bar 数。协议复用，无新机制。

**对比 TradingView**：

- ✅ silent NaN（不 raise）
- ✅ 暖机期信号静默忽略
- ✅ 不强制最小数据长度
- ➕ INFO 日志增加透明度（TradingView 无）

### 3.5 Plugin state / lifecycle 协议（G3）

**对齐 TradingView `var` 语义**：plugin 通过 `stateful=True` + schema 声明 state；framework 注入 dataclass 实例；plugin 用属性访问（`state.phase = "markup"`、`state.bars += 1`）。

#### 声明示例

```python
@algot.plugin(
    category="signal",
    stateful=True,
    state={"phase": "accumulation", "bars": 0, "range_high": 0.0},  # schema → 自动生成 dataclass
    min_bars=100,                                  # G1 warmup
    state_scope="global",                          # v1 default；v2 用 "per_symbol"
)
def wyckoff_signal(state, close, low, high, volume):
    state.phase = "markup"           # ← 跟 Pine `var phase := "markup"` 语义一致
    state.bars += 1                  # ← 跟 Pine `var bars += 1` 语义一致
    state.range_high = max(state.range_high, high)

    if state.phase == "markup" and close > state.range_high:
        return Signal(direction=OPEN_LONG, ...)
    return None
```

**为什么 dataclass（不 dict）**：
- 类型检查 + IDE 补全
- `dataclasses.asdict()` 直接序列化
- 防止拼写错误（`state.phsae` vs `state.phase`）

#### 框架行为

| 阶段 | 行为 |
|---|---|
| **注册** | `@algot.plugin` 检测 `stateful=True`；`state={}` schema 生成 dataclass class |
| **初始化** | framework `state = State()` 实例 → 注入首个 plugin 调用 |
| **Warmup（bar 0~min_bars-1）** | 调 plugin（state 更新），signal 全 drop（跟 G1 一致） |
| **Active（bar ≥ min_bars）** | plugin 调，signal 走 G2 执行模型（`time + exec_lag` open） |
| **Reset** | `algot run --reset` → state = 新 State() 实例 |
| **Persistence（live only）** | 每 N=10 bar + signal emit 后立即 serialize 到 disk |

#### 失败模式（fail-fast）

- ❌ Plugin 用 `state` 参数但忘加 `stateful=True` → **注册时 raise**
- ❌ Plugin state 不可 JSON 序列化（live mode） → **注册时 raise**

#### 反模式（不阻止，文档警告）

- ⚠️ 模块级 / 全局变量存 state → 框架无法保证一致性；v1 警告文档，v2 静态分析

#### Persistence 边界

| 类型 | 谁管 |
|---|---|
| **Plugin state**（Wyckoff phase / Kaiman params）| framework 序列化 |
| **Position state**（open positions / cash / equity）| backtest / broker 管 |
| **Time / bar index** | framework 单独持久化 |

#### TradingView 对齐

| Pine Script | algot G3 |
|---|---|
| `var string phase = "accumulation"` | `state={"phase": "accumulation"}` schema |
| `phase := "markup"` | `state.phase = "markup"` |
| `bars += 1` | `state.bars += 1` |
| 首次 bar 自动初始化 | framework `state = State()` 注入 |
| Chart reload → state 丢失 | live mode 持久化（algot 增量） |
| 无 framework warmup | `min_bars` 协议（algot 增量） |
| 无 introspection | framework 可 `state.__dict__` dump（algot 增量） |

#### v2 准备

- `state_scope="global"` (v1 default) → 单 state 实例
- `state_scope="per_symbol"` (v2) → state keyed by symbol

---

## 4. 模块划分（粗）

```
algot/
├── config/             # 配置加载（数据目录、表名、timeframe 等）
├── data/               # 数据层：从 sqlite 加载，对齐，暴露统一序列接口
├── algo/               # 算法层：插件机制、统一注册表（内置 + 用户插件平铺，无 builtin/contrib 之分）
├── strategy/           # 策略层：消费序列，产出 Signal
├── backtest/           # 回测层：消费 Signal + 历史数据，模拟成交，产出 PnL / sharpe / drawdown
├── broker/             # 经纪层接口：消费 Signal，调用券商 API（v1 stub / v2 真实接入）
├── engine/             # 计算引擎：按 bar 推进、缓存、live 约束执行
└── cli/                # 命令行入口（v1 形态）
```

> v0.2 起把 `engine/` 拆成 `strategy / backtest / broker` 三个独立模块，跟 §1.2 的信号解耦架构对齐。

详细设计见后续文档。

---

## 5. 关键设计取舍

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据格式 | sqlite 文件 | 与外部抓取工具约定，algot 不负责抓 |
| 算法组合 | 一切皆插件（统一 I/O） | v1 函数调用 + 注册；v2+ DAG 编排 |
| 时间步定义 | bar index 而非秒数 | 多 timeframe 友好 |
| 切片闭合 | 闭区间 `[A, B]` 含两端 | 与用户给出的示例一致 |
| 跨 TF 语法 | `resample()` 聚合函数 | 跟"算法即函数"哲学一致，零新语法 |
| live 模式 | per-call > live_by_tf > run-level > closed 兜底 | 详见 04 §3.1；4 级优先级，默认安全 |
| Signal 执行时机 | bar `time + exec_lag` open，exec_lag ≥ 1（默认 1） | 禁 lookahead；标准 backtest 约定 |
| Stateful plugin | `@algot.plugin(stateful=True, state={...})` schema-driven dataclass | 对齐 TradingView `var`；属性访问（`state.phase = ...`、`state.bars += 1`） |
| 多 TF 详细规范 | 见 `04-multi-timeframe.md` | 单独成 spec |

---

## 6. 待决策（v0.2 → v0.3）

下列问题在 v0.3 设计前需要明确：

1. ✅ **多 timeframe 语义**：**[已定]**，详见 `docs/04-multi-timeframe.md`
   - 单 TF 偏移：`bar[i]` = i 步前
   - 跨 TF：`resample(seq, N, unit)` 聚合函数
   - OHLCV 标准聚合 / 整点对齐 / 升采样 only
   - live 语义：per-call > live_by_tf > run-level > closed 兜底（详见 04 §3.1）

2. **多 symbol 支持**：单次回测是单标的还是要支持股票组合？

3. **实时 vs 历史**：只做历史回测，还是也要支持实时增量 bar 流入？

4. ✅ **算法组合 / 插件架构**：**[已定]** — 一切皆插件，统一 I/O，分阶段组合（2026-09-02）
   - **核心原则**：指标 / Wyckoff / 数据源 / 信号生成 / 仓位 / 风控 / 调度 都按插件实现；框架不关心实现（Python / numba / 外部进程），只认 I/O 约定
   - **v1**：函数调用为主（`sma(close, 20)` 即调 sma 插件）
   - **v1**：用户用 `@algot.plugin(category=..., pure=True, ...)` 注册新插件，可被 DSL 引用
   - **v2+**：图式串联（DAG），节点 = 插件调用，边 = 数据流；DSL 字符串 = DAG 序列化形式（仅内部）

5. **插件 I/O 契约（含 Signal 接口）**（v1 必须先定，否则插件骨架 + §1.2 都落不了）：
   - **插件分类（决定 I/O 约定形态）**：
     - `factor`（series → series）— sma / ema / rsi / 用户自定义指标
     - `signal`（data → Signal）— wyckoff / breakout_detector / 信号生成
     - `source`（query → bars）— yahoo / binance / csv 数据源
     - `sizer`（returns → fraction）— kelly / fixed_frac 仓位
     - `risk`（positions → Signal reduce/close）— stop_loss / max_drawdown
     - `scheduler`（time → bar）— session_calendar
   - **v1 落地范围**：仅 `factor` + `signal` 两类够用；其余 4 类留 v1.x
   - **Plugin 元数据**：`@algot.plugin(category=..., shape_in=..., shape_out=..., pure=True, min_bars=N, deps=[...], version=...)`（warmup 详见 §3.4；stateful lifecycle 详见 §3.5）
   - **Signal 数据结构（字段定义）**：
     ```python
     @dataclass
     class Signal:
         symbol: str              # 标的 ticker（如 'AAPL' / 'BTC/USDT'）
         time: int                # 触发 bar 序号（与 Sequence.index 对齐）
         direction: Direction     # enum: OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT
         price: float | None      # None=市价；数值=限价
         size: float              # 仓位单位数（shares/contracts；单位由 source data 决定）
         expiry: int | None = 0   # 0=仅当根 bar（默认）；N=N bar 后过期；None=永久
         tag: str | None = None   # 可选标签（backtest 分类 / 调试）
         id: str | None = None    # 框架自动注入 UUID（追踪 / cancel）
     ```
   - **执行模型 (G2)**：Signal 消费时机 = bar `Signal.time + strategy.exec_lag` open
     - `strategy.exec_lag` in `strategy.yaml`，默认 = 1
     - 默认值（exec_lag=1）：下 bar open（标准 backtest 约定）
     - `exec_lag >= 2`：下下 bar open 起（防开盘跳空假信号）
     - **`exec_lag < 1` 拒绝（ValueError）**：禁止 lookahead
- **时序约定**：`Signal.time` = 决策 bar 序号（策略决策在 bar `time` close）；**消费时刻 = bar `time + exec_lag` open**（G2 默认下 bar open）
   - **分发机制**：同步回调 / 事件总线
   - **回测 / 实盘挂接点对称 API**
   - **数据类型基线**：numpy / pandas / 自定义 Tensor？
   - **错误传播**：插件 throw vs 返回 NaN？（默认 throw + 数据层 NaN 透传）
   - **shape 兼容**：(a) 单 symbol 1D / (b) 多 symbol 2D 下插件声明接受哪种
   - **API 演进策略**：v1.x 内 additive only（加字段 = 默认值）；v2.0 才允许 breaking

6. **负数索引**：支持 Python 风格 `seq[-1]` 还是禁止？
---

## 7. 文档索引

| 文件 | 内容 |
|---|---|
| `00-vision.md` | 本文件：愿景 + 核心规范 + 待决策清单 |
| `01-architecture.md` | 整体架构图、模块协作、数据 / 控制流（待写）|
| `02-data-layer.md` | sqlite 加载、对齐、序列接口（待写）|
| `03-algorithms.md` | 算法插件机制、注册表、内置库（待写）|
| `04-multi-timeframe.md` | 多 TF + live 语义详细规范（**已写**）|