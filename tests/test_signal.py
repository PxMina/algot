"""Signal + Direction + Price/Size tests (per docs/05-signals.md §7 canonical)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from algot.signal import (
    Direction,
    FixedSize,
    LimitOrder,
    LimitRange,
    MarketOrder,
    PctSize,
    RiskSize,
    Signal,
)

T = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------- Direction ----------

def test_direction_5_states():
    """v1 5-state (per 05 §7)."""
    assert Direction.LONG.value == "long"
    assert Direction.SHORT.value == "short"
    assert Direction.FLAT.value == "flat"
    assert Direction.CLOSE_LONG.value == "close_long"
    assert Direction.CLOSE_SHORT.value == "close_short"
    assert len(Direction) == 5


# ---------- Price ----------

def test_market_order_default():
    m = MarketOrder()
    assert m is not None


def test_limit_order_price():
    m = LimitOrder(price=99.5)
    assert m.price == 99.5


def test_limit_order_nonpositive_raises():
    with pytest.raises(ValueError, match="LimitOrder.price must be > 0"):
        LimitOrder(price=0.0)


def test_limit_range_valid():
    r = LimitRange(min_price=98.0, max_price=100.0)
    assert r.min_price == 98.0
    assert r.max_price == 100.0


def test_limit_range_inverted_raises():
    with pytest.raises(ValueError, match="min_price"):
        LimitRange(min_price=101.0, max_price=99.0)


# ---------- Size ----------

def test_fixed_size_positive():
    f = FixedSize(shares=100)
    assert f.shares == 100


def test_fixed_size_negative_raises():
    with pytest.raises(ValueError, match="FixedSize.shares must be >= 0"):
        FixedSize(shares=-10)


def test_pct_size_valid_range():
    p = PctSize(pct=0.5)
    assert p.pct == 0.5


def test_pct_size_zero_raises():
    with pytest.raises(ValueError, match="PctSize.pct must be"):
        PctSize(pct=0.0)


def test_pct_size_over_one_raises():
    with pytest.raises(ValueError, match="PctSize.pct must be"):
        PctSize(pct=1.5)


def test_risk_size_valid():
    r = RiskSize(risk_amount=100.0, stop_loss=95.0)
    assert r.risk_amount == 100.0
    assert r.stop_loss == 95.0


# ---------- Signal ----------

def test_signal_basic_construction():
    s = Signal(
        symbol="AAPL",
        direction=Direction.LONG,
        price=MarketOrder(),
        size=FixedSize(shares=100),
        bar_time=T,
    )
    assert s.symbol == "AAPL"
    assert s.direction == Direction.LONG
    assert s.bar_time == T
    assert s.validity == 1  # default = current bar (spec 05 §5)
    assert s.tags == {}
    assert s.signal_id  # auto UUID


def test_signal_empty_symbol_raises():
    with pytest.raises(ValueError, match="symbol must be non-empty"):
        Signal(symbol="", direction=Direction.LONG)


def test_signal_bad_direction_raises():
    with pytest.raises(ValueError, match="direction must be a Direction"):
        Signal(symbol="AAPL", direction="long")  # type: ignore[arg-type]


def test_signal_validity_zero_raises():
    with pytest.raises(ValueError, match="validity cannot be 0"):
        Signal(symbol="AAPL", direction=Direction.LONG, validity=0)


def test_signal_validity_too_low_raises():
    with pytest.raises(ValueError, match="validity must be >= -1"):
        Signal(symbol="AAPL", direction=Direction.LONG, validity=-5)


def test_signal_validity_minus_one_means_permanent():
    """-1 = permanent validity (per 05 §5)."""
    s = Signal(symbol="AAPL", direction=Direction.LONG, validity=-1)
    assert s.validity == -1


def test_signal_with_limit_order():
    s = Signal(
        symbol="AAPL", direction=Direction.SHORT,
        price=LimitOrder(price=99.5), size=FixedSize(shares=50),
    )
    assert isinstance(s.price, LimitOrder)
    assert s.price.price == 99.5


def test_signal_with_pct_size():
    s = Signal(
        symbol="AAPL", direction=Direction.LONG,
        size=PctSize(pct=0.5),
    )
    assert s.size.pct == 0.5


def test_signal_with_risk_size():
    s = Signal(
        symbol="AAPL", direction=Direction.LONG,
        size=RiskSize(risk_amount=100.0, stop_loss=95.0),
    )
    assert s.size.risk_amount == 100.0


def test_signal_with_tags_and_signal_id():
    s = Signal(
        symbol="AAPL", direction=Direction.FLAT,
        tags={"reason": "golden_cross_exit"}, signal_id="abc-123",
    )
    assert s.tags["reason"] == "golden_cross_exit"
    assert s.signal_id == "abc-123"


def test_signal_default_price_market_order():
    s = Signal(symbol="AAPL", direction=Direction.LONG)
    assert isinstance(s.price, MarketOrder)


def test_signal_flat_helper():
    s = Signal.flat(symbol="AAPL", bar_time=T)
    assert s.direction == Direction.FLAT
    assert isinstance(s.size, FixedSize)
    assert s.size.shares == 0.0
    assert isinstance(s.price, MarketOrder)


def test_signal_flat_with_pct_size_raises():
    """FLAT must not use PctSize (per 05 §9.3)."""
    with pytest.raises(ValueError, match="PctSize cannot be used with FLAT"):
        Signal(symbol="AAPL", direction=Direction.FLAT, size=PctSize(pct=0.5))


def test_signal_flat_with_nonzero_fixed_raises():
    """FLAT size must be FixedSize(0) — position implied by broker state."""
    with pytest.raises(ValueError, match="FLAT signal size must be FixedSize"):
        Signal(symbol="AAPL", direction=Direction.FLAT,
               size=FixedSize(shares=100))
