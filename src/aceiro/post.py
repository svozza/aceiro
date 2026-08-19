"""Trusted executor: verify, render via a fixed template, upsert one comment.

Runs in the post job (the only job holding `pull-requests: write`). Trusts
nothing from the review job: re-runs the verifier here against provenance
inputs it fetches itself (the SHA-anchored diff and changed-file list, not the
bundle's copies of them), re-checks the PR head SHA still equals the reviewed
SHA (TOCTOU guard), then renders the artifact through a fixed template and
upserts a single sticky comment identified by a hidden HTML marker.

The artifact is the one thing that must come from the bundle, being the review
job's output. Everything the artifact is CHECKED against is first-party.

Any verifier rejection or SHA mismatch: nothing is posted, exit non-zero.

Comment ownership is marker AND author, and the author half is resolved at
runtime from the write token itself (resolve_bot_login) rather than configured:
a configured login can drift from the token actually posting, and this value
decides which prior comments the executor may edit. Unresolvable identity is
also fail-closed.

Ownership is per-generator, not per-run: every run for a PR shares the marker
and the login, so the withdrawal is additionally scoped to the reviewed-SHA
stamp in the body it is about to replace. The workflow serializes runs per PR,
and this is what holds when serialization does not.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA, BASE_SHA,
BASE_REF (the reviewed base BRANCH, which is what a retarget changes),
RUN_URL.
Arguments: --artifact-dir (review.json + run_metadata.json + context files),
--policy, --prompt, and optionally --marker/--title to identify which
generator's comment this is.

One executor, several generators. `--marker` exists because the comment identity
is per-GENERATOR while this code is shared: two reviewers posting under one marker
would upsert the SAME comment and overwrite each other's review. Copying this file
per generator was the alternative and is worse -- it is the verifier boundary, so a
fork means the trusted re-verification drifts per generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import cast

from artifact import redact_line, rendered_findings, severity_ranks
from github_api import api_json, fail, graphql, paginate, pr_moved
from canonicalize import decode_contributor_bytes, read_harness_text
from prepare_context import fetch_anchored_pair
from secret_taint import candidates_from_diff
from verify import GROUP_FIELD, NEWLINES_RE, Rejection, verify

# The incumbent's marker and heading. Defaults, so a caller passing neither posts
# exactly what it always did; a second generator overrides both.
MARKER = "<!-- ai-pr-review-sticky-comment -->"
TITLE = "🤖 AI Code Review"

# Presentation only; severity *order* comes from policy.json's enum (most
# severe first), the single home of the severity vocabulary.
SEVERITY_LABEL = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🔵 Low",
}


def sha_stamp(sha: str) -> str:
    """The footer's reviewed-SHA clause, which is also what identifies a posted
    comment as THIS run's. Rendered by one function so the withdrawal's search
    string cannot drift from what render() wrote."""
    return f"reviewed SHA: `{sha}`"


def stamped_for(body: str | None, sha: str) -> bool:
    """Whether `body` is a comment WE rendered for `sha`.

    Read from the LAST line, not searched for anywhere in the body. render()
    splices the summary, every finding body and the residual risk above the
    footer, and all three are generator text quoting contributor code — so a
    contributor can plant `reviewed SHA: \\`<sha>\\`` in a line of their diff, have
    it quoted into a finding, and a substring search would then accept that
    comment as proof a review was posted for a SHA it was never posted for. The
    footer is the only part of the body the executor authors, and it is the last
    line, so POSITION is what makes the stamp ours rather than quoted. The clause
    order within the footer is deliberately not matched: render() owns that
    wording, and pinning it here would make the witness fail closed on a footer
    reword — a refusal nobody would connect to this function.

    Same shape as find_own_comments' marker-from-line-1 rule, and the same trade:
    a line appended below our footer makes the comment unrecognisable. That fails
    CLOSED here — an unrecognised comment is no witness and withdraws nothing.
    """
    last_line = NEWLINES_RE.sub("\n", body or "").rstrip("\n").rsplit("\n", 1)[-1]
    return sha_stamp(sha) in last_line


def group_cross_reference(findings: list[dict], index: int) -> str | None:
    """The line under a grouped finding naming its siblings, or None for a singleton.

    ADR-0013's disclosure half. A commander cannot type `/fix 1,3` without being
    told that findings 1 and 3 are one defect, and the reviewer is the only
    participant that ever knows — so the artifact carries the claim (`group`) and
    the HARNESS renders the reference.

    Rendered here and nowhere else, because this is where ordinals exist. A
    model-authored "see also finding 3" cannot name an ordinal at all:
    `rendered_findings` sorts by severity at render time and the model never sees
    the sorted list, so model prose would name a DIFFERENT REAL FINDING whenever
    the two orders differ — the silent wrong-finding failure ADR-0007's second
    addendum exists to prevent. `findings` here is already the rendered list, and
    the ordinals are its positions.

    Harness-authored text, so nothing model-controlled composes structure: the
    only interpolated values are ordinals (integers), paths and lines. A path is
    contributor-supplied and pattern-constrained by the schema, and it is placed in
    a code span the same way the finding's own path already is.

    The reference is prose to a HUMAN and is never read back. Nothing parses it,
    and no code in the fix lane may read `group` at all — what authorises a write
    is the ordinals the commander typed.
    """
    group = findings[index][GROUP_FIELD]
    siblings = [
        (position, finding) for position, finding in enumerate(findings, start=1)
        if finding[GROUP_FIELD] == group and position != index + 1
    ]
    if not siblings:
        return None
    named = ", ".join(
        f"finding {position} (`{finding['path']}` line {finding['line']})"
        for position, finding in siblings
    )
    # Every ordinal in the group, this one included, in rendered order — that is
    # the command a reader can type verbatim.
    command = ",".join(
        str(position) for position, finding in enumerate(findings, start=1)
        if finding[GROUP_FIELD] == group
    )
    return (
        f"*Part of one coordinated fix with {named}. "
        f"`/fix {command}` remediates them together; "
        f"fixing one alone leaves the other half in place.*"
    )


def render(
    artifact: dict,
    metadata: dict,
    severity_order: dict[str, int],
    marker: str = MARKER,
    title: str = TITLE,
) -> str:
    """Fixed template. Model text is inserted verbatim only after the verifier
    has proven it inside the safe grammar; everything structural is ours.

    `marker` and `title` are the only per-generator parts, and both are ours
    rather than the model's -- they are never taken from the artifact.
    """
    lines = [
        marker,
        f"## {title}",
        "",
        "> [!NOTE]",
        "> This review was generated by an AI model. It is **not a human review**, "
        "may be wrong, and counts toward **no approval**. Treat it as a hint list.",
        "",
        "### Summary",
        "",
        artifact["summary"],
        "",
    ]

    findings = rendered_findings(artifact, severity_order)
    if findings:
        lines += ["### Findings", ""]
        for index, finding in enumerate(findings):
            lines += [
                f"#### {SEVERITY_LABEL.get(finding['severity'], finding['severity'])} — {finding['title']}",
                "",
                f"`{finding['path']}` line {finding['line']}",
                "",
                finding["body"],
                "",
            ]
            # Under the body, so a reader has the defect before the coordination
            # note. Singletons render nothing at all — the ordinary case is one
            # finding per group, and a line saying so on every finding would be
            # noise that teaches readers to skip the ones that matter.
            if reference := group_cross_reference(findings, index):
                lines += [reference, ""]
    else:
        lines += ["### Findings", "", "No confirmed defects found.", ""]

    if artifact["residual_risk"]:
        lines += ["### Residual risk", "", artifact["residual_risk"], ""]

    lines += [
        "---",
        "<sub>model: `{model}` · prompt: `{prompt}` · policy: `{policy}` · "
        "{stamp} · [run]({run_url})</sub>".format(stamp=sha_stamp(metadata["sha"]), **metadata),
    ]
    return "\n".join(lines)


VIEWER_LOGIN_QUERY = "query { viewer { login } }"


def resolve_bot_login() -> str:
    """The login the write token authenticates as, asked of the token itself.

    This decides which prior comments are OURS to edit or supersede: anyone can
    paste the marker into their own comment, so ownership is marker AND author,
    and the author half must come from the credential in hand -- a configured
    value can silently disagree with the token actually posting (the exact drift
    a consumer swapping GITHUB_TOKEN for an app token would introduce).

    GraphQL rather than REST GET /user because it is the one identity call every
    token type answers: /user is 403 "Resource not accessible by integration"
    for app installation tokens, which is what Actions' GITHUB_TOKEN is. For a
    bot, viewer.login comes back WITH the "[bot]" suffix (github-actions[bot],
    <app-slug>[bot]) -- byte-identical to the user.login on comments the token
    creates, so no mapping sits between resolution and the ownership check.

    Through github_api.graphql, not api_json: GraphQL answers HTTP 200 with an
    `errors` array rather than an error status, so a response can carry a usable
    login AND an error. Calling api_json directly skipped that check, and the same
    response was a clean success here and a raise through graphql() — so a
    partially-errored 200 (login present, another requested field errored) was
    consumed as identity. One helper, one errors check, both call sites.

    Fail-closed: anything but a non-empty login exits without posting. Guessing
    here means the executor may edit comments it does not own.
    """
    try:
        data = graphql(VIEWER_LOGIN_QUERY, {})
    except (RuntimeError, OSError, ValueError) as exc:
        fail(f"could not resolve the token's own login, nothing posted ({exc})")
    login = ((data.get("viewer")) or {}).get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not login:
        fail(f"could not resolve the token's own login, nothing posted (response: {json.dumps(data)[:300]})")
    return login


# The footer's run link, as render() writes it: .../actions/runs/<run_id>. Read
# back to bind an artifact to the run that POSTED, never to pick a run.
_FOOTER_RUN_RE = re.compile(r"/actions/runs/(\d+)")


def posting_run_id(repo: str, pr_number: int, reviewed_sha: str, *,
                   marker: str = MARKER, bot_login: str) -> int | None:
    """The Actions run id that posted our review for `reviewed_sha`, or None.

    The witness answers "was a review posted for this head". This answers "by
    which run", which is what lets the remediation lane fetch the artifact that
    run uploaded rather than whichever same-named artifact happens to be newest.

    Read from the same last line as stamped_for, and for the same reason: the
    footer is the only part of the body the executor authors. A run id recovered
    from anywhere else in the body would be contributor-influenced, which is the
    defect this closes rather than a way to close it. Returns None when the footer
    carries no run link, so a caller must decide what an unbindable comment means
    — this function never guesses a run.
    """
    existing = find_own_comment(repo, pr_number, marker, bot_login)
    if existing is None or not stamped_for(existing.get("body"), reviewed_sha):
        return None
    body = NEWLINES_RE.sub("\n", existing.get("body") or "").rstrip("\n")
    found = _FOOTER_RUN_RE.search(body.rsplit("\n", 1)[-1])
    return int(found.group(1)) if found else None


def posted_review_witness(repo: str, pr_number: int, reviewed_sha: str, *,
                          marker: str = MARKER, bot_login: str) -> int | None:
    """The id of our posted review for `reviewed_sha`, or None if there is none.

    The remediation channel's precondition (ADR-0007's second addendum). Deriving
    the commanded finding from review.json proves it belonged to an artifact the
    verifier accepts; it cannot prove that artifact was ever POSTED — and a
    commander is acting on a comment they read. This is that half, and it is
    GitHub's own authorship record rather than anything the harness signs: only
    the harness's credential can produce a comment whose first line is the marker
    and whose author is the login the write token resolves to.

    Existence and SHA only. The comment's markdown is deliberately never parsed
    back into a finding: bodies quote contributor code, so a fenced block can
    carry a literal `####` heading, and recovering structure from it would put a
    contributor-influenced parse in the trust path. The artifact answers "which
    finding"; this answers "was a review posted for this head", which is the only
    question the artifact cannot.

    Ownership and the stamp are both reused rather than restated — find_own_comment
    for the marker-and-author rule, stamped_for for the footer test — so a witness
    cannot come to disagree with the comment render() writes.
    """
    existing = find_own_comment(repo, pr_number, marker, bot_login)
    if existing is None or not stamped_for(existing.get("body"), reviewed_sha):
        return None
    return cast("int", existing["id"])


# A model identifier's lexicon, and nothing else. render() splices this value
# inside a code span inside a <sub>, so the requirement is lexical rather than
# grammatical: no backtick, no `<`, no whitespace, no newline. `:` `/` `.` `-`
# stay because Bedrock ARNs and inference-profile ids are built from them, and
# the cap is generous for the same reason.
MODEL_STAMP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")


def read_model_stamp(artifact_dir: Path) -> str:
    """The model the generator actually invoked, for the attribution footer.

    Produced by the arm that ran (cc_loop writes it from the AssistantMessage)
    rather than read from configuration here: both model arms are configured on
    every run — the Bedrock profile and the CLI model are separate inputs, each
    with a default — so configuration cannot say which one drove the session.

    Constrained to MODEL_STAMP_RE, because the bundle is the generator job's
    output and this executor trusts it for nothing else: an unconstrained string
    interpolated into the footer composes Markdown structure of the model's
    choosing under the harness's authenticated identity.

    Fail-closed. The stamp is the audit trail for "which model said this", and a
    placeholder would be a false one; there is no honest fallback.
    """
    path = artifact_dir / "run_metadata.json"
    if not path.is_file():
        fail(f"no run_metadata.json in the bundle: the model that produced this artifact is unknown ({path})")
    model = json.loads(read_harness_text(path)).get("model")
    if not isinstance(model, str) or not model:
        fail(f"run_metadata.json names no model, so the artifact cannot be attributed ({path})")
    if not MODEL_STAMP_RE.fullmatch(model):
        # The offending value is not echoed: it is the thing that composes
        # structure, and the job log is the one emit path with no redaction.
        fail(
            "run_metadata.json names a model outside the identifier charset, so the artifact "
            f"cannot be attributed ({path})"
        )
    return model


def check_marker(marker: str) -> None:
    """Refuse a marker that cannot identify one comment.

    An empty or whitespace-only marker made EVERY comment ours (`"" in body` is
    always true), so a consumer passing --marker "" replaced the first bot
    comment on the pull request with the review. A multi-line one is the mirror
    failure: render() writes the marker on line 1, so a marker containing a
    newline can never BE that line — it would search for a comment it does not
    post and create a new one every run.

    Surrounding whitespace is the same failure once more: find_own_comments
    compares the STRIPPED first line — the strip absorbs the CRLF GitHub returns —
    against the marker as given, so a padded marker never matches the comment it
    posted. The run posts, sees no comment it owns next time, and posts again,
    without bound.

    Refused rather than accommodated: there is no honest reading of "identify my
    comment by nothing", and this value decides which comments the write token
    may edit.
    """
    if not marker.strip():
        fail("--marker is empty; an empty marker matches every comment, so nothing identifies ours")
    if "\n" in marker or "\r" in marker:
        fail(f"--marker spans lines ({marker!r}); the marker is the comment's first line, so it must be one line")
    if marker != marker.strip():
        fail(
            f"--marker carries surrounding whitespace ({marker!r}); ownership compares the "
            "stripped first line, so this marker would never match its own comment and every "
            "run would post another"
        )


def find_own_comments(repo: str, pr_number: int, marker: str, bot_login: str) -> list[dict]:
    """Every comment this generator owns, oldest first.

    Plural because there can be more than one and there should not be: two runs
    that both paginated before either created a comment each POST, and the PR
    keeps both with the loser stale forever. The pagination order is creation
    order, so the first is the incumbent and the rest are duplicates to retire.
    """
    check_marker(marker)
    return [
        comment
        for page in paginate(f"/repos/{repo}/issues/{pr_number}/comments?")
        for comment in page
        if (comment.get("body") or "").split("\n", 1)[0].strip() == marker
        and (comment.get("user") or {}).get("login") == bot_login
    ]


def find_own_comment(repo: str, pr_number: int, marker: str, bot_login: str) -> dict | None:
    """This generator's own sticky comment, or None.

    Matched on the FIRST LINE being exactly `marker`, AND on author. Two
    independent halves: anyone can paste the marker into their own comment, so
    the author half is what makes a match ours to edit — and a substring match
    owned any comment merely CONTAINING the marker, including one of ours
    discussing it, so the first-line half is what makes the match the comment
    render() wrote rather than one that mentions it.
    """
    owned = find_own_comments(repo, pr_number, marker, bot_login)
    return owned[0] if owned else None


# What a retired duplicate says. It gives up the marker — the retirement has to
# surrender OWNERSHIP, or the next run finds the same duplicate and the pull
# request never converges — so it carries no marker line at all.
DUPLICATE_NOTICE = (
    "**Superseded AI review.** A concurrent run posted this alongside another "
    "copy; the live review is the other comment. Nothing here is current."
)


def upsert_comment(repo: str, pr_number: int, body: str, marker: str = MARKER, *, bot_login: str) -> int | None:
    """Update this generator's own sticky comment, or create it. Returns the id
    of the comment it updated, or None when it created one.

    The marker must be the SAME one render() wrote, or a reviewer would search
    for a comment it never posts and create a new one every run -- so callers
    pass one value to both. `bot_login` is keyword-only and has no default
    because it is the security half of the match: the one valid source is
    resolve_bot_login(), and a hardcoded fallback would be the coupling this
    parameter replaced.
    """
    owned = find_own_comments(repo, pr_number, marker, bot_login)

    # Reconcile before writing. More than one owned comment means a previous race
    # left duplicates (a run before the workflow's per-PR concurrency group, one
    # cancelled mid-POST, or a consumer without the group), and nothing ever
    # looked for a second one — so the loser sat stale indefinitely. The oldest
    # carries the review; the rest are retired and give up the marker.
    for duplicate in owned[1:]:
        api_json(
            f"/repos/{repo}/issues/comments/{duplicate['id']}",
            method="PATCH",
            payload={"body": DUPLICATE_NOTICE},
        )
        print(f"retired duplicate comment {duplicate['id']}")

    if owned:
        existing = owned[0]
        api_json(f"/repos/{repo}/issues/comments/{existing['id']}", method="PATCH", payload={"body": body})
        print(f"updated existing comment {existing['id']}")
        return cast("int", existing["id"])
    api_json(f"/repos/{repo}/issues/{pr_number}/comments", method="POST", payload={"body": body})
    print("created new comment")
    return None


# Kept inside the model-field safe grammar (no blockquote/heading — those are
# disallowed even for us where a test can hold us to it) so the one static
# body post.py writes is provably within the same envelope it enforces.
STALE_NOTICE = (
    "**AI review withdrawn.** The PR's head or base changed while the review "
    "was being posted, so it described a different diff. A new review will be "
    "posted by the run for the current revision."
)


def withdraw_own_review(repo: str, pr_number: int, marker: str, reviewed_sha: str, *, bot_login: str) -> bool:
    """Replace this run's own posted review with STALE_NOTICE. Returns whether
    a withdrawal was written.

    Scoped to the comment still carrying THIS run's reviewed-SHA stamp. Every
    run for the same PR shares the marker and the bot login, so an unscoped
    upsert would withdraw whatever is there — and the run that loses the race
    is the OLD one, whose withdrawal would land on top of the newer revision's
    valid review, leaving the PR with a withdrawal notice for a review that was
    never stale and no event to correct it.
    """
    existing = find_own_comment(repo, pr_number, marker, bot_login)
    if existing is None or not stamped_for(existing.get("body"), reviewed_sha):
        print(f"our review for {reviewed_sha} is no longer the posted comment; nothing withdrawn")
        return False
    api_json(
        f"/repos/{repo}/issues/comments/{existing['id']}",
        method="PATCH",
        payload={"body": f"{marker}\n{STALE_NOTICE}"},
    )
    print(f"withdrew comment {existing['id']}")
    return True


def check_pr_unmoved(repo: str, pr_number: int, reviewed_head: str, reviewed_base_ref: str) -> str | None:
    """Return None if the PR still points at the reviewed head AND base branch,
    else a human-readable description of what moved. The base matters too: a
    retarget (head unchanged, base edited) changes the diff the review claims to
    describe just as surely as a push does. See github_api.pr_moved for why the
    base half is a ref comparison and not a SHA one."""
    return pr_moved(cast("dict", api_json(f"/repos/{repo}/pulls/{pr_number}")), reviewed_head, reviewed_base_ref)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    # Default to the incumbent's identity so its call site is unchanged.
    parser.add_argument("--marker", default=MARKER, help="Hidden HTML marker identifying this generator's comment.")
    parser.add_argument("--title", default=TITLE, help="Comment heading naming the generator.")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    reviewed_sha = os.environ["HEAD_SHA"]
    # Two roles, deliberately separate: the SHA anchors the diff, the ref
    # detects a retarget. BASE_SHA is unusable for the second (github_api.pr_moved).
    reviewed_base = os.environ["BASE_SHA"]
    reviewed_base_ref = os.environ["BASE_REF"]

    artifact_path = args.artifact_dir / "review.json"
    try:
        artifact = json.loads(read_harness_text(artifact_path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(
            f"review artifact is missing, unreadable, or invalid JSON "
            f"({type(exc).__name__}); nothing posted"
        )
    policy_text = read_harness_text(args.policy)
    policy = json.loads(policy_text)

    # The provenance inputs are re-fetched, not read from the bundle. The
    # artifact must come from the review job -- it IS that job's output -- but
    # the diff and the changed-file list are facts about the PR that this job's
    # own token can establish, and re-verifying against the bundle's copies
    # would make the provenance phase only as strong as the job it distrusts.
    # The bundle copies stay in the artifact as reproducibility evidence.
    diff_bytes, changed_files = fetch_anchored_pair(repo, reviewed_base, reviewed_sha)
    diff_text = decode_contributor_bytes(diff_bytes)
    candidates_from_diff(diff_text, policy)

    # Verification happens HERE, where the write token lives. Job 2's claims
    # about having verified anything are not trusted.
    try:
        verify(artifact, diff_text, changed_files, policy)
    except Rejection as exc:
        # Redacted here rather than in github_api.fail: that module imports only
        # stdlib and is shared with prepare_context, so it has no policy in scope,
        # and a Rejection cannot redact itself either — it is raised from checks
        # that take no policy argument. The caller holding the policy is the one
        # place both facts are available.
        fail(f"artifact rejected, nothing posted: {redact_line(str(exc), policy)}")

    # TOCTOU guard, first half: the PR must still point at the reviewed head
    # and base before we render anything.
    if moved := check_pr_unmoved(repo, pr_number, reviewed_sha, reviewed_base_ref):
        fail(f"{moved}; nothing posted")

    # Resolved before the first write and reused for the withdrawal: the two
    # upserts must agree on identity, or a stale notice could land in a new
    # comment while the outdated review stays up.
    bot_login = resolve_bot_login()

    metadata = {
        "model": read_model_stamp(args.artifact_dir),
        "prompt": hashlib.sha256(args.prompt.read_bytes()).hexdigest()[:12],
        "policy": hashlib.sha256(policy_text.encode()).hexdigest()[:12],
        "sha": reviewed_sha,
        "run_url": os.environ["RUN_URL"],
    }
    severity_order = severity_ranks(policy)
    body = render(artifact, metadata, severity_order, args.marker, args.title)
    upsert_comment(repo, pr_number, body, args.marker, bot_login=bot_login)

    # TOCTOU guard, second half: the pre-check and the write are not atomic —
    # a push (or base retarget) landing between them leaves a review describing
    # the OLD diff attached to the new one, with no run to correct it if the
    # attacker cancels the new revision's workflow. Recheck after the write;
    # if the PR moved, overwrite our comment with a stale notice and fail.
    if moved := check_pr_unmoved(repo, pr_number, reviewed_sha, reviewed_base_ref):
        withdraw_own_review(repo, pr_number, args.marker, reviewed_sha, bot_login=bot_login)
        fail(f"{moved} while posting; review withdrawn")


if __name__ == "__main__":
    main()
