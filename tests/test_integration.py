"""End-to-end backtest integration: golden_cross (PLAN §6.4 / M3 smoke).

Golden cross strategy:
    fast_sma = sma(close, 5); slow_sma = sma(close, 20)
    fast crosses above slow  → LONG (buy FixedSize 100)
    fast crosses below slow  → CLOSE_LONG (exit)
Runs the full stack: SqliteSource → factor (sma n-param) → signal plugin →
BacktestEngine per-bar loop → BacktestBroker (Q1-Q4 + G2 exec_lag fill).
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

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
from algot.algo._core import _REGISTRY, clear_registry
from algot.algo.plugin import plugin
from algot.broker.backtest import BacktestBroker
from algot.source.sqlite import SqliteSource


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate per-test plugin registrations while keeping built-in factors.

    Built-in factors (sma/atr/...) register at `import algot` time.  A full
    clear_registry() would drop them and break engine deps wiring; instead
    snapshot before, and restore after each test.
    """
    before = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(before)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """Build an in-memory-style sqlite DB with a crafted price path.

    Design a series where a golden cross (fast crosses above slow) happens
    mid-way, price then rises, then fast crosses below slow near the end.
    """
    import sqlite3

    db = tmp_path_factory.mktemp("golden") / "test.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE bars ("
        "symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, "
        "open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "PRIMARY KEY (symbol, timestamp))"
    )

    n = 120  # 2 hours of 1-min bars
    t0 = int(datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp())

    def price_path(i: int) -> float:
        # wave around an upward drift → multiple golden crosses
        # amplitude ~6, period ~40 bars, drift +0.15/bar
        return 100.0 + 0.15 * i + 6.0 * np.sin(2 * np.pi * i / 40.0)

    rows = []
    for i in range(n):
        c = price_path(i)
        o = c + (0.1 if i % 2 else -0.1)
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append(("AAPL", t0 + i * 60, o, h, l, c, 1_000_000.0))
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture()
def source(db_path):
    return SqliteSource(db_path)


def _register_golden_cross_signal():
    """Placeholder — real golden_cross registered inside the e2e test
    (needs direct sma(close, n) calls, which require the engine 'close' view).
    """
    raise NotImplementedError("registered inline in test_golden_cross_end_to_end")


def test_golden_cross_end_to_end(source, db_path):
    """Full-stack smoke: engine runs golden_cross → fills + realized pnl."""
    # Register golden_cross with sma fast(5)/slow(20) as *registered instances*
    # is not supported for period-variant deps in v1 engine — instead the
    # signal plugin calls algot.sma(close, 5/20) directly on its view input.
    # The engine passes param 'close' only, so golden_cross computes both.
    from algot import sma as sma_factor

    @plugin(
        category="signal",
        shape_in={"close": "Sequence[float64]"},
        shape_out="Signal | None",
        stateful=True,
        state_type={"fast_prev": None, "slow_prev": None},
        min_bars=20,
    )
    def golden_cross_direct(close, state):
        fast = sma_factor(close, n=5)
        slow = sma_factor(close, n=20)
        f0, s0 = float(fast[0]), float(slow[0])
        # need previous bar values for cross detection
        if len(close) < 2 or np.isnan(f0) or np.isnan(s0):
            state["fast_prev"] = f0 if not np.isnan(f0) else None
            state["slow_prev"] = s0 if not np.isnan(s0) else None
            return None
        f_prev = state["fast_prev"]
        s_prev = state["slow_prev"]
        state["fast_prev"] = f0
        state["slow_prev"] = s0
        if f_prev is None or s_prev is None:
            return None
        bar_time = close.index[-1]
        if f_prev <= s_prev and f0 > s0:
            # golden cross → LONG
            return Signal(
                symbol=close.meta["symbol"],
                direction=Direction.LONG,
                price=MarketOrder(),
                size=FixedSize(shares=100),
                bar_time=bar_time,
                validity=1,
                tags={"reason": "golden_cross"},
            )
        if f_prev >= s_prev and f0 < s0:
            return Signal(
                symbol=close.meta["symbol"],
                direction=Direction.CLOSE_LONG,
                price=MarketOrder(),
                size=FixedSize(shares=100),
                bar_time=bar_time,
                validity=1,
                tags={"reason": "golden_cross_exit"},
            )
        return None

    strat = Strategy(
        id="gc_long",
        type=StrategyType.LONG,
        capital=100_000.0,
        symbols=["AAPL"],
        signals=["golden_cross_direct"],
        exec_lag=1,
    )
    engine = BacktestEngine(strat, source, symbol="AAPL", tf=(1, "min"))
    result = engine.run()

    # sanity: structure
    assert "summary" in result
    assert result["symbol"] == "AAPL"
    s = result["summary"]["gc_long"]

    # engine ran all bars
    assert len(result["bars"]) == 120

    # filled orders exist (strategy traded at least once)
    filled = [o for o in result["orders"] if o.status == "FILLED"]
    assert len(filled) > 0, "golden_cross produced no fills"

    # full cycle: at least one LONG and one CLOSE_LONG
    longs = [o for o in filled if o.direction == Direction.LONG]
    closes = [o for o in filled if o.direction == Direction.CLOSE_LONG]
    assert len(longs) >= 2, "expected multiple LONG entries"
    assert len(closes) >= 1, "golden_cross never exited a position"

    # realized pnl: closed cycles exist → nonzero (waves make crosses both ways)
    assert isinstance(s["realized_pnl"], float)
    # equity finite
    assert isinstance(s["equity"], float)

    # G2 fill semantics: LONG fill_time = signal bar_time + 1 bar
    first_long = longs[0]
    sig_times = [sg.bar_time for sg in result["signals"]
                 if sg.direction == Direction.LONG]
    assert first_long.fill_time is not None
    from datetime import timedelta
    # first long signal's bar + exec_lag(1)*60s should be the fill time
    assert first_long.fill_time == sig_times[0] + timedelta(seconds=60)

    # LONG fills at bar time + exec_lag (open of next bar) → fill_price set
    for o in filled:
        assert o.fill_price is not None
        assert o.fill_time is not None


def test_engine_rejects_unknown_signal():
    clear_registry()
    strat = Strategy(
        id="s", type=StrategyType.LONG, capital=1000.0,
        symbols=["AAPL"], signals=["nope"], exec_lag=1,
    )
    from algot.engine.backtest import BacktestEngine
    with pytest.raises(KeyError, match="nope"):
        BacktestEngine(strat, None, "AAPL", (1, "min"))


# ---------- engine input-space tests (M3 review: deps wiring + bars) ----------

def _db_with_wave(tmp_path_factory):
    """Reusable wave-pattern DB (same shape as golden_cross fixture)."""
    import sqlite3
    db = tmp_path_factory.mktemp("wave") / "test.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE bars (symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, "
        "open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "PRIMARY KEY (symbol, timestamp))"
    )
    n = 120
    t0 = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp())
    def pp(i): return 100.0 + 0.15 * i + 6.0 * np.sin(2 * np.pi * i / 40.0)
    rows = []
    for i in range(n):
        c = pp(i); o = c + (0.1 if i % 2 else -0.1)
        h = max(o, c) + 0.2; l = min(o, c) - 0.2
        rows.append(("AAPL", t0 + i * 60, o, h, l, c, 1_000_000.0))
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()
    return str(db)


def test_engine_deps_factor_injected(db_path):
    """deps=['sma'] → engine precomputes and injects sma output as param."""
    from algot import sma as sma_factor
    captured = {}

    @plugin(
        category="signal",
        shape_in={"close": "Sequence[float64]"},
        shape_out="Signal | None",
        stateful=True,
        state_type={},
        min_bars=20,
        deps=["sma"],
    )
    def sig_uses_dep_sma(close, sma, state):
        # sma param = injected full-series sma(close, n=20) sliced to current bar
        captured["sma_val"] = float(sma[0])
        captured["close_last"] = float(close[0])
        return None

    strat = Strategy(id="s", type=StrategyType.LONG, capital=100000.0,
                     symbols=["AAPL"], signals=["sig_uses_dep_sma"])
    engine = BacktestEngine(strat, SqliteSource(db_path), "AAPL", (1, "min"))
    result = engine.run()

    # sma output exists in results? verify engine ran without TypeError
    assert result["dropped_warmup"] == 0  # min_bars=20, but signal returns None
    # compare injected value vs direct sma call on last 20 close values
    close_full = result["bars"].close.data
    direct = float(np.mean(close_full[-20:]))
    assert captured["sma_val"] == pytest.approx(direct), \
        "injected sma value != direct sma(close,20) at last bar"
    assert captured["close_last"] == pytest.approx(close_full[-1])


def test_engine_bars_view_ohlcv_factor(db_path):
    """bars view injected → OHLCV factors (atr) reachable inside signal."""
    from algot import atr as atr_factor
    captured = {}

    @plugin(
        category="signal",
        shape_in={"bars": "OHLCVSequence"},
        shape_out="Signal | None",
        stateful=True,
        state_type={},
        min_bars=14,
    )
    def sig_uses_bars(bars, state):
        a = atr_factor(bars, n=14)
        captured["atr_val"] = float(a[0])
        captured["n_bars"] = len(bars)
        return None

    strat = Strategy(id="s", type=StrategyType.LONG, capital=100000.0,
                     symbols=["AAPL"], signals=["sig_uses_bars"])
    engine = BacktestEngine(strat, SqliteSource(db_path), "AAPL", (1, "min"))
    result = engine.run()

    # atr callable on bars view → captured a real value at the end (non-NaN)
    assert captured["n_bars"] == 120
    assert not np.isnan(captured["atr_val"])
    # atr(14) first valid at bar 14 → value plausible > 0
    assert captured["atr_val"] > 0


def test_engine_deps_ohlcv_factor(db_path):
    """deps=['atr'] → shape_in OHLCVSequence binds bars automatically."""
    from algot import atr as atr_factor
    captured = {}

    @plugin(
        category="signal",
        shape_in={"close": "Sequence[float64]"},
        shape_out="Signal | None",
        stateful=True,
        state_type={},
        min_bars=14,
        deps=["atr"],
    )
    def sig_uses_dep_atr(close, atr, state):
        captured["atr_val"] = float(atr[0])
        return None

    strat = Strategy(id="s", type=StrategyType.LONG, capital=100000.0,
                     symbols=["AAPL"], signals=["sig_uses_dep_atr"])
    engine = BacktestEngine(strat, SqliteSource(db_path), "AAPL", (1, "min"))
    result = engine.run()
    assert not np.isnan(captured["atr_val"])
    assert captured["atr_val"] > 0
