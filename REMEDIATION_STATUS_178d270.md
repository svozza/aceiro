# Remediation status — CODE_REVIEW_178d270.md

Findings from the two-engine review of `a1bd348..178d270`. `CODE_REVIEW_178d270.md`
is left unmodified; outcomes are recorded here.

Status values: **open** (not yet addressed) · **fixed** (change landed, with the
commit) · **accepted** (a recorded trade, with the reason) · **superseded**.

## Source findings

| ID | Severity | Location | Claim | Status |
|---|---|---|---|---|
| F1 | high | `prepare_fix_context.py:89-99` | Review artifact selected by name and `max(id)`, so its identity is never established | open |
| F2 | high | `post.py:194` (also `:370`) | SHA stamp matched as a bare substring, so generator text satisfies the posted-review witness | open |
| F3 | high | `suggest.py:582` | Retraction scoped per path, not per finding, so one command withdraws another's suggestion | open |
| F4 | medium | `execute_plan.py:254` | A `/fix` on a closed or merged pull request passes every gate and delivers | open |
| F5 | medium | `stack.py:120-123` | `fix_key` drops the line on its anchored branch, so repeated code collides into one dedup key | open |
| F6 | medium | `ai-pr-fix.yml:274` (and `ai-pr-review.yml`) | The gate step's position inside the gated job is unasserted | open |
| F7 | low | `github_api.py:375`, `stack.py:279-280` | `create_ref` is not the compare-and-swap three comments claim | open |
| F8 | low | `stack.py:325` | A 422 from `create_ref` escapes as an untyped traceback | open |
| F9 | low | `plan_verify.py:719`, `:755` | A suggestion whose `old` stops before a mid-file terminator proves different bytes than it commits | open |
| F10 | low | `execute_plan.py:215` | A head branch beginning with `-` makes every `/fix` on that pull request fail | open |
| F11 | low | `route_delivery.py:90`, `:73` | Two refusals leave a traceback instead of an audit record | open |
| F12 | low | `github_api.py:29` | `MAX_REDIRECTS = 5` is dead code, so the declared bound is not the enforced one | open |

## Test findings (slice 10, mutation testing)

| ID | Severity | Location | Claim | Status |
|---|---|---|---|---|
| T1 | medium | `test_stack.py:183` | `test_an_empty_bot_login_matches_nothing` never reaches the `not bot_login` guard | open |
| T2 | low | `test_stack.py:76` | `test_a_different_finding_is_a_different_key` never pins `path` independently | open |
| T3 | medium | `test_prepare_fix_context.py` | `test_nothing_is_emitted_empty` cannot reach the empty-value guard | open |
| T4 | medium | `test_prepare_fix_context.py` | `test_a_missing_review_artifact_is_refused` asserts against its own stub; F1's region has no real coverage | open |
| T5 | medium | `test_plan_verify.py` | The reviewed-SHA ambiguity guard is protected by no test in the file | open |
| T6 | medium | `test_plan_loop.py:397` | `test_a_non_integer_ordinal_is_refused` does not protect the `bool` exclusion | open |
| T7 | low | `test_suggest.py` | `test_an_unknown_commanded_path_withdraws_nothing` is vacuous | open |
| T8 | low | `test_github_api.py:309` | `test_a_redirect_loop_terminates` measures the stdlib, not this module | open |
| T9 | low | `test_fix_command.py:21` | `test_the_cap_is_the_policys_finding_limit` asserts the value, not the derivation | open |
| T10 | low | `test_route_delivery.py` | Four tests pass for the wrong reason, incl. the file's only credential-boundary assertion | open |

## Notes for the fix pass

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
