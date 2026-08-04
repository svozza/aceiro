"""Tests for github_api.py — the shared REST client.

No sockets: urlopen is stubbed. Covers pagination termination (the short-page
stop condition), and the retry policy (retryable status vs not, GET/PATCH
retried but POST never).
"""

import base64
import io
import urllib.error

import pytest

import github_api
from github_api import api_json, paginate


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after else {}
        super().__init__(url="/x", code=code, msg=f"HTTP {code}", hdrs=headers, fp=None)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)


# ----------------------------------------------------------- paginate ---


class TestPaginate:
    def test_stops_on_short_page(self, monkeypatch):
        # A single page shorter than 100 must terminate the generator.
        calls = []

        def fake_api_json(path, method="GET", payload=None):
            calls.append(path)
            return [{"id": 1}, {"id": 2}]  # 2 < 100 -> stop

        monkeypatch.setattr(github_api, "api_json", fake_api_json)
        pages = list(paginate("/repos/o/r/issues/1/comments?"))
        assert pages == [[{"id": 1}, {"id": 2}]]
        assert len(calls) == 1  # did NOT fetch a second page

    def test_follows_until_short_page(self, monkeypatch):
        full = [{"id": n} for n in range(100)]
        tail = [{"id": 999}]
        responses = [full, tail]
        seen = []

        def fake_api_json(path, method="GET", payload=None):
            seen.append(path)
            return responses.pop(0)

        monkeypatch.setattr(github_api, "api_json", fake_api_json)
        pages = list(paginate("/x?"))
        assert pages == [full, tail]
        assert "page=1" in seen[0] and "page=2" in seen[1]

    def test_exact_multiple_terminates_on_empty(self, monkeypatch):
        # A full page followed by an empty page: the empty page (0 < 100) stops it.
        responses = [[{"id": n} for n in range(100)], []]
        monkeypatch.setattr(github_api, "api_json", lambda *a, **k: responses.pop(0))
        pages = list(paginate("/x?"))
        assert pages[-1] == []
        assert len(pages) == 2


# -------------------------------------------------------- api_request ---


class TestApiRequest:
    def test_success_returns_body(self, monkeypatch):
        monkeypatch.setattr(
            github_api.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b'{"ok": true}')
        )
        assert api_json("/x") == {"ok": True}

    def test_get_retries_on_500_then_succeeds(self, monkeypatch, no_sleep):
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise FakeHTTPError(500)
            return FakeResponse(b'{"ok": true}')

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        assert api_json("/x") == {"ok": True}
        assert len(attempts) == 3

    def test_get_gives_up_after_max_attempts(self, monkeypatch, no_sleep):
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise FakeHTTPError(503)

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(FakeHTTPError):
            api_json("/x")
        assert len(attempts) == github_api.MAX_ATTEMPTS

    def test_non_retryable_status_raises_immediately(self, monkeypatch, no_sleep):
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise FakeHTTPError(404)

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(FakeHTTPError):
            api_json("/x")
        assert len(attempts) == 1  # 404 is not retryable

    def test_post_is_never_retried(self, monkeypatch, no_sleep):
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise FakeHTTPError(500)

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(FakeHTTPError):
            github_api.api_request("/x", method="POST", payload={"a": 1})
        assert len(attempts) == 1  # POST is unsafe to repeat

    def test_retry_after_header_used_for_delay(self, monkeypatch):
        delays = []
        monkeypatch.setattr(github_api.time, "sleep", lambda d: delays.append(d))
        responses = [FakeHTTPError(429, retry_after="5"), FakeResponse(b"{}")]

        def fake_urlopen(request, timeout=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        api_json("/x")
        assert delays == [5]  # honoured Retry-After, not the backoff default


# ------------------------------------------------------------------ reviews ---


@pytest.fixture
def capture(monkeypatch):
    """Record api_json calls made by the reviews helpers."""
    calls = []

    def fake_api_json(path, method="GET", payload=None):
        calls.append({"path": path, "method": method, "payload": payload})
        return {}

    monkeypatch.setattr(github_api, "api_json", fake_api_json)
    return calls


class TestSubmitReview:
    def test_posts_one_review_with_every_comment(self, capture):
        comments = [
            {"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"},
            {"path": "b.py", "line": 2, "side": "RIGHT", "body": "y"},
        ]
        github_api.submit_review("o/r", 7, "top-level body", comments, head_sha="reviewed-sha")

        assert len(capture) == 1  # one atomic call, not one per comment
        call = capture[0]
        assert call["path"] == "/repos/o/r/pulls/7/reviews"
        assert call["method"] == "POST"
        assert call["payload"]["body"] == "top-level body"
        assert call["payload"]["comments"] == comments

    def test_event_is_comment(self, capture):
        github_api.submit_review("o/r", 7, "b", [], head_sha="reviewed-sha")
        assert capture[0]["payload"]["event"] == "COMMENT"

    def test_the_review_is_bound_to_the_reviewed_head_sha(self, capture):
        # Omitting commit_id makes GitHub default to "the most recent commit in
        # the pull request", so a review verified against head A lands on head B
        # if the contributor pushed while this call was in flight — and an
        # A-derived suggestion then sits on B's version of the same line.
        github_api.submit_review("o/r", 7, "b", [], head_sha="reviewed-sha")
        assert capture[0]["payload"]["commit_id"] == "reviewed-sha"

    def test_the_head_sha_cannot_be_omitted(self):
        # Keyword-only with no default: the binding is the security property, so
        # a caller must state which SHA it verified rather than inherit GitHub's
        # "most recent commit" default by saying nothing.
        with pytest.raises(TypeError):
            github_api.submit_review("o/r", 7, "b", [])

    def test_an_empty_head_sha_is_refused_before_the_request(self, capture):
        # "" would be sent as commit_id and reach the same default, so it fails
        # closed here rather than posting an unbound review.
        with pytest.raises(ValueError, match="head SHA"):
            github_api.submit_review("o/r", 7, "b", [], head_sha="")
        assert capture == []

    def test_review_post_is_not_retried(self, monkeypatch, no_sleep):
        # A retried review POST could double-post the whole batch; POST is
        # excluded from RETRYABLE_METHODS, and this pins that for this path.
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise FakeHTTPError(500)

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(FakeHTTPError):
            github_api.submit_review("o/r", 7, "b", [], head_sha="reviewed-sha")
        assert len(attempts) == 1


class TestReviewCommentMutations:
    def test_review_comments_flattens_pages(self, monkeypatch):
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        monkeypatch.setattr(github_api, "paginate", lambda path: iter(pages))
        assert [c["id"] for c in github_api.review_comments("o/r", 7)] == [1, 2, 3]

    def test_review_comments_asks_for_the_pulls_endpoint(self, monkeypatch):
        # Inline review comments live under /pulls/{n}/comments; the issue
        # comments endpoint (the sticky comment's home) would list the wrong set.
        seen = []
        monkeypatch.setattr(github_api, "paginate", lambda path: seen.append(path) or iter([[]]))
        list(github_api.review_comments("o/r", 7))
        assert seen == ["/repos/o/r/pulls/7/comments?"]

    def test_delete_targets_the_review_comment_endpoint(self, capture):
        github_api.delete_review_comment("o/r", 42)
        assert capture[0] == {"path": "/repos/o/r/pulls/comments/42", "method": "DELETE", "payload": None}

    def test_patch_sends_the_new_body(self, capture):
        github_api.patch_review_comment("o/r", 42, "edited")
        assert capture[0]["path"] == "/repos/o/r/pulls/comments/42"
        assert capture[0]["method"] == "PATCH"
        assert capture[0]["payload"] == {"body": "edited"}


class TestReviewMutations:
    """The review-level helpers, used to supersede spent wrappers."""

    def test_pull_reviews_flattens_pages(self, monkeypatch):
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        monkeypatch.setattr(github_api, "paginate", lambda path: iter(pages))
        assert [r["id"] for r in github_api.pull_reviews("o/r", 7)] == [1, 2, 3]

    def test_pull_reviews_asks_for_the_reviews_endpoint(self, monkeypatch):
        # /pulls/{n}/reviews lists review OBJECTS; /pulls/{n}/comments lists the
        # inline comments inside them. Confusing the two would hand
        # supersede_previous_reviews comments to rewrite.
        seen = []
        monkeypatch.setattr(github_api, "paginate", lambda path: seen.append(path) or iter([[]]))
        list(github_api.pull_reviews("o/r", 7))
        assert seen == ["/repos/o/r/pulls/7/reviews?"]

    def test_update_review_body_puts_to_the_review(self, capture):
        github_api.update_review_body("o/r", 7, 99, "superseded")
        assert capture[0]["path"] == "/repos/o/r/pulls/7/reviews/99"
        assert capture[0]["method"] == "PUT"
        assert capture[0]["payload"] == {"body": "superseded"}

    def test_minimize_sends_the_node_id_and_the_outdated_classifier(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            github_api, "api_request",
            lambda path, method="GET", payload=None: sent.update(path=path, method=method, payload=payload)
            or b'{"data": {"minimizeComment": {"minimizedComment": {"isMinimized": true}}}}',
        )
        github_api.minimize_review("PRR_abc")
        assert sent["path"] == "/graphql"
        assert sent["method"] == "POST"
        assert sent["payload"]["variables"] == {"subjectId": "PRR_abc"}
        # OUTDATED is the claim we can substantiate — a later run superseded it.
        # SPAM/ABUSE/RESOLVED would each assert something the harness cannot know.
        assert "OUTDATED" in sent["payload"]["query"]
        assert "minimizeComment" in sent["payload"]["query"]

    def test_a_graphql_error_raises_even_though_the_http_status_is_200(self, monkeypatch):
        """GraphQL reports a denied mutation as HTTP 200 with an `errors` array.

        Without this the caller would treat a permission failure as success. The
        failure has to reach the fail-soft handler in the executor — which
        decides to continue — rather than being lost here, where nothing would
        report it.
        """
        monkeypatch.setattr(
            github_api, "api_request",
            lambda path, method="GET", payload=None: b'{"errors": [{"message": "Resource not accessible"}]}',
        )
        with pytest.raises(RuntimeError, match="Resource not accessible"):
            github_api.minimize_review("PRR_abc")

    def test_a_successful_graphql_call_returns_its_data(self, monkeypatch):
        monkeypatch.setattr(
            github_api, "api_request",
            lambda path, method="GET", payload=None: b'{"data": {"ok": true}}',
        )
        assert github_api.graphql("query {}", {}) == {"ok": True}


@pytest.fixture
def replies(monkeypatch):
    """Record api_json calls and answer each from a queue.

    The reviews helpers ignore their responses, so `capture` can return `{}`. The
    tree-write sequence threads each call's returned SHA into the next, so the
    stub has to answer with real-shaped payloads or the chain cannot be asserted.
    """
    calls = []
    queue = []

    def fake_api_json(path, method="GET", payload=None):
        calls.append({"path": path, "method": method, "payload": payload})
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(github_api, "api_json", fake_api_json)
    return calls, queue


class TestTheTreeWriteSequence:
    """blobs -> tree -> commit -> ref, the sequence the stacked follow-up PR
    commits through (ADR-0009 addendum).

    Chosen over PUT /repos/{repo}/contents/{path} for a containment reason rather
    than a stylistic one. The contents API is one COMMIT per file — up to
    max_patched_files of them — each immediately visible on a branch that must
    already exist, and POST is never retried, so a failure midway leaves a real
    branch holding half a fix and no pull request. Blobs, trees and commits are
    unreferenced objects: nothing is visible until the final POST /git/refs, and
    every partial failure before it leaves only garbage GitHub collects.
    """

    def test_a_blob_is_created_base64_encoded(self, replies):
        # base64, never "utf-8": the content is contributor file bytes with a
        # replacement spliced in, and a contributor file is not guaranteed to be
        # valid UTF-8. Declaring utf-8 would fail or silently corrupt such a file.
        calls, queue = replies
        queue.append({"sha": "blob-sha"})
        sha = github_api.create_blob("o/r", b"\xff\xfe not utf-8 at all\n")

        assert sha == "blob-sha"
        assert calls[0]["path"] == "/repos/o/r/git/blobs"
        assert calls[0]["method"] == "POST"
        assert calls[0]["payload"]["encoding"] == "base64"
        assert base64.b64decode(calls[0]["payload"]["content"]) == b"\xff\xfe not utf-8 at all\n"

    def test_a_tree_is_based_on_the_reviewed_tree_and_carries_file_modes(self, replies):
        calls, queue = replies
        queue.append({"sha": "tree-sha"})
        sha = github_api.create_tree("o/r", "base-tree-sha", {"src/a.py": "blob-a"})

        assert sha == "tree-sha"
        assert calls[0]["path"] == "/repos/o/r/git/trees"
        assert calls[0]["method"] == "POST"
        assert calls[0]["payload"]["base_tree"] == "base-tree-sha"
        assert calls[0]["payload"]["tree"] == [
            {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "blob-a"}
        ]

    def test_the_tree_is_based_so_unpatched_files_survive(self, replies):
        # base_tree is what makes this a PATCH of the reviewed tree rather than a
        # replacement of it. Without it the commit's tree holds ONLY the patched
        # files and every other file in the repository reads as deleted -- a
        # verified two-line fix delivered as "delete the repository".
        calls, queue = replies
        queue.append({"sha": "tree-sha"})
        github_api.create_tree("o/r", "base-tree-sha", {"src/a.py": "blob-a"})
        assert calls[0]["payload"]["base_tree"] == "base-tree-sha"

    def test_a_commit_names_the_reviewed_head_as_its_only_parent(self, replies):
        # The branch is cut from the reviewed head (ADR-0005: `old` byte-matches
        # the file THERE, so the patch applies cleanly on that tree and nowhere
        # else). A different parent would be a tree the anchor never described.
        calls, queue = replies
        queue.append({"sha": "commit-sha"})
        sha = github_api.create_commit("o/r", "the message", tree="tree-sha",
                                      parent="reviewed-head-sha")

        assert sha == "commit-sha"
        assert calls[0]["path"] == "/repos/o/r/git/commits"
        assert calls[0]["method"] == "POST"
        assert calls[0]["payload"]["tree"] == "tree-sha"
        assert calls[0]["payload"]["parents"] == ["reviewed-head-sha"]
        assert calls[0]["payload"]["message"] == "the message"

    def test_a_ref_is_created_fully_qualified(self, replies):
        # The API wants refs/heads/NAME; sending the bare branch name creates a
        # ref literally called "smtithy/fix-x" outside refs/heads, which no pull
        # request can open from.
        calls, queue = replies
        queue.append({"ref": "refs/heads/smtithy/fix-x"})
        github_api.create_ref("o/r", "smtithy/fix-x", "commit-sha")

        assert calls[0]["path"] == "/repos/o/r/git/refs"
        assert calls[0]["method"] == "POST"
        assert calls[0]["payload"] == {"ref": "refs/heads/smtithy/fix-x", "sha": "commit-sha"}

    def test_none_of_the_writes_are_retried(self, monkeypatch, no_sleep):
        # Every call in this sequence is a POST, and a retried one has an
        # uncertain outcome: a create that actually succeeded behind a 500 would
        # be repeated, and for the ref that means a second branch. RETRYABLE_METHODS
        # excludes POST; this pins it for each helper here rather than trusting the
        # constant to stay put.
        for call in (
            lambda: github_api.create_blob("o/r", b"x"),
            lambda: github_api.create_tree("o/r", "t", {"a": "b"}),
            lambda: github_api.create_commit("o/r", "m", tree="t", parent="p"),
            lambda: github_api.create_ref("o/r", "smtithy/x", "c"),
        ):
            attempts = []

            def fake_urlopen(request, timeout=None):
                attempts.append(1)
                raise FakeHTTPError(500)

            monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
            with pytest.raises(FakeHTTPError):
                call()
            assert len(attempts) == 1

    def test_open_pull_request_sends_the_base_it_is_given(self, replies):
        # `base` is a PARAMETER with no default. ADR-0009's addendum: the base is
        # never model-suppliable, and an executor "defaulting to main" would have
        # implemented the absurd reading -- merge the bug, then merge the fix, with
        # the default branch knowingly broken in between.
        calls, queue = replies
        queue.append({"number": 99, "html_url": "https://github.com/o/r/pull/99"})
        pr = github_api.open_pull_request("o/r", head="smtithy/fix-x", base="feature/x",
                                          title="Fix the thing", body="the body")

        assert pr["number"] == 99
        assert calls[0]["path"] == "/repos/o/r/pulls"
        assert calls[0]["method"] == "POST"
        assert calls[0]["payload"] == {
            "head": "smtithy/fix-x", "base": "feature/x",
            "title": "Fix the thing", "body": "the body",
        }

    def test_open_pull_request_has_no_default_base(self):
        # Keyword-only and required, so no caller can omit it and get a guess.
        with pytest.raises(TypeError):
            github_api.open_pull_request("o/r", head="smtithy/fix-x",
                                         title="t", body="b")

    def test_pull_requests_for_base_lists_all_states(self, monkeypatch):
        # state=all so a maintainer who CLOSED a follow-up PR is not overruled by
        # a repeat command: the dedup key must still find it, and reopening stays
        # the human's move. Asserted as its own membership rather than by comparing
        # the whole path -- a full-string compare also passes when `state` changes
        # to something else and the rest of the URL happens to match, which is a
        # test that reads as enforcement while enforcing the base alone.
        seen = []
        monkeypatch.setattr(github_api, "paginate", lambda path: seen.append(path) or iter([[]]))
        list(github_api.pull_requests_for_base("o/r", "feature/x"))
        assert "state=all" in seen[0], f"the dedup search must span closed PRs; got {seen[0]!r}"
        assert seen[0].startswith("/repos/o/r/pulls?")

    def test_pull_requests_for_base_flattens_pages(self, monkeypatch):
        pages = [[{"number": 1}, {"number": 2}], [{"number": 3}]]
        monkeypatch.setattr(github_api, "paginate", lambda path: iter(pages))
        assert [p["number"] for p in github_api.pull_requests_for_base("o/r", "f")] == [1, 2, 3]

    def test_a_base_branch_name_is_url_encoded(self, monkeypatch):
        # Branch names may legally contain characters that are not query-safe
        # (`feature/a+b`, say). Unencoded, `+` reads as a space and the listing
        # comes back for a DIFFERENT base -- so the dedup search would find
        # nothing and open a duplicate PR.
        seen = []
        monkeypatch.setattr(github_api, "paginate", lambda path: seen.append(path) or iter([[]]))
        list(github_api.pull_requests_for_base("o/r", "feature/a+b"))
        assert "feature%2Fa%2Bb" in seen[0]


class TestPrMoved:
    """The TOCTOU predicate both executors share. `base.sha` is live — it
    tracks the base branch's tip and moves forward with it — so a SHA
    comparison would refuse to post on any repository with merge traffic,
    while the artifact it is protecting was anchored to the event base
    precisely so an advance could not invalidate it."""

    def pr(self, head="reviewed-sha", base_ref="main", base_sha="base-tip"):
        return {"head": {"sha": head}, "base": {"ref": base_ref, "sha": base_sha}}

    def test_an_unmoved_pr_has_moved_nothing(self):
        assert github_api.pr_moved(self.pr(), "reviewed-sha", "main") is None

    def test_a_pushed_head_has_moved(self):
        moved = github_api.pr_moved(self.pr(head="pushed-sha"), "reviewed-sha", "main")
        assert moved and "head moved" in moved

    def test_a_retargeted_base_has_moved(self):
        moved = github_api.pr_moved(self.pr(base_ref="release/2"), "reviewed-sha", "main")
        assert moved and "retargeted" in moved

    def test_a_base_branch_advance_has_not_moved(self):
        # An unrelated PR merged to main while this run sat at the approval
        # gate, so base.sha is a commit the review never saw. The diff was
        # computed as EVENT_BASE...HEAD and is still exactly correct.
        pr = self.pr(base_sha="a-commit-that-landed-during-the-gate")
        assert github_api.pr_moved(pr, "reviewed-sha", "main") is None
