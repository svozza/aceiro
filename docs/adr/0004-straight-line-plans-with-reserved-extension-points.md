# Straight-line plans, with three reserved extension points

*Note (2026-08-15): "the prover" below is the TypeScript SMT prover ADR-0003
established, retired by [ADR-0016](0016-retire-the-typescript-prover-consolidate-on-python.md);
its checks live in `plan_verify.py`. The decision here — straight-line plans,
three reservations that refuse their shape — is unchanged, and the staged
grounding extension remains not built.*

§20 describes plans as programs — steps, branches, data bindings — and states
its taint policy over execution-time reads: data derived from `read_pr_file`
must never flow into `push_branch.name`. But the generator already reads PR
content at generation time, via the CLI's Read/Grep/Glob against the quarantine.
By the time an artifact exists, every literal in it is model-authored and
already PR-derived, so there is no `read_pr_file` node to taint from.

Deferring reads to execution time would make the taint sentence literal, but it
would also have the *trusted* executor interpreting a model-authored program
with control flow, and every execution-time read would be untrusted data
entering the privileged process — the same objection that removed `run_tests` in
ADR-0001. The benefit is small: the model already has full read access during
generation, and the head-SHA guard says to abort on drift between generation and
execution rather than adapt to it.

So plans are straight-line lists of literal-argument steps, and the prover's
initial remit is frame conditions and ordering among the mutating actions rather
than taint. §2.5's decision tree still routes to a solver via "more than one
mutating action?" — `patch`, `push_branch`, `open_pr` and `label` are four.

## What is reserved

Extensibility lives in the policy vocabulary, never in the artifact. A
fail-closed verifier cannot tolerate optional fields or unknown keys:
`check_schema` rejects extra keys, and `markdown_fields()` raises a policy error
on any string field that has not declared how it is validated. Three closures,
each of which reserves a shape and refuses it today:

1. **Steps are typed records with explicit ids.** `{id, kind, args}` even while
   straight-line, because a straight-line plan is the degenerate case of a
   program. Adding a `branch` kind later is an entry in `step_kinds` plus
   `"control_flow": ["branch"]`, not a shape change.
2. **`"argument_forms": ["literal"]`.** Execution-time bindings would arrive as
   a distinguishable shape (`{"$ref": "step1.output"}`), so an object where a
   string is expected rejects today. Discriminated by shape rather than by a
   wrapper on every argument, because wrapper noise costs generator liveness.
3. **No version field in the artifact.** A model-supplied schema version is a
   model-selected policy, which is v2 §2.1's banned move. The policy owns its
   version and its sha256 is already stamped into the transcript.

Deliberately NOT reserved: parallel steps, loops, per-step timeouts, retry
semantics, nested plans, conditional expressions. Inventing vocabulary for
threats we cannot yet state is the speculative generality §2.5 warns about, and
a policy field nobody enforces is worse than no field.

## Consequences

- The prover is encoded in its general form from day one — bindings, transitive
  taint, per-step reachability — as the spike already does, so the ∀-paths
  machinery is exercised from the first commit rather than dormant.
- Because `argument_forms` admits only literals, taint is trivially `unsat` on
  every admissible plan. A check that is green forever carries no signal, so the
  encoding's corpus must include synthetic binding-bearing plans that the schema
  forbids, asserting `sat` with the expected counterexample. The prover is
  tested beyond what the policy admits.
- If a later plan genuinely needs discovery (a multi-file refactor, a fix
  depending on a build artifact), the staged-grounding shape is the way in:
  generate → verify symbolically → resolve unprivileged → re-verify ground →
  execute. That keeps execution-time reads out of the privileged process. Not
  built now.
