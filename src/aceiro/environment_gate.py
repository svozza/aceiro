"""Assert the human-approval environment actually gates — ADR-0006's check.

The approval gate is an Actions environment with required reviewers. On the
extraction, that construction fails open silently: a called workflow's
`environment:` resolves against the CALLER's repository, and GitHub creates a
referenced environment that does not exist — with no protection rules. The
`approve` job then succeeds instantly, and an untrusted fork PR reaches the
generator with a live credential in scope. The run is green and nothing warns.

So the gate is asserted in code, inside the gated job itself (a separate gate
job can be skipped, and a skipped ancestor skips its descendants — a failure
mode that looks exactly like a refusal). Before the generator runs, this asks
the environment for its protection rules and refuses to proceed when the run
needed human approval but the environment cannot have provided it.

Approval is needed unless the author is trusted (write-or-above, resolved by
author_trust.py, never author_association) AND the PR is not a draft. When it
is needed, the environment must carry a required_reviewers rule with at least
one reviewer. Everything else refuses: a 404 (the environment GitHub just
implicitly created — it has no rules by documented behaviour), an HTTP error,
an unparseable body, an empty reviewer list. A false refusal costs the
consumer one setup step, named in the error; a false pass is the silent
fail-open this module exists to close.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, GATE_ENVIRONMENT,
AUTHOR_TRUSTED ("true" from author_trust, anything else is untrusted),
PR_DRAFT ("true"/"false").
"""

from __future__ import annotations

import os
import sys

from author_trust import TRUSTED_PERMISSIONS, is_trusted
from github_api import api_json, paginate

# A team's permission on the repository, as the repo-teams endpoint spells it.
# `permission` there is git-verb vocabulary (pull/triage/push/maintain/admin),
# NOT the collaborator API's quartet that author_trust reads, so the two cannot
# share one set. Both trusted values mean write-or-above, and a custom role is
# reported as whatever base it inherits.
TRUSTED_TEAM_PERMISSIONS = frozenset({"push", "maintain", "admin"})


def team_permission(repo: str, slug: str) -> str:
    """The team's permission on *repo*, or ``"none"``.

    Read from the repository's own team list rather than
    /orgs/{org}/teams/{slug}/repos/{repo}: the latter needs org scope the job
    token does not reliably hold, and a 404 there is indistinguishable from "no
    access". A team the repository does not list has no access, which is the same
    answer by a route the job can take. Never raises, for author_permission's
    reason.
    """
    try:
        for page in paginate(f"/repos/{repo}/teams?"):
            for team in page:
                if isinstance(team, dict) and team.get("slug") == slug:
                    permission = team.get("permission")
                    return permission if isinstance(permission, str) else "none"
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY failure mode
        print(f"could not resolve teams for {repo!r}: {exc}", file=sys.stderr)
    return "none"


def reviewer_is_trusted(repo: str, reviewer: dict) -> bool:
    """True only if this eligible reviewer holds write-or-above on *repo*.

    A User goes through author_trust.is_trusted, so "trusted" means one thing in
    this harness and is resolved from the permission API rather than from an
    association. A Team is resolved from the repository's team list. Any other
    reviewer type — or one this code cannot read a name out of — is untrusted:
    "I do not know what this is" must not read as trusted.
    """
    if not isinstance(reviewer, dict):
        return False
    who = reviewer.get("reviewer")
    if not isinstance(who, dict):
        return False
    match reviewer.get("type"):
        case "User":
            login = who.get("login")
            return isinstance(login, str) and is_trusted(repo, login)
        case "Team":
            slug = who.get("slug")
            return isinstance(slug, str) and team_permission(repo, slug) in TRUSTED_TEAM_PERMISSIONS
        case unknown:
            print(f"reviewer type {unknown!r} cannot be resolved to a permission; untrusted", file=sys.stderr)
            return False


def has_required_reviewers(repo: str, environment: str) -> bool:
    """True only if *environment* provably carries at least one required
    reviewer AND every eligible reviewer holds write-or-above.

    The second half is what makes an approval mean something. GitHub permits a
    READ-ONLY collaborator as an environment reviewer, and a deployment reviewer
    may approve their own run — so a rule naming one lets that account open a
    pull request, approve its own deployment, and reach the credential without
    ever holding write. A rule of the right type with the wrong people on it is
    the same fail-open as a rule with nobody on it, reached one step later.

    EVERY reviewer, not any: approval takes one of them, so the weakest decides.
    A rule listing four maintainers and one drive-by contributor gates nothing.
    That also settles `prevent_self_review`, which the report pairs with this —
    once every eligible reviewer could push the change directly, their approving
    their own run is not an escalation, so it needs no separate assertion.

    Never raises: every failure collapses to False, because the caller's only
    safe reading of "could not establish" is "does not gate" — the implicit
    creation this defends against also answers 404.
    """
    try:
        response = api_json(f"/repos/{repo}/environments/{environment}")
    except Exception as exc:
        print(f"could not resolve environment {environment!r}: {exc}", file=sys.stderr)
        return False
    if not isinstance(response, dict):
        return False
    rules = response.get("protection_rules") or []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
            continue
        reviewers = rule.get("reviewers") or []
        if not reviewers:
            continue
        if untrusted := [r for r in reviewers if not reviewer_is_trusted(repo, r)]:
            print(
                f"environment {environment!r} lists {len(untrusted)} of {len(reviewers)} required "
                "reviewer(s) without write-or-above; any one of them can approve, so the gate "
                f"admits an untrusted approver ({TRUSTED_PERMISSIONS} or a team with "
                f"{sorted(TRUSTED_TEAM_PERMISSIONS)} is required)",
                file=sys.stderr,
            )
            return False
        return True
    return False


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    environment = os.environ["GATE_ENVIRONMENT"]
    trusted = os.environ.get("AUTHOR_TRUSTED") == "true"
    draft = os.environ.get("PR_DRAFT") != "false"  # unknown reads as draft: fail closed

    if trusted and not draft:
        print("gate not required: author holds write-or-above and the PR is not a draft")
        return 0

    if has_required_reviewers(repo, environment):
        print(f"environment {environment!r} has required reviewers; gate is real")
        return 0

    print(
        f"::error::environment {environment!r} in {repo} has no required reviewers, "
        "so the approval this run claims to have received gated nothing. Create the "
        "environment and add at least one required reviewer, then re-run. "
        "(GitHub silently creates referenced environments WITHOUT protection rules; "
        "see aceiro ADR-0006.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
