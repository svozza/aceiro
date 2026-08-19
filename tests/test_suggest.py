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
import urllib.error
from pathlib import Path

import pytest

import suggest
from diff_map import anchor_signatures
from plan_verify import Step, check_suggestion_new_survives_markdown
from verify import Rejection, unterminated_fence

from test_plan_verify import PLAN_CHANGED_FILES, PLAN_DIFF, PLAN_TREE, tree_source

POLICY = json.loads((Path(__file__).parent.parent / "src" / "aceiro" / "policy.json").read_text())

METADATA = {
    "model": "global.anthropic.claude-opus-4-8",
    "policy": "0011223344ff",
    "sha": "reviewed-sha",
    "run_url": "https://github.com/o/r/actions/runs/1",
}

BOT = "aceiro[bot]"

# A fingerprint of the shape SUGGESTION_MARKER_RE accepts: 16 hex digits, which
# is what suggestion_fingerprint emits. A shorter stand-in would be rejected by
# the marker pattern and every ownership case would pass for that reason.
FINGERPRINT = "0f1e2d3c4b5a6978"

SIGNATURES = anchor_signatures(PLAN_DIFF, content_source=tree_source())

# The commanded finding of the default step(), and so the scope of every command
# below. Derived, never a constant: the scope the executor passes is computed from
# the commanded finding the same way.
COMMANDED = {"path": "src/app.py", "line": 2}

# A live wrapper of ours, exactly as the reconciler writes one. Derived rather
# than restated: the two bodies now carry a per-run SHA and the paths delivered
# for, so a hand-written copy would keep matching after the format changed —
# which is how the supersede skip's own discriminator stopped testing anything.
WRAPPER_BODY = suggest.review_body(head_sha="reviewed-sha", paths=["src/app.py"])


def finding_key(path="src/app.py", line=2):
    return suggest.finding_identity({"path": path, "line": line}, SIGNATURES)


def step(path="src/app.py", line=2, old="def load(path):\n",
         new="def load(path=None):\n", note="make path optional"):
    # Hand-built Step: the fixture convenience ADR-0017 reserves to tests.
    return Step(id="s0", kind="suggest",
                args={"path": path, "line": line, "old": old, "new": new, "note": note})


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
        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        assert "```suggestion\ndef load(path=None):\n```" in body

    def test_content_carrying_a_three_backtick_run_gets_a_longer_fence(self):
        body = suggest.render_suggestion(
            step(new='doc = """\n```\n"""\n'), FINGERPRINT, METADATA)
        assert "````suggestion" in body
        assert "```suggestion" not in body.replace("````suggestion", "")

    def test_the_fence_is_always_longer_than_the_longest_run_in_the_content(self):
        for run in range(1, 12):
            new = f"x = '{'`' * run}'\n"
            body = suggest.render_suggestion(step(new=new), FINGERPRINT, METADATA)
            opener = next(line for line in body.split("\n") if line.endswith("suggestion"))
            assert len(opener) - len("suggestion") > run, f"a run of {run} can close {opener!r}"

    # An ODD number of closing runs in each case. The original input here had an
    # EVEN count, so the content re-opened and re-closed and the footer landed
    # outside a fence however short the opener was — the test passed with
    # fence_marker replaced by `return "```"`, i.e. with the whole property gone.
    @pytest.mark.parametrize("hostile", [
        "```\n",                                    # bare closer, nothing after
        "escape\n```\n</sub>\n",                    # closer then a forged footer
        "```suggestion\nrm -rf /\n",                # a second appliable opener
        "a\n```\nb\n```\nc\n```\n",                 # three runs
    ])
    def test_the_content_cannot_reach_outside_the_block(self, hostile):
        # The property, asked of the renderer's own output rather than of the
        # length arithmetic: whatever the model put in `new`, the notice and the
        # policy hash must still be OUTSIDE the fence. ADR-0005's visibility
        # requirement — swallowed into a code span, the disclosure is text a reader
        # skips rather than the disclosure it is.
        from verify import code_lines

        body = suggest.render_suggestion(step(new=hostile), FINGERPRINT, METADATA)
        rendered = code_lines(body)
        assert any(METADATA["policy"] in line for line, _ in rendered), "the hash must be present"
        for line, is_code in rendered:
            if METADATA["policy"] in line:
                assert not is_code, "the policy hash was swallowed into the suggestion block"
            if suggest.NOT_A_HUMAN_REVIEW.split(".")[0] in line:
                assert not is_code, "the AI notice was swallowed into the suggestion block"

    def test_every_line_of_the_content_is_inside_the_block(self):
        from verify import code_lines

        hostile = "first\n```\nmiddle\n````\nlast\n"
        body = suggest.render_suggestion(step(new=hostile), FINGERPRINT, METADATA)
        rendered = dict(code_lines(body))
        for content_line in ("first", "middle", "last"):
            assert rendered[content_line] is True, f"{content_line!r} escaped the suggestion block"

    def test_one_trailing_newline_is_consumed_by_the_closing_fence(self):
        # `new` is line-oriented (the placement check makes `old` start and end at
        # line boundaries), so "a\n" is ONE line. Emitting it verbatim before the
        # closer would suggest a second, empty line — a blank line the plan never
        # described, appended to the contributor's file.
        body = suggest.render_suggestion(step(new="a\n"), FINGERPRINT, METADATA)
        assert "```suggestion\na\n```" in body

    def test_a_line_ending_the_file_carries_no_terminator_of_its_own(self):
        body = suggest.render_suggestion(step(new="a"), FINGERPRINT, METADATA)
        assert "```suggestion\na\n```" in body

    def test_a_carriage_return_never_reaches_the_renderer(self):
        # The renderer consumes only the \n, so a CR would ride into the block as
        # content — and markdown would then fold it to LF before the applier saw
        # it, committing bytes the plan did not describe. The renderer is not where
        # that is caught: check_suggestion_new_survives_markdown refuses the step,
        # so the shape below is unreachable rather than handled.
        with pytest.raises(Rejection, match="carriage return"):
            check_suggestion_new_survives_markdown("a\r\n", "plan.steps[0].args.new")

    def test_an_empty_new_is_a_deletion_suggestion(self):
        # min_length 0 in the policy: a suggestion that removes the anchored
        # lines is an EMPTY block. A blank line inside it would suggest replacing
        # them with one empty line instead of deleting them.
        body = suggest.render_suggestion(step(new=""), FINGERPRINT, METADATA)
        assert "```suggestion\n```" in body

    def test_a_single_newline_suggests_one_blank_line_not_a_deletion(self):
        # The distinction the case above turns on: "" deletes the anchored lines,
        # "\n" replaces them with one empty line. Both would render as an empty
        # block under a naive terminator strip, and the difference is a line of
        # the contributor's file.
        body = suggest.render_suggestion(step(new="\n"), FINGERPRINT, METADATA)
        assert "```suggestion\n\n```" in body


class TestSuggestionBody:
    def test_the_marker_is_the_first_line(self):
        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        assert body.split("\n")[0] == suggest.suggestion_marker(FINGERPRINT)

    def test_the_footer_is_the_last_line(self):
        # Both are stripped BY POSITION when content is compared, so each must be
        # exactly one line and in its stated place.
        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        assert body.split("\n")[-1].startswith("<sub>")

    def test_the_note_is_carried_verbatim(self):
        body = suggest.render_suggestion(step(note="`load` needs a default"), FINGERPRINT, METADATA)
        assert "`load` needs a default" in body

    def test_it_says_it_is_not_a_human_review(self):
        # ADR-0005's visibility requirement, which ADR-0009 extends to the
        # suggestion comment: the notice and the policy hash both appear.
        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        assert "no approval" in body
        assert METADATA["policy"] in body

    def test_the_reviewed_sha_is_stamped(self):
        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        assert METADATA["sha"] in body

    def test_the_model_text_passes_the_markdown_policy(self):
        # The wrapper the executor writes stays inside the grammar it enforces on
        # the model, minus the two lines that are deliberately raw HTML (the
        # marker and the <sub> footer) and the suggestion fence, whose content is
        # file bytes rather than prose.
        from verify import check_markdown_field

        body = suggest.render_suggestion(step(), FINGERPRINT, METADATA)
        lines = body.split("\n")
        fence_at = next(i for i, line in enumerate(lines) if line.endswith("suggestion"))
        prose = "\n".join(lines[1:fence_at])
        check_markdown_field(prose, POLICY["markdown"], "suggestion_comment")


# ------------------------------------------------------------- identity ---


class TestSuggestionFingerprint:
    """Identity is the anchored CODE, never the model's prose and never the line.

    Measured on live pull requests in the extraction source: a prose-derived key
    never matched twice because the model reworded on essentially every run, so
    each run deleted and reposted every comment; and (path, line) is out because
    GitHub re-anchors a live comment when the diff shifts.
    """

    def test_rewording_the_note_keeps_the_same_identity(self):
        # THE point: the same fix explained differently is the same fix, and keeps
        # its comment and any human thread under it.
        first = step(note="make path optional")
        reworded = step(note="`load` should default its argument")
        assert (suggest.suggestion_fingerprint(first.args, SIGNATURES)
                == suggest.suggestion_fingerprint(reworded.args, SIGNATURES))

    def test_the_suggested_replacement_is_not_in_the_key(self):
        # `new` is what the fix DOES, not what it is about. A revised replacement
        # for the same defect keeps the thread.
        assert (suggest.suggestion_fingerprint(step(new="def load(path=''):\n").args, SIGNATURES)
                == suggest.suggestion_fingerprint(step(new="def load(path=None):\n").args, SIGNATURES))

    def test_a_different_anchor_is_a_different_suggestion(self):
        assert (suggest.suggestion_fingerprint(step(line=2, old="def load(path):\n").args, SIGNATURES)
                != suggest.suggestion_fingerprint(
                    step(line=3, old="    check(path)\n").args, SIGNATURES))

    def test_it_differs_on_path(self):
        other = step(path="src/util.py", line=1, old="def check(path):\n")
        assert (suggest.suggestion_fingerprint(step().args, SIGNATURES)
                != suggest.suggestion_fingerprint(other.args, SIGNATURES))

    def test_a_line_shift_with_unchanged_code_keeps_identity(self):
        # GitHub re-anchors across a push, so the line number moves while the code
        # does not. The window moves with the code, so the key must not move.
        head = tree_source({"x.py": b"alpha\ntarget()\nomega\n"})
        shifted_head = tree_source({"x.py": b"new\nnew2\nalpha\ntarget()\nomega\n"})
        before = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n+alpha\n+target()\n+omega\n"
        after = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                 "@@ -1,5 +1,5 @@\n+new\n+new2\n+alpha\n+target()\n+omega\n")
        args_before = {"path": "x.py", "line": 2, "old": "target()\n"}
        args_after = {"path": "x.py", "line": 4, "old": "target()\n"}
        assert (suggest.suggestion_fingerprint(
                    args_before, anchor_signatures(before, content_source=head))
                == suggest.suggestion_fingerprint(
                    args_after, anchor_signatures(after, content_source=shifted_head)))

    def test_changing_the_anchored_code_changes_identity(self):
        # The code the suggestion is about was edited, so it is about something
        # new: the old comment is retracted rather than silently reused.
        head = tree_source({"x.py": b"alpha\npopitem(last=True)\nomega\n"})
        edited = tree_source({"x.py": b"alpha\npopitem(last=False)\nomega\n"})
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1,3 +1,3 @@\n+alpha\n+popitem(last=True)\n+omega\n")
        args = {"path": "x.py", "line": 2, "old": "popitem(last=True)\n"}
        assert (suggest.suggestion_fingerprint(args, anchor_signatures(diff, content_source=head))
                != suggest.suggestion_fingerprint(args, anchor_signatures(diff, content_source=edited)))

    def test_nothing_the_model_authors_reaches_the_key(self):
        # The model must not be able to steer which comment its suggestion
        # matches, so the whole key comes from the anchor.
        base = suggest.suggestion_fingerprint(step().args, SIGNATURES)
        for overrides in ({"note": "x" * 900}, {"new": "y" * 900}, {"new": ""}):
            assert suggest.suggestion_fingerprint(step(**overrides).args, SIGNATURES) == base

    def test_the_replaced_EXTENT_is_part_of_the_identity(self):
        # Same anchored line, different number of lines replaced: two different
        # suggestions, because the region they overwrite differs. Keyed on the
        # window alone they collided, and a collision takes the PATCH branch —
        # which rewrites the body but CANNOT move the addressed range, so the
        # comment kept a one-line anchor while carrying a three-line replacement.
        one_line = step(line=2, old="def load(path):\n")
        three_lines = step(line=2, old="def load(path):\n    check(path)\n    return os.environ\n")
        assert (suggest.suggestion_fingerprint(one_line.args, SIGNATURES)
                != suggest.suggestion_fingerprint(three_lines.args, SIGNATURES))

    def test_two_anchors_sharing_a_window_are_still_distinguished(self):
        # A window=1 signature is not unique for periodic content: three lines
        # repeating give lines 3 and 8 the same window. `old` is what separates
        # them — it must byte-match the tree exactly once, so a verified plan's
        # `old` is a fact about the file rather than something the model chose.
        head = tree_source({"d.py": (b"def a():\n    if flag:\n        risky()\n        pass\n"
                                     b"    return A\ndef b():\n    if flag:\n        risky()\n"
                                     b"        pass\n    return B\n")})
        diff = ("diff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n@@ -1,10 +1,10 @@\n"
                " def a():\n     if flag:\n+        risky()\n+        pass\n+    return A\n"
                " def b():\n     if flag:\n+        risky()\n+        pass\n+    return B\n")
        signatures = anchor_signatures(diff, content_source=head)
        assert signatures[("d.py", 3)] == signatures[("d.py", 8)], "the windows really do collide"
        first = {"path": "d.py", "line": 3, "old": "        risky()\n        pass\n    return A\n"}
        second = {"path": "d.py", "line": 8, "old": "        risky()\n        pass\n    return B\n"}
        assert (suggest.suggestion_fingerprint(first, signatures)
                != suggest.suggestion_fingerprint(second, signatures))

    def test_reindenting_the_anchor_still_keeps_identity(self):
        # `old` joins the key canonicalized the same way the window is, so the
        # churn the design exists to prevent stays prevented: a reformat that
        # changes only indentation is the same suggestion.
        spaces = step(line=2, old="def load(path):\n")
        assert (suggest.suggestion_fingerprint(spaces.args, SIGNATURES)
                == suggest.suggestion_fingerprint(
                    step(line=2, old="  def load(path):  \n").args, SIGNATURES))

    def test_a_missing_signature_falls_back_to_the_anchored_bytes(self):
        # Provenance makes this unreachable for a verified plan (line must be in a
        # hunk), but identity must degrade rather than crash — and the fallback is
        # still the anchored CODE, not the line number.
        absent = step(path="nowhere.py", line=999)
        assert suggest.suggestion_fingerprint(absent.args, SIGNATURES)

    def test_the_fallback_still_distinguishes_two_anchors(self):
        # A fallback keyed on the line number would collide here; keyed on the
        # bytes it does not.
        first = step(path="nowhere.py", line=999, old="alpha\n")
        second = step(path="nowhere.py", line=999, old="omega\n")
        assert (suggest.suggestion_fingerprint(first.args, SIGNATURES)
                != suggest.suggestion_fingerprint(second.args, SIGNATURES))

    def test_the_fallback_ignores_reindentation_like_the_signature_does(self):
        first = step(path="nowhere.py", line=999, old="    alpha\n")
        second = step(path="nowhere.py", line=999, old="\talpha\n")
        assert (suggest.suggestion_fingerprint(first.args, SIGNATURES)
                == suggest.suggestion_fingerprint(second.args, SIGNATURES))


# ------------------------------------------------------------ ownership ---


def comment(cid, fingerprint=None, login=BOT, in_reply_to=None, body=None, args=None,
            for_finding=None):
    """A live review comment as GitHub would list it, wrapping one suggest step.

    The fingerprint DEFAULTS to the real key for the step's args rather than to a
    constant: a canned comment carrying an arbitrary key is a comment about some
    other suggestion, so every reconciliation case would exercise the fresh-post
    path and none would exercise a match.

    `for_finding` is the commanded finding the comment records: None defaults to
    the step's own anchor (a delivery for the finding it addresses), an explicit key
    makes it another command's delivery, and False renders no finding marker at all
    — a comment written before the marker existed.
    """
    canned = step(**(args or {}))
    if body is None:
        if for_finding is None:
            for_finding = finding_key(canned.args["path"], canned.args["line"])
        body = suggest.render_suggestion(
            canned, fingerprint or suggest.suggestion_fingerprint(canned.args, SIGNATURES), METADATA,
            None if for_finding is False else for_finding)
    # `path` as GitHub lists it, so the canned shape carries what the real listing
    # carries. The retraction scope reads our own marker rather than this field.
    return {"id": cid, "body": body, "user": {"login": login},
            "in_reply_to_id": in_reply_to, "path": canned.args["path"]}


class TestOwnership:
    """The only gate in front of every DELETE and PATCH the reconciler issues.

    Both halves are load-bearing: anyone can paste the marker into their own
    review comment, so the marker alone would let a crafted comment (or a
    fingerprint collision with a human's) steer the executor into editing or
    deleting someone else's words. The authenticated author is the backstop, and
    it comes from the write token itself rather than from configuration.
    """

    def test_our_own_comment_yields_its_fingerprint(self):
        assert suggest.owned_fingerprint(comment(1, "0123456789abcdef"), BOT) == "0123456789abcdef"

    def test_a_human_comment_carrying_our_marker_is_not_ours(self):
        # The hijack case. A human can copy the marker verbatim; the author check
        # is what keeps their words out of reach.
        assert suggest.owned_fingerprint(comment(1, login="a-human"), BOT) is None

    def test_a_different_bots_comment_is_not_ours(self):
        assert suggest.owned_fingerprint(comment(1, login="other-bot[bot]"), BOT) is None

    def test_a_comment_without_our_marker_is_not_ours(self):
        assert suggest.owned_fingerprint(
            {"id": 1, "body": "just a review comment", "user": {"login": BOT}}, BOT) is None

    def test_the_reviewers_own_inline_marker_is_not_ours(self):
        # The two executors post different comment kinds under one bot identity.
        # A marker family collision would have the suggestion reconciler
        # retracting the reviewer's findings, so the pattern is exact.
        assert suggest.owned_fingerprint(
            {"id": 1, "body": "<!-- aipr:0123456789abcdef -->\nfinding", "user": {"login": BOT}}, BOT) is None

    def test_the_marker_is_read_from_the_first_line_only(self):
        # Model text can legally contain the marker's literal text inside a code
        # fence (raw HTML rejects only OUTSIDE one) — and `new` is not
        # markdown-checked at all. A body-wide scan would let a crafted
        # suggestion present itself as a comment of ours on any fingerprint.
        body = suggest.render_suggestion(
            step(new=f"x = 1\n{suggest.suggestion_marker('deadbeefdeadbeef')}\n"), FINGERPRINT, METADATA)
        assert suggest.owned_fingerprint({"id": 1, "body": body, "user": {"login": BOT}}, BOT) == FINGERPRINT

    def test_a_marker_only_on_a_later_line_owns_nothing(self):
        body = f"not our first line\n{suggest.suggestion_marker('deadbeefdeadbeef')}\n"
        assert suggest.owned_fingerprint({"id": 1, "body": body, "user": {"login": BOT}}, BOT) is None

    def test_the_struck_marker_is_read_from_the_first_line_only(self):
        # The mirror failure: a fenced STRUCK marker in model text would make a
        # live comment look already-retracted, so a stale suggestion carrying a
        # human reply would never be struck.
        body = suggest.render_suggestion(
            step(new=f"x = 1\n{suggest.STRUCK_MARKER}\n"), FINGERPRINT, METADATA)
        assert not suggest.is_struck({"id": 1, "body": body, "user": {"login": BOT}})

    def test_a_struck_comment_is_recognised_after_retraction(self):
        struck = f"{suggest.suggestion_marker(FINGERPRINT)} {suggest.STRUCK_MARKER}\nnotice"
        assert suggest.is_struck({"id": 1, "body": struck})
        assert suggest.owned_fingerprint({"id": 1, "body": struck, "user": {"login": BOT}}, BOT) == FINGERPRINT

    def test_a_missing_body_or_user_is_not_ours(self):
        # GitHub returns null for a deleted author; neither may raise inside the
        # gate that decides what may be mutated.
        assert suggest.owned_fingerprint({"id": 1, "body": None, "user": None}, BOT) is None
        assert suggest.owned_fingerprint({"id": 1}, BOT) is None

    def test_an_unresolved_bot_login_owns_nothing(self):
        # post.resolve_bot_login fails closed rather than returning "", but this
        # is the gate, so it must not treat an empty login as matching a comment
        # whose author GitHub reported as null.
        assert suggest.owned_fingerprint({"id": 1, "body": comment(1)["body"], "user": None}, "") is None

    def test_an_empty_login_does_not_MATCH_an_empty_author(self):
        # The case the guard above actually exists for, and the one `user: None`
        # never reaches: comparing two values that can each be absent, where
        # unknown == unknown reads as OURS. An author GitHub reports as the empty
        # string is not this token's identity.
        ours = comment(1)
        ours["user"] = {"login": ""}
        assert suggest.owned_fingerprint(ours, "") is None

    def test_an_empty_login_owns_no_review_wrapper_either(self):
        # The same guard on the review side, where a mis-scope OVERWRITES a body:
        # this one destroys a human's review summary rather than a comment.
        assert not suggest.is_our_review(
            {"id": 1, "body": WRAPPER_BODY, "user": {"login": ""}}, "")


# ----------------------------------------------------------- retraction ---


@pytest.fixture
def api(monkeypatch):
    """Record the reviews-API calls the reconciler makes, driven by canned
    comments. Network is never reached."""
    calls = {"reviews": [], "deleted": [], "patched": []}

    def set_comments(comments):
        monkeypatch.setattr(suggest, "review_comments", lambda repo, pr: iter(list(comments)))

    def set_reviews(reviews):
        monkeypatch.setattr(suggest, "pull_reviews", lambda repo, pr: iter(list(reviews)))

    set_comments([])
    set_reviews([])
    monkeypatch.setattr(
        suggest, "submit_review",
        lambda repo, pr, body, comments, head_sha: calls["reviews"].append(
            {"body": body, "comments": comments, "head_sha": head_sha}),
    )
    monkeypatch.setattr(suggest, "delete_review_comment",
                        lambda repo, cid: calls["deleted"].append(cid))
    monkeypatch.setattr(suggest, "patch_review_comment",
                        lambda repo, cid, body: calls["patched"].append({"id": cid, "body": body}))
    monkeypatch.setattr(suggest, "update_review_body", lambda repo, pr, rid, body: None)
    monkeypatch.setattr(suggest, "minimize_review", lambda node: None)
    return calls, set_comments, set_reviews


def struck_body(body=None, note=None):
    """The body retract() actually leaves behind, obtained by RUNNING it.

    Derived from production rather than restating its format: a hand-written copy
    would keep passing after retract() changed shape, which is exactly how an
    already-struck case stops testing anything.
    """
    parent = comment(10, body=body)
    captured = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(suggest, "patch_review_comment",
                      lambda repo, cid, body: captured.update(body=body))
        patch.setattr(suggest, "delete_review_comment",
                      lambda repo, cid: pytest.fail("should have struck, not deleted"))
        # {10} = a human replied to the parent, so retract must strike, not delete.
        suggest.retract("o/r", parent, {10}, suggest.WITHDRAWN_NOTE if note is None else note)
    return captured["body"]


class TestRetractionScope:
    """A run delivers the findings ONE command named (ADR-0007, ADR-0013), so it
    may only withdraw what that command could have produced.

    `wanted` is one command's plan while the comment listing is the whole pull
    request, so an unscoped retraction treated every other finding's live
    suggestion as withdrawn — with a note saying it was no longer in the latest
    remediation, which is untrue: it was never this command's subject. Two
    commands on one pull request is the designed flow, not an edge case.
    """

    def other_finding_comment(self, cid=11):
        """A live suggestion for a DIFFERENT finding, on a different file."""
        return comment(cid, args={"path": "src/util.py", "line": 1,
                                  "old": "def check(path):\n",
                                  "new": "def check(path=None):\n"})

    def test_another_findings_suggestion_is_left_alone(self, api):
        calls, set_comments, _ = api
        set_comments([self.other_finding_comment()])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == []
        assert calls["patched"] == []

    def test_another_findings_thread_is_not_falsely_withdrawn(self, api):
        # The reply path is the expensive one: a struck body asserts the
        # suggestion left the latest remediation, and a human is reading it.
        calls, set_comments, _ = api
        set_comments([self.other_finding_comment(),
                      {"id": 12, "body": "good catch", "user": {"login": "a-human"},
                       "in_reply_to_id": 11}])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["patched"] == []
        assert calls["deleted"] == []

    def test_this_findings_withdrawn_suggestion_is_still_retracted(self, api):
        # The scoping must not disable retraction: a suggestion the CURRENT
        # command no longer makes still comes down.
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_a_superseded_suggestion_of_this_command_is_retracted(self, api):
        # Different anchor, SAME commanded finding: one finding's fix may move
        # between runs, so this command did produce that comment earlier and no
        # longer does. The marker records the commanded finding, not the
        # suggestion's own line, which is what makes this distinguishable from
        # another finding's comment that merely shares the file.
        calls, set_comments, _ = api
        set_comments([comment(10, args={"line": 3, "old": "    check(path)\n",
                                        "new": "    check(path or '')\n"},
                              for_finding=finding_key())])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_another_findings_suggestion_on_the_commanded_file_is_left_alone(self, api):
        # The defect this scope replaced a path with: two findings of one accepted
        # artifact routinely share a file, so `/fix 2` read `/fix 1`'s live
        # suggestion as withdrawn and deleted it.
        calls, set_comments, _ = api
        set_comments([comment(10, args={"line": 3, "old": "    check(path)\n",
                                        "new": "    check(path or '')\n"},
                              for_finding=finding_key(line=3))])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [], (
            "a command withdrew another finding's suggestion because they share a file"
        )
        assert calls["patched"] == []

    def test_a_comment_recording_no_finding_is_left_standing(self, api):
        # Fail closed on the COMMENT side too: a comment whose subject cannot be
        # established is not one this command can prove it owns the withdrawal of.
        # Deleting it would be guessing. This is also what makes the marker's
        # introduction safe — comments delivered before it existed carry no key and
        # are left standing rather than swept by the first command to follow.
        calls, set_comments, _ = api
        set_comments([comment(10, for_finding=False)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == []
        assert calls["patched"] == []

    def test_a_comment_whose_path_github_omits_is_still_retracted_by_its_command(self, api):
        # The scope no longer reads the listing's `path`, so a comment GitHub lists
        # without one is still withdrawable by the command that delivered it — its
        # subject comes from our own marker, which GitHub cannot omit.
        calls, set_comments, _ = api
        pathless = comment(10)
        del pathless["path"]
        set_comments([pathless])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_an_unknown_scope_and_an_unknown_subject_do_not_MATCH(self, api):
        # The trap in comparing two values that can each be absent: unknown ==
        # unknown reads as "in scope" and deletes. Neither absence is an identity,
        # so an unknown scope withdraws nothing even from an unknown subject.
        calls, set_comments, _ = api
        pathless = comment(10)
        del pathless["path"]
        set_comments([pathless])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=None)
        assert calls["deleted"] == []
        assert calls["patched"] == []

    def test_an_unknown_scope_withdraws_nothing(self, api):
        # Fail closed: if the scope cannot be established, the run may still POST
        # (its own suggestions are verified) but must not take anything down.
        #
        # steps=[] is what makes this reach the guard. With the comment's own step
        # in the plan its fingerprint is `wanted`, so it is never stale and the
        # retraction loop never runs — the assertions then hold for a reason that
        # has nothing to do with the scope, and the guard could be deleted whole.
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=None)
        assert calls["deleted"] == []
        assert calls["patched"] == []


class TestTheScopeIsASetOfFindingKeys:
    """ADR-0013: the scope is the SET of findings the command named, while what a
    COMMENT records stays one finding's key.

    That asymmetry is what pays for the widening: two findings of one artifact
    routinely share a file, so `/fix 1,3` in one file reconciles with {K1, K3} and
    the earlier `/fix 1` comment is retracted by the existing reconciler with no new
    mechanism.
    """

    OTHER = dict(path="src/app.py", line=3)

    def other_comment(self, cid=10, for_finding=None):
        """A live suggestion of ours on line 3, delivered for another finding."""
        return comment(cid, args={"line": 3, "old": "    check(path)\n",
                                  "new": "    check(path or '')\n"},
                       for_finding=for_finding or finding_key(**self.OTHER))

    def test_a_widening_command_retracts_the_narrower_ones_comment(self, api):
        # THE payoff case, and it needs no new machinery: `/fix 1` delivered a
        # comment for K1; `/fix 1,3` on the same file is one contiguous replacement,
        # so K1's old comment is stale, in scope, and comes down. Under a scope of
        # one key it would have been left standing beside the wider fix — half a
        # defect, independently applicable.
        calls, set_comments, _ = api
        set_comments([self.other_comment(10, for_finding=finding_key())])
        suggest.reconcile_suggestions(
            "o/r", 1, [step()], SIGNATURES, METADATA,
            bot_login=BOT, head_sha="reviewed-sha",
            commanded_finding_keys=(finding_key(), finding_key(**self.OTHER)))
        assert calls["deleted"] == [10]

    def test_every_key_in_the_set_is_in_scope(self, api):
        # The conjunct's mirror: a comment delivered for the SECOND commanded
        # finding is as much in scope as one for the first. A check reading only the
        # first key would leave it standing.
        calls, set_comments, _ = api
        set_comments([self.other_comment(10)])
        suggest.reconcile_suggestions(
            "o/r", 1, [step()], SIGNATURES, METADATA,
            bot_login=BOT, head_sha="reviewed-sha",
            commanded_finding_keys=(finding_key(), finding_key(**self.OTHER)))
        assert calls["deleted"] == [10]

    def test_a_finding_outside_the_set_is_still_left_alone(self, api):
        # The widening must not become "retract everything". A third finding's
        # comment on the same file is untouched by a command naming the other two.
        calls, set_comments, _ = api
        set_comments([comment(10, args={"line": 4, "old": "    return os.environ\n",
                                        "new": "    return dict(os.environ)\n"},
                              for_finding=finding_key(line=4))])
        suggest.reconcile_suggestions(
            "o/r", 1, [step()], SIGNATURES, METADATA,
            bot_login=BOT, head_sha="reviewed-sha",
            commanded_finding_keys=(finding_key(), finding_key(**self.OTHER)))
        assert calls["deleted"] == [], (
            "a multi-finding command withdrew a comment for a finding it never named"
        )
        assert calls["patched"] == []

    def test_an_empty_scope_withdraws_nothing(self, api):
        # The empty tuple must read as None does. A set-membership check on an empty
        # set is False for every comment, and that is the fail-closed direction; the
        # test exists because the two spellings could easily diverge.
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                     bot_login=BOT, head_sha="reviewed-sha",
                                     commanded_finding_keys=())
        assert calls["deleted"] == []
        assert calls["patched"] == []

    def test_a_comment_records_ONE_finding_not_the_whole_set(self, api):
        # The marker stays per finding (ADR-0013): a comment speaks for exactly one,
        # and what carries a set is the command. So the posted body carries the
        # FIRST commanded key and not a fold of the set — otherwise every existing
        # comment's key would change the first time a command named two findings, and
        # the reconciler would read them all as unowned.
        _, set_comments, set_reviews = api
        posted = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(suggest, "submit_review",
                          lambda repo, pr, body, comments, *, head_sha: posted.extend(comments))
            suggest.reconcile_suggestions(
                "o/r", 1, [step()], SIGNATURES, METADATA,
                bot_login=BOT, head_sha="reviewed-sha",
                commanded_finding_keys=(finding_key(), finding_key(**self.OTHER)))
        first_line = posted[0]["body"].split("\n")[0]
        assert suggest.finding_marker(finding_key()) in first_line
        assert suggest.finding_marker(finding_key(**self.OTHER)) not in first_line

    def test_the_recorded_key_does_not_depend_on_the_sets_spelling(self, api):
        # Deterministic, because the executor hands over the canonical ordinal order:
        # two runs of ONE command must record the same representative, or the second
        # run reads the first's comment as another finding's and leaves it standing
        # while posting a duplicate.
        def recorded(keys):
            posted = []
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(suggest, "submit_review",
                              lambda repo, pr, body, comments, *, head_sha: posted.extend(comments))
                suggest.reconcile_suggestions(
                    "o/r", 1, [step()], SIGNATURES, METADATA,
                    bot_login=BOT, head_sha="reviewed-sha", commanded_finding_keys=keys)
            return posted[0]["body"].split("\n")[0]

        keys = (finding_key(), finding_key(**self.OTHER))
        assert recorded(keys) == recorded(keys)


class TestRetraction:
    def test_a_suggestion_with_no_reply_is_deleted(self, api):
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]
        assert calls["patched"] == []

    def test_a_suggestion_a_human_replied_to_is_struck_not_deleted(self, api):
        # DELETE would orphan the reply: it survives, but is promoted to a
        # standalone comment severed from its context.
        calls, set_comments, _ = api
        set_comments([comment(10),
                      {"id": 11, "body": "I disagree", "user": {"login": "a-human"},
                       "in_reply_to_id": 10}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == []
        assert len(calls["patched"]) == 1
        assert calls["patched"][0]["id"] == 10

    def test_our_own_reply_does_not_block_deletion(self, api):
        # Only a HUMAN reply is discussion worth preserving.
        calls, set_comments, _ = api
        set_comments([comment(10),
                      {"id": 11, "body": "ours", "user": {"login": BOT}, "in_reply_to_id": 10}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_a_reply_to_a_different_comment_does_not_block_deletion(self, api):
        calls, set_comments, _ = api
        set_comments([comment(10),
                      {"id": 11, "body": "about something else", "user": {"login": "a-human"},
                       "in_reply_to_id": 99}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_a_reply_landing_during_the_run_still_blocks_the_delete(self, api, monkeypatch):
        # The listing happens before the POST, the supersede pass and the re-render
        # PATCHes. A human replying in that window was absent from the snapshot, so
        # retract read "no discussion" and DELETED their reply's parent — the
        # orphaned-reply outcome its own docstring gives as the reason to strike
        # instead. Re-read before the deletes, which makes the window the last read
        # rather than the whole run.
        calls, _, _ = api
        pages = [[comment(10)],
                 [comment(10), {"id": 11, "body": "wait", "user": {"login": "a-human"},
                                "in_reply_to_id": 10}]]
        monkeypatch.setattr(suggest, "review_comments", lambda repo, pr: iter(pages.pop(0)))
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [], "the late reply's parent was deleted"
        assert len(calls["patched"]) == 1

    def test_a_failing_reply_re_read_keeps_the_earlier_answer(self, api, monkeypatch):
        # The re-read is a narrowing, not a new dependency: a 500 there must leave
        # the pre-write snapshot in force rather than lose the retraction.
        calls, _, _ = api
        pages = [[comment(10)]]

        def listing(repo, pr):
            if pages:
                return iter(pages.pop(0))
            raise RuntimeError("500")

        monkeypatch.setattr(suggest, "review_comments", listing)
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_the_replies_are_not_re_read_when_nothing_is_stale(self, api, monkeypatch):
        # One extra call, and only on the path that needs it: an unchanged plan
        # must stay a single listing.
        calls, _, _ = api
        listings = []

        def listing(repo, pr):
            listings.append(pr)
            return iter([comment(10)])

        monkeypatch.setattr(suggest, "review_comments", listing)
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(listings) == 1

    def test_a_reaction_does_not_pin_a_stale_suggestion(self, api):
        # Reactions deliberately do not count: one 👍 would pin a stale
        # suggestion forever, and a reaction is not discussion.
        calls, set_comments, _ = api
        pinned = comment(10)
        pinned["reactions"] = {"total_count": 3, "+1": 3}
        set_comments([pinned])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == [10]

    def test_the_note_does_not_claim_the_defect_was_fixed(self):
        # "Resolve" asserts the defect was addressed, which the harness cannot
        # know — a suggestion can vanish because the model had an off run.
        note = suggest.WITHDRAWN_NOTE.lower()
        assert "no longer" in note
        assert "does not claim" in note
        for word in ("resolved", "fixed it", "has been addressed."):
            assert word not in note

    def test_nothing_resolves_a_conversation(self):
        # The GraphQL resolve mutation must appear nowhere in the harness: the
        # bot states only what it knows.
        source = (Path(__file__).parent.parent / "src" / "aceiro").glob("*.py")
        for path in source:
            assert "resolveReviewThread" not in path.read_text(), path

    def test_the_notice_is_above_the_struck_body(self):
        # Below it, an unclosed model fence renders the notice as literal code —
        # and the struck marker then stops any later run from repairing it. Above
        # the body nothing the model wrote can capture it.
        body = struck_body()
        lines = body.split("\n")
        assert suggest.STRUCK_MARKER in lines[0]
        assert lines[1] == suggest.WITHDRAWN_NOTE

    def test_the_struck_marker_joins_the_fingerprint_on_line_one(self):
        # Both identity signals in the one place model text cannot reach: the
        # retraction must not cost the comment its identity, or the next run sees
        # an unknown comment and posts a second one alongside it.
        body = struck_body()
        assert suggest.is_struck({"body": body})
        assert (suggest.owned_fingerprint({"body": body, "user": {"login": BOT}}, BOT)
                == suggest.suggestion_fingerprint(step().args, SIGNATURES))

    def test_the_suggestion_fence_is_left_unstruck(self):
        # `~~` inside a fence renders literally, so striking there would corrupt
        # the quoted code rather than crossing it out.
        body = struck_body()
        assert "def load(path=None):" in body
        assert "~~def load(path=None):~~" not in body

    def test_the_visible_prose_is_struck(self):
        # Asked of the RENDERED result rather than of the wrapper's spelling, so
        # the property survives a change of wrapper: the note's text must come out
        # inside a strikethrough element.
        from verify import _PARSER

        body = struck_body(body=comment(10, args={"note": "make path optional"})["body"])
        html = _PARSER.render(body)
        assert "<s>make path optional</s>" in html or "<del>make path optional</del>" in html

    @pytest.mark.parametrize("note", [
        "~~Deprecated~~ - use the new helper instead.",   # legal: `s` is allowlisted
        "~/.config is the wrong path here",               # a leading tilde, plainly
        "~ this too",
    ])
    def test_striking_a_note_never_opens_a_fence(self, note):
        # `~~line~~` around a line already beginning with `~` yields `~~~…`, which
        # CommonMark reads as a TILDE FENCE opener: everything below — the
        # suggestion block and the <sub> attribution line — is swallowed into one
        # code block, and close_open_fence then appends its marker AFTER the footer
        # rather than before it. The retraction's own transform manufacturing a
        # fence that captures executor-authored text is exactly the hazard the
        # notice-above-body rule exists to prevent, and ADR-0005's disclosure is
        # what it eats.
        from verify import code_lines

        body = struck_body(body=comment(10, args={"note": note})["body"])
        for line, is_code in code_lines(body):
            if line.startswith("<sub>"):
                assert not is_code, "the attribution footer was swallowed into a fence"
            if METADATA["policy"] in line:
                assert not is_code, "the policy hash was swallowed into a fence"

    def test_a_struck_note_beginning_with_a_tilde_is_still_struck(self):
        # Not fixed by giving up on the line: it is visible prose, so it must be
        # crossed out like any other.
        body = struck_body(body=comment(10, args={"note": "~/.config is wrong"})["body"])
        assert "~/.config is wrong" in body
        assert "~~~" not in body

    def test_an_already_struck_suggestion_is_left_alone(self, api):
        # Without this a re-run strikes and re-appends the note every time.
        calls, set_comments, _ = api
        already = comment(10, body=struck_body())
        set_comments([already,
                      {"id": 11, "body": "hm", "user": {"login": "a-human"}, "in_reply_to_id": 10}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["patched"] == []
        assert calls["deleted"] == []

    def test_an_unclosed_fence_in_the_retracted_body_is_closed(self):
        # Defence in depth: check_plan_markdown rejects a `note` ending inside a
        # fence, so a verified plan cannot reach this — but the body being struck
        # is read back from GitHub, where a previous run on a previous grammar may
        # have written it.
        hostile = f"{suggest.suggestion_marker(FINGERPRINT)}\nnote\n\n```python\nunclosed"
        body = struck_body(body=hostile)
        assert unterminated_fence(body) is None

    def test_a_human_comment_carrying_our_marker_is_never_retracted(self, api):
        calls, set_comments, _ = api
        set_comments([comment(10, login="a-human")])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["deleted"] == []
        assert calls["patched"] == []

    def test_every_mutation_targets_a_comment_we_own(self, api):
        # The whole-reconciler statement of the ownership gate: whatever the PR
        # holds, nothing not ours is mutated.
        calls, set_comments, _ = api
        ours = comment(10)
        set_comments([
            ours,
            comment(11, login="a-human"),
            {"id": 12, "body": "plain human comment", "user": {"login": "a-human"}},
            {"id": 13, "body": "<!-- aipr:0f1e2d3c4b5a6978 -->\nthe reviewer's finding",
             "user": {"login": BOT}},
            {"id": 14, "body": "bot comment with no marker", "user": {"login": BOT}},
        ])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        touched = set(calls["deleted"]) | {p["id"] for p in calls["patched"]}
        assert touched == {10}


# ------------------------------------------------------- ordering + churn ---


class TestPostBeforeRetract:
    """The review POST is atomic: an unresolvable line 422s and creates ZERO
    comments (verified live in the extraction source). So failing there must leave
    the pull request's existing comments standing, never already deleted.
    """

    def test_a_failing_post_deletes_nothing(self, api, monkeypatch):
        calls, set_comments, _ = api
        order = []
        set_comments([comment(10, args={"line": 3, "old": "    check(path)\n"})])
        monkeypatch.setattr(suggest, "delete_review_comment",
                            lambda repo, cid: order.append(("delete", cid)))
        monkeypatch.setattr(suggest, "patch_review_comment",
                            lambda repo, cid, body: order.append(("patch", cid)))

        def failing_post(repo, pr, body, comments, head_sha):
            order.append(("post", len(comments)))
            raise urllib.error.HTTPError("/x", 422, "Line could not be resolved", {}, None)

        monkeypatch.setattr(suggest, "submit_review", failing_post)

        with pytest.raises(urllib.error.HTTPError):
            suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                          bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))

        assert ("post", 1) in order
        assert not [entry for entry in order if entry[0] in ("delete", "patch")], (
            f"a comment was mutated before the POST failed: {order}"
        )

    def test_a_failing_post_does_not_leave_a_restore_applied(self, api, monkeypatch):
        # The restore path runs AFTER the POST for this reason: restoring first
        # would leave a stale comment live if the POST then 422'd.
        calls, set_comments, _ = api
        order = []
        returning = comment(10, body=struck_body())
        set_comments([returning,
                      {"id": 11, "body": "hm", "user": {"login": "a-human"}, "in_reply_to_id": 10}])
        monkeypatch.setattr(suggest, "patch_review_comment",
                            lambda repo, cid, body: order.append(("patch", cid)))

        def failing_post(repo, pr, body, comments, head_sha):
            order.append(("post", len(comments)))
            raise urllib.error.HTTPError("/x", 422, "Line could not be resolved", {}, None)

        monkeypatch.setattr(suggest, "submit_review", failing_post)

        with pytest.raises(urllib.error.HTTPError):
            suggest.reconcile_suggestions(
                "o/r", 1,
                [step(), step(line=3, old="    check(path)\n", new="    check(path or '')\n")],
                SIGNATURES, METADATA, bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))

        assert ("post", 1) in order
        assert not [entry for entry in order if entry[0] == "patch"], (
            f"a restore was applied before the POST failed: {order}"
        )


class FakeGitHub:
    """A pull request's suggestion comments, mutated by the calls the reconciler
    actually makes.

    Exists so a test can drive reconcile_suggestions MORE THAN ONCE and have the
    second run see what the first really did. Every other case here hands run 2 a
    hand-authored picture of run 1's output, which cannot catch a bug whose trigger
    is inter-run state: a hand-written expected state encodes what the author
    BELIEVES the previous run posted. Here it is whatever production wrote.

    Only the three mutations the reconciler issues are modelled, each the way
    GitHub behaves: POST appends comments with fresh ids, PATCH replaces a body,
    DELETE removes the comment while any human reply survives as a now-parentless
    comment (all verified live in the extraction source).
    """

    def __init__(self, comments=()):
        self.comments = list(comments)
        self.next_id = 100
        self.posts = []

    def install(self, monkeypatch):
        monkeypatch.setattr(suggest, "review_comments", lambda repo, pr: iter(list(self.comments)))
        monkeypatch.setattr(suggest, "pull_reviews", lambda repo, pr: iter([]))
        monkeypatch.setattr(suggest, "submit_review", self._submit)
        monkeypatch.setattr(suggest, "patch_review_comment", self._patch)
        monkeypatch.setattr(suggest, "delete_review_comment", self._delete)
        return self

    def _submit(self, repo, pr, body, comments, head_sha):
        self.posts.append({"body": body, "comments": comments, "head_sha": head_sha})
        for posted in comments:
            self.comments.append({
                "id": self.next_id, "body": posted["body"], "user": {"login": BOT},
                "in_reply_to_id": None, "line": posted["line"], "path": posted["path"],
            })
            self.next_id += 1

    def _patch(self, repo, cid, body):
        for existing in self.comments:
            if existing["id"] == cid:
                existing["body"] = body
                return
        raise AssertionError(f"patched a comment that does not exist: {cid}")

    def _delete(self, repo, cid):
        before = len(self.comments)
        # A human reply outlives its parent, promoted to top-level (verified live).
        for other in self.comments:
            if other.get("in_reply_to_id") == cid:
                other["in_reply_to_id"] = None
        self.comments = [c for c in self.comments if c["id"] != cid]
        if len(self.comments) == before:
            raise AssertionError(f"deleted a comment that does not exist: {cid}")

    def ours(self):
        return [c for c in self.comments if suggest.owned_fingerprint(c, BOT)]


def run(github, steps, metadata=None):
    suggest.reconcile_suggestions("o/r", 1, steps, SIGNATURES, metadata or METADATA,
                                  bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))


class TestAcrossRuns:
    """Driven against a FakeGitHub carrying run 1's real output into run 2.

    Unreachable by any other tier: a single reconcile call has no previous run to
    disagree with, and this is where the churn defects the reference
    implementation measured actually live.
    """

    def test_an_unchanged_plan_re_run_writes_nothing(self, monkeypatch):
        # The guard on the content comparison. Re-rendering every matched comment
        # would rewrite all of them on every run, because the footer's [run] URL
        # differs per run — the churn this whole design removes.
        github = FakeGitHub().install(monkeypatch)
        run(github, [step()])
        assert len(github.posts) == 1
        assert len(github.ours()) == 1

        patched = []
        monkeypatch.setattr(suggest, "patch_review_comment",
                            lambda repo, cid, body: patched.append(cid))
        run(github, [step()], metadata={**METADATA, "run_url": "https://github.com/o/r/actions/runs/999"})

        assert len(github.posts) == 1, "reposted an unchanged suggestion"
        assert patched == [], "rewrote an unchanged comment because the run URL moved"

    def test_a_body_github_returns_with_crlf_is_not_rewritten(self, monkeypatch):
        # Only one side of the comparison came back from GitHub: this harness sends
        # LF and the API returns CRLF (post.check_marker's docstring records the
        # same measurement). Comparing them raw, an unchanged comment differed from
        # its own re-render at every interior newline and was rewritten every run,
        # without bound — the exact churn comment_content exists to prevent.
        calls = {"reviews": [], "deleted": [], "patched": []}
        served = comment(10)
        served["body"] = served["body"].replace("\n", "\r\n")
        monkeypatch.setattr(suggest, "review_comments", lambda repo, pr: iter([served]))
        monkeypatch.setattr(suggest, "pull_reviews", lambda repo, pr: iter([]))
        monkeypatch.setattr(suggest, "submit_review",
                            lambda repo, pr, body, comments, head_sha: calls["reviews"].append(1))
        monkeypatch.setattr(suggest, "patch_review_comment",
                            lambda repo, cid, body: calls["patched"].append(cid))
        monkeypatch.setattr(suggest, "delete_review_comment",
                            lambda repo, cid: calls["deleted"].append(cid))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["patched"] == []
        assert calls["reviews"] == []

    def test_a_reworded_note_updates_in_place(self, monkeypatch):
        # Identity is the anchor, so the comment matches; its CONTENT changed, so
        # it must be re-rendered rather than left saying the old thing.
        github = FakeGitHub().install(monkeypatch)
        run(github, [step(note="make path optional")])
        original_id = github.ours()[0]["id"]

        run(github, [step(note="`load` should default its argument")])

        assert [c["id"] for c in github.ours()] == [original_id], "the comment was replaced, not updated"
        assert len(github.posts) == 1, "a reworded note reposted the suggestion"
        assert "`load` should default its argument" in github.ours()[0]["body"]

    def test_a_revised_replacement_updates_in_place(self, monkeypatch):
        github = FakeGitHub().install(monkeypatch)
        run(github, [step(new="def load(path=None):\n")])
        original_id = github.ours()[0]["id"]

        run(github, [step(new="def load(path=''):\n")])

        assert [c["id"] for c in github.ours()] == [original_id]
        assert "def load(path=''):" in github.ours()[0]["body"]
        assert "def load(path=None):" not in github.ours()[0]["body"], (
            "the withdrawn replacement is still on the pull request"
        )

    def test_a_returning_suggestion_restores_its_struck_comment(self, monkeypatch):
        # A suggestion retracted on one run and produced again on the next: the
        # struck comment holds the human thread, and a key match proves it is about
        # the same code, so it is restored rather than left contradicting the
        # review that carries it.
        github = FakeGitHub().install(monkeypatch)
        run(github, [step()])
        parent_id = github.ours()[0]["id"]
        github.comments.append({"id": 900, "body": "why?", "user": {"login": "a-human"},
                                "in_reply_to_id": parent_id})

        run(github, [])
        assert suggest.is_struck(github.ours()[0]), "a replied-to comment was not struck"

        run(github, [step()])
        assert [c["id"] for c in github.ours()] == [parent_id], "the comment was replaced, not restored"
        assert not suggest.is_struck(github.ours()[0]), "the restored comment is still marked struck"
        assert len(github.posts) == 1, "the returning suggestion was reposted alongside its own comment"

    def test_a_retracted_suggestion_does_not_orphan_a_human_reply(self, monkeypatch):
        github = FakeGitHub().install(monkeypatch)
        run(github, [step()])
        parent_id = github.ours()[0]["id"]
        github.comments.append({"id": 900, "body": "why?", "user": {"login": "a-human"},
                                "in_reply_to_id": parent_id})

        run(github, [])

        assert [c["id"] for c in github.ours()] == [parent_id]
        reply = next(c for c in github.comments if c["id"] == 900)
        assert reply["in_reply_to_id"] == parent_id, "the human's reply was orphaned"

    def test_a_grown_hunk_does_not_churn_the_comment(self, monkeypatch):
        # The window-source contract, observed where it matters. An unrelated push
        # grows the hunk around the anchored line; under a diff-derived window the
        # signature moved, so the executor deleted a live thread and reposted the
        # same suggestion.
        head = tree_source({"x.py": b"alpha\ntarget()\nomega\ntail()\n"})
        narrow = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                  "@@ -2,1 +2,1 @@\n+target()\n")
        wide = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1,4 +1,4 @@\n alpha\n+target()\n omega\n tail()\n")
        moved = step(path="x.py", line=2, old="target()\n", new="target(fixed)\n")

        github = FakeGitHub().install(monkeypatch)
        suggest.reconcile_suggestions(
            "o/r", 1, [moved], anchor_signatures(narrow, content_source=head), METADATA,
            bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        original_id = github.ours()[0]["id"]

        suggest.reconcile_suggestions(
            "o/r", 1, [moved], anchor_signatures(wide, content_source=head), METADATA,
            bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))

        assert [c["id"] for c in github.ours()] == [original_id], (
            "a grown hunk deleted the live comment and reposted the same suggestion"
        )
        assert len(github.posts) == 1


class TestReviewWrappers:
    def test_our_previous_wrapper_is_rewritten_and_minimized(self, api, monkeypatch):
        calls, _, set_reviews = api
        updated, minimized = [], []
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": BOT},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: updated.append({"id": rid, "body": body}))
        monkeypatch.setattr(suggest, "minimize_review", lambda node: minimized.append(node))

        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))

        assert [u["id"] for u in updated] == [7]
        assert updated[0]["body"] == suggest.superseded_review_body(
            {"body": WRAPPER_BODY}), "the spent body is not the one the module writes"
        assert minimized == ["PRR_7"]

    def test_a_human_review_is_never_touched(self, api, monkeypatch):
        _, _, set_reviews = api
        updated = []
        set_reviews([{"id": 7, "body": "looks good to me", "user": {"login": "a-human"},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: updated.append(rid))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert updated == []

    def test_a_human_review_carrying_our_marker_is_never_touched(self, api, monkeypatch):
        # This gates a body OVERWRITE, so a mis-scope destroys a human's review
        # summary. The marker is copyable; the author check is the backstop.
        _, _, set_reviews = api
        updated = []
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": "a-human"},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: updated.append(rid))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert updated == []

    def test_an_already_superseded_wrapper_is_left_alone(self, api, monkeypatch):
        _, _, set_reviews = api
        updated = []
        set_reviews([{"id": 7,
                      "body": suggest.superseded_review_body({"body": WRAPPER_BODY}),
                      "user": {"login": BOT}, "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: updated.append(rid))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert updated == []

    def test_a_minimize_the_bot_cannot_perform_does_not_fail_the_run(self, api, monkeypatch):
        # Cosmetic tidying in front of an atomic POST: a permission the bot turns
        # out not to have must cost a collapsed timeline entry, never a delivery.
        calls, _, set_reviews = api
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": BOT},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "minimize_review",
                            lambda node: (_ for _ in ()).throw(RuntimeError("Resource not accessible")))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(calls["reviews"]) == 1, "a failed minimize cost the delivery"

    def test_a_failing_body_rewrite_does_not_fail_the_run(self, api, monkeypatch):
        calls, _, set_reviews = api
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": BOT},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: (_ for _ in ()).throw(RuntimeError("403")))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(calls["reviews"]) == 1

    def test_a_failing_review_list_does_not_fail_the_run(self, api, monkeypatch):
        calls, _, _ = api
        monkeypatch.setattr(suggest, "pull_reviews",
                            lambda repo, pr: (_ for _ in ()).throw(RuntimeError("500")))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(calls["reviews"]) == 1

    def test_a_review_the_api_lists_without_an_id_does_not_fail_the_run(self, api, monkeypatch):
        # The handler must not raise what it is handling: `review["id"]` inside the
        # except f-string re-raised the KeyError the try had caught, out of a
        # function whose whole contract is never to fail — and this runs BEFORE the
        # POST, so cosmetic tidying took the delivery with it.
        calls, _, set_reviews = api
        set_reviews([{"user": {"login": BOT}, "body": WRAPPER_BODY, "node_id": "N2"}])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(calls["reviews"]) == 1, "the suggestions were delivered anyway"

    def test_superseding_happens_before_the_new_review_is_posted(self, api, monkeypatch):
        # So "every wrapper except the newest" needs no id bookkeeping: at that
        # moment the newest does not exist yet.
        _, _, set_reviews = api
        order = []
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": BOT},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: order.append("supersede"))
        monkeypatch.setattr(suggest, "submit_review",
                            lambda repo, pr, body, comments, head_sha: order.append("post"))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert order == ["supersede", "post"]

    def test_nothing_is_superseded_when_there_is_nothing_to_post(self, api, monkeypatch):
        _, set_comments, set_reviews = api
        updated = []
        set_comments([comment(10)])
        set_reviews([{"id": 7, "body": WRAPPER_BODY, "user": {"login": BOT},
                      "node_id": "PRR_7"}])
        monkeypatch.setattr(suggest, "update_review_body",
                            lambda repo, pr, rid, body: updated.append(rid))
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert updated == []


class TestTheWrapperIsSelfDating:
    """ADR-0009's addendum B. `REVIEW_BODY` was the ONLY artefact the harness posts
    with no reviewed SHA — the suggestion comment's footer, the reviewer's sticky
    comment and the follow-up pull-request body all carry one — so after a push it
    was a live, undated claim pointing at comments GitHub had marked outdated.

    It matters more than symmetry: an artefact that never claims a currency never
    needs a later run to correct it, and `/fix` is commanded so there may BE no later
    run — `supersede_previous_reviews` executes only inside `if fresh:`.
    """

    def test_the_live_wrapper_carries_the_head_it_delivered_for(self):
        body = suggest.review_body(head_sha="abc1234", paths=["src/app.py"])
        assert "abc1234" in body

    def test_the_live_wrapper_carries_the_paths_it_delivered_for(self):
        body = suggest.review_body(head_sha="abc1234", paths=["src/b.py", "src/a.py"])
        assert "`src/a.py`" in body and "`src/b.py`" in body

    def test_the_paths_are_sorted_and_deduplicated(self):
        # Two suggestions on one path are refused, but a plan reaching here with a
        # repeated path must not produce a body naming it twice — and the order must
        # not vary with the plan's step order, or two runs of one command write two
        # different bodies.
        assert suggest.review_body(head_sha="s", paths=["b.py", "a.py", "b.py"]) == \
            suggest.review_body(head_sha="s", paths=["a.py", "b.py"])

    def test_the_posted_wrapper_names_the_verified_head_and_the_plans_paths(self, api):
        # Through the reconciler, so the values are the ones production passes rather
        # than the ones a test chose: the head the plan was VERIFIED against, and the
        # paths of the steps actually posted.
        _, _, _ = api
        posted = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(suggest, "submit_review",
                          lambda repo, pr, body, comments, *, head_sha: posted.append(body))
            suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                          bot_login=BOT, head_sha="verified-head",
                                          commanded_finding_keys=(finding_key(),))
        assert "verified-head" in posted[0]
        assert "`src/app.py`" in posted[0]

    def test_the_live_wrapper_still_carries_the_ownership_marker_on_line_one(self):
        # Ownership is unchanged: marker plus resolved bot login. The stamp is
        # appended to that line, not put in place of it.
        body = suggest.review_body(head_sha="s", paths=["a.py"])
        assert body.split("\n")[0].startswith(suggest.REVIEW_MARKER)
        assert suggest.is_our_review({"body": body, "user": {"login": BOT}}, BOT)

    def test_the_live_wrapper_is_not_mistaken_for_a_spent_one(self):
        # The discriminator must separate the two bodies, or the pass either rewrites
        # the wrapper it just posted or never rewrites anything.
        assert not suggest.is_superseded(
            {"body": suggest.review_body(head_sha="s", paths=["a.py"])})


class TestTheSpentWrapperStatesOnlyWhatItsRunEstablished:
    """The DEFECT addendum B was written about: the supersede pass takes no scope, so
    a wrapper superseded by a run scoped to another file had its body claim "any from
    this one that still apply are in the current suggestion comments".

    Measured false on `svozza/artel` #61 — the `version.rs` wrapper was superseded by
    a run scoped to `server.rs`, which never looked at `version.rs`, never re-derived
    that finding and could not have re-posted its suggestion.
    """

    # An EARLIER head than the reconciler is called with below, since the property
    # is that the spent body keeps the wrapper's own facts rather than the
    # superseding run's.
    OLD_HEAD = "cd8fa363"

    def spent(self, head_sha="abc1234", paths=("src/app.py",)):
        return suggest.superseded_review_body(
            {"body": suggest.review_body(head_sha=head_sha, paths=list(paths))})

    def test_the_spent_body_no_longer_claims_anything_was_carried_forward(self):
        body = self.spent()
        assert "still apply are in the current suggestion comments" not in body
        # And says so positively, rather than merely omitting it: a reader has to
        # know that the silence is deliberate.
        assert "did not evaluate" in body

    def test_the_spent_body_states_what_THAT_wrapper_delivered_for(self):
        # Recovered from the wrapper's own stamp, NOT from the rewriting run. Taking
        # this run's facts would be the same over-claim in a new place — a body
        # asserting a scope its own run never had.
        body = self.spent(head_sha=self.OLD_HEAD, paths=("src/version.rs",))
        assert self.OLD_HEAD in body
        assert "`src/version.rs`" in body

    def test_a_wrapper_from_another_run_is_not_relabelled_with_this_runs_facts(self, api):
        # The property stated as the production scenario: a run scoped to server.rs
        # supersedes a wrapper delivered for version.rs, and what the spent body says
        # must be version.rs at ITS head.
        _, _, set_reviews = api
        earlier = suggest.review_body(head_sha=self.OLD_HEAD, paths=["src/version.rs"])
        set_reviews([{"id": 7, "body": earlier, "user": {"login": BOT}, "node_id": "PRR_7"}])
        updated = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(suggest, "update_review_body",
                          lambda repo, pr, rid, body: updated.append(body))
            patch.setattr(suggest, "minimize_review", lambda node: None)
            suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                          bot_login=BOT, head_sha="reviewed-sha",
                                          commanded_finding_keys=(finding_key(),))
        assert self.OLD_HEAD in updated[0] and "`src/version.rs`" in updated[0]
        assert "reviewed-sha" not in updated[0], (
            "the spent body was relabelled with the SUPERSEDING run's head, so it claims a "
            "scope its own run never had"
        )
        assert "src/app.py" not in updated[0]

    def test_a_wrapper_with_no_stamp_says_the_scope_is_unrecorded(self):
        # Posted before the stamp existed. Fail-closed on a CLAIM rather than on an
        # effect: an unknown scope stated as a known one is exactly the over-claim
        # this change removes, so it is said out loud instead.
        body = suggest.superseded_review_body(
            {"body": f"{suggest.REVIEW_MARKER}\nan older wrapper"})
        assert "did not record the scope" in body

    def test_the_spent_body_is_recognised_as_spent(self):
        # The discriminator, which is what the constant comparison used to be. Both
        # bodies now carry a per-run SHA, so equality against a constant could never
        # hold again and every run would rewrite and re-minimize every wrapper.
        assert suggest.is_superseded({"body": self.spent()})

    def test_the_marker_is_read_from_line_one_only(self):
        # Same containment as every other marker here. A wrapper body carries no model
        # text today, so this is defence in depth — and it is the position rule that
        # makes it stay true if that ever changes.
        planted = f"{suggest.REVIEW_MARKER}\nlive\n{suggest.SUPERSEDED_MARKER}"
        assert not suggest.is_superseded({"body": planted})

    def test_the_STAMP_is_read_from_line_one_only(self):
        # The counterpart the sibling above has and this reader did not.
        # delivered_stamp's own docstring says "From the marker line only, like every
        # other marker here", and DELIVERED_RE's comment says "Read back from a
        # position this module AUTHORS" — but nothing failed when the search was
        # widened to the whole body.
        #
        # Under the whole-body read, a stamp anywhere below line 1 becomes that
        # wrapper's recorded scope, and the spent body then states it as fact: "This
        # review delivered suggestions for `src/victim.py` at `attacker-sha`, and it
        # is spent." The scope of a spent artefact is exactly the claim ADR-0009's
        # addendum B made self-dating about, so a position rule that holds by
        # coincidence is not holding.
        planted = (f"{suggest.REVIEW_MARKER}\nolder wrapper\n"
                   f"{suggest.delivered_marker('attacker-sha', ['src/victim.py'])}")
        assert suggest.delivered_stamp({"body": planted}) is None, (
            "a stamp below the marker line was read as the wrapper's recorded scope, so the "
            "spent body asserts a head and a path list this wrapper never delivered for"
        )
        # And the consequence, in the body a human reads: with no stamp on line 1 the
        # wrapper predates the stamp, which the spent body must SAY rather than fill in.
        assert "did not record the scope" in suggest.superseded_review_body({"body": planted})

    def test_a_spent_wrapper_is_still_ours(self):
        # Ownership must survive the rewrite, or the pass loses track of the wrappers
        # it spent and they accumulate un-minimized.
        assert suggest.is_our_review({"body": self.spent(), "user": {"login": BOT}}, BOT)

    def test_no_path_can_shift_the_stamps_boundaries(self):
        # The stamp packs a SHA and a path list into one marker, and a path is
        # contributor-influenced (schema-constrained, but its text is not ours). The
        # separators are outside policy.json's path pattern, so no legal path can
        # move the boundary — parametrized over the delimiters rather than one guess.
        for hostile in ("a|b.py", "a,b.py", "a>b.py", "a b.py"):
            stamp = suggest.delivered_stamp(
                {"body": suggest.review_body(head_sha="abc1234", paths=[hostile])})
            assert stamp is None or stamp[0] == "abc1234", (
                f"a path of {hostile!r} moved the stamp's SHA boundary"
            )

    def test_the_stamp_round_trips_what_the_live_body_recorded(self):
        # The two halves are one mechanism: if the writer and the reader disagree,
        # every spent body reads as unrecorded and the change buys nothing.
        body = suggest.review_body(head_sha="abc1234", paths=["src/b.py", "src/a.py"])
        assert suggest.delivered_stamp({"body": body}) == ("abc1234", ["src/a.py", "src/b.py"])

    @pytest.mark.parametrize("head_sha", [
        "cd8fa363",                                   # a short SHA, as GitHub abbreviates
        "a" * 40,                                     # a full one
        "reviewed-sha",                               # what every fixture in this file uses
        "HEAD",                                       # and whatever else a caller passes
    ])
    def test_the_reader_accepts_every_sha_the_writer_will_emit(self, head_sha):
        # The reader must not be NARROWER than the writer, and a hex class made it so:
        # a non-hex value round-tripped as UNRECORDED, so a spent body silently took
        # the wrapper-predates-the-stamp branch while every test naming the recorded
        # one still passed. Found that way.
        #
        # The SHA is harness-supplied (HEAD_SHA, from the pull request) and never
        # contributor-authored, so a hex class bought no containment the separator
        # exclusion does not already give — only a way for the two halves to disagree.
        body = suggest.review_body(head_sha=head_sha, paths=["src/a.py"])
        assert suggest.delivered_stamp({"body": body}) == (head_sha, ["src/a.py"])
        assert head_sha in suggest.superseded_review_body({"body": body}), (
            f"a wrapper stamped {head_sha!r} reads as having recorded no scope, so its spent "
            "body says the scope is unknown when the wrapper recorded it"
        )


class TestPostedPayload:
    def test_the_review_is_bound_to_the_verified_head_sha(self, api):
        calls, _, _ = api
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["reviews"][0]["head_sha"] == "reviewed-sha"

    def test_a_comment_anchors_to_the_steps_path_and_line(self, api):
        calls, _, _ = api
        suggest.reconcile_suggestions("o/r", 1, [step(line=3, old="    check(path)\n")],
                                      SIGNATURES, METADATA, bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        posted = calls["reviews"][0]["comments"][0]
        assert posted["path"] == "src/app.py"
        assert posted["line"] == 3
        assert posted["side"] == "RIGHT"

    def test_a_single_line_suggestion_addresses_one_line(self, api):
        # No start_line: a one-line anchor is the degenerate range, and sending
        # start_line == line is a 422 ("must be less than line").
        calls, _, _ = api
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        posted = calls["reviews"][0]["comments"][0]
        assert posted["line"] == 2
        assert "start_line" not in posted

    def test_a_multi_line_old_addresses_every_line_it_replaces(self, api):
        # THE property. plan_verify admits a multi-line `old` and provenance-checks
        # every line it spans (its own tests pin that), but GitHub replaces the
        # ADDRESSED range with the block's lines. Addressing only `line` replaces
        # one line and leaves the rest of the anchor standing — the contributor's
        # one click then commits bytes no checker proved.
        calls, _, _ = api
        old = "def load(path):\n    check(path)\n    return os.environ\n"
        suggest.reconcile_suggestions(
            "o/r", 1, [step(old=old, new="def load(path=None):\n")],
            SIGNATURES, METADATA, bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        posted = calls["reviews"][0]["comments"][0]
        assert posted["start_line"] == 2, "the range must begin where `old` is anchored"
        assert posted["line"] == 4, "and end on the last line `old` replaces"
        assert posted["start_side"] == posted["side"] == "RIGHT"

    def test_the_addressed_range_is_the_replaced_range_for_every_extent(self, api):
        # The arithmetic, over each extent the policy admits, because an off-by-one
        # here leaves one line of the contributor's file overwritten unanchored or
        # one line of the anchor unreplaced.
        calls, _, _ = api
        for span, old in enumerate(["def load(path):\n",
                                    "def load(path):\n    check(path)\n",
                                    "def load(path):\n    check(path)\n    return os.environ\n"]):
            calls["reviews"].clear()
            suggest.reconcile_suggestions("o/r", 1, [step(old=old, new="x = 1\n")],
                                          SIGNATURES, METADATA, bot_login=BOT,
                                          head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
            posted = calls["reviews"][0]["comments"][0]
            assert posted["line"] == 2 + span
            assert posted.get("start_line", 2) == 2

    def test_an_old_with_no_final_newline_still_addresses_its_own_lines(self, api):
        # A last line with no terminator is a line: plan_verify verifies that
        # anchor (its at_line_end rule admits end-of-file), so the range must
        # count it rather than dropping it.
        calls, _, _ = api
        suggest.reconcile_suggestions(
            "o/r", 1, [step(line=3, old="    check(path)\n    return os.environ",
                            new="    return {}\n")],
            SIGNATURES, METADATA, bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        posted = calls["reviews"][0]["comments"][0]
        assert posted["start_line"] == 3
        assert posted["line"] == 4

    def test_one_atomic_review_carries_every_comment(self, api):
        calls, _, _ = api
        suggest.reconcile_suggestions(
            "o/r", 1,
            [step(), step(line=3, old="    check(path)\n", new="    check(path or '')\n")],
            SIGNATURES, METADATA, bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert len(calls["reviews"]) == 1
        assert len(calls["reviews"][0]["comments"]) == 2

    def test_an_empty_plan_posts_no_review(self, api):
        calls, _, _ = api
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["reviews"] == []

    def test_an_existing_suggestion_is_not_reposted(self, api):
        # Re-posting is not deduped by GitHub: an identical comment on an
        # unchanged line creates a true duplicate.
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [step()], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha",
                                      commanded_finding_keys=(finding_key(),))
        assert calls["reviews"] == []
        assert calls["deleted"] == []
        assert calls["patched"] == []
