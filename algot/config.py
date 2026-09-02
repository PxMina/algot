"""strategy.yaml loading + validation (per 01-architecture §1 config role).

YAML schema (v1, M5):

    db: path/to/data.sqlite     # sqlite data file (required)
    symbol: AAPL                # single symbol (v1, required)
    timeframe: "1min"           # N + unit, short/long names both ok
    plugins:                    # optional; plugin files/modules to load
      - ./algos/golden_cross.py #   file path (relative to this yaml)
      - mypkg.my_plugin         #   or importable module path
    strategies:                 # one or more
      - id: gc_long             # unique strategy id (required)
        type: long              # long | short (required)
        capital: 100000         # optional, default 100_000
        signals: [golden_cross] # signal plugin names (engine: exactly 1)
        exec_lag: 1             # optional, default 1 (G2)

Backtest vs live keys (staleness etc.) are consumed by later milestones;
unknown top-level keys → error (typo protection).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from algot.broker.base import StrategyType
from algot.engine.strategy import Strategy
from algot.source.sqlite import _normalize_unit

# (N, unit) written as "N+unit" strings, e.g. "1min" / "5m" / "1d" / "1day".
_TF_RE = re.compile(r"^(\d+)\s*([A-Za-z]+)$")

_ALLOWED_UNITS = {"s", "sec", "m", "min", "h", "hour", "d", "day",
                  "w", "week", "mo", "month"}

_TOP_LEVEL_KEYS = {"db", "symbol", "timeframe", "plugins", "strategies"}
_STRATEGY_KEYS = {"id", "type", "capital", "signals", "exec_lag"}


class ConfigError(ValueError):
    """Raised on malformed strategy.yaml with a field-path in the message."""


def _err(path: str, msg: str) -> ConfigError:
    return ConfigError(f"strategy.yaml: {path}: {msg}")


def _require_mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise _err(path, f"expected a mapping, got {type(value).__name__}")
    return value


def parse_tf(raw: Any, path: str = "timeframe") -> tuple[int, str]:
    """'1min' / '5m' / '1day' → (1, 'min') normalized to long unit form."""
    if not isinstance(raw, str):
        raise _err(path, f"expected a timeframe string like '1min', got {raw!r}")
    m = _TF_RE.match(raw.strip())
    if not m:
        raise _err(path, f"cannot parse timeframe {raw!r} (want e.g. '1min', '1d')")
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit not in _ALLOWED_UNITS:
        raise _err(
            path,
            f"unknown unit {unit!r}; supported: "
            f"{sorted(_ALLOWED_UNITS)}",
        )
    if n <= 0:
        raise _err(path, f"N must be positive, got {n}")
    return (n, _normalize_unit(unit))


def _parse_strategy(raw: Any, path: str) -> Strategy:
    d = _require_mapping(raw, path)
    unknown = set(d) - _STRATEGY_KEYS
    if unknown:
        raise _err(path, f"unknown keys {sorted(unknown)}; valid: "
                         f"{sorted(_STRATEGY_KEYS)}")
    if "id" not in d:
        raise _err(path, "missing required key 'id'")
    if not isinstance(d["id"], str) or not d["id"].strip():
        raise _err(path, f"'id' must be a non-empty string, got {d['id']!r}")
    if "type" not in d:
        raise _err(path, f"strategy {d['id']!r}: missing required key 'type'")
    t = str(d["type"]).lower()
    if t not in ("long", "short"):
        raise _err(path, f"strategy {d['id']!r}: 'type' must be long|short, "
                         f"got {d['type']!r}")
    signals = d.get("signals", [])
    if not isinstance(signals, list) or not all(
        isinstance(s, str) for s in signals
    ):
        raise _err(path, f"strategy {d['id']!r}: 'signals' must be a list of "
                         f"plugin-name strings")

    strategy = Strategy(
        id=d["id"],
        type=StrategyType.LONG if t == "long" else StrategyType.SHORT,
        capital=float(d.get("capital", 100_000.0)),
        signals=list(signals),
        exec_lag=int(d.get("exec_lag", 1)),
    )
    if not strategy.signals:
        raise _err(path, f"strategy {d['id']!r}: at least one signal required")
    return strategy


@dataclass
class StrategyConfig:
    """Validated contents of one strategy.yaml."""

    db: str
    symbol: str
    timeframe: tuple[int, str]
    strategies: list[Strategy]
    plugins: list[str] = field(default_factory=list)
    yaml_path: str = ""  # absolute path of the yaml (plugin-relative base)

    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.yaml_path or "."))


def parse_strategy_yaml(path: str) -> StrategyConfig:
    """Load + validate strategy.yaml. Raises ConfigError / OSError."""
    if not os.path.exists(path):
        raise ConfigError(f"strategy.yaml: no such file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"strategy.yaml: invalid YAML: {e}") from e
    if doc is None:
        raise ConfigError("strategy.yaml: file is empty")
    doc = _require_mapping(doc, "(top level)")

    unknown = set(doc) - _TOP_LEVEL_KEYS
    if unknown:
        raise _err(
            "(top level)",
            f"unknown keys {sorted(unknown)}; valid: {sorted(_TOP_LEVEL_KEYS)}",
        )
    for key in ("db", "symbol"):
        if key not in doc:
            raise _err("(top level)", f"missing required key {key!r}")
        if not isinstance(doc[key], str) or not doc[key].strip():
            raise _err("(top level)", f"{key!r} must be a non-empty string")
    strategies_raw = doc.get("strategies")
    if not isinstance(strategies_raw, list) or not strategies_raw:
        raise _err("strategies", "expected a non-empty list")
    strategies = [
        _parse_strategy(s, f"strategies[{i}]")
        for i, s in enumerate(strategies_raw)
    ]
    ids = [s.id for s in strategies]
    if len(ids) != len(set(ids)):
        raise _err("strategies", f"duplicate strategy ids: {ids}")

    plugins = doc.get("plugins", [])
    if not isinstance(plugins, list) or not all(
        isinstance(p, str) and p.strip() for p in plugins
    ):
        raise _err("plugins", "expected a list of file/module paths")

    return StrategyConfig(
        db=doc["db"],
        symbol=doc["symbol"],
        timeframe=parse_tf(doc["timeframe"]),
        strategies=strategies,
        plugins=list(plugins),
        yaml_path=os.path.abspath(path),
    )
