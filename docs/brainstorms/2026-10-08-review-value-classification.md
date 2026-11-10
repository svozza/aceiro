---
date: 2026-10-08
topic: review-value-classification
status: diagnostic-complete
---

# Assess whether a supported finding is worth posting

## Inspiration and current evidence

The user suggested classifying high-value findings after reading [Uber's uReview article](https://www.uber.com/us/en/blog/ureview/) (August 12, 2025). Uber distinguishes incorrect comments from "issues that are generally valid but not important in that specific scenario." Its architecture separates generation, grading/filtering, validation and deduplication; categories and thresholds are informed by developer feedback. Article snapshots are retained in `handoff/ureview-inspiration-20261008/`.

Our latest [matched trial](../../handoff/clef-current-reviewer-20261008/RESULTS.md) retained the same 23 findings in both source-only and full-CLEF-assisted reviews. Fast confirmation of local mechanisms did not resolve unsupported input/producer premises. A usefulness stage would answer a different question, but would not replace that unresolved validity work.

## What to build

An experimental publication decision after candidate discovery and source checking. Keep two separate outputs:

- Evidence status: supported, unsupported, or uncertain. This checks the actual input/contract, source mechanism and claimed consequence.
- Review value: worth requesting a change, routine/duplicate, or insufficient context to assess. This checks whether the supported issue has a concrete consequence and a useful remedy in this change.

A low-severity edge-case defect can be worth fixing; a confident true observation can be low value. Neither a severity label nor a classifier probability establishes usefulness. Missing deployment evidence does not automatically invalidate a defect that follows from a supported public input. Missing evidence is not a negative bug verdict.

For source-bound cases, supplied supported-input conditions and consequences are adequate; do not require proof that production has already encountered a bug. Use explicit project policy/context where available, and defer context-dependent impact judgments when it is absent.

## Approaches and recommendation

1. Start with one ordinal value classifier, operating on a fixed set of complete candidate evidence packets. Preserve an uncertainty option and a short evidence-based consequence statement. Measure its decisions before allowing it to suppress comments. This is the recommended first experiment and a plausible bounded CLEF task, not a demonstrated capability.
2. If absolute labels are unstable, compare pairs of supported findings for relative value. This can prioritize within the existing report limit, but scales less well and cannot establish that either candidate deserves publication.
3. Learn fine-grained category thresholds from explicit developer feedback later. Uber's per-language/per-category strategy depends on sufficient feedback; copying those thresholds or suppressing entire languages/categories now would be unjustified.

The first value-classification trial keeps discovery unchanged to isolate publication decisions. A subsequent, separate [specialist-discovery proposal](2026-10-08-specialized-reviewers.md) explores multiple reviewers; it does not change that first trial. A feedback warehouse is outside this initial scope. Deduplicate equivalent remarks while retaining all repair locations and the existing inline-anchor/group contract. Do not blindly copy Uber's exclusion of configuration files: our scope includes security and behavior changes in configuration.

## First experiment

Reuse saved findings to screen feasibility without another expensive discovery run. Include valid but routine observations, high-value supported defects, unsupported-trigger claims and context-dependent cases. The latest 23 findings can provide a diagnostic set; final conclusions require additional held-out cases, since these are familiar and were not selected to measure usefulness.

Label the fixed set before inference using a trusted rubric: would this project's reviewer request a change, and what source-supported consequence justifies it? Keep correctness and value labels separate. Label uncertainty explicitly and record disagreements. A single operator's provisional judgments do not become developer-feedback ground truth. Use feedback from the user or maintainers to validate project-specific value judgments; do not use model agreement as the sole usefulness label.

Freeze evidence packets, labels, wording and candidate order before calls. Compare the current publication policy with the same candidates under the proposed value policy. A model may classify source data but cannot execute reviewed programs, builds, tests, dependencies, configuration, hooks or assemblies, or acquire additional permissions.

Measure:

- High-value supported defects retained, including uncommon valid-input cases.
- Routine/duplicate comments avoided and reasons for each omission.
- Uncertain and unsupported candidates mistakenly promoted.
- Coverage findings retained when they expose a specific consequential gap.
- Latency and cost, plus stability on held-out cases.

Do not claim improvement from fewer comments alone. A large reduction in low-value output with a small, measured loss may still be useful; the user prioritizes material improvement rather than perfection. Report that tradeoff explicitly. No invented developer-time savings. Uber's reported usefulness/address rates belong to its feedback system; absence of a regenerated comment is not independent proof that the issue was fixed.

## Open question

Project-specific value boundaries need calibration: generic new-code coverage requests may be routine, while an assertion that hides a concrete regression can be worth posting. This is a proposed design; no new model trial or production change has been performed. The existing experiment infrastructure remains unchanged and running for the user's teardown decision.

## Completed first diagnostic

See [value pilot results](../../handoff/clef-value-20261008/RESULTS.md): full CLEF marks all 23 actual findings as conditionally worthy and all eight routine controls as routine in both phrasings. Two performance controls shift between routine and unknown. Value-only filtering does not change current reviews. The separate evidence axis remains unreliable for input contracts. Provisional labels and obvious authored controls limit generalization; no production change was made. The user clarified the follow-up as broad, contracts, testability and security reviewers, with context management as an explicit objective. That proposal is updated but has not run.
