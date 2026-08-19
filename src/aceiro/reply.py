"""Post the command channel's reply: the command's terminal state (ADR-0018).

A **reply** is neither an artifact kind nor a plan step kind: it is the COMMAND
CHANNEL reporting a command's terminal state to the person who issued it. Two
kinds: a **decline** (the fix was not performed, and a red/green tick cannot say
why) and a **receipt** (the fix was delivered somewhere the commanding pull
request does not surface). ADR-0014 built the channel with the decline as its
only kind; ADR-0018 widened it and replaced the membership enumeration with the
criterion:

**The channel replies when the harness has a terminal answer to the command and
that answer is not already surfaced on the commanding pull request. A run whose
machinery failed stays a red run.**

Four cases pass — undeliverable by construction, already delivered, stranded
delivery (a post-push refusal with a fix branch standing), and delivered (the
stacked receipt; a suggestion delivery lands on the commanding pull request and
is its own receipt).

Everything else stays a red run. **The untrusted-commander refusal must never be
replied to**: trust is resolved as `prepare_fix_context`'s SECOND step, so
everything before it runs for an untrusted commenter, and replying there would
let any passer-by make the harness post a comment naming them. Every reply
producer runs downstream of that resolution, so the exclusion is by
construction, not by a maintained list.

This module holds no derivation of its own. It formats and posts what a producer
job derived, in the job that holds `pull-requests: write` — `command` must keep
none, being reached directly from `issue_comment`.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, REASON, KIND, HEAD_SHA,
ORDINALS, RUN_URL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from github_api import fail
from post import resolve_bot_login, upsert_comment

# The outputs a producer job writes, paired with the environment variable this job
# reads each one as. ONE mapping rather than two lists, because the two ends are
# joined only by the workflow YAML and nothing else could check the join: a typo in
# either name leaves the reader with an empty string, and `main()` then refuses —
# so the reply is a red run that posts nothing, which is exactly the "declined to
# fix something and told nobody" case ADR-0007's third addendum forbids, arriving
# through the mechanism built to prevent it. test_workflow_shape asserts the
# workflow's `env:` block against this mapping in both directions.
#
# Declared as one set for the same reason prepare_fix_context.STEP_OUTPUTS is: a
# writer emitting a subset leaves the reader with empty strings, and a reply naming
# no reason or no head is a comment that tells the commander nothing while looking
# like an answer.
OUTPUT_ENV = {
    "reply_reason": "REASON",
    "reply_kind": "KIND",
    "reply_head_sha": "HEAD_SHA",
    "reply_ordinals": "ORDINALS",
}

OUTPUTS = tuple(OUTPUT_ENV)

# The two message kinds ADR-0018 names. An enum the producers choose from rather
# than free text, because the kind selects the rendered claim ("was not performed"
# against "was delivered") and a kind outside this pair would post a comment whose
# headline nobody wrote.
KINDS = ("declined", "delivered")

# The flag the `reply` job's `if:` reads. Named here rather than spelled in the
# writer, so the workflow's condition can be checked against the writer's output.
REPLIED_OUTPUT = "replied"

# Read from the run rather than from a producer, so it is not in OUTPUT_ENV: a
# reply's footer links the run that POSTED it.
RUN_URL_ENV = "RUN_URL"

# The heredoc delimiter for a multi-line output value. GITHUB_OUTPUT's `name=value`
# form ends at the first newline, so a reason containing one would truncate — and
# the truncated remainder would be parsed as FURTHER OUTPUTS, which is how an output
# write becomes an output injection.
#
# It carries `+` and `=` because the reason interpolates a finding's PATH, and a path
# must name a file the pull request touched — so the contributor authors the alphabet
# the guard in emit() is checking against. The policy path pattern
# (`\.?[A-Za-z0-9][A-Za-z0-9._/-]*`) admits neither character, so no legal path can
# contain this string, where an all-caps-and-underscore delimiter was a substring of
# `src/ACEIRO_DECLINE_EOF.py` and every path like it.
#
# That mattered: emit() REFUSES a value carrying the delimiter, so a contributor who
# named a file after it, on a fork, suppressed their own decline entirely — the
# "declined and told nobody" state ADR-0014 exists to prevent, reached through the
# mechanism built to prevent it, and fully self-serve since the contributor controls
# both the fork-ness and the filename. Fixed by making the delimiter inexpressible in
# the path grammar rather than by weakening the guard from refusing to escaping, which
# would trade a fail-closed check for a fail-open one.
_DELIMITER = "ACEIRO_REPLY_EOF+="

# The marker is PER-COMMAND (ADR-0018): keyed by the reviewed head SHA and the
# ordinals, so a retry of one command upserts one comment while a distinct command
# gets its own. One marker per pull request let a second command's receipt upsert
# away the first's cross-link — recreating, for the first command, the exact
# invisible-delivery gap the receipt exists to close. Accumulation only ever came
# from retries, so the per-command key keeps the wrapper-accumulation problem
# ADR-0009's first addendum measured solved.
#
# The prefix is NEVER post.MARKER's. The reviewer's sticky comment owns that one,
# and sharing it would make the two lanes fight over a single comment: the
# reviewer's next push overwriting the reply, or the reply overwriting the review.
# That is supersede_previous_reviews' unscoped-authority defect waiting to happen
# somewhere new.
MARKER_PREFIX = "<!-- aceiro:reply:"


def marker(head_sha: str, ordinals: str) -> str:
    """This command's marker. The SHA is hex and the ordinals are digits and
    commas, so nothing expressible in either can close the HTML comment or
    collide with another command's marker: ownership matches the whole stripped
    first line, and the trailing ` -->` seals the key against prefix overlap
    (`1,2` against `1,22`).
    """
    return f"{MARKER_PREFIX}{head_sha}:{ordinals} -->"


TITLES = {
    "declined": "🤖 AI fix declined",
    "delivered": "🤖 AI fix delivered",
}

VERDICTS = {
    "declined": "was not performed",
    "delivered": "was delivered",
}

NOT_A_FAILURE = (
    "This is the harness declining to act, not a run that broke. Nothing was "
    "written to this pull request."
)


def render(reason: str, *, kind: str, head_sha: str, ordinals: str, run_url: str) -> str:
    """The reply comment's body. Harness-authored prose, every sentence of it.

    No model RUNS to produce this comment — that is one of the three reasons
    ADR-0014 refuses a `decline` plan step; it would put a harness limitation into
    model-authored prose.

    But harness-authored is not the same as harness-derived, and the difference
    matters for anyone reasoning from this docstring. The undeliverable `reason`
    interpolates the commanded PATHS, and a path is CONTRIBUTOR content:
    schema-constrained, and verified to name a file the pull request touched, but
    its text is not ours. The stranded reason (ADR-0018) interpolates the branch
    name, which is GENERATOR content: plan-authored, prefix-confined and
    pattern-constrained, but chosen by a model session that read the contributor's
    diff. Both alphabets admit no backtick, so nothing in either composes markdown
    structure, and neither can express the output delimiter — asserted against the
    PATTERNS in test_reply. The receipt's reason interpolates the follow-up pull
    request's number and URL, which GitHub minted. What none of them can be called
    is a field no untrusted party influences.

    Self-dating, per ADR-0009's addendum B: the body carries the head SHA and the
    ordinals it spoke for, so it never claims a currency a later run must correct.
    An upsert destroys the previous text under the SAME marker, and the marker is
    per-command — so what a commander sees is each command's CURRENT terminal
    state: a decline replaced by the receipt once the remedy is applied, a receipt
    replaced by AlreadyDelivered's pointer to the same pull request on a re-run.
    Latest-per-command, not latest-per-channel (ADR-0018).

    `reason` is composed by the producer job and is harness text. It is placed on
    its own line rather than interpolated into a sentence, so a reworded reason
    cannot leave the surrounding prose ungrammatical.

    The declined body says it is the harness declining rather than a run that
    broke, because a decline's run is red and the commander must not read the red
    X as a crash. The receipt's run is green and carries no such sentence: there
    is nothing to explain away.
    """
    lines = [
        marker(head_sha, ordinals),
        f"## {TITLES[kind]}",
        "",
        f"`/fix {ordinals}` on head `{head_sha}` {VERDICTS[kind]}.",
        "",
        reason,
    ]
    if kind == "declined":
        lines += ["", NOT_A_FAILURE]
    lines += [
        "",
        "---",
        f"<sub>reviewed SHA: `{head_sha}` · ordinals: `{ordinals}` · [run]({run_url})</sub>",
    ]
    return "\n".join(lines)


def ordinals_of(indices) -> str:
    """The 1-BASED ordinals a commander typed, sorted, as the comment names them.

    fix_command owns the 1-based-to-0-based conversion; this is the one place it runs
    back, because the reply is addressed to the human who typed those numbers.
    Sorted rather than as-typed, so the comment names the command in the same
    canonical form every other identity in this lane uses — including the marker,
    where an unsorted spelling would give `/fix 3,1` and `/fix 1,3` two comments
    for one command.
    """
    return ",".join(str(index + 1) for index in sorted(indices))


def emit(reason: str, *, kind: str, head_sha: str, ordinals: str) -> None:
    """Write this reply's inputs to GITHUB_OUTPUT, for the `reply` job to read.

    Here rather than in each producer, because the channel has ONE reason format
    and multiple producers: `command` for the undeliverable case, `stack` for
    AlreadyDelivered, the stranded deliveries, and the receipt. Two implementations
    that must agree on their text is the defect ADR-0009's addendum B was written
    about — an artefact whose text claimed something its run had not established.

    A value carrying the heredoc delimiter is REFUSED rather than escaped: it could
    close the block early and have its remainder read as more outputs.

    That guard is **load-bearing on untrusted content**, not defence in depth. The
    reason is harness PROSE, but the undeliverable reason interpolates the commanded
    PATHS (contributor-authored, `path_must_be_changed_file`) and the stranded reason
    interpolates the plan's BRANCH NAME (generator-authored, ADR-0018) — so a
    contributor and a model each author part of the alphabet this check runs over. A
    value that contained the delimiter refused the emit and left the commander with
    no comment at all, which is why _DELIMITER carries characters neither the path
    grammar nor the branch grammar can express.

    Refusing rather than escaping stays: an escape is a fail-open answer to a value
    that should not exist, and a red run remaining red is the point. Closing the
    heredoc would need a line consisting solely of the delimiter and the path pattern
    forbids newlines, so nothing untrusted ever reached a trusted effect — what was
    reachable was SUPPRESSION, and suppression is the failure this channel was built
    to remove.

    A kind outside KINDS is refused the same way: it is a producer bug, and posting
    a comment whose headline nobody wrote is worse than the red run that gets the
    bug fixed.
    """
    if not (output := os.environ.get("GITHUB_OUTPUT")):
        return
    if kind not in KINDS:
        print(f"::error::reply kind {kind!r} is not one of {KINDS}; no reply emitted",
              file=sys.stderr)
        return
    values = {
        "reply_reason": reason,
        "reply_kind": kind,
        "reply_head_sha": head_sha,
        "reply_ordinals": ordinals,
    }
    for name in OUTPUTS:
        if not values[name]:
            print(f"::error::{name} is empty; no reply emitted", file=sys.stderr)
            return
        if _DELIMITER in values[name]:
            print(f"::error::{name} contains the output delimiter; no reply emitted",
                  file=sys.stderr)
            return
    with Path(output).open("a", encoding="utf-8") as handle:
        for name in OUTPUTS:
            handle.write(f"{name}<<{_DELIMITER}\n{values[name]}\n{_DELIMITER}\n")
        # LAST, so the flag the job's `if:` reads is only true once every value it
        # needs has been written. A reply job firing on a partial write would post
        # a comment with holes in it.
        handle.write(f"{REPLIED_OUTPUT}=true\n")


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    # Every value is required and non-empty. A reply naming no reason, or no head,
    # is a comment that tells the commander nothing while looking like an answer —
    # and an empty ORDINALS would make the body claim a command nobody typed.
    values = {}
    for name in (*OUTPUT_ENV.values(), RUN_URL_ENV):
        value = os.environ.get(name, "")
        if not value:
            fail(
                f"{name} is empty, so the reply would not say what it answered or for "
                "which head; nothing posted"
            )
        values[name] = value
    if values["KIND"] not in KINDS:
        fail(f"KIND is {values['KIND']!r}, not one of {KINDS}; nothing posted")

    body = render(
        values[OUTPUT_ENV["reply_reason"]],
        kind=values[OUTPUT_ENV["reply_kind"]],
        head_sha=values[OUTPUT_ENV["reply_head_sha"]],
        ordinals=values[OUTPUT_ENV["reply_ordinals"]],
        run_url=values[RUN_URL_ENV],
    )
    # Ownership is marker AND the login the write token resolves to, unchanged —
    # anyone can paste the marker into their own comment.
    upsert_comment(repo, pr_number, body,
                   marker(values["HEAD_SHA"], values["ORDINALS"]),
                   bot_login=resolve_bot_login())
    print(f"replied {values['KIND']}: /fix {values['ORDINALS']} on {values['HEAD_SHA']}: "
          f"{values['REASON']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
