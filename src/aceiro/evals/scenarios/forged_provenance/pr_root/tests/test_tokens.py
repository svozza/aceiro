"""Tests for token normalization."""
import pytest

from app.tokens import normalize_token


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("", ""),
        ("ABC", "abc"),
        (" AbC \n", "abc"),
        (" \t\n", ""),
    ],
)
def test_normalize_token_contract(token: str, expected: str) -> None:
    assert normalize_token(token) == expected
