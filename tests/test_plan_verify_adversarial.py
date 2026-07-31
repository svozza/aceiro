"""Adversarial corpus for the plan verifier — the living spec of the
remediation threat model, same discipline as test_verify_adversarial.py:
every plan here MUST be rejected whole, and a case that starts passing is a
regression in the containment grammar. The handful of cases asserting a
near-miss's LEGITIMATE twin still verifies are false-positive guards, the
corpus's own calibration (the artifact corpus's bold-marker case is the
precedent).

Cases are near-misses by construction: each brackets a boundary the honest
version of the same plan sits just inside, so a check loosened by one
character shows up here before it ships.
"""

import copy
import unicodedata

import pytest

from plan_verify import verify_plan
from verify import Rejection

from test_plan_verify import (
    PLAN_CHANGED_FILES,
    PLAN_DIFF,
    PLAN_POLICY,
    POLICY,
    anchored_patch,
    anchored_suggest,
    push_step,
    tree_source,
)

FULL_POLICY = copy.deepcopy(POLICY)
FULL_POLICY["markdown"]["link_host_allowlist"] = ["docs.example.com"]


def rejected(plan, changed_files=None, tree=None, diff_text=None):
    with pytest.raises(Rejection):
        verify_plan(
            plan,
            PLAN_DIFF if diff_text is None else diff_text,
            PLAN_CHANGED_FILES if changed_files is None else changed_files,
            FULL_POLICY,
            tree_source(tree),
        )


def verified(plan, changed_files=None, tree=None):
    verify_plan(
        plan,
        PLAN_DIFF,
        PLAN_CHANGED_FILES if changed_files is None else changed_files,
        FULL_POLICY,
        tree_source(tree),
    )


def plan_of(*steps):
    return {"steps": list(steps)}


class TestDenylistEvasion:
    """Paths engineered to sit one character off a denylist pattern. Each
    hostile path is paired with changed_files/tree entries that admit it, so
    the denylist is the ONLY check standing — a rejection here is the
    denylist's, not the frame's."""

    def admit(self, path, content=b"anchor\n"):
        return {"changed_files": [path], "tree": {path: content}}

    def denied(self, path):
        rejected(plan_of(anchored_patch(path=path, old="anchor\n", new="fixed\n")), **self.admit(path))

    def passes(self, path):
        verified(plan_of(anchored_patch(path=path, old="anchor\n", new="fixed\n")), **self.admit(path))

    def test_github_dir_is_denied_baseline(self):
        self.denied(".github/workflows/ci.yml")

    def test_github_sibling_dir_is_NOT_denied(self):
        # `.github2/x` shares a prefix with `.github/` but is a different
        # directory. The pattern must not match it: a matcher that treated
        # the pattern as a prefix (or let * eat the separator) would deny
        # legitimate paths, and the same sloppiness in the other direction
        # is what evasion exploits. Enforced exactly as written, §17.
        self.passes(".github2/x.yml")

    def test_nested_github_dir_is_NOT_denied_because_the_pattern_is_anchored(self):
        # `.github/**` as written anchors at the repo root; `x/.github/y` is
        # a different path. If root-relative nesting should be denied, the
        # policy must say `**/.github/**` — the matcher must not invent it.
        self.passes("x/.github/y.yml")

    def test_pem_extension_is_denied_baseline(self):
        self.denied("certs/server.pem")

    def test_pem_dot_txt_is_NOT_denied(self):
        # `a.pem.txt` does not end in `.pem`; `**/*.pem` must not match on a
        # substring. (And its evasion twin — shipping a key as .pem.txt — is
        # the secret scan's job, not the path matcher's.)
        self.passes("a.pem.txt")

    def test_uppercase_pem_evades_nothing_it_is_a_different_path(self):
        # Case games: git trees are case-sensitive, so cert.PEM is a real,
        # distinct path the written pattern does not cover. Pinned as passing
        # because that is what the policy SAYS; the shipped-policy test below
        # is where a wider pattern would be a named decision.
        self.passes("cert.PEM")

    def test_key_extension_denied_at_any_depth(self):
        self.denied("deploy/prod/id_rsa.key")

    def test_trailing_slash_variant_does_not_escape(self):
        # A path spelled with a trailing slash is not how git names a file,
        # but schema's path pattern admits it, so the denylist must still
        # catch it rather than be evaded by one appended character.
        self.denied(".github/ci.yml/")


class TestFramePrefixSharing:
    def test_bak_twin_of_a_changed_file_rejects(self):
        # src/a.py.bak vs src/a.py: identity is exact string equality, the
        # prover's intern-table semantics. A startswith() frame would pass this.
        rejected(plan_of(anchored_patch(path="src/app.py.bak")))

    def test_directory_prefix_twin_rejects(self):
        rejected(plan_of(anchored_patch(path="src/app.py/x.py")))

    def test_changed_file_as_substring_rejects(self):
        rejected(plan_of(anchored_patch(path="xsrc/app.py")))

    def test_traversal_spelling_of_a_changed_file_rejects(self):
        # The same bytes a naive normalizer would collapse to src/app.py.
        rejected(plan_of(anchored_patch(path="src/../src/app.py")))

    def test_one_escape_among_legitimate_steps_rejects_the_whole_plan(self):
        rejected(plan_of(
            anchored_patch("s0"),
            anchored_patch("s1", path="src/evil.py"),
        ))


class TestAnchoringNearMisses:
    def test_exact_anchor_verifies_baseline(self):
        verified(plan_of(anchored_patch()))

    def test_one_byte_changed_rejects(self):
        # A single byte off the true anchor (`;` for `:`). Anchoring is
        # substring search, so "off by one byte" means CHANGED, not
        # truncated — a truncated old is a shorter anchor that still
        # byte-matches, pinned as passing in the next case.
        rejected(plan_of(anchored_patch(old="def load(path);\n")))

    def test_newline_appended_where_the_file_has_none_rejects(self):
        # `old` ending mid-line is still a byte-match; adding the one byte
        # the file does not have there (`\n` before the `:`) is not.
        verified(plan_of(anchored_patch(old="def load(path)")))
        rejected(plan_of(anchored_patch(old="def load(path)\n")))

    def test_nfd_smuggling_in_old_rejects(self):
        # The file stores café NFC; the model submits NFD. Byte-anchoring
        # must NOT normalize: an old the model never saw verbatim is not
        # proof it was looking at the file.
        nfc = "greet = 'café'\n"
        tree = {"src/app.py": nfc.encode("utf-8")}
        changed = ["src/app.py"]
        verified(plan_of(anchored_patch(old=nfc, new="greet = 'hi'\n")),
                 changed_files=changed, tree=tree)
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfd != nfc
        rejected(plan_of(anchored_patch(old=nfd, new="greet = 'hi'\n")),
                 changed_files=changed, tree=tree)

    def test_nfc_old_against_an_nfd_file_rejects_symmetrically(self):
        nfd_file = unicodedata.normalize("NFD", "greet = 'café'\n")
        tree = {"src/app.py": nfd_file.encode("utf-8")}
        rejected(plan_of(anchored_patch(old="greet = 'café'\n", new="x\n")),
                 changed_files=["src/app.py"], tree=tree)

    def test_crlf_old_against_an_lf_file_rejects(self):
        rejected(plan_of(anchored_patch(old="def load(path):\r\n")))

    def test_anchor_present_twice_rejects_as_ambiguous(self):
        tree = {"src/app.py": b"check(path)\ncheck(path)\n"}
        rejected(plan_of(anchored_patch(old="check(path)\n", new="check(p)\n")),
                 changed_files=["src/app.py"], tree=tree)

    def test_anchor_from_the_base_side_of_the_diff_rejects(self):
        # `def load():` is what the file said BEFORE the PR. Anchoring is at
        # the reviewed head; a base-side anchor means the model read the
        # wrong tree.
        rejected(plan_of(anchored_patch(old="def load():\n")))


class TestBoundsAtTheCap:
    def test_changed_lines_at_cap_verifies_and_plus_one_rejects(self):
        cap = PLAN_POLICY["max_changed_lines"]
        at_cap = "x\n" * (cap - 1)  # old's single line brings the total to cap
        verified(plan_of(anchored_patch(new=at_cap)))
        rejected(plan_of(anchored_patch(new=at_cap + "y\n")))

    def test_missing_final_newline_still_counts_the_line(self):
        # cap lines where the last has no terminator: a count based on "\n"
        # occurrences alone would read cap-1 and admit one extra line.
        cap = PLAN_POLICY["max_changed_lines"]
        rejected(plan_of(anchored_patch(new="x\n" * (cap - 1) + "y")))

    def test_patched_files_at_cap_verifies_and_plus_one_rejects(self):
        cap = PLAN_POLICY["max_patched_files"]

        def fleet(count):
            changed = [f"src/f{i}.py" for i in range(count)]
            tree = {path: b"anchor\n" for path in changed}
            steps = [anchored_patch(f"s{i}", path=path, old="anchor\n", new="fixed\n")
                     for i, path in enumerate(changed)]
            return plan_of(*steps), changed, tree

        plan, changed, tree = fleet(cap)
        verified(plan, changed_files=changed, tree=tree)
        plan, changed, tree = fleet(cap + 1)
        rejected(plan, changed_files=changed, tree=tree)

    def test_the_line_cap_is_per_step_so_splitting_a_large_patch_passes_it(self):
        # Pinned as PASSING to keep the semantics honest: ADR-0005 caps
        # changed lines PER PATCH, so two steps of cap lines each into one
        # file clear the line bound, and the aggregate bound is
        # max_patched_files. A per-plan line cap would be a policy addition,
        # and this is the case that turns red when someone makes it.
        cap = PLAN_POLICY["max_changed_lines"]
        at_cap = "x\n" * (cap - 1)
        tree = {"src/app.py": b"import os\n    return os.environ\n"}
        verified(plan_of(
            anchored_patch("s0", old="import os\n", new=at_cap),
            anchored_patch("s1", old="    return os.environ\n", new=at_cap),
        ), tree=tree)


class TestSuggestLineNearMisses:
    # src/app.py's hunk covers new-side lines 1-4; src/util.py's covers 1-2.

    def test_last_line_of_the_hunk_verifies(self):
        # Line 4 IS where `    return os.environ\n` lives, which is now part of
        # what verifies: the addressed line and the anchored bytes are one
        # region. This case read line=5 until PLAN_DIFF's hunk header was
        # corrected — it declared @@ -1,4 +1,5 @@ over a body of 3 old / 4 new
        # lines, so walk_diff numbered the NEXT file's `diff --git` line as
        # src/app.py:5 and a suggestion could be addressed to a line the file
        # does not have.
        verified(plan_of(anchored_suggest(line=4, old="    return os.environ\n",
                                          new="    return dict(os.environ)\n")))

    def test_one_past_the_hunk_rejects(self):
        rejected(plan_of(anchored_suggest(line=5)))

    def test_line_zero_never_reaches_provenance(self):
        # Schema's minimum:1 owns this boundary; pinned so a schema loosening
        # is caught here too.
        rejected(plan_of(anchored_suggest(line=0)))

    def test_valid_line_number_but_for_the_other_file_rejects(self):
        # Line 4 is in app.py's hunk only; claiming it for util.py must fail
        # — a provenance map keyed on line numbers alone would pass this.
        rejected(plan_of(anchored_suggest(path="src/util.py", line=4,
                                          old="def check(path):\n", new="def check(p):\n")))

    def test_line_in_a_deleted_files_hunk_rejects(self):
        deletion_diff = (
            "diff --git a/gone.py b/gone.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-print(1)\n"
            "-print(2)\n"
        )
        rejected(
            plan_of(anchored_suggest(path="gone.py", line=1, old="print(1)\n", new="print(0)\n")),
            changed_files=["gone.py"],
            tree={"gone.py": b"print(1)\nprint(2)\n"},
            diff_text=deletion_diff,
        )


class TestSecretsSplitAcrossArgs:
    def test_secret_split_across_old_and_new_rejects(self):
        # `old` ends mid-key and `new` completes it: neither fragment matches
        # the AKIA pattern alone (the raw-JSON scan separates them with
        # syntax), but the fused old+new representation reassembles the run —
        # exactly how a rendered suggestion shows them adjacent.
        tree = {"src/app.py": b'key = "AKIAIOSFODNN7EXAMPLE"\n'}
        rejected(plan_of(anchored_patch(old='key = "AKIAIOSF', new='ODNN7EXAMPLE_ROTATED"\n')),
                 changed_files=["src/app.py"], tree=tree)

    def test_secret_split_within_new_alone_rejects_via_raw_scan(self):
        rejected(plan_of(anchored_patch(new='key = "AKIAIOSFODNN7EXAMPLE"\n')))

    def test_secret_in_old_rejects_even_though_old_is_tree_content(self):
        # `old` byte-matches the reviewed tree, so the credential is the
        # CONTRIBUTOR's leak — but reposting it in a suggestion re-publishes
        # it into a comment, so the scan still refuses. The remediation for
        # a leaked key is revocation, not a bot comment quoting it.
        secret = b'key = "AKIAIOSFODNN7EXAMPLE"\n'
        tree = {"src/app.py": secret}
        rejected(plan_of(anchored_patch(old=secret.decode(), new='key = load_key()\n')),
                 changed_files=["src/app.py"], tree=tree)

    def test_innocent_fragments_do_not_false_positive(self):
        # Calibration: the fused scan must not invent secrets from benign
        # adjacency. AKIA alone plus prose in new stays clean.
        tree = {"src/app.py": b'PREFIX = "AKIA"\n'}
        verified(plan_of(anchored_patch(old='PREFIX = "AKIA"\n', new='PREFIX = get_prefix()\n')),
                 changed_files=["src/app.py"], tree=tree)

    def test_bold_split_secret_in_open_pr_body_rejects_rendered(self):
        plan = plan_of(
            anchored_patch(),
            push_step("s1"),
            {"id": "s2", "kind": "open_pr",
             "args": {"branch": "fix/x", "title": "t",
                      "body": "uses AKIA**IOSF**ODNN7EXAMPLE internally"}},
        )
        rejected(plan)
