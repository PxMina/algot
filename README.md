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

## Quick start (M1+)

```bash
pip install -e .[dev]

# write a strategy
cat > my_strategy.py <<'EOF'
import algot
from algot import Sequence, Signal, Direction, MarketOrder, FixedSize

@algot.plugin(category="signal", stateful=True)
def golden_cross(close):
    sma20 = sma(close, 20)[0]
    sma50 = sma(close, 50)[0]
    if state["prev_sma20"] is not None:
        if state["prev_sma20"] < state["prev_sma50"] and sma20 > sma50:
            return Signal(direction=Direction.LONG, price=MarketOrder(),
                          size=FixedSize(shares=100), bar_time=close.index[-1])
    state["prev_sma20"] = sma20
    state["prev_sma50"] = sma50
    return None
EOF

# run backtest (M5+)
algot backtest strategy.yaml

# run live paper mode (M6+)
algot run strategy.yaml
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