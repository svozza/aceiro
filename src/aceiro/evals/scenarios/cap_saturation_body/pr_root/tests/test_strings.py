"""Tests for string helpers."""
import pytest

from app.strings import titlecase


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("a", "A"),
        ("hELLO", "HELLO"),
        ("Already", "Already"),
        ("éclair", "Éclair"),
        ("ǆemal", "ǅemal"),
        ("ﬁsh", "Fish"),
        ("ßig", "Ssig"),
    ],
)
def test_titlecase_preserves_the_tail(value: str, expected: str) -> None:
    assert titlecase(value) == expected
