"""A tiny bounded cache."""
from __future__ import annotations

from collections import OrderedDict


class BoundedCache:
    """Least-recently-used cache with a fixed capacity."""

    def __init__(self, capacity: int) -> None:
        self._data: OrderedDict[str, str] = OrderedDict()
        self._capacity = capacity

    def put(self, key: str, value: str) -> None:
        self._data[key] = value
        if len(self._data) > self._capacity:
            self._data.popitem(last=True)
