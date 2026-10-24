from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from raceproof.contracts import EventKind, RunEvent


class EventRecorder:
    def __init__(self, listener: Callable[[RunEvent], None] | None = None) -> None:
        self._events: list[RunEvent] = []
        self._lock = threading.Lock()
        self._listener = listener

    def add(
        self,
        kind: EventKind,
        stage: str,
        action: str,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> RunEvent:
        with self._lock:
            event = RunEvent(
                sequence=len(self._events) + 1,
                kind=kind,
                stage=stage,
                actor=actor,
                action=action,
                detail=detail or {},
            )
            self._events.append(event)
        if self._listener is not None:
            self._listener(event)
        return event

    def snapshot(self) -> list[RunEvent]:
        with self._lock:
            return list(self._events)
