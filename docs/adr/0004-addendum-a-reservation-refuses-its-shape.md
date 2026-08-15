# Addendum to ADR-0004: a reservation refuses its shape, in code

*Note (2026-08-15): the TypeScript gate discussed below is retired
([ADR-0016](0016-retire-the-typescript-prover-consolidate-on-python.md)); the
refusals this addendum required live on in `plan_verify.check_reserved_closures`.*

ADR-0004 names three closures and says what each is for: "Three closures, each
of which reserves a shape and refuses it today." Two of them did refuse.
`control_flow` did not, in either gate.

Measured, on the shipped policy with one field widened to `["branch"]` and a
matching `step_kinds` entry:

- `checkPlanPolicy` accepted the policy, and `checkPlanSchema` then accepted a
  `branch` step as an ordinary typed record.
- `check_plan_schema` accepted the same plan. The Python gate read
  `control_flow` **nowhere at all** — not to enforce it, not to refuse it.

What that costs is precise, and it is not "a step kind nobody implemented". Both
gates reason about a straight-line plan *throughout*, and the reasoning is what
the branch would invalidate:

- `proveOrdering` pins each step's position variable to `eq(index)`, so its
  ∀-claim is about **this** sequence.
- `proveFrame` quantifies over a closed file set derived from those same fixed
  positions.
- `check_plan_containment` simulates steps applying **in sequence**, and the
  exactly-once anchor guarantee is a property of that simulation.

A `branch` step admitted into any of those is proved about as a sequential step.
Every policy would still report `holds`, and the branches nobody modelled would
be exactly the part no policy covered — a vacuous pass, which the prover's own
corpus calls its named failure mode.

**The decision, which is ADR-0004's own and not a new one:** a policy declaring
control flow this gate does not implement is refused, before any step is read.
`argument_forms` already worked this way on the TypeScript side ("this prover
only implements `["literal"]`"), so this makes the two closures symmetric rather
than introducing a rule.

Two details worth keeping:

- **It is a policy fault, not a plan rejection.** The prover raises
  `PolicyError`, which it has precisely so a bad deployment is never reported as
  "the model produced something invalid". Python has one failure channel, so it
  raises `Rejection` with `policy error:` leading the message, and a test pins
  that a widened policy carrying an invalid plan reports the *policy*.
- **Adding `branch` later is still an entry in `step_kinds` plus a
  `control_flow` entry**, as ADR-0004 says. What changes is that the entry now
  also requires teaching both gates the semantics — which was always the
  intent, and is now enforced rather than assumed. The refusal is the thing that
  makes "not a shape change" true instead of merely cheap.

## How it was found, which is the reusable part

Not by reading `control_flow`'s call sites — by a **policy-coverage assertion**:
enumerate the keys under `policy.plan` and fail if either gate has no reader for
one. It found `control_flow: no reader in plan_verify.py` on its first run.

That assertion is the guard the code review's "two-language seam" section names
as the cheapest one catching the whole class, and it is now in
`tests/test_plan_gate_differential.py`. It would also have caught `plan.ordering`
having no Python reader and `max_patched_files`/`max_changed_lines` having no
TypeScript one — both real, both previously invisible in a green suite. A key
with no entry fails, so adding one to `policy.json` forces the question "which
gates read this?" to be answered in review rather than discovered later.
