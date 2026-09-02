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
        Price: MarketOrder, LimitOrder, LimitRange
        Size: FixedSize, PctSize, RiskSize

    Broker (from algot.broker):
        BacktestBroker, PositionSlot, CashPool, Order, StrategyType

    Engine (from algot.engine):
        Strategy, BacktestEngine
"""

from algot.sequence import OHLCVSequence, Sequence
from algot.signal import (
    Direction,
    FixedSize,
    LimitOrder,
    LimitRange,
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
    "MarketOrder", "LimitOrder", "LimitRange", "Price",
    "FixedSize", "PctSize", "RiskSize", "Size",
    # plugin framework
    "plugin", "_REGISTRY",
    # config + cli
    "parse_strategy_yaml", "StrategyConfig", "ConfigError",
    # built-in factors
    "sma", "ema", "rsi", "atr", "adx", "stddev", "vwap",
    "donchian_high", "donchian_low",
    "crossover", "crossunder", "resample", "shift",
    # broker + engine
    "BacktestBroker", "PositionSlot", "CashPool", "Order", "StrategyType",
    "Strategy", "BacktestEngine",
    "__version__",
]


# Eager-import builtins. Order matters:
#   1. sequence + signal (no internal deps)
#   2. source (depends on sequence)
#   3. algo.plugin (depends on _core)
#   4. _core (registry)
#   5. builtins.factor (depends on plugin + sequence)
from algot.source import BaseSource, SqliteSource
from algot.algo.plugin import plugin
from algot.algo._core import _REGISTRY
from algot.algo.builtins.factor import (
    sma, ema, rsi, atr, adx, stddev, vwap,
    donchian_high, donchian_low,
    crossover, crossunder, resample, shift,
)

# config (yaml-driven strategies; used by algot.cli)
from algot.config import ConfigError, StrategyConfig, parse_strategy_yaml

# broker + engine (import after signal/sequence deps)
from algot.broker import BacktestBroker, CashPool, Order, PositionSlot, StrategyType
from algot.engine import BacktestEngine, Strategy