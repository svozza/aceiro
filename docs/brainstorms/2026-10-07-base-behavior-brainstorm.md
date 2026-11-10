---
date: 2026-10-07
topic: base-behavior
status: approved-prototype-and-trial-in-progress
---

# Derive behavior before exposing the patch

## What to test

The user wants a departure from recent source-navigation and guard-selection experiments. Test whether a reviewer finds different regressions when its starting questions come from existing workflows, described without access to the patch. First build a small catalog of concrete BASE behaviors; then give that catalog to the ordinary BASE/HEAD reviewer. This changes information exposure and task order, rather than adding another annotation or finding critic.

Each catalog entry describes an input/configuration, an entrypoint, the observable BASE behavior, source citations and unresolved assumptions. These are source-supported descriptions, not mandatory compatibility rules, verified execution or proposed defects. The final reviewer must check both versions and the PR's declared intent; deliberate changes are not bugs merely because they differ from BASE. Ordinary independent discovery remains available.

## Why this departure

The source-packet study posted 5/9 fresh runtime targets in every arm. Exit-focused discovery tied fresh production recovery with generic discovery and ordinary review while substantially increasing cost. Earlier independent investigation mostly assessed concerns already proposed by a diff-visible discovery session. Those designs leave the initial interpretation of the change largely shared.

| Approach | Question it answers | Main risk |
| --- | --- | --- |
| BASE-only behavior catalog, recommended | Does deriving concrete old workflows before seeing the patch reveal otherwise missed regressions? | Treating historical behavior as required compatibility; catalog may miss relevant workflows |
| Independent complete reviews | Are current misses mostly sample variance, and are reviewers complementary? | Repeated correlated misses; combining findings increases alarms and inference cost |
| Separate BASE and HEAD summaries, then reconciliation | Does independent version interpretation improve comparison? | Three-stage cost and reconciliation mistakes; too many moving parts for the first test |

A BASE/HEAD-isolation idea appeared as an unimplemented alternative in the October 4 hypothesis brainstorm. This first experiment would test BASE-only question formation, not claim checking or a whole-program equivalence system.

## Proposed comparison

Use three arms on six fresh production PRs from unused repositories:

1. Ordinary full review.
2. A behavior catalog generated with BASE, HEAD and diff visible, followed by ordinary review.
3. The same catalog task and submission shape with only BASE visible, followed by ordinary review.

Arms 2 and 3 control for another model call and structured behavior descriptions. They differ in first-stage information exposure. Same ordinary final system/schema/verifier, same reviewer tools and model, equal total configured time/turn allowances. Reuse the existing 900-second shared generator allowance and 45 total turns: ordinary 45, catalog 15 plus final 30. The catalog phase has a 300-second cap. Record all actual inference cost, preparation overhead and failures; no claim that equal limits imply equal compute.

Keep at most six concise scenarios. Both catalog arms receive the same sorted changed production paths for scope, without changed line ranges; the BASE-only arm sees no HEAD bytes, diff, PR title/body, golden labels, previous findings or other session history. Thus it is blind to patch content, not to affected filenames. New files absent from BASE remain explicitly unavailable; no inferred prior implementation. The final reviewer receives the ordinary complete inputs and catalog as untrusted evidence, not a checklist it must endorse.

Select sources by frozen seeded language/size/history rules without parser-card or target eligibility filters. Include empty catalogs and PRs with no established target. Never replace a case because it is unhelpful. Fetch fresh goldens only after all planned reviews complete. Six PRs produce 18 final reviews and 12 catalog sessions if all stages start; this is a proposed bounded screen, not a launch receipt.

## Implementation and security boundaries

Inspection of `cc_loop.build_options` shows readonly access is scoped through cwd/add_dirs, and ordinary preparation already contains BASE, HEAD and PR context. Reusing its ordinary user message in the catalog stage would invalidate blindness. Construct a distinct redacted BASE-only workspace and initial message, use it for both cwd and source-root parameters, and omit HEAD/context/output evidence directories from allowed source roots. Retain sanitized prompt/options/native receipts and exposure hashes. No shared or resumed conversation.

Before paid inference, prove BASE access and HEAD/diff/context denial with the actual runtime, including sibling/absolute-path attempts. An instruction to ignore HEAD is insufficient. If existing access controls cannot enforce this, isolate the filesystem further or stop this avenue with an explicit feasibility result. Do not weaken the source-access policy to make it work.

Use existing Read/Grep/Glob plus a fixed catalog-submission tool; no repository execution, test/build commands, Python probes, hooks, plugins, project configuration, assemblies or new LSP/library capability. Trusted operator harness code validates entry shape, bounds, citation membership and redaction. It does not certify behavior truth. The final reviewer keeps ordinary tools and submission. Catalog failure falls back within remaining shared budget and stays recorded, with no replacement attempt.

## What would count as useful

Measure source-supported introduced runtime findings posted in the final artifact, separate from residual awareness, coverage advice and novel test-harness observations. Audit every output's qualified alarms, factual/supporting errors, intentional-change accusations and repairs. Trace whether a catalog entry supplied a relevant workflow later reflected in a finding, without inspecting hidden reasoning. Catalog delivery and matching wording do not establish causal reliance.

The BASE-only arm needs a meaningful advantage over both ordinary and diff-visible catalogs, with acceptable alarm/cost tradeoffs. A tie or a gain confined to residual wording does not support rollout. Compare per-PR results; one run per arm is exploratory, not a statistical estimate. Audit all fresh original benchmark labels without relabeling history or declaring an official benchmark score.

## Current state and next step

The user chose this direction with “go”. Candidate f98a95f is implemented and passes 2,423 trusted tests/two skips plus type checks. Native testing found cwd/add_dirs alone allowed cross-root source reads; explicit trusted path hooks now deny the hidden canaries. All experiment final-review arms receive this same restriction. Default production remains unchanged.

Six fresh production sources were selected without AST/behavior/target screens. The unused production pool supports one Python, three TypeScript and two Go cases; initial Python-quota exhaustion and all exclusions are retained. First-stage visible catalogs receive HEAD/diff but no PR title/body, matching the blind catalog's exclusion of intent text; final review receives ordinary complete context.

The initial frozen receipt wrapper had a two-versus-three-argument MCP helper mismatch, caught before inference. Its setup remains retained. Corrected freeze is `/home/ec2-user/aceiro-eval-runs/base-behavior-corrected-20261007`. Full offline/native preflights are running before the 18-review/12-catalog matched trial. See [protocol](../../handoff/base-behavior-20261007/PROTOCOL.md) and [continuation](../../handoff/base-behavior-20261007/CONTINUE.md). No main/Rito rollout or automatic follow-on.
