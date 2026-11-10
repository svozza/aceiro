---
date: 2026-10-08
topic: specialized-reviewers
status: diagnostic-complete
---

# Independent specialist discovery, shared publication

## What to explore

The user asked whether multiple review agents should independently report findings while focusing on different areas. This complements the [value-classification proposal](2026-10-08-review-value-classification.md): specialist discovery could improve coverage, while a common publication stage controls validity, usefulness and duplicates.

The user clarified the intended design: preserve a broad reviewer and split existing prompt responsibilities across focused reviewers, with context management as an explicit objective. A starting arrangement is:

- Broad reviewer: overall behavior and interactions, including defects that fall between specialties.
- Contracts reviewer: public API changes, supported inputs, caller/producer/consumer obligations and compatibility.
- Testability reviewer: concrete changed behaviors that tests miss, ineffective assertions, failure paths and the ability to isolate a meaningful test.
- Security reviewer: authorization, sensitive data, injection and trust-boundary changes.

The current prompt already lists correctness, security, breaking API changes and missing/wrong tests. The experiment should derive responsibilities from those existing obligations. Each agent can retrieve the same full source, but concentrates its working context and investigation budget on its responsibility. Measure context size, source revisits, actual aggregate token usage and cross-file coverage to test the context-management hypothesis. Full source access alone does not establish that working context becomes smaller.

These areas can overlap; each reviewer keeps access to the full diff and BASE/HEAD source so that cross-file relationships remain visible. They work independently and receive no other reviewer's findings during discovery. Specialists return internal candidate records with source citations, input/contract assumptions, consequence and remaining uncertainty. They do not independently post public comments.

One shared controller retains the candidate union, records disagreements, checks evidence and value, consolidates equivalent remarks while preserving repair locations, and produces one artifact through the existing verifier. Agreement between reviewers is not proof; an important supported finding identified by only one reviewer must remain eligible. Aggregation must preserve each finding's necessary trigger, caller scope and source context rather than freely rewriting its meaning.

All reviewers retain the existing read-only tools/security posture. Specialization adds no execution of target source, dependencies, builds, tests, configuration, hooks or assemblies and no network or write permissions.

## Why it needs its own experiment

Prior claim-critic and fresh-verifier experiments operated on already proposed findings. They did not compare independent specialist discovery with generalist discovery. The claim-critic study improved some comments but made two good comments incomplete through local trimming; the fixed-budget fresh-verifier study lost delivery through first-stage timeouts. These demonstrate aggregation/context/budget risks, not that specialist discovery succeeds or fails.

The single-session structured-finding study was also a different change: it improved known target coverage with some false-claim tradeoffs. Its result is not evidence for multiple generators.

## Comparison

Use three arms on the same frozen PRs:

1. Current single generalist reviewer.
2. Four independent generalist reviewers, to measure the effect of additional sampling/compute.
3. One broad reviewer plus three focused reviewers, to measure specialization beyond those additional reviews.

Hold the merger/publication policy constant. Match model, tools and per-session allowances between the two four-reviewer arms; report their actual cost and latency against the normal single-reviewer baseline. Record all candidates before publication to distinguish discovery gains from filtering effects. Avoid introducing an unvalidated value classifier as a second changing variable; either use the same fixed policy in every arm or assess value in shadow mode.

Freeze prompts, source identities, budgets and grading rubric before calls; no old reviews or oracle labels reach discovery agents. Include PRs with cross-file state changes, supported edge cases, security contracts and concrete weak-test examples, plus benign/fixed controls. Familiar saved cases are diagnostic; broader conclusions require fresh held-out cases.

Measure additional distinct source-supported defects, high-value findings actually delivered, unsupported/low-value output, valid findings lost in aggregation, completion, latency and cost. More candidates, agreement counts or a shorter report alone are not improvements. The user accepts an imperfect result if the measured benefit is large enough; report any tradeoff rather than requiring zero errors.

## Sequence and current state

First test publication value on fixed candidates, then specialist discovery with that publication policy held constant. This makes both experiments interpretable. The proposal is not implemented and no new application-model sessions or coding subagents have been launched. Existing infrastructure and production remain unchanged.

## Active comparison

The user authorized the trial. See [frozen protocol](../../handoff/specialized-reviewers-20261008/PROTOCOL.md): eight public cases, 72 independent discovery sessions and 24 common publication sessions. Broad/general prompts are identical; focused roles use scope appendices. All 96 offline checks passed before discovery. No assistant-specific Decider grading, learned confidence thresholds, category suppression or Haiku comparison is included; the user explicitly deferred those. Source/context and inference contracts are frozen, production remains unchanged, and results are pending.

## Completed diagnostic

See [results](../../handoff/specialized-reviewers-20261008/RESULTS.md): all 96 sessions delivered. Focused discovery cost 9% less and used 18% less aggregate work than four generalists, with a 7% lower median input-peak proxy, but smaller per-case context in only three of eight PRs. Complete known runtime targets were seven of 13 for each multi arm and eight for single; coverage tradeoffs and qualified claims remain. One focused publisher aggregate cost is missing. No rollout recommended; no production change. Assistant-specific Decider/Haiku grading stayed separate and is unrun.
