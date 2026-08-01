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

## Prior art: the staging repo BUILT this, on `staging/inline-comments-test`

Two generations of prior art in the extraction source, and they point in
opposite directions. The agentcore reviewer refused inline comments outright
(its post.py: "inline comments cannot be edited as a set, so keeping them
deduplicated across pushes needs a GraphQL minimize-previous pass that the
summary upsert does not") — that refusal is why every reviewer posts one
sticky comment. But the incumbent reviewer then built the full solution on
the `staging/inline-comments-test` branch (~620-line post.py reconciler,
`diff_map.anchor_signatures`, 1.5k lines of tests; tip 70bebcd4), and its
commit history is a catalogue of live-measured failures that any fresh
design here would re-run. A suggestion IS an inline comment, so the
suggestion executor ports that reconciler rather than re-deriving it. Its
load-bearing lessons, each verified on a real PR there:

- **Identity comes from the anchored CODE, never the model's prose and never
  the line number.** The branch's first key hashed title+body: measured
  live, the model reworded every finding on every run over a byte-identical
  diff, so the key never matched twice and every run deleted and reposted
  everything. `(path, line)` is also out: GitHub re-anchors live comments
  when the diff shifts (verified: line 2→4 across a push, comment stayed
  live). The surviving key is `finding_fingerprint`: path + a whitespace-
  normalized, NFC'd signature of the anchored line and its neighbours
  (`anchor_signatures`, window=1 so two identical `return True` lines stay
  distinct). For a suggestion this maps directly: `old` IS the anchored
  code, so the fingerprint is the anchor signature, not a hash of prose.
  Severity and wording deliberately excluded — a re-graded finding keeps its
  comment and its thread.
  **The window's source is part of the contract.** `anchor_signatures` takes
  its window from lines the DIFF makes visible, so a neighbour outside every
  hunk reads as `absent` rather than as its real text. An unrelated push that
  grows a hunk around an unchanged line therefore changes that line's
  signature — the executor sees an unknown fingerprint plus an orphaned old
  one, and deletes a live thread to repost the same comment. No function of
  the diff alone can close this: in the narrow-hunk run the neighbour's text
  is not in the input, so clamping or dropping `absent` cannot recover it. The
  port must take the window from **file content at the head SHA** — which it
  already reads, for anchoring — and keep the diff-derived signatures for
  choosing WHICH lines are anchorable. Until then the identity key is
  hunk-boundary-sensitive, which is a churn bug and not a containment one.
- **Group by anchor; never an ordinal suffix.** A `#2` suffix is "the 2nd
  finding on this anchor", stable only while the set is: observed on PR
  #514, one finding split into two produced a matched comment plus a
  "new" one about the same defect. Findings sharing an anchor merge into ONE
  comment — and never key a dict by fingerprint alone, which silently drops
  all but one (the original defect: sticky comment counted more findings
  than the PR showed).
- **Retraction is reply-aware, and never "resolves".** A finding absent from
  the current artifact: DELETE its comment if no human replied; if a human
  replied, PATCH — plain-text notice ABOVE the struck-through body (below,
  an unclosed model fence captures the notice), fences left unstruck, the
  struck marker joining the fingerprint on the first line, which is the only
  line model text cannot reach (markers are read from line 1 only, because a
  fenced code block may legally CONTAIN the marker text). "Resolve" asserts
  the defect was addressed, which the harness cannot know.
- **Post before retracting.** The review POST is atomic (a 422 creates
  nothing), so new comments go up before stale ones come down — failing
  mid-run must leave the existing comments standing, never already-deleted.
- **Review wrappers accumulate and must be superseded.** Every run that
  posts inline comments must CREATE a review (no upsert exists for
  creation; submitted reviews cannot be deleted). Spent wrappers get their
  body rewritten to "superseded" and are minimized via GraphQL — best
  effort, never run-failing (observed on PR #512: 9 wrappers, 3 still
  pointing at any live comment).
- **Compare content, not bodies.** The per-run `[run]` URL in the footer
  differs every run; comparing whole bodies rewrites every comment every
  time. Strip the executor-authored first and last lines by POSITION and
  compare what remains.

What transfers unchanged from the earlier sketch: marker AND authenticated
author (the runtime-resolved bot login) gate every PATCH and DELETE;
anchoring at the current head is the freshness check and self-extinguishes
once a contributor applies the suggestion (`old` stops matching);
`suggest.line` gets the same in-hunk provenance check as `finding.line`, in
the verifier, not as a GitHub 422 in the executor. ADR-0007's
`(pr, head_sha, finding)` key remains correct for the stacked follow-up PR,
whose premise dies with the head — it is only wrong for suggestions, where
the head churns exactly when the suggestion does not.

The port is not yet scheduled; it lands with the remediation executor. The
branch is the reference implementation, and its test suite (test_post.py's
reconciler half, test_post_properties.py, test_diff_map.py) is the
port's acceptance bar.

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
same-repo-only fallback for what suggestions structurally cannot deliver —
above all the multi-file fix (see the atomicity rule in the main ADR: a
coordinated fix must never ship as independently applicable pieces, and a
PR's merge is atomic where per-file suggestions are not). A multi-file fix
on a FORK pull request therefore has no automated delivery; the finding and
its suggested direction are stated in the review, and a human does the rest.
That limitation is accepted and this addendum is where it is recorded.
