# Evals run on every pull request, once by default

Revised 2026-07-31; the original decision and its reversal are both recorded
because the premise, not the reasoning, is what changed.

**As first written:** in the staging repo the eval suite is path-filtered, so
it runs rarely. In `smtithy` every pull request touches the harness, so the
same trigger would run 11 scenarios against a real model on every push. Evals
therefore ran on a label or manual dispatch, plus a scheduled run on the
default branch — not on every pull request.

**The revision:** that cadence treated per-PR model cost as prohibitive
without measuring it. Measured (2026-07-31, both suites, `--runs 3`, live
Bedrock), a full pass is on the order of a dollar — and the observation that
every PR here is a harness change cuts the other way: the evals are the only
test of the behaviour the harness exists to produce, so the PRs most in need
of them were the ones the label had to remember to opt in. Evals now run on
every pull request (opened/synchronize/reopened), with the label kept as a
manual re-trigger for an unmoved head and the weekly schedule kept for
upstream model drift, which no PR of ours would trigger. The human gate is
orthogonal to cadence and unchanged: untrusted and draft authors wait at the
environment's required reviewer before their code runs with the credential,
whatever the trigger. The deterministic suite stays on every pull request in
quality_check.yml, where it is free, fast and blocking.

`--runs 1` is the default. `run_evals.py` accumulates `total_failed` across runs
and exits non-zero if any scenario failed on any run, so `--runs 3` is three
independent chances to catch a flake rather than majority voting. That makes the
two settings answer different questions: `--runs 1` asks whether the harness
works, `--runs 3` asks whether a behaviour is stable. Use 3 before merging a
prompt, policy or verifier change; 1 otherwise.

## Consequences

- A single green run is evidence the harness works, not that a behaviour is
  stable. The suite exists to grade behaviour rather than shape (§15), so
  treating a `--runs 1` pass as a stability claim is the misreading to guard
  against.
- Expect intermittent failures in the scenarios that grade judgement —
  `caller_impact_needs_investigation` and `provenance_boundary_adjacent_bug` —
  rather than the injection scenarios, which either fence correctly or do not.
  When one flakes, the fix is to remove model arithmetic from the scenario, not
  to widen the assertion.
- `run_evals.py` reports upstream `api_errors` and time spent backing off even
  on a clean pass, so throttling pressure is visible at `--runs 1` before it
  costs a review.
