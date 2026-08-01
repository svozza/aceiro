"""Shared GitHub REST client for the harness scripts (prepare_context, post,
execute_plan).

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


# --------------------------------------------------------- TOCTOU predicate --


def pr_moved(pr: dict, reviewed_head: str, reviewed_base_ref: str) -> str | None:
    """What moved since the artifact was produced, or None. Shared by both
    executors, which must not disagree about what "moved" means.

    Head is compared by SHA: a push is exactly what invalidates the review.

    Base is compared by REF, not by SHA. `base.sha` on the PR object tracks the
    tip of the base branch and moves forward with it (probed live: a
    microsoft/vscode PR opened in 2016 reports a base.sha dated 2026, with its
    head 90032 commits behind that sha), so a SHA comparison rejects every
    routine merge into the base branch. That is not a false alarm the caller can
    tolerate — nothing is posted and no later event retries — and it is not what
    the check is for: the artifact is anchored to the EVENT's base SHA precisely
    so a base advance cannot invalidate it (prepare_context.fetch_anchored_pair).
    A retarget changes the ref, which is what genuinely changes the comparison
    the artifact claims to describe.
    """
    if (head := pr["head"]["sha"]) != reviewed_head:
        return f"head moved since review ({head} != {reviewed_head})"
    if (base_ref := pr["base"]["ref"]) != reviewed_base_ref:
        return f"base retargeted since review ({base_ref} != {reviewed_base_ref})"
    return None


# ------------------------------------------------------- pull request reviews --

# The reviews API is the one endpoint family in this client that can gate a
# merge (APPROVE / REQUEST_CHANGES). This harness only ever comments: the event
# is a module constant spliced in below, and `submit_review` takes NO event
# parameter, so no caller — and no artifact field — can reach the other verbs.
# APPROVE is not derivable from anything the harness knows: the only signal it
# has is "the model reported no findings", which is not "the PR is correct".
REVIEW_EVENT = "COMMENT"


def submit_review(repo: str, pr_number: int, body: str, comments: list[dict]) -> dict | list:
    """Create one review carrying every inline comment in a single request.

    The batch is atomic: if any comment's line cannot be resolved against the
    diff, GitHub 422s the whole call and creates ZERO comments (verified live
    in the extraction source). That is what makes "post everything or nothing"
    cheap here — the caller never has to unwind a half-posted review.
    """
    return api_json(
        f"/repos/{repo}/pulls/{pr_number}/reviews",
        method="POST",
        payload={"event": REVIEW_EVENT, "body": body, "comments": comments},
    )


def pull_reviews(repo: str, pr_number: int):
    """Yield every review on the PR (all pages, flattened)."""
    for page in paginate(f"/repos/{repo}/pulls/{pr_number}/reviews?"):
        yield from page


def update_review_body(repo: str, pr_number: int, review_id: int, body: str) -> None:
    """Rewrite a SUBMITTED review's summary body.

    The docs describe this endpoint without stating a state restriction and are
    silent on submitted reviews; probed live on a submitted COMMENTED review, it
    succeeds and the state is unchanged. (Deleting one is genuinely impossible —
    "Submitted reviews cannot be deleted" — so this is the only way to correct
    what a spent review wrapper says.)
    """
    api_json(
        f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
        method="PUT",
        payload={"body": body},
    )


def graphql(query: str, variables: dict) -> dict:
    """One GraphQL POST. Raises on a GraphQL-level error.

    GraphQL is used for exactly one thing here — minimizing a spent review
    wrapper, which has no REST equivalent. It returns HTTP 200 with an `errors`
    array rather than an HTTP error status, so the failure has to be raised
    explicitly or a denied mutation would look like a success.
    """
    response = json.loads(api_request("/graphql", method="POST", payload={"query": query, "variables": variables}))
    if errors := response.get("errors"):
        raise RuntimeError("; ".join(error.get("message", str(error)) for error in errors))
    return response.get("data") or {}


MINIMIZE_MUTATION = """
mutation($subjectId: ID!) {
  minimizeComment(input: {subjectId: $subjectId, classifier: OUTDATED}) {
    minimizedComment { isMinimized }
  }
}
"""


def minimize_review(node_id: str) -> None:
    """Collapse a review in the UI, classified OUTDATED.

    Probed live in the extraction source: minimizing a review does NOT minimize
    the inline comments it posted — they stay visible with live positions,
    because minimization is per-node. That is what makes this safe to use on a
    spent wrapper whose comments the reconciler still owns.

    POST is never retried (an uncertain outcome must not be repeated), and this
    mutation is idempotent anyway — minimizing an already-minimized review is a
    no-op.
    """
    graphql(MINIMIZE_MUTATION, {"subjectId": node_id})


def review_comments(repo: str, pr_number: int):
    """Yield every inline review comment on the PR (all pages, flattened)."""
    for page in paginate(f"/repos/{repo}/pulls/{pr_number}/comments?"):
        yield from page


def delete_review_comment(repo: str, comment_id: int) -> None:
    api_json(f"/repos/{repo}/pulls/comments/{comment_id}", method="DELETE")


def patch_review_comment(repo: str, comment_id: int, body: str) -> None:
    api_json(f"/repos/{repo}/pulls/comments/{comment_id}", method="PATCH", payload={"body": body})
