---
date: 2026-10-02
topic: example-content-versus-presence
---

# Follow the example-content signal

## Question

Does the unsupported timeout example teach false regression attribution, while
having a worked example helps maintain coverage? The deletion trial observed
fewer false structured claims (3 to 1), but known delivery fell by one (20/24 to
19/24). Retain its rejection decision. All focused unchanged-backoff cases still
passed, so complete loss of that capability was not observed.

## Evidence audit

The extra Office365 loss is one discordant paired observation: control-only in
r1, both found in r2, both missed in r3. Every Cal.com input includes Office365
in the initial diff. None of the six sessions issues an Office365-specific
source request, including those that find it. The quota is unsaturated on
misses and no cutoff occurs. Tool traces do not establish that deletion caused
the omission or that a missing direct read explains it.

Unsupported old-caller race attribution appears in every Grafana control
(structured once, residual twice), versus candidate residual once. Two paired
observations favor deletion on this question. The unchanged TotalDocs target
is retained in all six outputs. These are few observations of reused snapshots,
not a general accuracy estimate.

## Approaches

1. Recommended: a fresh three-way diagnostic compares shipped timeout example,
   deletion, and the already prepared correctly wired buffer example. This tests
   content versus presence while preserving all other prompt/policy/tool values.
2. A fresh larger shipped-versus-deletion replication tests the original signal
   more directly, but cannot separate the value of example presence from content.

## Proposed bounded study

Three fresh repetitions on Cal.com, Grafana and rboto across three arms: 27 full
reviews. Those public sources contain the coverage loss, the attribution signal,
and the unrelated S3 error. Three focused scenarios repeated three times per
arm add 27 samples: real caller investigation, unchanged-backoff positive and
clean helper. Latin rotation balances source and arm order. Existing labels,
source qualifications, model, effort, budgets and shipped grader remain fixed.

No selective retries, extra sampling, modifications within the study, or
historical samples counted as new controls. Primary comparisons are supported
versus shipped and supported versus deletion; deletion versus shipped is the
fresh replication. Interpret precision only while preserving core delivery and
positive focused behavior; include residual/supporting errors and repair quality.
A favorable diagnostic would need a separate broader confirmation decision.
Rito deployment remains deferred; no deployment or PR is part of this research.

## Current state

Prior traces audited without model calls. The supported example is reapplied
alone on `5391472` as `f940f63`; deterministic validation passed (2,400 tests and type checking).
No preference reply arrived during preparation; the recommended three-way
public diagnostic was frozen and started under the existing research request. The previous rejection decisions and immutable evidence remain intact.
