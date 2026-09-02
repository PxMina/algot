"""Abstract data source interface (per docs/02-data-layer.md §3.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from algot.sequence import OHLCVSequence, Sequence


class BaseSource(ABC):
    """Abstract data source.

    v1 implementations: SqliteSource
    v2+: ParquetSource, InfluxSource, CSVSource, ...
    """

    @abstractmethod
    def load(
        self,
        symbol: str,
        tf: tuple[int, str],
        start: datetime | None = None,
        end: datetime | None = None,
        field: str = "close",
    ) -> "Sequence":
        """Load single-field sequence for [start, end] (inclusive both ends).

        Args:
            symbol: ticker ('AAPL', 'BTCUSDT')
            tf: (N, unit), e.g. (1, 'min'), (1, 'day')
            start: inclusive lower bound (None = earliest available)
            end:   inclusive upper bound (None = latest available)
            field: 'open' | 'high' | 'low' | 'close' | 'volume' (default close)

        Returns:
            Sequence with data + meta + index
        """
        ...

    @abstractmethod
    def load_ohlcv(
        self,
        symbol: str,
        tf: tuple[int, str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> "OHLCVSequence":
        """Load all 5 OHLCV fields (per 02 §2.1.1)."""
        ...

    @abstractmethod
    def last_bar_time(
        self,
        symbol: str,
        tf: tuple[int, str],
    ) -> datetime | None:
        """Live mode: timestamp of most recent bar. None = no data yet.

        Per 02 §8: data layer exposes this; engine decides staleness.
        """
        ...