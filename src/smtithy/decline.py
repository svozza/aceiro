"""Post the harness's reply to a command it will not perform (ADR-0014).

A **decline** is neither an artifact kind nor a plan step kind: it is a refusal the
COMMAND CHANNEL reports, before any model runs, to the person who issued the
command. `prepare_fix_context.Refused` already states that contract — "every
Refused here is a case where someone DID command a fix and the harness will not
perform it" — and what was missing was an addressee, not vocabulary.

**Exactly two refusals reply**, and the boundary is a security property rather than
a preference:

- **Undeliverable by construction** — a multi-path command on a fork pull request.
  Two commanded findings on distinct paths guarantee the stacked route, and a fork's
  head branch does not exist in the base repository for a pull request to be based
  on (ADR-0009's addendum). Derived in `command`, which already fetches the pull
  request, so the refusal costs seconds instead of a model session.
- **Already delivered** — a repeat command whose deduplication key matches an
  existing follow-up pull request. Derived in `stack`, because `fix_key` needs
  anchor signatures over the quarantine tree that `command` never fetches.

Everything else stays a red run. **The untrusted-commander refusal must never be
replied to**: trust is resolved as `prepare_fix_context`'s SECOND step, so
everything before it runs for an untrusted commenter, and replying there would let
any passer-by make the harness post a comment naming them. That is the shape
`parse_fix_command` already refuses when it declines to report malformed commands.
Two named refusals keep the decline derivable from the command's own shape: a
command the channel cannot express gets a reply, a run that failed gets a failed
run.

This module holds no derivation of its own. It formats and posts what a producer
job derived, in the job that holds `pull-requests: write` — `command` must keep
none, being reached directly from `issue_comment`.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, REASON, HEAD_SHA,
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
# so the decline is a red run that posts nothing, which is exactly the "declined to
# fix something and told nobody" case ADR-0007's third addendum forbids, arriving
# through the mechanism built to prevent it. test_workflow_shape asserts the
# workflow's `env:` block against this mapping in both directions.
#
# Declared as one set for the same reason prepare_fix_context.STEP_OUTPUTS is: a
# writer emitting a subset leaves the reader with empty strings, and a decline naming
# no reason or no head is a comment that tells the commander nothing while looking
# like an answer.
OUTPUT_ENV = {
    "decline_reason": "REASON",
    "decline_head_sha": "HEAD_SHA",
    "decline_ordinals": "ORDINALS",
}

OUTPUTS = tuple(OUTPUT_ENV)

# The flag the `decline` job's `if:` reads. Named here rather than spelled in the
# writer, so the workflow's condition can be checked against the writer's output.
DECLINED_OUTPUT = "declined"

# Read from the run rather than from a producer, so it is not in OUTPUT_ENV: a
# decline's footer links the run that POSTED it.
RUN_URL_ENV = "RUN_URL"

# The heredoc delimiter for a multi-line output value. GITHUB_OUTPUT's `name=value`
# form ends at the first newline, so a reason containing one would truncate — and
# the truncated remainder would be parsed as FURTHER OUTPUTS, which is how an output
# write becomes an output injection.
_DELIMITER = "SMTITHY_DECLINE_EOF"

# NEVER post.MARKER. The reviewer's sticky comment owns that one, and sharing it
# would make the two lanes fight over a single comment: the reviewer's next push
# overwriting the decline, or the decline overwriting the review. That is
# supersede_previous_reviews' unscoped-authority defect waiting to happen somewhere
# new.
MARKER = "<!-- smtithy:decline -->"

TITLE = "🤖 AI fix declined"

# Why this is upserted rather than appended, although a decline is the first
# harness artefact that is purely ADDITIVE in intent. The lane sets
# `cancel-in-progress: false` so no maintainer's command is discarded, which means
# every retry runs, which means N retried commands would leave N identical
# comments — the wrapper accumulation ADR-0009's first addendum measured at nine, in
# a channel with no upsert. The marker is for accumulation control, not
# reconciliation.
NOT_A_FAILURE = (
    "This is the harness declining to act, not a run that broke. Nothing was "
    "written to this pull request."
)


def render(reason: str, *, head_sha: str, ordinals: str, run_url: str) -> str:
    """The decline comment's body. Harness-authored, every word of it.

    Nothing model-controlled reaches this comment: a decline names a limitation of
    the CHANNEL, and there is no field in it a generator writes. That is one of the
    three reasons ADR-0014 refuses a `decline` plan step — it would put a harness
    limitation into model-authored prose.

    Self-dating, per ADR-0009's addendum B: the body carries the head SHA and the
    ordinals it spoke for, so it never claims a currency a later run must correct.
    An upsert destroys the previous decline's text, so a commander declined twice
    for different reasons sees only the latest — accepted, because a decline is a
    statement about the CURRENT state of the channel rather than a log, and the
    stamp is what makes that honest.

    `reason` is composed by the producer job and is harness text. It is placed on
    its own line rather than interpolated into a sentence, so a reworded reason
    cannot leave the surrounding prose ungrammatical.
    """
    return "\n".join([
        MARKER,
        f"## {TITLE}",
        "",
        f"`/fix {ordinals}` on head `{head_sha}` was not performed.",
        "",
        reason,
        "",
        NOT_A_FAILURE,
        "",
        "---",
        f"<sub>reviewed SHA: `{head_sha}` · ordinals: `{ordinals}` · [run]({run_url})</sub>",
    ])


def ordinals_of(indices) -> str:
    """The 1-BASED ordinals a commander typed, sorted, as the comment names them.

    fix_command owns the 1-based-to-0-based conversion; this is the one place it runs
    back, because the decline is addressed to the human who typed those numbers.
    Sorted rather than as-typed, so the comment names the command in the same
    canonical form every other identity in this lane uses.
    """
    return ",".join(str(index + 1) for index in sorted(indices))


def emit(reason: str, *, head_sha: str, ordinals: str) -> None:
    """Write this decline's inputs to GITHUB_OUTPUT, for the `decline` job to read.

    Here rather than in each producer, because ADR-0014 gives the decline ONE reason
    format and two producers: `command` for the undeliverable case and `stack` for
    AlreadyDelivered. Two implementations that must agree on their text is the defect
    ADR-0009's addendum B was written about — an artefact whose text claimed
    something its run had not established.

    A value carrying the heredoc delimiter is REFUSED rather than escaped: it could
    close the block early and have its remainder read as more outputs. Nothing
    model-controlled reaches here (every reason is harness prose), so this is defence
    in depth — worth having because the reason text is the part most likely to be
    reworded later, and a rewording that introduced a newline would be a silent
    truncation nobody would connect to this.
    """
    if not (output := os.environ.get("GITHUB_OUTPUT")):
        return
    values = {
        "decline_reason": reason,
        "decline_head_sha": head_sha,
        "decline_ordinals": ordinals,
    }
    for name in OUTPUTS:
        if not values[name]:
            print(f"::error::{name} is empty; no decline emitted", file=sys.stderr)
            return
        if _DELIMITER in values[name]:
            print(f"::error::{name} contains the output delimiter; no decline emitted",
                  file=sys.stderr)
            return
    with Path(output).open("a", encoding="utf-8") as handle:
        for name in OUTPUTS:
            handle.write(f"{name}<<{_DELIMITER}\n{values[name]}\n{_DELIMITER}\n")
        # LAST, so the flag the job's `if:` reads is only true once every value it
        # needs has been written. A decline job firing on a partial write would post
        # a comment with holes in it.
        handle.write(f"{DECLINED_OUTPUT}=true\n")


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    # Every value is required and non-empty. A decline naming no reason, or no head,
    # is a comment that tells the commander nothing while looking like an answer —
    # and an empty ORDINALS would make the body claim a command nobody typed.
    values = {}
    for name in (*OUTPUT_ENV.values(), RUN_URL_ENV):
        value = os.environ.get(name, "")
        if not value:
            fail(
                f"{name} is empty, so the decline would not say what it declined or for "
                "which head; nothing posted"
            )
        values[name] = value

    body = render(
        values[OUTPUT_ENV["decline_reason"]],
        head_sha=values[OUTPUT_ENV["decline_head_sha"]],
        ordinals=values[OUTPUT_ENV["decline_ordinals"]],
        run_url=values[RUN_URL_ENV],
    )
    # Ownership is marker AND the login the write token resolves to, unchanged —
    # anyone can paste the marker into their own comment.
    upsert_comment(repo, pr_number, body, MARKER, bot_login=resolve_bot_login())
    print(f"declined /fix {values['ORDINALS']} on {values['HEAD_SHA']}: {values['REASON']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
