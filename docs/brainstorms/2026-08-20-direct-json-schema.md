---
date: 2026-08-20
topic: direct-json-schema
---

# Direct JSON Schema

## What We're Building

Replace Aceiro's custom structural schema vocabulary with checked-in JSON
Schema Draft 2020-12 documents for review artifacts and remediation plans.
The model contract and deterministic verifier will read the same schema files
directly.

`policy.json` remains the configuration for contextual safety checks that JSON
Schema cannot express from the candidate object alone: Markdown rendering,
link destinations, secret scanning, diff provenance, plan ordering,
containment, effect classification, and aggregate change budgets.

## Why This Approach

Aceiro already translates its custom vocabulary into JSON Schema. Storing the
result directly removes a proprietary translation layer and lets standard JSON
Schema tooling inspect the public artifact and plan contracts.

We are not adding CEL, Cedar, Rego, a custom JSON Schema vocabulary, or custom
keywords. Contextual checks remain named verifier phases rather than being
disguised as schema validation.

## Key Decisions

- Use Draft 2020-12 schemas without custom keywords.
- Keep review and plan schemas in separate checked-in files.
- Derive structural facts such as finding limits, severities, step kinds, and
  argument shapes from the schemas.
- List Markdown-bearing fields explicitly in `policy.json`.
- Keep `max_distinct_groups` as a review semantic constraint in `policy.json`.
- Keep write classification and every contextual plan rule in `policy.json`.
- Hash the effective policy and both schemas together for run and posting
  provenance.
- Preserve whole-object, fail-closed validation and typed plan parsing.

## Open Questions

- None for the initial migration.

## Next Steps

Implement the migration with behavior-preserving regression coverage.
