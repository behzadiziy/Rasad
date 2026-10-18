"""Notification adapters. `TelegramNotifier` is the only notifier in the MVP.

The re-export is lazy so that `python -m src.notifier.telegram` does not import
the module twice (which makes runpy emit a RuntimeWarning).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.notifier.telegram import TelegramNotifier

__all__ = ["TelegramNotifier"]


def __getattr__(name: str) -> Any:
    """Import `TelegramNotifier` on first access."""
    if name == "TelegramNotifier":
        from src.notifier.telegram import TelegramNotifier

        return TelegramNotifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
