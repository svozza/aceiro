# File creation is a step kind behind an allowlist that ships empty

ADR-0005 closed with a named limitation: the most valuable remediation —
"you changed this function, here is the regression test" — is usually a new
file under `tests/`, which by definition the pull request did not touch, so
it is out of scope. It called that the limitation most likely to want
revisiting "when a concrete plan requires it". The concrete plan arrived
immediately: the review prompt's mandate includes "missing or wrong tests"
as a finding category, so the reviewer is designed to produce exactly the
findings whose commanded fix the plan vocabulary cannot express. The same
collision comes from the second behaviour the review side deliberately
teaches — defects in unchanged code, anchored to the changed line that makes
them reachable — where the finding's anchor is in frame but its fix is not.
Two intended review behaviours generate commanded fixes with no legal plan,
and a commanded task with no legal output pressures the generator toward an
in-frame fix that verifies and is wrong.

Re-examining ADR-0005's objections against file creation specifically, they
mostly invert:

**Anchoring inverts and strengthens.** A patch's `old` proves the model was
looking at the file; a created file's anchor is that the path **must not
exist at the reviewed SHA**. Same content source, same fail-closed posture —
the file appearing underneath the plan is drift, and drift means refuse.
It is stronger than patch anchoring: exactly-once ambiguity cannot arise on
a path with no bytes.

**The two-sources objection dissolves when create is its own kind with its
own closed set.** ADR-0005 rejected extra *patchable* paths because
patchable paths would then have two sources and allowlist/denylist
evaluation order becomes security-relevant (the §17 shape). A `create` step
kind never mixes sets:

- patch/suggest paths ⊆ `changed_files` (unchanged);
- create paths ⊆ `policy.plan.create_allowlist` ∧ ∉ `changed_files`;
- the path denylist applies on top of both, in that stated order.

No path has two sources; each kind has one, and each set is closed before
the denylist narrows it. A create path that IS a changed file rejects —
"create" claiming an existing path is either a botched patch or an
overwrite, and both must be said as what they are.

**The shipped default carries zero new behaviour.** `create_allowlist`
ships `[]`, the `link_host_allowlist` precedent exactly: creation is
inexpressible until a consumer names, in the reviewable security object,
the path patterns they accept model-authored new files under. The expected
first entry is `tests/**`; the policy is where that decision is made and
hashed, not here.

## Decision

- The plan vocabulary gains a `create` step kind: non-write-class (the
  effect is inert plan data until the executor's write chain runs, same as
  `patch`), args `{path, new}`. No `old` — there are no prior bytes — and
  no `line`. `new` has `min_length: 1`; an empty created file expresses no
  fix and is refused rather than waved through.
- Verifier checks, in the containment phase's cheapest-first order:
  - **Frame:** `path` matches `create_allowlist` and is not in
    `changed_files`. An empty allowlist rejects every create step, so the
    shipped default cannot express creation at all.
  - **Denylist:** the existing `path_denylist`, applied after the
    allowlist. A consumer allowing `tests/**` still cannot receive
    `tests/key.pem`.
  - **Anchoring (inverted):** the content source must FAIL to read the
    path. A successful read is the rejection.
  - **Parent containment:** every component of the path that exists at the
    reviewed SHA must be a real directory — not a symlink, not a file. The
    quarantine tree is contributor-authored, so `tests/` being a symlink
    pointing out of the repository is an expected hostile shape: a patch
    target's escape is caught by resolving the existing file, but a create
    path's tail does not exist to resolve, so the check walks the parents.
    The executor applies the same discipline at write time (`O_NOFOLLOW`
    posture: never write through a symlinked component).
  - **Bounding:** created paths count toward `max_patched_files` (one cap
    on "files this plan touches", not two caps with an interaction), and
    `new`'s line count against `max_changed_lines` per step.
  - `new` joins the secret scan's representations, raw and fused, as
    patch/suggest content already does. It is exempt from the markdown
    gate for the same pinned reason as patch `old`/`new`: file bytes,
    never rendered as prose, gated by anchoring's inverse and the human
    merge.
- The prover mirrors the frame change: `create` paths are a second
  quantified claim (∀ created files: allowlisted ∧ untouched) beside the
  existing one, over the same interned universe. `create` joins `patch` in
  the ordering rule before `push_branch`. Both gates read the same
  `create_allowlist`, and the shipped-policy tests on each side pin
  `create`'s argument set exactly, as they pin `open_pr`'s.

## The fork asymmetry does not go away

A create step can only be delivered as the stacked follow-up pull request:
suggestion blocks attach to existing diff lines and cannot create files.
The 0009 addendum established that a stacked PR is impossible on fork pull
requests. So the highest-value topology — a maintainer commanding a
regression test on a first-time contributor's fork PR — still has no
automated delivery even with `create` in the vocabulary. Creation fixes the
same-repo case; the fork case remains "state the direction in the review,
a human does the rest", and the decline channel is where the generator says
so honestly. The two features interlock rather than compete: the decline
channel is the honest exit and is needed regardless; `create` shrinks the
set of things that decline.

## Consequences

- A created file is the first plan content with no diff provenance at all.
  Patch content is unverifiable but at least scoped to code the PR put in
  front of a reviewer; a created file is entirely model-authored at a path
  nobody was looking at. What makes it acceptable is unchanged from
  ADR-0005 — the pull request is the gate, a human merges — but the
  consumer's allowlist choice is now a scoping decision with teeth, and the
  documentation for it must say so. Recommended pairing: allow `tests/**`,
  deny `**/conftest.py` — conftest is the one test file pytest executes for
  everyone on every run rather than when its tests are invoked, which makes
  it the natural home for an import-time payload.
- The delivery decision in ADR-0009 gains a clause it already implied: any
  plan containing a `create` step is follow-up-PR shaped, whatever else it
  contains. Still checkable from the verified step list; still the
  executor's decision, never the model's.
- The adversarial corpus grows the inverse near-misses: create of an
  existing file (both spellings — in `changed_files`, and merely on disk),
  create through a symlinked parent, create of `tests/../src/x.py`,
  allowlist evasion twins of the denylist cases (`testsX/`, `tests2/`),
  denylisted path inside the allowlist, and the empty-allowlist baseline
  asserting every create rejects under the shipped policy.
- The plan prompt must explain when to reach for `create` and that it
  implies the follow-up-PR shape — a measured change, evals before trust,
  same as everything else in that file.
- Not decided here: directory creation semantics beyond the write path
  (`tests/unit/new/` where `unit/` exists and `new/` does not is fine —
  the executor creates intermediate directories under the same no-symlink
  discipline), executable modes or any file metadata (a created file is
  `0644` content, full stop), and widening `suggest` to deliver creations
  (GitHub has no mechanism; if one appears it gets its own ADR).
- **Before implementation: run a `/grill-with-docs` session on this ADR.**
  It was written ahead of any code, so its claims — the objection
  inversions, the parent-containment check's sufficiency, the one-cap
  bounding — have been argued once and stress-tested never.
