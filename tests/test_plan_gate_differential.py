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
    PLAN_TREE,
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
    return prover_admits_text(json.dumps(plan), changed_files, tmp_path)


def prover_admits_text(plan_text: str, changed_files, tmp_path) -> bool:
    """True if prove-cli exits 0 (every policy holds).

    Exit 1 is a disproof and exit 2 is "nothing proved" (including a plan the TS
    schema gate rejects); both are "not admitted", which is the granularity the
    comparison needs.

    The plan travels as TEXT because some divergences are in its JSON spelling:
    `1.0` and `1` parse to one double, so re-serializing would erase the very
    difference under test.
    """
    plan_file = tmp_path / "plan.json"
    changed_file = tmp_path / "changed_files.json"
    plan_file.write_text(plan_text)
    changed_file.write_text(json.dumps(changed_files))
    result = subprocess.run(
        ["node", str(PROVER_JS), "--plan", str(plan_file),
         "--changed-files", str(changed_file), "--policy", str(POLICY_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode in (0, 1, 2), f"unexpected exit {result.returncode}: {result.stderr}"
    return result.returncode == 0


def corpus_tree(plan):
    """PLAN_TREE plus each step's own `old` bytes, keyed by its path.

    Anchoring runs in the same phase as the frame, denylist and cap checks and
    rejects a path the content source does not carry. A case naming a file
    outside PLAN_TREE was therefore rejected for the fixture's thinness before
    the check it exists for was consulted, and the boolean comparison cannot
    tell those two rejections apart: neutralising the denylist, or raising
    max_patched_files to 99, left all 21 cases passing on
    "cannot read ... at the reviewed SHA".

    Derived per case rather than by widening PLAN_TREE, which is imported by
    tests/test_execute_plan.py and materialised on disk by its pr_root fixture.
    """
    tree = dict(PLAN_TREE)
    for step in plan.get("steps", []):
        args = step.get("args", {})
        path, old = args.get("path"), args.get("old")
        if isinstance(path, str) and isinstance(old, str) and path not in tree:
            tree[path] = old.encode()
    return tree


def verifier_admits(plan, changed_files) -> bool:
    try:
        verify_plan(plan, PLAN_DIFF, changed_files, _full_policy(), tree_source(corpus_tree(plan)))
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
        # Both branches confined, both inside the namespace, and DIFFERENT: a
        # relation between two steps, which a per-step loop cannot see.
        "open-pr-from-a-branch-the-plan-never-pushed",
        {"steps": [anchored_patch("s0"), push_step("s1", name="smtithy/a"),
                   open_pr_step("s2", branch="smtithy/b")]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # The admitting direction, so the rule is shown to have made the gates
        # agree rather than to have widened one.
        "open-pr-from-exactly-the-pushed-branch",
        {"steps": [anchored_patch("s0"), push_step("s1", name="smtithy/fix-1"),
                   open_pr_step("s2", branch="smtithy/fix-1")]},
        PLAN_CHANGED_FILES,
        True,
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
    (
        # The bounding caps, which TypeScript enforced NOWHERE: over the file
        # count, over the per-step line count, and over the byte budget the line
        # count cannot see. Each was admitted by the prover and rejected by
        # verify_plan, which is what this file exists to catch.
        "over-max-patched-files",
        {"steps": [anchored_patch(f"s{i}", path=f"src/f{i}.py") for i in range(4)]},
        [f"src/f{i}.py" for i in range(4)],
        False,
    ),
    (
        "over-max-changed-lines-in-one-step",
        {"steps": [anchored_patch("s0", new="x\n" * 200)]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # Two changed lines, 16 KB of substitution: the case the line cap is
        # structurally blind to, and the reason the byte cap exists.
        "long-single-line-rewrite-under-the-line-cap",
        {"steps": [anchored_patch("s0", old="x" * 8000, new="y" * 8000)]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # Astral input, where the two gates' string-length metrics genuinely
        # differ (UTF-16 units vs code points). The byte budget is defined in
        # UTF-8 bytes precisely so this case cannot land on opposite verdicts.
        "astral-content-over-the-byte-budget",
        {"steps": [anchored_patch("s0", new="🙂" * 2001)]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # Each step inside every per-step bound, the plan total outside it. Both
        # gates must count the sum, or "bounded" is a property of one step only.
        "per-step-bounds-met-plan-total-exceeded",
        {"steps": [anchored_patch("s0", old="def load(path):\n", new="z" * 7000),
                   anchored_patch("s1", old="    check(path)\n", new="z" * 7000),
                   anchored_patch("s2", old="    return os.environ\n", new="z" * 7000)]},
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # The complement: a plan comfortably inside every bound must stay
        # admitted, or the caps have cost the harness its purpose.
        "well-inside-every-bound",
        {"steps": [anchored_patch("s0"), push_step("s1"), open_pr_step("s2")]},
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        # A string whose length the two gates measured differently: 2001 astral
        # code points is 2001 to Python and 4002 UTF-16 units to TypeScript, so
        # open_pr.body's max_length of 4000 admitted it in one gate and rejected
        # it in the other. Both count code points now, so both admit it — and the
        # secret scan, markdown gate and byte budget are what actually bound it.
        "astral-body-inside-max-length-by-code-points",
        {"steps": [anchored_patch("s0"), push_step("s1"),
                   open_pr_step("s2", body="🙂" * 2001)]},
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        # The same metric in its rejecting direction, so the cap is shown to bind
        # rather than merely to have been widened.
        "astral-body-over-max-length-by-code-points",
        {"steps": [anchored_patch("s0"), push_step("s1"),
                   open_pr_step("s2", body="🙂" * 4001)]},
        PLAN_CHANGED_FILES,
        False,
    ),
]

# Cases whose defect is the plan's JSON SPELLING, which json.dumps would erase:
# it writes 1.0 as 1. Each is the literal text handed to both gates.
TEXT_CASES = [
    (
        # `1.0` is a float to Python's json and an integer to Number.isInteger,
        # so the suggest step's line was rejected by one gate and admitted by the
        # other.
        "suggest-line-spelled-as-a-decimal",
        '{"steps": [{"id": "s0", "kind": "suggest", "args": {"path": "src/app.py", '
        '"line": 2.0, "old": "def load(path):\\n", "new": "def load(path=None):\\n", '
        '"note": "make path optional"}}]}',
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # The same step spelled as an integer is the admitted control, so the case
        # above is shown to turn on the spelling and nothing else.
        "suggest-line-spelled-as-an-integer",
        '{"steps": [{"id": "s0", "kind": "suggest", "args": {"path": "src/app.py", '
        '"line": 2, "old": "def load(path):\\n", "new": "def load(path=None):\\n", '
        '"note": "make path optional"}}]}',
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        # A lone surrogate is legal JSON and survives both parsers, so it is a
        # spelling case: json.dumps would write it back as an escape either way,
        # but the value it produces is a string no UTF-8 encoder takes. The Python
        # gate's containment phase encoded it and raised UnicodeEncodeError -- not
        # a Rejection -- while the prover held all six policies, so the two gates
        # disagreed on whether the plan is even well-formed. No admit/reject case
        # could see it: an uncaught exception is neither.
        "patch-new-holding-a-lone-surrogate",
        '{"steps": [{"id": "s0", "kind": "patch", "args": {"path": "src/app.py", '
        '"old": "def load(path):\\n", "new": "\\ud800"}}]}',
        PLAN_CHANGED_FILES,
        False,
    ),
    (
        # The paired control: an astral code point spelled as the surrogate pair
        # JSON uses for it is ordinary text, and both gates must admit it.
        "patch-new-holding-a-paired-surrogate",
        '{"steps": [{"id": "s0", "kind": "patch", "args": {"path": "src/app.py", '
        '"old": "def load(path):\\n", "new": "\\ud83c\\udf89 fixed\\n"}}]}',
        PLAN_CHANGED_FILES,
        True,
    ),
    (
        # Exponent notation is a float to Python too.
        "suggest-line-in-exponent-notation",
        '{"steps": [{"id": "s0", "kind": "suggest", "args": {"path": "src/app.py", '
        '"line": 2e0, "old": "def load(path):\\n", "new": "def load(path=None):\\n", '
        '"note": "make path optional"}}]}',
        PLAN_CHANGED_FILES,
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


@pytest.mark.parametrize("plan_text,changed_files,expected",
                         [c[1:] for c in TEXT_CASES], ids=[c[0] for c in TEXT_CASES])
def test_both_gates_agree_on_the_plans_json_spelling(plan_text, changed_files, expected, tmp_path):
    # json.loads on the same text both gates get, so neither side is handed a
    # normalized copy of what the other was asked to judge.
    python_verdict = verifier_admits(json.loads(plan_text), changed_files)
    prover_verdict = prover_admits_text(plan_text, changed_files, tmp_path)
    assert python_verdict == expected, (
        f"verify_plan {'admitted' if python_verdict else 'rejected'}, expected "
        f"{'admit' if expected else 'reject'}"
    )
    assert prover_verdict == expected, (
        f"prove-cli {'admitted' if prover_verdict else 'rejected'}, expected "
        f"{'admit' if expected else 'reject'}"
    )


def _cases_by_id():
    return {case_id: (plan, files) for case_id, plan, files, _ in CASES}


def test_the_denylist_cases_fail_when_the_denylist_stops_matching():
    """Neutralise the denylist and its two cases must go red.

    The named check has to be the REASON, not merely a reason. These cases patch
    files outside PLAN_TREE, so with a bare tree_source() they also violate
    anchoring -- and anchoring is enough to reject them on its own. Deleting the
    denylist then changed nothing observable and all 21 cases stayed green, which
    is the opposite of what a differential corpus is for.
    """
    import plan_verify

    cases = _cases_by_id()
    original = plan_verify.matches_denylist
    plan_verify.matches_denylist = lambda *args, **kwargs: None
    try:
        still_rejected = [
            case_id
            for case_id in ("denylisted-path-that-is-a-changed-file", "denylisted-pem-that-is-a-changed-file")
            if not verifier_admits(*cases[case_id])
        ]
    finally:
        plan_verify.matches_denylist = original
    assert not still_rejected, (
        "these cases reject with the denylist disabled, so they are not testing it: "
        f"{still_rejected}"
    )


def test_the_patched_file_cap_case_fails_when_the_cap_is_raised():
    cases = _cases_by_id()
    plan, changed_files = cases["over-max-patched-files"]
    policy = _full_policy()
    policy["plan"]["max_patched_files"] = 99
    try:
        verify_plan(plan, PLAN_DIFF, changed_files, policy, tree_source(corpus_tree(plan)))
    except Rejection as exc:
        pytest.fail(f"over-max-patched-files rejects with the cap raised to 99, so it is not testing it: {exc}")


def test_the_out_of_frame_case_fails_when_every_step_path_is_a_changed_file():
    cases = _cases_by_id()
    plan, changed_files = cases["out-of-frame-patch"]
    paths = [step["args"]["path"] for step in plan["steps"] if "path" in step.get("args", {})]
    try:
        verify_plan(plan, PLAN_DIFF, list(changed_files) + paths, _full_policy(), tree_source(corpus_tree(plan)))
    except Rejection as exc:
        pytest.fail(f"out-of-frame-patch rejects with its path inside the frame, so it is not testing it: {exc}")


# Every key under policy.plan, and the file(s) that must MENTION it for that gate
# to be reading it. A mention is a weak proxy for a reader and deliberately so:
# the point is to make an unread key impossible to add quietly, not to prove the
# reading is correct — the corpus above is what does that. A key with no entry
# fails, so adding one to policy.json forces the decision "which gates read this?"
# to be made and recorded here.
#
# This is the assertion that would have caught plan.ordering having no Python
# reader at all.
#
# The TS side is the ENFORCING files only. policy.ts is excluded although it is
# part of the gate: it is the loader, and PLAN_KEYS plus the PlanPolicy interface
# name every plan key by construction, so a policy that loads at all is a policy
# policy.ts mentions. Counting it made this assertion a tautology on the TS side.
PYTHON_GATE = REPO_ROOT / "src" / "smtithy" / "plan_verify.py"
POLICY_LOADER = REPO_ROOT / "ts" / "plan" / "policy.ts"
TS_GATE_FILES = [
    REPO_ROOT / "ts" / "plan" / "prove.ts",
    REPO_ROOT / "ts" / "plan" / "schema.ts",
]

# Keys read by one gate BY DESIGN, with the reason. ADR-0003 divides the labour:
# the prover owns reachability reasoning, the Python verifier owns the artifact
# and containment checks until the port. Each entry is that division stated, so a
# key drifting out of a gate cannot be waved through as "probably intentional".
SINGLE_GATE_KEYS = {
    # ADR-0004's reservation, refused rather than read. policy.ts throws
    # PolicyError for a non-empty control_flow, which is a refusal in the loader
    # and not enforcement in an enforcing file; plan_verify's
    # check_reserved_closures is the enforcing reader, so Python is the gate.
    "control_flow": "python",
}


def _policy_plan_keys() -> list[str]:
    return sorted(json.loads(POLICY_PATH.read_text())["plan"])


def _keys_with_no_reader(exemptions: dict[str, str]) -> list[str]:
    python_text = PYTHON_GATE.read_text()
    ts_text = "\n".join(path.read_text() for path in TS_GATE_FILES)
    missing = []
    for key in _policy_plan_keys():
        expected_in = exemptions.get(key, "both")
        if expected_in in ("both", "python") and key not in python_text:
            missing.append(f"{key}: no reader in {PYTHON_GATE.name}")
        if expected_in in ("both", "ts") and key not in ts_text:
            missing.append(f"{key}: no reader in ts/plan/")
    return missing


def test_every_policy_key_has_a_reader_in_both_gates():
    missing = _keys_with_no_reader(SINGLE_GATE_KEYS)
    assert not missing, (
        "a policy key no gate reads is a rule that reads as enforcement while "
        "enforcing nothing:\n  " + "\n  ".join(missing)
    )


def test_the_loader_cannot_witness_a_key_it_only_declares():
    """policy.ts must stay out of the enforcing file set, and here is why.

    The loader names every plan key by construction — PLAN_KEYS and the
    PlanPolicy interface both enumerate them, and requireKeys refuses a policy
    carrying any key outside PLAN_KEYS. So counting it as a reader means a key
    with a Python reader and no enforcement anywhere in prove.ts or schema.ts
    passes: a bound a reviewer reads in policy.json that the prover does not
    enforce, which the ADR-0003 addendum calls worse than an absent one.
    """
    assert POLICY_LOADER not in TS_GATE_FILES
    loader_text = POLICY_LOADER.read_text()
    unnamed = [key for key in _policy_plan_keys() if key not in loader_text]
    assert not unnamed, (
        "policy.ts no longer names every plan key, so the tautology this exclusion "
        f"exists for may have gone: {unnamed}"
    )


def test_every_exemption_is_load_bearing():
    # An exemption whose removal changes nothing is documentation wearing an
    # assertion's clothes: SINGLE_GATE_KEYS held one entry whose value was the
    # "both" default, so emptying the whole mapping left both coverage tests
    # green. Each entry must be the thing keeping a real gap named.
    for key in SINGLE_GATE_KEYS:
        without = {k: v for k, v in SINGLE_GATE_KEYS.items() if k != key}
        assert any(item.startswith(f"{key}:") for item in _keys_with_no_reader(without)), (
            f"SINGLE_GATE_KEYS[{key!r}] exempts nothing: the key has a reader in both gates, "
            "so the entry states a division of labour that is not happening"
        )


def test_the_coverage_list_names_every_shipped_key():
    # The assertion above iterates policy.json, so it cannot miss a key. This
    # pins the opposite direction: SINGLE_GATE_KEYS must not name a key the
    # policy no longer has, which would leave a stale exemption in place.
    stale = set(SINGLE_GATE_KEYS) - set(_policy_plan_keys())
    assert not stale, f"SINGLE_GATE_KEYS names keys policy.json does not have: {sorted(stale)}"
