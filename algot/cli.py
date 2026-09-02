"""algot CLI — `algot backtest strategy.yaml` (per PLAN M5 / 01 §1 cli role).

    algot backtest strategy.yaml [--db PATH] [--quiet]
        Load strategy.yaml, import plugin files, run one BacktestEngine per
        strategy and print per-strategy results to stdout.

    algot run strategy.yaml
        (M6) paper live mode — not implemented yet; exits with an error.

Plugin loading semantics (03 §10.2 rule): plugin files/modules carry ONLY
decorator + function definitions; CLI/data side-effects live in
strategy.yaml.  A plugin path ending in .py is resolved relative to the
yaml's directory; otherwise it is treated as an importable module path.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import logging
import os
import sys
from typing import Sequence

from algot import __version__
from algot.algo._core import _REGISTRY
from algot.config import ConfigError, StrategyConfig, parse_strategy_yaml
from algot.engine.backtest import BacktestEngine
from algot.engine.strategy import Strategy
from algot.source.sqlite import SqliteSource

log = logging.getLogger("algot.cli")

# Module prefix for dynamically loaded plugin files (avoid collisions with
# real packages on sys.path).
_PLUGIN_MODULE_PREFIX = "_algot_user_plugin_"


def _load_plugins(cfg: StrategyConfig) -> None:
    """Import every plugin entry so @algot.plugin registration runs."""
    for i, entry in enumerate(cfg.plugins):
        if entry.endswith(".py"):
            abspath = entry if os.path.isabs(entry) else os.path.join(
                cfg.base_dir, entry
            )
            if not os.path.exists(abspath):
                raise ConfigError(
                    f"plugins[{i}]: file not found: {abspath}"
                )
            stem = os.path.splitext(os.path.basename(abspath))[0]
            modname = f"{_PLUGIN_MODULE_PREFIX}{i}_{stem}"
            spec = importlib.util.spec_from_file_location(modname, abspath)
            if spec is None or spec.loader is None:
                raise ConfigError(f"plugins[{i}]: cannot load {abspath}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[modname] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(modname, None)
                raise
        else:
            try:
                importlib.import_module(entry)
            except ImportError as e:
                raise ConfigError(
                    f"plugins[{i}]: cannot import module {entry!r}: {e}"
                ) from e


def _describe_registry() -> str:
    by_cat: dict[str, list[str]] = {}
    for name, pc in _REGISTRY.items():
        by_cat.setdefault(pc.category, []).append(name)
    parts = []
    for cat in sorted(by_cat):
        parts.append(f"{cat}: {', '.join(sorted(by_cat[cat]))}")
    return "; ".join(parts) or "(empty)"


def _run_strategy(cfg: StrategyConfig, strategy: Strategy, db: str) -> dict:
    """One strategy × one symbol backtest (06 §6.2 host loop body)."""
    source = SqliteSource(db)
    engine = BacktestEngine(strategy, source, symbol=cfg.symbol,
                            tf=cfg.timeframe)
    return engine.run()


def _print_result(cfg: StrategyConfig, strategy: Strategy, result: dict) -> None:
    s = result["summary"][strategy.id]
    print(f"== strategy {strategy.id} [{strategy.type.value}] "
          f"symbol={cfg.symbol} tf={cfg.timeframe[0]}{cfg.timeframe[1]} ==")
    print(f"  bars={len(result['bars'])}  signals={len(result['signals'])}  "
          f"warmup_dropped={result['dropped_warmup']}")
    for o in result["orders"]:
        t = o.fill_time.strftime("%Y-%m-%d %H:%M") if o.fill_time else "--"
        px = f"{o.fill_price:.4f}" if o.fill_price is not None else "--"
        print(f"  {o.status:8s} {o.direction.value:12s} "
              f"{o.filled_shares:8.1f} @ {px} {t} {o.rejection_reason or ''}")
    print(f"  realized_pnl=${s['realized_pnl']:.2f}  "
          f"unrealized_pnl=${s['unrealized_pnl']:.2f}  "
          f"equity=${s['equity']:.2f}")


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = parse_strategy_yaml(args.config)
    _load_plugins(cfg)

    # Friendly diagnostics: resolve every requested signal name up front.
    for strategy in cfg.strategies:
        missing = [
            n for n in strategy.signals
            if n not in _REGISTRY or _REGISTRY[n].category != "signal"
        ]
        if missing:
            raise ConfigError(
                f"strategy {strategy.id!r}: signal plugin(s) not registered: "
                f"{missing}. Registered: {_describe_registry()}"
            )

    db = args.db or cfg.db
    if not os.path.exists(db):
        raise ConfigError(f"db file not found: {db}")

    for strategy in cfg.strategies:
        # v1 engine runs one strategy instance at a time with symbols=[symbol].
        strat = Strategy(
            id=strategy.id,
            type=strategy.type,
            capital=strategy.capital,
            symbols=[cfg.symbol],
            signals=strategy.signals,
            exec_lag=strategy.exec_lag,
        )
        result = _run_strategy(cfg, strat, db)
        if not args.quiet:
            _print_result(cfg, strat, result)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    parse_strategy_yaml(args.config)  # validate config early
    print("algot run: paper live mode lands in M6; "
          "use 'algot backtest' for now.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="algot",
        description="Algorithmic Trading Workbench — backtest + paper live "
                    "strategies driven by strategy.yaml",
    )
    parser.add_argument("--version", action="version",
                        version=f"algot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bt = sub.add_parser(
        "backtest", help="run a backtest from strategy.yaml"
    )
    p_bt.add_argument("config", help="path to strategy.yaml")
    p_bt.add_argument("--db", default=None,
                      help="override db path from the yaml")
    p_bt.add_argument("--quiet", action="store_true",
                      help="suppress per-order detail output")
    p_bt.set_defaults(func=cmd_backtest)

    p_run = sub.add_parser(
        "run", help="paper live mode (M6, not yet implemented)"
    )
    p_run.add_argument("config", help="path to strategy.yaml")
    p_run.set_defaults(func=cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # pragma: no cover - unexpected
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
