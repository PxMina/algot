"""CLI tests: `algot backtest strategy.yaml` end-to-end (PLAN M5)."""

from __future__ import annotations

import pathlib
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from algot.algo._core import _REGISTRY
from algot.cli import main
from algot.config import ConfigError, parse_strategy_yaml

# A minimal self-contained signal plugin (trend-following on sma cross).
PLUGIN_SRC = '''\
"""Auto-registering on import (03 §10.2 rule: files carry defs only)."""
import numpy as np
import algot
from algot import Direction, FixedSize, MarketOrder, Signal
from algot.algo.plugin import plugin


@plugin(
    category="signal",
    shape_in={"close": "Sequence[float64]"},
    shape_out="Signal | None",
    stateful=True,
    state_type={"prev": None},
    min_bars=20,
)
def trend_sig(close, state):
    fast = algot.sma(close, n=5)
    slow = algot.sma(close, n=20)
    f0, s0 = float(fast[0]), float(slow[0])
    if len(close) < 2 or np.isnan(f0) or np.isnan(s0):
        state["prev"] = None
        return None
    bull = f0 > s0
    prev = state["prev"]
    state["prev"] = bull
    if prev is None:
        return None
    bar_time = close.index[-1]
    if bull and not prev:
        return Signal(
            symbol=close.meta["symbol"], direction=Direction.LONG,
            price=MarketOrder(), size=FixedSize(shares=10),
            bar_time=bar_time, validity=1, tags={"reason": "golden"},
        )
    if not bull and prev:
        return Signal(
            symbol=close.meta["symbol"], direction=Direction.CLOSE_LONG,
            price=MarketOrder(), size=FixedSize(shares=10),
            bar_time=bar_time, validity=1, tags={"reason": "death"},
        )
    return None
'''


def _make_wave_db(tmp_path) -> pathlib.Path:
    """Wave-pattern db: sma5×sma20 crosses several times → signals fire."""
    db = tmp_path / "wave.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE bars (symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, "
        "open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "PRIMARY KEY (symbol, timestamp))"
    )
    n = 150
    t0 = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp())

    def pp(i):
        return 100.0 + 0.1 * i + 8.0 * np.sin(2 * np.pi * i / 40.0)

    rows = []
    for i in range(n):
        c = pp(i)
        o = c + (0.1 if i % 2 else -0.1)
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append(("AAPL", t0 + i * 60, o, h, l, c, 1_000_000.0))
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def _reload_builtins():
    """clear_registry() wipes built-in factors; re-register them by reloading
    the factor module (fresh registry → re-runs @plugin decorators)."""
    from algot.algo._core import clear_registry
    import importlib
    import algot.algo.builtins.factor
    clear_registry()
    importlib.reload(algot.algo.builtins.factor)


def _write_yaml(tmp_path, db, extra_top="", extra_strategies="") -> str:
    body = f"""\
db: {db}
symbol: AAPL
timeframe: 1min
plugins:
  - plugins/signal_trend.py
{extra_top}strategies:
  - id: t1
    type: long
    capital: 100000
    signals: [trend_sig]
    exec_lag: 1
{extra_strategies}"""
    p = tmp_path / "strategy.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def _setup(tmp_path):
    """Wave db + plugin file + built-ins, returns db path."""
    _reload_builtins()
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "signal_trend.py").write_text(PLUGIN_SRC)
    return _make_wave_db(tmp_path)


# ---------- CLI end-to-end ----------

def test_cli_backtest_end_to_end(tmp_path, capsys):
    """Happy path: yaml + plugin file → strategy summary on stdout."""
    db = _setup(tmp_path)
    yaml = _write_yaml(tmp_path, db)
    rc = main(["backtest", yaml])
    assert rc == 0
    out = capsys.readouterr().out
    assert "strategy t1" in out
    assert "realized_pnl=$" in out
    assert "equity=$" in out
    # wave data → at least one cross → at least one FILLED order
    assert "FILLED" in out
    # built-ins survive (not wiped by user plugin load)
    assert "sma" in _REGISTRY


def test_cli_backtest_two_strategies(tmp_path, capsys):
    """Two yaml strategies → both run (independent pools)."""
    _setup(tmp_path)
    db = tmp_path / "wave.db"
    extra = """\
  - id: t2
    type: long
    capital: 50000
    signals: [trend_sig]
    exec_lag: 2
"""
    yaml = _write_yaml(tmp_path, db, extra_strategies=extra)
    rc = main(["backtest", yaml])
    assert rc == 0
    out = capsys.readouterr().out
    assert "strategy t1" in out and "strategy t2" in out


def test_cli_db_override(tmp_path, capsys):
    """--db overrides the yaml path (missing yaml default is fine)."""
    _setup(tmp_path)
    real_db = tmp_path / "wave.db"
    yaml = _write_yaml(tmp_path, tmp_path / "nope.db")
    rc = main(["backtest", "--db", str(real_db), yaml])
    assert rc == 0


def test_cli_missing_db(tmp_path, capsys):
    _setup(tmp_path)
    yaml = _write_yaml(tmp_path, tmp_path / "nope.db")
    rc = main(["backtest", yaml])
    assert rc == 2
    assert "db file not found" in capsys.readouterr().err


def test_cli_missing_plugin_file(tmp_path, capsys):
    _reload_builtins()
    db = _make_wave_db(tmp_path)
    body = f"""\
db: {db}
symbol: AAPL
timeframe: 1min
plugins:
  - plugins/no_such.py
strategies:
  - id: t1
    type: long
    signals: [trend_sig]
"""
    p = tmp_path / "strategy.yaml"
    p.write_text(textwrap.dedent(body))
    rc = main(["backtest", str(p)])
    assert rc == 2
    assert "file not found" in capsys.readouterr().err


def test_cli_unknown_signal(tmp_path, capsys):
    """Signal name not registered → clean ConfigError, rc=2."""
    _setup(tmp_path)
    yaml = _write_yaml(tmp_path, tmp_path / "wave.db")
    p = pathlib.Path(yaml)
    p.write_text(p.read_text().replace("trend_sig", "no_such_signal"))
    rc = main(["backtest", yaml])
    assert rc == 2
    assert "not registered" in capsys.readouterr().err


def test_cli_bad_timeframe(tmp_path, capsys):
    """Malformed timeframe → ConfigError message with field path."""
    _setup(tmp_path)
    yaml = _write_yaml(tmp_path, tmp_path / "wave.db")
    p = pathlib.Path(yaml)
    p.write_text(p.read_text().replace("timeframe: 1min", "timeframe: abc"))
    rc = main(["backtest", yaml])
    assert rc == 2
    assert "timeframe" in capsys.readouterr().err


def test_cli_run_stub(tmp_path, capsys):
    """`algot run` = M6 stub → rc 2 with a clear message."""
    _setup(tmp_path)
    yaml = _write_yaml(tmp_path, tmp_path / "wave.db")
    rc = main(["run", yaml])
    assert rc == 2
    assert "M6" in capsys.readouterr().err


# ---------- config parsing unit tests ----------

def test_parse_tf_variants():
    from algot.config import parse_tf
    assert parse_tf("1min") == (1, "min")
    assert parse_tf("5m") == (5, "min")
    assert parse_tf("1d") == (1, "day")
    assert parse_tf("1day") == (1, "day")
    assert parse_tf("30sec") == (30, "sec")
    assert parse_tf("2hour") == (2, "hour")
    assert parse_tf("4week") == (4, "week")
    assert parse_tf("1month") == (1, "month")


def test_parse_tf_bad():
    from algot.config import parse_tf
    for bad in ("abc", "1", "0min", "-1d", "1x", "min1"):
        with pytest.raises(ConfigError):
            parse_tf(bad)


def test_config_unknown_top_key(tmp_path):
    _setup(tmp_path)
    yaml = _write_yaml(tmp_path, tmp_path / "wave.db", extra_top="bogus_key: 1\n")
    with pytest.raises(ConfigError, match="bogus_key"):
        parse_strategy_yaml(yaml)


def test_config_missing_required(tmp_path):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text("symbol: AAPL\n")  # no db
    with pytest.raises(ConfigError, match="db"):
        parse_strategy_yaml(str(yaml))


def test_config_duplicate_ids(tmp_path):
    yaml = tmp_path / "dup.yaml"
    yaml.write_text(textwrap.dedent(f"""\
        db: {tmp_path / 'x.db'}
        symbol: AAPL
        timeframe: 1min
        strategies:
          - id: a
            type: long
            signals: [x]
          - id: a
            type: short
            signals: [x]
    """))
    with pytest.raises(ConfigError, match="duplicate"):
        parse_strategy_yaml(str(yaml))


def test_config_strategy_type_case_insensitive(tmp_path):
    yaml = tmp_path / "case.yaml"
    yaml.write_text(textwrap.dedent(f"""\
        db: {tmp_path / 'x.db'}
        symbol: AAPL
        timeframe: 1min
        strategies:
          - id: a
            type: LONG
            signals: [x]
    """))
    cfg = parse_strategy_yaml(str(yaml))
    assert cfg.strategies[0].type.value == "long"
