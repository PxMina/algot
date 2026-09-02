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

Size union:
    FixedSize(shares)       — fixed share count
    PctSize(pct)            — pct of pool (LONG/SHORT) or position (CLOSE_*/FLAT)
    RiskSize(risk_amount, stop_loss) — risk-based position sizing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


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


Price = Union[MarketOrder, LimitOrder]


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


Size = Union[FixedSize, PctSize, RiskSize]


@dataclass
class Signal:
    """Strategy signal (per 05 §7 canonical schema).

    Emitted at bar close; consumed at bar time + exec_lag open (per G2).

    Fields:
        symbol:    ticker
        bar_time:  trigger bar START time (UTC unix seconds, per 00 §5.1)
        direction: Direction enum
        price:     Price union (default = MarketOrder)
        size:      Size union (default = FixedSize(0))
        expiry:    N bars validity; 0=current bar only; -1=permanent
        tag:       optional label string
        id:        framework-assigned UUID (set at emit)
    """
    symbol: str
    bar_time: int
    direction: Direction
    price: Price = field(default_factory=MarketOrder)
    size: Size = field(default_factory=lambda: FixedSize(shares=0.0))
    expiry: int = 0
    tag: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError(
                f"Signal.symbol must be non-empty str, got {self.symbol!r}"
            )
        if self.expiry is not None and self.expiry < -1:
            raise ValueError(
                f"Signal.expiry must be >= -1, got {self.expiry}"
            )