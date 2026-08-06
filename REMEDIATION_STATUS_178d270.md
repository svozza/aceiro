# Remediation status — CODE_REVIEW_178d270.md

Findings from the two-engine review of `a1bd348..178d270`. `CODE_REVIEW_178d270.md`
is left unmodified; outcomes are recorded here.

Status values: **open** (not yet addressed) · **fixed** (change landed, with the
commit) · **accepted** (a recorded trade, with the reason) · **superseded**.

## Source findings

| ID | Severity | Location | Claim | Status |
|---|---|---|---|---|
| F1 | high | `prepare_fix_context.py:89-99` | Review artifact selected by name and `max(id)`, so its identity is never established | fixed (`c3b91a9`) — bound to the posting run, read from the comment footer's run link; refused when no run can be established |
| F2 | high | `post.py:194` (also `:370`) | SHA stamp matched as a bare substring, so generator text satisfies the posted-review witness | fixed (`a3cd004`) — the stamp is read from the last line, the only part the executor authors |
| F3 | high | `suggest.py:582` | Retraction scoped per path, not per finding, so one command withdraws another's suggestion | fixed (`924f572`) — each comment records the finding it was delivered for; scope is that key |
| F4 | medium | `execute_plan.py:254` | A `/fix` on a closed or merged pull request passes every gate and delivers | fixed (`856be7b`) — `pr_snapshot` refuses anything not exactly `open`, before the first effect |
| F5 | medium | `stack.py:120-123` | `fix_key` drops the line on its anchored branch, so repeated code collides into one dedup key | fixed (`8c250eb`) — the line is folded into the anchored branch; `head_sha` already fixes the bytes |
| F6 | medium | `ai-pr-fix.yml:274` (and `ai-pr-review.yml`) | The gate step's position inside the gated job is unasserted | fixed (`2d215a7`) — gate-before-credential and gate-before-untrusted-content asserted over every lane |
| F7 | low | `github_api.py:375`, `stack.py:279-280` | `create_ref` is not the compare-and-swap three comments claim | fixed (`0cdb69f`) — all three comments corrected; the atomicity is on the branch name, not the key |
| F8 | low | `stack.py:325` | A 422 from `create_ref` escapes as an untyped traceback | fixed (`4c6b1a6`) — a 422 becomes a `Refusal` naming the branch and the orphaned commit |
| F9 | low | `plan_verify.py:719`, `:755` | A suggestion whose `old` stops before a mid-file terminator proves different bytes than it commits | fixed (`0cdb69f`) — `old` must consume a terminator that follows it; exempt at end of file |
| F10 | low | `execute_plan.py:215` | A head branch beginning with `-` makes every `/fix` on that pull request fail | fixed (`856be7b`) — `--head-branch=<v>`, one argv element |
| F11 | low | `route_delivery.py:90`, `:73` | Two refusals leave a traceback instead of an audit record | fixed (`4c6b1a6`) — argument TYPE checked, and `UnicodeDecodeError` caught |
| F12 | low | `github_api.py:29` | `MAX_REDIRECTS = 5` is dead code, so the declared bound is not the enforced one | fixed (`4c6b1a6`) — `MAX_REDIRECTS` is now the handler's `max_redirections` |

## Test findings (slice 10, mutation testing)

| ID | Severity | Location | Claim | Status |
|---|---|---|---|---|
| T1 | medium | `test_stack.py:183` | `test_an_empty_bot_login_matches_nothing` never reaches the `not bot_login` guard | fixed (`8c250eb`) — a `{"login": ""}` author reaches the guard a null author never did |
| T2 | low | `test_stack.py:76` | `test_a_different_finding_is_a_different_key` never pins `path` independently | fixed (`8c250eb`) — path and line each pinned alone |
| T3 | medium | `test_prepare_fix_context.py` | `test_nothing_is_emitted_empty` cannot reach the empty-value guard | fixed (`da573c1`) — an empty ref is driven through `main()`, the only way the guard runs |
| T4 | medium | `test_prepare_fix_context.py` | `test_a_missing_review_artifact_is_refused` asserts against its own stub; F1's region has no real coverage | fixed (`c3b91a9`) — the real fetch has its own coverage, incl. the forge |
| T5 | medium | `test_plan_verify.py` | The reviewed-SHA ambiguity guard is protected by no test in the file | fixed (`da573c1`) — the two ambiguity guards have distinct messages and a case each |
| T6 | medium | `test_plan_loop.py:397` | `test_a_non_integer_ordinal_is_refused` does not protect the `bool` exclusion | fixed (`da573c1`) — two findings, so `findings[1]` is a real element; matched on this guard's message |
| T7 | low | `test_suggest.py` | `test_an_unknown_commanded_path_withdraws_nothing` is vacuous | fixed (`924f572`) — `steps=[]`, so the retraction loop is actually reached |
| T8 | low | `test_github_api.py:309` | `test_a_redirect_loop_terminates` measures the stdlib, not this module | fixed (`4c6b1a6`) — a distinct-hop chain measures the declared bound |
| T9 | low | `test_fix_command.py:21` | `test_the_cap_is_the_policys_finding_limit` asserts the value, not the derivation | fixed (`da573c1`) — reloaded against a raised cap; a literal cannot follow |
| T10 | low | `test_route_delivery.py` | Four tests pass for the wrong reason, incl. the file's only credential-boundary assertion | fixed (`da573c1`) — the token read and the file reads are observed, not inferred |

All 22 findings are addressed. The suite is 1770 passing (from 1735); the
architecture diagram was updated in the same pass for the three nodes whose
stated properties changed. Every fix was mutation-tested: the property was
reverted and the test that names it observed to FAIL, then the source restored.

Two notes on what the fixes changed beyond the findings:

- **F3 changed a test's meaning, not just its fixture.**
  `test_a_comment_whose_path_github_omits_is_left_standing` asserted the old
  path-based scope. The retraction scope no longer reads the listing's `path`, so
  that comment is now withdrawable by the command that delivered it — its subject
  comes from our own marker, which GitHub cannot omit. The fail-closed property it
  was protecting moved to
  `test_a_comment_recording_no_finding_is_left_standing`, which is also what makes
  the marker's introduction backward-safe.
- **F12 nearly loosened a bound.** Setting `max_repeats = MAX_REDIRECTS` would
  have raised urllib's stricter default of 4 to 5. Only `max_redirections` is
  overridden; the loop test now says which bound it measures.

## What the fix pass was told, and how it held up

- **F1 + T4 are one hole seen twice.** The artifact selection is both unverified
  and untested. Fixing F1 without giving `fetch_reviewed_artifact` real coverage
  would leave the shape that produced it.
- **F2's fix must not weaken the witness.** The witness is deliberately
  existence-and-SHA only, so that contributor-influenced markdown stays out of the
  trust path. Anchor the stamp match to the executor-authored footer; do not start
  parsing the comment body into a finding.
- **F5's fix is additive.** `head_sha` is already in the key, so folding the
  finding's line in costs no anchor stability — this is what
  `suggest.suggestion_fingerprint` already does with `old`.
- **F6 wants a test, not a code change.** The evals lane already has the two
  ordering assertions; the agent lanes need the same.
- **F12 decides F9's sibling T8.** Deleting the dead constant and deleting the
  test that measures the stdlib are one change.
- **Mutation runs must set `PYTHONDONTWRITEBYTECODE=1`** and purge
  `__pycache__`. Stale bytecode produced two spurious survivors on the first pass
  of this round.
- Do not weaken a gate to close a finding. Three candidates were discarded this
  round precisely because their implied fixes were enforcement-shaped
  non-enforcement; see the discarded section of the review.
