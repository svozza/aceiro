# The review budget is the consumer's to set, and the turn ceiling is derived from it

Two ceilings bound how far the reviewer may investigate: a wall clock per attempt and a
turn count per session. Both fail closed with no artifact, both are read from the
environment, and **neither is reachable by a consumer of the reusable workflow.** So a
figure measured on one repository is every consumer's hard limit.

The budget becomes a single `workflow_call` input. The wall clock is derived from the
job's remaining time rather than from the job's timeout divided by the retry count, and
the turn ceiling is derived from the wall clock rather than set beside it.

## What made this due

Two consecutive production reviews of `svozza/artel` PR #61 exhausted a 600s budget
without submitting. The first diagnosis was provider latency, and it was wrong: it read
a per-event `usage.output_tokens`, which is a streaming snapshot. Measured against the
`ResultMessage`, the same run emitted **29,533** output tokens where the snapshots summed
to 148, with `duration_api_ms` 397,726 against a `duration_ms` of 396,325 — every
millisecond was the provider generating, at a wholly ordinary 74 tok/s.

That correction is what makes this an ADR rather than a number bump. If the wall clock
were absorbing latency, the fix would be a bigger constant. What it actually bounds is
**reasoning volume**, which is a property of the consumer's repository and the diff under
review — so a constant chosen against one repository is the wrong shape, not the wrong
value.

The measurement that generalises: a review that completed spent 397s of API time over 11
tool calls; a review that did not complete made 21 in 618s. Roughly 29s per tool call,
and the ceiling has to cover however many the work needs.

## Two ceilings, one question

`WALL_CLOCK_SECONDS` and `MAX_TURNS` are **co-limits**. They bound the same thing from
two directions, and whichever binds first ends the run.

They were also set independently, which is how raising one silently relocated the
failure. At the measured ~29s per call, a 900s budget permits ~31 calls; `MAX_TURNS` was
30. A consumer who raised the clock — had they been able to — would have hit the turn
ceiling instead, with a different message (`hit the 30-turn limit without calling
submit_review`) and no hint that the budget was no longer the constraint.

So this ADR exposes **one** knob, not two. A consumer can answer "how long may a review
take?" They cannot answer "how many agent turns should a review of my repository need?" —
that is a fact about the harness's own per-call cost, which the harness has measured and
the consumer has not.

## Why an input, and not the environment variable that already exists

`CC_WALL_CLOCK_SECONDS` and `CC_MAX_TURNS` are already environment-overridable, and that
reads like the knob already existing. It does not, through the interface consumers use:

- A caller of a reusable workflow **cannot inject environment variables** into the
  callee's jobs.
- `timeout-minutes` on a called workflow's job is the **callee's**, not the caller's.

So the env vars serve direct invocation — the eval runners, local runs — and nothing
else. Every consumer inherits both constants with no supported remedy. The remedy has to
be an input because that is the only channel the interface has.

Verified rather than assumed: `jobs.<job_id>.timeout-minutes` accepts the `inputs`
context (GitHub's context-availability table lists `github, needs, strategy, matrix,
vars, inputs`), so the job ceiling can follow the input directly.

## Why the budget is derived from remaining time, not from the timeout divided by attempts

Today the per-attempt clock is sized so that `MAX_ATTEMPTS` of them fit the job:
`WALL_CLOCK_SECONDS * MAX_ATTEMPTS + backoff < timeout-minutes`, pinned by
`test_workflow_shape`. That reserves a third of the job for each attempt.

**A wall-clock timeout does not retry.** The `timed_out` branch fails the run on the
attempt that hit it, so `MAX_ATTEMPTS` defends against API errors alone. The reserve's
worst case therefore requires two attempts to die by `api_error` at nearly the full wall
clock — which has never been observed (zero `api_errors` across 51 eval sessions and every
production run), while the failure that does occur uses one attempt's worth of a
three-attempt reserve and leaves the rest of the job unused.

Rationing against the unobserved case to constrain the observed one is the wrong trade. A
budget computed from **time actually remaining** does not have to make it: one deep
attempt may use nearly the whole job, and three attempts that fail fast on API errors
still fit, because an attempt that ended early returns its unused time to the next one.

This also removes the coupling rather than re-encoding it. Deriving the per-attempt clock
in YAML would put `MAX_ATTEMPTS` in a second file and need a second pin; deriving it from
the deadline puts it in one place, and that place is the module the constant already
lives in.

## What the turn ceiling promises after this, stated plainly

`MAX_TURNS` stops being a fixed backstop and becomes proportional to a consumer-supplied
number. That is a real change to what the bound guarantees and it is recorded here rather
than left to the implementation.

What it still guarantees: the session cannot run unbounded, it fails closed with no
accepted submission, and exceeding it yields no artifact. What it no longer guarantees: a
ceiling the consumer cannot influence. A consumer who sets a large timeout gets
proportionally more turns.

This is judged acceptable because the turn count is **not a security bound**. The bounds
that contain what a review can do are the read-only tool set, `DISALLOWED_TOOLS`, the
quarantine tree, and the policy the prover checks — none of which move. `MAX_TURNS`
bounds *cost and runaway*, and a consumer choosing to spend more of their own runner
minutes on their own repository is the decision the input exists to give them.

The ordering is what gets asserted: the clock must bind before the turn ceiling, so a
review that runs out of room reports the constraint the consumer set rather than one
they did not.

## Why `effort` is not exposed alongside it

The same generalisation argument reaches `effort`, and it is refused for a reason the
budget does not share.

`effort` is unset today, so the CLI resolves it per model. The eval suite grades the
prompt at whatever that resolves to, and ADR-0008 makes `--runs 3` the obligation for a
prompt version. A consumer setting `medium` would run a generator combination **nothing
has measured**, on a harness whose value rests on findings being trustworthy enough to
act on — and `provenance_boundary_adjacent_bug` already flakes 1-in-3 at the default, so
degraded steering would be invisible to the consumer.

The asymmetry that decides it: the budget is a **capability limit that fails a
consumer's run** with no remedy. `effort` is a **quality dial with a defensible default**
whose alternatives are unmeasured. One is a defect; the other is a product decision
carrying an eval obligation. If `effort` is ever exposed it should carry the prompt's own
discipline — the harness declaring which level the evals ran at, and any other value
documented as unmeasured.

`MAX_ATTEMPTS` and the API-error backoff are likewise not exposed: they are properties of
the **provider**, not of the consumer's repository, and they do not bind on the measured
failure.

## The deadline crosses a job boundary

The input reaches `cc_loop` as a deadline in the environment, which is the shape §4e was
burned by: a value that crosses a job boundary is joined by YAML, and YAML is the layer
no unit test reads. Renaming a `decline` output once left all 1,951 tests green while the
poster read nothing.

So the deadline gets the same treatment as `decline.OUTPUT_ENV`: one declared mapping,
asserted in both directions against the workflow.

The absent case is deliberately **tolerated** rather than fail-closed, because the eval
runners invoke `cc_loop` directly and set no deadline — an absent deadline falls back to
the module's own constant. What makes that safe is the wiring assertion, not a runtime
check: the workflow forgetting the variable is caught by the pin, not by the run. A
deadline that is *present but unparseable* is a different case and refuses, because
garbage means the wiring is broken rather than absent.

## Consequences

- One new input, `agent-timeout-minutes`, defaulting to the current 50 so existing
  consumers see no change. It governs both the job ceiling and the generator's deadline,
  from one value.
- The arithmetic pin changes meaning: it stops asserting that
  `WALL_CLOCK_SECONDS * MAX_ATTEMPTS` fits the job and starts asserting that the reserve
  leaves room for the steps after the generator. The old pin's purpose — catching a
  budget raised without the job following — is served by the input being the single
  source.
- `MAX_TURNS` becomes derived. The constant stays as the fallback for direct invocation,
  and the measured seconds-per-tool-call it derives from is stated as a measurement so
  re-measuring moves it.
- A consumer can now make a review take an hour. That is the point, and the runner
  minutes are theirs; `timeout-minutes` remains a ceiling rather than a reservation, so
  the cost is only incurred by runs that use it.
- Not addressed: the reviewer has no way to tell a consumer that their budget is the
  reason a review was thin. A timeout reports itself, but a review that submitted
  *something* under pressure looks identical to one that had room. The `session_usage`
  records make the distribution visible after the fact, which is the input this decision
  wanted and did not have.
