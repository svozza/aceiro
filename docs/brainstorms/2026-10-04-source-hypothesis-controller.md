---
date: 2026-10-04
topic: source-hypothesis-controller
status: implemented-experimentally
---

# Native hypothesis controller prototype

User approved the [architectural proposal](2026-10-04-source-bound-hypotheses.md).
An isolated branch adds actual `hypothesis_check.py`, syntax registry and trusted
Go parser helper, not only an evaluation prompt override. Source data is never
executed. Existing sentence catalog, drive_session and policy verifier are reused.

Two modes share source, metadata and catalog. Fresh monolithic control keeps
the original assessment prompt/schema and projects allowed original sentences.
Structured mode constructs a compact core and source-bound witness, then assesses
the immutable record and every original sentence in a fresh session. Exact
assessed text is assembled without a final writer. The controller enforces source
ownership, exact BASE correspondence, blocking-acquisition identity, complete
claim decisions and dependency/attribution consistency. Model truth is still
untrusted; syntax checks are not semantic proofs.

Go's standard parser avoids adding a parser dependency to this first Go-only
slice. It includes lexical receiver bindings and only direct fields declared as
standard sync mutexes, keeping unknown dispatch explicit. Full metadata is sent
as lossless column rows; neither sources nor operations are truncated.

Sixteen fresh cases: verbatim precise positive and old-race negative, four paired
repetitions per workflow. Same six source files, model/high effort, SDK, role and
180-second shared allowance. Construction capped at 75 seconds; assessment uses
only the remaining time. Native representation, staged inference and projection
are a workflow bundle, not a single prompt-variable comparison. No historical
controls or automatic reruns. This is component checking of supplied concerns;
broad discovery and automatic evidence selection remain untested.

Primary requires an accurate proposed new defect with bad restart-only impact
removed, plus correct old-race rejection. Inspect all hypotheses, sentence statuses,
dependencies, final text and failures. Successful units/schema alone are insufficient.
No rollout follows; unused-case confirmation must precede general claims.
