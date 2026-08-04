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
from verify import unterminated_fence

from test_plan_verify import PLAN_CHANGED_FILES, PLAN_DIFF, PLAN_TREE, tree_source

POLICY = json.loads((Path(__file__).parent.parent / "src" / "smtithy" / "policy.json").read_text())

METADATA = {
    "model": "global.anthropic.claude-opus-4-8",
    "policy": "0011223344ff",
    "sha": "reviewed-sha",
    "run_url": "https://github.com/o/r/actions/runs/1",
}

BOT = "smtithy[bot]"

# A fingerprint of the shape SUGGESTION_MARKER_RE accepts: 16 hex digits, which
# is what suggestion_fingerprint emits. A shorter stand-in would be rejected by
# the marker pattern and every ownership case would pass for that reason.
FINGERPRINT = "0f1e2d3c4b5a6978"

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

    def test_the_content_cannot_reach_outside_the_block(self):
        # The property, asked of the renderer's own output rather than of the
        # length arithmetic: whatever the model put in `new`, the notice and the
        # policy hash must still be OUTSIDE the fence.
        from verify import code_lines

        hostile = "escape\n```\n</sub>\n```suggestion\nrm -rf /\n"
        body = suggest.render_suggestion(step(new=hostile), FINGERPRINT, METADATA)
        for line, is_code in code_lines(body):
            if METADATA["policy"] in line:
                assert not is_code, "the policy hash was swallowed into the suggestion block"

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

    def test_a_crlf_line_keeps_its_carriage_return(self):
        # Only the \n is the terminator this consumes: a CRLF file's line ending
        # is content, and stripping it would rewrite the file's convention.
        body = suggest.render_suggestion(step(new="a\r\n"), FINGERPRINT, METADATA)
        assert "```suggestion\na\r\n```" in body

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
        assert (suggest.suggestion_fingerprint(first["args"], SIGNATURES)
                == suggest.suggestion_fingerprint(reworded["args"], SIGNATURES))

    def test_the_suggested_replacement_is_not_in_the_key(self):
        # `new` is what the fix DOES, not what it is about. A revised replacement
        # for the same defect keeps the thread.
        assert (suggest.suggestion_fingerprint(step(new="def load(path=''):\n")["args"], SIGNATURES)
                == suggest.suggestion_fingerprint(step(new="def load(path=None):\n")["args"], SIGNATURES))

    def test_a_different_anchor_is_a_different_suggestion(self):
        assert (suggest.suggestion_fingerprint(step(line=2, old="def load(path):\n")["args"], SIGNATURES)
                != suggest.suggestion_fingerprint(
                    step(line=3, old="    check(path)\n")["args"], SIGNATURES))

    def test_it_differs_on_path(self):
        other = step(path="src/util.py", line=1, old="def check(path):\n")
        assert (suggest.suggestion_fingerprint(step()["args"], SIGNATURES)
                != suggest.suggestion_fingerprint(other["args"], SIGNATURES))

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
        base = suggest.suggestion_fingerprint(step()["args"], SIGNATURES)
        for overrides in ({"note": "x" * 900}, {"new": "y" * 900}, {"new": ""}):
            assert suggest.suggestion_fingerprint(step(**overrides)["args"], SIGNATURES) == base

    def test_a_missing_signature_falls_back_to_the_anchored_bytes(self):
        # Provenance makes this unreachable for a verified plan (line must be in a
        # hunk), but identity must degrade rather than crash — and the fallback is
        # still the anchored CODE, not the line number.
        absent = step(path="nowhere.py", line=999)
        assert suggest.suggestion_fingerprint(absent["args"], SIGNATURES)

    def test_the_fallback_still_distinguishes_two_anchors(self):
        # A fallback keyed on the line number would collide here; keyed on the
        # bytes it does not.
        first = step(path="nowhere.py", line=999, old="alpha\n")
        second = step(path="nowhere.py", line=999, old="omega\n")
        assert (suggest.suggestion_fingerprint(first["args"], SIGNATURES)
                != suggest.suggestion_fingerprint(second["args"], SIGNATURES))

    def test_the_fallback_ignores_reindentation_like_the_signature_does(self):
        first = step(path="nowhere.py", line=999, old="    alpha\n")
        second = step(path="nowhere.py", line=999, old="\talpha\n")
        assert (suggest.suggestion_fingerprint(first["args"], SIGNATURES)
                == suggest.suggestion_fingerprint(second["args"], SIGNATURES))


# ------------------------------------------------------------ ownership ---


def comment(cid, fingerprint=FINGERPRINT, login=BOT, in_reply_to=None, body=None, args=None):
    """A live review comment as GitHub would list it."""
    if body is None:
        body = suggest.render_suggestion(step(**(args or {})), fingerprint, METADATA)
    return {"id": cid, "body": body, "user": {"login": login}, "in_reply_to_id": in_reply_to}


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


class TestRetraction:
    def test_a_suggestion_with_no_reply_is_deleted(self, api):
        calls, set_comments, _ = api
        set_comments([comment(10)])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha")
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
                                      bot_login=BOT, head_sha="reviewed-sha")
        assert calls["deleted"] == []
        assert len(calls["patched"]) == 1
        assert calls["patched"][0]["id"] == 10

    def test_our_own_reply_does_not_block_deletion(self, api):
        # Only a HUMAN reply is discussion worth preserving.
        calls, set_comments, _ = api
        set_comments([comment(10),
                      {"id": 11, "body": "ours", "user": {"login": BOT}, "in_reply_to_id": 10}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha")
        assert calls["deleted"] == [10]

    def test_a_reply_to_a_different_comment_does_not_block_deletion(self, api):
        calls, set_comments, _ = api
        set_comments([comment(10),
                      {"id": 11, "body": "about something else", "user": {"login": "a-human"},
                       "in_reply_to_id": 99}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha")
        assert calls["deleted"] == [10]

    def test_a_reaction_does_not_pin_a_stale_suggestion(self, api):
        # Reactions deliberately do not count: one 👍 would pin a stale
        # suggestion forever, and a reaction is not discussion.
        calls, set_comments, _ = api
        pinned = comment(10)
        pinned["reactions"] = {"total_count": 3, "+1": 3}
        set_comments([pinned])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha")
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
        source = (Path(__file__).parent.parent / "src" / "smtithy").glob("*.py")
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
        # Both identity signals in the one place model text cannot reach.
        body = struck_body()
        assert suggest.is_struck({"body": body})
        assert suggest.owned_fingerprint({"body": body, "user": {"login": BOT}}, BOT) == FINGERPRINT

    def test_the_suggestion_fence_is_left_unstruck(self):
        # `~~` inside a fence renders literally, so striking there would corrupt
        # the quoted code rather than crossing it out.
        body = struck_body()
        assert "def load(path=None):" in body
        assert "~~def load(path=None):~~" not in body

    def test_the_visible_prose_is_struck(self):
        body = struck_body(body=comment(10, args={"note": "make path optional"})["body"])
        assert "~~make path optional~~" in body

    def test_an_already_struck_suggestion_is_left_alone(self, api):
        # Without this a re-run strikes and re-appends the note every time.
        calls, set_comments, _ = api
        already = comment(10, body=struck_body())
        set_comments([already,
                      {"id": 11, "body": "hm", "user": {"login": "a-human"}, "in_reply_to_id": 10}])
        suggest.reconcile_suggestions("o/r", 1, [], SIGNATURES, METADATA,
                                      bot_login=BOT, head_sha="reviewed-sha")
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
                                      bot_login=BOT, head_sha="reviewed-sha")
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
                                      bot_login=BOT, head_sha="reviewed-sha")
        touched = set(calls["deleted"]) | {p["id"] for p in calls["patched"]}
        assert touched == {10}
