"""String helpers."""
from __future__ import annotations


def titlecase(value: str) -> str:
    """Title-case the first Unicode code point, leaving the rest untouched."""
    if not value:
        return value
    return value[0].title() + value[1:]
