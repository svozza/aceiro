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

REVIEW = {
    "summary": "`load` gained a check its callers do not expect.",
    "findings": [
        {"path": "src/app.py", "line": 2, "severity": "high",
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
        pfc, "fetch_reviewed_artifact",
        lambda repo, number, sha, output_dir: state["review"],
    )
    monkeypatch.setattr(
        pfc, "fetch_anchored_pair", lambda repo, base, head: state["fetched"],
    )
    state["output"] = tmp_path / "context"
    return state


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
        assert index == {"index": 0}

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
        with pytest.raises(pfc.Refused, match="2 but the posted review"):
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
        def gone(repo, number, sha, output_dir):
            raise pfc.Refused("the review run's artifact is no longer available")

        monkeypatch.setattr(pfc, "fetch_reviewed_artifact", gone)
        with pytest.raises(pfc.Refused, match="no longer available"):
            run(lane)


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
