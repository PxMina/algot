"""BacktestBroker tests — Q1-Q4 + cash flow (per docs/06-brokers.md §4-§6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from algot.broker.backtest import BacktestBroker
from algot.broker.base import Order, PositionSlot, StrategyType
from algot.signal import (
    Direction,
    FixedSize,
    MarketOrder,
    PctSize,
    RiskSize,
    Signal,
)

T0 = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
BAR_SEC = 60  # 1-min bars


def _make_broker(capital: float = 100_000.0) -> BacktestBroker:
    return BacktestBroker(pools={"s1": capital}, bar_seconds=BAR_SEC)


def _prices(px: float = 100.0):
    """Static price lookup: always returns px at open field."""
    def lookup(symbol: str, bar_time: datetime, field: str = "open") -> float:
        return px
    return lookup


def _sig(symbol="AAPL", direction=Direction.LONG, size=None, bar_time=T0, **kw) -> Signal:
    if size is None:
        size = FixedSize(shares=100)
    kw.setdefault("price", MarketOrder())
    return Signal(
        symbol=symbol, direction=direction, size=size,
        bar_time=bar_time, **kw,
    )


# ---------- Q1: weighted-average cost ----------

def test_q1_weighted_average_cost():
    b = _make_broker()
    # buy 100 @100
    orders = b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
                      T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(100)
    assert slot.avg_cost == pytest.approx(100.0)

    # add 50 @120 (bar T+1)
    orders = b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=50))],
                      T0 + timedelta(seconds=60), _prices(120.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(150)
    # (100*100 + 50*120) / 150 = 106.67
    assert slot.avg_cost == pytest.approx(106.6666667, rel=1e-6)
    # cash: 100000 - 10000 - 6000
    assert b.get_cash("s1") == pytest.approx(84_000)


# ---------- Q2: close > position → close all + WARN, no raise ----------

def test_q2_close_more_than_position_closes_all(caplog):
    import logging
    b = _make_broker()
    b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    with caplog.at_level(logging.WARNING):
        orders = b.submit("s1", StrategyType.LONG,
                          [_sig(direction=Direction.CLOSE_LONG, size=FixedSize(shares=500))],
                          T0 + timedelta(seconds=60), _prices(110.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    assert orders[0].filled_shares == pytest.approx(100)  # capped at position
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(0)
    assert slot.avg_cost == pytest.approx(0.0)
    # realized = (110-100)*100 = 1000
    assert b.get_realized_pnl("s1") == pytest.approx(1000.0)
    assert "closing all" in caplog.text or "closing all" in str(caplog.records)


def test_q2_close_with_no_position_rejected():
    b = _make_broker()
    orders = b.submit("s1", StrategyType.LONG,
                      [_sig(direction=Direction.CLOSE_LONG, size=FixedSize(shares=50))],
                      T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "REJECTED"


# ---------- Q3: independent pools ----------

def test_q3_independent_cash_pools():
    b = BacktestBroker(pools={"long_s": 10_000, "short_s": 50_000},
                       bar_seconds=BAR_SEC)
    assert b.get_cash("long_s") == 10_000
    assert b.get_cash("short_s") == 50_000
    # long buys
    b.submit("long_s", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    assert b.get_cash("long_s") == pytest.approx(0)
    assert b.get_cash("short_s") == pytest.approx(50_000)  # untouched


# ---------- Q4: sequential same-bar signals ----------

def test_q4_same_bar_sequential_open_then_close():
    b = _make_broker()
    sigs = [
        _sig(direction=Direction.LONG, size=FixedSize(shares=100)),
        _sig(direction=Direction.CLOSE_LONG, size=FixedSize(shares=100)),
    ]
    orders = b.submit("s1", StrategyType.LONG, sigs, T0, _prices(100.0), exec_lag=1)
    assert [o.status for o in orders] == ["FILLED", "FILLED"]
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(0)
    # second close saw post-open state → avg_cost correct
    # cash returns to 100000 (buy @100 sell @100, zero pnl)
    assert b.get_cash("s1") == pytest.approx(100_000.0, rel=1e-9)


# ---------- SHORT flow ----------

def test_short_open_and_close():
    b = _make_broker()
    o1 = b.submit("s1", StrategyType.SHORT,
                  [_sig(direction=Direction.SHORT, size=FixedSize(shares=100))],
                  T0, _prices(100.0), exec_lag=1)
    assert o1[0].status == "FILLED"
    slot = b.get_position("s1", "AAPL")
    assert slot.direction == StrategyType.SHORT
    assert slot.shares == pytest.approx(100)
    assert slot.avg_cost == pytest.approx(100.0)
    # short proceeds added: cash = 100000 + 10000
    assert b.get_cash("s1") == pytest.approx(110_000)

    # close short @90 → profit (100-90)*100 = 1000
    o2 = b.submit("s1", StrategyType.SHORT,
                  [_sig(direction=Direction.CLOSE_SHORT, size=FixedSize(shares=100))],
                  T0 + timedelta(seconds=60), _prices(90.0), exec_lag=1)
    assert o2[0].status == "FILLED"
    assert b.get_realized_pnl("s1") == pytest.approx(1000.0)
    # cash: 110000 - 9000 = 101000
    assert b.get_cash("s1") == pytest.approx(101_000)


# ---------- FLAT ----------

def test_flat_closes_strategy_positions():
    b = _make_broker()
    b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    # price moves to 110 at next bar
    orders = b.submit("s1", StrategyType.LONG,
                      [Signal.flat(symbol="AAPL", bar_time=T0 + timedelta(seconds=60))],
                      T0 + timedelta(seconds=60), _prices(110.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(0)
    assert b.get_realized_pnl("s1") == pytest.approx(1000.0)


def test_flat_only_closes_own_strategy():
    b = BacktestBroker(pools={"long_s": 10_000, "short_s": 10_000},
                       bar_seconds=BAR_SEC)
    b.submit("long_s", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    b.submit("short_s", StrategyType.SHORT,
             [_sig(direction=Direction.SHORT, size=FixedSize(shares=50))],
             T0, _prices(100.0), exec_lag=1)
    # long_s FLAT must not touch short_s position
    b.submit("long_s", StrategyType.LONG,
             [Signal.flat(symbol="AAPL", bar_time=T0 + timedelta(seconds=60))],
             T0 + timedelta(seconds=60), _prices(100.0), exec_lag=1)
    assert b.get_position("long_s", "AAPL").shares == pytest.approx(0)
    assert b.get_position("short_s", "AAPL").shares == pytest.approx(50)


# ---------- direction-typed strategy (C3) ----------

def test_strategy_direction_typing_violation_raises():
    b = _make_broker()
    with pytest.raises(ValueError, match="not allowed"):
        b.submit("s1", StrategyType.LONG,
                 [_sig(direction=Direction.SHORT)], T0, _prices(100.0), exec_lag=1)


# ---------- exec_lag validation + fill time ----------

def test_exec_lag_lt_1_raises():
    b = _make_broker()
    with pytest.raises(ValueError, match="exec_lag must be >= 1"):
        b.submit("s1", StrategyType.LONG, [_sig()], T0, _prices(100.0), exec_lag=0)


def test_fill_beyond_data_expired():
    """exec_lag fill time past data end → EXPIRED."""
    b = _make_broker()

    def lookup(symbol, bar_time, field="open"):
        raise KeyError(f"no bar at {bar_time}")

    orders = b.submit("s1", StrategyType.LONG, [_sig()], T0, lookup, exec_lag=1)
    assert orders[0].status == "EXPIRED"
    assert b.get_position("s1", "AAPL").shares == pytest.approx(0)


# ---------- insufficient cash (06 §4.1) ----------

def test_long_insufficient_cash_scales_down(caplog):
    import logging
    b = _make_broker(capital=1_000)
    # want 100 shares @100 = 10000 > 1000 → scaled to 10 shares
    with caplog.at_level(logging.WARNING):
        orders = b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
                          T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    assert orders[0].filled_shares == pytest.approx(10)
    assert b.get_cash("s1") == pytest.approx(0)
    assert "insufficient" in caplog.text.lower()


def test_long_zero_cash_rejected():
    b = _make_broker(capital=0)
    orders = b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
                      T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "REJECTED"
    assert orders[0].rejection_kind == "INSUFFICIENT_CASH"


# ---------- RiskSize / PctSize ----------

def test_risksize_long_sizing():
    b = _make_broker()
    sig = _sig(direction=Direction.LONG,
               size=RiskSize(risk_amount=1000.0, stop_loss=95.0))
    orders = b.submit("s1", StrategyType.LONG, [sig], T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    # risk/share = 5 → 200 shares
    assert orders[0].filled_shares == pytest.approx(200)
    assert b.get_position("s1", "AAPL").shares == pytest.approx(200)


def test_risksize_wrong_side_rejected():
    """LONG stop_loss >= entry → REJECTED (INVALID_SIZE), not filled."""
    b = _make_broker()
    sig = _sig(direction=Direction.LONG,
               size=RiskSize(risk_amount=1000.0, stop_loss=105.0))
    orders = b.submit("s1", StrategyType.LONG, [sig], T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "REJECTED"
    assert orders[0].rejection_kind == "INVALID_SIZE"
    assert "stop_loss" in (orders[0].rejection_reason or "")
    assert b.get_position("s1", "AAPL").shares == pytest.approx(0)


def test_pctsize_long():
    b = _make_broker(capital=10_000)
    sig = _sig(direction=Direction.LONG, size=PctSize(pct=0.5))
    orders = b.submit("s1", StrategyType.LONG, [sig], T0, _prices(100.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    # 50% of 10000 / 100 = 50 shares
    assert orders[0].filled_shares == pytest.approx(50)


def test_pctsize_close_position_pct():
    b = _make_broker()
    b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    # close 50% of position
    orders = b.submit("s1", StrategyType.LONG,
                      [_sig(direction=Direction.CLOSE_LONG, size=PctSize(pct=0.5))],
                      T0 + timedelta(seconds=60), _prices(110.0), exec_lag=1)
    assert orders[0].status == "FILLED"
    assert orders[0].filled_shares == pytest.approx(50)
    slot = b.get_position("s1", "AAPL")
    assert slot.shares == pytest.approx(50)
    # realized = (110-100)*50 = 500
    assert b.get_realized_pnl("s1") == pytest.approx(500.0)


# ---------- limit order → market + WARN (05 §3.2) ----------

def test_limit_order_fills_at_market_with_warn(caplog):
    import logging
    from algot.signal import LimitOrder
    b = _make_broker()
    with caplog.at_level(logging.WARNING):
        orders = b.submit(
            "s1", StrategyType.LONG,
            [_sig(price=LimitOrder(price=95.0))], T0, _prices(100.0), exec_lag=1,
        )
    assert orders[0].status == "FILLED"
    assert orders[0].fill_price == pytest.approx(100.0)  # filled at market
    assert "limit orders not simulated" in caplog.text


# ---------- finalize ----------

def test_finalize_marks_open_position():
    b = _make_broker()
    b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    summary = b.finalize(last_prices={"AAPL": 110.0})
    s = summary["s1"]
    assert s["realized_pnl"] == pytest.approx(0.0)
    assert s["unrealized_pnl"] == pytest.approx(1000.0)
    # equity = cash (90000) + unrealized (1000)
    assert s["equity"] == pytest.approx(91_000)


# ---------- M3 review: FLAT audit trail + requested_shares semantics ----------

def test_flat_produces_close_orders_in_history():
    """FLAT expands to per-slot CLOSE_* orders in fill_history (review #3)."""
    b = _make_broker()
    b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
             T0, _prices(100.0), exec_lag=1)
    orders = b.submit(
        "s1", StrategyType.LONG,
        [Signal.flat("AAPL", bar_time=T0 + timedelta(seconds=60))],
        T0 + timedelta(seconds=60), _prices(110.0), exec_lag=1,
    )
    # FLAT now returns the actual close order(s), not a summary stub
    assert len(orders) == 1
    o = orders[0]
    assert o.direction == Direction.CLOSE_LONG
    assert o.status == "FILLED"
    assert o.filled_shares == pytest.approx(100)
    assert o.fill_price == pytest.approx(110.0)
    # fill_history has: LONG open + CLOSE_LONG (2 entries, both real fills)
    assert [f.direction for f in b.fill_history] == \
        [Direction.LONG, Direction.CLOSE_LONG]
    assert b.get_realized_pnl("s1") == pytest.approx(1000.0)


def test_flat_on_empty_position_returns_empty():
    """FLAT with nothing held → no orders, no crash."""
    b = _make_broker()
    orders = b.submit(
        "s1", StrategyType.LONG,
        [Signal.flat("AAPL", bar_time=T0)],
        T0, _prices(100.0), exec_lag=1,
    )
    assert orders == []


def test_scale_down_keeps_original_requested_shares():
    """requested_shares = user request, filled_shares = actual (review #7)."""
    b = _make_broker(capital=1_000)
    orders = b.submit("s1", StrategyType.LONG, [_sig(size=FixedSize(shares=100))],
                      T0, _prices(100.0), exec_lag=1)
    o = orders[0]
    assert o.status == "FILLED"
    assert o.requested_shares == pytest.approx(100)   # original request
    assert o.filled_shares == pytest.approx(10)       # scaled to affordable
