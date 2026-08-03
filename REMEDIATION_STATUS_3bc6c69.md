# Remediation status: CODE_REVIEW_3bc6c69.md

The report is the round's input and is not modified (its own contract forbids it), so
findings fixed after the first batch are not marked FIXED in it. This file is the status
record. **The report is evidence, not a status document — read this first.**

Branch `feat/plan-executor`. Baseline `3bc6c69`.

## Primary findings: 23 of 26 fixed

| # | Finding (short) | Commit |
|---|---|---|
| 1 | proveFrame rejects a legal plan on a denylisted PR file | `972ad09` |
| 2 | Rejecting differential cases confounded by a missing anchor file | `d9f7601` |
| 3 | Entity-encoded bidi bypasses the canonicality gate | `3fa4589` |
| 4 | Invisible-split credentials survive redaction | `a7c68fc` |
| 5 | Fence tags with attributes survive neutralisation | `c79bfdb` |
| 6 | Gate-defeating environment rename | `9825e39` |
| 7 | plan_loop's fail-closed handler calls an unimported `fail` | `d0e649e` |
| 8 | push_branch and open_pr may target different branches | `dc36a1a` |
| 9 | The refusal to push to the contributor's branch is unreachable | `6b51b51` |
| 10 | A suggestion's anchor may end mid-line | `ef848b7` |
| 11 | Plan secret scan and rendered corpus stop at text nodes | `1906244` |
| 12 | GFM footnotes bypass the checked grammar | `3ab9ce5` |
| 13 | Unchecked bundle metadata forges the model stamp | `b3fda1c` |
| 14 | open_pr.title escapes the canonical-text gate | `9ae4534` |
| 15 | Policy-coverage assertion cannot detect an unenforced key | `759adc9` |
| 16 | Quarantine symlink assertion missing on the plan lane | `e68e828` |
| 17 | An accepted artifact is discarded on abnormal session end | `04fd1a5` |
| 17b | A failed transcript write escapes the submission handler | `d8ca200` |
| 18 | Rejection messages echo the offending value to the job log | `8df2d5f` |
| 19 | strip_quoted defeated by one reporting word in the payload | `8f66878` |
| 20 | A lone surrogate crashes the Python gate | `0cd6fdc` |
| 21 | Multiple same-file suggestions admitted (resolved by user decision: refuse) | `4d80318` |
| 22 | prove-cli exits 1 for a malformed command line or early crash | `4729106` |
| 23 | parsePlanJson's missing-`source` path is dead code | `c3ba722` |
| 24 | Loader validates patterns without the enforcer's `u` flag (+ minor `g2-5`) | `e2528ab` |

That table lists 25 rows for 26 findings because 17 and 17b are counted as one in the
report's total. **All 26 primary findings are now fixed.**

## Premise corrections found while fixing

Recorded because the report states them otherwise, and a later reader should not
re-derive them:

- **Finding 6** — the `needs:`-removal claim fails closed when tested; only the
  environment rename is real. (Marked inline in the report.)
- **Finding 2** — the stated mechanism (cases reject *on* the anchor reason) is
  backwards. (Marked inline in the report.)
- **Finding 23** — the report says the `TypeError` surfaces through `prove-cli` as
  exit 1, "compounding finding 22". **Measured exit is 2**: `parsePlanJson` is inside
  `main()`'s inner try, so it always failed closed. The finding's own premise (that
  `source` is `undefined` on Node 20) *was* confirmed, on a real Node 20.20.2 binary —
  the report notes nobody had run one.
- **Finding 24** — the report frames the asymmetry as TS-only. It runs **both ways**:
  `\p{L}` compiles in JS (plain and with `u`) and is a `PatternError` to Python's `re`;
  `a{,3}` is the reverse. So neither gate can mirror the other's verdict, and each
  refuses what its own enforcer cannot compile.

## Findings resolved by user decision

- **Finding 21** — ADR-0009 vs `prompts/ai-pr-plan.md` contradiction. Decision:
  **refuse — one `suggest` step per file**, enforced in cardinality in both gates, prompt
  corrected in the same commit. ADR-0009's "one suggestion per file per finding"
  consequence stands as written. Settled; do not reopen.
- **Finding 24** — where the Python-side spec check lives. Decision: **a shared eager
  sweep in `verify.py`**, beside `SCALAR_KEYS` (already the declared twin of
  `policy.ts`'s), called from both `check_schema` and `check_plan_schema`. A real Python
  policy loader mirroring `ts/plan/policy.ts` was considered and deferred — it overlaps
  several open minors (`g1-6`, `c1-5`, `g4-7`, `c4-6`) and would want its own ADR.

## Minor findings

**Not started.** 30 in the report, less those merged into landed primaries:

- `c3-4` → primary 15; `g4-4`/`g4-5`/`c4-2` → primary 11; `c3-10` → primary 9;
  `c6-5`/`g6-3` → primary 21; `g2-5` → primary 24 (landed with it in `e2528ab`);
  `g1-6` → **subsumed by primary 24's eager sweep** in `e2528ab`.
- `c6-1` was already recorded as largely superseded; its message-quality half is
  primary 22, landed in `4729106`.

Carried guidance for whoever takes them:

- The four-item `post.py` concurrency cluster (`g5-1`, `g5-3`, `g5-4`, `g5-5`) is the
  least-corroborated group in the report: one engine, every item PLAUSIBLE, and the
  shipped per-PR concurrency group already covers the scenarios. Expect to close most.
- `c7-6`/`g7-4` (missing blob `size`): cheap hardening at most; neither engine could
  construct a real API response with a blob entry lacking `size`. Do **not** reach for
  `git fetch --filter` — it would hand the generator a quarantine missing blobs.

## Scope boundary carried forward (not a defect)

`decide_delivery`'s multi-file suggestion refusal is the **only** automated guard against
a multi-file all-suggest plan. `verify_plan` accepts one: cardinality bounds suggestions
per file, and containment has no cross-step check. Stated in
`src/smtithy/evals/plan_scenarios/plan_multi_file_fix/expect.json`'s honesty note, and a
test asserts the word "VERIFIES" appears there — that test caught a previous session
weakening the note. Do not soften it. Moving the guard needs its own decision.

## Verification at HEAD

All four green, from a clean `dist/`:

```
.venv/bin/python -m pytest tests/ -q                                # 1271 passed
npm test && npm run typecheck                                       # 163 pass, typecheck clean
npm run build                                                       # required BEFORE the differential
.venv/bin/python -m pytest tests/test_plan_gate_differential.py -q  # 37 passed
```

`docs/architecture.html` per-node counts move with these: 1271 pytest / 163 node,
`execute_plan` node 42.
