"""Suggestion delivery: render a verified suggest step, reconcile it onto the PR.

The remediator's universal delivery (ADR-0009 and its addendum): a suggestion is
the only remediation that works across both repository topologies, because a
stacked follow-up pull request needs the head branch to exist in the base
repository and a fork's does not. The contributor applies it with one click, so
the commit lands on their branch through their own deliberate action — which is
also the last point a human sees the actual bytes, ADR-0005's human gate.

A suggestion IS an inline comment, so this ports the extraction source's
reconciler (staging/inline-comments-test @ 70bebcd4) rather than re-deriving it.
Its lessons, each measured on a real pull request there and each load-bearing:

- Identity is the anchored CODE. Not the model's prose (measured: it reworded
  every finding on every run over a byte-identical diff, so a title-derived key
  never matched twice and every run deleted and reposted everything), and not
  (path, line) — GitHub re-anchors live comments when the diff shifts. For a
  suggestion `old` IS the anchored code, so the fingerprint is its anchor
  signature.
- Retraction is reply-aware and never GitHub-"resolves": resolved asserts the
  defect was addressed, which the harness cannot know.
- New comments go up BEFORE stale ones come down, because the review POST is
  atomic — a 422 creates nothing, and failing there must leave the existing
  comments standing rather than already deleted.
- Review wrappers accumulate: creation has no upsert and a submitted review
  cannot be deleted, so spent ones are rewritten and minimized, best effort and
  never run-failing.
- Content is compared with the executor-authored first and last lines dropped BY
  POSITION, or the footer's per-run URL rewrites every comment every run.

Ownership is marker AND authenticated author throughout, the author half resolved
from the write token itself (post.resolve_bot_login): the marker is copyable, so
it alone would let a crafted comment steer this into editing someone else's words.
"""

from __future__ import annotations

import hashlib
import re

from diff_map import normalize_signature_line
from github_api import (
    delete_review_comment,
    minimize_review,
    patch_review_comment,
    pull_reviews,
    review_comments,
    submit_review,
    update_review_body,
)
from verify import code_lines, unterminated_fence

# Identity marker for a suggestion comment of ours, carrying the fingerprint the
# reconciler matches on. It RECOVERS a comment's identity on a later run rather
# than computing it: the hash is over already-verified content, so a model cannot
# choose what its suggestion collides with. Read from the first line only — see
# marker_line.
SUGGESTION_MARKER_RE = re.compile(r"<!-- smtithy:suggest:([0-9a-f]{16}) -->")

# Appended to the marker line by a strike-through retraction, so a re-run
# recognises an already-retracted comment instead of striking it again every run.
STRUCK_MARKER = "<!-- smtithy:struck -->"


def suggestion_marker(fingerprint: str) -> str:
    return f"<!-- smtithy:suggest:{fingerprint} -->"


def suggestion_fingerprint(step_args: dict, signatures: dict[tuple[str, int], str] | None = None) -> str:
    """Stable identity for one suggestion, computed by the executor.

    Keyed on the code the suggestion is anchored to — `path` plus the anchor
    signature of its line — never on the model's `note`. The note is the least
    stable thing in the system (the reference implementation measured a rewording
    on essentially every run), and a key that moves with it means every run
    deletes a live thread and reposts the same comment.

    The line NUMBER is not in the key either: GitHub re-anchors a live comment
    when the diff shifts, so the number moves while the code does not.

    A signature the map does not carry falls back to the anchored bytes
    themselves, canonicalized the same way. Provenance makes that unreachable for
    a verified plan — `line` must be in a hunk — but identity must degrade rather
    than crash, and `old` is the one thing always in hand.
    """
    path, line = step_args["path"], step_args["line"]
    signature = (signatures or {}).get((path, line))
    if signature is None:
        signature = "\x00".join(
            normalize_signature_line(part) for part in step_args["old"].split("\n")
        )
    return hashlib.sha256("\0".join([path, signature]).encode()).hexdigest()[:16]


# GitHub reads a ```suggestion block's content literally, so the delimiter must be
# longer than the longest backtick run the content holds or the content closes the
# block itself.
_BACKTICK_RUN_RE = re.compile(r"`+")


def fence_marker(content: str) -> str:
    """A backtick run no run inside `content` can close.

    `new` is file bytes: patch/suggest old and new are deliberately exempt from
    the markdown allowlist (they must byte-match the tree, and their gate is
    anchoring plus the human click), so this is the one model-controlled value in
    a suggestion body that may legally contain fence syntax.

    CommonMark closes a fence on a run of AT LEAST the opener's length, so the
    opener must be strictly longer than the longest run in the content, and never
    shorter than three.
    """
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(content)), default=0)
    return "`" * max(3, longest + 1)


NOT_A_HUMAN_REVIEW = (
    "**AI-suggested fix.** Generated by an AI model, not a human review, and it "
    "counts toward **no approval**. Read the diff before applying it — applying "
    "is what commits it to this branch."
)


def render_suggestion(step: dict, fingerprint: str, metadata: dict) -> str:
    """One verified suggest step as a review-comment body.

    Structure is ours; the model's `note` is inserted verbatim only after
    check_plan_markdown proved it inside the safe grammar, and `new` goes inside
    the suggestion fence, which is what GitHub applies.

    The marker is the first line and the attribution footer the last, both by
    position: `comment_content` drops them that way rather than searching for
    them, so nothing has to recognise a pattern model text could imitate.

    The notice and the policy hash sit OUTSIDE the fence (ADR-0005's visibility
    requirement, which ADR-0009 extends to this comment) — inside it they would
    render as code the reader skips rather than as the disclosure they are.
    """
    args = step["args"]
    marker = fence_marker(args["new"])
    # The terminator belongs to the closing fence, not to the content: `new` is
    # line-oriented, so emitting "a\n" verbatim before the closer would suggest a
    # trailing empty line the plan never described. An EMPTY new is the deletion
    # suggestion and contributes no line at all, which is what distinguishes it
    # from "\n" — one empty line, a line of the contributor's file either way.
    block = [f"{marker}suggestion"]
    if args["new"]:
        block.append(args["new"][:-1] if args["new"].endswith("\n") else args["new"])
    block.append(marker)
    return "\n".join([
        suggestion_marker(fingerprint),
        NOT_A_HUMAN_REVIEW,
        "",
        args["note"],
        "",
        *block,
        "<sub>🤖 model: `{model}` · policy: `{policy}` · reviewed SHA: `{sha}` · "
        "[run]({run_url})</sub>".format(**metadata),
    ])
