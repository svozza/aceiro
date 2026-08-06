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
  POSITION, or the footer's per-run URL rewrites every comment every run — and
  with line endings normalised, because only one side of that comparison came
  back from GitHub.
- Retraction is scoped to the COMMANDED finding: one run delivers one finding
  (ADR-0007) while the listing is the whole pull request, so an unscoped pass
  withdraws every other finding's live suggestion.

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
from verify import NEWLINES_RE, code_lines, unterminated_fence

# Identity marker for a suggestion comment of ours, carrying the fingerprint the
# reconciler matches on. It RECOVERS a comment's identity on a later run rather
# than computing it: the hash is over already-verified content, so a model cannot
# choose what its suggestion collides with. Read from the first line only — see
# marker_line.
SUGGESTION_MARKER_RE = re.compile(r"<!-- smtithy:suggest:([0-9a-f]{16}) -->")

# Appended to the marker line by a strike-through retraction, so a re-run
# recognises an already-retracted comment instead of striking it again every run.
STRUCK_MARKER = "<!-- smtithy:struck -->"

# Which COMMANDED FINDING a comment was delivered for. The retraction scope: a
# command may withdraw its own finding's suggestions and nothing else. Read from
# the marker line, like every other marker here, so contributor content cannot
# present itself as another command's delivery.
SUGGESTION_FINDING_RE = re.compile(r"<!-- smtithy:for:([0-9a-f]{16}) -->")


def suggestion_marker(fingerprint: str) -> str:
    return f"<!-- smtithy:suggest:{fingerprint} -->"


def finding_marker(finding_key: str) -> str:
    return f"<!-- smtithy:for:{finding_key} -->"


def finding_identity(finding: dict, signatures: dict[tuple[str, int], str] | None = None) -> str:
    """Stable identity for the COMMANDED FINDING a delivery speaks for.

    Keyed on the anchored code exactly as suggestion_fingerprint and stack.fix_key
    are — path, line, and the line's anchor signature — never on the model's prose,
    which is reworded on essentially every run. The line is included for the reason
    stack.fix_key includes it: a window=1 signature is not unique for periodic
    code, and two copy-pasted blocks are two findings.

    Deliberately excludes head_sha, unlike stack.fix_key: suggestions outlive a
    push (GitHub marks them outdated rather than removing them), so a comment
    delivered for this finding at an earlier head is still this finding's.
    """
    path, line = finding["path"], finding["line"]
    signature = (signatures or {}).get((path, line))
    anchored = (
        f"unanchored\0{line}" if signature is None
        else f"anchored\0{line}\0{normalize_signature_line(signature)}"
    )
    return hashlib.sha256("\0".join([path, anchored]).encode()).hexdigest()[:16]


def owned_finding_key(comment: dict, bot_login: str) -> str | None:
    """The finding a comment of OURS was delivered for, else None.

    None means the subject cannot be established — an older comment written before
    the marker existed, or a comment that is not ours — and the caller must then
    leave it standing. Ownership is checked here too, so a pasted marker cannot
    make a contributor's comment look like another command's delivery.
    """
    if not bot_login or (comment.get("user") or {}).get("login") != bot_login:
        return None
    match = SUGGESTION_FINDING_RE.search(marker_line(comment))
    return match.group(1) if match else None


def suggestion_fingerprint(step_args: dict, signatures: dict[tuple[str, int], str] | None = None) -> str:
    """Stable identity for one suggestion, computed by the executor.

    Keyed on the code the suggestion is anchored to — `path` plus the anchor
    signature of its line — never on the model's `note`. The note is the least
    stable thing in the system (the reference implementation measured a rewording
    on essentially every run), and a key that moves with it means every run
    deletes a live thread and reposts the same comment.

    The line NUMBER is not in the key either: GitHub re-anchors a live comment
    when the diff shifts, so the number moves while the code does not.

    `old` joins the window, because the window alone does not say WHAT the
    suggestion replaces. Two consequences of leaving it out, both measured here:
    a window=1 signature is not unique for periodic code (three repeating lines
    give two anchors the same window), and the same anchored line with a different
    replaced EXTENT read as the same suggestion — so a broadened fix took the PATCH
    branch, which rewrites a body but cannot move the addressed range, leaving a
    one-line anchor carrying a multi-line replacement.

    `old` is model-supplied but not model-CHOSEN: check_plan_containment requires
    it to byte-match the reviewed tree exactly once, so for a verified plan it is a
    fact about the file. Canonicalized the same way the window is, so the churn
    this design exists to prevent stays prevented — a reindentation is still the
    same suggestion.

    A signature the map does not carry falls back to the anchored bytes alone.
    Provenance makes that unreachable for a verified plan — `line` must be in a
    hunk — but identity must degrade rather than crash, and `old` is the one thing
    always in hand.
    """
    path, line = step_args["path"], step_args["line"]
    anchored = "\x00".join(
        normalize_signature_line(part) for part in step_args["old"].split("\n")
    )
    signature = (signatures or {}).get((path, line))
    parts = [path, anchored] if signature is None else [path, signature, anchored]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


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


def marker_line(comment: dict) -> str:
    """The comment's first line, the only part this module authored itself.

    Both markers are read from here rather than from the whole body. Model text
    can legally contain either marker's literal text inside a code fence (raw HTML
    rejects only OUTSIDE one), and a suggestion's `new` is not markdown-checked at
    all — so a body-wide scan would let crafted content present itself as a comment
    of ours on any fingerprint, and would let a live comment look already-retracted
    so a stale suggestion carrying a human reply was never struck.
    """
    return (comment.get("body") or "").split("\n", 1)[0]


def owned_fingerprint(comment: dict, bot_login: str) -> str | None:
    """The fingerprint of a suggestion comment this harness authored, else None.

    The only gate in front of every DELETE and PATCH the reconciler issues, and
    both halves are load-bearing: anyone can paste the marker into their own review
    comment, so the marker alone would let a crafted comment — or a fingerprint
    collision with a human's — steer this into editing or deleting someone else's
    words. The authenticated author is the backstop, and it comes from the write
    token itself (post.resolve_bot_login) rather than from configuration.

    An empty `bot_login` matches nothing, including a comment whose author GitHub
    reported as null: resolution fails closed upstream, and this gate must not
    turn an unresolved identity into ownership of a deleted author's comment.
    """
    if not bot_login or (comment.get("user") or {}).get("login") != bot_login:
        return None
    match = SUGGESTION_MARKER_RE.match(marker_line(comment).strip())
    return match.group(1) if match else None


def is_struck(comment: dict) -> bool:
    return STRUCK_MARKER in marker_line(comment)


NOT_A_HUMAN_REVIEW = (
    "**AI-suggested fix.** Generated by an AI model, not a human review, and it "
    "counts toward **no approval**. Read the diff before applying it — applying "
    "is what commits it to this branch."
)


WITHDRAWN_NOTE = (
    "**This suggestion is no longer in the latest AI remediation.** It may have "
    "been applied, or the model may simply not have produced it this time — this "
    "note does not claim it was fixed. Please resolve this conversation if it has "
    "been dealt with."
)


def human_replied_ids(all_comments: list[dict], bot_login: str) -> set[int]:
    """Comment ids a non-bot account has replied to, from one scan.

    Reactions deliberately do NOT count: a single 👍 would pin a stale suggestion
    forever, and a reaction is not discussion worth preserving.

    Built once per run rather than rescanned per retraction: the scan is over
    every review comment on the pull request, which is unbounded on a long-lived
    one, while the retractions are capped by the plan's steps.
    """
    return {
        other["in_reply_to_id"]
        for other in all_comments
        if other.get("in_reply_to_id") is not None
        and (other.get("user") or {}).get("login") != bot_login
    }


def strike_through(text: str) -> str:
    """Strike every visible line of a comment body.

    Per line rather than one span around the whole text, because `~~…~~` does not
    cross block boundaries. Code is left alone: `~~` inside a fence renders
    literally, so striking there would corrupt the suggestion's quoted code
    instead of crossing it out — and which lines are code comes from
    verify.code_lines, i.e. from markdown-it itself, which also covers indented
    blocks no fence-scanning version would notice.

    `<s>` rather than `~~…~~`, because the wrapper must not be able to become
    SYNTAX. A line already beginning with `~` — `~~Deprecated~~ …`, which the
    allowlist admits, or a bare `~/.config` — turned into `~~~…` under the tilde
    form, and CommonMark reads that as a tilde-fence opener: the suggestion block
    and the `<sub>` attribution line below it were swallowed into one code block,
    and close_open_fence then appended its marker after the footer rather than
    before it. An HTML tag has no such reading, and raw HTML is refused in MODEL
    text while remaining ours to emit.

    BEST EFFORT, and never the only signal: the text being struck is
    model-authored, and the grammar permits markdown that defeats a span. `retract`
    therefore leads with a plain-text notice that nothing below it can capture.
    """
    out = []
    for line, is_code in code_lines(text):
        skip = is_code or not line.strip() or line.startswith(("<!--", "<sub>"))
        out.append(line if skip else f"<s>{line}</s>")
    return "\n".join(out)


def close_open_fence(text: str) -> str:
    """Append a closing fence if `text` leaves one open.

    DEFENCE IN DEPTH. check_plan_markdown rejects a `note` that ends inside a
    fence, so a verified plan cannot reach this with an open one. It stays because
    the text it wraps is a comment body read back from GitHub, which a previous
    run — on a previous version of the grammar — may have written.

    Whether a fence is open, and the marker that closes it, both come from
    verify.unterminated_fence: closing a ``~~~``-opened block with ``` ``` ```
    does not close it.
    """
    marker = unterminated_fence(text)
    return text if marker is None else f"{text}\n{marker}"


def retract(repo: str, comment: dict, replied_ids: set[int], note: str) -> None:
    """Withdraw one suggestion comment of ours.

    Never GitHub-"resolves" it: resolved asserts the defect was addressed, which
    the harness cannot know — a suggestion can vanish because the model had an off
    run. The bot states only what it knows.

    No human reply -> DELETE: the comment claims nothing and there is no
    discussion to keep. A human replied -> PATCH, and the close decision is left
    to the human already in the thread; DELETE would orphan their reply, which
    survives but is promoted to a standalone comment severed from its context.

    The note goes ABOVE the struck body. Below it, it is at the mercy of the
    model's own markdown — an unclosed fence renders it as literal code, and the
    struck marker then stops any later run from repairing it — while above the
    body nothing the model wrote can capture it, and it is the first thing a
    human reads.
    """
    if comment["id"] not in replied_ids:
        delete_review_comment(repo, comment["id"])
        print(f"deleted suggestion comment {comment['id']}")
        return

    if is_struck(comment):
        print(f"suggestion comment {comment['id']} already retracted, left as is")
        return

    # The struck marker joins the fingerprint on the FIRST line, keeping both
    # identity signals in the one place model text cannot reach.
    head, _, rest = (comment.get("body") or "").partition("\n")
    body = f"{head.strip()} {STRUCK_MARKER}\n{note}\n\n{close_open_fence(strike_through(rest))}"
    patch_review_comment(repo, comment["id"], body)
    print(f"struck through suggestion comment {comment['id']} (has a human reply)")


def replaced_line_count(old: str) -> int:
    """Lines `old` occupies in the file — the same count plan_verify derives.

    A trailing newline ENDS the last line rather than starting an empty one, and a
    last line with no terminator is still a line (plan_verify's at_line_end rule
    admits end-of-file, so such an anchor verifies and must be addressable).
    Twin of plan_verify._line_count for a reason: the range this addresses has to
    be the range that was anchored, or the two disagree about what is replaced.
    """
    return old.count("\n") + (0 if old.endswith("\n") else 1) if old else 0


def comment_anchor(step: dict) -> dict:
    """Where one suggestion's comment attaches: the lines `old` replaces.

    GitHub replaces the ADDRESSED RANGE with the suggestion block's lines, while
    the verifier proves a property about substituting `old` with `new`. Those are
    the same effect only when the addressed range is exactly the range `old`
    covers — address one line of a three-line anchor and the applier commits the
    replacement plus the two lines it was meant to absorb, which is bytes no check
    ever saw. plan_verify admits a multi-line `old` and provenance-checks every
    line it spans, so the extent is verified; this is where it reaches the write.

    `start_line` is OMITTED for a single-line anchor rather than set equal to
    `line`: GitHub requires start_line < line and 422s on the degenerate range.

    RIGHT on both ends: the plan carries no side, so the model cannot ask for LEFT
    (which 422s on an added line), and a suggestion replaces new-side content by
    definition.
    """
    args = step["args"]
    end = args["line"] + replaced_line_count(args["old"]) - 1
    anchor = {"path": args["path"], "line": end, "side": "RIGHT"}
    if end > args["line"]:
        anchor |= {"start_line": args["line"], "start_side": "RIGHT"}
    return anchor


def render_suggestion(step: dict, fingerprint: str, metadata: dict,
                      finding_key: str | None = None) -> str:
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
    # Both markers on line 1: the fingerprint identifies the SUGGESTION, the
    # finding key identifies the COMMAND that may retract it.
    first_line = suggestion_marker(fingerprint)
    if finding_key is not None:
        first_line = f"{first_line} {finding_marker(finding_key)}"
    return "\n".join([
        first_line,
        NOT_A_HUMAN_REVIEW,
        "",
        args["note"],
        "",
        *block,
        "<sub>🤖 model: `{model}` · policy: `{policy}` · reviewed SHA: `{sha}` · "
        "[run]({run_url})</sub>".format(**metadata),
    ])


# ------------------------------------------------------- review wrappers ---

# Identity marker for a review wrapper of ours, so a later run can recognise and
# supersede it. Same status as the comment marker: executor-authored, and paired
# with an author check because anyone can paste it into their own review.
REVIEW_MARKER = "<!-- smtithy:review -->"

# The reviews API requires a body on a COMMENT event, so this is the one line the
# review itself carries; the substance is in the suggestion comments.
REVIEW_BODY = (
    f"{REVIEW_MARKER}\n"
    "🤖 **AI-suggested fixes** — see the suggestion comments below. Not a human "
    "review; counts toward no approval."
)

# Replaces a superseded wrapper's body. A spent wrapper's text says "see the
# suggestion comments below" while the reconciler deletes comments independently
# of the review that posted them, so a wrapper can end up pointing at nothing.
# This states only what is still true.
SUPERSEDED_REVIEW_BODY = (
    f"{REVIEW_MARKER}\n"
    "🤖 **Superseded AI remediation.** A later run has posted updated suggestions "
    "on this pull request; any from this one that still apply are in the current "
    "suggestion comments. Not a human review; counts toward no approval."
)


def is_our_review(review: dict, bot_login: str) -> bool:
    """Whether a review is a wrapper this harness posted.

    Both conditions are load-bearing, exactly as in owned_fingerprint: the marker
    alone would let anyone hand us someone else's review to rewrite. This gates a
    body OVERWRITE, so a mis-scope would destroy a human's review summary.
    """
    if not bot_login or (review.get("user") or {}).get("login") != bot_login:
        return False
    return (review.get("body") or "").strip().startswith(REVIEW_MARKER)


def supersede_previous_reviews(repo: str, pr_number: int, bot_login: str) -> None:
    """Neutralise the review wrappers left by earlier runs of this harness.

    Every run that posts a suggestion comment must CREATE a review to carry it —
    there is no "post into the existing review", and a submitted one cannot be
    deleted (only PENDING ones can) — so on a long-lived pull request the wrappers
    stack up, each claiming to point at comments the reconciler may since have
    deleted. Only CREATION lacks an upsert; a submitted review's BODY is editable,
    which is what this relies on.

    Two mutations, in increasing order of the permission they need: rewrite the
    body (REST, the same `pull-requests: write` this job already uses to POST
    reviews), then minimize it so the UI collapses it (GraphQL-only).

    Both are BEST EFFORT and neither may fail the run. This is cosmetic tidying in
    front of an atomic POST: a permission the bot turns out not to have must cost a
    collapsed timeline entry, never a delivery. Failures print and carry on.
    """
    try:
        reviews = [review for review in pull_reviews(repo, pr_number) if is_our_review(review, bot_login)]
    except Exception as exc:  # noqa: BLE001 — cosmetic tidying must never fail the run
        print(f"could not list reviews to supersede ({exc}); continuing")
        return

    for review in reviews:
        # The one just posted carries the current suggestions; only earlier ones are
        # spent. Identified by body rather than by id, so this needs no coupling to
        # what the POST returned.
        if (review.get("body") or "").strip() == SUPERSEDED_REVIEW_BODY.strip():
            continue
        # `.get`, not `review["id"]`, and the same value in the handler: the
        # subscript raised KeyError INSIDE the try, and the handler's own f-string
        # then re-raised it out of a function whose contract is never to fail —
        # losing the delivery to cosmetic tidying, since this runs before the POST.
        review_id = review.get("id")
        try:
            update_review_body(repo, pr_number, review_id, SUPERSEDED_REVIEW_BODY)
            print(f"marked review {review_id} superseded")
        except Exception as exc:  # noqa: BLE001
            print(f"could not rewrite review {review_id} ({exc}); continuing")
        if node_id := review.get("node_id"):
            try:
                minimize_review(node_id)
                print(f"minimized review {review_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"could not minimize review {review_id} ({exc}); continuing")


# ---------------------------------------------------------- the reconciler ---


def comment_content(body: str) -> str:
    """One of our suggestion comments minus the two lines this module authored.

    Used only to decide whether a live comment still says what the current plan
    says. The first line is the marker and the last is the attribution footer;
    both are ours, so they are dropped BY POSITION rather than searched for —
    model text cannot reach either position, and nothing here has to recognise a
    pattern model text could imitate.

    Dropping the footer is the point: its `[run]` URL differs on every run, so
    comparing whole bodies would report every comment as changed and rewrite all
    of them, every time.

    Line endings are normalised before comparing, because only one side of the
    comparison came back from GitHub: this harness sends LF and the API returns
    bodies with CRLF (measured — post.check_marker's docstring records the same
    fact), so an unchanged comment differed from its own re-render at every
    interior newline and was rewritten every run without bound. Normalising here is
    not the canonicality question ADR-0011 refuses to answer by rewriting: nothing
    normalised is ever POSTED, this decides only whether two bodies say the same
    thing.
    """
    lines = NEWLINES_RE.sub("\n", body or "").split("\n")
    return "\n".join(lines[1:-1]).strip()


def reconcile_suggestions(repo: str, pr_number: int, steps: list[dict],
                          signatures: dict[tuple[str, int], str], metadata: dict,
                          *, bot_login: str, head_sha: str,
                          commanded_finding_key: str | None) -> None:
    """Make the pull request's suggestion comments match the verified plan's.

    Re-posting is not idempotent — an identical comment on an unchanged line
    creates a true duplicate — so the reconciler dedups itself on
    suggestion_fingerprint: matched comments stay in place, new ones are posted,
    and the ones whose suggestion is gone are retracted.

    New comments go up BEFORE anything is retracted, because the batch is atomic:
    if a line cannot be resolved the POST 422s and creates nothing, and failing at
    that point must leave the existing comments standing rather than having
    already deleted them.

    All three keyword arguments have no defaults. `bot_login` is the security half
    of ownership. `head_sha` binds the review to the SHA the plan was verified
    against, so a push landing mid-run leaves the suggestion marked outdated rather
    than misplaced on content it never described.

    `commanded_finding_key` is the SCOPE. One run delivers one commanded finding
    (ADR-0007), while the comment listing is the whole pull request, so retraction
    has to be told what this command could have produced or it withdraws every
    OTHER finding's live suggestion — with a note claiming it left the latest
    remediation, which is untrue, and deleting the human thread under it if there
    is no reply to force a strike. Two commands on one pull request is the designed
    flow.

    The scope is the FINDING, not its file. A path was the scope first, on the
    reasoning that the scope gate already requires the plan to touch it — but two
    findings of one accepted artifact routinely share a file, and the reviewer was
    measured doing exactly that (ADR-0009 addendum C), so a path is the set TWO
    commands speak for. `/fix 2` then withdrew `/fix 1`'s live suggestion. Each
    comment records the finding it was delivered for (finding_marker, on the marker
    line), and a command retracts only comments carrying its own key.

    None retracts NOTHING, and so does a comment whose own key cannot be
    established: a run whose scope is unknown may still post — its own suggestions
    are verified — but must not take anything down. That also makes the change
    backward-safe, since comments delivered before the marker existed carry no key
    and are left standing rather than withdrawn by the first command to follow.
    """
    all_comments = list(review_comments(repo, pr_number))
    ours = [(fingerprint, c) for c in all_comments if (fingerprint := owned_fingerprint(c, bot_login))]
    replied_ids = human_replied_ids(all_comments, bot_login)
    wanted = {suggestion_fingerprint(step["args"], signatures): step for step in steps}
    live = {fingerprint for fingerprint, _ in ours}

    fresh = [(fingerprint, step) for fingerprint, step in wanted.items() if fingerprint not in live]
    if fresh:
        # BEFORE the POST, so "every wrapper except the newest" needs no id
        # bookkeeping — at this moment the newest does not exist yet. Safe to do
        # first because it only ever rewrites and collapses OUR OWN spent
        # wrappers, never a comment; if the POST then 422s, the run fails having
        # tidied history it was going to tidy anyway, and the suggestions on the
        # pull request are untouched.
        supersede_previous_reviews(repo, pr_number, bot_login)
        submit_review(
            repo,
            pr_number,
            REVIEW_BODY,
            [comment_anchor(step) | {
                "body": render_suggestion(step, fingerprint, metadata, commanded_finding_key)}
             for fingerprint, step in fresh],
            head_sha=head_sha,
        )
        print(f"posted review with {len(fresh)} suggestion comment(s)")
    else:
        print("no new suggestions to post")

    # A suggestion can be retracted on one run and produced again on the next. Its
    # struck comment already holds the human thread, and a key match proves it is
    # about the same code, so the comment is restored rather than left
    # contradicting the review that carries it. AFTER the POST: restoring first
    # would leave a stale comment live if the atomic POST then 422'd.
    #
    # A matched comment is also re-rendered when its CONTENT changed — the note or
    # the replacement can be revised between runs while the anchor, and so the
    # key, stays the same. Compared on comment_content rather than the whole body,
    # so the per-run `[run]` URL does not make every comment look changed and
    # rewrite them all.
    for fingerprint, comment in ours:
        if fingerprint not in wanted:
            continue
        body = render_suggestion(
            wanted[fingerprint], fingerprint, metadata, commanded_finding_key)
        if is_struck(comment):
            patch_review_comment(repo, comment["id"], body)
            print(f"restored previously retracted suggestion comment {comment['id']}")
        elif comment_content(comment.get("body") or "") != comment_content(body):
            patch_review_comment(repo, comment["id"], body)
            print(f"updated suggestion comment {comment['id']} (its suggestion changed)")

    # Scoped to what THIS command speaks for: a comment on another finding's file
    # is not withdrawn by a command that was never about it. `path` comes from the
    # listing, so a comment GitHub reports without one is out of scope and left
    # standing — the fail-closed reading, since the alternative is deleting a
    # comment whose subject could not be established.
    stale = [(fingerprint, comment) for fingerprint, comment in ours if fingerprint not in wanted]
    if stale:
        # Re-read immediately before the DELETEs. The listing above happened before
        # the POST, the supersede pass and the re-render PATCHes — several live
        # calls — and a human replying in that window was absent from the snapshot,
        # so `retract` read "no discussion" and DELETED their reply's parent. The
        # window cannot be closed (the read and the delete are not atomic) but it
        # can be made as small as the last read, and the cost of a stale answer is
        # asymmetric: a reply seen late means an unnecessary strike, a reply missed
        # means a severed thread. Failing to re-read keeps the pre-write answer
        # rather than losing the retraction.
        try:
            replied_ids = human_replied_ids(list(review_comments(repo, pr_number)), bot_login)
        except Exception as exc:  # noqa: BLE001 — the snapshot we already hold is a safe answer
            print(f"could not re-read replies before retracting ({exc}); using the earlier scan")

    for fingerprint, comment in stale:
        if commanded_finding_key is None or owned_finding_key(comment, bot_login) != commanded_finding_key:
            print(f"suggestion comment {comment['id']} is outside this command's scope, left as is")
            continue
        retract(repo, comment, replied_ids, WITHDRAWN_NOTE)
