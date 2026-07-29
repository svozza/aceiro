# The remediation executor never runs tests

§20 of the design doc specifies an ordering policy for plans: no write-class
step is reachable on any path unless a `run_tests` step with `passed == true`
dominates it. Implemented literally, that puts execution of untrusted code —
the PR author's diff, the model's patch, and whatever the test suite imports —
inside the same job that holds `contents: write` and `pull-requests: write`.
That job would both execute untrusted code and hold a write scope, which no job
in the review pipeline does today, and it bypasses the verifier entirely: a
malicious `conftest.py` does not need to smuggle anything past a markdown
allowlist.

We drop the dominance policy instead of sandboxing around it. The executor
pushes a branch and opens a follow-up PR without running tests, and the
consuming repo's existing required checks judge the result. A branch and a PR
are reversible, non-executable and human-gated — the same properties that made
the comment-upsert executor safe enough to skip a solver in §2.5 — so gating
them on a test run buys little and costs the credential split that the whole
trust architecture rests on.

## Consequences

- Two of §20's three plan-level policies remain: taint and frame conditions.
  Both are still ∀-shaped, so the §2.5 threshold for an SMT backend is still
  cleared; only the dominance obligation is gone.
- `passed` never becomes a cross-job claim, so no attestation format is needed
  to bind a test result to what was tested.
- If dominance turns out to be load-bearing, retrofitting it means rebuilding
  the credential split. That is the hard-to-reverse part of this decision.
- A test signal is still available without a privileged runner: react to the
  consuming repo's CI result on the follow-up PR after the fact.
