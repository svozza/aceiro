# Large-PR delivery and coverage (2026-11-10)

Decisions taken from this: ADR-0021 and ADR-0022. Raw runs (specs, transcripts, judge output) are
kept outside the repository under `aceiro-eval-runs/delivery-finalization-20261110`.

## Summary

- **Delivery is fixed for the failure modes observed.** A review that runs out of clock or
  turns now submits what it confirmed. At a 260s budget: 9 of 16 delivered before, 16 of 16 after.
- **Coverage, not precision, is the large-PR problem.** 98-100% of findings are source-supported
  in every arm, but one review finds 5.5 of the ~16 distinct supported defect families found per PR.
- **A second pass shown the first pass's findings is the best use of a second session**: 8.9
  families per PR against 6.7 for two independent reviews. Built as the opt-in `second-pass` input (ADR-0022).
- **Plain Claude Code found more per session than the reviewer** (6.4 against 5.5) at lower cost.
  The cause is not established.

## What the October timeouts were

1. The budget notices fired 0 times in 136 sessions across the three October large-PR studies.
   The experiment harness replaced `Read/Grep/Glob` with `mcp__source__*` tools and
   `ReviewBudget` matched only the built-in names. Production was not affected.
2. Provider latency. Median later-turn latency was 14.9s in October and 3.9s on 2026-11-10.
   The same eight PRs that lost 6 of 16 generalist reviews at 450s then lost none to the clock now.
3. On a fast provider the turn ceiling binds before the clock. It was derived from one August
   run (21 calls in 618s). Every failure at 450s (3 of 48) and 7 of 16 at 260s was the turn ceiling.
4. `GIT binary patch` literals (315 KB of PNG in large-09) came from the experiment's
   `git diff --binary`; GitHub's diff, which production uses, does not carry them.

## Changes on the branch

| Change | File | Evidence |
|---|---|---|
| Forced final submission at a 180s reserve and at the investigation turn ceiling | `cc_loop.py` | 18 of 18 finalizations delivered, in 30-71s |
| Source reads refused while finalizing | `review_budget.py` | unit test |
| Harness-authored "closed early" notice on a finalized review | `post.py` | unit test |
| Turn ceiling recalibrated (636 calls / 6,974s; fallback 125) | `cc_loop.py` | removes the fast-provider turn failures |
| Large lockfile hunks shown as one line with hosts | `artifact.py` | cost $4.60 -> $2.00 on the 65%-lockfile PR, no loss |
| Opt-in second pass and merge | `review_passes.py` | calibration below |
| Default finding cap raised from 10 to 20 | `policy.json` | the cap of 10 dropped 1.7 supported families from each large two-pass review it touched |
| A session that ends without submitting is told once | `cc_loop.py` | recovered a classifier-interrupted submission in 3 of 3 occurrences |
| ADR-0021, ADR-0022, architecture diagram | `docs/` | |

2,426 unit tests pass, type check clean, 42/42 review and 6/6 plan eval scenarios pass (one run).

## Experiments

All on Opus 5.5 at high effort with source-only tools, a model-only credential and no execution
of reviewed code. Judge: Fable 5, blind to arm, reading source, all arms of a PR in one session.

| Batch | Question | Sessions | Result |
|---|---|---|---|
| exp1 | Does October reproduce? main / notices / finalization, 8 PRs x2, 450s | 48 | 15/16 in each arm; all failures the turn ceiling |
| exp2a | 200s stress | 16 | exposed two bugs in my finalization (both fixed); not a valid comparison |
| exp2b | 4 largest PRs, +/- lockfile elision, 450s | 24 | elision: -56% cost on the lockfile-heavy PR; run on a harness with a race, so its delivery counts understate finalization |
| exp3 | 260s stress, fixed harness | 32 | 9/16 without, 16/16 with |
| exp4/4b | second pass; two partitions | 16 + 32 | see below |
| exp5 | plain Claude Code baseline | 16 | see below |
| exp6 | why plain covers more: prompt brake removed; diff as a file | 48 | neither explains it |

### Coverage (supported families per PR, mean of 8 PRs, joint judgement)

| Arrangement | Sessions | Families |
|---|---|---|
| One review | 1 | 5.50 |
| One review, "handful of tool calls" sentence replaced | 1 | 6.06 |
| One review, diff given as a file | 1 | 5.56 |
| Plain Claude Code | 1 | 6.38 |
| Two independent reviews | 2 | 6.73 |
| Two partition reviewers | 2 | 7.56 |
| Three independent reviews | 3 | 7.54 |
| Two plain Claude Code runs | 2 | 8.75 |
| First review + second pass | 2 | 8.88 |

- The second pass added 3.1 new families against 1.1 from an independent rerun, and gained on 8 of 8 PRs.
- Run-to-run noise between identical reviewer arms is about +/-0.4 families, so the 6.06 for the
  prompt edit is suggestive only.
- Reviews finalized at 260s found 1.13x the families of a natural 450s review (n=9).
- Plain Claude Code: $1.35 against $1.95 per review, 31 tool calls against 14; 17% of its findings
  were not anchored to a changed line and could not be posted inline as written.
- Re-judging with 48 more reviews in the pool moved these figures by under 0.1.

## Second-pass calibration and build (added later the same day)

18 public PRs of 250-1,500 changed lines, two first passes and two second passes each, 72/72 delivered.

| Band (changed lines) | First pass | Second pass adds | Rerun adds | Second-pass cost |
|---|---|---|---|---|
| 250-450 | 2.8 | 1.3 | 0.5 | $0.56 |
| 450-900 | 2.3 | 1.1 | 1.0 | $0.58 |
| 900-1,500 | 4.0 | 1.5 | 1.0 | $0.89 |

No size threshold: the gain tracks whether the first pass found anything (0.2 added on the 3
empty-first PRs, 1.5 on the other 15). 54 of 56 second-pass findings supported, 0 unsupported.

Built as an opt-in `second-pass` workflow input (ADR-0022, `review_passes.py`). Three live runs of
the real path: empty first pass skipped the second; the other two merged to exactly the 10-finding
cap. The repo eval scenarios do not exercise it.

## Tradeoffs

- **Finalization** turns a visible failure into a partial review. The notice is harness-authored so
  a contributor who slows a review cannot make a partial one read as whole. Consumers alarming on
  failed reviews lose that signal.
- **Lockfile elision** hides machine-written hunks from the first view. Hosts on added lines are
  kept, anchors still verify, the file stays readable. A source file named like a lockfile with
  over 200 diff lines would be hidden too, though listed and named.
- **Turn ceiling** at ~2.7x its old value: the clock is now the only practical bound on a session.
- **The session loop moved from `query()` to `ClaudeSDKClient`**, needed for interrupt. `/fix` runs
  the same loop with finalization off; its evals pass.
- **Second pass** would roughly double cost and add a sequential session on large PRs, and its
  union often exceeds the 10-finding cap.

## Limits

- One day's provider conditions; the delivery comparison with October is across dates.
- The judge's `supported` means the factual kernel is real. An independent audit of 24 verdicts
  found 22 agreed, 1 overclaimed, 1 unsettleable, 0 wrong. Severity and stated impact are not
  audited, and facts about external tools were accepted unverified.
- 8 PRs for coverage, 2 runs per arm per PR; 4 PRs for lockfile elision, one of them lockfile-heavy.
- The plain baseline had source-only tools, not a shell.

## Correction (2026-11-10, after merge): Grep failed in every session

The experiment's source-tool shim resolved ripgrep with `shutil.which('rg')`, and no `rg` binary was
on the session PATH. All 1,456 Grep calls in the 325 sessions of this study returned an error, in the
reviewer arms and the plain Claude Code arm alike. The October studies were not affected (20 of 830
failed). Production uses Claude Code's built-in Grep and was not affected.

- The delivery results do not depend on Grep.
- The shim now pins a ripgrep binary and refuses to start without it.

The coverage arms were re-run the same day with Grep working (202 sessions, 202 delivered, 1,125 of
1,142 Grep calls succeeded), and re-judged. These replace the coverage table above:

| Arrangement (8 large PRs) | Sessions | Families, no Grep | Families, Grep working |
|---|---|---|---|
| One review (4 runs per PR) | 1 | 5.50 | 6.06 |
| Plain Claude Code (4 runs per PR) | 1 | 6.38 | 6.84 |
| Two independent reviews | 2 | 6.73 | 7.75 |
| Two partition reviewers | 2 | 7.56 | 8.25 |
| Three independent reviews | 3 | 7.54 | 8.81 |
| Two plain Claude Code runs | 2 | 8.75 | 9.29 |
| First review + second pass | 2 | 8.88 | 9.00 |

- The pool is 17.1 supported families per PR. 98.6-100% of findings are supported in every arm.
- The second pass added 2.6 new families against 1.5 from an independent rerun of the same first
  review (difference 1.1, standard error 0.3, larger on 7 of 8 PRs). That result stands.
- Second pass against partition reviewers is not separated: 0.75 apart with a standard error of 1.2.
  The first reviews that fed the second pass were also above average (6.4 against 6.1).
- Plain Claude Code against the reviewer is +0.8 with a standard error of 0.65: ahead on 6 of 8 PRs,
  not separated. It made 31 tool calls against 13 and cost $1.53 against $1.84.
- Mid-size PRs (18, three runs): 3.3 families per review; the second pass added 1.4 on the 15 PRs
  whose first review found something (a rerun added 0.6) and 0.0 on the 3 where it found nothing.
  233 of 233 findings supported.
- Run-to-run standard deviation of one review within a PR: 1.35 families on large PRs, 0.73 on mid-size.

## Errors of mine that affected batches

- exp2a/exp2b ran before I fixed a cancel-scope race that dropped some finalizations.
- All 32 partition sessions in exp4 crashed on an import bug in the experiment entry; rerun as exp4b.
- A review accepted on the exact turn that reached the ceiling was dropped (2 sessions); fixed with a test.
- I reported 326 judged findings for exp1; the number is 275.

## Spend

About $436 for 234 model review sessions (6 receipts missing) plus $93 of judging.
