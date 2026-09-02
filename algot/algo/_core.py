"""Plugin core: PluginCall, StatefulState, registry, helpers (per 03-algorithms.md).

Public API (re-exported via algot.algo.__init__):
    PluginCall          — wrapped plugin callable with metadata
    StatefulState       — state container for stateful plugins (per G3)
    _REGISTRY           — global plugin registry (dict: name → PluginCall)
    make_state_from_schema — dict schema → StatefulState
    get_plugin          — retrieve plugin by name
    list_plugins        — list plugins (optionally by category)

State injection model:
    - Stateful plugins declare state_type (dict or dataclass).
    - Engine calls plugin.call(deps_kwds=..., state=state_or_None).
    - Decorator wrapper injects state as a magic local via frame trick,
      so plugin code references `state["key"]` without state being a param.
    - Per G3: state initialized on registration; persistence handled by engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


# Global plugin registry (per 03 §3.1)
_REGISTRY: dict[str, "PluginCall"] = {}


# Allowed categories (per 03 §3.1 — 7 classes, v1 implements 2)
ALLOWED_CATEGORIES = {
    "factor",      # v1
    "signal",      # v1
    "source",      # v2
    "sizer",       # v2
    "risk",        # v2
    "scheduler",   # v2
    "metric",      # v2 (analytical, not trading)
}


# Type whitelist for shape_in/shape_out (per 03 §6.5)
ALLOWED_DTYPES = {
    "int", "float", "str", "bool",
    "datetime",
    "Sequence",            # base 1D sequence (any dtype)
    "Sequence[float64]",
    "Sequence[float32]",
    "Sequence[int64]",
    "OHLCVSequence",
    "ndarray",
    "Signal",
    "None",
}


@dataclass
class StatefulState:
    """State container for stateful plugins (per G3).

    Dict-like interface. Must be JSON-serializable for live crash-recovery
    (per G3 persistence: pickle + JSON dump to .algot_state/).

    Usage in plugin code:
        @plugin(category="signal", stateful=True,
                state_type={"prev_ema": 0.0, "bars": 0})
        def my_signal(x):
            if state["bars"] > 0:
                # ... state["prev_ema"] is accessible
                pass
            state["prev_ema"] = x[0]
            state["bars"] += 1
    """
    _data: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def to_dict(self) -> dict:
        return dict(self._data)

    @classmethod
    def from_dict(cls, data: dict) -> "StatefulState":
        s = cls()
        s._data = dict(data)
        return s

    def __repr__(self) -> str:
        return f"StatefulState({self._data!r})"


def make_state_from_schema(schema: dict) -> StatefulState:
    """Create zero-init StatefulState from a {key: default} dict schema.

    Per G3 helper: `state_type={"prev": 0.0, "bars": 0}` → StatefulState
    where each key has its default value, cloned to avoid shared refs.

    All defaults must be JSON-serializable (int/float/str/bool/None/list/dict).
    """
    state = StatefulState()
    for k, v in schema.items():
        # Validate JSON-serializable at init time (per G3)
        try:
            json.dumps(v)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"state_type[{k!r}] = {v!r} is not JSON-serializable: {e}"
            ) from e
        state._data[k] = _json_clone(v)
    return state


def _json_clone(obj: Any) -> Any:
    """Deep-clone JSON-compatible values (avoid shared mutable refs)."""
    if isinstance(obj, dict):
        return {k: _json_clone(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_clone(x) for x in obj]
    return obj


class PluginCall:
    """Wrapped plugin callable with metadata (per 03 §3.1).

    Created by @algot.plugin decorator. Stored in _REGISTRY.

    The engine interacts with plugins via:
        pc = get_plugin("sma")
        pc.init_state()  # if stateful
        result = pc.call(deps_kwds={"x": seq}, state=state_or_None)
    """

    def __init__(
        self,
        func: Callable,
        *,
        name: str,
        category: str,
        shape_in: dict[str, str] | None = None,
        shape_out: dict[str, str] | None = None,
        pure: bool = True,
        deps: list[str] | None = None,
        version: str = "0.1.0",
        min_bars: int = 0,
        stateful: bool = False,
        state_type: type | dict | None = None,
    ):
        # ---- validation ----

        if category not in ALLOWED_CATEGORIES:
            raise ValueError(
                f"plugin {name!r}: category must be one of "
                f"{sorted(ALLOWED_CATEGORIES)}, got {category!r}"
            )

        if shape_in is not None:
            if not isinstance(shape_in, dict):
                raise ValueError(
                    f"plugin {name!r}: shape_in must be a dict "
                    f"{{param_name: dtype}}, got {type(shape_in).__name__}"
                )
            for k, v in shape_in.items():
                if v not in ALLOWED_DTYPES:
                    raise ValueError(
                        f"plugin {name!r}: shape_in[{k!r}] dtype {v!r} "
                        f"not in whitelist {sorted(ALLOWED_DTYPES)}"
                    )

        if shape_out is not None:
            if isinstance(shape_out, dict):
                for k, v in shape_out.items():
                    if v not in ALLOWED_DTYPES:
                        raise ValueError(
                            f"plugin {name!r}: shape_out[{k!r}] dtype {v!r} "
                            f"not in whitelist"
                        )
            elif isinstance(shape_out, str):
                if shape_out not in ALLOWED_DTYPES:
                    raise ValueError(
                        f"plugin {name!r}: shape_out dtype {shape_out!r} "
                        f"not in whitelist"
                    )
            else:
                raise ValueError(
                    f"plugin {name!r}: shape_out must be str or dict, "
                    f"got {type(shape_out).__name__}"
                )

        if stateful and state_type is None:
            raise ValueError(
                f"plugin {name!r}: stateful=True requires state_type "
                f"(dict schema or dataclass type)"
            )
        if state_type is not None and not stateful:
            raise ValueError(
                f"plugin {name!r}: state_type provided but stateful=False — "
                f"set stateful=True or remove state_type"
            )

        # Validate JSON-serializable defaults (per G3)
        if stateful and isinstance(state_type, dict):
            try:
                json.dumps(state_type)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"plugin {name!r}: state_type dict not JSON-serializable: {e}"
                ) from e

        if min_bars < 0:
            raise ValueError(
                f"plugin {name!r}: min_bars must be >= 0, got {min_bars}"
            )

        if deps is not None:
            for dep in deps:
                if not isinstance(dep, str):
                    raise ValueError(
                        f"plugin {name!r}: deps entries must be str, got {type(dep).__name__}"
                    )

        # ---- assign ----

        self.func = func
        self.name = name
        self.category = category
        self.shape_in = shape_in or {}
        self.shape_out = shape_out or {}
        self.pure = pure
        self.deps = list(deps) if deps else []
        self.version = version
        self.min_bars = min_bars
        self.stateful = stateful
        self.state_type = state_type
        self._state: StatefulState | None = None

    def init_state(self) -> None:
        """Create fresh state instance (per G3: framework creates on init/reset).

        Idempotent — re-initializes state to default values.
        """
        if not self.stateful:
            self._state = None
            return
        if isinstance(self.state_type, dict):
            self._state = make_state_from_schema(self.state_type)
        elif isinstance(self.state_type, type):
            # dataclass type: instantiate with defaults
            self._state = self.state_type()
            # Wrap dataclass instance in StatefulState via to_dict if it has one
            # Otherwise, expect state_type to inherit from StatefulState
            if not isinstance(self._state, StatefulState):
                if hasattr(self._state, "to_dict") and callable(self._state.to_dict):
                    wrapped = StatefulState()
                    wrapped._data = self._state.to_dict()
                    self._state = wrapped
                else:
                    raise ValueError(
                        f"plugin {self.name!r}: state_type {self.state_type.__name__} "
                        f"must inherit from StatefulState or expose to_dict()"
                    )
        else:
            raise ValueError(
                f"plugin {self.name!r}: state_type must be dict or type, "
                f"got {type(self.state_type).__name__}"
            )

    def reset_state(self) -> None:
        """Reset to fresh state (CLI --reset, per G3)."""
        self.init_state()

    def get_state(self) -> StatefulState | None:
        """Return current state (for persistence, per G3)."""
        return self._state

    def load_state(self, data: dict) -> None:
        """Restore state from dict (live crash-recovery, per G3)."""
        if not self.stateful:
            return
        self._state = StatefulState.from_dict(data)

    def call(
        self,
        deps_kwds: dict[str, Any] | None = None,
        state: Any = None,
    ) -> Any:
        """Call the plugin with injected deps + state (per 03 §3.2 dispatch).

        Args:
            deps_kwds: {dep_name: result} injected as kwargs from engine
            state:     StatefulState instance (or None for stateless plugins)

        Returns:
            Plugin's return value (Sequence / ndarray / Signal / None / etc.)
        """
        return self._invoke(deps_kwds or {}, state)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow direct invocation: algot.sma(x) → .call(deps_kwds={'x': x}).

        Positional args are bound by name to function signature parameters.
        Use kwargs for explicit param names. The 'state' param is reserved
        for framework injection (never pass it yourself).
        """
        import inspect
        sig = inspect.signature(self.func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        args_dict = dict(bound.arguments)
        # state is reserved for framework injection
        args_dict.pop("state", None)
        return self._invoke(args_dict, None)

    def _invoke(self, deps_kwds: dict[str, Any], state: Any) -> Any:
        """Internal: dispatch with proper state injection."""
        # Filter out 'state' from kwargs if user accidentally passed it
        clean_kwds = {k: v for k, v in deps_kwds.items() if k != "state"}
        if self.stateful:
            state_to_use = state if state is not None else self._state
            if state_to_use is None:
                raise RuntimeError(
                    f"plugin {self.name!r}: stateful but no state provided — "
                    f"call init_state() first"
                )
            # Pass state as kwarg; plugin must declare 'state' param
            return self.func(state=state_to_use, **clean_kwds)
        else:
            return self.func(**clean_kwds)


def _check_stateful_has_state_param(func: Callable, name: str) -> None:
    """Validate that stateful plugin's signature includes 'state' parameter.

    Per spec: stateful plugins must have 'state' in their signature so the
    framework can inject the state object as a kwarg.
    """
    import inspect
    sig = inspect.signature(func)
    if "state" not in sig.parameters:
        raise ValueError(
            f"plugin {name!r}: stateful=True requires 'state' parameter in "
            f"function signature, e.g.:\n"
            f"    @plugin(category='signal', stateful=True, "
            f"state_type={{'counter': 0}})\n"
            f"    def my_signal(x, state):\n"
            f"        state['counter'] += 1\n"
            f"        return Signal(...)"
        )


def get_plugin(name: str) -> PluginCall:
    """Retrieve a registered plugin by name."""
    if name not in _REGISTRY:
        raise KeyError(
            f"plugin {name!r} not found in registry. "
            f"Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def list_plugins(category: str | None = None) -> list[str]:
    """List all registered plugins, optionally filtered by category."""
    if category is None:
        return sorted(_REGISTRY.keys())
    return sorted(
        name for name, pc in _REGISTRY.items() if pc.category == category
    )


def clear_registry() -> None:
    """Clear the registry (used in tests; not for production)."""
    _REGISTRY.clear()