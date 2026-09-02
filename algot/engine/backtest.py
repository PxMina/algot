"""BacktestEngine — per-bar execution loop (per docs/03-algorithms.md §10).

v1 execution model:
    - Load full OHLCV for (symbol, timeframe) once from source.
    - Factor plugins: computed ONCE over the full series (vectorized).
      Each factor output is a full-length Sequence.
    - For each bar i (chronological), build per-bar views: factor outputs
      sliced to bars [0..i] so that inside a signal plugin `seq[0]` is the
      current bar and `seq[N]` is N bars back (02 §2.2 semantics).
    - Signal plugins are called per bar with those views; framework drops
      emissions while bar_idx < effective min_bars (G1) + logs INFO at end.
    - Collected signals → BacktestBroker.submit (G2 exec_lag fill).

v1 scope: single strategy instance; signal plugin deps resolved from the
strategy's declared signal plugin name (registered via @algot.plugin).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np

from algot.algo._core import _REGISTRY, PluginCall, get_plugin
from algot.broker.backtest import BacktestBroker
from algot.broker.base import Order, StrategyType
from algot.engine.strategy import Strategy
from algot.sequence import OHLCVSequence, Sequence
from algot.signal import Direction, Signal

log = logging.getLogger("algot.engine.backtest")

# Raw data names exposed to signal plugins (03 §10.2).
_RAW_INPUTS = ("close", "open", "high", "low", "volume")


class BacktestEngine:
    """Single-strategy backtest driver.

    Usage:
        engine = BacktestEngine(strategy, source, symbol, tf)
        result = engine.run(start=None, end=None)
    """

    def __init__(
        self,
        strategy: Strategy,
        source: Any,
        symbol: str,
        tf: tuple[int, str],
    ) -> None:
        if len(strategy.symbols) == 0 or symbol not in strategy.symbols:
            raise ValueError(
                f"symbol {symbol!r} not in strategy.symbols {strategy.symbols}"
            )
        if len(strategy.signals) != 1:
            raise ValueError(
                "v1 engine supports exactly one signal plugin per strategy; "
                f"got {strategy.signals}"
            )
        self.signal_name = strategy.signals[0]
        if self.signal_name not in _REGISTRY:
            raise KeyError(
                f"signal plugin {self.signal_name!r} not registered. "
                f"Registered signals: "
                f"{[n for n, p in _REGISTRY.items() if p.category == 'signal']}"
            )
        self._signal_pc: PluginCall = get_plugin(self.signal_name)
        if self._signal_pc.category != "signal":
            raise ValueError(
                f"plugin {self.signal_name!r} is {self._signal_pc.category}, "
                f"not signal"
            )
        self.strategy = strategy
        self.source = source
        self.symbol = symbol
        self.tf = tf
        self._bar_seconds = self.source._tf_to_seconds(tf)

    # ---------- run ----------

    def run(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """Run the backtest. Returns {summary, orders, signals, equity_curve}."""
        bars: OHLCVSequence = self.source.load_ohlcv(
            self.symbol, self.tf, start, end
        )
        n = len(bars)
        if n == 0:
            raise ValueError("no bars loaded")

        broker = BacktestBroker(
            pools={self.strategy.id: self.strategy.capital},
            bar_seconds=self._bar_seconds,
        )

        # 1. Compute factor outputs once (full series).
        raw = {name: getattr(bars, name) for name in _RAW_INPUTS}
        factor_out: dict[str, Sequence] = {}
        inputs: dict[str, Any] = dict(raw)

        # Resolve signal plugin signature: params it needs.
        import inspect
        sig_params = inspect.signature(self._signal_pc.func).parameters
        # deps declared on the plugin take precedence for factor wiring
        ordered_factors: list[str] = []
        for dep in self._signal_pc.deps:
            if dep in _REGISTRY and _REGISTRY[dep].category == "factor":
                if dep not in ordered_factors:
                    ordered_factors.append(dep)
        # any remaining factor-type params from the signature
        for pname in sig_params:
            if pname in ("state",) or pname in _RAW_INPUTS:
                continue
            if pname in _REGISTRY and _REGISTRY[pname].category == "factor":
                if pname not in ordered_factors:
                    ordered_factors.append(pname)

        for fname in ordered_factors:
            fpc: PluginCall = _REGISTRY[fname]
            # bind factor params: raw inputs by param name
            fkwargs: dict[str, Any] = {}
            for pname in inspect.signature(fpc.func).parameters:
                if pname in raw:
                    fkwargs[pname] = raw[pname]
                elif pname == "n" or pname == "period":
                    # default param n already bound by signature default;
                    # call without explicit override
                    continue
            result = fpc.call(deps_kwds=fkwargs, state=None)
            if not isinstance(result, Sequence):
                raise TypeError(
                    f"factor {fname!r} returned {type(result).__name__}, "
                    f"expected Sequence"
                )
            factor_out[fname] = result
            inputs[fname] = result

        # Warmup drop threshold: max min_bars across signal + its factors (G1).
        min_bars = self._signal_pc.min_bars
        for fname in ordered_factors:
            min_bars = max(min_bars, _REGISTRY[fname].min_bars)

        # 2. Per-bar loop: build views, call signal, emit → broker.
        all_signals: list[Signal] = []
        dropped_warmup = 0
        self._signal_pc.init_state()

        for i in range(n):
            if i < min_bars:
                # still call plugin (state updates), but drop any emission
                s = self._call_signal(inputs, i)
                if s is not None:
                    dropped_warmup += 1
                continue

            s = self._call_signal(inputs, i)
            if s is None:
                continue
            all_signals.append(s)
            broker.submit(
                strategy_id=self.strategy.id,
                strategy_type=self.strategy.type,
                signals=[s],
                bar_time=s.bar_time,
                fill_price_lookup=self._make_lookup(bars),
                exec_lag=self.strategy.exec_lag,
            )

        if dropped_warmup:
            log.info(
                "[%s] dropped %d signal(s) during warmup (bar_idx < %d)",
                self.strategy.id, dropped_warmup, min_bars,
            )

        # 3. Finalize: mark to market at last close.
        last_prices = {self.symbol: float(bars.close.data[-1])}
        summary = broker.finalize(last_prices)
        orders = [o for o in broker.fill_history]

        return {
            "strategy_id": self.strategy.id,
            "symbol": self.symbol,
            "tf": self.tf,
            "summary": summary,
            "orders": orders,
            "signals": all_signals,
            "dropped_warmup": dropped_warmup,
            "min_bars": min_bars,
            "bars": bars,
        }

    # ---------- helpers ----------

    def _call_signal(self, inputs: dict[str, Any], i: int) -> Signal | None:
        """Call signal plugin with per-bar views ending at bar i."""
        views: dict[str, Any] = {}
        import inspect
        for pname, p in inspect.signature(self._signal_pc.func).parameters.items():
            if pname == "state":
                continue
            if pname not in inputs:
                raise KeyError(
                    f"signal {self.signal_name!r} param {pname!r} not provided; "
                    f"available: {sorted(inputs.keys())}"
                )
            src = inputs[pname]
            # slice view: bars [0..i] of the underlying data
            data_view = src.data[: i + 1]
            idx_view = src.index[: i + 1] if src.index is not None else None
            views[pname] = Sequence(
                data=data_view,
                meta=dict(src.meta),
                index=idx_view,
            )
        result = self._signal_pc.call(
            deps_kwds=views, state=self._signal_pc.get_state()
        )
        if result is None:
            return None
        if not isinstance(result, Signal):
            raise TypeError(
                f"signal {self.signal_name!r} returned "
                f"{type(result).__name__}, expected Signal | None"
            )
        return result

    def _make_lookup(self, bars: OHLCVSequence):
        """fill_price_lookup(symbol, bar_time, field) → open at bar_time."""
        index = bars.close.index  # DatetimeIndex UTC
        fields = {
            "open": bars.open.data,
            "high": bars.high.data,
            "low": bars.low.data,
            "close": bars.close.data,
            "volume": bars.volume.data,
        }

        def lookup(symbol: str, bar_time: datetime, field: str = "open") -> float:
            if symbol != self.symbol:
                raise KeyError(f"unknown symbol {symbol!r}")
            if field not in fields:
                raise KeyError(f"unknown field {field!r}")
            # locate bar by exact time
            pos = index.get_indexer([bar_time], method=None)[0]
            if pos < 0:
                raise KeyError(f"no bar at {bar_time}")
            return float(fields[field][pos])

        return lookup
