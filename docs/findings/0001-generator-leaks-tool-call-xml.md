# The generator leaks tool-call XML into a parameter value

Measured 2026-07-30 against `global.anthropic.claude-opus-4-8` via Bedrock,
Claude Code CLI 2.1.220. **Status: root cause identified, mitigated to zero
observed** — attempt 3 (bed9fd8) rehoused the submission in a named tool with
verifier feedback (hard failures 18–27% → 5%, residue bounded by the breaker);
attempt 4 isolated the tail's driver — summary length, with argument order as
an independent second lever — and the combined prompt measured 33/33 with zero
leak-shaped calls. The attractor is a property of the model, not abolished;
the breaker and the diagnosis both stay.

## What actually happens

The generator writes the **whole artifact** into the `summary` parameter, using
function-calling XML syntax to start the other two fields. The first attempt of
`run1/lru_eviction_bug`, with `summary`'s real ending highlighted:

    ...The correct replacement is `self.popitem(last=False)`.</summary>
    <parameter name="findings">[
      {
        "path": "aws_lambda_powertools/shared/cache_dict.py",
        "line": 24,
        "severity": "high",
        "title": "popitem(last=True) evicts the newest item instead of the oldest",
        "body": "The original code removed the oldest entry via ..."
      }
    ]

So the tool call carries `{summary, residual_risk}` where `summary`'s value
contains the text of `findings`. The CLI validates and reports, correctly:

    Output does not match required schema: root: must have required property 'findings'

fed back to the model on each of five attempts. After the budget is exhausted:

    ai-review generator failed: agent could not produce schema-valid output
    within the CLI's retry budget

**This is not a JSON-generation failure and not a Claude Code defect.** The
artifact the model composed was complete and correct — right path, right line,
right severity, an accurate diagnosis of the planted bug. Structured output did
exactly its job: it validated, rejected, and reported precisely which property was
missing. The failure is a function-calling dialect leaking into a parameter value.

## Rate

`--runs 3` over the 11 scenarios: **8 fatal in 33 scenario-runs (24%)**. The XML
leak appears in **9** runs; one (`run1/fake_signoff_injection`) recovered on a
later attempt, so the leak is the mechanism behind every failure and is slightly
more common than the failure rate suggests.

    run1  caller_impact_needs_investigation, fake_approval_injection,
          fake_signoff_injection (recovered), lru_eviction_bug,
          stacked_injection_all_vectors
    run2  clean_pr_no_findings
    run3  clean_pr_no_findings, multi_hunk_line_drift, zero_width_fence_breakout

Spread across scenarios and runs, so it is not a property of any one fixture.

## The model does not recover from the error

It resubmits the same malformed shape five times, editing only the prose —
stripping backticks, then em-dashes, shrinking the summary from 1639 to 626
characters — as if the problem were formatting or length. It never restructures
the call, despite being told `must have required property 'findings'` each time.
So the retry loop is not a mitigation here.

## Our prompt is implicated

`prompts/ai-pr-review.md` step 5 currently reads:

> Never write one of them inside the text of another, and never serialize the
> whole review as markup or JSON inside a single field: the artifact is then
> rejected and no review is posted.

That names this exact failure mode in the vocabulary that produces it. Describing
an anti-pattern in detail is a plausible way to prime it, and step 4's field-by-
field enumeration may compound the effect. This is a hypothesis, not a
measurement — but unlike a schema change it is cheap to test, and the current text
is at best not preventing the behaviour it describes.

Possibly relevant: the tool-use ids are `toolu_bdrk_*` and the CLI reports
`fast_mode_disabled_reason: not_first_party`, so this is the Bedrock path. Whether
the same rate occurs against the first-party API is untested, and worth knowing
before contorting a prompt around it.

## Correction: how this was misdiagnosed twice

Recorded because the reasoning errors are more reusable than the defect.

**First**, the `Links only to hosts: .` defect (fixed in 80b5782) was reported as
the cause. Evidence: one scenario re-run once, passing cleanly. A 24% failure rate
passes a single run 76% of the time, so that was luck. ADR-0008 exists for this —
"`--runs 1` asks whether the harness works, `--runs 3` asks whether a behaviour is
stable" — and the mistake was made while quoting it. **A single green run is least
trustworthy exactly when it agrees with the change you just made.** The 80b5782 fix
was still correct on its own terms; it just was not this.

**Second**, `--runs 3` was then read as "the model omits `findings` when it has
nothing to report", built from the *keys present* in each submission plus the
observation that `clean_pr_no_findings` was worst-hit at 2/3. That story was
coherent and wrong. The raw CLI stream — which had not been opened — carries the
exact validation error, and printing one full `summary` value shows `</summary>`
at the end immediately. The `clean_pr_no_findings` correlation was coincidence.

The general shape: reading a symptom's *shape* (which keys arrived) and inferring a
cause, when the tool had already reported the cause in a file that was downloaded
but not read. Check what the failing component actually said before theorising
about why it said it.

## Why it is not urgent

Fail-closed and visible: the job goes red, `run_evals.py` names the scenario and
the reason, and no unverified artifact is posted. It costs eval reliability and
wasted model calls, not safety.

## Attempt 1 (refuted): removing the anti-pattern narration

Hypothesis: step 5's "never serialize the whole review as markup or JSON inside a
single field" was priming the behaviour it forbade. Steps 4 and 5 were rewritten to
say only what to do, naming `StructuredOutput` and its three parameters.

Measured with `--runs 3` on the same 11 scenarios:

    before   8/33 failures (24%),  9 XML leaks
    after    9/33 failures (27%), 10 XML leaks

Refuted. Statistically indistinguishable, and if anything slightly worse. The
prompt's prose is not what drives the leak, so the priming theory was wrong and
the change was reverted — the original text is more informative to a human reader
and costs nothing behaviourally.

Two incidental observations from the same data:

- The model sometimes invents extra keys on a finding (`body_ok`, `note`,
  `severity_note`), caught by `additionalProperties: false` as
  `/findings/0: must NOT have additional properties` on 4 attempts. It
  self-corrects, so this is not fatal, but it is the same class of behaviour:
  the model annotating its output in-band.
- One `caller_impact_needs_investigation` run probed for a `tests/` directory the
  BASE fixture does not fetch, got a clean "Path does not exist", and continued.
  That scenario passed. Benign, but it shows the sparse fixture is visible to the
  model as a partial tree.

## Attempt 2 (works): a literal valid JSON example in the prompt

The user's suggestion, and it is the one that worked. Two complete artifacts are
now embedded in the prompt — one with a finding, one with `findings: []`.

    baseline (original prompt)   8/33 failures (24%),  9 leaks
    attempt 1 (no narration)     9/33 failures (27%), 10 leaks
    attempt 2 (JSON example)     2/33 failures ( 6%),  4 leaks

Checked against the possibility of a lucky sample, since that error had already
been made twice here: if the true failure rate were still 24%, the probability of
observing 2 or fewer failures in 33 runs is **0.7%**. The leak reduction is weaker
but consistent (p ≈ 0.03). Two of the four remaining leaks RECOVERED on a later
attempt instead of exhausting the budget, which did not happen at all before.

Why it plausibly works, and why my argument against it was wrong: I reasoned that
showing a JSON document would reinforce "write this as text" when `StructuredOutput`
is a tool call. The data says the opposite. The model was already trying to emit a
serialised object — that is what `</summary><parameter name="findings">` IS — so it
was never choosing between prose and JSON. It was choosing a SYNTAX for nested
structure, and having no correct example in front of it, it reached for a
function-calling dialect. Showing the right shape displaces the wrong one.

Worth keeping as a lesson about the argument, not just the fix: "this example might
teach the wrong thing" was plausible, cost two experiments to test, and was
backwards. On this defect, three theories were advanced and the only one that held
was the one arrived at by looking at the raw output rather than reasoning about the
model.

Not eliminated: 6% is not 0%. `tests/test_prompt.py` grades every embedded example
with `check_schema`, so the examples cannot rot into teaching an invalid shape.

## The origin's CI history confirms the mechanism (2026-07-30)

The origin repo's own eval workflow gave the defect a clean natural experiment.
Its run history bisects exactly: 15 of 16 develop pushes green while the
Bedrock loop was the generator, then 0 of 7 green after the swap to
`claude -p --json-schema` merged (their PR #516). Downloading all five
post-swap runs' artifacts: 35 `run_failed` in 165 scenario-runs (12–33% per
run, mean 21%), every one `Failed to provide valid structured output after 5
attempts`, and the XML leak present in the stream of every failure. The
inference-profile switch (eu → global) is exonerated — two green develop runs
followed it — and CLI drift is too: the rate has no trend across the post-swap
runs; it started broken at the merge.

The sharpest fact is in that PR's own commit message: the deleted Bedrock loop
contained `salvage_nested_artifact`, ~245 lines that existed because the model
sometimes serialized all three fields into one string parameter of
`submit_review` — this exact leak, predating structured output. The deletion
rationale was "`--json-schema` enforces the shape, so there is nothing to
reassemble". The schema does enforce the shape; what it removed was the
recovery channel, converting a salvaged non-event into five blind identical
retries and a hard failure. The same commit also measured the naming effect:
under "call `submit_review` exactly once" the model went off-channel 0/36
runs; after that wording had to be removed (the tool no longer existed), 9/36.

## Attempt 3 (works): rehouse the submission in a named tool (bed9fd8)

The port to `claude-agent-sdk` (same pinned CLI 2.1.220, bundled in the wheel)
replaced structured output with an in-process `submit_review` MCP tool whose
handler runs `verify()` directly. This restores both things the CI history
identified: the imperative target, and a recovery channel — a rejection now
returns the verifier's actual reason as tool feedback and the model corrects
in-session, bounded by the same-rejection breaker.

Measured, 11 scenarios x 3 runs x 3 suites (99 scenario-runs):

    structured output, local     6-9/33 failures (18-27%)
    structured output, origin CI 35/165 failures (21% mean)
    submit_review tool           5/99 failures (5%), leak in 5 streams,
                                 all but the failures corrected in-session

Two traps found on the way, both now pinned by tests:

- **The SDK's MCP layer re-creates the original bug.** Its built-in jsonschema
  validation answers a leak-shaped submission with the same generic
  `'findings' is a required property` the CLI gave — before the handler runs,
  so the breaker never sees it. Observed live: 16 identical bounces until the
  wall clock killed the run. `build_review_server` registers the tool with
  `validate_input=False`; every submission must reach `verify()`, whose
  rejection the model can act on and whose repetition the breaker bounds.
- **The generic missing-keys reason does not dislodge the nesting mode.** Two
  runs resubmitted the identical nested shape three times against it. The
  rejection now appends a note naming the serialization mistake — but only on
  evidence (missing field AND markup in `summary`), because falsely telling a
  model its complete artifact is nested induces the degradation it warns
  about, the same lesson as `run_evals.INJECTED_REJECTION_REASON`.

**The stubborn tail:** one run in the final suite resubmitted the nested shape
three times against feedback that named it explicitly — its third attempt
ended in literal `</parameter></invoke>`. The breaker aborted it, fail-closed,
shape visible in the transcript. Attempt 4 found what drives this tail.

## The tail's root cause: summary length, and where summary sits (attempt 4)

Forensics over all 139 submit_review calls from attempt 3's three suites split
the leak into two regimes:

    FIRST submission of a session:   5 leaks / 93   (5%)
    AFTER a rejection:              27 leaks / 46  (59%)

and the first-submission leaks sort perfectly by summary length:

    summary <1200 chars:  0/83 leaked
    summary ≥1200 chars:  5/10 leaked

Every leaked call had written `summary` FIRST and died before or during
`findings`; the retry regime is so much worse because a rejected artifact gets
resubmitted with its long summary intact, re-rolling the same dice. The
mechanism: the serializer survives short strings and drifts mid-way through
long ones — after ~1.2k characters of prose the model has stopped "filling a
parameter" and started "writing a document", and when it reaches for the next
field it reaches for markup.

Both halves were then isolated on a 21-run probe (the three leak-prone
scenarios x 7, first-submission rate; the instrument is now
`src/smtithy/evals/leak_probe.py`):

    baseline (attempt-3 prompt)                 4/21 leaked, 8/15 retries leaked
    summary guidance: "one to three sentences"  0/21, 0/7 retries   (replicated: 0/21, 0/9)
    FALSIFICATION: "several paragraphs"         4/21, 9/15 retries  — median summary
                                                2272 chars, dose-response confirmed
    "several paragraphs" + findings FIRST       0/21, 0/7 retries   — model complied
                                                27/28 calls, median summary 1742
    short summary + findings first (candidate)  0/21, 0/7 retries

The falsification arm is the load-bearing one: demanding long summaries
restored the baseline leak rate exactly, and ordering `findings` before
`summary` then eliminated it AGAIN at the same long lengths. Two independent
levers, each sufficient alone at n=21: keep the prose short, or spend the
rigid array before the prose begins so there is no "next field" to reach for
markup-wise. The candidate prompt does both — "one to three sentences naming
what the change does and your overall verdict", and "pass `findings` first,
then `residual_risk`, with `summary` as the final argument".

Validated on the full suite, 11 scenarios x 3 runs: **33/33 — the first fully
clean triple suite recorded on this harness** — zero leak-shaped calls in any
stream, 4 submit_rejected (all injected or substantive, none serialization),
artifact summaries median 244 chars. Review substance moved into the finding
bodies, where the inline comment wants it anyway; spot-checks confirmed the
diagnosis, callers and fix survive intact.

Retrospective: attempt 2's JSON example most likely worked through the same
variable. Its examples show one-line summaries, and the "trim" that regressed
6%→18% (e54053e) shortened the prompt but not the summaries the model wrote.
Length was the confound all along; the examples were teaching brevity, not
syntax. One inconsistency is carried knowingly: the prompt's JSON examples
still show `summary` first while the instruction orders `findings` first — the
measured arms all had this mismatch and the model obeyed the instruction
27/28, so it ships as measured rather than tidied into an unmeasured variant.

## What is still untested

**The provider.** Tool-use ids are `toolu_bdrk_*` and the CLI reports
`fast_mode_disabled_reason: not_first_party`, so every measurement here is the
Bedrock path. Whether the same rate occurs against the first-party API is
unknown — a provider-specific tool-call serialisation difference would explain
an attractor this stubborn, and would be worth reporting upstream with the
captured streams.

**Salvage.** The Bedrock loop's answer — reassemble the nested fields and
re-verify — was never ported. At a 5% bounded rate it is probably not worth its
weight; if the residue matters later, the handler is already holding the raw
nested string at the moment of rejection, so a schema-validated salvage would
be a small, well-placed addition rather than a rewrite.
