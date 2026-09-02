"""Sequence indexing semantics tests (per docs/02-data-layer.md §2 + 00 §3.2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from algot.sequence import OHLCVSequence, Sequence


@pytest.fixture
def sample_seq():
    """Sample 6-bar sequence (per 00 §3.2 example)."""
    data = np.array([5.2, 4.09, 3.7, 2.1, 1.05, 0.55], dtype=np.float64)
    index = pd.DatetimeIndex(
        [
            "2024-01-01 09:30", "2024-01-01 09:31", "2024-01-01 09:32",
            "2024-01-01 09:33", "2024-01-01 09:34", "2024-01-01 09:35",
        ],
        tz="UTC",
    )
    meta = {
        "symbol": "AAPL",
        "timeframe": (1, "min"),
        "unit": "min",
    }
    return Sequence(data=data, meta=meta, index=index)


# ---------- single int indexing (00 §3.2) ----------

def test_seq_zero_returns_current(sample_seq):
    """seq[0] = current bar (per 00 §3.2)."""
    assert sample_seq[0] == 0.55


def test_seq_n_returns_n_steps_back(sample_seq):
    """seq[N] = N steps back."""
    assert sample_seq[1] == 1.05
    assert sample_seq[3] == 3.7
    assert sample_seq[5] == 5.2


def test_seq_negative_raises_not_implemented(sample_seq):
    """Negative index raises NotImplementedError per 00 §6.6."""
    with pytest.raises(NotImplementedError, match="v1 禁用负数索引"):
        sample_seq[-1]


def test_seq_index_out_of_range(sample_seq):
    """Index past end raises IndexError."""
    with pytest.raises(IndexError):
        sample_seq[100]


def test_seq_length(sample_seq):
    assert len(sample_seq) == 6


# ---------- slice indexing (00 §3.2) ----------

def test_seq_slice_newest_to_oldest(sample_seq):
    """seq[0, 3] = inclusive slice, A<B direction (newest→oldest)."""
    result = sample_seq[0, 3]
    np.testing.assert_array_equal(result, [0.55, 1.05, 2.1, 3.7])


def test_seq_slice_oldest_to_newest(sample_seq):
    """seq[3, 0] = inclusive slice, A>B direction (oldest→newest)."""
    result = sample_seq[3, 0]
    np.testing.assert_array_equal(result, [3.7, 2.1, 1.05, 0.55])


def test_seq_slice_inclusive_both_ends(sample_seq):
    """seq[0, 3] = 4 elements (both ends inclusive)."""
    result = sample_seq[0, 3]
    assert len(result) == 4


def test_seq_slice_equal_ends_single_element(sample_seq):
    """seq[N, N] = single element slice."""
    result = sample_seq[2, 2]
    assert len(result) == 1
    assert result[0] == 2.1


def test_seq_negative_slice_raises(sample_seq):
    """Negative in slice raises."""
    with pytest.raises(NotImplementedError):
        sample_seq[0, -1]


# ---------- meta auto-derive ----------

def test_seq_meta_dtype_auto_derived(sample_seq):
    """meta.dtype auto-syncs from data.dtype (per 02 §2.1)."""
    assert sample_seq.meta["dtype"] == np.float64


def test_seq_rejects_2d_data():
    """Sequence.data must be 1D."""
    data2d = np.array([[1, 2], [3, 4]], dtype=np.float64)
    with pytest.raises(ValueError, match="must be 1D"):
        Sequence(data=data2d)


def test_seq_default_index_when_none():
    """Default index is np.arange(len(data)) when None."""
    data = np.array([1.0, 2.0, 3.0])
    seq = Sequence(data=data)
    assert seq.index is not None
    np.testing.assert_array_equal(seq.index, [0, 1, 2])


# ---------- OHLCVSequence (02 §2.1.1) ----------

def test_ohlcv_sequence_shares_meta_and_index():
    """OHLCVSequence 5 fields share meta + index (per 02 §2.1.1)."""
    base_index = pd.DatetimeIndex(
        [f"2024-01-01 09:{30+i:02d}" for i in range(10)],
        tz="UTC",
    )
    base_meta = {"symbol": "AAPL", "timeframe": (1, "min"), "unit": "min"}

    def make_seq(arr):
        return Sequence(
            data=arr.astype(np.float64),
            meta=dict(base_meta),
            index=base_index,
        )

    ohlcv = OHLCVSequence(
        open=make_seq(np.arange(10) + 0.1),
        high=make_seq(np.arange(10) + 0.2),
        low=make_seq(np.arange(10) - 0.1),
        close=make_seq(np.arange(10, dtype=int)),
        volume=make_seq(np.arange(10) * 100),
    )

    # Length
    assert len(ohlcv) == 10
    assert len(ohlcv.open) == 10
    assert len(ohlcv.high) == 10
    assert len(ohlcv.low) == 10
    assert len(ohlcv.volume) == 10

    # Shared index
    assert ohlcv.index.equals(base_index)

    # meta comes from close
    assert ohlcv.meta["symbol"] == "AAPL"

    # Each field accessible (data is oldest→newest, seq[0]=last)
    assert ohlcv.close[0] == 9    # current close (data[-1])
    assert ohlcv.high[1] == 8.2   # 1 step back high (data[-2])
    assert ohlcv.low[2] == 6.9    # 2 steps back low (data[-3])
    assert ohlcv.open[3] == 6.1   # 3 steps back open (data[-4])
    assert ohlcv.volume[4] == 500  # 4 steps back volume (data[-5])


def test_ohlcv_sequence_partial_bar_semantics():
    """Live partial bar semantics (per 02 §2.1.1)."""
    base_index = pd.DatetimeIndex(
        [f"2024-01-01 09:{30+i:02d}" for i in range(2)],
        tz="UTC",
    )
    base_meta = {"symbol": "AAPL", "timeframe": (1, "min"), "unit": "min"}

    def make_seq(arr):
        return Sequence(
            data=arr.astype(np.float64),
            meta=dict(base_meta),
            index=base_index,
        )

    # Current partial bar [0] = data[-1] (current bar in progress)
    # Partial bar semantics: open[0]=first tick, high[0]=max so far, etc.
    # Data is oldest→newest; seq[0] = data[-1]
    ohlcv = OHLCVSequence(
        open=make_seq(np.array([99.5, 100.0])),     # [prev_bar_open, curr_bar_open=first_tick]
        high=make_seq(np.array([99.7, 100.5])),     # [prev, curr=max_so_far]
        low=make_seq(np.array([99.4, 99.8])),       # [prev, curr=min_so_far]
        close=make_seq(np.array([99.6, 100.3])),    # [prev, curr=latest_tick]
        volume=make_seq(np.array([2000, 1500])),    # [prev, curr=cumulative]
    )

    # Live partial bar [0] = current
    assert ohlcv.open[0] == 100.0
    assert ohlcv.high[0] == 100.5
    assert ohlcv.low[0] == 99.8
    assert ohlcv.close[0] == 100.3
    assert ohlcv.volume[0] == 1500

    # Previous bar [1] = data[-2]
    assert ohlcv.open[1] == 99.5
    assert ohlcv.close[1] == 99.6