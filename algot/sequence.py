"""Sequence data structures (per docs/02-data-layer.md §2 + §2.1.1).

Public API:
    - Sequence (1D bar-indexed sequence)
    - OHLCVSequence (5-field container, NOT a Sequence subclass)

Indexing semantics (per 00 §3.2 + 02 §2.2 + 00 §6.6):
    - seq[N]       = N steps back, seq[0] = current bar
    - seq[A, B]    = slice [A, B] inclusive both ends; direction by A<B vs A>B
    - negative idx = raise NotImplementedError
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

import numpy as np
import pandas as pd


@dataclass
class Sequence:
    """1D bar-indexed sequence.

    Fields:
        data:  1D np.ndarray (default np.float64)
        meta:  dict with {symbol, timeframe, unit, dtype, ...}
        index: pd.DatetimeIndex (UTC) or np.ndarray[int64] (bar positions)

    Indexing:
        seq[N]    = N steps back (seq[0] = current)
        seq[A, B] = inclusive slice; direction by A<B (newest→oldest) or A>B (oldest→newest)
        negative  = NotImplementedError (per 00 §6.6, aligned with Pine series)
    """

    data: np.ndarray
    meta: dict = field(default_factory=dict)
    index: Union[pd.DatetimeIndex, np.ndarray, None] = None

    def __post_init__(self) -> None:
        if self.data.ndim != 1:
            raise ValueError(
                f"Sequence.data must be 1D, got {self.data.ndim}D"
            )
        if self.index is None:
            self.index = np.arange(len(self.data), dtype=np.int64)
        # Auto-derive dtype in meta (per 02 §2.1)
        self.meta["dtype"] = self.data.dtype

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, key: Any) -> Any:
        """seq[N] = N steps back; seq[A, B] = slice inclusive both ends.

        Per 02 §2.2 + 00 §6.6:
            - negative idx → NotImplementedError
            - slice direction determined by A<B vs A>B
        """
        # Single int
        if isinstance(key, (int, np.integer)):
            if key < 0:
                raise NotImplementedError(
                    f"v1 禁用负数索引 (00 §6.6); got key={int(key)}"
                )
            if key >= len(self.data):
                raise IndexError(
                    f"bar_idx {int(key)} out of range (len={len(self.data)})"
                )
            return self.data[-(int(key) + 1)]

        # Tuple slice
        if isinstance(key, tuple) and len(key) == 2:
            start, end = int(key[0]), int(key[1])
            if start < 0 or end < 0:
                raise NotImplementedError(
                    f"v1 禁用负数索引 (00 §6.6); got key=({start}, {end})"
                )
            # Inclusive [A, B]; direction by start vs end
            if start <= end:
                indices = [-(i + 1) for i in range(start, end + 1)]
            else:
                indices = [-(i + 1) for i in range(start, end - 1, -1)]
            return self.data[indices]

        raise TypeError(
            f"Sequence.__getitem__ expects int or (int, int) tuple, "
            f"got {type(key).__name__}"
        )


@dataclass
class OHLCVSequence:
    """5-field OHLCV container (per 02 §2.1.1).

    NOT a Sequence subclass — keeps Sequence = 1D semantics invariant.
    Holds 5 Sequence instances sharing meta + index.

    Live partial-bar semantics (per 02 §2.1.1):
        open[0]   = current bar first tick
        high[0]   = current partial bar max so far
        low[0]    = current partial bar min so far
        close[0]  = latest tick price
        volume[0] = cumulative volume (current bar)
    """

    open: Sequence
    high: Sequence
    low: Sequence
    close: Sequence
    volume: Sequence

    @property
    def meta(self) -> dict:
        """Shared meta from close (5 fields should share)."""
        return self.close.meta

    @property
    def index(self):
        """Shared index from close."""
        return self.close.index

    def __len__(self) -> int:
        return len(self.close)