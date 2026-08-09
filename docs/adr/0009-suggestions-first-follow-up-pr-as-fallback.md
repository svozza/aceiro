# Remediation delivers suggestion blocks first, a follow-up PR as fallback

ADR-0001 and ADR-0007 settle remediation's effect as a branch plus a follow-up
pull request. Reviewing that choice against the common case exposed a mismatch:
the thing being fixed is a pull request that is not merged yet, and a separate
pull request is a heavy vehicle for "change two lines of the thing you are
already looking at". The obvious alternative — the bot pushes the fix to the
pull request's own branch — was rejected for reasons worth recording, because
they are what the chosen middle ground has to preserve:

- **It often cannot work.** Fork pull requests need "allow edits by
  maintainers" granted, and the base repository's token cannot push to a fork
  regardless. The most valuable remediation target (ADR-0007: a maintainer
  commanding a fix on a first-time contributor's pull request) is exactly the
  case where the push fails or needs a broader credential.
- **It causes the drift the rest of the system refuses on.** Every guard —
  patch anchoring, the head-SHA TOCTOU checks, the review comment's reviewed-SHA
  stamp — treats movement of the pull request's head as reason to withdraw. A
  bot push moves the head, retriggers the reviewer on the bot's own commit, and
  sets up the reviewer conversing with itself.
- **It collapses the human gate.** ADR-0005 admits patch content is
  unverifiable and leans entirely on "the pull request is the gate": model
  output stays inert until a human merges it. Pushed into the contributor's
  branch, the unverified patch becomes part of an existing merge candidate,
  interleaved with commits the contributor can amend afterwards.
- **It overwrites someone else's authorship.** The branch belongs to the
  contributor; a maintainer's `/fix` should not rewrite it over their head.

GitHub suggestion blocks sit between the two: a review comment whose body
carries a ```suggestion fence, anchored to lines of the diff. The CONTRIBUTOR
applies it with one click; the commit lands on their branch through their
action, co-authored and consented. This fixes the actual pull request — the
user-experience gap that prompted this review — while preserving every
property above:

- No new credential. The executor already holds `pull-requests: write`, which
  is sufficient to post review comments. `contents: write` is not needed for a
  suggestion, so the write scope SHRINKS for the common case.
- The human gate strengthens: the applying human is the pull request's author,
  not a maintainer merging a separate branch. Model output stays inert until a
  human — now the most-informed human — acts.
- Anchoring maps exactly. A suggestion is positioned on diff lines, which is
  ADR-0005's anchoring property enforced by GitHub's own mechanics; the
  verifier's byte-match of `old` against the reviewed SHA carries over
  unchanged, and drift refusal stays as-is (a moved head invalidates the
  suggestion's position, and GitHub marks it outdated — fail-visible).
- The frame condition is free. Suggestions can only attach to lines in the
  diff, which is a strict subset of ADR-0005's `changed_files` frame.

## Decision

- The plan vocabulary gains a `suggest` step kind: non-write-class, args
  `{path, line, old, new, note}`, subject to the same anchoring, bounding and
  denylist checks as `patch`. Added to `policy.json` from the start so the
  Python verifier and the TypeScript prover never disagree about the universe
  of step kinds.
- Suggestions are the default delivery. A remediation whose every patch fits
  suggestion constraints (single hunk per file, inside the diff, within
  bounds) is delivered as suggestion comments; the follow-up pull request is
  the fallback for what suggestions cannot express — still bounded by
  ADR-0005's caps and still within `changed_files`.
- The boundary is STRUCTURAL, not a size judgment, and atomicity is the
  reason. **The atomicity rule below is a property of ONE PLAN — corrected in
  the fourth addendum, which records that two commands can still deliver one
  defect in independently applicable pieces, and why that is accepted.**
  Suggestions are independently applicable: each is its own
  one-click commit, appliable in any subset and any order. A single
  finding whose fix spans multiple files (rename plus its call sites, a
  signature change plus its callers) is only correct as a whole — delivered
  as per-file suggestions it can be HALF-applied, leaving the branch broken
  in a way neither the reviewer nor the contributor intended. So a
  coordinated fix must never be delivered as independently applicable
  pieces, even where each piece would be mechanically postable. One path,
  one hunk → suggestion; a fix whose patch steps span paths (or multiple
  coordinated hunks in one file) → the stacked follow-up pull request,
  whose merge is atomic. Both sides of the rule are checkable from the
  verified plan's step list, which is what keeps the delivery decision the
  executor's.
- The choice between the two is the EXECUTOR's, made from checkable
  properties of the verified plan, never the model's. A model-selected
  delivery mode would be a model-selected policy, v2 §2.1's banned move —
  the same reasoning that keeps the schema version out of the artifact
  (ADR-0004).
- ADR-0001 is unchanged: no path runs tests. ADR-0007 is unchanged: commanded
  per finding by a trusted commander, deduplicated per (pr, head_sha,
  finding); a suggestion thread and a follow-up pull request are alternative
  effects behind the same command.

## Consequences

- The prover's ordering policy gets a second terminal shape. Today the only
  legal write chain is patch → push_branch → open_pr; a suggestion-only plan
  has NO write-class steps at all, which vacuously satisfies ordering. The
  CATCHES cases must grow a mutant proving a plan mixing `suggest` with
  `push_branch` still orders the push after every patch — vacuous-pass is
  this policy's known failure mode.
- One suggestion per file per finding, matching GitHub's one-hunk-per-
  suggestion mechanics. The bounding caps apply per suggestion, so policy
  needs no new numbers.
- A suggestion the contributor never applies is a silent no-op. That is
  accepted: the commander saw the suggestion posted (the command's visible
  effect), and nagging is a human's job, not the executor's.
- The rendered suggestion comment carries the same "generated by an AI model,
  counts toward no approval" notice and policy hash as the review comment and
  the follow-up pull-request body (ADR-0005's visibility requirement).
- Suggestion comments use the REST review-comment API with `side`/`line`
  addressing, which is another consumer of prepare_context's SHA-anchored
  diff — the line the suggestion lands on must be computed from the same diff
  the verifier checked, or the anchor and the placement can disagree.
