"""Strategy registry: plugins register by name and are built from config."""

from collections.abc import Mapping, Sequence
from typing import Any

from tbot.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy[S: type[Strategy]](cls: S) -> S:
    if cls.name in _REGISTRY:
        raise ValueError(f"strategy already registered: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def create_strategy(
    name: str, symbols: Sequence[str], params: Mapping[str, Any] | None = None
) -> Strategy:
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; available: {available_strategies()}")
    return _REGISTRY[name](symbols, params)
