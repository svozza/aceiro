# A second pass is shown what the first found

Measured 2026-11-10 (evidence: `handoff/delivery-finalization-20261110/RESULTS.md`).

A review's findings are almost all real: 98 to 100% were source-supported in
every arrangement tested. What a single review lacks is coverage. On eight pull
requests of 2,300 to 3,900 changed lines, one review found 5.5 of the roughly 16
distinct supported defect families that repeated reviews found between them.

Three ways of spending a second session were compared on those pull requests:

| Arrangement | Families per pull request |
|---|---|
| One review | 5.5 |
| Two independent reviews | 6.7 |
| Two reviewers each given half the files | 7.6 |
| A review, then a second shown the first's findings | 8.9 |

The second pass added 3.1 families where an independent rerun added 1.1, and
gained on all eight. On 18 further pull requests of 250 to 1,500 lines it added
1.1 to 1.5 in every size band, so the gain does not depend on size. It depends
on the first pass: where the first review was empty (3 pull requests) the second
added 0.2, and 9 of 36 second passes returned an empty review. 54 of its 56
findings were supported, 2 uncertain, none unsupported.

## Decision

**The review workflow takes a `second-pass` input, off by default.** When on, a
first pass that delivered at least one finding, was not closed early (ADR-0021)
and left room for a whole attempt is followed by a second session. It gets the
same prompt and context, plus the path, line and title of each first-pass finding
and an instruction to review the rest of the change. An empty second review is
valid.

- **The first review is the run's answer until a merged one verifies.** The
  second pass writes only under `second_pass/`. `review.json` is replaced only by
  a merged artifact that passes `verify()`; any failure, timeout or rejection
  leaves it untouched and the run successful.
- **The merge is deterministic.** First-pass findings, then second-pass findings
  on anchors the first did not use. Over the policy's finding cap the most severe
  are kept, the first pass winning ties. Groups are relabelled so the passes
  cannot share one. The summary is the first pass's.
- **What the second pass is shown is data.** The earlier findings already passed
  the verifier's Markdown and secret checks; they are fenced under
  `earlier_findings`, which joins `HARNESS_FENCE_TAGS` so reviewed content cannot
  forge or close it. Bodies are not shown.
- **It lives in `review_passes.py`, the review workflow's entry point, and not in
  `cc_loop.py`.** The merge relabels `group`, and ADR-0013 forbids a reader of
  that field in the remediation lane, which shares `cc_loop`. A test asserts the
  lane cannot reach the module.
- **No size trigger.** The data does not support one.

## Consequences

- About one more review's cost and latency on pull requests where the first pass
  found something, inside the same `agent-timeout-minutes`.
- **The finding cap now binds.** Both live two-pass runs on defect-dense pull
  requests merged to exactly the cap, dropping lower-severity findings. Raising
  it is a separate decision.
- The "first pass found something" condition rests on three empty-first pull
  requests. It is the cheap direction to be wrong in: a missed second pass costs
  coverage on a review that found nothing, not a wrong finding.
- The eval scenarios call `cc_loop.run` and do not exercise the second pass; it
  has unit tests and three live runs, not a scenario suite.
