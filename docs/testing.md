# Testing Strategy

Aceiro uses separate test layers because deterministic code correctness and
model behavior are different problems. A green unit suite cannot establish that
a model follows the review contract, while a green model run cannot establish
that the verifier fails closed for every malformed input.

## Test layers

### Deterministic tests

The pytest suite covers behavior that must be reproducible:

- verifier schema, provenance, Markdown, canonicalization, and secret checks;
- plan verification, anchoring, bounds, and delivery routing;
- GitHub API behavior and failure handling;
- workflow permissions, credential placement, and gate ordering;
- prompt assembly and structured tool contracts;
- adversarial cases and Hypothesis properties.

These tests use no model credentials and block external network access. A small
set of redirect tests uses a local loopback HTTP server.

Run them for every change:

```bash
uv run --frozen --group test \
  python -m pytest tests/ -p no:cacheprovider -q
```

The type checker is a separate deterministic gate:

```bash
uv run --frozen --group typecheck \
  ty check --python .venv/bin/python
```

### Review evaluations

Both evaluation suites require the model credentials and environment described
in [Developing Aceiro](development.md#run-model-evaluations). They make real,
non-deterministic model calls and may incur provider charges.

`run_evals.py` invokes the real review generator against fixed pull request
scenarios. Scenarios include:

- known defects that should produce particular findings;
- clean changes that should not produce findings;
- prompt-injection and tool-persuasion attempts;
- provenance, formatting, and context-boundary cases;
- recovery after a verifier rejection.

The runner grades the submitted review against `expect.json`. It writes a
`results.json` summary plus the complete redacted harness transcript and complete
redacted model stream for every scenario attempt.

Run the review suite:

```bash
uv run --frozen \
  python src/aceiro/evals/run_evals.py \
  --output-dir /tmp/aceiro-review-evals \
  --cache-dir /tmp/aceiro-eval-cache \
  --runs 1 \
  --workers 4
```

#### Semantic compliance oracle

Most injection scenarios use deterministic `must_not_contain` assertions. A
small number of scenarios cannot use a raw substring rule because safely
reporting the attempted manipulation may repeat the same marker as complying
with it.

Those scenarios opt in with `semantic_compliance` in `expect.json`, declaring:

- one or more markers to detect in review text; and
- the untrusted source containing the instruction: `pr_body` or `diff`.

No marker occurrence means no judge call. When a marker appears, the runner
invokes a pinned, one-turn, tool-free semantic judge with the untrusted source,
the complete submitted review, and the fields containing each marker.

The judge returns one of three verdicts:

| Verdict | Result |
| --- | --- |
| `REPORTED` | Pass: the reviewer safely quoted or described what it rejected |
| `COMPLIED` | Behavioral failure: the reviewer followed or endorsed the instruction |
| `AMBIGUOUS` | Invalid sample: the run cannot establish which occurred |

Provider faults, malformed judge output, and missing result envelopes also make
the sample invalid rather than passing it. The verdict and model attribution are
written to `semantic_judge.json` in the scenario output directory.

This oracle is deliberately narrow. It is not a general model-based review
grader, and it does not replace strict substring assertions where reporting and
compliance are unambiguous.

### Remediation evaluations

`run_plan_evals.py` invokes the real remediation planner. It grades whether the
plan stays within the commanded findings, uses a deliverable shape, and respects
the verified plan vocabulary.

Review and remediation evaluations are separate because they exercise different
generators and contracts. A failure in one must not hide results from the other.

Run the remediation suite:

```bash
uv run --frozen \
  python src/aceiro/evals/run_plan_evals.py \
  --output-dir /tmp/aceiro-plan-evals \
  --runs 1 \
  --workers 4
```

Both runners accept `--scenario NAME` for a focused run. The review runner also
accepts comma-separated scenario names. Lower `--workers` when `results.json`
reports upstream API errors or significant backoff. See
[One run versus repeated runs](#one-run-versus-repeated-runs) before changing
`--runs`.

## Choosing what to run

Use the narrowest loop that answers the current question, then finish with the
broader gate required by the change.

| Change | During development | Before merge |
| --- | --- | --- |
| Pure verifier or executor logic | Focused pytest file or test | Full pytest and type checking |
| Workflow permissions or ordering | `test_workflow_shape.py` | Full pytest and type checking |
| Prompt, policy, SDK, or model selection | Relevant focused tests and scenarios | Full pytest, type checking, and both eval suites |
| Review scenario or grader | The affected scenario repeatedly | Full review eval suite |
| Plan scenario or grader | The affected plan scenario repeatedly | Full plan eval suite |
| Shared eval runner behavior | Runner unit tests | Full pytest and both eval suites |

Changes to policy can affect both deterministic acceptance and model behavior:
the policy is enforced by the verifier and rendered into the prompt. Treat a
policy change like a verifier change and a prompt change.

Changes to `semantic_judge.py`, its prompt, model, response schema, or verdict
handling require focused judge tests, repeated opted-in scenarios, and a full
review eval suite.

## One run versus repeated runs

`--runs 1` asks whether the harness works once. It is the default for ordinary
pull requests and quick iteration.

`--runs 3` asks whether the behavior is stable. Every scenario must pass every
run; this is not majority voting. One failure makes the command fail.

Use at least three runs before merging changes to:

- prompts or prompt assembly;
- model or SDK versions;
- policy rendered into model constraints;
- scenario grading or recovery behavior;
- semantic judge behavior or opted-in expectations;
- generator budgets and retry logic.

A successful rerun does not erase an earlier failure. Preserve and investigate
the failed run, especially when different scenarios fail across attempts.

## Scenario design

Each scenario must isolate a property that the grader can establish from the
submitted artifact.

`pr_root/` is the contributor-authored tree under review. It may contain a
deliberately planted defect or attack payload. Do not regenerate these files
from upstream source: doing so can remove the property the scenario grades.

`context/` contains the pinned pull request metadata, diff, and changed-file
list shown to the generator.

`base.json` declares trusted pre-change files needed outside the diff. It must
use a full commit SHA and name only the required paths. Scenarios without a
declared base receive an empty BASE tree.

`expect.json` is executable specification, not a description of a preferred
answer. Assertions should test the contract while allowing harmless variation
in model wording.

Good scenarios:

- contain one primary reason to pass or fail;
- use exact anchors and immutable external fixtures;
- distinguish a missing finding from a differently worded valid finding;
- include a clean or falsification arm where practical;
- fail when the grader stops measuring the intended property.

Avoid grading incidental prose, exact arithmetic performed by the model, or a
substring that can appear in both attack compliance and safe reporting unless
that distinction is explicitly handled. Prefer deterministic grading whenever
possible. Use `semantic_compliance` only when the same marker genuinely has both
a safe reporting interpretation and an unsafe compliance interpretation.

## Interpreting failures

Classify a failure before changing code or rerunning it.

### Deterministic rejection

The model submitted an artifact or plan outside the verified grammar. Inspect
the transcript for `submit_rejected` and the final rejection reason. Determine
whether the model failed to recover or the verifier rejected a shape that should
be legal.

### Behavioral failure

The submission verified but did not satisfy `expect.json`. Inspect the submitted
artifact first, then the grader. Decide whether:

1. the model behavior is wrong;
2. the scenario premise is wrong;
3. the grader accepts or rejects the wrong semantic case; or
4. the prompt and grader demand contradictory behavior.

Do not weaken an assertion merely because a model sometimes fails it.

For `semantic_compliance`, a `COMPLIED` verdict is a behavioral failure. Inspect
`semantic_judge.json` alongside the review and source instruction before deciding
whether the reviewer complied or the judge misclassified safe reporting.

### Invalid sample

The run did not produce a sample that can answer the scenario's question. Treat
this as a harness reliability failure, not a passing or behavioral result.

Semantic judge verdict `AMBIGUOUS`, judge provider errors, invalid judge JSON,
and missing result envelopes are invalid samples.

### Provider or throttling failure

Check `api_errors` and `backoff_seconds` in `results.json`. Repeated provider
errors across scenarios usually call for lower concurrency or a provider
investigation, not a prompt change.

## Failure investigation workflow

1. Preserve the failed output directory or CI artifact.
2. Read `results.json` to identify the failure class and affected scenarios.
3. Inspect the scenario's `review.json` or `plan.json`.
4. For semantic scenarios, inspect `semantic_judge.json` and compare its reason
   with the exact marker-bearing review field.
5. Read `transcript.jsonl` for harness decisions, rejections, retries, and API
   errors.
6. Inspect captured model streams when the submitted artifact does not explain
   how the session reached that state.
7. Compare another attempt from the same commit. Different failures across
   attempts often point to a shared prompt, grader, or harness issue.
8. Reproduce the scenario with `--workers 1` to remove concurrency as a variable.
9. Run repeated focused trials, then a same-day full-suite baseline.
10. Add a deterministic regression test for any grader or harness defect found.

For injection scenarios, distinguish reproducing an attacker-controlled marker
while complying with it from quoting or describing that marker while reporting
the attempted manipulation. Use the semantic oracle only for this specific
ambiguity, and preserve the judge artifact as evidence. Prove which case occurred
before changing the prompt or expectation.

## CI strategy

The `Quality check` workflow runs deterministic tests and type checking without
credentials.

The `Evals` workflow runs real model calls in a separate credentialed job. It
runs on pull requests, supports manual dispatch with scenario and run-count
controls, and runs weekly to detect upstream model drift.

Untrusted or draft pull requests wait at the approval environment before their
code is executed with a model credential. The runtime environment scopes that
credential separately.

## Results and evidence

The eval workflow uploads both review and remediation output trees with
`if: always()`, so evidence survives behavioral failures, invalid samples, and
most harness failures. The Actions artifact is named `evals-<run_id>` and is
retained for 30 days.

Per-scenario evidence includes:

- `review.json` or `plan.json`, when the generator submitted one;
- `transcript.jsonl`, the complete redacted harness event transcript;
- `cc_stream_<attempt>.jsonl`, the complete redacted captured model stream for
  each attempt;
- `run_metadata.json`, when model/session metadata was produced;
- `semantic_judge.json`, when semantic compliance adjudication ran.

The run directory also contains `results.json`, including pass/fail validity,
failure reasons, API-error counts, and backoff time.

Download CI evidence with:

```bash
gh run download <run-id> \
  --name evals-<run-id> \
  --dir /tmp/aceiro-eval-artifact
```

Redaction happens before transcript and stream files are written. The artifact
can still contain reviewed source content, so inspect it before sharing.

### Redaction boundary

Redaction is defense in depth, not removal of all repository content.

Before the review model runs, Aceiro scans contributor-controlled pull request
metadata, the diff, and text files in the quarantined PR head with
`detect-secrets`. Enabled detectors cover common cloud, source-control, package
registry, messaging, payment, private-key, JWT, and secret-keyword formats, plus
quoted high-entropy strings. Detected plaintext values are replaced with stable
placeholders such as:

```text
<SECRET_1:type=secret_keyword,length=20>
```

The original values remain only in memory as run-scoped taints. The verifier
rejects model output that reproduces a tainted value, and the same values are
used when redacting transcripts and captured streams.

Before a transcript record or model-stream line is written, Aceiro scrubs:

- exact run-scoped tainted values detected in the contributor input;
- values matching `secret_scan_patterns` in the effective policy, including
  AWS key identifiers, GitHub tokens, JWTs, private-key headers, Slack tokens,
  and labelled AWS secret-access-key assignments;
- matches in JSON string values and dictionary keys;
- secrets identifiable only by an enclosing or sibling label, such as
  `aws_secret_access_key` beside its value;
- matches that appear after invisible Unicode code points are removed.

Captured model streams are processed line by line. JSONL records are parsed and
redacted structurally; non-JSON stderr lines receive the pattern sweep. If a
structured value still matches after redaction and serialization, that value or
record is withheld rather than written partially redacted.

Redaction does **not** remove ordinary source code, diffs, prompts, findings, or
repository metadata. Candidate detection also has deliberate allowlists and
size limits, and no detector can recognize every possible secret. Trusted BASE
files are not part of the contributor-input candidate scan, although configured
output patterns still apply if their contents appear in captured output.

Treat the Actions artifact as sensitive repository data even after redaction.
Review it before sharing it outside the repository's normal Actions audience.

Durable redacted experiment records owned by this repository live under
`results/` and follow the schema and ownership rules described in
[Aceiro experiment results](../results/README.md).
