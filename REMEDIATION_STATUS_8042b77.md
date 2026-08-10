# Remediation status — CODE_REVIEW_8042b77.md

Findings from the two-engine review of `978d772..8042b77`. `CODE_REVIEW_8042b77.md`
is left unmodified; outcomes are recorded here.

Status values: **open** (not yet addressed) · **fixed** (change landed, with the
commit) · **accepted** (a recorded trade, with the reason) · **superseded**.

## Findings

| ID | Severity | Location | Claim | Status |
|---|---|---|---|---|
| F2 | high | `test_plan_gate_differential.py:760-808` | The fix-lane token scan cannot see a `group` reader written in the repository's own idiom, so ADR-0013's load-bearing condition is unenforced | fixed (`a9abf72`) — the scan resolves a name bound to the field's value, to a fixpoint, and handles `FSTRING_MIDDLE` and `verify.GROUP_FIELD`; both arms calibrated against source fixtures, one per arm |
| F3 | medium | `test_plan_gate_differential.py:747-753` | `FIX_LANE_FILES` names 8 modules under a comment claiming all 19, and `artifact.py` could host an invisible authorising reader | fixed (`a9abf72`) — the lane is derived as the import closure of `ai-pr-fix.yml`'s entry points; `post.py` and `verify.py` are named exemptions with a load-bearing assertion each |
| F1 | medium | `prepare_fix_context.py:267` reading `:244` | The ordinals resolve in model order while every other reader uses rendered order, so ADR-0014's decline fires on the wrong command | fixed (`49be547`) — `rendered_findings(review, severity_ranks(policy))`; both directions pinned with unsorted fixtures, plus the cross-reference round trip |
| F4 | medium | `execute_plan.py:514`, `:530-536`, `:568-572` | The executor's wiring hands `verify_plan` a set it never proves is whole; four collapses to one finding survive | fixed (`881ef88`) — a second two-finding bundle spies the gate's parameter, compares the key against the one-finding key, and refuses a plan missing the second commanded path; six mutations die |
| F6 | medium | `suggest.py` (`delivered_stamp`) | Not pinned to the marker line, unlike every sibling marker reader | fixed (`3f4c295`) — a stamp below line 1 reads as `None` and the spent body says the scope was unrecorded |
| F5 | low | `verify.py:280-318` | `check_group_cardinality` has no test anywhere, and the shipped cap cannot fire | fixed (`6772621`) — cap set to 4, the satisfiability relationship recorded in `policy.json`'s description, the cross-references-to-nowhere over-claim struck, ten tests including the shipped-cap satisfiability assertion |
| F7 | low | `prepare_fix_context.py:270-278`, guard at `decline.py:176-179` | A contributor-chosen filename suppresses the decline entirely, and three docstrings plus the ADR claim that is impossible | fixed (`c4b5d57`) — the delimiter carries `+` and `=`, which the path grammar cannot express; all three claims corrected and ADR-0014 amended |
| F8 | low | `plan_loop.py:209` | The canonical ordering is asserted with data that cannot distinguish it | fixed (`a512c41`) — re-spelled with `[9, 1]` against a ten-finding fixture at `max_items` |
| F9 | low | `evals/run_evals.py:534-551` | `check_grouping` keys on path, not finding, so the two halves of the defect can be in different groups and the scenario passes | fixed (`44d03fe`) — computed over the findings on wanted paths, folded into the existing `shared`; the `constants.py` calibration decided and recorded in the fixture |
| F10 | low | `test_run_evals.py` | The new eval fixture is absent from both meta-test pinning lists | fixed (`dbde1a8`) — `grouped_cross_file_defect` and `provenance_boundary_adjacent_bug` added to both, plus a reverse assertion deriving the coverage |

All ten findings are addressed. The suite is **2019 passing** (from 1956), node is
168, typecheck is clean and the differential corpus is 97 — down from 100 because
three tests moved into `tests/test_group_is_advisory.py`, which holds 25.

## The revert each fix was checked against

Every fix was mutation-tested: the property was reverted, the test that names it
observed to FAIL, then the source restored. Run with `PYTHONDONTWRITEBYTECODE=1`
and `__pycache__` purged first.

| Finding | Reverted | Result |
|---|---|---|
| F2 | the review's widening written into `plan_loop.py` in the house idiom (`from verify import GROUP_FIELD`; `f[GROUP_FIELD] in {...}`) | `test_the_group_field_has_NO_reader_in_the_fix_lane` fails, naming `plan_loop.py` |
| F3 | a `widen_to_groups` helper added to `artifact.py` | the same assertion fails, naming `artifact.py` |
| F2/F3 | each of the four scanner arms killed; the closure walk stopped following imports; an exemption that exempts nothing; an exemption naming a module outside the lane | each fails a named test — the STRING arm included, which was previously killable with the whole suite green |
| F1 | `findings = review["findings"]` | the three new order tests fail and **nothing else in the suite changes verdict**, which is the review's "leaves the suite green either way" finding closed |
| F4 | `fix_key(..., commanded_findings[:1], ...)`; `commanded_finding_keys` over `[:1]`; `read_commanded_findings(...)[:1]`; `ordinals="1"`; `[::-1]`; `(finding_identity(xs[0]),)` | all six fail named tests; the `[:1]` on `read_commanded_findings` fails five, being the one that genuinely opens the gate |
| F6 | `DELIVERED_RE.search(review.get("body") or "")` (whole body) | `test_the_STAMP_is_read_from_line_one_only` fails; previously 129 green |
| F5 | `max_distinct_groups: 10`; `> cap` → `>= cap`; the call site; the whole cardinality arm; each policy-fault arm | six reverts, each failing a named test; the vacuous value fails `test_the_SHIPPED_cap_is_satisfiable` |
| F7 | `_DELIMITER = "SMTITHY_DECLINE_EOF"` | four tests fail; separately, deleting the guard fails `test_a_reason_carrying_the_delimiter_emits_nothing`, so it is still pinned as REFUSING |
| F8 | `sorted(set(indices))` → `list(set(indices))`; and → `sorted(indices)` | the first fails the order test (previously 66 green), the second fails `test_a_repeated_ordinal_resolves_to_one_finding` — both halves independently pinned |
| F9 | the per-path intersection | the two new grader tests fail and no shipped test changes verdict, which is the constraint that ruled out a new message |
| F10 | `line_in` 8 → 9 and 3 → 1; both `body_contains_any` blocks deleted, per fixture | four hollowings, each failing two named parametrized cases; removing a `DEFECTS` entry fails the new reverse assertion |

`docs/architecture.html` was updated in the same commit as each code change it
depicts, for the five nodes whose stated properties changed: the review comment
(the no-reader condition and the distinct-group cap), the plan executor (the
plurality wiring), the decline (rendered-order resolution and the delimiter), and
the generator's eval line (finding-granularity grouping and the pinning lists).

## Two findings were REFUTED — do not re-file them

Recorded so a future round does not re-open a closed question. Both were reached by
both engines independently, and the refuter's question in each case was the same
one neither finder asked: **does changing the thing you blame actually remove the
harm?**

- **The `fix_key` set-collapse.** The `sorted({...})` set comprehension is not the
  cause. `/fix 1 == /fix 2` collides under the implied fix too, because
  `finding_component` is non-injective at same-path-same-line — and that is
  pre-existing on `main`, the residue of last round's F5 fix at the next
  granularity down. The set semantics are also test-pinned
  (`test_a_repeated_finding_does_not_change_the_key`), so the implied fix would
  have broken a test. **If `finding_component` is ever fixed,
  `suggest.finding_identity` collides identically and must be fixed with it** —
  check last round's F5 record before opening it at all.

- **The S5 reconciler case** (a verified `/fix 1,3` on one file addressing only
  finding 3's line while the comment records finding 1's key). The reported harm —
  two live independently-applicable suggestions — is **geometry-independent**: a
  refuter re-ran the identical two-command sequence under the ADR-blessed
  contiguous plan and got byte-for-byte the same outcome. The retraction predicate
  is `owned_finding_key(comment, bot_login) not in scope` — key versus scope, with
  no line, region, `old` or `new` in it. The state is ADR-0009 addendum D's
  explicitly accepted cross-command partiality, stated verbatim in
  `reconcile_suggestions`' own docstring. The *mislabelling* framing does not
  survive either: deriving the representative from the covered region only swaps
  **which** later command leaves two comments live, and when the region spans both
  findings both keys qualify, so `keys[0]` is still the needed tiebreak. **Do not
  "fix" this.**

## Deliberately out of scope, with the reason

- **ADR-0013's same-file contiguity claim.** The ADR asserts that "two findings
  anchored in one file produce one contiguous replacement", and nothing enforces
  contiguity or coverage: a plan can deliver half a same-file command with nothing
  refusing, warning or recording it. Unaffected by the S5 refutation, which was
  about whether the *reconciler* mislabels; this is about what the *ADR claims*. An
  ADR amendment in addendum D's shape rather than a code fix, and it **needs a
  decision on whether region coverage becomes a checked property** — so it is left
  for the owner rather than decided here.

  Note the precedent this round set twice: where a document described a harm the
  code does not prevent, the sentence was struck (F5's docstring) or amended (F7's
  ADR) rather than the check strengthened to match the prose.

- **`finding_component`'s non-injectivity.** Pre-existing, refuted as filed, and
  any fix spans `stack.py` and `suggest.py`. See the refutation above.

- **The unpinned "Refuse to run unpinned" step** on all six workflow jobs. Real —
  de-pinning any job's checkout leaves the suite green — but pre-existing on five
  of the six, so it is its own piece of work rather than this branch's remediation.

## Notes on what the fixes changed beyond the findings

- **F2/F3's fix moved the assertion out of the differential file.** A pure-Python
  token scan sat behind `pytestmark = skipif(not PROVER_JS.exists())`, so its reach
  depended on a build it does not use, and CI's Python-only `test_verifier` job is
  right to have no Node. Nothing was hidden — `test_gate_differential` builds and
  did run it — but the placement was fragile, so the scan now lives in
  `tests/test_group_is_advisory.py`. That is why the differential count moved from
  100 to 97.

- **F3's fix derives the lane rather than extending the list.** Adding
  `artifact.py` to a literal list would have closed the reproduced vector and left
  the shape that produced it. `decline.py` is in the derived closure but was
  refuted as a vector in its own right: it receives no finding and cannot widen a
  scope.

- **F5's fix required a decision the review did not make.** The cap had to become a
  number, and a number can drift back — so the load-bearing artefact is
  `test_the_SHIPPED_cap_is_satisfiable`, which derives its bound from the policy
  rather than comparing against a literal. A future `max_items` bump now fails
  there instead of silently re-vacating the cap.

- **F9's fix was paired with a calibration decision, and the measurement changed
  the answer.** Adding `constants.py` to `grouped_paths` was considered and is
  worse: `grouped_paths` requires a finding on every path it names, no measured run
  files one there (12/12 across 3 runs, 2026-08-10), so the answer the reviewer
  actually gives would fail. That is `plan_multi_file_fix`'s failure mode exactly —
  a fixture no achievable answer satisfies. The scenario therefore keeps measuring
  a willingness to under-group, with the cost stated in its own `grouping_note` and
  a note that a run failing there means the fixture is wrong, not the reviewer.

- **F10's fix added a reverse assertion the finding did not ask for.** Both pinning
  lists are hand-kept, which is the actual defect class: a scenario carrying
  `line_in` and appearing in neither list is invisible. The next such scenario now
  fails until someone decides where it belongs.

- **F7's fix deliberately did not weaken the guard.** Switching from refusing to
  escaping would have traded a fail-closed check for a fail-open one. The delimiter
  became inexpressible instead, and the property is asserted against the path
  PATTERN rather than against the three reproduced spellings — a delimiter change
  defeating only those three fails.

## What the fix pass was told, and how it held up

- **F2 and F3 are one root cause and were fixed together**, as the review said: the
  scan's reach is narrower than its name asserts, in two directions.
- **F4 is a wiring gap, not a gate gap.** `check_commanded_scope`'s plurality
  already had real coverage that kills a `[:1]` inside the gate; scoping the fix to
  the producer end is what kept it one piece of work rather than four.
- **Sorted fixtures cannot distinguish two orders**, which is why F1's fix is one
  line and the care went into the fixtures.
- **A new error message can break shipped tests.** F9's literal fix broke two that
  match on the existing message; folding it into `shared` was the constraint, not a
  preference.
- **`str.replace(..., 1)` finds docstrings before code in this repo.** Every
  mutation was `git diff`ed and read before the run.
- **Mutation runs set `PYTHONDONTWRITEBYTECODE=1`** and purged `__pycache__` first,
  the rule last round recorded after two spurious survivors.
- **Do not weaken a gate to close a finding.** Two documents were corrected this
  round (F5's docstring, F7's ADR) precisely so that no check had to be stretched
  to make a false sentence true.
- **Not pushed.** git-defender has blocked this repository and blocks are cached,
  so a clean push would prove nothing. Commits are local.
