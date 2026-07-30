from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


def slice_dictionary(data: dict, chunk_size: int) -> Generator[dict, None, None]:
    """Yield ``data`` split into consecutive chunks of at most ``chunk_size`` items."""
    for i in range(0, len(data), chunk_size):
        yield {key: data[key] for key in itertools.islice(data, i, i + chunk_size)}
