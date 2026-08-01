"""Tests for post.py — the trusted executor holding the write token.

Covers the render template, the marker+author sticky-comment upsert (hijack
guard), the runtime resolution of the bot's own login (and its fail-closed
path), and main()'s re-verify / TOCTOU-SHA gates. Network is never touched:
api_json/paginate are stubbed.
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


# ------------------------------------------------------ resolve_bot_login ---


class TestResolveBotLogin:
    def stub_graphql(self, monkeypatch, response, calls=None):
        def fake_api_json(path, method="GET", payload=None):
            if calls is not None:
                calls.append({"path": path, "method": method, "payload": payload})
            return response

        monkeypatch.setattr(post, "api_json", fake_api_json)

    def test_resolves_viewer_login_via_graphql(self, monkeypatch):
        # The one identity call every token type answers: REST /user is 403
        # "Resource not accessible by integration" for installation tokens,
        # which is what Actions' GITHUB_TOKEN is.
        calls = []
        self.stub_graphql(monkeypatch, {"data": {"viewer": {"login": "github-actions[bot]"}}}, calls)
        assert post.resolve_bot_login() == "github-actions[bot]"
        assert calls == [
            {"path": "/graphql", "method": "POST", "payload": {"query": post.VIEWER_LOGIN_QUERY}},
        ]

    def test_app_bot_login_is_used_verbatim(self, monkeypatch):
        # viewer.login already carries the [bot] suffix for app tokens --
        # byte-identical to comment user.login, so no mapping may sit between.
        self.stub_graphql(monkeypatch, {"data": {"viewer": {"login": "my-review-app[bot]"}}})
        assert post.resolve_bot_login() == "my-review-app[bot]"

    @pytest.mark.parametrize(
        "response",
        [
            {"errors": [{"message": "Resource not accessible by integration"}]},
            {"data": {"viewer": None}},
            {"data": {"viewer": {"login": ""}}},
            {"data": {"viewer": {"login": None}}},
            {"data": None},
            {},
            [],
        ],
        ids=["graphql-error", "null-viewer", "empty-login", "null-login", "null-data", "empty", "non-dict"],
    )
    def test_fails_closed_on_anything_but_a_login(self, monkeypatch, response):
        # Guessing an identity here means the executor may edit comments it
        # does not own; every malformed shape must exit, not default.
        self.stub_graphql(monkeypatch, response)
        with pytest.raises(SystemExit):
            post.resolve_bot_login()


# -------------------------------------------------------- upsert_comment ---

BOT_LOGIN = "github-actions[bot]"


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


def comment(cid, body, login=BOT_LOGIN):
    return {"id": cid, "body": body, "user": {"login": login}}


class TestUpsertComment:
    def test_updates_existing_bot_comment(self, capture_api):
        calls, set_pages = capture_api
        set_pages([[comment(42, f"old {post.MARKER}")]])
        post.upsert_comment("o/r", 1, "new body", bot_login=BOT_LOGIN)
        assert len(calls) == 1
        assert calls[0]["method"] == "PATCH"
        assert "/comments/42" in calls[0]["path"]
        assert calls[0]["payload"] == {"body": "new body"}

    def test_marker_with_wrong_author_is_ignored(self, capture_api):
        # Hijack guard: anyone can paste the marker; only the bot's comment is ours.
        calls, set_pages = capture_api
        set_pages([[comment(99, f"forged {post.MARKER}", login="attacker")]])
        post.upsert_comment("o/r", 1, "new body", bot_login=BOT_LOGIN)
        assert calls[0]["method"] == "POST"
        assert "/issues/1/comments" in calls[0]["path"]

    def test_resolved_identity_scopes_ownership(self, capture_api):
        # The consumer swapped GITHUB_TOKEN for an app token: the incumbent's
        # old github-actions[bot] comment carries the right marker but is no
        # longer OURS to edit. A hardcoded login would have hijacked it.
        calls, set_pages = capture_api
        set_pages([[comment(42, f"old review {post.MARKER}", login="github-actions[bot]")]])
        post.upsert_comment("o/r", 1, "new body", bot_login="my-review-app[bot]")
        assert calls[0]["method"] == "POST", "edited a comment authored by a different identity"

    def test_no_marker_creates_new(self, capture_api):
        calls, set_pages = capture_api
        set_pages([[comment(7, "unrelated comment")]])
        post.upsert_comment("o/r", 1, "new body", bot_login=BOT_LOGIN)
        assert calls[0]["method"] == "POST"

    def test_finds_bot_comment_across_pages(self, capture_api):
        calls, set_pages = capture_api
        page1 = [comment(1, "chatter"), comment(2, f"forged {post.MARKER}", login="someone")]
        page2 = [comment(3, f"ours {post.MARKER}")]
        set_pages([page1, page2])
        post.upsert_comment("o/r", 1, "new body", bot_login=BOT_LOGIN)
        assert calls[0]["method"] == "PATCH"
        assert "/comments/3" in calls[0]["path"]

    HANDROLLED = "<!-- ai-pr-review:handrolled -->"

    def test_another_generators_comment_is_not_hijacked(self, capture_api):
        # THE reason the marker is a parameter. Two reviewers run on every PR; each
        # must find only its own comment. Searching with the default marker while a
        # sibling's comment is present would PATCH the sibling's review away.
        calls, set_pages = capture_api
        set_pages([[comment(42, f"the incumbent's review {post.MARKER}")]])
        post.upsert_comment("o/r", 1, "new body", self.HANDROLLED, bot_login=BOT_LOGIN)
        assert calls[0]["method"] == "POST", "hijacked another generator's comment instead of creating its own"

    def test_each_generator_updates_its_own_comment(self, capture_api):
        # Both comments present, as on any real PR after the first run: each
        # generator must PATCH its own id and leave the other alone.
        calls, set_pages = capture_api
        both = [comment(10, f"incumbent {post.MARKER}"), comment(11, f"handrolled {self.HANDROLLED}")]

        set_pages([list(both)])
        post.upsert_comment("o/r", 1, "x", bot_login=BOT_LOGIN)
        assert "/comments/10" in calls[0]["path"]

        calls.clear()
        set_pages([list(both)])
        post.upsert_comment("o/r", 1, "y", self.HANDROLLED, bot_login=BOT_LOGIN)
        assert "/comments/11" in calls[0]["path"]


# ----------------------------------------------------------------- main ---


@pytest.fixture
def artifact_dir(tmp_path, valid_artifact):
    (tmp_path / "review.json").write_text(json.dumps(valid_artifact))
    (tmp_path / "diff.patch").write_text(SAMPLE_DIFF)
    (tmp_path / "changed_files.json").write_text(json.dumps(CHANGED_FILES))
    (tmp_path / "run_metadata.json").write_text(json.dumps({"model": "claude-sonnet-4-5"}))
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
    monkeypatch.setenv("BASE_REF", "main")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["post.py", "--artifact-dir", str(artifact_dir), "--policy", str(policy_path), "--prompt", str(prompt_path)],
    )
    # The executor fetches its own provenance inputs; the network stands in for
    # a PR whose real changes are conftest's sample diff. Tests about the fetch
    # itself override this.
    monkeypatch.setattr(
        post, "fetch_anchored_pair", lambda repo, base, head: (SAMPLE_DIFF.encode(), list(CHANGED_FILES)),
    )
    return artifact_dir, policy_path


def stub_pr_shas(monkeypatch, *responses):
    """Stub the API calls main() makes before posting: the identity resolution
    (any /graphql call answers with the bot's viewer login) and the PR fetches
    for the TOCTOU checks — one (head sha, base ref) pair per expected call, the
    last pair reused if more calls arrive. base.sha is present but never equal to
    BASE_SHA: it is live, so a check reading it would be reading the wrong thing.
    """
    remaining = list(responses)

    def fake_api_json(path, method="GET", payload=None):
        if path == "/graphql":
            return {"data": {"viewer": {"login": BOT_LOGIN}}}
        head, base_ref = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return {"head": {"sha": head}, "base": {"ref": base_ref, "sha": "live-base-tip"}}

    monkeypatch.setattr(post, "api_json", fake_api_json)


UNMOVED = ("reviewed-sha", "main")


def stub_comment_store(monkeypatch, *responses, after_write=None):
    """Like stub_pr_shas, but backed by a mutable comment list so the REAL
    upsert_comment and withdraw_own_review run against it.

    Returns the store, so a test can assert what the PR ends up showing rather
    than what a stubbed upsert was handed. `after_write` is called with the store
    once this run's review has landed — the window a concurrent run's write
    occupies, which is the only place one can interleave.
    """
    store: list[dict] = []
    remaining = list(responses)
    writes = {"n": 0}

    def note_write():
        writes["n"] += 1
        if writes["n"] == 1 and after_write is not None:
            after_write(store)

    def fake_api_json(path, method="GET", payload=None):
        if path == "/graphql":
            return {"data": {"viewer": {"login": BOT_LOGIN}}}
        if method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            next(c for c in store if c["id"] == cid)["body"] = payload["body"]
            note_write()
            return {}
        if method == "POST":
            store.append(comment(len(store) + 1, payload["body"]))
            note_write()
            return {}
        head, base_ref = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return {"head": {"sha": head}, "base": {"ref": base_ref, "sha": "live-base-tip"}}

    monkeypatch.setattr(post, "api_json", fake_api_json)
    monkeypatch.setattr(post, "paginate", lambda _p: iter([list(store)]))
    return store


class TestMain:
    def test_happy_path_posts_rendered_body(self, main_env, monkeypatch, valid_artifact):
        stub_pr_shas(monkeypatch, UNMOVED)
        posted = {}
        monkeypatch.setattr(
            post,
            "upsert_comment",
            lambda repo, pr, body, marker=None, bot_login=None: posted.update(
                repo=repo, pr=pr, body=body, marker=marker, bot_login=bot_login,
            ),
        )

        post.main()

        assert posted["repo"] == "o/r"
        assert posted["pr"] == 1
        # Bytes posted == render() of the artifact that was verified.
        assert posted["body"].splitlines()[0] == post.MARKER
        assert valid_artifact["summary"] in posted["body"]
        # The ownership check runs under the identity the token resolved to,
        # not a configured one.
        assert posted["bot_login"] == BOT_LOGIN

    def test_unresolvable_identity_posts_nothing(self, main_env, monkeypatch):
        # Fail-closed: if the token's own login cannot be resolved, the
        # executor must not guess -- guessing decides which comments it may
        # edit. Verification and the TOCTOU pre-check pass; identity fails.
        def fake_api_json(path, method="GET", payload=None):
            if path == "/graphql":
                return {"errors": [{"message": "something upstream broke"}]}
            return {"head": {"sha": "reviewed-sha"}, "base": {"ref": "main", "sha": "live-base-tip"}}

        monkeypatch.setattr(post, "api_json", fake_api_json)
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

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
            lambda repo, pr, body, marker=None, bot_login=None: posted.update(body=body, marker=marker),
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
        stub_pr_shas(monkeypatch, ("different-sha", "main"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_base_retargeted_posts_nothing(self, main_env, monkeypatch):
        # A base retarget (head unchanged) changes the diff the review
        # describes just as surely as a push; it must also block posting.
        stub_pr_shas(monkeypatch, ("reviewed-sha", "release/2"))
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_a_base_branch_advance_still_posts(self, main_env, monkeypatch):
        # The reviewed diff was computed as EVENT_BASE...HEAD, so an unrelated
        # merge into the base branch while the run sat at the approval gate
        # leaves it exactly correct. base.sha is live and moves with that merge
        # (probed: a 2016 vscode PR reports a 2026 base.sha), so a SHA
        # comparison would post nothing for most PRs on a busy repository.
        stub_comment_store(monkeypatch, UNMOVED)
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a) or 1)

        post.main()

        assert len(posted) == 1, "a benign base-branch advance must not block the review"

    def test_head_moved_during_post_withdraws_comment(self, main_env, monkeypatch):
        # The pre-check and the write are not atomic: a push landing between
        # them is caught by the post-write recheck, which overwrites the
        # just-posted review with a stale notice and fails the job.
        store = stub_comment_store(monkeypatch, UNMOVED, ("pushed-sha", "main"))

        with pytest.raises(SystemExit):
            post.main()

        assert len(store) == 1, "the review was posted, then withdrawn in place"
        assert "withdrawn" in store[0]["body"]
        assert store[0]["body"].splitlines()[0] == post.MARKER

    def test_base_retarget_during_post_withdraws_comment(self, main_env, monkeypatch):
        store = stub_comment_store(monkeypatch, UNMOVED, ("reviewed-sha", "release/2"))

        with pytest.raises(SystemExit):
            post.main()

        assert "withdrawn" in store[0]["body"]

    def test_a_withdrawal_will_not_clobber_a_newer_revisions_review(self, main_env, monkeypatch):
        # Runs for the same PR share the marker and the bot login. Between this
        # run's write and its recheck, the run for the new head replaces the
        # sticky comment with ITS review -- withdrawing that would leave the PR
        # showing a withdrawal for a review that was never stale, and no further
        # event exists to correct it.
        newer = (
            f"{post.MARKER}\nthe pushed-sha run's review\n"
            f"<sub>{post.sha_stamp('pushed-sha')} · [run](u)</sub>"
        )

        def concurrent_run_wins(store):
            store[0]["body"] = newer

        store = stub_comment_store(
            monkeypatch, UNMOVED, ("pushed-sha", "main"),
            after_write=concurrent_run_wins,
        )

        with pytest.raises(SystemExit):
            post.main()

        assert "withdrawn" not in store[0]["body"]
        assert "the pushed-sha run's review" in store[0]["body"]

    def test_provenance_is_checked_against_the_fetched_diff_not_the_bundles(
        self, main_env, monkeypatch, artifact_dir, valid_artifact
    ):
        # post.py's docstring says it trusts nothing from the review job. The
        # diff and the changed-file list are two of verify()'s three inputs, so
        # a bundle claiming a file the PR never touched must not make a finding
        # on that file provenant.
        forged_path = "src/never_touched.py"
        artifact = json.loads(json.dumps(valid_artifact))
        artifact["findings"] = [finding(path=forged_path, line=1)]
        (artifact_dir / "review.json").write_text(json.dumps(artifact))
        (artifact_dir / "diff.patch").write_text(
            f"diff --git a/{forged_path} b/{forged_path}\n"
            f"--- a/{forged_path}\n+++ b/{forged_path}\n@@ -1,1 +1,1 @@\n+forged\n"
        )
        (artifact_dir / "changed_files.json").write_text(json.dumps([forged_path]))

        stub_comment_store(monkeypatch, UNMOVED)
        # The first-party fetch reports what the PR really changed.
        monkeypatch.setattr(
            post, "fetch_anchored_pair",
            lambda repo, base, head: (SAMPLE_DIFF.encode(), list(CHANGED_FILES)),
        )
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == [], "posted a finding on a file the PR never changed"

    def test_the_fetched_pair_is_anchored_to_the_reviewed_shas(self, main_env, monkeypatch):
        # The executor must ask about the SAME comparison the review described;
        # anchoring its own fetch anywhere else re-verifies a different diff.
        asked = {}

        def fake_fetch(repo, base, head):
            asked.update(repo=repo, base=base, head=head)
            return SAMPLE_DIFF.encode(), list(CHANGED_FILES)

        stub_comment_store(monkeypatch, UNMOVED)
        monkeypatch.setattr(post, "fetch_anchored_pair", fake_fetch)
        post.main()

        assert asked == {"repo": "o/r", "base": "reviewed-base", "head": "reviewed-sha"}

    def test_the_footer_carries_the_stamp_the_withdrawal_searches_for(self, valid_artifact):
        # The two must not drift: a rendered footer the withdrawal cannot
        # recognise makes every withdrawal a silent no-op.
        metadata = {"model": "m", "prompt": "p", "policy": "c", "sha": "abc123",
                    "run_url": "https://example.invalid/run"}
        body = post.render(valid_artifact, metadata, {"medium": 0, "high": 1, "low": 2, "critical": 3})
        assert post.sha_stamp("abc123") in body

    def test_stale_notice_passes_the_markdown_policy(self):
        # The withdrawal body is static, but it must never be the one piece
        # of posted text outside the safe grammar.
        from verify import check_markdown_field

        check_markdown_field(post.STALE_NOTICE, POLICY["markdown"], "stale_notice")


class TestModelStamp:
    """The footer's model is the audit trail for "which model said this". Both
    model arms are configured on every run — a Bedrock profile and a CLI model,
    each with a default — so only the arm that ran can name what answered."""

    def test_the_stamp_is_the_model_the_generator_recorded(self, main_env, monkeypatch, artifact_dir):
        stub_comment_store(monkeypatch, UNMOVED)
        (artifact_dir / "run_metadata.json").write_text(json.dumps({"model": "claude-sonnet-4-5"}))
        posted = {}
        monkeypatch.setattr(
            post, "upsert_comment",
            lambda repo, pr, body, marker=None, bot_login=None: posted.update(body=body),
        )

        post.main()

        assert "model: `claude-sonnet-4-5`" in posted["body"]

    def test_an_unattributable_artifact_posts_nothing(self, main_env, monkeypatch, artifact_dir):
        # Fail-closed rather than stamping a placeholder: a footer naming a
        # model that never ran is a false audit trail, which is worse than none.
        (artifact_dir / "run_metadata.json").unlink()
        stub_comment_store(monkeypatch, UNMOVED)
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []

    def test_a_metadata_file_naming_no_model_posts_nothing(self, main_env, monkeypatch, artifact_dir):
        (artifact_dir / "run_metadata.json").write_text(json.dumps({"model": None}))
        stub_comment_store(monkeypatch, UNMOVED)
        posted = []
        monkeypatch.setattr(post, "upsert_comment", lambda *a, **k: posted.append(a))

        with pytest.raises(SystemExit):
            post.main()
        assert posted == []
