"""@algot.plugin decorator (per docs/03-algorithms.md §3.1).

Usage:
    @plugin(category="factor", shape_in={"x": "Sequence[float64]"},
            shape_out="Sequence[float64]", min_bars=20)
    def sma(x):
        # x is a Sequence (1D, indexed seq[N]=N steps back)
        # ...

    @plugin(category="signal", stateful=True,
            state_type={"prev_ema": 0.0, "bars": 0})
    def my_signal(x):
        # 'state' is injected as a magic local
        if state["bars"] > 0:
            # ... use state["prev_ema"]
            pass
        state["prev_ema"] = x[0]
        state["bars"] += 1
"""

from __future__ import annotations

import warnings
from typing import Callable

from algot.algo._core import _REGISTRY, PluginCall, _check_stateful_has_state_param


def plugin(
    *,
    category: str,
    shape_in: dict[str, str] | None = None,
    shape_out: dict[str, str] | None = None,
    pure: bool = True,
    deps: list[str] | None = None,
    version: str = "0.1.0",
    min_bars: int = 0,
    stateful: bool = False,
    state_type: type | dict | None = None,
) -> Callable[[Callable], PluginCall]:
    """Decorator: register a function as an algot plugin.

    Required:
        category: one of {factor, signal, source, sizer, risk, scheduler}

    Optional:
        shape_in:     {param_name: dtype} — input contract validation
        shape_out:    dtype (str) or {name: dtype} — output contract
        pure:         stateless function (default True)
        deps:         list of plugin names this depends on
        version:      semantic version (default "0.1.0")
        min_bars:     warmup period (default 0 = no warmup)
        stateful:     plugin maintains state across bars (default False)
        state_type:   dict schema or dataclass type (required if stateful=True)

    Stateful plugin signature MUST include 'state' parameter:
        @plugin(category="signal", stateful=True, state_type={"x": 0})
        def my_signal(x, state):
            state["x"] += 1
            return Signal(...)

    Returns:
        PluginCall instance (stored in _REGISTRY under func.__name__)
    """
    def decorator(func: Callable) -> PluginCall:
        if stateful:
            _check_stateful_has_state_param(func, func.__name__)

        pc = PluginCall(
            func,
            name=func.__name__,
            category=category,
            shape_in=shape_in,
            shape_out=shape_out,
            pure=pure,
            deps=deps,
            version=version,
            min_bars=min_bars,
            stateful=stateful,
            state_type=state_type,
        )

        if func.__name__ in _REGISTRY:
            warnings.warn(
                f"plugin {func.__name__!r} already registered; overwriting",
                UserWarning,
                stacklevel=2,
            )

        _REGISTRY[func.__name__] = pc
        return pc

    return decorator