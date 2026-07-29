# The plan prover is TypeScript, on z3-solver via WASM

§20's review-and-remediate extension needs an SMT backend: taint is an
∃-dataflow-chain question over branches and joins, and frame conditions are
quantified over all files. Z3's Python bindings are the mature ones, so the
default assumption was that the prover — and therefore the whole harness —
had to be Python. A spike disproved that.

`z3-solver` is published from the Z3 repository itself (first-party TypeScript
bindings, 31 versions tracking upstream, most recent 2026-07-17, one runtime
dependency). Encoding §20's taint policy over a six-step plan with transitive
taint and per-step reachability: the WASM module loads in 85 ms, the negated
policy is `sat` against an unconstrained plan space in 98 ms, `unsat` in 6 ms
once the enforcement constraint is added, and a quantified frame condition over
uninterpreted functions is `unsat` in 16 ms. Counterexample extraction returns a
readable leaking path — `read_pr_file` at step 0, `push_branch` at step 1 with
its argument bound to step 0 — which is what §2.5 requires of a `sat` verdict.

The prover is therefore TypeScript, and so is the transport tier: context
acquisition, the generator loop, rendering and GitHub I/O.

## Consequences

- The verification tier spans two languages *for now*. The artifact verifier
  stays Python (`verify.py`, canonicalization, the diff walk) because its risk is
  `markdown-it` rendering behaviour and a hand-tabulated Unicode table, and its
  seven real defects were canonicalization bugs that would have to be re-derived
  against a different implementation. The plan verifier is TypeScript because its
  risk is reachability reasoning. §2.5 already treats these as different problems
  requiring different tools, so the boundary tracks a real distinction rather
  than cutting across one.
- **The intended end state is one language.** Once everything else is built and
  stable, the artifact verifier is ported to TypeScript in a phased way, and the
  seam closes. The port is deliberately last because it is the only component
  whose correctness is empirical rather than textual: it must be moved against a
  working baseline, not alongside one.
- `policy.json` becomes shared data with two readers in two languages. It was
  already the reviewable security object; now it is also the only thing keeping
  the two verifiers describing the same policy.
- The encoding layer — plan to constraints — is new trusted code, exactly as
  §2.5 warns: the solver's answer is no more trustworthy than the encoding
  behind it. It needs its own adversarial corpus. The spike already surfaced one
  rough edge: model evaluation returned `null` rather than `-1` for a variable
  unconstrained in practice.
- The diff walk stays Python and single *while the verifier is Python*.
  Verification owns it; the TypeScript side consumes its NDJSON output. The
  inverse would have the verifier checking provenance against a walk supplied by
  the code being verified, and two parsers is the split that ac637fe9
  deliberately collapsed. The walk moves with the verifier when the port happens
  — never separately, for the same reason.
- Two test runners (pytest and the TypeScript runner) until the port completes,
  and both run for the whole duration of the port itself, since the Python is the
  oracle. The second runner is not saved until the port finishes.

## The port, when it happens

Recorded here so the plan is not re-invented under pressure. It needs its own ADR
at the time, because the decision to start it depends on conditions that do not
exist yet.

- **Differential oracle, not translation.** Both implementations run against the
  same corpus in CI until no case can distinguish them. The Python is deleted only
  when the corpus cannot tell them apart.
- **Replicate the cases exactly.** `test_verify_adversarial.py` is 486 lines its
  own docstring calls "the living spec of the threat model", where "a case that
  starts passing is a regression in the verifier's safe grammar". Each case must
  be preserved as a case, not paraphrased into a suite.
- **Rejecting is not enough; the reason must match.** A ported case can reject for
  the wrong reason and look green. This is the specification-versus-deduction gap
  §2.5 is about, so the differential harness compares rejection *kinds* — for
  which `rejection_fingerprint` already exists, since the circuit breaker needed
  exactly this normalization.
- **`markdown-it` in TypeScript is a different implementation of the same spec.**
  The seven canonicalization defects — cross-field reference definitions,
  rendered-versus-source secret scanning, GFM autolinks and cross-repo refs,
  heading/blockquote impersonation, U+034F defeating fence matching — must each be
  re-established empirically against it, not assumed to transfer.
- **Phase in the order risk allows**: the Unicode table and fence escaping first
  (pure functions, exhaustively testable), then the markdown allowlist (highest
  risk, needs the oracle most), then the diff walk with the provenance check that
  depends on it.
