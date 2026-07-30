# Addendum to ADR-0009: the follow-up PR is stacked, and the push question

Two questions asked of ADR-0009 in review, both natural enough that they will
be asked again. Recorded with their answers, one of which exposed a gap the
ADR had left unstated.

## "Why not let the bot push to the branch when it isn't a fork?"

For a same-repo pull request, two of the four rejection reasons genuinely
weaken: there is no maintainer-edits checkbox to need, and the executor's
follow-up-PR path already holds `contents: write`, so pushing to the PR's
branch needs no credential it wasn't going to hold — only the push target
changes. Maintainers pushing to each other's same-repo branches is also
ordinary collaboration, which softens the authorship objection.

What settles it is a GitHub fact the original ADR did not use: **on a
same-repo pull request, anyone with write access can apply a suggestion
block** — applying is not restricted to the author. So in the exact scenario
proposed (a maintainer commands `/fix` on a maintainer's branch), the
commander can click "Apply suggestion" the moment it is posted. The commit
lands on the branch through a deliberate human action, with the diff visible
at the moment of decision. A direct bot push buys the removal of that one
click and nothing else — and the click is load-bearing: under ADR-0005 the
patch content is unverified by construction, so that click is the last point
where a human looks at the actual bytes before they join a merge candidate.
Paying for its removal with drift machinery on the security boundary (the
pipeline would have to recognise and exempt its own pushes, a trust judgment
the path currently doesn't make) is a bad trade.

The case where push-to-branch would buy more — a fix too large for
suggestion blocks — is exactly the case where a human should be reading the
diff before it lands, which is what the follow-up pull request provides.

**Revisit trigger:** sustained real usage showing maintainers applying large
suggestion batches by hand and asking for automation. That evidence would
justify designing a policy-gated, same-repo-only, authenticated-pusher-aware
push mode. Until then, no.

## "A follow-up PR against main means knowingly merging broken code" — the gap

The objection is correct against the shape it assumes: if the follow-up pull
request targeted the default branch, the flow would be "merge the bug, then
merge the fix", with a window where the default branch is knowingly broken.
That shape was never intended, but ADR-0009 and ADR-0001 never said so, and
`open_pr`'s args (`branch`, `title`, `body`) name no base — an executor
"defaulting to main" would have implemented the absurd reading.

The intended shape falls out of ADR-0005's anchoring rule: each patch's
`old` must byte-match the file **at the reviewed SHA** — the pull request's
head, not the default branch. A patch anchored there only applies cleanly on
top of the reviewed head, so:

- **The fix branch is cut from the reviewed head SHA.**
- **The follow-up pull request's base is the reviewed pull request's own
  head branch** — a stacked pull request. The author (or any maintainer)
  merges the fix INTO the open pull request; the original pull request
  updates; review continues; one complete pull request merges to the default
  branch. Broken code never lands anywhere.
- Merging the stacked fix moves the original pull request's head, which
  re-triggers review of the now-complete branch. That is the ordinary
  drift path (a human merged something), not the self-caused drift the push
  option was rejected for.

Pinned as policy, not convention:

- **The base is never model-suppliable.** `open_pr` deliberately has no
  `base` argument, and a shipped-policy test on each side of the boundary
  (prover and verifier) asserts its argument set exactly, so a `base` arg
  appearing in the policy is a failing test and a named decision, not a
  quiet addition. A model-chosen base is a model-chosen merge target — the
  same banned move as a model-selected policy version (ADR-0004).
- The executor sets the base from the pull-request context it already holds
  (the same context whose head SHA anchored the patches), and refuses if the
  reviewed head has moved — the TOCTOU posture post.py already takes.

## The fork asymmetry, and what it does to ADR-0009's ordering

A stacked pull request is impossible for fork pull requests: GitHub requires
a pull request's base branch to exist in the base repository, and a fork
PR's head branch does not. So for the first-time-contributor case — the one
ADR-0007 calls most valuable — the follow-up pull request cannot target
their branch at all.

Consequence: **suggestion blocks are the only remediation delivery that
works across both repository topologies.** The contributor applies the
suggestion into their own fork branch with one click; no cross-repo
credential, no stacked base. This strengthens ADR-0009's decision beyond its
original argument: suggestions-first is not merely the better default, it is
the only universal mechanism, and the stacked follow-up pull request is a
same-repo-only fallback for fixes too large to express as suggestions. A
large fix on a FORK pull request has no automated delivery; the finding and
its suggested direction are stated in the review, and a human does the rest.
That limitation is accepted and this addendum is where it is recorded.
