# The schema phase parses to typed steps; a type checker enforces the wiring

Extends [ADR-0004](0004-straight-line-plans-with-reserved-extension-points.md)'s
step shape and the consolidation posture of
[ADR-0016](0016-retire-the-typescript-prover-consolidate-on-python.md).

## The defect class this answers

Every phase of `plan_verify` after the schema takes the raw plan dict and
indexes it directly: cardinality reads `step["kind"]`, the frame reads
`step["args"]["path"]`, bounding calls `.count` and `.encode` on `old` and
`new`. Nothing any phase receives *proves* the schema phase ran — the driver's
call order in `verify_plan` is the only thing standing between those reads and
unvalidated model output. Mutation-testing that exact assumption (findings
0002, the F1 replay) found the class live: with the schema no-opped, ill-shaped
steps crash cardinality with `KeyError`, the frame with `TypeError`, bounding
with `AttributeError` — and one shape, a duplicate step id, is **accepted**
end to end, because no phase after schema reads ids at all. `86a1154` and the
follow-up guards refused the instances mutation testing reached; the survey
recorded the rest as bounded rather than closed, because refusing each one
means re-proving the schema's contract inside every reader.

The class exists because the phases *validate* and then hand back the same
untyped data they were given. The fix is to **parse**: make the schema phase
the one constructor of a value that cannot exist unless it passed.

## Decision

1. `check_plan_schema` becomes a parser, `parse_plan(candidate, policy_plan)
   -> tuple[Step, ...]`, where `Step` is a frozen dataclass
   `(id: str, kind: str, args: Mapping[str, str | int])`. Same checks, same
   `Rejection` messages, same first-violation-wins order — the return type is
   the only new thing. Every later phase's signature takes the parsed steps.
   The unwired-schema failure mode then stops being a guard, a mutation test
   or a convention and becomes **inexpressible**: the driver cannot reach a
   phase without steps, and there is no way to obtain steps but the parser.
2. Only the parser constructs `Step` in `src/`; tests may build them by hand,
   which is the fixture convenience the per-phase classes already rely on.
   This is a convention a type checker cannot see, so it is stated here and
   held by review.
3. A type checker joins CI as **its own job**, beside `quality_check`, not
   inside it — that job's contract (deterministic pytest, no secrets,
   gate-free) stays exactly what test_workflow_shape pins. **ty**, not
   pyright and not mypy: pyright ships on Node, and reintroducing the
   toolchain ADR-0016 deleted to check the module that ADR consolidated would
   be the detour argument in reverse; ty is uv-installed and hash-pinned from
   PyPI like everything else in the runner, from the same vendor as uv itself.
   It is a compiled artifact, which is acceptable where it was not for
   pydantic: the checker runs only in CI as a dev tool and never inside the
   credentialed jobs, so it sits outside the gate's runtime trust boundary.
   It is also the youngest of the three — if its inference proves too shallow
   to hold the parser-only-constructor discipline, mypy is the drop-in
   fallback, because the durable decision here is the checked signatures, not
   the checker. The checker is what makes the new signatures teeth rather
   than documentation.

## What this deliberately does not change

- **The cross-job posture.** `execute_plan` re-verifies from `plan.json`
  bytes in the process holding the write token, and `decide_delivery` /
  `one_step` re-decide from the plan's structure. A typed object cannot travel
  across a job boundary, and one arriving from the plan job would be exactly
  the claim this harness refuses to trust. Parsing replaces redundancy only
  *inside* a process, where the parse and the read share a memory space.
- **The refusal texts and phase order.** The corpus's pinned messages, the
  retry feedback the plan session sees, and the policy-error convention are
  untouched. That is also the standing argument against pydantic here: its
  messages are library-owned, its error model collects rather than stopping at
  the first violation, the schema is policy-driven at runtime, a gate must
  never coerce, and `pydantic-core` is a compiled artifact inside the
  credentialed jobs' trust boundary. jsonschema fails the same message and
  ordering tests and still only validates — the class this ADR closes comes
  from handing back untyped data, which a validator does by definition.
- **ADR-0004's closures.** `Step` is `{id, kind, args}` with literal scalar
  args — the parsed twin of the shape that ADR reserved, not a widening.

## What is deliberately given up

1. **Phases runnable against arbitrary raw dicts.** A standalone caller parses
   first; the per-phase tests construct `Step`s. That churn — every fixture in
   the per-phase classes — is the bulk of the cost.
2. **The interim guards, retired with their reason.** The missing-arg and
   non-string refusals added after the F1 mutation check guard reads that can
   no longer receive unparsed data; they and their tests go with the refactor
   rather than surviving as dead code. The three schema-unwiring detectors in
   the corpus (`max_steps`, the `$ref` binding, duplicate ids at driver level)
   stay: they assert refusals of real attacks, and detecting unwiring was
   always their side effect.
3. **Arg-level shape stays a parse guarantee, not a type.** `args` is a
   mapping, so `step.args["path"]` is well-typed even for a kind with no
   `path`; the parser's per-kind key check is what makes the read safe, and
   mypy cannot see it. Per-kind arg records (`PatchArgs`, `SuggestArgs`, …)
   for the five kinds the gate's readers actually index are the extension if
   that residue ever bites — deferred, because the readers' key sets are
   pinned by the policy agreement tests today.

## Consequences

- `verify_plan`'s public signature is unchanged (a dict in, `Rejection` out),
  so `execute_plan` and `plan_loop` do not move. The change is internal to the
  module and its tests.
- The mutation experiment that motivated this becomes unrunnable in its
  current form — no-opping the parser leaves nothing to pass to the phases.
  That is the point, and the findings doc's survey paragraph is the record of
  what the experiment showed while it could run.
- The ty job is a second required check. It holds no secrets and runs on
  every PR from any fork, the same standing rule as `quality_check`.
