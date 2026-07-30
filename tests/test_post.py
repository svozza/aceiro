"""Tests for post.py — the trusted executor holding the write token.

Covers the three units with no prior coverage: the render template, the
marker+author sticky-comment upsert (hijack guard), and main()'s re-verify /
TOCTOU-SHA gates. Network is never touched: api_json/paginate are stubbed.
"""

import json
import sys

import pytest

import post
from conftest import CHANGED_FILES, POLICY, SAMPLE_DIFF
from verify import verify

SEVERITIES = POLICY["artifact_schema"]["findings"]["item_fields"]["severity"]["values"]
SEVERITY_ORDER = {name: rank for rank, name in enumerate(SEVERITIES)}

METADATA = {
    "model": "global.anthropic.claude-opus-4-8",
    "prompt": "abc123def456",
    "policy": "0011223344ff",
    "sha": "deadbeef",
    "run_url": "https://github.com/o/r/actions/runs/1",
}


def finding(severity="high", title="t", path="tests/unit/test_logger.py", line=1, body="b"):
    return {"severity": severity, "title": title, "path": path, "line": line, "body": body}


# --------------------------------------------------------------- render ---


class TestRender:
    def test_marker_is_first_line(self, valid_artifact):
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert body.splitlines()[0] == post.MARKER

    def test_default_marker_and_title_are_the_incumbents(self, valid_artifact):
        # Several generators share this executor, so the defaults are what keeps
        # the incumbent's call site unchanged: it passes neither and must post
        # exactly the comment it always did.
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert body.splitlines()[0] == "<!-- ai-pr-review-sticky-comment -->"
        assert body.splitlines()[1] == "## 🤖 AI Code Review"

    def test_marker_and_title_are_overridable(self, valid_artifact):
        # Comment identity is per-GENERATOR while this code is shared. Without an
        # override two reviewers upsert the SAME comment and overwrite each other's
        # review -- the reader sees one, alternating, and never learns why.
        body = post.render(
            valid_artifact,
            METADATA,
            SEVERITY_ORDER,
            "<!-- ai-pr-review:handrolled -->",
            "Hand-rolled Bedrock AI review",
        )
        assert body.splitlines()[0] == "<!-- ai-pr-review:handrolled -->"
        assert body.splitlines()[1] == "## Hand-rolled Bedrock AI review"
        # The incumbent's marker must NOT appear, or its comment gets hijacked.
        assert "ai-pr-review-sticky-comment" not in body

    def test_two_generators_render_distinct_markers(self, valid_artifact):
        # The property that actually matters, stated directly: whatever the values,
        # two generators must not collide on the marker upsert_comment searches for.
        first = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        second = post.render(
            valid_artifact, METADATA, SEVERITY_ORDER, "<!-- ai-pr-review:handrolled -->", "Other",
        )
        assert first.splitlines()[0] != second.splitlines()[0]

    def test_summary_inserted_verbatim(self, valid_artifact):
        valid_artifact["summary"] = "A very specific summary sentence."
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "A very specific summary sentence." in body

    def test_findings_sorted_most_severe_first(self, valid_artifact):
        valid_artifact["findings"] = [
            finding(severity="low", title="LOW-ONE"),
            finding(severity="critical", title="CRIT-ONE"),
            finding(severity="medium", title="MED-ONE"),
        ]
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert body.index("CRIT-ONE") < body.index("MED-ONE") < body.index("LOW-ONE")

    def test_empty_findings_placeholder(self, valid_artifact):
        valid_artifact["findings"] = []
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "No confirmed defects found." in body

    def test_residual_risk_omitted_when_empty(self, valid_artifact):
        valid_artifact["residual_risk"] = ""
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "### Residual risk" not in body

    def test_residual_risk_present_when_set(self, valid_artifact):
        valid_artifact["residual_risk"] = "Could not confirm thread-safety."
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "### Residual risk" in body
        assert "Could not confirm thread-safety." in body

    def test_metadata_footer_rendered(self, valid_artifact):
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "global.anthropic.claude-opus-4-8" in body
        assert "deadbeef" in body
        assert METADATA["run_url"] in body

    def test_rendered_body_of_verified_artifact_has_no_injection(self, valid_artifact):
        # A verified artifact rendered through the template must not introduce
        # anything outside the safe grammar (mentions/raw HTML/off-site links).
        verify(valid_artifact, SAMPLE_DIFF, CHANGED_FILES, POLICY)
        body = post.render(valid_artifact, METADATA, SEVERITY_ORDER)
        assert "<script" not in body
        assert "@" not in valid_artifact["summary"]  # sanity: golden is clean


# -------------------------------------------------------- upsert_comment ---


@pytest.fixture
def capture_api(monkeypatch):
    """Record api_json calls and drive paginate from a canned page list."""
    calls = []

    def fake_api_json(path, method="GET", payload=None):
        calls.append({"path": path, "method": method, "payload": payload})
        return {}

    def make_paginate(pages):
        def fake_paginate(path_with_query):
            yield from pages
        return fake_paginate

    monkeypatch.setattr(post, "api_json", fake_api_json)
    return calls, lambda pages: monkeypatch.setattr(post, "paginate", make_paginate(pages))


def comment(cid, body, login=post.BOT_LOGIN):
    return {"id": cid, "body": body, "user": {"login": login}}


class TestUpsertComment:
    def test_updates_existing_bot_comment(self, capture_api):
        calls, set_pages = capture_api
        set_pages([[comment(42, f"old {post.MARKER}")]])
        post.upsert_comment("o/r", 1, "new body")
        assert len(calls) == 1
        assert calls[0]["method"] == "PATCH"
        assert "/comments/42" in calls[0]["path"]
        assert calls[0]["payload"] == {"body": "new body"}

    def test_marker_with_wrong_author_is_ignored(self, capture_api):
        # Hijack guard: anyone can paste the marker; only the bot's comment is ours.
        calls, set_pages = capture_api
        set_pages([[comment(99, f"forged {post.MARKER}", login="attacker")]])
        post.upsert_comment("o/r", 1, "new body")
        assert calls[0]["method"] == "POST"
        assert "/issues/1/comments" in calls[0]["path"]

    def test_no_marker_creates_new(self, capture_api):
        calls, set_pages = capture_api
        set_pages([[comment(7, "unrelated comment")]])
        post.upsert_comment("o/r", 1, "new body")
        assert calls[0]["method"] == "POST"

    def test_finds_bot_comment_across_pages(self, capture_api):
        calls, set_pages = capture_api
        page1 = [comment(1, "chatter"), comment(2, f"forged {post.MARKER}", login="someone")]
        page2 = [comment(3, f"ours {post.MARKER}")]
        set_pages([page1, page2])
        post.upsert_comment("o/r", 1, "new body")
        assert calls[0]["method"] == "PATCH"
        assert "/comments/3" in calls[0]["path"]

    HANDROLLED = "<!-- ai-pr-review:handrolled -->"

    def test_another_generators_comment_is_not_hijacked(self, capture_api):
        # THE reason the marker is a parameter. Two reviewers run on every PR; each
        # must find only its own comment. Searching with the default marker while a
        # sibling's comment is present would PATCH the sibling's review away.
        calls, set_pages = capture_api
        set_pages([[comment(42, f"the incumbent's review {post.MARKER}")]])
        post.upsert_comment("o/r", 1, "new body", self.HANDROLLED)
        assert calls[0]["method"] == "POST", "hijacked another generator's comment instead of creating its own"

    def test_each_generator_updates_its_own_comment(self, capture_api):
        # Both comments present, as on any real PR after the first run: each
        # generator must PATCH its own id and leave the other alone.
        calls, set_pages = capture_api
        both = [comment(10, f"incumbent {post.MARKER}"), comment(11, f"handrolled {self.HANDROLLED}")]

        set_pages([list(both)])
        post.upsert_comment("o/r", 1, "x")
        assert "/comments/10" in calls[0]["path"]

        calls.clear()
        set_pages([list(both)])
        post.upsert_comment("o/r", 1, "y", self.HANDROLLED)
        assert "/comments/11" in calls[0]["path"]


# ----------------------------------------------------------------- main ---


@pytest.fixture
def artifact_dir(tmp_path, valid_artifact):
    (tmp_path / "review.json").write_text(json.dumps(valid_artifact))
    (tmp_path / "diff.patch").write_text(SAMPLE_DIFF)
    (tmp_path / "changed_files.json").write_text(json.dumps(CHANGED_FILES))
    return tmp_path


@pytest.fixture
def main_env(tmp_path, monkeypatch, artifact_dir):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("system prompt")

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("HEAD_SHA", "reviewed-sha")
    monkeypatch.setenv("BASE_SHA", "reviewed-base")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/1")
    monkeypatch.setenv("BEDROCK_INFERENCE_PROFILE", "global.anthropic.claude-opus-4-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["post.py", "--artifact-dir", str(artifact_dir), "--policy", str(policy_path), "--prompt", str(prompt_path)],
    )
    return artifact_dir, policy_path


def stub_pr_shas(monkeypatch, *responses):
    """Stub the PR fetches main() does for its TOCTOU checks: one (head, base)
    pair per expected call, the last pair reused if more calls arrive."""
    remaining = list(responses)

    def fake_api_json(*args, **kwargs):
        head, base = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return {"head": {"sha": head}, "base": {"sha": base}}

    monkeypatch.setattr(post, "api_json", fake_api_json)


UNMOVED = ("reviewed-sha", "reviewed-base")


class TestMain:
    def test_happy_path_posts_rendered_body(self, main_env, monkeypatch, valid_artifact):
        stub_pr_shas(monkeypatch, UNMOVED)
        posted = {}
        monkeypatch.setattr(
            post,
            "upsert_comment",
            lambda repo, pr, body, marker=None: posted.update(repo=repo, pr=pr, body=body, marker=marker),
        )

        post.main()

        assert posted["repo"] == "o/r"
        assert posted["pr"] == 1
        # Bytes posted == render() of the artifact that was verified.
        assert posted["body"].splitlines()[0] == post.MARKER
        assert valid_artifact["summary"] in posted["body"]

    def test_main_passes_its_marker_to_both_render_and_upsert(self, main_env, monkeypatch, valid_artifact):
        # The two must agree or the reviewer searches for a comment it never writes
        # and creates a new one every run. Asserted through main() because that is
        # the seam: render and upsert can each honour the marker while main forgets
        # to hand it to one of them, which no unit test of either would catch.
        stub_pr_shas(monkeypatch, UNMOVED)
        # main_env has already installed the base argv; append the two flags to it.
        monkeypatch.setattr(sys, "argv", [*sys.argv, "--marker", "<!-- m -->", "--title", "T"])
        posted = {}
        monkeypatch.setattr(
            post,
            "upsert_comment",
            lambda repo, pr, body, marker=None: posted.update(body=body, marker=marker),
        )

        post.main()

        assert posted["marker"] == "<!-- m -->", "main() did not pass the marker to upsert_comment"
        assert posted["body"].splitlines()[0] == "<!-- m -->"
        assert posted["body"].splitlines()[1] == "## T"

    def test_rejected_artifact_posts_nothing(self, main_env, monkeypatch, artifact_dir):
        # Corrupt the artifact so re-verification fails in the post job.
        bad = json.loads((artifact_dir / "review.json").read_text())
        bad["auto_merge"] = True  # extra key -> schema rejection
        (artifact_dir / "review.json").write_text(json.dumps(bad))
        stub_pr_shas(monkeypatch, UNMOVED)
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_head_moved_posts_nothing(self, main_env, monkeypatch):
        # TOCTOU guard: head advanced since the review ran.
        stub_pr_shas(monkeypatch, ("different-sha", "reviewed-base"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_base_retargeted_posts_nothing(self, main_env, monkeypatch):
        # A base retarget (head unchanged) changes the diff the review
        # describes just as surely as a push; it must also block posting.
        stub_pr_shas(monkeypatch, ("reviewed-sha", "different-base"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_head_moved_during_post_withdraws_comment(self, main_env, monkeypatch):
        # The pre-check and the write are not atomic: a push landing between
        # them is caught by the post-write recheck, which overwrites the
        # just-posted review with a stale notice and fails the job.
        stub_pr_shas(monkeypatch, UNMOVED, ("pushed-sha", "reviewed-base"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda repo, pr, body, marker=None: posted.append(body))

        with pytest.raises(SystemExit):
            post.main()

        assert len(posted) == 2  # the review, then its withdrawal
        assert "withdrawn" in posted[1]
        assert posted[1].splitlines()[0] == post.MARKER  # upsert finds and replaces it

    def test_base_retarget_during_post_withdraws_comment(self, main_env, monkeypatch):
        stub_pr_shas(monkeypatch, UNMOVED, ("reviewed-sha", "retargeted-base"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda repo, pr, body, marker=None: posted.append(body))

        with pytest.raises(SystemExit):
            post.main()

        assert len(posted) == 2
        assert "withdrawn" in posted[1]

    def test_stale_notice_passes_the_markdown_policy(self):
        # The withdrawal body is static, but it must never be the one piece
        # of posted text outside the safe grammar.
        from verify import check_markdown_field

        check_markdown_field(post.STALE_NOTICE, POLICY["markdown"], "stale_notice")
