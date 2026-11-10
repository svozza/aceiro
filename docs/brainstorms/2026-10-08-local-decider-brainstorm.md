---
date: 2026-10-08
topic: local-candidate-decider
status: proposed-after-source-investigation
---

# Local decisions about candidate bugs

## What to test

The user proposes Strands Decider as a local model that can repeatedly assess suspected bugs, letting the reviewer examine more candidates without another expensive reasoning session for every check. This is a useful new direction: add a fast, differently trained classifier to the evidence loop. Start by measuring narrow source-grounding decisions, then assess whether those decisions improve full reviews.

Strands Decider uses a Qwen3.5-2B torso, LoRA and a pointer head; it chooses supplied options in a forward pass and cannot write an explanation or patch. Multiple different questions can share one encoded state through a prefix cache. The questions cannot see each other's text, but their errors remain correlated because they use the same model and evidence. Repeating the identical state/question does not add independent votes or justify higher confidence.

## Concrete integration

When the reviewer has a candidate, send the local assessor a compact evidence packet: candidate's atomic claim, exact HEAD source, corresponding BASE source when relevant, essential caller/guard context, and explicit unknowns. No reviewer verdict, confident rationale or requested preferred answer. The trusted harness resolves and redacts source itself; the caller cannot select a model, server URL, arbitrary filesystem root or executable operation.

Ask a fixed small set of questions using `choice` with **supported / contradicted / insufficient evidence**:

- Does the cited source support the claimed observation?
- Does the supplied BASE comparison establish that this behavior changed?
- Does the concrete input/path shown reach the alleged failure, or does a guard block it?

Use each answer as advice. A contradiction or missing premise should prompt a targeted Read/Grep lookup, within the existing source/time bounds, followed by a new decision using the additional evidence. The ordinary reviewer owns the final finding and can disagree. No automatic suppression, promotion or severity adjustment in the first prototype. At most one bounded recheck after new evidence; no “ask until it agrees.”

The assessor cannot infer source that was omitted. “Insufficient evidence” is not “not a bug.” A result alone does not prove causality, contract intent or deployment behavior. Literal source membership/name equality/range validity stays deterministic where possible; the model is for semantic judgments beyond those checks.

## Alternatives considered

| Approach | Benefit | Risk / decision |
| --- | --- | --- |
| Narrow advisory checks, recommended | Test inexpensive independent source judgments and feedback without risking blanket vetoes | Extra protocol/evidence cost; needs measured coding accuracy |
| One whole-claim bug verdict | Simple first comparison, captures whether model can judge broader concerns | Deep multi-file reasoning exceeds likely capability/window; measure as control |
| Automatic bug gate | Potentially suppress false findings without frontier calls | Could remove valid findings confidently; defer until domain calibration and preservation are established |

The initial recommendation is a component capability test, not another always-on full-review pipeline. Later integration is worthwhile only if the assessor catches concrete errors while retaining valid claims and remains cheap enough on the actual host.

## Model and runtime findings

The October 1 blog describes v19. Repository main now documents v21. Source investigated at commit `63d24ae286e50105fba0bd1db15b0e243aea4650`; official v21 metadata at revision `2b52a6235c1b8306bbfa30b00b9d4b74b63a39f5` sets **4096 tokens** and bfloat16. Base provenance points to Qwen3.5-2B-Base revision `b1485b2fa6dfa1287294f269f5fb618e03d52d7c`, which upstream describes as inferred from training-time main. Pin every artifact used in a trial rather than letting the package/checkpoint/base float.

Default serving truncates long state; enable `strict_window` and treat oversize as unavailable, recording exact token counts. A larger nominal base-model context is not evidence this classifier is trained/calibrated for larger inputs. Question count and source-window selection need explicit bounds. Payloads should retain the guard/counterexample, not just the suspected line.

The README's confidence claims concern short classification tasks. The evaluation documentation explicitly warns that calibration differs on long documents and answer-adequacy tasks. A 0.9 score is not a demonstrated 90% probability of code correctness. Measure calibration and false-positive/false-negative behavior on our own source-claim domain; no default threshold is adopted from the weather demo.

This host has four x86 CPU threads and no detected NVIDIA tool. CPU inference is supported; upstream reports much slower CPU latency (about 1.6 seconds for a short ask on an eight-thread Xeon) than its 115ms GPU median. Measure warm/cold latency on actual code packets. Upstream documents a shared per-request engine state race; start with one request at a time. Batching distinct questions inside one request is still useful.

## Security boundary

Running an operator-installed, pinned inference runtime is distinct from allowing the reviewer to execute Python or target code. The reviewer keeps Read/Grep/Glob and a narrowly shaped assessment capability if a later integration is authorized. The assessor receives data and returns fixed choices/probabilities, with no agent loop, tools, network access, repository mounts, credentials, builds, tests, project configuration, hooks, plugins or assemblies.

Stage approved runtime/model artifacts outside reviewed repositories. Use safetensors exports; no remote model code; pin base/adapter/tokenizer/package/dependencies; disable download/network after staging. The inspected loader uses standard Transformers classes and explicitly `weights_only=True` for its optional `.pt` head path; choose the safetensors export anyway. Bound source size, question count, request time and worker resources, and serialize evaluation. Returned decisions remain untrusted and cannot override the existing final verifier or source-access policy.

## First experiment

Prepare a frozen small component set from retained, source-audited claims: real bugs, false literal observations (e.g. singular/plural metric-name collision), preexisting behavior, guard-blocked paths, and genuinely missing contracts. Include both bug and non-bug cases with comparable source detail. Group variants by repository/underlying defect when splitting development/calibration versus held-out checks; reused public cases are diagnostic, not unseen accuracy evidence. Keep labels out of inference packets.

Compare the whole-claim choice against the fixed decomposed questions on identical evidence. Use one frozen neutral question wording and then a separate fixed paraphrase/order robustness condition—correlated consistency, not extra independent votes. Add a source-withheld condition to see whether decisions depend on evidence rather than claim tone. Measure rejection of false claims, retention of valid cores, handling of unknowns, confidence calibration, strict-window coverage, warm/cold latency and memory. Report unresolved and oversize cases instead of dropping them. Do not run another full-PR model trial before this shows useful discrimination.

## Current state

Research and proposed design are complete; evidence and exact upstream/configuration snapshots are retained in [the investigation handoff](../../handoff/strands-decider-investigation-20261008/README.md). No package installation, weight download, local inference or application change has run. Main and Rito are unchanged. Next concrete step: stage a CPU-only pinned runtime and run the bounded component capability check before choosing any automatic integration policy.
