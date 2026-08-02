# Prompt: remediate the findings in CODE_REVIEW_3bc6c69.md

Copy everything below the line into a fresh agent session in this repo.

---

You are fixing security and correctness defects in **smtithy**, a harness for AI agents that
are never trusted, only verified. Read `CONTEXT.md` first — it is the project's controlled
vocabulary, and using the wrong word for a component here is a real error, not a style
preference. Then read `README.md` and the ADRs in `docs/adr/` that bear on whatever you touch.

## Your input

`CODE_REVIEW_3bc6c69.md` in the repo root. It is the output of a two-engine review (a Claude
reviewer and a GPT/codex reviewer, independently) over the 47 commits `aa6d206..3bc6c69` — the
remediation programme that followed the previous review, plus the plan executor and prover CLI.
Its 97 candidate findings were adversarially adjudicated. It contains **26 primary findings +
30 minor findings**, plus a list of 11 refuted candidates.

**The staged first batch is already done** — findings 7, 1, 2, 3, 4, 5 and 6 are fixed and
committed locally in `d0e649e..f40b5aa`, each marked **FIXED** in the report with its commit.
Do not redo them. Read those seven commits first (`git log -p d0e649e..f40b5aa`): they establish
the voice, the test-first discipline, and two premise corrections this report now records
inline.

Each finding carries: locations, what is wrong, a concrete failure scenario, a fix direction,
twin files to keep in sync, and **adjudicator notes for the fixer**. Read the adjudicator notes
before you write anything — they frequently say *don't* fix it the obvious way and explain why.
In this round they are unusually load-bearing: for several findings the adjudicator **tested
both candidate fix directions and only one works** (finding 1 above all), and for others the
suggested fix is impossible as stated.

Metadata you should trust and act on:

- **`CONFIRMED`** — an adjudicator reproduced the reasoning against the real code and judged the
  failure scenario constructible. Fix these.
- **`PLAUSIBLE`** — the concern is real but reachability or impact was not established. Verify
  it yourself before fixing. If you conclude it does not hold, say so and skip it.
- **Found by claude + gpt** — both engines found it independently. Strongest signal.
- **Two findings were partly wrong in their own reasoning, and both are marked inline.** Finding
  6's `needs:`-removal claim fails closed when tested; only the environment rename is real.
  Finding 2's stated mechanism (that the cases reject *on* the anchor reason) is backwards, and
  that changed what its test had to be. Where an entry records a correction, trust the correction
  over the original claim — and treat it as a warning that the same entry's other claims deserve
  checking.
- **Area 5** (`post.py`, `github_api.py`, `diff_map.py`) was reviewed by **one engine, not two**.
  Its findings are under-corroborated — the four-item `post.py` concurrency cluster in the minor
  list especially, where the adjudicator judged every one PLAUSIBLE and noted the shipped
  per-PR concurrency group already covers the scenarios. Re-derive those before fixing. Area 7's
  second engine landed and found four confirmed defects the first pass missed, which is the
  measure of what single-engine coverage costs.
- The **refuted** section at the end exists so you do not re-raise those claims. If you think a
  refutation is wrong, argue it explicitly rather than silently fixing the thing.

## Scope and order

Work in the order the report is ranked — it is already sorted by trust-boundary breach, then
reachability by attacker-controlled PR content, then fail-open behaviour, then correctness.
Do **not** attempt all 55 remaining in one pass.

**Start with these, and stop for review before going further:**

1. **Finding 16** — the plan lane's missing quarantine symlink assertion. It was blocked on
   finding 7's import and is now unblocked. The fix is *structural*: move the assertion into
   `drive_session` so both lanes inherit it, and remove the `cc_loop` call site in the same
   change so the review lane does not assert twice.
2. **Finding 17 + 17b** — the three unguarded exits that discard a verified artifact
   (`api_error`, wall clock, and the `submit_failed`/`submit_rejected` log that escapes before
   `spend()`). One change covers the first two; 17b is a separate helper. **The wall-clock path
   is pinned by no test at all**, so it needs tests for both halves of its behaviour.
3. **Finding 11** — the shared "scanned representations" helper. It collapses five reported
   findings (`c1-3`, `c4-2`, `c4-5`, `g4-4`, `g4-5`) into one change and closes the plan gate's
   secret-scan gap.
4. **Finding 12** — GFM footnotes bypassing the checked grammar. It shares
   `check_markdown_field`'s "evaluate on non-code lines" scaffolding with the already-landed
   finding 3, so read `3fa4589` before writing it.
5. **Finding 8 + 9** — the branch-relation rule and the unwired `head_branch`. Both are
   twin fixes that must land in both gates plus a differential case, and 9's notes name a
   hoist that must **not** be done.

Then report back with what you changed and what you found, and wait. Do not continue into the
low findings without being told to.

## Rules

**Reproduce before fixing.** Every finding cites specific lines and states a concrete failure
scenario with inputs. Run it. The report is evidence, not instruction — if a premise does not
hold against the code in front of you, say so and skip it rather than writing a fix for a defect
that is not there. You are the last check before these become commits.

**Every fix needs a test that fails before it and passes after. Write the test first and watch
it fail.** A fix whose test passes on unpatched code is not a fix. This round's report exists
because the previous round's tests were written by the same agent as the fixes, and several
passed on unpatched code — do not repeat that. Where a finding says the test must assert
something specific (finding 7: assert the `run_failed` **record**, not the return code; finding
16 / `g8-5`: assert the **order**, not the outcome), that instruction is the finding.

**One finding per commit**, with a declarative subject naming the property now enforced — read
`git log` first and match its voice (`fix(verify): …`, `feat(plan): …`). The body states the
defect and the invariant, not just the change. **Never add `Co-Authored-By` lines.**

**Keep the twins in sync.** `src/smtithy/plan_verify.py` and `ts/plan/{schema,prove,policy}.ts`
are behavioural twins over the shared `src/smtithy/policy.json`. A Python-only fix to a shared
property *creates* the divergence half this report is about. Findings 8, 9, 11, 12, 20, 21 and
24 all name twins explicitly. Where a finding says fix it on one side only — finding 1 is
TS-only, finding 10 is correctly Python-only because the prover has no file access — that is
deliberate, and the reason is in the entry.

**Do not weaken a gate to make a test pass.** These are fail-closed by design. If a fix makes
something legitimate fail, that is a finding of its own — report it, do not relax the check.
Note the inverse appears in this round: finding 1 is a gate that fails closed *too much*, and
the fix is still to make it correct rather than to loosen it.

**Policy is data.** `policy.json` is hashed and reviewable. Finding 14 is a policy change, not
a code change. Do not hardcode into Python or TypeScript what belongs in the policy object, and
do not silently change shipped defaults — an empty allowlist that ships empty is a deliberate
fail-closed default (ADR-0010).

**Prefer structural fixes to local guards.** Finding 16's fix is to move the assertion into
`drive_session` so both lanes inherit it, rather than adding a second call site. This round's
report is largely a catalogue of fixes that held only at the call site they were written for —
when you introduce a shared helper, grep every call site and route them all through it, and say
in the commit that you did.

**Comments describe the API or explain what the code cannot say itself.** No editorialising, no
narrating what a change fixed, no before/after history. Several docstrings in this range now
assert sharing that does not hold (`rendered_markdown`, `escape_fence`, `link_destinations`) —
correct those to describe what the code does, and where a finding says a docstring reads as a
design allowance for an unwired caller (finding 9), tighten it.

**Stay in scope.** Fix the finding in front of you. No drive-by refactors, no reformatting, no
renaming. If you spot something new, note it for the report rather than fixing it.

## Verification

Run all four — this repo has two suites deliberately (ADR-0003), and the differential needs a
fresh build:

```
.venv/bin/python -m pytest tests/ -q                                # 1124 passed at 3bc6c69
npm test && npm run typecheck                                       # 145 tests, 145 pass
npm run build                                                       # required BEFORE the differential runs
.venv/bin/python -m pytest tests/test_plan_gate_differential.py -q  # 26 passed at 3bc6c69
```

All four are green at `3bc6c69`. `.venv` exists. **Do not add dependencies to the hash-pinned
lockfiles** — finding 12's notes explain why the obvious footnote fix (a markdown-it plugin) is
not available to you.

`npm run build` before the differential is not optional: finding `c3-11` shows a stale `dist/`
makes the whole 26-case corpus a green test of the previous prover, so a differential run
without a rebuild is not evidence.

If a pre-existing test fails for reasons unrelated to your change, say that explicitly rather
than folding it into your result.

## Documentation

`docs/architecture.html` is a living document: if you change a component's behaviour in a way
the diagram depicts, update it in the same commit as the code. **Its per-node test counts move
with the code** — if your change adds tests, the counts change, and `execute_plan` is currently
marked *delivery pending*, so check whether your change alters that status.

If a fix contradicts a documented decision, or establishes a new one, that needs an ADR in
`docs/adr/` — do not silently diverge from an ADR. Where a finding is a divergence between code
and an ADR, **the ADR is usually right and the code is wrong**; every such divergence this round
resolved that way. But findings 15 and 21 are the exception in each direction: finding 15 has an
ADR making a claim about a test that is not true, so the ADR sentence needs correcting in the
same change; finding 21 has ADR-0009 and `prompts/ai-pr-plan.md` contradicting each other, and
that disagreement must be resolved before the predicate is written.

## Do not

- **Do not push.** Commit locally and stop — pushing is the user's call.
- Do not open a PR, create branches beyond what you need, or touch git remotes.
- Do not fix the refuted findings.
- Do not modify `CODE_REVIEW_aa6d206.md`, `FIX_PROMPT.md`, or `CODE_REVIEW_3bc6c69.md` — the
  first two are the record of the previous round and the third is your input. If you want to
  record outcomes, add a status column or a separate file.

## Report back

For each finding you touched: what you changed, the test that now locks it and the evidence you
watched it fail first, and the verdict you reached if it was `PLAUSIBLE` or partly refuted. For
each you skipped: why. Then stop.
