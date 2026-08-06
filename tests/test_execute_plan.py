"""Tests for execute_plan.py — the executor's re-verify, decide and deliver.

Network is never touched (api_json is stubbed, and conftest's no_network fixture
holds that claim to account); the prover runs as a stub subprocess script so the
exit-code contract is exercised for real.

Covers decide_delivery's whole case analysis (ADR-0009: the decision is the
executor's, from checkable plan structure), run_prover's three-way exit
contract (0 proved / 1 disproved-with-counterexample / 2 nothing-proved),
the TOCTOU + fork gates on the PR snapshot, main()'s fail-closed ordering
(verify before prove, prove before decide, decide before any fetch), and the
suggestion delivery's wiring — what reaches the reconciler, and what refuses
before anything is posted. The reconciler's own behaviour is test_suggest.py.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

import execute_plan
import stack
from diff_map import anchor_signatures
from execute_plan import Refusal, decide_delivery
from verify import Rejection

from test_plan_verify import PLAN_DIFF, PLAN_CHANGED_FILES, PLAN_TREE, tree_source

POLICY = json.loads(
    (execute_plan._HARNESS_ROOT / "policy.json").read_text()
)
# The shipped label_allowlist is EMPTY (fail-closed: a consumer names the labels
# it accepts), so every label step would reject for that one uninteresting reason
# and the refusal cases below could not reach the delivery decision they exist to
# test. Allowlisting the harness's own label keeps those cases about DELIVERY.
POLICY["plan"]["label_allowlist"] = ["ai-remediation"]


def suggest(step_id="s0", path="src/app.py", line=2):
    return {
        "id": step_id,
        "kind": "suggest",
        "args": {"path": path, "line": line, "old": "def load(path):\n",
                 "new": "def load(path=None):\n", "note": "make path optional"},
    }


def patch(step_id="s0", path="src/app.py", old="def load(path):\n"):
    return {"id": step_id, "kind": "patch",
            "args": {"path": path, "old": old, "new": "def load(path=None):\n"}}


def push(step_id="s8", name="smtithy/fix-x"):
    return {"id": step_id, "kind": "push_branch", "args": {"name": name}}


def open_pr(step_id="s9", branch="smtithy/fix-x"):
    return {"id": step_id, "kind": "open_pr",
            "args": {"branch": branch, "title": "Fix load()", "body": "Fixes the finding."}}


def label(step_id="s7"):
    return {"id": step_id, "kind": "label", "args": {"name": "ai-remediation"}}


# ------------------------------------------------------- decide_delivery ---


class TestDecideDelivery:
    def test_single_file_suggestions_deliver_as_suggestions(self):
        delivery = decide_delivery([suggest("s0"), suggest("s1", line=3)])
        assert delivery.mode == "suggestions"
        assert delivery.path == "src/app.py"

    def test_label_alongside_suggestions_is_fine(self):
        # label is a side effect, not a fix step; it must not confuse the
        # decision in either direction.
        assert decide_delivery([suggest(), label()]).mode == "suggestions"

    def test_patch_chain_delivers_as_stacked_pr(self):
        delivery = decide_delivery([patch(), push(), open_pr()])
        assert delivery.mode == "stacked_pr"
        assert delivery.path is None

    def test_multi_file_patches_still_one_stacked_pr(self):
        steps = [patch("s0"), patch("s1", path="src/util.py", old="def check(path):\n"),
                 push(), open_pr()]
        assert decide_delivery(steps).mode == "stacked_pr"

    def test_no_fix_step_refuses(self):
        # The label-only hole: such a plan verifies today; the executor is
        # where it must fail visibly rather than no-op (chunk D designs the
        # honest decline channel).
        with pytest.raises(Refusal, match="no fix step"):
            decide_delivery([label()])

    def test_write_chain_alone_refuses(self):
        with pytest.raises(Refusal, match="no fix step"):
            decide_delivery([push(), open_pr()])

    def test_mixed_suggest_and_patch_refuses(self):
        # Unreachable for a plan the prompt shaped, refused anyway: "the
        # verifier must have caught it" is not a delivery mechanism.
        with pytest.raises(Refusal, match="mixed"):
            decide_delivery([suggest("s0"), patch("s1"), push(), open_pr()])

    def test_suggestions_spanning_files_refuse(self):
        # ADR-0009's atomicity rule: per-file suggestions of a coordinated
        # fix can be half-applied. A multi-file fix is patch steps or nothing.
        with pytest.raises(Refusal, match="span 2 files"):
            decide_delivery([suggest("s0"), suggest("s1", path="src/util.py")])

    def test_suggestions_with_a_write_chain_refuse(self):
        with pytest.raises(Refusal, match="nothing to push"):
            decide_delivery([suggest(), push(), open_pr()])

    def test_patch_without_push_refuses(self):
        # Verifies (ordering is vacuous with no write step) but has no
        # delivery: nothing would carry the patch anywhere.
        with pytest.raises(Refusal, match="exactly one push_branch and one open_pr"):
            decide_delivery([patch()])

    def test_patch_without_open_pr_refuses(self):
        with pytest.raises(Refusal, match=r"got 1 and 0"):
            decide_delivery([patch(), push()])

    def test_two_write_chains_refuse(self):
        with pytest.raises(Refusal, match=r"got 2 and 2"):
            decide_delivery([patch(), push("s2"), open_pr("s3"), push("s4"), open_pr("s5")])


# ------------------------------------------------------------ run_prover ---


@pytest.fixture
def stub_prover(tmp_path):
    """A node script standing in for prove-cli: exit code and streams driven
    by the test, so run_prover's contract is exercised through a REAL
    subprocess boundary rather than a mocked subprocess.run."""

    def make(exit_code, stdout="", stderr=""):
        script = tmp_path / "prover.js"
        script.write_text(
            f"process.stdout.write({json.dumps(stdout)});\n"
            f"process.stderr.write({json.dumps(stderr)});\n"
            f"process.exitCode = {exit_code};\n"
        )
        return script

    return make


@pytest.fixture
def prover_inputs(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": [suggest()]}))
    changed = tmp_path / "changed_files.json"
    changed.write_text(json.dumps(PLAN_CHANGED_FILES))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY))
    return plan_path, changed, policy_path


class TestRunProver:
    def test_exit_zero_passes_and_echoes_verdicts(self, stub_prover, prover_inputs, capsys):
        prover = stub_prover(0, stdout="ordering: holds (1.0ms)\n")
        execute_plan.run_prover(prover, *prover_inputs, head_branch="feature/x")
        assert "ordering: holds" in capsys.readouterr().out

    def test_exit_one_fails_with_the_counterexample(self, stub_prover, prover_inputs, capsys):
        # Exit 1 is an audit record: the counterexample must reach the log.
        prover = stub_prover(1, stdout="frame: VIOLATED (2.0ms)\n  step s0 writes src/evil.py\n")
        with pytest.raises(SystemExit):
            execute_plan.run_prover(prover, *prover_inputs, head_branch="feature/x")
        err = capsys.readouterr().err
        assert "DISPROVED" in err
        assert "step s0 writes src/evil.py" in err

    def test_exit_two_fails_as_operational(self, stub_prover, prover_inputs, capsys):
        # Exit 2 means nothing was proved at all — an operational failure of
        # the run, not evidence about the plan, and logged as such.
        prover = stub_prover(2, stderr="prove-cli: changed-files: expected an array of strings\n")
        with pytest.raises(SystemExit):
            execute_plan.run_prover(prover, *prover_inputs, head_branch="feature/x")
        err = capsys.readouterr().err
        assert "operational failure" in err
        assert "expected an array of strings" in err
        assert "DISPROVED" not in err

    def test_exit_two_carries_an_undecided_reason_from_stdout(self, stub_prover, prover_inputs, capsys):
        # An UNDECIDED policy exits 2 (nothing was proved) but reports on STDOUT,
        # where every verdict line goes. Logging stderr alone would drop the one
        # thing an operator can act on — which solver query gave up, and why.
        prover = stub_prover(
            2,
            stdout="ordering: holds (1.0ms)\nframe: UNDECIDED (3.0ms)\n"
                   "  frame: UNDECIDED — the solver returned unknown\n"
                   "  solver reason: max. resource limit exceeded\n",
        )
        with pytest.raises(SystemExit):
            execute_plan.run_prover(prover, *prover_inputs, head_branch="feature/x")
        err = capsys.readouterr().err
        assert "operational failure" in err
        assert "max. resource limit exceeded" in err
        assert "DISPROVED" not in err

    def test_unrunnable_prover_fails_closed(self, tmp_path, prover_inputs, capsys):
        with pytest.raises(SystemExit):
            execute_plan.run_prover(tmp_path / "does-not-exist.js", *prover_inputs, head_branch="feature/x")
        assert "nothing was proved" in capsys.readouterr().err

    def test_a_head_branch_beginning_with_a_dash_still_reaches_the_prover(
            self, tmp_path, prover_inputs, capsys):
        # git accepts `-evil` as a branch name (`git check-ref-format
        # refs/heads/-evil` exits 0) and prepare_fix_context forwards head_ref
        # verbatim, so a contributor chooses this value. Passed as a separate argv
        # element, Node's parseArgs reads it as an option and throws, and every
        # /fix on that pull request ends red blaming the harness.
        #
        # The stub is the REAL parseArgs, so it fails exactly as prove-cli does on
        # the two-element form. A stub that accepted either spelling would pass
        # whichever way the argv is built and pin nothing.
        script = tmp_path / "parse_argv.js"
        script.write_text(
            "const { parseArgs } = require('node:util');\n"
            "const { values } = parseArgs({ options: {\n"
            "  plan: { type: 'string' }, 'changed-files': { type: 'string' },\n"
            "  policy: { type: 'string' }, 'head-branch': { type: 'string' },\n"
            "} });\n"
            "process.stdout.write('head-branch seen: ' + values['head-branch'] + '\\n');\n"
            "process.exitCode = 0;\n"
        )
        execute_plan.run_prover(script, *prover_inputs, head_branch="-evil")
        assert "head-branch seen: -evil" in capsys.readouterr().out


# --------------------------------------------------- pr_snapshot / fork ---


def pr_payload(head="reviewed-sha", base_ref="main", base_sha="base-tip",
               head_repo="o/r", base_repo="o/r", head_ref="feature/x", state="open"):
    # `state` as GitHub really lists it: a remediation's premise dies with the pull
    # request, so a payload without one would let every delivery case pass on a
    # shape the API never returns.
    return {
        "state": state,
        "head": {"sha": head, "ref": head_ref,
                 "repo": {"full_name": head_repo} if head_repo else None},
        "base": {"ref": base_ref, "sha": base_sha, "repo": {"full_name": base_repo}},
    }


class TestPrSnapshot:
    def stub(self, monkeypatch, response):
        monkeypatch.setattr(
            execute_plan, "api_json", lambda path, method="GET", payload=None: response
        )

    def test_unmoved_pr_returns_the_snapshot(self, monkeypatch):
        self.stub(monkeypatch, pr_payload())
        pr = execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert pr["head"]["ref"] == "feature/x"

    def test_moved_head_fails(self, monkeypatch, capsys):
        self.stub(monkeypatch, pr_payload(head="new-sha"))
        with pytest.raises(SystemExit):
            execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert "head moved" in capsys.readouterr().err

    def test_retargeted_base_fails(self, monkeypatch, capsys):
        # A retarget changes the diff the plan claims to fix just as surely
        # as a push does — post.py's rule, inherited.
        self.stub(monkeypatch, pr_payload(base_ref="release/2"))
        with pytest.raises(SystemExit):
            execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert "retargeted" in capsys.readouterr().err

    def test_a_merged_pr_fails_before_any_effect(self, monkeypatch, capsys):
        # A merged pull request keeps the reviewed head SHA, so pr_moved returns
        # None and nothing else refuses: head.repo stays non-null so is_fork does
        # not fire, and the commit and tree stay readable. On the stacked path the
        # base is the head branch, which merging may have deleted, and create_ref
        # runs before POST /pulls — so the run would leave a branch behind and no
        # follow-up pull request.
        self.stub(monkeypatch, pr_payload(state="closed"))
        with pytest.raises(SystemExit):
            execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        err = capsys.readouterr().err
        assert "not open" in err
        assert "nothing executed" in err

    def test_an_unknown_state_is_not_open(self, monkeypatch, capsys):
        # Anything that is not exactly "open" is refused, so a state this harness
        # has not seen reads as not-open rather than as permission.
        self.stub(monkeypatch, pr_payload(state=None))
        with pytest.raises(SystemExit):
            execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert "not open" in capsys.readouterr().err

    def test_the_state_is_checked_before_the_move(self, monkeypatch, capsys):
        # Ordering matters only for the message a commander reads: a merged pull
        # request whose head ALSO moved should be reported as closed, since
        # reopening it is not the remedy.
        self.stub(monkeypatch, pr_payload(state="closed", head="new-sha"))
        with pytest.raises(SystemExit):
            execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert "not open" in capsys.readouterr().err

    def test_a_base_branch_advance_still_executes(self, monkeypatch):
        # The plan was anchored to the event base, so an unrelated merge into
        # the base branch does not invalidate it. base.sha is live and moves
        # with that merge, which is why the retarget test above is on the ref.
        self.stub(monkeypatch, pr_payload(base_sha="landed-during-the-gate"))
        pr = execute_plan.pr_snapshot("o/r", 1, "reviewed-sha", "main")
        assert pr["head"]["ref"] == "feature/x"


class TestIsFork:
    def test_same_repo_is_not_a_fork(self):
        assert not execute_plan.is_fork(pr_payload())

    def test_different_head_repo_is_a_fork(self):
        assert execute_plan.is_fork(pr_payload(head_repo="fork-owner/r"))

    def test_deleted_fork_repo_counts_as_fork(self):
        # head.repo is null when the fork was deleted: still no branch in the
        # base repository to base a stacked PR on.
        assert execute_plan.is_fork(pr_payload(head_repo=None))


# ------------------------------------------------------------------ main ---


@pytest.fixture
def artifact_dir(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "plan.json").write_text(json.dumps({"steps": [suggest()]}))
    (artifact / "diff.patch").write_text(PLAN_DIFF)
    (artifact / "changed_files.json").write_text(json.dumps(PLAN_CHANGED_FILES))
    # The ACCEPTED artifact and the commanded ordinal, not a bare finding: the
    # executor derives the finding by re-verifying the artifact and indexing it,
    # so its membership in an accepted review is structural rather than claimed.
    # Both are inputs to a gate (ADR-0007), so both are fail-closed like plan.json.
    (artifact / "review.json").write_text(json.dumps({
        "summary": "`load` gained a check its callers do not expect.",
        "findings": [{
            "path": "src/app.py", "line": 2, "severity": "high",
            "title": "load() breaks callers", "body": "the body",
        }],
        "residual_risk": "",
    }))
    (artifact / "commanded_index.json").write_text(json.dumps({"index": 0}))
    return artifact


@pytest.fixture
def pr_root(tmp_path):
    root = tmp_path / "pr_root"
    for path, content in PLAN_TREE.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


@pytest.fixture
def main_env(tmp_path, monkeypatch, artifact_dir, pr_root, stub_prover):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY))
    prover = stub_prover(0, stdout="ordering: holds (1.0ms)\n")

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("HEAD_SHA", "reviewed-sha")
    monkeypatch.setenv("BASE_SHA", "event-base-sha")
    monkeypatch.setenv("BASE_REF", "main")
    # The reviewed head BRANCH. Outside the harness namespace here, so the fixture
    # plans stay admissible and the refusal is tested by naming it deliberately.
    monkeypatch.setenv("HEAD_REF", "feature/x")
    monkeypatch.setattr(sys, "argv", [
        "execute_plan.py",
        "--artifact-dir", str(artifact_dir),
        "--pr-root", str(pr_root),
        "--policy", str(policy_path),
        "--prover", str(prover),
    ])
    # The executor fetches its own provenance inputs; the network stands in for a
    # PR whose real changes are the plan fixtures'. Tests about the fetch itself
    # override this.
    monkeypatch.setattr(
        execute_plan, "fetch_anchored_pair",
        lambda repo, base, head: (PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)),
    )
    return artifact_dir


def stub_delivery(monkeypatch):
    """Neutralise the delivery for a test whose property is a GATE.

    These reach delivery now that it is built, and it needs a bundle model stamp
    and a resolved login that a gate test has no reason to supply. Stubbing the
    reconciler and those two reads keeps each case about the gate it was written
    for, rather than about what delivery happens to require.
    """
    monkeypatch.setattr(execute_plan, "reconcile_suggestions",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(execute_plan, "resolve_bot_login", lambda: "smtithy[bot]")
    monkeypatch.setattr(execute_plan, "read_model_stamp", lambda artifact_dir: "test-model")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/1")


def commanded_at(artifact_dir, *, line, path="src/app.py"):
    """Re-anchor the bundle's commanded finding.

    The executor derives the finding by re-verifying review.json against the diff
    it fetched, so a test that narrows that diff must move the finding into it —
    otherwise the run refuses on provenance before reaching the property the test
    is about.
    """
    review = json.loads((artifact_dir / "review.json").read_text())
    review["findings"][0].update(path=path, line=line)
    (artifact_dir / "review.json").write_text(json.dumps(review))


def stub_pr(monkeypatch, response):
    calls = []

    def fake_api_json(path, method="GET", payload=None):
        calls.append(path)
        return response

    monkeypatch.setattr(execute_plan, "api_json", fake_api_json)
    return calls


class TestMain:
    def test_verified_suggestion_plan_reaches_the_decision(self, main_env, monkeypatch, capsys):
        # The decision itself, without the delivery: resolve_bot_login is left
        # unstubbed here, so the run fails closed at ownership resolution AFTER
        # announcing the decision. TestSuggestionDelivery covers the delivery.
        stub_pr(monkeypatch, pr_payload())
        monkeypatch.setattr(execute_plan, "resolve_bot_login",
                            lambda: execute_plan.fail("could not resolve the token's own login"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        captured = capsys.readouterr()
        assert "delivery decision: suggestion comments on 'src/app.py'" in captured.out
        assert "could not resolve" in captured.err

    def test_verified_patch_plan_decides_stacked_pr_on_the_head_branch(
            self, main_env, monkeypatch, capsys):
        # The DECISION, not the delivery: resolve_bot_login is left to fail closed
        # so the run stops right after announcing, which is also what pins the
        # announcement's position -- a decision printed only on the success path
        # would leave a failed run with no record of what it chose.
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(), push(), open_pr()]}))
        stub_pr(monkeypatch, pr_payload(head_ref="feature/x"))
        monkeypatch.setattr(execute_plan, "resolve_bot_login",
                            lambda: execute_plan.fail("could not resolve the token's own login"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        # The base comes from the live PR context, never the plan (open_pr
        # has no base argument — ADR-0009 addendum).
        assert "stacked PR based on 'feature/x'" in capsys.readouterr().out

    def test_a_push_to_the_reviewed_head_branch_is_rejected_here(
            self, main_env, monkeypatch, capsys):
        # dd7f879 called the head-branch refusal the check branch_prefix cannot
        # express -- the one stopping the push-to-the-contributor's-branch mode
        # ADR-0009's addendum spends its argument rejecting. It was reachable from
        # no production caller: head_branch defaulted to None here and in
        # plan_loop, so a contributor branch legally named inside the namespace
        # (which the addendum names as the case) was a legal push target.
        monkeypatch.setenv("HEAD_REF", "smtithy/theirs")
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(), push(name="smtithy/theirs"),
                       open_pr(branch="smtithy/theirs")]}))
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "own head branch" in capsys.readouterr().err

    def test_the_head_ref_reaches_the_prover_too(self, main_env, monkeypatch, capsys):
        # Both gates or neither: a Python-only wiring leaves the prover admitting
        # what the verifier refuses, which is the divergence the differential
        # corpus exists to catch.
        monkeypatch.setenv("HEAD_REF", "smtithy/theirs")
        seen = []
        monkeypatch.setattr(execute_plan, "run_prover",
                            lambda *args, **kwargs: seen.append(kwargs.get("head_branch")))
        stub_delivery(monkeypatch)
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert seen == ["smtithy/theirs"]

    def test_a_missing_head_ref_fails_closed(self, main_env, monkeypatch, capsys):
        # ADR-0012's reading for BASE_REF, applied to this one: absence is a
        # KeyError rather than a default, because a default would silently
        # re-disable the check.
        monkeypatch.delenv("HEAD_REF", raising=False)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(KeyError):
            execute_plan.main()

    def test_rejected_plan_never_reaches_the_prover_or_network(
            self, main_env, monkeypatch, capsys):
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [suggest(path="src/evil.py")]}))
        calls = stub_pr(monkeypatch, pr_payload())

        def exploding_prover(*args):
            raise AssertionError("prover ran for a rejected plan")

        monkeypatch.setattr(execute_plan, "run_prover", exploding_prover)
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "plan rejected" in capsys.readouterr().err
        assert calls == []

    def test_a_rejection_reaching_the_job_log_is_redacted(
            self, main_env, monkeypatch, capsys):
        # A plan Rejection interpolates the refused value the same way an
        # artifact one does, and this executor holds the policy: the transcript
        # and the stream capture are redacted, so the job log was the remaining
        # emit path.
        step = suggest()
        step["args"]["note"] = "see [d](http://logs.evil/?k=AKIAIOSFODNN7EXAMPLE)"
        (main_env / "plan.json").write_text(json.dumps({"steps": [step]}))
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        err = capsys.readouterr().err
        assert "plan rejected" in err
        assert "AKIAIOSFODNN7EXAMPLE" not in err

    def test_a_plan_scoped_to_the_wrong_file_is_rejected_here_too(
            self, main_env, monkeypatch, capsys):
        # The executor re-verifies scope rather than trusting the plan job to
        # have: it is the process holding the write credential, and ADR-0007's
        # "the command names one finding" is only a property if the gate at the
        # credential reads it. A plan patching another changed file passes frame,
        # denylist, ordering and anchoring — scope is the only thing refusing it.
        (main_env / "plan.json").write_text(json.dumps({"steps": [{
            "id": "s0", "kind": "suggest",
            "args": {"path": "src/util.py", "line": 1, "old": "def check(path):\n",
                     "new": "def check(path=None):\n", "note": "make path optional"},
        }]}))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "commanded finding" in capsys.readouterr().err
        assert calls == []

    def test_a_bundle_with_no_accepted_review_is_refused(self, main_env, monkeypatch, capsys):
        # Fail-closed on the missing input, read_model_stamp's rule: a plan whose
        # commanded finding cannot be derived cannot have its scope checked, and
        # proceeding would silently restore the prompt-only enforcement.
        (main_env / "review.json").unlink()
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "review.json" in capsys.readouterr().err
        assert calls == []

    def test_a_bundle_with_no_commanded_ordinal_is_refused(self, main_env, monkeypatch, capsys):
        # The ordinal is the half that cannot be re-derived here — which finding a
        # maintainer commanded is a fact about the COMMAND, not about the PR — so
        # its absence is fail-closed too, not a default of zero. Defaulting would
        # silently remediate the most severe finding of whatever review is in the
        # bundle, which is a fix nobody asked for.
        (main_env / "commanded_index.json").unlink()
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "commanded_index.json" in capsys.readouterr().err
        assert calls == []

    def test_a_forged_finding_no_review_contains_cannot_authorise_a_fix(
            self, main_env, monkeypatch, capsys):
        # The gap the bundle change closes, at the gate that holds the write
        # token. Under the old contract a well-shaped finding.json was an INPUT,
        # so this file alone directed the scope check at src/util.py and a real
        # remediation followed on a defect no reviewer found. Now it is not read
        # at all: the finding comes from review.json, whose own finding is on
        # src/app.py, and the plan below touches only src/util.py.
        (main_env / "finding.json").write_text(json.dumps(
            {"path": "src/util.py", "line": 1, "severity": "critical",
             "title": "no reviewer found this", "body": "but the fix would be real"}
        ))
        (main_env / "plan.json").write_text(json.dumps({"steps": [{
            "id": "s0", "kind": "suggest",
            "args": {"path": "src/util.py", "line": 1, "old": "def check(path):\n",
                     "new": "def check(path=None):\n", "note": "make path optional"},
        }]}))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "commanded finding" in capsys.readouterr().err
        assert calls == []

    def test_a_review_the_verifier_rejects_authorises_nothing(self, main_env, monkeypatch, capsys):
        # The bundle is the plan job's output, so its artifact gets the same
        # verifier post.py applies to the review job's — against provenance inputs
        # this process fetched itself. An artifact that could not have been
        # accepted cannot command a fix.
        review = json.loads((main_env / "review.json").read_text())
        review["findings"][0]["path"] = "src/never_touched_by_this_pr.py"
        (main_env / "review.json").write_text(json.dumps(review))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "not a changed file" in capsys.readouterr().err
        assert calls == []

    def test_the_ordinal_resolves_against_the_rendered_order(self, main_env, monkeypatch, capsys):
        # Both gates must read the ordinal the same way or they disagree about
        # which finding was commanded — the exact drift one reader exists to
        # prevent. Ordered so the artifact's order and the rendered order differ:
        # index 0 is the CRITICAL finding on src/app.py, which the fixture plan
        # touches, so a run indexing review.json directly refuses on scope.
        (main_env / "review.json").write_text(json.dumps({
            "summary": "two findings, least severe first",
            "findings": [
                {"path": "src/util.py", "line": 1, "severity": "low",
                 "title": "minor", "body": "b"},
                {"path": "src/app.py", "line": 2, "severity": "critical",
                 "title": "load() breaks callers", "body": "the body"},
            ],
            "residual_risk": "",
        }))
        stub_delivery(monkeypatch)
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert "delivery decision: suggestion comments on 'src/app.py'" in capsys.readouterr().out

    def test_disproved_plan_never_reaches_the_network(self, main_env, monkeypatch, capsys, stub_prover, tmp_path):
        disprover = stub_prover(1, stdout="frame: VIOLATED\n")
        argv = sys.argv[:]
        argv[argv.index("--prover") + 1] = str(disprover)
        monkeypatch.setattr(sys, "argv", argv)
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "DISPROVED" in capsys.readouterr().err
        assert calls == []

    def test_a_plan_no_delivery_carries_never_reaches_the_network(self, main_env, monkeypatch, capsys):
        # A lone label is in-grammar and vacuously ordered. It is now refused by
        # cardinality in the gate rather than by decide_delivery, which is strictly
        # earlier -- a Rejection is feedback a session can act on. The property
        # this test exists for is unchanged: nothing is fetched.
        (main_env / "plan.json").write_text(json.dumps({"steps": [label()]}))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "no fix step" in capsys.readouterr().err
        assert calls == []

    def test_decide_delivery_still_refuses_a_fixless_plan_on_its_own(self):
        # The gate now rejects this shape first, so the executor's own refusal is
        # no longer reachable through main(). It stays as defence in depth -- the
        # executor re-decides rather than trusting that a gate ran -- and is
        # asserted directly so removing it cannot go unnoticed.
        with pytest.raises(Refusal, match="no fix step"):
            decide_delivery([push("s0"), open_pr("s1")])

    def test_moved_head_fails_after_the_decision(self, main_env, monkeypatch, capsys):
        stub_pr(monkeypatch, pr_payload(head="moved-sha"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "head moved" in capsys.readouterr().err

    def test_stacked_pr_on_a_fork_is_refused(self, main_env, monkeypatch, capsys):
        # ADR-0009 addendum: a fork PR's head branch does not exist in the
        # base repository, so there is nothing to base a stacked PR on.
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(), push(), open_pr()]}))
        stub_pr(monkeypatch, pr_payload(head_repo="fork-owner/r"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "stacked PR refused" in capsys.readouterr().err

    def test_suggestions_on_a_fork_are_fine(self, main_env, monkeypatch, capsys):
        # The fork gate applies to the stacked PR only: suggestions are the
        # one delivery that works across both repository topologies.
        stub_delivery(monkeypatch)
        stub_pr(monkeypatch, pr_payload(head_repo="fork-owner/r"))
        execute_plan.main()
        captured = capsys.readouterr()
        assert "delivery decision: suggestion comments" in captured.out
        assert "stacked PR refused" not in captured.err


class TestProvenanceInputsAreFirstParty:
    """The frame condition — every patched path is a file the PR touched — is
    only as strong as the changed-file list it quantifies over, and that list
    arrived in the bundle from the job that ran the generator.

    Both gates read it: verify_plan takes the parsed list, and the prover takes
    a PATH, so a partial fix would leave the two proving different things about
    different files. That is why the fetched list is written to disk and the
    prover is pointed at THAT file.
    """

    FORGED = "src/forged.py"

    def forged_bundle(self, artifact_dir, pr_root):
        """A plan targeting a file the PR never changed, with a bundle diff and
        changed-file list that claim it did.

        Anchoring passes on purpose: the file exists in the quarantine with the
        bytes the step names, because the contributor authored it. Provenance is
        the only thing standing between the plan and a suggestion on a file the
        PR never touched.
        """
        target = pr_root / self.FORGED
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"import os\ndef load(path):\n    return path\n")
        (artifact_dir / "plan.json").write_text(json.dumps({
            "steps": [suggest(path=self.FORGED, line=2)],
        }))
        (artifact_dir / "diff.patch").write_text(
            f"diff --git a/{self.FORGED} b/{self.FORGED}\n"
            f"--- a/{self.FORGED}\n+++ b/{self.FORGED}\n"
            "@@ -1,3 +1,3 @@\n import os\n+def load(path):\n     return path\n"
        )
        (artifact_dir / "changed_files.json").write_text(json.dumps([self.FORGED]))

    def test_a_tampered_bundle_cannot_make_a_path_provenant(
        self, main_env, pr_root, monkeypatch, capsys
    ):
        self.forged_bundle(main_env, pr_root)
        # The first-party fetch reports what the PR really changed.
        monkeypatch.setattr(
            execute_plan, "fetch_anchored_pair",
            lambda repo, base, head: (PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)),
        )
        stub_pr(monkeypatch, pr_payload())

        with pytest.raises(SystemExit):
            execute_plan.main()

        captured = capsys.readouterr()
        assert "not a file this PR touched" in captured.err
        assert "delivery decision" not in captured.out

    def test_the_prover_is_given_the_fetched_list_not_the_bundles(
        self, main_env, pr_root, monkeypatch
    ):
        # verify_plan takes the parsed list and the prover takes a path. If the
        # prover keeps reading the bundle's file, the frame condition is proved
        # against the very list the executor refused to trust.
        self.forged_bundle(main_env, pr_root)
        monkeypatch.setattr(
            execute_plan, "fetch_anchored_pair",
            lambda repo, base, head: (PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)),
        )
        seen = {}

        def spy_prover(prover_js, plan_path, changed_files_path, policy_path, **kwargs):
            seen["listed"] = json.loads(Path(changed_files_path).read_text())

        monkeypatch.setattr(execute_plan, "run_prover", spy_prover)
        # A plan the fetched list DOES support, so the run reaches the prover.
        (main_env / "plan.json").write_text(json.dumps({"steps": [suggest()]}))
        stub_delivery(monkeypatch)
        stub_pr(monkeypatch, pr_payload())

        execute_plan.main()

        assert seen["listed"] == list(PLAN_CHANGED_FILES)
        assert self.FORGED not in seen["listed"], "the prover read the bundle's changed-file list"

    def test_the_fetch_is_anchored_to_the_reviewed_shas(self, main_env, monkeypatch):
        asked = {}

        def fake_fetch(repo, base, head):
            asked.update(repo=repo, base=base, head=head)
            return PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)

        monkeypatch.setattr(execute_plan, "fetch_anchored_pair", fake_fetch)
        stub_delivery(monkeypatch)
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()

        assert asked == {"repo": "o/r", "base": "event-base-sha", "head": "reviewed-sha"}


# -------------------------------------------------------------- delivery ---


@pytest.fixture
def delivery_env(main_env, monkeypatch):
    """main_env plus the bundle's run_metadata.json and a resolved bot login.

    Both are things delivery needs and the decision did not: the model stamp is
    the attribution the posted comment carries, and the login is the security half
    of comment ownership.
    """
    (main_env / "run_metadata.json").write_text(json.dumps({"model": "global.anthropic.claude-opus-4-8"}))
    monkeypatch.setattr(execute_plan, "resolve_bot_login", lambda: "smtithy[bot]")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/1")
    return main_env


@pytest.fixture
def posted(monkeypatch):
    """Capture reconcile_suggestions' arguments without reaching the reconciler."""
    calls = []
    monkeypatch.setattr(
        execute_plan, "reconcile_suggestions",
        lambda repo, pr, steps, signatures, metadata, **kwargs: calls.append(
            {"repo": repo, "pr": pr, "steps": steps, "signatures": signatures,
             "metadata": metadata, **kwargs}),
    )
    return calls


class TestSuggestionDelivery:
    def test_a_verified_suggestion_plan_is_delivered(self, delivery_env, posted, monkeypatch, capsys):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(posted) == 1
        assert [s["id"] for s in posted[0]["steps"]] == ["s0"]
        assert "delivered 1 suggestion" in capsys.readouterr().out

    def test_the_run_exits_zero_once_delivered(self, delivery_env, posted, monkeypatch):
        # The refusal chunk A ended on is gone: a delivered remediation must not
        # look like a failed run.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()  # no SystemExit

    def test_only_the_suggest_steps_are_delivered(self, delivery_env, posted, monkeypatch):
        # A label step is a side effect, not a suggestion; handing it to the
        # reconciler would index args it does not have.
        (delivery_env / "plan.json").write_text(json.dumps({"steps": [suggest(), label()]}))
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert [s["kind"] for s in posted[0]["steps"]] == ["suggest"]

    def test_the_review_is_bound_to_the_reviewed_head_sha(self, delivery_env, posted, monkeypatch):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert posted[0]["head_sha"] == "reviewed-sha"

    def test_ownership_uses_the_resolved_bot_login(self, delivery_env, posted, monkeypatch):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert posted[0]["bot_login"] == "smtithy[bot]"

    def test_a_push_landing_while_delivering_fails_the_run(self, delivery_env, posted, monkeypatch, capsys):
        # The pre-check and the write are not atomic, and several live API calls
        # sit between them (the login resolve, the comment listing, the supersede
        # pass). post.py re-checks after ITS write for exactly this reason, and
        # submit_review's docstring says the post-write half of the check stays.
        # Unchecked, the run reports "delivered" green having already retracted the
        # previous run's comments against a head that no longer exists.
        payloads = [pr_payload(), pr_payload(head="pushed-over-us")]
        monkeypatch.setattr(execute_plan, "api_json",
                            lambda path, method="GET", payload=None: payloads.pop(0))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert len(posted) == 1, "the suggestions were already posted; this is the check AFTER"
        err = capsys.readouterr().err
        assert "head moved since review" in err
        assert "while delivering" in err, "the message must say the write already happened"

    def test_nothing_is_withdrawn_when_the_head_moves_under_a_delivery(
            self, delivery_env, posted, monkeypatch, capsys):
        # And the failure does NOT undo the posting. The comments are bound to the
        # reviewed SHA by commit_id, so GitHub marks them OUTDATED against the new
        # head — fail-visible, which is the property ADR-0009 leans on. Deleting
        # them would destroy correctly-outdated suggestions and any human thread
        # under them; the run fails so the commander sees it, and leaves them be.
        payloads = [pr_payload(), pr_payload(head="pushed-over-us")]
        monkeypatch.setattr(execute_plan, "api_json",
                            lambda path, method="GET", payload=None: payloads.pop(0))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "withdrawn" not in capsys.readouterr().err

    def test_a_delivery_on_an_unmoved_head_does_not_fail(self, delivery_env, posted, monkeypatch):
        # The check must not fire on the ordinary path: two fetches of an unmoved
        # PR agree, so a green run stays green.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()  # no SystemExit
        assert len(posted) == 1

    def test_the_retraction_scope_is_the_commanded_finding(self, delivery_env, posted, monkeypatch):
        # One command names one finding (ADR-0007), so this run may only withdraw
        # what that command could have produced. Unscoped, the reconciler reads
        # every OTHER finding's live suggestion as withdrawn and takes it down.
        # From the COMMANDED FINDING, not from the plan's steps: scope is a fact
        # about the command. The FINDING and not its file, because two findings of
        # one accepted artifact routinely share a file.
        import suggest

        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        commanded = json.loads(
            (delivery_env / "review.json").read_text())["findings"][0]
        expected = suggest.finding_identity(
            commanded, anchor_signatures(PLAN_DIFF, content_source=tree_source()))
        assert posted[0]["commanded_finding_key"] == expected
        # Not the file: a path is the set two commands speak for.
        assert posted[0]["commanded_finding_key"] != commanded["path"]

    def test_an_unresolvable_login_posts_nothing(self, delivery_env, posted, monkeypatch, capsys):
        # Ownership decides which comments the write token may edit or delete, so
        # a guess is not available: post.resolve_bot_login fails closed, and this
        # must happen BEFORE anything is posted.
        def unresolvable():
            execute_plan.fail("could not resolve the token's own login, nothing posted")

        monkeypatch.setattr(execute_plan, "resolve_bot_login", unresolvable)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []
        assert "could not resolve" in capsys.readouterr().err

    def test_the_signatures_come_from_the_quarantine_tree_at_the_reviewed_sha(
            self, delivery_env, posted, monkeypatch):
        # The window's source is part of the identity contract (ADR-0009
        # addendum). The executor already reads this tree for anchoring, and
        # passing it here is what makes the key independent of where the hunk
        # boundaries fall.
        #
        # A NARROW hunk is what distinguishes the two sources: PLAN_DIFF's hunks
        # span their whole files, so both windows agree there and this case would
        # pass against a diff-derived window. Here line 3's neighbours exist only
        # in the tree, so an `absent` in the signature means the window came from
        # the diff.
        narrow = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -3,1 +3,1 @@\n+    check(path)\n"
        )
        monkeypatch.setattr(
            execute_plan, "fetch_anchored_pair",
            lambda repo, base, head: (narrow.encode(), list(PLAN_CHANGED_FILES)),
        )
        # The commanded finding is anchored inside THIS diff's only hunk: the
        # executor now derives it by re-verifying the review against the diff it
        # fetched, so a fixture whose finding sits outside these hunks is refused
        # for provenance before reaching the property under test.
        commanded_at(delivery_env, line=3)
        (delivery_env / "plan.json").write_text(json.dumps({"steps": [{
            "id": "s0", "kind": "suggest",
            "args": {"path": "src/app.py", "line": 3, "old": "    check(path)\n",
                     "new": "    check(path or '')\n", "note": "guard the empty case"},
        }]}))
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()

        signature = posted[0]["signatures"][("src/app.py", 3)]
        assert "def load(path):" in signature, "the preceding line came from the diff, not the tree"
        assert "return os.environ" in signature, "the following line came from the diff, not the tree"
        assert "absent" not in signature

    def test_the_signatures_are_computed_from_the_fetched_diff(
            self, delivery_env, posted, monkeypatch):
        # Identity must be keyed on the diff both gates checked, never the
        # bundle's copy: a tampered bundle diff would let a plan job choose which
        # existing comment its suggestion collides with.
        #
        # The two diffs must DIFFER OBSERVABLY or this asserts nothing. main()
        # never reads the bundle's diff.patch at all, so writing a forged file
        # there and checking the result looks unforged passes under any wiring —
        # including a deliberately forged one. The signature map's KEY SET is what
        # distinguishes them: the fetched diff is what decides which lines are
        # anchorable, so a bundle diff naming a different hunk would key the map on
        # lines the fetched diff makes no anchor for.
        bundle_diff = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                       "@@ -40,2 +40,2 @@\n+forged\n+forged2\n")
        (delivery_env / "diff.patch").write_text(bundle_diff)
        fetched_keys = set(anchor_signatures(PLAN_DIFF, content_source=tree_source()))
        bundle_keys = set(anchor_signatures(bundle_diff, content_source=tree_source()))
        assert fetched_keys != bundle_keys, "the fixture must make the two diffs distinguishable"

        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert set(posted[0]["signatures"]) == fetched_keys
        assert ("src/app.py", 40) not in posted[0]["signatures"], "keyed on the bundle's hunk"

    def test_the_comment_is_attributed_to_the_model_that_ran(self, delivery_env, posted, monkeypatch):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert posted[0]["metadata"]["model"] == "global.anthropic.claude-opus-4-8"
        assert posted[0]["metadata"]["sha"] == "reviewed-sha"

    def test_a_bundle_naming_no_model_posts_nothing(self, delivery_env, posted, monkeypatch, capsys):
        # read_model_stamp's rule: the stamp is the audit trail for "which model
        # said this", and a placeholder would be a false one.
        (delivery_env / "run_metadata.json").unlink()
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []
        assert "run_metadata.json" in capsys.readouterr().err

    def test_the_policy_hash_is_of_the_policy_that_gated_this_plan(
            self, delivery_env, posted, monkeypatch):
        # ADR-0005's visibility requirement: the hash a reader sees must be the
        # policy this executor actually enforced, so it is computed from the file
        # passed to --policy rather than from the harness's own copy.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        policy_path = Path(sys.argv[sys.argv.index("--policy") + 1])
        expected = hashlib.sha256(policy_path.read_bytes()).hexdigest()[:12]
        assert posted[0]["metadata"]["policy"] == expected

    def test_a_stacked_pr_plan_is_never_delivered_as_suggestions(
            self, delivery_env, posted, stacked, monkeypatch):
        # What survives of the refusal this replaces. The two deliveries are
        # ALTERNATIVES, not a fallback chain: a plan whose fix spans files (or
        # coordinates several hunks in one) is only correct as a whole, and
        # per-file suggestions of it can be half-applied. ADR-0009's atomicity
        # rule is why the stacked PR exists, so falling back to suggestions here
        # would deliver exactly the state the rule refuses.
        (delivery_env / "plan.json").write_text(json.dumps({"steps": [patch(), push(), open_pr()]}))
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert posted == [], "a stacked plan must not be delivered as suggestion comments"
        assert len(stacked) == 1

    def test_suggestions_are_delivered_on_a_fork_pr(self, delivery_env, posted, monkeypatch):
        # The whole reason this is the delivery built first: a stacked PR needs the
        # head branch to exist in the base repository, and a fork's does not.
        stub_pr(monkeypatch, pr_payload(head_repo="fork-owner/r"))
        execute_plan.main()
        assert len(posted) == 1

    def test_nothing_is_delivered_when_the_head_moved(self, delivery_env, posted, monkeypatch, capsys):
        stub_pr(monkeypatch, pr_payload(head="moved-sha"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []

    def test_nothing_is_delivered_for_a_rejected_plan(self, delivery_env, posted, monkeypatch, capsys):
        (delivery_env / "plan.json").write_text(json.dumps({"steps": [suggest(path="src/evil.py")]}))
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []

    def test_nothing_is_delivered_for_a_disproved_plan(
            self, delivery_env, posted, monkeypatch, stub_prover):
        disprover = stub_prover(1, stdout="frame: VIOLATED\n")
        argv = sys.argv[:]
        argv[argv.index("--prover") + 1] = str(disprover)
        monkeypatch.setattr(sys, "argv", argv)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []


# --------------------------------------------------- stacked PR delivery ---


@pytest.fixture
def stacked(monkeypatch):
    """Capture deliver_stacked_pr's arguments without reaching the tree API."""
    calls = []

    def fake(repo, steps, applied, **kwargs):
        calls.append({"repo": repo, "steps": steps, "applied": applied, **kwargs})
        return {"number": 99, "html_url": "https://github.com/o/r/pull/99"}

    monkeypatch.setattr(execute_plan, "deliver_stacked_pr", fake)
    return calls


@pytest.fixture
def stacked_env(delivery_env):
    """delivery_env with a patch-chain plan, which decides stacked_pr."""
    (delivery_env / "plan.json").write_text(json.dumps(
        {"steps": [patch(), push(), open_pr()]}))
    return delivery_env


class TestStackedPrDelivery:
    def test_a_verified_patch_plan_is_delivered(self, stacked_env, stacked, monkeypatch, capsys):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(stacked) == 1
        assert "opened" in capsys.readouterr().out

    def test_the_run_exits_zero_once_delivered(self, stacked_env, stacked, monkeypatch):
        # The fail() this replaces made every stacked decision a red run. A
        # delivered remediation must not look like a failed one.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()  # no SystemExit

    def test_the_base_is_the_reviewed_prs_own_head_branch(self, stacked_env, stacked, monkeypatch):
        # ADR-0009's addendum, end to end: the base comes from the LIVE PR context,
        # so the fix merges INTO the open pull request and broken code never lands
        # on the default branch.
        stub_pr(monkeypatch, pr_payload(head_ref="contributor/their-branch"))
        execute_plan.main()
        assert stacked[0]["base"] == "contributor/their-branch"

    def test_the_base_is_not_the_prs_base_branch(self, stacked_env, stacked, monkeypatch):
        # The absurd reading the addendum was written to forbid: basing on `main`
        # means "merge the bug, then merge the fix".
        stub_pr(monkeypatch, pr_payload(base_ref="main", head_ref="feature/x"))
        execute_plan.main()
        assert stacked[0]["base"] == "feature/x"
        assert stacked[0]["base"] != "main"

    def test_the_applied_bytes_come_from_the_shared_applier(self, stacked_env, stacked, monkeypatch):
        # Not a re-derivation: these are the bytes plan_verify's anchoring phase
        # produced, which are the bytes that were bounded, denylisted and
        # secret-scanned. The fixture patch turns `def load(path):` into
        # `def load(path=None):` in src/app.py.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert stacked[0]["applied"] == {
            "src/app.py": b"import os\ndef load(path=None):\n    check(path)\n    return os.environ\n"
        }

    def test_the_key_is_the_commanded_findings_key(self, stacked_env, stacked, monkeypatch):
        # ADR-0007's (pr, head_sha, finding). The finding is the DERIVED one, so
        # the key cannot be steered by anything the plan job wrote.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        expected = stack.fix_key(
            1, "reviewed-sha",
            {"path": "src/app.py", "line": 2},
            execute_plan.anchor_signatures(
                PLAN_DIFF, content_source=execute_plan.tree_content_source(
                    Path(sys.argv[sys.argv.index("--pr-root") + 1]))),
        )
        assert stacked[0]["key"] == expected

    def test_the_reviewed_sha_is_passed_as_the_commit_parent_source(
            self, stacked_env, stacked, monkeypatch):
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert stacked[0]["reviewed_sha"] == "reviewed-sha"

    def test_a_fork_pr_is_refused_before_delivery(self, stacked_env, stacked, monkeypatch, capsys):
        # A fork PR's head branch does not exist in the base repository, so there
        # is no branch for a stacked PR to base on (ADR-0009 addendum).
        stub_pr(monkeypatch, pr_payload(head_repo="fork-owner/r"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []
        assert "fork" in capsys.readouterr().err

    def test_an_already_delivered_command_refuses_without_re_delivering(
            self, stacked_env, stacked, monkeypatch, capsys):
        # ADR-0007: two maintainers typing /fix 3, or one typing it twice, must not
        # produce two branches and two pull requests.
        def already(*args, **kwargs):
            raise execute_plan.AlreadyDelivered("already at #12")

        monkeypatch.setattr(execute_plan, "deliver_stacked_pr", already)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "already" in capsys.readouterr().err

    def test_a_refused_shape_fails_the_run(self, stacked_env, stacked, monkeypatch, capsys):
        def refuse(*args, **kwargs):
            raise execute_plan.StackRefusal("no patched content to commit")

        monkeypatch.setattr(execute_plan, "deliver_stacked_pr", refuse)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "no patched content" in capsys.readouterr().err

    def test_nothing_is_delivered_for_a_rejected_plan(self, stacked_env, stacked, monkeypatch):
        (stacked_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(path="src/evil.py"), push(), open_pr()]}))
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []

    def test_nothing_is_delivered_for_a_disproved_plan(
            self, stacked_env, stacked, monkeypatch, stub_prover):
        disprover = stub_prover(1, stdout="frame: VIOLATED\n")
        argv = sys.argv[:]
        argv[argv.index("--prover") + 1] = str(disprover)
        monkeypatch.setattr(sys, "argv", argv)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []

    def test_nothing_is_delivered_when_the_head_moved(self, stacked_env, stacked, monkeypatch):
        stub_pr(monkeypatch, pr_payload(head="moved-sha"))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []

    def test_no_suggestion_is_posted_for_a_patch_plan(self, stacked_env, stacked, posted, monkeypatch):
        # The two deliveries are alternatives, not a fallback chain: a stacked plan
        # must not also post suggestions, whose half-application is the state
        # ADR-0009's atomicity rule refuses.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert posted == []


# ------------------------------------------------------ the --allow gate ---


class TestTheAllowGate:
    """`--allow` declares which delivery mode the JOB INVOKING THIS may perform,
    and a mismatch fails closed after verification.

    It exists because the lane routes on the plan's structure to decide which job
    runs, and the two jobs hold different credentials: `execute` has
    pull-requests: write, `stack` also has contents: write. The routing job holds
    no credential and its input is a plan the executor has not yet verified, so
    unverified structure decides which job STARTS.

    This is what bounds that concession to exactly "which job starts". A plan
    misrepresenting its own mode reaches a job whose --allow refuses it, so the
    credential is minted and then not used: a self-inflicted DoS with no benefit,
    rather than a delivery performed under a scope it should not have had.
    """

    def argv_with(self, monkeypatch, *allow):
        argv = sys.argv[:]
        for mode in allow:
            argv += ["--allow", mode]
        monkeypatch.setattr(sys, "argv", argv)

    def test_a_suggestion_plan_is_delivered_when_suggestions_are_allowed(
            self, delivery_env, posted, monkeypatch):
        self.argv_with(monkeypatch, "suggestions")
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(posted) == 1

    def test_a_stacked_plan_is_refused_by_a_suggestions_only_job(
            self, stacked_env, stacked, monkeypatch, capsys):
        # The case the whole gate exists for: a plan that routed as suggestions
        # (or lied) arriving at the job WITHOUT contents: write must not deliver.
        self.argv_with(monkeypatch, "suggestions")
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []
        err = capsys.readouterr().err
        assert "stacked_pr" in err and "not allowed" in err

    def test_a_suggestion_plan_is_refused_by_a_stacked_only_job(
            self, delivery_env, posted, monkeypatch, capsys):
        # And the other direction, which is not symmetric in consequence but is in
        # posture: the job holding contents: write must not quietly do the other
        # delivery either, or its credential is minted for work it was not routed
        # to do.
        self.argv_with(monkeypatch, "stacked_pr")
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert posted == []
        assert "suggestions" in capsys.readouterr().err

    def test_a_stacked_plan_is_delivered_when_stacked_is_allowed(
            self, stacked_env, stacked, monkeypatch):
        self.argv_with(monkeypatch, "stacked_pr")
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(stacked) == 1

    def test_the_refusal_lands_before_any_write(self, stacked_env, stacked, monkeypatch):
        # Fail-closed means nothing was created, not that the failure was reported
        # after the fact.
        self.argv_with(monkeypatch, "suggestions")
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert stacked == []

    def test_an_unknown_mode_is_refused_at_parse_time(self, delivery_env, monkeypatch, capsys):
        # A typo in the workflow must be refused BY ARGPARSE, and the distinction
        # is not cosmetic. Without `choices` the run still exits -- an allowlist
        # matching nothing refuses every plan -- so asserting SystemExit alone
        # passes either way and enforces nothing. What separates them is WHERE the
        # complaint comes from: argparse names the invalid choice on stderr and
        # exits 2 without ever verifying a plan, while a nothing-matches allowlist
        # reports a delivery refusal after doing the whole gate. The first is a
        # deployment error the operator can read; the second looks like a bad plan.
        self.argv_with(monkeypatch, "suggestionz")
        with pytest.raises(SystemExit) as exit_info:
            execute_plan.main()
        assert exit_info.value.code == 2, "an invalid --allow must be an argparse error, not a refusal"
        err = capsys.readouterr().err
        assert "suggestionz" in err and "invalid choice" in err
        assert "delivery" not in err, "the plan was verified before the typo was noticed"

    def test_omitting_allow_permits_every_mode(self, delivery_env, posted, monkeypatch):
        # Backwards-compatible by design: the flag CONFINES a job, and a caller
        # that names no confinement (an operator running this by hand) is not
        # silently confined to nothing. The workflow always passes it; that is
        # asserted in test_workflow_shape, where the workflow is the subject.
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(posted) == 1

    def test_both_modes_may_be_allowed_at_once(self, stacked_env, stacked, monkeypatch):
        self.argv_with(monkeypatch, "suggestions", "stacked_pr")
        stub_pr(monkeypatch, pr_payload())
        execute_plan.main()
        assert len(stacked) == 1


class TestAnEmptyGateInputIsRefused:
    """A required env var that is present but EMPTY must fail closed.

    The reason KeyError alone is not enough, found in production: the upstream step
    emitted only two of its four outputs, so BASE_REF and HEAD_REF arrived as empty
    strings. `os.environ[...]` succeeds for those, so the fail-closed KeyError this
    module relies on never fired, and:

    - the retarget check compared a live base ref against "" and refused every
      command -- loud, and merely broken;
    - HEAD_REF is what makes the reviewed-head-branch push refusal REACHABLE
      (check_write_class_targets). Empty, the `head_branch is not None` guard still
      holds and `branch == ""` matches no real branch, so the check ran and enforced
      nothing. Silent, and a containment hole.

    An empty value is therefore worse than a missing one, and both must be refused
    here rather than only upstream: this process trusts no other job.
    """

    @pytest.mark.parametrize("name", ["HEAD_SHA", "BASE_SHA", "BASE_REF", "HEAD_REF"])
    def test_an_empty_required_env_var_fails_closed(self, main_env, monkeypatch, capsys, name):
        monkeypatch.setenv(name, "")
        with pytest.raises(SystemExit):
            execute_plan.main()
        err = capsys.readouterr().err
        assert name in err, f"the failure must name {name}; got {err!r}"

    @pytest.mark.parametrize("name", ["HEAD_SHA", "BASE_SHA", "BASE_REF", "HEAD_REF"])
    def test_a_missing_required_env_var_still_fails_closed(self, main_env, monkeypatch, name):
        # The KeyError posture ADR-0012 established, kept.
        monkeypatch.delenv(name, raising=False)
        with pytest.raises((SystemExit, KeyError)):
            execute_plan.main()

    def test_an_empty_head_ref_does_not_silently_pass_the_push_refusal(
            self, main_env, monkeypatch, capsys):
        # The containment case specifically: a plan pushing to the contributor's own
        # branch must not be delivered because HEAD_REF happened to be empty.
        monkeypatch.setenv("HEAD_REF", "")
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(), push(name="smtithy/theirs"), open_pr(branch="smtithy/theirs")]}))
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "HEAD_REF" in capsys.readouterr().err
