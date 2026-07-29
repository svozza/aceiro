"""Tests for diff_map.py — the one diff walk both consumers derive from.

Two things depend on this answer and must never disagree: provenance (which
lines a finding may anchor to) and the annotated diff the model reads (the
number it copies into `line`). They share this walk precisely so a divergence is
not expressible; these tests pin the walk's own rules.
"""

import pytest

from diff_map import split_diff_lines, walk_diff
from conftest import DELETED_FILE_DIFF, SAMPLE_DIFF


def numbers_by_path(diff_text):
    mapping: dict[str, list[int]] = {}
    for position in walk_diff(diff_text):
        if position.new_line is not None:
            mapping.setdefault(position.path, []).append(position.new_line)
    return mapping


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
