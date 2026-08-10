# The command names a set of findings, and the human is the only source of its scope

ADR-0009's addendum C established that a multi-file coordinated fix cannot be
scoped from one anchored finding, refused both widenings of the **Finding**, and
left the gap located in the command's shape with the ADR to be written. This is
that ADR.

`/fix N` becomes `/fix N[,M...]`: the command names one **or more** findings of one
accepted review artifact, and the fix must touch every path they name.

## The scope comes from the human, and there was no other candidate

Four sources were available, and three of them are refused by decisions this
project has already taken.

**A model-emitted group id as the scope** — the reviewer marks findings 1 and 3 as
one defect and `/fix 1` fixes the group — is the model-chosen scope addendum C
refused, moved one artifact upstream. The gate cannot check that two findings are
instances of one defect; that is ADR-0005's unverifiable content question. Reading
the group as authority means the model decides how many files a write touches.

**The diff** — `/fix` bare, scanning the whole change — is `/fix all`, asked and
answered (§3c: no single finding to key the deduplication on, cardinality allows one
write chain, and it removes the per-finding human judgement ADR-0007 buys). The
shipping tools take their scope this way (CodeRabbit's checkbox, Copilot's free
text), and they can because nothing in their pipeline has a verified-scope property
to preserve.

**Nothing** — leave addendum C's revisit trigger standing — was the honest fallback
if the concession below turned out to be unacceptable.

What remains is the commander, and the shape it produces is the one the rest of the
harness already has: the model proposes, a **trusted author** scopes, and the checker
verifies coverage. The harness never judges whether the named findings are one
defect. It bounds and verifies what was asserted:

- every ordinal resolves in the same accepted artifact at the same head SHA, through
  the derivation ADR-0007's second addendum built (`review.json` plus the ordinals;
  no forgeable finding input appears anywhere);
- the named paths are within `max_patched_files`;
- **`check_commanded_scope` becomes ⊆ rather than ∈** — every commanded finding's
  path must be among the fix's paths. For a single ordinal this is exactly today's
  check, so the existing property is a special case rather than a thing replaced.

## Stacked delivery's trigger falls out; it is not engineered

Addendum C's sharpest finding was that the stacked follow-up pull request has **no
reachable trigger** through `/fix`, and its warning was that reaching one by
changing the prompt's expression rule would be "engineering the trigger to fit the
vehicle".

Nothing here changes the prompt's expression rule. Two commanded findings on
distinct paths mean the fix must touch both paths; a suggestion carries exactly one
path (GitHub's review comment has one `path`, which addendum C established makes a
multi-file suggestion *unrepresentable*); so `decide_delivery` routes `stacked_pr`
because the plan genuinely spans files. The trigger is a consequence of the command's
scope, and the multi-file coordinated change was ADR-0009's motivating case from the
start.

The same-file case needs no new machinery either. `check_plan_cardinality` already
permits at most one `suggest` step per path, so two findings anchored in one file
route to suggestions, and that rule is unchanged by this ADR.

**Amended 2026-08-10.** This paragraph asserted that two same-file findings "produce
one contiguous replacement — which is correct", and the clause is struck: nothing
enforces contiguity or coverage, so the sentence described a property the code does
not have. What cardinality actually guarantees is *one* `suggest` step per path. Which
lines that step addresses is not checked. Measured, for two findings on non-adjacent
changed lines:

- **two `suggest` steps** — refused by cardinality, as the paragraph says;
- **one span covering both plus the lines between** — verifies, and rewrites the
  uncommanded lines inside the span;
- **one step covering only *one* finding** — verifies, delivers half the command, and
  nothing refuses, warns or records it.

A coverage check was considered and **refused**, because a finding's anchor is not
where its fix goes. CONTEXT.md's Finding entry is explicit — "anchored to the changed
line responsible, not to the defect" — and the harness's own grouping scenario is the
demonstration: the findings anchor to the use site because the import is out of hunk,
while the correct fix changes the import. The two regions are disjoint *by design*, so
a rule requiring the fix to cover the anchored line would refuse the answer the evals
grade as right. It would also be `suggest`-only, since `patch` carries no `line` — and
`patch` is the kind the multi-file case this ADR exists for actually routes to.

The span-rewrite half is **not this ADR's**: the placement logic is byte-identical on
`main` and reachable through a single `/fix N`, which predates the set-valued command
entirely. It is ADR-0005's accepted position — anchoring and the bounding caps
constrain *where* a fix may land and *how much* it may change, never which lines
within that region, because that is the unverifiable content question. The human
merge is the control. Recorded here rather than fixed, and the claim corrected rather
than the check stretched to make it true — ADR-0009 addendum D's precedent, applied
twice more in the same pass (`check_group_cardinality`'s docstring, ADR-0014's
Consequences).

## The concession, stated plainly

`/fix 1,5` naming two genuinely unrelated defects produces one mixed follow-up pull
request — the shape §3c refused as "unreviewable". The harness cannot distinguish it
from a legitimately coordinated fix, because that is the same unverifiable content
question arriving at the command instead of at the finding.

So this ADR does not close that hole; it **relocates** it, from a shape the harness
refuses structurally to one it permits on the commander's trust. That is accepted,
bounded the way `route_delivery`'s concession is bounded:

- the commander holds write-or-above, resolved from the collaborator API before
  anything else happens (ADR-0007);
- `max_patched_files` bounds the blast radius whatever was named;
- the artefact is a pull request the same human has to read and merge, which is
  ADR-0005's containment argument unchanged.

A commander who groups unrelated findings has made a reviewable mess for themselves,
in public, under their own name. That is a different category from a model doing it
unbidden.

## Deduplication: the key is over the set, the comment marker stays per finding

The two identity keys answer to different masters, and under a multi-finding command
they diverge.

**`fix_key` is computed over the set** — the sorted per-finding identities folded
into one key, so `/fix 3,1` is the same command as `/fix 1,3` and cannot open two
pull requests. `/fix 1,3` issued twice is the duplicate ADR-0007 refuses.

**`/fix 1` followed by `/fix 1,3` computes different keys and is honoured.** It is a
different scope, a different fix and a different artefact; refusing it would mean a
commander who narrowed too far could never widen. Read as a **widening**, which is
also how it reads to the human who typed it.

**The comment marker stays per finding.** Each suggestion comment speaks for exactly
one finding (`finding_marker` / `owned_finding_key`, landed 2026-08-06); what carries
a set is the *command*. So the change is to the scope comparison in
`reconcile_suggestions`, not to what a comment records about itself.

That asymmetry pays for itself on the shared-file case: `/fix 1,3` with both anchors
in one file reconciles with scope `{K1, K3}`, so the earlier `/fix 1` comment is in
scope, absent from the wanted set, and retracted by the existing reconciler with no
new mechanism.

## The orphan a widening can leave, and why nothing is built for it

When the widening crosses from suggestions to a stacked pull request, the earlier
command's suggestion comment survives: `execute_plan` returns on the stacked path
before `reconcile_suggestions` is reached, so the stacked delivery cannot touch it.
One half of a coordinated fix stays independently applicable through the leftover
artefact of the earlier command.

A retraction pass on the stacked path was designed and then **withdrawn**, because
its own reachability argument defeats it:

- On one head, the only route to this state is a commander who did not read the
  cross-reference the harness rendered for them. ADR-0009's second addendum already
  priced that trade for the stale stacked pull request — "closing a pull request is
  one click, for the same maintainer who typed the command" — and deleting a
  suggestion you orphaned by widening your own command is the same click.
- If the contributor applies the orphan after the stacked pull request exists, the
  head moves and the stacked pull request's `patch` no longer applies cleanly, so
  GitHub shows it as conflicting. That is the fail-visible signal the second
  addendum's accept-and-record decision rests on. **The half-fix cannot land
  silently.**

Accepting it costs no code at all, which is the other half of the argument: the
alternative adds a write path to the job holding the broadest credential in the lane
in order to tidy a state only a careless command reaches.

## Disclosure: the reviewer states the split, the harness renders the reference

A commander cannot type `/fix 1,3` without knowing that findings 1 and 3 are one
defect, and the reviewer is the only participant that ever knows. So each finding
carries a **required `group` integer**, and same-valued findings are claimed to be
one defect.

Three properties make this safe rather than a re-run of the refused candidate:

- **Required, not optional.** ADR-0004 is explicit that a fail-closed verifier
  cannot tolerate optional fields or unknown keys — `check_schema` rejects extras and
  `markdown_fields()` raises on any undeclared string field. Singleton groups are the
  ordinary case.
- **The verifier bounds it and never believes it.** Integer, range, a cap on distinct
  groups. Whether the claim is true is ADR-0005's content question and is not
  checked, which is why the field can never be the source of a write's scope.
- **The cross-reference is rendered by the harness, in `post.render`.** A
  model-authored "see also finding 3" cannot name an ordinal: `rendered_findings`
  sorts by severity at render time and the model never sees the sorted list, so
  model prose would name a *different real finding* whenever the two orders differ —
  the silent wrong-finding failure ADR-0007's second addendum exists to prevent.
  `post.render` already calls `rendered_findings` and is the only place ordinals
  exist.

A top-level `groups: [[0, 1]]` array was refused: model-supplied **indices** into the
findings list is that same model-order-versus-rendered-order hazard in a new place. A
group id on the finding carries no indices.

**The condition that keeps a group advisory: no code in the fix lane may read it.**
`/fix 1` must not expand to finding 1's group. What authorises the write is the
ordinals the human typed, and a group is prose addressed to that human. The drift
from advisory to authorising is one convenience commit wide, so it is enforced the
way ADR-0004's addendum enforced `control_flow` — by a coverage assertion, run in
reverse: this field has **no reader** in `prepare_fix_context`, `plan_loop` or
`plan_verify`. Without that assertion the disclosure should be refused outright,
because a group that authorises anything is the model-chosen scope this ADR opens by
refusing.

## Consequences

- **`prompts/ai-pr-review.md` is version 4 and eval-measured, unlike the plan
  prompt's DRAFT.** So this change carries ADR-0008's `--runs 3` obligation, a
  prompt-version bump, and the field through every eval fixture and test artifact.
  The price was weighed and accepted rather than discovered.
- **A required field makes prior artifacts uncommandable.** `read_commanded_finding`
  runs the full review verifier over `review.json`, so every artifact posted before
  the change now fails `verify()` and no `/fix` can be issued against it. Fail-closed
  and self-healing — retention is 90 days and the next push posts a review carrying
  the field — but in-flight pull requests are briefly uncommandable, which is a
  migration fact worth stating before someone reports it as a defect.
- `read_commanded_index` reads a set rather than an integer, and every bound the
  second addendum records still applies per element: `bool` is an `int` in Python and
  a negative value is the one out-of-range ordinal that silently resolves to a real
  finding. A set adds two of its own — the empty set is not a command, and duplicate
  ordinals collapse rather than naming a finding twice.
- The commanded finding reaching the plan prompt becomes commanded findings, plural,
  each fenced. The prompt's "that finding, nothing else" becomes "those findings,
  nothing else", which is a change to a **DRAFT** prompt and therefore the ordinary
  kind of edit — but it is the edit that makes multi-file plans expressible, so it is
  named here rather than left to the implementation.
- `decide_delivery`'s region check (owed since addendum C as defensive hardening) now
  has its trigger. Two `suggest` steps on one path route to suggestions and post two
  independently applicable comments; unreachable as prompted, but this ADR makes
  multi-finding plans reachable and the rule ADR-0009 claims is "checkable from the
  verified plan's step list" should be checked from the step list before that
  happens.
- `check_plan_cardinality` is unchanged, and that is a finding rather than an
  omission: its one-suggestion-per-file rule is what makes the same-file
  multi-finding case correct, and its no-fix-step refusal still protects the shape
  that reaches `contents: write` while remediating nothing.
