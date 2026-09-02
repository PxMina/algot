"""Signal dataclass + Direction enum + price/size unions (per docs/05-signals.md §7).

Direction 5-state v1:
    LONG        — open long position
    SHORT       — open short position
    FLAT        — close all positions (strategy-scope)
    CLOSE_LONG  — close long only
    CLOSE_SHORT — close short only

Price union:
    MarketOrder()          — market order (execute at bar open + exec_lag)
    LimitOrder(price)      — limit order at specified price
    LimitRange(min, max)   — price-range order (v1 broker rejects / fills at market)

Size union:
    FixedSize(shares)       — fixed share count
    PctSize(pct)            — pct of pool (LONG/SHORT) or position (CLOSE_*/FLAT)
    RiskSize(risk_amount, stop_loss) — risk-based position sizing

Canonical schema note (M3 contract fix):
    This module follows docs/05-signals.md §7 canonical — NOT the earlier
    M2-era schema (symbol/bar_time:int/expiry/tag/id).  Per C1 review
    (commit 4e2e277) + 06-brokers usage, the fields are:
        symbol    (str, required)          — ticker; broker needs it for fill lookup
        direction (Direction, required)
        price     (union, default MarketOrder)
        size      (union, default FixedSize(0))
        bar_time  (datetime UTC)           — trigger bar START time (02 §5.1)
        validity  (int)  1=current bar; N=N bars; -1=permanent
        signal_id (str, auto UUID)
        tags      (dict, metadata)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Union
from uuid import uuid4


class Direction(Enum):
    """5-state direction enum (per 05 §7 + 00 §6.5 C2)."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"


@dataclass
class MarketOrder:
    """Market order — execute at bar open + exec_lag (per G2)."""
    pass


@dataclass
class LimitOrder:
    """Limit order at specified price."""
    price: float

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(
                f"LimitOrder.price must be > 0, got {self.price}"
            )


@dataclass
class LimitRange:
    """Price-range order (VWAP / bracket; v2 full support)."""
    min_price: float
    max_price: float

    def __post_init__(self) -> None:
        if self.min_price >= self.max_price:
            raise ValueError(
                f"LimitRange: min_price ({self.min_price}) must be < "
                f"max_price ({self.max_price})"
            )


@dataclass
class FixedSize:
    """Fixed share count (per 05 §9.2)."""
    shares: float

    def __post_init__(self) -> None:
        if self.shares < 0:
            raise ValueError(
                f"FixedSize.shares must be >= 0, got {self.shares}"
            )


@dataclass
class PctSize:
    """Pct of pool (LONG/SHORT) or position (CLOSE_*/FLAT) (per 05 §9.3)."""
    pct: float

    def __post_init__(self) -> None:
        if not (0.0 < self.pct <= 1.0):
            raise ValueError(
                f"PctSize.pct must be 0 < pct <= 1.0, got {self.pct}"
            )


@dataclass
class RiskSize:
    """Risk-based position sizing (per 05 §9.3)."""
    risk_amount: float
    stop_loss: float


Price = Union[MarketOrder, LimitOrder, LimitRange]
Size = Union[FixedSize, PctSize, RiskSize]

PRICE_TYPES: tuple = (MarketOrder, LimitOrder, LimitRange)
SIZE_TYPES: tuple = (FixedSize, PctSize, RiskSize)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Signal:
    """Strategy signal (per 05 §7 canonical schema).

    Emitted at bar close; consumed at bar time + exec_lag open (per G2).

    Fields:
        symbol:    ticker (str, required)
        direction: Direction enum (5-state)
        price:     Price union (default = MarketOrder)
        size:      Size union (default = FixedSize(0))
        bar_time:  trigger bar START time (datetime UTC, per 00 §5.1)
        validity:  N bars validity; 1 = current bar only; -1 = permanent
        signal_id: auto UUID
        tags:      user metadata dict
    """
    symbol: str
    direction: Direction
    size: Size = field(default_factory=lambda: FixedSize(shares=0.0))
    price: Price = field(default_factory=MarketOrder)
    bar_time: datetime = field(default_factory=_now_utc)
    validity: int = 1
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    tags: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError(
                f"Signal.symbol must be non-empty str, got {self.symbol!r}"
            )
        if not isinstance(self.direction, Direction):
            raise ValueError(
                "Signal.direction must be a Direction enum, "
                f"got {self.direction!r}"
            )
        if not isinstance(self.price, PRICE_TYPES):
            raise ValueError(
                f"Signal.price must be MarketOrder | LimitOrder | LimitRange, "
                f"got {type(self.price).__name__}"
            )
        if not isinstance(self.size, SIZE_TYPES):
            raise ValueError(
                f"Signal.size must be FixedSize | PctSize | RiskSize, "
                f"got {type(self.size).__name__}"
            )
        if self.validity == 0:
            raise ValueError(
                "Signal.validity cannot be 0; use 1 for current bar, "
                "-1 for permanent"
            )
        if self.validity < -1:
            raise ValueError(
                f"Signal.validity must be >= -1, got {self.validity}"
            )
        if self.direction == Direction.FLAT:
            if isinstance(self.size, PctSize):
                raise ValueError(
                    "PctSize cannot be used with FLAT "
                    "(use FixedSize(0) or Signal.flat() helper)"
                )
            if isinstance(self.size, FixedSize) and self.size.shares != 0:
                raise ValueError(
                    "FLAT signal size must be FixedSize(0) "
                    "(position is implied by broker state)"
                )

    @classmethod
    def flat(cls, symbol: str, bar_time: datetime | None = None,
             **kwargs) -> "Signal":
        """Helper for FLAT signal (size 占位 FixedSize(0))."""
        return cls(
            symbol=symbol,
            direction=Direction.FLAT,
            price=MarketOrder(),
            size=FixedSize(shares=0.0),
            bar_time=bar_time or _now_utc(),
            **kwargs,
        )
