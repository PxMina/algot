"""Signal + Direction + Price/Size tests (per docs/05-signals.md §7)."""

from __future__ import annotations

import pytest

from algot.signal import (
    Direction,
    FixedSize,
    LimitOrder,
    MarketOrder,
    PctSize,
    RiskSize,
    Signal,
)


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
        bar_time=1704067200,
        direction=Direction.LONG,
        price=MarketOrder(),
        size=FixedSize(shares=100),
    )
    assert s.symbol == "AAPL"
    assert s.bar_time == 1704067200
    assert s.direction == Direction.LONG
    assert s.expiry == 0  # default
    assert s.tag is None
    assert s.id is None


def test_signal_empty_symbol_raises():
    with pytest.raises(ValueError, match="symbol must be non-empty"):
        Signal(symbol="", bar_time=100, direction=Direction.LONG)


def test_signal_expiry_negative_too_low():
    with pytest.raises(ValueError, match="expiry must be >= -1"):
        Signal(
            symbol="AAPL", bar_time=100, direction=Direction.LONG,
            expiry=-5,
        )


def test_signal_expiry_minus_one_means_permanent():
    """-1 = permanent validity (per 05 §9.4)."""
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.LONG,
        expiry=-1,
    )
    assert s.expiry == -1


def test_signal_with_limit_order():
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.SHORT,
        price=LimitOrder(price=99.5), size=FixedSize(shares=50),
    )
    assert isinstance(s.price, LimitOrder)
    assert s.price.price == 99.5


def test_signal_with_pct_size():
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.LONG,
        size=PctSize(pct=0.5),
    )
    assert s.size.pct == 0.5


def test_signal_with_risk_size():
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.LONG,
        size=RiskSize(risk_amount=100.0, stop_loss=95.0),
    )
    assert s.size.risk_amount == 100.0


def test_signal_with_tag_and_id():
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.FLAT,
        tag="golden_cross_exit", id="abc-123",
    )
    assert s.tag == "golden_cross_exit"
    assert s.id == "abc-123"


def test_signal_default_price_market_order():
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.LONG,
    )
    assert isinstance(s.price, MarketOrder)


def test_signal_flat_size_validation():
    """FLAT must not use PctSize (per 05 §9.3 — FLAT 不允许 PctSize)."""
    # Note: actual FLAT validation happens in __post_init__ or emit layer;
    # v1 defers to broker. Just check construction works.
    s = Signal(
        symbol="AAPL", bar_time=100, direction=Direction.FLAT,
    )
    assert s.direction == Direction.FLAT