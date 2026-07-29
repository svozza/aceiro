# Remediation is commanded per finding, by a trusted commander

§20 has remediation happen "on a maintainer's command", which means an
`issue_comment` trigger. That event carries a different trust question and less
context than the review path, so three things are settled here.

**Trust follows the commander, not the author.** `author_trust.py` resolves the
pull-request author's permission; for a command the question is whether the
person who typed it holds write-or-above. `is_trusted(repo, login)` is already
pure and takes a login, so it is reused with a different argument. The author's
permission is deliberately *not* consulted: it tells you nothing about whether a
patch should be attempted, and ADR-0005 establishes that patch content is
unverified regardless of who authored the pull request. A maintainer commanding a
fix on a first-time contributor's pull request is the most valuable case, and
requiring author trust as well would forbid exactly that.

**The command names one finding.** `/fix <n>` rather than "fix everything
trivial". Per-finding scoping lets a maintainer accept one suggestion without the
others, and it makes the deduplication key natural.

**Drift means refuse.** `issue_comment` carries an issue number and no SHAs, so
the pull request must be fetched to resolve them — at which point the head SHA is
whatever it is now, not what the commander was looking at. The remediator
proceeds only if the head SHA still equals the SHA of the review whose finding is
being fixed, extending `post.py`'s existing TOCTOU guard. This window does not
exist in the review path, where `pull_request_target` freezes the base SHA.

## Consequences

- A command is not idempotent the way a push is. Two maintainers typing `/fix 3`,
  or one typing it twice, must not produce two branches and two pull requests.
  The remediator holds a deduplication key on `(pr, head_sha, finding)` and
  refuses when a follow-up pull request for that tuple already exists — the
  equivalent of the reviewer's marker-keyed sticky comment. This is not in §20
  and is the kind of gap that only appears in production.
- The effect is always a follow-up pull request, never a push to the pull
  request's own branch. ADR-0001 already committed to this, and it also avoids a
  mechanical problem: pushing to a fork's head branch requires maintainer-edits
  to have been granted, and often it has not.
- `issue_comment` runs the default branch's workflow and always carries the write
  token, the same trap as `pull_request_target`. The gate assertion from ADR-0006
  applies here too.
