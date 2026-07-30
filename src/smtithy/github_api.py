"""Shared GitHub REST client for the harness scripts (prepare_context, post).

One retry core: bounded exponential backoff honouring Retry-After, applied
only to methods that are safe to repeat (a failed POST has an uncertain
outcome — the resource may exist — so it is never retried).

Environment: GITHUB_TOKEN.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_METHODS = {"GET", "PATCH"}
MAX_ATTEMPTS = 4


def api_request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> bytes:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if body else {}),
        },
    )
    attempts = MAX_ATTEMPTS if method in RETRYABLE_METHODS else 1
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status = getattr(exc, "code", None)
            retryable = status in RETRYABLE_STATUS or status is None
            if not retryable or attempt == attempts:
                raise
            retry_after = (getattr(exc, "headers", None) or {}).get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            print(f"transient GitHub API failure ({exc}); retrying in {delay}s", file=sys.stderr)
            time.sleep(min(delay, 60))
    raise AssertionError("unreachable")


def api_json(path: str, method: str = "GET", payload: dict | None = None) -> dict | list:
    return json.loads(api_request(path, method=method, payload=payload) or b"{}")


def paginate(path_with_query: str):
    """Yield pages (lists) for a paged endpoint until a short page ends it.

    ``path_with_query`` must already contain a query string (``?...``).
    """
    page = 1
    while True:
        batch = api_json(f"{path_with_query}&per_page=100&page={page}")
        yield batch
        if len(batch) < 100:
            return
        page += 1


def fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)
