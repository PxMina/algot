"""Broker base types (per docs/06-brokers.md §2 + §3 + §9).

Public API:
    StrategyType   — LONG / SHORT (strategy direction, NOT Signal Direction)
    PositionSlot   — per (strategy, symbol) single-direction position
    CashPool       — per-strategy capital
    Order          — execution record (one per Signal)
    BaseBroker     — ABC: submit / get_position / get_cash / get_realized_pnl
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Literal, Union

from algot.signal import Direction, MarketOrder, Signal, Size


class StrategyType(str, Enum):
    """Strategy 方向类型（区别于 Signal.direction；StrategyType 仅 long/short）。"""
    LONG = "long"
    SHORT = "short"


@dataclass
class PositionSlot:
    """Per (strategy, symbol) single-direction position (06 §3.1).

    v1 invariant: slot.direction == strategy_type.
    """
    strategy_id: str
    symbol: str
    direction: StrategyType
    shares: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class CashPool:
    """Per-strategy capital (06 §3.2)."""
    strategy_id: str
    initial_capital: float
    current_cash: float
    total_realized_pnl: float = 0.0


OrderStatus = Literal["FILLED", "REJECTED", "EXPIRED", "PENDING"]


@dataclass
class Order:
    """Execution record for one Signal (06 §9)."""
    signal_id: str
    strategy_id: str
    symbol: str
    direction: Direction
    status: OrderStatus
    requested_shares: float
    filled_shares: float = 0.0
    fill_price: float | None = None
    fill_time: datetime | None = None
    fee: float = 0.0
    slippage: float = 0.0
    rejection_reason: str | None = None
    rejection_kind: str | None = None  # INSUFFICIENT_CASH / INVALID_SIZE / BROKER_ERROR


# fill_price_lookup(symbol, bar_time, field="open") -> float
FillLookup = Callable[[str, datetime, str], float]


class BaseBroker(ABC):
    """撮合层 ABC (06 §2)."""

    @abstractmethod
    def submit(
        self,
        strategy_id: str,
        strategy_type: StrategyType,
        signals: list[Signal],
        bar_time: datetime,
        fill_price_lookup: FillLookup,
        exec_lag: int = 1,
    ) -> list[Order]:
        """撮合一组 Signals（已按 emit 顺序），返回 Order 列表。"""
        ...

    @abstractmethod
    def get_position(self, strategy_id: str, symbol: str) -> PositionSlot:
        """查询当前持仓（v1 单 slot per strategy）。"""
        ...

    @abstractmethod
    def get_cash(self, strategy_id: str) -> float:
        """查询当前可用现金。"""
        ...

    @abstractmethod
    def get_realized_pnl(self, strategy_id: str) -> float:
        """查询已实现 PnL。"""
        ...
