# Spike: is z3-solver (WASM) usable for smtithy's plan prover?

Answered yes. See ADR-0003. Kept because the numbers are the evidence for that
decision, and because the encoding in `taint.mjs` is the starting point for the
real plan prover.

## Running it

```bash
npm install z3-solver@5.0.0
node taint.mjs
```

## What it encodes

§20's taint policy over a bounded plan: six steps, each with a tool, a
reachability bit, a taint bit, and a backward argument binding to an earlier
step's output. Taint propagates transitively — a step is tainted if it reads PR
content or binds an argument to a tainted step. The policy is asserted
**negated**, so `unsat` means no leaking path exists on any path, and `sat`
returns one.

Three checks:

1. **Unconstrained plan space** — expect `sat`. Proves the encoding can express
   a violation at all; an encoding that is accidentally unsatisfiable would
   report `unsat` for every plan and approve everything.
2. **With enforcement** — expect `unsat`. Write-class steps may not be tainted.
3. **Quantified frame condition** — expect `unsat`. `ForAll`/`Exists` over
   uninterpreted functions: every plan-modified file is a PR-touched file.

## Results (node v24.16.0, z3-solver 5.0.0, 2026-07-29)

| Measurement | Value |
| --- | --- |
| WASM module load | 85 ms |
| Taint, unconstrained | `sat`, 98 ms |
| Taint, with enforcement | `unsat`, 6 ms |
| Frame condition, quantified | `unsat`, 16 ms |

Counterexample from check 1, which is the audit-log artifact §2.5 asks for:

```
step 0: read_pr_file   reachable, tainted
step 1: push_branch    reachable, tainted, argSrc=0   <- the leak
```

## Known rough edge

Model evaluation returned `null` rather than `-1` for `argsrc_0`, a variable
unconstrained in practice. Cosmetic here, but it is the reason the encoding
layer needs its own tests: a plan-to-constraints translation is new trusted
code, and the solver's answer is only as good as the encoding behind it.

## What this spike does NOT answer

Whether the artifact verifier should move to TypeScript. That risk is
`markdown-it` rendering behaviour and the Unicode default-ignorable table, which
has nothing to do with solver bindings. ADR-0003 keeps it in Python.
