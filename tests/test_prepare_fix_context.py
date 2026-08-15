"""Tests for the remediation lane's context preparation.

The `/fix N` counterpart to prepare_context: it resolves the pull request from an
issue number, refuses on every precondition the command channel adds, and composes
the directory the plan session reads. Network is stubbed throughout.

The preconditions are the point of the module, and each is a refusal that must
happen BEFORE the plan session (and therefore before a model credential) is
reached:

    the comment is a command            fix_command.parse_fix_command
    the commenter holds write           author_trust.is_trusted (ADR-0007)
    the issue is a pull request         an issue_comment fires on both
    the head has not moved              drift = refuse (ADR-0007)
    a review was posted for that head   post.posted_review_witness
    the ordinal names one of its findings
"""

import json

import pytest

import prepare_fix_context as pfc
from conftest import POLICY
from test_plan_verify import PLAN_CHANGED_FILES, PLAN_DIFF

# Captured before any fixture replaces the module attribute, so the tests that are
# ABOUT the fetch can put the real one back.
pfc_real_fetch = pfc.fetch_reviewed_artifact

REVIEW = {
    "summary": "`load` gained a check its callers do not expect.",
    "findings": [
        {"path": "src/app.py", "line": 2, "severity": "high", "group": 1,
         "title": "load() breaks callers", "body": "the body"},
    ],
    "residual_risk": "",
}


def pr_payload(head_sha="reviewed-sha", base_ref="main", base_sha="event-base"):
    return {
        "number": 7,
        "title": "add a check",
        "body": "",
        "head": {"sha": head_sha, "ref": "feature/x",
                 "repo": {"full_name": "o/r"}},
        "base": {"ref": base_ref, "sha": base_sha, "repo": {"full_name": "o/r"}},
    }


@pytest.fixture
def lane(monkeypatch, tmp_path):
    """Every collaborator stubbed to the happy path; each test breaks one."""
    state = {
        "pr": pr_payload(),
        # The /issues/N view: `pull_request` present is what marks it as one.
        "issue": {"number": 7, "pull_request": {"url": "https://api/pulls/7"}},
        "trusted": True,
        "witness": 4242,
        # The run whose footer the witness read, and so the run whose artifact the
        # commanded finding must come from.
        "posting_run": 5001,
        "review": dict(REVIEW),
        # A REAL anchored pair: the review is verified here, so a placeholder diff
        # would refuse on provenance and every happy-path case would pass for the
        # wrong reason.
        "fetched": (PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)),
    }

    def fake_api_json(path, **kw):
        # Two endpoints, deliberately distinguished: /issues/N is what an
        # issue_comment resolves against and is where the "is this a pull request?"
        # marker lives, while /pulls/N is what carries the head and base. A stub
        # answering both with one payload would let the probe pass on a shape the
        # real issues endpoint never returns.
        if "/issues/" in path:
            return state["issue"]
        return state["pr"]

    monkeypatch.setattr(pfc, "api_json", fake_api_json)
    monkeypatch.setattr(pfc, "is_trusted", lambda repo, login: state["trusted"])
    monkeypatch.setattr(pfc, "resolve_bot_login", lambda: "smtithy[bot]")
    monkeypatch.setattr(
        pfc, "posted_review_witness",
        lambda repo, number, sha, bot_login: state["witness"],
    )
    monkeypatch.setattr(
        pfc, "posting_run_id",
        lambda repo, number, sha, bot_login: state["posting_run"],
    )

    def fake_fetch(repo, number, sha, output_dir, *, run_id):
        # run_id is asserted rather than ignored: the artifact this lane returns is
        # the posting run's, so a caller that stopped passing it would be caught
        # here rather than silently reading whatever this stub holds.
        assert run_id == state["posting_run"]
        return state["review"]

    monkeypatch.setattr(pfc, "fetch_reviewed_artifact", fake_fetch)
    monkeypatch.setattr(
        pfc, "fetch_anchored_pair", lambda repo, base, head: state["fetched"],
    )
    state["output"] = tmp_path / "context"
    return state


def real_artifact_fetch(lane, monkeypatch, artifacts):
    """Put the REAL fetch_reviewed_artifact back and serve it `artifacts`.

    The lane stubs the fetch wholesale, which is right for the tests that are
    about the other preconditions and wrong for the ones about the fetch itself:
    a stub raising what it was told to raise proves nothing about the listing,
    the expiry filter or the run binding.
    """
    monkeypatch.setattr(pfc, "fetch_reviewed_artifact", pfc_real_fetch)
    inner = pfc.api_json

    def fake(path, **kw):
        if "/actions/artifacts?" in path:
            return {"artifacts": artifacts}
        return inner(path, **kw)

    monkeypatch.setattr(pfc, "api_json", fake)


def zipped_review(review):
    """An Actions artifact zip, as download_review will really unzip it."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr("review.json", json.dumps(review))
    return buf.getvalue()


def run(lane, body="/fix 1"):
    return pfc.prepare(
        repo="o/r", issue_number=7, comment_body=body,
        commenter="maintainer", output_dir=lane["output"],
        policy=POLICY,
    )


class TestTheHappyPath:
    def test_it_composes_the_bundle_the_plan_session_reads(self, lane):
        run(lane)
        written = {p.name for p in lane["output"].iterdir()}
        assert written == {
            "pr.json", "diff.patch", "changed_files.json",
            "review.json", "commanded_index.json",
        }

    def test_the_ordinal_written_is_the_zero_based_index(self, lane):
        run(lane)
        index = json.loads((lane["output"] / "commanded_index.json").read_text())
        assert index == {"indices": [0]}

    def test_the_review_written_is_the_one_fetched(self, lane):
        run(lane)
        assert json.loads((lane["output"] / "review.json").read_text()) == REVIEW

    def test_the_anchors_are_the_reviewed_shas(self, lane):
        # The plan must be anchored to the same comparison the review was, or the
        # anchor and the review describe different content. The head is the PR's
        # current head — checked unmoved above — and the base is the PR's base at
        # that moment, both written where prepare_context writes them.
        run(lane)
        pr = json.loads((lane["output"] / "pr.json").read_text())
        assert pr["head_sha"] == "reviewed-sha"
        assert pr["base_sha"] == "event-base"


class TestThePreconditions:
    def test_a_body_that_is_not_a_command_does_nothing(self, lane):
        # Not a refusal: there is nothing to report to someone who issued no
        # command, and every ordinary comment would otherwise be noise.
        assert run(lane, body="looks good to me") is None
        assert not lane["output"].exists()

    def test_an_untrusted_commenter_is_refused(self, lane):
        lane["trusted"] = False
        with pytest.raises(pfc.Refused, match="write"):
            run(lane)
        assert not lane["output"].exists()

    def test_trust_is_resolved_on_the_comment_author(self, lane, monkeypatch):
        # ADR-0007's central point: the COMMENT author, never the PR author. The
        # valuable case is a maintainer commanding a fix on a first-time
        # contributor's PR, which requiring author trust would forbid.
        seen = []
        monkeypatch.setattr(pfc, "is_trusted",
                            lambda repo, login: seen.append(login) or True)
        run(lane)
        assert seen == ["maintainer"]

    def test_an_issue_that_is_no_pull_request_is_refused(self, lane):
        # issue_comment fires for issues as well as pull requests, so the
        # resolution can legitimately find something with no head to review.
        lane["issue"] = {"number": 7}
        with pytest.raises(pfc.Refused, match="pull request"):
            run(lane)

    def test_a_head_with_no_posted_review_is_refused(self, lane):
        # The witness: the commander acts on a comment they read, so a command for
        # a head this harness never reviewed is refused rather than reviewed now.
        lane["witness"] = None
        with pytest.raises(pfc.Refused, match="no posted review"):
            run(lane)
        assert not lane["output"].exists()

    def test_an_ordinal_past_the_reviews_findings_is_refused(self, lane):
        # Refused HERE as well as at both gates: this is the one place a commander
        # sees why, and composing a context whose ordinal addresses nothing would
        # spend a model call to fail closed later.
        with pytest.raises(pfc.Refused, match=r"\[2\] but the posted review"):
            run(lane, body="/fix 2")
        assert not lane["output"].exists()

    def test_a_review_the_verifier_rejects_is_refused(self, lane):
        # The artifact is the trust anchor for the commanded finding, so it is
        # verified where it is fetched too — the plan session and the executor
        # both re-verify, and this is the refusal that reaches the commander.
        lane["review"] = {**REVIEW, "findings": [
            {**REVIEW["findings"][0], "path": "src/never_changed.py"},
        ]}
        with pytest.raises(pfc.Refused, match="not a changed file"):
            run(lane)

    def test_a_missing_review_artifact_is_refused(self, lane, monkeypatch):
        # Actions artifacts expire (90 days), so this is a real state rather than
        # a theoretical one: the review is gone and the command cannot be honoured.
        # The refusal is driven through the REAL fetch_reviewed_artifact against an
        # empty listing, not a stub that raises what it was told to raise — the
        # listing, the expiry filter and the run binding are the property here.
        real_artifact_fetch(lane, monkeypatch, [])
        with pytest.raises(pfc.Refused, match="no unexpired artifact"):
            run(lane)

    def test_an_expired_artifact_is_no_artifact(self, lane, monkeypatch):
        # Expiry is what makes "absent" reachable, so it is filtered rather than
        # trusted to be absent from the listing.
        real_artifact_fetch(lane, monkeypatch, [
            {"id": 100, "expired": True, "workflow_run": {"id": lane["posting_run"]}},
        ])
        with pytest.raises(pfc.Refused, match="no unexpired artifact"):
            run(lane)

    def test_the_command_is_refused_when_no_run_link_binds_the_review(self, lane, monkeypatch):
        # posting_run_id returns None for a comment whose footer carries no run
        # link. Without a run there is nothing to bind the artifact to, so the
        # command is refused rather than falling back to name-and-recency.
        monkeypatch.setattr(pfc, "posting_run_id", lambda *a, **k: None)
        with pytest.raises(pfc.Refused, match="carries no run link"):
            run(lane)


class TestTheCommandNamesASet:
    """ADR-0013: `/fix N,M` names several findings, and the composed context carries
    every ordinal — canonically ordered, so the file is a function of the SET.
    """

    TWO_FINDINGS = [
        REVIEW["findings"][0],
        {"path": "src/util.py", "line": 1, "severity": "low", "group": 1,
         "title": "check() is unreachable", "body": "the other body"},
    ]

    def indices(self, lane):
        return json.loads((lane["output"] / "commanded_index.json").read_text())["indices"]

    def test_every_commanded_ordinal_is_written(self, lane):
        lane["review"] = {**REVIEW, "findings": self.TWO_FINDINGS}
        run(lane, body="/fix 1,2")
        assert self.indices(lane) == [0, 1]

    def test_the_file_is_the_same_whichever_order_was_typed(self, lane, tmp_path):
        # Sorted here rather than only in stack.fix_key, so the two cannot disagree
        # about whether ordering was part of the command. Held one step upstream of
        # the key means a reader of the context sees the canonical form too.
        lane["review"] = {**REVIEW, "findings": self.TWO_FINDINGS}
        run(lane, body="/fix 2,1")
        first = (lane["output"] / "commanded_index.json").read_bytes()
        lane["output"] = tmp_path / "again"
        run(lane, body="/fix 1,2")
        assert (lane["output"] / "commanded_index.json").read_bytes() == first

    def test_one_ordinal_past_the_end_refuses_the_whole_command(self, lane):
        # NOT the subset that resolves: the commander asserted these findings take
        # one remediation, so composing a context for half of them would be a scope
        # the harness chose. Refused before a model credential exists, which is the
        # one place a commander reads why.
        lane["review"] = {**REVIEW, "findings": self.TWO_FINDINGS}
        with pytest.raises(pfc.Refused, match=r"\[3\] but the posted review"):
            run(lane, body="/fix 1,3")
        assert not lane["output"].exists()

    def test_the_refusal_names_every_ordinal_that_missed(self, lane):
        # A count alone leaves the commander guessing which of the ordinals they
        # typed was the wrong one.
        lane["review"] = {**REVIEW, "findings": self.TWO_FINDINGS}
        with pytest.raises(pfc.Refused, match=r"\[3, 4\]"):
            run(lane, body="/fix 1,3,4")

    def test_a_repeated_ordinal_is_written_once(self, lane):
        run(lane, body="/fix 1,1")
        assert self.indices(lane) == [0]


class TestTheOrdinalIsResolvedInRenDeredOrder:
    """`/fix 3` is the third finding of the COMMENT the commander read.

    plan_loop.read_commanded_findings and post.render both resolve through
    artifact.rendered_findings; this module read review["findings"] directly, which
    is MODEL order. rendered_findings' own docstring names the hazard: "Resolving
    that ordinal against the artifact's own order names a different finding than the
    commander pointed at whenever the model's order is not already sorted, and
    nothing about that failure is visible: both findings are real."

    Every fixture in this file and all four plan_scenarios reviews are
    severity-order-invariant, which is why nothing caught it — a SORTED fixture
    cannot distinguish the two orders, so these carry model orders that are not
    severity order and would pass under either reading otherwise.

    Nothing requires the model to emit sorted findings: the review prompt states no
    ordering rule, the verifier has no ordering check, and run_evals refuses to state
    eval expectations as ordinals BECAUSE the two orders can differ.
    """

    def finding(self, path, line, severity, group, title):
        return {"path": path, "line": line, "severity": severity, "group": group,
                "title": title, "body": "the body"}

    def fork(self, lane):
        lane["pr"] = {**pr_payload(),
                      "head": {"sha": "reviewed-sha", "ref": "feature/x",
                               "repo": {"full_name": "contributor/r"}}}
        return lane

    # Model order [high app.py, low util.py, medium app.py], which RENDERS as
    # [high app.py, medium app.py, low util.py]. So `/fix 1,2` names two findings in
    # ONE file — one contiguous suggestion, legal on a fork.
    ONE_FILE_WHEN_RENDERED = "one_file_when_rendered"
    # Model order [low app.py, high app.py, medium util.py], rendering as
    # [high app.py, medium util.py, low app.py]. So `/fix 1,2` genuinely spans two
    # paths and has no delivery on a fork.
    TWO_FILES_WHEN_RENDERED = "two_files_when_rendered"

    def review_for(self, which):
        if which == self.ONE_FILE_WHEN_RENDERED:
            findings = [
                self.finding("src/app.py", 2, "high", 1, "load() breaks callers"),
                self.finding("src/util.py", 1, "low", 2, "check() is unreachable"),
                self.finding("src/app.py", 3, "medium", 1, "and the caller here"),
            ]
        else:
            findings = [
                self.finding("src/app.py", 2, "low", 1, "load() breaks callers"),
                self.finding("src/app.py", 3, "high", 2, "the caller here"),
                self.finding("src/util.py", 1, "medium", 2, "check() is unreachable"),
            ]
        return {**REVIEW, "findings": findings}

    def test_a_deliverable_command_on_a_fork_is_not_falsely_declined(self, lane):
        """Direction (a): the decline fires on a command that HAS a delivery.

        The gate failing closed wrongly, and worse than that — the harness posts a
        `pull-requests: write` comment asserting the command names files it does not
        name, which is the over-claiming artefact ADR-0009's addendum B is about.

        This is the case test_two_findings_sharing_a_file_on_a_fork_are_honoured
        exists to protect, with the one thing that fixture lacks: a model order that
        is not already severity order.
        """
        lane["review"] = self.review_for(self.ONE_FILE_WHEN_RENDERED)
        self.fork(lane)
        assert run(lane, body="/fix 1,2")["indices"] == [0, 1], (
            "the rendered ordinals 1,2 are two findings in src/app.py — one contiguous "
            "suggestion a fork can carry — and the command was declined, because the paths "
            "were resolved in model order"
        )

    def test_the_reachable_false_decline_is_the_harness_own_suggested_command(self, lane):
        """The aggravating fact: the false decline is reachable by OBEYING the harness.

        post.group_cross_reference composes a `/fix N,M` in rendered ordinals and
        tells the commander to type it verbatim. Under the model-order read, 864 of
        9472 harness-authored cross-references are declined on a fork with a false
        claim about what they name. So the round trip the harness advertises is
        pinned here: its own composed command, fed back through prepare().
        """
        import re

        from artifact import rendered_findings, severity_ranks
        from post import group_cross_reference

        review = self.review_for(self.ONE_FILE_WHEN_RENDERED)
        rendered = rendered_findings(review, severity_ranks(POLICY))
        reference = group_cross_reference(rendered, 0)
        assert reference is not None, "the fixture renders no cross-reference, so it pins no round trip"
        # Read out of the rendered prose rather than recomposed, so this is the
        # string a commander can actually copy out of the posted comment.
        composed = re.search(r"`(/fix [^`]+)`", reference)
        assert composed, f"the cross-reference composes no /fix command: {reference}"
        command = composed.group(1)

        lane["review"] = review
        self.fork(lane)
        # Every ordinal in the group, so the whole command the harness told the
        # commander to type must be honoured — not just the two-ordinal prefix.
        assert run(lane, body=command)["indices"] == sorted(
            position for position, finding in enumerate(rendered)
            if finding["group"] == rendered[0]["group"]
        ), (
            f"the harness composed {command!r} and prepare() declined it; following the "
            "cross-reference's own instruction gets the command refused with a false claim "
            "about which files it names"
        )

    def test_a_command_with_no_delivery_still_declines(self, lane):
        """Direction (b): the fast decline does not fire on a command that genuinely
        has no delivery, which is the entire cost ADR-0014 was written to avoid.

        Under the model-order read prepare() sees only {src/app.py} and composes the
        context — so the commander spends the approval gate, a model session, both
        gates and a `contents: write` job to get a red run in a log they must
        click into.
        """
        lane["review"] = self.review_for(self.TWO_FILES_WHEN_RENDERED)
        self.fork(lane)
        with pytest.raises(pfc.Undeliverable) as exc:
            run(lane, body="/fix 1,2")
        # The rendered pair is [high src/app.py, medium src/util.py]; model order 0,1
        # is two findings in src/app.py and would compose a context.
        assert "src/util.py" in str(exc.value), (
            "the decline names the wrong files, so the reason is resolved in a different "
            "order than the paths it decided on"
        )

    def test_the_range_check_counts_the_findings_and_not_their_order(self, lane):
        # The order-invariant half, kept honest: rendered_findings is a permutation,
        # so the past-the-end refusal must be unchanged by the switch. This read fed
        # ONLY this check before the decline started reading a finding's content.
        lane["review"] = self.review_for(self.ONE_FILE_WHEN_RENDERED)
        with pytest.raises(pfc.Refused, match=r"\[4\] but the posted review has 3"):
            run(lane, body="/fix 1,4")

    def test_the_review_written_keeps_model_order(self, lane):
        # review.json is the artifact as posted, and every downstream reader
        # re-renders it. Sorting it here would make the file disagree with the
        # artifact the run published, and the ordinals in commanded_index.json are
        # rendered positions resolved against it.
        lane["review"] = self.review_for(self.ONE_FILE_WHEN_RENDERED)
        run(lane, body="/fix 1")
        written = json.loads((lane["output"] / "review.json").read_text())
        assert written == lane["review"]


class TestTheDeclineChannelRepliesToExactlyTwoRefusals:
    """ADR-0014: a decline is a reply the command channel makes, and WHICH refusals
    get one is a security property rather than a preference.

    This module derives ONE of the two: the undeliverable-by-construction case, a
    multi-path command on a fork pull request. `stack` derives the other
    (AlreadyDelivered), because fix_key needs anchor signatures over a quarantine
    tree this job never fetches.

    **The untrusted-commander refusal must NEVER be replied to.** It is reached
    before trust is resolved, so a reply there would let any passer-by make the
    harness post a comment naming them — unauthenticated write amplification, and the
    shape parse_fix_command already refuses when it declines to report malformed
    commands.
    """

    TWO_FILES = [
        REVIEW["findings"][0],
        {"path": "src/util.py", "line": 1, "severity": "low", "group": 1,
         "title": "check() is unreachable", "body": "the other body"},
    ]

    def fork(self, lane):
        """The same pull request, from a fork: head repo differs from base repo."""
        lane["pr"] = {**pr_payload(),
                      "head": {"sha": "reviewed-sha", "ref": "feature/x",
                               "repo": {"full_name": "contributor/r"}}}
        return lane

    def two_file_command(self, lane):
        lane["review"] = {**REVIEW, "findings": self.TWO_FILES}
        return lane

    def test_a_multi_path_command_on_a_fork_is_undeliverable(self, lane):
        # Two commanded findings on DISTINCT paths mean the fix must touch both
        # (check_commanded_scope, ⊆); a review comment carries exactly one `path`, so
        # the only delivery is a stacked pull request; and a fork's head branch does
        # not exist in this repository for one to be based on. No delivery exists.
        self.fork(self.two_file_command(lane))
        with pytest.raises(pfc.Undeliverable, match="fork"):
            run(lane, body="/fix 1,2")

    def test_it_is_refused_before_any_context_is_written(self, lane):
        # The whole point of hoisting it here: the commander would otherwise spend
        # the approval gate, a model session, both gates and a contents: write
        # job to receive a red run in a log they must click into.
        self.fork(self.two_file_command(lane))
        with pytest.raises(pfc.Undeliverable):
            run(lane, body="/fix 1,2")
        assert not lane["output"].exists(), (
            "a context the plan session could read was composed for a command that has no "
            "delivery, so the model session runs anyway"
        )

    def test_a_multi_path_command_on_a_same_repo_pull_request_is_honoured(self, lane):
        # The calibration case, and it must stay legal: this is exactly the command
        # ADR-0013 exists to enable, and it is stacked delivery's first reachable
        # trigger. Declining it would make the decline channel a refusal of the
        # feature it was built alongside.
        self.two_file_command(lane)
        assert run(lane, body="/fix 1,2")["indices"] == [0, 1]

    def test_a_single_path_command_on_a_fork_is_honoured(self, lane):
        # One path is one suggestion comment, which works on a fork — that is why
        # suggestions were built first. Before ADR-0013 this whole path was
        # unreachable, and it must stay reachable now.
        self.fork(lane)
        assert run(lane, body="/fix 1")["indices"] == [0]

    def test_two_findings_sharing_a_file_on_a_fork_are_honoured(self, lane):
        # The distinction is PATHS, not ordinals. Two findings in one file are one
        # contiguous suggestion, which a fork can carry, so a check counting
        # commanded findings rather than their paths would decline a deliverable
        # command.
        lane["review"] = {**REVIEW, "findings": [
            REVIEW["findings"][0],
            {**REVIEW["findings"][0], "line": 4, "title": "also here"},
        ]}
        self.fork(lane)
        assert run(lane, body="/fix 1,2")["indices"] == [0, 1]

    def test_the_reason_names_both_files_and_the_fork(self, lane):
        # The commander has to be able to act on it: which files, and why there is no
        # delivery. Without the paths they cannot tell which half of their command
        # made it undeliverable.
        self.fork(self.two_file_command(lane))
        with pytest.raises(pfc.Undeliverable) as exc:
            run(lane, body="/fix 1,2")
        assert "src/app.py" in str(exc.value) and "src/util.py" in str(exc.value)
        assert "fork" in str(exc.value)

    def test_the_refusal_carries_the_head_and_ordinals_the_comment_must_date_itself_with(
            self, lane):
        # ADR-0009 addendum B's self-dating rule. They travel on the exception because
        # only the raising site knows them, and re-deriving in main() would be a
        # second reader of what the command said.
        self.fork(self.two_file_command(lane))
        with pytest.raises(pfc.Undeliverable) as exc:
            run(lane, body="/fix 2,1")
        assert exc.value.head_sha == "reviewed-sha"
        # 1-BASED and sorted: the comment is addressed to the human who typed them.
        assert exc.value.ordinals == "1,2"

    def test_an_untrusted_commander_is_NEVER_replied_to(self, lane):
        # THE security property. Trust is prepare()'s SECOND step, so everything
        # before it runs for an untrusted commenter — a reply here is one any
        # passer-by can trigger, naming themselves, under the harness's authenticated
        # identity. Asserted on the TYPE, because that is what main() branches on.
        lane["trusted"] = False
        self.fork(self.two_file_command(lane))
        with pytest.raises(pfc.Refused) as exc:
            run(lane, body="/fix 1,2")
        assert not isinstance(exc.value, pfc.Undeliverable), (
            "the untrusted-commander refusal is an Undeliverable, so the harness would post a "
            "comment naming anybody who types a /fix on a fork pull request"
        )

    @pytest.mark.parametrize("break_it,body", [
        ("not_a_pr", "/fix 1,2"),
        ("no_witness", "/fix 1,2"),
        ("no_run_link", "/fix 1,2"),
        ("bad_ordinal", "/fix 1,9"),
    ])
    def test_no_other_refusal_replies(self, lane, break_it, body):
        # Everything else stays a red run. Two named refusals keep the decline
        # DERIVABLE from the command's own shape — a command the channel cannot
        # express gets a reply, a run that failed gets a failed run — rather than
        # needing a hand-maintained exemption list, which is the shape §2's silently
        # unasserted gate-lane list already cost.
        self.fork(self.two_file_command(lane))
        if break_it == "not_a_pr":
            lane["issue"] = {"number": 7}
        elif break_it == "no_witness":
            lane["witness"] = None
        elif break_it == "no_run_link":
            lane["posting_run"] = None
        with pytest.raises(pfc.Refused) as exc:
            run(lane, body=body)
        assert not isinstance(exc.value, pfc.Undeliverable), (
            f"the {break_it!r} refusal replies; only a command the channel cannot express "
            "and one already delivered get a comment"
        )


class TestTheEmittedDecline:
    """main() is what writes the decline job's inputs, so the write is tested apart
    from the derivation — the production defect this lane already had was an
    ABSENT output rather than a wrong value.
    """

    def emit(self, lane, monkeypatch, tmp_path, body="/fix 1,2"):
        lane["review"] = {**REVIEW, "findings": TestTheDeclineChannelRepliesToExactlyTwoRefusals.TWO_FILES}
        lane["pr"] = {**pr_payload(),
                      "head": {"sha": "reviewed-sha", "ref": "feature/x",
                               "repo": {"full_name": "contributor/r"}}}
        output = tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("ISSUE_NUMBER", "7")
        monkeypatch.setenv("COMMENT_BODY", body)
        monkeypatch.setenv("COMMENT_AUTHOR", "maintainer")
        monkeypatch.setattr(
            "sys.argv", ["prepare_fix_context.py", "--output-dir", str(lane["output"])])
        code = pfc.main()
        return code, output.read_text()

    def test_the_run_still_fails(self, lane, monkeypatch, tmp_path):
        # The command was not performed. A green run claiming otherwise would be the
        # artefact whose text over-claims that ADR-0009's addendum B was written
        # about — and the reply is what makes failing cheaply sufficient, not a
        # substitute for it.
        code, _ = self.emit(lane, monkeypatch, tmp_path)
        assert code == 1

    def test_every_output_the_reply_job_reads_is_written(self, lane, monkeypatch, tmp_path):
        import reply

        _, written = self.emit(lane, monkeypatch, tmp_path)
        assert "replied=true" in written
        for name in reply.OUTPUTS:
            assert name in written, f"{name} is absent; the reply job reads it as empty"
        # An undeliverable command was not performed, and the kind is what selects
        # the posted claim (ADR-0018).
        assert written.split("reply_kind<<")[1].splitlines()[1] == "declined"

    def test_the_emitted_reason_is_the_one_raised(self, lane, monkeypatch, tmp_path):
        _, written = self.emit(lane, monkeypatch, tmp_path)
        assert "fork" in written and "src/util.py" in written

    def test_nothing_is_emitted_for_a_refusal_that_does_not_reply(self, lane, monkeypatch,
                                                                  tmp_path):
        # The security property again, through main() rather than the exception type:
        # an untrusted commenter must leave GITHUB_OUTPUT with no decline in it, or
        # the posting job fires.
        lane["trusted"] = False
        code, written = self.emit(lane, monkeypatch, tmp_path)
        assert code == 1
        assert "replied" not in written, (
            "an untrusted commenter's refusal emitted a reply, so the harness posts a comment "
            "naming them"
        )

    def test_an_honoured_command_emits_no_reply(self, lane, monkeypatch, tmp_path):
        # The other direction: the ordinary path must not set the flag, or every
        # successful command would also post a decline. (The delivered receipt is
        # the STACK producer's, at delivery — this job knows only that the command
        # was accepted, not that it was performed.)
        lane["review"] = dict(REVIEW)
        output = tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("ISSUE_NUMBER", "7")
        monkeypatch.setenv("COMMENT_BODY", "/fix 1")
        monkeypatch.setenv("COMMENT_AUTHOR", "maintainer")
        monkeypatch.setattr(
            "sys.argv", ["prepare_fix_context.py", "--output-dir", str(lane["output"])])
        assert pfc.main() == 0
        assert "replied" not in output.read_text()


class TestDrift:
    def test_a_head_that_moved_since_the_review_is_refused(self, lane):
        # ADR-0007: issue_comment carries an issue number and no SHAs, so the head
        # is whatever it is NOW, not what the commander was looking at. The witness
        # is scoped to a SHA, so a moved head has no posted review for it — this
        # asserts the refusal names drift rather than the witness, because the two
        # reasons send a commander to different places.
        lane["pr"] = pr_payload(head_sha="pushed-since")
        lane["witness"] = None
        with pytest.raises(pfc.Refused, match="no posted review"):
            run(lane)

    def test_the_witness_is_asked_about_the_live_head(self, lane, monkeypatch):
        # The SHA the witness is looked up under must be the live head, or a
        # command could be honoured against a head whose review was never posted.
        asked = []
        monkeypatch.setattr(
            pfc, "posted_review_witness",
            lambda repo, number, sha, bot_login: asked.append(sha) or 1,
        )
        lane["pr"] = pr_payload(head_sha="current-head")
        run(lane)
        assert asked == ["current-head"]


class TestTheStepOutputsCarryEveryRefTheGatesNeed:
    """The four values the delivery jobs read from this step's outputs.

    Found in production, on the first `/fix` command that got this far: the step
    emitted only head_sha and base_sha, so BASE_REF and HEAD_REF reached the
    executor as EMPTY STRINGS. Two consequences, and the second is the serious one:

    - the TOCTOU check compared the live base ref against "" and refused every
      command with "base retargeted since review (ai-pr-review != )". A verified,
      fully proved plan was discarded at the last gate.
    - HEAD_REF is what makes the reviewed-head-branch refusal reachable
      (check_write_class_targets: the harness never pushes to the contributor's
      branch, ADR-0009 addendum). Present-but-empty passes `os.environ[...]`, so the
      `head_branch is not None` guard holds and `branch == ""` matches no real
      branch: the check ran, enforced nothing, and reported nothing. That is the
      failure mode this project's own notes call out -- a test or gate that reads as
      enforcement while enforcing nothing.

    So these assert the CONTRACT of prepare()'s return value, which main() writes
    verbatim into GITHUB_OUTPUT.
    """

    def test_prepare_returns_the_base_ref(self, lane):
        # The retarget check's input. ADR-0012: compared by REF, never by SHA.
        assert run(lane)["base_ref"] == "main"

    def test_prepare_returns_the_head_ref(self, lane):
        # The push-target refusal's input, and the reason it is reachable at all.
        assert run(lane)["head_ref"] == "feature/x"

    def test_the_refs_come_from_the_pull_request_not_the_event(self, lane):
        # issue_comment carries no refs at all, which is why they are resolved from
        # the fetched pull request. A different PR must give different refs.
        lane["pr"] = pr_payload(base_ref="release/2")
        result = run(lane)
        assert result["base_ref"] == "release/2"

    def test_every_value_the_delivery_jobs_read_is_present_and_non_empty(self, lane):
        # The whole contract in one assertion, because the defect was an ABSENT key
        # rather than a wrong one: a test naming only the keys it expects would have
        # passed on the broken version for every key it did not think to name.
        result = run(lane)
        for key in ("head_sha", "base_sha", "base_ref", "head_ref"):
            assert key in result, f"{key} missing; the delivery job reads it as an empty string"
            assert result[key], f"{key} is empty, which disables the gate that reads it"


class TestTheEmittedOutputsMatchWhatPrepareReturned:
    """main() writes the outputs, so the write is tested separately from the value.

    The production defect lived HERE, not in prepare(): the values existed and the
    writer only emitted two of them.
    """

    def emit(self, lane, monkeypatch, tmp_path, capsys):
        output = tmp_path / "github_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("ISSUE_NUMBER", "7")
        monkeypatch.setenv("COMMENT_BODY", "/fix 1")
        monkeypatch.setenv("COMMENT_AUTHOR", "maintainer")
        monkeypatch.setattr(
            "sys.argv", ["prepare_fix_context.py", "--output-dir", str(lane["output"])])
        assert pfc.main() == 0
        return dict(
            line.split("=", 1)
            for line in output.read_text().splitlines() if "=" in line
        )

    def test_all_four_refs_are_emitted(self, lane, monkeypatch, tmp_path, capsys):
        emitted = self.emit(lane, monkeypatch, tmp_path, capsys)
        assert emitted["commanded"] == "true"
        assert emitted["head_sha"] == "reviewed-sha"
        assert emitted["base_sha"] == "event-base"
        assert emitted["base_ref"] == "main", "BASE_REF reaches the executor empty; every command refuses"
        assert emitted["head_ref"] == "feature/x", (
            "HEAD_REF reaches the executor empty, so the reviewed-head-branch refusal "
            "matches no branch and silently enforces nothing"
        )

    def test_nothing_is_emitted_empty(self, lane, monkeypatch, tmp_path, capsys):
        # An empty value is worse than a missing one: os.environ[KEY] succeeds, so
        # the executor's fail-closed KeyError never fires.
        emitted = self.emit(lane, monkeypatch, tmp_path, capsys)
        for key, value in emitted.items():
            assert value != "", f"{key} was emitted empty; the reader cannot tell it apart from a real value"

    @pytest.mark.parametrize("ref", ["base_ref", "head_ref"])
    def test_an_empty_ref_is_refused_rather_than_emitted(self, lane, monkeypatch, tmp_path,
                                                         capsys, ref):
        # Scanning the lines the writer PRODUCED cannot reach the guard: it exists
        # to stop an empty value being written, so on the happy path the file is
        # byte-identical whether it is there or not. This drives an empty ref
        # through main() instead, which is the only way the guard runs.
        #
        # Reachable because both refs come from the pull request payload, and a
        # payload field this harness did not choose can be absent or empty.
        monkeypatch.setattr(
            pfc, "prepare",
            lambda **kw: {"head_sha": "reviewed-sha", "base_sha": "event-base",
                          "base_ref": "main", "head_ref": "feature/x",
                          "indices": [0]} | {ref: ""},
        )
        output = tmp_path / "step-output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("ISSUE_NUMBER", "7")
        monkeypatch.setenv("COMMENT_BODY", "/fix 1")
        monkeypatch.setenv("COMMENT_AUTHOR", "maintainer")
        monkeypatch.setattr(
            "sys.argv", ["prepare_fix_context.py", "--output-dir", str(lane["output"])])
        assert pfc.main() == 1, f"an empty {ref} was emitted instead of refused"
        assert "gate" in capsys.readouterr().err
        assert f"{ref}=" not in output.read_text()


class TestTheArtifactIsTheOneThatWasPosted:
    """The artifact the commanded finding is derived from must be the output of the
    run that POSTED the review the commander read.

    The name is derivable from the pull request number and the head SHA, both
    public, and the listing is repository-wide — so name-and-recency alone would let
    any artifact in the repository carrying that name become the trust anchor for
    `/fix N`, deciding which defect is addressed and what text the plan session
    reads inside the `<commanded_finding>` fence.

    Driven through the real fetch_reviewed_artifact and the real download_review,
    with only api_json/api_request standing in for the network, because the listing
    filter and the run binding are the properties under test.
    """

    GENUINE = {"summary": "the posted review", "findings": [], "residual_risk": ""}
    OTHER = {"summary": "another run's review", "findings": [], "residual_risk": ""}

    def fetch(self, monkeypatch, tmp_path, artifacts, zips, run_id):
        monkeypatch.setattr(
            pfc, "api_json", lambda path, **kw: {"artifacts": artifacts})
        import github_api
        monkeypatch.setattr(
            github_api, "api_request",
            lambda path, **kw: zips[int(path.rstrip("/zip").rsplit("/", 1)[-1])])
        return pfc.fetch_reviewed_artifact(
            "o/r", 7, "reviewed-sha", tmp_path, run_id=run_id)

    def test_the_posting_runs_artifact_is_the_one_read(self, monkeypatch, tmp_path):
        review = self.fetch(
            monkeypatch, tmp_path,
            [{"id": 100, "expired": False, "workflow_run": {"id": 5001}}],
            {100: zipped_review(self.GENUINE)},
            run_id=5001,
        )
        assert review["summary"] == "the posted review"

    def test_a_newer_same_named_artifact_from_another_run_is_not_read(
            self, monkeypatch, tmp_path):
        # The forge: a higher artifact id under the same name, from any other run in
        # the repository. max(id) preferred it; the run binding refuses it.
        review = self.fetch(
            monkeypatch, tmp_path,
            [
                {"id": 100, "expired": False, "workflow_run": {"id": 5001}},
                {"id": 200, "expired": False, "workflow_run": {"id": 5002}},
            ],
            {100: zipped_review(self.GENUINE), 200: zipped_review(self.OTHER)},
            run_id=5001,
        )
        assert review["summary"] == "the posted review", (
            "the artifact was chosen by recency, so a same-named artifact from "
            "another run became the trust anchor for the commanded finding"
        )

    def test_an_artifact_carrying_no_run_is_not_read(self, monkeypatch, tmp_path):
        # A listing entry with no workflow_run cannot be bound to the posting run,
        # so it is not a candidate. Fail closed rather than treat absent as matching.
        with pytest.raises(pfc.Refused, match="no unexpired artifact"):
            self.fetch(
                monkeypatch, tmp_path,
                [{"id": 200, "expired": False}],
                {200: zipped_review(self.OTHER)},
                run_id=5001,
            )

    def test_an_unreadable_bundle_is_refused(self, monkeypatch, tmp_path):
        # download_review's own refusal, reached through the real zipfile rather
        # than asserted against a stub.
        with pytest.raises(pfc.Refused, match="no readable review.json"):
            self.fetch(
                monkeypatch, tmp_path,
                [{"id": 100, "expired": False, "workflow_run": {"id": 5001}}],
                {100: b"not a zip at all"},
                run_id=5001,
            )
