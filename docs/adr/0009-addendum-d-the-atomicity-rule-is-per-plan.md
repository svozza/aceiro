# Fourth addendum to ADR-0009: the atomicity rule is per plan, not per defect

ADR-0009 states the atomicity rule as a claim about defects:

> a coordinated fix must never be delivered as independently applicable pieces,
> even where each piece would be mechanically postable.

What is enforced is a property of **one plan's step list**. `decide_delivery` counts
the distinct paths among the steps it was handed, and `check_plan_cardinality`
permits at most one `suggest` step per path. Both quantify over a single plan.

Two commands produce two plans. Each is internally atomic, and together they deliver
exactly the shape the rule forbids.

## The gap is open today, in production

Trace the case the reviewer was measured producing (addendum C: it split one
cross-file defect — two call sites passing the wrong argument — into findings 1 and
2). A commander types `/fix 1`. Every gate passes: the finding is an element of the
accepted artifact, its path is in `changed_files`, `check_commanded_scope` holds, the
plan is one contiguous `suggest` step, `decide_delivery` returns suggestions. The
comment goes up, the contributor clicks Apply, and half of one defect is fixed.

Nothing refused, warned, or recorded that the other half exists.

**And the harness cannot close it.** Refusing `/fix 1` on the ground that finding 1
is half a defect requires knowing that findings 1 and 2 are one defect — the content
question ADR-0005 establishes is unverifiable and addendum C refused both widenings
over. The split happens at the **reviewer**, one artifact upstream, where no plan can
see it.

So the ADR's unqualified sentence claims something no gate enforces, which is the
failure mode this project keeps having to correct: a rule that reads as enforcement
while enforcing something narrower.

## Decision: correct the language, accept cross-command partiality

**No single remediation is delivered as independently applicable pieces.** That is
the enforced property, it is what `decide_delivery` and `check_plan_cardinality`
check, and it is all ADR-0009 should claim. Partiality across two commands is
accepted.

Accepted rather than merely tolerated, because the two sub-cases are not equally bad
and the harmful one is the loud one:

- **Independently improving** — two inverted call sites. Fixing one is a strict
  improvement, and finding 2's inline comment is still on the pull request, still
  visible, still commandable. Nothing claimed completeness: the commander asked for
  finding 1 and received finding 1.
- **Mutually dependent** — a signature change plus its callers. Half-applied this is
  *worse* than the original defect, and it is the case that **fails the consuming
  repository's own required checks**, pre-merge, on the contributor's branch. That is
  precisely where ADR-0001 rests the test signal: "the consuming repo's existing
  required checks judge the result."

The asymmetry is the argument, and it is checkable rather than hopeful: the invisible
failure is the benign one, and the harmful one is caught by the pull request's own
gate. What remains is a mutually-dependent fix that breaks a **runtime** contract
rather than the build — and that is not a new hole. It is ADR-0005's accepted one:
patch content is unverified by construction, and the pull request is the gate.

## The disclosure obligation this decision carries

Accepting cross-command partiality is only honest if the commander can see the split
before choosing their command. So the reviewer states its grouping and the harness
renders the cross-reference, which is ADR-0013's `group` field — and the multi-finding
command exists so that a commander who reads it has something to type.

Without ADR-0013 this addendum would be accepting a partial fix while giving the
commander no way to avoid it. With it, `/fix 1` on a grouped finding is a choice
rather than a trap.

## Consequences

- ADR-0009's decision section keeps its reasoning and loses its over-claim. The
  structural boundary (one path, one hunk → suggestion; spanning paths or coordinated
  hunks → the stacked pull request) is unchanged and correct.
- `check_plan_cardinality`'s docstring is more accurate than the ADR was: "the gate
  cannot tell a coordinated pair from an independent one — that is a judgement about
  the code, not a property of the plan — so it refuses the shape." That sentence is
  the per-plan reading, already written down in the code.
- A commander who wants atomicity for a split defect has one, through ADR-0013's
  set-valued command. The partial path remains available, and the harness still
  cannot tell that taking it was a mistake.
