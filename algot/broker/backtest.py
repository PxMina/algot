"""BacktestBroker — full backtest matching (per docs/06-brokers.md §4-§6).

Implements Q1-Q4:
    Q1  weighted-average cost on adds (§5.1)
    Q2  close > current → close all + WARN (§4.3, no raise)
    Q3  independent CashPool per strategy (§3.2)
    Q4  same-bar multi-Signal applied sequentially in emit order (§5.4)

Fill semantics (G2 / §6.1):
    Signal emitted at bar T (bar_time = bar T START).
    Filled at bar T+exec_lag OPEN price.  exec_lag default 1.
    Fill beyond last bar → EXPIRED.

v1 policy: no partial fills at Order level — but LONG buys that exceed
pool cash are scaled down to affordable shares + WARN (§4.1 explicit flow).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from algot.broker.base import (
    BaseBroker,
    CashPool,
    FillLookup,
    Order,
    PositionSlot,
    StrategyType,
)
from algot.signal import Direction, FixedSize, LimitOrder, MarketOrder, PctSize, RiskSize, Signal

log = logging.getLogger("algot.broker.backtest")

# Direction usages valid per strategy type (C3 direction-typed strategies).
_VALID_DIRECTIONS = {
    StrategyType.LONG: {Direction.LONG, Direction.CLOSE_LONG, Direction.FLAT},
    StrategyType.SHORT: {Direction.SHORT, Direction.CLOSE_SHORT, Direction.FLAT},
}


class BacktestBroker(BaseBroker):
    """v1 完整回测撮合 (06 §6)."""

    def __init__(
        self,
        pools: dict[str, float],
        bar_seconds: int,
    ) -> None:
        """pools: {strategy_id: initial_capital}. bar_seconds: TF interval. """
        self._pools: dict[str, CashPool] = {
            sid: CashPool(sid, cap, cap) for sid, cap in pools.items()
        }
        self._slots: dict[tuple[str, str], PositionSlot] = {}
        self._bar_seconds = bar_seconds
        self.fill_history: list[Order] = []
        # map strategy_id → StrategyType (validated on first submit per strategy)
        self._strategy_types: dict[str, StrategyType] = {}

    # ---------- public queries ----------

    def get_position(self, strategy_id: str, symbol: str) -> PositionSlot:
        key = (strategy_id, symbol)
        if key not in self._slots:
            st = self._strategy_types.get(strategy_id, StrategyType.LONG)
            self._slots[key] = PositionSlot(
                strategy_id=strategy_id, symbol=symbol, direction=st
            )
        return self._slots[key]

    def get_cash(self, strategy_id: str) -> float:
        if strategy_id not in self._pools:
            raise KeyError(f"unknown strategy_id: {strategy_id}")
        return self._pools[strategy_id].current_cash

    def get_realized_pnl(self, strategy_id: str) -> float:
        if strategy_id not in self._pools:
            raise KeyError(f"unknown strategy_id: {strategy_id}")
        return self._pools[strategy_id].total_realized_pnl

    def get_slot_map(self) -> dict[tuple[str, str], PositionSlot]:
        return self._slots

    # ---------- submit / matching ----------

    def submit(
        self,
        strategy_id: str,
        strategy_type: StrategyType,
        signals: list[Signal],
        bar_time: datetime,
        fill_price_lookup: FillLookup,
        exec_lag: int = 1,
    ) -> list[Order]:
        if strategy_id not in self._pools:
            raise KeyError(f"unknown strategy_id: {strategy_id}")
        if exec_lag < 1:
            raise ValueError(
                f"exec_lag must be >= 1, got {exec_lag} (00 §6.5 G2)"
            )
        self._strategy_types[strategy_id] = strategy_type

        fill_bar_time = bar_time + timedelta(seconds=exec_lag * self._bar_seconds)

        orders: list[Order] = []
        # Q4: sequential application, each sees post-previous state
        for signal in signals:
            valid = _VALID_DIRECTIONS[strategy_type]
            if signal.direction not in valid:
                raise ValueError(
                    f"strategy {strategy_id!r} is {strategy_type.value}; "
                    f"direction {signal.direction.value} not allowed "
                    f"(valid: {sorted(d.value for d in valid)})"
                )
            order = self._apply_one(
                strategy_id, signal, fill_bar_time, fill_price_lookup
            )
            orders.append(order)
            self.fill_history.append(order)
        return orders

    # ---------- single-signal application (06 §4.1-§4.4) ----------

    def _apply_one(
        self,
        strategy_id: str,
        signal: Signal,
        fill_bar_time: datetime,
        lookup: FillLookup,
    ) -> Order:
        pool = self._pools[strategy_id]
        slot = self.get_position(strategy_id, signal.symbol)
        st = self._strategy_types[strategy_id]

        try:
            fill_price = lookup(signal.symbol, fill_bar_time, "open")
        except (KeyError, IndexError):
            return Order(
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                symbol=signal.symbol,
                direction=signal.direction,
                status="EXPIRED",
                requested_shares=0.0,
                rejection_reason=(
                    f"no bar at fill time {fill_bar_time} (past data end)"
                ),
                rejection_kind="BROKER_ERROR",
            )
        if fill_price is None:
            return Order(
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                symbol=signal.symbol,
                direction=signal.direction,
                status="REJECTED",
                requested_shares=0.0,
                rejection_reason=f"no open price for {signal.symbol} at {fill_bar_time}",
                rejection_kind="BROKER_ERROR",
            )

        # Market order default; LimitOrder → v1 fills at market + WARN (05 §3.2)
        if isinstance(signal.price, LimitOrder):
            log.warning(
                "limit orders not simulated in v1 BacktestBroker; "
                "signal %s filled at market (%.4f)",
                signal.signal_id[:8], fill_price,
            )
        elif not isinstance(signal.price, MarketOrder):  # LimitRange
            log.warning(
                "LimitRange order not simulated in v1; signal %s filled at market",
                signal.signal_id[:8],
            )

        # FLAT handled first: close ALL this strategy's slots (06 §4.4)
        if signal.direction == Direction.FLAT:
            self._apply_flat(strategy_id, fill_bar_time, lookup)
            return Order(
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                symbol=signal.symbol,
                direction=Direction.FLAT,
                status="FILLED",
                requested_shares=0.0,
                filled_shares=0.0,
                fill_price=fill_price,
                fill_time=fill_bar_time,
            )

        try:
            requested = self._compute_shares(signal, pool, slot, fill_price)
        except ValueError as exc:
            return Order(
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                symbol=signal.symbol,
                direction=signal.direction,
                status="REJECTED",
                requested_shares=0.0,
                rejection_reason=str(exc),
                rejection_kind="INVALID_SIZE",
            )

        if requested <= 0:
            return Order(
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                symbol=signal.symbol,
                direction=signal.direction,
                status="REJECTED",
                requested_shares=0.0,
                rejection_reason="no-op: computed shares <= 0",
                rejection_kind="INVALID_SIZE",
            )

        if signal.direction == Direction.LONG:
            order = self._open_long(pool, slot, signal, requested, fill_price, fill_bar_time)
        elif signal.direction == Direction.SHORT:
            order = self._open_short(pool, slot, signal, requested, fill_price, fill_bar_time)
        elif signal.direction == Direction.CLOSE_LONG:
            order = self._close(pool, slot, signal, requested, fill_price, fill_bar_time,
                                Direction.CLOSE_LONG)
        elif signal.direction == Direction.CLOSE_SHORT:
            order = self._close(pool, slot, signal, requested, fill_price, fill_bar_time,
                                Direction.CLOSE_SHORT)
        else:  # pragma: no cover
            raise ValueError(f"unhandled direction {signal.direction}")
        return order

    def _compute_shares(
        self,
        signal: Signal,
        pool: CashPool,
        slot: PositionSlot,
        fill_price: float,
    ) -> float:
        """Resolve size union → share count for the signal (06 §4.1-4.3).

        Returns 0 for no-op (FLAT handled separately).
        """
        size = signal.size
        if isinstance(size, FixedSize):
            return float(size.shares)
        if isinstance(size, PctSize):
            if signal.direction in (Direction.CLOSE_LONG, Direction.CLOSE_SHORT):
                return slot.shares * size.pct
            return (pool.current_cash * size.pct) / fill_price
        if isinstance(size, RiskSize):
            # entry price assumed = fill price (paper/backtest, 06 §4.1)
            if signal.direction in (Direction.CLOSE_LONG, Direction.CLOSE_SHORT):
                raise ValueError("RiskSize cannot be used to close a position")
            risk_per_share = abs(fill_price - size.stop_loss)
            if risk_per_share == 0:
                raise ValueError(
                    f"RiskSize.stop_loss == entry price ({fill_price}), "
                    f"infinite shares"
                )
            if signal.direction == Direction.LONG and size.stop_loss >= fill_price:
                raise ValueError(
                    f"RiskSize: LONG stop_loss ({size.stop_loss}) must be < "
                    f"entry ({fill_price})"
                )
            if signal.direction == Direction.SHORT and size.stop_loss <= fill_price:
                raise ValueError(
                    f"RiskSize: SHORT stop_loss ({size.stop_loss}) must be > "
                    f"entry ({fill_price})"
                )
            return size.risk_amount / risk_per_share
        raise ValueError(f"unknown size type {type(size).__name__}")

    def _open_long(self, pool, slot, signal, shares, fill_price, fill_bar_time) -> Order:
        """Buy shares; insufficient cash → scale down + WARN (06 §4.1)."""
        required = shares * fill_price
        if required > pool.current_cash:
            affordable = pool.current_cash / fill_price
            if affordable <= 0:
                return Order(
                    signal_id=signal.signal_id, strategy_id=pool.strategy_id,
                    symbol=slot.symbol, direction=Direction.LONG, status="REJECTED",
                    requested_shares=shares, rejection_reason=(
                        f"insufficient long pool cash: need ${required:.2f}, "
                        f"have ${pool.current_cash:.2f}"
                    ), rejection_kind="INSUFFICIENT_CASH",
                )
            log.warning(
                "[%s][%s] insufficient long pool cash: need $%.2f, have $%.2f, "
                "adjusted to %.2f shares",
                pool.strategy_id, slot.symbol, required, pool.current_cash, affordable,
            )
            shares = affordable

        self._add(slot, shares, fill_price, Direction.LONG)
        pool.current_cash -= shares * fill_price
        return Order(
            signal_id=signal.signal_id, strategy_id=pool.strategy_id,
            symbol=slot.symbol, direction=Direction.LONG, status="FILLED",
            requested_shares=shares, filled_shares=shares,
            fill_price=fill_price, fill_time=fill_bar_time,
        )

    def _open_short(self, pool, slot, signal, shares, fill_price, fill_bar_time) -> Order:
        """Sell short; proceeds add to cash (v1 unlimited borrow, 06 §4.2)."""
        if shares <= 0:
            return Order(
                signal_id=signal.signal_id, strategy_id=pool.strategy_id,
                symbol=slot.symbol, direction=Direction.SHORT, status="REJECTED",
                requested_shares=shares, rejection_reason="SHORT shares must be > 0",
                rejection_kind="INVALID_SIZE",
            )
        self._add(slot, shares, fill_price, Direction.SHORT)
        pool.current_cash += shares * fill_price
        return Order(
            signal_id=signal.signal_id, strategy_id=pool.strategy_id,
            symbol=slot.symbol, direction=Direction.SHORT, status="FILLED",
            requested_shares=shares, filled_shares=shares,
            fill_price=fill_price, fill_time=fill_bar_time,
        )

    def _close(self, pool, slot, signal, shares, fill_price, fill_bar_time,
               direction: Direction) -> Order:
        """Reduce position + realize PnL (06 §4.3).  Q2: close > current → all."""
        st = StrategyType.LONG if direction == Direction.CLOSE_LONG else StrategyType.SHORT
        if slot.shares <= 0:
            return Order(
                signal_id=signal.signal_id, strategy_id=pool.strategy_id,
                symbol=slot.symbol, direction=direction, status="REJECTED",
                requested_shares=shares, rejection_reason=(
                    f"no {st.value} position to close"
                ), rejection_kind="INVALID_SIZE",
            )
        to_close = min(shares, slot.shares)  # Q2: cap at position
        if to_close < shares:
            log.warning(
                "[%s][%s] close requested %.2f shares, only %.2f available, "
                "closing all",
                pool.strategy_id, slot.symbol, shares, slot.shares,
            )
        if to_close <= 0:
            return Order(
                signal_id=signal.signal_id, strategy_id=pool.strategy_id,
                symbol=slot.symbol, direction=direction, status="REJECTED",
                requested_shares=shares, rejection_reason="nothing to close",
                rejection_kind="INVALID_SIZE",
            )

        if direction == Direction.CLOSE_LONG:
            realized = (fill_price - slot.avg_cost) * to_close
            pool.current_cash += to_close * fill_price
        else:
            realized = (slot.avg_cost - fill_price) * to_close
            pool.current_cash -= to_close * fill_price

        slot.shares -= to_close
        slot.realized_pnl += realized
        pool.total_realized_pnl += realized
        if slot.shares == 0:
            slot.avg_cost = 0.0

        return Order(
            signal_id=signal.signal_id, strategy_id=pool.strategy_id,
            symbol=slot.symbol, direction=direction, status="FILLED",
            requested_shares=shares, filled_shares=to_close,
            fill_price=fill_price, fill_time=fill_bar_time,
        )

    def _add(self, slot: PositionSlot, shares: float, fill_price: float,
             direction: Direction) -> None:
        """Q1 weighted-average cost on adds (06 §5.1)."""
        new_total_cost = slot.shares * slot.avg_cost + shares * fill_price
        slot.shares += shares
        slot.avg_cost = new_total_cost / slot.shares

    def _apply_flat(self, strategy_id: str, fill_bar_time: datetime,
                    lookup: FillLookup) -> None:
        """FLAT: close ALL slots of this strategy (06 §4.4)."""
        for key, slot in list(self._slots.items()):
            if slot.strategy_id != strategy_id or slot.shares <= 0:
                continue
            pool = self._pools[strategy_id]
            fill_price = lookup(slot.symbol, fill_bar_time, "open")
            if slot.direction == StrategyType.LONG:
                sig = Signal(
                    symbol=slot.symbol,
                    direction=Direction.CLOSE_LONG,
                    price=MarketOrder(),
                    size=FixedSize(shares=slot.shares),
                    bar_time=fill_bar_time,
                )
                self._close(pool, slot, sig, slot.shares, fill_price, fill_bar_time,
                            Direction.CLOSE_LONG)
            else:
                sig = Signal(
                    symbol=slot.symbol,
                    direction=Direction.CLOSE_SHORT,
                    price=MarketOrder(),
                    size=FixedSize(shares=slot.shares),
                    bar_time=fill_bar_time,
                )
                self._close(pool, slot, sig, slot.shares, fill_price, fill_bar_time,
                            Direction.CLOSE_SHORT)

    # ---------- finalize ----------

    def finalize(self, last_prices: dict[str, float]) -> dict:
        """Mark open positions to market at last close (06 §6.3).

        Returns summary per strategy: {strategy_id: {...}}.
        """
        summary = {}
        for sid, pool in self._pools.items():
            unrealized = 0.0
            for key, slot in self._slots.items():
                if slot.strategy_id != sid or slot.shares <= 0:
                    continue
                px = last_prices.get(slot.symbol)
                if px is None:
                    log.warning("[%s][%s] no last price for mark-to-market", sid, slot.symbol)
                    continue
                if slot.direction == StrategyType.LONG:
                    u = (px - slot.avg_cost) * slot.shares
                else:
                    u = (slot.avg_cost - px) * slot.shares
                unrealized += u
                log.info("[%s][%s] unrealized PnL at end: $%.2f", sid, slot.symbol, u)
            equity = pool.current_cash + unrealized
            summary[sid] = {
                "strategy_id": sid,
                "initial_capital": pool.initial_capital,
                "current_cash": pool.current_cash,
                "realized_pnl": pool.total_realized_pnl,
                "unrealized_pnl": unrealized,
                "equity": equity,
                "total_return": equity - pool.initial_capital,
            }
            log.info(
                "[%s] final cash: $%.2f, realized PnL: $%.2f, "
                "total return: $%.2f",
                sid, pool.current_cash, pool.total_realized_pnl,
                equity - pool.initial_capital,
            )
        return summary
