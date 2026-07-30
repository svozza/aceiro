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

Still to arrive: the eval workflow, context acquisition, rendering, and GitHub I/O.

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
holds that line), and `boto3` is gone. What is left arrives with the code that
carries it:

- `post.py`'s hardcoded `BOT_LOGIN`, which becomes runtime-resolved from the
  credential in hand rather than a config input. It is a security property: the
  comment marker is copyable, so ownership needs the author too.
- The generator prompt's project description.

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
