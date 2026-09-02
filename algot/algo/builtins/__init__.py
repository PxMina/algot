"""Built-in plugins (per docs/03-algorithms.md §11).

v1 builtins:
    factors: sma, ema, rsi, atr, adx, stddev, vwap,
             donchian_high, donchian_low, crossover, crossunder, resample, shift
    signals: (none — examples in examples/ dir)
"""

from algot.algo.builtins.factor import (
    adx,
    atr,
    crossover,
    crossunder,
    donchian_high,
    donchian_low,
    ema,
    resample,
    rsi,
    shift,
    sma,
    stddev,
    vwap,
)

__all__ = [
    "adx", "atr", "crossover", "crossunder",
    "donchian_high", "donchian_low", "ema",
    "resample", "rsi", "shift", "sma",
    "stddev", "vwap",
]