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
