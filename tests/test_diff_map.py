"""Tests for diff_map.py — the one diff walk both consumers derive from.

Two things depend on this answer and must never disagree: provenance (which
lines a finding may anchor to) and the annotated diff the model reads (the
number it copies into `line`). They share this walk precisely so a divergence is
not expressible; these tests pin the walk's own rules.
"""

import re

import pytest

from diff_map import anchor_signatures, split_diff_lines, walk_diff
from conftest import DELETED_FILE_DIFF, SAMPLE_DIFF


def numbers_by_path(diff_text):
    mapping: dict[str, list[int]] = {}
    for position in walk_diff(diff_text):
        if position.new_line is not None:
            mapping.setdefault(position.path, []).append(position.new_line)
    return mapping


class TestTheSampleDiffIsCoherent:
    """SAMPLE_DIFF is the reference fixture for provenance across the whole
    suite, so its hunk headers must describe its own body.

    The logger.py header declared 9 new-side lines over a body carrying 7. The
    declared count is authoritative (deliberately — see
    test_declared_count_wins_over_line_shape_inside_a_hunk), so walk_diff kept
    numbering past the hunk and assigned lines 17 and 18 to `diff --git ...` and
    `new file mode 100644` — DIFF METADATA, offered to the model as anchorable and
    accepted by provenance. A finding on line 17 verified, and the executor would
    have posted an inline comment on a line that is not code in the fixture's own
    model of the file.

    Worse for the suite than for production: a regression in over-declared-hunk
    handling cannot be detected when the reference fixture already relies on it.
    """

    def test_the_hunk_map_is_exactly_the_hand_written_one(self):
        # Hand-written from reading the fixture, the way test_run_evals pins
        # scenario diffs against their pr_root. logger.py's hunk starts at 10 and
        # carries 7 new-side lines; the new test file carries 4.
        assert _hunk_lines(SAMPLE_DIFF) == {
            "aws_lambda_powertools/logging/logger.py": {10, 11, 12, 13, 14, 15, 16},
            "tests/unit/test_logger.py": {1, 2, 3, 4},
        }

    def test_no_anchorable_line_is_diff_metadata(self):
        # The property the map above encodes, stated directly: nothing a finding
        # may anchor to is a line git wrote about the diff rather than a line of
        # the file.
        for position in walk_diff(SAMPLE_DIFF):
            if position.new_line is None:
                continue
            assert not position.text.startswith(("diff --git ", "index ", "new file mode ", "@@ ")), (
                f"line {position.new_line} of {position.path} is diff metadata: {position.text!r}"
            )

    def test_every_declared_count_matches_its_body(self):
        # The general rule over both hunks, so a future edit to either header
        # fails here rather than silently extending the anchorable set.
        lines = SAMPLE_DIFF.splitlines()
        headers = [i for i, line in enumerate(lines) if line.startswith("@@ ")]
        assert headers, "no hunk headers found; this assertion has gone stale"
        for start in headers:
            declared = int(re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", lines[start]).group(2) or "1")
            counted = 0
            for line in lines[start + 1:]:
                if line.startswith(("diff --git ", "@@ ")):
                    break
                if not line.startswith(("-", "\\")):
                    counted += 1
            assert counted == declared, (
                f"header {lines[start]!r} declares {declared} new-side lines over a body of {counted}"
            )


class TestWalkDiff:
    def test_one_position_per_physical_line(self):
        # Callers zip the result against the diff's own lines (strict=True), so
        # a length mismatch would be a hard error at the call site.
        assert len(walk_diff(SAMPLE_DIFF)) == len(SAMPLE_DIFF.splitlines())

    def test_context_and_added_lines_are_numbered_from_the_header(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -5,3 +5,3 @@\n ctx\n+add\n ctx2\n"
        assert numbers_by_path(diff) == {"x.py": [5, 6, 7]}

    def test_removed_lines_consume_no_number(self):
        # The rule the model kept getting wrong when it had to derive numbers:
        # a `-` line is not in the new file, so the next kept line takes the
        # number the removed line would otherwise have used.
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,4 +1,2 @@\n ctx\n-gone_a\n-gone_b\n+added\n"
        positions = walk_diff(diff)
        assert [p.new_line for p in positions[4:]] == [1, None, None, 2]

    def test_no_newline_marker_consumes_no_number(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n+one\n\\ No newline at end of file\n"
        assert numbers_by_path(diff) == {"x.py": [1]}

    def test_deleted_file_contributes_nothing(self):
        # Nothing in a deleted file exists at the head SHA, so nothing in it can
        # be anchored — or offered to the model as anchorable.
        assert numbers_by_path(DELETED_FILE_DIFF) == {}

    def test_multiple_files_each_restart_at_their_own_header(self):
        assert numbers_by_path(SAMPLE_DIFF)["tests/unit/test_logger.py"] == [1, 2, 3, 4]

    def test_declared_count_wins_over_line_shape_inside_a_hunk(self):
        # An added line whose text begins "++ " must be treated as hunk content,
        # not as a "+++ " file header — the declared new-side count is
        # authoritative. Mis-parsing this would silently move every subsequent
        # number and let a finding anchor to the wrong file.
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -1,1 +1,3 @@\n+normal\n+++ b/spoof.py\n+after\n"
        )
        assert numbers_by_path(diff) == {"x.py": [1, 2, 3]}

    def test_hunk_header_is_flagged_and_unnumbered(self):
        # parse_diff_hunks needs the flag to register a path that has a hunk but
        # no anchorable lines; the header itself is not a position.
        headers = [p for p in walk_diff(SAMPLE_DIFF) if p.is_hunk_header]
        assert len(headers) == 2
        assert all(p.new_line is None for p in headers)

    def test_lines_outside_any_file_have_no_path(self):
        assert walk_diff(SAMPLE_DIFF)[0].path is None  # the "diff --git" line

    def test_empty_diff(self):
        assert walk_diff("") == []

    # Git C-quotes any path that is not plain ASCII-printable: the whole target
    # is wrapped in double quotes and the offending bytes are octal-escaped
    # (verified against real `git diff` output). Undecoded, the hunk map is keyed
    # on a string the files API can never produce, so every finding on such a
    # file is rejected — one accented filename makes a whole review unusable.

    def test_c_quoted_utf8_path_is_decoded(self):
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,1 +1,2 @@\n ctx\n+add\n"
        )
        assert numbers_by_path(diff) == {"café.py": [1, 2]}

    def test_c_quoted_embedded_quote_is_decoded(self):
        diff = (
            'diff --git "a/q\\"uote.py" "b/q\\"uote.py"\n'
            '--- "a/q\\"uote.py"\n'
            '+++ "b/q\\"uote.py"\n'
            "@@ -1,1 +1,1 @@\n+one\n"
        )
        assert numbers_by_path(diff) == {'q"uote.py': [1]}

    def test_c_quoted_backslash_and_control_escapes_are_decoded(self):
        diff = (
            '--- "a/back\\\\slash\\tx.py"\n'
            '+++ "b/back\\\\slash\\tx.py"\n'
            "@@ -1,1 +1,1 @@\n+one\n"
        )
        assert numbers_by_path(diff) == {"back\\slash\tx.py": [1]}

    def test_c_quoted_deleted_file_still_contributes_nothing(self):
        # /dev/null is never quoted, but the unquoting must not disturb it.
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n-gone\n"
        )
        assert numbers_by_path(diff) == {}

    def test_unquoted_path_with_a_literal_backslash_is_untouched(self):
        # Only a target that STARTS with a quote is C-quoted. An unquoted path is
        # taken verbatim, so a backslash in it is a literal backslash.
        diff = "--- a/back\\slash.py\n+++ b/back\\slash.py\n@@ -1,1 +1,1 @@\n+one\n"
        assert numbers_by_path(diff) == {"back\\slash.py": [1]}

    def test_quoted_path_keys_match_the_files_api_filename(self):
        # The end-to-end point of the decode: parse_diff_hunks' key is what
        # check_provenance compares against changed_files.json, which holds the
        # files API's real UTF-8 name.
        from verify import parse_diff_hunks

        diff = (
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,1 +1,2 @@\n ctx\n+add\n"
        )
        assert set(parse_diff_hunks(diff)) == {"café.py"}


class TestSplitDiffLines:
    """Only `\\n` ends a line in a unified diff.

    Found by an adversarial review: `str.splitlines()` also breaks on U+2028,
    U+2029, U+000B, U+000C and U+0085. The diff is contributor-controlled, so one
    U+2028 inside an added line would make it count as two — shifting every later
    line number, labelling a fragment in the annotation column, and telling the
    model about a line that is not the line it sees.
    """

    # Written as escapes on purpose: a literal U+2028 in this source file would
    # itself be split by any tool that uses splitlines(), including this test.
    UNICODE_BREAKS = ["\u2028", "\u2029", "\v", "\f", "\u0085"]

    def diff_with(self, separator):
        return (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            f'@@ -1,2 +1,2 @@\n+note = "a{separator}b"\n+dangerous()\n'
        )

    @pytest.mark.parametrize("separator", UNICODE_BREAKS)
    def test_unicode_break_inside_a_line_is_not_a_line_break(self, separator):
        assert len(split_diff_lines(self.diff_with(separator))) == 6

    @pytest.mark.parametrize("separator", UNICODE_BREAKS)
    def test_numbering_is_unaffected_by_a_unicode_break(self, separator):
        # `dangerous()` must be line 2, not unnumbered: this is the whole point
        # of the fix. A splitlines() walk counts the U+2028 line as two, so
        # `dangerous()` shifts to 3 and the number the model is handed is not
        # the line it sees.
        diff = self.diff_with(separator)
        # One position per physical line, so they pair by index with the lines.
        numbered = {
            position.new_line: line
            for position, line in zip(walk_diff(diff), split_diff_lines(diff), strict=True)
            if position.new_line is not None
        }
        assert set(numbered) == {1, 2}
        assert "dangerous()" in numbered[2]

    def test_crlf_tail_is_stripped(self):
        lines = split_diff_lines("a\r\nb\r\n")
        assert lines == ["a", "b"]

    def test_no_trailing_empty_line(self):
        assert split_diff_lines("a\nb\n") == ["a", "b"]
        assert split_diff_lines("a\nb") == ["a", "b"]

    def test_empty_input(self):
        assert split_diff_lines("") == []


class TestAnchorSignatures:
    """Signatures give the executor an identity key for a finding that does not
    depend on the model's prose.

    Motivation, measured on a live PR in the extraction source: the model
    reworded every finding on every run over a byte-identical diff, so a
    prose-derived key never matched twice.
    """

    SHIFT_BEFORE = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n+alpha\n+target()\n+omega\n"
    SHIFT_AFTER = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,5 +1,5 @@\n+inserted\n+inserted2\n+alpha\n+target()\n+omega\n"
    )

    def test_one_signature_per_anchorable_line(self):
        signatures = anchor_signatures(SAMPLE_DIFF)
        assert set(signatures) == {
            (path, line) for path, lines in _hunk_lines(SAMPLE_DIFF).items() for line in lines
        }

    def test_signature_survives_a_pure_line_shift(self):
        # The code moved down two lines; its signature must not change, or a
        # re-anchored comment gets deleted and reposted.
        assert anchor_signatures(self.SHIFT_BEFORE)[("x.py", 2)] == anchor_signatures(self.SHIFT_AFTER)[("x.py", 4)]

    def test_signature_changes_when_the_anchored_line_changes(self):
        edited = self.SHIFT_BEFORE.replace("target()", "target(fixed)")
        assert anchor_signatures(self.SHIFT_BEFORE)[("x.py", 2)] != anchor_signatures(edited)[("x.py", 2)]

    def test_signature_changes_when_a_neighbour_changes(self):
        # The window is what distinguishes two identical-looking lines, so a
        # neighbour edit has to move the signature.
        edited = self.SHIFT_BEFORE.replace("omega", "different")
        assert anchor_signatures(self.SHIFT_BEFORE)[("x.py", 2)] != anchor_signatures(edited)[("x.py", 2)]

    def test_identical_lines_with_different_neighbours_differ(self):
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -1,6 +1,6 @@\n+if a:\n+    return True\n+if b:\n+    return True\n+done()\n+tail()\n"
        )
        signatures = anchor_signatures(diff)
        assert signatures[("x.py", 2)] != signatures[("x.py", 4)]

    def test_whitespace_only_reformatting_is_the_same_signature(self):
        indented = self.SHIFT_BEFORE.replace("+target()", "+    target()")
        assert anchor_signatures(self.SHIFT_BEFORE)[("x.py", 2)] == anchor_signatures(indented)[("x.py", 2)]

    # ADR-0009's addendum specifies the fingerprint as "a whitespace-normalized,
    # NFC'd signature of the anchored line and its neighbours". Both halves are
    # load-bearing and neither was implemented: the identity key decides whether
    # the executor keeps a live comment thread or deletes it and reposts, which
    # is the churn the anchor design exists to avoid.

    def test_a_canonically_equivalent_line_has_the_same_signature(self):
        # An editor rewriting café from NFC to NFD on save is no semantic change,
        # and GitHub renders both identically. Without normalization the anchor
        # moves, so the executor deletes the thread (losing human replies and
        # resolution state) and reposts the same comment.
        composed = self.SHIFT_BEFORE.replace("+target()", "+café = 1")
        decomposed = self.SHIFT_BEFORE.replace("+target()", "+café = 1")
        assert anchor_signatures(composed)[("x.py", 2)] == anchor_signatures(decomposed)[("x.py", 2)]

    def test_normalization_reaches_the_neighbours_too(self):
        # The window is part of the signature, so a neighbour's re-encoding moves
        # the anchor exactly as the anchored line's would.
        composed = self.SHIFT_BEFORE.replace("+omega", "+café")
        decomposed = self.SHIFT_BEFORE.replace("+omega", "+café")
        assert anchor_signatures(composed)[("x.py", 2)] == anchor_signatures(decomposed)[("x.py", 2)]

    @pytest.mark.parametrize("separator", [" ", " ", " ", "", "\v", "\f"])
    def test_non_ascii_whitespace_is_not_folded_into_a_space(self, separator):
        # str.split() folds every Unicode whitespace code point, so two DIFFERENT
        # lines collapse to one signature — and the folded set includes the very
        # separators split_diff_lines refuses to treat as line breaks. Whitespace
        # normalization exists to survive reindentation, not to erase a line's
        # content: a collision hands one comment's identity to another anchor.
        spaced = self.SHIFT_BEFORE.replace("+target()", "+a b")
        exotic = self.SHIFT_BEFORE.replace("+target()", f"+a{separator}b")
        assert anchor_signatures(spaced)[("x.py", 2)] != anchor_signatures(exotic)[("x.py", 2)]

    @pytest.mark.parametrize("separator", [" ", " ", " ", "", "\v", "\f", "　"])
    def test_non_ascii_whitespace_at_a_line_END_is_not_folded_either(self, separator):
        # The case above pins the INTERIOR, where _INDENT_RE decides. The ends are
        # decided by str.strip(), which folds every Unicode whitespace code point —
        # so the deliberate narrowness of _INDENT_RE stopped at the first and last
        # character, and a line differing from another only by a trailing U+2028
        # collapsed onto it. Same cost as any collision: one comment's identity,
        # and so its DELETE/PATCH gate, handed to another anchor.
        plain = self.SHIFT_BEFORE.replace("+target()", "+a b")
        trailing = self.SHIFT_BEFORE.replace("+target()", f"+a b{separator}")
        assert anchor_signatures(plain)[("x.py", 2)] != anchor_signatures(trailing)[("x.py", 2)]

    @pytest.mark.parametrize("padding", ["  ", "\t", " \t ", "\t\t"])
    def test_indentation_at_a_line_end_still_normalizes(self, padding):
        # The property the strip is FOR, pinned alongside the narrowing: trailing
        # whitespace of the SAME class _INDENT_RE folds is reindentation, which the
        # signature must survive. Each spelling separately, so stripping only one
        # of space and tab leaves a case failing.
        bare = self.SHIFT_BEFORE.replace("+target()", "+target()")
        padded = self.SHIFT_BEFORE.replace("+target()", f"+target(){padding}")
        assert anchor_signatures(bare)[("x.py", 2)] == anchor_signatures(padded)[("x.py", 2)]

    @pytest.mark.parametrize("padding", ["  ", "\t", " \t "])
    def test_leading_indentation_normalizes_too(self, padding):
        # The other end, for the same reason: _INDENT_RE folds an interior run to
        # one space, so without the strip a leading run would leave a space the
        # bare line does not have.
        bare = self.SHIFT_BEFORE.replace("+target()", "+target()")
        indented = self.SHIFT_BEFORE.replace("+target()", f"+{padding}target()")
        assert anchor_signatures(bare)[("x.py", 2)] == anchor_signatures(indented)[("x.py", 2)]

    def test_indentation_by_space_or_tab_still_normalizes(self):
        # The property the normalization is FOR, pinned alongside the narrowing
        # above so the fix cannot be read as "stop normalizing".
        tabbed = self.SHIFT_BEFORE.replace("+target()", "+\t\ttarget()")
        spaced = self.SHIFT_BEFORE.replace("+target()", "+    target()")
        assert anchor_signatures(tabbed)[("x.py", 2)] == anchor_signatures(spaced)[("x.py", 2)]

    def test_hunk_edges_are_marked_absent_not_wrapped(self):
        # The first line has no predecessor. It must not silently borrow the last
        # line of the file, which would make two different anchors collide.
        signatures = anchor_signatures(self.SHIFT_BEFORE)
        assert "absent" in signatures[("x.py", 1)]
        assert signatures[("x.py", 1)] != signatures[("x.py", 3)]

    def test_removed_lines_have_no_signature(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,2 @@\n ctx\n-gone\n+added\n"
        assert set(anchor_signatures(diff)) == {("x.py", 1), ("x.py", 2)}

    def test_deleted_file_has_no_signatures(self):
        assert anchor_signatures(DELETED_FILE_DIFF) == {}

    def test_same_code_in_two_files_is_distinguished_by_path(self):
        # The key includes the path separately, but the signature map is keyed on
        # (path, line) so the two never merge here either.
        two_files = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n+same\n+code\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n+same\n+code\n"
        )
        signatures = anchor_signatures(two_files)
        assert ("a.py", 1) in signatures and ("b.py", 1) in signatures

    @pytest.mark.parametrize("separator", TestSplitDiffLines.UNICODE_BREAKS)
    def test_numbering_is_unaffected_by_a_unicode_break(self, separator):
        # `dangerous()` must be line 2, not unnumbered — the same guarantee
        # TestSplitDiffLines pins for walk_diff, observed here at the signature
        # level because the reconciler keys comment identity on this map.
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            f'@@ -1,2 +1,2 @@\n+note = "a{separator}b"\n+dangerous()\n'
        )
        signatures = anchor_signatures(diff)
        assert set(signatures) == {("x.py", 1), ("x.py", 2)}
        assert "dangerous()" in signatures[("x.py", 2)]


class TestTheWindowComesFromTheHeadContent:
    """ADR-0009's addendum: the window's source is part of the identity contract.

    A window taken from the diff reads a neighbour outside every hunk as `absent`
    rather than as its real text, so an unrelated push that GROWS a hunk around an
    unchanged line changes that line's signature. The executor then sees an
    unknown fingerprint plus an orphaned old one, and deletes a live comment
    thread to repost the same comment. No function of the diff alone can close it
    — in the narrow-hunk run the neighbour's text is not in the input at all.

    So the window comes from file content at the head SHA, which the executor
    already reads for anchoring, and the diff keeps deciding WHICH lines are
    anchorable.
    """

    # One file, one head SHA, two diffs of it. `target()` is unchanged in both,
    # and its neighbours are unchanged too: only the hunk BOUNDARY moves.
    HEAD = b"alpha\ntarget()\nomega\ntail()\n"

    NARROW = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -2,1 +2,1 @@\n+target()\n"
    )
    WIDE = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,4 +1,4 @@\n alpha\n+target()\n omega\n tail()\n"
    )

    @staticmethod
    def source(tree=None):
        tree = {"x.py": TestTheWindowComesFromTheHeadContent.HEAD} if tree is None else tree

        def read(path: str) -> bytes:
            if path not in tree:
                raise FileNotFoundError(path)
            return tree[path]

        return read

    def test_a_grown_hunk_does_not_move_the_signature(self):
        narrow = anchor_signatures(self.NARROW, content_source=self.source())
        wide = anchor_signatures(self.WIDE, content_source=self.source())
        assert narrow[("x.py", 2)] == wide[("x.py", 2)]

    def test_the_diff_derived_window_is_what_moves_it(self):
        # The defect this contract closes, pinned in its failing direction so the
        # fix cannot be read as "the two were always equal".
        narrow = anchor_signatures(self.NARROW)
        wide = anchor_signatures(self.WIDE)
        assert narrow[("x.py", 2)] != wide[("x.py", 2)]

    def test_a_neighbour_outside_every_hunk_is_its_real_text(self):
        signatures = anchor_signatures(self.NARROW, content_source=self.source())
        assert "alpha" in signatures[("x.py", 2)]
        assert "omega" in signatures[("x.py", 2)]
        assert "absent" not in signatures[("x.py", 2)]

    def test_the_diff_still_decides_which_lines_are_anchorable(self):
        # The head content carries four lines; only the one the narrow hunk makes
        # visible may be anchored to, because provenance is the diff's answer and
        # this map is what the executor keys comments on.
        assert set(anchor_signatures(self.NARROW, content_source=self.source())) == {("x.py", 2)}

    def test_a_file_edge_is_absent_rather_than_wrapped(self):
        # Line 1 has no predecessor in the FILE either, so `absent` is the honest
        # answer there — it must not borrow the last line and collide.
        signatures = anchor_signatures(self.WIDE, content_source=self.source())
        assert "absent" in signatures[("x.py", 1)]
        assert signatures[("x.py", 1)] != signatures[("x.py", 4)]

    def test_an_unreadable_path_falls_back_to_the_diff_window(self):
        # Anchoring read the file moments earlier, so this is the tree changing
        # underneath the executor. Degrading to the diff-derived window costs the
        # boundary-independence for that path; raising would cost the whole
        # delivery, and identity is not a containment property.
        empty = anchor_signatures(self.NARROW, content_source=self.source(tree={}))
        assert empty == anchor_signatures(self.NARROW)

    @pytest.mark.parametrize("raised", [
        FileNotFoundError("gone"),
        IsADirectoryError("a directory"),
        PermissionError("no"),
        # Not an OSError: a reader given a name carrying an embedded NUL raises
        # this from the stat rather than failing to open. "Unreadable" either way,
        # and the fallback exists so an identity problem never costs the delivery.
        ValueError("embedded null character in path"),
    ])
    def test_any_unreadable_path_degrades_rather_than_raising(self, raised):
        def refuses(path):
            raise raised

        assert anchor_signatures(self.NARROW, content_source=refuses) == anchor_signatures(self.NARROW)

    def test_the_head_content_is_decoded_as_contributor_bytes(self):
        # File content at the head SHA is contributor-controlled, so an invalid
        # byte must yield a usable signature rather than raise out of the
        # reconciler — decode_contributor_bytes' discipline, same as the diff's.
        tree = {"x.py": b"alpha\ntarget()\n\xff\xfe\ntail()\n"}
        signatures = anchor_signatures(self.NARROW, content_source=self.source(tree))
        assert "target()" in signatures[("x.py", 2)]

    def test_a_unicode_separator_in_the_head_content_is_not_a_line_break(self):
        # split_diff_lines' rule, applied to file content: U+2028 does not end a
        # line, so it must not shift every later line's number and hand one
        # anchor another line's window.
        tree = {"x.py": "alpha\ntar get()\nomega\ntail()\n".encode()}
        signatures = anchor_signatures(self.NARROW, content_source=self.source(tree))
        assert "omega" in signatures[("x.py", 2)]


def _hunk_lines(diff_text):
    from verify import parse_diff_hunks

    return {path: lines for path, lines in parse_diff_hunks(diff_text).items() if lines}
