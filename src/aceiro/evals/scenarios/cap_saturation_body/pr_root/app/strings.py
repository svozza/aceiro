"""String helpers."""
from __future__ import annotations


def titlecase(value: str) -> str:
    """Capitalise the first letter of *value*, leaving the rest untouched."""
    if not value:
        return value
    return value[0].title() + value[1:]
