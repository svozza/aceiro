# Prompt: remediate the findings in CODE_REVIEW_aa6d206.md

Copy everything below the line into a fresh agent session in this repo.

---

You are fixing security and correctness defects in **smtithy**, a harness for AI agents that
are never trusted, only verified. Read `CONTEXT.md` first — it is the project's controlled
vocabulary, and using the wrong word for a component here is a real error, not a style
preference. Then read `README.md` and the ADRs in `docs/adr/` that bear on whatever you touch.

## Your input

`CODE_REVIEW_aa6d206.md` in the repo root. It is the output of a two-engine review (a Claude
reviewer and a GPT/codex reviewer, independently) whose 104 candidate findings were then
adversarially adjudicated. It contains **37 primary findings + 41 minor findings**, plus a
list of 10 refuted candidates.

Each finding carries: locations, what is wrong, a concrete failure scenario, a fix direction,
twin files to keep in sync, and **adjudicator notes for the fixer**. Read the adjudicator
notes before you write anything — they frequently say *don't* fix it the obvious way and
explain why (several name a test that already pins the behaviour you'd otherwise break).

Metadata you should trust and act on:

- **`CONFIRMED`** — an adjudicator reproduced the reasoning against the real code and judged
  the failure scenario constructible. Fix these.
- **`PLAUSIBLE`** — the concern is real but reachability or impact was not established.
  Verify it yourself before fixing. If you conclude it does not hold, say so and skip it.
- **Found by claude + gpt** — both engines found it independently. Strongest signal.
- Two findings (`g-20`, `g-45`) got **no adjudicator verdict** and are flagged inline as
  unverified. Re-check those from scratch before acting.
- The **refuted** section at the end exists so you do not re-raise those claims. If you think
  a refutation is wrong, argue it explicitly rather than silently fixing the thing.

## Scope and order

Work in the order the report is ranked — it is already sorted by trust-boundary breach, then
reachability by attacker-controlled PR content, then fail-open behaviour, then correctness.
Do **not** attempt all 78 in one pass.

**Start with these, and stop for review before going further:**

1. Finding 1 — secret scan blind to invisible code points (`verify.py:217`)
2. Finding 2 — link allowlist defeated by dot segments (`verify.py:228`)
3. Finding 3 — suggestion placement untied to anchored bytes (`plan_verify.py:252`)
4. Finding 4 — ordering policy enforced by no Python code (`plan_verify.py:363`)
5. Finding 5 — TS frame proof reports denylist hits it never asserts (`ts/plan/prove.ts:238`)

These five are all high-severity, all CONFIRMED, and 1–2 and 4–5 share root causes with
several later findings, so fixing them collapses part of the backlog. Findings 1 and 2 in
particular are the two places where attacker-controlled content reaches a posted comment.

Then report back with what you changed and what you found, and wait. Do not continue into the
medium/low findings without being told to.

## Rules

**Read before you write.** Every finding cites specific lines. Open them. The report is
evidence, not instruction — if a finding's premise does not hold against the code in front of
you, say so and skip it rather than writing a fix for a defect that is not there. You are the
last check before these become commits.

**One finding per commit.** Message body states the defect and the invariant now enforced, not
just the change. Match the existing log's voice — read `git log` first; commits here read like
`fix(verify): …` / `feat(plan): …` with a declarative subject that names the property.
**Never add `Co-Authored-By` lines.**

**Every fix needs a test that fails before it and passes after.** Write the test first and
watch it fail — a fix whose test passes on unpatched code is not a fix. The report names the
test to add for each finding; several adjudicator notes name the exact existing test file and
class to extend. `tests/test_verify_adversarial.py` is described by its own docstring as the
living spec of the threat model, where a case that starts passing is a regression — new
canonicalization cases belong there.

**Keep the twins in sync.** `src/smtithy/plan_verify.py` and `ts/plan/{schema,prove,policy}.ts`
are meant to be behavioural twins over the shared `src/smtithy/policy.json`. A Python-only fix
to a shared property *creates* the divergence the report is about. Where a finding says "fix in
TypeScript, not Python, because `verify.py` is the oracle", follow it — that is deliberate, per
ADR-0003. Read "On the two-language seam" in the report before touching either gate.

**Do not weaken a gate to make a test pass.** These are fail-closed by design. If a fix makes
something legitimate fail, that is a finding of its own — report it, do not relax the check.

**Policy is data.** `policy.json` is hashed and reviewable. Some fixes are policy changes, not
code changes (the report says which). Do not hardcode into Python or TypeScript what belongs
in the policy object, and do not silently change shipped policy defaults — an empty allowlist
that ships empty is a deliberate fail-closed default (ADR-0010).

**Stay in scope.** Fix the finding in front of you. No drive-by refactors, no reformatting, no
renaming. If you spot something new, note it for the report rather than fixing it.

## Verification

Run both suites — this repo has two deliberately (ADR-0003):

```
python -m pytest tests/ -q          # artifact verifier, Python
npm test                            # plan prover, TypeScript (tsc && node --test)
npm run typecheck
```

Python dependencies are **not** installed in this working copy and there is no `.venv`. Create
one and install from the hash-pinned lockfiles before you start:

```
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

`.venv/` is already gitignored. Both suites must be green before you report done, and say so
with the actual output. If a pre-existing test fails for reasons unrelated to your change, say
that explicitly rather than folding it into your result.

## Documentation

`docs/architecture.html` is a living document: if you change a component's behaviour in a way
the diagram depicts, update it in the same commit as the code.

If a fix contradicts a documented decision, or establishes a new one, that needs an ADR in
`docs/adr/` — do not silently diverge from an ADR. Where a finding is a *divergence* between
code and an ADR, the ADR is usually right and the code is wrong; check which before choosing.

## Do not

- Do not push. Commit locally and stop — pushing is the user's call.
- Do not open a PR, create branches beyond what you need, or touch git remotes.
- Do not fix the refuted findings.
- Do not delete or rewrite `CODE_REVIEW_aa6d206.md`. If you want to record outcomes, add a
  status column or a separate file.

## Report back

For each finding you touched: what you changed, the test that now locks it, and the verdict you
reached if it was `PLAUSIBLE` or unverified. For each you skipped: why. Then stop.
