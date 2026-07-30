"""Tests for author_trust.py — the gate's permission decision.

The property that matters: trusted is granted ONLY for write-or-above, and
every failure mode resolves to untrusted. Network is never touched.
"""

import urllib.error

import pytest

import author_trust

REPO = "aws-powertools/powertools-lambda-python"


@pytest.fixture
def api(monkeypatch):
    """Stub api_json with a recorded response (or a raised exception)."""

    def install(response):
        calls = []

        def fake_api_json(path, **kwargs):
            calls.append(path)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(author_trust, "api_json", fake_api_json)
        return calls

    return install


class TestAuthorPermission:
    @pytest.mark.parametrize("permission", ["admin", "write"])
    def test_write_or_above_is_trusted(self, api, permission):
        api({"permission": permission})
        assert author_trust.is_trusted(REPO, "someone") is True

    @pytest.mark.parametrize("permission", ["read", "none", "triage"])
    def test_below_write_is_untrusted(self, api, permission):
        # `read` is the one that matters: a read-only outside collaborator
        # reports author_association=COLLABORATOR, which the old skip list
        # treated as trusted. They cannot push, so they are not trusted.
        api({"permission": permission})
        assert author_trust.is_trusted(REPO, "readonly-collaborator") is False

    def test_maintain_reports_as_write_and_is_trusted(self, api):
        # GitHub collapses `maintain` onto `write` in this field; two of this
        # repo's maintainers hold exactly that role.
        api({"permission": "write"})
        assert author_trust.is_trusted(REPO, "maintainer") is True

    def test_queries_the_permission_endpoint_for_the_author(self, api):
        calls = api({"permission": "write"})
        author_trust.is_trusted(REPO, "octocat")
        assert calls == [f"/repos/{REPO}/collaborators/octocat/permission"]


class TestFailsClosed:
    def test_http_error_is_untrusted(self, api):
        # 404 is the ordinary answer for a fork author who is not a
        # collaborator at all -- exactly the case the gate must catch.
        api(urllib.error.HTTPError("u", 404, "Not Found", {}, None))
        assert author_trust.is_trusted(REPO, "outsider") is False

    def test_server_error_is_untrusted(self, api):
        api(urllib.error.HTTPError("u", 500, "Server Error", {}, None))
        assert author_trust.is_trusted(REPO, "someone") is False

    def test_network_error_is_untrusted(self, api):
        api(urllib.error.URLError("no route"))
        assert author_trust.is_trusted(REPO, "someone") is False

    def test_unexpected_exception_is_untrusted(self, api):
        # A bare `except HTTPError` would let this one escape and fail the
        # job open if the caller ever wrapped it in a try/except.
        api(ValueError("malformed json"))
        assert author_trust.is_trusted(REPO, "someone") is False

    def test_empty_author_is_untrusted_without_an_api_call(self, api):
        calls = api({"permission": "admin"})
        assert author_trust.is_trusted(REPO, "") is False
        assert calls == []  # never ask about an empty login

    def test_missing_permission_key_is_untrusted(self, api):
        api({})
        assert author_trust.is_trusted(REPO, "someone") is False

    def test_non_string_permission_is_untrusted(self, api):
        api({"permission": {"admin": True}})
        assert author_trust.is_trusted(REPO, "someone") is False

    def test_role_name_is_not_consulted(self, api):
        # A custom org role could be named anything; only the collapsed
        # `permission` field is authoritative about write access.
        api({"permission": "read", "role_name": "ai-review-superuser"})
        assert author_trust.is_trusted(REPO, "someone") is False


class TestMain:
    def _run(self, monkeypatch, tmp_path, permission, author="octocat"):
        output = tmp_path / "gh-output"
        output.write_text("")
        monkeypatch.setattr(author_trust, "api_json", lambda path, **kw: {"permission": permission})
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setenv("PR_AUTHOR", author)
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        author_trust.main()
        return output.read_text()

    def test_writes_trusted_true_for_write_access(self, monkeypatch, tmp_path):
        assert "trusted=true" in self._run(monkeypatch, tmp_path, "write")

    def test_writes_trusted_false_for_read_access(self, monkeypatch, tmp_path):
        assert "trusted=false" in self._run(monkeypatch, tmp_path, "read")

    def test_output_is_lowercase_for_yaml_comparison(self, monkeypatch, tmp_path):
        # The workflow compares against the string 'true'; Python's True would
        # never match and would silently gate everything (fail closed, but
        # uselessly).
        assert self._run(monkeypatch, tmp_path, "admin").strip() == "trusted=true"

    def test_missing_github_output_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(author_trust, "api_json", lambda path, **kw: {"permission": "write"})
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setenv("PR_AUTHOR", "octocat")
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        author_trust.main()  # must not raise
