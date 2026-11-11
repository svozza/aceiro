# The review passes are a count

**SUPERSEDED by [ADR-0024](0024-a-review-is-up-to-four-passes-each-shown-what-came-before.md)
(2026-11-11).**

Measured 2026-11-11 on prompt v6 (raw runs under
`aceiro-eval-runs/delivery-finalization-20261110`, batches `st1`, `st2`, `st3m`,
`st3l`).

ADR-0022 added one optional second pass. Further passes, each shown the findings
of every earlier pass, were then measured. Supported defect families per pull
request, every run built on a different first review:

| After | 8 pull requests of 2,300-3,900 lines | Added | 18 of 250-1,500 lines | Added |
|---|---|---|---|---|
| First review | 6.8 | | 3.4 | |
| Second pass | 10.3 | 3.5 | 5.3 | 1.9 |
| Third pass | 12.9 | 2.5 | 6.3 | 1.0 |
| Fourth pass | 15.1 | 2.2 | not measured | |

- 170 of 170 third- and fourth-pass findings on the large pull requests were
  source-supported, and 59 of 60 third-pass findings on the smaller ones. A pass
  costs about $2.05 on the large and $0.61 on the smaller.
- The large pull requests are not exhausted at four passes: all sessions judged
  together name 24.6 families per pull request.
- On the smaller pull requests the third pass added nothing in 23 of 54 runs.
  Where the second pass had added nothing (10 runs) the third added 0.1.
- What a later pass adds is less severe. On the large pull requests high severity
  was 22% of first-review families, 10% of the second pass's, 9% of the third's
  and 4% of the fourth's; low severity was 25%, 36%, 46% and 41%. On the smaller
  ones 58% of what the third pass added was low severity and 2% high.

Three alternatives to a further pass were measured and rejected:

- **A cheaper model for the later pass.** Sonnet 5.5 added 1.6 families where
  Opus 5.5 added 3.3 (worse on 8 of 8), at 72% of the cost.
- **Showing the later pass the first's `residual_risk`.** +0.4 on the large pull
  requests (interval includes zero), -0.4 on 18 of 250 to 1,500 lines (interval
  excludes zero).
- **One review at maximum effort.** 5.1 families against 3.5 at high effort on
  the 18 smaller pull requests, but a review and a second pass found 5.4 for
  $1.21 against $2.29, in under half the time.

## Decision

**The `second-pass` input is replaced by `review-passes`, a count from 1 to 4,
default 1.** `review-passes: 2` is what `second-pass: true` was.

- **Each later pass is ADR-0022's second pass.** Same prompt, context and note;
  it is shown every finding merged so far and writes only under `pass_<n>/`.
  `review.json` is replaced only by a merged artifact that passes `verify()`.
- **Passes stop at the first that adds nothing.** A pass that fails, is rejected,
  is closed early, has no room for a whole attempt, or merges to no new finding
  ends the loop. So does the finding cap, since a full review cannot grow. The
  measured basis is the 0.1 a third pass added after an empty second.
- **The ceiling is 4.** Nothing beyond a fourth pass has been measured.
- **A count outside 1 to 4 fails the run** rather than being clamped: a typo
  should not silently buy one review where three were asked for.

## Consequences

- `second-pass` is removed, not aliased. No consumer had it on.
- Each pass costs about one more review and three more minutes on a large pull
  request, in sequence, inside `agent-timeout-minutes`.
- Three passes on a large pull request merge to about 14 findings and four to
  about 16, so the default cap of 20 starts to bind, and close to half of what
  the later passes add is low severity. The cost that grows fastest is the
  reader's attention, not the model's.
- The fourth pass is measured on large pull requests only.
