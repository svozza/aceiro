# The base SHA anchors the diff; the base ref detects a retarget

Both executors refused to act whenever the live PR's `base.sha` differed from the
event's `BASE_SHA`, under the heading "base changed since review". The intent is
right — a retarget changes the comparison the artifact claims to describe, so it
must invalidate the artifact just as a push does — but `base.sha` cannot express
it.

**`base.sha` on the PR object is live.** It tracks the tip of the base branch and
moves forward as that branch advances. Probed against the public API rather than
inferred: `microsoft/vscode` PRs opened in 2016, 2019 and 2020 all report the
same `base.sha`, dated 2026-01-20, and PR #7559's head is 90032 commits *behind*
that SHA. It is not a merge base frozen at creation time.

So the predicate fired on every routine merge into the base branch. On a
repository with ordinary merge traffic, a review that sat at the human-approval
gate while an unrelated PR landed would post nothing — and the only symptom is a
red post job, because there is no later event to retry against. The reviewer
silently stops working on the repositories busy enough to want it.

The comparison was also the wrong question to ask. `prepare_context` anchors the
diff to the *event's* base SHA precisely so a base-branch advance cannot change
what is under review; the ADR-0006-era comment there says so directly ("the base
branch may have advanced while the run sat at the human-approval gate"). An
executor that then refuses on exactly that advance discards the immunity the
anchoring bought.

## The decision

The two facts about the base have two separate carriers, and neither is used for
the other's job:

- **`BASE_SHA`** anchors the diff and the base checkout. Frozen at event time.
  Never compared against the live PR.
- **`BASE_REF`** is the reviewed base *branch*. The executors' TOCTOU check
  compares this. A retarget changes the ref; a branch advance does not.

The predicate is `github_api.pr_moved`, shared by `post.py` and
`execute_plan.py`, because the two executors disagreeing about what "moved"
means is a divergence with the same shape as the two plan gates'.

## Why the ref and not an ancestry check

Allowing an advance by asserting `BASE_SHA` is an ancestor of the live `base.sha`
also works, and is strictly more API calls for strictly less information: it
answers "did the base branch move in a way that includes what we reviewed",
which is a fact about the branch. What matters is whether the PR still targets
the branch the artifact was computed against, and the ref answers that in the
object already fetched.

A retarget changes ref *and* sha, so the ref comparison loses no detection. The
converse — a retarget between two branches that happen to share a tip — leaves
the comparison `BASE_SHA...HEAD` unchanged, so the artifact is still describing
the right diff.

## Consequences

- **A force-push to the base branch is not detected.** Accepted: the reviewed
  diff is anchored to `BASE_SHA`, so it still describes the comparison it always
  did. If that commit is gone from the remote entirely, the compare fetch fails
  and the executor fails closed on its own.

- **Consumers pass one more thing.** `BASE_REF` is workflow-level `env` in
  `ai-pr-review.yml`, so no consumer input changes; a consumer invoking the
  scripts directly must supply it, and its absence is a `KeyError` rather than a
  default, which is the fail-closed reading.

- **`execute_plan.py` inherits it unbuilt.** Its workflow does not exist yet, so
  the env contract is documented in its module docstring and asserted by its
  tests. The workflow that eventually runs it must supply `BASE_REF`.
