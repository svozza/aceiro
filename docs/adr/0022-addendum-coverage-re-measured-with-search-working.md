# Addendum to ADR-0022: the coverage figures re-measured with search working

**SUPERSEDED by [ADR-0024](0024-a-review-is-up-to-four-passes-each-shown-what-came-before.md)
(2026-11-11).**

ADR-0022's figures came from an experiment harness in which every Grep call
failed (`docs/findings/0005-large-pr-delivery-and-coverage.md`, Correction). The
arms were re-run on 2026-11-10 with Grep working: 202 sessions, all delivered.

| Arrangement | ADR-0022 | Re-measured |
|---|---|---|
| One review | 5.5 | 6.1 |
| Two independent reviews | 6.7 | 7.8 |
| Two reviewers each given half the files | 7.6 | 8.3 |
| A review, then a second shown the first's findings | 8.9 | 9.0 |

## What stands

- A second pass beats an independent rerun of the same first review: 2.6 new
  families against 1.5, larger on 7 of 8 pull requests (difference 1.1, standard
  error 0.3).
- On the 18 pull requests of 250 to 1,500 lines it added 1.4 where the first
  review found something (15) and 0.0 where it found nothing (3). The skip after
  an empty first pass holds.
- 88 of 88 second-pass findings were supported.

## What does not

- The second pass is not separated from two half-file reviewers: 0.75 families
  apart with a standard error of 1.2. The decision's case against partitioning is
  that it needs a partition plan and the second pass does not, not the measured gap.

The decision is unchanged.
