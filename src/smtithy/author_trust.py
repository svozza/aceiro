"""Resolve a PR author's REAL repository permission — the gate's trust decision.

`author_association` is not a permission check. It reports the actor's social
relationship to the repo, not their access:

- A read-only outside collaborator reports `COLLABORATOR` — the same value a
  maintainer with write access reports on a PRIVATE repo, where org membership
  is invisible so `MEMBER` never appears. Confirmed against real accounts with
  `push: false` that still read as `COLLABORATOR`, so a skip list keyed on
  association hands the trusted path to accounts that cannot push.
- `MEMBER` only appears for PUBLICLY visible org membership, so going public
  changes which value the same person reports — a skip list keyed on
  association silently changes meaning at exactly the moment it starts
  mattering.

This asks the permission API instead. It FAILS CLOSED: any HTTP error, any
unrecognized role, any missing author resolves to untrusted, which routes the
PR through the human-approval environment rather than around it. A false
negative costs one maintainer click; a false positive lets an unreviewed fork
PR execute with a live credential in scope.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, PR_AUTHOR, GITHUB_OUTPUT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

from github_api import api_json

# `permission` collapses GitHub's roles onto the legacy quartet
# admin/write/read/none: `maintain` reports as `write`, and a custom role
# reports as whatever base it inherits. Both trusted values mean
# write-or-above. `role_name` is deliberately NOT consulted: it can carry
# org-defined custom role names this code has never heard of, and guessing at
# an unknown name's power is exactly the mistake author_association made.
TRUSTED_PERMISSIONS = frozenset({"admin", "write"})


def author_permission(repo: str, author: str) -> str:
    """The author's permission on *repo*, or ``"none"`` if it cannot be
    established. Never raises: the caller's only safe reading of a failure is
    "not trusted", so failures are collapsed into the least-privileged answer
    here rather than left for each call site to remember."""
    if not author:
        return "none"
    try:
        response = cast("dict", api_json(f"/repos/{repo}/collaborators/{author}/permission"))
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY failure mode
        print(f"::warning::could not resolve permission for {author!r} ({exc}); treating as untrusted", file=sys.stderr)
        return "none"
    permission = response.get("permission")
    return permission if isinstance(permission, str) else "none"


def is_trusted(repo: str, author: str) -> bool:
    """True only if *author* holds write-or-above on *repo*."""
    return author_permission(repo, author) in TRUSTED_PERMISSIONS


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    author = os.environ.get("PR_AUTHOR", "")
    # Branch to a LITERAL rather than formatting the resolved value into the
    # log line and the output file. The permission response is not a secret,
    # but CodeQL's clear-text-logging/storage rules flag any value that flows
    # from an authenticated API response into a log or a file, and a
    # security-critical file is the wrong place to carry standing alerts that
    # future readers have to re-adjudicate. Emitting a constant also removes
    # any chance of leaking response detail into the step log.
    answer = "true" if is_trusted(repo, author) else "false"
    print(f"author={author or '(none)'} trusted={answer}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"trusted={answer}\n")


if __name__ == "__main__":
    main()
