"""Compose the remediation lane's context from a `/fix N[,M...]` command.

prepare_context's counterpart for the command channel (ADR-0007). It runs in the
gated `fix` job, before the plan session, and every precondition the command
channel adds is refused HERE — a refusal a commander can read, reached before a
model credential is in scope.

The preconditions, and why each is this module's rather than a gate's:

- **The body is a command.** A comment that is not one produces nothing at all,
  not a refusal: there is nothing to report to someone who issued no command.
- **The commenter holds write-or-above**, resolved on the COMMENT author and never
  the pull request's (ADR-0007). The valuable case is a maintainer commanding a
  fix on a first-time contributor's pull request, which requiring author trust
  would forbid.
- **The issue is a pull request.** `issue_comment` fires for both.
- **A review was posted for the head being commanded** (post.posted_review_witness).
  Deriving the commanded finding from the artifact proves it belonged to an
  accepted review; it cannot prove that review was ever posted, and the commander
  is acting on a comment they read. This is also where drift lands: the witness is
  scoped to a SHA, so a head that moved since the review has no witness.
- **Every ordinal names one of that review's findings.** Both gates refuse an
  out-of-range ordinal, but composing a context that addresses nothing would spend
  a model call to fail closed later, and this is the one place a commander sees why.
  One bad ordinal refuses the whole command: the commander asserted these findings
  take one remediation (ADR-0013), so the subset that resolves is not what they
  asked for. Resolved in RENDERED order (`artifact.rendered_findings`), the same
  order `plan_loop` and `post.render` use, because `/fix 3` is the third finding of
  the comment the commander read and `review.json` holds model order.

The composed directory is prepare_context's, plus the two files that make the
commanded finding derivable rather than supplied (ADR-0007's second addendum):
`review.json` and `commanded_index.json`. No `finding.json` — there is deliberately
no forgeable finding input anywhere in this lane.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, ISSUE_NUMBER, COMMENT_BODY,
COMMENT_AUTHOR, GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import cast

from artifact import POLICY_PATH, rendered_findings, severity_ranks
from author_trust import is_trusted
from canonicalize import read_harness_text
from reply import emit as emit_reply
from reply import ordinals_of
from fix_command import parse_fix_command
from github_api import api_json, is_fork
from post import posted_review_witness, posting_run_id, resolve_bot_login
from prepare_context import fetch_anchored_pair
from verify import Rejection, verify


# The step outputs the delivery jobs read as environment variables, declared as one
# set so the writer cannot emit a subset of them. It emitted only the two SHAs, and
# the two refs therefore reached the executor as empty strings — see main().
#
# Every entry is a GATE INPUT, which is why an empty one is a defect rather than a
# cosmetic gap: BASE_REF is the retarget comparison (ADR-0012, compared by ref
# because base.sha tracks the branch tip) and HEAD_REF is what makes the
# reviewed-head-branch push refusal reachable at all (ADR-0009 addendum).
STEP_OUTPUTS = ("head_sha", "base_sha", "base_ref", "head_ref")


class Refused(Exception):
    """A command that cannot be honoured, with the reason a commander should see.

    Distinct from producing nothing: a body that is not a command is not a
    refusal, because there is nobody to tell. Every Refused here is a case where
    someone DID command a fix and the harness will not perform it.
    """


class Undeliverable(Refused):
    """A Refused the harness REPLIES to (ADR-0014's channel, widened by ADR-0018).

    A subclass rather than a flag, because which refusals get a comment is a
    security property and not a preference. Everything else here stays a red run,
    and the untrusted-commander refusal in particular MUST NOT be replied to: trust
    is resolved as prepare()'s second step, so everything before it runs for an
    untrusted commenter and a reply there would let any passer-by make the harness
    post a comment naming them.

    Replying to every Refused would need a hand-maintained exemption for that one
    case, and a hand-maintained security exemption list is what §2's silently
    unasserted gate-lane list already cost. Raising a distinct type instead means a
    new refusal is silent unless someone chooses this class deliberately — the
    fail-closed direction.

    Carries the head SHA and the ordinals the command named, because the reply
    comment must date itself with both (ADR-0009's addendum B). They travel on the
    exception rather than being re-derived in main(): only the raising site knows
    them, and re-deriving would be a second reader of what the command said.
    """

    def __init__(self, reason: str, *, head_sha: str, ordinals: list[int]):
        super().__init__(reason)
        self.reason = reason
        self.head_sha = head_sha
        # 1-BASED and sorted, through the one helper that converts back, because the
        # comment is addressed to the human who typed those numbers.
        self.ordinals = ordinals_of(ordinals)


def fetch_reviewed_artifact(repo: str, pr_number: int, head_sha: str, output_dir: Path,
                            *, run_id: int) -> dict:
    """The accepted review artifact for `head_sha`, from the run that POSTED it.

    The artifact is the trust anchor for the commanded finding, so which artifact
    this is decides which defect a `/fix N` addresses and what text the plan
    session reads inside the `<commanded_finding>` fence. `run_id` is therefore
    required, not optional: it comes from the footer of our own posted comment
    (post.posting_run_id), so the artifact is bound to the run whose review the
    commander actually read.

    The name alone cannot establish that. It is derivable from the pull request
    number and the head SHA — both public — and the listing is repository-wide, so
    any artifact in the repository carrying that name would otherwise be a
    candidate, and `max(id)` would prefer the newest one rather than the posted
    one. Matching on `workflow_run.id` is what makes this the review job's OUTPUT
    rather than merely something named like it.

    Retention is finite (90 days), and the name is keyed on the reviewed SHA, so
    "absent" is a real state rather than a theoretical one and it is refused rather
    than worked around.
    """
    name = f"ai-review-{pr_number}-{head_sha}"
    listed = cast("dict", api_json(f"/repos/{repo}/actions/artifacts?name={name}"))
    artifacts = [
        a for a in listed.get("artifacts", [])
        if not a.get("expired")
        and (a.get("workflow_run") or {}).get("id") == run_id
    ]
    if not artifacts:
        raise Refused(
            f"no unexpired artifact {name!r} from run {run_id}, the run that posted the review "
            "this command names, so the review whose finding is commanded cannot be read; no fix"
        )
    # One run uploads the name once; if that ever changes, the newest upload of
    # THAT run is still the one its own comment describes.
    newest = max(artifacts, key=lambda a: a["id"])
    return download_review(repo, newest["id"], output_dir)


def download_review(repo: str, artifact_id: int, output_dir: Path) -> dict:
    """Unzip the review bundle and return its review.json.

    Separate from the listing so the network shape of each half is stubbable on
    its own, and because this is the step that touches the filesystem.
    """
    import io
    import zipfile

    from github_api import api_request

    payload = api_request(f"/repos/{repo}/actions/artifacts/{artifact_id}/zip")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            raw = bundle.read("review.json")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise Refused(
            f"the review run's artifact carries no readable review.json ({exc}); no fix"
        ) from exc
    return cast("dict", json.loads(raw.decode("utf-8")))


def prepare(*, repo: str, issue_number: int, comment_body: str, commenter: str,
            output_dir: Path, policy: dict) -> dict | None:
    """Compose the plan session's context, or None when the body is no command.

    Raises Refused when a command cannot be honoured. Nothing is written until
    every precondition has passed: a partially composed directory would be a
    context the plan session could read.
    """
    indices = parse_fix_command(comment_body)
    if indices is None:
        return None

    # Trust first, and on the COMMENT author (ADR-0007). Before the artifact
    # download and before any content is read, so an untrusted commenter cannot
    # make this job do work on their behalf.
    if not is_trusted(repo, commenter):
        raise Refused(
            f"{commenter!r} does not hold write-or-above on {repo}, so they cannot command a "
            "fix (ADR-0007: trust follows the commander)"
        )

    pr = cast("dict", api_json(f"/repos/{repo}/issues/{issue_number}"))
    if "pull_request" not in pr:
        raise Refused(
            f"issue #{issue_number} is not a pull request, so there is no change to fix"
        )
    pr = cast("dict", api_json(f"/repos/{repo}/pulls/{issue_number}"))
    head_sha = pr["head"]["sha"]
    base_sha = pr["base"]["sha"]
    # Both REFS as well as both SHAs, because an issue_comment payload carries none
    # of them and every one is a gate input downstream (ADR-0012 for the base ref,
    # ADR-0009's addendum for the head ref). They are resolved here, once, from the
    # pull request this command names.
    base_ref = pr["base"]["ref"]
    head_ref = pr["head"]["ref"]

    # The witness, against the LIVE head. This is also the drift refusal ADR-0007
    # requires: issue_comment carries no SHA, so the head is whatever it is now,
    # and a review posted for an earlier push does not carry this SHA's stamp.
    bot_login = resolve_bot_login()
    if posted_review_witness(repo, issue_number, head_sha, bot_login=bot_login) is None:
        raise Refused(
            f"no posted review for the current head {head_sha}: either the head moved since the "
            "review the command names, or no review was posted for it; no fix"
        )

    # Which run posted it, so the artifact read below is that run's own output.
    # Refused rather than defaulted: without this the artifact would be chosen by
    # name and recency, and a same-named artifact from any other run in the
    # repository would become the trust anchor for the commanded finding.
    posting_run = posting_run_id(repo, issue_number, head_sha, bot_login=bot_login)
    if posting_run is None:
        raise Refused(
            f"the posted review for {head_sha} carries no run link in its footer, so the artifact "
            "whose finding is commanded cannot be bound to the run that posted it; no fix"
        )

    review = fetch_reviewed_artifact(
        repo, issue_number, head_sha, output_dir, run_id=posting_run)

    diff_bytes, changed_files = fetch_anchored_pair(repo, base_sha, head_sha)

    # Verified here as well as at both gates. The gates' re-verification is what
    # makes the property hold; this is what makes the refusal legible, and it runs
    # against the same anchored pair the context will carry.
    try:
        verify(review, diff_bytes.decode("utf-8", errors="replace"), changed_files, policy)
    except Rejection as exc:
        raise Refused(
            f"the posted review for {head_sha} is not one the verifier accepts ({exc}); no fix"
        ) from exc

    # RENDERED order, the same resolution plan_loop.read_commanded_findings and
    # post.render use: `/fix 3` is the third finding of the comment the commander
    # read, and review.json holds model order. This read fed only the
    # order-invariant range check below until the decline started reading a
    # finding's CONTENT — after which model order names a different real finding
    # whenever the model's order is not already sorted, and the decline fires on the
    # wrong command in both directions. The harness reaches that case by its own
    # advice: post.group_cross_reference composes a `/fix N,M` in rendered ordinals
    # and tells the commander to type it verbatim.
    findings = rendered_findings(review, severity_ranks(policy))
    # Every ordinal, and one out of range refuses the WHOLE command: the commander
    # asserted that these findings take one remediation, so honouring the subset
    # that happens to resolve would deliver a scope nobody named (ADR-0013).
    if past_the_end := sorted(index + 1 for index in indices if index >= len(findings)):
        raise Refused(
            f"the command names finding(s) {past_the_end} but the posted review has "
            f"{len(findings)}; no fix"
        )

    # Undeliverable by construction, checked HERE where it costs seconds (ADR-0014).
    # Two commanded findings on DISTINCT paths mean the fix must touch both paths
    # (check_commanded_scope, ⊆), a review comment carries exactly one `path`, so
    # decide_delivery routes stacked_pr — and a fork's head branch does not exist in
    # the base repository for a pull request to be based on (ADR-0009's addendum).
    # Nothing about that can change between here and the delivery, so it is knowable
    # at command time and the commander would otherwise spend the approval gate, a
    # model session, both gates and a contents: write job to receive a red run
    # with an ::error:: line in a log they must click into.
    #
    # Before the artifact is composed, so no context the plan session could read is
    # written. The paths come from the DERIVED findings — resolved above through the
    # accepted artifact — so nothing forgeable decides whether this fires.
    commanded_paths = {findings[index]["path"] for index in indices}
    if len(commanded_paths) > 1 and is_fork(pr):
        raise Undeliverable(
            f"The command names findings on {len(commanded_paths)} files "
            f"({', '.join(f'`{path}`' for path in sorted(commanded_paths))}), so the fix must "
            "touch every one of them. A suggestion comment carries exactly one file, so a "
            "multi-file fix can only be delivered as a stacked follow-up pull request — and this "
            "pull request comes from a **fork**, whose head branch does not exist in this "
            "repository for a pull request to be based on (ADR-0009). There is no delivery for "
            "this command on this pull request. Commanding each finding on its own will deliver "
            "each half as a suggestion, which is a partial fix; the review comment's "
            "cross-reference says what they are halves of.",
            head_sha=head_sha,
            ordinals=sorted(indices),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "pr.json").write_text(json.dumps({
        "number": issue_number,
        "title": pr["title"],
        "body": pr["body"],
        "base_sha": base_sha,
        "head_sha": head_sha,
    }, ensure_ascii=False))
    (output_dir / "diff.patch").write_bytes(diff_bytes)
    (output_dir / "changed_files.json").write_text(json.dumps(changed_files), encoding="utf-8")
    (output_dir / "review.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    # SORTED, so the file is byte-identical for `/fix 3,1` and `/fix 1,3` — the same
    # property stack.fix_key rests on, held one step earlier so the two cannot
    # disagree about whether ordering was part of the command.
    (output_dir / "commanded_index.json").write_text(
        json.dumps({"indices": sorted(indices)}), encoding="utf-8")
    return {"head_sha": head_sha, "base_sha": base_sha,
            "base_ref": base_ref, "head_ref": head_ref, "indices": sorted(indices)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    issue_number = int(os.environ["ISSUE_NUMBER"])
    policy = json.loads(read_harness_text(POLICY_PATH))

    try:
        result = prepare(
            repo=repo,
            issue_number=issue_number,
            comment_body=os.environ["COMMENT_BODY"],
            commenter=os.environ["COMMENT_AUTHOR"],
            output_dir=args.output_dir,
            policy=policy,
        )
    except Undeliverable as exc:
        # ADR-0014: a refusal the harness REPLIES to. The reason and the facts the
        # comment must date itself with are emitted for the `reply` job, which
        # holds pull-requests: write; this job holds none, being reached directly
        # from issue_comment. The run still FAILS — the command was not performed,
        # and a green run claiming otherwise would be the artefact whose text
        # over-claims that ADR-0009's addendum B was written about.
        print(f"::error::{exc}", file=sys.stderr)
        emit_reply(exc.reason, kind="declined", head_sha=exc.head_sha, ordinals=exc.ordinals)
        return 1
    except Refused as exc:
        # A refusal is the run's outcome, not a crash: it exits non-zero so the
        # check is red where the commander is looking, with the reason in the log.
        # NO reply: see Undeliverable's docstring — the untrusted-commander refusal
        # arrives here, and it is reached before trust is resolved.
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if result is None:
        print("no /fix command in this comment; nothing to do")
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write("commanded=false\n")
        return 0

    ordinals = ", ".join(str(index + 1) for index in result["indices"])
    print(f"commanded finding(s) {ordinals} on head {result['head_sha']}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write("commanded=true\n")
            for name in STEP_OUTPUTS:
                value = result[name]
                # Emitted from a declared set rather than line by line, and refused
                # if empty. Two of these were simply MISSING from the writer, so the
                # executor read them as empty strings: the retarget check then
                # compared a live ref against "" and refused every command, and the
                # reviewed-head-branch refusal matched no branch and silently
                # enforced nothing. An empty value is worse than an absent one,
                # because os.environ[...] succeeds and the reader's fail-closed
                # KeyError never fires.
                if not value:
                    print(f"::error::{name} resolved empty; it is a gate input and "
                          "an empty one disables the gate that reads it", file=sys.stderr)
                    return 1
                handle.write(f"{name}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
