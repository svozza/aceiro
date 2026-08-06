"""Shared GitHub REST client for the harness scripts (prepare_context, post,
execute_plan).

One retry core: bounded exponential backoff honouring Retry-After, applied
only to methods that are safe to repeat (a failed POST has an uncertain
outcome — the resource may exist — so it is never retried).

Environment: GITHUB_TOKEN.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import cast

API_ROOT = "https://api.github.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_METHODS = {"GET", "PATCH"}
MAX_ATTEMPTS = 4


# The depth this client will follow, enforced below by overriding urllib's own
# max_redirections (10). Declared and ENFORCED in one place: as a bare constant it
# was dead, and the effective bound was urllib's — so the region documented a limit
# no reader could rely on. GitHub's API redirects at most once on the paths this
# harness uses, so five is slack rather than a constraint.
MAX_REDIRECTS = 5


class _StripAuthOnCrossOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect leaves the API host.

    A handler rather than an `except HTTPError` branch, because urllib follows
    redirects INSIDE urlopen: `http_error_302` calls `open()` again and only the
    final response surfaces, so a caller inspecting exception codes never sees the
    hop at all. Verified against a local server — urlopen returns 200 having
    re-sent `Authorization` verbatim to the new host, and raises nothing.

    That default is wrong for this client.
    /repos/{repo}/actions/artifacts/{id}/zip answers 302 to Azure Blob Storage,
    which rejects the unexpected bearer token with 401 (measured — it broke the
    first real /fix command) and has meanwhile been handed a GitHub credential it
    should never see. The signed storage URL carries its own auth in the query
    string.

    Same-origin hops keep the header: api.github.com redirecting within itself is
    still GitHub, and an authenticated endpoint that merely moved must not start
    failing. Compared on scheme+netloc, never by prefix —
    `api.github.com.evil.test` contains the API host as a prefix, and a prefix test
    would forward the token to it.
    """

    # urllib reads this off the handler, so the module's stated bound is the one the
    # opener enforces rather than a second, larger default (10). max_repeats — the
    # separate bound on revisiting ONE url — is deliberately left at urllib's
    # default of 4, which is already stricter than this.
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        api = urllib.parse.urlsplit(API_ROOT)
        hop = urllib.parse.urlsplit(new.full_url)
        if (hop.scheme, hop.netloc) != (api.scheme, api.netloc):
            # remove_header is case-insensitive on the capitalized form urllib
            # stores, which is why this does not filter the dict by hand.
            new.remove_header("Authorization")
        return new


# One opener for the whole client, so no call site can forget the handler.
_OPENER = urllib.request.build_opener(_StripAuthOnCrossOriginRedirect())


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
            # The module opener, never urllib.request.urlopen: redirects are followed
            # inside this call, so the handler stripping Authorization on a
            # cross-origin hop is the only place that decision can be made.
            with _OPENER.open(request, timeout=60) as response:
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


def submit_review(repo: str, pr_number: int, body: str, comments: list[dict], *, head_sha: str) -> dict | list:
    """Create one review carrying every inline comment in a single request.

    The batch is atomic: if any comment's line cannot be resolved against the
    diff, GitHub 422s the whole call and creates ZERO comments (verified live
    in the extraction source). That is what makes "post everything or nothing"
    cheap here — the caller never has to unwind a half-posted review.

    `head_sha` is the SHA the artifact was VERIFIED against, sent as commit_id.
    Keyword-only with no default because omitting it is not a neutral choice:
    the documented behaviour is "defaults to the most recent commit in the pull
    request", so a review of head A attaches to head B when a push lands between
    the executor's TOCTOU pre-check and this request, and an A-derived suggestion
    then sits on B's version of a line that still resolves. Naming the SHA moves
    the anchor to the commit the artifact describes: the comment is marked
    outdated against the newer head rather than misplaced on it. The pre- and
    post-write drift checks stay — this bounds the window they cannot close,
    since the check and the write are not atomic.
    """
    if not head_sha:
        raise ValueError("submit_review needs the reviewed head SHA; an unbound review would attach to whatever is current")
    return api_json(
        f"/repos/{repo}/pulls/{pr_number}/reviews",
        method="POST",
        payload={"event": REVIEW_EVENT, "body": body, "comments": comments, "commit_id": head_sha},
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
    # A GraphQL response is an object. Anything else cannot be inspected for
    # `errors`, so it is a failure rather than a body to read `data` out of —
    # otherwise the errors check is skipped by whatever shape arrived.
    if not isinstance(response, dict):
        raise RuntimeError(f"GraphQL answered with {type(response).__name__}, not an object")
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


# ------------------------------------------------- the stacked-PR write path --

# blobs -> tree -> commit -> ref, then POST /pulls. The sequence the stacked
# follow-up pull request is delivered through (ADR-0009 addendum).
#
# Chosen over PUT /repos/{repo}/contents/{path} for containment, not style. That
# endpoint is one COMMIT per file — up to policy.plan.max_patched_files of them —
# each immediately visible on a branch that must already exist, and POST is never
# retried here, so a failure partway through leaves a real branch carrying half a
# fix and no pull request to explain it. Blobs, trees and commits are UNREFERENCED
# objects: nothing is visible to anyone until the final create_ref, and every
# partial failure before that point leaves only garbage GitHub eventually
# collects. The single visible mutation is also what gives the dedup key its
# atomicity — create_ref 422s on a ref that already exists, so GitHub itself is
# the compare-and-swap.
#
# Every call below is a POST and therefore never retried. That is deliberate and
# it is why the ordering matters: an uncertain outcome on an unreferenced object
# costs nothing, while an uncertain outcome on the ref would risk a second branch.

# Regular non-executable file. The harness never delivers a mode change: a patch
# step carries `path`, `old` and `new` and says nothing about permissions, so
# preserving a mode it never described would mean reading one from the tree and
# claiming the plan asked for it. 100644 for every blob, and a file that was
# executable at the reviewed SHA comes back non-executable in the fix commit —
# visible in the follow-up PR's diff, which is the fail-visible direction.
BLOB_MODE = "100644"


def create_blob(repo: str, content: bytes) -> str:
    """Upload one file's bytes as a blob; return its SHA.

    base64 rather than "utf-8" encoding, because the content is a contributor's
    file with a verified replacement spliced into it and a contributor's file is
    not guaranteed to be valid UTF-8. Declaring utf-8 for bytes that are not would
    either fail the call or corrupt the file — and the corrupted version would be
    bytes no gate ever saw, which is exactly the divergence the shared applier
    exists to prevent.
    """
    blob = cast("dict", api_json(
        f"/repos/{repo}/git/blobs",
        method="POST",
        payload={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
    ))
    return blob["sha"]


def read_commit(repo: str, sha: str) -> dict:
    """One commit object, for its `tree` SHA.

    A GET, so it is retried. Needed because /git/trees takes a TREE sha while
    everything else in this path speaks commit SHAs: the reviewed head is a commit,
    and passing it where a tree is expected names a different object.
    """
    return cast("dict", api_json(f"/repos/{repo}/git/commits/{sha}"))


def create_tree(repo: str, base_tree: str, blobs: dict[str, str]) -> str:
    """Build a tree that is `base_tree` with `blobs` (path -> blob SHA) replaced.

    `base_tree` is load-bearing: it makes this a PATCH of the reviewed tree rather
    than a replacement of it. Omitted, the new tree holds ONLY the patched paths
    and every other file in the repository reads as DELETED — a verified two-line
    fix delivered as "delete everything else", which no bound in the policy would
    have caught because the policy bounds what the plan CHANGES, not what a tree
    omits.
    """
    return cast("dict", api_json(
        f"/repos/{repo}/git/trees",
        method="POST",
        payload={
            "base_tree": base_tree,
            "tree": [
                {"path": path, "mode": BLOB_MODE, "type": "blob", "sha": sha}
                for path, sha in blobs.items()
            ],
        },
    ))["sha"]


def create_commit(repo: str, message: str, *, tree: str, parent: str) -> str:
    """Create a commit object with exactly one parent; return its SHA.

    The parent is the REVIEWED HEAD, always. ADR-0005 anchors every `old` to the
    file's bytes at that SHA, so the patch applies cleanly on that tree and on no
    other; committing onto a different parent would produce content the anchor
    never described. Singular rather than a list for that reason — this path has no
    use for a merge commit, and accepting several parents would let a caller build
    one by accident.
    """
    return cast("dict", api_json(
        f"/repos/{repo}/git/commits",
        method="POST",
        payload={"message": message, "tree": tree, "parents": [parent]},
    ))["sha"]


def create_ref(repo: str, branch: str, sha: str) -> dict | list:
    """Point a NEW branch at `sha`. The one visible mutation of the sequence.

    Fully qualified to refs/heads/, because the API takes a ref and not a branch
    name: sending the bare name creates a ref literally called `smtithy/fix-x`
    outside refs/heads, which no pull request can open from and nothing in the UI
    shows.

    422s if the ref already exists, which is the compare-and-swap this delivery's
    deduplication relies on rather than a failure to paper over: GitHub decides who
    wins, atomically, with no read-then-write window for a second command to slip
    into.
    """
    return api_json(
        f"/repos/{repo}/git/refs",
        method="POST",
        payload={"ref": f"refs/heads/{branch}", "sha": sha},
    )


def open_pull_request(repo: str, *, head: str, base: str, title: str, body: str) -> dict:
    """Open the follow-up pull request from `head` into `base`.

    `base` is a required keyword parameter with NO default, and that is a pinned
    decision rather than an implementation detail (ADR-0009 addendum: "the base is
    never model-suppliable"). The plan's `open_pr` step deliberately carries no
    base argument, so the value can only come from the live pull-request context
    the executor holds. A default of "main" here would have implemented the absurd
    reading the addendum was written to forbid — merge the bug, then merge the fix,
    with the default branch knowingly broken in between.
    """
    return cast("dict", api_json(
        f"/repos/{repo}/pulls",
        method="POST",
        payload={"head": head, "base": base, "title": title, "body": body},
    ))


def pull_requests_for_base(repo: str, base: str):
    """Yield every pull request opened against `base`, in ANY state.

    `state=all` because a maintainer who CLOSED a follow-up pull request has made a
    decision, and a repeat command must not overrule it by opening a second one:
    the deduplication key has to be able to find the closed PR. Reopening is the
    human's move, and it stays theirs.

    The base is URL-encoded: branch names may legally hold characters that are not
    query-safe, and an unencoded `+` reads as a space, which would return the
    listing for a DIFFERENT base — the dedup search would then find nothing and
    open a duplicate.
    """
    for page in paginate(f"/repos/{repo}/pulls?state=all&base={urllib.parse.quote(base, safe='')}"):
        yield from page


def delete_review_comment(repo: str, comment_id: int) -> None:
    api_json(f"/repos/{repo}/pulls/comments/{comment_id}", method="DELETE")


def patch_review_comment(repo: str, comment_id: int, body: str) -> None:
    api_json(f"/repos/{repo}/pulls/comments/{comment_id}", method="PATCH", payload={"body": body})
