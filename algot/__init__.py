"""algot — Algorithmic Trading Workbench.

Public API (v1):
    Data layer:
        Sequence, OHLCVSequence (from algot.sequence)
        BaseSource, SqliteSource (from algot.source)

    Plugin framework (from algot.algo):
        plugin (decorator)
        _REGISTRY (global plugin registry)

    Built-in plugins (from algot.algo.builtins):
        sma, ema, rsi, atr, adx, stddev, vwap
        donchian_high, donchian_low
        crossover, crossunder
        resample, shift

    Signals (from algot.signal):
        Signal, Direction
        Price: MarketOrder, LimitOrder
        Size: FixedSize, PctSize, RiskSize
"""

from algot.sequence import OHLCVSequence, Sequence
from algot.signal import (
    Direction,
    FixedSize,
    LimitOrder,
    MarketOrder,
    PctSize,
    Price,
    RiskSize,
    Signal,
    Size,
)

__version__ = "0.1.0"

__all__ = [
    # data layer
    "Sequence", "OHLCVSequence",
    "BaseSource", "SqliteSource",
    # signals
    "Signal", "Direction",
    "MarketOrder", "LimitOrder", "Price",
    "FixedSize", "PctSize", "RiskSize", "Size",
    # plugin framework
    "plugin", "_REGISTRY",
    # built-in factors (lazy import to avoid circular)
    "sma", "ema", "rsi", "atr", "adx", "stddev", "vwap",
    "donchian_high", "donchian_low",
    "crossover", "crossunder", "resample", "shift",
    "__version__",
]


# Lazy-import builtins to avoid circular: _core depends on nothing in algot,
# but builtins uses Sequence + plugin. Let me just import them explicitly.
from algot.source import BaseSource, SqliteSource
from algot.algo.plugin import plugin
from algot.algo._core import _REGISTRY
from algot.algo.builtins.factor import (
    sma, ema, rsi, atr, adx, stddev, vwap,
    donchian_high, donchian_low,
    crossover, crossunder, resample, shift,
)