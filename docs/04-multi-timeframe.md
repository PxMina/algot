# algot — 多 Timeframe 与 live 语义规范

> 状态：草案 v0.1（2026-08-31）
> 上游：`docs/00-vision.md §3`（核心规范）、`docs/00-vision.md §1.2`（信号解耦）
> 下游：`docs/02-data-layer.md`（数据层）、`docs/03-algorithms.md`（算法层）、`docs/05-signals.md`（信号抽象，待写）

---

## 1. 单 TF 内的偏移语义

承接 `00-vision.md §3.1`，单 TF 上下文天然无歧义：

| Timeframe | `seq[3]` 含义 |
|---|---|
| 1min | 3 分钟前 |
| 5min | 15 分钟前 |
| 1h | 3 小时前 |
| 1d | 3 天前 |

**"3 根 bar" 与 "3 小时" 在单 TF 下等价**——这是 1:1 巧合，不是定义上的"按时间"。

---

## 2. 跨 TF 引用：`resample()` 函数

### 2.1 签名

```
resample(seq, N, unit, *, agg="default", live=None)
```

- `seq`：底层序列（必须来自更细粒度 TF）
- `N, unit`：目标 TF = N × unit
- 支持的 `unit`（短长共存，短名无歧义）：
  - `sec` / `s` — 秒
  - `min` / `m` — 分
  - `hour` / `h` — 时
  - `day` / `d` — 日
  - `week` / `w` — 周
  - `month` / `mo` — 月（**注意**：`mo` 而非 `m`，避免与 minute 歧义）
- `agg`：聚合函数（默认 `"default"`；具体规则见 §2.2）
- `live`：是否包含未完成 bar（可选；默认跟随 run-level 全局，见 §3）

### 2.2 聚合规则

- **OHLCV 标准序列**（`open / high / low / close / volume`）：硬编码标准聚合
  - `open` = first
  - `high` = max
  - `low` = min
  - `close` = last
  - `volume` = sum
- **自定义算法序列**：
  - `agg="default"`（默认）→ `last`
  - `agg="sum" / "mean" / "max" / "min" / "first" / ...` → 显式覆盖

### 2.3 时间对齐

**整点对齐**（`[00:00, 00:05)`、`[09:30, 09:35)`、`[00:00, 1d)`），不滚动。理由：跨数据源 / 跨日一致性最好。

### 2.4 方向：v1 只支持升采样

`resample()` 只能从细 TF 聚合到粗 TF（细→粗）。从粗 TF 取细 TF 数据需要 fabricating，v1 **不支持**——降采样调用应直接报错。

### 2.5 组合示例

```
# 5min close 的 20 周期均线
sma(resample(close, 5, "min"), 20)

# 3 天前的 1d daily close
resample(close, 1, "day")[3]

# 价差：当前 close 减 1d daily close
close - resample(close, 1, "day")[0]

# 当日 partial daily close（live=True 显式 opt-in）
resample(close, 1, "day", live=True)[0]

# 自定义指标的跨 TF 聚合：日线 RSI 14 的均值
mean(resample(rsi(close, 14), 1, "day", agg="mean"), 5)
```

---

## 3. live 语义

### 3.1 live 配置来源

```
┌────────────────────────────────────────────┐
│  Run-level 全局默认                          │
│  • algot run            （默认 = closed）    │
│  • algot run --live     （opt-in live）      │
│  • config.yaml: mode: live   （等价写法）     │
├────────────────────────────────────────────┤
│  Per-call override（escape hatch）           │
│  • resample(..., live=True)                 │
│  • resample(..., live=False)                │
└────────────────────────────────────────────┘

> per-TF / per-symbol 不做（YAGNI，未来真需要再加）
```

### 3.2 默认值

- 全局默认 = `closed`（no-look-ahead）
- 理由：
  1. 回测和实盘默认行为一致（`closed` 下两者完全等价）
  2. **回测里 `live=True` 不算 lookahead，但易让策略在 partial bar 上过拟合**（见 §3.4）
  3. `live=True` 是"高级选项"，明确想要早期信号的用户才用

### 3.3 回测硬约束（不变式）

**回测引擎必须保证**：在任意回测 bar `t` 上，`live=True` 序列的"截至时刻"**严格 ≤ `t`**。

- ✅ 正确：在 14:35 的 1min bar 上，`resample(close, 1, "day", live=True)[0]` 拿的是"今天 09:30 ~ 14:35"的 partial daily bar
- ❌ 错误：拿到的是"今天 09:30 ~ 15:00"的完整 daily bar（**这就是 lookahead！**）

**这是实现约束，不是约定**——`02-data-layer.md` / `03-algorithms.md` 必须有断言或单测守住。

### 3.4 已知陷阱：partial bar 过拟合

`live=True` 拿到的 partial bar **信息量弱于 closed bar**（close 不稳定）。在回测里 partial bar 上的策略看似稳定，实盘里：

- 14:35 partial close = X → 买入
- 14:36 partial close = Y → 卖出（如果 Y != X）
- → **信号闪烁**，实盘持续亏钱

docs 必须警告用户：**partial bar 上的策略几乎一定在实盘崩**，要么默认 `closed`，要么有意识地把 live 决策转成 closed 决策（如等下一根 bar close）。

---

## 4. 策略 TF 自动推导

策略不显式声明 TF。引擎按以下规则推导主 TF：

1. 找到所有引用的"基础序列"（未被 `resample` 包装的原始序列）
2. 主 TF = **最细粒度的**基础序列所在的 TF
3. `resample()` 派生序列的 TF 自动高于主 TF

例：
- 策略只引用 `close` → 主 TF = `close` 所在的 TF
- 策略引用 `close` 和 `resample(close, 5, "min")` → 主 TF = `close` 的 TF
- 策略只引用 `resample(close, 5, "min")` → 主 TF = `close` 的 TF（resample 是派生）

---

## 5. 待细化（TBD）

下列细节在 v0.3 之前需要进一步明确：

1. **bar timestamp 端点语义**：14:35 这个 1min bar 的 timestamp 是 `[14:35, 14:36)` 的开始（14:35:00）还是结束（14:36:00）？影响 partial bar 的"截至"边界。
2. **跨日 / 跨周末 / 跨假日的 session 边界**：`resample(close, 1, "day")` 在周一怎么处理？需要交易日历模块。
3. **实盘最后一公里延迟**：daily bar 在收盘后还会有集合竞价、停牌复牌的数据微调。
4. **多 symbol × 多 TF 的联合加载**：当策略同时引用 AAPL 1min 和 AAPL 1d 时，数据层如何 batch 加载？