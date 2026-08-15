# Retire the TypeScript prover; the harness is all Python

Supersedes [ADR-0003](0003-plan-prover-in-typescript-via-z3-wasm.md).

ADR-0003 put the plan prover in TypeScript because TypeScript was the
destination: the artifact verifier was to be ported once stable, the two
runners were to coexist for the duration of the port, and the Python gate was
the differential oracle for it. That direction is reversed — everything is
Python — so the prover was a detour rather than a beachhead, and ADR-0003's
discarded premise becomes the argument: Z3's Python bindings are the mature
ones, and a harness that stays Python never needed the spike that made
TypeScript viable.

## Why this is a deletion, not a rewrite

Every live property the prover proved already had a Python implementation in
the same call, `plan_verify.verify_plan`: ordering (`check_plan_ordering`,
written as the prover's twin with identical semantics), frame and denylist and
bounds (`check_plan_containment`), write-class targets
(`check_write_class_targets`), cardinality (`check_plan_cardinality`), and
schema (`check_plan_schema`). Python additionally checks markdown and secrets,
which the prover never did — the Python gate was a strict superset.

That was not inferred: the differential oracle
(`tests/test_plan_gate_differential.py`) fed one plan to both gates and
compared verdicts across 97 collected tests, all green on 2026-08-15 at
`f6e076e` (178 TS / 2096 pytest / 97 differential). `proveTaint` is the one
check with no Python twin, and none is needed — it is vacuous on the shipped
policy (no `read_pr_file` kind, `argument_forms` is `["literal"]`), and
findings block F2e records that it is vacuous rather than sealed: declaring a
source kind `write_class: true` makes it fire with no bindings at all. It was
never working security to lose.

## What is deliberately given up

1. **A second, independently authored implementation of five properties.**
   Cross-language N-version redundancy was the strongest argument for keeping
   the prover, and it collapses with the language boundary: same-language
   twins would share idioms, libraries and the author's blind spots, so
   rebuilding the redundancy in Python buys little of what it cost.
2. **The in-place SMT option.** The one candidate future property is staged
   grounding (ADR-0004's `generate → verify symbolically → resolve → re-verify
   → execute`, still "Not built now"). Two honest caveats stand: symbolic
   verification does not require SMT (abstract interpretation, bounded
   enumeration, SAT and constraint propagation are all options), so staged
   grounding is the strongest *available* argument for a solver, not a proof
   one is necessary.

## Where the encoding lives

The Z3 encoding is recoverable from history, not carried in the tree: the
last commit containing `ts/plan/prove.ts` is **`825c72b`**. Anyone reviving
it should carry `proveTaint`'s test corpus with it, including the synthetic
`bindings` cases and the write-class-source case above.

## Consequences

- One language, one test runner, no Node step in any workflow. The delivery
  jobs no longer build a prover before delivering, and the three-way
  subprocess exit contract (0 proved / 1 disproved / 2 nothing proved) is
  gone with the subprocess — it guarded a hazard ("a crashed prover must not
  read as a disproof") that cannot occur in-process.
- `policy.json` has one reader. The differential's defect class — a policy
  key one gate enforced and the other ignored — becomes "a key no check
  reads", and is guarded in-process: `check_plan_policy_keys` bounds the key
  set, and `TestEveryPlanPolicyKeyHasAReader` (test_plan_verify.py) requires
  a consuming read for every shipped key, scanning with the allowlist excised
  so the allowlist cannot witness its own keys.
- ADR-0003's port plan ("The port, when it happens") is void, not deferred.
- `docs/findings/0002-prover-attack-suite.py` drove the deleted CLI and is
  historical as of `825c72b`; findings block F (F2a–F2e) records the analysis
  this decision rests on, including where an earlier version of it was wrong.
- The red-team testbed (`svozza/smtithy-redteam`) is kept so the adversarial
  matrix can be re-run against the all-Python harness.
