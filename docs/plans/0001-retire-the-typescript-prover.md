# Plan: retire the TypeScript prover and consolidate on Python

**Status:** ready to execute. Not started.
**Written:** 2026-08-15. **Author of the decision:** svozza.
**Prerequisite reading:** ADR-0003, ADR-0004, and `docs/findings/0002-real-pr-testbed-results.md`
block F (F2a–F2e). This plan assumes none of it — everything needed is restated below — but
F2e records where an earlier version of this analysis was wrong, which is worth knowing.

---

## 1. The decision, and why the calculus changed

ADR-0003 put the plan prover in TypeScript. Its reasoning was that §20 needed an SMT backend,
that *"Z3's Python bindings are the mature ones, so the default assumption was that the
prover — and therefore the whole harness — had to be Python"*, and that a spike disproved the
assumption by showing `z3-solver` works in TypeScript.

That only mattered because **TypeScript was the destination**. `quality_check.yml:22` states it
outright: the two runners both exist *"for the whole duration of the verifier's eventual port,
since the Python is the differential oracle for it"*. The TS prover was the beachhead for
porting the Python verifier to TypeScript.

**That direction is cancelled. Everything is to be Python.**

So the prover is a detour rather than a destination, and ADR-0003's discarded premise becomes
the argument: Python is the mature host for Z3. Two further consequences:

- **The differential oracle was explicitly a migration guard** —
  `tests/test_plan_gate_differential.py:4` calls it *"the guard ADR-0003 specifies for the
  cutover period"*, comparing *"two implementations in two languages"*. With no cutover there
  is nothing for it to guard.
- **Cross-language N-version redundancy was the strongest remaining argument for keeping the
  TS prover, and it collapses.** Same-language twins share idioms, libraries and the author's
  blind spots; the redundancy's value came precisely from the language boundary.

## 2. The decisive fact: Python already checks everything

This is what makes the work a deletion rather than a rewrite. Every live property the TS prover
proves already has a Python implementation in the same call, `plan_verify.verify_plan`
(`:1047`):

| TS prover | Python twin | file:line |
|---|---|---|
| `checkPlanSchema` | `check_plan_schema` | `plan_verify.py:186` |
| `proveOrdering` | `check_plan_ordering` — documented as *"The Python twin of ts/plan/prove.ts proveOrdering, and semantics must stay identical to it"* | `plan_verify.py:871` |
| `proveFrame` (frame **and** denylist) | `check_plan_containment` — *"frame, denylist, suggest.line provenance, bounding, anchoring"* | `plan_verify.py:591` |
| `proveWriteTargets` | `check_write_class_targets` | `plan_verify.py:478` |
| `proveCardinality` | `check_plan_cardinality` | `plan_verify.py:792` |
| `proveBounds` | folded into `check_plan_containment` ("bounding") | `plan_verify.py:591` |
| `proveTaint` | **none, and none needed** — vacuous, see §6 trap 5 | — |

Python additionally checks `check_plan_markdown` (`:984`) and `check_plan_secrets` (`:1005`),
which have no TS counterpart. **The Python gate is a superset.**

And this is not inferred — `tests/test_plan_gate_differential.py` feeds one plan to both gates
and compares verdicts across **97 collected tests**, all green as of 2026-08-15.

**Therefore: deleting the TS prover removes no property coverage.** It gives up exactly two
things, stated honestly:

1. A second, independently authored implementation of five properties.
2. The in-place SMT option for a future policy that needs symbolic reasoning (ADR-0004's
   "staged grounding"). §5 preserves this cheaply.

## 3. Order of work, with a gate after each step

Do these in order. Each step ends with a check that must pass before the next begins.

### Step 0 — prove the premise before deleting anything

```
npm test                                          # expect 178 pass
.venv/bin/python -m pytest tests/ -q              # expect 2096+ pass
.venv/bin/python -m pytest tests/test_plan_gate_differential.py -q   # expect 97 pass
```

**Gate:** the differential must be green. It is the evidence that Python's coverage is
complete. **If it is red or skipped, STOP** — the premise of this plan is false, and a red
differential means one gate enforces something the other does not. Record the three numbers in
the commit message.

### Step 1 — preserve the Z3 encoding outside the delivery path (recommended, do first)

The encoding is the only artifact here that would be expensive to recreate. Move it somewhere
it survives without shipping:

- Option A (lightest): delete it, and record the commit SHA in the superseding ADR so it can be
  recovered from history. `ts/plan/prove.ts` at the SHA recorded in §7.
- Option B (recommended if staged grounding is on the roadmap): port `proveTaint`'s transitive
  encoding to a Python module under `spikes/` or `experiments/`, using `z3-solver` from PyPI,
  **not** imported by anything in `src/smtithy/`. Carry its test corpus with it, including the
  synthetic `bindings` cases and the write-class-source case from §6 trap 5.

**Do not** port `proveFrame`'s encoding into the delivery path — see §6 trap 6.

**Gate:** whichever option, `grep -rn "z3" src/smtithy/` returns nothing.

### Step 2 — remove the prover invocation from the executor

In `src/smtithy/execute_plan.py`:

- Delete `run_prover` (`:219`), `DEFAULT_PROVER` (`:92`), `PROVER_TIMEOUT_SECONDS` (`:100`), the
  `--prover` argument (`:63`), and the call site.
- Keep `verify_plan`. It is doing the work.
- The three-way exit semantics (0 proved / 1 disproved / 2 nothing proved) exist **only** because
  the prover is an out-of-process call into another language. Do not preserve them. The hazard
  they guard — *"a crashed prover cannot arrive here as a disproof"* — cannot occur in-process.

**Gate:** `pytest tests/ -q` green except `test_plan_gate_differential.py`, which will now fail
or skip. That is expected; it is removed in step 5.

### Step 3 — strip Node from CI

- `.github/workflows/ai-pr-fix.yml`: remove `Set up Node` and `Install and build the prover`
  from **both** `execute` (`:527`, `:533`) and `stack` (`:647`, `:653`).
- `.github/workflows/quality_check.yml`: this needs structural change, not a line edit. It has
  three jobs (`test_verifier:53`, `test_prover:72`, `test_gate_differential:97`) and a header
  comment whose entire rationale is the two-runner split. Delete `test_prover` and
  `test_gate_differential`, and rewrite the header comment — do not leave it describing a
  seam that no longer exists.

**Gate:** `grep -rn "Set up Node\|npm ci\|z3-solver" .github/` returns nothing.

### Step 4 — delete the TypeScript

```
ts/                      8 files, 3296 lines (incl. 178 tests)
package.json             build = tsc, test = tsc && node --test
package-lock.json
tsconfig.json
node_modules/            (already gitignored)
dist/                    (already gitignored — see .gitignore:6)
```

Remove the now-dead `.gitignore` entries for `dist/` and `node_modules/`.

**Gate:** `find . -name "*.ts" -not -path "./node_modules/*" | wc -l` is 0.

### Step 5 — retire the differential oracle

Delete `tests/test_plan_gate_differential.py`. **But inherit its purpose deliberately.** It
exists because of a real defect it caught, recorded at `quality_check.yml:32`: *"plan.ordering
came to be read by the prover and by no Python code at all with a green suite"* — a policy key
enforced by one gate and invisible to the other.

In an all-Python world the equivalent failure is a policy key **no** checker reads. That guard
already exists (`plan_verify.check_plan_policy_keys:128`, "Refuse a plan policy carrying a key
no reader consults, or missing one"). **Verify it is actually asserted by a test**, and if the
coverage is weaker than the differential's was, strengthen it in this step. Do not delete the
oracle and leave the class of bug unguarded.

**Gate:** a test fails if a key is added to `policy.json`'s `plan` block that nothing reads.

### Step 6 — supersede ADR-0003 and clean the citations

Write the superseding ADR: the direction reversed, Python is the destination, the prover's
properties were already implemented in Python, the differential confirmed it across 97 cases,
and the encoding is preserved per step 1. Record the SHA that last contained `ts/plan/prove.ts`.

Then fix the stale references. These cite ADR-0003 or the prover's TypeScript-ness and will be
wrong:

```
.github/workflows/quality_check.yml      (header comment, done in step 3)
src/smtithy/plan_verify.py               ("the Python twin of ts/plan/prove.ts …")
docs/adr/0003-*.md                        (mark superseded)
docs/adr/0004-*.md                        (its consequences reference the prover's encoding)
spikes/z3-typescript/README.md            (leave as a historical spike; note the outcome)
docs/findings/0002-*.md                   (block F — add a pointer, do not rewrite history)
```

`docs/findings/0002-prover-attack-suite.py` targets the TS CLI and stops being runnable. Either
port it to drive `verify_plan` directly, or mark it historical with the SHA it last ran against.

**Gate:** `grep -rln "prover is TypeScript\|prove-cli" --include=*.py --include=*.yml --include=*.md .`
returns only intentionally historical documents.

### Step 7 — prove it end to end on a real pull request

Unit tests cannot show the fix lane still delivers. The red-team testbed is being kept for
exactly this (`svozza/smtithy-redteam`, see §8): open a PR with a planted defect, let the review
post, then `/fix 1` and confirm a suggestion is delivered and `execute` succeeds with no Node in
the job.

**Gate:** one delivered suggestion, and the `execute` job's step list contains no Node step.

## 4. Definition of done

- No `.ts` file, no `package.json`, no `node_modules`, no Node step in any workflow.
- `pytest tests/ -q` green at 2096 or more.
- ADR-0003 marked superseded; no comment claims the prover is TypeScript.
- A key added to `policy.json`'s `plan` block with no reader fails a test.
- One live `/fix` delivered a suggestion after the change.
- The Z3 encoding is either recoverable by a recorded SHA or living outside `src/smtithy/`.

## 5. The open question this plan deliberately does not settle

Whether smtithy ever wants an SMT solver again depends on whether **staged grounding** happens
(ADR-0004's `generate → verify symbolically → resolve unprivileged → re-verify ground → execute`,
marked "Not built now"). That is the one candidate property that iteration handles badly, because
the values do not exist yet at verification time.

Two honest caveats, both from the F2e review:

- Symbolic verification does **not** require SMT. Abstract interpretation, bounded enumeration,
  SAT and constraint propagation are all options. SMT becomes compelling only with rich
  constraints, branching path conditions, or large value domains.
- So staged grounding is the strongest *available* argument for a solver, not a proof that one
  is necessary.

Step 1 Option B keeps the option alive for the price of one unshipped module. Do not let this
question block the deletion: nothing in §3 is hard to reverse, and the encoding is in git.

## 6. Traps — every one of these cost something already

1. **`execute` is NOT the `contents: write` job.** `execute` (`:490`) holds `contents: read` +
   `pull-requests: write`; **`stack` (`:597`) is the only `contents: write` job**, and
   `ai-pr-fix.yml:31` says so. An earlier analysis read the permissions at `:597` and attributed
   them to the job above. Both jobs build the prover, so both need step 3.
2. **Comments citing ADR-0003 are load-bearing prose, not decoration.** `quality_check.yml`'s
   header explains why two runners exist; leaving it in place after deleting one runner leaves a
   file that lies about itself.
3. **Do not port `run_prover`'s exit semantics.** They encode a subprocess hazard that ceases to
   exist. Porting them would preserve complexity for a risk that is gone.
4. **This codebase's registries are hand-kept allowlists that fail loudly, by design.** Several
   tests pin scenario census, graded lines, and policy keys. When one fails, extend it — the
   discipline is what catches real errors. During the eval work on 2026-08-15 exactly this
   caught a `line_in` pointing at a blank line.
5. **`proveTaint` is vacuous, and NOT sealed.** It never fires on the shipped policy (no
   `read_pr_file` kind; `argument_forms` is `["literal"]`). But declaring the source kind as
   `write_class: true` makes it fire with no bindings at all, because the source step is then
   itself a write-class step — reproduced 2026-08-15:
   `taint: VIOLATED (109.1ms) / 0: read_pr_file (rd) tainted <- the leak`. If the encoding is
   preserved in step 1, carry that case. Do not treat the taint policy as working security.
6. **Do not port `proveFrame`'s encoding.** It declares quantified uninterpreted functions and
   then manually closes and enumerates the domain — more trusted encoding code than a set
   membership check requires, and precisely the risk ADR-0003 names about itself. Python already
   does it in one line (`if path not in changed: raise`).
7. **`test_plan_gate_differential` is load-sensitive.** Its prover subprocess has a 120s
   timeout; under a fully parallel suite it has produced a `TimeoutExpired` that passes on
   re-run. A timeout there is contention, not a regression — check before diagnosing.
8. **`requirements.txt` is hash-pinned** (`uv pip compile --generate-hashes`), so adding
   `z3-solver` for step 1 Option B means regenerating with hashes, not appending a line.
9. **`dist/` and `infra/` are gitignored deliberately** (`.gitignore:6`, `:21-25`). `infra/` is
   unrelated to this work but is the reason a cited setup file appears missing.

## 7. Facts to re-verify at the start, in case the tree has moved

Line numbers below were correct on 2026-08-15 at commit `27b0166`. Re-check them rather than
trusting them:

- `execute_plan.py`: `run_prover:219`, `DEFAULT_PROVER:92`, `PROVER_TIMEOUT_SECONDS:100`
- `plan_verify.py`: `verify_plan:1047` and the check list at `:1064-1071`
- `ai-pr-fix.yml`: `execute:484`, `stack:592`, Node at `:527`/`:533` and `:647`/`:653`
- `quality_check.yml`: jobs at `:53`, `:72`, `:97`
- Test counts: 178 TS, 2096 Python, 97 differential

## 8. What NOT to do

- **Do not reimplement the six checks.** They exist in `plan_verify.py`. This is a deletion.
- **Do not delete the differential test before step 5**, and not at all until its purpose is
  demonstrably inherited (§3 step 5).
- **Do not tear down the red-team testbed.** `svozza/smtithy-redteam` and the
  `smtithy-redteam-oidc-role` stack are being kept deliberately so the adversarial matrix can be
  re-run against the ported harness — step 7 needs it. Note the standing cost: while it exists,
  any workflow run in that repository can assume a Bedrock-invoke role.
- **Do not treat a green unit suite as proof the fix lane works.** Step 7 exists because
  delivery is a property of the workflow, not of the code.
