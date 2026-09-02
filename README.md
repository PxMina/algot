# algot

**Algorithmic Trading Workbench** — Python function-call strategy framework with backtest + paper live mode.

## Status

**v1 development** — see [PLAN.md](PLAN.md) for milestones, [docs/](docs/) for spec.

## Spec overview

- **Plugin-based**: `@algot.plugin(category="factor" | "signal")` — composable strategy functions
- **Sequence I/O**: bar-indexed sequences (`seq[0]` = current, `seq[N]` = N steps back)
- **Long/short strategy**: direction-typed strategies with independent capital pools
- **Backtest + Live**: same strategy code, swap broker
- **State persistence**: live crash-recovery via pickle

## Quick start

```bash
pip install -e .[dev]
```

### 1. Data — sqlite with one `bars` table

```sql
CREATE TABLE bars (
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,  -- bar START, unix seconds (UTC)
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timestamp)
);
```

### 2. Plugins — your algo/ dir (decorator + functions only, 03 §10.2)

```python
# algos/golden_cross.py — registers `golden_cross` on import
import numpy as np
import algot
from algot import Direction, FixedSize, MarketOrder, Signal
from algot.algo.plugin import plugin

@plugin(category="signal", shape_in={"close": "Sequence[float64]"},
        stateful=True, state_type={"prev": None}, min_bars=20)
def golden_cross(close, state):
    fast, slow = algot.sma(close, n=5), algot.sma(close, n=20)
    f0, s0 = float(fast[0]), float(slow[0])
    bull = not (np.isnan(f0) or np.isnan(s0)) and f0 > s0
    prev, state["prev"] = state["prev"], bull or None
    if prev is None or prev == bull:
        return None
    if bull:  # golden cross → long
        return Signal(symbol=close.meta["symbol"], direction=Direction.LONG,
                      price=MarketOrder(), size=FixedSize(shares=100),
                      bar_time=close.index[-1], tags={"reason": "golden"})
    return Signal(symbol=close.meta["symbol"], direction=Direction.CLOSE_LONG,
                  price=MarketOrder(), size=FixedSize(shares=100),
                  bar_time=close.index[-1], tags={"reason": "death"})
```

### 3. strategy.yaml

```yaml
db: data.sqlite          # sqlite bars
symbol: AAPL             # v1: single symbol
timeframe: 1min            # N+unit; short/long unit names both ok
plugins:
  - algos/golden_cross.py   # file path (relative to this yaml) or module path
strategies:
  - id: gc_long
    type: long               # long | short (direction-typed)
    capital: 100000
    signals: [golden_cross]  # signal plugin names
    exec_lag: 1              # fill at bar T+exec_lag open (G2)
```

### 4. Run

```bash
algot backtest strategy.yaml   # per-strategy summary to stdout
```

## Documentation

- [docs/00-vision.md](docs/00-vision.md) — vision + decisions
- [docs/01-architecture.md](docs/01-architecture.md) — module map
- [docs/02-data-layer.md](docs/02-data-layer.md) — Sequence + sqlite
- [docs/03-algorithms.md](docs/03-algorithms.md) — plugin framework
- [docs/04-multi-timeframe.md](docs/04-multi-timeframe.md) — resample + live priority
- [docs/05-signals.md](docs/05-signals.md) — Signal dataclass
- [docs/06-brokers.md](docs/06-brokers.md) — broker interfaces
- [PLAN.md](PLAN.md) — v1 implementation roadmap (7 milestones)

## License

MIT