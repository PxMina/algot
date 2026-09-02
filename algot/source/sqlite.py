"""SQLite data source (per docs/02-data-layer.md §3.2).

Required schema:
    CREATE TABLE bars (
        symbol TEXT NOT NULL,
        timestamp INTEGER NOT NULL,    -- bar START time (unix seconds, UTC)
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (symbol, timestamp)
    );

If schema doesn't match, raises clear error on first load (no silent fallback).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from algot.sequence import OHLCVSequence, Sequence
from algot.source.base import BaseSource

logger = logging.getLogger(__name__)


# Unit alias → seconds (per 02 §4, aligned with 04 §2.1)
UNIT_TO_SECONDS: dict[str, int] = {
    # long form
    "second": 1,
    "min": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    # short aliases (m=min, mo=month to avoid ambiguity)
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "mo": 2592000,
}

# OHLCV columns in DB rows
OHLCV_FIELD_INDEX: dict[str, int] = {
    "open": 1,
    "high": 2,
    "low": 3,
    "close": 4,
    "volume": 5,
}

# Long form for short aliases (avoid m vs mo ambiguity)
_SHORT_TO_LONG = {
    "s": "second",
    "m": "min",
    "h": "hour",
    "d": "day",
    "w": "week",
    "mo": "month",
}


def _normalize_unit(unit: str) -> str:
    """Normalize short unit to long form (per 02 §4).

    m → min; mo → month; others unchanged (including already-long forms).
    """
    return _SHORT_TO_LONG.get(unit, unit)


class SqliteSource(BaseSource):
    """SQLite-backed data source (per 02 §3.2 schema).

    Schema verification runs once on first load; subsequent loads assume OK.
    Gap detection runs per load (inserts NaN + INFO log per 02 §7).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        detect_gaps: bool = True,
    ):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"sqlite DB not found: {self.db_path}\n"
                f"Check strategy.yaml data.path setting."
            )
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._detect_gaps = detect_gaps
        self._verify_schema()

    # ---------- schema verification ----------

    def _verify_schema(self) -> None:
        """Verify DB schema matches expected (per 02 §3.2). Raise clear error if not."""
        try:
            rows = self.conn.execute("PRAGMA table_info(bars)").fetchall()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"sqlite DB schema error: table 'bars' not found.\n"
                f"DB: {self.db_path}\n"
                f"Expected table 'bars' with columns: "
                f"symbol, timestamp, open, high, low, close, volume.\n"
                f"Original error: {e}"
            ) from e

        # PRAGMA table_info on non-existent table returns empty (not raise)
        if not rows:
            raise RuntimeError(
                f"sqlite DB schema error: table 'bars' not found.\n"
                f"DB: {self.db_path}\n"
                f"Expected table 'bars' with columns: "
                f"symbol, timestamp, open, high, low, close, volume."
            )

        actual = {row[1] for row in rows}
        expected = {
            "symbol", "timestamp",
            "open", "high", "low", "close", "volume",
        }
        missing = expected - actual
        if missing:
            raise RuntimeError(
                f"sqlite DB schema mismatch: missing columns {sorted(missing)}.\n"
                f"DB: {self.db_path}\n"
                f"Expected: {sorted(expected)}\n"
                f"Actual:   {sorted(actual)}\n"
                f"See docs/02-data-layer.md §3.2 for required schema."
            )

    # ---------- public API ----------

    def load(
        self,
        symbol: str,
        tf: tuple[int, str],
        start: datetime | None = None,
        end: datetime | None = None,
        field: str = "close",
    ) -> Sequence:
        """Load single-field sequence (per 02 §3.2 + §7)."""
        if field not in OHLCV_FIELD_INDEX:
            raise ValueError(
                f"field must be one of {list(OHLCV_FIELD_INDEX)}, got {field!r}"
            )

        bar_seconds = self._tf_to_seconds(tf)
        field_idx = OHLCV_FIELD_INDEX[field]

        start_ts = int(start.timestamp()) if start else 0
        end_ts = int(end.timestamp()) if end else 2**31 - 1

        sql_rows = self.conn.execute(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM bars WHERE symbol = ? AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp",
            (symbol, start_ts, end_ts),
        ).fetchall()

        if not sql_rows:
            raise ValueError(
                f"no data for {symbol} {tf} in [{start}, {end}]"
            )

        timestamps = np.array([r[0] for r in sql_rows], dtype=np.int64)
        raw_data = np.array([r[field_idx] for r in sql_rows], dtype=np.float64)

        if self._detect_gaps:
            timestamps, raw_data = self._fill_gaps(
                timestamps, raw_data, bar_seconds, symbol, tf
            )

        index = pd.DatetimeIndex(
            [pd.Timestamp(int(ts), unit="s", tz="UTC") for ts in timestamps]
        )

        meta = {
            "symbol": symbol,
            "timeframe": (tf[0], _normalize_unit(tf[1])),
            "unit": _normalize_unit(tf[1]),
            "dtype": np.float64,
            "field": field,
        }

        return Sequence(data=raw_data, meta=meta, index=index)

    def load_ohlcv(
        self,
        symbol: str,
        tf: tuple[int, str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> OHLCVSequence:
        """Load all 5 OHLCV fields (per 02 §2.1.1)."""
        return OHLCVSequence(
            open=self.load(symbol, tf, start, end, field="open"),
            high=self.load(symbol, tf, start, end, field="high"),
            low=self.load(symbol, tf, start, end, field="low"),
            close=self.load(symbol, tf, start, end, field="close"),
            volume=self.load(symbol, tf, start, end, field="volume"),
        )

    def last_bar_time(
        self,
        symbol: str,
        tf: tuple[int, str],
    ) -> datetime | None:
        """Most recent bar timestamp; None if no data (per 02 §8)."""
        row = self.conn.execute(
            "SELECT MAX(timestamp) FROM bars WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row[0] is None:
            return None
        return datetime.fromtimestamp(int(row[0]), tz=timezone.utc)

    # ---------- internals ----------

    def _tf_to_seconds(self, tf: tuple[int, str]) -> int:
        """Convert (N, unit) to seconds."""
        n, unit = tf
        unit_normalized = _normalize_unit(unit)
        if unit_normalized not in UNIT_TO_SECONDS:
            raise ValueError(
                f"unsupported unit {unit!r}; "
                f"supported: {sorted(set(UNIT_TO_SECONDS.values()))}"
            )
        return n * UNIT_TO_SECONDS[unit_normalized]

    def _fill_gaps(
        self,
        timestamps: np.ndarray,
        data: np.ndarray,
        expected_interval: int,
        symbol: str,
        tf: tuple[int, str],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Detect gaps and fill with NaN (per 02 §7).

        Strategy: if consecutive timestamps > 1.5x expected_interval,
        insert (n_missing) NaN bars between them.
        """
        if len(timestamps) < 2:
            return timestamps, data

        new_ts: list[int] = [int(timestamps[0])]
        new_data: list[float] = [float(data[0])]
        gap_total = 0

        for i in range(1, len(timestamps)):
            prev_ts = int(timestamps[i - 1])
            curr_ts = int(timestamps[i])
            actual_interval = curr_ts - prev_ts

            if actual_interval > expected_interval * 1.5:
                n_missing = (actual_interval // expected_interval) - 1
                prev_str = pd.Timestamp(prev_ts, unit="s", tz="UTC")
                curr_str = pd.Timestamp(curr_ts, unit="s", tz="UTC")
                logger.info(
                    f"[data gap] {symbol} {tf}: inserting {n_missing} NaN bars "
                    f"between {prev_str} and {curr_str}"
                )
                for j in range(1, n_missing + 1):
                    new_ts.append(prev_ts + j * expected_interval)
                    new_data.append(np.nan)
                gap_total += n_missing

            new_ts.append(curr_ts)
            new_data.append(float(data[i]))

        if gap_total > 0:
            logger.info(
                f"[data gap summary] {symbol} {tf}: "
                f"{gap_total} NaN bars inserted across {len(timestamps)} real bars"
            )

        return (
            np.array(new_ts, dtype=np.int64),
            np.array(new_data, dtype=np.float64),
        )

    def close(self) -> None:
        self.conn.close()