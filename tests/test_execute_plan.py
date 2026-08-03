"""Tests for execute_plan.py — the executor's re-verify + delivery decision.

Chunk A of the plan executor: everything up to (and refusing at) the actual
delivery. Network is never touched (api_json is stubbed); the prover runs as
a stub subprocess script so the exit-code contract is exercised for real.

Covers decide_delivery's whole case analysis (ADR-0009: the decision is the
executor's, from checkable plan structure), run_prover's three-way exit
contract (0 proved / 1 disproved-with-counterexample / 2 nothing-proved),
the TOCTOU + fork gates on the PR snapshot, and main()'s fail-closed
ordering: verify before prove, prove before decide, decide before any fetch.
"""

import json
import sys
from pathlib import Path

import pytest

import execute_plan
from execute_plan import Refusal, decide_delivery
from verify import Rejection

from test_plan_verify import PLAN_DIFF, PLAN_CHANGED_FILES, PLAN_TREE

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


# --------------------------------------------------- pr_snapshot / fork ---


def pr_payload(head="reviewed-sha", base_ref="main", base_sha="base-tip",
               head_repo="o/r", base_repo="o/r", head_ref="feature/x"):
    return {
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
    # The commanded finding travels in the bundle: the executor re-verifies the
    # plan's SCOPE against it (ADR-0007), so it is an input to a gate rather than
    # evidence, like plan.json itself.
    (artifact / "finding.json").write_text(json.dumps(
        {"path": "src/app.py", "line": 2, "severity": "high", "title": "t", "body": "b"}
    ))
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


def stub_pr(monkeypatch, response):
    calls = []

    def fake_api_json(path, method="GET", payload=None):
        calls.append(path)
        return response

    monkeypatch.setattr(execute_plan, "api_json", fake_api_json)
    return calls


class TestMain:
    def test_verified_suggestion_plan_reaches_the_decision(self, main_env, monkeypatch, capsys):
        # Chunk A ends AT the decision: the run must still exit non-zero
        # ("decided but not delivered"), never green with nothing posted.
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        captured = capsys.readouterr()
        assert "delivery decision: suggestion comments on 'src/app.py'" in captured.out
        assert "not implemented yet" in captured.err

    def test_verified_patch_plan_decides_stacked_pr_on_the_head_branch(
            self, main_env, monkeypatch, capsys):
        (main_env / "plan.json").write_text(json.dumps(
            {"steps": [patch(), push(), open_pr()]}))
        stub_pr(monkeypatch, pr_payload(head_ref="feature/x"))
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
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
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

    def test_a_bundle_with_no_commanded_finding_is_refused(self, main_env, monkeypatch, capsys):
        # Fail-closed on the missing input, read_model_stamp's rule: a plan whose
        # commanded finding is unknown cannot have its scope checked, and
        # proceeding would silently restore the prompt-only enforcement.
        (main_env / "finding.json").unlink()
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "finding.json" in capsys.readouterr().err
        assert calls == []

    def test_a_tampered_finding_cannot_widen_the_scope(self, main_env, monkeypatch, capsys):
        # The bundle is the plan job's output, so its finding gets the same
        # artifact-element check plan_loop applies — a "finding" that is not one
        # cannot be used to authorise a fix.
        (main_env / "finding.json").write_text(json.dumps({"path": "src/app.py"}))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "missing keys" in capsys.readouterr().err
        assert calls == []

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

    def test_refused_plan_never_reaches_the_network(self, main_env, monkeypatch, capsys):
        # Verifies and proves (a lone label is in-grammar and vacuously
        # ordered) but no delivery carries it: refuse before any fetch.
        (main_env / "plan.json").write_text(json.dumps({"steps": [label()]}))
        calls = stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()
        assert "refused: no fix step" in capsys.readouterr().err
        assert calls == []

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
        stub_pr(monkeypatch, pr_payload(head_repo="fork-owner/r"))
        with pytest.raises(SystemExit):
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
        stub_pr(monkeypatch, pr_payload())

        with pytest.raises(SystemExit):
            execute_plan.main()

        assert seen["listed"] == list(PLAN_CHANGED_FILES)
        assert self.FORGED not in seen["listed"], "the prover read the bundle's changed-file list"

    def test_the_fetch_is_anchored_to_the_reviewed_shas(self, main_env, monkeypatch):
        asked = {}

        def fake_fetch(repo, base, head):
            asked.update(repo=repo, base=base, head=head)
            return PLAN_DIFF.encode(), list(PLAN_CHANGED_FILES)

        monkeypatch.setattr(execute_plan, "fetch_anchored_pair", fake_fetch)
        stub_pr(monkeypatch, pr_payload())
        with pytest.raises(SystemExit):
            execute_plan.main()

        assert asked == {"repo": "o/r", "base": "event-base-sha", "head": "reviewed-sha"}
