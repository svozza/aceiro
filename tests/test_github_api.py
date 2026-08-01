"""Tests for github_api.py — the shared REST client.

No sockets: urlopen is stubbed. Covers pagination termination (the short-page
stop condition), and the retry policy (retryable status vs not, GET/PATCH
retried but POST never).
"""

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
        github_api.submit_review("o/r", 7, "top-level body", comments)

        assert len(capture) == 1  # one atomic call, not one per comment
        call = capture[0]
        assert call["path"] == "/repos/o/r/pulls/7/reviews"
        assert call["method"] == "POST"
        assert call["payload"]["body"] == "top-level body"
        assert call["payload"]["comments"] == comments

    def test_event_is_comment(self, capture):
        github_api.submit_review("o/r", 7, "b", [])
        assert capture[0]["payload"]["event"] == "COMMENT"

    def test_review_post_is_not_retried(self, monkeypatch, no_sleep):
        # A retried review POST could double-post the whole batch; POST is
        # excluded from RETRYABLE_METHODS, and this pins that for this path.
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise FakeHTTPError(500)

        monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(FakeHTTPError):
            github_api.submit_review("o/r", 7, "b", [])
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
