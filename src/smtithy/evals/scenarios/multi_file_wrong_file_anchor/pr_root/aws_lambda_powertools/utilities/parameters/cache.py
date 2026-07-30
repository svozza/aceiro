from __future__ import annotations

import time
from typing import Any


class ParameterCache:
    """Time-boxed cache for resolved parameter values."""

    def __init__(self, max_age_seconds: int = 5):
        self.max_age_seconds = max_age_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def _now(self) -> float:
        return time.monotonic()

    def is_expired(self, stored_at: float) -> bool:
        """True once the entry is older than max_age_seconds."""
        age = self._now() - stored_at
        return age > self.max_age_seconds

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self.is_expired(stored_at):
            del self._store[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._store[key] = (self._now(), value)
