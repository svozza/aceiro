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

from plan_verify import check_plan_schema  # noqa: E402
from verify import Rejection  # noqa: E402

PLAN_POLICY = json.loads(
    (Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text()
)["plan"]


def patch_step(step_id="s0", path="src/a.py", old="a", new="b"):
    return {"id": step_id, "kind": "patch", "args": {"path": path, "old": old, "new": new}}


def suggest_step(step_id="s0", path="src/a.py"):
    return {
        "id": step_id,
        "kind": "suggest",
        "args": {"path": path, "line": 1, "old": "a", "new": "b", "note": "n"},
    }


def push_step(step_id="s1", name="fix/x"):
    return {"id": step_id, "kind": "push_branch", "args": {"name": name}}


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

    def test_every_string_arg_is_markdown_checked_or_pattern_constrained(self):
        # verify.py's markdown_fields rule, applied to plan args: a string
        # that is neither would flow into a posted comment or a git ref
        # unchecked. Enforced against the shipped policy so adding a lax arg
        # spec fails here before it ships.
        for kind, spec in PLAN_POLICY["step_kinds"].items():
            for arg_name, arg_spec in spec["args"].items():
                if arg_spec["type"] != "string":
                    continue
                constrained = arg_spec.get("markdown") or "pattern" in arg_spec or arg_name in ("old", "new")
                assert constrained, f"{kind}.{arg_name}: string arg with no markdown flag and no pattern"

    def test_old_and_new_are_exempt_because_they_are_never_rendered(self):
        # patch/suggest old+new are file bytes, not prose: old must byte-match
        # the tree (ADR-0005 anchoring) and new is code whose only gate is the
        # human merge. Pinned so the exemption above stays a named decision.
        for kind in ("patch", "suggest"):
            args = PLAN_POLICY["step_kinds"][kind]["args"]
            assert "pattern" not in args["old"] and not args["old"].get("markdown")
            assert "pattern" not in args["new"] and not args["new"].get("markdown")


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
