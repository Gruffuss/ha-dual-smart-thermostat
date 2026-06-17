"""Shared helpers for feature step handlers."""

from __future__ import annotations

import inspect
from typing import Any, Dict
from unittest.mock import AsyncMock


def build_schema_context_from_flow(
    flow_instance, collected_config: dict, current_config: dict | None = None
) -> Dict[str, Any]:
    """Return a lightweight schema context containing a hass states snapshot and merged configs.

    This avoids passing Home Assistant objects directly into schema factories (which may be
    test Mocks) and provides a consistent shape for both config and options flows.
    """
    ctx: dict[str, Any] = dict(collected_config or {})

    # Merge current_config for options flows so defaults and selectors can read persisted values
    if current_config:
        # Do not overwrite explicit collected flags
        for k, v in current_config.items():
            ctx.setdefault(k, v)

    # Build a minimal hass snapshot if available. The real Home Assistant
    # StateMachine exposes its states via ``async_all()`` (a synchronous method
    # returning a list of State objects) and does NOT support dict-style
    # ``values()``. We try ``async_all`` first so the snapshot is actually
    # populated in production, falling back to ``values()`` for dict-like test
    # doubles. (Previously only ``values()`` was tried, so the snapshot was
    # always empty in production — which silently broke selectors that read
    # live entity capabilities from it.)
    hass = getattr(flow_instance, "hass", None)
    states = getattr(hass, "states", None) if hass is not None else None
    if states is not None:
        maybe_values = _states_snapshot_values(states)
        if maybe_values is not None:
            try:
                # Build a simple mapping of entity_id -> state object. Use
                # getattr for entity_id in case test mocks omit it.
                ctx["hass"] = {
                    "states": {getattr(s, "entity_id", None): s for s in maybe_values}
                }
            except Exception:
                # If snapshot fails, skip it (factories handle missing hass).
                pass

    return ctx


def _states_snapshot_values(states) -> list | None:
    """Return a list of State objects from a hass StateMachine or test double.

    Prefers the real StateMachine API ``async_all()``; falls back to a dict-like
    ``values()`` used by some test mocks. Returns the first non-empty collection
    found (or the first empty one seen, or None) without ever calling an
    AsyncMock / coroutine function synchronously.
    """
    fallback: list | None = None
    for accessor_name in ("async_all", "values"):
        accessor = getattr(states, accessor_name, None)
        if accessor is None:
            continue
        try:
            if callable(accessor):
                if isinstance(accessor, AsyncMock) or inspect.iscoroutinefunction(
                    accessor
                ):
                    continue
                result = accessor()
            else:
                result = accessor
            if result is None or inspect.isawaitable(result):
                continue
            result_list = list(result)
        except Exception:
            continue
        if result_list:
            return result_list
        if fallback is None:
            fallback = result_list
    return fallback
