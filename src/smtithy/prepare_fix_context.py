"""Compose the remediation lane's context from a `/fix N` command.

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
- **The ordinal names one of that review's findings.** Both gates refuse an
  out-of-range ordinal, but composing a context that addresses nothing would spend
  a model call to fail closed later, and this is the one place a commander sees why.

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

from artifact import POLICY_PATH
from author_trust import is_trusted
from canonicalize import read_harness_text
from fix_command import parse_fix_command
from github_api import api_json
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
    index = parse_fix_command(comment_body)
    if index is None:
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

    findings = review["findings"]
    if index >= len(findings):
        raise Refused(
            f"the command names finding {index + 1} but the posted review has {len(findings)}; "
            "no fix"
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
    (output_dir / "commanded_index.json").write_text(json.dumps({"index": index}), encoding="utf-8")
    return {"head_sha": head_sha, "base_sha": base_sha,
            "base_ref": base_ref, "head_ref": head_ref, "index": index}


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
    except Refused as exc:
        # A refusal is the run's outcome, not a crash: it exits non-zero so the
        # check is red where the commander is looking, with the reason in the log.
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if result is None:
        print("no /fix command in this comment; nothing to do")
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write("commanded=false\n")
        return 0

    print(f"commanded finding {result['index'] + 1} on head {result['head_sha']}")
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
