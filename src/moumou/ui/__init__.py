"""Lazy P2 interface exports; importing this package does not load optional Rich."""

from __future__ import annotations

__all__ = ["run_cli", "load_events", "load_replay", "replay", "replay_tree"]


def __getattr__(name: str):
    if name == "run_cli":
        from .cli import run_cli

        globals()[name] = run_cli
        return run_cli
    if name in {"load_events", "load_replay", "replay", "replay_tree"}:
        from importlib import import_module

        module = import_module(".replay", __name__)
        # Submodule loading sets package.replay to a module. Restore the public
        # function exports together so subsequent lazy access is consistent.
        for exported in ("load_events", "load_replay", "replay", "replay_tree"):
            globals()[exported] = getattr(module, exported)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
