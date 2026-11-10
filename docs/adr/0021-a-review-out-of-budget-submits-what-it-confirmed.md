# A review out of budget submits what it confirmed

Measured 2026-11-10 on 24 public pull requests of 1,500 to 14,000 changed lines
(evidence: `docs/findings/0005-large-pr-delivery-and-coverage.md`).

A review that reached its wall clock or its turn ceiling without an accepted
submission delivered nothing, however much it had established. Three facts made
that worse than it looked:

1. **Delivery swings with the provider.** The same eight pull requests that
   lost 6 of 16 reviews to a 450-second clock in October lost none in November:
   the median later-turn latency had fallen from 14.9s to 3.9s.
2. **On a fast provider the turn ceiling binds first**, which ADR-0015 set out
   to prevent. The ceiling was derived from one August run (21 calls in 618s).
   At November's rate it ended reviews at about half their clock: every one of
   the 3 failures in 48 sessions at 450s, and 7 of 16 at 260s, was the turn
   ceiling, none the clock.
3. **The advisory notices cannot reach the failures that matter.** They ride on
   source-tool results, so a session inside one long reasoning turn never sees
   one, and none fired in 48 sessions that finished under half their budget.

## Decision

**A fixed reserve of each review attempt is held back for a forced final
submission.** When a session with no accepted artifact reaches the reserve, or
has used the investigation's turn ceiling, the loop interrupts it and sends one
harness-authored message in the same conversation: submit the defects already
confirmed, state in `residual_risk` what was not examined. Nothing else changes
hands: no evidence is replayed, no second session starts, no checkpoint tool
exists.

- The hard clock is unchanged; a finalization that does not submit in the
  reserve fails as a timeout did.
- Source reads are refused by a hook once finalization begins. The session can
  only lose capability.
- The artifact goes through the same `verify()` and the same submission breaker.
- The CLI's own turn limit sits `FINALIZE_TURNS` above the investigation's, so
  the loop ends the investigation and the CLI limit is only a backstop.
- The reserve is skipped when it would leave the investigation under
  `MIN_ATTEMPT_SECONDS`.
- The remediation generator is untouched: a partial plan is not a plan.

**Whether a review was closed early is the harness's to say.** `run_metadata.json`
carries `finalized`, and `post.py` renders a warning above the summary. A
contributor can slow a review (a large diff, a deep call graph); they must not be
able to make a partial review read as a whole one, and the model's own account in
`residual_risk` is model text.

**The turn ceiling is recalibrated** from 45 delivered reviews (636 calls in
6,974s) and the fallback raised with it, so the clock binds first on the fastest
provider measured. The ceiling bounds runaway, not cost; the clock bounds cost.

**Large lockfile hunks are not shown.** Lockfiles were 17 to 75% of the diff
bytes on 5 of the 24 pull requests. A lockfile diff over 200 lines is displayed
as one line: its size and every host named on its added lines. Matching is by
exact basename; the verifier still accepts anchors in the file and it stays
readable under the PR head root. The host list is what a reviewer could act on
in a lockfile, and it is kept.

## Measured

- At 260s, 8 pull requests twice: 9 of 16 delivered without the reserve, 16 of
  16 with it. All 18 sessions that entered finalization, across every batch,
  submitted, in 30 to 71 seconds.
- On the pull request whose diff is 65% `package-lock.json`, cost per review
  fell from $4.60 to $2.00 with no loss of source-supported findings.
- 42 of 42 review scenarios and 6 of 6 plan scenarios pass on the branch.

## Consequences

- A timeout that used to post nothing now posts a review marked as closed early.
  Consumers who alarm on review failures will see fewer of them; the notice is
  the signal instead.
- The session runs on `ClaudeSDKClient` rather than `query()`, since an
  interrupt needs a live connection. Tests fake the client, not the function.
- A lockfile-named source file over 200 diff lines is hidden from the first
  view. It is listed, named in the stub and readable; the reviewer has to
  choose to open it.
- The reserve is a constant sized at about 2.5 times the slowest finalization
  measured. A provider four times slower would need it raised.
