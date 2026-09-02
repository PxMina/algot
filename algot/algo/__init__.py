"""algot.algo — plugin framework.

Public API:
    plugin              — @algot.plugin decorator
    _REGISTRY           — global plugin registry (dict: name → PluginCall)
    PluginCall          — wrapped plugin callable with metadata
    StatefulState       — state container for stateful plugins (per G3)
    make_state_from_schema — dict schema → StatefulState
    get_plugin          — retrieve plugin by name
    list_plugins        — list plugins (optionally by category)
    clear_registry      — clear registry (for tests)
"""

from algot.algo._core import (
    _REGISTRY,
    ALLOWED_CATEGORIES,
    ALLOWED_DTYPES,
    PluginCall,
    StatefulState,
    clear_registry,
    get_plugin,
    list_plugins,
    make_state_from_schema,
)
from algot.algo.plugin import plugin

__all__ = [
    "plugin",
    "_REGISTRY",
    "ALLOWED_CATEGORIES",
    "ALLOWED_DTYPES",
    "PluginCall",
    "StatefulState",
    "make_state_from_schema",
    "get_plugin",
    "list_plugins",
    "clear_registry",
]