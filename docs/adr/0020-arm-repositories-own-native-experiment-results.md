# Arm repositories own native experiment results

Date: 2026-08-18

## Decision

Each evaluation arm owns the durable, redacted record of its native runs.
Smtithy owns the canonical fixture definitions, the shared result schema, and
cross-arm aggregation.

An arm result records observations and provenance. It does not declare that the
arm is secure, superior, or equivalent to another arm. Comparative rates and
conclusions belong to the central report.

## Repository responsibilities

The arm repository stores:

- `results/index.json`, an append-only inventory of committed result records;
- `results/experiments/<experiment>/<cohort>.json`, validated against
  `src/smtithy/evals/arm_result.schema.json`;
- exact harness, fixture-source, model, and GitHub run identifiers;
- hashes and names for external redacted artifacts;
- explicit scored, excluded, and structural-N/A cells;
- `supersedes` links rather than destructive replacement of historical results.

Large transcripts and raw model artifacts remain external. They must be
redacted, retained outside the Git tree, and referenced by immutable run ID and
SHA-256.

Smtithy stores:

- canonical fixture bytes and arm-neutral useful-work oracles;
- the shared result schema;
- a lock file pinning the result commit consumed from each arm;
- experiment-centric comparison documents and generated reports.

## Trust boundary

An arm's summary is not trusted as a cross-arm conclusion. The central
aggregator validates the shared schema, checks internal count invariants,
verifies referenced hashes when artifacts are available, and derives
comparison tables from per-cell dimensions.

Security, review quality, capability, and run validity remain separate
dimensions. Excluded and structural-N/A cells are never converted into passes
or failures.

## Discoverability

Every arm repository must point to this ADR from its `results/README.md`.
Smtithy's `CONTEXT.md` points future agents to the schema and ownership rule.

## Consequences

- Results survive GitHub Actions artifact expiry in a compact, redacted form.
- Native evidence remains next to the harness that produced it.
- The central HTML report is reproducible from pinned arm result commits.
- Updating an arm does not require manually editing one monolithic findings
  document.
- Schema changes require a version bump and explicit migration of consumers.
