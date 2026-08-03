"""Tests for plan_verify.py — the Python twin of ts/plan/schema.ts.

Shape gate only (containment and markdown are later phases). The cases mirror
ts/plan/schema.test.ts deliberately: the two gates read the same policy.json,
and a plan one admits and the other rejects is a defect in one of them. Where
the TS suite pins a closure from ADR-0004, the same case appears here with the
same name, so a divergence shows up as a one-sided test change in review.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "smtithy"))

from plan_verify import (  # noqa: E402
    check_plan_cardinality,
    check_plan_containment,
    check_plan_ordering,
    check_plan_schema,
    glob_to_regexp,
    matches_denylist,
    tree_content_source,
    verify_plan,
)
from verify import Rejection  # noqa: E402

POLICY = json.loads(
    (Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text()
)
PLAN_POLICY = POLICY["plan"]


def patch_step(step_id="s0", path="src/a.py", old="a", new="b"):
    return {"id": step_id, "kind": "patch", "args": {"path": path, "old": old, "new": new}}


def suggest_step(step_id="s0", path="src/a.py"):
    return {
        "id": step_id,
        "kind": "suggest",
        "args": {"path": path, "line": 1, "old": "a", "new": "b", "note": "n"},
    }


def push_step(step_id="s1", name="smtithy/fix-x"):
    return {"id": step_id, "kind": "push_branch", "args": {"name": name}}


def open_pr_step(step_id="s2", branch="smtithy/fix-x", title="t", body="the fix"):
    return {"id": step_id, "kind": "open_pr", "args": {"branch": branch, "title": title, "body": body}}


def label_step(step_id="s3", name="needs-tests"):
    return {"id": step_id, "kind": "label", "args": {"name": name}}


def _full_policy():
    """POLICY with a populated link allowlist, for cases that run the whole
    driver: the shipped list is empty, so open_pr.body's markdown check would
    reject any link and every case would pass for that one reason."""
    policy = copy.deepcopy(POLICY)
    policy["markdown"]["link_host_allowlist"] = ["docs.example.com"]
    return policy


def valid_plan():
    return {"steps": [patch_step(), push_step()]}


class TestPlanShape:
    def test_a_valid_plan_passes(self):
        check_plan_schema(valid_plan(), PLAN_POLICY)

    def test_non_object_rejects(self):
        for candidate in (None, [], "steps", 42):
            with pytest.raises(Rejection, match="expected a JSON object"):
                check_plan_schema(candidate, PLAN_POLICY)

    def test_missing_steps_rejects(self):
        with pytest.raises(Rejection, match="missing steps"):
            check_plan_schema({}, PLAN_POLICY)

    def test_version_field_is_an_unexpected_key_not_a_feature(self):
        # ADR-0004's third closure: a model-supplied schema version is a
        # model-selected policy. Not special-cased — just unexpected.
        plan = valid_plan()
        plan["version"] = 2
        with pytest.raises(Rejection, match=r"unexpected keys \['version'\]"):
            check_plan_schema(plan, PLAN_POLICY)

    def test_empty_steps_rejects_as_a_visible_failure(self):
        # An empty plan is a remediation that would silently do nothing.
        with pytest.raises(Rejection, match="empty"):
            check_plan_schema({"steps": []}, PLAN_POLICY)

    def test_max_steps_is_enforced(self):
        steps = [patch_step(f"s{i}") for i in range(PLAN_POLICY["max_steps"] + 1)]
        with pytest.raises(Rejection, match="exceeds max_steps"):
            check_plan_schema({"steps": steps}, PLAN_POLICY)


class TestStepShape:
    def test_unknown_kind_rejects_whole_plan_and_names_the_universe(self):
        # Allowlist, not denylist: an unknown kind is a request the harness
        # does not understand, never a skippable no-op.
        plan = {"steps": [{"id": "s0", "kind": "run_tests", "args": {}}]}
        with pytest.raises(Rejection, match="not a declared step kind") as exc:
            check_plan_schema(plan, PLAN_POLICY)
        for kind in ("patch", "push_branch", "open_pr", "label", "suggest"):
            assert kind in str(exc.value)

    @pytest.mark.parametrize(
        "kind", ["toString", "constructor", "__proto__", "valueOf", "hasOwnProperty", "items", "get"]
    )
    def test_inherited_and_dunder_names_are_unknown_kinds(self, kind):
        # `kind in step_kinds` is an own-key test on a dict, so these are already
        # unknown here. Mirrors ts/plan/schema.test.ts, where a plain-object
        # lookup resolved Object.prototype names and threw a TypeError instead.
        plan = {"steps": [{"id": "s0", "kind": kind, "args": {}}]}
        with pytest.raises(Rejection, match="not a declared step kind"):
            check_plan_schema(plan, PLAN_POLICY)

    def test_extra_step_keys_reject(self):
        step = patch_step()
        step["when"] = "always"
        with pytest.raises(Rejection, match=r"unexpected keys \['when'\]"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_missing_id_kind_or_args_rejects(self):
        for dropped in ("id", "kind", "args"):
            step = patch_step()
            del step[dropped]
            with pytest.raises(Rejection, match="missing keys"):
                check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_duplicate_ids_reject(self):
        with pytest.raises(Rejection, match="duplicate id"):
            check_plan_schema({"steps": [patch_step("s0"), push_step("s0")]}, PLAN_POLICY)

    @pytest.mark.parametrize("bad_id", ["", "S0", "0s", "s-0", "x" * 41, 7, None])
    def test_id_grammar_is_conservative(self, bad_id):
        # Ids appear in audit output and counterexamples; the grammar must
        # match ts/plan/schema.ts's ID_RE exactly.
        step = patch_step()
        step["id"] = bad_id
        with pytest.raises(Rejection, match="short lowercase identifier"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)


class TestArgs:
    def test_binding_shaped_arg_rejects_and_names_the_closure(self):
        # ADR-0004's second closure: an execution-time binding arrives as an
        # object where a scalar is expected. The message must point at
        # argument_forms, not read like a type typo.
        step = patch_step()
        step["args"]["path"] = {"$ref": "step1.output"}
        with pytest.raises(Rejection, match="argument_forms"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_array_arg_rejects_the_same_way(self):
        step = patch_step()
        step["args"]["old"] = ["a", "b"]
        with pytest.raises(Rejection, match="argument_forms"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_extra_args_reject(self):
        step = patch_step()
        step["args"]["mode"] = "0644"
        with pytest.raises(Rejection, match=r"unexpected keys \['mode'\]"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_missing_args_reject(self):
        step = patch_step()
        del step["args"]["old"]
        with pytest.raises(Rejection, match=r"missing keys \['old'\]"):
            check_plan_schema({"steps": [step]}, PLAN_POLICY)

    def test_scalar_specs_are_enforced_via_check_scalar(self):
        # One case per spec facet; check_scalar itself is covered by the
        # artifact suite. Length is measured on NFC there, so it is here too.
        too_long = patch_step(path="p" * 501)
        with pytest.raises(Rejection, match="max_length"):
            check_plan_schema({"steps": [too_long]}, PLAN_POLICY)

        bad_pattern = push_step(name="-starts-with-dash")
        with pytest.raises(Rejection, match="pattern"):
            check_plan_schema({"steps": [bad_pattern]}, PLAN_POLICY)

        bad_int = suggest_step()
        bad_int["args"]["line"] = 0
        with pytest.raises(Rejection, match="below minimum"):
            check_plan_schema({"steps": [bad_int]}, PLAN_POLICY)

        bool_is_not_int = suggest_step()
        bool_is_not_int["args"]["line"] = True
        with pytest.raises(Rejection, match="expected integer"):
            check_plan_schema({"steps": [bool_is_not_int]}, PLAN_POLICY)

    def test_suggest_step_passes_whole(self):
        check_plan_schema({"steps": [suggest_step()]}, PLAN_POLICY)

    # JSON permits \ud800 and both parsers accept it, so a plan can carry a
    # string no UTF-8 encoder will take. The containment phase encodes, the
    # transcript encodes, and neither raises Rejection: the shape gate is where
    # a plan that cannot be encoded stops being well-formed. Mirrors
    # ts/plan/schema.test.ts.

    @pytest.mark.parametrize("bad", ["\ud800", "a\udfffb", "x\ud83d"])
    def test_an_unpaired_surrogate_is_a_shape_violation(self, bad):
        with pytest.raises(Rejection, match="unpaired surrogate"):
            check_plan_schema({"steps": [patch_step(new=bad)]}, PLAN_POLICY)

    def test_an_unpaired_surrogate_in_any_string_arg_rejects(self):
        with pytest.raises(Rejection, match="unpaired surrogate"):
            check_plan_schema({"steps": [open_pr_step(title="Fix \ud800 it")]}, PLAN_POLICY)

    def test_an_astral_character_is_not_a_surrogate(self):
        # Calibration: an emoji IS a surrogate pair in UTF-16 and a single code
        # point in Python, and it encodes fine. A check reading UTF-16 units
        # would refuse it.
        check_plan_schema({"steps": [patch_step(new="🎉 fixed\n")]}, PLAN_POLICY)


class TestShippedPolicyAgreement:
    """The Python gate must enforce the SHIPPED policy, not a test double.
    These pin the policy facts plan_verify relies on, mirroring
    ts/plan/shipped-policy.test.ts so drift in policy.json breaks both suites,
    not just the one that happens to load it first."""

    def test_step_kind_universe(self):
        assert sorted(PLAN_POLICY["step_kinds"]) == ["label", "open_pr", "patch", "push_branch", "suggest"]

    def test_suggest_is_not_write_class(self):
        # ADR-0009: a suggestion becomes a review comment the contributor
        # applies; it adds no write-class action to the §2.5 count.
        assert PLAN_POLICY["step_kinds"]["suggest"]["write_class"] is False

    def test_open_pr_has_no_base_argument(self):
        # ADR-0009 addendum: the follow-up PR is STACKED on the reviewed PR's
        # own head branch, and the executor sets that base from PR context. A
        # `base` arg here would make the merge target model-suppliable — the
        # same banned move as a model-selected policy version — so the arg set
        # is pinned exactly. Mirrored in ts/plan/shipped-policy.test.ts.
        assert sorted(PLAN_POLICY["step_kinds"]["open_pr"]["args"]) == ["body", "branch", "title"]

    def test_every_string_arg_is_markdown_checked_or_charset_constrained(self):
        # verify.py's markdown_fields rule, applied to plan args, with the hole
        # that made open_pr.title ungated closed: "has a pattern" was sufficient,
        # and `[^\r\n]+` is a pattern that excludes two code points and admits
        # every other one — including the U+202E that makes a title read as an
        # approval. ADR-0011 makes invisible and bidirectional controls an
        # unconditional invariant on posted text and plan text inherits it, so a
        # NEGATED class is not a constraint for the property that matters. An arg
        # is gated by the markdown allowlist or by a pattern that allowlists its
        # characters; old/new are file bytes, never rendered as prose.
        for kind, spec in PLAN_POLICY["step_kinds"].items():
            for arg_name, arg_spec in spec["args"].items():
                if arg_spec["type"] != "string" or arg_name in ("old", "new"):
                    continue
                if arg_spec.get("markdown"):
                    continue
                pattern = arg_spec.get("pattern")
                assert pattern, f"{kind}.{arg_name}: string arg with no markdown flag and no pattern"
                assert "[^" not in pattern, (
                    f"{kind}.{arg_name}: pattern {pattern!r} is a negated class, which allowlists "
                    "nothing — either give it a charset or mark it markdown"
                )

    def test_old_and_new_are_exempt_because_they_are_never_rendered(self):
        # patch/suggest old+new are file bytes, not prose: old must byte-match
        # the tree (ADR-0005 anchoring) and new is code whose only gate is the
        # human merge. Pinned so the exemption above stays a named decision.
        for kind in ("patch", "suggest"):
            args = PLAN_POLICY["step_kinds"][kind]["args"]
            assert "pattern" not in args["old"] and not args["old"].get("markdown")
            assert "pattern" not in args["new"] and not args["new"].get("markdown")

    def test_branch_prefix_is_a_harness_owned_namespace(self):
        # The one argument that decides where `contents: write` is pointed. A
        # prefix is what makes "not the default branch, not the contributor's
        # head branch" a property of the NAME rather than a list of exclusions.
        # Mirrored in ts/plan/shipped-policy.test.ts.
        assert PLAN_POLICY["branch_prefix"] == "smtithy/"

    def test_bounding_is_declared_in_both_dimensions(self):
        # ADR-0005's bounding half. A line count bounds nothing about line
        # LENGTH, so a byte budget ships alongside it; mirrored in
        # ts/plan/shipped-policy.test.ts.
        assert PLAN_POLICY["max_changed_lines"] >= 1
        assert PLAN_POLICY["max_changed_bytes"] >= 1
        assert PLAN_POLICY["max_plan_changed_bytes"] >= PLAN_POLICY["max_changed_bytes"]

    def test_the_plan_byte_budget_bounds_more_than_the_per_step_cap(self):
        # A plan cap at or above max_steps x per-step is not a cap: several steps
        # may share one file, so max_patched_files does not bound the sum either.
        assert PLAN_POLICY["max_plan_changed_bytes"] < PLAN_POLICY["max_changed_bytes"] * PLAN_POLICY["max_steps"]

    def test_the_byte_budget_admits_the_patch_a_real_fix_needs(self):
        # Fail-closed must not mean useless: a per-step budget below the largest
        # single-line target the arg spec accepts would make the caps
        # unsatisfiable for a plausible fix. The `old` arg caps at 20000
        # characters, so a budget under that is deliberate — the point is that a
        # 40 KB substitution is not a "smallest correct fix" (prompts/ai-pr-plan).
        assert PLAN_POLICY["max_changed_bytes"] < PLAN_POLICY["step_kinds"]["patch"]["args"]["old"]["max_length"] * 2

    def test_label_allowlist_ships_empty(self):
        # link_host_allowlist's precedent (ADR-0010's too): a label is a control
        # surface — this repo's evals workflow triggers on `run-evals` — so a
        # plan can apply none until a consumer names the ones it accepts.
        assert PLAN_POLICY["label_allowlist"] == []


class TestReservedClosures:
    """ADR-0004's reservations refuse their shape TODAY, or they are not
    reservations. control_flow ships empty and argument_forms literal-only, and
    both gates must REFUSE a policy that widens them rather than reading the flag
    and carrying on — a branch step the verifier treats as an ordinary
    straight-line step is a branch nothing reasons about.

    Mirrored in ts/plan/policy.test.ts: the prover's ordering and frame proofs
    quantify over plan positions, which is a claim about a straight-line plan.
    """

    def test_shipped_policy_reserves_both(self):
        assert PLAN_POLICY["control_flow"] == []
        assert PLAN_POLICY["argument_forms"] == ["literal"]

    def test_a_policy_declaring_control_flow_is_refused(self):
        policy = copy.deepcopy(PLAN_POLICY)
        policy["control_flow"] = ["branch"]
        policy["step_kinds"]["branch"] = {
            "write_class": False,
            "args": {"cond": {"type": "string", "min_length": 1, "max_length": 100, "pattern": "[a-z]+"}},
        }
        with pytest.raises(Rejection, match="control_flow"):
            check_plan_schema({"steps": [patch_step()]}, policy)

    def test_a_policy_declaring_another_argument_form_is_refused(self):
        # The prover already refuses this (checkPlanPolicy: "this prover only
        # implements [\"literal\"]"); the Python gate read the key nowhere, so a
        # widened policy would have been enforced by one gate and not the other.
        policy = copy.deepcopy(PLAN_POLICY)
        policy["argument_forms"] = ["literal", "binding"]
        with pytest.raises(Rejection, match="argument_forms"):
            check_plan_schema({"steps": [patch_step()]}, policy)

    def test_the_refusal_precedes_any_step_check(self):
        # A policy this gate cannot implement must not be reported as a bad plan:
        # the reason has to name the policy, not the model's output.
        policy = copy.deepcopy(PLAN_POLICY)
        policy["control_flow"] = ["branch"]
        with pytest.raises(Rejection, match="policy error"):
            check_plan_schema({"steps": [{"id": "nope", "kind": "unknown_kind", "args": {}}]}, policy)

    def test_the_shipped_policy_still_passes(self):
        # The reservations must refuse a WIDENED policy, not the shipped one.
        check_plan_schema({"steps": [patch_step()]}, PLAN_POLICY)


class TestMutationDiscipline:
    def test_gate_rejects_are_not_order_dependent_smoke(self):
        # A quick differential guard: shuffling which violation comes first
        # must still reject. (Full adversarial corpus is a separate file, per
        # the artifact verifier's precedent.)
        plan = copy.deepcopy(valid_plan())
        plan["steps"][0]["args"]["path"] = {"$ref": "x"}
        plan["steps"][1]["id"] = plan["steps"][0]["id"]
        with pytest.raises(Rejection):
            check_plan_schema(plan, PLAN_POLICY)


# --------------------------------------------- containment (ADR-0005) ------
#
# Fixtures for the containment phase: a diff, its changed files, and a content
# source. The content source is a plain dict lookup — verify_plan takes a
# callable precisely so tests need no filesystem.

PLAN_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
-def load():
+def load(path):
+    check(path)
     return os.environ
diff --git a/src/util.py b/src/util.py
index 3333333..4444444 100644
--- a/src/util.py
+++ b/src/util.py
@@ -1,2 +1,2 @@
-def check():
+def check(path):
     pass
"""

PLAN_CHANGED_FILES = ["src/app.py", "src/util.py"]

PLAN_TREE = {
    "src/app.py": b"import os\ndef load(path):\n    check(path)\n    return os.environ\n",
    "src/util.py": b"def check(path):\n    pass\n",
}


def tree_source(tree=None):
    tree = PLAN_TREE if tree is None else tree

    def read(path: str) -> bytes:
        if path not in tree:
            raise FileNotFoundError(path)
        return tree[path]

    return read


def contained(plan, **overrides):
    kwargs = dict(
        diff_text=PLAN_DIFF,
        changed_files=PLAN_CHANGED_FILES,
        policy_plan=PLAN_POLICY,
        content_source=tree_source(),
    )
    kwargs.update(overrides)
    return check_plan_containment(plan, **kwargs)


def anchored_patch(step_id="s0", path="src/app.py", old="def load(path):\n", new="def load(path=None):\n"):
    return {"id": step_id, "kind": "patch", "args": {"path": path, "old": old, "new": new}}


def anchored_suggest(step_id="s0", path="src/app.py", line=2, old="def load(path):\n",
                     new="def load(path=None):\n", note="make path optional"):
    return {
        "id": step_id,
        "kind": "suggest",
        "args": {"path": path, "line": line, "old": old, "new": new, "note": note},
    }


class TestGlobSemantics:
    """The denylist matcher's semantics, pinned in both directions — the §17
    lesson is that a pattern is enforced exactly as written, so what the
    writing means must be tested, not assumed. Mirrors prove.test.ts's
    globToRegExp block case for case."""

    def test_double_star_spans_separators(self):
        assert glob_to_regexp(".github/**").fullmatch(".github/workflows/ci.yml")

    def test_double_star_slash_also_matches_zero_directories(self):
        assert glob_to_regexp("**/*.pem").fullmatch("key.pem")
        assert glob_to_regexp("**/*.pem").fullmatch("certs/deep/key.pem")
        assert glob_to_regexp(".github/**").fullmatch(".github/x")

    def test_single_star_does_not_span_separators(self):
        assert glob_to_regexp("src/*.py").fullmatch("src/a.py")
        assert not glob_to_regexp("src/*.py").fullmatch("src/nested/a.py")

    def test_a_dot_is_a_literal_dot(self):
        # fnmatch would pass both of these too, but the reason this matcher
        # exists is that fnmatch treats ** as *; the dot is where a naive
        # regex TRANSLATION goes wrong instead.
        assert not glob_to_regexp("**/*.pem").fullmatch("keyXpem")
        assert not glob_to_regexp(".github/**").fullmatch("Xgithub/x.yml")

    def test_anchored_both_ends(self):
        assert not glob_to_regexp("src/*.py").fullmatch("prefix/src/a.py")
        assert not glob_to_regexp(".github/**").fullmatch("vendor/.github/x")

    def test_matching_is_case_sensitive(self):
        # fnmatch is case-insensitive on some platforms; paths in a git tree
        # are not, and neither is the TS matcher.
        assert not glob_to_regexp("**/*.pem").fullmatch("cert.PEM")

    def test_matches_denylist_names_the_pattern(self):
        assert matches_denylist(".github/workflows/ci.yml", PLAN_POLICY["path_denylist"]) == ".github/**"
        assert matches_denylist("src/a.py", PLAN_POLICY["path_denylist"]) is None


class TestFrame:
    def test_anchored_plan_within_frame_passes(self):
        contained({"steps": [anchored_patch(), push_step("s1")]})

    def test_patch_outside_changed_files_rejects(self):
        with pytest.raises(Rejection, match="not a file this PR touched"):
            contained({"steps": [anchored_patch(path="src/evil.py")]})

    def test_suggest_outside_changed_files_rejects(self):
        # ADR-0009: suggest binds to the frame exactly as patch does.
        with pytest.raises(Rejection, match="not a file this PR touched"):
            contained({"steps": [anchored_suggest(path="src/evil.py")]})

    def test_non_anchored_kinds_are_exempt(self):
        # push_branch/open_pr/label carry no file path; a frame check that
        # tripped over them would reject every complete plan.
        contained({"steps": [push_step("s1")]})


class TestDenylist:
    def test_denylisted_path_rejects_even_when_changed(self):
        # The denylist narrows changed_files: the PR touching a workflow file
        # does not make it patchable.
        with pytest.raises(Rejection, match="path denylist"):
            contained(
                {"steps": [anchored_patch(path=".github/workflows/ci.yml")]},
                changed_files=[".github/workflows/ci.yml"],
            )


class TestSuggestLineProvenance:
    def test_line_inside_a_hunk_passes(self):
        contained({"steps": [anchored_suggest(line=2)]})

    def test_line_outside_any_hunk_rejects(self):
        with pytest.raises(Rejection, match="not inside any diff hunk"):
            contained({"steps": [anchored_suggest(line=400)]})

    def test_line_in_another_files_hunk_rejects(self):
        # src/util.py has lines 1-2 in hunks; src/app.py's hunk covers 1-4.
        # Line 2 exists in both, so probe with a line only app.py has.
        with pytest.raises(Rejection, match="not inside any diff hunk"):
            contained(
                {"steps": [anchored_suggest(path="src/util.py", line=4,
                                            old="def check(path):\n", new="def check(path=None):\n")]}
            )

    # `old` proves the model read SOME bytes; `line` decides which bytes the
    # suggestion block overwrites. ADR-0009 addendum: they must be one region.

    def test_old_at_a_different_in_hunk_line_rejects(self):
        # `    return os.environ\n` is unique in src/app.py, at line 4. Line 2
        # is in a hunk too, so both existing checks pass — and the suggestion
        # would replace `def load(path):`, deleting the function signature.
        with pytest.raises(Rejection, match="anchored at line 4"):
            contained({"steps": [anchored_suggest(line=2, old="    return os.environ\n",
                                                  new="    return {}\n")]})

    def test_old_at_the_named_line_passes(self):
        contained({"steps": [anchored_suggest(line=4, old="    return os.environ\n",
                                              new="    return {}\n")]})

    def test_multi_line_old_spanning_in_hunk_lines_passes(self):
        # Lines 2-3, both in src/app.py's hunk set.
        contained({"steps": [anchored_suggest(line=2, old="def load(path):\n    check(path)\n",
                                              new="def load(path=None):\n")]})

    def test_multi_line_old_addressed_at_the_wrong_line_rejects(self):
        with pytest.raises(Rejection, match="anchored at line 2"):
            contained({"steps": [anchored_suggest(line=3, old="def load(path):\n    check(path)\n",
                                                  new="def load(path=None):\n")]})

    def test_multi_line_old_running_out_of_the_hunk_rejects(self):
        # src/util.py's hunk covers lines 1-2 only, and `old` spans 1-2 there,
        # so probe the boundary on a tree whose file is longer than its hunk.
        tree = {"src/util.py": b"def check(path):\n    pass\n    extra()\n"}
        with pytest.raises(Rejection, match="is not inside any diff hunk"):
            contained(
                {"steps": [anchored_suggest(path="src/util.py", line=2,
                                            old="    pass\n    extra()\n", new="    return\n")]},
                content_source=tree_source(tree),
            )

    def test_old_not_starting_at_a_line_boundary_rejects(self):
        # A fragment starting mid-line has no line to be addressed AT: the
        # suggestion block replaces whole lines, so a sub-line anchor cannot
        # describe what would be overwritten.
        with pytest.raises(Rejection, match="does not start at the beginning of a line"):
            contained({"steps": [anchored_suggest(line=2, old="load(path):\n",
                                                  new="load(path=None):\n")]})

    def test_old_not_ending_at_a_line_boundary_rejects(self):
        # `def load` starts at line 2 and stops mid-line, so the replaced range
        # extends past the anchor: applying it deletes `(path):`, bytes `old`
        # never named.
        with pytest.raises(Rejection, match="does not end at the end of a line"):
            contained({"steps": [anchored_suggest(line=2, old="def load", new="def safe")]})

    def test_multi_line_old_ending_mid_line_rejects(self):
        with pytest.raises(Rejection, match="does not end at the end of a line"):
            contained({"steps": [anchored_suggest(line=2, old="def load(path):\n    check",
                                                  new="def safe(path):\n    ok")]})

    def test_old_ending_at_end_of_file_without_a_newline_passes(self):
        # The last line of a file with no final newline ends at a line boundary
        # — there is no byte after it to overwrite.
        tree = {"src/app.py": b"import os\ndef load(path):\n    check(path)\n    return os.environ"}
        contained(
            {"steps": [anchored_suggest(line=4, old="    return os.environ",
                                        new="    return dict(os.environ)")]},
            content_source=tree_source(tree),
        )


class TestBounding:
    def test_at_cap_distinct_files_pass(self):
        changed = [f"src/f{i}.py" for i in range(PLAN_POLICY["max_patched_files"])]
        tree = {path: b"anchor\n" for path in changed}
        steps = [anchored_patch(f"s{i}", path=path, old="anchor\n", new="fixed\n")
                 for i, path in enumerate(changed)]
        contained({"steps": steps}, changed_files=changed, content_source=tree_source(tree))

    def test_one_file_over_cap_rejects(self):
        count = PLAN_POLICY["max_patched_files"] + 1
        changed = [f"src/f{i}.py" for i in range(count)]
        steps = [anchored_patch(f"s{i}", path=path) for i, path in enumerate(changed)]
        with pytest.raises(Rejection, match="max_patched_files"):
            contained({"steps": steps}, changed_files=changed)

    def test_file_count_is_distinct_paths_not_steps(self):
        # max_patched_files bounds FILES; several suggestions into one file
        # are one file. (One-suggestion-per-file is the executor's delivery
        # rule, not this bound.)
        steps = [
            anchored_patch("s0", old="import os\n", new="import os, sys\n"),
            anchored_patch("s1", old="    return os.environ\n", new="    return dict(os.environ)\n"),
        ]
        contained({"steps": steps})

    def test_changed_lines_at_cap_passes_and_cap_plus_one_rejects(self):
        cap = PLAN_POLICY["max_changed_lines"]
        # old contributes 1 line; new brings the step's total to the cap.
        at_cap = "x\n" * (cap - 1)
        tree = {"src/app.py": PLAN_TREE["src/app.py"]}
        contained({"steps": [anchored_patch(new=at_cap)]}, content_source=tree_source(tree))
        with pytest.raises(Rejection, match="max_changed_lines"):
            contained({"steps": [anchored_patch(new=at_cap + "y\n")]}, content_source=tree_source(tree))

    def test_changed_lines_count_both_sides(self):
        # diff --stat's number: removed plus added, so a rewrite counts twice
        # what its longer side alone would.
        cap = PLAN_POLICY["max_changed_lines"]
        half = "x\n" * (cap // 2 + 1)
        tree = {"src/app.py": half.encode()}
        with pytest.raises(Rejection, match="max_changed_lines"):
            contained({"steps": [anchored_patch(old=half, new=half.replace("x", "y"))]},
                      content_source=tree_source(tree))

    # The line count is blind to line LENGTH, so "bounded" has to hold in both
    # dimensions or a minified/generated line — a plausible real patch target —
    # carries an arbitrarily large substitution through a cap of 120.

    def test_a_long_single_line_rewrite_is_bounded_by_bytes(self):
        cap = PLAN_POLICY["max_changed_bytes"]
        old = "x" * cap
        new = "y" * cap
        tree = {"src/app.py": old.encode() + b"\n"}
        with pytest.raises(Rejection, match="max_changed_bytes"):
            contained({"steps": [anchored_patch(old=old, new=new)]}, content_source=tree_source(tree))

    def test_changed_bytes_at_cap_passes_and_cap_plus_one_rejects(self):
        cap = PLAN_POLICY["max_changed_bytes"]
        old = "def load(path):\n"
        at_cap = "y" * (cap - len(old.encode()))
        tree = {"src/app.py": PLAN_TREE["src/app.py"]}
        contained({"steps": [anchored_patch(new=at_cap)]}, content_source=tree_source(tree))
        with pytest.raises(Rejection, match="max_changed_bytes"):
            contained({"steps": [anchored_patch(new=at_cap + "y")]}, content_source=tree_source(tree))

    def test_changed_bytes_count_both_sides(self):
        # Same diff --stat reading as the line cap: old plus new, so a rewrite
        # cannot spend the whole budget twice.
        cap = PLAN_POLICY["max_changed_bytes"]
        half = "x" * (cap // 2 + 1)
        tree = {"src/app.py": half.encode() + b"\n"}
        with pytest.raises(Rejection, match="max_changed_bytes"):
            contained({"steps": [anchored_patch(old=half, new=half.replace("x", "y"))]},
                      content_source=tree_source(tree))

    def test_changed_bytes_are_utf8_bytes_not_code_points(self):
        # The budget bounds what reaches the file, and a file holds bytes: a
        # 3-byte code point must cost 3. Measured in code points, this content
        # would be a third of its real size.
        cap = PLAN_POLICY["max_changed_bytes"]
        new = "一" * (cap // 3 + 1)  # 3 UTF-8 bytes each
        tree = {"src/app.py": PLAN_TREE["src/app.py"]}
        with pytest.raises(Rejection, match="max_changed_bytes"):
            contained({"steps": [anchored_patch(new=new)]}, content_source=tree_source(tree))

    def test_the_plan_total_is_bounded_across_steps(self):
        # Per-step alone is not a bound on the plan: max_patched_files x
        # max_changed_bytes would be the real ceiling, and several steps may
        # share one file. The plan budget is what makes "bounded" a property of
        # the delivery rather than of one step.
        cap = PLAN_POLICY["max_plan_changed_bytes"]
        per_step = PLAN_POLICY["max_changed_bytes"]
        assert cap < per_step * PLAN_POLICY["max_steps"], "a plan cap at or above N*per-step bounds nothing new"
        filler = "z" * (per_step - 200)
        anchors = [f"anchor{i}\n" for i in range(cap // per_step + 2)]
        tree = {"src/app.py": "".join(anchors).encode()}
        steps = [anchored_patch(f"s{i}", old=anchor, new=filler + f"\n{i}\n")
                 for i, anchor in enumerate(anchors)]
        with pytest.raises(Rejection, match="max_plan_changed_bytes"):
            contained({"steps": steps}, content_source=tree_source(tree))


class TestAnchoring:
    def test_old_matching_the_reviewed_tree_passes(self):
        contained({"steps": [anchored_patch()]})

    def test_old_not_in_the_file_rejects(self):
        with pytest.raises(Rejection, match="does not byte-match"):
            contained({"steps": [anchored_patch(old="def load():\n")]})

    def test_missing_file_rejects_as_unreadable(self):
        with pytest.raises(Rejection, match="cannot read"):
            contained({"steps": [anchored_patch()]}, content_source=tree_source({}))

    def test_ambiguous_anchor_rejects(self):
        tree = {"src/app.py": b"pass\npass\n"}
        with pytest.raises(Rejection, match="ambiguous"):
            contained({"steps": [anchored_patch(old="pass\n")]}, content_source=tree_source(tree))

    def test_overlapping_occurrences_are_ambiguous(self):
        # bytes.count() counts NON-OVERLAPPING occurrences, so `aa` in `xaaay`
        # counts 1 while it really begins at two offsets. str.replace picks the
        # first; the executor would be applying a write the verifier called
        # unique, which is the guess exactly-once exists to refuse.
        tree = {"src/app.py": b"xaaay\n"}
        with pytest.raises(Rejection, match="ambiguous"):
            contained({"steps": [anchored_patch(old="aa", new="bb")]}, content_source=tree_source(tree))

    def test_overlapping_occurrences_are_ambiguous_at_apply_time_too(self):
        # The pending-content half of the same count: `aa` is unique in the file
        # at the reviewed SHA and unique by count() after s0 applies, but s0's
        # `new` gives it two overlapping start offsets, so the write s1 describes
        # is ambiguous where it will actually land.
        tree = {"src/app.py": b"aa\n"}
        with pytest.raises(Rejection, match="ambiguous"):
            contained({"steps": [
                anchored_patch("s0", old="aa\n", new="xaaay\n"),
                anchored_patch("s1", old="aa", new="q"),
            ]}, content_source=tree_source(tree))

    def test_a_genuinely_unique_anchor_still_passes(self):
        # The property the overlap count must not break: one occurrence stays one.
        contained({"steps": [anchored_patch()]})

    # Each step's `old` was matched against the file at the reviewed SHA, so
    # exactly-once held for the FIRST step applied to a path and no later one.
    # The rejection message promises the executor an unambiguous application.

    def test_two_disjoint_patches_into_one_file_still_pass(self):
        # The calibration case: sequential application is fine when the regions
        # do not interact, and this must stay legal.
        contained({"steps": [
            anchored_patch("s0", old="import os\n", new="import os, sys\n"),
            anchored_patch("s1", old="    return os.environ\n", new="    return dict(os.environ)\n"),
        ]})

    def test_a_step_whose_new_duplicates_a_later_anchor_rejects(self):
        # Each `old` occurs exactly once pre-plan; after s0 applies, s1's anchor
        # occurs twice and its replacement is ambiguous at apply time.
        with pytest.raises(Rejection, match="ambiguous"):
            contained({"steps": [
                anchored_patch("s0", old="    return os.environ\n",
                               new="    return os.environ\n    return os.environ\n"),
                anchored_patch("s1", old="    return os.environ\n", new="    return {}\n"),
            ]})

    def test_a_step_that_destroys_a_later_anchor_rejects(self):
        # s0 replaces a block containing s1's anchor, so by the time s1 applies
        # its `old` is gone.
        with pytest.raises(Rejection, match="no longer"):
            contained({"steps": [
                anchored_patch("s0", old="def load(path):\n    check(path)\n", new="def load():\n"),
                anchored_patch("s1", old="    check(path)\n", new="    check(path, strict=True)\n"),
            ]})

    def test_two_steps_with_the_same_old_on_one_path_reject(self):
        # Both anchor the same region: whichever applies first removes the
        # other's anchor.
        with pytest.raises(Rejection, match="no longer|ambiguous"):
            contained({"steps": [
                anchored_patch("s0", old="    check(path)\n", new="    check(path, 1)\n"),
                anchored_patch("s1", old="    check(path)\n", new="    check(path, 2)\n"),
            ]})

    def test_a_step_may_not_anchor_on_text_an_earlier_step_invented(self):
        # The simulation narrows what is anchorable, never widens it: `old` must
        # still byte-match the reviewed SHA (ADR-0005), so text a previous step's
        # `new` introduced is content the model never read.
        with pytest.raises(Rejection, match="does not byte-match"):
            contained({"steps": [
                anchored_patch("s0", old="def load(path):\n", new="def introduced():\n"),
                anchored_patch("s1", old="def introduced():\n", new="y\n"),
            ]})

    def test_steps_on_different_paths_do_not_interact(self):
        contained({"steps": [
            anchored_patch("s0", old="def load(path):\n", new="def load(path=None):\n"),
            anchored_patch("s1", path="src/util.py", old="def check(path):\n",
                           new="def check(path=None):\n"),
        ]})

    def test_anchoring_runs_after_the_frame(self):
        # An out-of-frame path must reject as out-of-frame, not reach the
        # content source: the filesystem is only consulted for paths that
        # already passed the closed-set checks.
        def exploding(path):
            raise AssertionError(f"content source consulted for {path!r}")

        with pytest.raises(Rejection, match="not a file this PR touched"):
            contained({"steps": [anchored_patch(path="src/evil.py")]}, content_source=exploding)


class TestWriteClassTargets:
    """push_branch.name decides where the executor's `contents: write` credential
    points, and it is model-supplied. ADR-0009's addendum: never the default
    branch, never the contributor's own branch."""

    def push(self, name):
        return {"id": "s1", "kind": "push_branch", "args": {"name": name}}

    def test_a_prefixed_branch_passes(self):
        contained({"steps": [anchored_patch("s0"), self.push("smtithy/fix-1"), open_pr_step("s2",
                   branch="smtithy/fix-1")]})

    def test_the_default_branch_rejects(self):
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("main")]})

    def test_an_unprefixed_branch_rejects(self):
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("feature/contributor-branch")]})

    def test_a_prefix_lookalike_rejects(self):
        # Prefix confusion, the near-miss: a namespace that merely STARTS with
        # the same characters is a different namespace.
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy-evil/fix")]})

    def test_a_traversal_out_of_the_namespace_rejects(self):
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy/../main")]})

    def test_open_pr_branch_is_constrained_too(self):
        # open_pr.branch is the head of the follow-up PR: the same target under
        # a different arg name.
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy/ok"),
                                 open_pr_step("s2", branch="main")]})

    def test_the_reviewed_head_branch_rejects_even_when_prefixed(self):
        # The prefix cannot express this one: if the PR's own head branch
        # happens to live in the namespace, pushing to it is still the
        # push-to-contributor's-branch mode ADR-0009's addendum decided against.
        # It is a plan INPUT, so it is threaded in rather than inferred.
        with pytest.raises(Rejection, match="reviewed pull request's own head branch"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy/theirs")]},
                      head_branch="smtithy/theirs")

    def test_the_pull_request_must_open_from_the_branch_that_was_pushed(self):
        # Each branch was confined independently, so both could sit inside the
        # namespace and name DIFFERENT branches. The executor would then push the
        # verified patch to one and open the follow-up PR from another, whose
        # content the plan never described and whose bytes no frame bounded --
        # `smtithy/b` surviving from an earlier command is enough.
        with pytest.raises(Rejection, match="opens from"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy/a"),
                                 open_pr_step("s2", branch="smtithy/b")]})

    def test_the_prefix_message_still_wins_when_the_branch_is_also_unprefixed(self):
        # Ordering: confinement first. An off-namespace branch is a worse fault
        # than a mismatched one, and this is what the reader needs named.
        with pytest.raises(Rejection, match="branch_prefix"):
            contained({"steps": [anchored_patch("s0"), self.push("smtithy/ok"),
                                 open_pr_step("s2", branch="main")]})

    def test_a_push_with_no_open_pr_is_unaffected(self):
        # The relation is only expressible when both steps exist; cardinality
        # deliberately admits this shape.
        contained({"steps": [anchored_patch("s0"), self.push("smtithy/fix-1")]})

    def test_a_non_string_branch_is_named_as_a_shape_fault(self):
        # Shape is the schema phase's business, and it runs first, so a malformed
        # branch must be reported as one rather than as a mismatch. Through
        # verify_plan, because that is where the phase order lives.
        plan = {"steps": [anchored_patch("s0"), self.push("smtithy/a"),
                          open_pr_step("s2", branch="smtithy/a")]}
        plan["steps"][1]["args"]["name"] = 7
        with pytest.raises(Rejection) as caught:
            verify_plan(plan, PLAN_DIFF, PLAN_CHANGED_FILES, _full_policy(), tree_source())
        assert "expected string" in str(caught.value)
        assert "opens from" not in str(caught.value)

    def test_a_label_off_the_allowlist_rejects(self):
        with pytest.raises(Rejection, match="label_allowlist"):
            contained({"steps": [anchored_patch("s0"),
                                 {"id": "s1", "kind": "label", "args": {"name": "run-evals"}}]})

    def test_the_shipped_empty_allowlist_rejects_every_label(self):
        assert PLAN_POLICY["label_allowlist"] == []
        with pytest.raises(Rejection, match="label_allowlist"):
            contained({"steps": [anchored_patch("s0"),
                                 {"id": "s1", "kind": "label", "args": {"name": "anything"}}]})

    def test_an_allowlisted_label_passes(self):
        policy = copy.deepcopy(PLAN_POLICY)
        policy["label_allowlist"] = ["needs-tests"]
        contained({"steps": [anchored_patch("s0"),
                             {"id": "s1", "kind": "label", "args": {"name": "needs-tests"}}]},
                  policy_plan=policy)

    def test_label_matching_is_exact_not_prefix(self):
        policy = copy.deepcopy(PLAN_POLICY)
        policy["label_allowlist"] = ["needs-tests"]
        with pytest.raises(Rejection, match="label_allowlist"):
            contained({"steps": [anchored_patch("s0"),
                                 {"id": "s1", "kind": "label", "args": {"name": "needs-tests-urgently"}}]},
                      policy_plan=policy)


class TestCommandedFindingScope:
    """ADR-0007: the command names ONE finding, and that scope was enforced by
    the prompt alone — verify_plan never saw the finding.

    So a generator steered by text in the head tree (or plain model error) into
    patching the two OTHER files a PR changed, and none of the finding's own,
    produced a plan that verified: every path is in changed_files, none is
    denylisted, the write chain is ordered. The commander asked for auth.py and
    got settings.py and ci_helper.py.

    The property is that the finding's file is AMONG the fix's paths, not that it
    is the only one: ADR-0009 explicitly supports a multi-file fix delivered as a
    stacked PR, so requiring equality would refuse the case the ADR exists for.
    A fix that touches the commanded file plus others is a judgement call a human
    reviews; one that never touches it is not the commanded fix at all.
    """

    FINDING = {"path": "src/app.py", "line": 2, "severity": "high", "title": "t", "body": "b"}

    def test_a_fix_on_the_commanded_file_passes(self):
        contained({"steps": [anchored_patch("s0")]}, commanded_finding=self.FINDING)

    def test_a_fix_that_never_touches_the_commanded_file_rejects(self):
        with pytest.raises(Rejection, match="commanded finding"):
            contained(
                {"steps": [anchored_patch("s0", path="src/util.py", old="def check(path):\n",
                                          new="def check(path=None):\n")]},
                commanded_finding=self.FINDING,
            )

    def test_a_multi_file_fix_including_the_commanded_file_passes(self):
        # ADR-0009's stacked-PR case: the fix spans files, and one of them is the
        # finding's. Refusing this would forbid the delivery mode the ADR adds.
        contained(
            {"steps": [
                anchored_patch("s0"),
                anchored_patch("s1", path="src/util.py", old="def check(path):\n",
                               new="def check(path=None):\n"),
            ]},
            commanded_finding=self.FINDING,
        )

    def test_no_commanded_finding_refuses_nothing_extra(self):
        # The review lane passes no finding; None must not start rejecting plans.
        contained({"steps": [anchored_patch("s0", path="src/util.py", old="def check(path):\n",
                                            new="def check(path=None):\n")]})

    def test_a_plan_with_no_fix_step_is_not_scope_checked_here(self):
        # A plan expressing no fix has no path set to compare, so scope has
        # nothing to say about it; cardinality is what refuses one that does
        # nothing useful (open_pr with no push_branch, and so on).
        contained({"steps": [push_step("s0")]}, commanded_finding=self.FINDING)


class TestWriteChainCardinality:
    """Ordering constrains the write chain's ORDER; nothing constrained how many
    times it appeared. Read from policy's write_class flags rather than a
    hardcoded kind list."""

    def test_the_legal_single_chain_passes(self):
        check_plan_cardinality(
            {"steps": [patch_step("s0"), push_step("s1"), open_pr_step("s2")]}, PLAN_POLICY
        )

    def test_nine_chains_for_one_patch_reject(self):
        # The report's scenario: fits max_steps, ordered correctly (all pushes
        # before all opens), and would produce eighteen external effects.
        steps = [patch_step("s0")]
        steps += [push_step(f"p{i}", name=f"smtithy/b{i}") for i in range(9)]
        steps += [open_pr_step(f"o{i}", branch=f"smtithy/b{i}") for i in range(9)]
        # Kinds are checked in sorted order, so open_pr is the first violation.
        with pytest.raises(Rejection, match="9 open_pr steps"):
            check_plan_cardinality({"steps": steps}, PLAN_POLICY)

    def test_two_push_branches_reject(self):
        with pytest.raises(Rejection, match="push_branch"):
            check_plan_cardinality(
                {"steps": [patch_step("s0"), push_step("s1"), push_step("s2", name="smtithy/other"),
                           open_pr_step("s3")]},
                PLAN_POLICY,
            )

    def test_two_open_prs_reject(self):
        with pytest.raises(Rejection, match="open_pr"):
            check_plan_cardinality(
                {"steps": [patch_step("s0"), push_step("s1"), open_pr_step("s2"),
                           open_pr_step("s3", branch="smtithy/other")]},
                PLAN_POLICY,
            )

    def test_open_pr_without_a_push_rejects(self):
        # Nothing to open a PR from: the branch was never pushed.
        with pytest.raises(Rejection, match="no push_branch"):
            check_plan_cardinality({"steps": [patch_step("s0"), open_pr_step("s1")]}, PLAN_POLICY)

    def test_a_push_with_no_open_pr_passes_the_cardinality_rule(self):
        # Incomplete, but that is decide_delivery's Refusal to make — this phase
        # bounds COUNT, and rejecting here would pre-empt a different rule.
        check_plan_cardinality({"steps": [patch_step("s0"), push_step("s1")]}, PLAN_POLICY)

    def test_a_suggestion_plan_may_not_carry_a_write_chain(self):
        # ADR-0009: a suggestion is the delivery that needs no branch.
        with pytest.raises(Rejection, match="suggest"):
            check_plan_cardinality(
                {"steps": [suggest_step("s0"), push_step("s1"), open_pr_step("s2")]}, PLAN_POLICY
            )

    def test_a_suggestion_only_plan_passes(self):
        check_plan_cardinality({"steps": [suggest_step("s0")]}, PLAN_POLICY)

    def test_duplicate_labels_reject(self):
        # Two identical effects for one finding, in relative order, which
        # pairwise ordering alone accepts.
        with pytest.raises(Rejection, match="label"):
            check_plan_cardinality(
                {"steps": [patch_step("s0"), label_step("s1"), label_step("s2")]}, PLAN_POLICY
            )

    def test_verify_plan_enforces_cardinality(self):
        steps = [anchored_patch("s0")]
        steps += [push_step(f"p{i}", name=f"smtithy/b{i}") for i in range(9)]
        steps += [open_pr_step(f"o{i}", branch=f"smtithy/b{i}") for i in range(9)]
        with pytest.raises(Rejection, match="at most once"):
            verify_plan({"steps": steps}, PLAN_DIFF, PLAN_CHANGED_FILES, _full_policy(), tree_source())


class TestOrdering:
    """The Python twin of ts/plan/prove.test.ts describe('proveOrdering').

    Case for case, same names, because the semantics must be byte-identical:
    the prover and this module read the same policy.ordering, and a plan one
    admits and the other rejects is the defect this module's docstring names.
    """

    def test_holds_when_patch_precedes_push_branch_precedes_open_pr(self):
        check_plan_ordering({"steps": [patch_step("s0"), push_step("s1"), open_pr_step("s2")]}, PLAN_POLICY)

    def test_catches_push_branch_before_patch(self):
        with pytest.raises(Rejection, match="push_branch.*precedes.*patch"):
            check_plan_ordering({"steps": [push_step("s0"), patch_step("s1")]}, PLAN_POLICY)

    def test_catches_open_pr_before_push_branch(self):
        with pytest.raises(Rejection, match="open_pr.*precedes.*push_branch"):
            check_plan_ordering({"steps": [open_pr_step("s0"), push_step("s1")]}, PLAN_POLICY)

    def test_catches_a_violation_across_unrelated_steps_between_the_pair(self):
        # The rule is about relative order, not adjacency.
        with pytest.raises(Rejection):
            check_plan_ordering(
                {"steps": [open_pr_step("s0"), patch_step("s1"), push_step("s2")]}, PLAN_POLICY
            )

    def test_holds_for_a_plan_with_no_orderable_pair(self):
        check_plan_ordering({"steps": [label_step("s0")]}, PLAN_POLICY)

    def test_holds_for_repeated_patches_which_no_rule_orders(self):
        check_plan_ordering(
            {"steps": [patch_step("s0"), patch_step("s1", path="src/b.py"), push_step("s2")]}, PLAN_POLICY
        )

    # ordering with suggest steps (ADR-0009), mirroring the TS describe block.

    def test_holds_for_a_suggestion_only_plan_vacuously(self):
        check_plan_ordering({"steps": [suggest_step("s0")]}, PLAN_POLICY)

    def test_catches_push_branch_before_patch_alongside_suggestions(self):
        # Vacuous-pass is this policy's known failure mode: suggest steps must
        # not dilute the ordering obligation on the write chain beside them.
        with pytest.raises(Rejection, match="push_branch"):
            check_plan_ordering(
                {"steps": [suggest_step("s0"), push_step("s1"), patch_step("s2", path="src/b.py")]},
                PLAN_POLICY,
            )

    def test_verify_plan_enforces_ordering(self):
        # The phase is wired into the driver, not merely available: the plan is
        # otherwise fully valid (anchored, in-frame, in-bounds). This plan
        # violates BOTH rules, and rules are evaluated in policy order, so the
        # patch/push_branch one is reported — first violation wins.
        plan = {"steps": [open_pr_step("s0"), push_step("s1"), anchored_patch("s2")]}
        with pytest.raises(Rejection, match=r"push_branch.*precedes.*patch.*ordering policy forbids"):
            verify_plan(plan, PLAN_DIFF, PLAN_CHANGED_FILES, _full_policy(), tree_source())

    def test_verify_plan_admits_the_legal_write_chain(self):
        plan = {"steps": [anchored_patch("s0"), push_step("s1"), open_pr_step("s2")]}
        verify_plan(plan, PLAN_DIFF, PLAN_CHANGED_FILES, _full_policy(), tree_source())


class TestTreeContentSource:
    def test_reads_bytes_under_the_root(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_bytes(b"anchor\n")
        assert tree_content_source(tmp_path)("src/a.py") == b"anchor\n"

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            tree_content_source(tmp_path)("src/missing.py")

    def test_symlink_out_of_the_tree_reads_as_missing(self, tmp_path):
        # The quarantine tree is contributor-authored, so a symlink pointing
        # out of it is an expected hostile shape: never followed.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"AKIA-shaped bytes")
        root = tmp_path / "pr_root"
        root.mkdir()
        (root / "link.py").symlink_to(outside / "secret")
        with pytest.raises(OSError, match="outside the reviewed tree"):
            tree_content_source(root)("link.py")

    def test_dotdot_traversal_reads_as_missing(self, tmp_path):
        outside = tmp_path / "secret"
        outside.write_bytes(b"x")
        root = tmp_path / "pr_root"
        root.mkdir()
        with pytest.raises(OSError, match="outside the reviewed tree"):
            tree_content_source(root)("../secret")

    # Confinement was checked on the RESOLVED path only, so a symlink whose
    # target stayed INSIDE the tree was followed happily — and the bytes of
    # another, possibly denylisted or unchanged, path were served as the content
    # of the declared one. The frame and denylist checks are lexical (they see
    # only the declared string), so they cannot see through the link.

    def test_in_tree_symlink_to_a_denylisted_target_reads_as_missing(self, tmp_path):
        root = tmp_path / "pr_root"
        (root / "src").mkdir(parents=True)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_bytes(b"name: ci\non: push\n")
        (root / "src" / "link.py").symlink_to("../.github/workflows/ci.yml")
        with pytest.raises(OSError, match="symlink"):
            tree_content_source(root)("src/link.py")

    def test_in_tree_symlink_to_an_ordinary_target_reads_as_missing(self, tmp_path):
        # Not only denylisted targets: serving b.py's bytes as a.py's content is
        # a false anchor whatever b.py is.
        root = tmp_path / "pr_root"
        (root / "src").mkdir(parents=True)
        (root / "src" / "real.py").write_bytes(b"anchor\n")
        (root / "src" / "alias.py").symlink_to("real.py")
        with pytest.raises(OSError, match="symlink"):
            tree_content_source(root)("src/alias.py")

    def test_a_symlinked_parent_directory_reads_as_missing(self, tmp_path):
        # The component case: the leaf is a regular file, but a DIRECTORY on the
        # way to it is a link, so the declared path still does not name the bytes.
        root = tmp_path / "pr_root"
        (root / "real").mkdir(parents=True)
        (root / "real" / "a.py").write_bytes(b"anchor\n")
        (root / "alias").symlink_to("real", target_is_directory=True)
        with pytest.raises(OSError, match="symlink"):
            tree_content_source(root)("alias/a.py")

    def test_a_regular_file_beside_a_symlink_still_reads(self, tmp_path):
        # False-positive guard: refusing links must not refuse the tree.
        root = tmp_path / "pr_root"
        (root / "src").mkdir(parents=True)
        (root / "src" / "real.py").write_bytes(b"anchor\n")
        (root / "src" / "alias.py").symlink_to("real.py")
        assert tree_content_source(root)("src/real.py") == b"anchor\n"

    def test_containment_rejects_a_symlinked_path(self, tmp_path):
        # End to end: the rejection the verifier reports is the anchoring one,
        # so the plan is refused rather than anchored against the target's bytes.
        root = tmp_path / "pr_root"
        (root / "src").mkdir(parents=True)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_bytes(b"name: ci\non: push\n")
        (root / "src" / "app.py").symlink_to("../.github/workflows/ci.yml")
        plan = {"steps": [anchored_patch(path="src/app.py", old="name: ci\n", new="name: evil\n")]}
        with pytest.raises(Rejection, match="cannot read"):
            contained(plan, content_source=tree_content_source(root))


class TestPlanMarkdownAndSecrets:
    def full_policy(self):
        policy = copy.deepcopy(POLICY)
        policy["markdown"]["link_host_allowlist"] = ["docs.example.com"]
        return policy

    def full_plan(self, note="make `path` optional", body="Fixes the reviewed finding.",
                  old="def load(path):\n", new="def load(path=None):\n"):
        """The patch delivery: a chain carrying open_pr.body's markdown.

        Suggest and patch are two separate deliveries (decide_delivery refuses a
        plan mixing them), so note is exercised by suggest_plan below rather than
        by bolting a suggest step onto this one.
        """
        return {
            "steps": [
                {"id": "s1", "kind": "patch",
                 "args": {"path": "src/app.py", "old": old, "new": new}},
                push_step("s2"),
                {"id": "s3", "kind": "open_pr",
                 "args": {"branch": "smtithy/fix-x", "title": "Fix load()", "body": body}},
            ]
        }

    def suggest_plan(self, note="make `path` optional", new="def load(path=None):\n"):
        """The suggestion delivery, carrying suggest.note's markdown."""
        return {"steps": [anchored_suggest(note=note, new=new)]}

    def run(self, plan, policy=None):
        verify_plan(plan, PLAN_DIFF, PLAN_CHANGED_FILES, policy or self.full_policy(), tree_source())

    def test_a_full_plan_verifies_end_to_end(self):
        self.run(self.full_plan())

    def test_a_suggestion_plan_verifies_end_to_end(self):
        self.run(self.suggest_plan())

    def test_suggest_note_is_markdown_checked(self):
        with pytest.raises(Rejection, match="raw HTML"):
            self.run(self.suggest_plan(note="<script>alert(1)</script>"))

    def test_open_pr_body_is_markdown_checked(self):
        with pytest.raises(Rejection, match="@-mention"):
            self.run(self.full_plan(body="ping @maintainer to merge"))

    def test_secret_in_new_rejects(self):
        # new is exempt from the markdown gate (file bytes, not prose) but
        # NOT from the secret scan: the raw-JSON representation covers it.
        self.run(self.full_plan(new='KEY = "not-a-secret"\n'))
        with pytest.raises(Rejection, match="secret scan"):
            self.run(self.full_plan(new='KEY = "AKIAIOSFODNN7EXAMPLE"\n'))

    def test_secret_in_note_rendered_form_rejects(self):
        # Bold-split key: invisible to the raw scan, complete once rendered —
        # the same case check_secrets pins for review comments.
        with pytest.raises(Rejection, match="secret scan"):
            self.run(self.suggest_plan(note="uses key AKIA**IOSF**ODNN7EXAMPLE here"))
