# A review is up to four passes, each shown what came before

Supersedes [ADR-0022](0022-a-second-pass-is-shown-what-the-first-found.md), its
addendum, and [ADR-0023](0023-the-review-passes-are-a-count.md).

ADR-0022 added an optional second pass on figures later found to come from a
harness whose search tool failed on every call. ADR-0023 made the pass a count
without restating what a pass is. This record states the whole decision once, on
measurements taken on 2026-11-11 with search working and review prompt v6.

## Context

A review's findings are almost all real: 98 to 100% were source-supported in
every arrangement measured. What a single review lacks is coverage. Supported
defect families per pull request, each run built on a different first review:

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
- **Later passes are as precise as the first.** 170 of 170 third- and
  fourth-pass findings on the large pull requests were supported, and 59 of 60
  third-pass findings on the smaller ones.
- A pass costs about $2.05 on a large pull request and $0.61 on a smaller one.

Alternatives measured and not adopted:

| Alternative | Result |
|---|---|
| Two reviewers each given half the files, then passes over their union | 9.1, 12.3 and 15.3 families at 2, 3 and 4 sessions against 10.2, 12.9 and 15.1; behind on 7 of 8 pull requests at 2 and at 3 sessions, 15 to 30% cheaper |
| A cheaper model (Sonnet 5.5) for the later pass | added 1.6 families where Opus 5.5 added 3.3, at 72% of the cost |
| Showing the later pass what the first could not settle | +0.4 on the large pull requests (interval includes zero), -0.4 on the smaller (interval excludes zero) |
| One review at maximum effort | 5.1 families against 3.5, but a review and a second pass found 5.4 for $1.21 against $2.29 |
| More passes on larger pull requests | size predicts a pass's yield no better than what the previous pass added (correlation 0.59 each) and is not monotonic below 1,600 lines |

## Decision

**A consumer chooses how many review passes to run, from 1 to 4. The default
is 1.**

- **A later pass is shown what the earlier passes found** — where each finding
  is and its title — and is told to review the rest of the change, starting with
  the kinds of defect not yet reported. It may find nothing.
- **Passes stop at the first that adds nothing.** No later pass follows a first
  review with no findings, a pass that failed or was closed early (ADR-0021), or
  a pass that found nothing new.
- **A later pass can only add to a review.** The review so far stands unless the
  combined one passes the same verification a single review must pass.
- **Combining passes involves no judgement.** Earlier findings are kept; a later
  finding at a place already reported is dropped; over the finding cap the most
  severe are kept, earlier passes winning ties.
- **What a pass is shown is data, not instruction.** Earlier findings are model
  output about contributor code and are presented as such.
- **Passes belong to review only.** The remediation lane cannot reach them
  (ADR-0013).
- **A count outside 1 to 4 fails the run** rather than being adjusted.
- **No size trigger and no file partitioning.** Stopping when a pass adds
  nothing already spends fewer passes where there is less to find, and
  partitioning saves cost without finding more.

## Consequences

- Each pass costs about one more review and about three minutes on a large pull
  request, one after another, inside the consumer's review time budget.
  Partitioned first passes would run side by side; that is the reason to revisit
  them if latency matters.
- The default finding cap is 20. Three passes on a large pull request combine to
  about 14 findings and four to about 16, so the cap starts to bind, and close to
  half of what the later passes add is low severity. The cost that grows fastest
  is the reader's attention.
- The fourth pass is measured on large pull requests only, and nothing beyond a
  fourth has been measured.
- Later passes are covered by unit tests and these measurements, not by the
  model evaluation scenarios.
