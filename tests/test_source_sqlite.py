"""SqliteSource tests (per docs/02-data-layer.md §3.2 + §7 + §8)."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from algot.sequence import OHLCVSequence, Sequence
from algot.source import SqliteSource
from algot.source.sqlite import UNIT_TO_SECONDS, _normalize_unit


# ---------- happy path ----------

def test_load_returns_sequence(sqlite_path, sample_tf):
    """load() returns Sequence with correct meta."""
    src = SqliteSource(sqlite_path)
    seq = src.load("AAPL", sample_tf, field="close")
    assert isinstance(seq, Sequence)
    assert seq.meta["symbol"] == "AAPL"
    assert seq.meta["field"] == "close"
    assert seq.meta["unit"] == "min"


def test_load_close_data_count(sqlite_path, sample_tf):
    """load('close') returns 100 bars (95 real + 5 NaN from gap fill)."""
    src = SqliteSource(sqlite_path)
    seq = src.load("AAPL", sample_tf, field="close")
    assert len(seq) == 100


def test_load_ohlcv_returns_5_fields(sqlite_path, sample_tf):
    """load_ohlcv() returns OHLCVSequence with all 5 fields."""
    src = SqliteSource(sqlite_path)
    bars = src.load_ohlcv("AAPL", sample_tf)
    assert isinstance(bars, OHLCVSequence)
    assert len(bars.close) == 100
    assert len(bars.open) == 100
    assert len(bars.high) == 100
    assert len(bars.low) == 100
    assert len(bars.volume) == 100
    # All share same index
    assert bars.close.index.equals(bars.open.index)


def test_load_invalid_field_raises(sqlite_path, sample_tf):
    src = SqliteSource(sqlite_path)
    with pytest.raises(ValueError, match="field must be one of"):
        src.load("AAPL", sample_tf, field="bogus")


def test_load_no_data_raises(sqlite_path, sample_tf):
    src = SqliteSource(sqlite_path)
    with pytest.raises(ValueError, match="no data for"):
        src.load("UNKNOWN", sample_tf)


# ---------- gap fill (02 §7) ----------

def test_gap_fill_inserts_5_nan_bars(sqlite_path, sample_tf):
    """5-bar gap → 5 NaN inserted (per 02 §7)."""
    src = SqliteSource(sqlite_path)
    seq = src.load("AAPL", sample_tf, field="close")
    nan_count = int(np.isnan(seq.data).sum())
    assert nan_count == 5


def test_gap_fill_disabled_via_flag(sqlite_path, sample_tf):
    """detect_gaps=False returns real data only (95 bars)."""
    src = SqliteSource(sqlite_path, detect_gaps=False)
    seq = src.load("AAPL", sample_tf, field="close")
    assert len(seq) == 95
    assert not np.isnan(seq.data).any()


# ---------- unit alias (02 §4) ----------

def test_short_unit_normalizes_to_long(sqlite_path):
    """Short unit (m/min, mo/month) normalizes to long form."""
    src = SqliteSource(sqlite_path)
    seq_m = src.load("AAPL", (1, "m"), field="close")
    seq_min = src.load("AAPL", (1, "min"), field="close")
    np.testing.assert_array_equal(seq_m.data, seq_min.data)
    assert seq_m.meta["unit"] == "min"


def test_unit_normalize_helper():
    assert _normalize_unit("m") == "min"
    assert _normalize_unit("mo") == "month"
    assert _normalize_unit("s") == "second"
    assert _normalize_unit("h") == "hour"
    assert _normalize_unit("d") == "day"
    assert _normalize_unit("w") == "week"
    # Long forms unchanged
    assert _normalize_unit("min") == "min"
    assert _normalize_unit("day") == "day"


def test_invalid_unit_raises(sqlite_path):
    src = SqliteSource(sqlite_path)
    with pytest.raises(ValueError, match="unsupported unit"):
        src.load("AAPL", (1, "year"))


def test_unit_to_seconds_table():
    """Per 04 §2.1: 6 units defined."""
    expected = {"second", "min", "hour", "day", "week", "month"}
    assert set(UNIT_TO_SECONDS) >= expected
    assert UNIT_TO_SECONDS["min"] == 60
    assert UNIT_TO_SECONDS["hour"] == 3600
    assert UNIT_TO_SECONDS["day"] == 86400


# ---------- error handling ----------

def test_missing_db_file_raises(tmp_path):
    """Clear error when DB file doesn't exist (per William feedback)."""
    with pytest.raises(FileNotFoundError, match="sqlite DB not found"):
        SqliteSource(tmp_path / "nonexistent.db")


def test_missing_bars_table_raises(tmp_path):
    """Clear error when 'bars' table doesn't exist."""
    db_file = tmp_path / "wrong.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE other_table (x INTEGER)")
    conn.close()
    with pytest.raises(RuntimeError, match="bars.*not found"):
        SqliteSource(db_file)


def test_missing_columns_raises(tmp_path):
    """Clear error when required columns missing."""
    db_file = tmp_path / "missing_cols.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE bars (
            symbol TEXT,
            timestamp INTEGER,
            open REAL, high REAL, low REAL
            -- missing: close, volume
        )
        """
    )
    conn.close()
    with pytest.raises(RuntimeError, match="missing columns"):
        SqliteSource(db_file)


# ---------- last_bar_time (02 §8) ----------

def test_last_bar_time_returns_max_timestamp(sqlite_path, sample_tf):
    """last_bar_time returns timestamp of most recent bar."""
    src = SqliteSource(sqlite_path)
    last = src.last_bar_time("AAPL", sample_tf)
    assert last is not None
    # Last bar = 9:30 + 99 minutes = 11:09 (since bar 99 was the last real insert)
    # (because i in range(100) skips 30-34, but last index is 99)
    assert last.hour == 11
    assert last.minute == 9


def test_last_bar_time_none_for_missing_symbol(sqlite_path, sample_tf):
    """None when no data for symbol."""
    src = SqliteSource(sqlite_path)
    assert src.last_bar_time("UNKNOWN", sample_tf) is None