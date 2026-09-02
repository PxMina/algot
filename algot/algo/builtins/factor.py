"""Built-in factors (per docs/03-algorithms.md §11).

Implementation notes:
    - All factors accept Sequence[float64] or OHLCVSequence.
    - Bars where insufficient data → NaN (per G1 warmup).
    - For each factor, 'period' is the N parameter (window size).
    - In v1, period is hardcoded per factor; user-defined period via
      parameterization is a v1.x feature.

Factor list (per 03 §11):
    sma           : simple moving average
    ema           : exponential moving average
    rsi           : Relative Strength Index (period=14)
    atr           : Average True Range (period=14, needs OHLCV)
    adx           : Average Directional Index (period=14, needs OHLCV)
    stddev        : rolling standard deviation
    vwap          : Volume-Weighted Average Price (needs OHLCV)
    donchian_high : rolling max
    donchian_low  : rolling min
    crossover     : x crosses above y → 1.0 at bar, else 0.0
    crossunder    : x crosses below y → 1.0 at bar, else 0.0
    resample      : upsample to coarser timeframe
    shift         : lag/lead (shift back by N)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from algot.algo.plugin import plugin
from algot.sequence import OHLCVSequence, Sequence

if TYPE_CHECKING:
    pass


# ---------- helpers ----------

def _ensure_seq(x) -> Sequence:
    """Coerce to Sequence (raise if not Sequence)."""
    if isinstance(x, Sequence):
        return x
    raise TypeError(
        f"expected Sequence (or subclass), got {type(x).__name__}"
    )


def _ensure_ohlcv(x) -> OHLCVSequence:
    """Coerce to OHLCVSequence (raise if not)."""
    if isinstance(x, OHLCVSequence):
        return x
    raise TypeError(
        f"expected OHLCVSequence, got {type(x).__name__}"
    )


def _na_seq(n: int, like: Sequence) -> Sequence:
    """All-NaN Sequence of length n, with index + meta copied from like."""
    data = np.full(n, np.nan, dtype=np.float64)
    return Sequence(data=data, meta=dict(like.meta), index=like.index)


# Indexing convention reminder (per 00 §3.2):
#   seq[N]        = N steps back, seq[0] = current
#   seq.data[0]   = OLDEST bar (left)
#   seq.data[-1]  = CURRENT bar (right)
# Window for "rolling mean at position i (i steps back from current)" =
#   i.e. for seq[i] (i steps back), rolling mean over window ending at i:
#     start = len(seq) - 1 - i     # index in data array for position i
#     end   = start + period       # window of size period


# ---------- sma ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def sma(x, n=20):
    """Simple moving average over a rolling window (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: rolling window period (default 20)

    Returns:
        Sequence[float64] of same length.
        seq[N] = NaN for N < n-1 (window not full yet);
        seq[N] = mean of last `n` bars ending at seq[N] otherwise.
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n:
        return _na_seq(length, seq)

    result = np.full(length, np.nan, dtype=np.float64)
    for j in range(n - 1, length):
        window = seq.data[j - n + 1 : j + 1]
        result.data[j] = float(np.mean(window))
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


# ---------- ema ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def ema(x, n=20):
    """Exponential moving average (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: smoothing period (default 20); alpha = 2/(n+1)

    Returns:
        Sequence[float64]. NaN for first n-1 bars (SMA seed at j=n-1).
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n:
        return _na_seq(length, seq)

    alpha = 2.0 / (n + 1)
    result = np.full(length, np.nan, dtype=np.float64)

    # Seed with SMA at first valid chronological position (j = n-1)
    result.data[n - 1] = float(np.mean(seq.data[:n]))

    # EMA updates: result.data[j] = α·data[j] + (1-α)·result.data[j-1]
    for j in range(n, length):
        result.data[j] = alpha * seq.data[j] + (1 - alpha) * result.data[j - 1]

    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


# ---------- rsi ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=14,
)
def rsi(x, n=14):
    """Relative Strength Index (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: period (default 14, industry standard)

    Returns:
        Sequence[float64]. Values 0-100. NaN for bars < n.
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n + 1:
        return _na_seq(length, seq)

    # Compute deltas in chronological order: data[0]=oldest, data[length-1]=newest
    deltas = np.diff(seq.data)  # length-1
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    raw = np.full(length, np.nan, dtype=np.float64)

    # First RSI at chronological position n: avg over gains[:n]
    avg_gain = float(np.mean(gains[:n]))
    avg_loss = float(np.mean(losses[:n]))

    if avg_loss == 0:
        raw[n] = 100.0
    else:
        rs = avg_gain / avg_loss
        raw[n] = 100.0 - 100.0 / (1.0 + rs)

    # Wilder's smoothing: avg = (avg·(n-1) + new) / n
    for i in range(n + 1, length):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n

        if avg_loss == 0:
            raw[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            raw[i] = 100.0 - 100.0 / (1.0 + rs)

    return Sequence(data=raw, meta=dict(seq.meta), index=seq.index)


# ---------- atr ----------

@plugin(
    category="factor",
    shape_in={"x": "OHLCVSequence"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=14,
)
def atr(x, n=14):
    """Average True Range (per 03 §11).

    Args:
        x: OHLCVSequence
        n: period (default 14)

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    """
    bars = _ensure_ohlcv(x)
    length = len(bars)
    if length < n + 1:
        return _na_seq(length, bars.close)

    # Compute TR for bars[1..length-1]
    tr = np.zeros(length - 1)
    for i in range(1, length):
        h = bars.high.data[i]
        l = bars.low.data[i]
        pc = bars.close.data[i - 1]
        tr[i - 1] = max(h - l, abs(h - pc), abs(l - pc))

    # NaN until position n, seeded with SMA of tr[:n]
    raw = np.full(length, np.nan, dtype=np.float64)
    raw[n] = float(np.mean(tr[:n]))

    # Wilder's smoothing
    for i in range(n, length - 1):
        raw[i + 1] = (raw[i] * (n - 1) + tr[i]) / n

    return Sequence(data=raw, meta=dict(bars.close.meta), index=bars.close.index)


# ---------- adx ----------

@plugin(
    category="factor",
    shape_in={"x": "OHLCVSequence"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=14,
)
def adx(x, n=14):
    """Average Directional Index (per 03 §11).

    Args:
        x: OHLCVSequence
        n: period (default 14)

    +DI = 100 * avg(+DM) / avg(TR); -DI = 100 * avg(-DM) / avg(TR)
    DX = 100 * |+DI - -DI| / (+DI + -DI)
    ADX = Wilder's-smoothed mean of DX
    """
    bars = _ensure_ohlcv(x)
    length = len(bars)
    if length < n * 2 + 1:
        return _na_seq(length, bars.close)

    # Compute TR, +DM, -DM
    tr = np.zeros(length - 1)
    plus_dm = np.zeros(length - 1)
    minus_dm = np.zeros(length - 1)

    for i in range(1, length):
        h = bars.high.data[i]
        l = bars.low.data[i]
        ph = bars.high.data[i - 1]
        pl = bars.low.data[i - 1]
        pc = bars.close.data[i - 1]

        tr[i - 1] = max(h - l, abs(h - pc), abs(l - pc))
        up_move = h - ph
        down_move = pl - l
        if up_move > down_move and up_move > 0:
            plus_dm[i - 1] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i - 1] = down_move

    # Wilder smoothing of TR, +DM, -DM
    def wilder(values: np.ndarray, period: int) -> np.ndarray:
        result = np.zeros(len(values) + 1)
        result[period] = float(np.sum(values[:period]))
        for i in range(period, len(values)):
            result[i + 1] = result[i] - result[i] / period + values[i]
        return result

    atr_smooth = wilder(tr, n)
    plus_dm_smooth = wilder(plus_dm, n)
    minus_dm_smooth = wilder(minus_dm, n)

    # +DI and -DI
    plus_di = np.zeros(length - 1)
    minus_di = np.zeros(length - 1)
    for i in range(length - 1):
        if atr_smooth[i + 1] != 0:
            plus_di[i] = 100.0 * plus_dm_smooth[i + 1] / atr_smooth[i + 1]
            minus_di[i] = 100.0 * minus_dm_smooth[i + 1] / atr_smooth[i + 1]

    # DX
    dx = np.zeros(length - 1)
    for i in range(length - 1):
        denom = plus_di[i] + minus_di[i]
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

    # ADX = Wilder-smoothed mean of DX
    raw = np.full(length, np.nan, dtype=np.float64)
    if len(dx) >= n * 2:
        adx_val = float(np.mean(dx[:n]))
        raw[n * 2] = adx_val
        for i in range(n * 2 + 1, length):
            raw[i] = (raw[i - 1] * (n - 1) + dx[i - 1 - n]) / n

    return Sequence(data=raw, meta=dict(bars.close.meta), index=bars.close.index)


# ---------- stddev ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def stddev(x, n=20):
    """Rolling standard deviation (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: window period (default 20). Population stddev (ddof=0).
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n:
        return _na_seq(length, seq)

    result = np.full(length, np.nan, dtype=np.float64)
    for j in range(n - 1, length):
        window = seq.data[j - n + 1 : j + 1]
        result.data[j] = float(np.std(window, ddof=0))
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


# ---------- vwap ----------

@plugin(
    category="factor",
    shape_in={"x": "OHLCVSequence"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def vwap(x):
    """Volume-Weighted Average Price, cumulative from start (per 03 §11).

    Typical price = (high + low + close) / 3.
    Cumulative VWAP at each bar.
    """
    bars = _ensure_ohlcv(x)
    n = len(bars)

    raw = np.full(n, np.nan, dtype=np.float64)
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        tp = (bars.high.data[i] + bars.low.data[i] + bars.close.data[i]) / 3.0
        cum_pv += tp * bars.volume.data[i]
        cum_v += bars.volume.data[i]
        if cum_v > 0:
            raw[i] = cum_pv / cum_v
    return Sequence(data=raw, meta=dict(bars.close.meta), index=bars.close.index)


# ---------- donchian_high / donchian_low ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def donchian_high(x, n=20):
    """Rolling max over window (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: window period (default 20)
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n:
        return _na_seq(length, seq)

    result = np.full(length, np.nan, dtype=np.float64)
    for j in range(n - 1, length):
        window = seq.data[j - n + 1 : j + 1]
        result.data[j] = float(np.max(window))
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def donchian_low(x, n=20):
    """Rolling min over window (per 03 §11).

    Args:
        x: input Sequence[float64]
        n: window period (default 20)
    """
    seq = _ensure_seq(x)
    length = len(seq)
    if length < n:
        return _na_seq(length, seq)

    result = np.full(length, np.nan, dtype=np.float64)
    for j in range(n - 1, length):
        window = seq.data[j - n + 1 : j + 1]
        result.data[j] = float(np.min(window))
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


# ---------- crossover / crossunder ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]", "y": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def crossover(x, y):
    """Crossover: x crosses above y → 1.0 at that bar, else 0.0 (03 §11).

    result.data[j] = 1.0 iff x_j > y_j and x_{j-1} <= y_{j-1}
    (j=0 is never a cross; j is chronological data position).
    TradingView semantics: golden cross fires on the bar where the
    fast line first closes above the slow line.
    """
    seq_x = _ensure_seq(x)
    seq_y = _ensure_seq(y)
    n = len(seq_x)
    if len(seq_y) != n:
        raise ValueError(
            f"crossover: x and y must be same length, got {n} vs {len(seq_y)}"
        )

    result = np.zeros(n, dtype=np.float64)
    xd, yd = seq_x.data, seq_y.data
    for j in range(1, n):
        # skip comparisons where either side is NaN (NaN passthrough)
        if np.isnan(xd[j]) or np.isnan(yd[j]) \
                or np.isnan(xd[j - 1]) or np.isnan(yd[j - 1]):
            continue
        if xd[j] > yd[j] and xd[j - 1] <= yd[j - 1]:
            result[j] = 1.0
    return Sequence(data=result, meta=dict(seq_x.meta), index=seq_x.index)


@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]", "y": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def crossunder(x, y):
    """Crossunder: x crosses below y → 1.0 at that bar, else 0.0 (03 §11).

    result.data[j] = 1.0 iff x_j < y_j and x_{j-1} >= y_{j-1}.
    Death-cross semantics.
    """
    seq_x = _ensure_seq(x)
    seq_y = _ensure_seq(y)
    n = len(seq_x)
    if len(seq_y) != n:
        raise ValueError(
            f"crossunder: x and y must be same length, got {n} vs {len(seq_y)}"
        )

    result = np.zeros(n, dtype=np.float64)
    xd, yd = seq_x.data, seq_y.data
    for j in range(1, n):
        if np.isnan(xd[j]) or np.isnan(yd[j]) \
                or np.isnan(xd[j - 1]) or np.isnan(yd[j - 1]):
            continue
        if xd[j] < yd[j] and xd[j - 1] >= yd[j - 1]:
            result[j] = 1.0
    return Sequence(data=result, meta=dict(seq_x.meta), index=seq_x.index)


# ---------- resample ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def resample(x):
    """Upsample to coarser timeframe (per 03 §11).

    v1: upsampling only. N is implicit period from min_bars.

    Args:
        x: input Sequence (any unit)

    Returns:
        Sequence with same length (NaN for incomplete windows).
    """
    period = 5
    seq = _ensure_seq(x)
    n = len(seq)
    if n < period:
        return _na_seq(n, seq)

    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(period - 1, n):
        start = n - 1 - i
        window = seq.data[start:start + period]
        # Default agg: last (most recent in window = window[-1])
        result[i] = float(window[-1])
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)


# ---------- shift ----------

@plugin(
    category="factor",
    shape_in={"x": "Sequence[float64]"},
    shape_out="Sequence[float64]",
    pure=True,
    min_bars=0,
)
def shift(x):
    """Shift sequence back by N steps (per 03 §11).

    seq[N] of output = seq[N + n_shift] of input.
    Bars where shifted past end → NaN.

    In data-array terms:
        result.data[i] = seq.data[i - n_shift]  for i in [n_shift..n-1]
        result.data[0..n_shift-1] = NaN
    """
    n_shift = 1
    seq = _ensure_seq(x)
    n = len(seq)
    if n <= n_shift:
        return _na_seq(n, seq)

    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(n_shift, n):
        result.data[i] = seq.data[i - n_shift]
    return Sequence(data=result, meta=dict(seq.meta), index=seq.index)