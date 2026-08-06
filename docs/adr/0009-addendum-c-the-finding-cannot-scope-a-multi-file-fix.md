# Third addendum to ADR-0009: a finding cannot scope a multi-file fix

ADR-0009 built the stacked follow-up pull request for "a fix that is only correct as
a whole", and named the multi-file coordinated change as its motivating case. The
first production run of the remediation lane showed that the review-to-fix pipeline
**cannot produce one**, and the chain is structural rather than incidental:

- a **Finding names ONE path and ONE line** — enforced by `policy.json`'s
  `item_fields`, and defined that way in `CONTEXT.md`. A multi-anchor finding would
  need a partial-acceptance rule the verifier deliberately does not have.
- the plan prompt says **"Fix the commanded finding — that finding, nothing else"**,
  and "other findings get their own commands".
- so every plan is confined to one file, and a single-file fix is naturally
  CONTIGUOUS → one hunk → suggestions.

Measured on `svozza/artel` #61: the reviewer split ONE cross-file defect (two call
sites passing the wrong argument) into findings 1 and 2, each with a valid one-line
fix. Four commands produced four suggestions and `stack` was skipped every time.

## The industry does not solve this either, and its split is ours

Checked against the mechanism and against three shipping tools, because "our channel
is too narrow" and "this shape does not exist" have different answers.

**GitHub's API forbids it.** A review comment carries exactly one `path`; there is no
parameter by which one comment addresses two files. A suggestion CAN span a line
range within one file (`start_line`/`start_side` → `line`/`side`), but never two
files. So a multi-file suggestion is not a feature anyone has declined to build — it
is unrepresentable.

Every serious tool therefore splits at exactly the boundary ADR-0009 chose:

- **Qodo Merge** — "Small, single-file fixes get a one-click **Apply Suggestion**
  button"; "larger fixes come with an Agent prompt you can hand to an AI coding tool
  to implement with full context."
- **CodeRabbit** — generated changes are "committed to a new branch and a follow-up
  PR is opened against your original branch", and one run's edits across several
  directories "all land on one new branch surfaced as a single follow-up PR."
- **Copilot code review** — inline suggestions "where possible"; anything larger goes
  to **Fix with Copilot**, which delegates to the coding agent and produces "a new
  pull request against your branch or a commit on the same pull request."

Single-file → suggestion, multi-file → branch-and-pull-request is therefore
convergent design, not a local limitation. ADR-0009's decision is confirmed by
everyone who has shipped this, and the stacked pull request is the right vehicle.

## The gap is the COMMAND's shape, not the finding's

What the comparison also shows is where the others get their scope from. CodeRabbit's
multi-file flow is triggered by a checkbox or a broad command and scans the whole
diff; Copilot's is a free-text instruction to an agent. **Neither derives a
multi-file fix's scope from a single anchored finding**, because nothing can: a
Finding is a precise one-anchor object, which is exactly what makes it a good
suggestion and structurally incapable of scoping a coordinated change.

So the two candidates that tried to widen the FINDING are both refused:

- **Let a finding name a coordinated fix across files.** It fights provenance and the
  one-anchor rule, and the blast radius is larger than that: every identity key in
  the system maps `(path, line)` to one signature — `suggestion_fingerprint`,
  `fix_key`, `anchor_signatures` — so a two-anchor finding has no single fingerprint
  and both dedup mechanisms need redesigning. It also breaks the ordinal contract,
  since `rendered_findings` sorts one flat list and `/fix 3` means "the third finding
  of the comment I read" (ADR-0007's second addendum).
- **Let the remediator fix the defect's other instances.** Tempting because
  `check_commanded_scope` is already AMONG, not equal to — the *gate* permits it and
  only the prompt forbids it, so it looks like a one-line change. That is the reason
  to refuse it. The gate is AMONG so that a genuinely coordinated fix is
  *expressible*; it does not verify that the other paths are instances of the same
  defect, which is the content question ADR-0005 establishes is unverifiable. Loosen
  the prompt and the scope of every path beyond the commanded one rests on the
  model's word — a model-chosen scope, adjacent to the model-chosen policy ADR-0004
  bans.

## Decision

**The stacked follow-up pull request serves same-file, multi-region fixes today. Its
multi-file case is reachable only through a command shape that does not yet exist,
and ADR-0009's multi-file language is aspirational until that ADR is written.**

Recorded rather than engineered away, because the evidence says the shape of the
answer is a differently-scoped command — one whose scope comes from something other
than one finding's anchor. `/fix all` was asked and answered already (§3c: not
needed, and it breaks the dedup key, the one-write-chain cardinality, and the
per-finding human judgement ADR-0007 buys), so the next shape is not that one either.
It is the same question chunk D's decline channel asks — what does the harness do
when a finding needs an action the channel cannot express — and it should be designed
with it, not before it.

**Revisit trigger:** the reviewer observably splitting one defect into per-file
findings on real pull requests, with maintainers commanding both halves separately.
That is a reviewer-side measurement, and it is the evidence that would justify
designing a second command shape.

## The same-file trigger is not reachable either

Recorded because it was believed to be the one reachable route to stacked delivery,
and repeated attempts to trigger it have all failed. The mechanism is the prompt, and
it is not a near miss:

- **"A suggestion plan is exactly ONE `suggest` step"**, next to "the smallest correct
  fix wins" and "every changed line is a line a human must review". To reach stacked
  delivery for a same-file fix the model would have to emit `patch` plus `push_branch`
  plus `open_pr` — three steps and a whole pull request — for something the same
  prompt tells it to express as one contiguous replacement.
- **Contiguity is a choice, not a constraint.** `old` must "match the file
  byte-for-byte and occur exactly once; include enough surrounding lines to make it
  unique" — so two regions in one file are ALWAYS expressible as a single pair, by
  widening the span to swallow the gap and re-emitting the unchanged middle in `new`.
  The only limits are the bounding caps and in-hunk provenance. The hoped-for forcing
  case — two regions far enough apart that no single pair can span them — assumes a
  constraint the vocabulary does not impose.

So the earlier framing ("multi-file is the motivating case; hunk count is the
reachable trigger") is wrong in both halves. **Stacked delivery has no reachable
trigger through `/fix` at all.** It is built, gated and covered by 44 unit tests and
revert checks, and nothing the review-to-fix pipeline can produce reaches it. That is
the finding, and it is stronger than "has never run in production".

Two consequences worth stating:

- The evidence gap cannot be closed by planting a cleverer defect. Reaching the
  stacked path requires changing the prompt's expression rule, and the prompt is
  DRAFT and pinned by a test precisely so that changing it is a decision. Doing that
  to exercise a delivery mode would be engineering the trigger to fit the vehicle.
- Whatever command shape the multi-file case eventually needs is therefore also what
  gives stacked delivery its first real trigger. The two questions are one question,
  which is further reason not to answer them separately.

## The atomicity rule is enforced by the prompt, not the gate

Found while checking the trigger, and it is a defect rather than a limitation.

ADR-0009 says both sides of the atomicity rule "are checkable from the verified
plan's step list, which is what keeps the delivery decision the executor's".
`decide_delivery` routes suggestions on **distinct paths** and refuses more than one.
It does not count regions. So a plan with two `suggest` steps on ONE path returns
`Delivery("suggestions", path)` and posts both — two independently applicable
comments for a fix that may only be correct as a whole, which is the precise harm the
atomicity rule exists to prevent.

Nothing reaches that state today, and the prompt is why twice over: it tells the model
to use `patch` for "more than one hunk in a file", and it also says a suggestion plan
is exactly one `suggest` step. So this is **defensive hardening, not a live defect** —
the shape is unreachable from the generator side as currently prompted.

It is still worth closing, for the reason the production test's fourth finding
demonstrated: the prompt and `decide_delivery` disagreeing about delivery is a defect
class this project has already paid for once, and there the prompt was the thing that
was wrong. A rule this ADR claims is "checkable from the verified plan's step list"
should be checked from the step list, so that a future prompt revision cannot quietly
make it false. Ordering: it belongs with whatever change eventually gives stacked
delivery a trigger, since that change is what would make the shape reachable.

## Consequences

- The stacked delivery is built and gated (the tree route, the dedup key,
  `contents: write`, the branch-and-PR sequence, 44 unit tests and revert checks) and
  **has no reachable trigger**. It is not merely unproven in production: nothing the
  review-to-fix pipeline can produce routes to it. Its first real exercise arrives
  with the command shape above, so the code stays as built and unexecuted until then —
  a deliberate state, recorded here so it is not mistaken for an untested oversight.
- ADR-0009's fork asymmetry is unchanged and now tighter than it read: a multi-file
  fix on a fork pull request has no automated delivery, and it also has no reachable
  command. Both limitations are accepted.
- `CONTEXT.md`'s **Finding** entry is correct as written and deliberately not
  widened. One anchor is the property that makes provenance checkable.
