---
date: 2026-10-08
topic: assistant-quality-grading
status: diagnostic-complete
---

# Decider quality grading inspired by uReview

The user highlighted uReview's assistant-specific quality prompt and confidence score, semantic merging, and category suppression based on historical developer value. This is separate from the active broad-plus-focused reviewer comparison; the user explicitly requested no mixing into that experiment.

After discovery comparison, reuse frozen candidate records with origin assistant, language, comment category and bounded source evidence. Test grading of evidence, consequence and actionability with a rubric suited to the review responsibility. Keep factual support distinct from conditional value. Deduplicate while preserving repair locations, then evaluate category policy. Our coarse value pilot does not establish calibrated correctness confidence or learned per-language thresholds.

Confidence thresholds require held-out evaluations and project-specific human feedback. Do not invent history or copy Uber's thresholds. Record prediction confidence and actual errors by role/language/category first; sparse groups must remain explicit. A classifier's probability is not an empirical usefulness rate. Do not add category suppression to the current comparison.

The user requested Haiku 5.5 for the same task if Decider performance is weak. Verify availability and exact provider/model ID before that trial; do not silently substitute another Haiku version. Compare using the same frozen candidates, evidence, rubric and grading, with cost/latency as well as supported-findings preservation. The current discovery model remains unchanged. No model comparison has been run from this note.

## Completed first matched diagnostic

See [quality-grader results](../../handoff/quality-grader-20261008/RESULTS.md): 47 actual candidates with fixed evidence and role-specific criteria. Full CLEF failed the predeclared diagnostic rule, so the exact available Haiku5.5 model was compared with fresh Opus. Haiku was about3.8times faster, with similar handling of key contract uncertainty, but conservative/inconsistent deferrals remain and the reported costs do not establish savings. Labels provisional; source-scope ambiguity recorded without after-output tuning. No confidence thresholds, category suppression or production rollout. A broader advisory/shadow trial with independent human readiness labels is the next useful validation.
