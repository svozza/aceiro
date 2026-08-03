# Remediation status: CODE_REVIEW_3bc6c69.md

The report is the round's input and is not modified (its own contract forbids it), so
findings fixed after the first batch are not marked FIXED in it. This file is the status
record. **The report is evidence, not a status document — read this first.**

Branch `feat/plan-executor`. Baseline `3bc6c69`.

## Primary findings: all 26 fixed

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
| 23 | parsePlanJson's missing-`source` path is dead code | `c3ba722`, then `f9467dd` |
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

- **Finding 23** — repair the guard, or delete it as dead code (the adjudicator offered
  both). First fixed as a repair, on the reasoning that `engines` is advisory and Node 20
  could therefore still run the prover. Decision: **Node 20 is deprecated and out of
  scope**, so the branch is dead on every supported runtime and deletion is the honest
  change. The no-source-text case is now refused by the integer-lexeme predicate itself
  (`String(undefined)` is not a lexeme) rather than by a guard nothing can reach, so the
  fail-closed direction survives the deletion without inert scaffolding.
- **Finding 21** — ADR-0009 vs `prompts/ai-pr-plan.md` contradiction. Decision:
  **refuse — one `suggest` step per file**, enforced in cardinality in both gates, prompt
  corrected in the same commit. ADR-0009's "one suggestion per file per finding"
  consequence stands as written. Settled; do not reopen.
- **Finding 24** — where the Python-side spec check lives. Decision: **a shared eager
  sweep in `verify.py`**, beside `SCALAR_KEYS` (already the declared twin of
  `policy.ts`'s), called from both `check_schema` and `check_plan_schema`. A real Python
  policy loader mirroring `ts/plan/policy.ts` was considered and deferred — it overlaps
  several open minors (`g1-6`, `c1-5`, `g4-7`, `c4-6`) and would want its own ADR.

## Minor findings: all 30 resolved

Every entry is fixed, closed with a reason, or subsumed. 12 landed as fixes, 7 as
test-integrity commits, 4 recorded as verified-not-a-defect, 7 subsumed by primaries.

### Fixed

| id | what changed | commit |
|---|---|---|
| `c8-1` + `g8-4` | nested `findings_any`/`steps_any` keys validated (was **high as raised**) | `5a324dd` |
| `g8-2` | BASE reached by path-bearing fields with real containment, not substring | `7997e14` |
| `c4-6` | a top-level field reaches a reader or the policy-error path | `71f2db4` |
| `g4-7` | `ARRAY_KEYS` for the findings array's own keys | `78f097d` |
| `g4-6` | generator contract derived from the policy | `49e1300` |
| `g5-6` | a marker that cannot match its own comment is refused | `17f2569` |
| `c7-4` | a path with a space read from a header naming one path | `1b7c45b` |
| `c1-6` + `g1-3` | a fixless write chain refused in both gates | `1f9934d` |
| `c7-3` | the capture cannot mask the failure it is evidence of; 6 encodings | `9a03193` |
| `c7-6` + `g7-4` | an unsized blob is refused (cheap hardening, no measured bypass) | `7386782` |
| `g7-3` | a list at the page limit cannot be shown complete | `5979cd2` |
| `c1-5` | plan-policy keys allowlisted, both directions | `f4af2a5` |

### Test integrity

| id | what changed | commit |
|---|---|---|
| `g8-1` | the trusted checkout is asserted to hold trusted code | `c497d3e` |
| `g8-7` | every action and install pinned, in every workflow | `db860f9` |
| `g3-2` | a rejecting case pinned to its reason, not a boolean | `71e89b1` |
| `c3-8` | `max_steps` bounded in both directions | `ddfa80e` |
| `c3-11` + `g3-6` | a stale build fails; an absent one skips | `c2d924a` |
| `c3-12` | a case rejects for its own reason and no other | `9206f23` |
| `g3-1` + `g3-4` | the coverage census, enforced | `55cbbc6` |

### Closed: verified, not fixing

- **`g5-1`** — REFUTED as stated. A lost write response does not "skip" the post-write
  drift gate: `api_request` raises, so the run dies at the write and never reaches the
  recheck. Demonstrated by stubbing a `URLError` on the POST. `PATCH` is retried 4×.
- **`g5-3`, `g5-4`, `g5-5`** — the shipped per-PR concurrency group
  (`cancel-in-progress: true`) plus the SHA-stamped withdrawal cover the cross-revision
  case, which `tests/test_post.py:664` already pins. `g5-5` reproduces mechanically but
  its impact claim does not: the oldest comment keeps its previous review, so the PR is
  never left holding only retirement notices. Same-SHA collision needs a consumer to drop
  the concurrency group, and both runs then reviewed the same diff.
- **`c6-4` + `g6-2`** — recorded rather than fixed, which the report asked for either way
  (`48cb0e6`). Nothing in-process can establish membership: the accepted artifact is
  another job's output, so re-reading it from the same bundle compares a forgeable input
  against a forgeable copy. What bounds it today is that the remediation lane has no
  workflow, so nothing contributor-reachable composes `context_dir`.
- **`g1-2`** and **`c1-2`** — recorded in the docstrings (`467bba7`). `g1-2` is inherent
  to anchoring the scan to the plan. `c1-2` does not hold as stated: the anchoring loop
  threads `applied` through `suggest` and `patch` alike, so a suggestion is checked
  against pending content; cardinality refuses two on one path besides.

### Subsumed by primaries

`c3-4` → 15 · `g4-4`/`g4-5`/`c4-2` → 11 · `c3-10` → 9 · `c6-5`/`g6-3` → 21 ·
`g2-5` → 24 · `g1-6` → 24's eager sweep · `c6-1` → 22 · `g8-5` → 16 (verified by
deleting the `assert_no_symlinks` call: both lane tests fail, so the order is pinned)

## Premise corrections found in the minor batch

- **`g5-1`** — refuted; see above.
- **`g5-5`** — reproduces, but leaves the previous review visible rather than only
  retirement notices.
- **`c1-2`** — the stated location still admits the shape; what refuses it is cardinality,
  a different phase. The exploit is dead, the finding's own claim was wrong.
- **`c3-8`** — the candidate's stated precondition (needs finding 2 repaired first) is
  wrong: `max_steps` is checked in `check_plan_schema`, before the anchoring phase that
  confounded the other cases.
- **`c3-12`** — the report's named mutation (doubling `max_changed_bytes`) was already
  caught. The real gap was that no test asserted a case rejects *only* for its own
  reason.

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
.venv/bin/python -m pytest tests/ -q                                # 1373 passed
npm test && npm run typecheck                                       # 165 pass, typecheck clean
npm run build                                                       # required BEFORE the differential
.venv/bin/python -m pytest tests/test_plan_gate_differential.py -q  # 97 passed
```

`docs/architecture.html` per-node counts move with these: 1373 pytest / 165 node,
`execute_plan` node 42.

The differential grew from 37 to 97: the reason table, the exclusivity check and the
coverage census are parametrised per case, and four cases were added
(`over-both-the-line-and-byte-caps`, `over-max-steps`,
`several-steps-well-under-max-steps`, `denylist-near-miss-that-must-be-admitted`,
`write-chain-with-no-fix-step`).

**Run `npm run build` first.** The corpus now FAILS on a stale `dist/` rather than
silently testing the previous prover (`c2d924a`), and skips entirely when `dist/` is
absent so CI's Node-less `test_verifier` job stays correct.
