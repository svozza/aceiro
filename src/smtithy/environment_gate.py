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

from github_api import api_json


def has_required_reviewers(repo: str, environment: str) -> bool:
    """True only if *environment* provably carries at least one required
    reviewer. Never raises: every failure collapses to False here, because
    the caller's only safe reading of "could not establish" is "does not
    gate" — the implicit creation this defends against also answers 404."""
    try:
        response = api_json(f"/repos/{repo}/environments/{environment}")
    except Exception as exc:
        print(f"could not resolve environment {environment!r}: {exc}", file=sys.stderr)
        return False
    if not isinstance(response, dict):
        return False
    rules = response.get("protection_rules") or []
    return any(
        isinstance(rule, dict)
        and rule.get("type") == "required_reviewers"
        and (rule.get("reviewers") or [])
        for rule in rules
    )


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
        "see smtithy ADR-0006.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
