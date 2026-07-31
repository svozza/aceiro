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
    check_plan_containment,
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
@@ -1,4 +1,5 @@
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
        # src/util.py has lines 1-2 in hunks; src/app.py's hunk covers 1-5.
        # Line 2 exists in both, so probe with a line only app.py has.
        with pytest.raises(Rejection, match="not inside any diff hunk"):
            contained(
                {"steps": [anchored_suggest(path="src/util.py", line=5,
                                            old="def check(path):\n", new="def check(path=None):\n")]}
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

    def test_anchoring_runs_after_the_frame(self):
        # An out-of-frame path must reject as out-of-frame, not reach the
        # content source: the filesystem is only consulted for paths that
        # already passed the closed-set checks.
        def exploding(path):
            raise AssertionError(f"content source consulted for {path!r}")

        with pytest.raises(Rejection, match="not a file this PR touched"):
            contained({"steps": [anchored_patch(path="src/evil.py")]}, content_source=exploding)


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


class TestPlanMarkdownAndSecrets:
    def full_policy(self):
        policy = copy.deepcopy(POLICY)
        policy["markdown"]["link_host_allowlist"] = ["docs.example.com"]
        return policy

    def full_plan(self, note="make `path` optional", body="Fixes the reviewed finding.",
                  old="def load(path):\n", new="def load(path=None):\n"):
        return {
            "steps": [
                anchored_suggest(note=note),
                {"id": "s1", "kind": "patch",
                 "args": {"path": "src/app.py", "old": old, "new": new}},
                push_step("s2"),
                {"id": "s3", "kind": "open_pr",
                 "args": {"branch": "fix/x", "title": "Fix load()", "body": body}},
            ]
        }

    def run(self, plan, policy=None):
        verify_plan(plan, PLAN_DIFF, PLAN_CHANGED_FILES, policy or self.full_policy(), tree_source())

    def test_a_full_plan_verifies_end_to_end(self):
        self.run(self.full_plan())

    def test_suggest_note_is_markdown_checked(self):
        with pytest.raises(Rejection, match="raw HTML"):
            self.run(self.full_plan(note="<script>alert(1)</script>"))

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
            self.run(self.full_plan(note="uses key AKIA**IOSF**ODNN7EXAMPLE here"))
