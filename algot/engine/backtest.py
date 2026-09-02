"""BacktestEngine — per-bar execution loop (per docs/03-algorithms.md §10).

v1 execution model:
    - Load full OHLCV for (symbol, timeframe) once from source.
    - Factor plugins: computed ONCE over the full series (vectorized).
      Each factor output is a full-length Sequence.
    - For each bar i (chronological), build per-bar views: raw inputs,
      factor outputs AND the OHLCVSequence `bars` sliced to [0..i] so that
      inside a signal plugin `seq[0]` is the current bar and `seq[N]` is N
      bars back (02 §2.2 semantics).
    - Signal plugins are called per bar with those views; framework drops
      emissions while bar_idx < effective min_bars (G1) + logs INFO at end.
    - Collected signals → BacktestBroker.submit (G2 exec_lag fill).

Signal plugin input space (03 §10.2):
    close/open/high/low/volume  — raw Sequences
    bars                        — OHLCVSequence (atr/vwap/adx family)
    <factor_name>               — dep factor output (registered via deps=[...]
                                  or declared as a signature param); factor
                                  inputs bound by shape_in dtype (default price
                                  = close).  Parameterized factor calls
                                  (sma(close, n=5)) are done inside the plugin
                                  body per 03 §8.1.

v1 scope: single strategy instance; single signal plugin; single symbol
(multi-symbol = host for-loop per 06 §6.2).
"""

from __future__ import annotations

import inspect
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
        inputs["bars"] = bars  # OHLCVSequence — OHLCV factors (atr/vwap/adx)

        # Resolve signal plugin signature: params it needs.
        sig_params = inspect.signature(self._signal_pc.func).parameters
        # deps declared on the plugin take precedence for factor wiring
        ordered_factors: list[str] = []
        for dep in self._signal_pc.deps:
            if dep in _REGISTRY and _REGISTRY[dep].category == "factor":
                if dep not in ordered_factors:
                    ordered_factors.append(dep)
        # any remaining factor-type params from the signature
        for pname in sig_params:
            if pname in ("state",) or pname in inputs:
                continue
            if pname in _REGISTRY and _REGISTRY[pname].category == "factor":
                if pname not in ordered_factors:
                    ordered_factors.append(pname)

        for fname in ordered_factors:
            fpc: PluginCall = _REGISTRY[fname]
            fkwargs = self._bind_factor_inputs(fpc, inputs)
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
            # G2 sanity: signal.bar_time should be THIS bar's START (02 §5.1).
            # A plugin using the Signal default (now()) would EXPIRED silently.
            bar_start = bars.close.index[i]
            if s.bar_time != bar_start:
                log.warning(
                    "[%s] signal %s bar_time %s != current bar %s; "
                    "fill may EXPIRED (use close.index[-1] as bar_time)",
                    self.strategy.id, s.signal_id[:8],
                    s.bar_time, bar_start,
                )
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

    def _bind_factor_inputs(self, fpc: PluginCall, inputs: dict[str, Any]) -> dict:
        """Bind a factor plugin's inputs by shape_in dtype semantics.

        Rules (per 03 §10.2 plugin-store wiring):
          1. param name present in inputs (raw / bars / earlier factor) → use it
          2. dtype OHLCVSequence → inputs['bars']
          3. dtype Sequence[float64] (or Sequence) → inputs['close'] (default price)
        Parameter defaults (e.g. `n=20`) are left to the function signature.
        """
        fkwargs: dict[str, Any] = {}
        for pname, dtype in fpc.shape_in.items():
            if pname in inputs:
                fkwargs[pname] = inputs[pname]
            elif dtype == "OHLCVSequence":
                fkwargs[pname] = inputs["bars"]
            elif dtype.startswith("Sequence") or dtype == "ndarray":
                fkwargs[pname] = inputs["close"]
            else:
                raise KeyError(
                    f"factor {fpc.name!r}: cannot bind input {pname!r} "
                    f"(dtype {dtype}); available: {sorted(inputs.keys())}"
                )
        return fkwargs

    def _call_signal(self, inputs: dict[str, Any], i: int) -> Signal | None:
        """Call signal plugin with per-bar views ending at bar i.

        View types mirror their source:
          - raw Sequence (close/open/…)        → Sequence  view (data[:i+1])
          - factor output Sequence              → Sequence  view
          - bars (OHLCVSequence)                → OHLCVSequence view (all fields)
        Inside the plugin, seq[0] = current bar, seq[N] = N bars back.
        """
        views: dict[str, Any] = {}
        for pname, p in inspect.signature(self._signal_pc.func).parameters.items():
            if pname == "state":
                continue
            if pname not in inputs:
                raise KeyError(
                    f"signal {self.signal_name!r} param {pname!r} not provided; "
                    f"available: {sorted(inputs.keys())}"
                )
            src = inputs[pname]
            views[pname] = self._view(src, i)
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

    @staticmethod
    def _view(src: Any, i: int) -> Any:
        """Slice full-series source to bars [0..i] (current bar = index i)."""
        if isinstance(src, OHLCVSequence):
            return OHLCVSequence(
                open=Sequence(src.open.data[: i + 1], dict(src.open.meta),
                              src.open.index[: i + 1]),
                high=Sequence(src.high.data[: i + 1], dict(src.high.meta),
                              src.high.index[: i + 1]),
                low=Sequence(src.low.data[: i + 1], dict(src.low.meta),
                             src.low.index[: i + 1]),
                close=Sequence(src.close.data[: i + 1], dict(src.close.meta),
                               src.close.index[: i + 1]),
                volume=Sequence(src.volume.data[: i + 1], dict(src.volume.meta),
                                src.volume.index[: i + 1]),
            )
        if isinstance(src, Sequence):
            return Sequence(
                data=src.data[: i + 1],
                meta=dict(src.meta),
                index=src.index[: i + 1] if src.index is not None else None,
            )
        raise TypeError(
            f"cannot build bar view for {type(src).__name__}; "
            f"expected Sequence | OHLCVSequence"
        )

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
