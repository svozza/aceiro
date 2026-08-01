"""The differential oracle: one plan, both gates, same verdict.

The guard ADR-0003 specifies for the cutover period. Both gates were already
tested, but over DIFFERENT corpora — no test fed one input to both and compared
the answers, so a policy read by only one of them stayed invisible.

The VERDICT is compared, not the message: two implementations in two languages
whose prose differs by design.

Requires the built prover (`npm run build`) and skips loudly without it; a
silently-skipped differential is the failure mode this file exists to prevent.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "smtithy"))

from plan_verify import verify_plan  # noqa: E402
from verify import Rejection  # noqa: E402

from test_plan_verify import (  # noqa: E402
    PLAN_CHANGED_FILES,
    PLAN_DIFF,
    _full_policy,
    anchored_patch,
    anchored_suggest,
    label_step,
    open_pr_step,
    push_step,
    tree_source,
)

PROVER_JS = REPO_ROOT / "dist" / "plan" / "prove-cli.js"
POLICY_PATH = REPO_ROOT / "src" / "smtithy" / "policy.json"

pytestmark = pytest.mark.skipif(
    not PROVER_JS.exists(),
    reason=f"prover not built at {PROVER_JS} — run `npm run build` (CI does this before pytest)",
)


def prover_admits(plan, changed_files, tmp_path) -> bool:
    """True if prove-cli exits 0 (every policy holds).

    Exit 1 is a disproof and exit 2 is "nothing proved" (including a plan the TS
    schema gate rejects); both are "not admitted", which is the granularity the
    comparison needs.
    """
    plan_file = tmp_path / "plan.json"
    changed_file = tmp_path / "changed_files.json"
    plan_file.write_text(json.dumps(plan))
    changed_file.write_text(json.dumps(changed_files))
    result = subprocess.run(
        ["node", str(PROVER_JS), "--plan", str(plan_file),
         "--changed-files", str(changed_file), "--policy", str(POLICY_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode in (0, 1, 2), f"unexpected exit {result.returncode}: {result.stderr}"
    return result.returncode == 0


def verifier_admits(plan, changed_files) -> bool:
    try:
        verify_plan(plan, PLAN_DIFF, changed_files, _full_policy(), tree_source())
    except Rejection:
        return False
    return True


# Each case is (id, plan, changed_files, expected_admitted). The expectation is
# stated rather than derived so a case where BOTH gates are wrong in the same
# direction still fails — comparing the two against each other alone would call
# that agreement.
CASES = [
    (
        "legal-write-chain",
        {"steps": [anchored_patch("s0"), push_step("s1"), open_pr_step("s2")]},
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        "open-pr-before-push-before-patch",
        {"steps": [open_pr_step("s0"), push_step("s1"), anchored_patch("s2")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "push-before-patch",
        {"steps": [push_step("s0"), anchored_patch("s1")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "open-pr-before-push",
        {"steps": [anchored_patch("s0"), open_pr_step("s1"), push_step("s2")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "violation-across-an-unrelated-step",
        {"steps": [open_pr_step("s0"), anchored_patch("s1"), push_step("s2")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # ADR-0009: no write-class step, so ordering holds vacuously on both
        # sides. The case that must not become a rejection when ordering is
        # enforced in a second place.
        "suggestion-only-vacuous-ordering",
        {"steps": [anchored_suggest("s0")]},
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        # Rejected by BOTH gates on the shipped policy, whose label_allowlist
        # ships empty — a label is a control surface, so a consumer must name the
        # ones it accepts. The case stays here in its rejecting form because a
        # gate that stopped enforcing the allowlist is exactly the drift this
        # file exists to catch.
        "label-off-the-shipped-empty-allowlist",
        {"steps": [anchored_patch("s0"), label_step("s1")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "branch-outside-the-harness-namespace",
        {"steps": [anchored_patch("s0"), push_step("s1", name="main"),
                   open_pr_step("s2", branch="main")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # Fits max_steps and is correctly ordered (all pushes before all opens),
        # so only cardinality stands between it and eighteen external effects.
        "nine-write-chains-for-one-patch",
        {"steps": [anchored_patch("s0")]
                  + [push_step(f"p{i}", name=f"smtithy/b{i}") for i in range(9)]
                  + [open_pr_step(f"o{i}", branch=f"smtithy/b{i}") for i in range(9)]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "suggest-plan-carrying-a-write-chain",
        {"steps": [anchored_suggest("s0"), push_step("s1"), open_pr_step("s2")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        "out-of-frame-patch",
        {"steps": [anchored_patch("s0", path="src/evil.py")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # The denylist is the divergence in the other direction: Python rejected
        # this while the prover printed 'frame: holds' and exited 0, because the
        # path IS in changed_files so the frame obligation alone was satisfied.
        "denylisted-path-that-is-a-changed-file",
        {"steps": [anchored_patch("s0", path=".github/workflows/ci.yml")]},
        [".github/workflows/ci.yml"],
        False,
    ),
    (
        "denylisted-pem-that-is-a-changed-file",
        {"steps": [anchored_patch("s0", path="deploy/key.pem")]},
        ["deploy/key.pem"],
        False,
    ),
]


@pytest.mark.parametrize("plan,changed_files,expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_both_gates_reach_the_same_verdict(plan, changed_files, expected, tmp_path):
    python_verdict = verifier_admits(plan, changed_files)
    prover_verdict = prover_admits(plan, changed_files, tmp_path)
    assert python_verdict == expected, (
        f"verify_plan {'admitted' if python_verdict else 'rejected'}, expected "
        f"{'admit' if expected else 'reject'}"
    )
    assert prover_verdict == expected, (
        f"prove-cli {'admitted' if prover_verdict else 'rejected'}, expected "
        f"{'admit' if expected else 'reject'}"
    )
