"""Golden cross reference strategy (per docs/03-algorithms.md §11.2).

NOT auto-registered in the plugin registry — copy into your own algo/ dir
to use.  Shows the full signal-plugin shape: stateful, per-bar invocation,
engine-provided 'close' view (seq[0] = current bar).

Run against an algot SQLite DB:
    python -m examples.golden_cross path/to/data.sqlite

Strategy:
    fast = sma(close, 5); slow = sma(close, 20)
    fast crosses above slow → LONG (FixedSize 100 shares)
    fast crosses below slow → CLOSE_LONG (exit all)
"""

from __future__ import annotations

import sys

import numpy as np

import algot
from algot import (
    BacktestEngine,
    Direction,
    FixedSize,
    MarketOrder,
    Signal,
    Strategy,
    StrategyType,
)
from algot.algo._core import clear_registry
from algot.algo.plugin import plugin
from algot.source.sqlite import SqliteSource


@plugin(
    category="signal",
    shape_in={"close": "Sequence[float64]"},
    shape_out="Signal | None",
    stateful=True,
    state_type={"fast_prev": None, "slow_prev": None},
    min_bars=20,
)
def golden_cross(close, state):
    """LONG on sma5×sma20 golden cross; CLOSE_LONG on death cross."""
    fast = algot.sma(close, n=5)
    slow = algot.sma(close, n=20)
    f0, s0 = float(fast[0]), float(slow[0])
    if len(close) < 2 or np.isnan(f0) or np.isnan(s0):
        state["fast_prev"] = f0 if not np.isnan(f0) else None
        state["slow_prev"] = s0 if not np.isnan(s0) else None
        return None
    fp, sp = state["fast_prev"], state["slow_prev"]
    state["fast_prev"], state["slow_prev"] = f0, s0
    if fp is None or sp is None:
        return None

    bar_time = close.index[-1]
    if fp <= sp and f0 > s0:
        return Signal(
            symbol=close.meta["symbol"],
            direction=Direction.LONG,
            price=MarketOrder(),
            size=FixedSize(shares=100),
            bar_time=bar_time,
            validity=1,
            tags={"reason": "golden_cross"},
        )
    if fp >= sp and f0 < s0:
        return Signal(
            symbol=close.meta["symbol"],
            direction=Direction.CLOSE_LONG,
            price=MarketOrder(),
            size=FixedSize(shares=100),
            bar_time=bar_time,
            validity=1,
            tags={"reason": "death_cross"},
        )
    return None


def main(db_path: str, symbol: str = "AAPL", tf: tuple = (1, "min")) -> None:
    """Run golden_cross backtest against db_path."""
    # Module import registered `golden_cross` via @plugin.  If the caller
    # cleared the registry, re-register by re-importing this module.
    import importlib
    import sys

    me = sys.modules.get(__name__)
    import algot.algo._core as core
    if "golden_cross" not in core._REGISTRY and me is not None:
        importlib.reload(me)
    source = SqliteSource(db_path)
    strat = Strategy(
        id="gc_long",
        type=StrategyType.LONG,
        capital=100_000.0,
        symbols=[symbol],
        signals=["golden_cross"],
        exec_lag=1,
    )
    result = BacktestEngine(strat, source, symbol=symbol, tf=tf).run()

    s = result["summary"][strat.id]
    print(f"symbol={result['symbol']} tf={tf} bars={len(result['bars'])}")
    print(f"signals={len(result['signals'])} "
          f"warmup_dropped={result['dropped_warmup']}")
    for o in result["orders"]:
        t = o.fill_time.strftime("%H:%M") if o.fill_time else "--"
        print(f"  {o.status:8s} {o.direction.value:12s} "
              f"fill={o.filled_shares:6.1f} @ {o.fill_price} {t} "
              f"{o.rejection_reason or ''}")
    print(f"realized_pnl=${s['realized_pnl']:.2f} "
          f"unrealized=${s['unrealized_pnl']:.2f} "
          f"equity=${s['equity']:.2f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m examples.golden_cross <db.sqlite> [SYMBOL]")
        sys.exit(1)
    db = sys.argv[1]
    sym = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
    main(db, sym)
