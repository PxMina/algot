"""Pytest fixtures: in-memory / tmp-path sqlite with sample data."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def sqlite_path(tmp_path):
    """Sample sqlite DB with 95 real AAPL bars (gap at indices 30-34).

    Schema per docs/02-data-layer.md §3.2:
        symbol TEXT, timestamp INTEGER, open/high/low/close/volume REAL
    Bar START time = unix seconds (UTC). 1min intervals.
    """
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE bars (
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, timestamp)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_bars_symbol_time ON bars(symbol, timestamp)"
    )

    base = datetime(2024, 1, 1, 9, 30, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(100):
        if 30 <= i < 35:
            continue  # 5-bar gap (intentional)
        ts = int((base + timedelta(minutes=i)).timestamp())
        o = 100 + i * 0.01
        c = o + 0.05
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        v = 1000 + i * 10
        rows.append(("AAPL", ts, o, h, l, c, v))

    conn.executemany(
        "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def sample_tf():
    """1min timeframe tuple."""
    return (1, "min")