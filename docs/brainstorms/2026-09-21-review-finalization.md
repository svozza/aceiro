---
date: 2026-09-21
topic: review-finalization
status: implemented-experimentally
---

# Explicit investigation and reporting phases

Implementation and tests are complete on a local, opt-in runtime branch.
See [reporting-phase-results.md](../../handoff/reporting-phase-results.md).
The reporting and automatic handoff work; shorter investigation and consistent
repair quality remain unproven. No deployment has been made.

## What to build

Give the actual review generator an explicit transition from investigation to
reporting. The controller ends investigation while enough budget remains to
produce a review; the reporting phase uses collected evidence and cannot start
new searches. Candidate findings and their supporting evidence are retained
throughout the run.

The final artifact still passes the existing verifier and independent executor
verification. Intermediate work is not posted as a completed review, and
uncertain claims remain residual risk.

## Evidence

Both native Claude Code model trials reached the fifty-minute outer limit
without a final review. Opus 5 made 105 tool calls and Opus 4.8 made 83.
Approximately 24% and 34% respectively mentioned dependency implementations.
These are topic counts, not proof that the calls were unnecessary.

There were no exact duplicate requests after excluding descriptive metadata.
Simple request deduplication therefore does not address the observed behavior.
The investigations revisit topics through different queries and source windows.

Opus 5 executed its successful persistence reproduction at tool call 76, then
made 29 more tool calls without delivering a review. Useful evidence existed,
but the run had no compulsory reporting step.

The existing 20-read cap is different: it refuses further source calls but
does not interrupt a long response or guarantee that reporting starts.

## First experiment: replay existing evidence into reporting

Use completed tool requests and their returned observations from saved runs.
Exclude hidden thinking, evaluator/oracle notes and previous model defect
claims. Preserve the original task, source identity, file/line evidence and
tool failures. Treat repository content and tool output as untrusted data.

Start with the Opus 5 checkpoint immediately after its successful reproduction.
Ask a reporting-only invocation to produce the final review without further
inspection. Keep the model fixed; use a short explicit reporting budget.
This is a test of the new reporting stage, not evidence of faster discovery
or a pure prompt-wording comparison.

If it produces a correct review, repeat the same reporting instruction on
earlier evidence checkpoints and the saved fixed-case evidence. Changing the
checkpoint is then the only experimental variable.

Measure completion, supported findings, missed findings, unsupported claims,
fixed-case regressions, time and cost separately. In particular:

- Recover the established finding with a valid trigger and accurate impact.
- Avoid invented duplicate execution where revision guards prevent it.
- Do not repeat the repaired persistence finding on the fixed snapshot.
- Do not present partial coverage as comprehensive.

These trials reuse the expensive investigation already completed. No fresh
fifty-minute discovery run is needed to screen the reporting stage.

## Runtime implementation after that screen

Implement the phase transition in the production generator, not through an
evaluation monkey-patch. Preserve collected evidence when investigation ends,
disable investigation tools during reporting and reserve an explicit reporting
allowance within the consumer's total budget.

A prompt asking the model to finish and a hard kill at the job deadline are not
this transition. The controller must actually start the reporting phase.
How to preserve the live conversation versus transfer its observations is an
implementation choice to validate against the replay result.

## Subsequent investigation improvements

Evaluate these separately, after reporting works:

1. Budget pursuit of an individual defect hypothesis, so one dependency or
   transaction question cannot consume the whole review.
2. Provide version-matched dependency context and relevant contracts directly,
   measuring whether that reduces searches without increasing false findings.
3. Improve scope selection using behavior and dependencies. The current
   directory/byte-based split groups 32 service files together and changes other
   assignments after a tiny fix; its boundaries are not sufficiently stable.

## Open questions

- At which saved checkpoint can reporting recover the finding reliably?
- Does a reporting-only stage reject the unsupported claims seen previously?
- How much time and output budget does reporting need on large cases?
- Can the runtime retain compact, sufficient evidence without promoting model
  suspicions into authoritative facts?

## Current state

Trace analysis was followed by short reporting replays and actual runtime
integration. Results, regressions and limitations are in the linked handoff.
Private traces and the audit remain under `/tmp/rito-reviewer-v2-private`,
including `review-loop-audit.json` and `reporting-phase-assessment.json`.

Anthropic's [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
describes predefined workflows, checkpoints, environmental evidence and
explicit stopping conditions as established controls. They motivate this
direction; they do not prove its performance on these fixtures.
