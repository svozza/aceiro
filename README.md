# smtithy

A harness for agents that are never trusted, only verified: a model proposes, a
deterministic checker finds no counterexample, and a trusted executor acts.

The architecture is **generate → verify → execute**. Model output is untrusted
data, never instructions. A deterministic verifier proves it satisfies a policy
held as reviewable data. A trusted executor renders the verified artifact and
performs the effect. The generator holds no write credential and calls no write
API.

The AI PR reviewer is the first application. Review-and-remediate, where the
verified object is a plan rather than a flat record, is the second — and the
reason there is an SMT solver in the name.

Start with [CONTEXT.md](CONTEXT.md) for the vocabulary, then
[docs/adr/](docs/adr/) for the decisions. [ADR-0003](docs/adr/0003-plan-prover-in-typescript-via-z3-wasm.md)
is the one that explains the shape of the codebase.

## Status

Being extracted from a consuming repository, in sequence.

Public deliberately: on a free plan, Actions environment protection rules — the
approval gate ADR-0006 is about — only work on public repositories, and Actions
minutes are only free there. Going private silently strips the gate's protection
rule, which is one of three fail-open modes observed for real and recorded in
[the ADR-0006 addendum](docs/adr/0006-addendum-observed-gate-failures.md). What is here now is
the artifact verifier and its tests, moved behaviour-preserving:

| | |
| --- | --- |
| `src/smtithy/verify.py` | the verifier — the security boundary. Interprets `policy.json`; allowlists a safe grammar and rejects the whole artifact otherwise. |
| `src/smtithy/artifact.py` | fence escaping, the Unicode default-ignorable table, secret redaction. |
| `src/smtithy/diff_map.py` | the ONE diff parser. Verification owns the walk. |
| `src/smtithy/policy.json` | the declarative policy — the reviewable security object, hashed into the transcript. |
| `tests/` | goldens, hypothesis properties, and a 486-line adversarial corpus that is the executable spec of the threat model. |

And the plan prover, in TypeScript:

| | |
| --- | --- |
| `ts/plan/policy.ts` | the plan half of `policy.json`, typed. Loaded, never constructed: there are no defaults, because a default would be a rule nobody reviewed. |
| `ts/plan/schema.ts` | the shape gate, carrying ADR-0004's three reserved closures. Runs before the solver — an encoding built from an unchecked shape is reasoning about a structure it assumed. |
| `ts/plan/prove.ts` | ordering and frame conditions, asserted **negated** so `unsat` means the policy holds on every path. Returns a counterexample on `sat`. |

Still to arrive: context acquisition, rendering, and GitHub I/O.

## Running the evals

The deterministic suite runs on every pull request, and so do the evals —
real model calls against fixed scenarios, both the review suite and the plan
suite (ADR-0008, revised: a full pass costs on the order of a dollar, and in
this repository every pull request is a harness change, which is exactly what
the evals grade). Untrusted or draft authors wait at the `ai-pr-review`
environment's required reviewer before their code runs with the credential.
Also available:

- **The `run-evals` label**, to re-run a pull request whose head has not moved.
- **`workflow_dispatch`**, with `runs` (1 or 3) and an optional single `scenario`.
- **Weekly on `main`**, which is the only way upstream model drift gets caught —
  no pull request of ours would trigger it.

`--runs 1` is the default and answers *does the harness work*. `--runs 3` answers
*is this behaviour stable*: `run_evals.py` accumulates failures across runs and
exits non-zero if any scenario failed on any run, so it is three independent
chances to catch a flake, not majority voting. Use 3 before merging a prompt,
policy or verifier change. **A single green run is not evidence of stability** —
that is the misreading ADR-0008 exists to guard against.

Expect the judgement-grading scenarios (`caller_impact_needs_investigation`,
`provenance_boundary_adjacent_bug`) to flake occasionally, rather than the
injection ones, which either fence correctly or do not. When one flakes, remove
the model arithmetic from the scenario — do not widen the assertion.

### Running the evals locally

The local loop is how eval work actually happens — CI is the gate, not the
development environment, and pushing to re-run costs a round trip per data
point. The suite runs from the working tree, so an uncommitted prompt or
verifier change is measurable immediately:

```bash
CLAUDE_CODE_USE_BEDROCK=1 ANTHROPIC_MODEL=global.anthropic.claude-opus-4-8 \
AWS_PROFILE=<profile> AWS_REGION=eu-west-1 \
DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_AUTOUPDATER=1 \
CLAUDE_CONFIG_DIR=/tmp/claude-cfg \
python src/smtithy/evals/run_evals.py \
  --output-dir /tmp/eval-out --cache-dir /tmp/eval-base-cache --runs 3
```

Any Python 3.13 venv with `pip install --require-hashes -r requirements.txt`
works, and any AWS profile whose credentials can `bedrock:InvokeModel` on the
inference profile named in `ANTHROPIC_MODEL` (the same pair CI's scoped session
policy allows). `CLAUDE_CONFIG_DIR` points somewhere disposable so the run
can't pick up a developer's own Claude Code settings. Expect a `--runs 3`
suite to take on the order of fifteen minutes at the default `--workers 4`,
and a full pass of both suites to cost on the order of a dollar.

`--scenario NAME` runs a single scenario while iterating on it — then finish
with the full `--runs 3` before believing the change, since a fix measured on
one scenario has been observed to move the failure to another.

The plan generator has its own suite over the same layout plus a
`context/finding.json` (the commanded finding, ADR-0007), graded on the
ADR-0009 shape invariants rather than step inventories. CI runs it in the
same gated job as the review suite; locally:

```bash
# same environment as above; no --cache-dir, plan scenarios need no BASE
python src/smtithy/evals/run_plan_evals.py --output-dir /tmp/plan-eval-out --runs 3
```

Each scenario's output directory holds the forensics when something fails:
`transcript.jsonl` carries the harness's own events (`run_failed`,
`submit_rejected`, `api_error`), and `cc_stream_N.jsonl` is the captured
model stream for attempt N — the place to look for leak-shaped submissions
(`</summary>`, `<parameter name=`).

### The leak probe

`evals/leak_probe.py` is the cheaper instrument for prompt changes. It runs
leak-prone scenarios N times and mines the captured streams for one thing:
did each `submit_review` call arrive with `findings` present, or did the
artifact leak into `summary` as function-calling XML? No grading — many more
data points per token than the full suite, which is what isolated the two
leak levers (summary length, argument order) in
[finding 0001](docs/findings/0001-generator-leaks-tool-call-xml.md):

```bash
# same environment as above
python src/smtithy/evals/leak_probe.py \
  --out /tmp/probe-out --cache-dir /tmp/eval-base-cache --n 7
```

It reports first submissions separately from post-rejection retries (the two
regimes leak at very different rates), and summary-length stats against the
measured 1200-character boundary even when nothing leaked — creeping summary
length is the leading indicator. Use it the way finding 0001 did: probe a
same-day baseline, change one variable, probe again, and include a
falsification arm — deliberately induce the failure — before trusting a zero.

**Known defect, restructured away from hard failure:** under the CLI's
`--json-schema` structured output, the generator sometimes composed a correct
artifact but serialized the whole thing into the `summary` parameter using
function-calling XML (`</summary>` `<parameter name="findings">`), so the
required `findings` property was genuinely absent and the CLI burned its five
internal retries on identical attempts — a hard scenario failure with no
recovery channel, ~20% of scenario-runs at its worst.
[docs/findings/0001](docs/findings/0001-generator-leaks-tool-call-xml.md) has the
numbers, and records the two wrong diagnoses and one refuted fix that preceded
the mitigations.

The artifact now arrives through the in-process `submit_review` tool instead
(see `cc_loop.build_review_server`), which changes the failure's shape rather
than assuming it away: a leak-shaped submission reaches `verify()`, gets a
rejection that names the serialization mistake, and the model corrects it
in-session — bounded by the same-rejection breaker, never a spiral. Two
subtleties are load-bearing and pinned by tests: the MCP layer's own schema
validation must stay OFF (its generic "required property" bounce hides the
submission from the breaker — observed live, 16 identical bounces to a wall-
clock timeout), and the nested-artifact note only fires on evidence, because
falsely telling a model its complete artifact is nested induces the very
degradation it warns about. Fail-closed throughout — the job goes red, no
unverified artifact is posted — so any residue costs eval reliability, not
safety.

`tests/test_verify_adversarial.py` is not an ordinary test file. Its own
docstring calls it the living spec of the threat model, where **a case that
starts passing is a regression in the verifier's safe grammar**. Treat a
newly-green case there as a defect until proven otherwise.

## Running the tests

Python 3.13. Dependencies are hash-pinned, and installed that way in CI.

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

And the prover, on Node 24:

```bash
npm ci
npm run typecheck
npm test
```

Two runners, deliberately (ADR-0003): pytest for the artifact verifier, whose
risk is `markdown-it` rendering behaviour and a hand-tabulated Unicode table, and
`node --test` for the plan prover, whose risk is reachability reasoning. Both run
until the verifier is ported last, behind a differential oracle — the Python is
the oracle, so the second runner is not saved until the port finishes.

### Reading the prover's tests

The policies are asserted **negated**, so `unsat` means "holds". That makes one
failure mode invisible to ordinary tests: an encoding that is accidentally
*contradictory* returns `unsat` for every plan — a prover that approves
everything, and looks green doing it. So the load-bearing cases are the ones
named `CATCHES`, which must come back `sat` with a counterexample naming the
offending step. Verified by mutation: making the ordering encoding contradictory
fails all three `CATCHES` cases while the rest stay green.

`proveTaint` is expected to hold for every plan the schema admits, because
`argument_forms` is `["literal"]` and there is nothing to taint from. A check
that is green forever carries no signal, so it takes a synthetic `bindings`
argument the schema can never produce, and the corpus uses it to assert `sat`
with the expected leaking path — including one laundered through an intermediate
step, since transitivity is the part most easily got wrong. ADR-0004 requires
this: the prover is tested beyond what the policy admits.

## Configuring the policy

`policy.json` ships fail-closed: `link_host_allowlist` is **empty**, so every
link in a finding rejects until you name the hosts you trust. Link-free reviews
work untouched — the common case is unaffected — but a consumer that wants
findings to link to its own docs has to say so:

```json
"link_host_allowlist": ["docs.example.com", "github.com/your-org/"]
```

A trailing slash means prefix match; a path-less entry matches that host and
everything beneath it. Never list a host you do not control: an allowlisted host
is somewhere a compromised generator is permitted to point a maintainer.

## Remaining coupling from the extraction

Tracked in ADR-0002. Both of the original dependency/policy items are resolved —
the Powertools hosts are out of the shipped policy (`tests/test_policy_defaults.py`
holds that line), and `boto3` is gone. `post.py`'s hardcoded `BOT_LOGIN` is
resolved too: the executor now asks the write token who it is (GraphQL
`viewer { login }`, the one identity call every token type answers — REST
`/user` is 403 for the installation tokens Actions issues) and fails closed if
it can't. It is a security property, not configuration: the comment marker is
copyable, so ownership needs the author, and the author must come from the
credential in hand.

The prompt's project description is a runtime substitution now, too: set
`SMTITHY_PROJECT_DESCRIPTION` and the one project-naming clause in the prompt
is swapped for the consumer's own text (`artifact.apply_project_description`).
Unset, the assembled prompt is byte-identical to the measured default — prompt
edits are measured changes, and this seam is built so the default never needs
re-measuring. A supplied description that cannot land raises rather than
silently reviewing under the wrong project identity.

The test fixtures still use Powertools hostnames and paths, deliberately. The
adversarial corpus's near-misses are near-misses *of those exact strings*, so
`tests/conftest.py` re-injects the two hosts the corpus was calibrated against.

## Eval fixtures: two kinds, do not confuse them

The eval scenarios feed the generator two directories, and the difference
matters — conflating them is how a scenario ends up grading nothing.

**PR HEAD (`pr_root/`)** is the content under review. These files are
hand-reduced and carry **deliberately planted defects**: `caller_impact`'s
`slice_dictionary` yields `i + chunk_size - 1` where real upstream has
`i + chunk_size`. The bug *is* the fixture. Never regenerate these from real
source — that removes the very defect the scenario grades.

**BASE** is the trusted pre-change tree the model may read with Read/Grep/Glob.
It used to be the whole enclosing checkout, which is what tied the suite to one
repository. It is now declared per scenario in `base.json` and fetched from a
pinned commit:

```json
{"repo": "owner/name", "sha": "<40 hex>", "paths": ["pkg/mod.py"]}
```

Ten of the eleven scenarios declare none and get an **empty** BASE — stricter
than before, since a scenario that accidentally leaned on unrelated repository
content now fails instead of passing for an undeclared reason. Only
`caller_impact_needs_investigation` needs one, because it grades whether the
model went looking for a real *caller* rather than pattern-matching the diff.

A full 40-character SHA is required, never a branch or tag: `expect.json` grades
an exact line number, and a moving ref would let the premise drift out from under
it. Fixtures cache under `.eval-base-cache/` (gitignored — the declaration is the
source of truth). The two premise checks that read fetched content skip unless
the cache is populated or `SMTITHY_FETCH_FIXTURES=1` is set, so the
deterministic suite makes no network calls.
