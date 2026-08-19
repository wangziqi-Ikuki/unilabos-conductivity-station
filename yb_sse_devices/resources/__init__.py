"""电导工站物料资源：耗材、料架、Deck。"""

from typing import Any

__all__ = [
    "ConductivityStation_Deck",
    "ConductivityFunnel",
    "ConductivityMold",
    "ConductivitySinteringBottle",
    "conductivity_rack_layer",
]

_EXPORTS = {
    "ConductivityStation_Deck": (".decks", "ConductivityStation_Deck"),
    "ConductivityFunnel": (".materials", "ConductivityFunnel"),
    "ConductivityMold": (".materials", "ConductivityMold"),
    "ConductivitySinteringBottle": (".materials", "ConductivitySinteringBottle"),
    "conductivity_rack_layer": (".warehouses", "conductivity_rack_layer"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), attr)
    globals()[name] = value
    return value
