"""Plugin framework tests (per docs/03-algorithms.md §3).

Tests cover:
    - @plugin decorator + registration
    - Metadata validation (category, shape_in/out, stateful)
    - _REGISTRY management
    - Stateful plugin state injection (magic local 'state')
    - deps tracking
    - state_type dict/dataclass validation
    - JSON-serializable state check
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import algot.algo
from algot.algo import (
    _REGISTRY,
    PluginCall,
    StatefulState,
    clear_registry,
    get_plugin,
    list_plugins,
    make_state_from_schema,
    plugin,
)


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear registry before each test to avoid cross-test pollution."""
    clear_registry()
    yield
    clear_registry()


# ---------- decorator basic ----------

def test_plugin_decorator_returns_plugincall():
    @plugin(category="factor", shape_in={"x": "Sequence[float64]"})
    def my_factor(x):
        return x
    assert isinstance(my_factor, PluginCall)
    assert my_factor.name == "my_factor"
    assert my_factor.category == "factor"


def test_plugin_registered_in_registry():
    @plugin(category="signal")
    def my_signal(x):
        return None
    assert "my_signal" in _REGISTRY
    assert _REGISTRY["my_signal"].func is my_signal.func


def test_plugin_get_plugin_lookup():
    @plugin(category="factor")
    def lookup_test(x):
        return x
    pc = get_plugin("lookup_test")
    assert pc.name == "lookup_test"


def test_plugin_get_unknown_raises():
    with pytest.raises(KeyError, match="plugin 'nonexistent' not found"):
        get_plugin("nonexistent")


def test_plugin_list_plugins():
    @plugin(category="factor")
    def f1(x): return x

    @plugin(category="signal")
    def s1(x): return None

    all_plugins = list_plugins()
    assert "f1" in all_plugins
    assert "s1" in all_plugins

    factor_plugins = list_plugins(category="factor")
    assert "f1" in factor_plugins
    assert "s1" not in factor_plugins


# ---------- category validation ----------

def test_plugin_invalid_category_raises():
    with pytest.raises(ValueError, match="category must be one of"):
        @plugin(category="bogus_category")
        def bad_cat(x):
            return x


def test_plugin_all_categories_accepted():
    """All 7 categories from spec §3.1."""
    categories = ["factor", "signal", "source", "sizer", "risk", "scheduler", "metric"]
    for cat in categories:
        @plugin(category=cat)
        def test_func(x):
            return x
        assert test_func.category == cat


# ---------- shape_in validation ----------

def test_plugin_shape_in_valid_dtype():
    @plugin(category="factor", shape_in={"x": "Sequence[float64]"})
    def f(x): return x
    assert f.shape_in == {"x": "Sequence[float64]"}


def test_plugin_shape_in_invalid_dtype_raises():
    with pytest.raises(ValueError, match="dtype .* not in whitelist"):
        @plugin(category="factor", shape_in={"x": "BogusType"})
        def bad(x): return x


def test_plugin_shape_in_must_be_dict():
    with pytest.raises(ValueError, match="shape_in must be a dict"):
        @plugin(category="factor", shape_in="Sequence[float64]")  # type: ignore[arg-type]
        def bad(x): return x


# ---------- shape_out validation ----------

def test_plugin_shape_out_str():
    @plugin(category="factor", shape_out="Sequence[float64]")
    def f(x): return x
    assert f.shape_out == "Sequence[float64]"


def test_plugin_shape_out_dict():
    @plugin(category="factor", shape_out={"result": "Sequence[float64]"})
    def f(x): return x
    assert f.shape_out == {"result": "Sequence[float64]"}


def test_plugin_shape_out_invalid_raises():
    with pytest.raises(ValueError, match="dtype .* not in whitelist"):
        @plugin(category="factor", shape_out="BadType")
        def bad(x): return x


# ---------- stateful validation ----------

def test_plugin_stateful_requires_state_type():
    with pytest.raises(ValueError, match="stateful=True requires state_type"):
        @plugin(category="signal", stateful=True)
        def bad(x, state): return None


def test_plugin_state_type_without_stateful_raises():
    with pytest.raises(ValueError, match="stateful=False"):
        @plugin(category="signal", stateful=False, state_type={"x": 0})
        def bad(x): return None


def test_plugin_state_type_dict_json_validation():
    """JSON-serializable default check (per G3)."""
    with pytest.raises(ValueError, match="not JSON-serializable"):
        @plugin(category="signal", stateful=True,
                state_type={"bad": lambda: 42})  # function not JSON-serializable
        def bad(x, state): return None


def test_plugin_state_type_dataclass_accepted():
    @dataclass
    class MyState:
        prev_ema: float = 0.0
        bars: int = 0

    @plugin(category="signal", stateful=True, state_type=MyState)
    def my_sig(x, state): return None


# ---------- call / dispatch ----------

def test_plugin_call_with_deps():
    """deps_kwds inject as kwargs."""
    @plugin(category="factor", deps=["dep_x"])
    def consumer(dep_x, state=None):
        return dep_x * 2

    pc = get_plugin("consumer")
    result = pc.call(deps_kwds={"dep_x": 21})
    assert result == 42


def test_plugin_call_stateless_no_state():
    @plugin(category="factor")
    def add_one(x, state=None):
        return x + 1

    pc = get_plugin("add_one")
    assert pc.call(deps_kwds={"x": 10}) == 11


# ---------- stateful state injection ----------

def test_plugin_state_magic_local_access():
    """Plugin code accesses state['key'] as parameter."""
    @plugin(category="signal", stateful=True,
            state_type={"counter": 0, "last": None})
    def counter_sig(x, state):
        state["counter"] += 1
        state["last"] = x
        return state["counter"]

    pc = get_plugin("counter_sig")
    pc.init_state()

    # Call multiple times — state persists
    r1 = pc.call(deps_kwds={"x": 1})
    r2 = pc.call(deps_kwds={"x": 2})
    r3 = pc.call(deps_kwds={"x": 3})

    assert r1 == 1
    assert r2 == 2
    assert r3 == 3
    assert pc.get_state()["counter"] == 3
    assert pc.get_state()["last"] == 3


def test_plugin_state_init_state():
    @plugin(category="signal", stateful=True,
            state_type={"a": 1, "b": 2.5, "c": "hello"})
    def s(x, state): return state["a"]

    pc = get_plugin("s")
    pc.init_state()
    state = pc.get_state()
    assert state["a"] == 1
    assert state["b"] == 2.5
    assert state["c"] == "hello"


def test_plugin_state_reset():
    @plugin(category="signal", stateful=True,
            state_type={"counter": 0})
    def s(x, state):
        state["counter"] += 1
        return state["counter"]

    pc = get_plugin("s")
    pc.init_state()
    pc.call(deps_kwds={"x": 1})
    pc.call(deps_kwds={"x": 2})
    assert pc.get_state()["counter"] == 2

    pc.reset_state()
    assert pc.get_state()["counter"] == 0


def test_plugin_state_persistence_roundtrip():
    @plugin(category="signal", stateful=True,
            state_type={"x": 0, "y": 0})
    def s(x, state):
        state["x"] += 1
        return None

    pc = get_plugin("s")
    pc.init_state()
    pc.call(deps_kwds={"x": 1})
    pc.call(deps_kwds={"x": 2})

    # Serialize (per G3 live persistence)
    snapshot = pc.get_state().to_dict()

    # Restore in new plugin instance
    pc.load_state(snapshot)
    assert pc.get_state()["x"] == 2


def test_plugin_state_load_state_no_op_for_stateless():
    @plugin(category="factor")
    def s(x, state=None): return x

    pc = get_plugin("s")
    pc.load_state({"any": "data"})  # should not raise
    assert pc.get_state() is None


# ---------- StatefulState class ----------

def test_stateful_state_dict_interface():
    s = StatefulState()
    s["key1"] = 42
    s["key2"] = "hello"
    assert s["key1"] == 42
    assert s["key2"] == "hello"
    assert "key1" in s
    assert "missing" not in s
    assert s.get("missing") is None
    assert s.get("missing", 99) == 99


def test_make_state_from_schema_json_clone():
    """Defaults are cloned to avoid shared mutable refs."""
    schema = {"items": [1, 2, 3], "config": {"nested": True}}
    s1 = make_state_from_schema(schema)
    s1["items"].append(99)
    s2 = make_state_from_schema(schema)
    assert s2["items"] == [1, 2, 3]  # not affected by s1's mutation


def test_make_state_from_schema_rejects_non_json():
    with pytest.raises(ValueError, match="not JSON-serializable"):
        make_state_from_schema({"x": {1, 2, 3}})  # set is not JSON


# ---------- min_bars validation ----------

def test_plugin_min_bars_negative_raises():
    with pytest.raises(ValueError, match="min_bars must be >= 0"):
        @plugin(category="factor", min_bars=-1)
        def bad(x): return x


def test_plugin_min_bars_stored():
    @plugin(category="factor", min_bars=20)
    def f(x): return x
    assert f.min_bars == 20


# ---------- deps validation ----------

def test_plugin_deps_stored():
    @plugin(category="factor", deps=["sma_20", "atr_14"])
    def f(x): return x
    assert f.deps == ["sma_20", "atr_14"]


def test_plugin_deps_must_be_strings():
    with pytest.raises(ValueError, match="deps entries must be str"):
        @plugin(category="factor", deps=[42])  # type: ignore[list-item]
        def bad(x): return x


# ---------- duplicate registration warning ----------

def test_plugin_duplicate_registration_warns():
    @plugin(category="factor")
    def dup(x): return x
    with pytest.warns(UserWarning, match="already registered"):
        @plugin(category="factor")
        def dup(x): return x  # noqa: F811


# ---------- stateless plugins don't need state ----------

def test_stateless_plugin_state_arg_none():
    """Stateless plugins don't have state in signature."""
    @plugin(category="factor")
    def stateless(x):
        # state is NOT available for stateless plugins
        return x * 2

    pc = get_plugin("stateless")
    result = pc.call(deps_kwds={"x": 5})
    assert result == 10


def test_stateless_plugin_init_state_no_op():
    @plugin(category="factor")
    def f(x): return x
    pc = get_plugin("f")
    pc.init_state()  # should not raise
    assert pc.get_state() is None