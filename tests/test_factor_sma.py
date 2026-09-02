"""Built-in factor tests (per docs/03-algorithms.md §11).

Covers: sma, ema, rsi, atr, adx, stddev, vwap, donchian_h/l, crossover, crossunder, resample, shift
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import algot
from algot import Sequence, OHLCVSequence
from algot.algo import _REGISTRY, get_plugin, clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure builtins are registered (they self-register on import)."""
    # Importing builtins triggers registration; just verify they're there
    from algot.algo.builtins import factor as _f  # noqa: F401
    yield


def _make_seq(values: list[float], name: str = "TEST") -> Sequence:
    data = np.array(values, dtype=np.float64)
    index = pd.DatetimeIndex(
        [f"2024-01-01 09:{i:02d}" for i in range(len(values))],
        tz="UTC",
    )
    return Sequence(
        data=data,
        meta={"symbol": name, "timeframe": (1, "min"), "unit": "min"},
        index=index,
    )


# ============================================================================
# sma tests
# ============================================================================

def test_sma_registered():
    """sma is registered in _REGISTRY."""
    assert "sma" in _REGISTRY


def test_sma_metadata():
    pc = get_plugin("sma")
    assert pc.category == "factor"
    assert pc.shape_in == {"x": "Sequence[float64]"}
    assert pc.shape_out == "Sequence[float64]"
    assert pc.min_bars == 0


def test_sma_length_too_short_returns_all_nan():
    """n < 20: all NaN."""
    seq = _make_seq([1.0, 2.0, 3.0])
    result = algot.sma(seq)
    assert len(result) == 3
    assert np.all(np.isnan(result.data))


def test_sma_at_period_first_valid():
    """At i=19 (20th bar from current), result becomes valid."""
    # 20 values: [1, 2, ..., 20]
    seq = _make_seq(list(range(1, 21)))
    result = algot.sma(seq)
    # result[19] = mean(seq.data[0:20]) = mean(1..20) = 10.5
    assert result.data[19] == pytest.approx(10.5)


def test_sma_rolling_correctness():
    """Each result.data[j] = mean(data[j-period+1 : j+1])."""
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    seq = _make_seq(values)
    result = algot.sma(seq)

    # result.data[19] = mean(data[0:20]) = mean(1..20) = 10.5
    assert result.data[19] == pytest.approx(10.5)

    # result.data[20] = mean(data[1:21]) = mean(2..21) = 11.5
    assert result.data[20] == pytest.approx(11.5)


def test_sma_nan_before_period():
    """Bars 0..18 = NaN (period=20)."""
    seq = _make_seq(list(range(1, 25)))  # 24 values
    result = algot.sma(seq)
    for i in range(19):
        assert np.isnan(result.data[i]), f"result[{i}] should be NaN, got {result.data[i]}"


def test_sma_preserves_index():
    seq = _make_seq(list(range(1, 21)))
    result = algot.sma(seq)
    assert result.index.equals(seq.index)


def test_sma_preserves_meta():
    seq = _make_seq(list(range(1, 21)), name="AAPL")
    result = algot.sma(seq)
    assert result.meta["symbol"] == "AAPL"


def test_sma_raises_on_non_sequence():
    """Passing a non-Sequence raises."""
    with pytest.raises(TypeError, match="expected Sequence"):
        algot.sma([1, 2, 3, 4])  # type: ignore[arg-type]


def test_sma_nan_passthrough():
    """NaN in input propagates to output."""
    values = [1, 2, np.nan, 4, 5] + [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    seq = _make_seq(values)
    result = algot.sma(seq)
    # The NaN at index 2 (data position) propagates into many windows
    # result[17] = mean(data[0:20]) includes index 2 → NaN
    assert np.isnan(result.data[17])


def test_sma_call_via_plugin():
    """Call via PluginCall.call()."""
    seq = _make_seq(list(range(1, 21)))
    pc = get_plugin("sma")
    result = pc.call(deps_kwds={"x": seq})
    assert result.data[19] == pytest.approx(10.5)


# ============================================================================
# ema tests
# ============================================================================

def test_ema_registered():
    assert "ema" in _REGISTRY


def test_ema_at_period_seeded_with_sma():
    """EMA seeded with SMA at bar period-1."""
    seq = _make_seq(list(range(1, 21)))
    result = algot.ema(seq)
    assert result.data[19] == pytest.approx(10.5)  # same as SMA at seed


def test_ema_subsequent_updates():
    """EMA[j] = α·data[j] + (1-α)·EMA[j-1]."""
    seq21 = _make_seq(list(range(1, 22)))  # n=21
    result = algot.ema(seq21)
    alpha = 2.0 / 21.0

    # result.data[19] is seed = SMA(1..20) = 10.5
    assert result.data[19] == pytest.approx(10.5)

    # result.data[20] = α·data[20] + (1-α)·result.data[19]
    # data[20] = 21 (since range(1, 22) = 1..21)
    expected = alpha * 21.0 + (1 - alpha) * 10.5
    assert result.data[20] == pytest.approx(expected)


def test_ema_nan_before_period():
    seq = _make_seq(list(range(1, 25)))
    result = algot.ema(seq)
    for i in range(19):
        assert np.isnan(result.data[i])


# ============================================================================
# rsi tests
# ============================================================================

def test_rsi_registered():
    assert "rsi" in _REGISTRY


def test_rsi_metadata():
    pc = get_plugin("rsi")
    assert pc.shape_in == {"x": "Sequence[float64]"}
    assert pc.min_bars == 14


def test_rsi_known_values():
    """RSI of an uptrend should be high (close to 100)."""
    # Monotonically increasing — RSI should approach 100
    seq = _make_seq(list(range(1, 30)))
    result = algot.rsi(seq)
    # After period bars, RSI > 80 for uptrend
    assert result.data[20] > 80


def test_rsi_downtrend_low():
    """RSI of a downtrend should be low (close to 0)."""
    seq = _make_seq(list(range(30, 0, -1)))  # 30, 29, ..., 1
    result = algot.rsi(seq)
    assert result.data[20] < 20


def test_rsi_nan_before_period():
    seq = _make_seq([1.0] * 30)
    result = algot.rsi(seq)
    for i in range(14):
        assert np.isnan(result.data[i])


# ============================================================================
# atr tests (OHLCVSequence required)
# ============================================================================

def test_atr_registered():
    assert "atr" in _REGISTRY


def test_atr_requires_ohlcv():
    """atr raises on non-OHLCVSequence."""
    seq = _make_seq([1.0, 2.0, 3.0])
    with pytest.raises(TypeError, match="expected OHLCVSequence"):
        algot.atr(seq)


def test_atr_simple_range():
    """Simple constant range → ATR = range."""
    # Build OHLCVSequence where each bar: H-L=2 (constant)
    n = 30
    high = _make_seq([102.0] * n)
    low = _make_seq([100.0] * n)
    close = _make_seq([101.0] * n)
    open_ = _make_seq([101.0] * n)
    vol = _make_seq([1000.0] * n)
    bars = OHLCVSequence(open=open_, high=high, low=low, close=close, volume=vol)

    result = algot.atr(bars)
    # Constant range bars → TR = 2 → ATR seeded at SMA = 2.0
    assert result.data[14] == pytest.approx(2.0)


# ============================================================================
# adx tests (OHLCVSequence required)
# ============================================================================

def test_adx_registered():
    assert "adx" in _REGISTRY


def test_adx_requires_ohlcv():
    seq = _make_seq([1.0, 2.0])
    with pytest.raises(TypeError):
        algot.adx(seq)


def test_adx_flat_market_low_value():
    """Flat market → +DM=0, -DM=0 → ADX=0."""
    n = 50
    flat = _make_seq([100.0] * n)
    bars = OHLCVSequence(
        open=flat, high=flat, low=flat, close=flat,
        volume=_make_seq([1.0] * n),
    )
    result = algot.adx(bars)
    # ADX should be 0 (or NaN) for flat market
    if not np.isnan(result.data[28]):
        assert result.data[28] == pytest.approx(0.0, abs=0.1)


# ============================================================================
# stddev tests
# ============================================================================

def test_stddev_registered():
    assert "stddev" in _REGISTRY


def test_stddev_constant_input_zero():
    """Constant input → stddev = 0."""
    seq = _make_seq([5.0] * 30)
    result = algot.stddev(seq)
    # All values same → stddev = 0
    valid = result.data[~np.isnan(result.data)]
    assert np.all(valid == pytest.approx(0.0))


def test_stddev_known():
    """stddev of [1..20] at bar 19 = population stddev of 1..20."""
    seq = _make_seq(list(range(1, 21)))
    result = algot.stddev(seq)
    expected = float(np.std(np.arange(1, 21), ddof=0))
    assert result.data[19] == pytest.approx(expected)


# ============================================================================
# vwap tests
# ============================================================================

def test_vwap_registered():
    assert "vwap" in _REGISTRY


def test_vwap_simple():
    """VWAP cumulative — first bar = first bar's TP."""
    n = 5
    high = _make_seq([101.0] * n)
    low = _make_seq([99.0] * n)
    close = _make_seq([100.0] * n)
    open_ = _make_seq([100.0] * n)
    vol = _make_seq([1000.0] * n)
    bars = OHLCVSequence(open=open_, high=high, low=low, close=close, volume=vol)

    result = algot.vwap(bars)
    # TP = (101+99+100)/3 = 100
    # Cumulative VWAP at bar 0 = 100
    assert result.data[0] == pytest.approx(100.0)


# ============================================================================
# donchian_h/l tests
# ============================================================================

def test_donchian_high_registered():
    assert "donchian_high" in _REGISTRY


def test_donchian_low_registered():
    assert "donchian_low" in _REGISTRY


def test_donchian_high_known():
    """donchian_high[19] = max(data[0:20])."""
    seq = _make_seq(list(range(1, 21)))
    result = algot.donchian_high(seq)
    assert result.data[19] == pytest.approx(20.0)


def test_donchian_low_known():
    """donchian_low[19] = min(data[0:20])."""
    seq = _make_seq(list(range(1, 21)))
    result = algot.donchian_low(seq)
    assert result.data[19] == pytest.approx(1.0)


# ============================================================================
# crossover / crossunder tests
# ============================================================================

def test_crossover_registered():
    assert "crossover" in _REGISTRY


def test_crossover_detects_cross():
    """x crosses above y → 1.0 at crossover bar."""
    x = _make_seq([1, 2, 5, 4, 3, 6, 7, 8])  # crosses at data[2]=5
    y = _make_seq([3, 3, 3, 3, 3, 3, 3, 3])

    result = algot.crossover(x, y)
    # At least one crossover should be detected
    assert np.sum(result.data == 1.0) >= 1


def test_crossunder_registered():
    assert "crossunder" in _REGISTRY


def test_crossunder_detects_cross():
    """x crosses below y → 1.0."""
    x = _make_seq([5, 4, 1, 2, 3, 0, 1, 2])
    y = _make_seq([3, 3, 3, 3, 3, 3, 3, 3])

    result = algot.crossunder(x, y)
    # At least one crossunder detected
    assert np.sum(result.data == 1.0) >= 1


# ============================================================================
# resample / shift tests
# ============================================================================

def test_resample_registered():
    assert "resample" in _REGISTRY


def test_shift_registered():
    assert "shift" in _REGISTRY


def test_shift_lags_by_n():
    """shift(seq)[0] = seq[1] (current bar of output = 1-step-back of input)."""
    seq = _make_seq([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    result = algot.shift(seq)
    # result[0] (current) = seq[1] = 9 → result.data[-1] = 9
    assert result.data[-1] == pytest.approx(9.0)
    # result[8] (8 steps back) = seq[9] = 1 → result.data[1] = 1
    assert result.data[1] == pytest.approx(1.0)
    # result[9] = NaN → result.data[0] = NaN
    assert np.isnan(result.data[0])

# ---------- n parameterization (M3: engine needs sma fast/slow) ----------

def test_sma_custom_period():
    """sma(x, n=5) uses 5-bar window (per 03 §11 signature)."""
    seq = _make_seq(list(range(1, 31)))  # 1..30
    r5 = algot.sma(seq, n=5)
    r20 = algot.sma(seq)  # default 20
    # r5 first valid at data index 4 = mean(1..5) = 3.0
    assert r5.data[4] == pytest.approx(3.0)
    # r20 first valid at data index 19 = mean(1..20) = 10.5
    assert r20.data[19] == pytest.approx(10.5)
    # both same length
    assert len(r5) == len(r20) == 30


def test_ema_custom_period():
    """ema(x, n=5) alpha = 2/(5+1)."""
    seq = _make_seq(list(range(1, 21)))
    r = algot.ema(seq, n=5)
    alpha = 2.0 / 6.0
    # seed at data index 4 = mean(1..5) = 3.0
    assert r.data[4] == pytest.approx(3.0)
    # data[5] = alpha*6 + (1-alpha)*3.0
    expected = alpha * 6.0 + (1 - alpha) * 3.0
    assert r.data[5] == pytest.approx(expected)


def test_factor_n_param_direct_plugin_call():
    """PluginCall.__call__ binds n kwarg."""
    pc = get_plugin("stddev")
    seq = _make_seq(list(range(1, 21)))
    r = pc(seq, n=5)  # stddev over 5-window
    # data[4] = population std of 1..5 = sqrt(2)
    import math
    assert r.data[4] == pytest.approx(math.sqrt(2.0))
