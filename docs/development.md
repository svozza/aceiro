# Developing Smtithy

This guide covers local setup, deterministic tests, type checking, dependency
updates, and model evaluation commands. See
[Testing Strategy](testing.md) for when to use each test layer and how to
investigate failures.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Git

The repository uses:

- `uv` to install Python and synchronize the locked environment;
- `pyproject.toml` for direct dependencies and dependency groups;
- `uv.lock` for the complete reproducible resolution;
- `pytest` and Hypothesis for deterministic tests;
- `ty` for static type checking.

Model evaluations additionally require one of the supported model credentials:
a long-lived Bedrock API key, ordinary AWS credentials that can invoke Bedrock,
or a direct Anthropic API key. They are not part of the unit-test setup.

## Set up the environment

From the repository root:

```bash
uv python install 3.13
uv sync --frozen --all-groups --no-install-project
```

The dependency groups have separate responsibilities:

| Group | Purpose |
| --- | --- |
| Project dependencies | Runtime and generator dependencies |
| `test` | `pytest`, Hypothesis, and test dependencies |
| `typecheck` | The `ty` type checker |

## Run the tests

Run the complete deterministic suite:

```bash
uv run --frozen --group test \
  python -m pytest tests/ -p no:cacheprovider -q
```

This is the same test command used by the quality-check workflow. The suite
blocks external network access. A small group of GitHub redirect tests uses a
local loopback HTTP server.

Common focused commands:

```bash
# One test file
uv run --frozen --group test \
  python -m pytest tests/test_verify_cli.py -q

# One test function
uv run --frozen --group test \
  python -m pytest tests/test_verify_cli.py::test_valid_artifact_exits_zero -q

# Tests whose names match an expression
uv run --frozen --group test \
  python -m pytest tests/ -k "link and allowlist" -q

# Show stdout and stderr while debugging
uv run --frozen --group test \
  python -m pytest tests/test_verify_cli.py -s -vv
```

Most tests need no credentials, internet access, or external services. Tests
that inspect a fetched evaluation fixture skip unless its local cache exists or
`SMTITHY_FETCH_FIXTURES=1` is set.

See [Testing Strategy](testing.md#test-layers) for what the deterministic suite
does and does not establish.

## Run the type checker

```bash
uv run --frozen --group typecheck \
  ty check --python .venv/bin/python
```

`ty.toml` limits checking to the harness under `src/smtithy` and excludes the
deliberately incomplete code stored inside evaluation scenarios.

Run tests and type checking before opening a pull request:

```bash
uv run --frozen --group test \
  python -m pytest tests/ -p no:cacheprovider -q
uv run --frozen --group typecheck \
  ty check --python .venv/bin/python
```

## Update dependencies

Direct dependencies are exact pins in `pyproject.toml`. Edit the applicable
entry under `project.dependencies` or `dependency-groups`, then update the lock
and environment:

```bash
uv lock --upgrade-package <package>
uv sync --all-groups --no-install-project
```

Review both `pyproject.toml` and `uv.lock`, then run the deterministic checks.
Do not edit `uv.lock` manually.

Dependency changes can alter security-sensitive behavior. In particular:

- changes to `markdown-it-py`, `jsonschema`, or `detect-secrets` require the
  verifier and adversarial tests;
- changes to `claude-agent-sdk` can change generator behavior and require model
  evaluations in addition to deterministic tests;
- changes to `ty` require a clean type-check run.

## Run model evaluations

Evaluations make real model calls and may incur provider charges. Use them for
prompt, policy, verifier, generator, or model changes rather than as the normal
unit-test loop. See [Testing Strategy](testing.md#one-run-versus-repeated-runs)
for run-count guidance and [Interpreting failures](testing.md#interpreting-failures)
before treating a rerun as evidence.

Set the common evaluation environment:

```bash
export ANTHROPIC_MODEL=global.anthropic.claude-opus-4-8
export SMTITHY_EVAL_JUDGE_MODEL=global.anthropic.claude-opus-4-8
export DISABLE_TELEMETRY=1
export DISABLE_ERROR_REPORTING=1
export DISABLE_AUTOUPDATER=1
export CLAUDE_CONFIG_DIR=/tmp/smtithy-claude-config
```

Then choose one authentication method.

For a long-lived Bedrock API key:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=eu-west-1
export AWS_BEARER_TOKEN_BEDROCK=<bedrock-api-key>
```

For ordinary AWS credentials, including an AWS profile:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=eu-west-1
export AWS_PROFILE=<profile>
```

The credentials must allow invocation of the profiles selected by
`ANTHROPIC_MODEL` and `SMTITHY_EVAL_JUDGE_MODEL`.

For the direct Anthropic API:

```bash
unset CLAUDE_CODE_USE_BEDROCK
export ANTHROPIC_API_KEY=<anthropic-api-key>
export ANTHROPIC_MODEL=<anthropic-model>
export SMTITHY_EVAL_JUDGE_MODEL=<anthropic-model>
```

`SMTITHY_EVAL_JUDGE_MODEL` is used only by review scenarios that opt into
semantic compliance grading and only when a configured marker appears. The
default is the Bedrock Opus inference profile shown above. Set it explicitly to
a valid first-party model name for direct Anthropic runs.

Run the review scenarios:

```bash
uv run --frozen \
  python src/smtithy/evals/run_evals.py \
  --output-dir /tmp/smtithy-review-evals \
  --cache-dir /tmp/smtithy-eval-cache \
  --runs 1 \
  --workers 4
```

Run the remediation-plan scenarios:

```bash
uv run --frozen \
  python src/smtithy/evals/run_plan_evals.py \
  --output-dir /tmp/smtithy-plan-evals \
  --runs 1 \
  --workers 4
```

Useful options:

- `--scenario NAME` runs one review scenario while iterating;
- `--workers N` controls concurrent model sessions;
- `--runs 1` checks whether the harness works once;
- `--runs 3` checks stability by requiring every scenario to pass every run.

Each output directory contains the graded results, submitted artifact when one
was produced, complete redacted harness transcript, and complete redacted model
stream for every attempt:

- `results.json` contains the scenario verdicts;
- `<scenario>/review.json` or `<scenario>/plan.json` is the submitted artifact;
- `<scenario>/transcript.jsonl` is the harness event transcript;
- `<scenario>/cc_stream_<attempt>.jsonl` is the captured model stream for that
  attempt;
- `<scenario>/run_metadata.json` records model/session metadata when produced;
- `<scenario>/semantic_judge.json` records semantic-compliance adjudication when
  invoked.

CI uploads both the review and plan output trees even when a step fails. The
Actions artifact is named `evals-<run_id>` and retained for 30 days. Download it
from the workflow run page or with:

```bash
gh run download <run-id> \
  --name evals-<run-id> \
  --dir /tmp/smtithy-eval-artifact
```

Transcripts and streams are redacted before being written, but they still
contain ordinary reviewed source content and may contain secrets that detection
does not recognize. See the testing strategy's
[redaction boundary](testing.md#redaction-boundary) before sharing an artifact.

## Repository map

| Path | Contents |
| --- | --- |
| `src/smtithy/` | Reviewer, verifier, remediation, and GitHub integration code |
| `src/smtithy/evals/` | Evaluation runners and fixed scenarios |
| `tests/` | Deterministic unit, property, adversarial, and workflow-shape tests |
| `prompts/` | Model prompts |
| `.github/workflows/` | CI and reusable GitHub Actions workflows |
| `docs/adr/` | Architecture decisions and their rationale |
| `docs/findings/` | Research and experiment reports |
| `results/` | Redacted experiment records owned by this repository |

## CI checks

The `Quality check` workflow runs on pull requests and on pushes to `main`:

```bash
uv run --frozen --group test \
  python -m pytest tests/ -p no:cacheprovider -q
uv run --frozen --group typecheck \
  ty check --python .venv/bin/python
```

The `Evals` workflow is separate because it runs pull-request code with a live
model credential. Do not add credentials or external-service calls to the
deterministic quality-check workflow.
