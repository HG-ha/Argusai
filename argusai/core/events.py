"""Tiny in-process event bus used to stream progress to CLI/GUI."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Thread-safe fan-out bus. Subscribers are plain callables.

    Emitted events are also stored on a ring buffer so late subscribers (e.g.
    a GUI polling every 500ms) can replay what they missed.
    """

    def __init__(self, history: int = 5000) -> None:
        self._subs: list[Callable[[Event], None]] = []
        self._history: list[Event] = []
        self._max_history = history
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return _unsubscribe

    def emit(self, type: str, **payload: Any) -> Event:
        evt = Event(type=type, payload=payload)
        with self._lock:
            self._history.append(evt)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(evt)
            except Exception:
                pass
        return evt

    def since(self, index: int) -> list[Event]:
        with self._lock:
            return self._history[index:]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._history)
