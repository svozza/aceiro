"""Tests for suggest.py — the suggestion delivery, ported from the reference
reconciler on the extraction source's staging/inline-comments-test @ 70bebcd4.

ADR-0009's addendum names that branch's suite as this port's acceptance bar, so
each of its live-measured lessons has a case here: identity from the anchored
CODE and never prose or a line number, ownership as marker AND authenticated
author with the marker read from line 1 only, reply-aware retraction that never
"resolves", post-before-retract, wrappers superseded best-effort, and content
compared with the executor-authored lines stripped by position.

Network is never touched: the reviews helpers are stubbed.
"""

import json
import sys
from pathlib import Path

import pytest

import suggest
from diff_map import anchor_signatures

from test_plan_verify import PLAN_CHANGED_FILES, PLAN_DIFF, PLAN_TREE, tree_source

POLICY = json.loads((Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text())

METADATA = {
    "model": "global.anthropic.claude-opus-4-8",
    "policy": "0011223344ff",
    "sha": "reviewed-sha",
    "run_url": "https://github.com/o/r/actions/runs/1",
}

BOT = "smtithy[bot]"

SIGNATURES = anchor_signatures(PLAN_DIFF, content_source=tree_source())


def step(path="src/app.py", line=2, old="def load(path):\n",
         new="def load(path=None):\n", note="make path optional"):
    return {"id": "s0", "kind": "suggest",
            "args": {"path": path, "line": line, "old": old, "new": new, "note": note}}


# ------------------------------------------------------------- the fence ---


class TestSuggestionFence:
    """`new` is file bytes: deliberately NOT markdown-checked (pinned by
    test_plan_verify's test_old_and_new_are_exempt_because_they_are_never_rendered,
    since it must byte-match the tree and is gated by anchoring plus the human
    click). So it is the one model-controlled value in this body that can contain
    fence syntax, and the fence delimiter is the harness's to choose.

    CONTEXT.md's canonicalization: establish what GitHub will render before
    deciding the text is safe. A three-backtick fence around content holding
    three backticks ends where the CONTENT says, not where the harness meant —
    everything after it leaves the suggestion block, and the notice and policy
    hash ADR-0005 requires land inside a code span instead of being read.
    """

    def test_a_plain_suggestion_uses_a_three_backtick_fence(self):
        body = suggest.render_suggestion(step(), "abc123", METADATA)
        assert "```suggestion\ndef load(path=None):\n```" in body

    def test_content_carrying_a_three_backtick_run_gets_a_longer_fence(self):
        body = suggest.render_suggestion(
            step(new='doc = """\n```\n"""\n'), "abc123", METADATA)
        assert "````suggestion" in body
        assert "```suggestion" not in body.replace("````suggestion", "")

    def test_the_fence_is_always_longer_than_the_longest_run_in_the_content(self):
        for run in range(1, 12):
            new = f"x = '{'`' * run}'\n"
            body = suggest.render_suggestion(step(new=new), "abc123", METADATA)
            opener = next(line for line in body.split("\n") if line.endswith("suggestion"))
            assert len(opener) - len("suggestion") > run, f"a run of {run} can close {opener!r}"

    def test_the_content_cannot_reach_outside_the_block(self):
        # The property, asked of the renderer's own output rather than of the
        # length arithmetic: whatever the model put in `new`, the notice and the
        # policy hash must still be OUTSIDE the fence.
        from verify import code_lines

        hostile = "escape\n```\n</sub>\n```suggestion\nrm -rf /\n"
        body = suggest.render_suggestion(step(new=hostile), "abc123", METADATA)
        for line, is_code in code_lines(body):
            if METADATA["policy"] in line:
                assert not is_code, "the policy hash was swallowed into the suggestion block"

    def test_every_line_of_the_content_is_inside_the_block(self):
        from verify import code_lines

        hostile = "first\n```\nmiddle\n````\nlast\n"
        body = suggest.render_suggestion(step(new=hostile), "abc123", METADATA)
        rendered = dict(code_lines(body))
        for content_line in ("first", "middle", "last"):
            assert rendered[content_line] is True, f"{content_line!r} escaped the suggestion block"

    def test_one_trailing_newline_is_consumed_by_the_closing_fence(self):
        # `new` is line-oriented (the placement check makes `old` start and end at
        # line boundaries), so "a\n" is ONE line. Emitting it verbatim before the
        # closer would suggest a second, empty line — a blank line the plan never
        # described, appended to the contributor's file.
        body = suggest.render_suggestion(step(new="a\n"), "abc123", METADATA)
        assert "```suggestion\na\n```" in body

    def test_a_line_ending_the_file_carries_no_terminator_of_its_own(self):
        body = suggest.render_suggestion(step(new="a"), "abc123", METADATA)
        assert "```suggestion\na\n```" in body

    def test_a_crlf_line_keeps_its_carriage_return(self):
        # Only the \n is the terminator this consumes: a CRLF file's line ending
        # is content, and stripping it would rewrite the file's convention.
        body = suggest.render_suggestion(step(new="a\r\n"), "abc123", METADATA)
        assert "```suggestion\na\r\n```" in body

    def test_an_empty_new_is_a_deletion_suggestion(self):
        # min_length 0 in the policy: a suggestion that removes the anchored
        # lines is an EMPTY block. A blank line inside it would suggest replacing
        # them with one empty line instead of deleting them.
        body = suggest.render_suggestion(step(new=""), "abc123", METADATA)
        assert "```suggestion\n```" in body

    def test_a_single_newline_suggests_one_blank_line_not_a_deletion(self):
        # The distinction the case above turns on: "" deletes the anchored lines,
        # "\n" replaces them with one empty line. Both would render as an empty
        # block under a naive terminator strip, and the difference is a line of
        # the contributor's file.
        body = suggest.render_suggestion(step(new="\n"), "abc123", METADATA)
        assert "```suggestion\n\n```" in body


class TestSuggestionBody:
    def test_the_marker_is_the_first_line(self):
        body = suggest.render_suggestion(step(), "abc123", METADATA)
        assert body.split("\n")[0] == suggest.suggestion_marker("abc123")

    def test_the_footer_is_the_last_line(self):
        # Both are stripped BY POSITION when content is compared, so each must be
        # exactly one line and in its stated place.
        body = suggest.render_suggestion(step(), "abc123", METADATA)
        assert body.split("\n")[-1].startswith("<sub>")

    def test_the_note_is_carried_verbatim(self):
        body = suggest.render_suggestion(step(note="`load` needs a default"), "abc123", METADATA)
        assert "`load` needs a default" in body

    def test_it_says_it_is_not_a_human_review(self):
        # ADR-0005's visibility requirement, which ADR-0009 extends to the
        # suggestion comment: the notice and the policy hash both appear.
        body = suggest.render_suggestion(step(), "abc123", METADATA)
        assert "no approval" in body
        assert METADATA["policy"] in body

    def test_the_reviewed_sha_is_stamped(self):
        body = suggest.render_suggestion(step(), "abc123", METADATA)
        assert METADATA["sha"] in body

    def test_the_model_text_passes_the_markdown_policy(self):
        # The wrapper the executor writes stays inside the grammar it enforces on
        # the model, minus the two lines that are deliberately raw HTML (the
        # marker and the <sub> footer) and the suggestion fence, whose content is
        # file bytes rather than prose.
        from verify import check_markdown_field

        body = suggest.render_suggestion(step(), "abc123", METADATA)
        lines = body.split("\n")
        fence_at = next(i for i, line in enumerate(lines) if line.endswith("suggestion"))
        prose = "\n".join(lines[1:fence_at])
        check_markdown_field(prose, POLICY["markdown"], "suggestion_comment")
