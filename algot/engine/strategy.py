"""Strategy config dataclass (per docs/06-brokers.md §3/Q3 + 00 §6.4).

Q3 model: long/short are 2 completely independent strategies.  Each strategy
declares its own capital + symbols + plugin chain + type (long/short).

v1: per-strategy single symbol (multi-symbol = host for-loop per 06 §6.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from algot.broker.base import StrategyType


@dataclass
class Strategy:
    """One backtest/live strategy instance.

    Fields:
        id:        unique strategy id (used as broker key)
        type:      StrategyType.LONG / SHORT (direction-typed, 00 §6.4 C3)
        capital:   initial cash pool for THIS strategy
        symbols:   list of symbols to trade (v1: engine runs one at a time)
        signals:   names of signal plugins to run (order matters, Q4)
        exec_lag:  bars between emit and fill open (G2, default 1)
    """
    id: str
    type: StrategyType = StrategyType.LONG
    capital: float = 100_000.0
    symbols: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    exec_lag: int = 1

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Strategy.id must be non-empty")
        if self.capital <= 0:
            raise ValueError(f"Strategy.capital must be > 0, got {self.capital}")
        if self.exec_lag < 1:
            raise ValueError(
                f"Strategy.exec_lag must be >= 1, got {self.exec_lag} (00 §6.5 G2)"
            )
