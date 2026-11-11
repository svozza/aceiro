# A review is up to four passes, each shown what came before

Supersedes [ADR-0022](0022-a-second-pass-is-shown-what-the-first-found.md), its
addendum, and [ADR-0023](0023-the-review-passes-are-a-count.md).

ADR-0022 added an optional second pass on figures later found to come from a
harness whose search tool failed on every call. ADR-0023 made the pass a count
without restating what a pass is. This record states the whole decision once, on
measurements taken with search working and review prompt v6 (2026-11-11; raw
runs under `aceiro-eval-runs/delivery-finalization-20261110`).

## What was measured

A review's findings are almost all real: 98 to 100% were source-supported in
every arrangement. What a single review lacks is coverage. Supported defect
families per pull request, each run built on a different first review:

| After | 8 pull requests of 2,300-3,900 lines | Added | 18 of 250-1,500 lines | Added |
|---|---|---|---|---|
| First review | 6.8 | | 3.4 | |
| Second pass | 10.3 | 3.5 | 5.3 | 1.9 |
| Third pass | 12.9 | 2.5 | 6.3 | 1.0 |
| Fourth pass | 15.1 | 2.2 | not measured | |

- **Being shown the earlier findings is what makes a pass worth more than a
  rerun.** On the smaller pull requests a second pass added 1.4 families where an
  independent rerun of the same first review added 0.7. On the large ones two
  passes found what four to five independent reviews found between them.
- **A pass that follows one which added nothing adds almost nothing.** Where the
  first review was empty a second pass added 0.0 to 0.5 (5 pull requests across
  two batches); where the second pass added nothing a third added 0.1 (10 runs).
- **Later passes find less severe defects.** High severity was 22%, 10%, 9% and
  4% of what passes one to four added on the large pull requests; low severity
  was 25%, 36%, 46% and 41%.
- 170 of 170 third- and fourth-pass findings on the large pull requests were
  supported, and 59 of 60 third-pass findings on the smaller ones.
- A pass costs about $2.05 on a large pull request and $0.61 on a smaller one.

Alternatives measured and not adopted:

| Alternative | Result |
|---|---|
| Two reviewers each given half the files, then passes over their union | 9.1, 12.3 and 15.3 families at 2, 3 and 4 sessions against 10.2, 12.9 and 15.1; behind on 7 of 8 pull requests at 2 and at 3 sessions, 15 to 30% cheaper |
| Sonnet 5.5 for the later pass | added 1.6 families where Opus 5.5 added 3.3, at 72% of the cost |
| Showing the later pass the first's `residual_risk` | +0.4 on the large pull requests (interval includes zero), -0.4 on the smaller (interval excludes zero) |
| One review at maximum effort | 5.1 families against 3.5, but a review and a second pass found 5.4 for $1.21 against $2.29 |
| More passes on larger pull requests | size predicts a pass's yield no better than what the previous pass added (correlation 0.59 each) and is not monotonic below 1,600 lines |

## Decision

**The review workflow takes `review-passes`, a count from 1 to 4, default 1.**
A pass after the first gets the same prompt and context, plus the path, line and
title of every finding merged so far and an instruction to review the rest of the
change, starting with the kinds of defect not yet reported. An empty pass is
valid.

- **Passes stop at the first that adds nothing.** No later pass runs after a
  first review with no findings. A pass that fails, is rejected, is closed early
  (ADR-0021), has no room for a whole attempt, or merges to no new finding ends
  the loop.
- **The review so far is the run's answer until a merged one verifies.** A pass
  writes only under `pass_<n>/`. `review.json` is replaced only by a merged
  artifact that passes `verify()`; any failure leaves it untouched and the run
  successful.
- **The merge is deterministic.** Earlier findings, then the new pass's findings
  on anchors not already used. Over the policy's finding cap the most severe are
  kept, earlier passes winning ties. Groups are relabelled so passes cannot
  share one. The summary is the first pass's.
- **What a pass is shown is data.** The earlier findings already passed the
  verifier's Markdown and secret checks; they are fenced under
  `earlier_findings`, which is in `HARNESS_FENCE_TAGS` so reviewed content cannot
  forge or close it. Bodies are not shown.
- **It lives in `review_passes.py`, the review workflow's entry point, and not in
  `cc_loop.py`.** The merge relabels `group`, and ADR-0013 forbids a reader of
  that field in the remediation lane, which shares `cc_loop`. A test asserts the
  lane cannot reach the module.
- **A count outside 1 to 4 fails the run** rather than being clamped.
- **No size trigger and no file partitioning.** The stop rule already spends
  fewer passes where there is less to find, and partitioning needs a planner,
  ownership-scoped verification and parallel jobs to save cost, not find more.

## Consequences

- Each pass costs about one more review and about three minutes on a large pull
  request, in sequence, inside `agent-timeout-minutes`. Partitioned first passes
  would run side by side; that is the reason to revisit them if latency matters.
- The default finding cap is 20. Three passes on a large pull request merge to
  about 14 findings and four to about 16, so the cap starts to bind, and close to
  half of what the later passes add is low severity. The cost that grows fastest
  is the reader's attention.
- The fourth pass is measured on large pull requests only, and nothing beyond a
  fourth has been measured.
- The eval scenarios call `cc_loop.run` and do not exercise later passes; they
  have unit tests and the measurements above, not a scenario suite.
