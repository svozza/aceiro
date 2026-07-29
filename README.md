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

Being extracted from a consuming repository, in sequence. What is here now is
the artifact verifier and its tests, moved behaviour-preserving:

| | |
| --- | --- |
| `src/smtithy/verify.py` | the verifier — the security boundary. Interprets `policy.json`; allowlists a safe grammar and rejects the whole artifact otherwise. |
| `src/smtithy/artifact.py` | fence escaping, the Unicode default-ignorable table, secret redaction. |
| `src/smtithy/diff_map.py` | the ONE diff parser. Verification owns the walk. |
| `src/smtithy/policy.json` | the declarative policy — the reviewable security object, hashed into the transcript. |
| `tests/` | goldens, hypothesis properties, and a 486-line adversarial corpus that is the executable spec of the threat model. |

Still to arrive: the eval suite, context acquisition, the generator loop,
rendering, GitHub I/O, and the plan prover.

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

Two test runners are expected here eventually — pytest for the Python verifier
and a TypeScript runner for the plan prover — until the verifier is ported last,
behind a differential oracle. ADR-0003 records the method.

## Known coupling, not yet removed

Inherited from the repository this was extracted from, and tracked in ADR-0002:

- `policy.json`'s `link_host_allowlist` names Powertools hosts.
- `requirements.txt` still pins `boto3`, which no remaining module imports —
  the Bedrock loop it served was replaced by the Claude Code CLI upstream.
